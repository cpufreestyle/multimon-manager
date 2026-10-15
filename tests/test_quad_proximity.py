#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四宫格「就近匹配」的单测：窗口不用横穿屏幕（Moom 4 快照匹配同判据）。

用户报过「智能和非智能模式都不准」「窗口乱跑」。根因之一是分配只看宽高比
契合，本来靠在左上的窗口被甩到右列。proximity 成本（窗口中心到格子中心的
平均距离，权重见 quad_plan.PROX_WEIGHT）补上这块，但它只打破平局——宽高比
契合的结论不许被它颠倒，这里两类场景都钉住。

另一条是坐标系：第二块屏（真机 RV200 Pro 工作区在 (0,-900)）的原点不是
(0,0)，调用方不传 origin 时窗口中心与格子中心不在一个坐标系里，距离排序
整个反掉——「就近」会变成「任选」。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quad_plan import (plan_quad_layout, plan_quad_smart,  # noqa: E402
                       choose_quad_slots, _center, _prox_cost)


def W(hwnd, w, h, x=None, y=None):
    d = {"hwnd": hwnd, "w": w, "h": h}
    if x is not None:
        d["x"], d["y"] = x, y
    return d


class TestProximityKeepsWindowsOnTheirSide(unittest.TestCase):
    """四个同尺寸窗口：契合成本完全打平，只有距离能把它们分开。"""

    WINS = [W("L1", 800, 600, 0, 0), W("L2", 800, 600, 100, 500),
            W("R1", 800, 600, 1120, 0), W("R2", 800, 600, 1200, 500)]

    def test_left_windows_stay_in_left_column(self):
        slots, _sx, _sy = plan_quad_layout(self.WINS, 1920, 1080)
        got = {c: w["hwnd"] for c, w in slots}
        self.assertEqual({got[0], got[2]}, {"L1", "L2"})
        self.assertEqual({got[1], got[3]}, {"R1", "R2"})

    def test_without_geometry_keeps_input_order(self):
        """兼容：窗口字典没有 x/y（容量预检、旧调用方）时行为与从前一致。

        没有几何信息 prox 恒为 0，几种摆法成本打平，第一个候选（输入顺序）
        胜出——既有测试的桩多不带 x/y，靠这条保证不回归。
        """
        bare = [W("L1", 800, 600), W("L2", 800, 600),
                W("R1", 800, 600), W("R2", 800, 600)]
        slots, _sx, _sy = plan_quad_layout(bare, 1920, 1080)
        self.assertEqual([w["hwnd"] for _c, w in slots],
                         ["L1", "L2", "R1", "R2"])


class TestSecondScreenOrigin(unittest.TestCase):
    """第二块屏的工作区原点不是 (0,0)：不传 origin 距离就算错了。"""

    HI = W("HI", 600, 400, 1200, -880)   # 中心 x 1550，远
    LO = W("LO", 600, 400, 150, -420)    # 中心 x 450，近

    def test_origin_puts_far_window_in_top_corner(self):
        slots, _sx, _sy = plan_quad_layout(
            [self.HI, self.LO], 1600, 900, corners=[0, 2], origin=(0, -900))
        self.assertEqual([(c, w["hwnd"]) for c, w in slots],
                         [(0, "HI"), (2, "LO")])

    def test_missing_origin_flips_the_assignment(self):
        """同一场景不传 origin：中心点被算进主屏坐标系，排序整个反掉。

        这条证明 origin 是承重墙——真机 RV200 Pro 工作区在 (0,-900)，调用方
        漏传它「就近」就变成「任选」。
        """
        slots, _sx, _sy = plan_quad_layout(
            [self.HI, self.LO], 1600, 900, corners=[0, 2])
        self.assertEqual([(c, w["hwnd"]) for c, w in slots],
                         [(0, "LO"), (2, "HI")])


class TestProximityNeverOverridesFit(unittest.TestCase):
    """权重只够打破平局：宽高比契合的结论不许被距离颠倒。"""

    # 四个窗口全都放在「契合结论的反面」：宽窗在左、竖窗在右
    WINS = [{"hwnd": "chat", "w": 1500, "h": 900, "x": 0, "y": 0},
            {"hwnd": "term", "w": 400, "h": 900, "x": 1500, "y": 0},
            {"hwnd": "code", "w": 1200, "h": 700, "x": 0, "y": 500},
            {"hwnd": "doc", "w": 700, "h": 1000, "x": 1500, "y": 500}]

    def test_widest_still_goes_right_column(self):
        lay, _sx, _sy = plan_quad_smart(self.WINS, 1920, 1080)
        self.assertEqual({lay[1]["hwnd"], lay[3]["hwnd"]}, {"chat", "code"})

    def test_tallest_still_goes_left_column(self):
        lay, _sx, _sy = plan_quad_smart(self.WINS, 1920, 1080)
        self.assertEqual({lay[0]["hwnd"], lay[2]["hwnd"]}, {"term", "doc"})


class TestProxCostUnits(unittest.TestCase):
    def test_center_none_without_geometry(self):
        self.assertIsNone(_center({"w": 100, "h": 100}))
        self.assertIsNone(_center({"x": "a", "y": 1, "w": 10, "h": 10}))
        self.assertIsNone(_center({"x": 0, "y": 0, "w": 0, "h": 10}))
        self.assertIsNone(_center(None))

    def test_near_assignment_costs_less(self):
        near = {"x": 0, "y": 0, "w": 100, "h": 100}
        far = {"x": 900, "y": 0, "w": 100, "h": 100}
        centers = {0: (50, 50), 1: (150, 50)}
        good = _prox_cost([(near, 0), (far, 1)], centers, 200.0)
        bad = _prox_cost([(far, 0), (near, 1)], centers, 200.0)
        self.assertLess(good, bad)

    def test_window_without_geometry_does_not_dilute(self):
        """取平均不取求和：没几何的窗口不该把整项成本稀释掉。"""
        near = {"x": 0, "y": 0, "w": 100, "h": 100}
        far = {"x": 900, "y": 0, "w": 100, "h": 100}
        blind = {"w": 100, "h": 100}
        centers = {0: (50, 50), 1: (150, 50)}
        base = _prox_cost([(near, 0), (far, 1)], centers, 200.0)
        with_blind = _prox_cost([(near, 0), (far, 1), (blind, 2)],
                                centers, 200.0)
        self.assertEqual(base, with_blind)


class TestChooseQuadSlotsKeepsPickedCorners(unittest.TestCase):
    """子集选择里「用户点选的角」优先于就近——点掉的格就是不要那些角。"""

    POOL = [("L1", W("L1", 800, 600, 0, 0)),
            ("L2", W("L2", 800, 600, 100, 500)),
            ("R1", W("R1", 800, 600, 1120, 0)),
            ("R2", W("R2", 800, 600, 1200, 500))]
    MINS = {h: (100, 100) for h, _w in POOL}

    def test_four_picked_corners_are_not_swapped(self):
        slots, unplaced = choose_quad_slots(
            self.POOL, self.MINS, 1920, 1080, 12, False,
            corners=[0, 1, 2, 3], origin=(0, 0))
        self.assertEqual([(c, h) for c, h, _w in slots],
                         [(0, "L1"), (1, "L2"), (2, "R1"), (3, "R2")])
        self.assertEqual(unplaced, [])

    def test_two_picked_corners_keep_their_windows(self):
        slots, _un = choose_quad_slots(
            self.POOL[:2], {h: (100, 100) for h, _w in self.POOL[:2]},
            1920, 1080, 12, False, corners=[0, 1], origin=(0, 0))
        self.assertEqual([(c, h) for c, h, _w in slots],
                         [(0, "L1"), (1, "L2")])
