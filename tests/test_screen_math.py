#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""screen_math 与布局自适应的单测（无 GUI、无 AppleScript）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import screen_math  # noqa: E402


class FakeMonitor:
    def __init__(self, left, top, width, height,
                 work_left=None, work_top=None, work_width=None, work_height=None):
        self.left = left
        self.top = top
        self.width = width
        self.height = height
        self.work_left = work_left if work_left is not None else left
        self.work_top = work_top if work_top is not None else top
        self.work_width = work_width if work_width is not None else width
        self.work_height = work_height if work_height is not None else height


MONS = [
    FakeMonitor(0, 0, 1512, 897, work_top=33, work_height=864),
    FakeMonitor(1512, 0, 1080, 1920),
]


class TestIntersectionMonitor(unittest.TestCase):
    def test_cross_boundary_prefers_larger_intersection(self):
        # 窗口横跨两块屏交界：与右屏交集 (200x400) 比与左屏的 (133x400) 大，
        # 应判给右屏——中心点法在交界处就是靠运气
        idx = screen_math.monitor_index_by_intersection(
            MONS, 1312, 100, 600, 400)
        self.assertEqual(idx, 1)

    def test_left_heavy_window_judged_left(self):
        # 只有一小截伸进右屏：交集大部分在左屏，应判左屏
        idx = screen_math.monitor_index_by_intersection(
            MONS, 1000, 100, 600, 400)
        self.assertEqual(idx, 0)

    def test_center_fallback_when_no_intersection(self):
        # 窗口完全脱离所有屏（读回异常坐标）：退回中心点法（右屏中心）
        idx = screen_math.monitor_index_by_intersection(
            MONS, 2000, 100, 100, 100)
        self.assertEqual(idx, 1)


class TestRelativeFrames(unittest.TestCase):
    def test_normalize_denormalize_roundtrip(self):
        rel = screen_math.normalize_to_work(10, 103, 700, 500, 0, 33, 1512, 864)
        x, y, w, h = screen_math.denormalize_to_work(rel, 0, 33, 1512, 864)
        self.assertEqual((x, y, w, h), (10, 103, 700, 500))

    def test_remap_to_smaller_screen_keeps_proportions(self):
        rel = screen_math.normalize_to_work(0, 33, 1512, 864, 0, 33, 1512, 864)
        x, y, w, h = screen_math.denormalize_to_work(rel, 1512, 0, 1080, 1920)
        self.assertEqual((x, y, w, h), (1512, 0, 1080, 1920))


class TestClampToWork(unittest.TestCase):
    def test_oversized_window_clamped_inside(self):
        # 顶边越界（y < work_top）+ 宽超工作区：先夹宽，再把 y 钉回工作区顶
        x, y, w, h = screen_math.clamp_to_work(-53, -1000, 1603, 800,
                                               -53, -900, 1600, 900)
        # 高度 800 没超 900，但顶边越出 100px：钉回工作区顶（宁可靠上，不越界）
        self.assertEqual(w, 1600)
        self.assertEqual(y, -900)
        self.assertEqual(x, -53)
        self.assertLessEqual(y + h, -900 + 900)

    def test_bottom_overflow_moves_up(self):
        # 底边越界：把 y 上移到「底边贴住工作区底」，与四宫格修复同一原则
        x, y, w, h = screen_math.clamp_to_work(-53, -300, 600, 800,
                                               -53, -900, 1600, 900)
        self.assertEqual(y + h, -900 + 900)

    def test_zero_work_area_is_noop(self):
        self.assertEqual(screen_math.clamp_to_work(5, 5, 100, 100, 0, 0, 0, 0),
                         (5, 5, 100, 100))


if __name__ == "__main__":
    unittest.main()
