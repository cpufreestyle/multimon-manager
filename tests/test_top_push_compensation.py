#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「顶边被系统下压」的补偿单测（place_tile）。

真机现象（Mi 竖屏 1152x2048，work_top=-1066）：macOS 不让窗口盖住菜单栏，
贴着工作区顶边放置时顶边会被下压 31px（实测 WorkBuddy / Comet / OneNote /
ChatGPT / 系统设置 / Marvis 全部一样，是屏级限制不是 App 怪癖）。

不补偿的后果：最上面那块与第二块重叠 31px——下层窗口的标题栏被上层的画面
盖住，用户看到的现象是「这扇窗拖不动」（拖标题栏抓到的是上面那扇）。

补偿规则：把下压的像素数从高度里扣掉，底边保持不动。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import windows_mac as wm  # noqa: E402


class _Mon(object):
    """最小显示器替身（monitors_mac.MonitorInfo 的字段子集）。"""

    def __init__(self, left=1512, top=-1066, width=1152, height=2048,
                 name="Mi Monitor"):
        self.left = left
        self.top = top
        self.width = width
        self.height = height
        self.device_name = name
        self.work_rect = (left, top, width, height)
        self.work_left = left
        self.work_top = top
        self.work_width = width
        self.work_height = height
        self.is_primary = False
        self.is_builtin = False


class _FakeScreen(object):
    """模拟 macOS：贴顶的窗口顶边被钳到 min_top，其余按写入生效。"""

    def __init__(self, min_top=-1035, clamp_size=None):
        self.min_top = min_top
        # clamp_size: {hwnd: (w, h)} 模拟 App 把尺寸钳在别处
        self.clamp_size = clamp_size or {}
        self.writes = []
        self.drawn = {}

    def set(self, hwnd, x, y, w, h, activate=True, **_k):
        self.writes.append((hwnd, (x, y, w, h)))
        cy = max(y, self.min_top) if y < self.min_top else y
        # App 自己的尺寸钳制（最小/最大尺寸）优先于系统钳制
        cw, ch = self.clamp_size.get(hwnd, (w, h))
        self.drawn[hwnd] = (x, cy, cw, ch)
        return True

    def get(self, hwnd):
        return self.drawn.get(hwnd, (0, 0, 0, 0))


class TestPlaceTilePush(unittest.TestCase):
    """place_tile：顶边被下压时补回高度，底边不动。"""

    def setUp(self):
        self._prev = {k: getattr(wm, k) for k in
                      ("set_window_rect", "_fresh_rect", "_cg_rect")}

    def tearDown(self):
        for k, v in self._prev.items():
            setattr(wm, k, v)

    def _install(self, screen):
        wm.set_window_rect = screen.set
        wm._fresh_rect = screen.get
        wm._cg_rect = screen.get

    def test_push_is_compensated_and_bottom_edge_kept(self):
        """顶边被压 31px → 高度补 31px，底边仍是原来要的位置。"""
        sc = _FakeScreen()
        self._install(sc)
        y, h, pushed = wm.place_tile("A::1", 1512, -1066, 1152, 683)
        self.assertEqual(pushed, 31)
        self.assertEqual(y, -1035)
        self.assertEqual(h, 683 - 31)
        # 底边不变：-1066 + 683 == -1035 + 652
        self.assertEqual(y + h, -1066 + 683)
        # 补高度要真的写回去一次
        self.assertEqual(len(sc.writes), 2)
        self.assertEqual(sc.writes[-1][1], (1512, -1035, 1152, 683 - 31))

    def test_no_push_writes_once(self):
        """顶边没被压（主屏就是这样）就只写一次，不动高度。"""
        sc = _FakeScreen(min_top=-99999)
        self._install(sc)
        y, h, pushed = wm.place_tile("A::1", 0, 34, 1512, 885)
        self.assertEqual(pushed, 0)
        self.assertEqual((y, h), (34, 885))
        self.assertEqual(len(sc.writes), 1)

    def test_app_clamped_size_is_not_compensated(self):
        """App 把尺寸钳在别处时读回不可信：不补偿，别把窗口削矮。"""
        sc = _FakeScreen(clamp_size={"A::1": (754, 536)})
        self._install(sc)
        y, h, pushed = wm.place_tile("A::1", 1512, -1066, 1152, 683)
        self.assertEqual(pushed, 0)
        self.assertEqual(h, 683)
        self.assertEqual(len(sc.writes), 1)

    def test_write_that_did_not_move_anything_is_not_compensated(self):
        """写入没生效（CG 几何写前写后一样）时读回不算数，不补偿。"""
        sc = _FakeScreen()
        self._install(sc)
        wm._cg_rect = lambda hwnd: (0, 0, 0, 0)   # 读不到 → 前后一致
        wm._fresh_rect = lambda hwnd: (1512, -1035, 1152, 683)
        y, h, pushed = wm.place_tile("A::1", 1512, -1066, 1152, 683)
        self.assertEqual(pushed, 0)
        self.assertEqual(h, 683)

    def test_push_larger_than_tile_is_skipped(self):
        """格子被压得只剩一条缝时保持原高度：宁可轻微重叠也不让窗口没法用。"""
        sc = _FakeScreen(min_top=-400)     # 只剩 17px 高
        self._install(sc)
        y, h, pushed = wm.place_tile("A::1", 1512, -1066, 1152, 683)
        self.assertEqual(pushed, 666)
        self.assertEqual(h, 683)
        self.assertEqual(len(sc.writes), 1)


class TestThreeStackNoOverlap(unittest.TestCase):
    """三栏在「顶边被下压」的屏上，段与段之间不能重叠。"""

    def setUp(self):
        self._prev = {k: getattr(wm, k) for k in
                      ("set_window_rect", "_fresh_rect", "_cg_rect",
                       "get_window_rect", "monitors", "list_target_windows",
                       "_osa")}
        self.sc = _FakeScreen(min_top=-1035)
        wm.set_window_rect = self.sc.set
        wm._fresh_rect = self.sc.get
        wm._cg_rect = self.sc.get
        wm.get_window_rect = self.sc.get
        wm.monitors = type("M", (), {"enum_monitors": staticmethod(
            lambda force=False, work=True: [_Mon()])})()
        # 假窗口列表：用例不依赖真机此刻开着几个窗口
        wm.list_target_windows = lambda: [
            {"pid": 100 + i, "owner": "App%d" % i, "name": "win%d" % i,
             "hwnd": "App%d::win%d" % (i, i), "x": 0, "y": 0, "w": 800,
             "h": 600}
            for i in range(3)]
        wm._osa = lambda script: ""      # 假 App 名，激活调用不用真跑
        wm._pinned_target = None

    def tearDown(self):
        wm.set_window_rect = self._prev["set_window_rect"]
        wm._fresh_rect = self._prev["_fresh_rect"]
        wm._cg_rect = self._prev["_cg_rect"]
        wm.get_window_rect = self._prev["get_window_rect"]
        wm.monitors = self._prev["monitors"]
        wm.list_target_windows = self._prev["list_target_windows"]
        wm._osa = self._prev["_osa"]

    def test_segments_do_not_overlap_and_tile_usable_area(self):
        """三段的可见矩形首尾相接，末端正好收在屏幕底。"""
        ok = wm.snap_three_stack(top_hwnd="A::a", mid_hwnd="B::b",
                                 bot_hwnd="C::c", use_pinned=False, monitor=0)
        self.assertTrue(ok)
        # 按「首次写入顺序」认上/中/下三段（显式 hwnd 不在窗口列表里时，
        # snap_three_stack 会退回自动挑选，写入的 hwnd 与传入的不一定同名）
        order = []
        for hwnd, _r in self.sc.writes:
            if hwnd not in order:
                order.append(hwnd)
        self.assertEqual(len(order), 3)
        rects = [self.sc.drawn[h] for h in order]
        for i in range(2):
            self.assertLessEqual(rects[i][1] + rects[i][3], rects[i + 1][1],
                                 "第 %d 段压住了第 %d 段" % (i, i + 1))
        self.assertEqual(rects[0][1], -1035)          # 顶段贴菜单栏下沿
        self.assertEqual(rects[2][1] + rects[2][3], -1066 + 2048)


if __name__ == "__main__":
    unittest.main()
