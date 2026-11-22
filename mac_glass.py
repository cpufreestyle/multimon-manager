# -*- coding: utf-8 -*-
"""macOS 原生毛玻璃增强。

模块只使用系统 Objective-C runtime，不依赖 PyObjC。原生视图只放在 Tk
内容视图底层，初始化失败时自动回退到普通界面。
"""
import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger(__name__)
IS_MAC = sys.platform == "darwin"


class _ObjC:
    """Objective-C 消息发送的最小 ctypes 封装。"""

    def __init__(self):
        objc_path = ctypes.util.find_library("objc")
        if not objc_path:
            raise RuntimeError("libobjc 不可用")
        self.lib = ctypes.cdll.LoadLibrary(objc_path)
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.lib.objc_getClass.argtypes = [ctypes.c_char_p]
        self.lib.sel_registerName.restype = ctypes.c_void_p
        self.lib.sel_registerName.argtypes = [ctypes.c_char_p]
        self.lib.objc_msgSend.restype = ctypes.c_void_p
        self.lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    def cls(self, name):
        value = self.lib.objc_getClass(name.encode("utf-8"))
        if not value:
            raise RuntimeError("Objective-C class 不可用: %s" % name)
        return value

    def sel(self, name):
        value = self.lib.sel_registerName(name.encode("utf-8"))
        if not value:
            raise RuntimeError("Objective-C selector 不可用: %s" % name)
        return value

    def msg(self, target, name, restype, argtypes=(), *args):
        if not target:
            return None
        self.lib.objc_msgSend.restype = restype
        self.lib.objc_msgSend.argtypes = (
            [ctypes.c_void_p, ctypes.c_void_p] + list(argtypes))
        return self.lib.objc_msgSend(target, self.sel(name), *args)


def _window_for_title(objc, title):
    pointer = ctypes.c_void_p
    app = objc.msg(objc.cls("NSApplication"), "sharedApplication", pointer)
    windows = objc.msg(app, "windows", pointer)
    count = objc.msg(windows, "count", ctypes.c_ulong, ()) or 0
    for index in range(int(count)):
        window = objc.msg(windows, "objectAtIndex:", pointer,
                          (ctypes.c_ulong,), index)
        title_obj = objc.msg(window, "title", pointer)
        title_ptr = objc.msg(title_obj, "UTF8String", ctypes.c_char_p)
        current = title_ptr.decode("utf-8", "replace") if title_ptr else ""
        if current == title:
            return window
    return None


def apply(root, enabled=True):
    """给 Tk 主窗口添加系统毛玻璃底层，返回是否成功。"""
    if not IS_MAC or not enabled:
        return False
    if getattr(root, "_mmm_glass_view", None):
        return True
    try:
        root.update_idletasks()
        objc = _ObjC()
        window = _window_for_title(objc, root.title())
        if not window:
            logger.info("玻璃效果跳过：找不到 Tk 主窗口")
            return False

        pointer = ctypes.c_void_p
        content = objc.msg(window, "contentView", pointer)
        bounds = objc.msg(content, "bounds", pointer)
        if not content or not bounds:
            return False

        clear = objc.msg(
            objc.msg(objc.cls("NSColor"), "clearColor", pointer),
            "colorWithAlphaComponent:", pointer, (ctypes.c_double,), 0.0)
        objc.msg(window, "setOpaque:", None, (ctypes.c_bool,), False)
        objc.msg(window, "setBackgroundColor:", None, (pointer,), clear)
        objc.msg(window, "setTitlebarAppearsTransparent:", None,
                 (ctypes.c_bool,), True)
        objc.msg(window, "setTitleVisibility:", None, (ctypes.c_int,), 0)

        effect = objc.msg(objc.cls("NSVisualEffectView"), "alloc", pointer)
        effect = objc.msg(effect, "initWithFrame:", pointer, (pointer,), bounds)
        if not effect:
            return False
        # 21 = NSVisualEffectMaterialUnderWindowBackground。
        objc.msg(effect, "setMaterial:", None, (ctypes.c_int,), 21)
        objc.msg(effect, "setBlendingMode:", None, (ctypes.c_int,), 0)
        objc.msg(effect, "setState:", None, (ctypes.c_int,), 0)
        # NSViewWidthSizable | NSViewHeightSizable。
        objc.msg(effect, "setAutoresizingMask:", None, (ctypes.c_ulong,), 18)
        # NSWindowBelow，玻璃视图永远在 Tk 控件下面，不拦截点击。
        objc.msg(content, "addSubview:positioned:relativeTo:", None,
                 (pointer, ctypes.c_int, pointer), effect, -1, None)

        root._mmm_glass_view = effect
        root._mmm_glass_window = window
        logger.info("macOS 毛玻璃已启用")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("macOS 毛玻璃不可用，回退普通界面: %s", exc)
        return False
