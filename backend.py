"""平台后端分发。

根据 sys.platform 选择对应的 Windows / macOS 实现模块，并对外暴露统一的
monitors / wallpaper / windows / hotkeys / tray 接口，供 ui.py / main.py / profiles.py 使用。

Windows: monitors, wallpaper, windows, hotkeys, tray (ctypes)
macOS:   monitors_mac, wallpaper_mac, windows_mac, hotkeys_mac, tray_mac (系统命令 / 框架)

新增平台时，只需在此处增加分支，并新增 *_<platform>.py 即可。
"""
import sys

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"


if IS_MAC:
    import monitors_mac as monitors
    import wallpaper_mac as wallpaper
    import windows_mac as windows
    import hotkeys_mac as hotkeys
    import tray_mac as tray
    import display_notify_mac as display_notify
    import accessibility_mac as accessibility
    import dragsnap_mac as dragsnap
else:
    import monitors
    import wallpaper
    import windows
    import hotkeys
    import tray
    import display_notify
    import dragsnap


# 统一常量（供 UI 使用）
try:
    VK_LEFT = hotkeys.VK_LEFT
    VK_RIGHT = hotkeys.VK_RIGHT
    VK_UP = hotkeys.VK_UP
    VK_DOWN = hotkeys.VK_DOWN
    VK_Z = hotkeys.VK_Z
except Exception:  # noqa: BLE001
    VK_LEFT = VK_RIGHT = VK_UP = VK_DOWN = VK_Z = 0

MOD_ALT = getattr(hotkeys, "MOD_ALT", 1)
MOD_CONTROL = getattr(hotkeys, "MOD_CONTROL", 2)
MOD_WIN = getattr(hotkeys, "MOD_WIN", 8)
MOD_SHIFT = getattr(hotkeys, "MOD_SHIFT", 4)
DIGIT_KEYS = getattr(hotkeys, "DIGIT_KEYS", {d: ord(str(d)) for d in range(10)})

# 重新导出子模块的关键属性，供 ui.py 通过 backend 直接访问
# （ui.py 使用 `import backend as wallpaper` 等别名，需要这些属性在 backend 根空间）
POSITION = wallpaper.POSITION
apply_single = wallpaper.apply_single
apply_per_monitor = wallpaper.apply_per_monitor
enum_monitors = monitors.enum_monitors
stage_manager_enabled = monitors.stage_manager_enabled
move_active_to_next_monitor = windows.move_active_to_next_monitor
snap_active = windows.snap_active
list_target_windows = windows.list_target_windows
set_target = windows.set_target
set_window_rect = windows.set_window_rect
# 把指定窗口移到目标显示器（两端签名第三参不同但都可省略，统一按两参调用）
move_to_monitor = getattr(windows, "move_to_monitor", lambda *a, **k: False)
snap_two_side_by_side = getattr(windows, "snap_two_side_by_side", lambda *a, **k: False)
snap_three_stack = getattr(windows, "snap_three_stack", lambda *a, **k: False)
# 窗口置顶切换（Windows 实现；macOS 暂无对应实现时为 None，UI 自动隐藏入口）
toggle_topmost = getattr(windows, "toggle_topmost", None)
HotkeyManager = hotkeys.HotkeyManager
# 拖拽吸附监听（F3；macOS 为真实实现，Windows 为占位）
DragSnapWatcher = dragsnap.DragSnapWatcher
register_display_callback = display_notify.register
# 撤销上一次窗口移动（F7）
undo_last_move = windows.undo_last_move


def move_target_to_monitor(index, use_pinned=True, activate=False):
    """把目标/活动窗口移动到**指定索引**的显示器（跨平台统一入口）。

    与 move_active_to_next_monitor 的区别：那个只能相邻屏逐个跳，多屏时很费事；
    这里一次到位。activate=False 时只移动不激活（界面按钮模式）。

    Windows 与 macOS 的 move_to_monitor 签名不同（Windows 多一个 src 快照参数），
    故这里必须用关键字传 activate，不能按位置传。
    """
    try:
        ms = monitors.enum_monitors()
    except Exception:  # noqa: BLE001
        return False
    if index is None or not isinstance(index, int) or not (0 <= index < len(ms)):
        return False
    hwnd = windows.get_foreground_window(use_pinned)
    if not hwnd:
        return False
    windows.move_to_monitor(hwnd, ms[index], activate=activate)
    return True

# 辅助功能授权（仅 macOS 有意义；Windows 始终视为已授权）
if IS_MAC:
    is_accessibility_trusted = accessibility.is_trusted
    request_accessibility = accessibility.request_trusted
    open_accessibility_settings = accessibility.open_settings
else:
    def is_accessibility_trusted():
        return True

    def request_accessibility():
        return False

    def open_accessibility_settings():
        pass
