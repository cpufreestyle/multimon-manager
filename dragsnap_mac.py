"""拖拽吸附监听（macOS，F3，零第三方依赖）。

独立的 CGEventTap（与快捷键那个分开，互不影响）：只监听左键
按下/拖拽/抬起，把鼠标位置回调给 UI；由 UI 判断"是否靠近屏幕边缘"、
显示吸附预览浮层，并在松手时执行吸附。

线程模型（重要，与 hotkeys_mac 修正后一致）：tap 建在**专用线程**自己的
CFRunLoop 上，不碰 Tk 的主线程 runloop。旧实现把 source 加到
CFRunLoopGetMain()，注释还写着「绝不要改后台线程」——实测那正是崩溃根因：
mouse dragged 事件极频繁，每次都在 Tcl 释放 GIL 的窗口期回调进 Python，
触发 Fatal Python error: PyEval_RestoreThread（tstate 为 NULL），进程 abort。
真机二分定位：只禁掉拖拽吸附就不再崩（rc 124 存活 vs 134 abort）。
回调只入队/读坐标，不碰 tkinter，放后台线程安全；stop() 用 CFRunLoopStop 唤醒。

回调必须极快返回：macOS 对事件 tap 有超时保护，回调阻塞过久会禁用它。
因此本模块只读取鼠标坐标并转发，不做任何窗口枚举/子进程调用。
"""
import ctypes
import threading

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
        self._tap_thread = None
        # tap 字段会被 stop()（调用方线程）置 None，而 _handler 在 tap 线程读
        # 它们——不加锁就是数据竞争，实测踩出过 SIGSEGV。
        self._lock = threading.Lock()

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
        # 必须在**专用线程**里建 tap 并取该线程的 runloop：CFRunLoopGetCurrent
        # 是线程局部的。放主线程会让 mouse dragged 在 Tcl 的 GIL 窗口期回调进
        # Python，直接 abort（见模块 docstring）。
        ready = threading.Event()
        failed = threading.Event()

        def _run():
            self._c_cb = _hk.CGEventTapCallBack(self._handler)
            self._tap = q.CGEventTapCreate(
                _hk.kCGSessionEventTap, _hk.kCGHeadInsertEventTap,
                _hk.kCGEventTapOptionDefault, _MASK, self._c_cb, None,
            )
            if not self._tap:
                print("[dragsnap] 创建鼠标事件监听失败（需要辅助功能权限）")
                failed.set()
                ready.set()
                return
            self._src = q.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            if not self._src:
                self._tap = None
                failed.set()
                ready.set()
                return
            self._rl = q.CFRunLoopGetCurrent()
            q.CFRunLoopAddSource(self._rl, self._src, _hk._COMMON_MODES)
            ready.set()
            q.CFRunLoopRun()

        self._tap_thread = threading.Thread(target=_run, daemon=True)
        self._tap_thread.start()
        ready.wait(5)
        if failed.is_set() or not self._tap or not self._src:
            self._running = False
            return False
        self._running = True
        return True

    def stop(self):
        q = _hk._quartz
        with self._lock:
            if not self._running:
                return
            self._running = False
            tap, src, rl = self._tap, self._src, self._rl
            self._tap = self._src = self._rl = None
        if q is None:
            return
        if tap:
            try:
                q.CGEventTapEnable(tap, False)
            except Exception:  # noqa: BLE001
                pass
        if rl and src:
            try:
                q.CFRunLoopRemoveSource(rl, src, _hk._COMMON_MODES)
            except Exception:  # noqa: BLE001
                pass
        # 停掉专用线程的 runloop，否则该线程一直阻塞
        if rl:
            try:
                q.CFRunLoopStop(rl)
            except Exception:  # noqa: BLE001
                pass

    def _handler(self, proxy, etype, event, refcon):
        etype_u = etype & 0xFFFFFFFF
        if etype_u in (0xFFFFFFFE, 0xFFFFFFFF):  # tap 被禁用：自愈
            with self._lock:
                tap = self._tap
            if tap:
                try:
                    _hk._quartz.CGEventTapEnable(tap, True)
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
