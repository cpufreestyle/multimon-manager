#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并排/竖排容量预检：line_capacity 与状态栏补白。

并排「排完互相压着」和竖排「三段重叠」是同一类物理上限：App 的最小窗口
尺寸（真机 ChatGPT 高 600、Qoder CN 540）让它缩不到目标格子。预检只读
上次排布缓存，不动窗口；没缓存时返回 None，界面回到原来的行为。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402

try:
    import ui  # noqa: E402
    import backend as b  # noqa: E402
except Exception:  # noqa: BLE001
    ui = None
    b = None


class _Mon:
    def __init__(self, left=0, top=0, width=1920, height=1080, name="M1"):
        self.left, self.top = left, top
        self.width, self.height = width, height
        self.is_primary = True
        self.device_name = name
        self.device_path = name
        self.work_rect = (left, top, width, height)
        self.work_left, self.work_top = left, top
        self.work_width, self.work_height = width, height


class _FakeMonitors:
    def __init__(self, ms):
        self._ms = ms

    def enum_monitors(self, *a, **kw):
        return list(self._ms)


LAND = _Mon(0, 0, 1920, 1080, "Land")
LAND_900 = _Mon(0, -900, 1600, 870, "RV200")
PORTRAIT = _Mon(-1080, -1080, 1080, 1920, "Tall")


class LineCapacityTest(unittest.TestCase):
    def setUp(self):
        wm._QUAD_MIN_CACHE.clear()
        self._saved = (wm.monitors, wm._gap_settings, wm._cg_rect)
        wm.monitors = _FakeMonitors([LAND, LAND_900, PORTRAIT])
        wm._gap_settings = lambda: (12, False)
        wm._cg_rect = lambda h: None

    def tearDown(self):
        wm.monitors, wm._gap_settings, wm._cg_rect = self._saved
        wm._QUAD_MIN_CACHE.clear()

    def _cap(self, mins, **kw):
        wm._QUAD_MIN_CACHE.update(mins)
        return wm.line_capacity(list(mins), **kw)

    def test_no_cache_returns_none(self):
        self.assertIsNone(wm.line_capacity(["A::1", "B::1"], axis="row"))
        self.assertIsNone(wm.line_capacity([], axis="row"))
        self.assertIsNone(wm.line_capacity(None))

    def test_two_fit_side_by_side(self):
        cap = self._cap({"A::1": (400, 300), "B::1": (400, 300)},
                        axis="row")
        self.assertEqual((cap["fit"], cap["total"]), (2, 2))
        self.assertEqual(cap["axis"], "row")
        self.assertEqual(cap["available"], 1920)
        self.assertEqual(cap["need"], 400 + 400 + 12)
        self.assertEqual(cap["deficit"], 0)

    def test_two_too_wide(self):
        cap = self._cap({"A::1": (1000, 300), "B::1": (1000, 300)},
                        axis="row")
        self.assertEqual(cap["fit"], 1)
        self.assertEqual(cap["deficit"], 2012 - 1920)

    def test_three_tall_windows_on_short_screen(self):
        cap = self._cap({"A::1": (400, 600), "B::1": (400, 600),
                         "C::1": (400, 600)},
                        axis="col", monitor="RV200")
        self.assertEqual(cap["available"], 870)
        self.assertEqual(cap["fit"], 1)
        self.assertGreater(cap["deficit"], 0)

    def test_auto_axis_follows_portrait(self):
        cap = self._cap({"A::1": (400, 600), "B::1": (400, 600)},
                        axis="auto", monitor="Tall")
        self.assertEqual(cap["axis"], "col")
        self.assertEqual(cap["available"], 1920)

    def test_uncached_windows_are_optimistic(self):
        wm._QUAD_MIN_CACHE.update({"A::1": (400, 300)})
        cap = wm.line_capacity(["A::1", "B::1"], axis="row",
                               monitor="Land")
        self.assertEqual(cap["fit"], 2, "未探测按 0 计，容量是上限")
        self.assertEqual(cap["uncached"], ["B::1"])


@unittest.skipIf(ui is None, "tkinter/ui 不可导入")
class LineCapacityNoteTest(unittest.TestCase):
    def setUp(self):
        self._fn = b.line_capacity

    def tearDown(self):
        b.line_capacity = self._fn

    def test_no_deficit_returns_empty(self):
        b.line_capacity = lambda *a, **k: {
            "axis": "col", "need": 100, "available": 900, "deficit": 0}
        self.assertEqual(
            ui.App._line_capacity_note(None, ["A::1"], "col", None), "")

    def test_deficit_note_names_axis_and_numbers(self):
        b.line_capacity = lambda *a, **k: {
            "axis": "col", "need": 1812, "available": 870, "deficit": 942}
        note = ui.App._line_capacity_note(None, ["A::1", "B::1", "C::1"],
                                          "col", 0)
        self.assertIn("最小高合计 1812px > 屏 870px", note)

    def test_missing_cap_returns_empty(self):
        b.line_capacity = lambda *a, **k: None
        self.assertEqual(
            ui.App._line_capacity_note(None, ["A::1"], "row", 0), "")

    def test_exception_does_not_break_arrange(self):
        def boom(*a, **k):
            raise RuntimeError("boom")
        b.line_capacity = boom
        self.assertEqual(
            ui.App._line_capacity_note(None, ["A::1"], "row", 0), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
