"""全局快捷键（macOS 实现，零第三方依赖）。

通过 ctypes 加载系统框架 ApplicationServices/Quartz 的 CGEventTap 实现
全局热键监听。这是 macOS 原生方案，无需任何第三方包（不依赖 pyobjc）。

需要辅助功能权限（Accessibility）：首次运行会提示用户在
系统设置 -> 隐私与安全性 -> 辅助功能 中授权运行本程序的 Python。
"""
import ctypes
import ctypes.util
import queue
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
# tap 被系统禁用时回调会收到这两种类型：回调阻塞超时 / 被用户禁用。
# 必须在此重新启用，否则快捷键永久失效（表现为按几次后彻底无反应）。
kCGEventTapDisabledByTimeout = 0xFFFFFFFE
kCGEventTapDisabledByUser = 0xFFFFFFFF
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

# 数字键 1-9 的 macOS 虚拟键码（CGKeycode）。注意它与 ASCII 不同：
# 例如 "1" 的 CGKeycode 是 18 而非 ord("1")=49，用 ord() 注册会导致快捷键
# 永远匹配不上。UI 统一通过 backend.DIGIT_KEYS 取用。
DIGIT_KEYS = {
    0: 29, 1: 18, 2: 19, 3: 20, 4: 21, 5: 23, 6: 22, 7: 26, 8: 28, 9: 25,
}

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
        _quartz.CGEventTapEnable.restype = None
        _quartz.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
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
        # 事件回调只入队，实际动作交给工作线程执行（原因见 _handler 注释）
        self._queue = queue.Queue()
        self._worker_thread = None

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
        self._queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

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
        """事件回调：必须尽快返回，绝不能在此执行耗时动作。

        macOS 对事件 tap 有超时保护。若回调长时间不返回（例如在此直接跑
        osascript —— 一次分屏要启动 2 个进程、耗时数百毫秒），系统会自动
        禁用该 tap，之后只收到 kCGEventTapDisabledByTimeout；若不重新启用，
        快捷键将永久失效。因此这里只做两件事：自愈 tap、把动作丢进队列。
        """
        # etype 声明为 c_int，无符号常量会以负数形式传入，统一按 32 位比较
        etype_u = etype & 0xFFFFFFFF
        if etype_u in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUser):
            if self._tap:
                try:
                    _quartz.CGEventTapEnable(self._tap, True)
                except Exception:  # noqa: BLE001
                    pass
            return event

        if etype != kCGEventKeyDown:
            return event
        code = _quartz.CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
        flags = _quartz.CGEventGetIntegerValueField(event, kCGEventFlags)
        for _hid, (mod, vk, cb) in self.hotkeys.items():
            if vk == code and self._flags_match(flags, mod):
                self._queue.put(cb)  # 只入队，立即返回
                break  # 一个按键只触发一个动作
        return event

    def _worker(self):
        """工作线程：真正执行快捷键动作（osascript 等耗时操作）。"""
        while True:
            cb = self._queue.get()
            if cb is None:  # 停止信号
                break
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass

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
        # 先禁用 tap 再停 runloop，否则系统侧监听会残留，反复启用会累积
        if self._tap:
            try:
                _quartz.CGEventTapEnable(self._tap, False)
            except Exception:  # noqa: BLE001
                pass
        if self._rl:
            try:
                _quartz.CFRunLoopStop(self._rl)
            except Exception:  # noqa: BLE001
                pass
        # 唤醒工作线程退出（否则它会一直阻塞在 get）
        try:
            self._queue.put(None)
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
