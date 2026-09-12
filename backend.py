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
else:
    import monitors
    import wallpaper
    import windows
    import hotkeys
    import tray
    import display_notify


# 统一常量（供 UI 使用）
try:
    VK_LEFT = hotkeys.VK_LEFT
    VK_RIGHT = hotkeys.VK_RIGHT
    VK_UP = hotkeys.VK_UP
    VK_DOWN = hotkeys.VK_DOWN
except Exception:  # noqa: BLE001
    VK_LEFT = VK_RIGHT = VK_UP = VK_DOWN = 0

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
snap_two_side_by_side = getattr(windows, "snap_two_side_by_side", lambda *a, **k: False)
snap_three_stack = getattr(windows, "snap_three_stack", lambda *a, **k: False)
HotkeyManager = hotkeys.HotkeyManager
register_display_callback = display_notify.register

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
