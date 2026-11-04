#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨屏铺满：竖屏智能避让（不适合竖屏的窗口不发到竖屏）、一屏4窗（2×2四宫格）与手动选择映射单测。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm


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


def _win(hwnd, x=0, y=0, w=100, h=100):
    return {"hwnd": hwnd, "owner": hwnd.split("::")[0],
            "name": hwnd.split("::")[1] if "::" in hwnd else "win",
            "x": x, "y": y, "w": w, "h": h, "pid": 99}


def _run_fill(wins, mons, **kw):
    placed = []
    prev = {}
    for attr in ("list_target_windows", "monitors", "_find_window",
                 "get_window_rect", "set_window_rect", "push_undo"):
        prev[attr] = getattr(wm, attr)

    class _Mons:
        def enum_monitors(self):
            return mons

    wm.monitors = _Mons()
    wm.list_target_windows = lambda: wins
    wm._find_window = lambda hwnd: next(
        (w for w in wins if w["hwnd"] == hwnd), None)
    wm.get_window_rect = lambda hwnd, *a, **k: next(
        ((w["x"], w["y"], w["w"], w["h"]) for w in wins if w["hwnd"] == hwnd),
        (0, 0, 0, 0))
    wm.push_undo = lambda *a, **k: None

    def _place(hwnd, x, y, w, h, activate=True):
        placed.append((hwnd, x, y, w, h))
        return True

    wm.set_window_rect = _place
    try:
        done = wm.fill_all_monitors(**kw)
    finally:
        for k, v in prev.items():
            setattr(wm, k, v)
    return done, placed


class TestSpreadPortraitAvoidance(unittest.TestCase):
    """验证不适合竖屏的横向宽窗绝不发往竖屏，并支持纵向窗口与一屏4窗。"""

    def setUp(self):
        # M_land: 横屏 1920x1080; M_port: 竖屏 1080x1920
        self.m_land = _Mon(0, 0, 1920, 1080, name="横屏1")
        self.m_port = _Mon(1920, 0, 1080, 1920, name="竖屏2")

    def test_is_window_portrait_suitable_heuristic(self):
        """测试横纵比判断：宽/高 >= 1.15 为横屏宽窗，不适合竖屏。"""
        wide = _win("Browser::1", w=1400, h=900)  # aspect 1.55
        tall = _win("WeChat::1", w=450, h=800)    # aspect 0.56
        square = _win("Square::1", w=500, h=500)  # aspect 1.0

        self.assertFalse(wm.is_window_portrait_suitable(wide, self.m_port))
        self.assertTrue(wm.is_window_portrait_suitable(tall, self.m_port))
        self.assertTrue(wm.is_window_portrait_suitable(square, self.m_port))

    def test_wide_window_not_sent_to_portrait_screen(self):
        """仅有竖屏且窗口全为宽窗时，自动避让，不发往竖屏。"""
        wins = [_win("Browser::1", w=1400, h=900)]
        mons = [self.m_port]

        done, placed = _run_fill(wins, mons, filter_portrait=True)
        self.assertEqual(done, 0)
        self.assertEqual(placed, [])

    def test_smart_routing_landscape_and_portrait(self):
        """横竖双屏时，智能把宽窗留给横屏，纵向窄窗送往竖屏。"""
        # wins 顺序故意让宽窗在后、窄窗在前
        narrow = _win("Chat::1", w=400, h=800)
        wide = _win("IDE::1", w=1600, h=900)
        wins = [narrow, wide]
        mons = [self.m_land, self.m_port]

        done, placed = _run_fill(wins, mons, filter_portrait=True)
        self.assertEqual(done, 2)
        # 竖屏应该拿到窄窗，横屏应该拿到宽窗
        placed_map = {hwnd: (x, y, w, h) for hwnd, x, y, w, h in placed}
        self.assertIn("IDE::1", placed_map)
        self.assertIn("Chat::1", placed_map)
        # IDE::1 在横屏上 (x=0)
        self.assertEqual(placed_map["IDE::1"][0], 0)
        self.assertEqual(placed_map["IDE::1"][2], 1920)
        # Chat::1 在竖屏上 (x=1920)
        self.assertEqual(placed_map["Chat::1"][0], 1920)
        self.assertEqual(placed_map["Chat::1"][2], 1080)

    def test_single_screen_four_quad_operation(self):
        """一个屏幕4个窗口：mode='four_quad' 排满 2×2 四等分。"""
        wins = [_win(f"W::{i}", w=600, h=400) for i in range(1, 6)]
        mons = [self.m_land]

        done, placed = _run_fill(wins, mons, mode="four_quad")
        self.assertEqual(done, 4)
        self.assertEqual(len(placed), 4)

        # 检查 4 个角的位置无缝拼接 1920x1080
        hwnds = [p[0] for p in placed]
        self.assertEqual(hwnds, ["W::1", "W::2", "W::3", "W::4"])
        # 左上: 0, 0, 960, 540
        self.assertEqual(placed[0], ("W::1", 0, 0, 960, 540))
        # 右上: 960, 0, 960, 540
        self.assertEqual(placed[1], ("W::2", 960, 0, 960, 540))
        # 左下: 0, 540, 960, 540
        self.assertEqual(placed[2], ("W::3", 0, 540, 960, 540))
        # 右下: 960, 540, 960, 540
        self.assertEqual(placed[3], ("W::4", 960, 540, 960, 540))

    def test_multi_screen_four_quad(self):
        """多屏每屏 4 窗：两个横屏分别排 4 个窗口（共排8个）。"""
        m_land2 = _Mon(1920, 0, 1920, 1080, name="横屏2")
        wins = [_win(f"W::{i}", w=600, h=400) for i in range(1, 10)]
        mons = [self.m_land, m_land2]

        done, placed = _run_fill(wins, mons, mode="four_quad")
        self.assertEqual(done, 8)
        self.assertEqual(len(placed), 8)
        # 前 4 个在 M1，后 4 个在 M2
        self.assertEqual([p[0] for p in placed[:4]], ["W::1", "W::2", "W::3", "W::4"])
        self.assertEqual([p[0] for p in placed[4:]], ["W::5", "W::6", "W::7", "W::8"])
        self.assertEqual(placed[4][1], 1920)  # x
        self.assertEqual(placed[4][2], 0)     # y

    def test_manual_assignments(self):
        """手动选择模式：精确指定每块屏的窗口映射。"""
        wins = [_win("W::1"), _win("W::2"), _win("W::3")]
        mons = [self.m_land, self.m_port]
        # 手动将 W::3 分配到屏0，W::1 分配到屏1
        assignments = {0: "W::3", 1: "W::1"}

        done, placed = _run_fill(wins, mons, assignments=assignments)
        self.assertEqual(done, 2)
        self.assertEqual(placed[0], ("W::3", 0, 0, 1920, 1080))
        self.assertEqual(placed[1], ("W::1", 1920, 0, 1080, 1920))


if __name__ == "__main__":
    unittest.main()
