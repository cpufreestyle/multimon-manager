#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拖拽吸附监听器的线程模型单测（Windows：WH_MOUSE_LL，mock user32/kernel32）。

两条不变量决定这个写法，回归会以很难查的形式出现：
  1) 低级鼠标钩子有系统超时（默认 300ms），回调里做任何阻塞/UI 操作都会被
     系统直接摘掉钩子——现象是「预览时有时无」，所以回调必须只转交事件；
  2) WH_MOUSE_LL 的回调跑在安装它的线程上，那条线程必须有消息循环。钩子
     装在专用线程上，界面主循环（Tk）才不会被鼠标事件淹没。

这里用假 user32/kernel32 把这两条钉住，并验证事件映射与 stop 的拆桥顺序。
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dragsnap as ds  # noqa: E402


class _FakeFn:
    """可写 restype/argtypes 的假 ctypes 函数。"""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *a, **k):
        return self._fn(*a, **k)


class _FakeUser32:
    def __init__(self):
        self.installed = []          # (hook_type, thread_id)
        self.unhooked = []
        self.quit_posts = []         # (thread_id, msg)
        self.next_hook = 0x1234
        self.install_thread = None
        self.lock = threading.Lock()
        self.SetWindowsHookExW = _FakeFn(self._install)
        self.UnhookWindowsHookEx = _FakeFn(self._unhook)
        self.CallNextHookEx = _FakeFn(lambda *a: 0)
        self.PostThreadMessageW = _FakeFn(self._post)
        self.TranslateMessage = _FakeFn(lambda *a: True)
        self.DispatchMessageW = _FakeFn(lambda *a: 0)
        # GetMessageW 默认立刻返回 WM_QUIT，让线程自然退出
        self.GetMessageW = _FakeFn(lambda *a: 0)

    def _install(self, hook_type, proc, hmod, tid):
        with self.lock:
            self.install_thread = threading.get_ident()
            self.installed.append((hook_type, tid))
        return self.next_hook

    def _unhook(self, hook):
        with self.lock:
            self.unhooked.append(hook)
        return True

    def _post(self, tid, msg, w, l):
        with self.lock:
            self.quit_posts.append((tid, msg))
        return True


class _FakeMSLL:
    """假 MSLLHOOKSTRUCT 内容：只有 pt.x / pt.y 被用到。"""

    class _Pt:
        def __init__(self, x, y):
            self.x, self.y = x, y

    def __init__(self, x, y):
        self.pt = self._Pt(x, y)


class _FakePointer:
    """假 ctypes 指针：cast() 的返回值，实现里取它的 .contents。"""

    def __init__(self, contents):
        self.contents = contents


@unittest.skipUnless(ds.user32 is not None,
                     "dragsnap 的 Windows 分支仅 Windows 可测")
class DragSnapWindowsTest(unittest.TestCase):
    def setUp(self):
        self._u = ds.user32
        self.fake_u = _FakeUser32()
        ds.user32 = self.fake_u
        # cast 出来的结构换成可控的假对象
        self._cast = ds.ctypes.cast
        self._contents = _FakeMSLL(11, 22)
        ds.ctypes.cast = lambda _lp, _t: _FakePointer(self._contents)

    def tearDown(self):
        ds.user32 = self._u
        ds.ctypes.cast = self._cast

    def _watcher(self):
        w = ds.DragSnapWatcher()
        self.events = []
        w.set_callback(lambda stage, x, y: self.events.append((stage, x, y)))
        return w

    def test_hook_installed_on_dedicated_thread(self):
        w = self._watcher()
        self.assertTrue(w.start())
        try:
            self.assertEqual(len(self.fake_u.installed), 1)
            hook_type, tid = self.fake_u.installed[0]
            self.assertEqual(hook_type, ds.WH_MOUSE_LL)
            # dwThreadId=0 才是全局钩子
            self.assertEqual(tid, 0)
            self.assertEqual(self.fake_u.install_thread, w._thread.ident)
            self.assertNotEqual(self.fake_u.install_thread,
                                threading.current_thread().ident)
        finally:
            w.stop()

    def test_callback_forwards_stages_and_coords(self):
        w = self._watcher()
        self.assertTrue(w.start())
        try:
            proc = w._hook_ref
            self.assertIsNotNone(proc)
            self._contents = _FakeMSLL(320, 240)
            proc(ds.HC_ACTION, ds.WM_LBUTTONDOWN, 0x7FE1)
            proc(ds.HC_ACTION, ds.WM_MOUSEMOVE, 0x7FE2)
            proc(ds.HC_ACTION, ds.WM_LBUTTONUP, 0x7FE3)
            proc(ds.HC_ACTION, 0x0204, 0x7FE4)     # 右键按下：不该转发
            proc(5, ds.WM_MOUSEMOVE, 0x7FE5)        # 非 HC_ACTION：不该转发
        finally:
            w.stop()
        self.assertEqual([s for s, _x, _y in self.events],
                         ["down", "drag", "up"])
        self.assertEqual(self.events[1][1:], (320, 240))

    def test_callback_swallows_exceptions(self):
        """回调里抛异常绝不能再抛回系统——那会让钩子被摘掉。"""
        w = ds.DragSnapWatcher()
        w.set_callback(lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertTrue(w.start())
        try:
            proc = w._hook_ref
            self._contents = _FakeMSLL(1, 2)
            proc(ds.HC_ACTION, ds.WM_MOUSEMOVE, 0x7FE6)
        finally:
            w.stop()
        self.assertTrue(self.fake_u.unhooked)

    def test_stop_posts_quit_and_unhooks(self):
        w = self._watcher()
        self.assertTrue(w.start())
        t, tid = w._thread, w._thread.ident
        w.stop()
        self.assertEqual([(t, m) for t, m in self.fake_u.quit_posts],
                         [(tid, ds.WM_QUIT)])
        self.assertEqual(self.fake_u.unhooked, [self.fake_u.next_hook])
        self.assertFalse(t.is_alive())
        self.assertIsNone(w._hook)

    def test_stop_is_idempotent(self):
        w = self._watcher()
        self.assertTrue(w.start())
        w.stop()
        w.stop()
        self.assertEqual(len(self.fake_u.unhooked), 1)
        self.assertEqual(len(self.fake_u.quit_posts), 1)

    def test_restart_after_stop_works(self):
        w = self._watcher()
        self.assertTrue(w.start())
        w.stop()
        self.assertTrue(w.start())
        w.stop()
        self.assertEqual(len(self.fake_u.installed), 2)

    def test_start_fails_when_hook_cannot_be_installed(self):
        self.fake_u.next_hook = 0        # SetWindowsHookExW 返回空
        w = self._watcher()
        self.assertFalse(w.start())
        t = w._thread
        t.join(1.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.fake_u.unhooked, [])

    def test_no_windll_means_unavailable(self):
        saved = ds.user32
        ds.user32 = None
        try:
            w = self._watcher()
            self.assertFalse(w.start())
            w.stop()
        finally:
            ds.user32 = saved


if __name__ == "__main__":
    unittest.main()
