"""系统托盘图标（纯 ctypes，Shell_NotifyIcon）。"""
import ctypes
from ctypes import wintypes
import os
import threading

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32
kernel32.GetModuleHandleW.argtypes = [ctypes.c_void_p]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p

WM_TRAY = 0x8000 + 20
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202
WM_COMMAND = 0x0111

NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("hWnd", wintypes.HWND),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_ulong),
        ("dwStateMask", ctypes.c_ulong),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_ulong),
        ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


# 64 位句柄参数必须显式声明，否则被当 32 位截断。
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_long
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_uint, wintypes.LPCWSTR]
user32.AppendMenuW.restype = ctypes.c_bool
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = ctypes.c_bool
user32.TrackPopupMenuEx.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenuEx.restype = ctypes.c_bool
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = ctypes.c_bool
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = ctypes.c_bool
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = ctypes.c_bool
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = ctypes.c_long
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.LoadImageW.restype = ctypes.c_void_p
shell32.Shell_NotifyIconW.argtypes = [ctypes.c_ulong, ctypes.POINTER(NOTIFYICONDATA)]
shell32.Shell_NotifyIconW.restype = ctypes.c_long


class TrayIcon:
    def __init__(self):
        self.hwnd = None
        self.hicon = None
        self.on_open = None
        self.on_exit = None
        self._wndproc = None
        self._clsname = "MultiMonTrayWnd"

    def create(self, icon_path, tip="多屏管理器", on_open=None, on_exit=None):
        self.on_open = on_open
        self.on_exit = on_exit
        if icon_path and os.path.exists(icon_path):
            self.hicon = user32.LoadImageW(
                0, icon_path, 1, 0, 0, 0x00000010 | 0x00000080  # LR_DEFAULTSIZE | LR_LOADFROMFILE
            )

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam == WM_RBUTTONUP:
                    self._show_menu()
                elif lparam == WM_LBUTTONUP:
                    if self.on_open:
                        self.on_open()
                return 0
            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == 1001 and self.on_open:
                    self.on_open()
                elif cmd == 1002 and self.on_exit:
                    self.on_exit()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.lpszClassName = self._clsname
        wc.hInstance = kernel32.GetModuleHandleW(None)
        try:
            user32.RegisterClassExW(ctypes.byref(wc))
        except Exception:  # noqa: BLE001
            print("[tray] RegisterClassExW 异常")
        self.hwnd = user32.CreateWindowExW(
            0, self._clsname, "tray", 0, 0, 0, 0, 0, wintypes.HWND(-3), None, None, None
        )
        if not self.hwnd:
            print("[tray] CreateWindowExW 失败，托盘不可用")
            return

        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon or 0
        nid.szTip = tip[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        self._thread = threading.Thread(target=self._message_loop, daemon=True)
        self._thread.start()

    def _message_loop(self):
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _show_menu(self):
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, 0x0000, 1001, "打开主界面")
        user32.AppendMenuW(hmenu, 0x0000, 1002, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        user32.TrackPopupMenuEx(hmenu, 0, pt.x, pt.y, self.hwnd, None)
        user32.DestroyMenu(hmenu)

    def destroy(self):
        if self.hwnd:
            nid = NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
            nid.hWnd = self.hwnd
            nid.uID = 1
            try:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            except Exception:  # noqa: BLE001
                pass
            try:
                user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_DESTROY
            except Exception:  # noqa: BLE001
                pass
            try:
                user32.DestroyWindow(self.hwnd)
            except Exception:  # noqa: BLE001
                pass
            self.hwnd = None
        self.on_open = None
        self.on_exit = None


if __name__ == "__main__":
    t = TrayIcon()
    t.create(None, "测试托盘", on_open=lambda: print("open"), on_exit=lambda: print("exit"))
    print("托盘已启动，右键查看菜单。")
    import time
    time.sleep(10)
    t.destroy()
