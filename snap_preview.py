"""吸附预览浮层（F3，跨平台）。

用一个置顶、半透明、无边框的高亮窗口标出"窗口将落入的区域"。
关键点：浮层必须**忽略鼠标事件**（点击穿透），否则拖拽经过浮层时会被浮层
截获，导致拖拽中断。macOS 下通过 objc 运行时给 Tk 的 NSWindow 设置
`ignoresMouseEvents = YES`；Windows 下给 HWND 加上
`WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`
（半透明 + 鼠标穿透 + 不抢焦点 + 不进任务栏）。
"""
import sys
import tkinter as tk

_WIN_USER32 = None
if sys.platform.startswith("win"):
    try:
        import ctypes
        _u = ctypes.windll.user32
        # 64 位下指针参数必须显式声明，否则 GetWindowLongPtrW 的返回值会被
        # 当 32 位截断（hotkeys.py 同教训）
        _u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _u.GetWindowLongPtrW.restype = ctypes.c_void_p
        _u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_void_p]
        _u.SetWindowLongPtrW.restype = ctypes.c_void_p
        _WIN_USER32 = _u
    except Exception:  # noqa: BLE001  拿不到 user32 就不做穿透
        _WIN_USER32 = None

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000

_HL_COLOR = "#2f7cf6"
_ALPHA = 0.30


class SnapPreview:
    """懒创建的半透明高亮浮层；不进入任务栏、不接受交互。"""

    def __init__(self, root):
        self.root = root
        self.win = None
        self._visible = False
        self._click_through_bound = False
        # 点击穿透设置失败后不再反复重试：避免每次 show 都调用原生 objc API
        self._click_through_failed = False

    def _ensure(self):
        if self.win is not None:
            return self.win
        w = tk.Toplevel(self.root)
        w.withdraw()
        try:
            w.overrideredirect(True)
        except Exception:  # noqa: BLE001
            pass
        try:
            w.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        try:
            w.attributes("-alpha", _ALPHA)
        except Exception:  # noqa: BLE001
            pass
        try:
            w.configure(bg=_HL_COLOR)
        except Exception:  # noqa: BLE001
            pass
        self.win = w
        return w

    def _bind_click_through(self):
        """令浮层忽略鼠标事件（最多尝试一次，成功或失败后都不再重试）。

        平台做法不同，目的相同：拖拽经过浮层时不被它截获。
        """
        if self._click_through_bound or self._click_through_failed:
            return
        try:
            self.root.update_idletasks()
            if sys.platform == "darwin":
                ok = _set_ignores_mouse_events(self.win.winfo_id())
            elif _WIN_USER32 is not None and self.win is not None:
                ok = _set_win_click_through(_WIN_USER32, self.win.winfo_id())
            else:
                ok = False
            if ok:
                self._click_through_bound = True
            else:
                self._click_through_failed = True
        except Exception:  # noqa: BLE001
            self._click_through_failed = True

    def show(self, x, y, width, height):
        """把浮层显示在 (x, y, width, height)（屏幕坐标，左上原点）。"""
        w = self._ensure()
        try:
            w.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
            if not self._visible:
                w.deiconify()
                self._visible = True
            w.lift()
        except Exception:  # noqa: BLE001
            return
        self._bind_click_through()

    def hide(self):
        if self.win is not None and self._visible:
            try:
                self.win.withdraw()
            except Exception:  # noqa: BLE001
                pass
            self._visible = False

    def destroy(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.win = None
            self._visible = False


def _set_ignores_mouse_events(view_id):
    """通过 objc 消息把 Tk 窗口设为忽略鼠标事件。成功返回 True。

    Tk 在 macOS 上 winfo_id() 返回的是 NSView 指针；依次发 `window`
    取到 NSWindow，再设 `setIgnoresMouseEvents:YES`。
    """
    import ctypes
    import ctypes.util

    path = ctypes.util.find_library("objc")
    if not path:
        return False
    objc = ctypes.CDLL(path)
    objc.objc_msgSend.restype = ctypes.c_void_p
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def sel(name):
        return objc.sel_registerName(name.encode())

    view = ctypes.c_void_p(view_id)
    win = objc.objc_msgSend(view, sel("window"))
    if not win:
        return False
    # setIgnoresMouseEvents: 带 BOOL 参数，单独声明签名
    objc.objc_msgSend.restype = None
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
    objc.objc_msgSend(ctypes.c_void_p(win), sel("setIgnoresMouseEvents:"), True)
    return True


def _set_win_click_through(user32, hwnd):
    """给 Windows 浮层加「半透明 + 鼠标穿透 + 不抢焦点 + 不进任务栏」。

    Tk 的 winfo_id() 在 Windows 上返回的是 HWND。只改扩展样式里我们关心的
    那几位，其它位原样保留——overrideredirect/alpha 已经设过的位不能丢。
    """
    import ctypes
    hwnd = int(hwnd)
    if not hwnd:
        return False
    ex = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE) or 0
    ex |= (_WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE
           | _WS_EX_TOOLWINDOW)
    return bool(user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, ex))
