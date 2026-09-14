"""吸附预览浮层（F3，跨平台）。

用一个置顶、半透明、无边框的高亮窗口标出"窗口将落入的区域"。
关键点：浮层必须**忽略鼠标事件**（点击穿透），否则拖拽经过浮层时会被浮层
截获，导致拖拽中断。macOS 下通过 objc 运行时给 Tk 的 NSWindow 设置
`ignoresMouseEvents = YES`；其它平台暂不设置（Windows 可用 WS_EX_TRANSPARENT，
此处未实现，F3 以 macOS 优先）。
"""
import sys
import tkinter as tk

_HL_COLOR = "#2f7cf6"
_ALPHA = 0.30


class SnapPreview:
    """懒创建的半透明高亮浮层；不进入任务栏、不接受交互。"""

    def __init__(self, root):
        self.root = root
        self.win = None
        self._visible = False
        self._click_through_bound = False

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
        """macOS：令浮层忽略鼠标事件（只需成功一次）。"""
        if self._click_through_bound or sys.platform != "darwin" or self.win is None:
            return
        try:
            self.root.update_idletasks()
            if _set_ignores_mouse_events(self.win.winfo_id()):
                self._click_through_bound = True
        except Exception:  # noqa: BLE001
            pass

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
