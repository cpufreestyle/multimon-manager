#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排布前统一退出全屏：_prepare_for_layout 的契约。

真机实测：ChatGPT 全屏时 `set position/size` 返回成功，几何一步不变
（1600x870 原地不动）。四格路径早就逐窗退出全屏，并排/竖排/单窗吸附/
网格以前没有——表现就是「点了没反应」。这个函数是那个统一收口点。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402


class PrepareForLayoutTest(unittest.TestCase):
    def setUp(self):
        self._saved = (wm._exit_fullscreen_for_layout, wm._fresh_rect,
                       wm._last_rect)
        self._orig_rect = dict(wm._last_rect)
        self.calls = []

    def tearDown(self):
        wm._exit_fullscreen_for_layout, wm._fresh_rect = self._saved[:2]
        wm._last_rect.clear()
        wm._last_rect.update(self._orig_rect)

    def test_refreshes_geometry_after_exit(self):
        wm._exit_fullscreen_for_layout = lambda h, notes: (
            self.calls.append(h) or h == "A::1")
        wm._fresh_rect = lambda h: (10, 20, 800, 600)
        wm._last_rect.pop("A::1", None)
        wm._prepare_for_layout(["A::1", "B::1"])
        self.assertEqual(self.calls, ["A::1", "B::1"])
        self.assertEqual(wm._last_rect.get("A::1"), (10, 20, 800, 600),
                         "退出全屏后必须用新几何刷新缓存")

    def test_no_entry_when_not_fullscreen(self):
        wm._exit_fullscreen_for_layout = lambda h, notes: False
        wm._fresh_rect = lambda h: (1, 2, 3, 4)
        wm._last_rect.pop("A::1", None)
        wm._prepare_for_layout(["A::1"])
        self.assertNotIn("A::1", wm._last_rect)

    def test_bad_geometry_is_ignored(self):
        wm._exit_fullscreen_for_layout = lambda h, notes: True
        wm._fresh_rect = lambda h: (0, 0, 0, 0)
        wm._last_rect.pop("A::1", None)
        wm._prepare_for_layout(["A::1"])
        self.assertNotIn("A::1", wm._last_rect)

    def test_exception_does_not_propagate(self):
        def boom(h, notes):
            raise RuntimeError("ax")
        wm._exit_fullscreen_for_layout = boom
        self.assertEqual(wm._prepare_for_layout(["A::1"]), {})

    def test_empty_input(self):
        wm._exit_fullscreen_for_layout = lambda h, notes: True
        self.assertEqual(wm._prepare_for_layout([]), {})
        self.assertEqual(wm._prepare_for_layout(None), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
