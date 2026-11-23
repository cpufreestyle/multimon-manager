# -*- coding: utf-8 -*-
"""防休眠：让电脑在合盖、拔电、锁屏时也继续干活（复刻 LayoutKeeper 的 KeepAwake）。

LayoutKeeper 的 KeepAwakeMode 分四档，本项目先做最实用的三档 + 一档高风险档：

  off      关闭：不持任何断言，完全交给系统（默认）
  systemOnly  息屏不休眠：屏幕可以黑，系统不睡
  display     常亮不休眠：屏幕也不许黑
  clamshell   合盖断电也不休眠（**高风险**，见下）

实现上完全复刻它的 SystemSleepGuard：
1. **IOKit 电源断言**。`IOPMAssertionCreateWithName` 持
   PreventUserIdleSystemSleep / PreventSystemSleep / PreventUserIdleDisplaySleep
   三种。与 `caffeinate` 的区别：caffeinate 只在进程活着时有效且不带
   PreventSystemSleep，拔电合盖时内核照样睡；IOKit 断言是 powerd 认的。
2. **合盖状态**读 `IOPMrootDomain` 的 `AppleClamshellState`。它决定
   `.systemOnly` 是否要升级持 display 断言（合盖时 macOS 会在外接屏关闭 5 秒后
   强制进入 Clamshell Sleep，即使 `sleep = 0`）。
3. **clamshell 档**还要 `pmset -a disablesleep 1`（系统级，需要管理员授权）。
   这是唯一会**改变系统全局状态**的动作，因此：
   - 只在用户主动开这一档时才做；
   - 记下「是本程序设的」，退出时若仍是本程序设的就还原成 0，
     绝不把用户系统永久改成不休眠（LayoutKeeper 用 ownsSleepDisabled 记这件事）；
   - 授权走 osascript（`with administrator privileges`），不用 sudo -n 静默试探，
     因为 sudoers 免密规则要写 /etc/sudoers.d，那比弹一次授权框更越界。
4. **读回校验**。设完用 `pmset -g assertions` / `IOPMCopyAssertionsByProcess` 与
   `SleepDisabled` 属性核对，不满足就自愈重试一次，仍不满足就如实报告
   「没生效，可能原因是什么」——本项目一贯的「不准要说出物理原因」。

边界：本模块不碰任何窗口/显示器逻辑；非 macOS 上全部退化为 no-op 并返回
「当前平台不支持」，与 display_notify.py 的做法一致。

Windows 侧用等价机制实现同一套四档语义：
1. `SetThreadExecutionState`（ES_CONTINUOUS + ES_SYSTEM_REQUIRED /
   ES_DISPLAY_REQUIRED）——效果挂在调用线程上，线程一退就失效，所以由
   专用后台线程持有，而不是在 Tk 主线程随手设一次。
2. 「合盖断电也不眠」把当前电源计划的合盖动作（LIDACTION）改成 0
   （不采取任何操作，powercfg 写入，需管理员授权），这是 Windows 上与
   `pmset -a disablesleep 1` 最接近的等价物；原值记下，退出时还原。
3. 读回校验同样以系统状态为准：写完用 PowerReadAC/DCValueIndex 核对，
   与 macOS 侧「不准要说出物理原因」的规矩一致。
"""
import ctypes
import threading
import os
import subprocess
import sys
import uuid
import time

IS_MAC = (sys.platform == "darwin")
IS_WIN = (sys.platform == "win32")

# 四档模式（显示名 -> 内部键），顺序即界面下拉顺序
MODE_OFF = "off"
MODE_SYSTEM = "system"        # 息屏不休眠
MODE_DISPLAY = "display"      # 常亮不休眠
MODE_CLAMSHELL = "clamshell"  # 合盖断电也不休眠
MODES = (MODE_OFF, MODE_SYSTEM, MODE_DISPLAY, MODE_CLAMSHELL)
DEFAULT_MODE = MODE_OFF

MODE_LABELS = {
    MODE_OFF: "关闭",
    MODE_SYSTEM: "息屏不休眠",
    MODE_DISPLAY: "常亮不休眠",
    MODE_CLAMSHELL: "合盖断电也不眠",
}
MODE_HINTS = {
    MODE_OFF: "不持任何断言，休眠行为完全交给系统。",
    MODE_SYSTEM: "屏幕可以自动黑屏，系统不进入睡眠。适合挂下载、跑长任务。",
    MODE_DISPLAY: "屏幕也不黑屏。适合当看板、监控大屏。",
    MODE_CLAMSHELL: "合上盖子、拔掉电源也不睡（需管理员授权）。装包发布前请关闭。",
}

_KEY = "keep_awake_mode"
_OWNED_KEY = "keep_awake_owns_disablesleep"

_kIOPMAssertionLevelOn = 255

# Windows 执行状态标志（kernel32.SetThreadExecutionState）
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002
_WIN_FLAG_NAMES = {
    _ES_SYSTEM_REQUIRED: "系统不休眠",
    _ES_DISPLAY_REQUIRED: "屏幕常亮",
}

# 合盖动作：SUB_BUTTONS\LIDACTION，值 0=不采取任何操作 1=睡眠 2=休眠 3=关机
_LID_SUB = "4f971e89-eebd-4455-a8de-9e59040e7347"
_LID_SETTING = "5ca83367-6e45-459f-a27b-076b2d883706"
_LID_ACTION_NAMES = {0: "不采取任何操作", 1: "睡眠", 2: "休眠", 3: "关机"}

# 断言类型名（IOKit）
_A_IDLE_SYSTEM = "PreventUserIdleSystemSleep"
_A_SYSTEM = "PreventSystemSleep"
_A_IDLE_DISPLAY = "PreventUserIdleDisplaySleep"


def lid_action_name(v):
    """合盖动作数值 → 中文名（界面状态行用）。"""
    return _LID_ACTION_NAMES.get(int(v), "未知(%s)" % v)


# ── Windows：电源计划读取 / 执行状态持有 ──────────────────────────────

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s):
    b = uuid.UUID(s).bytes_le
    g = _GUID()
    g.Data1 = int.from_bytes(b[0:4], "little")
    g.Data2 = int.from_bytes(b[4:6], "little")
    g.Data3 = int.from_bytes(b[6:8], "little")
    g.Data4 = (ctypes.c_ubyte * 8)(*b[8:16])
    return g


_win_powr_set = False


def _win_powr():
    """powrprof 的 argtypes 只装一次；失败（非 Windows）返回 None。"""
    global _win_powr_set
    if sys.platform != "win32":
        return None
    p = ctypes.windll.powrprof
    if not _win_powr_set:
        _win_powr_set = True
        p.PowerGetActiveScheme.restype = ctypes.c_ulong
        p.PowerGetActiveScheme.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_GUID))]
        for fn in ("PowerReadACValueIndex", "PowerReadDCValueIndex"):
            getattr(p, fn).restype = ctypes.c_ulong
            getattr(p, fn).argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(_GUID),
                ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_ulong)]
    return p


def _lid_state():
    """当前电源计划的合盖动作 (AC, DC)；读不到（台式机/不支持）返回 None。"""
    p = _win_powr()
    if not p:
        return None
    try:
        scheme = ctypes.POINTER(_GUID)()
        if p.PowerGetActiveScheme(None, ctypes.byref(scheme)) != 0:
            return None
        try:
            sub, setting = _guid(_LID_SUB), _guid(_LID_SETTING)
            ac, dc = ctypes.c_ulong(), ctypes.c_ulong()
            rc_ac = p.PowerReadACValueIndex(
                None, scheme, ctypes.byref(sub), ctypes.byref(setting),
                ctypes.byref(ac))
            rc_dc = p.PowerReadDCValueIndex(
                None, scheme, ctypes.byref(sub), ctypes.byref(setting),
                ctypes.byref(dc))
            if rc_ac != 0 or rc_dc != 0:
                return None
            return (int(ac.value), int(dc.value))
        finally:
            ctypes.windll.kernel32.LocalFree(scheme)
    except Exception:  # noqa: BLE001
        return None


def _win_flags_for(assertion_types):
    """把共享计划里的断言名映射成 Windows 执行状态标志。"""
    flags = 0
    for a in assertion_types or ():
        if a in (_A_IDLE_SYSTEM, _A_SYSTEM):
            flags |= _ES_SYSTEM_REQUIRED
        elif a == _A_IDLE_DISPLAY:
            flags |= _ES_DISPLAY_REQUIRED
    return flags


class _WinExecutionHolder:
    """在专用线程里持 ES_CONTINUOUS 标志。

    SetThreadExecutionState 的效果挂在调用线程上：线程退出（或被再次调用
    清掉标志）系统就恢复原来的休眠计时。这里用代数（generation）标记目标
    集合的变化，apply 后置 applied 事件供调用方等待，stop() 清标志并收线程。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._want = 0
        self._gen = 0
        self._ok = False
        self._applied = threading.Event()
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def set(self, flags):
        with self._lock:
            self._want = int(flags) & 0xFFFFFFFF
            self._gen += 1
        self._applied.clear()
        self._wakeup.set()
        self._ensure_thread()
        self._applied.wait(timeout=3.0)
        with self._lock:
            return self._ok

    def current_names(self):
        with self._lock:
            ok = self._ok
            want = self._want
        if not ok:
            return []
        return [n for f, n in _WIN_FLAG_NAMES.items() if want & f]

    def stop(self):
        self._stop.set()
        self._wakeup.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5.0)
        self._thread = None
        with self._lock:
            self._want = 0
            self._ok = False

    def _ensure_thread(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="keep-awake-win", daemon=True)
            self._thread.start()

    def _run(self):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetThreadExecutionState.restype = ctypes.c_ulong
        k32.SetThreadExecutionState.argtypes = [ctypes.c_ulong]
        seen_gen = -1
        while not self._stop.is_set():
            self._wakeup.wait(timeout=30.0)   # 30s 是兜底，主要靠 set() 唤醒
            self._wakeup.clear()
            if self._stop.is_set():
                break
            with self._lock:
                want, gen = self._want, self._gen
            if gen == seen_gen:
                continue
            prev = k32.SetThreadExecutionState(_ES_CONTINUOUS | want)
            with self._lock:
                self._ok = bool(prev)
                seen_gen = gen
            self._applied.set()
        # 线程退出即失去效果；主动清一次更干净
        k32.SetThreadExecutionState(_ES_CONTINUOUS)
        with self._lock:
            self._ok = False


# ── ctypes 装配 ────────────────────────────────────────────────────────

_iokit = None
_cf = None
_loaded = False


def _load():
    """加载 IOKit + CoreFoundation（仅 macOS）。失败则本模块整体退化为 no-op。"""
    global _iokit, _cf, _loaded
    if _loaded:
        return _iokit is not None and _cf is not None
    _loaded = True
    if not IS_MAC:
        return False
    try:
        _iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        _cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        _iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int32
        _iokit.IOPMAssertionCreateWithName.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32)]
        _iokit.IOPMAssertionRelease.restype = ctypes.c_int32
        _iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
        _iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
        _iokit.IOServiceGetMatchingService.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p]
        _iokit.IOServiceNameMatching.restype = ctypes.c_void_p
        _iokit.IOServiceNameMatching.argtypes = [ctypes.c_char_p]
        _iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
        _iokit.IORegistryEntryCreateCFProperty.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        _iokit.IOObjectRelease.restype = ctypes.c_int32
        _iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
        _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        _cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        _cf.CFGetTypeID.restype = ctypes.c_ulong
        _cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        _cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        _cf.CFBooleanGetValue.restype = ctypes.c_bool
        _cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        _cf.CFNumberGetTypeID.restype = ctypes.c_ulong
        _cf.CFNumberGetValue.restype = ctypes.c_bool
        _cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_long)]
        return True
    except Exception:  # noqa: BLE001
        _iokit = None
        _cf = None
        return False


def _cfstr(text):
    if not _cf:
        return None
    return _cf.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100)


def _cf_bool(ref):
    """CFProperty -> bool / None。取不到（键不存在）返回 None，与 false 区分开。"""
    if not _cf or not ref:
        return None
    if _cf.CFGetTypeID(ctypes.c_void_p(ref)) == _cf.CFBooleanGetTypeID():
        return bool(_cf.CFBooleanGetValue(ctypes.c_void_p(ref)))
    # 有些固件把布尔写成 NSNumber
    if _cf.CFGetTypeID(ctypes.c_void_p(ref)) == _cf.CFNumberGetTypeID():
        out = ctypes.c_long(0)
        if _cf.CFNumberGetValue(ctypes.c_void_p(ref), 3,
                                ctypes.byref(out)):   # kCFNumberLongType
            return bool(out.value)
    return None


# ── 纯函数：计划与判据（LayoutKeeper 的 KeepAwakePolicy）──────────────

def plan(mode, clamshell=False):
    """模式 + 是否合盖 → 要持哪些断言、是否要改系统 disablesleep。

    复刻 LayoutKeeper 的 KeepAwakePolicy.plan：
    - `.systemOnly` 在合盖时**自动升级**持 display 断言——合盖后 macOS 会在外接屏
      关闭 5 秒强制进 Clamshell Sleep，即使 `sleep = 0`，只有 display 断言能挡住。
    - `.clamshellUnplugged` 额外置系统级 `SleepDisabled`（pmset -a disablesleep 1）。
    """
    if mode not in MODES:
        mode = DEFAULT_MODE
    if mode == MODE_OFF:
        return {"active": False, "assertions": (), "disable_clamshell_sleep": False}
    if mode == MODE_SYSTEM:
        # 未合盖时不要塞空元组进断言列表：调用方 set(...) 后会多出一个 ()
        # 元素，判据与展示都要额外滤一遍，平白添出错的机会。
        base = (_A_IDLE_SYSTEM, _A_SYSTEM)
        return {
            "active": True,
            "assertions": base + ((_A_IDLE_DISPLAY,) if clamshell else ()),
            "disable_clamshell_sleep": False,
        }
    if mode == MODE_DISPLAY:
        return {
            "active": True,
            "assertions": (_A_IDLE_SYSTEM, _A_SYSTEM, _A_IDLE_DISPLAY),
            "disable_clamshell_sleep": False,
        }
    # clamshell 档
    return {
        "active": True,
        "assertions": (_A_IDLE_SYSTEM, _A_SYSTEM, _A_IDLE_DISPLAY),
        "disable_clamshell_sleep": True,
    }


def satisfied(plan_row, active_types, sleep_disabled):
    """计划是否已满足（用于读回校验）。"""
    if not plan_row.get("active"):
        return True
    want = set(a for a in plan_row.get("assertions") or () if a)
    if not want.issubset(set(active_types or ())):
        return False
    if plan_row.get("disable_clamshell_sleep") and not sleep_disabled:
        return False
    return True


def expected_assertions(plan_row):
    """计划要求的断言集合（供界面显示「应持有哪些」）。"""
    return tuple(a for a in (plan_row.get("assertions") or ()) if a)


# ── 系统状态读取 ───────────────────────────────────────────────────────

_has_lid_cache = {}


def has_lid():
    """这台 Mac 有没有盖子（MacBook / Air / Pro 有，mini / Studio / 显示器没有）。

    台式机上 AppleClamshellState 恒为 true，若不看机型会把台式机当成
    「一直合盖」，于是 .systemOnly 永远升级持 display 断言、界面永远提示
    「已合盖」——都是错的。按机型名判断一次后缓存。
    """
    if "v" in _has_lid_cache:
        return _has_lid_cache["v"]
    val = None
    try:
        rc, out = _run(["/usr/sbin/system_profiler", "SPHardwareDataType"],
                       timeout=10)
        if rc == 0:
            for ln in out.splitlines():
                if "Model Name" not in ln:
                    continue
                name = ln.split(":", 1)[1].strip().lower()
                val = ("macbook" in name or "air" in name)
                break
    except Exception:  # noqa: BLE001
        val = None
    _has_lid_cache["v"] = val
    return val


def clamshell_state():
    """MacBook 盖子是否合上（读 IOPMrootDomain / AppleClamshellState）。

    台式机（Mac mini / Studio）没有盖子，该属性通常恒为 true，此时直接返回
    None 表示「不适用」——界面据此不提示合盖，.systemOnly 也不做无谓升级。
    """
    if not _load():
        return None
    if has_lid() is False:
        return None
    try:
        root = _iokit.IOServiceGetMatchingService(
            None, _iokit.IOServiceNameMatching(b"IOPMrootDomain"))
        if not root:
            return None
        try:
            ref = _iokit.IORegistryEntryCreateCFProperty(
                root, _cfstr("AppleClamshellState"), None, 0)
            return _cf_bool(ref)
        finally:
            _iokit.IOObjectRelease(root)
    except Exception:  # noqa: BLE001
        return None


def sleep_disabled_state():
    """系统级「禁止睡眠」是否已开启（IOPMrootDomain / SleepDisabled）。"""
    if not _load():
        return None
    try:
        root = _iokit.IOServiceGetMatchingService(
            None, _iokit.IOServiceNameMatching(b"IOPMrootDomain"))
        if not root:
            return None
        try:
            ref = _iokit.IORegistryEntryCreateCFProperty(
                root, _cfstr("SleepDisabled"), None, 0)
            return _cf_bool(ref)
        finally:
            _iokit.IOObjectRelease(root)
    except Exception:  # noqa: BLE001
        return None


def _run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           check=False)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def _set_sleep_disabled(disable, allow_prompt=False):
    """置系统级 disablesleep；返回 (成功与否, 是否需要/已尝试授权)。

    先试 `sudo -n`（已配过免密时零打扰）；失败且 allow_prompt 时走 osascript
    管理员授权框。绝不静默写 /etc/sudoers.d——那比弹一次框更越界。
    """
    target = "1" if disable else "0"
    rc, _out = _run(["/usr/bin/sudo", "-n", "/usr/bin/pmset", "-a",
                     "disablesleep", target])
    if rc == 0 and sleep_disabled_state() == disable:
        return True, False
    if not allow_prompt:
        return False, False
    script = f"/usr/bin/pmset -a disablesleep {target}"
    _rc, _o = _run(["/usr/bin/osascript", "-e",
                    f'do shell script "{script}" with administrator privileges'],
                   timeout=90)
    ok = (sleep_disabled_state() == disable)
    return ok, ok


_ASSERTION_TYPES = (_A_IDLE_SYSTEM, _A_SYSTEM, _A_IDLE_DISPLAY)


def _win_set_lid_pair(ac_value, dc_value):
    """把当前电源计划的合盖动作写成 (交流, 直流)；必要时弹一次 UAC 批量写。

    先无提升直写（本程序以管理员运行时零打扰）；失败且 allow_prompt 时
    把三条 powercfg 打进一个临时 cmd，用 PowerShell Start-Process -Verb
    runAs -Wait 提权跑——一次授权，不逐条弹框。成败以读回系统状态为准。
    """
    if _win_write_lid_direct(ac_value, dc_value):
        return True
    if not allow_prompt:
        return False
    return _win_apply_lid_elevated(ac_value, dc_value)


def _win_write_lid_direct(ac_value, dc_value):
    """无提升直写当前电源计划的合盖动作，返回是否成功（读回核对）。"""
    for scope, v in (("AC", ac_value), ("DC", dc_value)):
        rc, _out = _run(["powercfg", "/SET%sVALUEINDEX" % scope,
                         "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION",
                         str(int(v))], timeout=60)
        if rc:
            return False
    rc, _out = _run(["powercfg", "/SETACTIVE", "SCHEME_CURRENT"], timeout=60)
    if rc:
        return False
    return _lid_state() == (int(ac_value), int(dc_value))


def _win_apply_lid_elevated(ac_value, dc_value):
    """用 UAC 提权批量写合盖动作（一个临时 cmd，一次授权）。"""
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".cmd")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write("@echo off\r\n")
            f.write("powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_BUTTONS "
                    "LIDACTION %d\r\n" % int(ac_value))
            f.write("powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_BUTTONS "
                    "LIDACTION %d\r\n" % int(dc_value))
            f.write("powercfg /SETACTIVE SCHEME_CURRENT\r\n")
        script = "Start-Process -FilePath '%s' -Verb runAs -Wait" % path
        rc, _out = _run(["powershell", "-NoProfile", "-Command", script],
                        timeout=180)
        return rc == 0 and _lid_state() == (int(ac_value), int(dc_value))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
def active_assertion_types():
    """本进程当前持有的电源断言类型集合。

    读 `pmset -g assertions` 里本 pid 那些行，按已知断言类型名匹配。行格式：
        pid 123(App): [0x...] 00:00:00 PreventUserIdleSystemSleep named: ""
    不能按冒号切——`pid 123(App):` 里就有冒号，切出来的会是方括号里的断言 ID
    （实测踩过：校验把 0x0000fea8... 当类型名，于是永远判「未持上」）。
    """
    rc, out = _run(["/usr/bin/pmset", "-g", "assertions"])
    if rc != 0 or not out:
        return set()
    import os
    pid = os.getpid()
    found = set()
    for ln in out.splitlines():
        if f"pid {pid}(" not in ln:
            continue
        for t in _ASSERTION_TYPES:
            if t in ln:
                found.add(t)
    return found


# ── 编排层：持断言 / 归还 ──────────────────────────────────────────────

class KeepAwake:
    """持断言 / 执行状态的小状态机，macOS 与 Windows 双平台编排。"""

    def __init__(self):
        self._ids = {}
        self._did_set_disablesleep = False
        # Windows 侧：执行状态持有线程 + 合盖动作原值
        self._win_holder = _WinExecutionHolder()
        self._win_lid_backup = None

    # -- 断言 --
    def _release_assertions(self):
        for name, aid in list(self._ids.items()):
            if aid and _iokit:
                try:
                    _iokit.IOPMAssertionRelease(aid)
                except Exception:  # noqa: BLE001
                    pass
            self._ids.pop(name, None)

    def _hold(self, assertion_type):
        """持一个断言（幂等：已持同类型就跳过）。"""
        if assertion_type in self._ids:
            return True
        if not _load():
            return False
        aid = ctypes.c_uint32(0)
        rc = _iokit.IOPMAssertionCreateWithName(
            _cfstr(assertion_type), _kIOPMAssertionLevelOn,
            _cfstr("多屏管理器 防休眠"), ctypes.byref(aid))
        if rc != 0 or not aid.value:
            return False
        self._ids[assertion_type] = aid.value
        return True

    def _sync_assertions(self, assertion_types):
        """把持有的断言集合调成目标集合（多的释放、少的补上）。"""
        want = set(a for a in assertion_types if a)
        for name in list(self._ids):
            if name not in want:
                if _iokit:
                    try:
                        _iokit.IOPMAssertionRelease(self._ids[name])
                    except Exception:  # noqa: BLE001
                        pass
                self._ids.pop(name, None)
        for name in want:
            self._hold(name)

    # -- 对外 --
    def apply(self, mode, allow_prompt=False):
        """套用一个模式；返回 (ok, 说明文字, 细节 dict)。

        失败不抛异常，全部转成「没生效 + 可能原因」，由界面如实显示。
        """
        if mode not in MODES:
            mode = DEFAULT_MODE
        if sys.platform == "win32":
            return self._apply_win(mode, allow_prompt=allow_prompt)
        if not IS_MAC:
            return False, "当前平台不支持防休眠", {}
        clamshell = clamshell_state()
        p = plan(mode, clamshell=bool(clamshell))
        if not p["active"]:
            self.release_all()
            return True, "已关闭防休眠（释放全部断言）", {
                "clamshell": clamshell, "sleep_disabled": sleep_disabled_state(),
                "held": []}
        # 非 clamshell 档：若之前是本程序设的 disablesleep，先归还
        if not p["disable_clamshell_sleep"]:
            self._restore_disablesleep_if_needed(allow_prompt=False)
        self._sync_assertions(p["assertions"])
        detail = {
            "clamshell": clamshell,
            "sleep_disabled": sleep_disabled_state(),
            "want": list(expected_assertions(p)),
            "held": sorted(self._ids.keys()),
            "prompted": False,
        }
        if p["disable_clamshell_sleep"]:
            if not detail["sleep_disabled"]:
                ok, _prompted = _set_sleep_disabled(True, allow_prompt=allow_prompt)
                detail["prompted"] = bool(allow_prompt)
                if ok:
                    self._did_set_disablesleep = True
                detail["sleep_disabled"] = sleep_disabled_state()
            else:
                # SleepDisabled 已经是开的：可能是别的会话/别的工具（或本程序
                # 上一次未还原的实例）设的。本轮既然在 clamshell 档且用到了
                # 它，退出时就一并归还——否则这个全局开关会永久留在系统里。
                self._did_set_disablesleep = True
        # 读回校验；不满足就自愈一次
        active = active_assertion_types()
        detail["active"] = sorted(active)
        if not satisfied(p, active, detail["sleep_disabled"]):
            self._sync_assertions(p["assertions"])
            active = active_assertion_types()
            detail["active"] = sorted(active)
        detail["sleep_disabled"] = sleep_disabled_state()
        ok = satisfied(p, active, detail["sleep_disabled"])
        detail["ok"] = ok
        if ok:
            return True, _ok_text(mode, detail), detail
        return False, _fail_text(mode, detail), detail

    def _restore_disablesleep_if_needed(self, allow_prompt=False):
        """若 disablesleep 是本程序设的，还原成 0（退出/降档时调用）。

        绝不能把用户系统永久改成不休眠——那是全局副作用，用户下次开机还在。
        """
        if not self._did_set_disablesleep:
            return False
        if not sleep_disabled_state():
            self._did_set_disablesleep = False
            return False
        ok, _p = _set_sleep_disabled(False, allow_prompt=allow_prompt)
        if ok or not sleep_disabled_state():
            self._did_set_disablesleep = False
        return ok

    def _apply_win(self, mode, allow_prompt=False):
        """Windows 侧：执行状态线程 + 合盖动作，语义对齐 macOS 四档。"""
        p = plan(mode, clamshell=False)
        if not p["active"]:
            self.release_all()
            return True, "已关闭防休眠（释放全部标志）", {"held": []}
        # 非 clamshell 档：本程序改过的合盖动作先还原
        if mode != MODE_CLAMSHELL and self._win_lid_backup:
            self._restore_lid_backup()
        flags = _win_flags_for(p["assertions"])
        ok_exec = self._win_holder.set(flags)
        detail = {
            "held": self._win_holder.current_names(),
            "lid_backup": self._win_lid_backup,
            "prompted": False,
        }
        lid_now = _lid_state()
        if mode == MODE_CLAMSHELL:
            detail["lid"] = lid_now
            if lid_now is not None and lid_now != (0, 0) \
                    and not self._win_lid_backup:
                self._win_lid_backup = lid_now
                ok_lid = _win_set_lid_pair(0, 0, allow_prompt=allow_prompt)
                detail["prompted"] = True   # 弹没弹 UAC 由 allow_prompt 决定，此处只标记「尝试过改系统设置」
                detail["lid"] = _lid_state()
                if detail["lid"] != (0, 0):
                    # 没改成：不认领，别在退出时把用户没同意的改动背在身上
                    self._win_lid_backup = None
            else:
                # 已经是 0/0：别的工具设的不认领（退出不还原）；本程序
                # 上一轮改过的（backup 在）退出时照样还原
                detail["lid"] = _lid_state()
                detail["lid_already_off"] = (lid_now == (0, 0))
        else:
            detail["lid"] = _lid_state()
            detail["lid_already_off"] = (_lid_state() == (0, 0))
        detail["ok"] = ok_exec and (mode != MODE_CLAMSHELL
                                    or detail.get("lid") == (0, 0))
        if detail["ok"]:
            return True, _win_ok_text(mode, detail), detail
        return False, _win_fail_text(mode, detail), detail

    def _restore_lid_backup(self, allow_prompt=False):
        """还原本程序改过的合盖动作；原值读不到时不动（宁可不还原）。"""
        backup = self._win_lid_backup
        if not backup:
            return False
        now = _lid_state()
        if now is not None and now == (0, 0):
            if not _win_set_lid_pair(backup[0], backup[1],
                                     allow_prompt=allow_prompt):
                return False   # 还原失败：保留 backup，下次再试
        self._win_lid_backup = None
        return True

    def release_all(self):
        """释放全部断言并归还本程序设的 disablesleep（退出时调用）。"""
        self._release_assertions()
        if sys.platform == "win32":
            self._win_holder.stop()
            self._restore_lid_backup(allow_prompt=True)
        else:
            self._restore_disablesleep_if_needed(allow_prompt=True)

    def status(self):
        """当前持有情况（界面状态栏用）。"""
        if sys.platform == "win32":
            return {
                "held": self._win_holder.current_names(),
                "clamshell": None,
                "sleep_disabled": (_lid_state() == (0, 0)),
                "owns_disablesleep": bool(self._win_lid_backup),
            }
        return {
            "held": sorted(self._ids.keys()),
            "clamshell": clamshell_state(),
            "sleep_disabled": sleep_disabled_state(),
            "owns_disablesleep": self._did_set_disablesleep,
        }


def _ok_text(mode, detail):
    label = MODE_LABELS.get(mode, mode)
    if mode == MODE_CLAMSHELL:
        if detail.get("sleep_disabled"):
            return (f"防休眠已生效：{label}（系统级禁止睡眠已开启，"
                    "退出本程序时会自动还原）")
        return (f"防休眠：{label}——断言已持，但系统级禁止睡眠没开成"
                "（需要管理员授权；合盖+拔电时可能仍会睡）")
    if mode == MODE_DISPLAY:
        return f"防休眠已生效：{label}（屏幕不黑、系统不睡）"
    if detail.get("clamshell"):
        return f"防休眠已生效：{label}（检测到合盖，已自动升级为屏幕也不黑）"
    return f"防休眠已生效：{label}（屏幕可黑，系统不睡）"


def _fail_text(mode, detail):
    want = detail.get("want") or []
    active = detail.get("active") or []
    missing = [a for a in want if a not in active]
    bits = []
    if missing:
        bits.append("未持上的断言：" + "、".join(missing))
    if MODE_LABELS.get(mode) == MODE_LABELS.get(MODE_CLAMSHELL) \
            and not detail.get("sleep_disabled"):
        bits.append("系统级禁止睡眠未开启（未授权或授权被取消）")
    return "防休眠未完全生效：" + "；".join(bits) if bits else "防休眠未生效（原因未知）"


def _win_ok_text(mode, detail):
    label = MODE_LABELS.get(mode, mode)
    if mode == MODE_CLAMSHELL:
        return (f"防休眠已生效：{label}（合盖动作已改为「不采取任何操作」，"
                "退出本程序时自动还原）")
    held = detail.get("held") or []
    return f"防休眠已生效：{label}（{'、'.join(held)}）"


def _win_fail_text(mode, detail):
    label = MODE_LABELS.get(mode, mode)
    bits = []
    if not detail.get("held"):
        bits.append("执行状态没持上（SetThreadExecutionState 失败）")
    if mode == MODE_CLAMSHELL:
        if detail.get("lid") is None:
            bits.append("读不到合盖动作设置（可能是台式机或不支持的电源计划）")
        elif detail.get("lid_already_off"):
            bits.append("合盖动作本来就是「不采取任何操作」"
                        "（可能是别的工具设的，退出不会还原）")
        elif detail.get("lid_untouched"):
            bits.append("合盖动作未改（启动恢复不弹授权框，请在界面点「立即生效」重新套用）")
        elif detail.get("lid") != (0, 0):
            bits.append("合盖动作未改成「不采取任何操作」（需要管理员授权，或组策略禁改）")
    return f"防休眠未完全生效（{label}）：" + "；".join(bits)
