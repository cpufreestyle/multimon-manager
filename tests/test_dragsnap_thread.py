#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拖拽吸附监听器的线程模型单测（mock Quartz，不碰真机鼠标）。

背景：DragSnapWatcher 原来把 CGEventTap 的 runloop source 注册到 Tk 主线程
CFRunLoop（CFRunLoopGetMain），由 Tk mainloop 驱动。mouse dragged 事件极频繁，
每次都在 Tcl 释放 GIL 的窗口期回调进 Python，PyEval_RestoreThread 拿到 NULL
线程状态就直接 abort——真机现象是「界面无法显示 / 托盘按钮全部失效」。

判据：只禁掉拖拽吸附，同一份代码从 rc=134（必崩）变 rc=124（存活满）。
修法：tap 改建在专用线程自己的 CFRunLoop 上。

这里用假 Quartz 把两条不变量钉住，避免回归：
  1) 注册用的 runloop 必须是专用线程的，绝不能是主线程的；
  2) stop() 必须停掉那个 runloop 并清空字段，否则线程阻塞、重复启停失效。
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dragsnap_mac as ds  # noqa: E402


class _FakeRunLoop:
    """记录被注册/停止的 runloop。"""

    def __init__(self):
        self.added = []
        self.removed = []
        self.stopped = []
        self.ran = []
        self.lock = threading.Lock()

    def add(self, rl, src):
        with self.lock:
            self.added.append((rl, src))

    def remove(self, rl, src):
        with self.lock:
            self.removed.append((rl, src))

    def stop(self, rl):
        with self.lock:
            self.stopped.append(rl)

    def ran_run(self, rl):
        with self.lock:
            self.ran.append(rl)


class _FakeFn:
    """可写 restype/argtypes 的假 ctypes 函数。"""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *a, **k):
        return self._fn(*a, **k)


class _FakeQuartz:
    """只实现 DragSnapWatcher 用到的几个入口。"""

    def __init__(self, rl):
        self._rl = rl
        self._next_id = [1000]
        self.taps = []
        self.CGEventGetLocation = _FakeFn(self._loc)

    def _loc(self, event):
        return ds.CGPoint(1.0, 2.0)

    def _id(self):
        self._next_id[0] += 1
        return self._next_id[0]


    def CGEventTapCreate(self, *a, **k):
        tap = self._id()
        self.taps.append(tap)
        return tap

    def CFMachPortCreateRunLoopSource(self, *a, **k):
        return self._id()

    def CFRunLoopGetCurrent(self):
        # 关键：返回调用方线程标识，测试据此判断是否跑在专用线程
        return threading.get_ident()

    def CFRunLoopGetMain(self):
        return "MAIN-RUNLOOP"

    def CFRunLoopAddSource(self, rl, src, mode):
        self._rl.add(rl, src)

    def CFRunLoopRemoveSource(self, rl, src, mode):
        self._rl.remove(rl, src)

    def CFRunLoopStop(self, rl):
        self._rl.stop(rl)

    def CFRunLoopRun(self):
        self._rl.ran_run("RUN-ENTERED")


class TestDragSnapThreading(unittest.TestCase):
    def setUp(self):
        self._rl = _FakeRunLoop()
        self._q = _FakeQuartz(self._rl)
        self._orig_q = ds._hk._quartz
        self._orig_modes = ds._hk._COMMON_MODES
        ds._hk._quartz = self._q
        ds._hk._COMMON_MODES = "modes"

    def tearDown(self):
        ds._hk._quartz = self._orig_q
        ds._hk._COMMON_MODES = self._orig_modes

    def _watcher(self):
        w = ds.DragSnapWatcher()
        w.set_callback(lambda *a: None)
        return w

    def test_tap_registers_on_dedicated_thread_not_main(self):
        """source 必须注册到专用线程 runloop，不能是主线程的。"""
        w = self._watcher()
        self.assertTrue(w.start())
        try:
            added = self._rl.added
            self.assertTrue(added, "没有任何 source 被注册")
            self.assertTrue(self._rl.ran, "CFRunLoopRun 从未被调用")
            for rl, _src in added:
                self.assertNotEqual(
                    rl, "MAIN-RUNLOOP",
                    "source 注册到了主线程 runloop——这正是 abort 的根因")
        finally:
            w.stop()

    def test_tap_thread_is_separate_from_caller(self):
        """start() 必须在调用方线程之外另起线程建 tap。"""
        w = self._watcher()
        w.start()
        try:
            self.assertIsNotNone(w._tap_thread)
            self.assertNotEqual(w._tap_thread, threading.current_thread())
            self.assertTrue(w._running)
            self.assertIsNotNone(w._tap)
            self.assertIsNotNone(w._src)
            self.assertIsNotNone(w._rl)
        finally:
            w.stop()

    def test_stop_clears_state_and_stops_runloop(self):
        """stop() 必须停掉专用线程 runloop 并清空字段。"""
        w = self._watcher()
        w.start()
        rl = w._rl
        w.stop()
        self.assertFalse(w._running)
        self.assertIsNone(w._tap)
        self.assertIsNone(w._src)
        self.assertIsNone(w._rl)
        self.assertIn(rl, self._rl.stopped)

    def test_restart_after_stop_works(self):
        """启停之后能再次启动。"""
        w = self._watcher()
        self.assertTrue(w.start())
        w.stop()
        self.assertTrue(w.start())
        w.stop()
        self.assertEqual(len(self._rl.stopped), 2)

    def test_stop_is_idempotent(self):
        """重复 stop() 不能报错，也不能重复停 runloop。"""
        w = self._watcher()
        w.start()
        w.stop()
        w.stop()
        self.assertEqual(len(self._rl.stopped), 1)

    def test_handler_forwards_stages_and_coords(self):
        """_handler 只读坐标并转发，不做阻塞/UI 操作。"""
        w = self._watcher()
        seen = []
        w.set_callback(lambda stage, x, y: seen.append((stage, x, y)))
        w._handler(None, ds.kCGEventLeftMouseDown, "evt", None)
        w._handler(None, ds.kCGEventLeftMouseDragged, "evt", None)
        w._handler(None, ds.kCGEventLeftMouseUp, "evt", None)
        self.assertEqual([s[0] for s in seen], ["down", "drag", "up"])
        self.assertEqual(seen[1][1:], (1, 2))


if __name__ == "__main__":
    unittest.main()

