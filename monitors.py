"""显示器枚举与虚拟桌面信息（纯 ctypes，无第三方依赖）。"""
import copy
import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.windll.user32

# 枚举结果缓存（TTL=2s，避免同一次操作内重复调用 Windows API）
_cache = {"data": None, "ts": 0}
_CACHE_TTL = 2.0


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]

    def as_tuple(self):
        return (self.left, self.top, self.right - self.left, self.bottom - self.top)


# EnumDisplayMonitors 的回调与参数类型只需声明一次（模块加载时，且 RECT 已定义）。
MonitorEnumProc = ctypes.WINFUNCTYPE(
    ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), ctypes.c_void_p
)
user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(RECT),
    MonitorEnumProc,
    ctypes.c_void_p,
]
user32.EnumDisplayMonitors.restype = ctypes.c_bool


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


@dataclass
class MonitorInfo:
    index: int
    device_name: str        # 设备名，如 \\.\DISPLAY1
    device_path: str = ""   # IDesktopWallpaper 用的监视器路径（如 \\.\DISPLAY1）
    is_primary: bool = False
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    work_left: int = 0
    work_top: int = 0
    work_width: int = 0
    work_height: int = 0

    @property
    def rect(self):
        return (self.left, self.top, self.width, self.height)

    @property
    def work_rect(self):
        return (self.work_left, self.work_top, self.work_width, self.work_height)


def enum_monitors():
    """枚举所有显示器，返回 MonitorInfo 列表（按设备顺序排列）。带短期缓存。"""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        # 返回深拷贝，避免调用方修改污染缓存
        return copy.deepcopy(_cache["data"])
    monitors = []

    def callback(hmon, hdc, lprect, lparam):
        info = MONITORINFOEX()
        info.cbSize = ctypes.sizeof(MONITORINFOEX)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            m = MonitorInfo(
                index=len(monitors),
                device_name=info.szDevice,
                device_path=info.szDevice,
                is_primary=bool(info.dwFlags & 1),
                left=info.rcMonitor.left,
                top=info.rcMonitor.top,
                width=info.rcMonitor.right - info.rcMonitor.left,
                height=info.rcMonitor.bottom - info.rcMonitor.top,
                work_left=info.rcWork.left,
                work_top=info.rcWork.top,
                work_width=info.rcWork.right - info.rcWork.left,
                work_height=info.rcWork.bottom - info.rcWork.top,
            )
            monitors.append(m)
        return True

    proc = MonitorEnumProc(callback)
    user32.EnumDisplayMonitors(None, None, proc, None)
    _cache["data"] = list(monitors)
    _cache["ts"] = time.time()
    return copy.deepcopy(_cache["data"])


def get_virtual_screen():
    """返回虚拟桌面包围盒 (x, y, w, h)。"""
    x = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    y = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    w = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    h = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
    return (x, y, w, h)


def get_primary_monitor():
    for m in enum_monitors():
        if m.is_primary:
            return m
    return None


if __name__ == "__main__":
    for m in enum_monitors():
        print(m)
    print("virtual:", get_virtual_screen())
