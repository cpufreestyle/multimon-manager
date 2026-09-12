"""macOS 辅助功能（Accessibility）授权检测与请求。

多屏管理器需要辅助功能权限才能：
  - 监听全局快捷键（CGEventTap）
  - 通过 System Events 操控其它 App 的窗口（移动/并排/置顶）

首次未授权时，本模块可主动弹出系统授权对话框，并引导用户到设置开启。
零第三方依赖，仅用 ctypes 调用系统框架。
"""
import subprocess
import sys


def is_trusted():
    """本进程是否已获得辅助功能授权。检测失败时保守返回 False。"""
    if sys.platform != "darwin":
        return True
    try:
        from ctypes import c_bool, cdll, util
        serv = cdll.LoadLibrary(util.find_library("ApplicationServices"))
        fn = serv.AXIsProcessTrusted
        fn.restype = c_bool
        fn.argtypes = []
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False


def request_trusted():
    """主动弹出系统授权对话框（"XXX 想控制这台电脑"）。

    仅在用户尚未授权时有意义：系统会引导其前往设置打开开关。
    注意：最终开关仍需用户在 系统设置 中手动开启，App 无法自动勾选。
    返回 True 表示调用成功（至于用户是否授权由系统决定）；异常时返回 False。
    """
    if sys.platform != "darwin":
        return False
    try:
        from ctypes import c_bool, c_char_p, c_int, c_void_p, addressof, cdll, util

        cf = cdll.LoadLibrary(util.find_library("CoreFoundation"))
        serv = cdll.LoadLibrary(util.find_library("ApplicationServices"))

        # 构造 options 字典：{ "AXTrustedCheckOptionPrompt": kCFBooleanTrue }
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_int]
        key = cf.CFStringCreateWithCString(
            None, b"AXTrustedCheckOptionPrompt", 0x08000100)  # kCFStringEncodingUTF8
        val = c_void_p.in_dll(cf, "kCFBooleanTrue")
        keys = (c_void_p * 1)(key)
        vals = (c_void_p * 1)(val)
        # callbacks 参数需要结构体的「地址」：in_dll(c_void_p) 读出的是首字段值，
        # 须用 addressof 取符号本身的地址再包成 void* 传入。
        key_cb = c_void_p(addressof(c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")))
        val_cb = c_void_p(addressof(c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
        cf.CFDictionaryCreate.restype = c_void_p
        cf.CFDictionaryCreate.argtypes = [
            c_void_p, c_void_p, c_void_p, c_int, c_void_p, c_void_p]
        opts = cf.CFDictionaryCreate(
            None, keys, vals, 1, key_cb, val_cb)

        fn = serv.AXIsProcessTrustedWithOptions
        fn.restype = c_bool
        fn.argtypes = [c_void_p]
        return bool(fn(opts))
    except Exception:  # noqa: BLE001
        return False


def open_settings():
    """打开 系统设置 → 隐私与安全性 → 辅助功能 页面。"""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["open",
             "x-apple.systempreferences:com.apple.preference.security"
             "?Privacy_Accessibility"],
            check=False, timeout=5,
        )
    except Exception:  # noqa: BLE001
        pass
