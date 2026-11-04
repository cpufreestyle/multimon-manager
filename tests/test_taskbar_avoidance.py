# -*- coding: utf-8 -*-
"""多屏任务栏避让单测：分屏与平铺时工作区必须避开任务栏。

回归背景：
多屏任务栏为无边框置顶条（overrideredirect + topmost）。
若不从显示器可用工作区（work_rect）中扣除其高度：
1. 底部位置：窗口底边会排入任务栏下方（重叠），导致无法操作底边或拖拽窗口。
2. 顶部位置：任务栏位于 mon.top + 32..62，正好压住贴顶窗口的标题栏，导致窗口无法拖动。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitors_mac as mm  # noqa: E402
import taskbar  # noqa: E402


class _FakeMonitor:
    def __init__(self, rect, work_rect=None, name="M1"):
        self.left, self.top, self.width, self.height = rect
        if work_rect is None:
            work_rect = rect
        self.work_left, self.work_top, self.work_width, self.work_height = work_rect
        self.device_name = name
        self.device_path = name

    @property
    def work_rect(self):
        return (self.work_left, self.work_top, self.work_width, self.work_height)


class TestTaskbarAvoidance(unittest.TestCase):

    def test_disabled_taskbar_keeps_work_rect(self):
        """任务栏未启用时，工作区不受任何影响。"""
        m = _FakeMonitor((0, 0, 1600, 900), (0, 25, 1600, 875))
        mm._apply_taskbar_avoidance([m], enabled=False, position="bottom")
        self.assertEqual(m.work_rect, (0, 25, 1600, 875))

    def test_bottom_taskbar_trims_work_height(self):
        """底部任务栏：工作区底部必须收紧到任务栏上方（留 safety 间距）。"""
        # 1600x900 屏幕，任务栏在底部：
        # ty = 900 - 30 - 8 = 862
        # safety = 4 -> limit_bottom = 858
        m = _FakeMonitor((0, 0, 1600, 900), (0, 0, 1600, 900))
        mm._apply_taskbar_avoidance([m], enabled=True, position="bottom", safety=4)
        _, ty, _, _ = taskbar.bar_geometry(m, "bottom")
        self.assertEqual(ty, 862)
        self.assertEqual(m.work_top, 0)
        self.assertEqual(m.work_height, 858)
        self.assertEqual(m.work_top + m.work_height, ty - 4)

    def test_top_taskbar_shifts_work_top_down(self):
        """顶部任务栏：工作区顶部必须让开任务栏底部（避开标题栏遮挡）。"""
        # 1600x900 屏幕，任务栏在顶部：
        # ty = 0 + 8 + 24 = 32, th = 30 -> 底部为 62
        # safety = 4 -> limit_top = 66
        m = _FakeMonitor((0, 0, 1600, 900), (0, 25, 1600, 875))
        mm._apply_taskbar_avoidance([m], enabled=True, position="top", safety=4)
        _, ty, _, th = taskbar.bar_geometry(m, "top")
        self.assertEqual(ty, 32)
        self.assertEqual(m.work_top, ty + th + 4)
        self.assertEqual(m.work_top, 66)
        # 高度应相应减少 diff = 66 - 25 = 41
        self.assertEqual(m.work_height, 875 - 41)

    def test_multi_monitors_avoidance(self):
        """多屏环境下，每块显示器均按自身坐标系避开任务栏。"""
        m1 = _FakeMonitor((0, 0, 1512, 982), (0, 25, 1512, 957), name="M1")
        m2 = _FakeMonitor((1512, -200, 1920, 1080), (1512, -200, 1920, 1080), name="M2")
        mm._apply_taskbar_avoidance([m1, m2], enabled=True, position="bottom", safety=4)

        # M1 bottom: 0 + 982 - 38 = 944 -> limit 940
        self.assertEqual(m1.work_top + m1.work_height, 940)
        # M2 bottom: -200 + 1080 - 38 = 842 -> limit 838
        self.assertEqual(m2.work_top + m2.work_height, 838)

    def test_dock_already_taller_does_not_double_trim(self):
        """当 Dock 已经在底部且高度大于任务栏时，不重复二次裁剪。"""
        # 假设 Dock 在底部占据至 y=820，当前工作区已经止于 820
        # 任务栏 limit_bottom 为 858（大于 820）
        m = _FakeMonitor((0, 0, 1600, 900), (0, 25, 1600, 795))  # bottom = 820
        mm._apply_taskbar_avoidance([m], enabled=True, position="bottom", safety=4)
        self.assertEqual(m.work_height, 795)
        self.assertEqual(m.work_top + m.work_height, 820)


if __name__ == "__main__":
    unittest.main()
