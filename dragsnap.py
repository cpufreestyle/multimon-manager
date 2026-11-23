"""拖拽吸附监听。

macOS 用 CGEventTap（windows_mac 侧），Windows 用 WH_MOUSE_LL 低级鼠标
钩子。两端对上层的契约一致：set_callback(cb) 收一个 cb(stage, x, y)，
stage ∈ {"down", "drag", "up"}，坐标是屏幕绝对坐标；start() 返回是否真的
装上了监听。

Windows 侧的两个硬约束决定了下面这个写法：
   1. 低级鼠标钩子有系统超时（默认 300ms）。回调里做任何 UI / 窗口枚举 /
      阻塞调用，系统会直接摘掉钩子，表现为「预览时有时无」。所以回调里只
      把事件转交上层就立刻 CallNextHookEx。
   2. WH_MOUSE_LL 的回调跑在**安装它的那条线程**上，那条线程必须有消息
      循环。因此钩子装在一条专用线程上，界面主循环（Tk）完全不受影响；
      停止时给它发 WM_QUIT 让它自然退出，再摘钩子。
"""

import ctypes
import threading

try:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
except AttributeError:  # macOS / 非 Windows：没有 windll
    user32 = None
    wintypes = None


def _log(msg):
    """拖拽钩子异常记日志（记不上也不影响功能）。"""
    try:
        import logging
        logging.getLogger(__name__).warning("拖拽吸附监听：%s", msg)
    except Exception:  # noqa: BLE001
        pass


if user32 is not None:
    # 低级鼠标钩子常量
    WH_MOUSE_LL = 14
    HC_ACTION = 0
    WM_QUIT = 0x0012
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202

    # 回调签名：(nCode, wParam, lParam) -> LRESULT。64 位下句柄/指针参数
    # 必须显式声明，否则会被当 32 位截断（hotkeys.py 同教训）。
    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)

    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_long
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                   wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
    user32.GetMessageW.restype = ctypes.c_long
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_long
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint,
                                          wintypes.WPARAM, wintypes.LPARAM]
    user32.PostThreadMessageW.restype = wintypes.BOOL

    class MSLLHOOKSTRUCT(ctypes.Structure):
        """WH_MOUSE_LL 的 lParam 指向的结构，这里只用到鼠标坐标。"""
        _fields_ = [("pt", wintypes.POINT),
                    ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p)]


class DragSnapWatcher:
    """全局拖拽监听（Windows：WH_MOUSE_LL）。

    钩子装在专用线程的消息循环上；回调只做「取坐标 → 转交上层」就返回。
    set_callback(cb) 之后 start() / stop() 可反复调用。
    """

    def __init__(self):
        self._cb = None
        self._thread = None
        self._hook = None
        self._installed = False
        # HOOKPROC 实例必须保活：被垃圾回收后系统再调它就是野指针崩溃
        self._hook_ref = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def set_callback(self, cb):
        self._cb = cb

    def start(self):
        if user32 is None:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._ready.clear()
            self._installed = False
            t = threading.Thread(target=self._run, name="dragsnap-hook",
                                 daemon=True)
            self._thread = t
        t.start()
        # 装钩子要等线程跑起来；超时按装不上处理，别把界面卡住
        if not self._ready.wait(3.0):
            self.stop()
            return False
        return self._installed

    def _run(self):
        # CPython 在 Windows 上 threading.get_ident() 就是 Win32 线程 ID，
        # 直接拿它当 PostThreadMessageW 的目标（少一次 kernel32 调用）
        tid = threading.get_ident()

        def _hook_proc(ncode, wparam, lparam):
            try:
                if ncode == HC_ACTION and self._cb is not None:
                    stage = {WM_LBUTTONDOWN: "down", WM_MOUSEMOVE: "drag",
                             WM_LBUTTONUP: "up"}.get(int(wparam or 0))
                    if stage is not None:
                        info = ctypes.cast(
                            lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        self._cb(stage, info.pt.x, info.pt.y)
            except Exception:  # noqa: BLE001  钩子回调里绝不能抛出去
                pass
            return user32.CallNextHookEx(None, ncode, wparam, lparam)

        self._hook_ref = HOOKPROC(_hook_proc)
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hook_ref,
                                              None, 0)
        self._installed = bool(self._hook)
        self._ready.set()
        if not self._hook:
            # 装不上（权限不足 / 被安全软件拦）：退出消息循环，让 start() 失败
            _log("SetWindowsHookExW(WH_MOUSE_LL) 返回空，拖拽监听不可用")
            return
        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:      # 0 = WM_QUIT，-1 = 出错
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        # 消息循环已退出：此时摘钩子不会与在途的回调撞上
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def stop(self):
        with self._lock:
            t = self._thread
            self._thread = None
        if t is None:
            return
        if t.ident and t.ident != threading.get_ident():
            user32.PostThreadMessageW(t.ident, WM_QUIT, 0, 0)
        t.join(3.0)
        if t.is_alive():
            # 线程没退出就先摘钩子：宁可丢监听，也不能留下一个全局鼠标钩子
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None
