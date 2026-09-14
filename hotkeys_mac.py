"""全局快捷键（macOS 实现，零第三方依赖）。

通过 ctypes 加载系统框架 ApplicationServices/Quartz 的 CGEventTap 实现
全局热键监听。这是 macOS 原生方案，无需任何第三方包（不依赖 pyobjc）。

需要辅助功能权限（Accessibility）：首次运行会提示用户在
系统设置 -> 隐私与安全性 -> 辅助功能 中授权运行本程序的 Python。

线程模型（重要）：事件 tap 的 runloop source 注册在「主线程」CFRunLoop 上，
由 Tk 的 mainloop 驱动，因此回调在主线程执行。切勿改成后台线程跑
CFRunLoopRun —— 与 Tk 的 CFRunLoop 并存会在 Python 3.13/3.14 触发
Fatal Python error: PyEval_RestoreThread: GIL is released。
真正耗时的动作（osascript）交给一个不触碰 tkinter 的普通工作线程执行。
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
# CGEventTapLocation: kCGHIDEventTap=0, kCGSessionEventTap=1
kCGSessionEventTap = 1
# CGEventTapPlacement: kCGHeadInsertEventTap=0, kCGTailAppendEventTap=1
kCGHeadInsertEventTap = 0
kCGEventKeyDown = 10
# 关键：CGEventTapCreate 的 eventsOfInterest 是「位掩码」而不是事件类型值。
# 正确写法是 CGEventMaskBit(kCGEventKeyDown) = 1 << 10 = 1024。
# 直接传 10 会被系统解释成 bit1|bit3（鼠标左/右键按下），键盘事件被全部过滤，
# 表现为「快捷键注册成功、tap 也建起来了，但永远不触发」。
kCGEventMaskKeyDown = 1 << kCGEventKeyDown
# CGEventTapOptions: kCGEventTapOptionDefault=0（可修改并继续传递事件）
kCGEventTapOptionDefault = 0
# tap 被系统禁用时回调会收到这两种类型：回调阻塞超时 / 被用户禁用。
# 必须在此重新启用，否则快捷键永久失效（表现为按几次后彻底无反应）。
kCGEventTapDisabledByTimeout = 0xFFFFFFFE
kCGEventTapDisabledByUser = 0xFFFFFFFF
kCGEventFlagMaskControl = 0x00040000
kCGEventFlagMaskAlternate = 0x00080000
kCGEventFlagMaskShift = 0x00020000
kCGEventFlagMaskCommand = 0x00100000

# CGEventField 字段号（实测扫描确认，切勿再改回 113/115 —— 那会导致
# CGEventGetIntegerValueField 恒返回 0，键码读不出来，快捷键永远不触发）
kCGKeyboardEventKeycode = 9
kCGEventFlags = 59

MOD_ALT = 1
MOD_CONTROL = 2
MOD_SHIFT = 4
MOD_WIN = 8

VK_LEFT = 123
VK_RIGHT = 124
VK_UP = 126
VK_DOWN = 125
# Z 键（撤销），macOS CGKeycode
VK_Z = 6

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
    # 修饰键优先用专用 API CGEventGetFlags，比读字段号更可靠
    _quartz.CGEventGetFlags.restype = ctypes.c_uint64
    _quartz.CGEventGetFlags.argtypes = [ctypes.c_void_p]
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
        _quartz.CFRunLoopGetMain.restype = ctypes.c_void_p
        _quartz.CFRunLoopGetMain.argtypes = []
        _quartz.CFRunLoopRemoveSource.restype = None
        _quartz.CFRunLoopRemoveSource.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        _quartz.CFRunLoopRunInMode.restype = ctypes.c_int
        _quartz.CFRunLoopRunInMode.argtypes = [
            ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
    except Exception:  # noqa: BLE001
        pass


def _load_runloop_mode():
    """取 kCFRunLoopCommonModes（CFStringRef 指针）。

    Quartz/ApplicationServices 会 re-export CoreFoundation 的符号，用 in_dll
    直接读指针值；读不到则退回 kCFRunLoopDefaultMode。
    """
    if not _HAS_QUARTZ:
        return None
    for name in ("kCFRunLoopCommonModes", "kCFRunLoopDefaultMode"):
        try:
            return ctypes.c_void_p.in_dll(_quartz, name)
        except Exception:  # noqa: BLE001
            continue
    return None


# runloop source 必须注册到某个 mode 才会被派发
_COMMON_MODES = _load_runloop_mode()


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
        self._running = False
        self._tap = None
        self._rl = None
        self._src = None
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
        # 关键：事件 tap 挂到「主线程」CFRunLoop，由 Tk 的 mainloop 驱动。
        # 绝不能再开后台线程跑 CFRunLoopRun —— 两个 CFRunLoop（Tk 的在
        # 主线程、我们的在后台线程）同时跑会打架，在 Python 3.13/3.14 上会
        # 触发 Fatal Python error: PyEval_RestoreThread: GIL is released。
        if not self._setup_on_main_runloop():
            return
        # 耗时动作（osascript）仍交给普通工作线程，但它只是普通 Python 线程，
        # 不跑 CFRunLoop、不触碰 tkinter，是安全的。
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

    def _setup_on_main_runloop(self):
        """在主线程创建事件 tap，并把 source 加到主线程 CFRunLoop。"""
        self._cb = CGEventTapCallBack(self._handler)
        self._tap = _quartz.CGEventTapCreate(
            kCGSessionEventTap, kCGHeadInsertEventTap,
            kCGEventTapOptionDefault, kCGEventMaskKeyDown,
            self._cb, None,
        )
        if not self._tap:
            print("[hotkeys_mac] 创建事件监听失败（需要辅助功能权限）")
            self._running = False
            return False
        self._src = _quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        if not self._src:
            print("[hotkeys_mac] 创建 runloop source 失败")
            self._running = False
            self._tap = None
            return False
        self._rl = _quartz.CFRunLoopGetMain()
        _quartz.CFRunLoopAddSource(self._rl, self._src, _COMMON_MODES)
        return True

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
        flags = self._event_flags(event)
        for _hid, (mod, vk, cb) in self.hotkeys.items():
            if vk == code and self._flags_match(flags, mod):
                self._queue.put(cb)  # 只入队，立即返回
                # 已匹配并消费该按键：返回 None 阻止事件继续传递给前台 App，
                # 否则前台程序也会收到同样的 ⌘⌥+数字 组合（"双重触发"）。
                return None
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

    def _event_flags(self, event):
        """读事件的修饰键状态：优先用 CGEventGetFlags，失败则回退字段号。"""
        try:
            return int(_quartz.CGEventGetFlags(event))
        except Exception:  # noqa: BLE001
            pass
        try:
            return int(_quartz.CGEventGetIntegerValueField(event, kCGEventFlags))
        except Exception:  # noqa: BLE001
            return 0

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
        # 从主线程 runloop 摘掉 source，否则停止后仍会收到按键
        if self._rl and self._src:
            try:
                _quartz.CFRunLoopRemoveSource(self._rl, self._src, _COMMON_MODES)
            except Exception:  # noqa: BLE001
                pass
        # 唤醒工作线程退出（否则它会一直阻塞在 get）
        try:
            self._queue.put(None)
        except Exception:  # noqa: BLE001
            pass
        self._tap = None
        self._rl = None
        self._src = None


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
            if _HAS_QUARTZ and _COMMON_MODES:
                # tap 挂在主线程 runloop；命令行 demo 没有 Tk 驱动它，
                # 这里手动泵一下（0.3s 超时，保证 Ctrl+C 能响应）
                _quartz.CFRunLoopRunInMode(_COMMON_MODES, 0.3, False)
            else:
                time.sleep(0.3)
    except KeyboardInterrupt:
        hk.stop()
