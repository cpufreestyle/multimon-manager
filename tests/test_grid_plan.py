#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""grid_plan 的单测：虚拟桌面网格的纯几何（不碰窗口、不碰显示器枚举）。

覆盖三类事实，它们都是真机踩过的坑：

1. **格子绝不跨屏**。macOS 会把横跨两屏的窗口判给其中一块（真机右下窗
   x+w=2880 越过 DELL 右界，整个窗被搬到 Sidecar）——这就是「四个窗口不在
   一个屏幕上」的根因。assign_cells 必须把每格裁进某一块屏的工作区。
2. **等分不出现 1px 缝**。宽高为奇数时最后一列/行用减法补齐。
3. **间距按贴边算**。网格线落在屏中间是窗间缝；格子被裁到屏边界后那条线
   已是屏幕外缘，再留缝就是「没吸附到边上」。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grid_plan as gp  # noqa: E402


class _Mon:
    """最小显示器替身：只需要工作区四个扁平字段与设备名。"""

    def __init__(self, left=0, top=0, width=1920, height=1080, name="M1"):
        self.work_left, self.work_top = left, top
        self.work_width, self.work_height = width, height
        self.device_name = name


def _cells(vr, cols, rows):
    return gp.grid_cells(vr[0], vr[1], vr[2], vr[3], cols, rows)


class TestVirtualRect(unittest.TestCase):
    def test_bounding_box_of_works(self):
        ms = [_Mon(0, 33, 1512, 887, "Built-in"),
              _Mon(0, -900, 1600, 870, "RV200"),
              _Mon(-1080, -1100, 1080, 1920, "DELL")]
        self.assertEqual(gp.virtual_rect(ms), (-1080, -1100, 2680, 2020))

    def test_empty_returns_none(self):
        self.assertIsNone(gp.virtual_rect([]))
        self.assertIsNone(gp.virtual_rect(None))
        self.assertIsNone(gp.virtual_rect([None, None]))


class TestGridCells(unittest.TestCase):
    def test_no_1px_seam_on_odd_size(self):
        """奇数宽/高下，右列/下排必须用减法补齐，不能短 1px。"""
        cells = _cells((0, 0, 1921, 1081), gp.equal_fracs(2),
                       gp.equal_fracs(2))
        self.assertEqual(len(cells), 4)
        for r in (0, 1):
            row = [c for c in cells if c[0] == r]
            self.assertEqual(sum(c[4] for c in row), 1921,
                             "第 %d 排左右两列没拼满（出现 1px 缝）" % r)
        for c in (0, 1):
            col = [x for x in cells if x[1] == c]
            self.assertEqual(sum(x[5] for x in col), 1081,
                             "第 %d 列上下两排没拼满（出现 1px 缝）" % c)

    def test_row_major_order(self):
        """行优先：第 k 个窗口去第 k 格，结果才可预期。"""
        cells = _cells((0, 0, 900, 600), gp.equal_fracs(3),
                       gp.equal_fracs(2))
        self.assertEqual([(c[0], c[1]) for c in cells],
                         [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)])

    def test_zero_area_returns_empty(self):
        self.assertEqual(_cells((0, 0, 0, 600), gp.equal_fracs(2),
                                gp.equal_fracs(2)), [])

    def test_weighted_columns(self):
        """2/3+1/3：右列应是左列宽度的一半。"""
        cells = _cells((0, 0, 1200, 600), (2.0 / 3, 1.0 / 3),
                       gp.equal_fracs(2))
        left = next(c for c in cells if c[1] == 0)
        right = next(c for c in cells if c[1] == 1)
        self.assertEqual(left[4], 800)
        self.assertEqual(right[4], 400)


class TestAssignCells(unittest.TestCase):
    """格子必须裁进某一块屏的工作区——绝不跨屏。"""

    MONS = [_Mon(0, 33, 1512, 887, "Built-in"),      # 主屏
            _Mon(0, -900, 1600, 870, "RV200"),       # 上方
            _Mon(-1080, -1100, 1080, 1920, "DELL")]  # 左侧竖屏

    def _check_inside(self, asg, mons=None):
        """每个格子都必须完整落在某一块屏的工作区内（绝不跨屏）。"""
        for row, col, mi, x, y, w, h in asg:
            mon = (mons or self.MONS)[mi]
            self.assertGreater(w, 0, "格子 (%d,%d) 宽为 0" % (row, col))
            self.assertGreater(h, 0, "格子 (%d,%d) 高为 0" % (row, col))
            self.assertGreaterEqual(x, mon.work_left,
                                    "格子 (%d,%d) 越过所在屏左界" % (row, col))
            self.assertLessEqual(x + w, mon.work_left + mon.work_width,
                                 "格子 (%d,%d) 越过所在屏右界" % (row, col))
            self.assertGreaterEqual(y, mon.work_top)
            self.assertLessEqual(y + h, mon.work_top + mon.work_height)

    def test_every_preset_stays_inside_one_screen(self):
        """10 个预设布局，在真机三屏几何下每个格子都必须落在单块屏内。"""
        vr = gp.virtual_rect(self.MONS)
        for label in gp.layout_labels():
            cols, rows = gp.layout_by_label(label)
            asg = gp.assign_cells(_cells(vr, cols, rows), self.MONS)
            self.assertTrue(asg, "%s 一个格子都没剩下" % label)
            self._check_inside(asg)

    def test_3x2_six_cells_all_kept(self):
        """3×2 六等分：6 格全部裁在各自屏内（屏间空隙没有吃掉格子）。"""
        vr = gp.virtual_rect(self.MONS)
        cols, rows = gp.layout_by_label("3×2 六等分")
        asg = gp.assign_cells(_cells(vr, cols, rows), self.MONS)
        self.assertEqual(len(asg), 6)
        self._check_inside(asg)

    def test_cell_in_gap_is_dropped(self):
        """整格落在屏间空隙里：丢掉，不拿它去摆窗口。

        两块 500 宽的屏中间留 500 空隙，虚拟桌面 1500 切三等分，中间那一格
        正好整格落在空隙里（500..1000），与两块屏交集都是 0。
        """
        ms = [_Mon(0, 0, 500, 500, "L"), _Mon(1000, 0, 500, 500, "R")]
        vr = gp.virtual_rect(ms)
        self.assertEqual(vr, (0, 0, 1500, 500))
        cells = _cells(vr, gp.equal_fracs(3), gp.equal_fracs(2))
        self.assertEqual(len(cells), 6)
        asg = gp.assign_cells(cells, ms)
        # 中间那一列（col=1）两排都丢掉，只剩 4 格
        self.assertEqual(len(asg), 4)
        self.assertEqual({a[1] for a in asg}, {0, 2})
        self._check_inside(asg, ms)

    def test_cell_overlapping_gap_is_clipped(self):
        """跨着空隙的格子被裁进交集最大的那块屏，绝不跨屏。"""
        ms = [_Mon(0, 0, 800, 500, "L"), _Mon(1000, 0, 800, 500, "R")]
        vr = gp.virtual_rect(ms)
        cells = _cells(vr, gp.equal_fracs(2), gp.equal_fracs(2))
        asg = gp.assign_cells(cells, ms)
        self.assertEqual(len(asg), 4)
        self._check_inside(asg, ms)

    def test_no_monitors(self):
        self.assertEqual(gp.assign_cells([(0, 0, 0, 0, 10, 10)], []), [])
        self.assertEqual(gp.assign_cells([(0, 0, 0, 0, 10, 10)], None), [])


class TestCellGapRect(unittest.TestCase):
    """间距判据是「裁完还贴不贴所在屏工作区边界」，不是网格行列号。"""

    def test_internal_edges_get_gap(self):
        mon = _Mon(0, 0, 1000, 1000)
        # 2×2 的左上格：左/上是屏幕外缘，右/下是窗间缝
        got = gp.cell_gap_rect(0, 0, 2, 2, 0, 0, 500, 500, mon, 12, False)
        self.assertEqual(got, (0, 0, 494, 494))

    def test_screen_edge_no_gap_unless_opted_in(self):
        """贴屏幕外缘的边不留缝——再留就是「没吸附到边上」。"""
        mon = _Mon(0, 0, 1000, 1000)
        got = gp.cell_gap_rect(0, 0, 2, 2, 0, 0, 500, 500, mon, 12, True)
        self.assertEqual(got, (6, 6, 488, 488))

    def test_clipped_edge_is_screen_edge(self):
        """格子被裁到屏边界后，那条「网格线」是屏幕外缘，不留缝。"""
        mon = _Mon(0, 0, 1000, 500)
        # 1000x500 的屏按 3 列切，col=2 是最后一列：右边贴屏幕右界（不留缝），
        # 左边与 col=1 相邻（留半条缝）
        got = gp.cell_gap_rect(0, 2, 2, 3, 700, 0, 300, 250, mon, 12, False)
        self.assertEqual(got, (706, 0, 294, 244))

    def test_gap_zero_returns_input(self):
        mon = _Mon(0, 0, 1000, 1000)
        r = (0, 0, 500, 500)
        self.assertEqual(gp.cell_gap_rect(0, 0, 2, 2, *r, mon, 0, False), r)

    def test_tiny_cell_not_shrunk_to_zero(self):
        """格子比 gap 还小时原样返回，不能让窗口尺寸变成 0。"""
        mon = _Mon(0, 0, 1000, 1000)
        r = (0, 0, 4, 4)
        self.assertEqual(gp.cell_gap_rect(0, 0, 2, 2, *r, mon, 12, False), r)


class TestAssignWindows(unittest.TestCase):
    def _w(self, hwnd, w, h):
        return {"hwnd": hwnd, "w": w, "h": h}

    def test_manual_order_is_identity(self):
        wins = [self._w("a", 100, 100), self._w("b", 200, 200)]
        self.assertEqual(gp.assign_windows(wins, [(50, 50), (50, 50)],
                                           smart=False),
                         [0, 1])

    def test_smart_puts_wide_window_in_wide_cell(self):
        """最横的窗去最横的格：智能分配唯一可预期的行为。"""
        wins = [self._w("wide", 1600, 600), self._w("tall", 400, 1200)]
        sizes = [(400, 1200), (1600, 600)]
        perm = gp.assign_windows(wins, sizes, smart=True)
        self.assertEqual(perm[0], 1, "横窗该去横格")
        self.assertEqual(perm[1], 0)

    def test_smart_is_a_permutation(self):
        wins = [self._w("a", 900, 600), self._w("b", 500, 700),
                self._w("c", 1200, 400), self._w("d", 600, 600)]
        sizes = [(600, 600)] * 4
        perm = gp.assign_windows(wins, sizes, smart=True)
        self.assertEqual(sorted(perm), [0, 1, 2, 3])

    def test_single_window(self):
        self.assertEqual(gp.assign_windows([self._w("a", 10, 10)], [(5, 5)]),
                         [0])

    def test_many_windows_falls_back_to_sorted(self):
        """n>6 退到按宽高比排序配对（确定性启发式，不引入匈牙利算法）。"""
        wins = [self._w("w%d" % i, 100 * (i + 1), 100) for i in range(8)]
        sizes = [(100 * (i + 1), 100) for i in range(8)]
        perm = gp.assign_windows(wins, sizes, smart=True)
        self.assertEqual(sorted(perm), list(range(8)))


class TestPickAndResolve(unittest.TestCase):
    def _w(self, hwnd, owner=None, pid=99):
        return {"hwnd": hwnd, "owner": owner or hwnd.split("::")[0],
                "name": hwnd.split("::")[1],
                "x": 0, "y": 0, "w": 100, "h": 100, "pid": pid}

    def test_pick_skips_own_pid_and_owners(self):
        cands = [self._w("me::1", pid=1), self._w("wine::1"),
                 self._w("A::1"), self._w("B::1")]
        got = gp.pick_windows(cands, 4, skip_owners={"wine"}, own_pids={1})
        self.assertEqual([w["hwnd"] for w in got], ["A::1", "B::1"])

    def test_pick_takes_at_most_n(self):
        cands = [self._w("A::1"), self._w("B::1"), self._w("C::1")]
        got = gp.pick_windows(cands, 2)
        self.assertEqual([w["hwnd"] for w in got], ["A::1", "B::1"])

    def test_resolve_reports_missing(self):
        """选中的窗口已关掉：如实报出来，不静默换窗口顶上。"""
        cands = [self._w("A::1")]
        wins, missing = gp.resolve_hwnds(["A::1", "Z::9"], cands)
        self.assertEqual([w["hwnd"] for w in wins], ["A::1"])
        self.assertEqual(missing, ["Z::9"])

    def test_resolve_dedupes(self):
        cands = [self._w("A::1")]
        wins, _m = gp.resolve_hwnds(["A::1", "A::1"], cands)
        self.assertEqual(len(wins), 1)


class TestFitReport(unittest.TestCase):
    MONS = [_Mon(0, 0, 1000, 1000, "M1")]

    def _asg(self, gap=0, edge=False):
        vr = gp.virtual_rect(self.MONS)
        cols, rows = gp.layout_by_label("2×2 四等分")
        return gp.assign_cells(_cells(vr, cols, rows), self.MONS), gap, edge

    def _w(self, hwnd, w=100, h=100):
        return {"hwnd": hwnd, "w": w, "h": h}

    def test_all_fit(self):
        asg, gap, edge = self._asg()
        wins = [self._w("A::1"), self._w("B::1"), self._w("C::1"),
                self._w("D::1")]
        slots, too_small = gp.fit_report(asg, wins, {}, gap, edge, self.MONS)
        self.assertEqual(len(slots), 4)
        self.assertEqual(too_small, [])

    def test_too_small_window_left_alone(self):
        """最小尺寸超出格子：进 too_small，调用方让它保持原位。"""
        asg, gap, edge = self._asg()
        wins = [self._w("A::1"), self._w("B::1"), self._w("C::1"),
                self._w("D::1")]
        mins = {"A::1": (600, 400)}
        slots, too_small = gp.fit_report(asg, wins, mins, gap, edge,
                                         self.MONS)
        self.assertEqual(len(slots), 3)
        self.assertEqual([h for h, _w, _r in too_small], ["A::1"])

    def test_slack_tolerates_probe_jitter(self):
        """探测有 1~2px 抖动，差几像素就能放下的窗口不该被判成装不下。"""
        asg, gap, edge = self._asg()
        wins = [self._w("A::1"), self._w("B::1"), self._w("C::1"),
                self._w("D::1")]
        # 格子 500x500，容差 6px：506 装得下
        mins = {"A::1": (506, 506)}
        _slots, too_small = gp.fit_report(asg, wins, mins, gap, edge,
                                          self.MONS)
        self.assertEqual(too_small, [])

    def test_slots_use_gap_rect(self):
        """slots 里的矩形是让出间距后的目标值，不是原始格子。"""
        asg, gap, edge = self._asg(gap=12)
        wins = [self._w("A::1"), self._w("B::1"), self._w("C::1"),
                self._w("D::1")]
        slots, _ts = gp.fit_report(asg, wins, {}, gap, edge, self.MONS)
        by_rc = {(a[0], a[1]): a for a, _h, _w in slots}
        # 左上格贴左上：起点不动，右下角让出半个缝
        self.assertEqual(by_rc[(0, 0)][3:], (0, 0, 494, 494))
        # 右下格贴右下：起点让出半个缝，尺寸补齐
        self.assertEqual(by_rc[(1, 1)][3:], (506, 506, 494, 494))

    def test_more_windows_than_cells(self):
        """窗口多于格子：多出的窗口没有格子可去，不硬塞。"""
        asg, gap, edge = self._asg()
        wins = [self._w("A::1"), self._w("B::1"), self._w("C::1"),
                self._w("D::1"), self._w("E::1")]
        slots, too_small = gp.fit_report(asg, wins, {}, gap, edge,
                                         self.MONS)
        self.assertEqual(len(slots), 4)
        self.assertEqual(too_small, [])


class TestLayoutTable(unittest.TestCase):
    def test_labels_roundtrip(self):
        for label in gp.layout_labels():
            cols, rows = gp.layout_by_label(label)
            self.assertAlmostEqual(sum(cols), 1.0, places=6)
            self.assertAlmostEqual(sum(rows), 1.0, places=6)

    def test_unknown_label_returns_none(self):
        self.assertIsNone(gp.layout_by_label("不存在"))

    def test_equal_fracs_sums_to_one(self):
        for n in (1, 2, 3, 4):
            self.assertAlmostEqual(sum(gp.equal_fracs(n)), 1.0, places=6)

    def test_norm_fracs_rejects_bad_input(self):
        self.assertEqual(gp._norm_fracs(None, 3), gp.equal_fracs(3))
        self.assertEqual(gp._norm_fracs([], 2), gp.equal_fracs(2))
        self.assertEqual(gp._norm_fracs([0, -1], 2), gp.equal_fracs(2))
        # 比例不求和 1 时归一化
        got = gp._norm_fracs([2, 1], 2)
        self.assertAlmostEqual(sum(got), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
