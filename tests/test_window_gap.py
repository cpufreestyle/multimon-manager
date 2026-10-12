#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口间距 gap_rect 的单测（借鉴 Rectangle Pro 的 GapCalculation）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402

WL, WT, WW, WH = 0, 0, 1920, 1080


class TestGapRect(unittest.TestCase):
    def setUp(self):
        self._prev = wm._gap_settings

    def tearDown(self):
        wm._gap_settings = self._prev

    def _gap(self, gap, edges=False):
        wm._gap_settings = lambda: (gap, edges)

    def test_zero_gap_is_noop(self):
        """默认 0 间距，行为与以前完全一致。"""
        self._gap(0)
        rect = (0, 0, 960, 540)
        self.assertEqual(wm.gap_rect("quad-tl", *rect, WL, WT, WW, WH), rect)

    def test_topleft_insets_inner_sides_only(self):
        """左上格只让出右边和下边（贴屏幕的边不动）。"""
        self._gap(8)
        self.assertEqual(
            wm.gap_rect("quad-tl", 0, 0, 960, 540, WL, WT, WW, WH),
            (0, 0, 956, 536))

    def test_bottomright_insets_inner_sides_only(self):
        """右下格只让出左边和上边。"""
        self._gap(8)
        self.assertEqual(
            wm.gap_rect("quad-br", 960, 540, 960, 540, WL, WT, WW, WH),
            (964, 544, 956, 536))

    def test_gap_screen_edges_insets_outer_sides(self):
        """勾选「屏幕边也留间距」后，贴边的四条边才让出。"""
        self._gap(8, edges=True)
        self.assertEqual(
            wm.gap_rect("quad-tl", 0, 0, 960, 540, WL, WT, WW, WH),
            (4, 4, 952, 532))

    def test_between_windows_gap_is_gap(self):
        """左右两格之间的缝正好是 gap（右格左边让 gap + 左格右边让 gap）。"""
        self._gap(10)
        left = wm.gap_rect("quad-tl", 0, 0, 960, 540, WL, WT, WW, WH)
        right = wm.gap_rect("quad-tr", 960, 0, 960, 540, WL, WT, WW, WH)
        self.assertEqual(right[0] - (left[0] + left[2]), 10)

    def test_tiny_rect_untouched(self):
        """格子比间距还小时不缩，避免算出 0 宽/高。"""
        self._gap(50)
        rect = (0, 0, 60, 40)
        self.assertEqual(wm.gap_rect("quad-tl", *rect, WL, WT, WW, WH), rect)

    def test_maximize_treats_all_sides_as_outer(self):
        """maximize 没有邻居，四边都算外边：不勾屏幕边就不让间距。"""
        self._gap(8)
        self.assertEqual(
            wm.gap_rect("maximize", 0, 0, 1920, 1080, WL, WT, WW, WH),
            (0, 0, 1920, 1080))
        self._gap(8, edges=True)
        self.assertEqual(
            wm.gap_rect("maximize", 0, 0, 1920, 1080, WL, WT, WW, WH),
            (4, 4, 1912, 1072))

    def test_half_snaps_inset_towards_split(self):
        """左右半屏各向分割线让出间距。"""
        self._gap(8)
        left = wm.gap_rect("left", 0, 0, 960, 1080, WL, WT, WW, WH)
        right = wm.gap_rect("right", 960, 0, 960, 1080, WL, WT, WW, WH)
        self.assertEqual(left, (0, 0, 956, 1080))
        self.assertEqual(right, (964, 0, 956, 1080))


if __name__ == "__main__":
    unittest.main()
