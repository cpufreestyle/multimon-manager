#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四宫格贴角契约（windows_mac._quad_anchor）的单测。

窗口被 App 最小尺寸顶大后，锚定边必须贴住**让出间距后的目标矩形**的边——
照原始格子贴会把屏幕边缝（edge_gap）吃成 0，用户看到的就是「没吸附到边上」。
溢出只允许发生在远离锚定角的那一侧（吃窗口间的缝），且不许越出工作区：
越界会被 macOS 把整个窗口判到别的屏（真机实测 ChatGPT 被搬到 RV200 Pro）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402


class TestQuadAnchor(unittest.TestCase):
    # 工作区 (0, 0, 1000, 800)
    WL, WT, WW, WH = 0, 0, 1000, 800

    def anchor(self, corner, target, size):
        fx, fy, fw, fh = target
        gw, gh = size
        return wm._quad_anchor(corner, fx, fy, fw, fh, gw, gh,
                               self.WL, self.WT, self.WW, self.WH)

    def test_exact_size_lands_on_target_corner(self):
        for corner in range(4):
            self.assertEqual(self.anchor(corner, (100, 100, 400, 300),
                                         (400, 300)),
                             (100, 100))

    def test_oversized_window_keeps_anchor_edge(self):
        # 右上：右缘贴目标右缘、上缘贴目标上缘，溢出只去左边（吃缝）
        self.assertEqual(self.anchor(1, (400, 0, 400, 400), (600, 400)),
                         (200, 0))
        # 左上：左缘贴目标左缘，溢出只去右边
        self.assertEqual(self.anchor(0, (0, 0, 400, 400), (600, 400)),
                         (0, 0))
        # 右下：右缘、下缘都贴目标，溢出只去左上
        self.assertEqual(self.anchor(3, (400, 400, 400, 400), (600, 600)),
                         (200, 200))
        # 左下：左缘贴目标、下缘贴目标，溢出只去上边
        self.assertEqual(self.anchor(2, (0, 400, 400, 400), (600, 600)),
                         (0, 200))

    def test_oversized_window_never_leaves_workspace(self):
        cases = [(1, (700, 0, 300, 400), (600, 400)),
                 (3, (400, 500, 400, 300), (600, 600)),
                 (2, (0, 400, 400, 400), (600, 600))]
        for corner, target, size in cases:
            ax, ay = self.anchor(corner, target, size)
            self.assertGreaterEqual(ax, self.WL, "左缘越出工作区")
            self.assertGreaterEqual(ay, self.WT, "上缘越出工作区")
            self.assertLessEqual(ax + size[0], self.WW, "右缘越出工作区")
            self.assertLessEqual(ay + size[1], self.WH, "下缘越出工作区")

    def test_anchor_past_workspace_edge_is_clamped(self):
        """照目标右缘锚定会把窗口推出工作区左缘：钳回工作区内。

        不钳的结局是 ax 为负——macOS 把越界的窗口判到别的屏，正是「四个
        窗口不在一个屏幕上」的搬运路径。
        """
        self.assertEqual(self.anchor(1, (100, 0, 400, 400), (900, 400)),
                         (0, 0))
        self.assertEqual(self.anchor(0, (100, 100, 400, 400), (1400, 400)),
                         (0, 100))

    def test_edge_gap_is_not_eaten(self):
        """屏幕边留了缝（edge_gap）：锚定边贴让缝后的目标边，不是屏幕边。

        目标矩形 (6, 6, ...) 是让出半条缝后的值；窗口被顶大时左缘必须停在
        6，而不是被按到 0——那条缝被吃成 0 正是「没吸附到边上」的由来。
        """
        self.assertEqual(self.anchor(0, (6, 6, 488, 394), (600, 500)),
                         (6, 6))
        # 右下格：右缘贴目标右缘（1000-6）、下缘贴目标下缘（800-6）
        self.assertEqual(self.anchor(3, (506, 400, 488, 394), (600, 500)),
                         (394, 294))
