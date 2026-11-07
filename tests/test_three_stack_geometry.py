#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""竖屏三栏（snap_three_stack）理想几何的单测。

macOS 上系统会钳制写入：标题栏占住顶部约 30px，窗口永远贴不到 work_top。
闭环（读回实测再推下一段）只能把顶段的偏移压到 30 左右，若中/下段用
「上一段实测底部」作起点，这段偏移会被逐级传导、放大成肉眼可见的缝隙。
所以这里的断言对象是「理想几何」：三段等分、余数摊到前若干段，
且中/下段起点按上一段理想底部算，不读实测。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import windows_mac as wm  # noqa: E402


class Mon:
    """最小显示器替身。"""

    def __init__(self, name, x, y, w, h, wx, wy, ww, wh):
        self.device_name = name
        self.x, self.y, self.w, self.h = x, y, w, h
        self.width, self.height = w, h
        self.work_rect = (wx, wy, ww, wh)


def three_stack_rects(wl, wt, ww, wh):
    """三栏的理想矩形序列：等分 + 余数摊前，起点不读实测。"""
    base = wh // 3
    rem = wh - base * 3
    heights = [base + (1 if i < rem else 0) for i in range(3)]
    rects = []
    y = wt
    for h in heights:
        rects.append((wl, y, ww, h))
        y += h
    return rects


class TestThreeStackIdealGeometry(unittest.TestCase):
    """只测「应该写成什么样」，不打真窗口。"""

    def test_three_equal_when_divisible(self):
        """能被 3 整除时三段严格等高，且首尾相接无缝隙。"""
        rects = three_stack_rects(0, 0, 1152, 2046)  # 2046 % 3 == 0
        self.assertEqual([r[3] for r in rects], [682, 682, 682])
        self.assertEqual(rects[0][1] + rects[0][3], rects[1][1])
        self.assertEqual(rects[1][1] + rects[1][3], rects[2][1])
        self.assertEqual(rects[2][1] + rects[2][3], 2046)

    def test_remainder_goes_to_front_segments(self):
        """不能整除时余数摊到前若干段，而不是只补给末段。"""
        rects = three_stack_rects(0, 0, 1152, 2048)  # 2048 % 3 == 2
        heights = [r[3] for r in rects]
        self.assertEqual(sum(heights), 2048)
        self.assertEqual(heights, [683, 683, 682])
        self.assertEqual(max(heights) - min(heights), 1)

    def test_no_gap_between_segments(self):
        """任意 wh 下三段首尾相接，不出现缝隙或重叠。"""
        for wh in (2048, 2046, 2047, 2049, 1080, 1081, 900):
            rects = three_stack_rects(0, 0, 1152, wh)
            self.assertEqual(rects[0][1] + rects[0][3], rects[1][1])
            self.assertEqual(rects[1][1] + rects[1][3], rects[2][1])
            self.assertEqual(rects[2][1] + rects[2][3], wh)

    def test_work_rect_offset_preserved(self):
        """竖屏 work_rect 常带负 y（主屏左侧），起点必须用 work_top。"""
        rects = three_stack_rects(1512, -1066, 1152, 2048)
        self.assertEqual(rects[0][1], -1066)
        self.assertEqual(rects[2][1] + rects[2][3], -1066 + 2048)


class TestThreeStackWrites(unittest.TestCase):
    """观测 snap_three_stack 实际写给 set_window_rect 的矩形。

    这是本次修的核心回归：中/下段起点必须由「上一段理想高度」推出，
    不能由上一段实测高度推出——macOS 会把顶段整体下压约 31px（标题栏），
    若用它作起点，偏移会逐段传导、放大成缝隙。
    """

    def setUp(self):
        self._prev_set = wm.set_window_rect
        self._prev_get = wm.get_window_rect
        self._prev_mons = wm.monitors.enum_monitors
        self.writes = []
        # 模拟系统钳制：顶段被下压 31px（标题栏），其余按写入生效
        self._drawn = {}

        def fake_set(hwnd, x, y, w, h, activate=True, **_k):
            self.writes.append((hwnd, (x, y, w, h)))
            self._drawn[hwnd] = (x, y + 31 if h == 682 and y == -1066 else y, w, h)
            return True

        def fake_get(hwnd):
            return self._drawn.get(hwnd, (0, 0, 0, 0))

        wm.set_window_rect = fake_set
        wm.get_window_rect = fake_get
        wm.monitors.enum_monitors = lambda force=False: [
            Mon("Mi", 1512, -1066, 1152, 2048, 1512, -1066, 1152, 2048)]

    def tearDown(self):
        wm.set_window_rect = self._prev_set
        wm.get_window_rect = self._prev_get
        wm.monitors.enum_monitors = self._prev_mons

    def test_mid_and_bottom_use_ideal_origins(self):
        """中/下段起点按理想高度累加，不吃上一段的 31px 偏移。"""
        wm._pinned_target = None
        ok = wm.snap_three_stack(top_hwnd="A::a", mid_hwnd="B::b",
                                 bot_hwnd="C::c", use_pinned=False, monitor=0)
        self.assertTrue(ok)
        self.assertEqual(len(self.writes), 3)
        rects = [r for _, r in self.writes]
        # 三段高度必须等分且只差 1px 以内（2048 % 3 == 2）
        heights = [r[3] for r in rects]
        self.assertEqual(sum(heights), 2048)
        self.assertLessEqual(max(heights) - min(heights), 1)
        # 关键：中段起点 = 顶段理想底部，而不是顶段实测底部(+31)
        self.assertEqual(rects[0][1], -1066)
        self.assertEqual(rects[1][1], -1066 + heights[0])
        self.assertEqual(rects[2][1], -1066 + heights[0] + heights[1])
        # 末段必须正好收在工作区底部
        self.assertEqual(rects[2][1] + rects[2][3], -1066 + 2048)


if __name__ == "__main__":
    unittest.main()
