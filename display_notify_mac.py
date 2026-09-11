"""显示器配置变化监听（macOS，基于 CoreGraphics reconfiguration callback）。

通过 CGDisplayRegisterReconfigurationCallback 监听显示器的增减/重排/镜像等
配置变化，仅在配置稳定（结束事件，非 begin）时回调用户函数。回调由系统在
注册线程的 runloop 上派发，因此需在独立线程内完成注册并运行 runloop。
"""
import ctypes
import ctypes.util
import threading


def _load_cg():
    path = ctypes.util.find_library("CoreGraphics")
    if not path:
        return None
    try:
        return ctypes.CDLL(path)
    except Exception:  # noqa: BLE001
        return None


_cg = _load_cg()
_HAS_CG = _cg is not None

# CGDisplayChangeSummaryFlags：配置刚开始时为 1，结束事件无此位
kCGDisplayBeginConfigurationFlag = 1 << 0

# void (*CGDisplayReconfigurationCallBack)(CGDirectDisplayID, CGDisplayChangeSummaryFlags, void*)
_CallbackType = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)

_cb_ref = None   # 保活回调对象，避免被 GC 回收
_rl = None       # 当前 runloop 句柄，供停止使用

if _HAS_CG:
    try:
        _cg.CGDisplayRegisterReconfigurationCallback.argtypes = [_CallbackType, ctypes.c_void_p]
        _cg.CGDisplayRegisterReconfigurationCallback.restype = ctypes.c_int32
        _cg.CGDisplayRemoveReconfigurationCallback.argtypes = [_CallbackType, ctypes.c_void_p]
        _cg.CGDisplayRemoveReconfigurationCallback.restype = ctypes.c_int32
        _cg.CFRunLoopGetCurrent.argtypes = []
        _cg.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        _cg.CFRunLoopRun.argtypes = []
        _cg.CFRunLoopRun.restype = None
        _cg.CFRunLoopStop.argtypes = [ctypes.c_void_p]
        _cg.CFRunLoopStop.restype = None
    except Exception:  # noqa: BLE001
        pass


def register(on_change):
    """注册显示器配置变化回调，返回取消注册的函数。

    on_change 在显示器配置稳定（结束事件）时被调用。无 CoreGraphics 或已注册
    时返回 None。回调可能来自系统后台线程，调用方需自行切回 UI 线程。
    """
    global _cb_ref, _rl
    if not _HAS_CG or _cb_ref is not None:
        return None

    def _cb(display, flags, user_data):
        if flags & kCGDisplayBeginConfigurationFlag:
            return  # 配置刚开始，忽略；等结束事件再通知
        try:
            on_change()
        except Exception:  # noqa: BLE001
            pass

    _cb_ref = _CallbackType(_cb)

    def _run():
        global _rl
        if _cg.CGDisplayRegisterReconfigurationCallback(_cb_ref, None) != 0:
            return
        _rl = _cg.CFRunLoopGetCurrent()
        _cg.CFRunLoopRun()

    threading.Thread(target=_run, daemon=True).start()

    def unregister():
        global _cb_ref, _rl
        if _cb_ref is not None:
            try:
                _cg.CGDisplayRemoveReconfigurationCallback(_cb_ref, None)
            except Exception:  # noqa: BLE001
                pass
            _cb_ref = None
        if _rl is not None:
            try:
                _cg.CFRunLoopStop(_rl)
            except Exception:  # noqa: BLE001
                pass
            _rl = None

    return unregister
