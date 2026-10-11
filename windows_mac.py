"""窗口跨屏移动与分屏吸附（macOS 实现，零第三方依赖）。

读取窗口几何：优先用 CoreGraphics 的 CGWindowListCopyWindowInfo —— 一次调用即可
拿到**按 z 序（从前到后）**排列的窗口列表，含 owner PID / 应用名 / bounds。
这样既能拿到准确坐标，又能**跳过本程序自己的窗口**（点击本程序按钮时前台就是
自己，旧实现因此会把"当前前台窗口"误判成本程序，去移动自己的窗口）。

写入窗口几何：用 System Events 的 position / size。
注意：较新 macOS 的 System Events window 类**没有 bounds 属性**，取 bounds 恒报
-1728「不能获得」。旧实现依赖 bounds，导致：
  - 读取恒失败 → get_window_rect 返回 (0,0,0,0)
  - 写入 `set bounds of window 1 to {...}` 静默失败 → 按钮按了完全没反应
改用 position + size 后读写均可用。

macOS 坐标系原点在左上，与 Windows 转换后的坐标系一致。
"""
import ctypes
import ctypes.util
import logging
import os
import subprocess
import threading
import time

import monitors_mac as monitors
from monitors_mac import CGRect

import quad_plan

logger = logging.getLogger(__name__)

# ── 撤销栈：记录每次移动/缩放前的窗口几何，供"撤销移动"使用（F7）──
_undo_stack = []
_undo_lock = threading.Lock()
_suppress_undo = False


def push_undo(hwnd, rect):
    """记录一次移动前几何（hwnd, (x, y, w, h)）；保留最近 20 步。"""
    with _undo_lock:
        _undo_stack.append((hwnd, tuple(rect)))
        if len(_undo_stack) > 20:
            _undo_stack.pop(0)


# ── AppleScript 执行 ─────────────────────────────────────────────────

def _osa(script):
    """执行 AppleScript，失败时记录日志（旧实现静默吞错，导致问题无法定位）。"""
    try:
        r = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0 and r.stderr.strip():
            logger.warning("AppleScript 执行失败: %s", r.stderr.strip()[:200])
        return r.stdout.strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("AppleScript 调用异常: %s", e)
        return ""


# ── Quartz（CoreGraphics + CoreFoundation）ctypes 绑定 ───────────────

_cg = None
_cf = None
_K = {}

_KCF_STRING_ENCODING_UTF8 = 0x08000100
_KCF_NUMBER_INT_TYPE = 9

_K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY = 1 << 0
_K_CG_WINDOW_LIST_EXCLUDE_DESKTOP_ELEMENTS = 1 << 4


def _load_quartz():
    global _cg, _cf
    try:
        cg_path = ctypes.util.find_library("CoreGraphics")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not cg_path or not cf_path:
            logger.warning("未找到 CoreGraphics/CoreFoundation，窗口几何将退化为 AppleScript")
            return
        cg = ctypes.CDLL(cg_path)
        cf = ctypes.CDLL(cf_path)

        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p

        cg.CGRectMakeWithDictionaryRepresentation.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(CGRect)
        ]
        cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool

        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFDictionaryGetValueIfPresent.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        ]
        cf.CFDictionaryGetValueIfPresent.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None

        _cg, _cf = cg, cf
        logger.info("Quartz(CoreGraphics/CoreFoundation) 加载成功")
    except Exception as e:  # noqa: BLE001
        logger.warning("加载 Quartz 失败: %s", e)


_load_quartz()


def _k(name):
    """取 CoreGraphics 导出的 CFString 常量（如 kCGWindowBounds）。"""
    if name not in _K:
        ref = None
        if _cg is not None:
            try:
                ref = ctypes.c_void_p.in_dll(_cg, name).value
            except Exception:  # noqa: BLE001
                ref = None
        _K[name] = ref
    return _K[name]


def _cfstring_to_str(ref):
    if not ref or _cf is None:
        return ""
    try:
        n = _cf.CFStringGetLength(ctypes.c_void_p(ref))
        if n <= 0:
            return ""
        buf = ctypes.create_string_buffer(n * 4 + 1)
        if _cf.CFStringGetCString(
            ctypes.c_void_p(ref), buf, len(buf), _KCF_STRING_ENCODING_UTF8
        ):
            return buf.value.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _cfnumber_to_int(ref):
    if not ref or _cf is None:
        return None
    v = ctypes.c_int(0)
    try:
        if _cf.CFNumberGetValue(
            ctypes.c_void_p(ref), _KCF_NUMBER_INT_TYPE, ctypes.byref(v)
        ):
            return v.value
    except Exception:  # noqa: BLE001
        pass
    return None


# 系统浮层/后台代理进程：它们偶尔会以 layer 0 出现在窗口列表里（如 WindowManager 的
# "Gesture Blocking Overlay"），若被当成"最前窗口"会导致操作打空，故一律排除。
_SKIP_OWNERS = {
    "WindowManager",
    "Window Server",
    "Dock",
    "SystemUIServer",
    "loginwindow",
    "ControlCenter",
    "NotificationCenter",
    "TextInputMenuAgent",
    "universalaccessd",
    "AccessibilityUIServer",
    "ViewBridgeAuxiliary",
    "WallpaperAgent",
    "MenuBarAgent",
    "UIKitSystem",
    "System Events",
}


def _dict_get(d, key_name):
    """从 CFDictionary 取值；不存在返回 None。"""
    k = _k(key_name)
    if not k or not d or _cf is None:
        return None
    out = ctypes.c_void_p(0)
    try:
        if _cf.CFDictionaryGetValueIfPresent(
            ctypes.c_void_p(d), ctypes.c_void_p(k), ctypes.byref(out)
        ):
            return out.value
    except Exception:  # noqa: BLE001
        pass
    return None


# ── 本程序自身的进程白名单（窗口操作时应跳过自己） ────────────────────

_OWN_PIDS = set()


def register_own_pid(pid):
    """登记本应用的进程 PID（主进程自动登记，托盘子进程由 main.py 登记）。"""
    if pid:
        _OWN_PIDS.add(int(pid))


register_own_pid(os.getpid())


# ── 窗口枚举（CoreGraphics，按 z 序从前到后） ────────────────────────

def list_windows_front_to_back(min_size=80):
    """返回按 z 序（最前在前）排列的屏幕窗口列表。

    每项: {"pid", "owner", "name", "x", "y", "w", "h"}
    仅保留普通窗口层（layer 0）且尺寸合理的项。
    """
    if _cg is None or _cf is None:
        return []
    arr = None
    try:
        opt = (
            _K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY
            | _K_CG_WINDOW_LIST_EXCLUDE_DESKTOP_ELEMENTS
        )
        arr = _cg.CGWindowListCopyWindowInfo(opt, 0)
        if not arr:
            return []
        count = _cf.CFArrayGetCount(ctypes.c_void_p(arr))
        result = []
        for i in range(count):
            d = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(arr), i)
            if not d:
                continue
            layer = _cfnumber_to_int(_dict_get(d, "kCGWindowLayer"))
            if layer not in (0, None):  # 跳过 Dock / 菜单栏等非普通层
                continue
            bounds_ref = _dict_get(d, "kCGWindowBounds")
            if not bounds_ref:
                continue
            r = CGRect()
            if not _cg.CGRectMakeWithDictionaryRepresentation(
                ctypes.c_void_p(bounds_ref), ctypes.byref(r)
            ):
                continue
            w, h = int(r.size.width), int(r.size.height)
            if w < min_size or h < min_size:
                continue
            owner = _cfstring_to_str(_dict_get(d, "kCGWindowOwnerName"))
            if owner in _SKIP_OWNERS:
                continue
            result.append({
                "pid": _cfnumber_to_int(_dict_get(d, "kCGWindowOwnerPID")),
                "owner": owner,
                "name": _cfstring_to_str(_dict_get(d, "kCGWindowName")),
                "wid": _cfnumber_to_int(_dict_get(d, "kCGWindowNumber")),
                "x": int(r.origin.x),
                "y": int(r.origin.y),
                "w": w,
                "h": h,
            })
        return result
    except Exception as e:  # noqa: BLE001
        logger.warning("枚举窗口失败: %s", e)
        return []
    finally:
        if arr and _cf is not None:
            try:
                _cf.CFRelease(ctypes.c_void_p(arr))
            except Exception:  # noqa: BLE001
                pass


def front_external_window(min_reasonable=(160, 120)):
    """最前面的**非本程序**窗口；没有则返回 None（避免误操作自己）。

    分两轮：先找尺寸正常的窗口，避免自动选中最小化/其它 Space 的缩略小窗
    （实测列表里混有 87×108、117×121 之类的小窗）；找不到再放宽条件。
    """
    wins = [w for w in list_windows_front_to_back()
            if w["pid"] not in _OWN_PIDS and w["owner"]]
    mw, mh = min_reasonable
    for w in wins:
        if w["w"] >= mw and w["h"] >= mh:
            return w
    return wins[0] if wins else None


# ── 对外接口 ─────────────────────────────────────────────────────────

# hwnd 格式：「应用名::窗口名#序号」。
# 序号是该应用同名窗口按 z 序的 1 基编号（唯一时省略，向后兼容旧格式
# 「应用名::窗口名」）。UU远程 这类应用所有窗口同名（多为空串），只靠
# 「应用::窗口名」会全部撞成一个标识：枚举去重后 6 个窗口只剩 1 个，
# 且 set_window_rect 的 AppleScript 永远写 window 1——这就是"自动排列
# 不准 / 只显示一个窗口"的根因。带序号后，规则引擎 / 并排 / 三栏 / 任务栏
# 都能区分并精确操作每个窗口。

def make_hwnd(owner, name, seq=None):
    """生成窗口标识；seq 为同名窗口的 1 基序号（唯一/首个时不带 #n）。"""
    base = f"{owner}::{name}"
    return f"{base}#{seq}" if seq and seq > 1 else base


def parse_hwnd(hwnd):
    """拆出 (应用名, 窗口名, 序号)；旧格式（无 #n）序号返回 None。"""
    app, _, rest = hwnd.partition("::")
    seq = None
    if "#" in rest:
        rest, _, tail = rest.rpartition("#")
        try:
            seq = int(tail)
        except ValueError:  # noqa: BLE001
            rest = f"{rest}#{tail}"  # 标题本身含 #，原样保留
    return app, rest, seq


def _same_name_windows(app, name):
    """该应用下同窗口名的全部窗口（z 序），按 hwnd 序号语义排列。"""
    return [w for w in list_windows_front_to_back()
            if w["owner"] == app and (w["name"] or "") == (name or "")]


_last_rect = {}  # "app::winname#seq" -> (x, y, w, h)，来自 CG 的精确几何

# 界面上固定的目标窗口。自动判断（"最前面的非本程序窗口"）经常猜错：
# 例如用户停在 IDE 聊天窗口时点按钮，最前面的就是 IDE，结果把 IDE 分屏了。
# 因此提供显式指定；为 None 时回到自动判断。
_pinned_target = None


def set_target(hwnd):
    """固定要操作的窗口；传 None 表示恢复自动判断。"""
    global _pinned_target
    _pinned_target = hwnd
    logger.info("目标窗口设为: %s", hwnd or "自动")


def get_target():
    return _pinned_target


def list_target_windows():
    """列出可选窗口（供界面下拉框），已排除本程序与系统浮层。

    返回 [{"hwnd", "label", "owner", "name", "x", "y", "w", "h"}, ...]

    同名窗口（如 UU远程 的多个会话窗口）按 z 序编号区分，不再被去重吞掉。
    """
    out = []
    name_count = {}
    name_seen = {}
    raw = list_windows_front_to_back()
    for w in raw:
        key = (w["owner"], w["name"] or "")
        name_count[key] = name_count.get(key, 0) + 1
    for w in raw:
        key = (w["owner"], w["name"] or "")
        n = name_seen.get(key, 0) + 1
        name_seen[key] = n
        seq = n if name_count[key] > 1 else None
        hwnd = make_hwnd(w["owner"], w["name"] or "", seq)
        title = f'{w["owner"]} — {w["name"]}' if w["name"] else w["owner"]
        # 带上尺寸，便于区分同名/小窗口
        label = f'{title}  ({w["w"]}×{w["h"]})'
        if seq:
            # 序号放最前：下拉框宽度有限会截尾，放前面才看得到区分
            label = f"#{seq} {label}"
        out.append({**w, "hwnd": hwnd, "seq": seq, "label": label})
    return out


def _find_window(hwnd):
    """按 hwnd 重新定位窗口，拿到最新几何（窗口可能被移动过）。

    兼容旧格式「app::name」：同名多窗口时取最前面那个。
    """
    if not hwnd:
        return None
    app, name, seq = parse_hwnd(hwnd)
    same = _same_name_windows(app, name)
    if not same:
        return None
    idx = min((seq or 1) - 1, len(same) - 1)
    return same[idx]


def get_foreground_window(use_pinned=True):
    """返回 '应用名::窗口名' 标识。

    use_pinned=True（界面按钮）时优先用界面固定的目标窗口；
    use_pinned=False（全局快捷键）时始终作用于真正的活动窗口。
    """
    if use_pinned and _pinned_target:
        w = _find_window(_pinned_target)
        if w:
            _last_rect[_pinned_target] = (w["x"], w["y"], w["w"], w["h"])
            logger.info("目标窗口(已固定): %s  几何=%s",
                        _pinned_target, _last_rect[_pinned_target])
            return _pinned_target
        logger.warning("固定的目标窗口已关闭或不可见: %s，临时回退自动判断",
                       _pinned_target)

    w = front_external_window()
    if w:
        # 同名多窗口时补上 z 序编号，避免后续操作落到别的同名窗口上
        same = _same_name_windows(w["owner"], w["name"] or "")
        seq = None
        if len(same) > 1:
            for i, s in enumerate(same, 1):
                if s.get("wid") is not None and s.get("wid") == w.get("wid"):
                    seq = i
                    break
        key = make_hwnd(w["owner"], w["name"] or "", seq)
        _last_rect[key] = (w["x"], w["y"], w["w"], w["h"])
        logger.info("目标窗口: %s  几何=%s", key, _last_rect[key])
        return key

    logger.warning(
        "CG 未找到可操作窗口（已排除本程序 PID %s），回退 AppleScript",
        sorted(_OWN_PIDS),
    )

    # 回退：CoreGraphics 不可用时用 System Events。
    # 注意必须带上 unix id —— 点按钮时前台就是本程序自己，若不按 PID 排除，
    # 会去移动自己的窗口，表现为"按了没反应"。
    script = (
        'tell application "System Events"\n'
        '  set p to first application process whose frontmost is true\n'
        '  set pid to unix id of p\n'
        '  set appName to name of p\n'
        '  if (count of windows of p) > 0 then\n'
        '    return (pid as text) & "|" & appName & "::" & (name of window 1 of p)\n'
        '  end if\n'
        'end tell\n'
        'return ""\n'
    )
    res = _osa(script)
    if not res or "|" not in res:
        return None
    pid_str, rest = res.split("|", 1)
    try:
        if int(pid_str) in _OWN_PIDS:
            logger.warning("前台就是本程序自己（PID %s），放弃操作", pid_str)
            return None
    except ValueError:  # noqa: BLE001
        pass
    return rest


def get_window_rect(hwnd):
    """返回 (x, y, w, h)。

    优先用 CoreGraphics 枚举按 (owner + name) 精确匹配，完全不经过 AppleScript，
    因此不受窗口标题特殊字符（如全角引号）影响（修复 B 债：OneNote / 微信开发者工具
    等标题含全角引号的窗口几何读取失败）。CG 取不到时再回退 System Events 的
    position+size。
    """
    if not hwnd:
        return (0, 0, 0, 0)
    if hwnd in _last_rect:
        return _last_rect[hwnd]

    app, name, seq = parse_hwnd(hwnd)
    # 优先走 CG：按 owner 匹配，精确匹配窗口名。
    # 带序号的 hwnd 只认「同名第 N 个」，绝不回退到该应用最前面的窗口——
    # 同名多窗口（UU远程）时回退会让不同 hwnd 都读成同一个窗口的几何，
    # 闭环读回/写入全乱，表现为"几个窗口跟着一起动"。
    try:
        ordinal = 0
        for w in list_windows_front_to_back():
            if w["owner"] != app:
                continue
            if (w["name"] or "") == (name or ""):
                ordinal += 1
                if seq and ordinal != seq:
                    continue  # 带序号的 hwnd 只认对应那个同名窗口
                rect = (w["x"], w["y"], w["w"], w["h"])
                _last_rect[hwnd] = rect
                return rect
        if seq:
            # 序号牌没匹配到（窗口可能已关闭/改名），不拿别的窗口顶替
            logger.warning("序号牌 %s 未匹配到同名窗口，返回零几何", hwnd)
            return (0, 0, 0, 0)
        # 无序号（唯一窗口）：允许按 owner 兜底到该应用最前窗口
        for w in list_windows_front_to_back():
            if w["owner"] == app:
                rect = (w["x"], w["y"], w["w"], w["h"])
                _last_rect[hwnd] = rect
                return rect
    except Exception:  # noqa: BLE001
        pass

    # 回退：System Events 的 position+size（注意 bounds 在 macOS 当前不可用，恒报 -1728）
    # 带序号时按「同名第 N 个」选择窗口，避免总是读到 window 1
    win_sel = _ax_window_selector(hwnd)
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    set p to position of {win_sel}\n'
        f'    set s to size of {win_sel}\n'
        f'    return (item 1 of p & "," & item 2 of p & "," '
        f'& item 1 of s & "," & item 2 of s)\n'
        f'  end tell\n'
        f'end tell\n'
    )
    out = _osa(script)
    try:
        x, y, w, h = (int(float(v)) for v in out.split(","))
        rect = (x, y, w, h)
    except Exception:  # noqa: BLE001
        logger.warning("读取窗口几何失败: %r -> %r", hwnd, out)
        return (0, 0, 0, 0)
    _last_rect[hwnd] = rect
    return rect


def _ax_window_selector(hwnd):
    """按 hwnd 生成 AppleScript 的窗口选择子。

    默认是 "window 1"；同名多窗口的 hwnd 带 #n 序号时用 "window N"。
    AX 窗口顺序与 CG z 序一致（实测 UU远程 6 个窗口），因此 z 序第 N 个
    同名窗口就是 AX 的 window N。
    """
    _app, _name, seq = parse_hwnd(hwnd)
    if not seq:
        return "window 1"
    return f"window {seq}"


def _ax_window_by_geometry(app, rect, name=None):
    """按几何在 AX 窗口列表里找出对应的 window N 选择子。

    CG 的窗口枚举（list_windows_front_to_back）与 AppleScript 的 AX 窗口
    顺序**不一定一致**：QClaw 有两个 AX 窗口，window 1 是 430x40 的悬浮小条、
    window 2 才是 1200x800 的主窗口，而 CG 只列出主窗口。此时 hwnd 不带 #n
    （CG 侧只有一个同名窗口），_ax_window_selector 退化成 "window 1"，
    结果把几何写到小条上、读回的却是主窗口——表现为「这个窗口完全推不动」。

    做法：枚举该应用所有 AX 窗口的位置/尺寸，取与目标矩形最接近的那个，
    返回 "window N"。找不到时返回 None，让调用方回退到按序号。
    """
    if not rect or rect == (0, 0, 0, 0):
        return None
    tx, ty, tw, th = rect
    script = (
        'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        '    set out to ""\n'
        '    repeat with w in windows\n'
        '      set p to position of w\n'
        '      set s to size of w\n'
        '      set out to out & (item 1 of p as text) & "," & '
        '(item 2 of p as text) & "," & (item 1 of s as text) & "," & '
        '(item 2 of s as text) & ";"\n'
        '    end repeat\n'
        '  end tell\n'
        'end tell\n'
        'return out'
    )
    raw = _osa(script)
    if not raw:
        return None
    best, best_dev = None, None
    for i, item in enumerate(raw.split(";"), 1):
        item = item.strip()
        if not item:
            continue
        parts = item.split(",")
        if len(parts) != 4:
            continue
        try:
            nums = [abs(int(float(v))) for v in parts]
        except ValueError:  # noqa: BLE001
            continue
        dev = (abs(nums[0] - tx) + abs(nums[1] - ty)
               + abs(nums[2] - tw) + abs(nums[3] - th))
        if best_dev is None or dev < best_dev:
            best, best_dev = i, dev
    if best is None:
        return None
    # 偏差过大（超过 64px 总和）说明几何已变（窗口被移动过），匹配不可信
    if best_dev > 64:
        by_name = _ax_window_by_name(app, name)
        if by_name:
            logger.info("AX 几何匹配偏差过大 (%d)，改用窗口名匹配 -> %s",
                        best_dev, by_name)
            return by_name
        logger.info("AX 几何匹配偏差过大 (%d)，回退按序号", best_dev)
        return None
    return f"window {best}"


def _ax_window_by_name(app, name):
    """按窗口名在 AX 窗口列表里找 window N（几何匹配失效时的兜底）。

    几何匹配失效时不能直接回退 window 1：真机 Chrome 的 EchoID 主窗口在
    y=-870 的副屏，CG 几何与 AX 对不上就回退 window 1，几何便写到了
    「要保存密码吗」对话框上，主窗口看着完全推不动。

    AX 的 name 与 CG 的窗口标题未必字字相同，所以用「互相包含」判断，
    并优先取名字更长（更具体）的那个，避免都含空串时误配。
    """
    want = (name or "").strip()
    if not want:
        return None
    # 语句之间必须有换行符：AppleScript 不接受把多条语句拼在同一行（缺了 \n
    # 时 osascript 一律报 -2741 语法错误，这条按窗口名兜底的分支等于死代码）
    script = (
        'tell application "System Events"\n'
        '  tell process "' + app + '"\n'
        '    set out to ""\n'
        '    repeat with w in windows\n'
        '      set out to out & (name of w as text) & ";"\n'
        '    end repeat\n'
        '  end tell\n'
        'end tell\n'
        'return out'
    )
    raw = _osa(script)
    if not raw:
        return None
    names = [n.strip() for n in raw.split(";")]
    best, best_len = None, 0
    for i, n in enumerate(names, 1):
        if not n:
            continue
        if want in n or n in want:
            if len(n) > best_len:
                best, best_len = i, len(n)
    if best is None and len(names) == 1:
        return "window 1"
    return f"window {best}" if best else None


def _wait_rect_stable(hwnd, timeout=1.5, interval=0.08, need=3):
    """等窗口几何稳定后再读回；始终不稳时返回最后一次读数。

    System Events 写入 position/size 后系统会播放移动 / 缩放动画，此时 CG 读到
    的是**动画中间帧**。闭环若把中间帧当实测值，会高估偏差并反向补偿过头，
    把窗口越推越远，4 次重试很快耗尽，最终落位偏差可达数百 px。

    竖屏（portrait）尤其明显：纵向跨度 2000+px，一次「上半屏 → 下半屏」要移动
    近千 px，动画时间长、中间帧与目标相差极远，这就是「竖屏上中下不准」的根因。
    小位移（主屏内微调）动画短，才显得准。

    判定方式：连续 need 次读回完全一致才认为动画结束。只要求「连续 2 次」是不够的——
    动画卡顿时会连续采到同一个中间帧而误判稳定，闭环随即按中间帧大幅反向补偿，
    窗口被越推越远（实测下半屏会被推到屏幕外）。
    """
    last = None
    same = 0
    waited = 0.0
    while waited < timeout:
        _last_rect.pop(hwnd, None)
        try:
            cur = get_window_rect(hwnd)
        except Exception:  # noqa: BLE001
            return last
        if cur and cur != (0, 0, 0, 0):
            if cur == last:
                same += 1
                if same >= need:
                    return cur
            else:
                same = 1
            last = cur
        time.sleep(interval)
        waited += interval
    return last


def set_window_rect(hwnd, x, y, w, h, activate=True):
    """移动/缩放窗口：反复写入同一目标值推进到位（幂等重设），而非反向补偿。

    坐标系约定：传入的 (x, y, w, h) 是窗口应占据的屏幕矩形，与 zone_rect / snap
    的分区计算保持一致。

    早期实现是「读回实测偏差后反向补偿」，但 System Events 对一次写入只做部分应用
    （变化量越大动画越长），读回值取决于写入前的窗口状态、并不稳定，据此补偿会把
    窗口推飞——竖屏（小米 portrait 1152x2048）上「上半屏 / 整屏高度」就是这么跑偏
    数百 px 的（用户反馈「竖屏上中下不准」）。

    现改为幂等重设：每次都写入同一个目标值，等动画结束后读回，只要还在变好就继续；
    读回不再变化即认为已触达系统 / App 可达边界（例如标题栏占住的顶部约 30px，
    窗口永远贴不到 work_top），此时停止，不再徒劳补偿。
    """
    if not hwnd:
        return
    if not _suppress_undo:
        try:
            push_undo(hwnd, get_window_rect(hwnd))
        except Exception:  # noqa: BLE001
            pass
    app2, nm, _sq = parse_hwnd(hwnd)
    known = _last_rect.get(hwnd)
    win_sel = (_ax_window_by_geometry(app2, known, nm)
               or _ax_window_selector(hwnd))
    # 优先按几何在 AX 窗口列表里定位：CG 枚举顺序与 AX 顺序未必一致，
    # 只按 hwnd 序号选窗口会把几何写到别的窗口上（见 _ax_window_by_geometry）
    app = app2
    # 原始目标（content 坐标），闭环全程以此为基准，偏差按「实测 - 原始目标」累计
    tx, ty, tw, th = (int(round(v)) for v in (x, y, w, h))
    # 幂等重设次数上限：下半屏约 3 次命中；整屏高度类（变化量最大）需更多次才触顶
    max_retry = 16
    prev = None
    best_dev = None
    stall = 0
    logger.info("移动窗口: 进程=%s -> content x=%d y=%d w=%d h=%d", app, tx, ty, tw, th)
    # 退出 zoom/全屏态，否则 size 变更可能被忽略
    _osa(
        f'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    if (count of windows) > 0 then\n'
        f'      try\n'
        f'        tell {win_sel} to if zoomed then set zoomed to false\n'
        f'      end try\n'
        f'    end if\n'
        f'  end tell\n'
        f'end tell\n'
    )
    # 幂等重设推进：System Events 对一次写入只「部分应用」——变化量越大动画越长，
    # 单次写入常常只走一部分（竖屏整屏高度实测只到 1465/2002）。反复写入同一个目标
    # 会持续推进，直到命中或触到系统上限（下半屏实测 3 次即精确命中）。
    #
    # 早期实现是「按读回偏差反向补偿」，但读回值依赖写入前的窗口状态、并不稳定，
    # 据此算出的补偿量会把窗口推飞（竖屏实测偏差被放大到数百 px），故改为幂等重设：
    # 每次都写同一个目标值，只判断「是否还在变好」，不做数值补偿。
    for attempt in range(max_retry):
        _osa(
            f'tell application "System Events"\n'
            f'  tell process "{app}"\n'
            f'    set position of {win_sel} to {{{tx}, {ty}}}\n'
            f'    set size of {win_sel} to {{{tw}, {th}}}\n'
            f'  end tell\n'
            f'end tell\n'
        )
        # 必须等动画结束再读，否则读到中间帧会误判。这里用较短的超时：重设是「反复
        # 推进」，每轮只需判断是否还在变好，不必等到完全静止，好把时间留给下一轮
        c = _wait_rect_stable(hwnd, timeout=0.5)
        if not c or c == (0, 0, 0, 0):
            break
        dev = max(abs(a - t) for a, t in zip(c, (tx, ty, tw, th)))
        if dev <= 2:
            break
        # 停止条件用「偏差是否还在改善」而不是「读回是否变化」：整屏高度这类大变化
        # 每次推进都很小，读回看着像没动，其实仍在靠近；提前停会卡在半路。
        if best_dev is None or dev < best_dev - 1:
            best_dev = dev
            stall = 0
        else:
            stall += 1
            if stall >= 2:
                # 连续两次都没再改善 → 已触达系统/App 可达边界（例如标题栏占住的
                # 顶部约 30px，窗口永远贴不到 work_top），再写也只是白等
                logger.info("已达可达边界：实测=%s 目标=(%d,%d,%d,%d) 偏差=%d",
                            c, tx, ty, tw, th, dev)
                break
        prev = c
        logger.info("幂等重设 第%d次 实测=%s 偏差=%d", attempt + 1, c, dev)
    if activate:
        _osa(f'tell application "{app}" to activate')


def undo_last_move():
    """撤销最近一次窗口移动/缩放（恢复该窗口移动前的几何）。

    返回被还原的 hwnd；无可撤销项时返回 None。恢复过程自身不再次入栈。
    """
    global _suppress_undo
    with _undo_lock:
        if not _undo_stack:
            return None
        hwnd, rect = _undo_stack.pop()
    _suppress_undo = True
    try:
        set_window_rect(hwnd, rect[0], rect[1], rect[2], rect[3], activate=False)
    finally:
        _suppress_undo = False
    return hwnd


def _monitor_by_relative(monitors_list, src_hwnd):
    x, y, w, h = get_window_rect(src_hwnd)
    cx, cy = x + w // 2, y + h // 2
    for i, m in enumerate(monitors_list):
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return i
    return 0


def move_to_monitor(hwnd, monitor, activate=True):
    x, y, w, h = get_window_rect(hwnd)
    src = monitors.enum_monitors()
    idx = _monitor_by_relative(src, hwnd)
    src_m = src[idx] if idx < len(src) else src[0]
    rel_x = (x - src_m.left) / max(src_m.width, 1)
    rel_y = (y - src_m.top) / max(src_m.height, 1)
    new_x = monitor.work_left + rel_x * max(monitor.work_width - w, 0)
    new_y = monitor.work_top + rel_y * max(monitor.work_height - h, 0)
    set_window_rect(hwnd, new_x, new_y, w, h, activate=activate)


def snap(hwnd, monitor, zone, activate=True):
    wl, wt, ww, wh = monitor.work_rect
    _x, _y, w, h = get_window_rect(hwnd)
    if zone == "left":
        set_window_rect(hwnd, wl, wt, ww // 2, wh, activate=activate)
    elif zone == "right":
        set_window_rect(hwnd, wl + ww // 2, wt, ww - ww // 2, wh, activate=activate)
    elif zone == "top":
        set_window_rect(hwnd, wl, wt, ww, wh // 2, activate=activate)
    elif zone == "bottom":
        set_window_rect(hwnd, wl, wt + wh // 2, ww, wh - wh // 2, activate=activate)
    elif zone == "maximize":
        set_window_rect(hwnd, wl, wt, ww, wh, activate=activate)
    elif zone == "center":
        set_window_rect(hwnd, wl + (ww - w) // 2, wt + (wh - h) // 2, w, h,
                        activate=activate)
    elif zone == "left-third":
        set_window_rect(hwnd, wl, wt, ww // 3, wh, activate=activate)
    elif zone == "middle-third":
        set_window_rect(hwnd, wl + ww // 3, wt, ww // 3, wh, activate=activate)
    elif zone == "right-third":
        set_window_rect(hwnd, wl + 2 * (ww // 3), wt, ww - 2 * (ww // 3), wh,
                        activate=activate)
    elif zone == "quad-tl":
        set_window_rect(hwnd, wl, wt, ww // 2, wh // 2, activate=activate)
    elif zone == "quad-tr":
        set_window_rect(hwnd, wl + ww // 2, wt, ww - ww // 2, wh // 2, activate=activate)
    elif zone == "quad-bl":
        set_window_rect(hwnd, wl, wt + wh // 2, ww // 2, wh - wh // 2, activate=activate)
    elif zone == "quad-br":
        set_window_rect(hwnd, wl + ww // 2, wt + wh // 2, ww - ww // 2, wh - wh // 2,
                        activate=activate)


def _monitor_index_by_rect(monitors_list, x, y, w, h):
    """按给定矩形（而非 hwnd）判断所在显示器索引。"""
    cx, cy = x + w // 2, y + h // 2
    for i, m in enumerate(monitors_list):
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return i
    return 0


def _resolve_monitor_index(monitors_list, monitor):
    """把 monitor 参数（int 索引 / device_name 字符串 / None）解析为显示器索引。

    无法解析或越界时返回 None，调用方应回退到「按窗口所在屏」逻辑。
    """
    if monitor is None:
        return None
    if isinstance(monitor, int):
        return monitor if 0 <= monitor < len(monitors_list) else None
    if isinstance(monitor, str):
        for i, m in enumerate(monitors_list):
            if m.device_name == monitor or m.device_path == monitor:
                return i
    return None


def snap_two_side_by_side(left_hwnd=None, right_hwnd=None, use_pinned=True, monitor=None):
    """把两个窗口并排到同一屏的左右半屏（左/右可显式指定）。

    left_hwnd / right_hwnd 为 "应用::窗口" 标识；未指定时自动取最前面的两个
    窗口（左侧优先用界面固定的目标窗口）。monitor 可指定目标显示器
    （int 索引或 device_name 字符串）；为 None 时以左侧窗口当前所在屏幕为准。
    并排完成后把左右窗口激活到最前，确保并排结果可见、不被其他窗口遮挡。

    竖屏（portrait，高>宽）上自动改为「上/下各半屏」堆叠：竖屏宽度有限，
    左右并排两个窗口会挤成窄条、几乎不可用；上下堆叠才是竖屏的自然用法。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return False

    cands = [w for w in list_target_windows()
             if w["pid"] not in _OWN_PIDS and w["owner"]]
    cands_by_hwnd = {w["hwnd"]: w for w in cands}

    # 解析左窗口：显式指定 > 界面固定目标 > 最前面窗口
    left_w = None
    if left_hwnd:
        left_w = cands_by_hwnd.get(left_hwnd) or _find_window(left_hwnd)
    elif use_pinned and _pinned_target:
        left_w = _find_window(_pinned_target)
    if left_w is None:
        if not cands:
            logger.warning("没有可用于并排的窗口")
            return False
        left_w = cands[0]
    left_hwnd = left_w.get("hwnd") or make_hwnd(left_w["owner"], left_w["name"] or "")

    # 解析右窗口：显式指定 > 与左窗口不同的最前面窗口
    right_w = None
    if right_hwnd and right_hwnd != left_hwnd:
        right_w = cands_by_hwnd.get(right_hwnd) or _find_window(right_hwnd)
    if right_w is None:
        for w in cands:
            if w["hwnd"] != left_hwnd:
                right_w = w
                break
    if right_w is None:
        logger.warning("只找到一个可用窗口，无法并排")
        return False
    right_hwnd = right_w.get("hwnd") or make_hwnd(right_w["owner"], right_w["name"] or "")

    # 以左侧窗口当前所在的屏幕为准；若显式指定了显示器则用指定的
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, left_w["x"], left_w["y"], left_w["w"], left_w["h"])
    mon = ms[idx]

    # 登记几何，供后续 get_window_rect 使用
    _last_rect[left_hwnd] = (left_w["x"], left_w["y"], left_w["w"], left_w["h"])
    _last_rect[right_hwnd] = (right_w["x"], right_w["y"], right_w["w"], right_w["h"])

    wl, wt, ww, wh = mon.work_rect
    if mon.height > mon.width:
        # 竖屏：左右并排改为上/下堆叠（上半屏 / 下半屏）
        half_h = wh // 2
        set_window_rect(left_hwnd, wl, wt, ww, half_h, activate=True)
        set_window_rect(right_hwnd, wl, wt + half_h, ww, wh - half_h, activate=True)
        logger.info("竖屏并排(上下堆叠): 上=%s 下=%s（屏幕 %s）",
                    left_hwnd, right_hwnd, mon.device_name)
    else:
        half = ww // 2
        # 并排后激活左右两个窗口，让它们显示到所有窗口最前面（可见、不被遮挡）
        set_window_rect(left_hwnd, wl, wt, half, wh, activate=True)
        set_window_rect(right_hwnd, wl + half, wt, ww - half, wh, activate=True)
        logger.info("并排完成: 左=%s 右=%s（屏幕 %s）", left_hwnd, right_hwnd,
                    mon.device_name)
    return True


def snap_three_stack(top_hwnd=None, mid_hwnd=None, bot_hwnd=None, use_pinned=True, monitor=None):
    """把三个窗口堆叠到同一屏的上/中/下三栏（竖屏排列）。

    top/mid/bot_hwnd 为 "应用::窗口" 标识；未指定时自动取最前面的三个窗口
    （顶部优先用界面固定的目标窗口）。monitor 可指定目标显示器
    （int 索引或 device_name 字符串）；为 None 时以顶部窗口当前所在屏幕为准。
    排列完成后把三个窗口激活到最前，确保结果可见、不被遮挡。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return False

    cands = [w for w in list_target_windows()
             if w["pid"] not in _OWN_PIDS and w["owner"]]
    cands_by_hwnd = {w["hwnd"]: w for w in cands}

    # 解析顶部窗口：显式指定 > 界面固定目标 > 最前面窗口
    top_w = None
    if top_hwnd:
        top_w = cands_by_hwnd.get(top_hwnd) or _find_window(top_hwnd)
    elif use_pinned and _pinned_target:
        top_w = _find_window(_pinned_target)
    if top_w is None:
        if not cands:
            logger.warning("没有可用于三栏排列的窗口")
            return False
        top_w = cands[0]
    top_hwnd = top_w.get("hwnd") or make_hwnd(top_w["owner"], top_w["name"] or "")

    # 解析中部窗口：显式指定 > 与顶部不同的最前面窗口
    mid_w = None
    if mid_hwnd and mid_hwnd != top_hwnd:
        mid_w = cands_by_hwnd.get(mid_hwnd) or _find_window(mid_hwnd)
    if mid_w is None:
        for w in cands:
            if w["hwnd"] != top_hwnd:
                mid_w = w
                break
    if mid_w is None:
        logger.warning("只找到一个可用窗口，无法三栏排列")
        return False
    mid_hwnd = mid_w.get("hwnd") or make_hwnd(mid_w["owner"], mid_w["name"] or "")

    # 解析底部窗口：显式指定 > 与顶部/中部都不同的最前面窗口
    bot_w = None
    if bot_hwnd and bot_hwnd not in (top_hwnd, mid_hwnd):
        bot_w = cands_by_hwnd.get(bot_hwnd) or _find_window(bot_hwnd)
    if bot_w is None:
        for w in cands:
            h = w["hwnd"]
            if h != top_hwnd and h != mid_hwnd:
                bot_w = w
                break
    if bot_w is None:
        logger.warning("只找到两个可用窗口，无法三栏排列")
        return False
    bot_hwnd = bot_w.get("hwnd") or make_hwnd(bot_w["owner"], bot_w["name"] or "")

    # 以顶部窗口当前所在的屏幕为准；若显式指定了显示器则用指定的
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, top_w["x"], top_w["y"], top_w["w"], top_w["h"])
    mon = ms[idx]

    wl, wt, ww, wh = mon.work_rect
    third = wh // 3
    set_window_rect(top_hwnd, wl, wt, ww, third, activate=False)
    # 实测闭环：System Events 对窗口几何有系统级钳制（标题栏占住的顶部约 30px
    # 让窗口永远贴不到 work_top，竖屏大高度变化也常只应用一部分）。若按理想
    # thirds 继续写中/下两栏，三栏之间会出现缝隙或重叠——表现为「竖排不准」。
    # 因此上栏写完后读回实测几何，用它推导中/下栏的真实起点。
    t_rect = get_window_rect(top_hwnd)
    t_y = t_rect[1] if (t_rect and t_rect != (0, 0, 0, 0)) else wt
    t_h = t_rect[3] if (t_rect and t_rect[3] > 0) else third
    used = max(1, (t_y + t_h) - wt)
    m_y = wt + used + 1
    m_h = max(1, third - 1)
    set_window_rect(mid_hwnd, wl, m_y, ww, m_h, activate=False)
    m_rect = get_window_rect(mid_hwnd)
    m_h_used = m_rect[3] if (m_rect and m_rect[3] > 0) else m_h
    b_y = m_y + m_h_used + 1
    b_h = max(1, (wt + wh) - b_y)
    set_window_rect(bot_hwnd, wl, b_y, ww, b_h, activate=False)
    _osa('tell application "' + top_w['owner'] + '" to activate')
    logger.info("三栏排列完成: 上=%s 中=%s 下=%s（屏幕 %s）", top_hwnd, mid_hwnd,
                bot_hwnd, mon.device_name)
    return True


def _is_portrait(mon):
    """屏幕是否为竖屏（高大于宽）。"""
    return mon.height > mon.width


# 最近一次四宫格排列的实测报告：[(hwnd, 目标矩形, 实测矩形或None), ...]
# 供界面说明「哪些格子被应用的最小窗口尺寸钳制了」
_last_quad_report = []


# 最近一次四宫格实际使用的比例 (split_x, split_y)，智能模式下即程序算出的值，
# 界面读它把拖拽分割线同步到真实比例
_last_quad_splits = (0.5, 0.5)


# 最近一次四宫格的实际分配顺序 [左上, 右上, 左下, 右下]（hwnd）。
# 智能模式会按窗口宽高比重排四个角，界面必须回报「谁最终去了哪个角」，
# 否则用户看到的下拉顺序与实际摆放不一致，就是「不准」。
_last_quad_order = [None, None, None, None]


# 最近一次四宫格需要如实告知用户的情况（屏幕装不下四个最小尺寸、某窗口
# 被系统判给了别的屏……）。界面读它在状态栏里提示，而不是只说「排列完成」。
_last_quad_notes = []


def fill_all_monitors(hwnds=None, use_pinned=True, mode="max",
                      monitor_indices=None):
    """把若干窗口分别铺满各自的显示器，一个窗口一块屏。

    典型场景是四屏环境：四个窗口各占一块屏。已有能力都是「单屏内」排布
    （并排 / 竖排 / 四宫格），缺的是跨屏——要逐屏铺满得先跨屏移动再最大化，
    点好几次。

    hwnds: 显式指定窗口列表；为 None 时取最前面的若干个（数量 = 屏幕数，
    首个优先用界面固定的目标窗口）。窗口数少于屏幕数时按窗口数铺（多出来的屏
    不动），多于屏幕数时忽略多余窗口，绝不超屏摆放。

    mode: "max" 每块屏铺满整个工作区；"quad" 每块屏左上角四分之一。

    monitor_indices: 只铺这些下标的屏（按给定顺序）。None 时铺全部屏。
    竖屏常常不适合放全屏窗口（竖屏上的横屏应用会被压成一条），所以在界面上
    默认只勾横屏，用户可自行增删。

    窗口按枚举顺序与显示器一一对应（第 k 个窗口去第 k 块屏），这样「屏一 →
    屏二 → 屏三」的次序与界面显示器列表一致，结果可预期。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return 0

    if monitor_indices is not None:
        picked = []
        for i in monitor_indices:
            if isinstance(i, int) and 0 <= i < len(ms) and i not in picked:
                picked.append(i)
        ms = [ms[i] for i in picked]
        if not ms:
            logger.warning("没有可铺的目标屏")
            return 0

    if hwnds is None:
        cands = [w for w in list_target_windows()
                 if w["pid"] not in _OWN_PIDS and w["owner"]]
        picked = []
        seen = set()
        # 界面固定的目标窗口优先放在第一位（只有它，多个固定目标没有意义）
        first = _find_window(_pinned_target) if use_pinned and _pinned_target else None
        if first is not None:
            h = first.get("hwnd") or make_hwnd(first["owner"], first["name"] or "")
            picked.append(h)
            seen.add(h)
        for w in cands:
            if len(picked) >= len(ms):
                break
            h = w.get("hwnd") or make_hwnd(w["owner"], w["name"] or "")
            if h in seen:
                continue
            picked.append(h)
            seen.add(h)
        hwnds = picked

    if not hwnds:
        logger.warning("没有可用于跨屏铺满的窗口")
        return 0

    done = 0
    for hwnd, mon in zip(list(hwnds), ms):
        if not hwnd:
            continue
        try:
            if mode == "quad":
                wl, wt, ww, wh = mon.work_rect
                set_window_rect(hwnd, wl, wt, ww // 2, wh // 2, activate=False)
            else:
                set_window_rect(hwnd, mon.work_left, mon.work_top,
                                mon.work_width, mon.work_height, activate=False)
            done += 1
        except Exception:  # noqa: BLE001
            logger.warning("跨屏铺满失败: hwnd=%s 屏=%s", hwnd, mon.device_name,
                           exc_info=True)
    logger.info("跨屏铺满完成: %d 个窗口 -> %d 块屏（mode=%s）",
                done, len(ms), mode)
    return done

# ── 四宫格：最小尺寸实测与几何 ─────────────────────────────────────────

# 探测最小尺寸时请求的尺寸：宽高都明显小于常见 App 的下限，读回的就是下限
_PROBE_W, _PROBE_H = 320, 120

# 探测写入后等动画的时间（真机实测 0.3s 已稳定，留点余量）
_probe_settle_secs = 0.45


def _fresh_rect(hwnd):
    """绕过 _last_rect 缓存读一次真实几何。

    get_window_rect 命中缓存时直接返回缓存值，而缓存可能是写入过程中的旧值 /
    动画中间帧。四宫格的闭环读回必须用实测值，否则读到的是自己刚写的目标值，
    察觉不到「窗口被系统搬到了别的屏」。
    """
    _last_rect.pop(hwnd, None)
    try:
        got = get_window_rect(hwnd)
    except Exception:  # noqa: BLE001
        return (0, 0, 0, 0)
    return got if got else (0, 0, 0, 0)


def _probe_min_size(hwnd, at_x, at_y):
    """实测一个窗口的最小可用尺寸：写一个小尺寸，读回被 App 钳制后的几何。

    macOS 上查不到这个值——真机实测 ChatGPT / Qoder CN 等窗口根本没有
    AXMinSize 属性，只能写小再读回。返回 (min_w, min_h)；读不到时返回 (0, 0)。

    位置先挪到目标屏工作区左上角再缩小：App 的最小尺寸会把窗口撑回去，
    贴着屏幕底边缩小时撑回去的那部分就出了屏，macOS 会把整个窗口判给别的屏
    （真机实测 RV200 Pro 上 ChatGPT y=-200 被搬到主屏）——探测不能反过来把
    窗口搬走。写完留下的几何无所谓，紧接着的正式排布会把它放进自己的格子。
    """
    app, name, _seq = parse_hwnd(hwnd)
    sel = (_ax_window_by_geometry(app, _last_rect.get(hwnd), name)
           or _ax_window_selector(hwnd) or "window 1")
    script = (
        'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    set position of {sel} to {{{int(at_x)}, {int(at_y)}}}\n'
        f'    set size of {sel} to {{{_PROBE_W}, {_PROBE_H}}}\n'
        '  end tell\n'
        'end tell\n'
    )
    # 真机实测：同一个 App 用同一个小尺寸写入，读回值不稳定——有时是钳制后的
    # 下限、有时是写入过程里的中间值（还带着旧的大尺寸）。所以探两次取逐维更小
    # 的那个：写小读回大，说明这一轮还没落到位，取更小的那次才接近真实下限。
    best = None
    for _ in range(2):
        _osa(script)
        time.sleep(_probe_settle_secs)
        got = _fresh_rect(hwnd)
        if not got or got == (0, 0, 0, 0):
            continue
        cur = (got[2], got[3])
        if cur[0] <= 0 or cur[1] <= 0:
            continue
        best = cur if best is None else (min(best[0], cur[0]),
                                         min(best[1], cur[1]))
    if best is None:
        logger.warning("最小尺寸探测读不到几何: %s", hwnd)
        return (0, 0)
    return best


def _pick_quad_screen(ms, cur_idx, corners):
    """给四宫格挑一块放得下四格最小尺寸的屏；没有合适的就返回当前屏。

    当前屏放得下就不换，避免窗口白跑一趟。要换时只在横屏里挑——竖屏上排四个
    窗口并不好用（用户明确说过「不适合竖屏的窗口不要发到竖屏屏幕」）。
    优先换到完全放得下的横屏；一块都放不下时，也要离开竖屏，退而选面积最大
    的横屏——留一点重叠（允许上下重叠即可排下）也比把四个窗口挤在竖屏上好，
    这正是用户报的场景：竖屏 DELL 上四宫格挤成一团。
    """
    need_w, need_h = quad_plan.quad_requirements(corners)
    cur = ms[cur_idx]
    cur_wl, _t, cur_ww, cur_wh = cur.work_rect
    cur_fits = need_w <= cur_ww and need_h <= cur_wh
    if cur_fits and cur.height <= cur.width:
        return cur_idx
    fits, landscape = [], []
    for i, m in enumerate(ms):
        if m.height > m.width:      # 竖屏：不主动送
            continue
        _ml, _mt, mw, mh = m.work_rect
        landscape.append(i)
        if need_w <= mw and need_h <= mh:
            fits.append(i)
    # 完全放得下的横屏优先；都放不下就退到面积最大的横屏（离开竖屏）。
    # 横屏一块都没有（例如只有一块竖屏外接）才维持原地。
    pool = fits or landscape
    if not pool:
        return cur_idx
    return max(pool, key=lambda i: ms[i].work_rect[2] * ms[i].work_rect[3])


def snap_four_quad(tl_hwnd=None, tr_hwnd=None, bl_hwnd=None, br_hwnd=None,
                   use_pinned=True, monitor=None, split_x=0.5, split_y=0.5,
                   smart=False):
    """把四个窗口排到同一屏的 2×2 四宫格（左上/右上/左下/右下）。

    与单窗的四等分吸附（snap 的 quad-*）配套：那四个按钮一次只动一个窗口，
    本函数一次摆四个窗口。

    split_x / split_y 给出分割比例（0~1，0.5 = 四等分）。等分并不总是最合适：
    左栏放终端想窄一点、右栏放浏览器想宽一点是常见需求，所以界面上做了可
    拖拽的分割线，把这两个比例一起传进来。

    smart=True 时改用智能分配：传入哪些窗口都行，谁去哪个角、用什么比例
    都由 quad_plan.plan_quad_smart 按各窗口的宽高比算出来。此时 tl/tr/bl/br
    只是「参与排列的四个窗口」（顺序不限），split_x/split_y 作为兜底值。

    四个 hwnd 均可显式指定；未指定时自动取最前面的四个窗口（左上优先用界面
    固定的目标窗口）。monitor 可指定目标显示器（int 索引或 device_name 字符串）；
    为 None 时以左上窗口当前所在屏幕为准，该屏放不下四格最小尺寸时自动换一块
    放得下的横屏。

    排布前先实测每个窗口的最小尺寸：App 的最小尺寸（真机 ChatGPT 高 600、
    Qoder CN 540）会把窗口撑出我们给的格子，撑过屏幕底边后 macOS 会把整个
    窗口判给别的屏——这就是「四个窗口排完不在一个屏幕上」的根因，不是坐标算错。
    四格按「比例切分值」与「窗口最小尺寸」取大者，并钉在工作区内：上排贴顶、
    下排贴底、左列贴左、右列贴右，最小尺寸把哪一格撑大就往外撑，绝不越界。
    工作区实在装不下四个最小尺寸时，才允许上下 / 左右重叠，并在
    _last_quad_notes 里如实说明（界面据此提示用户）。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return False

    # 上一轮的说明先清空：界面读 _last_quad_notes 做提示，留着旧值会把上次的
    # 「装不下 / 被搬屏」报在这一次成功的排列上
    global _last_quad_notes
    _last_quad_notes.clear()

    cands = [w for w in list_target_windows()
             if w["pid"] not in _OWN_PIDS and w["owner"]]
    cands_by_hwnd = {w["hwnd"]: w for w in cands}

    picked, seen = [], set()
    for want in (tl_hwnd, tr_hwnd, bl_hwnd, br_hwnd):
        w = None
        if want:
            w = cands_by_hwnd.get(want) or _find_window(want)
        elif use_pinned and _pinned_target and _pinned_target not in seen:
            w = _find_window(_pinned_target)
        if w is None:
            for c in cands:
                h = c.get("hwnd") or make_hwnd(c["owner"], c["name"] or "")
                if h not in seen:
                    w = c
                    break
        if w is None:
            logger.warning("可用窗口不足四个，无法四宫格排列")
            return False
        h = w.get("hwnd") or make_hwnd(w["owner"], w["name"] or "")
        # 登记 CG 侧几何：set_window_rect 靠它在 AX 窗口列表里认出这个窗口
        # （CG 枚举序与 AX 序不一致时，纯按序号会选到别的窗口）
        _last_rect[h] = (w["x"], w["y"], w["w"], w["h"])
        picked.append((h, w))
        seen.add(h)

    tl_hwnd, tl_w = picked[0]
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, tl_w["x"], tl_w["y"],
                                     tl_w["w"], tl_w["h"])
    if idx is None or not (0 <= idx < len(ms)):
        idx = 0
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect

    # 1) 实测四个窗口的最小尺寸
    mins = {}
    for h, _w in picked:
        mins[h] = _probe_min_size(h, wl + 1, wt + 1)
    corners = [(mins[h][0], mins[h][1]) for h, _w in picked]
    need_w, need_h = quad_plan.quad_requirements(corners)
    logger.info("四格最小尺寸需求 %dx%d（屏幕 %s %dx%d）：%s",
                need_w, need_h, mon.device_name, ww, wh,
                "、".join("%s=%dx%d" % (h.split("::")[0], mins[h][0], mins[h][1])
                          for h, _w in picked))

    # 2) 选屏：用户点名了屏就尊重它（放不下也尽力排在上面并如实说明）；
    #    「自动」时该屏放不下就换一块放得下的横屏，竖屏不主动送。
    # 不仅「装不下」要换屏：当前屏是竖屏时也换——用户明确说过不适合竖屏的
    # 窗口不要发到竖屏屏幕，四宫格在竖屏上必挤（真机 DELL U2414H 上实测）。
    if monitor is None and (need_w > ww or need_h > wh or wh > ww):
        better = _pick_quad_screen(ms, idx, corners)
        if better != idx:
            logger.info("屏幕 %s（%dx%d）不适合四宫格（四格最小尺寸 %dx%d），"
                        "改排到 %s",
                        mon.device_name, ww, wh, need_w, need_h,
                        ms[better].device_name)
            idx = better
            mon = ms[idx]
            wl, wt, ww, wh = mon.work_rect

    # 3) 比例与四个角的归属
    sx, sy = float(split_x), float(split_y)
    if smart:
        # 智能分配：按各窗口的宽高比重排四个角的归属，并重算分割比例。
        # 直接用 picked 里的 w（带 x/y/w/h）做判据，不再重新枚举——重新拉列表
        # 会拿到不同的顺序与几何，重排时按 (owner, name) 匹配就会错位
        # （真机实测 plan 说豆包去 TL，实际 ChatGPT 去了 TR）。
        try:
            planned, sx2, sy2 = quad_plan.plan_quad_smart(
                [dict(w, hwnd=h, min_w=mins[h][0], min_h=mins[h][1])
                 for h, w in picked], ww, wh)
        except ValueError as e:  # noqa: BLE001
            logger.warning("智能分配失败，退回手动比例: %s", e)
        else:
            # planned 里的元素就是传入的 dict（已带 hwnd 键），直接取
            reordered = [(w.get("hwnd"), w) for w in planned]
            if all(h for h, _w in reordered) and len(reordered) == 4:
                picked = reordered
                sx, sy = sx2, sy2
                logger.info("智能分配结果: 左上=%s 右上=%s 左下=%s 右下=%s "
                            "（横向 %.0f%% / 竖向 %.0f%%）",
                            picked[0][0], picked[1][0], picked[2][0],
                            picked[3][0], sx * 100, sy * 100)
            else:
                logger.warning("智能分配结果异常，保留手动顺序")
    # 比例钳在安全区间：贴边会让某一格只剩几个像素，等于把窗口藏起来
    sx = min(max(sx, 0.15), 0.85)
    sy = min(max(sy, 0.15), 0.85)
    c1, c2, r1, r2, ov_x, ov_y = quad_plan.grid_geometry(
        ww, wh, sx, sy, [(mins[h][0], mins[h][1]) for h, _w in picked])
    # 实际生效的比例：格子被最小尺寸抬高后与传入的 sx/sy 已有出入，界面上的
    # 分割线必须按实际值显示，否则用户看到的比例与排出来的不符
    global _last_quad_splits
    _last_quad_splits = (min(max(c1 / float(ww or 1), 0.15), 0.85),
                         min(max(r1 / float(wh or 1), 0.15), 0.85))

    notes = []

    wr, wb = wl + ww, wt + wh          # 工作区右 / 下边界
    min_right_w = max(mins[picked[1][0]][0], mins[picked[3][0]][0])
    min_bot_h = max(mins[picked[2][0]][1], mins[picked[3][0]][1])
    min_bl_w = mins[picked[2][0]][0]

    # 上排贴工作区左 / 顶，右列贴着左列的**实测**右边缘起排。
    # 用实测值（而不是只按算好的 c1）是必须的：左列 App 有最小窗口宽度
    # （实测 ChatGPT 请求 454 读回 480），左列比格子宽时右列按理想值排会互相
    # 压盖；左列比格子窄时按理想值排又会在两列之间留下一道空洞。
    set_window_rect(picked[0][0], wl, wt, c1, r1, activate=False)
    tl_rect = _fresh_rect(picked[0][0])
    right_x = wr - c2
    if tl_rect and tl_rect != (0, 0, 0, 0) and tl_rect[2] > 0:
        right_x = tl_rect[0] + tl_rect[2] + 1
    right_x = max(wl + 1, right_x)
    # 右列必须留够它自己的最小宽度，否则右列窗口会被撑出工作区右边——
    # 越出屏幕的窗口会被 macOS 判给别的屏。装不下时宁可让两列重叠。
    if min_right_w and right_x + min_right_w > wr:
        right_x = max(wl + 1, wr - min_right_w)
    right_x = min(right_x, max(wl + 1, wr - 1))
    right_w = max(1, wr - right_x)
    set_window_rect(picked[1][0], right_x, wt, right_w, r1, activate=False)
    # 下排左列与实测左列对齐
    half_w = max(1, right_x - wl, min_bl_w)
    # 下排起点：先贴住上排**实测**底边（App 的最小高度会让上排缩不到理想值，
    # 按理想值写下排两排会重叠上百 px）。但下排自己也有最小高度，高度不足时
    # 它会为了满足下限往下撑、越过屏幕底边——那正是 macOS 把整个窗口搬到
    # 别的屏的触发条件。所以下排高度无论如何都要保住自己的下限，宁可让两排
    # 重叠：重叠只是难看，被搬走才是用户报的故障。
    top_bottom = wt + r1
    for h, _w in picked[:2]:
        rect = _fresh_rect(h)
        if rect and rect != (0, 0, 0, 0) and rect[3] > 0:
            top_bottom = max(top_bottom, rect[1] + rect[3])
    bot_y = top_bottom + 1
    if bot_y + min_bot_h > wb:
        bot_y = max(wt + 1, wb - min_bot_h)
    bot_y = min(bot_y, max(wt + 1, wb - 1))
    bot_h = max(1, wb - bot_y)
    set_window_rect(picked[2][0], wl, bot_y, half_w, bot_h, activate=False)
    # 下排左格同样可能被 App 的最小宽度撑宽（与上排左格同源）。若右下格仍按
    # 理想 half_w 起排，两格会互相压盖——用户看到的就是「右下窗口叠在左下上」。
    bl_rect = _fresh_rect(picked[2][0])
    br_x = wl + half_w
    if bl_rect and bl_rect != (0, 0, 0, 0) and bl_rect[2] > 0:
        br_x = max(br_x, bl_rect[0] + bl_rect[2] + 1)
    # 与上排同源：右下格也要留够自己的最小宽度，越出工作区右边同样会被搬屏
    if min_right_w and br_x + min_right_w > wr:
        br_x = max(wl + 1, wr - min_right_w)
    br_x = min(max(br_x, wl + 1), max(wl + 1, wr - 1))
    br_w = max(1, wr - br_x)
    set_window_rect(picked[3][0], br_x, bot_y, br_w, bot_h, activate=False)
    logger.info("四宫格排列完成: 左上=%s 右上=%s 左下=%s 右下=%s（屏幕 %s，"
                "横向 %.0f%% / 竖向 %.0f%%）",
                picked[0][0], picked[1][0], picked[2][0], picked[3][0],
                mon.device_name, _last_quad_splits[0] * 100,
                _last_quad_splits[1] * 100)
    # 逐个读回报「实测 vs 目标」偏差：App 的最小窗口尺寸（Electron 类常见）
    # 会让某一格缩不到目标值，呈现为「不准」。这不是坐标算错，把偏差写进
    # 日志和返回值，界面才能如实说明「哪些格子被应用钳制了」。
    report = []
    boxes = [(wl, wt, c1, r1),
             (right_x, wt, right_w, r1),
             (wl, bot_y, half_w, bot_h),
             (br_x, bot_y, br_w, bot_h)]
    for (h, _w), want in zip(picked, boxes):
        got = _fresh_rect(h)
        if not got or got == (0, 0, 0, 0):
            report.append((h, want, None))
            notes.append("%s 读不到几何" % h.split("::")[0])
            continue
        dev = max(abs(g - t) for g, t in zip(got, want))
        report.append((h, want, got))
        # 窗口被系统判给了别的屏：中心点不在目标屏范围内。这是「四窗不在
        # 一个屏幕上」的直接证据，必须报出来，不能记成「被最小尺寸钳制」。
        cx, cy = got[0] + got[2] / 2.0, got[1] + got[3] / 2.0
        if not (mon.left <= cx < mon.left + mon.width
                and mon.top <= cy < mon.top + mon.height):
            notes.append("%s 被系统移到了别的屏（实测 %s）"
                         % (h.split("::")[0], got))
            logger.warning("四宫格：%s 被移到别的屏 -> %s", h, got)
        elif dev > 8:
            logger.info("四宫格偏差 %s：目标=%s 实测=%s（最大偏差 %d px，"
                        "可能被应用的最小窗口尺寸钳制）", h, want, got, dev)
    global _last_quad_report
    _last_quad_report = report
    global _last_quad_order
    _last_quad_order = [h for h, _w in picked]
    # 重叠量用实测值算：前面 ov_x / ov_y 是理想几何的估计，而 App 的最小尺寸、
    # 标题栏占位都会让窗口实际比格子大，只有实测才反映用户看到的重叠。
    # 上排 = 顶边较小的两格、下排 = 其余两格（摆放时就是按这个顺序写的）。
    live = [r for _h, _w, r in report if r]
    if len(live) == 4:
        rows = sorted(live, key=lambda r: r[1])
        cols = sorted(live, key=lambda r: r[0])
        real_ov_y = max(0, max(r[1] + r[3] for r in rows[:2])
                        - min(r[1] for r in rows[2:]))
        real_ov_x = max(0, max(r[0] + r[2] for r in cols[:2])
                        - min(r[0] for r in cols[2:]))
        if real_ov_x or real_ov_y:
            notes.append("四格没完全排开：实测上下重叠 %dpx、左右重叠 %dpx"
                         % (real_ov_y, real_ov_x))
            logger.warning("四宫格：%s", notes[-1])
    # 就地清空再填充（而不是重新绑定）：本函数顶部已用 global 声明过它，
    # 这里再声明一次会报「name used prior to global declaration」。就地改内容
    # 让界面持有的引用（若有）也同步更新。
    _last_quad_notes[:] = notes
    return True


def monitors_list_snapshot():
    return monitors.enum_monitors()


def distribute_app_windows(owner_pattern, counts=None, activate=True):
    """把指定应用的所有窗口分配到各显示器，每屏内上下等分堆叠。

    owner_pattern: 应用名（不区分大小写子串匹配），如 "UU远程"。
    counts: 每屏分几个窗口（按显示器枚举顺序），如 [3, 3] 表示
            第 1 块屏 3 个、第 2 块屏 3 个；为 None 时按窗口数平均分配。
    返回 (移动成功的窗口数, 失败的窗口数)。

    每屏内部的堆叠方向按屏幕朝向自动选择：
    - 竖屏（高>宽）：上下等分（一列多行）
    - 横屏：左右等分（一行多列）
    """
    ms = monitors.enum_monitors()
    if not ms:
        return 0, 0
    pat = (owner_pattern or "").lower()
    wins = [w for w in list_target_windows()
            if w["owner"] and pat in w["owner"].lower()
            and w["pid"] not in _OWN_PIDS]
    if not wins:
        logger.warning("没有找到应用 %r 的窗口", owner_pattern)
        return 0, 0

    # 分配每屏窗口数
    if counts is None:
        n = len(wins)
        base, extra = divmod(n, len(ms))
        counts = [base + (1 if i < extra else 0) for i in range(len(ms))]
    counts = list(counts)

    ok, miss = 0, 0
    wi = 0
    for mi, mon in enumerate(ms):
        if wi >= len(wins):
            break
        k = counts[mi] if mi < len(counts) else 0
        group = wins[wi:wi + k]
        wi += k
        if not group:
            continue
        wl, wt, ww, wh = mon.work_rect
        portrait = mon.height > mon.width
        n = len(group)
        rects = []
        for j, w in enumerate(group):
            if portrait:
                # 竖屏：上下等分（第 j 行）
                slot_h = wh // n
                y = wt + j * slot_h
                h = slot_h if j < n - 1 else wh - j * slot_h
                rects.append((wl, y, ww, h))
            else:
                # 横屏：左右等分（第 j 列）
                slot_w = ww // n
                x = wl + j * slot_w
                wid = slot_w if j < n - 1 else ww - j * slot_w
                rects.append((x, wt, wid, wh))
        # 分两轮写：先统一改尺寸、再统一改位置。
        # 一次写入里 position+size 同时下发时，系统按写入前的旧位置/旧尺寸
        # 做边界钳制（例如窗口在屏外时宽度钳到 680、贴边后没再拉到目标宽），
        # 导致竖屏整宽 1152 落到 680、横屏 504 落到最小宽度 698。拆开两轮后
        # 第二轮写入时窗口已在新位置/新尺寸，钳制基准正确。
        for w, rect in zip(group, rects):
            try:
                set_window_rect(w["hwnd"], w["x"], w["y"], rect[2], rect[3],
                                activate=False)
            except Exception:  # noqa: BLE001
                pass
        for w, rect in zip(group, rects):
            try:
                set_window_rect(w["hwnd"], rect[0], rect[1], rect[2], rect[3],
                                activate=False)
                ok += 1
            except Exception:  # noqa: BLE001
                miss += 1
    if activate and wins:
        app = wins[0]["owner"]
        _osa(f'tell application "{app}" to activate')
    logger.info("跨屏分配: 应用=%s 分配=%s 成功=%d 失败=%d",
                owner_pattern, counts, ok, miss)
    return ok, miss


def move_active_to_next_monitor(direction=1, use_pinned=True, activate=True):
    ms = monitors.enum_monitors()
    if not ms:
        return
    hwnd = get_foreground_window(use_pinned=use_pinned)
    if not hwnd:
        logger.warning("未找到可操作的前台窗口（可能只剩本程序自己）")
        return
    idx = _monitor_by_relative(ms, hwnd)
    n = len(ms)
    nxt = (idx + direction) % n
    move_to_monitor(hwnd, ms[nxt], activate=activate)


def snap_active(zone, use_pinned=True, activate=True):
    """分屏。

    activate=False 时只移动窗口、不把它激活到最前 —— 界面按钮用此模式，
    这样管理器窗口不会被目标窗口挤到后面，也就无需常驻置顶。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return
    hwnd = get_foreground_window(use_pinned=use_pinned)
    if not hwnd:
        logger.warning("未找到可操作的前台窗口（可能只剩本程序自己）")
        return
    idx = _monitor_by_relative(ms, hwnd)
    snap(hwnd, ms[idx], zone, activate=activate)


if __name__ == "__main__":
    print("own pids:", _OWN_PIDS)
    print("front external:", front_external_window())
    hwnd = get_foreground_window()
    print("front window:", hwnd)
    if hwnd:
        print("rect:", get_window_rect(hwnd))
