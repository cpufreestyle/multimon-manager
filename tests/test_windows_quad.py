#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 侧四宫格与暂存单测：quad_capacity / quad_predict / 暂存取回。

这三块原来只有 macOS 实现（windows_mac.py），Windows 上界面用
getattr(backend, ...) 防御式调用，缺函数只是静默失效——用户看到的就是
「容量预检那一行永远是空的」「排列预演永远不弹」「暂存按钮按了没反应」。
本文件把 Windows 侧的对应实现钉住：

  · 选窗（_quad_pick_windows）必须和正式排布同一套：预演算的窗口和实际
    动的窗口不一样，预告本身就是错的；
  · 尺寸缓存要能落盘再读回（quad_cache.json），否则每次开程序预演都闭嘴；
  · 容量预检要能算出「最多排几个」，装不下时如实报未探测名单；
  · 预演只读：一个 set_window_rect 都不该发生；
  · 暂存/取回是 FIFO，且已暂存的窗口不重复登记。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import windows as wm  # noqa: E402
except Exception:  # noqa: BLE001  windows.py 依赖 ctypes.windll
    wm = None


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


def _win(hwnd, x=0, y=0, w=800, h=600, owner=None):
    return {"hwnd": hwnd, "pid": 4242, "owner": owner or hwnd,
            "name": "", "x": x, "y": y, "w": w, "h": h}


class _FakeWorld:
    """把窗口枚举 / 几何读写换成假的，几何写回一张表供读回。"""

    def __init__(self, wins, mons, min_sizes=None, cache_path=None):
        self.wins = {w["hwnd"]: dict(w) for w in wins}
        self.order = [w["hwnd"] for w in wins]
        self.mons = _FakeMonitors(mons)
        self.min_sizes = dict(min_sizes or {})
        self.placed = []
        if cache_path is not None:
            self._cache_path = cache_path
        else:
            fd, self._cache_path = tempfile.mkstemp(suffix=".json")
            os.close(fd)
            os.unlink(self._cache_path)

    def install(self, gap=(0, False)):
        self._saved = {}
        for attr in ("monitors", "list_windows_front_to_back", "_find_window",
                     "get_window_rect", "set_window_rect", "visible_rect",
                     "is_window", "_gap_settings", "_quad_cache_file",
                     "_pinned_target"):
            self._saved[attr] = getattr(wm, attr)
        wm.monitors = self.mons
        wm.list_windows_front_to_back = lambda *a, **k: [
            dict(self.wins[h]) for h in self.order]
        wm._find_window = self._find
        wm.get_window_rect = self._get_rect
        wm.set_window_rect = self._set_rect
        wm.visible_rect = self._get_rect
        wm.is_window = lambda h: h in self.wins
        wm._gap_settings = lambda: gap
        wm._quad_cache_file = lambda: self._cache_path
        wm._pinned_target = None

    def restore(self):
        for k, v in self._saved.items():
            setattr(wm, k, v)

    def _find(self, hwnd):
        w = self.wins.get(hwnd)
        return dict(w) if w else None

    def _get_rect(self, hwnd):
        w = self.wins.get(hwnd)
        return (w["x"], w["y"], w["w"], w["h"]) if w else (0, 0, 0, 0)

    def _set_rect(self, hwnd, x, y, w, h, activate=True, exact=False, **kw):
        # 模拟 App 的最小窗口尺寸：写小了读回来还是那么大
        mw, mh = self.min_sizes.get(hwnd, (0, 0))
        w = max(w, mw)
        h = max(h, mh)
        self.wins[hwnd] = {"hwnd": hwnd, "x": x, "y": y, "w": w, "h": h,
                           "pid": 4242, "owner": str(hwnd), "name": ""}
        self.placed.append((hwnd, x, y, w, h))
        return True


@unittest.skipIf(wm is None, "windows.py 依赖 ctypes.windll，仅 Windows 可导入")
class QuadPickTest(unittest.TestCase):
    def setUp(self):
        self.wins = [_win(101, 0, 0), _win(202, 300, 0),
                     _win(303, 0, 400), _win(404, 300, 400)]
        self._saved_pin = wm._pinned_target
        wm._pinned_target = None

    def tearDown(self):
        wm._pinned_target = self._saved_pin

    def _pick(self, wants, pinned=None):
        wm._pinned_target = pinned
        saved = (wm.list_windows_front_to_back, wm._find_window)
        wm.list_windows_front_to_back = lambda *a, **k: [
            dict(w) for w in self.wins]
        by_hwnd = {w["hwnd"]: w for w in self.wins}
        wm._find_window = lambda h: (dict(by_hwnd[h]) if h in by_hwnd else None)
        try:
            return wm._quad_pick_windows(tuple(wants))
        finally:
            wm.list_windows_front_to_back, wm._find_window = saved
            wm._pinned_target = None

    def test_auto_fills_four_in_order(self):
        picked, corners, missing = self._pick([None] * 4)
        self.assertEqual([h for h, _w in picked], [101, 202, 303, 404])
        self.assertEqual(corners, [0, 1, 2, 3])
        self.assertEqual(missing, [])

    def test_explicit_two_stops_auto_fill(self):
        picked, corners, missing = self._pick([202, 404, None, None])
        # 显式选满两个就不再补足：界面上点掉不想要的格就只排选中的
        self.assertEqual([h for h, _w in picked], [202, 404])
        self.assertEqual(corners, [0, 1])
        self.assertEqual(missing, [])

    def test_pinned_fills_empty_corner(self):
        # 显式选满两个就不再补足，钉住的目标窗口也用不上；只选一个时才补
        picked, corners, _m = self._pick([None, 202, None, None], pinned=303)
        self.assertEqual([h for h, _w in picked], [303, 202, 101, 404])
        self.assertEqual(corners, [0, 1, 2, 3])

    def test_missing_window_is_reported_not_silently_replaced(self):
        picked, corners, missing = self._pick([101, 999, 303, 404])
        self.assertIn(999, missing)
        # 缺的角不补（显式选满 2 个以上），剩下的照排
        self.assertEqual([h for h, _w in picked], [101, 303, 404])
        self.assertEqual(corners, [0, 2, 3])


@unittest.skipIf(wm is None, "windows.py 依赖 ctypes.windll，仅 Windows 可导入")
class QuadCacheTest(unittest.TestCase):
    def setUp(self):
        self._min = dict(wm._QUAD_MIN_CACHE)
        self._max = dict(wm._QUAD_MAX_CACHE)
        self._want = dict(wm._last_quad_want)
        self._saved_file = wm._quad_cache_file
        wm._QUAD_MIN_CACHE.clear()
        wm._QUAD_MAX_CACHE.clear()
        wm._last_quad_want.clear()
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        wm._quad_cache_file = lambda: self.path

    def tearDown(self):
        wm._QUAD_MIN_CACHE.clear()
        wm._QUAD_MIN_CACHE.update(self._min)
        wm._QUAD_MAX_CACHE.clear()
        wm._QUAD_MAX_CACHE.update(self._max)
        wm._last_quad_want.clear()
        wm._last_quad_want.update(self._want)
        wm._quad_cache_file = self._saved_file
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_save_load_roundtrip(self):
        wm._QUAD_MIN_CACHE.update({101: (640, 480), 202: (900, 600)})
        wm._QUAD_MAX_CACHE.update({303: (754, 0)})
        wm._quad_cache_save()
        self.assertTrue(os.path.exists(self.path))
        wm._QUAD_MIN_CACHE.clear()
        wm._QUAD_MAX_CACHE.clear()
        wm._quad_cache_load()
        self.assertEqual(wm._QUAD_MIN_CACHE[101], (640, 480))
        self.assertEqual(wm._QUAD_MIN_CACHE[202], (900, 600))
        self.assertEqual(wm._QUAD_MAX_CACHE[303], (754, 0))

    def test_load_missing_file_is_silent(self):
        wm._quad_cache_load()
        self.assertEqual(wm._QUAD_MIN_CACHE, {})

    def test_remember_measured_learns_min_size(self):
        # 目标 400x300，App 顶回 520x360：读回值就是最小尺寸
        wm._last_quad_want[101] = (0, 0, 400, 300)
        wm._quad_remember_measured(101, (0, 0, 520, 360))
        self.assertEqual(wm._QUAD_MIN_CACHE[101], (520, 360))

    def test_remember_measured_learns_max_size(self):
        # 目标 900x700，只长到 754x600：App 拒绝长到目标值
        wm._last_quad_want[202] = (0, 0, 900, 700)
        wm._quad_remember_measured(202, (0, 0, 754, 600))
        self.assertEqual(wm._QUAD_MAX_CACHE[202], (754, 600))
        # 宽度没被钳制（754 就是读回值），不能顺手记成最小尺寸
        self.assertNotIn(202, wm._QUAD_MIN_CACHE)

    def test_remember_measured_ignores_zero_rect(self):
        wm._last_quad_want[303] = (0, 0, 400, 300)
        wm._quad_remember_measured(303, (0, 0, 0, 0))
        self.assertNotIn(303, wm._QUAD_MIN_CACHE)


RV200 = _Mon(0, -900, 1600, 900, "RV200 Pro")
RV200.work_rect = (0, -870, 1600, 870)
BUILTIN = _Mon(0, 0, 1512, 896, "Built-in")
DELL_V = _Mon(-1080, -1080, 1080, 1920, "DELL")
# 真机日志里的四个窗口与最小尺寸：合计需 1539x1266，工作区只有 1600x870
REAL = {101: (480, 600), 202: (569, 426), 303: (515, 492), 404: (1024, 666)}


@unittest.skipIf(wm is None, "windows.py 依赖 ctypes.windll，仅 Windows 可导入")
class QuadCapacityTest(unittest.TestCase):
    def setUp(self):
        self._min = dict(wm._QUAD_MIN_CACHE)
        wm._QUAD_MIN_CACHE.clear()
        self._saved_file = wm._quad_cache_file
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        wm._quad_cache_file = lambda: self.path

    def tearDown(self):
        wm._QUAD_MIN_CACHE.clear()
        wm._QUAD_MIN_CACHE.update(self._min)
        wm._quad_cache_file = self._saved_file
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _cap(self, hwnds, mons, monitor=None):
        world = _FakeWorld([], mons)
        world.install()
        try:
            return wm.quad_capacity(hwnds, monitor)
        finally:
            world.restore()

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

    def test_uncached_reported_as_optimistic(self):
        wm._QUAD_MIN_CACHE.update({101: (480, 600)})
        cap = self._cap([101, 202, 303, 404], [RV200])
        self.assertEqual(cap["fit"], 4)      # 没探过的按 0 计 = 乐观
        self.assertEqual(len(cap["uncached"]), 3)

    def test_named_monitor_is_respected(self):
        wm._QUAD_MIN_CACHE.update(REAL)
        cap = self._cap(list(REAL), [RV200, BUILTIN], monitor="Built-in")
        self.assertEqual(cap["screen"], "Built-in")
        self.assertEqual(cap["work"], (1512, 896))


@unittest.skipIf(wm is None, "windows.py 依赖 ctypes.windll，仅 Windows 可导入")
class QuadPredictTest(unittest.TestCase):
    def setUp(self):
        self.wins = [_win(101, 0, 0), _win(202, 300, 0),
                     _win(303, 0, 400), _win(404, 300, 400)]
        self.mons = [_Mon(0, 0, 1600, 1000, "Main")]
        self._min = dict(wm._QUAD_MIN_CACHE)
        self._max = dict(wm._QUAD_MAX_CACHE)
        wm._QUAD_MIN_CACHE.clear()
        wm._QUAD_MAX_CACHE.clear()
        self.world = _FakeWorld(self.wins, self.mons)
        self.world.install(gap=(0, False))
        self._saved_pin = wm._pinned_target
        wm._pinned_target = None

    def tearDown(self):
        self.world.restore()
        wm._QUAD_MIN_CACHE.clear()
        wm._QUAD_MIN_CACHE.update(self._min)
        wm._QUAD_MAX_CACHE.clear()
        wm._QUAD_MAX_CACHE.update(self._max)
        wm._pinned_target = self._saved_pin

    def test_no_cache_stays_silent(self):
        warns, detail = wm.quad_predict()
        self.assertEqual(warns, [])
        self.assertEqual(len(detail["uncached"]), 4)
        self.assertEqual(self.world.placed, [])   # 预演绝不能动窗口

    def test_fitting_quad_has_no_warning(self):
        wm._QUAD_MIN_CACHE.update({101: (400, 300), 202: (400, 300),
                                   303: (400, 300), 404: (400, 300)})
        warns, detail = wm.quad_predict()
        self.assertEqual(warns, [])
        self.assertEqual((detail["fit"], detail["total"]), (4, 4))
        self.assertEqual(detail["screen"], "Main")
        self.assertEqual(self.world.placed, [])

    def test_max_size_reports_blank(self):
        wm._QUAD_MIN_CACHE.update({101: (400, 300), 202: (400, 300),
                                   303: (400, 300), 404: (400, 300)})
        # 202 上次实测宽最多 754，右列格子 800 宽 → 右侧约 46px 空白（不警告），
        # 换个夸张的上限：格子 800、上限 300 → 500px 空白必须报出来
        wm._QUAD_MAX_CACHE.update({202: (300, 0)})
        warns, detail = wm.quad_predict()
        self.assertTrue(any("空白" in w for w in warns), warns)
        self.assertTrue(any("202" in w for w in warns), warns)
        self.assertEqual(len(detail["blank"]), 1)
        self.assertEqual(self.world.placed, [])

    def test_too_big_reports_partial_placement(self):
        wm._QUAD_MIN_CACHE.update({101: (900, 700), 202: (900, 700),
                                   303: (900, 700), 404: (900, 700)})
        warns, detail = wm.quad_predict()
        self.assertTrue(warns)
        self.assertIn("只排", warns[0])
        self.assertEqual(detail["fit"], 1)
        self.assertEqual(self.world.placed, [])

    def test_two_selected_windows_are_predicted_as_selected(self):
        wm._QUAD_MIN_CACHE.update({101: (400, 300), 202: (400, 300)})
        warns, detail = wm.quad_predict(tl_hwnd=101, tr_hwnd=202)
        self.assertEqual((detail["fit"], detail["total"]), (2, 2))
        self.assertEqual(warns, [])

    def test_missing_window_is_reported(self):
        wm._QUAD_MIN_CACHE.update({101: (400, 300), 202: (400, 300)})
        warns, _d = wm.quad_predict(tl_hwnd=101, tr_hwnd=202, bl_hwnd=999)
        self.assertTrue(any("已不可用" in w for w in warns), warns)


@unittest.skipIf(wm is None, "windows.py 依赖 ctypes.windll，仅 Windows 可导入")
class SnapFourQuadRecordTest(unittest.TestCase):
    """snap_four_quad 的实测闭环：报告 / 角位 / 缓存必须记下来给界面用。"""

    def setUp(self):
        self.wins = [_win(101, 0, 0), _win(202, 300, 0),
                     _win(303, 0, 400), _win(404, 300, 400)]
        self.mons = [_Mon(0, 0, 1600, 1000, "Main")]
        self._min = dict(wm._QUAD_MIN_CACHE)
        self._max = dict(wm._QUAD_MAX_CACHE)
        self._want = dict(wm._last_quad_want)
        wm._QUAD_MIN_CACHE.clear()
        wm._QUAD_MAX_CACHE.clear()
        wm._last_quad_want.clear()
        self._saved_file = wm._quad_cache_file
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        wm._quad_cache_file = lambda: self.path
        self._saved_pin = wm._pinned_target
        wm._pinned_target = None

    def tearDown(self):
        wm._QUAD_MIN_CACHE.clear()
        wm._QUAD_MIN_CACHE.update(self._min)
        wm._QUAD_MAX_CACHE.clear()
        wm._QUAD_MAX_CACHE.update(self._max)
        wm._last_quad_want.clear()
        wm._last_quad_want.update(self._want)
        wm._quad_cache_file = self._saved_file
        wm._pinned_target = self._saved_pin
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _run(self, min_sizes=None):
        world = _FakeWorld(self.wins, self.mons, min_sizes,
                           cache_path=self.path)
        world.install(gap=(0, False))
        try:
            ok = wm.snap_four_quad()
        finally:
            world.restore()
        return ok, world

    def test_report_slots_and_cache(self):
        ok, world = self._run()
        self.assertTrue(ok)
        self.assertEqual(len(wm._last_quad_report), 4)
        self.assertEqual(sorted(h for h, _w, _g in wm._last_quad_report),
                         [101, 202, 303, 404])
        # 每个窗口都真的被摆了，且写入目标与读回一致（无钳制时）
        for hwnd, want, got in wm._last_quad_report:
            self.assertIsNotNone(got)
            self.assertEqual(want, got)
        # 角位按序 0-3，界面据此把实际归属写回四个下拉
        self.assertEqual(dict(wm._last_quad_slots),
                         {0: 101, 1: 202, 2: 303, 3: 404})
        self.assertEqual(wm._last_quad_unwritable, [])
        self.assertTrue(os.path.exists(self.path))

    def test_clamped_window_is_recorded_not_unwritable(self):
        ok, world = self._run(min_sizes={101: (1200, 900)})
        self.assertTrue(ok)
        report = {h: (want, got) for h, want, got in wm._last_quad_report}
        want, got = report[101]
        self.assertEqual(got[2], 1200)      # 被最小宽度顶大
        self.assertNotEqual(want, got)
        self.assertNotIn(101, wm._last_quad_unwritable)

    def test_two_windows_only_places_two(self):
        world = _FakeWorld(self.wins, self.mons, cache_path=self.path)
        world.install()
        try:
            ok = wm.snap_four_quad(tl_hwnd=101, tr_hwnd=202)
        finally:
            world.restore()
        self.assertTrue(ok)
        self.assertEqual(sorted(h for h, *_r in world.placed), [101, 202])
        self.assertEqual(dict(wm._last_quad_slots), {0: 101, 1: 202})

    def test_single_window_fails_and_clears_state(self):
        world = _FakeWorld([_win(101)], self.mons)
        world.install()
        try:
            ok = wm.snap_four_quad()
        finally:
            world.restore()
        self.assertFalse(ok)
        self.assertEqual(wm._last_quad_report, [])
        self.assertEqual(wm._last_quad_slots, [])

    def _rects(self, world):
        return {h: (w["x"], w["y"], w["w"], w["h"])
                for h, w in world.wins.items()}

    def test_four_windows_fill_work_area_seamlessly(self):
        ok, world = self._run()
        self.assertTrue(ok)
        r = self._rects(world)
        wl, wt, ww, wh = self.mons[0].work_rect
        # 右列起点 / 下排起点按左窗、上窗的实测右缘/下缘 +1 定（避免 DWM
        # 透明边框让相邻窗口互相压盖），所以四格之间有一条 1px 的缝
        self.assertEqual(r[101], (wl, wt, ww // 2, wh // 2))
        self.assertEqual(r[202], (wl + ww // 2 + 1, wt, ww - ww // 2 - 1,
                                  wh // 2))
        self.assertEqual(r[303], (wl, wt + wh // 2 + 1, ww // 2 + 1,
                                  wh - wh // 2 - 1))
        self.assertEqual(r[404], (wl + ww // 2 + 1, wt + wh // 2 + 1,
                                  ww - ww // 2 - 1, wh - wh // 2 - 1))
        # 四格合起来必须正好铺满工作区，不能越界
        for _hwnd, (_x, _y, w, h) in r.items():
            self.assertLessEqual(w, ww)
            self.assertLessEqual(h, wh)

    def test_clamped_left_window_pushes_right_column(self):
        # 左上窗最小宽度 1200：右列起点让到它实测右缘之后，且下排左列
        # 与上排沿用同一条分割线（不错开）
        ok, world = self._run(min_sizes={101: (1200, 900)})
        self.assertTrue(ok)
        r = self._rects(world)
        self.assertEqual(r[101][2], 1200)
        self.assertGreaterEqual(r[202][0], r[101][0] + r[101][2])
        self.assertEqual(r[303][2], r[101][2] + 1)
        self.assertEqual(r[303][0], r[101][0])
        self.assertGreaterEqual(r[303][1], r[101][1] + r[101][3])
        self.assertLessEqual(r[303][0] + r[303][2], r[202][0])

    def test_three_windows_form_t_not_dead_quarter(self):
        # 三窗（左上/右上/左下）：右下拉通整宽，不能留一块四分之一屏死区
        world = _FakeWorld(self.wins, self.mons, cache_path=self.path)
        world.install()
        try:
            ok = wm.snap_four_quad(tl_hwnd=101, tr_hwnd=202, bl_hwnd=303)
        finally:
            world.restore()
        self.assertTrue(ok)
        self.assertEqual([h for h, *_r in world.placed], [101, 202, 303])
        r = self._rects(world)
        wl, wt, ww, wh = self.mons[0].work_rect
        self.assertNotIn(404, dict(wm._last_quad_slots))
        self.assertEqual(r[101], (wl, wt, ww // 2, wh // 2))
        self.assertEqual(r[202], (wl + ww // 2 + 1, wt, ww - ww // 2 - 1,
                                  wh // 2))
        self.assertEqual(r[303], (wl, wt + wh // 2 + 1, ww, wh - wh // 2 - 1))

    def test_two_windows_in_one_row_fill_height(self):
        # 同排两窗：那一排撑满整高，不是只占上半屏
        world = _FakeWorld(self.wins, self.mons, cache_path=self.path)
        world.install()
        try:
            ok = wm.snap_four_quad(tl_hwnd=101, tr_hwnd=202)
        finally:
            world.restore()
        self.assertTrue(ok)
        r = self._rects(world)
        wl, wt, ww, wh = self.mons[0].work_rect
        self.assertEqual(r[101], (wl, wt, ww // 2, wh))
        self.assertEqual(r[202], (wl + ww // 2 + 1, wt, ww - ww // 2 - 1, wh))

    def test_two_windows_in_one_column_fill_width(self):
        # 同列两窗：那一列撑通整宽，上下各一半
        world = _FakeWorld(self.wins, self.mons, cache_path=self.path)
        world.install()
        try:
            ok = wm.snap_four_quad(tl_hwnd=101, bl_hwnd=303)
        finally:
            world.restore()
        self.assertTrue(ok)
        r = self._rects(world)
        wl, wt, ww, wh = self.mons[0].work_rect
        self.assertEqual(r[101], (wl, wt, ww, wh // 2))
        self.assertEqual(r[303], (wl, wt + wh // 2 + 1, ww, wh - wh // 2 - 1))


@unittest.skipIf(wm is None, "windows.py 依赖 ctypes.windll，仅 Windows 可导入")
class StashTest(unittest.TestCase):
    """窗口暂存 / 取回（FIFO，复刻 Rectangle Pro 的 Stash）。"""

    def setUp(self):
        self._min = dict(wm._QUAD_MIN_CACHE)
        self.mons = [_Mon(0, 0, 1600, 1000, "Main")]
        self.wins = [_win(101, 100, 100, 800, 600), _win(202, 200, 200)]
        self._saved_pin = wm._pinned_target
        wm._pinned_target = 101
        self._saved_proc = wm._proc_name
        wm._proc_name = lambda pid: "FakeApp"
        self._saved_gwl = getattr(wm, "_get_window_long", None)
        import stash as stash_mod
        self._stash = stash_mod
        fd, self.stash_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.stash_path)
        self._saved_stash_path = stash_mod._path
        stash_mod._path = lambda: self.stash_path
        self.world = _FakeWorld(self.wins, self.mons)
        self.world.install()
        self._saved_fg = wm.get_foreground_window
        wm.get_foreground_window = lambda use_pinned=True: 101

    def tearDown(self):
        self.world.restore()
        wm._pinned_target = self._saved_pin
        wm._proc_name = self._saved_proc
        wm.get_foreground_window = self._saved_fg
        self._stash._path = self._saved_stash_path
        if os.path.exists(self.stash_path):
            os.unlink(self.stash_path)

    def test_stash_then_unstash_restores_geometry(self):
        self.assertTrue(wm.stash_active(use_pinned=True))
        stashed = self._stash.list_stashed()
        self.assertEqual(len(stashed), 1)
        item = stashed[0]
        self.assertEqual(item["hwnd"], 101)
        self.assertEqual((item["x"], item["y"], item["w"], item["h"]),
                         (100, 100, 800, 600))
        # 暂存位：贴工作区顶边，只留一条可见
        rect = self.world._get_rect(101)
        self.assertEqual(rect[0], 0)
        self.assertLess(rect[3], 100)
        # 取回：原位原样恢复
        hwnd, ok = wm.unstash_first()
        self.assertEqual((hwnd, ok), (101, True))
        self.assertEqual(self.world._get_rect(101), (100, 100, 800, 600))
        self.assertEqual(self._stash.list_stashed(), [])

    def test_stash_is_fifo(self):
        wm.get_foreground_window = lambda use_pinned=True: 101
        self.assertTrue(wm.stash_active())
        wm.get_foreground_window = lambda use_pinned=True: 202
        self.assertTrue(wm.stash_active())
        hwnd, ok = wm.unstash_first()
        self.assertEqual((hwnd, ok), (101, True))
        hwnd, ok = wm.unstash_first()
        self.assertEqual((hwnd, ok), (202, True))

    def test_already_stashed_window_is_skipped(self):
        self.assertTrue(wm.stash_active())
        self.assertFalse(wm.stash_active())
        self.assertEqual(len(self._stash.list_stashed()), 1)

    def test_unstash_empty_list(self):
        self.assertEqual(wm.unstash_first(), (None, False))

    def test_unstash_closed_window_is_dropped(self):
        wm.get_foreground_window = lambda use_pinned=True: 101
        wm.stash_active()
        self.world.wins.pop(101)          # 窗口已关闭
        self.assertEqual(wm.unstash_first(), (None, False))


if __name__ == "__main__":
    unittest.main()
