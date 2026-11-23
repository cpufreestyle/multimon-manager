#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""windows.snap_two_side_by_side 的回归测试。

这个函数曾经在取到 work_rect 之后多写了一句 `sx, sy = split_x, split_y`
——那是 snap_four_quad 的参数名，本模块里并不存在。于是每次点「并排左右」
都在摆放任何窗口之前抛 NameError，Tk 只往 stderr 打一个 traceback，状态栏
文字不变，用户看到的就是「按钮点了没反应」。

这里把整条调用链（枚举显示器 / 列窗口 / 摆放窗口）都换成假的，确认函数能
一路走到摆放并返回 True，且横屏对半切、竖屏改上下堆叠（与 macOS 侧一致，
也是界面文案承诺的行为）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows  # noqa: E402


class _Mon:
    def __init__(self, width=1920, height=1080, name="\\\\.\\DISPLAY1"):
        self.left, self.top = 0, 0
        self.width, self.height = width, height
        self.is_primary = True
        self.device_name = name
        self.device_path = name
        self.work_rect = (0, 0, width, height)


class _FakeMonitors:
    def __init__(self, mons):
        self._mons = mons

    def enum_monitors(self):
        return list(self._mons)


def _win(hwnd, x=0, y=0, w=400, h=300):
    return {"hwnd": hwnd, "pid": hwnd, "owner": "app%d" % hwnd,
            "name": "win%d" % hwnd, "x": x, "y": y, "w": w, "h": h}


class SnapTwoSideBySideTest(unittest.TestCase):
    def setUp(self):
        self._mon_mod = windows.monitors
        self._list = windows.list_windows_front_to_back
        self._place = windows._place_window
        self.placed = []
        windows.monitors = _FakeMonitors([_Mon()])
        windows.list_windows_front_to_back = lambda **kw: [
            _win(11, 0, 0, 400, 300), _win(22, 400, 0, 400, 300)]
        windows._place_window = (
            lambda hwnd, x, y, w, h: self.placed.append((hwnd, x, y, w, h))
            or True)

    def tearDown(self):
        windows.monitors = self._mon_mod
        windows.list_windows_front_to_back = self._list
        windows._place_window = self._place

    def test_landscape_splits_work_rect_in_half(self):
        self.assertTrue(windows.snap_two_side_by_side())
        # 奇数宽时右半用减法补齐，两块无缝拼满工作区
        self.assertEqual(self.placed,
                         [(11, 0, 0, 960, 1080), (22, 960, 0, 960, 1080)])

    def test_odd_width_has_no_gap(self):
        windows.monitors = _FakeMonitors([_Mon(width=1921)])
        self.assertTrue(windows.snap_two_side_by_side())
        (_, lx, _, lw, _), (_, rx, _, rw, _) = self.placed
        self.assertEqual(lx, 0)
        self.assertEqual(lx + lw, rx)
        self.assertEqual(rx + rw, 1921)

    def test_portrait_stacks_top_and_bottom(self):
        windows.monitors = _FakeMonitors([_Mon(width=1080, height=1920)])
        self.assertTrue(windows.snap_two_side_by_side())
        self.assertEqual(self.placed,
                         [(11, 0, 0, 1080, 960), (22, 0, 960, 1080, 960)])

    def test_explicit_hwnds_are_used(self):
        self.assertTrue(windows.snap_two_side_by_side(left_hwnd=22,
                                                       right_hwnd=11))
        self.assertEqual([p[0] for p in self.placed], [22, 11])

    def test_single_window_returns_false(self):
        windows.list_windows_front_to_back = lambda **kw: [_win(11)]
        self.assertFalse(windows.snap_two_side_by_side())
        self.assertEqual(self.placed, [])

    def test_no_windows_returns_false(self):
        windows.list_windows_front_to_back = lambda **kw: []
        self.assertFalse(windows.snap_two_side_by_side())
        self.assertEqual(self.placed, [])


if __name__ == "__main__":
    unittest.main()
