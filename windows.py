"""窗口跨屏移动与分屏吸附（纯 ctypes / Win32）。"""
import ctypes
import logging
import os
from ctypes import wintypes

import monitors

logger = logging.getLogger(__name__)

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
    if exact:
        l, t, r, b = frame_insets(hwnd)
        x, y, w, h = x - l, y - t, w + l + r, h + t + b
    flags = SWP_NOZORDER | SWP_FRAMECHANGED
    if not activate:
        flags |= SWP_NOACTIVATE
    ok = user32.SetWindowPos(hwnd, None, int(x), int(y), int(w), int(h), flags)
    if activate:
        user32.SetForegroundWindow(hwnd)
    return bool(ok)


def _monitor_by_relative(monitors_list, src_hwnd):
    """根据窗口当前所在屏幕，返回其索引。"""
    x, y, w, h = get_window_rect(src_hwnd)
    cx, cy = x + w // 2, y + h // 2
    for i, m in enumerate(monitors_list):
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return i
    return 0


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
    if zone == "left":
        ok = set_window_rect(hwnd, wl, wt, ww // 2, wh, activate=activate, exact=True)
    elif zone == "right":
        ok = set_window_rect(hwnd, wl + ww // 2, wt, ww - ww // 2, wh,
                             activate=activate, exact=True)
    elif zone == "top":
        ok = set_window_rect(hwnd, wl, wt, ww, wh // 2, activate=activate, exact=True)
    elif zone == "bottom":
        ok = set_window_rect(hwnd, wl, wt + wh // 2, ww, wh - wh // 2,
                             activate=activate, exact=True)
    elif zone == "center":
        ok = set_window_rect(hwnd, wl + (ww - w) // 2, wt + (wh - h) // 2, w, h,
                             activate=activate, exact=True)
    elif zone == "left-third":
        ok = set_window_rect(hwnd, wl, wt, ww // 3, wh, activate=activate, exact=True)
    elif zone == "middle-third":
        ok = set_window_rect(hwnd, wl + ww // 3, wt, ww // 3, wh,
                             activate=activate, exact=True)
    elif zone == "right-third":
        ok = set_window_rect(hwnd, wl + 2 * (ww // 3), wt, ww - 2 * (ww // 3), wh,
                             activate=activate, exact=True)
    elif zone == "quad-tl":
        ok = set_window_rect(hwnd, wl, wt, ww // 2, wh // 2,
                             activate=activate, exact=True)
    elif zone == "quad-tr":
        ok = set_window_rect(hwnd, wl + ww // 2, wt, ww - ww // 2, wh // 2,
                             activate=activate, exact=True)
    elif zone == "quad-bl":
        ok = set_window_rect(hwnd, wl, wt + wh // 2, ww // 2, wh - wh // 2,
                             activate=activate, exact=True)
    elif zone == "quad-br":
        ok = set_window_rect(hwnd, wl + ww // 2, wt + wh // 2, ww - ww // 2,
                             wh - wh // 2, activate=activate, exact=True)
    else:
        logger.warning("snap: 未知区域 %s", zone)
        return False
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
    """按给定矩形（而非 hwnd）判断所在显示器索引。"""
    cx, cy = x + w // 2, y + h // 2
    for i, m in enumerate(monitors_list):
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return i
    return 0


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


if __name__ == "__main__":
    print("当前显示器:", [m.device_name for m in monitors.enum_monitors()])
