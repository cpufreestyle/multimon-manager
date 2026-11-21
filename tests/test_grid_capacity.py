#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网格容量预检：grid_capacity（后端）与 _grid_capacity_text（界面文案）。

与四宫格容量预检同一职责：App 的最小窗口尺寸是硬上限，不该等点完「网格批排」
才发现有两个窗口没动。预检只读上次排布缓存的最小尺寸，不动任何窗口；
没探过的窗口按 0 计（乐观），必须如实列出。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402

try:  # ui 依赖 tkinter；无显示环境下降级跳过（只少覆盖文案格式化）
    import ui  # noqa: E402
except Exception:  # noqa: BLE001
    ui = None


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


SCREEN = _Mon(0, 0, 1920, 1080, "S")


class GridCapacityTest(unittest.TestCase):
    def setUp(self):
        wm._QUAD_MIN_CACHE.clear()
        self._saved = (wm.monitors, wm._gap_settings, wm.list_target_windows)
        wm.monitors = _FakeMonitors([SCREEN])
        wm._gap_settings = lambda: (12, False)

    def tearDown(self):
        wm.monitors, wm._gap_settings, wm.list_target_windows = self._saved
        wm._QUAD_MIN_CACHE.clear()

    def _cap(self, hwnds, mins, **kw):
        wm._QUAD_MIN_CACHE.clear()
        wm._QUAD_MIN_CACHE.update(mins)
        wins = [{"hwnd": h, "pid": 1, "owner": h.split("::")[0],
                 "name": h.split("::")[1], "x": 0, "y": 0,
                 "w": 300, "h": 200}
                for h in hwnds]
        wm.list_target_windows = lambda: wins
        return wm.grid_capacity(**kw)

    def test_no_cache_returns_none(self):
        self.assertIsNone(self._cap(["A::1", "B::1"], {}, cols=2, rows=1))
        self.assertIsNone(wm.grid_capacity(cols=2, rows=1))

    def test_empty_screen_selection_returns_none(self):
        cap = self._cap(["A::1"], {"A::1": (300, 300)},
                        cols=2, rows=1, monitor_indices=[])
        self.assertIsNone(cap)

    def test_two_windows_two_cells(self):
        cap = self._cap(["A::1", "B::1"],
                        {"A::1": (300, 300), "B::1": (300, 300)},
                        cols=2, rows=1)
        self.assertEqual((cap["fit"], cap["total"]), (2, 2))
        self.assertEqual((cap["cols"], cap["rows"], cap["cells"]), (2, 1, 2))
        self.assertEqual(cap["deficit"], {"w": 0, "h": 0})
        self.assertEqual(cap["uncached"], [])

    def test_min_width_binds_and_both_still_fit(self):
        """A 的最小宽 1200 超过半屏 960：预检必须按撑宽后的几何算，报 2/2。"""
        cap = self._cap(["A::1", "B::1"],
                        {"A::1": (1200, 400), "B::1": (300, 300)},
                        cols=2, rows=1)
        self.assertEqual((cap["fit"], cap["total"]), (2, 2),
                         "撑宽列后两个都该排得下：%s" % (cap,))
        self.assertEqual(cap["deficit"], {"w": 0, "h": 0})

    def test_too_wide_reports_deficit(self):
        cap = self._cap(["A::1", "B::1"],
                        {"A::1": (2000, 400), "B::1": (300, 300)},
                        cols=2, rows=1)
        self.assertEqual(cap["fit"], 1)
        self.assertGreater(cap["deficit"]["w"], 0)

    def test_uncached_windows_listed(self):
        cap = self._cap(["A::1", "B::1"], {"A::1": (300, 300)},
                        cols=2, rows=1)
        self.assertEqual(cap["uncached"], ["B::1"])

    def test_layout_preset_is_honoured(self):
        names = ["A::1", "B::1", "C::1", "D::1", "E::1", "F::1"]
        cap = self._cap(names, {h: (300, 300) for h in names},
                        layout="3×2 六等分")
        self.assertEqual((cap["cols"], cap["rows"], cap["cells"]), (3, 2, 6))
        self.assertEqual((cap["fit"], cap["total"]), (6, 6))


@unittest.skipIf(ui is None, "tkinter/ui 不可导入")
class GridCapacityTextTest(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(ui.App._grid_capacity_text(None), "")
        self.assertEqual(ui.App._grid_capacity_text({}), "")

    def test_all_fit(self):
        t = ui.App._grid_capacity_text(
            {"fit": 2, "total": 2, "cols": 2, "rows": 1,
             "deficit": {"w": 0, "h": 0}, "uncached": []})
        self.assertEqual(t, "2/2 可排")

    def test_deficit_text(self):
        t = ui.App._grid_capacity_text(
            {"fit": 1, "total": 2, "cols": 2, "rows": 1,
             "deficit": {"w": 380, "h": 0}, "uncached": []})
        self.assertEqual(t, "1/2 可排 · 宽差380")

    def test_uncached_suffix(self):
        t = ui.App._grid_capacity_text(
            {"fit": 1, "total": 2, "cols": 2, "rows": 1,
             "deficit": {"w": 0, "h": 0}, "uncached": ["B::1"]})
        self.assertEqual(t, "1/2 可排，1 个未探测")


if __name__ == "__main__":
    unittest.main(verbosity=2)
