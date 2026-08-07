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

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


def get_foreground_window():
    return user32.GetForegroundWindow()


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


def move_to_monitor(hwnd, monitor):
    """将窗口移动到目标显示器，保持相对位置比例。"""
    x, y, w, h = get_window_rect(hwnd)
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
    left/right/top/bottom/maximize/center。"""
    wl, wt, ww, wh = monitor.work_rect
    x, y, w, h = get_window_rect(hwnd)
    if zone == "left":
        set_window_rect(hwnd, wl, wt, ww // 2, wh)
    elif zone == "right":
        set_window_rect(hwnd, wl + ww // 2, wt, ww - ww // 2, wh)
    elif zone == "top":
        set_window_rect(hwnd, wl, wt, ww, wh // 2)
    elif zone == "bottom":
        set_window_rect(hwnd, wl, wt + wh // 2, ww, wh - wh // 2)
    elif zone == "maximize":
        set_window_rect(hwnd, wl, wt, ww, wh)
    elif zone == "center":
        set_window_rect(hwnd, wl + (ww - w) // 2, wt + (wh - h) // 2, w, h)


def monitors_list_snapshot():
    return monitors.enum_monitors()


def move_active_to_next_monitor(direction=1):
    ms = monitors.enum_monitors()
    if not ms:
        return
    hwnd = get_foreground_window()
    if not hwnd:
        return
    idx = _monitor_by_relative(ms, hwnd)
    n = len(ms)
    nxt = (idx + direction) % n
    move_to_monitor(hwnd, ms[nxt])


def snap_active(zone):
    ms = monitors.enum_monitors()
    if not ms:
        return
    hwnd = get_foreground_window()
    if not hwnd:
        return
    idx = _monitor_by_relative(ms, hwnd)
    snap(hwnd, ms[idx], zone)


if __name__ == "__main__":
    print("当前显示器:", [m.device_name for m in monitors.enum_monitors()])
