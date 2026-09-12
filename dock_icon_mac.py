"""在 macOS 上设置应用（Dock）图标。

Tk 应用以 `python3 main.py` 方式运行时，Dock 显示的是 Python 解释器的图标；
这里用 ctypes 调 AppKit 的 `NSApplication.setApplicationIconImage:` 把图标换成
本程序自己的图标。纯标准库，无第三方依赖。
"""
import ctypes
import ctypes.util

_objc = None
_failed = False


def _load():
    """加载 objc 运行时并声明 msgSend 所需签名。"""
    global _objc, _failed
    if _objc is not None or _failed:
        return _objc
    try:
        _objc = ctypes.CDLL(ctypes.util.find_library("objc"))
        _objc.objc_getClass.restype = ctypes.c_void_p
        _objc.objc_getClass.argtypes = [ctypes.c_char_p]
        _objc.sel_registerName.restype = ctypes.c_void_p
        _objc.sel_registerName.argtypes = [ctypes.c_char_p]
        _objc.objc_msgSend.restype = ctypes.c_void_p
    except Exception:  # noqa: BLE001
        _failed = True
        _objc = None
    return _objc


def set_dock_icon(path):
    """把 Dock / 应用图标设为 path（PNG）。成功返回 True，失败返回 False。"""
    objc = _load()
    if objc is None or not path:
        return False
    try:
        def cls(name):
            return objc.objc_getClass(name)

        def sel(name):
            return objc.sel_registerName(name)

        # 无参数消息：sharedApplication / alloc
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        app = objc.objc_msgSend(cls(b"NSApplication"), sel(b"sharedApplication"))
        if not app:
            return False
        img = objc.objc_msgSend(cls(b"NSImage"), sel(b"alloc"))
        if not img:
            return False
        # initWithContentsOfFile:（1 个 const char*）
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
        img = objc.objc_msgSend(img, sel(b"initWithContentsOfFile:"),
                                path.encode("utf-8"))
        if not img:
            return False
        # setApplicationIconImage:（1 个对象指针）
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        objc.objc_msgSend(app, sel(b"setApplicationIconImage:"), img)
        return True
    except Exception:  # noqa: BLE001
        return False
