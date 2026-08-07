"""窗口跨屏移动与分屏吸附（macOS 实现，零第三方依赖）。

通过 System Events 的 AppleScript 查询/操作最前面的窗口。
macOS 坐标系原点在左上，与 Windows 转换后的坐标系一致。
"""
import subprocess

import monitors_mac as monitors

# AppleScript 里窗口 bounds 是 {x, y, width, height}（左上原点）


def _osa(script):
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def get_foreground_window():
    # macOS 没有直接"前台窗口 hwnd"，返回 ('app', 'window') 标识
    script = (
        'tell application "System Events"\n'
        '  set frontApp to name of first application process whose frontmost is true\n'
        '  tell process frontApp\n'
        '    if (count of windows) > 0 then\n'
        '      return frontApp & "::" & (name of window 1)\n'
        '    end if\n'
        '  end tell\n'
        'end tell\n'
        'return ""\n'
    )
    res = _osa(script)
    return res or None


def get_window_rect(hwnd):
    if not hwnd:
        return (0, 0, 0, 0)
    app, win = hwnd.split("::", 1)
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    set b to (get bounds of window 1)\n'
        f'    return (item 1 of b & "," & item 2 of b & "," & (item 3 of b - item 1 of b) & "," & (item 4 of b - item 2 of b))\n'
        f'  end tell\n'
        f'end tell\n'
    )
    out = _osa(script)
    try:
        x, y, w, h = (int(float(v)) for v in out.split(","))
        return (x, y, w, h)
    except Exception:  # noqa: BLE001
        return (0, 0, 0, 0)


def set_window_rect(hwnd, x, y, w, h, activate=True):
    if not hwnd:
        return
    app, _ = hwnd.split("::", 1)
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    set bounds of window 1 to {{{int(x)}, {int(y)}, {int(x)+int(w)}, {int(y)+int(h)}}}\n'
        f'  end tell\n'
        f'end tell\n'
    )
    _osa(script)
    if activate:
        _osa(f'tell application "{app}" to activate')


def _monitor_by_relative(monitors_list, src_hwnd):
    x, y, w, h = get_window_rect(src_hwnd)
    cx, cy = x + w // 2, y + h // 2
    for i, m in enumerate(monitors_list):
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return i
    return 0


def move_to_monitor(hwnd, monitor):
    x, y, w, h = get_window_rect(hwnd)
    src = monitors.enum_monitors()
    idx = _monitor_by_relative(src, hwnd)
    src_m = src[idx] if idx < len(src) else src[0]
    rel_x = (x - src_m.left) / max(src_m.width, 1)
    rel_y = (y - src_m.top) / max(src_m.height, 1)
    new_x = monitor.work_left + rel_x * max(monitor.work_width - w, 0)
    new_y = monitor.work_top + rel_y * max(monitor.work_height - h, 0)
    set_window_rect(hwnd, new_x, new_y, w, h)


def snap(hwnd, monitor, zone):
    wl, wt, ww, wh = monitor.work_rect
    _x, _y, w, h = get_window_rect(hwnd)
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
    hwnd = get_foreground_window()
    print("front window:", hwnd)
    if hwnd:
        print("rect:", get_window_rect(hwnd))
