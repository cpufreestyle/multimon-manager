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


# ── 本程序自身的进程白名单（窗口操作时应跳过自己） ────────────────────

_OWN_PIDS = set()


def register_own_pid(pid):
    """登记本应用的进程 PID（主进程自动登记，托盘子进程由 main.py 登记）。"""
    if pid:
        _OWN_PIDS.add(int(pid))


register_own_pid(os.getpid())


def get_foreground_window(use_pinned=True):
    """返回当前前台窗口的整数 HWND。

    use_pinned=True（界面按钮）时优先用界面固定的目标窗口；
    use_pinned=False（全局快捷键）时始终作用于真正的活动窗口。
    """
    if use_pinned and _pinned_target:
        if _find_window(_pinned_target):
            return _pinned_target
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


def _proc_name(pid):
    """返回进程可执行文件名（用于窗口 label），失败返回 None。"""
    try:
        h = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            return None
        buf = ctypes.create_unicode_buffer(260)
        psapi.GetModuleBaseNameW(h, None, buf, 260)
        kernel32.CloseHandle(h)
        return buf.value or None
    except Exception:  # noqa: BLE001
        return None


def list_windows_front_to_back(min_size=80):
    """返回按 z 序（最前在前）排列的屏幕窗口列表。

    每项: {"hwnd"(int), "pid", "owner", "name", "x", "y", "w", "h"}
    仅保留可见、有标题、尺寸合理的窗口，并排除本程序自身 PID。
    """
    results = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in _OWN_PIDS:
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
    """按整数 HWND 重新定位窗口，拿到最新几何；不存在则返回 None。"""
    if not hwnd:
        return None
    for w in list_windows_front_to_back():
        if w["hwnd"] == hwnd:
            return w
    return None


def _monitor_index_by_rect(monitors_list, x, y, w, h):
    """按给定矩形（而非 hwnd）判断所在显示器索引。"""
    cx, cy = x + w // 2, y + h // 2
    for i, m in enumerate(monitors_list):
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return i
    return 0


def _place_window(hwnd, x, y, w, h):
    """先还原（退出最大化）再移动到目标矩形并激活到最前。"""
    user32.ShowWindow(hwnd, SW_RESTORE)
    set_window_rect(hwnd, x, y, w, h, activate=True)


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
    cands = [w for w in list_windows_front_to_back()
             if w["pid"] not in _OWN_PIDS]
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
    cands = [w for w in list_windows_front_to_back()
             if w["pid"] not in _OWN_PIDS]
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
