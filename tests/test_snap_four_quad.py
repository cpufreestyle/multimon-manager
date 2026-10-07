#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""snap_four_quad（2×2 四宫格排列）单测：mock 窗口与显示器，不碰真机。

横屏四宫格就是上下左右四等分：屏幕工作区按宽/高各切一半，四个矩形无缝拼满
（宽高为奇数时，右列/下排用减法补齐，避免出现 1px 缝隙）。这与单窗的
quad-tl/tr/bl/br 吸附是同一套几何，区别是一次摆四个窗口。

只覆盖 windows_mac（本仓库现有单测的既有前提）：windows.py 依赖
ctypes.windll，在 macOS 上无法导入。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402


class _Mon:
    def __init__(self, left=0, top=0, width=1920, height=1080, name="M1"):
        self.left, self.top = left, top
        self.width, self.height = width, height
        self.is_primary = True
        self.device_name = name
        self.device_path = name
        self.work_rect = (left, top, width, height)


def _win(hwnd, x=0, y=0, w=100, h=100):
    return {"hwnd": hwnd, "owner": hwnd.split("::")[0],
            "name": hwnd.split("::")[1], "x": x, "y": y, "w": w, "h": h,
            "pid": 99}


def _run(wins, mons, **kw):
    """把窗口查找 / 显示器枚举替换成假的，返回 (ok, placed)。

    readback: {hwnd: 读回矩形}，用于模拟「系统/App 钳制」——写入目标值后
    读回的可能是别的尺寸（部分 App 有最小窗口高度）。未列出的窗口按
    写入值原样读回。
    """
    placed = []
    prev = {}
    for attr in ("list_target_windows", "monitors", "_find_window",
                 "get_window_rect", "set_window_rect", "push_undo"):
        prev[attr] = getattr(wm, attr)

    class _Mons:
        def enum_monitors(self):
            return mons

    readback = kw.pop("readback", {})
    state = {}

    def _rect(hwnd, *a, **k):
        return readback.get(hwnd) or state.get(hwnd) or (0, 0, 0, 0)

    wm.monitors = _Mons()
    wm.list_target_windows = lambda: wins
    wm._find_window = lambda hwnd: next(
        (w for w in wins if w["hwnd"] == hwnd), None)
    wm.get_window_rect = _rect
    wm.push_undo = lambda *a, **k: None

    def _place(hwnd, x, y, w, h, activate=True):
        placed.append((hwnd, x, y, w, h))
        state[hwnd] = (x, y, w, h)
        return True

    wm.set_window_rect = _place
    try:
        ok = wm.snap_four_quad(**kw)
    finally:
        for k, v in prev.items():
            setattr(wm, k, v)
    return ok, placed


class TestSnapFourQuad(unittest.TestCase):
    WINS = [_win("A::1", x=10, y=20), _win("B::1", x=2000, y=30),
            _win("C::1", x=10, y=2000), _win("D::1", x=3000, y=20)]

    def test_auto_four_windows_fill_2x2(self):
        ok, placed = _run(list(self.WINS), [_Mon()])
        self.assertTrue(ok)
        wl, wt, ww, wh = 0, 0, 1920, 1080
        hw, hh = ww // 2, wh // 2
        # 下排按实测闭环起点摆放：读回即理想半高，故 bot_y = wt + hh + 1
        bot_y = wt + hh + 1
        self.assertEqual(placed, [
            ("A::1", wl, wt, hw, hh),
            ("B::1", wl + hw, wt, ww - hw, hh),
            ("C::1", wl, bot_y, hw, (wt + wh) - bot_y),
            ("D::1", wl + hw, bot_y, ww - hw, (wt + wh) - bot_y),
        ])

    def test_odd_geometry_no_seam(self):
        """宽高为奇数时四个矩形必须无缝拼满，不留 1px 缝隙。"""
        ok, placed = _run(list(self.WINS), [_Mon(width=1921, height=1081)])
        self.assertTrue(ok)
        # 左列右边缘必须贴住右列左边缘，否则两个窗口中间露缝
        self.assertEqual(placed[0][1] + placed[0][3], placed[1][1])
        # 两排不能重叠：下排顶边必须在上排底边之下（闭环留 1px 间隔）
        self.assertGreater(placed[2][2], placed[0][2] + placed[0][4])
        # 右下角必须精确落在工作区右下角
        self.assertEqual(placed[3][1] + placed[3][3], 1921)
        self.assertEqual(placed[3][2] + placed[3][4], 1081)
        # 上排读回即理想半高时，下排起点应为上排底边 + 1
        self.assertEqual(placed[2][2], placed[0][2] + placed[0][4] + 1)

    def test_explicit_hwnds(self):
        ok, placed = _run(list(self.WINS), [_Mon()],
                          tl_hwnd="D::1", tr_hwnd="C::1",
                          bl_hwnd="B::1", br_hwnd="A::1")
        self.assertTrue(ok)
        self.assertEqual([p[0] for p in placed],
                         ["D::1", "C::1", "B::1", "A::1"])

    def test_insufficient_windows(self):
        ok, placed = _run(list(self.WINS)[:3], [_Mon()])
        self.assertFalse(ok)
        self.assertEqual(placed, [], "窗口不足四个时不应摆放任何窗口")

    def test_monitor_offset(self):
        """指定非主屏时，四宫格应落在该屏工作区内。"""
        ok, placed = _run(list(self.WINS),
                          [_Mon(), _Mon(left=1920, top=0, name="M2")],
                          monitor=1)
        self.assertTrue(ok)
        wl, wt, ww, wh = 1920, 0, 1920, 1080
        bot_y = wt + wh // 2 + 1
        self.assertEqual(placed[0], ("A::1", wl, wt, ww // 2, wh // 2))
        self.assertEqual(placed[3],
                         ("D::1", wl + ww // 2, bot_y,
                          ww - ww // 2, (wt + wh) - bot_y))

    def test_bottom_row_starts_below_measured_top(self):
        """上排有最小高度缩不下去时，下排必须贴着实测底边走，不能重叠。"""
        wl, wt, ww, wh = 0, 0, 1920, 1080
        half_h = wh // 2
        ok, placed = _run(
            list(self.WINS), [_Mon()],
            # 左上/右上被系统钳制在 600 / 570 高（真机实测值）
            readback={"A::1": (wl, wt, ww // 2, 600),
                      "B::1": (wl + ww // 2, wt, ww - ww // 2, 570)})
        self.assertTrue(ok)
        # 上排仍按理想半高写入（有权重高的窗口优先保证位置）
        self.assertEqual(placed[0], ("A::1", wl, wt, ww // 2, half_h))
        self.assertEqual(placed[1],
                         ("B::1", wl + ww // 2, wt, ww - ww // 2, half_h))
        # 下排起点 = 实测最底边 + 1，两排不能重叠
        bot_y = max(wt + 600, wt + 570) + 1
        self.assertEqual(placed[2], ("C::1", wl, bot_y, ww // 2,
                                     (wt + wh) - bot_y))
        self.assertEqual(placed[3], ("D::1", wl + ww // 2, bot_y,
                                      ww - ww // 2, (wt + wh) - bot_y))
        # 关键：下排顶边必须在上排实测底边之下
        self.assertGreater(placed[2][2], max(wt + 600, wt + 570))

    def test_clamped_when_readback_insane(self):
        """读回值异常（如读到别的窗口/0）时，下排仍必须留在屏内。"""
        ok, placed = _run(list(self.WINS), [_Mon()],
                          readback={"A::1": (0, 0, 0, 0)})
        self.assertTrue(ok)
        wt, wh = 0, 1080
        self.assertLessEqual(placed[2][2], max(wt + 1, (wt + wh) - 1))
        self.assertGreaterEqual(placed[2][2], wt + 1)
        self.assertGreaterEqual(placed[2][3], 1)


if __name__ == "__main__":
    unittest.main()
