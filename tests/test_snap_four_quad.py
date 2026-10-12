#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口排布单测：snap_four_quad（2×2 四宫格）与 fill_all_monitors（跨屏铺满）。

横屏四宫格就是上下左右四等分：屏幕工作区按宽/高各切一半，四个矩形无缝拼满
（宽高为奇数时，右列/下排用减法补齐，避免出现 1px 缝隙）。这与单窗的
quad-tl/tr/bl/br 吸附是同一套几何，区别是一次摆四个窗口。

跨屏铺满是另一个形状的问题：若干窗口分别铺满各自的屏，且要能只铺勾选的屏
（竖屏上的横屏应用会被压扁，默认不该往竖屏铺）。

只覆盖 windows_mac（本仓库现有单测的既有前提）：windows.py 依赖
ctypes.windll，在 macOS 上无法导入。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import windows_mac as wm  # noqa: E402


class _Mon:
    def __init__(self, left=0, top=0, width=1920, height=1080, name="M1"):
        self.left, self.top = left, top
        self.width, self.height = width, height
        self.is_primary = True
        self.device_name = name
        self.device_path = name
        self.work_rect = (left, top, width, height)
        # 真实 MonitorInfo 还有这四个扁平字段，fill_all_monitors 的 max 模式用
        self.work_left, self.work_top = left, top
        self.work_width, self.work_height = width, height


def _win(hwnd, x=0, y=0, w=100, h=100):
    return {"hwnd": hwnd, "owner": hwnd.split("::")[0],
            "name": hwnd.split("::")[1], "x": x, "y": y, "w": w, "h": h,
            "pid": 99}


def _run(wins, mons, **kw):
    """把窗口查找 / 显示器枚举替换成假的，返回 (ok, placed)。

    readback: {hwnd: 读回矩形}，用于模拟「系统/App 钳制」——写入目标值后
   读回的可能是别的尺寸（部分 App 有最小窗口高度）。未列出的窗口按
   写入值原样读回。

    min_sizes: {hwnd: (min_w, min_h)}，模拟 App 的最小窗口尺寸。snap_four_quad
    会先实测每个窗口的最小尺寸（真机是写个小尺寸再读回，走 AppleScript），
    单测必须把这一步也换掉——否则会真的去动用户的窗口。
    """
    placed = []
    prev = {}
    for attr in ("list_target_windows", "monitors", "_find_window",
                 "get_window_rect", "set_window_rect", "push_undo",
                 "_osa", "_probe_min_size", "list_windows_front_to_back",
                 "_readback_rect"):
        prev[attr] = getattr(wm, attr)

    class _Mons:
        def enum_monitors(self):
            return mons

    readback = kw.pop("readback", {})
    min_sizes = kw.pop("min_sizes", {})
    state = {}

    def _rect(hwnd, *a, **k):
        return readback.get(hwnd) or state.get(hwnd) or (0, 0, 0, 0)

    # 闭环会用「读回宽度 + 1」定右列起点。fake 若直接把请求宽度当读回值，
    # 右列会比理想值多 1px。真实系统遇钳制才会这样，常规用例期望的是
    # 「读回 == 请求」，所以在没有钳制的窗口上把读回宽度退回 1px，
    # 让闭环算出的起点正好等于理想 col_w。
    def _exact(hwnd, x, y, w, h):
        return (x, y, max(1, w - 1), h) if hwnd not in readback else (x, y, w, h)

    wm.monitors = _Mons()
    wm.list_target_windows = lambda: wins
    wm._find_window = lambda hwnd: next(
        (w for w in wins if w["hwnd"] == hwnd), None)
    wm.get_window_rect = _rect
    wm.push_undo = lambda *a, **k: None
    # 最小尺寸探测本身会真的写 AppleScript 动用户的窗口，单测里必须假掉
    wm._osa = lambda script: ""
    wm._probe_min_size = lambda hwnd, at_x, at_y: tuple(
        min_sizes.get(hwnd, (0, 0)))
    # _readback_rect 会走真实 CG 枚举（list_windows_front_to_back）去找
    # 「刚写入的那个窗口」——单测里没有真机窗口，必须换成 fake 的读回。
    wm.list_windows_front_to_back = lambda *a, **k: wins

    def _readback(hwnd, target=None):
        return readback.get(hwnd) or state.get(hwnd) or (0, 0, 0, 0)

    wm._readback_rect = _readback
    # _settled_readback 在动画未停时会再等 0.35s 读一次；单测里
    # 没有真动画，直接复用同一份 fake 读回即可。
    wm._settled_readback = _readback

    def _place(hwnd, x, y, w, h, activate=True):
        placed.append((hwnd, x, y, w, h))
        # 见 _exact 说明：未指定 readback 的窗口读回宽度比请求少 1px，
        # 令「读回 + 1」正好回到理想分割点
        state[hwnd] = _exact(hwnd, x, y, w, h)
        return True

    wm.set_window_rect = _place
    try:
        ok = wm.snap_four_quad(**kw)
    finally:
        for k, v in prev.items():
            setattr(wm, k, v)
    return ok, placed


class TestSnapFourQuad(unittest.TestCase):
    WINS = [_win("A::1", x=10, y=20), _win("B::1", x=2000, y=30),
            _win("C::1", x=10, y=2000), _win("D::1", x=3000, y=20)]

    def test_auto_four_windows_fill_2x2(self):
        """四个格子必须严格铺满工作区（2x2 四等分），窗口落在各自格子里。"""
        ok, placed = _run(list(self.WINS), [_Mon()])
        self.assertTrue(ok)
        wl, wt, ww, wh = 0, 0, 1920, 1080
        hw, hh = ww // 2, wh // 2
        cells = wm._last_quad_boxes
        self.assertEqual(cells, [(wl, wt, hw, hh),
                                 (wl + hw, wt, ww - hw, hh),
                                 (wl, wt + hh, hw, wh - hh),
                                 (wl + hw, wt + hh, ww - hw, wh - hh)])
        # 每个窗口都必须落在自己的格子里（不越格、不跨界）
        for (hwnd, x, y, w, h), (bx, by, bw, bh) in zip(placed, cells):
            self.assertGreaterEqual(x, bx)
            self.assertGreaterEqual(y, by)
            self.assertLessEqual(x + w, bx + bw)
            self.assertLessEqual(y + h, by + bh)

    def test_odd_geometry_no_seam(self):
        """宽高为奇数时四个矩形必须无缝拼满，不留 1px 缝隙。"""
        ok, placed = _run(list(self.WINS), [_Mon(width=1921, height=1081)])
        self.assertTrue(ok)
        cells = wm._last_quad_boxes
        # 左列右边缘必须贴住右列左边缘，否则两个窗口中间露缝
        self.assertEqual(cells[0][0] + cells[0][2], cells[1][0])
        # 两排不能重叠：下排顶边正好接上排底边
        self.assertEqual(cells[2][1], cells[0][1] + cells[0][3])
        # 右下角必须精确落在工作区右下角
        self.assertEqual(cells[3][0] + cells[3][2], 1921)
        self.assertEqual(cells[3][1] + cells[3][3], 1081)
        for (hwnd, x, y, w, h), (bx, by, bw, bh) in zip(placed, cells):
            self.assertGreaterEqual(x, bx)
            self.assertGreaterEqual(y, by)
            self.assertLessEqual(x + w, bx + bw)
            self.assertLessEqual(y + h, by + bh)

    def test_explicit_hwnds(self):
        ok, placed = _run(list(self.WINS), [_Mon()],
                          tl_hwnd="D::1", tr_hwnd="C::1",
                          bl_hwnd="B::1", br_hwnd="A::1")
        self.assertTrue(ok)
        self.assertEqual([p[0] for p in placed],
                         ["D::1", "C::1", "B::1", "A::1"])

    def test_insufficient_windows(self):
        ok, placed = _run(list(self.WINS)[:3], [_Mon()])
        self.assertFalse(ok)
        self.assertEqual(placed, [], "窗口不足四个时不应摆放任何窗口")

    def test_monitor_offset(self):
        """指定非主屏时，四宫格应落在该屏工作区内。"""
        ok, placed = _run(list(self.WINS),
                          [_Mon(), _Mon(left=1920, top=0, name="M2")],
                          monitor=1)
        self.assertTrue(ok)
        wl, wt, ww, wh = 1920, 0, 1920, 1080
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0], (wl, wt, ww // 2, wh // 2))
        self.assertEqual(cells[3], (wl + ww // 2, wt + wh // 2,
                                    ww - ww // 2, wh - wh // 2))
        for (hwnd, x, y, w, h), (bx, by, bw, bh) in zip(placed, cells):
            self.assertGreaterEqual(x, bx)
            self.assertLessEqual(x + w, bx + bw)

    def test_bottom_row_starts_below_measured_top(self):
        """上排有最小高度缩不下去时，下排必须贴着实测底边走，不能重叠。"""
        wl, wt, ww, wh = 0, 0, 1920, 1080
        half_h = wh // 2
        ok, placed = _run(
            list(self.WINS), [_Mon()],
            # 左上/右上被系统钳制在 600 / 570 高（真机实测值）
            # 宽度按 _exact 的同一约定少 1px（AX 读回比实际少 1），表示「左列
            # 高度被钳制、宽度正好是理想左半屏」，右列起点应落在理想分割点
            readback={"A::1": (wl, wt, ww // 2 - 1, 600),
                      "B::1": (wl + ww // 2, wt, ww - ww // 2, 570)})
        self.assertTrue(ok)
        # 新语义：格子严格四等分铺满，窗口在格子里等比缩放。被最小尺寸钳制
        # 的窗口只会比 fit 尺寸大，但方位上仍贴住自己那一格该贴的边。
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0], (wl, wt, ww // 2, half_h))
        self.assertEqual(cells[1], (wl + ww // 2, wt, ww - ww // 2, half_h))
        self.assertEqual(cells[2], (wl, wt + half_h, ww // 2, wh - half_h))
        self.assertEqual(cells[3],
                         (wl + ww // 2, wt + half_h, ww - ww // 2, wh - half_h))
        # 上排两个窗口都必须在各自格子里
        for (hwnd, x, y, w, h), (bx, by, bw, bh) in zip(placed[:2], cells[:2]):
            self.assertGreaterEqual(x, bx)
            self.assertGreaterEqual(y, by)
            self.assertLessEqual(x + w, bx + bw)
            self.assertLessEqual(y + h, by + bh)

    def test_clamped_when_readback_insane(self):
        """读回值异常（如读到别的窗口/0）时，下排仍必须留在屏内。"""
        ok, placed = _run(list(self.WINS), [_Mon()],
                          readback={"A::1": (0, 0, 0, 0)})
        self.assertTrue(ok)
        wt, wh = 0, 1080
        # 读回异常也不能把下排顶出工作区
        last_c = [p for p in placed if p[0] == "C::1"][-1]
        self.assertLessEqual(last_c[2], max(wt + 1, (wt + wh) - 1))
        self.assertGreaterEqual(last_c[2], wt + 1)
        self.assertGreaterEqual(last_c[3], 1)
        # 四格仍严格铺满
        self.assertEqual(wm._last_quad_boxes[2], (0, wh // 2, 960, wh - wh // 2))

    def test_bottom_right_avoids_clamped_bottom_left(self):
        """下排左格被最小宽度撑宽时，右下格必须贴实测定界，两格不能压盖。

        上排早就有这层保护，下排原先没有：左下写入后不管实测多宽，右下仍按
        理想 half_w 起排，两格在水平方向重叠——用户看到的就是「右下窗口盖住
        左下」。这里给左下返回一个比请求更宽的实测矩形，断言右下起点避开了它。
        """
        wl, ww = 0, 1920
        half_w = ww // 2
        bl_rect = (wl, 541, 1000, 539)     # 实测比请求的 960 宽出 40px
        ok, placed = _run(list(self.WINS), [_Mon()], readback={"C::1": bl_rect})
        self.assertTrue(ok)
        last_d = [p for p in placed if p[0] == "D::1"][-1]
        # 新语义：格子固定，右下窗口右缘贴住格子右边（不越格）
        self.assertLessEqual(last_d[1] + last_d[3], wl + ww)
        self.assertLessEqual(last_d[1], wl + ww)
        # 左下被顶宽到 1000 时右下仍不越格：右缘贴住格子右边
        cells = wm._last_quad_boxes
        self.assertLessEqual(last_d[1] + last_d[3],
                             cells[3][0] + cells[3][2])

    def test_bottom_right_stays_inside_screen(self):
        """左下实测宽到满屏（异常值）时，右下仍要留在屏内且有正宽度。"""
        ok, placed = _run(list(self.WINS), [_Mon()],
                          readback={"C::1": (0, 541, 99999, 539)})
        self.assertTrue(ok)
        last_d = [p for p in placed if p[0] == "D::1"][-1]
        self.assertGreaterEqual(last_d[1], 0)
        self.assertLessEqual(last_d[1], 1920)
        self.assertGreater(last_d[3], 0)
        self.assertLessEqual(last_d[1] + last_d[3], 1920)

    def test_reports_actual_assignment_order(self):
        """排列完成后必须回报「谁实际去了哪个角」。

        智能模式会按窗口宽高比重排四个角的归属；界面若继续显示用户排的
        顺序，看到的就是「不准」。后端把实际顺序写进 _last_quad_order，
        界面据此回写下拉与网格。
        """
        ok, placed = _run(list(self.WINS), [_Mon()],
                          tl_hwnd="D::1", tr_hwnd="C::1",
                          bl_hwnd="B::1", br_hwnd="A::1")
        self.assertTrue(ok)
        # 回报的顺序必须与实际写入的顺序逐一对应（不是用户传入的顺序）
        self.assertEqual([p[0] for p in placed],
                         ["D::1", "C::1", "B::1", "A::1"])
        self.assertEqual(wm._last_quad_order,
                         ["D::1", "C::1", "B::1", "A::1"])

    def test_right_column_follows_measured_left(self):
        """左列被最小宽度撑宽时，右列必须贴着实测左边界，不能重叠。"""
        wl, wt, ww, wh = 0, 0, 1920, 1080
        col_w = int(round(ww * 0.3))          # 请求左列 576
        # 真机实测：ChatGPT 请求 454 读回 480（最小窗口宽度钳制）
        ok, placed = _run(list(self.WINS), [_Mon()], split_x=0.3,
                          readback={"A::1": (wl, wt, 480, 400)})
        self.assertTrue(ok)
        # 新语义：格子按 split_x 固定，左列被 App 顶宽不影响右列起点
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0][0], wl)
        self.assertEqual(cells[0][2], col_w)
        self.assertEqual(cells[1][0], wl + col_w)
        self.assertEqual(cells[1][2], ww - col_w)
        # 右上窗口左缘贴在格子左边（不越格）
        last_b = [p for p in placed if p[0] == "B::1"][-1]
        self.assertLessEqual(last_b[1], cells[1][0] + 8)
        # 四个窗口都落在各自格子里
        for (hwnd, x, y, w, h), (bx, by, bw, bh) in zip(placed, cells):
            self.assertGreaterEqual(x, bx - 8)
            self.assertLessEqual(x + w, bx + bw + 8)

    def test_all_four_stay_on_one_screen_when_rows_too_tall(self):
        """真机故障回归：屏装不下四格最小高度时，四个窗口必须仍留在同一屏。

        真机现场是 RV200 Pro（工作区 1600x900）：上排 ChatGPT 最小高 600、
        下排 Qoder CN 最小高 540，四等分后下排只有 450 高。旧实现让下排为了
        满足 540 的下限往下撑、越过屏幕底边，macOS 随即把整个窗口判给别的屏
        ——用户看到的就是「排列四个窗口不在一个屏幕上」。

        修复后下排高度必须保住自己的下限（宁可两排重叠），四格的顶边 + 高度
        一律不得越过工作区底边。
        """
        wl, wt, ww, wh = 0, 0, 1600, 900
        mins = {"A::1": (500, 600), "B::1": (400, 570),
                "C::1": (720, 540), "D::1": (600, 420)}
        ok, placed = _run(list(self.WINS),
                          [_Mon(width=ww, height=wh)], min_sizes=mins)
        self.assertTrue(ok)
        # 新语义：格子严格四等分铺满；窗口 fit 进格子，被最小尺寸顶大时
        # 方位贴边，底边绝不越出工作区（那是 macOS 搬屏的触发条件）
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0], (wl, wt, ww // 2, wh // 2))
        self.assertEqual(cells[2], (wl, wt + wh // 2, ww // 2, wh - wh // 2))
        for hwnd, x, y, w, h in placed:
            self.assertGreaterEqual(x, wl, "%s 越过工作区左边" % hwnd)
            self.assertGreaterEqual(y, wt, "%s 越过工作区顶边" % hwnd)
            self.assertLessEqual(x + w, wl + ww, "%s 越过工作区右边" % hwnd)
            self.assertLessEqual(y + h, wt + wh, "%s 越过工作区底边" % hwnd)

    def test_row_shrinks_other_row_instead_of_overflowing(self):
        """一排被最小尺寸抬高时，另一排让出空间，不允许两侧叠加越界。"""
        ww, wh = 1600, 1000
        mins = {"A::1": (0, 800), "B::1": (0, 800),
                "C::1": (0, 200), "D::1": (0, 200)}
        ok, placed = _run(list(self.WINS),
                          [_Mon(width=ww, height=wh)], min_sizes=mins)
        self.assertTrue(ok)
        # 新语义：格子仍铺满工作区，但切分按实测最小尺寸校正——上排要 800
        # 高就给它 800，下排只需要 200 就拿剩下的 200，两排都不溢出。
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0], (0, 0, ww // 2, 800))
        self.assertEqual(cells[2], (0, 800, ww // 2, 200))
        # 两排高度之和仍等于工作区高（铺满、不留缝）
        self.assertEqual(cells[0][3] + cells[2][3], wh)
        for hwnd, x, y, w, h in placed:
            self.assertLessEqual(y + h, wh, "%s 越界" % hwnd)

    def test_default_split_is_quarter(self):
        """默认 split_x/split_y = 0.5，与旧行为一致（四等分）。"""
        ok, placed = _run(list(self.WINS), [_Mon()])
        self.assertTrue(ok)
        # 上排两格等宽、左右列分界线正中间
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0][2], cells[1][2])
        self.assertEqual(cells[0][0] + cells[0][2], cells[1][0])
        self.assertEqual(cells[0][2], 960)
        self.assertEqual(cells[0][3], 540)

    def test_custom_split_changes_columns(self):
        """split_x 决定左右列宽：左窄右宽。"""
        ok, placed = _run(list(self.WINS), [_Mon()], split_x=0.3)
        self.assertTrue(ok)
        wl, _t, ww, _h = 0, 0, 1920, 1080
        col_w = int(round(ww * 0.3))
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0], (wl, 0, col_w, 540))
        self.assertEqual(cells[1], (wl + col_w, 0, ww - col_w, 540))
        # 下排左列与上排左列同宽（左右两列对齐）
        self.assertEqual(cells[2][0], wl)
        self.assertEqual(cells[2][2], col_w)
        self.assertEqual(cells[3][0], wl + col_w)
        self.assertEqual(cells[3][2], ww - col_w)

    def test_custom_split_changes_rows(self):
        """split_y 决定上下排高：上矮下高。"""
        ok, placed = _run(list(self.WINS), [_Mon()], split_y=0.35)
        self.assertTrue(ok)
        _wl, wt, _ww, wh = 0, 0, 1920, 1080
        row_h = int(round(wh * 0.35))
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0][1], wt)          # 上排贴顶
        self.assertEqual(cells[0][3], row_h)
        self.assertEqual(cells[2][1], wt + row_h)

    def test_split_is_clamped(self):
        """比例贴边（0/1）时不许把某格压成几个像素。"""
        ww, wh = 1920, 1080
        for bad in (0.0, 1.0, -5, 99):
            ok, placed = _run(list(self.WINS), [_Mon()], split_x=bad)
            self.assertTrue(ok)
            # 左列宽至少 15%（钳制下限），不会出现 0 宽
            self.assertGreaterEqual(wm._last_quad_boxes[0][2], int(ww * 0.15) - 1)
            self.assertGreaterEqual(wm._last_quad_boxes[1][2], int(ww * 0.15) - 1)
        for bad in (0.0, 1.0, -5, 99):
            ok, placed = _run(list(self.WINS), [_Mon()], split_y=bad)
            self.assertTrue(ok)
            self.assertGreaterEqual(wm._last_quad_boxes[0][3], int(wh * 0.15) - 1)


def _run_fill(wins, mons, **kw):
    """同 _run，但跑 fill_all_monitors，返回 (done, placed)。"""
    placed = []
    prev = {}
    for attr in ("list_target_windows", "monitors", "_find_window",
                 "get_window_rect", "set_window_rect", "push_undo"):
        prev[attr] = getattr(wm, attr)

    class _Mons:
        def enum_monitors(self):
            return mons

    wm.monitors = _Mons()
    wm.list_target_windows = lambda: wins
    wm._find_window = lambda hwnd: next(
        (w for w in wins if w["hwnd"] == hwnd), None)
    wm.get_window_rect = lambda hwnd, *a, **k: (0, 0, 0, 0)
    wm.push_undo = lambda *a, **k: None

    def _place(hwnd, x, y, w, h, activate=True):
        placed.append((hwnd, x, y, w, h))
        return True

    wm.set_window_rect = _place
    try:
        done = wm.fill_all_monitors(**kw)
    finally:
        for k, v in prev.items():
            setattr(wm, k, v)
    return done, placed


class TestFillAllMonitors(unittest.TestCase):
    """跨屏铺满：窗口与屏一一对应，且能只铺勾选的屏。"""

    WINS = [_win("A::1"), _win("B::1"), _win("C::1"), _win("D::1")]
    # 三屏：横、横、竖（用户的 DELL U2414H 就是 1080x1920）
    MONS = [_Mon(width=1512, height=982),
            _Mon(left=1512, width=1600, height=900, name="M2"),
            _Mon(left=3112, width=1080, height=1920, name="竖屏")]

    def test_default_spreads_to_all_screens(self):
        done, placed = _run_fill(list(self.WINS), list(self.MONS))
        self.assertEqual(done, 3)
        self.assertEqual([p[0] for p in placed], ["A::1", "B::1", "C::1"])
        # 每块屏用自己的工作区尺寸，左上角对齐
        self.assertEqual(placed[0], ("A::1", 0, 0, 1512, 982))
        self.assertEqual(placed[1], ("B::1", 1512, 0, 1600, 900))
        self.assertEqual(placed[2], ("C::1", 3112, 0, 1080, 1920))

    def test_portrait_excluded_by_indices(self):
        """只勾横屏时，竖屏不该被铺到。"""
        done, placed = _run_fill(list(self.WINS), list(self.MONS),
                                 monitor_indices=[0, 1])
        self.assertEqual(done, 2)
        self.assertEqual([p[0] for p in placed], ["A::1", "B::1"])
        # 没有一个窗口落在竖屏工作区（宽不会等于 1080 那条）
        self.assertNotIn(1080, {p[3] for p in placed})

    def test_portrait_only_when_selected(self):
        """显式只勾竖屏时，窗口才铺到竖屏。"""
        done, placed = _run_fill(list(self.WINS), list(self.MONS),
                                 monitor_indices=[2])
        self.assertEqual(done, 1)
        self.assertEqual(placed[0], ("A::1", 3112, 0, 1080, 1920))

    def test_order_follows_selection(self):
        """勾选顺序即铺屏顺序，与界面一一对应。"""
        done, placed = _run_fill(list(self.WINS), list(self.MONS),
                                 monitor_indices=[1, 0])
        self.assertEqual(done, 2)
        self.assertEqual(placed[0], ("A::1", 1512, 0, 1600, 900))
        self.assertEqual(placed[1], ("B::1", 0, 0, 1512, 982))

    def test_quad_mode_fills_top_left_quarter(self):
        done, placed = _run_fill(list(self.WINS), list(self.MONS),
                                 mode="quad")
        self.assertEqual(placed[0], ("A::1", 0, 0, 1512 // 2, 982 // 2))

    def test_fewer_windows_than_screens(self):
        """窗口比屏少时只铺已有的，done 反映实际铺了几屏。"""
        done, placed = _run_fill(list(self.WINS)[:2], list(self.MONS))
        self.assertEqual(done, 2)
        self.assertEqual(len(placed), 2)

    def test_invalid_indices_are_dropped(self):
        """越界/重复/非整数下标都应被忽略，不落到错误的屏上。"""
        done, placed = _run_fill(list(self.WINS), list(self.MONS),
                                 monitor_indices=[0, 99, 0, "x", -1])
        self.assertEqual(done, 1)
        self.assertEqual(placed[0], ("A::1", 0, 0, 1512, 982))

    def test_empty_indices_spreads_nothing(self):
        """一个屏都没勾时什么都不铺，也不报错。"""
        done, placed = _run_fill(list(self.WINS), list(self.MONS),
                                 monitor_indices=[])
        self.assertEqual(done, 0)
        self.assertEqual(placed, [])


if __name__ == "__main__":
    unittest.main()
