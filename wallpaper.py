"""多显示器壁纸控制。

优先使用 Windows 8+ 的 IDesktopWallpaper COM 接口实现"每屏不同壁纸"，
失败时回退到 SystemParametersInfoW 设置单一壁纸。
全部基于 ctypes，无第三方依赖。

注意：各显示器的"设备路径"（如 \\\\.\\DISPLAY1）直接取自 EnumDisplayMonitors，
无需调用 COM 的 GetMonitorDevicePathAt（部分环境下该方法会返回 E_FAIL）。
"""
import ctypes
from ctypes import wintypes

import monitors

ole32 = ctypes.windll.ole32
user32 = ctypes.windll.user32

# 在模块加载时声明一次 argtypes，避免每次调用都重复赋值。
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR, wintypes.UINT]
user32.SystemParametersInfoW.restype = ctypes.c_bool
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
user32.SendNotifyMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendNotifyMessageW.restype = ctypes.c_bool


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# 必须设置 argtypes，否则 64 位指针参数会被当成 32 位 c_int 截断。
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_ulong,
    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None


# CLSID_DesktopWallpaper = {C2CF3110-460E-4fc1-B9D0-8A1C0C9CC4BD}
CLSID_DesktopWallpaper = GUID(
    0xC2CF3110, 0x460E, 0x4FC1,
    (ctypes.c_ubyte * 8)(0xB9, 0xD0, 0x8A, 0x1C, 0x0C, 0x9C, 0xC4, 0xBD),
)
# IID_IDesktopWallpaper = {B92B56A9-8B55-4E14-9A89-0199BBB6F93B}
IID_IDesktopWallpaper = GUID(
    0xB92B56A9, 0x8B55, 0x4E14,
    (ctypes.c_ubyte * 8)(0x9A, 0x89, 0x01, 0x99, 0xBB, 0xB6, 0xF9, 0x3B),
)

# DESKTOP_WALLPAPER_POSITION
POSITION = {
    "center": 0,
    "tile": 1,
    "stretch": 2,
    "fit": 3,
    "fill": 4,
    "span": 5,
}


class DesktopWallpaper:
    """IDesktopWallpaper COM 包装（仅用到 SetWallpaper / SetPosition）。"""

    def __init__(self):
        self.pv = None
        self._vtbl = None
        self._available = False
        self._coinit = False
        try:
            hr = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
            self._coinit = hr >= 0
            self._create()
            self._available = True
        except Exception as e:  # noqa: BLE001
            print("[wallpaper] COM 不可用，回退单屏模式:", e)
            self._available = False

    def _create(self):
        pv = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_DesktopWallpaper),
            None,
            0x17,  # CLSCTX_ALL
            ctypes.byref(IID_IDesktopWallpaper),
            ctypes.byref(pv),
        )
        if hr != 0:
            raise ctypes.WinError(hr & 0xFFFFFFFF)
        self.pv = pv
        # pv 指向对象，对象首 8 字节是 vtable 指针，需二次解引用才能得到方法表。
        obj = ctypes.cast(pv, ctypes.POINTER(ctypes.c_void_p))
        vtbl = ctypes.cast(obj[0], ctypes.POINTER(ctypes.c_void_p))
        self._vtbl = vtbl

        HRESULT = ctypes.c_long
        # 方法索引（IUnknown 之后）：
        # 3 SetWallpaper, 10 SetPosition
        self._SetWallpaper = ctypes.WINFUNCTYPE(
            HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p
        )(int(vtbl[3]))
        self._SetPosition = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_uint)(int(vtbl[10]))

    def available(self):
        return self._available

    def set_position(self, name):
        pos = POSITION.get(name, 4)
        self._SetPosition(self.pv, ctypes.c_uint(pos))

    def set_wallpaper(self, monitor_id, path):
        """monitor_id 为设备路径（如 \\\\.\\DISPLAY1）；None 表示所有显示器。"""
        self._SetWallpaper(self.pv, monitor_id, path)

    def close(self):
        """释放 COM 资源。"""
        if self.pv is not None:
            self.pv = None
            self._vtbl = None
        if self._coinit:
            try:
                ole32.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
            self._coinit = False
        self._available = False

    def __del__(self):
        self.close()


_dw = None


def get_desktop_wallpaper():
    global _dw
    if _dw is None:
        _dw = DesktopWallpaper()
    return _dw


def close():
    """释放缓存的 COM 实例（若存在）。供进程退出时调用。"""
    global _dw
    if _dw is not None:
        try:
            _dw.close()
        except Exception:  # noqa: BLE001
            pass
        _dw = None


def apply_per_monitor(mapping, position="fill"):
    """mapping: {device_path: image_path}。position 为全局填充方式。"""
    dw = get_desktop_wallpaper()
    if not dw.available():
        if mapping:
            _set_legacy(next(iter(mapping.values())), position)
        return False
    dw.set_position(position)
    for dev_path, img in mapping.items():
        dw.set_wallpaper(dev_path, img)
    return True


def apply_single(image_path, position="fill"):
    dw = get_desktop_wallpaper()
    if not dw.available():
        _set_legacy(image_path, position)
        return False
    dw.set_position(position)
    dw.set_wallpaper(None, image_path)
    return True


def _set_legacy(path, position):
    """无 COM 时的回退方案：设置注册表并调用 SystemParametersInfoW + 广播刷新。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0, winreg.KEY_SET_VALUE)
        style = {"fill": "10", "fit": "6", "stretch": "2", "tile": "0", "center": "0", "span": "22"}.get(position, "10")
        tile = "0" if position != "tile" else "1"
        winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, style)
        winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, tile)
        winreg.CloseKey(key)
    except Exception:  # noqa: BLE001
        pass
    user32.SystemParametersInfoW(20, 0, path, 3)  # SPI_SETDESKWALLPAPER | UPDATEINIFILE | SENDCHANGE
    # 额外广播 WM_SETTINGCHANGE 确保所有窗口即时刷新
    user32.SendNotifyMessageW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment")


if __name__ == "__main__":
    dw = get_desktop_wallpaper()
    print("available:", dw.available())
    print("monitors:", [m.device_name for m in monitors.enum_monitors()])
