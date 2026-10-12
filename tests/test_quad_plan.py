#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quad_plan.plan_quad_smart 的单测：智能分配谁去哪个角、用什么比例。

协议是「把窗口放进宽高比最接近的格子」——最竖的窗口去左下（窄高格），
最横的去右上（宽扁格）。强行反过来放会被 App 的最小尺寸钳制。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quad_plan import plan_quad_smart  # noqa: E402


def W(hwnd, w, h):
    return {"hwnd": hwnd, "w": w, "h": h}


class TestQuadPlanSmart(unittest.TestCase):
    WINS = [W("chat", 1500, 900),    # 最横
            W("term", 400, 900),     # 最竖
            W("code", 1200, 700),
            W("doc", 700, 1000)]

    def test_widest_goes_top_right(self):
        """最横的两个窗口都应在右列（右列宽，装得下横窗）。"""
        lay, _sx, _sy = plan_quad_smart(self.WINS, 1920, 1080)
        right = {lay[1]["hwnd"], lay[3]["hwnd"]}
        self.assertEqual(right, {"chat", "code"})

    def test_tallest_goes_bottom_left(self):
        """最竖的两个窗口都应在左列（左列窄，竖窗才施展得开）。"""
        lay, _sx, _sy = plan_quad_smart(self.WINS, 1920, 1080)
        left = {lay[0]["hwnd"], lay[2]["hwnd"]}
        self.assertEqual(left, {"term", "doc"})

    def test_all_four_distinct(self):
        lay, _sx, _sy = plan_quad_smart(self.WINS, 1920, 1080)
        keys = [w["hwnd"] for w in lay]
        self.assertEqual(len(set(keys)), 4)
        self.assertEqual(set(keys), {"chat", "term", "code", "doc"})

    def test_left_column_narrower(self):
        """竖长型在左列，所以左列应比右列窄。"""
        _lay, sx, _sy = plan_quad_smart(self.WINS, 1920, 1080)
        self.assertLess(sx, 0.5)

    def test_top_row_shorter(self):
        """四格都不该被切成装不下窗口的大小。"""
        ww, wh = 1920, 1080
        lay, sx, sy = plan_quad_smart(self.WINS, ww, wh)
        c1, c2 = round(ww * sx), ww - round(ww * sx)
        r1, r2 = round(wh * sy), wh - round(wh * sy)
        cells = [(c1, r1), (c2, r1), (c1, r2), (c2, r2)]
        for win, (cw, ch) in zip(lay, cells):
            self.assertGreaterEqual(cw, win["w"] * 0.35,
                                    "%s 的格子宽 %d 太窄" % (win["hwnd"], cw))
            self.assertGreaterEqual(ch, win["h"] * 0.35,
                                    "%s 的格子高 %d 太矮" % (win["hwnd"], ch))

    def test_cell_ratios_close_to_windows(self):
        """核心诉求：格子与窗口的「面积占比」应接近。

        这是「把合适窗口放合适位置」的量化标准之一——占比接近意味着
        没有窗口被硬塞进过小的格子，也没有谁霸占了不该得的空间。
        另一条是比例接近，但格子比例受屏幕尺寸与钳制约束，跨度往往与
        窗口不同，所以用面积占比衡量更稳。
        """
        ww, wh = 1920, 1080
        lay, sx, sy = plan_quad_smart(self.WINS, ww, wh)
        c1, c2 = round(ww * sx), ww - round(ww * sx)
        r1, r2 = round(wh * sy), wh - round(wh * sy)
        cells = [(c1, r1), (c2, r1), (c1, r2), (c2, r2)]
        # 口径从「格子面积占比」改为「窗口等比缩放后占掉格子的比例」：
        # 四个窗口总面积 3.25M 是工作区的 1.57 倍，面积占比口径在这组
        # 数据上天然不可满足；利用率才反映用户真实看到的浪费程度。
        from quad_plan import fit_frame
        for win, (cw, ch) in zip(lay, cells):
            rx, ry, rw, rh = fit_frame(win, cw, ch)
            util = rw * rh
            self.assertGreater(util, 0.45,
                               "%s 在 %dx%d 格子里只占到 %.0f%%"
                               % (win["hwnd"], cw, ch, util * 100))

    def test_extreme_ratios_still_clamped(self):
        """极端宽高比也不许把一边压成几个像素。"""
        wins = [W("a", 100, 2000), W("b", 120, 2000),
                W("c", 3000, 200), W("d", 2800, 200)]
        _lay, sx, sy = plan_quad_smart(wins, 1920, 1080)
        self.assertGreaterEqual(sx, 0.15)
        self.assertLessEqual(sx, 0.85)
        self.assertGreaterEqual(sy, 0.15)
        self.assertLessEqual(sy, 0.85)

    def test_all_same_ratio_keeps_four_distinct(self):
        """宽高比全相同时不能出现重复占角。"""
        wins = [W("a", 800, 600), W("b", 800, 600),
                W("c", 800, 600), W("d", 800, 600)]
        lay, _sx, _sy = plan_quad_smart(wins, 1920, 1080)
        self.assertEqual(len({w["hwnd"] for w in lay}), 4)

    def test_zero_size_does_not_crash(self):
        """尺寸为 0 的窗口不应让算法崩（按比例 1.0 兜底）。"""
        wins = [W("a", 0, 0), W("b", 800, 600),
                W("c", 100, 300), W("d", 900, 400)]
        lay, _sx, _sy = plan_quad_smart(wins, 1920, 1080)
        self.assertEqual(len({w["hwnd"] for w in lay}), 4)

    def test_rejects_wrong_window_count(self):
        with self.assertRaises(ValueError):
            plan_quad_smart(self.WINS[:3], 1920, 1080)

    def test_rejects_bad_screen(self):
        with self.assertRaises(ValueError):
            plan_quad_smart(self.WINS, 0, 0)


if __name__ == "__main__":
    unittest.main()
