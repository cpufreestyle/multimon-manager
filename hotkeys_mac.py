"""全局快捷键（macOS 实现，零第三方依赖）。

通过 ctypes 加载系统框架 ApplicationServices/Quartz 的 CGEventTap 实现
全局热键监听。这是 macOS 原生方案，无需任何第三方包（不依赖 pyobjc）。

需要辅助功能权限（Accessibility）：首次运行会提示用户在
系统设置 -> 隐私与安全性 -> 辅助功能 中授权运行本程序的 Python。
"""
import ctypes
import ctypes.util
import threading

# ---- 加载系统框架 ----
_quartz = None
for _fw in ("Quartz", "ApplicationServices"):
    _path = ctypes.util.find_library(_fw)
    if _path:
        try:
            _quartz = ctypes.CDLL(_path)
            break
        except Exception:  # noqa: BLE001
            _quartz = None

_HAS_QUARTZ = _quartz is not None

# ---- 常量（来自 Carbon/HIToolbox 与 Quartz）----
kCGSessionEventTap = 0
kCGHeadInsertEventTap = 1
kCGEventKeyDown = 10
kCGEventFlagMaskControl = 0x00040000
kCGEventFlagMaskAlternate = 0x00080000
kCGEventFlagMaskShift = 0x00020000
kCGEventFlagMaskCommand = 0x00100000

# 字段号
kCGKeyboardEventKeycode = 113
kCGEventFlags = 115

MOD_ALT = 1
MOD_CONTROL = 2
MOD_SHIFT = 4
MOD_WIN = 8

VK_LEFT = 123
VK_RIGHT = 124
VK_UP = 126
VK_DOWN = 125

# ctypes 函数签名
if _HAS_QUARTZ:
    _quartz.CGEventTapCreate.restype = ctypes.c_void_p
    _quartz.CGEventTapCreate.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    _quartz.CGEventGetIntegerValueField.restype = ctypes.c_longlong
    _quartz.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_int]
    try:
        _quartz.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
        _quartz.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        _quartz.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        _quartz.CFRunLoopGetCurrent.argtypes = []
        _quartz.CFRunLoopAddSource.restype = None
        _quartz.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        _quartz.CFRunLoopRun.restype = None
        _quartz.CFRunLoopRun.argtypes = []
        _quartz.CFRunLoopStop.restype = None
        _quartz.CFRunLoopStop.argtypes = [ctypes.c_void_p]
    except Exception:  # noqa: BLE001
        pass


CGEventTapCallBack = ctypes.CFUNCTYPE(
    ctypes.c_void_p,
    ctypes.c_void_p,  # proxy
    ctypes.c_int,     # type
    ctypes.c_void_p,  # event
    ctypes.c_void_p,  # refcon
)


class HotkeyManager:
    def __init__(self):
        self.hotkeys = {}
        self.thread = None
        self._running = False
        self._tap = None
        self._rl = None
        self._cb = None

    def register(self, modifiers, vk, callback):
        hid = 1 + len(self.hotkeys)
        self.hotkeys[hid] = (modifiers, vk, callback)
        return hid

    def start(self):
        if self._running or not self.hotkeys or not _HAS_QUARTZ:
            if not _HAS_QUARTZ:
                print("[hotkeys_mac] 未找到 Quartz 框架，全局快捷键不可用")
            return
        self._running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self._cb = CGEventTapCallBack(self._handler)
        self._tap = _quartz.CGEventTapCreate(
            kCGSessionEventTap, kCGHeadInsertEventTap, 0, kCGEventKeyDown,
            self._cb, None,
        )
        if not self._tap:
            print("[hotkeys_mac] 创建事件监听失败（需要辅助功能权限）")
            self._running = False
            return
        rl = _quartz.CFRunLoopGetCurrent()
        self._rl = rl
        source = _quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        _quartz.CFRunLoopAddSource(rl, source, None)
        _quartz.CFRunLoopRun()

    def _handler(self, proxy, etype, event, refcon):
        if etype != kCGEventKeyDown:
            return event
        code = _quartz.CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
        flags = _quartz.CGEventGetIntegerValueField(event, kCGEventFlags)
        for _hid, (mod, vk, cb) in self.hotkeys.items():
            if vk == code and self._flags_match(flags, mod):
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    pass
        return event

    def _flags_match(self, flags, mod):
        want = 0
        if mod & MOD_CONTROL:
            want |= kCGEventFlagMaskControl
        if mod & MOD_ALT:
            want |= kCGEventFlagMaskAlternate
        if mod & MOD_SHIFT:
            want |= kCGEventFlagMaskShift
        if mod & MOD_WIN:
            want |= kCGEventFlagMaskCommand
        return (flags & want) == want

    def stop(self):
        self._running = False
        if self._rl:
            try:
                _quartz.CFRunLoopStop(self._rl)
            except Exception:  # noqa: BLE001
                pass
        self._tap = None
        self._rl = None


if __name__ == "__main__":
    import time
    import windows_mac as windows

    hk = HotkeyManager()
    hk.register(MOD_CONTROL | MOD_ALT, VK_RIGHT, lambda: windows.move_active_to_next_monitor(1))
    hk.register(MOD_CONTROL | MOD_ALT, VK_LEFT, lambda: windows.move_active_to_next_monitor(-1))
    hk.start()
    print("Mac 快捷键已启动，Ctrl+Alt+←/→。按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        hk.stop()
