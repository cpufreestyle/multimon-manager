#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动探测最小尺寸：probe_min_sizes 与界面取窗口列表的逻辑。

容量提示（并排/竖排/网格）都依赖 `_QUAD_MIN_CACHE`，而它原来只在四格/网格
排布时写入。`probe_min_sizes` 给界面一个显式入口：缩窗口→读回→复位→写盘，
失败/已关闭都要如实计数，不能静默当成 0。
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402

try:
    import ui  # noqa: E402
    import backend as b  # noqa: E402
except Exception:  # noqa: BLE001
    ui = None
    b = None


WIN = {"hwnd": "A::1", "owner": "A", "name": "1",
       "x": 100, "y": 120, "w": 700, "h": 500, "pid": 1}


class ProbeMinSizesTest(unittest.TestCase):
    def setUp(self):
        wm._QUAD_MIN_CACHE.clear()
        self._saved = (wm._find_window, wm._readback_rect, wm._probe_min_size,
                       wm.set_window_rect, wm._quad_cache_save,
                       wm._exit_fullscreen_for_layout, wm._ax_is_fullscreen)
        self.writes = []
        self.saved_cache = 0
        self.probe_calls = []

        wm._find_window = lambda h: dict(WIN) if h == "A::1" else None
        wm._readback_rect = lambda h, target=None: (100, 120, 700, 500)
        wm.set_window_rect = lambda h, x, y, w, hh, activate=True: (
            self.writes.append((h, x, y, w, hh)))
        wm._quad_cache_save = lambda: setattr(
            self, "saved_cache", self.saved_cache + 1)
        wm._exit_fullscreen_for_layout = lambda h, notes: False
        wm._ax_is_fullscreen = lambda h: False

    def tearDown(self):
        (wm._find_window, wm._readback_rect, wm._probe_min_size,
         wm.set_window_rect, wm._quad_cache_save,
         wm._exit_fullscreen_for_layout, wm._ax_is_fullscreen) = self._saved
        wm._QUAD_MIN_CACHE.clear()

    def test_probe_caches_size_and_restores_geometry(self):
        wm._probe_min_size = lambda h, x, y: (480, 600)
        res = wm.probe_min_sizes(["A::1"])
        self.assertEqual(res["probed"], 1)
        self.assertEqual(res["items"], [("A::1", (480, 600))])
        self.assertEqual(wm._QUAD_MIN_CACHE["A::1"], (480, 600))
        self.assertEqual(self.writes, [("A::1", 100, 120, 700, 500)],
                         "探测完必须放回原几何")
        self.assertEqual(self.saved_cache, 1, "探测成功要落盘")

    def test_missing_window_counted_not_probed(self):
        called = []
        wm._probe_min_size = lambda h, x, y: called.append(h) or (1, 1)
        res = wm.probe_min_sizes(["B::2"])
        self.assertEqual(res["missing"], 1)
        self.assertEqual(res["probed"], 0)
        self.assertEqual(called, [])

    def test_failed_probe_not_cached_but_still_restored(self):
        wm._probe_min_size = lambda h, x, y: (0, 0)
        res = wm.probe_min_sizes(["A::1"])
        self.assertEqual(res["failed"], 1)
        self.assertNotIn("A::1", wm._QUAD_MIN_CACHE)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.saved_cache, 0)

    def test_exception_in_probe_counts_as_failed(self):
        def boom(h, x, y):
            raise RuntimeError("ax")
        wm._probe_min_size = boom
        res = wm.probe_min_sizes(["A::1"])
        self.assertEqual((res["probed"], res["failed"]), (0, 1))
        self.assertEqual(len(self.writes), 1)

    def test_empty_input(self):
        wm._probe_min_size = lambda h, x, y: (1, 1)
        res = wm.probe_min_sizes([])
        self.assertEqual(res, {"probed": 0, "failed": 0, "missing": 0,
                               "items": []})

    def test_fullscreen_window_is_not_cached(self):
        """全屏时尺寸写入被忽略，探到的只是全屏尺寸——绝不能当最小尺寸缓存。"""
        wm._ax_is_fullscreen = lambda h: True
        called = []
        wm._probe_min_size = lambda h, x, y: called.append(h) or (1600, 870)
        res = wm.probe_min_sizes(["A::1"])
        self.assertEqual((res["probed"], res["failed"]), (0, 1))
        self.assertNotIn("A::1", wm._QUAD_MIN_CACHE)
        self.assertEqual(called, [], "全屏窗口不该进入探测")


@unittest.skipIf(ui is None, "tkinter/ui 不可导入")
class ProbeTargetsTest(unittest.TestCase):
    """界面上「点名过谁就测谁」：四格优先、去重、都没点名取最前 4 个。"""

    class _Var:
        def __init__(self, v=""):
            self.v = v

        def get(self):
            return self.v

    def _fake(self, **kw):
        base = dict(
            _quad_short_map={"A": "A::1", "B": "B::1"},
            quad_tl_var=self._Var("A"), quad_tr_var=self._Var("自动"),
            quad_bl_var=self._Var("自动"), quad_br_var=self._Var("自动"),
            _target_map={"自动": None, "C": "C::1"},
            sbs_left_var=self._Var("C"), sbs_right_var=self._Var("自动"),
            stack_top_var=self._Var("自动"), stack_mid_var=self._Var("自动"),
            stack_bot_var=self._Var("自动"), target_var=self._Var("自动"),
        )
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_quad_selection_first_and_deduped(self):
        app = self._fake()
        got = ui.App._capacity_probe_targets(app)
        self.assertEqual(got, ["A::1", "C::1"])

    def test_falls_back_to_front_windows(self):
        saved = b.list_target_windows
        b.list_target_windows = lambda: [dict(WIN, hwnd="X::1"),
                                         dict(WIN, hwnd="X::2")]
        try:
            app = self._fake(
                _quad_short_map={"自动": None},
                quad_tl_var=self._Var("自动"), quad_tr_var=self._Var("自动"),
                _target_map={"自动": None},
                sbs_left_var=self._Var("自动"), sbs_right_var=self._Var("自动"),
                stack_top_var=self._Var("自动"),
                stack_mid_var=self._Var("自动"),
                stack_bot_var=self._Var("自动"), target_var=self._Var("自动"),
            )
            got = ui.App._capacity_probe_targets(app)
        finally:
            b.list_target_windows = saved
        self.assertEqual(got, ["X::1", "X::2"])

    def test_returns_empty_when_nothing_available(self):
        saved = b.list_target_windows
        b.list_target_windows = lambda: []
        try:
            app = self._fake(
                _quad_short_map={}, _target_map={"自动": None},
                quad_tl_var=self._Var("自动"), quad_tr_var=self._Var("自动"),
                quad_bl_var=self._Var("自动"), quad_br_var=self._Var("自动"),
                sbs_left_var=self._Var("自动"), sbs_right_var=self._Var("自动"),
                stack_top_var=self._Var("自动"),
                stack_mid_var=self._Var("自动"),
                stack_bot_var=self._Var("自动"), target_var=self._Var("自动"),
            )
            got = ui.App._capacity_probe_targets(app)
        finally:
            b.list_target_windows = saved
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
