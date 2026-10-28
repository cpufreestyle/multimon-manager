# -*- coding: utf-8 -*-
"""防休眠：让 Mac 在合盖、拔电、锁屏时也继续干活（复刻 LayoutKeeper 的 KeepAwake）。

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
"""
import ctypes
import subprocess
import sys
import time

IS_MAC = (sys.platform == "darwin")

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

# 断言类型名（IOKit）
_A_IDLE_SYSTEM = "PreventUserIdleSystemSleep"
_A_SYSTEM = "PreventSystemSleep"
_A_IDLE_DISPLAY = "PreventUserIdleDisplaySleep"


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
    """持断言的小状态机。非 macOS 或加载失败时所有方法都是安全的 no-op。"""

    def __init__(self):
        self._ids = {}
        self._did_set_disablesleep = False

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
        if not IS_MAC:
            return False, "当前平台不支持（防休眠仅 macOS 有 IOKit 断言）", {}
        if mode not in MODES:
            mode = DEFAULT_MODE
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

    def release_all(self):
        """释放全部断言并归还本程序设的 disablesleep（退出时调用）。"""
        self._release_assertions()
        self._restore_disablesleep_if_needed(allow_prompt=True)

    def status(self):
        """当前持有情况（界面状态栏用）。"""
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
