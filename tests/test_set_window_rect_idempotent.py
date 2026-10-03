#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""set_window_rect 的「幂等重设」逻辑单测（mock，不依赖真机 / 不碰真实窗口）。

背景：竖屏（小米 portrait 1152x2048）上「上半屏 / 整屏高度」曾跑偏数百 px，用户反馈
「竖屏上中下不准」。根因是早期实现按读回偏差**反向补偿**，而 System Events 对一次写入
只做「部分应用」（变化量越大动画越长），读回值取决于写入前的窗口状态、并不稳定，据此
算出的补偿量会把窗口越推越远。

现改为**幂等重设**：每次都写入同一个目标值，等动画结束后读回，只要还在变好就继续；
不再变好即认为触达系统 / App 可达边界并停止。

本测试用可模拟「只走一部分」的假 System Events 验证三条关键行为。
"""
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402

HWND = "App::Win"


class _FakeSystemEvents:
    """假 System Events：写入后窗口只向目标靠近 ratio（1.0 = 一次到位，0 = 推不动）。"""

    def __init__(self, start, ratio=1.0):
        self.cur = list(start)
        self.ratio = ratio
        self.writes = []

    def __call__(self, script):
        nums = re.findall(r"\{(-?\d+), (-?\d+)\}", script)
        if len(nums) == 2:  # 同时含 position 与 size 的写入
            x, y = int(nums[0][0]), int(nums[0][1])
            w, h = int(nums[1][0]), int(nums[1][1])
            self.writes.append((x, y, w, h))
            tgt = (x, y, w, h)
            for i in range(4):
                self.cur[i] += (tgt[i] - self.cur[i]) * self.ratio
        return ""

    def get(self, hwnd):
        return tuple(int(round(v)) for v in self.cur)


class TestIdempotentSetWindowRect(unittest.TestCase):
    def setUp(self):
        self._osa = wm._osa
        self._get = wm.get_window_rect
        self._sleep = time.sleep
        self._push_undo = wm.push_undo
        time.sleep = lambda _s: None          # 去掉等待，单测要快
        wm.push_undo = lambda *a, **k: None

    def tearDown(self):
        wm._osa = self._osa
        wm.get_window_rect = self._get
        time.sleep = self._sleep
        wm.push_undo = self._push_undo

    def _run(self, start, target, ratio):
        se = _FakeSystemEvents(start, ratio)
        wm._osa = se
        wm.get_window_rect = se.get
        wm.set_window_rect(HWND, *target, activate=False)
        return se

    def test_hits_in_one_write(self):
        """一次到位：命中后应立即停止，不做多余写入。"""
        se = self._run((0, 0, 800, 600), (100, 200, 800, 600), ratio=1.0)
        self.assertTrue(se.writes, "应至少写入一次")
        self.assertEqual(se.writes[0], (100, 200, 800, 600))
        self.assertLessEqual(len(se.writes), 2, "命中后不应继续重试")

    def test_partial_application_keeps_pushing(self):
        """只走一部分时应反复推进直至命中（竖屏真机的实际行为）。"""
        target = (1000, 800, 1200, 1000)
        se = self._run((0, 0, 800, 600), target, ratio=0.5)
        self.assertGreaterEqual(len(se.writes), 3, "部分应用应多次推进")
        # 关键：每次都写同一个目标值（幂等），绝不能做数值补偿
        self.assertEqual(len(set(se.writes)), 1, "每次必须写入同一个目标值")
        final = se.get(HWND)
        dev = max(abs(a - t) for a, t in zip(final, target))
        self.assertLessEqual(dev, 2, "最终应收敛到目标（实际 %s）" % (final,))

    def test_stops_when_no_progress(self):
        """推进不动（触达可达边界）时应停止，不能无限重试。"""
        se = self._run((0, 0, 800, 600), (1000, 800, 1200, 1000), ratio=0.0)
        self.assertLessEqual(len(se.writes), 4, "无进展时应很快停止，不能空转")


if __name__ == "__main__":
    unittest.main()
