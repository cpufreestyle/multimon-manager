#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复刻 Rectangle Pro 收费功能的单测：Stash、几何邻接换屏、竖屏旋转、UUID 绑定。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import screen_math  # noqa: E402
import stash  # noqa: E402
import windows_mac as wm  # noqa: E402


class _Mon:
    def __init__(self, name, l, t, w, h, dp=None):
        self.device_name = name
        self.left, self.top, self.width, self.height = l, t, w, h
        self.device_path = dp or name
        self.work_rect = (l, t, w, h)


class TestAdjacentMonitor(unittest.TestCase):
    def test_row_of_three_chains(self):
        mons = [_Mon("L", 0, 0, 1920, 1080),
                _Mon("M", 1920, 0, 1920, 1080),
                _Mon("R", 3840, 0, 1920, 1080)]
        self.assertEqual(
            screen_math.adjacent_monitor_index(mons, 0, 1), 1)
        self.assertEqual(
            screen_math.adjacent_monitor_index(mons, 1, 1), 2)
        self.assertIsNone(
            screen_math.adjacent_monitor_index(mons, 2, 1))

    def test_l_shape_does_not_wrap(self):
        """L 形布局：右下角往右不该绕回左上角那块。"""
        mons = [_Mon("topLeft", 0, 0, 1920, 1080),
                _Mon("botRight", 1920, 1080, 1920, 1080)]
        self.assertIsNone(
            screen_math.adjacent_monitor_index(mons, 1, 1))
        self.assertIsNone(
            screen_math.adjacent_monitor_index(mons, 0, 1))

    def test_portrait_in_middle_is_skipped(self):
        """中间夹竖屏时，左右横屏互为邻居（竖屏被 ignore 掉）。"""
        mons = [_Mon("L", 0, 0, 1920, 1080),
                _Mon("P", 1920, 0, 1080, 1920),
                _Mon("R", 3000, 0, 1920, 1080)]
        ignore = {1}
        self.assertEqual(
            screen_math.adjacent_monitor_index(mons, 0, 1, ignore=ignore), 2)
        self.assertEqual(
            screen_math.adjacent_monitor_index(mons, 2, -1, ignore=ignore), 0)

    def test_no_overlap_is_not_adjacent(self):
        """上下排列的两块屏不算左右邻居（投影不重叠）。"""
        mons = [_Mon("top", 0, 0, 1920, 1080),
                _Mon("bottom", 0, 1080, 1920, 1080)]
        self.assertIsNone(
            screen_math.adjacent_monitor_index(mons, 0, 1))


class TestPortraitZone(unittest.TestCase):
    def test_landscape_unchanged(self):
        self.assertEqual(wm.portrait_zone("left", 1920, 1080), "left")
        self.assertEqual(wm.portrait_zone("left-third", 1920, 1080),
                         "left-third")

    def test_portrait_rotates(self):
        self.assertEqual(wm.portrait_zone("left", 1080, 1920), "top")
        self.assertEqual(wm.portrait_zone("right", 1080, 1920), "bottom")
        self.assertEqual(wm.portrait_zone("left-third", 1080, 1920),
                         "top-third")
        self.assertEqual(wm.portrait_zone("right-third", 1080, 1920),
                         "bottom-third")
        self.assertEqual(wm.portrait_zone("middle-third", 1080, 1920),
                         "middle-third")

    def test_rotation_roundtrip(self):
        """旋转两次回到原方位（映射是自逆的）。"""
        for z in ("left", "right", "top", "bottom",
                  "left-third", "right-third", "top-third", "bottom-third"):
            once = wm.portrait_zone(z, 1080, 1920)
            twice = wm.portrait_zone(once, 1080, 1920)
            self.assertEqual(twice, z, "%s 旋转两次应为自身" % z)

    def test_portrait_third_targets_exist(self):
        """旋转出来的 zone 必须在 snap() 的 targets 里有定义。"""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "windows_mac.py"), encoding="utf-8").read()
        for z in ("top-third", "middle-third-h", "bottom-third"):
            self.assertIn('"%s":' % z, src, "snap() 缺少 %s 的目标" % z)


class TestStash(unittest.TestCase):
    def setUp(self):
        self._prev_path = stash._path
        self._tmp = os.path.join(tempfile_dir(), "stash_test.json")
        stash._path = lambda: self._tmp

    def tearDown(self):
        stash._path = self._prev_path
        if os.path.exists(self._tmp):
            os.remove(self._tmp)

    def test_stash_and_unstash_fifo(self):
        self.assertEqual(stash.count(), 0)
        stash.push({"hwnd": "A::1", "owner": "A", "x": 1, "y": 2, "w": 3, "h": 4})
        stash.push({"hwnd": "B::1", "owner": "B", "x": 5, "y": 6, "w": 7, "h": 8})
        self.assertEqual(stash.count(), 2)
        first = stash.pop()
        self.assertEqual(first["hwnd"], "A::1")
        second = stash.pop()
        self.assertEqual(second["hwnd"], "B::1")
        self.assertIsNone(stash.pop())

    def test_no_duplicate_push(self):
        """同一个窗口不重复登记（调用方负责判，这里验证数据结构可用）。"""
        items = [i["hwnd"] for i in stash.list_stashed()]
        self.assertNotIn("A::1", items)
        stash.push({"hwnd": "A::1", "owner": "A", "x": 0, "y": 0, "w": 1, "h": 1})
        stash.push({"hwnd": "A::1", "owner": "A", "x": 0, "y": 0, "w": 1, "h": 1})
        # 数据结构本身允许重复，去重由 stash_active 做；这里只验证读写一致
        self.assertEqual(stash.count(), 2)

    def test_clear(self):
        stash.push({"hwnd": "A::1", "owner": "A", "x": 0, "y": 0, "w": 1, "h": 1})
        stash.clear()
        self.assertEqual(stash.count(), 0)

    def test_stash_rect_pins_to_work_top(self):
        """暂存位置贴工作区顶边，且只露一小条（不真正消失）。"""
        mon = _Mon("M", 0, 0, 1920, 1080)
        x, y, w, h = stash.stash_rect(mon)
        self.assertEqual((x, y), (0, 0))
        self.assertEqual(w, 1920)
        self.assertGreater(h, 0)
        self.assertLess(h, 1080 * 0.2)


def tempfile_dir():
    import tempfile
    d = tempfile.mkdtemp(prefix="mm_stash_")
    return d


if __name__ == "__main__":
    unittest.main()
