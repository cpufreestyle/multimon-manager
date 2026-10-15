#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四宫格容量预检单测：quad_capacity（后端）与 _quad_capacity_text（界面文案）。

容量预检回答的是「这块屏当前选择最多排几个」——四宫格容量是 App 最小窗口
尺寸的硬上限，放不下四个时照排四个只能重叠（真机 RV200 Pro 上四个窗口最小
尺寸合计需 1539x1266、工作区只有 1600x870）。预检只读上次排布缓存的最小
尺寸，不动窗口；没探过的窗口按 0 计（乐观），必须如实报出未探测名单。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402

try:  # ui 依赖 tkinter；无显示环境下降级跳过（只少覆盖一行文案格式化）
    import ui  # noqa: E402
except Exception:  # noqa: BLE001
    ui = None


class _Mon:
    def __init__(self, left=0, top=0, width=1920, height=1080, name="M1"):
        self.left, self.top = left, top
        self.width, self.height = width, height
        self.is_primary = True
        self.device_name = name
        self.device_path = name
        self.work_rect = (left, top, width, height)
        self.work_left, self.work_top = left, top
        self.work_width, self.work_height = width, height


class _FakeMonitors:
    def __init__(self, ms):
        self._ms = ms

    def enum_monitors(self, *a, **kw):
        return list(self._ms)


# 真机 2026-10-15 21:28 日志里的四个窗口与最小尺寸（RV200 Pro 1600x870）
REAL = {"ChatGPT::": (480, 600), "Comet::": (569, 426),
        "Shadowrocket::": (515, 492), "GameHub::": (1024, 666)}
RV200 = _Mon(0, -900, 1600, 900, "RV200 Pro")
RV200.work_rect = (0, -870, 1600, 870)
BUILTIN = _Mon(0, 0, 1512, 896, "Built-in")
DELL_V = _Mon(-1080, -1080, 1080, 1920, "DELL")


class QuadCapacityTest(unittest.TestCase):
    def setUp(self):
        wm._QUAD_MIN_CACHE.clear()
        self._saved = (wm.monitors, wm._gap_settings, wm._cg_rect)
        wm._gap_settings = lambda: (12, False)
        # 预检读第一个窗口所在屏；测试里不关心，统一读不到
        wm._cg_rect = lambda h: None

    def tearDown(self):
        wm.monitors, wm._gap_settings, wm._cg_rect = self._saved
        wm._QUAD_MIN_CACHE.clear()

    def _cap(self, hwnds, ms, monitor=None):
        wm.monitors = _FakeMonitors(ms)
        return wm.quad_capacity(hwnds, monitor)

    def test_no_cache_returns_none(self):
        self.assertIsNone(self._cap(list(REAL), [RV200]))
        self.assertIsNone(wm.quad_capacity([], [RV200]))
        self.assertIsNone(wm.quad_capacity(None, [RV200]))

    def test_real_machine_two_of_four(self):
        wm._QUAD_MIN_CACHE.update(REAL)
        cap = self._cap(list(REAL), [RV200, BUILTIN, DELL_V])
        self.assertEqual((cap["fit"], cap["total"]), (2, 4))
        self.assertEqual(cap["need"], (1539, 1266))
        self.assertEqual(cap["uncached"], [])
        self.assertEqual(cap["screen"], "RV200 Pro")
        self.assertEqual(cap["work"], (1600, 870))

    def test_named_screen_respected_even_if_it_fits_less(self):
        wm._QUAD_MIN_CACHE.update(REAL)
        big = _Mon(0, 0, 2560, 1440, "Big")
        # 点名 RV200 Pro：哪怕 Big 屏也摆得更多，也只报点名屏的容量
        cap = self._cap(list(REAL), [RV200, big], monitor="RV200 Pro")
        self.assertEqual(cap["screen"], "RV200 Pro")
        self.assertEqual(cap["fit"], 2)
        cap2 = self._cap(list(REAL), [RV200, big], monitor=1)
        self.assertEqual((cap2["screen"], cap2["fit"]), ("Big", 4))

    def test_auto_picks_landscape_over_portrait(self):
        wm._QUAD_MIN_CACHE.update(REAL)
        # 竖屏 DELL 上这四个能排 3 个，但用户明确说不往竖屏发：自动模式
        # 必须在横屏里挑，报出来的容量才是排布真正会用的
        cap = self._cap(list(REAL), [DELL_V, RV200])
        self.assertEqual(cap["screen"], "RV200 Pro")
        self.assertEqual(cap["fit"], 2)

    def test_uncached_windows_listed_and_need_withheld(self):
        wm._QUAD_MIN_CACHE.update({"ChatGPT::": (480, 600),
                                   "Comet::": (569, 426)})
        cap = self._cap(list(REAL), [RV200])
        # 未探测的窗口按 0 计，容量是上限：4 个都「放得下」只是乐观值，
        # 必须靠 uncached 名单把「没探过」如实报出来
        self.assertEqual(cap["fit"], 4)
        # uncached 报的是应用名（展示用，已剥掉 hwnd 的 :: 后缀）
        self.assertEqual(sorted(cap["uncached"]),
                         ["GameHub", "Shadowrocket"])
        self.assertIsNone(cap["need"])

    def test_all_fit(self):
        mins = {h: (400, 300) for h in ("a::", "b::", "c::", "d::")}
        wm._QUAD_MIN_CACHE.update(mins)
        cap = self._cap(list(mins), [BUILTIN])
        self.assertEqual((cap["fit"], cap["total"]), (4, 4))
        self.assertEqual(cap["need"], (800, 600))

    def test_subset_selection_two_windows(self):
        wm._QUAD_MIN_CACHE.update(REAL)
        cap = self._cap(["GameHub::", "ChatGPT::"], [RV200])
        self.assertEqual((cap["fit"], cap["total"]), (2, 2))
        self.assertEqual(cap["need"], (1504, 666))


@unittest.skipIf(ui is None, "tkinter/ui 不可导入")
class QuadCapacityTextTest(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(ui.App._quad_capacity_text(None), "")
        self.assertEqual(ui.App._quad_capacity_text({}), "")

    def test_all_fit_text(self):
        t = ui.App._quad_capacity_text(
            {"fit": 4, "total": 4, "screen": "Big", "work": (2560, 1440),
             "need": (800, 600), "uncached": []})
        self.assertEqual(t, "4/4 放得下（Big 2560×1440）")

    def test_partial_text(self):
        t = ui.App._quad_capacity_text(
            {"fit": 2, "total": 4, "screen": "RV200 Pro", "work": (1600, 870),
             "need": (1539, 1266), "uncached": []})
        self.assertEqual(t, "最多排 2/4：全排需 1539×1266（RV200 Pro 1600×870）")

    def test_partial_without_need_and_uncached(self):
        t = ui.App._quad_capacity_text(
            {"fit": 2, "total": 4, "screen": "RV200 Pro", "work": (1600, 870),
             "need": None, "uncached": ["GameHub::", "Shadowrocket::"]})
        self.assertEqual(t, "最多排 2/4（RV200 Pro 1600×870），2 个未探测")

    def test_uncached_never_claims_fit(self):
        # fit==total 但有未探测窗口时也不能说「放得下」：未探测按 0 计，
        # 结论必须留在「上限」上
        t = ui.App._quad_capacity_text(
            {"fit": 4, "total": 4, "screen": "RV200 Pro", "work": (1600, 870),
             "need": None, "uncached": ["GameHub::"]})
        self.assertEqual(t, "最多排 4/4（RV200 Pro 1600×870），1 个未探测")


if __name__ == "__main__":
    unittest.main()
