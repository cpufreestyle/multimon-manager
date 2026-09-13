"""窗口跨屏移动与分屏吸附（纯 ctypes）。"""
import ctypes
from ctypes import wintypes

import monitors

user32 = ctypes.windll.user32

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

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

SW_RESTORE = 0x09
SW_MAXIMIZE = 0x03


def get_foreground_window():
    return user32.GetForegroundWindow()


def is_window(hwnd):
    """句柄是否仍指向一个存在的窗口（窗口可能已被关闭）。"""
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def list_windows():
    """枚举当前可见的顶层窗口，返回 [(hwnd, title), ...]。

    仅保留可见且有标题的窗口，隐藏/无标题的系统窗口会被过滤掉。
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


def set_window_rect(hwnd, x, y, w, h, activate=True):
    flags = SWP_NOZORDER | SWP_FRAMECHANGED
    if not activate:
        flags |= SWP_NOACTIVATE
    user32.SetWindowPos(hwnd, None, int(x), int(y), int(w), int(h), flags)
    if activate:
        user32.SetForegroundWindow(hwnd)


def _monitor_by_relative(monitors_list, src_hwnd):
    """根据窗口当前所在屏幕，返回其索引。"""
    x, y, w, h = get_window_rect(src_hwnd)
    cx, cy = x + w // 2, y + h // 2
    for i, m in enumerate(monitors_list):
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return i
    return 0


def move_to_monitor(hwnd, monitor, src=None):
    """将窗口移动到目标显示器，保持相对位置比例。

    src 为显示器列表快照；缺省时自动枚举（已有列表时应传入避免重复调用）。
    """
    x, y, w, h = get_window_rect(hwnd)
    if src is None:
        src = monitors_list_snapshot()
    idx = _monitor_by_relative(src, hwnd)
    src_m = src[idx] if idx < len(src) else src[0]
    rel_x = (x - src_m.left) / max(src_m.width, 1)
    rel_y = (y - src_m.top) / max(src_m.height, 1)
    new_x = monitor.work_left + rel_x * max(monitor.work_width - w, 0)
    new_y = monitor.work_top + rel_y * max(monitor.work_height - h, 0)
    set_window_rect(hwnd, new_x, new_y, w, h)


def snap(hwnd, monitor, zone):
    """将窗口吸附到目标显示器的某个区域。zone 取值:
    left/right/top/bottom/maximize/center，三分屏 left-third/middle-third/right-third，
    四等分 quad-tl/quad-tr/quad-bl/quad-br。"""
    wl, wt, ww, wh = monitor.work_rect
    x, y, w, h = get_window_rect(hwnd)
    if zone == "maximize":
        # 真正最大化（含任务栏避让、动画与双击标题栏还原行为）
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        return
    # 非最大化区域前先还原窗口，否则已最大化的窗口不会被正确缩放/移动
    user32.ShowWindow(hwnd, SW_RESTORE)
    if zone == "left":
        set_window_rect(hwnd, wl, wt, ww // 2, wh)
    elif zone == "right":
        set_window_rect(hwnd, wl + ww // 2, wt, ww - ww // 2, wh)
    elif zone == "top":
        set_window_rect(hwnd, wl, wt, ww, wh // 2)
    elif zone == "bottom":
        set_window_rect(hwnd, wl, wt + wh // 2, ww, wh - wh // 2)
    elif zone == "center":
        set_window_rect(hwnd, wl + (ww - w) // 2, wt + (wh - h) // 2, w, h)
    elif zone == "left-third":
        set_window_rect(hwnd, wl, wt, ww // 3, wh)
    elif zone == "middle-third":
        set_window_rect(hwnd, wl + ww // 3, wt, ww // 3, wh)
    elif zone == "right-third":
        set_window_rect(hwnd, wl + 2 * (ww // 3), wt, ww - 2 * (ww // 3), wh)
    elif zone == "quad-tl":
        set_window_rect(hwnd, wl, wt, ww // 2, wh // 2)
    elif zone == "quad-tr":
        set_window_rect(hwnd, wl + ww // 2, wt, ww - ww // 2, wh // 2)
    elif zone == "quad-bl":
        set_window_rect(hwnd, wl, wt + wh // 2, ww // 2, wh - wh // 2)
    elif zone == "quad-br":
        set_window_rect(hwnd, wl + ww // 2, wt + wh // 2, ww - ww // 2, wh - wh // 2)


def monitors_list_snapshot():
    return monitors.enum_monitors()


def move_window_to_next_monitor(hwnd, direction=1):
    """把指定窗口移到相邻显示器（direction: -1 上一屏 / 1 下一屏）。"""
    ms = monitors.enum_monitors()
    if not ms or not hwnd:
        return False
    idx = _monitor_by_relative(ms, hwnd)
    n = len(ms)
    move_to_monitor(hwnd, ms[(idx + direction) % n], src=ms)
    return True


def snap_window(hwnd, zone):
    """把指定窗口吸附到其所在显示器的某个区域。"""
    ms = monitors.enum_monitors()
    if not ms or not hwnd:
        return False
    idx = _monitor_by_relative(ms, hwnd)
    snap(hwnd, ms[idx], zone)
    return True


def move_active_to_next_monitor(direction=1):
    return move_window_to_next_monitor(get_foreground_window(), direction)


def snap_active(zone):
    return snap_window(get_foreground_window(), zone)


if __name__ == "__main__":
    print("当前显示器:", [m.device_name for m in monitors.enum_monitors()])
