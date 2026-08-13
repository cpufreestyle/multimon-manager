"""全局快捷键（纯 ctypes，消息-only 窗口 + 消息循环）。"""
import ctypes
from ctypes import wintypes
import threading

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 64 位句柄参数必须显式声明，否则被当 32 位截断。
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.RegisterHotKey.restype = ctypes.c_bool
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = ctypes.c_bool
kernel32.GetModuleHandleW.argtypes = [ctypes.c_void_p]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_long
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = ctypes.c_bool
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_long
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = ctypes.c_long
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = ctypes.c_bool
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_long

MOD_ALT = 1
MOD_CONTROL = 2
MOD_SHIFT = 4
MOD_WIN = 8

# 方向键虚拟键码（backend.py 依赖这些常量，缺失会导致注册失败）
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28

WM_HOTKEY = 0x0312
WM_DESTROY = 0x0010


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


# 这些需要 WNDCLASSEXW，放在类定义之后。
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND


class HotkeyManager:
    def __init__(self):
        self.hwnd = None
        self.hotkeys = {}      # id -> (modifiers, vk, callback)
        self.thread = None
        self._running = False
        self._wndproc = None
        self._clsname = "MultiMonHotkeyWnd"

    def register(self, modifiers, vk, callback):
        hid = 1 + len(self.hotkeys)
        self.hotkeys[hid] = (modifiers, vk, callback)
        return hid

    def start(self):
        if self._running or not self.hotkeys:
            return
        self._running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self._wndproc = WNDPROC(self._wndproc_cb)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.lpszClassName = self._clsname
        wc.hInstance = kernel32.GetModuleHandleW(None)
        try:
            atom = user32.RegisterClassExW(ctypes.byref(wc))
            if not atom:
                print(f"[hotkeys] RegisterClassExW 失败, err={ctypes.GetLastError()}")
        except Exception as e:  # noqa: BLE001
            print("[hotkeys] RegisterClassExW 异常:", e)
        self.hwnd = user32.CreateWindowExW(
            0, self._clsname, "hk", 0, 0, 0, 0, 0, wintypes.HWND(-3), None, None, None
        )
        if not self.hwnd:
            print("[hotkeys] CreateWindowExW 失败")
            self._running = False
            return
        for hid, (mod, vk, _cb) in self.hotkeys.items():
            if not user32.RegisterHotKey(self.hwnd, hid, mod, vk):
                print(f"[hotkeys] 注册失败 id={hid} (mod={mod}, vk={vk})")
        msg = wintypes.MSG()
        while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                entry = self.hotkeys.get(msg.wParam)
                if entry and self._running:  # 仅在 running 时回调
                    try:
                        entry[2]()
                    except Exception as e:  # noqa: BLE001
                        print("[hotkeys] 回调异常:", e)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc_cb(self, hwnd, msg, wparam, lparam):
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def stop(self):
        self._running = False
        # 复制一份再遍历，防止字典在迭代中修改
        hks = dict(self.hotkeys)
        if self.hwnd:
            for hid in hks:
                user32.UnregisterHotKey(self.hwnd, hid)
            try:
                user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)
            except Exception:  # noqa: BLE001
                pass
            try:
                user32.DestroyWindow(self.hwnd)
            except Exception:  # noqa: BLE001
                pass
            self.hwnd = None
        # 注意：不清理 self.hotkeys，以便 stop() 之后可以再次 start() 重新注册


if __name__ == "__main__":
    import time
    import windows

    hk = HotkeyManager()
    hk.register(MOD_CONTROL | MOD_ALT, 0x27, lambda: windows.move_active_to_next_monitor(1))
    hk.register(MOD_CONTROL | MOD_ALT, 0x25, lambda: windows.move_active_to_next_monitor(-1))
    hk.start()
    print("快捷键已启动，Ctrl+Alt+←/→ 移动活动窗口。按回车停止。")
    input()
    hk.stop()
