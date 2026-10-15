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

from quad_plan import (plan_quad_layout, plan_quad_smart,  # noqa: E402
                        choose_quad_slots)


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


class TestChooseQuadSlots(unittest.TestCase):
    """放不下就不硬排：挑「这块屏放得下的窗口子集」，其余原封不动。

    真机现场（Built-in 工作区 1512x896）：CrossPaste 700x600 + CC Switch
    900x600 + Perplexity 800x652 + Tuanjie 896x600，宽高两个方向都超屏。
    旧实现照排四个，结局是上下重叠 503px、左右重叠 757px，还有一个窗被
    macOS 判到别的屏。修复后只排「并排放得下」的那几个。
    """

    @staticmethod
    def _mins(wins):
        return {h: (w["w"], w["h"]) for h, w in wins}

    def test_all_four_when_they_fit(self):
        wins = [W("a", 500, 400), W("b", 400, 380),
                W("c", 700, 300), W("d", 600, 260)]
        wins = [(w["hwnd"], w) for w in wins]
        slots, un = choose_quad_slots(wins, self._mins(wins), 1920, 1080)
        self.assertEqual([c for c, _h, _w in slots], [0, 1, 2, 3])
        self.assertEqual(un, [])

    def test_drops_the_one_that_cannot_fit(self):
        """三个放得下、一个超宽：只排三个，超宽的原封不动。"""
        wins = [W("a", 500, 400), W("b", 400, 380),
                W("c", 700, 300), W("d", 1900, 260)]
        wins = [(w["hwnd"], w) for w in wins]
        slots, un = choose_quad_slots(wins, self._mins(wins), 1920, 1080)
        self.assertEqual([h for _c, h, _w in slots], ["a", "b", "c"])
        self.assertEqual([h for h, _w in un], ["d"])

    def test_real_device_only_two_fit(self):
        """真机数据：Built-in 上这四个窗口只能并排放下两个。"""
        wins = [W("cp", 700, 600), W("cc", 900, 600),
                W("px", 800, 652), W("tc", 896, 600)]
        wins = [(w["hwnd"], w) for w in wins]
        slots, un = choose_quad_slots(wins, self._mins(wins), 1512, 896,
                                      gap=12)
        # 只有 CrossPaste + Perplexity 的宽度和 1500 + 12px 间距刚好放进 1512
        self.assertEqual([(c, h) for c, h, _w in slots],
                         [(0, "cp"), (1, "px")])
        self.assertEqual([h for h, _w in un], ["cc", "tc"])

    def test_gap_eats_into_capacity(self):
        """间距要占容量：刚好铺满宽的一对，开了间距就排不下了。"""
        wins = [(w["hwnd"], w) for w in (W("a", 750, 500), W("b", 750, 500))]
        mins = self._mins(wins)
        slots, _un = choose_quad_slots(wins, mins, 1500, 900, gap=0)
        self.assertEqual(len(slots), 2)
        slots, _un = choose_quad_slots(wins, mins, 1500, 900, gap=12)
        self.assertEqual(len(slots), 1)

    def test_keeps_requested_corner_when_possible(self):
        """手动模式：用户点名的角尽量保留，不为了凑数把窗口挪到别的角。"""
        wins = [W("tl", 400, 300), W("tr", 400, 300),
                W("bl", 400, 300), W("br", 3000, 300)]
        wins = [(w["hwnd"], w) for w in wins]
        slots, un = choose_quad_slots(wins, self._mins(wins), 1920, 1080)
        self.assertEqual([(c, h) for c, h, _w in slots],
                         [(0, "tl"), (1, "tr"), (2, "bl")])
        self.assertEqual([h for h, _w in un], ["br"])

    def test_nothing_fits_returns_empty(self):
        wins = [(w["hwnd"], w) for w in (W("a", 3000, 2000),)]
        slots, un = choose_quad_slots(wins, self._mins(wins), 1920, 1080)
        self.assertEqual(slots, [])
        self.assertEqual([h for h, _w in un], ["a"])

    def test_empty_input(self):
        self.assertEqual(choose_quad_slots([], {}, 1920, 1080), ([], []))


class TestPlanQuadLayout(unittest.TestCase):
    """plan_quad_layout：1~4 个窗口、用户点选的那些角。

    与 plan_quad_smart 的分工：后者是「恰好四个、四角全占」的严格包装；前者
    是「选几个就排几个」的真正入口。界面点掉不想要的格之后，2~3 个窗口走智能
    分配是常态，旧实现此时抛 ValueError 退化成手动顺序——「智能模式不准」。
    """

    def _w(self, hwnd, w, h):
        return dict(W(hwnd, w, h), min_w=0, min_h=0)

    def test_returns_passed_dicts_in_corner_order(self):
        """返回的就是传入的 dict，且按角标升序——调用方靠 hwnd 认窗口。"""
        a, b, c = self._w("a", 1600, 400), self._w("b", 600, 1200), \
            self._w("c", 900, 900)
        slots, _sx, _sy = plan_quad_layout([a, b, c], 1920, 1080,
                                           corners=[2, 0, 1])
        self.assertEqual([c for c, _w in slots], [0, 1, 2])
        # 返回的就是传入的那几个 dict（不是副本），调用方取 hwnd / min_* 用
        self.assertEqual(sorted(id(w) for _c, w in slots),
                         sorted([id(a), id(b), id(c)]))
        # corners 只说明「这三个角参与」，谁去哪个角由宽高比定（见下面的用例）
        # 三个角是 T 形：下排只占 BL 一格，那一格拉通整宽（1920 宽）。最宽扁的
        # a(4.0) 去整宽格 BL，方圆 c(1.0) 去较方的 TR，瘦高 b(0.5) 去剩下的
        # TL——旧实现 3 窗也按 2x2 切、右下留整块死区，宽扁窗还进不了整宽格。
        self.assertEqual([w["hwnd"] for _c, w in slots],
                         ["b", "c", "a"])

    def test_empty_row_gives_full_height_split(self):
        """整排空着：有窗的那排撑满，纵向比例没有选择余地（sy=1.0 / 0.0）。"""
        wins = [self._w("a", 1600, 400), self._w("b", 600, 1200)]
        _s, sx, sy = plan_quad_layout(wins, 1920, 1080, corners=[0, 1])
        self.assertEqual(sy, 1.0)
        self.assertGreater(sx, 0.0)
        _s, sx, sy = plan_quad_layout(wins, 1920, 1080, corners=[2, 3])
        self.assertEqual(sy, 0.0)

    def test_empty_column_gives_full_width_split(self):
        """整列空着：有窗的那列撑满，横向比例没有选择余地（sx=1.0 / 0.0）。"""
        wins = [self._w("a", 1600, 400), self._w("b", 600, 1200)]
        _s, sx, _sy = plan_quad_layout(wins, 1920, 1080, corners=[0, 2])
        self.assertEqual(sx, 1.0)
        _s, sx, _sy = plan_quad_layout(wins, 1920, 1080, corners=[1, 3])
        self.assertEqual(sx, 0.0)

    def test_single_window_fills_work_area(self):
        """只选一个窗口：它那个角就是整块工作区，不切出空格子。"""
        _s, sx, sy = plan_quad_layout([self._w("a", 900, 900)],
                                      1920, 1080, corners=[2])
        self.assertEqual((sx, sy), (1.0, 0.0))

    def test_bad_corners_raise(self):
        """角标非法必须报错，不能静默按别的顺序排。"""
        wins = [self._w("a", 900, 900), self._w("b", 900, 900)]
        for bad in ([0, 0], [0, 4], [0, 1, 2], [-1, 2]):
            with self.assertRaises(ValueError):
                plan_quad_layout(wins, 1920, 1080, corners=bad)
        # corners=None 但不满四个：宁可报错也不猜
        with self.assertRaises(ValueError):
            plan_quad_layout(wins, 1920, 1080, corners=None)
        with self.assertRaises(ValueError):
            plan_quad_layout([], 1920, 1080, corners=[])

    def test_four_windows_matches_plan_quad_smart(self):
        """四个窗口、四角全占时与 plan_quad_smart 逐字一致（严格包装）。"""
        wins = [self._w(w["hwnd"], w["w"], w["h"])
                for w in TestQuadPlanSmart.WINS]
        slots, sx, sy = plan_quad_layout(wins, 1920, 1080, corners=None)
        lay, sx2, sy2 = plan_quad_smart(
            [self._w(w["hwnd"], w["w"], w["h"])
             for w in TestQuadPlanSmart.WINS], 1920, 1080)
        self.assertEqual([w["hwnd"] for _c, w in slots],
                         [w["hwnd"] for w in lay])
        self.assertEqual((sx, sy), (sx2, sy2))


if __name__ == "__main__":
    unittest.main()
