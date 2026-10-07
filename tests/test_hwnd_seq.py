#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同名多窗口 hwnd 序号机制的单测（mock，不依赖真机窗口）。

背景：UU远程 这类应用的所有窗口同名（多为空串），旧实现用「应用::窗口名」
当唯一标识，枚举时同名去重只剩 1 个，且 AppleScript 永远写 window 1——
表现为「只显示一个窗口 / 自动排列不准」。现改为 hwnd 带 #n 序号
（同名第 N 个，z 序），同名窗口各自可寻址。

另覆盖 _ax_window_by_geometry：CG 枚举序与 AppleScript 的 AX 窗口序不一致时
（QClaw 的 window 1 是 430x40 悬浮小条、window 2 才是主窗口），按序号选窗会
把几何写到别的窗口上，实测表现为「该窗口完全推不动」。改为按已知几何在 AX
列表里匹配 window N。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402


class TestAxWindowByGeometry(unittest.TestCase):
    AX_OUT = "0,0,800,600;50,50,900,700"

    def setUp(self):
        self._osa = wm._osa

    def tearDown(self):
        wm._osa = self._osa

    def _set_ax(self, text):
        calls = []

        def _osa(script):
            calls.append(script)
            return text

        wm._osa = _osa
        return calls

    def test_matches_closest_window(self):
        """按已知几何匹配到 AX 里几何最接近的那个窗口。"""
        self._set_ax(self.AX_OUT)
        sel = wm._ax_window_by_geometry("QClaw", (50, 50, 900, 700))
        self.assertEqual(sel, "window 2")

    def test_returns_none_when_deviation_too_large(self):
        """几何已变（偏差 > 64）时不信匹配，交给调用方回退。"""
        self._set_ax(self.AX_OUT)
        self.assertIsNone(wm._ax_window_by_geometry("QClaw", (900, 900, 100, 100)))

    def test_returns_none_on_empty_rect(self):
        self._set_ax(self.AX_OUT)
        self.assertIsNone(wm._ax_window_by_geometry("QClaw", (0, 0, 0, 0)))

    def test_returns_none_when_ax_gives_nothing(self):
        self._set_ax("")
        self.assertIsNone(wm._ax_window_by_geometry("QClaw", (50, 50, 900, 700)))

    def test_selector_still_works_without_geometry(self):
        """没有已知几何时仍按 hwnd 序号选窗（旧行为保留）。"""
        wm._last_rect.clear()
        self.assertEqual(wm._ax_window_selector("App::Win"), "window 1")
        self.assertEqual(wm._ax_window_selector("App::Win#3"), "window 3")


def _fake_wins():
    """模拟 UU远程：同一进程 3 个同名（空名）窗口 + 1 个有别名的窗口。"""
    return [
        {"pid": 100, "owner": "UU远程", "name": "", "wid": 11,
         "x": 0, "y": 0, "w": 800, "h": 600},
        {"pid": 100, "owner": "UU远程", "name": "", "wid": 12,
         "x": 50, "y": 50, "w": 800, "h": 600},
        {"pid": 100, "owner": "UU远程", "name": "", "wid": 13,
         "x": 100, "y": 100, "w": 800, "h": 600},
        {"pid": 100, "owner": "UU远程", "name": "设置", "wid": 14,
         "x": 200, "y": 200, "w": 400, "h": 300},
    ]


class TestHwndSeq(unittest.TestCase):
    def setUp(self):
        self._orig = wm.list_windows_front_to_back
        wm.list_windows_front_to_back = _fake_wins
        wm._last_rect.clear()

    def tearDown(self):
        wm.list_windows_front_to_back = self._orig
        wm._last_rect.clear()

    def test_no_dedup_same_name(self):
        """同名窗口不再被去重吞掉，每个都有独立 hwnd。"""
        tws = [w for w in wm.list_target_windows() if w["owner"] == "UU远程"]
        self.assertEqual(len(tws), 4)
        hwnds = [w["hwnd"] for w in tws]
        self.assertEqual(len(set(hwnds)), 4, "hwnd 必须互不相同")
        self.assertIn("UU远程::", hwnds)          # 首个不带序号（旧格式兼容）
        self.assertIn("UU远程::#2", hwnds)
        self.assertIn("UU远程::#3", hwnds)
        self.assertIn("UU远程::设置", hwnds)      # 唯一名不带序号

    def test_parse_hwnd(self):
        self.assertEqual(wm.parse_hwnd("App::Win"), ("App", "Win", None))
        self.assertEqual(wm.parse_hwnd("App::Win#3"), ("App", "Win", 3))
        self.assertEqual(wm.parse_hwnd("App::"), ("App", "", None))
        self.assertEqual(wm.parse_hwnd("App::#2"), ("App", "", 2))
        # 标题本身含 # 且尾部不是数字：原样保留
        self.assertEqual(wm.parse_hwnd("App::C#笔记"), ("App", "C#笔记", None))

    def test_find_window_by_seq(self):
        """带序号的 hwnd 能找到对应那个同名窗口（按 z 序）。"""
        w2 = wm._find_window("UU远程::#2")
        self.assertIsNotNone(w2)
        self.assertEqual(w2["wid"], 12, "应取 z 序第 2 个同名窗口")
        w1 = wm._find_window("UU远程::")
        self.assertEqual(w1["wid"], 11)

    def test_get_window_rect_by_seq(self):
        """带序号的 hwnd 读到的是对应窗口的几何，而非永远 window 1。"""
        r2 = wm.get_window_rect("UU远程::#3")
        self.assertEqual(r2, (100, 100, 800, 600))
        r0 = wm.get_window_rect("UU远程::设置")
        self.assertEqual(r0, (200, 200, 400, 300))

    def test_ax_selector(self):
        self.assertEqual(wm._ax_window_selector("App::Win"), "window 1")
        self.assertEqual(wm._ax_window_selector("App::Win#4"), "window 4")

    def test_make_hwnd(self):
        self.assertEqual(wm.make_hwnd("A", "B"), "A::B")
        self.assertEqual(wm.make_hwnd("A", "B", 1), "A::B")
        self.assertEqual(wm.make_hwnd("A", "", 2), "A::#2")


if __name__ == "__main__":
    unittest.main()
