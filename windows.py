"""窗口跨屏移动与分屏吸附（纯 ctypes / Win32）。"""
import ctypes
import logging
import os
import threading
from ctypes import wintypes

import monitors

import quad_plan
import grid_plan
import screen_math

logger = logging.getLogger(__name__)

# ── 撤销栈：记录每次移动/缩放前的窗口几何，供"撤销移动"使用（F7）──
_undo_stack = []
_undo_lock = threading.Lock()
_suppress_undo = False
# 「上次摆放位」记忆：窗口被本程序摆过后又被用户拖走时，可一键回到
# 上次被程序摆到的位置（Moom 的 Last Moom Frame 思路）。
_last_arranged = {}
_last_arranged_lock = threading.Lock()
# snap() 最近一次的实测报告：[(hwnd, want, got)]，供 UI 状态栏如实提示
# 「被应用最小/固定尺寸钳制，贴的是哪条边」。
_last_snap_report = []


def push_undo(hwnd, rect):
    """记录一次移动前几何（hwnd, (x, y, w, h)）；保留最近 20 步。"""
    with _undo_lock:
        _undo_stack.append((hwnd, tuple(rect)))
        if len(_undo_stack) > 20:
            _undo_stack.pop(0)


def remember_arranged(hwnd, rect):
    """记录窗口被程序摆到的位置（hwnd -> (x, y, w, h)），最多 40 个。"""
    with _last_arranged_lock:
        _last_arranged[hwnd] = tuple(rect)
        while len(_last_arranged) > 40:
            _last_arranged.pop(next(iter(_last_arranged)))


def last_arranged(hwnd):
    """返回该窗口上次被程序摆到的位置；没记录时返回 None。"""
    with _last_arranged_lock:
        return _last_arranged.get(hwnd)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi
try:
    dwmapi = ctypes.windll.dwmapi
except Exception:  # noqa: BLE001  # Vista 之前的系统没有 dwmapi
    dwmapi = None

# 64 位句柄参数必须显式声明，否则被当 32 位截断。
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(monitors.RECT)]
user32.GetWindowRect.restype = ctypes.c_bool
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
user32.SetWindowPos.restype = ctypes.c_bool
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = ctypes.c_bool
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = ctypes.c_bool

# 窗口枚举（供界面列出可选择的窗口）
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = ctypes.c_bool
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = ctypes.c_bool
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = ctypes.c_bool
if dwmapi is not None:
    dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
# 窗口的 DPI 感知级别（Win10 1607+）。老系统上这两个 API 不存在，
# 访问会抛 AttributeError，调用方已包 try/except，恒按「非感知」处理。
DPI_AWARENESS_PER_MONITOR = 2

# GetWindowLongPtrW 只在 64 位系统存在（32 位 Python 无 Ptr 版本）。
# 在模块加载时声明一次即可：ctypes 的 argtypes 是函数对象上的全局状态，
# 运行时反复改写会与热键线程/托盘线程的并发调用互相干扰。
_get_window_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
_get_window_long.restype = ctypes.c_ssize_t

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
psapi.GetModuleBaseNameW.argtypes = [
    wintypes.HANDLE, wintypes.HANDLE, ctypes.c_wchar_p, wintypes.DWORD]
psapi.GetModuleBaseNameW.restype = wintypes.DWORD

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

SW_RESTORE = 0x09
SW_MAXIMIZE = 0x03
SW_SHOWNOACTIVATE = 0x04

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_MAXIMIZE = 0x01000000
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = wintypes.HWND(-1)
HWND_NOTOPMOST = wintypes.HWND(-2)


# ── 本程序自身的进程白名单（窗口操作时应跳过自己） ────────────────────

_OWN_PIDS = set()


def register_own_pid(pid):
    """登记本应用的进程 PID（主进程自动登记，托盘子进程由 main.py 登记）。"""
    if pid:
        _OWN_PIDS.add(int(pid))


register_own_pid(os.getpid())


# ── 窗口筛选与几何补偿（Windows 端专有） ────────────────────────────

# 系统壳窗口 / 浮层的窗口类名：它们「可见且有标题」，但不该出现在可选列表里
_SHELL_CLASSES = {
    "Shell_TrayWnd",                            # 主任务栏
    "Shell_SecondaryTrayWnd",                   # 副屏任务栏
    "Progman",                                  # 桌面
    "WorkerW",
    "DV2ControlHost",                           # 开始菜单 / 操作中心
    "Windows.UI.Core.CoreWindow",
    "ApplicationManager_DesktopShellWindow",
    "ForegroundStaging",
    "Xaml_WindowedPopupClass",
    "Microsoft.Windows.Shell.RunDialog",
    "#32768",                                   # 弹出菜单
    "Tooltips_class32",
    "EdgeUiInputWndClass",
    "Shell_InputSwitchTopLevelWindow",
    "LivePreviewWndClass",
}

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14


def _class_name(hwnd):
    """返回窗口类名（用于过滤系统壳窗口）。"""
    try:
        buf = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(hwnd, buf, 256):
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    return ""


def is_cloaked(hwnd):
    """窗口是否被 DWM 隐藏（UWP 应用后台挂起 / 处于非当前虚拟桌面）。"""
    if dwmapi is None:
        return False
    try:
        v = ctypes.c_uint32()
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
        return hr == 0 and bool(v.value)
    except Exception:  # noqa: BLE001
        return False


def is_maximized(hwnd):
    """窗口当前是否处于最大化状态。"""
    try:
        return bool(_get_window_long(hwnd, GWL_STYLE) & WS_MAXIMIZE)
    except Exception:  # noqa: BLE001
        return False


def visible_rect(hwnd):
    """窗口的可见矩形（不含 DWM 透明边框）；取不到时退回 GetWindowRect。"""
    if dwmapi is not None:
        try:
            r = monitors.RECT()
            hr = dwmapi.DwmGetWindowAttribute(
                hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(r), ctypes.sizeof(r))
            if hr == 0:
                return r.as_tuple()
        except Exception:  # noqa: BLE001
            pass
    return get_window_rect(hwnd)


def frame_insets(hwnd):
    """返回 GetWindowRect 相对「可见区域」的四面不可见边框厚度 (l, t, r, b)。

    Windows 10/11 上普通窗口的 GetWindowRect 含一圈透明阴影边框（每边约 7px）。
    直接按它摆放，并排的两个窗口之间会露出一条缝隙，贴边时也会离屏幕边缘一截。
    用 DWMWA_EXTENDED_FRAME_BOUNDS 拿到真实可见矩形即可算出补偿量。
    最大化窗口 / 取不到属性时返回 (0, 0, 0, 0)。
    """
    if dwmapi is None:
        return (0, 0, 0, 0)
    try:
        outer = monitors.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(outer)):
            return (0, 0, 0, 0)
        inner = monitors.RECT()
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(inner), ctypes.sizeof(inner))
        if hr != 0:
            return (0, 0, 0, 0)
        l, t = inner.left - outer.left, inner.top - outer.top
        r, b = outer.right - inner.right, outer.bottom - inner.bottom
        # 异常值（第三方主题 / 极端缩放）直接放弃补偿，避免把窗口放歪
        if not (0 <= l <= 64 and 0 <= t <= 64 and 0 <= r <= 64 and 0 <= b <= 64):
            return (0, 0, 0, 0)
        return (l, t, r, b)
    except Exception:  # noqa: BLE001
        return (0, 0, 0, 0)


def window_is_per_monitor_aware(hwnd):
    """窗口所属进程是否 per-monitor DPI aware。

    只有这类窗口的**物理**尺寸是恒定的——挪到不同 DPI 的屏上它的像素尺寸不变、
    肉眼大小却变了。unaware / system-aware 的窗口由系统做位图缩放，不用我们管。
    """
    try:
        ctx = user32.GetWindowDpiAwarenessContext(hwnd)
        if not ctx:
            return False
        return (user32.GetAwarenessFromDpiAwarenessContext(ctx)
                == DPI_AWARENESS_PER_MONITOR)
    except Exception:  # noqa: BLE001
        return False


# 跨屏移动时是否自动调整窗口尺寸（保持视觉大小 / 装不下时缩小）。
# 界面暂不暴露开关，代码层面可用 set_resize_on_monitor_change(False) 关掉。
_resize_on_monitor_change = True


def set_resize_on_monitor_change(enabled):
    """跨屏移动时是否自动缩放窗口（跨 DPI 保持肉眼大小、目标屏装不下时缩小）。"""
    global _resize_on_monitor_change
    _resize_on_monitor_change = bool(enabled)
    logger.info("跨屏自动调整窗口尺寸: %s", _resize_on_monitor_change)


def fit_size_for_monitor(hwnd, src_m, dst_m, w, h, margin=0.96):
    """按目标屏调整窗口尺寸，返回 (new_w, new_h)。

    两步：
    1. **跨 DPI 保持视觉大小**：目标屏与源屏缩放比不同、且窗口是 per-monitor
       DPI aware（物理尺寸不随 DPI 变），按 DPI 比例缩放，肉眼大小保持一致；
    2. **装不下就缩小**：目标屏工作区比窗口小（例如从 4K 屏挪到 1080p 笔记本
       屏），再等比缩到留 4% 边距，避免窗口溢到屏幕外。
    """
    nw, nh = max(1, int(w)), max(1, int(h))
    if not _resize_on_monitor_change:
        return nw, nh
    scale = 1.0
    try:
        s_dpi = getattr(src_m, "dpi_x", 96) or 96
        d_dpi = getattr(dst_m, "dpi_x", 96) or 96
        if s_dpi != d_dpi and window_is_per_monitor_aware(hwnd):
            scale = d_dpi / s_dpi
    except Exception:  # noqa: BLE001
        scale = 1.0
    if scale != 1.0:
        nw, nh = max(1, int(round(nw * scale))), max(1, int(round(nh * scale)))
        logger.info("跨屏 DPI %s%% -> %s%%，窗口按 %.3f 缩放",
                    getattr(src_m, "scale_percent", 100),
                    getattr(dst_m, "scale_percent", 100), scale)
    aw, ah = dst_m.work_width, dst_m.work_height
    if aw > 0 and ah > 0 and (nw > aw or nh > ah):
        k = min(aw * margin / max(nw, 1), ah * margin / max(nh, 1), 1.0)
        old = (nw, nh)
        nw, nh = max(1, int(nw * k)), max(1, int(nh * k))
        logger.info("目标屏装不下 %s，缩小到 %s", old, (nw, nh))
    return nw, nh


def _verify_visible(hwnd, x, y, w, h, tol=2):
    """校验窗口可见矩形是否真的落到期望位置。

    最小尺寸限制、UIPI（目标进程权限更高）拦截时，SetWindowPos 仍会返回成功
    但窗口不动，这里用于识别这种情况并记日志。
    """
    try:
        rx, ry, rw, rh = visible_rect(hwnd)
    except Exception:  # noqa: BLE001
        return True
    return (abs(rx - x) <= tol and abs(ry - y) <= tol
            and abs(rw - w) <= tol and abs(rh - h) <= tol)


def get_foreground_window(use_pinned=True):
    """返回要操作窗口的整数 HWND。

    use_pinned=True（界面按钮）时优先用界面固定的目标窗口；未固定目标时
    取「最前面的非本程序窗口」——点按钮瞬间前台是本程序自己，直接用
    GetForegroundWindow 会把管理器自己分屏/移动（与 macOS 行为对齐）；
    use_pinned=False（全局快捷键）时始终作用于真正的活动窗口。
    """
    if use_pinned and _pinned_target:
        if _find_window(_pinned_target):
            return _pinned_target
    if use_pinned:
        w = front_external_window()
        if w:
            return w["hwnd"]
    return user32.GetForegroundWindow()


def is_window(hwnd):
    """句柄是否仍指向一个存在的窗口（窗口可能已被关闭）。"""
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def list_windows():
    """枚举当前可见的顶层窗口，返回 [(hwnd, title), ...]。

    仅保留可见且有标题的窗口；隐藏、最小化的窗口以及系统壳窗口
    （任务栏 / 桌面 / 开始菜单 / UWP 挂起窗口）会被过滤掉。
    """
    items = []

    @WNDENUMPROC
    def _cb(hwnd, _lparam):
        try:
            h = int(hwnd)
        except (TypeError, ValueError):
            return True
        if not h or not user32.IsWindowVisible(h):
            return True
        if user32.IsIconic(h) or is_cloaked(h):
            return True
        if _class_name(h) in _SHELL_CLASSES:
            return True
        n = user32.GetWindowTextLengthW(h)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        title = buf.value.strip()
        if title:
            items.append((h, title))
        return True

    user32.EnumWindows(_cb, 0)
    return items


def get_window_rect(hwnd):
    rect = monitors.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.as_tuple()


def set_window_rect(hwnd, x, y, w, h, activate=True, exact=False):
    """摆放窗口，返回 True 表示 SetWindowPos 成功。

    exact=True 时按「可见区域」对齐：补偿 Windows 10/11 的 DWM 透明阴影边框，
    让并排 / 贴边的窗口真正贴合（见 frame_insets）。
    """
    if not _suppress_undo:
        try:
            push_undo(hwnd, get_window_rect(hwnd))
        except Exception:  # noqa: BLE001
            pass
    _internal = _suppress_undo
    if exact:
        l, t, r, b = frame_insets(hwnd)
        x, y, w, h = x - l, y - t, w + l + r, h + t + b
    flags = SWP_NOZORDER | SWP_FRAMECHANGED
    if not activate:
        flags |= SWP_NOACTIVATE
    ok = user32.SetWindowPos(hwnd, None, int(x), int(y), int(w), int(h), flags)
    if activate:
        user32.SetForegroundWindow(hwnd)
    if ok and not _internal:
        try:
            remember_arranged(hwnd, (x, y, w, h))
        except Exception:  # noqa: BLE001
            pass
    return bool(ok)


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


def return_to_last_arranged(hwnd):
    """把窗口送回它上次被程序摆到的位置（第二层撤销 / snap back）。"""
    rect = last_arranged(hwnd)
    if not rect:
        return False
    global _suppress_undo
    _suppress_undo = True
    try:
        set_window_rect(hwnd, rect[0], rect[1], rect[2], rect[3], activate=False)
    finally:
        _suppress_undo = False
    return True


def _monitor_by_relative(monitors_list, src_hwnd):
    """根据窗口当前所在屏幕，返回其索引（交集最长优先）。"""
    x, y, w, h = get_window_rect(src_hwnd)
    return screen_math.monitor_index_by_intersection(monitors_list, x, y, w, h)


def move_to_monitor(hwnd, monitor, src=None, activate=True):
    """将窗口移动到目标显示器，保持相对位置比例。返回是否成功。

    src 为显示器列表快照；缺省时自动枚举（已有列表时应传入避免重复调用）。
    activate=False 时只移动不激活（界面按钮模式，管理器不会被挤到后面）。

    最大化窗口：先落到目标屏再重新最大化，跨屏后仍是最大化状态——此前直接
    SetWindowPos 会把最大化「打破」成一个铺满的普通大窗口。
    """
    x, y, w, h = get_window_rect(hwnd)
    if src is None:
        src = monitors_list_snapshot()
    idx = _monitor_by_relative(src, hwnd)
    src_m = src[idx] if idx < len(src) else src[0]
    if is_maximized(hwnd):
        prev = user32.GetForegroundWindow() if not activate else None
        user32.ShowWindow(hwnd, SW_RESTORE)
        set_window_rect(hwnd, monitor.work_left, monitor.work_top, w, h,
                        activate=False)
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        if prev:
            user32.SetForegroundWindow(prev)
        return True
    # 以下一律在「可见矩形」空间里算：GetWindowRect 含一圈透明阴影边框，
    # 铺满工作区的窗口用它比大小会高出几个像素，被误判成「装不下」而反复缩小。
    vx, vy, vw, vh = visible_rect(hwnd)
    # 相对位置源与目标都用工作区：此前混用「全屏矩形」与「工作区」，
    # 任务栏占据的那条会被当成可移动范围，导致跨屏后位置整体偏移。
    rel_x = (vx - src_m.work_left) / max(src_m.work_width, 1)
    rel_y = (vy - src_m.work_top) / max(src_m.work_height, 1)
    # 尺寸按目标屏修正：跨 DPI 保持肉眼大小，且保证装得进目标屏工作区
    nw, nh = fit_size_for_monitor(hwnd, src_m, monitor, vw, vh)
    new_x = monitor.work_left + rel_x * max(monitor.work_width - nw, 0)
    new_y = monitor.work_top + rel_y * max(monitor.work_height - nh, 0)
    ok = set_window_rect(hwnd, new_x, new_y, nw, nh,
                         activate=activate, exact=True)
    if not ok:
        logger.warning("move_to_monitor: SetWindowPos 失败 (hwnd=%s)", hwnd)
    return ok


def snap(hwnd, monitor, zone, activate=True):
    """将窗口吸附到目标显示器的某个区域。zone 取值:
    left/right/top/bottom/maximize/center，三分屏 left-third/middle-third/right-third，
    四等分 quad-tl/quad-tr/quad-bl/quad-br。

    activate=False 时只移动不激活目标窗口（界面按钮模式）：「最大化」退化为
    铺满工作区（真最大化会抢焦点）。

    摆放一律带 exact=True：补偿 Windows 10/11 的 DWM 透明边框，分屏无缝贴合。
    """
    wl, wt, ww, wh = monitor.work_rect
    x, y, w, h = get_window_rect(hwnd)
    # 最大化 / 最小化的窗口必须先还原，否则 SetWindowPos 不会改变它的几何
    need_restore = is_maximized(hwnd) or bool(user32.IsIconic(hwnd))
    # SW_RESTORE 会抢焦点；不激活模式先记下原前台窗口，摆放完再还回去
    prev = user32.GetForegroundWindow() if (need_restore and not activate) else None

    if zone == "maximize":
        if activate:
            # 真正最大化（含任务栏避让、动画与双击标题栏还原行为）
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
            return True
        if need_restore:
            user32.ShowWindow(hwnd, SW_RESTORE)
        ok = set_window_rect(hwnd, wl, wt, ww, wh, activate=False, exact=True)
        if prev:
            user32.SetForegroundWindow(prev)
        return ok

    if need_restore:
        user32.ShowWindow(hwnd, SW_RESTORE)
    ok = False
    targets = {
        "left": (wl, wt, ww // 2, wh),
        "right": (wl + ww // 2, wt, ww - ww // 2, wh),
        "top": (wl, wt, ww, wh // 2),
        "bottom": (wl, wt + wh // 2, ww, wh - wh // 2),
        "center": (wl + (ww - w) // 2, wt + (wh - h) // 2, w, h),
        "left-third": (wl, wt, ww // 3, wh),
        "middle-third": (wl + ww // 3, wt, ww // 3, wh),
        "right-third": (wl + 2 * (ww // 3), wt, ww - 2 * (ww // 3), wh),
        "quad-tl": (wl, wt, ww // 2, wh // 2),
        "quad-tr": (wl + ww // 2, wt, ww - ww // 2, wh // 2),
        "quad-bl": (wl, wt + wh // 2, ww // 2, wh - wh // 2),
        "quad-br": (wl + ww // 2, wt + wh // 2, ww - ww // 2, wh - wh // 2),
    }
    if zone not in targets:
        logger.warning("snap: 未知区域 %s", zone)
        return False
    tx, ty, tw, th = targets[zone]
    ok = set_window_rect(hwnd, tx, ty, tw, th, activate=activate, exact=True)
    # App 的最小/固定尺寸会钳制请求尺寸。读回实际尺寸后，把「用户要贴的
    # 那条边」重新钉到工作区边界（右列贴右缘、下排贴底缘），并如实记录。
    got = get_window_rect(hwnd)
    if got and got != (0, 0, 0, 0):
        ax, ay = screen_math.reanchor_edge(zone, got[0], got[1], got[2], got[3],
                                           wl, wt, ww, wh)
        if (ax, ay) != (got[0], got[1]):
            set_window_rect(hwnd, ax, ay, got[2], got[3],
                            activate=activate, exact=True)
            final = get_window_rect(hwnd)
            if final and final != (0, 0, 0, 0):
                got = final
        _last_snap_report[:] = [(hwnd, (tx, ty, tw, th), tuple(got))]
    if prev:
        user32.SetForegroundWindow(prev)
    return ok


def monitors_list_snapshot():
    return monitors.enum_monitors()


def move_window_to_next_monitor(hwnd, direction=1, activate=True):
    """把指定窗口移到相邻显示器（direction: -1 上一屏 / 1 下一屏）。"""
    ms = monitors.enum_monitors()
    if not ms or not hwnd:
        return False
    idx = _monitor_by_relative(ms, hwnd)
    n = len(ms)
    return move_to_monitor(hwnd, ms[(idx + direction) % n], src=ms,
                           activate=activate)


def snap_window(hwnd, zone, activate=True):
    """把指定窗口吸附到其所在显示器的某个区域。"""
    ms = monitors.enum_monitors()
    if not ms or not hwnd:
        return False
    idx = _monitor_by_relative(ms, hwnd)
    return snap(hwnd, ms[idx], zone, activate=activate)


def move_active_to_next_monitor(direction=1, use_pinned=True, activate=True):
    """移动活动/目标窗口到相邻显示器（与 windows_mac 同名函数签名一致）。

    use_pinned=True 优先用界面固定的目标窗口；False 始终作用于当前活动窗口。
    """
    return move_window_to_next_monitor(
        get_foreground_window(use_pinned), direction, activate=activate)


def raise_window(hwnd):
    """单窗口精准置顶（Windows）。"""
    if not hwnd:
        return False
    try:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        return True
    except Exception:
        return False


def pull_window_to_cursor_monitor(hwnd=None, use_pinned=True, activate=True):
    """将窗口拉取到当前鼠标所在的显示器（Windows）。"""
    target_mon = getattr(monitors, "get_cursor_monitor", lambda: None)()
    if not target_mon:
        ms = monitors.enum_monitors()
        target_mon = ms[0] if ms else None
    if not target_mon:
        return False

    if not hwnd:
        all_wins = list_windows_front_to_back()
        candidate = None
        for w in all_wins:
            cx = w["x"] + w["w"] / 2
            cy = w["y"] + w["h"] / 2
            in_target = (target_mon.left <= cx < target_mon.left + target_mon.width and
                         target_mon.top <= cy < target_mon.top + target_mon.height)
            if not in_target:
                candidate = w["hwnd"]
                break
        if not candidate:
            candidate = get_foreground_window(use_pinned=use_pinned)
        hwnd = candidate

    if not hwnd:
        return False

    old_rect = get_window_rect(hwnd)
    if not old_rect or old_rect == (0, 0, 0, 0):
        return False

    ox, oy, ow, oh = old_rect
    wl, wt, ww, wh = target_mon.work_rect
    nw = min(ow, ww)
    nh = min(oh, wh)
    nx = wl + max(0, (ww - nw) // 2)
    ny = wt + max(0, (wh - nh) // 2)

    set_window_rect(hwnd, nx, ny, nw, nh, activate=activate)
    if activate:
        raise_window(hwnd)
    return True


def snap_active(zone, use_pinned=True, activate=True):
    """把活动/目标窗口吸附到所在显示器的某个区域（与 windows_mac 签名一致）。"""
    return snap_window(get_foreground_window(use_pinned), zone, activate=activate)


def front_external_window():
    """返回最前面的非本程序窗口（z 序第一个）；没有则 None。

    「自动（上次活动窗口）」语义：点按钮瞬间前台是本程序自己，自动模式
    应取 z 序最前的外部窗口（list_windows_front_to_back 已排除自身 PID）。
    """
    for w in list_windows_front_to_back():
        return w
    return None


def is_topmost(hwnd):
    """窗口当前是否处于置顶（WS_EX_TOPMOST）状态。"""
    try:
        return bool(_get_window_long(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    except Exception:  # noqa: BLE001
        return False


def toggle_topmost(hwnd=None, use_pinned=True):
    """切换窗口置顶（always-on-top）状态。

    hwnd 缺省时取目标窗口（use_pinned=True 优先界面固定的目标，否则活动窗口）。
    返回 True=已置顶 / False=已取消置顶 / None=失败。不改变窗口焦点。
    """
    h = hwnd or get_foreground_window(use_pinned)
    if not h or not is_window(h):
        logger.warning("toggle_topmost: 没有可操作的窗口")
        return None
    make_top = not is_topmost(h)
    insert_after = HWND_TOPMOST if make_top else HWND_NOTOPMOST
    if not user32.SetWindowPos(h, insert_after, 0, 0, 0, 0,
                               SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE):
        logger.warning("toggle_topmost: SetWindowPos 失败 (hwnd=%s)", h)
        return None
    logger.info("toggle_topmost: hwnd=%s -> %s", h, "置顶" if make_top else "取消置顶")
    return make_top


_proc_name_cache = {}


def _proc_name(pid):
    """返回进程可执行文件名（用于窗口 label），失败返回 None。结果按 PID 缓存。

    每次枚举窗口都对每个窗口 OpenProcess 一遍开销不小（界面刷新列表时会
    频繁调用），进程名在运行期内不变，缓存即可。
    """
    if pid in _proc_name_cache:
        return _proc_name_cache[pid]
    name = None
    try:
        h = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if h:
            buf = ctypes.create_unicode_buffer(260)
            psapi.GetModuleBaseNameW(h, None, buf, 260)
            kernel32.CloseHandle(h)
            name = buf.value or None
    except Exception:  # noqa: BLE001
        name = None
    # 进程退出后 PID 可能被复用，设上限避免长期运行无限增长
    if len(_proc_name_cache) > 512:
        _proc_name_cache.clear()
    _proc_name_cache[pid] = name
    return name


def list_windows_front_to_back(min_size=80, include_minimized=False):
    """返回按 z 序（最前在前）排列的屏幕窗口列表。

    每项: {"hwnd"(int), "pid", "owner", "name", "x", "y", "w", "h"}
    仅保留可见、有标题、尺寸合理的窗口，并排除本程序自身 PID、系统壳窗口
    （任务栏 / 桌面 / 开始菜单）以及被 DWM 隐藏的挂起窗口（UWP 后台应用）。
    默认还排除最小化窗口——它们并不在屏幕上，分屏对它们没有意义；
    include_minimized=True 可以把它们列出来。
    """
    results = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.IsIconic(hwnd) and not include_minimized:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in _OWN_PIDS:
            return True
        if _class_name(hwnd) in _SHELL_CLASSES or is_cloaked(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        rect = monitors.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < min_size or h < min_size:
            return True
        owner = _proc_name(pid.value)
        results.append({
            "hwnd": int(hwnd), "pid": pid.value,
            "owner": owner, "name": title,
            "x": rect.left, "y": rect.top, "w": w, "h": h,
        })
        return True

    # 复用模块级 WNDENUMPROC，避免与 EnumWindows 的 argtypes 类型不一致
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return results


# 界面上固定的目标窗口（"自动"判断经常猜错，故允许显式指定）。
_pinned_target = None


def set_target(hwnd):
    """固定要操作的窗口（整数 HWND）；传 None 表示恢复自动判断。"""
    global _pinned_target
    _pinned_target = hwnd
    logger.info("目标窗口设为: %s", hwnd if hwnd is not None else "自动")


def get_target():
    return _pinned_target


def list_target_windows():
    """列出可选窗口（供界面下拉框），已排除本程序与系统浮层。

    返回 [{"hwnd", "label", "owner", "name", "x", "y", "w", "h"}, ...]
    """
    out = []
    seen = set()
    for w in list_windows_front_to_back():
        if w["hwnd"] in seen:
            continue
        seen.add(w["hwnd"])
        title = w["name"] if w["name"] else (w["owner"] or "")
        label = f'{title}  ({w["w"]}×{w["h"]})' if title else f'({w["w"]}×{w["h"]})'
        out.append({**w, "label": label})
    return out


def _find_window(hwnd):
    """按整数 HWND 重新定位窗口，拿到最新几何；不存在则返回 None。

    不走 list_windows_front_to_back：那个列表会过滤掉最小化窗口，而固定目标
    被最小化后用户仍应能对它做分屏 / 跨屏操作，否则固定目标会「消失」。
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    rect = monitors.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    length = user32.GetWindowTextLengthW(hwnd)
    title = ""
    if length > 0:
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
    return {
        "hwnd": int(hwnd), "pid": pid.value,
        "owner": _proc_name(pid.value), "name": title,
        "x": rect.left, "y": rect.top,
        "w": rect.right - rect.left, "h": rect.bottom - rect.top,
    }


def _monitor_index_by_rect(monitors_list, x, y, w, h):
    """按给定矩形（而非 hwnd）判断所在显示器索引：交集最长优先。"""
    return screen_math.monitor_index_by_intersection(monitors_list, x, y, w, h)


def _place_window(hwnd, x, y, w, h):
    """先还原（退出最大化 / 最小化）再移动到目标矩形并激活到最前。"""
    if is_maximized(hwnd) or user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    ok = set_window_rect(hwnd, x, y, w, h, activate=True, exact=True)
    if ok and not _verify_visible(hwnd, x, y, w, h):
        logger.warning("_place_window: 窗口未按预期摆放 (hwnd=%s)", hwnd)
    return ok


def _resolve_pair(left_hwnd, right_hwnd, use_pinned, cands, by_hwnd):
    """从候选窗口中解析并排的左右窗口。"""
    left_w = None
    if left_hwnd:
        left_w = by_hwnd.get(left_hwnd) or _find_window(left_hwnd)
    elif use_pinned and _pinned_target:
        left_w = _find_window(_pinned_target)
    if left_w is None:
        if not cands:
            return None, None
        left_w = cands[0]
    left_hwnd = left_w["hwnd"]

    right_w = None
    if right_hwnd and right_hwnd != left_hwnd:
        right_w = by_hwnd.get(right_hwnd) or _find_window(right_hwnd)
    if right_w is None:
        for w in cands:
            if w["hwnd"] != left_hwnd:
                right_w = w
                break
    if right_w is None:
        return left_w, None
    return left_w, right_w


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
            if getattr(m, "device_name", None) == monitor:
                return i
    return None


def snap_two_side_by_side(left_hwnd=None, right_hwnd=None, use_pinned=True, monitor=None):
    """把两个窗口并排到同一屏的左右半屏（左/右可显式指定，整数 HWND）。

    未指定时自动取最前面的两个窗口（左侧优先用界面固定的目标窗口）。
    monitor 可指定目标显示器（int 索引或 device_name 字符串）；为 None 时
    以左侧窗口当前所在屏幕为准，并排后激活两窗口到最前。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return False
    cands = list_windows_front_to_back()
    by_hwnd = {w["hwnd"]: w for w in cands}

    left_w, right_w = _resolve_pair(
        left_hwnd, right_hwnd, use_pinned, cands, by_hwnd)
    if left_w is None:
        logger.warning("没有可用于并排的窗口")
        return False
    if right_w is None:
        logger.warning("只找到一个可用窗口，无法并排")
        return False

    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(
            ms, left_w["x"], left_w["y"], left_w["w"], left_w["h"])
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect
    # 并排只有对半这一种切法，没有可调比例，也就不需要 split_x/split_y
    # （那两个名字是四宫格的参数，写在这里只会 NameError）。
    if mon.height > mon.width:
        # 竖屏：左右并排会挤成两条窄条，改成上/下各半屏堆叠。界面文案与
        # 工具提示（「竖屏时改作上半屏」）说的就是这件事，与 macOS 侧一致。
        half_h = wh // 2
        _place_window(left_w["hwnd"], wl, wt, ww, half_h)
        _place_window(right_w["hwnd"], wl, wt + half_h, ww, wh - half_h)
        logger.info("竖屏并排(上下堆叠): 上=%s 下=%s（屏幕 %s）",
                    left_w["hwnd"], right_w["hwnd"], mon.device_name)
    else:
        half = ww // 2
        _place_window(left_w["hwnd"], wl, wt, half, wh)
        _place_window(right_w["hwnd"], wl + half, wt, ww - half, wh)
        logger.info("并排完成(Windows): 左=%s 右=%s（屏幕 %s）",
                    left_w["hwnd"], right_w["hwnd"], mon.device_name)
    return True


def snap_three_stack(top_hwnd=None, mid_hwnd=None, bot_hwnd=None, use_pinned=True, monitor=None):
    """把三个窗口堆叠到同一屏的上/中/下三栏（竖屏排列，整数 HWND）。

    top/mid/bot_hwnd 可显式指定；未指定时自动取最前面的三个窗口
    （顶部优先用界面固定的目标窗口）。monitor 可指定目标显示器
    （int 索引或 device_name 字符串）；为 None 时以顶部窗口当前所在屏幕为准。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return False
    cands = list_windows_front_to_back()
    by_hwnd = {w["hwnd"]: w for w in cands}

    # 顶部窗口
    top_w = None
    if top_hwnd:
        top_w = by_hwnd.get(top_hwnd) or _find_window(top_hwnd)
    elif use_pinned and _pinned_target:
        top_w = _find_window(_pinned_target)
    if top_w is None:
        if not cands:
            logger.warning("没有可用于三栏排列的窗口")
            return False
        top_w = cands[0]
    top_hwnd = top_w["hwnd"]

    # 中部窗口
    mid_w = None
    if mid_hwnd and mid_hwnd != top_hwnd:
        mid_w = by_hwnd.get(mid_hwnd) or _find_window(mid_hwnd)
    if mid_w is None:
        for w in cands:
            if w["hwnd"] != top_hwnd:
                mid_w = w
                break
    if mid_w is None:
        logger.warning("只找到一个可用窗口，无法三栏排列")
        return False
    mid_hwnd = mid_w["hwnd"]

    # 底部窗口
    bot_w = None
    if bot_hwnd and bot_hwnd not in (top_hwnd, mid_hwnd):
        bot_w = by_hwnd.get(bot_hwnd) or _find_window(bot_hwnd)
    if bot_w is None:
        for w in cands:
            h = w["hwnd"]
            if h != top_hwnd and h != mid_hwnd:
                bot_w = w
                break
    if bot_w is None:
        logger.warning("只找到两个可用窗口，无法三栏排列")
        return False
    bot_hwnd = bot_w["hwnd"]

    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(
            ms, top_w["x"], top_w["y"], top_w["w"], top_w["h"])
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect
    third = wh // 3
    _place_window(top_hwnd, wl, wt, ww, third)
    _place_window(mid_hwnd, wl, wt + third, ww, third)
    _place_window(bot_hwnd, wl, wt + 2 * third, ww, wh - 2 * third)
    logger.info("三栏排列完成(Windows): 上=%s 中=%s 下=%s（屏幕 %s）",
                top_hwnd, mid_hwnd, bot_hwnd, mon.device_name)
    return True


def snap_four_quad(tl_hwnd=None, tr_hwnd=None, bl_hwnd=None, br_hwnd=None,
                   use_pinned=True, monitor=None, split_x=0.5, split_y=0.5,
                   smart=False):
    """把四个窗口排到同一屏的 2×2 四宫格（左上/右上/左下/右下，整数 HWND）。

    与 snap 的 quad-* 四等分吸附配套：那四个一次只动一个窗口，本函数一次摆四个。

    四个 hwnd 均可显式指定；未指定时自动取最前面的四个窗口（左上优先用界面
    固定的目标窗口）。monitor 可指定目标显示器；为 None 时以左上窗口所在屏为准。

    split_x / split_y 给出分割比例（0~1，0.5 = 四等分）。等分并不总是最合适：
    左栏放终端想窄一点、右栏放浏览器想宽一点是常见需求，界面上做了可拖拽的
    分割线，把这两个比例一起传进来。

    smart=True 时改用智能分配：传入哪些窗口都行，谁去哪个角、用什么比例
    都由 quad_plan.plan_quad_smart 按各窗口的宽高比算出来。此时 tl/tr/bl/br
    只是「参与排列的四个窗口」（顺序不限），split_x/split_y 作为兜底值。
    """
    _quad_reset_state()
    picked, picked_corners, missing = _quad_pick_windows(
        (tl_hwnd, tr_hwnd, bl_hwnd, br_hwnd), use_pinned=use_pinned)
    if missing:
        logger.warning("四宫格：%d 个指定窗口已不可用，只排剩下的", len(missing))
    if len(picked) < 2:
        logger.warning("可用窗口不足两个，无法四宫格排列")
        return False
    ms = monitors.enum_monitors()
    if not ms:
        return False
    tl_hwnd, tl_w = picked[0]
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, tl_w["x"], tl_w["y"],
                                     tl_w["w"], tl_w["h"])
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect
    # 默认用手动传入的比例；smart 成功时被下面的智能分配覆盖
    sx, sy = split_x, split_y
    if smart:
        # 智能分配：按各窗口的宽高比重排四个角的归属，并重算分割比例。
        # 直接用 picked 里的 w（带 x/y/w/h）做判据，不重新枚举——重新拉列表
        # 顺序与几何都会变，按 (owner, name) 匹配会错位
        try:
            planned, sx2, sy2 = quad_plan.plan_quad_smart(
                [dict(w, hwnd=h) for h, w in picked], ww, wh)
        except ValueError as e:  # noqa: BLE001
            logger.warning("智能分配失败，退回手动比例: %s", e)
        else:
            reordered = []
            for w in planned:
                reordered.append((w.get("hwnd"), w))
            if all(h for h, _w in reordered) and len(reordered) == 4:
                picked = reordered
                sx, sy = sx2, sy2
                logger.info("智能分配结果(Windows): 左上=%s 右上=%s 左下=%s 右下=%s "
                            "（横向 %.0f%% / 竖向 %.0f%%）",
                            picked[0][0], picked[1][0], picked[2][0], picked[3][0],
                            sx * 100, sy * 100)
            else:
                logger.warning("智能分配结果异常，保留手动顺序")
    # 比例钳在安全区间：贴边会让某一格只剩几个像素，等于把窗口藏起来
    sx = min(max(float(sx), 0.15), 0.85)
    sy = min(max(float(sy), 0.15), 0.85)
    global _last_quad_splits
    _last_quad_splits = (sx, sy)
    # 格子几何与预演（quad_predict）共用 quad_plan：形状由「占了哪些角」定。
    # 三窗 T 形时那一格拉通整宽、两窗同排时那一排撑满整高——空着的角若留成
    # 整块四分之一屏，用户看到的就是「没排好」的死区。
    occ = sorted(set(picked_corners))
    by_corner = {c: h for c, (h, _w) in zip(picked_corners, picked)}
    cells = quad_plan.quad_cells(quad_plan.quad_shape(occ),
                                 int(round(ww * sx)), int(round(wh * sy)),
                                 wl, wt, ww, wh)
    # 写入前记下原几何：摆放后若一字未动，就是「写入无效」（App 不开放
    # 自动化 / 被系统钉住），与被最小尺寸钳制是两回事，必须分开报
    before_rects = {}
    for h, _w in picked:
        before_rects[h] = get_window_rect(h)

    def _put(corner):
        h = by_corner[corner]
        _last_quad_want[h] = cells[corner]
        _place_window(h, *cells[corner])

    left_w = cells[0][2] if 0 in occ else (cells[2][2] if 2 in occ else ww)
    left_fixed = False
    for row_i, (lc, rc) in enumerate(((0, 1), (2, 3))):
        if lc not in occ and rc not in occ:
            continue
        if row_i:
            # 下排起点按上排实测下缘让位：部分 App 有最小窗口高度，上排可能
            # 缩不到理想高度，仍按理想值写下排会让两排重叠
            bot_y = None
            for tc in (0, 1):
                if tc not in occ:
                    continue
                rect = get_window_rect(by_corner[tc])
                if rect and rect != (0, 0, 0, 0) and rect[3] > 0:
                    cand = min(max(rect[1] + rect[3] + 1, wt + 1),
                               max(wt + 1, (wt + wh) - 1))
                    bot_y = cand if bot_y is None else max(bot_y, cand)
            if bot_y is not None:
                for bc_ in (2, 3):
                    if bc_ in occ:
                        cells[bc_] = (cells[bc_][0], bot_y, cells[bc_][2],
                                      max(1, (wt + wh) - bot_y))
        if lc in occ:
            # 左列宽以上排定下来的为准：上排被最小宽度顶宽时下排跟着让，
            # 两排的分割线不错开。拉通整宽的那一格（T 形 / 只有一列有窗）
            # 没有右邻居，宽度必须保持整宽
            if rc in occ and cells[rc][2] < ww:
                cells[lc] = (cells[lc][0], cells[lc][1], left_w, cells[lc][3])
            _put(lc)
            # 左窗 App 可能有最小窗口宽度，读回实测宽度后按它定右列起点，两列
            # 不再互相压盖（宽度不匀是应用侧钳制，无法消除）
            if rc in occ and cells[rc][2] < ww and not left_fixed:
                tl_rect = get_window_rect(by_corner[lc])
                if tl_rect and tl_rect != (0, 0, 0, 0) and tl_rect[2] > 0:
                    left_w = (max(wl + 1, min(wl + ww - 1,
                                              tl_rect[0] + tl_rect[2] + 1))
                              - wl)
                    left_fixed = True
        if rc in occ:
            if lc in occ:
                # 上下两排沿用同一条列分割线，否则两排列宽错开，像没排齐。
                # 拉通整宽的那一格（T 形）没有右邻居，宽度不能改
                if cells[rc][2] < ww:
                    cells[rc] = (wl + left_w, cells[rc][1],
                                 max(1, ww - left_w), cells[rc][3])
            _put(rc)
    # 实测闭环：把「写入目标 vs 读回结果」记下来给界面逐格说明，并据实学出
    # 各窗口的最小/上限尺寸——容量预检与排列预演消费的正是这份缓存
    _last_quad_slots[:] = list(zip(picked_corners, [h for h, _w in picked]))
    _last_quad_report[:] = []
    _last_quad_unwritable[:] = []
    for h, _w in picked:
        want = _last_quad_want.get(h)
        got = get_window_rect(h)
        if not got or got == (0, 0, 0, 0):
            _last_quad_report.append((h, want, None))
            continue
        _last_quad_report.append((h, want, got))
        _quad_remember_measured(h, got)
        if want and got == before_rects.get(h) and any(want):
            _last_quad_unwritable.append(h)
    _quad_cache_save()
    logger.info("四宫格排列完成(Windows): 左上=%s 右上=%s 左下=%s 右下=%s"
                "（屏幕 %s，横向 %.0f%% / 竖向 %.0f%%）",
                by_corner.get(0), by_corner.get(1),
                by_corner.get(2), by_corner.get(3),
                mon.device_name, sx * 100, sy * 100)
    return True


# ── 虚拟桌面网格（跨屏 m×n 批排，Windows）────────────────────────────

# 最近一次虚拟网格需要如实告知用户的情况。界面读它在状态栏里提示，
# 而不是只说「排列完成」——用户反复抱怨「不准」，而多数不准其实是
# 装不下、写入无效、被应用钳制这三类物理限制。
_last_grid_notes = []

# 最近一次虚拟网格的实测报告 [(hwnd, 目标矩形, 实测矩形或None), ...]
_last_grid_report = []


def fill_grid(hwnds=None, cols=None, rows=None, layout=None, smart=False,
              monitor_indices=None, use_pinned=True):
    """把若干窗口排进跨屏虚拟桌面的 m×n（或加权比例）网格（整数 HWND）。

    与 fill_all_monitors（一个窗口一块屏）互补：把勾选的多块屏当成一块大桌面，
    按比例切格子，一个格子一个窗口。等分（2×2 / 3×2 / 3×3）与加权
    （2/3+1/3）走同一条路，由 grid_plan 的比例元组描述。

    三条约束与 macOS 版一致，都是真机踩过的坑：

    1. **格子绝不跨屏**。Windows 上横跨两屏的窗口同样会被系统重新归属，
       grid_plan.assign_cells 把每格归到交集最大的屏并裁进它的工作区。
    2. **装不下的窗口不硬排**。Windows 侧没有 AppleScript 最小尺寸探测，
       这里按窗口当前尺寸作乐观判据（min=0），fit_report 仍会拦住
       「格子本身退化到 0」的情况；装不下的保持原位并如实报告。
    3. **间距按贴边算**（grid_plan.cell_gap_rect）。

    layout 优先于 cols/rows；两者都没给时按 2×2。
    monitor_indices: 只排这些下标的屏（按给定顺序）。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return 0

    if monitor_indices is not None:
        keep = []
        for i in monitor_indices:
            if isinstance(i, int) and 0 <= i < len(ms) and i not in keep:
                keep.append(i)
        ms = [ms[i] for i in keep]
        if not ms:
            logger.warning("虚拟网格：没有可用的目标屏")
            return 0

    global _last_grid_notes, _last_grid_report
    _last_grid_notes.clear()
    _last_grid_report = []

    if layout:
        got = grid_plan.layout_by_label(layout)
        if got is None:
            logger.warning("虚拟网格：未知布局 %r，按自定义行列处理", layout)
            got = (grid_plan.equal_fracs(cols or 2),
                   grid_plan.equal_fracs(rows or 2))
    else:
        got = (grid_plan.equal_fracs(cols or 2),
               grid_plan.equal_fracs(rows or 2))
    col_fracs, row_fracs = got
    n_cols, n_rows = len(col_fracs), len(row_fracs)
    if n_cols > grid_plan.MAX_COLS or n_rows > grid_plan.MAX_ROWS:
        logger.warning("虚拟网格：%d×%d 超出上限（最多 %d列×%d行）",
                       n_cols, n_rows, grid_plan.MAX_COLS, grid_plan.MAX_ROWS)
        return 0

    vr = grid_plan.virtual_rect(ms)
    if vr is None:
        return 0
    cells = grid_plan.grid_cells(vr[0], vr[1], vr[2], vr[3],
                                 col_fracs, row_fracs)
    asg = grid_plan.assign_cells(cells, ms)
    if not asg:
        _last_grid_notes.append("勾选的屏摆不出可用的网格（屏之间有重叠？）")
        logger.warning("虚拟网格：格子与任何屏都没有交集")
        return 0

    # 选窗：显式 hwnd 优先，否则按 z 序取前 len(asg) 个（滤自身进程）
    wins = []
    if hwnds:
        cands = list_windows_front_to_back()
        by_hwnd = {w["hwnd"]: w for w in cands}
        for h in hwnds:
            if not h or h in [w["hwnd"] for w in wins]:
                continue
            w = by_hwnd.get(h) or _find_window(h)
            if w is None:
                _last_grid_notes.append(
                    "指定的窗口 %s 已关闭，未参与排列" % h)
                logger.warning("虚拟网格：指定的窗口 %s 已不可用", h)
                continue
            wins.append(w)
    if not wins:
        cands = [w for w in list_windows_front_to_back()
                 if w["pid"] not in _OWN_PIDS]
        wins = grid_plan.pick_windows(cands, len(asg), own_pids=_OWN_PIDS)
        if use_pinned and _pinned_target and not wins:
            pinned = _find_window(_pinned_target)
            if pinned is not None:
                wins = [pinned]
    if not wins:
        _last_grid_notes.append("没有可操作的窗口")
        logger.warning("虚拟网格：没有可操作的窗口")
        return 0

    # 间距：Windows 侧读同一份设置（settings.json 与 macOS 共用）
    try:
        import settings
        _s = settings.load()
        gap = int(_s.get("window_gap", 0) or 0)
        edge_gap = bool(_s.get("gap_screen_edges", False))
    except Exception:  # noqa: BLE001
        gap, edge_gap = 0, False
    gap = max(0, gap)

    # Windows 侧没有最小尺寸探测：按 0（乐观）计，fit_report 只拦格子退化
    mins = {w["hwnd"]: (0, 0) for w in wins}
    slots, too_small = grid_plan.fit_report(asg, wins, mins, gap, edge_gap, ms)
    if not slots:
        _last_grid_notes.append("这些窗口放不进网格格子，未做排列")
        logger.warning("虚拟网格：%d 个窗口全部放不进各自格子", len(wins))
        return 0

    # 智能分配必须拿 **slots 里的窗口**算（macOS 侧同一教训：fit_report 剔掉
    # 装不下的窗口后 wins 与 slots 的下标会错开），且算出的 order 必须真的
    # 用于写入循环——只算不用的 order 让「智能」开关形同虚设。
    n = len(slots)
    slot_wins = [s[2] for s in slots]
    order = list(range(n))
    if smart and n > 1:
        sizes = [(a[5], a[6]) for a, _h, _w in slots]
        perm = grid_plan.assign_windows(slot_wins, sizes, smart=True)
        order = [0] * n
        for wi, ci in enumerate(perm):
            if 0 <= ci < n:
                order[ci] = wi

    done = 0
    for k in range(n):
        row, col, mi, gx, gy, gw, gh = slots[k][0]
        hwnd = slot_wins[order[k]].get("hwnd") or slots[k][1]
        try:
            if _place_window(hwnd, gx, gy, gw, gh):
                done += 1
                _last_grid_report.append((hwnd, (gx, gy, gw, gh),
                                          get_window_rect(hwnd)))
        except Exception:  # noqa: BLE001
            logger.warning("虚拟网格：写入 %s 失败", hwnd, exc_info=True)
            _last_grid_notes.append("%s 摆放失败" % hwnd)
    for h, _w, r in too_small:
        _last_grid_notes.append(
            "%s 放不下它那一格（%dx%d），已保持原位" % (h, r[2], r[3]))
    logger.info("虚拟网格完成(Windows): %d/%d 个窗口 -> %d 格（%d列×%d行）",
                done, len(wins), len(asg), n_cols, n_rows)
    return done
def _is_portrait(mon):
    """屏幕是否为竖屏（高大于宽）。"""
    return mon.height > mon.width


# 最近一次四宫格实际使用的比例 (split_x, split_y)，智能模式下即程序算出的值
_last_quad_splits = (0.5, 0.5)

# ── 四宫格尺寸缓存（与 macOS 侧同结构：quad_cache.json）────────────────

_QUAD_MIN_CACHE = {}   # hwnd -> (min_w, min_h)，上次实测的最小尺寸
_QUAD_MAX_CACHE = {}   # hwnd -> (max_w, max_h)，上次实测的尺寸上限
_QUAD_CACHE_MAX_ENTRIES = 500
_last_quad_notes = []       # 一次排布要如实告知用户的情况（状态栏显示）
_last_quad_slots = []       # [(corner, hwnd), ...] 本次实际分配的角
_last_quad_report = []      # [(hwnd, want, got), ...] 写入目标 vs 实测
_last_quad_unwritable = []  # 写入后几何一步没动的 hwnd


def _quad_cache_file():
    """四宫格尺寸缓存文件路径（与 settings.json 同目录）。"""
    try:
        import settings
        base = settings.data_dir()
    except Exception:  # noqa: BLE001
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "quad_cache.json")


def _quad_cache_save():
    """把最小/上限缓存合并写盘（原子写，静默失败）。"""
    import json
    import tempfile
    try:
        data = {
            "version": 1,
            "mins": {h: [int(v[0]), int(v[1])]
                     for h, v in _QUAD_MIN_CACHE.items() if v},
            "maxes": {h: [int(v[0]), int(v[1])]
                      for h, v in _QUAD_MAX_CACHE.items() if v},
        }
        for _d in (data["mins"], data["maxes"]):
            while len(_d) > _QUAD_CACHE_MAX_ENTRIES:
                _d.pop(next(iter(_d)))
        path = _quad_cache_file()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                   prefix=".quad_cache.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:  # noqa: BLE001
        logger.warning("四宫格缓存写盘失败: %s", e)


def _quad_cache_load():
    """App 启动时调一次：把上次的尺寸缓存读进内存（静默失败）。"""
    import json
    try:
        path = _quad_cache_file()
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return

        def _pair(v):
            return (isinstance(v, list) and len(v) == 2
                    and all(isinstance(x, int) and not isinstance(x, bool)
                            for x in v))

        def _key(h):
            # Windows 的 hwnd 是整数，JSON 的键只能是字符串：读回来必须还原
            # 成 int，否则缓存永远对不上实际窗口（macOS 侧键本来就是字符串）
            try:
                return int(h)
            except (TypeError, ValueError):
                return h

        for h, v in (data.get("mins") or {}).items():
            if _pair(v):
                _QUAD_MIN_CACHE[_key(h)] = (v[0], v[1])
        for h, v in (data.get("maxes") or {}).items():
            if _pair(v):
                _QUAD_MAX_CACHE[_key(h)] = (v[0], v[1])
        logger.info("四宫格缓存已读入：%d 个最小尺寸、%d 个尺寸上限",
                    len(_QUAD_MIN_CACHE), len(_QUAD_MAX_CACHE))
    except Exception as e:  # noqa: BLE001
        logger.warning("四宫格缓存读盘失败，按无缓存处理: %s", e)


def _quad_remember_measured(hwnd, rect):
    """把写入后读回的实测尺寸并入缓存（Mac 侧同语义）。

    Windows 上 App 的最小尺寸表现为「SetWindowPos 后读回比目标大」：读回
    值就是下限。上限只在「读回比目标小」时记——App 拒绝长到目标值。
    """
    if not rect or rect == (0, 0, 0, 0):
        return
    want = _last_quad_want.get(hwnd)
    if want:
        w, h = int(rect[2]), int(rect[3])
        if w > int(want[2]) + 4:
            _QUAD_MIN_CACHE[hwnd] = (w, _QUAD_MIN_CACHE.get(hwnd, (0, h))[1])
        if h > int(want[3]) + 4:
            mw = _QUAD_MIN_CACHE.get(hwnd, (0, 0))[0]
            _QUAD_MIN_CACHE[hwnd] = (mw if mw > 0 else w, h)
        if 0 < w < int(want[2]) - 4:
            _QUAD_MAX_CACHE[hwnd] = (w, _QUAD_MAX_CACHE.get(hwnd, (0, 0))[1])
        if 0 < h < int(want[3]) - 4:
            mw, mh = _QUAD_MAX_CACHE.get(hwnd, (0, 0))
            _QUAD_MAX_CACHE[hwnd] = (mw, h)
    while len(_QUAD_MIN_CACHE) > _QUAD_CACHE_MAX_ENTRIES:
        _QUAD_MIN_CACHE.pop(next(iter(_QUAD_MIN_CACHE)))
    while len(_QUAD_MAX_CACHE) > _QUAD_CACHE_MAX_ENTRIES:
        _QUAD_MAX_CACHE.pop(next(iter(_QUAD_MAX_CACHE)))


# 本轮四宫格写入目标（hwnd -> (x, y, w, h)）：实测并入缓存时对比用
_last_quad_want = {}


def _gap_settings():
    """读取窗口间距设置：(gap, gap_screen_edges)。异常时按关闭处理。"""
    try:
        import settings
        s = settings.load()
        gap = int(s.get("window_gap", 0) or 0)
        edges = bool(s.get("gap_screen_edges", False))
    except Exception:  # noqa: BLE001
        return (0, False)
    return (max(0, gap), edges)


def _quad_reset_state():
    """清空上一轮四宫格的实测状态（界面据此显示排布明细）。

    失败路径也要清：留着上一次的 report/slots，界面会把「没排成」说成
    「某个窗口差 N px」，用户看到的还是「不准」。
    """
    _last_quad_notes[:] = []
    _last_quad_slots[:] = []
    _last_quad_report[:] = []
    _last_quad_unwritable[:] = []
    _last_quad_want.clear()


def _quad_pick_windows(wants, use_pinned=True):
    """按四角选出要排的窗口：显式 hwnd 优先，空格自动补足。

    snap_four_quad 与 quad_predict（排列预演）共用这一套：预演必须算正式
    排布会动的那批窗口，两边选得不一样，预告本身就是错的——那正是「预告说
    排四个、实际动了三个」的不准来源。

    wants: (左上, 右上, 左下, 右下) 的 hwnd，None = 该格「自动」。
    use_pinned: 空格是否优先用界面固定的目标窗口（与 snap_four_quad 同义）。
    返回 (picked, picked_corners, missing)：
      picked         [(hwnd, win_dict)]，按角序排好的待排窗口
      picked_corners 每个窗口对应的角序号 0..3
      missing        显式选中但已不可用（关掉）的 hwnd。如实报出来，不能
                     静默用别的窗口顶上——用户选中的格子被换成别的窗口，
                     看到的就是「不准」
    显式选中 2 个及以上时不再自动补足：界面上点掉不想要的格就只排选中的
    两三个，这块屏放不下四个是常态，能让用户主动选 2/3 个来排，比每次放不下
    再解释一遍有用。
    """
    cands = list_windows_front_to_back()
    by_hwnd = {w["hwnd"]: w for w in cands}
    keep_only_selected = len([x for x in wants if x]) >= 2
    picked, picked_corners, seen, missing = [], [], set(), []
    for corner, want in enumerate(wants):
        w = None
        if want:
            w = by_hwnd.get(want) or _find_window(want)
            if w is None and want not in seen:
                logger.warning("四宫格：指定的窗口 %s 已不可用", want)
                missing.append(want)
        elif (not keep_only_selected and use_pinned and _pinned_target
              and _pinned_target not in seen):
            w = _find_window(_pinned_target)
        if w is None:
            if keep_only_selected:
                continue          # 用户点掉了这个角：留空，不补
            for c in cands:
                if c["hwnd"] not in seen:
                    w = c
                    break
        if w is None or w["hwnd"] in seen:
            # 桌面上一共没那么多窗口：排已有的，少于两个才失败
            continue
        picked.append((w["hwnd"], w))
        picked_corners.append(corner)
        seen.add(w["hwnd"])
    return picked, picked_corners, missing


def _quad_window_label(hwnd):
    """四宫格提示里的窗口名（进程名优先，取不到退回标题/hwnd）。"""
    w = _find_window(hwnd) or {}
    return w.get("owner") or w.get("name") or str(hwnd)


def is_window_portrait_suitable(hwnd_or_win, mon=None):
    """判断窗口是否适合放在竖屏（height > width）上。"""
    if not hwnd_or_win:
        return True
    w, h = 0, 0
    if isinstance(hwnd_or_win, dict):
        w = hwnd_or_win.get("w", 0)
        h = hwnd_or_win.get("h", 0)
    elif isinstance(hwnd_or_win, (tuple, list)) and len(hwnd_or_win) >= 4:
        w, h = hwnd_or_win[2], hwnd_or_win[3]
    else:
        fw = _find_window(hwnd_or_win)
        if fw:
            w, h = fw.get("w", 0), fw.get("h", 0)
        else:
            rect = get_window_rect(hwnd_or_win)
            if rect:
                w, h = rect[2], rect[3]
    if w <= 0 or h <= 0:
        return True
    if w / float(h) >= 1.15:
        return False
    if mon is not None:
        ww = getattr(mon, "work_width", getattr(mon, "width", 0))
        if ww > 0 and w > ww:
            return False
    return True


def fill_all_monitors(hwnds=None, use_pinned=True, mode="max",
                      monitor_indices=None, filter_portrait=True,
                      assignments=None):
    """把若干窗口分别铺满各自的显示器，一个窗口一块屏（或每屏 2×2 四宫格）（Windows）。"""
    ms = monitors.enum_monitors()
    if not ms:
        logger.warning("未检测到显示器")
        return 0

    if assignments is not None:
        done = 0
        all_monitors = list(ms)
        if hwnds is None:
            try:
                ws = list_target_windows(use_pinned=use_pinned)
            except TypeError:
                ws = list_target_windows()
            auto_pool = [w["hwnd"] for w in ws] if ws else []
        else:
            auto_pool = list(hwnds)

        explicit_hwnds = set()
        for t in assignments.values():
            if isinstance(t, (list, tuple)):
                for item in t:
                    if item and item not in ("auto", "none", "", None):
                        explicit_hwnds.add(item)
            elif t and t not in ("auto", "none", "", None):
                explicit_hwnds.add(t)
        auto_pool = [h for h in auto_pool if h not in explicit_hwnds]

        for m_idx, target in assignments.items():
            try:
                m_idx = int(m_idx)
            except (ValueError, TypeError):
                continue
            if not (0 <= m_idx < len(all_monitors)):
                continue
            mon = all_monitors[m_idx]
            wl = getattr(mon, "work_left", mon.left)
            wt = getattr(mon, "work_top", mon.top)
            ww = getattr(mon, "work_width", mon.width)
            wh = getattr(mon, "work_height", mon.height)
            is_portrait = (mon.height > mon.width) or (wh > ww)

            if mode in ("four_quad", "4quad", "quad4", "2x2", "2×2"):
                target_list = list(target) if isinstance(target, (list, tuple)) else [target]
                resolved_list = []
                for item in target_list[:4]:
                    if item == "auto":
                        cand = None
                        for c in auto_pool:
                            if not (filter_portrait and is_portrait and not is_window_portrait_suitable(c, mon)):
                                cand = c
                                break
                        if cand:
                            auto_pool.remove(cand)
                            resolved_list.append(cand)
                    elif item and item != "none":
                        resolved_list.append(item)

                w_left = ww // 2
                w_right = ww - w_left
                h_top = wh // 2
                h_bottom = wh - h_top
                slots = [
                    (wl, wt, w_left, h_top),
                    (wl + w_left, wt, w_right, h_top),
                    (wl, wt + h_top, w_left, h_bottom),
                    (wl + w_left, wt + h_top, w_right, h_bottom),
                ]
                for h, (sx, sy, sw, sh) in zip(resolved_list[:4], slots):
                    if filter_portrait and is_portrait and not is_window_portrait_suitable(h, mon):
                        logger.info("跨屏铺满(手动)：跳过不适合竖屏的窗口 %s", h)
                        continue
                    try:
                        _place_window(h, sx, sy, sw, sh)
                        done += 1
                    except Exception:
                        logger.warning("手动四宫格铺满失败: hwnd=%s", h, exc_info=True)
            else:
                h = target[0] if isinstance(target, (list, tuple)) and target else target
                if h == "auto":
                    cand = None
                    for c in auto_pool:
                        if not (filter_portrait and is_portrait and not is_window_portrait_suitable(c, mon)):
                            cand = c
                            break
                    h = cand
                    if h:
                        auto_pool.remove(h)
                if not h or h == "none":
                    continue
                if filter_portrait and is_portrait and not is_window_portrait_suitable(h, mon):
                    logger.info("跨屏铺满(手动)：跳过不适合竖屏的窗口 %s", h)
                    continue
                try:
                    if mode == "quad":
                        _place_window(h, wl, wt, ww // 2, wh // 2)
                    else:
                        _place_window(h, wl, wt, ww, wh)
                    done += 1
                except Exception:
                    logger.warning("手动跨屏铺满失败: hwnd=%s 屏=%s", h, mon.device_name,
                                   exc_info=True)
        return done

    if monitor_indices is not None:
        keep = []
        for i in monitor_indices:
            try:
                idx = int(i)
                if 0 <= idx < len(ms) and idx not in keep:
                    keep.append(idx)
            except (ValueError, TypeError):
                continue
        ms = [ms[i] for i in keep]
        if not ms:
            logger.warning("没有可铺的目标屏")
            return 0

    if hwnds is None:
        wins = list_target_windows(use_pinned=use_pinned)
        picked = []
        seen = set()
        for w in wins:
            if w["hwnd"] in seen:
                continue
            picked.append(w["hwnd"])
            seen.add(w["hwnd"])
        hwnds = picked
    else:
        hwnds = list(hwnds)

    if not hwnds:
        logger.warning("没有可用于跨屏铺满的窗口")
        return 0

    done = 0
    rem_hwnds = list(hwnds)

    if mode in ("four_quad", "4quad", "quad4", "2x2", "2×2"):
        for mon in ms:
            wl = getattr(mon, "work_left", mon.left)
            wt = getattr(mon, "work_top", mon.top)
            ww = getattr(mon, "work_width", mon.width)
            wh = getattr(mon, "work_height", mon.height)
            is_portrait = (mon.height > mon.width) or (wh > ww)

            if is_portrait and filter_portrait:
                suitable = [h for h in rem_hwnds if is_window_portrait_suitable(h, mon)]
                if len(suitable) < 2:
                    logger.info("跨屏铺满(四宫格)：跳过竖屏 %s，缺少适合竖屏的窗口", mon.device_name)
                    continue
                picked_for_mon = suitable[:4]
            else:
                picked_for_mon = rem_hwnds[:4]

            if not picked_for_mon:
                continue

            for h in picked_for_mon:
                if h in rem_hwnds:
                    rem_hwnds.remove(h)

            w_left = ww // 2
            w_right = ww - w_left
            h_top = wh // 2
            h_bottom = wh - h_top
            slots = [
                (wl, wt, w_left, h_top),
                (wl + w_left, wt, w_right, h_top),
                (wl, wt + h_top, w_left, h_bottom),
                (wl + w_left, wt + h_top, w_right, h_bottom),
            ]
            for h, (sx, sy, sw, sh) in zip(picked_for_mon, slots):
                try:
                    _place_window(h, sx, sy, sw, sh)
                    done += 1
                except Exception:
                    logger.warning("跨屏铺满(四宫格)失败: hwnd=%s 屏=%s", h, mon.device_name,
                                   exc_info=True)
        logger.info("跨屏铺满(四宫格)完成(Windows): %d 个窗口 -> %d 块屏", done, len(ms))
        return done

    matched_pairs = []
    if filter_portrait:
        has_portrait_later = any((m.height > m.width) or (getattr(m, "work_height", m.height) > getattr(m, "work_width", m.width)) for m in ms)
        for mon in ms:
            if not rem_hwnds:
                break
            is_portrait = (mon.height > mon.width) or (getattr(mon, "work_height", mon.height) > getattr(mon, "work_width", mon.width))
            if is_portrait:
                target_h = None
                for candidate in rem_hwnds:
                    if is_window_portrait_suitable(candidate, mon):
                        target_h = candidate
                        break
                if target_h:
                    rem_hwnds.remove(target_h)
                    matched_pairs.append((target_h, mon))
                else:
                    logger.info("跨屏铺满：跳过竖屏 %s，无适合竖屏的纵向窗口", mon.device_name)
            else:
                chosen = None
                if has_portrait_later:
                    for candidate in rem_hwnds:
                        if not is_window_portrait_suitable(candidate, None):
                            chosen = candidate
                            break
                if not chosen:
                    chosen = rem_hwnds[0]
                rem_hwnds.remove(chosen)
                matched_pairs.append((chosen, mon))
    else:
        for hwnd, mon in zip(rem_hwnds, ms):
            matched_pairs.append((hwnd, mon))

    for hwnd, mon in matched_pairs:
        wl = getattr(mon, "work_left", mon.left)
        wt = getattr(mon, "work_top", mon.top)
        ww = getattr(mon, "work_width", mon.width)
        wh = getattr(mon, "work_height", mon.height)
        try:
            if mode == "quad":
                _place_window(hwnd, wl, wt, ww // 2, wh // 2)
            else:
                _place_window(hwnd, wl, wt, ww, wh)
            done += 1
        except Exception:  # noqa: BLE001
            logger.warning("跨屏铺满失败: hwnd=%s 屏=%s", hwnd, mon.device_name,
                           exc_info=True)
    logger.info("跨屏铺满完成(Windows): %d 个窗口 -> %d 块屏（mode=%s）",
                done, len(ms), mode)
    return done



def quad_capacity(hwnds, monitor=None):
    """预检：当前选择在一块屏上最多排得下几个窗口（不动任何窗口）。

    为什么需要它：四宫格的容量是硬上限——App 的最小窗口尺寸改不了，放不下
    四个时「照排四个」的结局只能是重叠或越界。把容量在点「2×2 排列」之前
    摆出来，用户才知道该关谁、换谁，而不是排完看到重叠以为程序算错。

    最小尺寸消费上次排布的缓存（_QUAD_MIN_CACHE）。没探过的窗口按 0 计——
    0 是乐观值，fit 因此是上限；返回值的 uncached 列出没探过的窗口，界面
    据此说明「容量偏乐观」。

    hwnds: 按 左上/右上/左下/右下 顺序的 hwnd 列表（可少于四个）。
    monitor: int 索引 / device_name 字符串 / None（自动）。None 时在横屏里挑
        排得最多的（竖屏不算），并列时取第一个窗口所在屏。

    返回 None 表示一个窗口的最小尺寸都没缓存（从没排过），界面据此不显示。
    """
    hwnds = [h for h in (hwnds or []) if h]
    if not hwnds:
        return None
    cached = {h: _QUAD_MIN_CACHE[h] for h in hwnds if h in _QUAD_MIN_CACHE}
    if not cached:
        return None
    gap, edge_gap = _gap_settings()
    ms = monitors.enum_monitors()
    if not ms:
        return None
    wins = [(h, None) for h in hwnds]
    mins_all = {h: _QUAD_MIN_CACHE.get(h, (0, 0)) for h in hwnds}

    def _fit(m):
        _wl, _wt, mw, mh = m.work_rect
        slots, _unplaced = quad_plan.choose_quad_slots(
            wins, mins_all, mw, mh, gap, edge_gap, origin=(_wl, _wt))
        return len(slots)

    # 点名屏就尊重它（放不下也照样报这块屏的容量）；否则横屏里挑最优
    idx = _resolve_monitor_index(ms, monitor) if monitor is not None else None
    if idx is not None and 0 <= idx < len(ms):
        best = ms[idx]
    else:
        first_idx = None
        try:
            r = get_window_rect(hwnds[0])
            if r and r != (0, 0, 0, 0):
                first_idx = _monitor_index_by_rect(ms, r[0], r[1], r[2], r[3])
        except Exception:  # noqa: BLE001  预检只是建议，读不到几何就按序取
            first_idx = None
        best, best_key = None, None
        for i, m in enumerate(ms):
            if m.height > m.width:
                continue          # 竖屏不送
            key = (_fit(m), 1 if i == first_idx else 0)
            if best is None or key > best_key:
                best, best_key = m, key
        if best is None:          # 一台横屏都没有：退回第一块，容量照算
            best = ms[0]
    need = None
    if len(cached) == len(hwnds):
        corners = [(0, 0)] * 4
        for corner, h in enumerate(hwnds[:4]):
            corners[corner] = mins_all[h]
        need = quad_plan.quad_requirements(corners)
    return {"screen": best.device_name,
            "work": (best.work_rect[2], best.work_rect[3]),
            "fit": _fit(best), "total": len(hwnds),
            "need": need,
            "uncached": [_quad_window_label(h) for h in hwnds
                         if h not in cached]}


def quad_predict(tl_hwnd=None, tr_hwnd=None, bl_hwnd=None, br_hwnd=None,
                 use_pinned=True, monitor=None, smart=False, split_x=0.5,
                 split_y=0.5, blank_tol=100):
    """预演：用上次实测缓存的尺寸推算一次四宫格，**不动任何窗口**。

    与 snap_four_quad 的关系：同一套选窗、同一块目标屏、同一组比例，只把
    「现场实测」换成「上次排布的缓存」（_QUAD_MIN_CACHE / _QUAD_MAX_CACHE），
    一个 set_window_rect 都不调。放不下的窗口、会留大片空白的格子，在点
    「2×2 排列」之前就说清楚。

    任一窗口没有最小尺寸缓存（从没排过）时返回空警告——预演只能闭嘴，正式
    排布会现场实测。这是已知限制：首次排列没有预告，行为与以前一致。

    返回 (warnings, detail)：warnings 是给用户看的一句话列表（空 = 没什么
    要预告的）；detail 是 dict，供日志与界面使用。
    """
    picked, picked_corners, missing = _quad_pick_windows(
        (tl_hwnd, tr_hwnd, bl_hwnd, br_hwnd), use_pinned=use_pinned)
    detail = {"screen": "", "work": (0, 0), "fit": 0, "total": len(picked),
              "splits": None, "blank": [], "uncached": []}
    warnings = []
    if missing:
        names = "、".join(_quad_window_label(h) for h in missing[:3])
        warnings.append("「%s」已不可用，将只排列剩下的窗口" % names)
    if len(picked) < 2:
        return warnings, detail
    mins = {}
    for h, _w in picked:
        m = _QUAD_MIN_CACHE.get(h)
        if not m or m[0] <= 0 or m[1] <= 0:
            detail["uncached"].append(h)
            continue
        mins[h] = m
    if detail["uncached"]:
        return warnings, detail
    try:
        ms = monitors.enum_monitors()
    except Exception:  # noqa: BLE001  预演只是预告，读不到屏就闭嘴
        ms = []
    if not ms:
        return [], detail
    _h0, w0 = picked[0]
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, w0["x"], w0["y"], w0["w"], w0["h"])
    if idx is None or not (0 <= idx < len(ms)):
        idx = 0
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect
    sx, sy = float(split_x), float(split_y)
    if smart and len(picked) == 4:
        # 与 snap_four_quad 的智能分配同一条路径：按宽高比重排角的归属
        try:
            planned, sx2, sy2 = quad_plan.plan_quad_smart(
                [dict(w, hwnd=h) for h, w in picked], ww, wh)
        except ValueError as e:  # noqa: BLE001
            logger.warning("预演：智能分配失败，按手动比例推算: %s", e)
        else:
            reordered = [(w.get("hwnd"), w) for w in planned]
            if len(reordered) == 4 and all(h for h, _w in reordered):
                picked = reordered
                picked_corners = [0, 1, 2, 3]
                sx, sy = sx2, sy2
    sx = min(max(sx, 0.15), 0.85)
    sy = min(max(sy, 0.15), 0.85)
    gap, edge_gap = _gap_settings()
    slots, unplaced = quad_plan.choose_quad_slots(
        [(h, w) for h, w in picked], mins, ww, wh, gap, edge_gap,
        corners=list(picked_corners), origin=(wl, wt))
    if not slots:
        return warnings, detail
    maxes = {h: _QUAD_MAX_CACHE[h] for _c, h, _w in slots
             if h in _QUAD_MAX_CACHE}
    occ = [c for c, _h, _w in slots]
    shape = quad_plan.quad_shape(occ)
    c1 = int(round(ww * sx))
    r1 = int(round(wh * sy))
    cells = quad_plan.quad_cells(shape, c1, r1, wl, wt, ww, wh)
    for corner, h, _w in slots:
        bx, by, bw, bh = cells[corner]
        cap = maxes.get(h) or (0, 0)
        # 这一格窗口实际能占多大：有上限的轴按上限，没上限的轴按格子
        pw = min(bw, cap[0]) if cap[0] > 0 else bw
        ph = min(bh, cap[1]) if cap[1] > 0 else bh
        bw2, bh2 = bw - pw, bh - ph
        if bw2 <= blank_tol and bh2 <= blank_tol:
            continue
        cap_s = []
        if cap[0] > 0:
            cap_s.append("宽最多 %d" % cap[0])
        if cap[1] > 0:
            cap_s.append("高最多 %d" % cap[1])
        side = []
        if bw2 > blank_tol:
            side.append("右侧")
        if bh2 > blank_tol:
            side.append("下方")
        warnings.append(
            "「%s」上次实测%s，这一格是 %d×%d，排完%s约 %dpx 空白"
            % (_quad_window_label(h), "、".join(cap_s), bw, bh,
               "、".join(side), max(bw2, bh2)))
        detail["blank"].append((h, bw2, bh2))
    if unplaced:
        names = "、".join(
            "%s(最小%dx%d)" % (_quad_window_label(h), mins[h][0], mins[h][1])
            for h, _w in unplaced)
        warnings.insert(
            0, "只排 %d/%d 个：%s 按上次实测的最小尺寸放不下这块屏的四宫格，"
               "将保持原位" % (len(slots), len(picked), names))
    detail["screen"] = mon.device_name
    detail["work"] = (ww, wh)
    detail["fit"] = len(slots)
    detail["splits"] = (sx, sy)
    if warnings:
        logger.info("四宫格预演：%s（%s %dx%d，预计 %d/%d 个）",
                    "；".join(warnings), mon.device_name, ww, wh,
                    len(slots), len(picked))
    return warnings, detail


def stash_active(use_pinned=True, activate=False):
    """把当前/目标窗口暂存到屏幕顶边（复刻 Rectangle Pro 的 Stash）。

    返回 True 表示已暂存。已经暂存过的窗口不重复登记。
    """
    import stash
    ms = monitors.enum_monitors()
    if not ms:
        return False
    hwnd = get_foreground_window(use_pinned=use_pinned)
    if not hwnd:
        logger.warning("暂存：未找到可操作的前台窗口")
        return False
    if any(i.get("hwnd") == hwnd for i in stash.list_stashed()):
        logger.info("暂存：%s 已在暂存列表，跳过", hwnd)
        return False
    x, y, w, h = get_window_rect(hwnd)
    if not (x or y or w or h):
        return False
    idx = _monitor_by_relative(ms, hwnd)
    sx, sy, sw, sh = stash.stash_rect(ms[idx])
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    set_window_rect(hwnd, sx, sy, sw, sh, activate=activate)
    stash.push({"hwnd": hwnd, "owner": _proc_name(pid.value) or "",
                "x": x, "y": y, "w": w, "h": h})
    logger.info("已暂存 %s（原位 %s）", hwnd, (x, y, w, h))
    return True


def unstash_first(activate=False):
    """取回最早暂存的窗口到它原来的位置（FIFO）。

    返回 (hwnd, True) 或 (None, False)。窗口已关闭时返回 None。
    """
    import stash
    item = stash.pop()
    if not item:
        return (None, False)
    hwnd = item.get("hwnd")
    if not hwnd:
        return (None, False)
    if not is_window(hwnd):
        logger.info("取回：%s 已关闭，跳过", hwnd)
        return (None, False)
    try:
        set_window_rect(hwnd, item["x"], item["y"], item["w"], item["h"],
                        activate=activate)
    except Exception as e:  # noqa: BLE001
        logger.warning("取回 %s 失败: %s", hwnd, e)
        return (hwnd, False)
    logger.info("已取回 %s -> %s",
                hwnd, (item["x"], item["y"], item["w"], item["h"]))
    return (hwnd, True)


if __name__ == "__main__":
    print("当前显示器:", [m.device_name for m in monitors.enum_monitors()])
