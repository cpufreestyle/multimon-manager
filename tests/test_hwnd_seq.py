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

    def test_falls_back_to_name_when_geometry_mismatch(self):
        """几何对不上（窗口在副屏）时应改用窗口名匹配，不能回退 window 1。

        真机 Chrome 的 EchoID 主窗口在 y=-870 副屏，CG 几何与 AX 差 978px；
        若回退 window 1，几何会写到「要保存密码吗」对话框上，主窗口看着
        完全推不动。
        """
        calls = []

        def _osa(script):
            calls.append(script)
            if "position of w" in script:
                # 主窗口在副屏，几何与目标差很远
                return "0,-870,1600,870;0,0,448,394"
            if "name of w" in script:
                return "要保存密码吗？;EchoID - 录音 - Google Chrome"
            return ""

        wm._osa = _osa
        sel = wm._ax_window_by_geometry("Google Chrome", (0, 33, 906, 447),
                                        name="EchoID")
        self.assertEqual(sel, "window 2")

    def test_name_match_prefers_longer_name(self):
        """多个窗口名都包含目标时，取更具体的那个。"""

        def _osa(script):
            if "name of w" in script:
                return "a;ab;abc"
            return ""

        wm._osa = _osa
        # 三个都命中（互相包含），取最具体的 abc = window 3
        self.assertEqual(wm._ax_window_by_name("App", "ab"), "window 3")

    def test_negative_coords_match_exactly(self):
        """副屏窗口的 x/y 是负数，必须原样比较，不能取绝对值。

        真机 ChatGPT 主窗口在 RV200 Pro（y=-870），CG 与 AX 都是
        (0,-870,793,870)。旧实现对 AppleScript 返回值取 abs()，目标 y=-870
        与实测 y=870 相差 1740，偏差阈值判定失效，副屏窗口全部退化成按序号
        选窗——几何写到别的窗口上，表现为「四格不准 / 没吸附到边上」。
        """
        self._set_ax("0,-870,793,870;600,-870,1000,640")
        sel = wm._ax_window_by_geometry("ChatGPT", (0, -870, 793, 870))
        self.assertEqual(sel, "window 1")

    def test_negative_coords_pick_closest(self):
        """同为负坐标时，按偏差最小挑选，不被符号干扰。"""
        self._set_ax("0,-870,793,870;600,-870,1000,640")
        sel = wm._ax_window_by_geometry("爱思助手", (600, -870, 1000, 640))
        self.assertEqual(sel, "window 2")

    def test_negative_coords_still_reject_far_match(self):
        """副屏几何对不上时依然走窗口名兜底，不会误配成正坐标的那个。"""
        calls = []

        def _osa(script):
            calls.append(script)
            if "position of w" in script:
                return "0,-870,793,870;600,-870,1000,640"
            if "name of w" in script:
                return "主窗口;小窗"
            return ""

        wm._osa = _osa
        sel = wm._ax_window_by_geometry("App", (0, 33, 906, 447), name="主窗口")
        self.assertEqual(sel, "window 1")

    def test_thousands_separator_does_not_break_parse(self):
        """坐标过千时 as text 会插入千位分隔符，不能按逗号切分。

        真机日志见过 "546, ,, -870, ,, 1054, ,, 870" 这种 8 段输出：split(",")
        解析直接抛 ValueError，几何读取报废，四宫格退化成按序号选窗。
        """
        self._set_ax("546, ,, -870, ,, 1054, ,, 870;0,0,448,394")
        sel = wm._ax_window_by_geometry("App Store", (546, -870, 1054, 870))
        self.assertEqual(sel, "window 1")

    def test_parse_nums_extracts_signed_ints(self):
        self.assertEqual(wm._parse_nums("546,1,054,-870,870"),
                         [546, 1, 54, -870, 870])
        self.assertEqual(wm._parse_nums("546, ,, -870, ,, 1054, ,, 870"),
                         [546, -870, 1054, 870])
        self.assertEqual(wm._parse_nums(""), [])
        self.assertEqual(wm._parse_nums(None), [])

    def test_name_match_single_window(self):
        """整个应用只有一个窗口，名字再不像也只能是它。"""

        def _osa(script):
            if "name of w" in script:
                return "totally different"
            return ""

        wm._osa = _osa
        self.assertEqual(wm._ax_window_by_name("App", "Wanted"), "window 1")

    def test_name_match_empty_name_returns_none(self):
        def _osa(script):
            return "whatever"

        wm._osa = _osa
        self.assertIsNone(wm._ax_window_by_name("App", ""))

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
