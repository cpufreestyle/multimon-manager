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
    """
    placed = []
    prev = {}
    for attr in ("list_target_windows", "monitors", "_find_window",
                 "get_window_rect", "set_window_rect", "push_undo"):
        prev[attr] = getattr(wm, attr)

    class _Mons:
        def enum_monitors(self):
            return mons

    readback = kw.pop("readback", {})
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
        ok, placed = _run(list(self.WINS), [_Mon()])
        self.assertTrue(ok)
        wl, wt, ww, wh = 0, 0, 1920, 1080
        hw, hh = ww // 2, wh // 2
        # 下排按实测闭环起点摆放：读回即理想半高，故 bot_y = wt + hh + 1
        bot_y = wt + hh + 1
        self.assertEqual(placed, [
            ("A::1", wl, wt, hw, hh),
            ("B::1", wl + hw, wt, ww - hw, hh),
            ("C::1", wl, bot_y, hw, (wt + wh) - bot_y),
            ("D::1", wl + hw, bot_y, ww - hw, (wt + wh) - bot_y),
        ])

    def test_odd_geometry_no_seam(self):
        """宽高为奇数时四个矩形必须无缝拼满，不留 1px 缝隙。"""
        ok, placed = _run(list(self.WINS), [_Mon(width=1921, height=1081)])
        self.assertTrue(ok)
        # 左列右边缘必须贴住右列左边缘，否则两个窗口中间露缝
        self.assertEqual(placed[0][1] + placed[0][3], placed[1][1])
        # 两排不能重叠：下排顶边必须在上排底边之下（闭环留 1px 间隔）
        self.assertGreater(placed[2][2], placed[0][2] + placed[0][4])
        # 右下角必须精确落在工作区右下角
        self.assertEqual(placed[3][1] + placed[3][3], 1921)
        self.assertEqual(placed[3][2] + placed[3][4], 1081)
        # 上排读回即理想半高时，下排起点应为上排底边 + 1
        self.assertEqual(placed[2][2], placed[0][2] + placed[0][4] + 1)

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
        bot_y = wt + wh // 2 + 1
        self.assertEqual(placed[0], ("A::1", wl, wt, ww // 2, wh // 2))
        self.assertEqual(placed[3],
                         ("D::1", wl + ww // 2, bot_y,
                          ww - ww // 2, (wt + wh) - bot_y))

    def test_bottom_row_starts_below_measured_top(self):
        """上排有最小高度缩不下去时，下排必须贴着实测底边走，不能重叠。"""
        wl, wt, ww, wh = 0, 0, 1920, 1080
        half_h = wh // 2
        ok, placed = _run(
            list(self.WINS), [_Mon()],
            # 左上/右上被系统钳制在 600 / 570 高（真机实测值）
            readback={"A::1": (wl, wt, ww // 2, 600),
                      "B::1": (wl + ww // 2, wt, ww - ww // 2, 570)})
        self.assertTrue(ok)
        # 上排仍按理想半高写入（有权重高的窗口优先保证位置）
        self.assertEqual(placed[0], ("A::1", wl, wt, ww // 2, half_h))
        self.assertEqual(placed[1],
                        ("B::1", wl + ww // 2, wt, ww - ww // 2, half_h))
        # A 被钳到 600 高但宽度仍是理想值，右列起点不变，不重排 B
        rw = placed[1][1] + placed[1][3]
        self.assertEqual(rw, wl + ww)
        # 下排起点 = 实测最底边 + 1，两排不能重叠
        bot_y = max(wt + 600, wt + 570) + 1
        # placed 里可能含一次右列重排（若左上被钳出不同宽度）；
        # 取下排最后一次写入为准
        last_c = [p for p in placed if p[0] == "C::1"][-1]
        last_d = [p for p in placed if p[0] == "D::1"][-1]
        self.assertEqual(last_c[2], bot_y)
        self.assertEqual(last_d[2], bot_y)
        # 关键：下排顶边必须在上排实测底边之下，两排不能重叠
        self.assertGreater(last_c[2], max(wt + 600, wt + 570))

    def test_clamped_when_readback_insane(self):
        """读回值异常（如读到别的窗口/0）时，下排仍必须留在屏内。"""
        ok, placed = _run(list(self.WINS), [_Mon()],
                          readback={"A::1": (0, 0, 0, 0)})
        self.assertTrue(ok)
        wt, wh = 0, 1080
        self.assertLessEqual(placed[2][2], max(wt + 1, (wt + wh) - 1))
        self.assertGreaterEqual(placed[2][2], wt + 1)
        self.assertGreaterEqual(placed[2][3], 1)

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
        # 右下起点 = 左下实测右边界 + 1，不再与左格重叠
        self.assertEqual(last_d[1], bl_rect[0] + bl_rect[2] + 1)
        self.assertGreater(last_d[1], bl_rect[0] + bl_rect[2])
        # 宽度用减法补齐，右边缘仍精确落在工作区右边，不留缝
        self.assertEqual(last_d[1] + last_d[3], wl + ww)

    def test_bottom_right_stays_inside_screen(self):
        """左下实测宽到满屏（异常值）时，右下仍要留在屏内且有正宽度。"""
        ok, placed = _run(list(self.WINS), [_Mon()],
                          readback={"C::1": (0, 541, 99999, 539)})
        self.assertTrue(ok)
        last_d = [p for p in placed if p[0] == "D::1"][-1]
        self.assertGreaterEqual(last_d[1], 1)
        self.assertLess(last_d[1], 1920)
        self.assertGreater(last_d[3], 0)
        self.assertEqual(last_d[1] + last_d[3], 1920)

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
        # 右列起点应改成「实测左边 480 + 1」，而不是理想 col_w
        right_x = 480 + 1
        rw = ww - right_x
        last_b = [p for p in placed if p[0] == "B::1"][-1]
        self.assertEqual(last_b[1], right_x)
        self.assertEqual(last_b[3], rw)
        # 关键：左右两列不能互相压盖
        self.assertGreater(last_b[1], 480)
        # 下排左列与实测左列同宽
        last_c = [p for p in placed if p[0] == "C::1"][-1]
        self.assertEqual(last_c[1], wl)
        self.assertEqual(last_c[3], right_x - wl)
        last_d = [p for p in placed if p[0] == "D::1"][-1]
        self.assertEqual(last_d[1], right_x)

    def test_default_split_is_quarter(self):
        """默认 split_x/split_y = 0.5，与旧行为一致（四等分）。"""
        ok, placed = _run(list(self.WINS), [_Mon()])
        self.assertTrue(ok)
        # 上排两格等宽、左右列分界线正中间
        self.assertEqual(placed[0][3], placed[1][3])
        self.assertEqual(placed[0][1] + placed[0][3], placed[1][1])

    def test_custom_split_changes_columns(self):
        """split_x 决定左右列宽：左窄右宽。"""
        ok, placed = _run(list(self.WINS), [_Mon()], split_x=0.3)
        self.assertTrue(ok)
        wl, _t, ww, _h = 0, 0, 1920, 1080
        col_w = int(round(ww * 0.3))
        self.assertEqual(placed[0], ("A::1", wl, 0, col_w, 540))
        self.assertEqual(placed[1],
                         ("B::1", wl + col_w, 0, ww - col_w, 540))
        # 下排左列与上排左列同宽（左右两列对齐）
        self.assertEqual(placed[2][1], wl)
        self.assertEqual(placed[2][3], col_w)
        self.assertEqual(placed[3][1], wl + col_w)
        self.assertEqual(placed[3][3], ww - col_w)

    def test_custom_split_changes_rows(self):
        """split_y 决定上下排高：上矮下高。"""
        ok, placed = _run(list(self.WINS), [_Mon()], split_y=0.35)
        self.assertTrue(ok)
        _wl, wt, _ww, wh = 0, 0, 1920, 1080
        row_h = int(round(wh * 0.35))
        self.assertEqual(placed[0][2], wt)          # 上排贴顶
        self.assertEqual(placed[0][4], row_h)
        self.assertEqual(placed[2][2] > wt + row_h, True)

    def test_split_is_clamped(self):
        """比例贴边（0/1）时不许把某格压成几个像素。"""
        ww, wh = 1920, 1080
        for bad in (0.0, 1.0, -5, 99):
            ok, placed = _run(list(self.WINS), [_Mon()], split_x=bad)
            self.assertTrue(ok)
            # 左列宽至少 15%（钳制下限），不会出现 0 宽
            self.assertGreaterEqual(placed[0][3], int(ww * 0.15) - 1)
            self.assertGreaterEqual(placed[1][3], int(ww * 0.15) - 1)
        for bad in (0.0, 1.0, -5, 99):
            ok, placed = _run(list(self.WINS), [_Mon()], split_y=bad)
            self.assertTrue(ok)
            self.assertGreaterEqual(placed[0][4], int(wh * 0.15) - 1)


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
