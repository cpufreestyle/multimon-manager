"""macOS 辅助功能（Accessibility）授权检测与请求。

多屏管理器需要辅助功能权限才能：
  - 监听全局快捷键（CGEventTap）
  - 通过 System Events 操控其它 App 的窗口（移动/并排/置顶）

首次未授权时，本模块可主动弹出系统授权对话框，并引导用户到设置开启。
零第三方依赖，仅用 ctypes 调用系统框架。
"""
import os
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


def responsible_binary():
    """返回 TCC 实际认账的那个可执行文件路径（未授权时要加进列表的就是它）。

    不能直接用 sys.executable：Homebrew 的 framework Python 是个小程序，跑起来
    会把自己重新 exec 进 Python.app，ps 里看到的是
    .../Versions/3.14/Resources/Python.app/Contents/MacOS/Python，而
    sys.executable 只是 .../opt/python@3.14/bin/python3.14 这个软链。macOS 的
    辅助功能授权按「真正负责的进程」记账，照 sys.executable 去加列表，用户会
    发现加了没反应——这正是「怎么授权都不生效」的常见原因。

    用 _NSGetExecutablePath 取本进程真实可执行路径；取不到时退回 sys.executable
    的 realpath。非 macOS 或都失败时返回 None。
    """
    if sys.platform != "darwin":
        return None
    try:
        # 注意：本模块顶层没有 import ctypes（原实现只 from ctypes import 具体
        # 名字），这里必须把 CDLL 一起导进来，否则 NameError 会被下面的 except
        # 吞掉、静默退回 sys.executable 的软链路径——那正是要避免的错路径。
        from ctypes import CDLL, byref, c_uint32, create_string_buffer

        libc = CDLL(None)
        size = c_uint32(0)
        # 第一遍只问需要多大缓冲区
        libc._NSGetExecutablePath(None, byref(size))
        buf = create_string_buffer(size.value)
        if libc._NSGetExecutablePath(buf, byref(size)) == 0:
            p = buf.value.decode("utf-8", "replace")
            if p:
                return p
    except Exception:  # noqa: BLE001
        pass
    try:
        p = os.path.realpath(sys.executable)
        return p if p and os.path.exists(p) else None
    except Exception:  # noqa: BLE001
        return None


def reveal_binary():
    """在 Finder 里显示出 responsible_binary() 指向的那个文件（选中状态）。

    辅助功能列表支持直接把文件拖进去——这是最省事的授权路径：点一下，Finder
    弹出并选中该二进制，用户拖进 系统设置 → 隐私与安全性 → 辅助功能 即可。
    比让用户在文件选择器里手敲一长串 Cellar 路径可靠得多。

    返回是否成功打开了 Finder。
    """
    if sys.platform != "darwin":
        return False
    p = responsible_binary()
    if not p or not os.path.exists(p):
        return False
    try:
        subprocess.run(["open", "-R", p], check=False, timeout=5)
        return True
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
