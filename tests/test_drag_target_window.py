# -*- coding: utf-8 -*-
"""拖拽吸附目标窗口判定的单测。

回归背景：
旧实现松手吸附时无条件取 `wins = b.list_target_windows(); wins[0]`。
这导致：用户拖动的是窗口 B，松手时却把列表第一个窗口 A（或浮层小窗）
吸到了边缘，真正拖拽的窗口未被响应，出现「窗口乱跳」或「误操作别处窗口」。
现在按拖拽捕获坐标精准匹配正在拖拽的窗口。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MMM_ADVANCED_UI", "1")

try:
    import tkinter as tk
    import ui  # noqa: E402
    import backend as b  # noqa: E402
except Exception:  # noqa: BLE001
    tk = None
    ui = None
    b = None


@unittest.skipIf(tk is None or ui is None, "环境缺少 tkinter 或 ui 无法加载")
class TestDragTargetWindow(unittest.TestCase):

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ui.App(self.root)

    def tearDown(self):
        try:
            self.app._stop_drag_snap()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def test_find_window_at_point(self):
        """精准定位点击坐标所在的窗口。"""
        win_a = {"hwnd": "AppA::WinA", "owner": "AppA", "name": "WinA",
                 "x": 100, "y": 100, "w": 800, "h": 600}
        win_b = {"hwnd": "AppB::WinB", "owner": "AppB", "name": "WinB",
                 "x": 1000, "y": 100, "w": 600, "h": 500}
        windows = [win_a, win_b]

        # 命中 win_a
        found = self.app._find_window_at(200, 200, windows)
        self.assertIsNotNone(found)
        self.assertEqual(found["hwnd"], "AppA::WinA")

        # 命中 win_b
        found = self.app._find_window_at(1200, 300, windows)
        self.assertIsNotNone(found)
        self.assertEqual(found["hwnd"], "AppB::WinB")

        # 空白区域
        found = self.app._find_window_at(50, 50, windows)
        self.assertIsNone(found)

    def test_overlapping_windows_picks_frontmost(self):
        """重叠窗口时，优先取 z 序在前的顶层窗口。"""
        # win_top 在前，win_bottom 在后，两者在 (300, 300) 重叠
        win_top = {"hwnd": "Front::Win", "owner": "Front", "name": "Win",
                   "x": 200, "y": 200, "w": 400, "h": 300}
        win_bottom = {"hwnd": "Back::Win", "owner": "Back", "name": "Win",
                      "x": 100, "y": 100, "w": 600, "h": 500}
        windows = [win_top, win_bottom]

        found = self.app._find_window_at(300, 300, windows)
        self.assertIsNotNone(found)
        self.assertEqual(found["hwnd"], "Front::Win")

    def test_ignores_tiny_and_own_windows(self):
        """忽略自身窗口及小于 160x120 的微型浮层小窗。"""
        tiny = {"hwnd": "Helper::Float", "owner": "Helper", "name": "Float",
                "x": 200, "y": 200, "w": 80, "h": 30}
        own = {"hwnd": "Python::多屏管理器", "owner": "多屏管理器", "name": "主窗",
               "x": 200, "y": 200, "w": 800, "h": 600}
        real = {"hwnd": "Editor::Doc", "owner": "Editor", "name": "Doc",
                "x": 100, "y": 100, "w": 1000, "h": 700}
        windows = [tiny, own, real]

        found = self.app._find_window_at(220, 210, windows)
        self.assertIsNotNone(found)
        # 应该跳过 tiny 和 own，命中 real
        self.assertEqual(found["hwnd"], "Editor::Doc")

    def test_drag_and_drop_snaps_captured_window_not_wins_zero(self):
        """拖拽捕获的是窗口 B 时，松手吸附必须移动窗口 B，而不是处于列表首位的窗口 A。"""
        win_a = {"hwnd": "AppA::WinA", "owner": "AppA", "name": "WinA",
                 "x": 100, "y": 100, "w": 500, "h": 400}
        win_b = {"hwnd": "AppB::WinB", "owner": "AppB", "name": "WinB",
                 "x": 700, "y": 100, "w": 500, "h": 400}
        fake_windows = [win_a, win_b]

        snapped = []

        def fake_set_window_rect(hwnd, x, y, w, h, activate=False):
            snapped.append((hwnd, x, y, w, h))

        # 模拟在 win_b 坐标处按下鼠标（down）
        with mock.patch.object(b, "list_target_windows", return_value=fake_windows), \
                mock.patch.object(b, "set_window_rect", side_effect=fake_set_window_rect):

            # 1. 用户在 win_b 上点下 (750, 150)
            self.app._on_mouse_event("down", 750, 150)
            self.app._process_drag_event()
            self.assertEqual(self.app._drag_target_hwnd, "AppB::WinB")

            # 2. 用户拖入吸附热区
            self.app._drag_zone = (0, 0, 800, 900)

            # 3. 用户松手（up）
            self.app._on_mouse_event("up", 10, 450)
            self.app._process_drag_event()

            # 断言：被吸附移动的一定是 AppB::WinB，而不是列表第一项 AppA::WinA
            self.assertEqual(len(snapped), 1)
            self.assertEqual(snapped[0][0], "AppB::WinB")
            self.assertEqual(snapped[0][1:], (0, 0, 800, 900))


if __name__ == "__main__":
    unittest.main()
