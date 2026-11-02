#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分屏工作区避开 macOS Dock 的单测。

真机 macOS 27.2 上 NSScreen.visibleFrame 的底边是 930，而 Dock 面板顶边
是 926：只依赖 visibleFrame 会让窗口压住 Dock。这里固定住三层契约：
面板矩形解析、固定 Dock 的工作区收紧（含四条边与安全距离）、以及四宫格
与虚拟网格确实只消费收紧后的 work_rect。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dock_autohide_mac as da  # noqa: E402
import grid_plan  # noqa: E402
import monitors_mac as mm  # noqa: E402
import quad_plan  # noqa: E402


def _mon(left=0, top=0, width=1512, height=982,
         work_left=0, work_top=34, work_width=1512, work_height=896,
         name="Built-in"):
    return mm.MonitorInfo(
        index=0, device_name=name, device_path=name, is_primary=True,
        left=left, top=top, width=width, height=height,
        work_left=work_left, work_top=work_top,
        work_width=work_width, work_height=work_height)


class _Mon:
    """轻量替身：grid_plan / quad_plan 只读这几个扁平字段。"""

    def __init__(self, rect, name="M1"):
        self.work_left, self.work_top, self.work_width, self.work_height = rect
        self.left, self.top = rect[0], rect[1]
        self.width, self.height = rect[2], rect[3]
        self.device_name = name
        self.device_path = name


class TestDockPanelParsing(unittest.TestCase):
    def test_pipe_format(self):
        self.assertEqual(da._parse_panel_rect("25|926|1462|46"),
                         (25, 926, 1462, 46))

    def test_rejects_incomplete_or_empty_panel(self):
        self.assertIsNone(da._parse_panel_rect("25|926|1462"))
        self.assertIsNone(da._parse_panel_rect("25|926|0|46"))
        self.assertIsNone(da._parse_panel_rect(""))


class TestFixedDockWorkArea(unittest.TestCase):
    def test_bottom_dock_leaves_safety_gap(self):
        m = _mon()
        self.assertTrue(mm._trim_work_for_dock(m, (25, 926, 1462, 46)))
        self.assertEqual(m.work_top, 34)
        bottom = m.work_top + m.work_height
        self.assertLess(bottom, 926, "窗口底边仍落在 Dock 顶边或更低处")
        self.assertEqual(bottom, 926 - mm.DOCK_SAFETY_PT)

    def test_dock_on_left_or_right_trims_that_side(self):
        left = _mon(work_left=0, work_width=1512)
        self.assertTrue(mm._trim_work_for_dock(
            left, (0, 300, 82, 600)))
        self.assertEqual(left.work_left, 82 + mm.DOCK_SAFETY_PT)

        right = _mon(work_left=0, work_width=1512)
        self.assertTrue(mm._trim_work_for_dock(
            right, (1430, 300, 82, 600)))
        self.assertEqual(right.work_left + right.work_width,
                         1430 - mm.DOCK_SAFETY_PT)

    def test_dock_on_other_monitor_does_not_trim_current(self):
        mons = [_mon(), _mon(left=-1133, top=-1080, width=1080, height=1920,
                             work_left=-1133, work_top=-1080,
                             work_width=1080, work_height=1920,
                             name="Portrait")]
        with mock.patch.object(da, "get_autohide", return_value=False), \
                mock.patch.object(da, "get_panel_rect",
                                  return_value=(-1133, 750, 1080, 90)):
            mm._apply_dock_avoidance(mons)
        self.assertEqual(mons[0].work_rect, (0, 34, 1512, 896))
        self.assertLessEqual(mons[1].work_top + mons[1].work_height,
                             750 - mm.DOCK_SAFETY_PT)

    def test_autohide_does_not_reserve_space(self):
        m = _mon()
        with mock.patch.object(da, "get_autohide", return_value=True), \
                mock.patch.object(da, "get_panel_rect",
                                  return_value=(25, 926, 1462, 46)) as panel:
            mm._apply_dock_avoidance([m])
        self.assertEqual(m.work_rect, (0, 34, 1512, 896))
        panel.assert_not_called()

    def test_panel_failure_falls_back_to_visible_frame_inset(self):
        m = _mon()
        with mock.patch.object(da, "get_autohide", return_value=False), \
                mock.patch.object(da, "get_panel_rect", return_value=None):
            mm._apply_dock_avoidance([m])
        # visibleFrame 底边 930 比屏幕底 982 高 52pt：识别为底部 Dock
        self.assertEqual(m.work_top + m.work_height,
                         930 - mm.DOCK_SAFETY_PT)

    def test_dock_query_exception_does_not_break_enumeration(self):
        m = _mon()
        with mock.patch.object(da, "get_autohide",
                               side_effect=RuntimeError("boom")):
            mm._apply_dock_avoidance([m])
        self.assertGreater(m.work_width, 0)
        self.assertGreater(m.work_height, 0)


class TestQuadAndGridStayAboveDock(unittest.TestCase):
    def test_quad_bottom_cells_end_above_dock(self):
        m = _mon()
        mm._trim_work_for_dock(m, (25, 926, 1462, 46))
        wl, wt, ww, wh = m.work_rect
        shape = quad_plan.quad_shape([0, 1, 2, 3])
        cells = quad_plan.quad_cells(shape, ww // 2, wh // 2,
                                     wl, wt, ww, wh)
        for _x, _y, _w, h in cells:
            self.assertTrue(_y + h <= 926 - mm.DOCK_SAFETY_PT)
        self.assertEqual(cells[3][1] + cells[3][3],
                         926 - mm.DOCK_SAFETY_PT)

    def test_grid_clipped_rects_end_above_dock(self):
        m = _mon()
        mm._trim_work_for_dock(m, (25, 926, 1462, 46))
        mon = _Mon(m.work_rect)
        vr = grid_plan.virtual_rect([mon])
        cells = grid_plan.grid_cells(vr[0], vr[1], vr[2], vr[3],
                                     grid_plan.equal_fracs(2),
                                     grid_plan.equal_fracs(2))
        asg = grid_plan.assign_cells(cells, [mon])
        self.assertEqual(len(asg), 4)
        for _r, _c, _mi, x, y, w, h in asg:
            self.assertLessEqual(y + h, 926 - mm.DOCK_SAFETY_PT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
