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
else:
    import monitors
    import wallpaper
    import windows
    import hotkeys
    import tray


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
