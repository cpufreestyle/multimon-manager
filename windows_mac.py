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

import monitors_mac as monitors
from monitors_mac import CGRect

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

_last_rect = {}  # "app::winname" -> (x, y, w, h)，来自 CG 的精确几何

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
    """
    out = []
    seen = set()
    for w in list_windows_front_to_back():
        hwnd = f'{w["owner"]}::{w["name"]}'
        if hwnd in seen:
            continue
        seen.add(hwnd)
        title = f'{w["owner"]} — {w["name"]}' if w["name"] else w["owner"]
        # 带上尺寸，便于区分同名/小窗口
        label = f'{title}  ({w["w"]}×{w["h"]})'
        out.append({**w, "hwnd": hwnd, "label": label})
    return out


def _find_window(hwnd):
    """按 "app::name" 重新定位窗口，拿到最新几何（窗口可能被移动过）。"""
    if not hwnd:
        return None
    for w in list_windows_front_to_back():
        if f'{w["owner"]}::{w["name"]}' == hwnd:
            return w
    return None


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
        key = f'{w["owner"]}::{w["name"]}'
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

    app, name = hwnd.split("::", 1)
    # 优先走 CG：按 owner 匹配，精确匹配窗口名；同时记录该 App 最前的窗口作后备，
    # 避免标题含特殊字符/空格差异导致完全匹配不到。
    try:
        front = None
        for w in list_windows_front_to_back():
            if w["owner"] != app:
                continue
            if front is None:
                front = w
            if w["name"] == name:
                rect = (w["x"], w["y"], w["w"], w["h"])
                _last_rect[hwnd] = rect
                return rect
        if front is not None:
            rect = (front["x"], front["y"], front["w"], front["h"])
            _last_rect[hwnd] = rect
            return rect
    except Exception:  # noqa: BLE001
        pass

    # 回退：System Events 的 position+size（注意 bounds 在 macOS 当前不可用，恒报 -1728）
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    set p to position of window 1\n'
        f'    set s to size of window 1\n'
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


def set_window_rect(hwnd, x, y, w, h, activate=True):
    if not _suppress_undo:
        try:
            push_undo(hwnd, get_window_rect(hwnd))
        except Exception:  # noqa: BLE001
            pass
    """移动/缩放窗口。用 position + size（bounds 属性在当前 macOS 不可用）。

    部分 App 在 zoomed/全屏态或 Auto Layout 约束下会忽略 `set size`，故先退出
    zoom；改为先定位到目标位置再设尺寸（在正确位置更容易被 App 接受），最后读回
    实际几何校验，偏差过大时再补一次 size。
    """
    if not hwnd:
        return
    app, _win = hwnd.split("::", 1)
    x, y, w, h = int(round(x)), int(round(y)), int(round(w)), int(round(h))
    logger.info("移动窗口: 进程=%s -> x=%d y=%d w=%d h=%d", app, x, y, w, h)
    # 退出 zoom/全屏态，否则 size 变更可能被忽略
    _osa(
        f'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    if (count of windows) > 0 then\n'
        f'      try\n'
        f'        tell window 1 to if zoomed then set zoomed to false\n'
        f'      end try\n'
        f'    end if\n'
        f'  end tell\n'
        f'end tell\n'
    )
    # 先定位到目标位置，再调整尺寸（避免 App 在越界/缩放态拒绝 size）
    _osa(
        f'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    set position of window 1 to {{{x}, {y}}}\n'
        f'    set size of window 1 to {{{w}, {h}}}\n'
        f'  end tell\n'
        f'end tell\n'
    )
    # 读回实际几何校验；偏差过大则再补一次 size
    _last_rect.pop(hwnd, None)
    try:
        _, _, aw, ah = get_window_rect(hwnd)
    except Exception:  # noqa: BLE001
        aw = ah = None
    if aw is not None and (abs(aw - w) > 8 or abs(ah - h) > 8):
        logger.warning("尺寸未生效(期望 %dx%d 实际 %dx%d)，重试 size", w, h, aw, ah)
        _osa(
            f'tell application "System Events"\n'
            f'  tell process "{app}"\n'
            f'    set size of window 1 to {{{w}, {h}}}\n'
            f'  end tell\n'
            f'end tell\n'
        )
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
    """
    ms = monitors.enum_monitors()
    if not ms:
        return False

    cands = [w for w in list_windows_front_to_back()
             if w["pid"] not in _OWN_PIDS and w["owner"]]
    cands_by_hwnd = {f'{w["owner"]}::{w["name"]}': w for w in cands}

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
    left_hwnd = f'{left_w["owner"]}::{left_w["name"]}'

    # 解析右窗口：显式指定 > 与左窗口不同的最前面窗口
    right_w = None
    if right_hwnd and right_hwnd != left_hwnd:
        right_w = cands_by_hwnd.get(right_hwnd) or _find_window(right_hwnd)
    if right_w is None:
        for w in cands:
            if f'{w["owner"]}::{w["name"]}' != left_hwnd:
                right_w = w
                break
    if right_w is None:
        logger.warning("只找到一个可用窗口，无法并排")
        return False
    right_hwnd = f'{right_w["owner"]}::{right_w["name"]}'

    # 以左侧窗口当前所在的屏幕为准；若显式指定了显示器则用指定的
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, left_w["x"], left_w["y"], left_w["w"], left_w["h"])
    mon = ms[idx]

    # 登记几何，供后续 get_window_rect 使用
    _last_rect[left_hwnd] = (left_w["x"], left_w["y"], left_w["w"], left_w["h"])
    _last_rect[right_hwnd] = (right_w["x"], right_w["y"], right_w["w"], right_w["h"])

    wl, wt, ww, wh = mon.work_rect
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

    cands = [w for w in list_windows_front_to_back()
             if w["pid"] not in _OWN_PIDS and w["owner"]]
    cands_by_hwnd = {f'{w["owner"]}::{w["name"]}': w for w in cands}

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
    top_hwnd = f'{top_w["owner"]}::{top_w["name"]}'

    # 解析中部窗口：显式指定 > 与顶部不同的最前面窗口
    mid_w = None
    if mid_hwnd and mid_hwnd != top_hwnd:
        mid_w = cands_by_hwnd.get(mid_hwnd) or _find_window(mid_hwnd)
    if mid_w is None:
        for w in cands:
            if f'{w["owner"]}::{w["name"]}' != top_hwnd:
                mid_w = w
                break
    if mid_w is None:
        logger.warning("只找到一个可用窗口，无法三栏排列")
        return False
    mid_hwnd = f'{mid_w["owner"]}::{mid_w["name"]}'

    # 解析底部窗口：显式指定 > 与顶部/中部都不同的最前面窗口
    bot_w = None
    if bot_hwnd and bot_hwnd not in (top_hwnd, mid_hwnd):
        bot_w = cands_by_hwnd.get(bot_hwnd) or _find_window(bot_hwnd)
    if bot_w is None:
        for w in cands:
            h = f'{w["owner"]}::{w["name"]}'
            if h != top_hwnd and h != mid_hwnd:
                bot_w = w
                break
    if bot_w is None:
        logger.warning("只找到两个可用窗口，无法三栏排列")
        return False
    bot_hwnd = f'{bot_w["owner"]}::{bot_w["name"]}'

    # 以顶部窗口当前所在的屏幕为准；若显式指定了显示器则用指定的
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, top_w["x"], top_w["y"], top_w["w"], top_w["h"])
    mon = ms[idx]

    wl, wt, ww, wh = mon.work_rect
    third = wh // 3
    # 三栏排列后激活三个窗口，让它们显示到所有窗口最前面（可见、不被遮挡）
    set_window_rect(top_hwnd, wl, wt, ww, third, activate=True)
    set_window_rect(mid_hwnd, wl, wt + third, ww, third, activate=True)
    set_window_rect(bot_hwnd, wl, wt + 2 * third, ww, wh - 2 * third, activate=True)
    logger.info("三栏排列完成: 上=%s 中=%s 下=%s（屏幕 %s）", top_hwnd, mid_hwnd,
                bot_hwnd, mon.device_name)
    return True


def monitors_list_snapshot():
    return monitors.enum_monitors()


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
