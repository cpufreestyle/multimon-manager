"""拖拽吸附监听（macOS，F3，零第三方依赖）。

独立的 CGEventTap（与快捷键那个分开，互不影响）：只监听左键
按下/拖拽/抬起，把鼠标位置回调给 UI；由 UI 判断"是否靠近屏幕边缘"、
显示吸附预览浮层，并在松手时执行吸附。

线程模型与 hotkeys_mac 完全一致（重要）：tap 的 runloop source 注册在
**主线程** CFRunLoop（CFRunLoopGetMain），由 Tk 的 mainloop 驱动，回调在主线程
执行。绝不要改成后台线程跑 CFRunLoopRun —— 与 Tk 的 runloop 并存会在
Python 3.13/3.14 触发 Fatal Python error: PyEval_RestoreThread。

回调必须极快返回：macOS 对事件 tap 有超时保护，回调阻塞过久会禁用它。
因此本模块只读取鼠标坐标并转发，不做任何窗口枚举/子进程调用。
"""
import ctypes

import hotkeys_mac as _hk

# CGEventType
kCGEventLeftMouseDown = 1
kCGEventLeftMouseUp = 2
kCGEventLeftMouseDragged = 6
_MASK = ((1 << kCGEventLeftMouseDown)
         | (1 << kCGEventLeftMouseUp)
         | (1 << kCGEventLeftMouseDragged))

_STAGE_BY_TYPE = {
    kCGEventLeftMouseDown: "down",
    kCGEventLeftMouseUp: "up",
    kCGEventLeftMouseDragged: "drag",
}


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _bind_location():
    q = _hk._quartz
    if q is None:
        return False
    try:
        q.CGEventGetLocation.restype = CGPoint
        q.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        return True
    except Exception:  # noqa: BLE001
        return False


class DragSnapWatcher:
    """监听鼠标拖拽并把 (stage, x, y) 回调给主线程。

    stage 取值：'down' / 'drag' / 'up'；x, y 为屏幕坐标（左上原点）。
    """

    def __init__(self):
        self._cb = None
        self._tap = None
        self._src = None
        self._rl = None
        self._c_cb = None
        self._running = False

    def set_callback(self, cb):
        self._cb = cb

    def start(self):
        q = _hk._quartz
        if self._running:
            return True
        if q is None or _hk._COMMON_MODES is None:
            print("[dragsnap] 缺少 Quartz/runloop，拖拽吸附不可用")
            return False
        if not _bind_location():
            print("[dragsnap] 绑定 CGEventGetLocation 失败")
            return False
        self._c_cb = _hk.CGEventTapCallBack(self._handler)
        self._tap = q.CGEventTapCreate(
            _hk.kCGSessionEventTap, _hk.kCGHeadInsertEventTap,
            _hk.kCGEventTapOptionDefault, _MASK, self._c_cb, None,
        )
        if not self._tap:
            print("[dragsnap] 创建鼠标事件监听失败（需要辅助功能权限）")
            return False
        self._src = q.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        if not self._src:
            self._tap = None
            return False
        self._rl = q.CFRunLoopGetMain()
        q.CFRunLoopAddSource(self._rl, self._src, _hk._COMMON_MODES)
        self._running = True
        return True

    def stop(self):
        if not self._running:
            return
        self._running = False
        q = _hk._quartz
        if q is None:
            return
        if self._tap:
            try:
                q.CGEventTapEnable(self._tap, False)
            except Exception:  # noqa: BLE001
                pass
        if self._rl and self._src:
            try:
                q.CFRunLoopRemoveSource(self._rl, self._src, _hk._COMMON_MODES)
            except Exception:  # noqa: BLE001
                pass
        self._tap = self._src = self._rl = None

    def _handler(self, proxy, etype, event, refcon):
        etype_u = etype & 0xFFFFFFFF
        if etype_u in (0xFFFFFFFE, 0xFFFFFFFF):  # tap 被禁用：自愈
            if self._tap:
                try:
                    _hk._quartz.CGEventTapEnable(self._tap, True)
                except Exception:  # noqa: BLE001
                    pass
            return event
        stage = _STAGE_BY_TYPE.get(etype_u)
        if stage is not None and self._cb is not None:
            try:
                p = _hk._quartz.CGEventGetLocation(event)
                self._cb(stage, int(p.x), int(p.y))
            except Exception:  # noqa: BLE001
                pass
        return event
