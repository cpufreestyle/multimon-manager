"""显示器配置变化监听（macOS，基于 CoreGraphics reconfiguration callback）。

通过 CGDisplayRegisterReconfigurationCallback 监听显示器的增减/重排/镜像等
配置变化，仅在配置稳定（结束事件，非 begin）时回调用户函数。

线程模型（重要）：回调由「注册线程」的 runloop 派发。因此必须在**主线程**
注册，由 Tk 的 mainloop 驱动派发。绝不能在后台线程注册并跑 CFRunLoopRun()，
否则两个 runloop 并存会触发 Fatal Python error: PyEval_RestoreThread
(GIL is released)。在主线程注册时 on_change 直接在主线程执行，可安全操作
tkinter，无需调用方再切线程。
"""
import ctypes
import ctypes.util


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

if _HAS_CG:
    try:
        _cg.CGDisplayRegisterReconfigurationCallback.argtypes = [_CallbackType, ctypes.c_void_p]
        _cg.CGDisplayRegisterReconfigurationCallback.restype = ctypes.c_int32
        _cg.CGDisplayRemoveReconfigurationCallback.argtypes = [_CallbackType, ctypes.c_void_p]
        _cg.CGDisplayRemoveReconfigurationCallback.restype = ctypes.c_int32
    except Exception:  # noqa: BLE001
        pass


def register(on_change):
    """注册显示器配置变化回调，返回取消注册的函数。

    on_change 在显示器配置稳定（结束事件）时被调用，且**在主线程执行**，
    可直接操作 tkinter。无 CoreGraphics 或已注册时返回 None。

    注意：必须在主线程调用本函数，回调才会被 Tk 的 mainloop 派发。
    """
    global _cb_ref
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
    # 在主线程注册 → 回调由主线程 runloop（Tk mainloop）派发，无需后台线程
    if _cg.CGDisplayRegisterReconfigurationCallback(_cb_ref, None) != 0:
        _cb_ref = None
        return None

    def unregister():
        global _cb_ref
        if _cb_ref is not None:
            try:
                _cg.CGDisplayRemoveReconfigurationCallback(_cb_ref, None)
            except Exception:  # noqa: BLE001
                pass
            _cb_ref = None

    return unregister
