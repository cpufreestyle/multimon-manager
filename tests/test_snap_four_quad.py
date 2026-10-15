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
    # 每例都拿一份窗口副本：fake 写入会把新几何写回窗口 dict（CG 枚举要跟着
    # 变，否则「写入无效」的判据全落空），共用 WINS 会把上一例的几何带给下一例
    wins = [dict(w) for w in wins]
    placed = []
    prev = {}
    for attr in ("list_target_windows", "monitors", "_find_window",
                 "get_window_rect", "set_window_rect", "push_undo",
                 "_osa", "_probe_min_size", "list_windows_front_to_back",
                 "_readback_rect", "_gap_settings"):
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
    # 窗口间距固定成 12/False：下面各例的期望几何是按这个值算出来的，不能跟着
    # 开发机 settings.json 漂（用户改一次间距，全套数值就全错）
    wm._gap_settings = lambda: (12, False)
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
        # CG 枚举同步：snap_four_quad 用 CG 几何判断「写入有没有生效」（App 不
        # 开放自动化时 AX 读回会失败，只有 CG 还看得见窗口）。fake 写入不更新
        # 枚举的话，所有窗口都会被误判成写入无效。指定了 readback 的窗口按
        # readback 走——那是在模拟「App 把尺寸钳制在别处」。
        got = readback.get(hwnd) or (x, y, w, h)
        for win in wins:
            if win["hwnd"] == hwnd:
                win["x"], win["y"], win["w"], win["h"] = got
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

    def test_three_windows_are_arranged_not_rejected(self):
        """桌面上只有三个窗口也排：放不下四个不该等于什么都不做。

        真机常态是四个放不下，用户也会在网格里点掉几格只排两三个——「不足四个
        就失败」把这个功能在 1512x896 这类小屏上基本废掉了。
        """
        ok, placed = _run(list(self.WINS)[:3], [_Mon()])
        self.assertTrue(ok)
        self.assertEqual([p[0] for p in placed], ["A::1", "B::1", "C::1"])
        # 三格是 T 形：左上/右上各半屏，左下拉通整宽。旧实现把右下留成整块
        # 四分之一屏，用户看到的就是一块没人用的死区
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0], (0, 0, 960, 540))
        self.assertEqual(cells[1], (960, 0, 960, 540))
        self.assertEqual(cells[2], (0, 540, 1920, 540))
        self.assertEqual(cells[3], (960, 540, 0, 540))
        self.assertNotIn("D::1", {p[0] for p in placed})

    def test_only_one_window_fails(self):
        """一个窗口排不出四宫格：明确失败，且不摆任何窗口。"""
        ok, placed = _run(list(self.WINS)[:1], [_Mon()])
        self.assertFalse(ok)
        self.assertEqual(placed, [])

    def test_two_explicit_windows_fill_the_screen(self):
        """只选两个角（网格里点掉另外两个）就只排两个，并且铺满整屏。"""
        ok, placed = _run(list(self.WINS), [_Mon()],
                          tl_hwnd="A::1", tr_hwnd="B::1")
        self.assertTrue(ok)
        self.assertEqual([p[0] for p in placed], ["A::1", "B::1"])
        a, b = placed
        # placed 元组是 (hwnd, x, y, w, h)
        self.assertEqual((a[1], a[2]), (0, 0))
        self.assertEqual(a[2] + a[4], 1080)        # 高铺满
        self.assertEqual(b[1] + b[3], 1920)        # 右缘贴工作区右边
        self.assertEqual(b[2] + b[4], 1080)
        self.assertEqual(b[1] - (a[1] + a[3]), 12)  # 中间只隔一个窗口间距
        # 只有上排有窗口：横向分割线贴到底边（100%）
        self.assertAlmostEqual(wm._last_quad_splits[1], 1.0)

    def test_explicit_column_keeps_left_column(self):
        """显式选左上 + 左下：按选中的角排（左列），不压缩成上排。"""
        ok, placed = _run(list(self.WINS), [_Mon()],
                          tl_hwnd="A::1", bl_hwnd="C::1")
        self.assertTrue(ok)
        self.assertEqual([p[0] for p in placed], ["A::1", "C::1"])
        a, b = placed
        self.assertEqual(a[1] + a[3], 1920)        # 上块铺满宽
        self.assertEqual(b[1] + b[3], 1920)        # 下块铺满宽
        self.assertEqual(b[2] - (a[2] + a[4]), 12)
        # 只有左列有窗口：竖向分割线贴在右边（100%）
        self.assertAlmostEqual(wm._last_quad_splits[0], 1.0)

    def test_explicit_three_leave_fourth_corner_empty(self):
        """显式选三个：第四个角留空，不拿别的窗口补上。"""
        ok, placed = _run(list(self.WINS), [_Mon()],
                          tl_hwnd="A::1", tr_hwnd="B::1", bl_hwnd="C::1")
        self.assertTrue(ok)
        self.assertEqual([p[0] for p in placed], ["A::1", "B::1", "C::1"])
        self.assertNotIn("D::1", {p[0] for p in placed})
        # 下排只有左下：这一格拉通整宽排成 T 形，右下角宽 0（不占地方）
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0], (0, 0, 960, 540))
        self.assertEqual(cells[1], (960, 0, 960, 540))
        self.assertEqual(cells[2], (0, 540, 1920, 540))
        self.assertEqual(cells[3], (960, 540, 0, 540))

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
        ok, placed = _run(
            list(self.WINS), [_Mon()],
            # 左上/右上被系统钳制在 600 / 570 高（真机实测值）
            # 宽度按 _exact 的同一约定少 1px（AX 读回比实际少 1），表示「左列
            # 高度被钳制、宽度正好是理想左半屏」，右列起点应落在理想分割点
            readback={"A::1": (wl, wt, ww // 2 - 1, 600),
                      "B::1": (wl + ww // 2, wt, ww - ww // 2, 570)})
        self.assertTrue(ok)
        # 二趟重排：第一遍按探测到的最小尺寸（这里探测不到，全是 0）切四等分，
        # 写入后读回的是窗口**实际**占的 600 高——比格子（540）还大，说明这一排
        # 装不下。把实测尺寸并回最小尺寸再切一遍：上排长到 600 + 间距 6 = 606，
        # 下排从 606 起，两排不再重叠（旧实现只写一遍，上下重叠 60px）。
        cells = wm._last_quad_boxes
        top_h = 606
        self.assertEqual(cells[0], (wl, wt, ww // 2, top_h))
        self.assertEqual(cells[1], (wl + ww // 2, wt, ww - ww // 2, top_h))
        self.assertEqual(cells[2], (wl, wt + top_h, ww // 2, wh - top_h))
        self.assertEqual(cells[3],
                         (wl + ww // 2, wt + top_h, ww - ww // 2, wh - top_h))
        # 上排两个窗口的最后一次写入都必须落在各自格子里
        last = {}
        for hwnd, x, y, w, h in placed:
            last[hwnd] = (x, y, w, h)
        for hwnd, cell in (("A::1", cells[0]), ("B::1", cells[1])):
            x, y, w, h = last[hwnd]
            self.assertGreaterEqual(x, cell[0])
            self.assertGreaterEqual(y, cell[1])
            self.assertLessEqual(x + w, cell[0] + cell[2])
            self.assertLessEqual(y + h, cell[1] + cell[3])
        # 上排底边不许越过下排顶边：这是「上下重叠」的直接度量
        got = {h: g for h, _w, g in wm._last_quad_report if g}
        self.assertLessEqual(got["A::1"][1] + got["A::1"][3], cells[2][1])
        self.assertLessEqual(got["B::1"][1] + got["B::1"][3], cells[2][1])

    def test_second_pass_lifts_row_when_probe_undersized(self):
        """探测偏小时，二趟重排必须把上排抬到实测高度，两排不能重叠。"""
        wl, wt, ww, wh = 0, 0, 1920, 1080
        ok, placed = _run(
            list(self.WINS), [_Mon()],
            # 最小尺寸探测一个都没探到（全 0），只有左上被钳在 600 高
            readback={"A::1": (wl, wt, ww // 2 - 1, 600)})
        self.assertTrue(ok)
        cells = wm._last_quad_boxes
        # 上排长到 600 + 间距 6，下排从那里起：旧实现只写一遍，上排仍是 540，
        # 左上窗口向下溢出 60px 压住左下——「有些窗口没有吸附到边上」的根因
        self.assertEqual(cells[0][3], 606)
        self.assertEqual(cells[2][1], 606)
        self.assertEqual(cells[2][3], wh - 606)
        # 左上窗口的最后一次写入落在自己格子里
        last = {}
        for hwnd, x, y, w, h in placed:
            last[hwnd] = (x, y, w, h)
        x, y, w, h = last["A::1"]
        self.assertGreaterEqual(x, cells[0][0])
        self.assertGreaterEqual(y, cells[0][1])
        self.assertLessEqual(x + w, cells[0][0] + cells[0][2])
        self.assertLessEqual(y + h, cells[0][1] + cells[0][3])
        # 左上窗口的实测底边不许越过左下窗口的顶边
        got = {h: g for h, _w, g in wm._last_quad_report if g}
        self.assertLessEqual(got["A::1"][1] + got["A::1"][3],
                             got["C::1"][1])

    def test_smart_two_windows_same_row_fill_height(self):
        """两个窗口占同一排：这一排撑满工作区高，不是上下各半屏。"""
        wins = [_win("A::1", x=10, y=20, w=1600, h=400),   # 宽扁 4.0
                _win("B::1", x=2000, y=30, w=600, h=1200)]  # 瘦高 0.5
        ok, placed = _run(wins, [_Mon()],
                          tl_hwnd="A::1", tr_hwnd="B::1", smart=True)
        self.assertTrue(ok)
        self.assertEqual(len(placed), 2)
        # 宽扁的 A 去左上宽格、瘦高的 B 去右上窄格：智能分配按宽高比换角，
        # 且 A 本来就在最左（就近），不横穿屏幕
        slots = dict(wm._last_quad_slots)
        self.assertEqual(slots[0], "A::1")
        self.assertEqual(slots[1], "B::1")
        # 只有上排有窗口：上排撑满整个工作区高，下排高度为 0
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0][3], 1080)
        self.assertEqual(cells[2][3], 0)
        # 左宽右窄：宽扁窗占宽格、瘦高窗占窄格
        self.assertEqual(cells[0], (0, 0, 1248, 1080))
        self.assertEqual(cells[1], (1248, 0, 672, 1080))
        self.assertGreater(cells[0][2], cells[1][2])
        # 界面上的竖向分割线必须报「撑满」（1.0），不能报成 0.85
        self.assertAlmostEqual(wm._last_quad_splits[1], 1.0)

    def test_smart_three_windows_reorder_by_aspect(self):
        """三个窗口也走智能分配：谁去哪个角由宽高比定，不是传入顺序。"""
        wins = [_win("A::1", x=10, y=20, w=1600, h=400),    # 宽扁 4.0
                _win("B::1", x=2000, y=30, w=600, h=1200),  # 瘦高 0.5
                _win("C::1", x=10, y=2000, w=900, h=900)]   # 方圆 1.0
        ok, placed = _run(wins, [_Mon()],
                          tl_hwnd="A::1", tr_hwnd="B::1", bl_hwnd="C::1",
                          smart=True)
        self.assertTrue(ok)
        # 旧实现 plan_quad_smart 严格要求四个窗口，三个直接抛 ValueError 退化成
        # 手动顺序——「智能模式不准」的直接来源。现在三个也按宽高比分配。
        self.assertEqual(len(placed), 3)
        slots = dict(wm._last_quad_slots)
        self.assertEqual(set(slots), {0, 1, 2})
        # 按宽高比各就各位：最宽扁的 A(4.0) 去下拉通整宽的格、方正的 C(1.0)
        # 去左上近方格、瘦高的 B(0.5) 去右上格。实测格子宽高比 1.40 / 1.14 /
        # 5.93，与三个窗口的宽高比同序——最宽扁的那个拿最宽的那格。
        self.assertEqual(slots[0], "C::1")
        self.assertEqual(slots[1], "B::1")
        self.assertEqual(slots[2], "A::1")
        # 与传入顺序不同：这才是「智能」该有的样子（旧实现三个窗口直接抛
        # ValueError 退化成手动顺序，等于没有智能模式）
        self.assertNotEqual([h for _c, h in wm._last_quad_slots],
                            ["A::1", "B::1", "C::1"])
        # T 形：下排只有左下，这一格拉通整宽；右下角宽 0，不留死区
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0], (0, 0, 1056, 756))
        self.assertEqual(cells[1], (1056, 0, 864, 756))
        self.assertEqual(cells[2], (0, 756, 1920, 324))
        self.assertEqual(cells[3], (1056, 756, 0, 324))
        # 两列不重叠、下排接在上排底边
        self.assertEqual(cells[0][0] + cells[0][2], cells[1][0])
        self.assertEqual(cells[2][1], cells[0][1] + cells[0][3])

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
        """格子按 split_x 固定；窗口直接填满自己那一格（只让出间距）。"""
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
        # 新语义：窗口直接填满自己那一格，与单窗 quad-* 吸附同一套语义。
        # B 落在右上格（1344x540）：上缘贴工作区顶边（屏幕边不让缝）、右缘贴
        # 屏幕右缘，左缘与下缘各让出 6px 间距凑成 12px 的缝。
        last_b = [p for p in placed if p[0] == "B::1"][-1]
        # placed 元组是 (hwnd, x, y, w, h)
        self.assertEqual(last_b[1], 582)      # 左缘 = 格子左缘 576 + 6px
        self.assertEqual(last_b[2], 0)        # 上缘 = 工作区顶边
        self.assertEqual(last_b[3], 1338)     # 宽 = 格子宽 1344 - 6px
        self.assertEqual(last_b[4], 534)      # 高 = 格子高 540 - 6px
        # 四个窗口都落在各自格子里（不越格）
        for (hwnd, x, y, w, h), (bx, by, bw, bh) in zip(placed, cells):
            self.assertGreaterEqual(x, bx)
            self.assertGreaterEqual(y, by)
            self.assertLessEqual(x + w, bx + bw)
            self.assertLessEqual(y + h, by + bh)

    def test_all_four_stay_on_one_screen_when_rows_too_tall(self):
        """真机故障回归：屏装不下四格最小尺寸时，只排放得下的且都在同一屏。

        真机现场是 Built-in（工作区 1512x896）：四个窗口的 App 最小尺寸总和
        1824x1318 超过屏幕。旧实现照排四个，结局是上下重叠 503px、左右重叠
        757px，还有一个窗被 macOS 判到别的屏——用户看到的就是「排列四个窗口
        不在一个屏幕上」「四格不准」。

        修复后：只排「并排放得下」的那几个，放不下的原封不动，并如实说明。
        两排最小高度 600+540 再加间距就超过 900，四格必然重叠，所以这里只能
        排两个——宁可少排，也不能排成重叠成一团。
        """
        wl, wt, ww, wh = 0, 0, 1600, 900
        mins = {"A::1": (500, 600), "B::1": (400, 570),
                "C::1": (720, 540), "D::1": (600, 420)}
        ok, placed = _run(list(self.WINS),
                          [_Mon(width=ww, height=wh)], min_sizes=mins)
        self.assertTrue(ok)
        # 只有上排两个窗口放得下（600 + 540 + 间距 > 900，四格必重叠）
        self.assertEqual([p[0] for p in placed], ["A::1", "B::1"])
        # 排放的窗口一个都不许多出工作区（越界就是被 macOS 搬屏的前兆）
        for hwnd, x, y, w, h in placed:
            self.assertGreaterEqual(x, wl, "%s 越过工作区左边" % hwnd)
            self.assertGreaterEqual(y, wt, "%s 越过工作区顶边" % hwnd)
            self.assertLessEqual(x + w, wl + ww, "%s 越过工作区右边" % hwnd)
            self.assertLessEqual(y + h, wt + wh, "%s 越过工作区底边" % hwnd)
        # 放不下的两个窗口必须原封不动：一次写入都不该有
        written = {p[0] for p in placed}
        self.assertNotIn("C::1", written)
        self.assertNotIn("D::1", written)
        # 只有上排两个窗口放得下：这一排撑满整个工作区高（两个窗口并排铺满
        # 屏幕，不留半屏空白），下排高度为 0
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0], (wl, wt, ww // 2, wh))
        self.assertEqual(cells[1], (wl + ww // 2, wt, ww - ww // 2, wh))
        self.assertEqual(cells[2], (wl, wt + wh, ww // 2, 0))
        self.assertEqual(cells[3], (wl + ww // 2, wt + wh, ww - ww // 2, 0))
        # 说明里必须点名谁没排、为什么——用户据此知道该关谁、换谁
        notes = "；".join(wm._last_quad_notes)
        self.assertIn("只排出 2/4", notes)
        self.assertIn("C(最小720x540)", notes)
        self.assertIn("D(最小600x420)", notes)

    def test_all_four_placed_when_min_sizes_fit(self):
        """最小尺寸放得下时仍排四个：子集选择不能把排得下的窗口漏掉。"""
        mins = {"A::1": (500, 400), "B::1": (400, 380),
                "C::1": (700, 300), "D::1": (600, 260)}
        ok, placed = _run(list(self.WINS), [_Mon()], min_sizes=mins)
        self.assertTrue(ok)
        self.assertEqual([p[0] for p in placed],
                         ["A::1", "B::1", "C::1", "D::1"])
        # 四个都排下了，就不该留下任何「放不下」的说明
        self.assertEqual(wm._last_quad_notes, [])

    def test_smart_mode_also_drops_unfittable_windows(self):
        """智能模式同样只排放得下的：比例优化变不出物理空间。"""
        mins = {"A::1": (500, 600), "B::1": (400, 570),
                "C::1": (720, 540), "D::1": (600, 420)}
        ok, placed = _run(list(self.WINS),
                          [_Mon(width=1600, height=900)], min_sizes=mins,
                          smart=True)
        self.assertTrue(ok)
        self.assertEqual(len(placed), 2)
        # 子集选择的意义就在于：排下的两个互不压盖

        def _overlap(p, q):
            dx = min(p[1] + p[3], q[1] + q[3]) - max(p[1], q[1])
            dy = min(p[2] + p[4], q[2] + q[4]) - max(p[2], q[2])
            return max(0, dx) * max(0, dy)

        self.assertEqual(_overlap(placed[0], placed[1]), 0)
        self.assertIn("只排出 2/4", "；".join(wm._last_quad_notes))

    def test_two_windows_fill_the_whole_screen(self):
        """只排放得下两个时，这两个要铺满整屏，不留半屏空白。

        真机数据（Built-in 1512x900）：四个窗口最小宽 700/800/900/896，只有
        700+800 加一个间距刚好放进 1512。旧实现把这两个摆在四宫格的上排、
        下面留 558px 空白，看起来就是「没排好」；现在这一排撑满整高。
        """
        ww, wh = 1512, 900
        mins = {"A::1": (700, 600), "B::1": (800, 600),
                "C::1": (900, 600), "D::1": (896, 600)}
        ok, placed = _run(list(self.WINS),
                          [_Mon(width=ww, height=wh)], min_sizes=mins)
        self.assertTrue(ok)
        self.assertEqual([p[0] for p in placed], ["A::1", "B::1"])
        a, b = placed
        # placed 元组是 (hwnd, x, y, w, h)：两个窗口左右并排铺满工作区
        self.assertEqual((a[1], a[2]), (0, 0))       # 左上角贴工作区
        self.assertEqual(a[2] + a[4], wh)            # 高铺满
        self.assertEqual(b[1] + b[3], ww)            # 右缘贴工作区右边
        self.assertEqual(b[2] + b[4], wh)
        self.assertEqual(b[1] - (a[1] + a[3]), 12)   # 中间只隔一个窗口间距
        # 界面上的分割线：横向贴到底边（100%），纵向按两列实际宽度
        self.assertAlmostEqual(wm._last_quad_splits[1], 1.0)
        self.assertAlmostEqual(wm._last_quad_splits[0], 706 / float(ww), places=3)

    def test_two_windows_stacked_fill_the_whole_width(self):
        """并排宽度不够、只能上下堆时，这两个要铺满整屏的宽。

        与 test_two_windows_fill_the_whole_screen 是同一条规则的另一个方向：
        只有一列有窗口时，这一列撑满整个工作区宽，两块上下铺满。
        """
        ww, wh = 1512, 900
        mins = {"A::1": (900, 400), "B::1": (900, 400),
                "C::1": (2000, 400), "D::1": (2000, 400)}
        ok, placed = _run(list(self.WINS),
                          [_Mon(width=ww, height=wh)], min_sizes=mins)
        self.assertTrue(ok)
        # 900+900 加间距超过 1512，只能上下堆：左上 + 左下
        self.assertEqual([p[0] for p in placed], ["A::1", "B::1"])
        a, b = placed
        # placed 元组是 (hwnd, x, y, w, h)
        self.assertEqual(a[1] + a[3], ww)            # 上块铺满宽
        self.assertEqual(b[1] + b[3], ww)            # 下块铺满宽
        self.assertEqual(a[1], 0)                    # 左缘贴工作区左边
        self.assertEqual(b[2] - (a[2] + a[4]), 12)   # 中间只隔一个窗口间距
        # 只有一列有窗口：右列宽度为 0，界面上的竖向分割线贴在右边
        cells = wm._last_quad_boxes
        self.assertEqual(cells[1], (ww, 0, 0, wh // 2))
        self.assertAlmostEqual(wm._last_quad_splits[0], 1.0)

    def test_unwritable_window_is_reported_as_such(self):
        """写入后一步没动的窗口要如实说「写入无效」，不能算成被最小尺寸钳制。

        真机 CrossPaste：AppleScript 恒报 -1719（窗口根本不开放自动化），
        任何 AX 写入都被忽略，窗口留在原地。旧实现只报一个很大的偏差，状态栏
        写成「被应用最小尺寸钳制」——那是现象不是原因；而且这条判断早先藏在
        「中心不在目标屏」里，中心恰好在目标屏上的写不动窗口就被整条漏掉了。
        """
        ok, placed = _run(list(self.WINS), [_Mon()],
                          readback={"A::1": (10, 20, 100, 100)})
        self.assertTrue(ok)
        self.assertIn("A::1", wm._last_quad_unwritable)
        self.assertIn("写入后位置尺寸都没变", "；".join(wm._last_quad_notes))
        # 写不动不是把它踢出四宫格的理由：角还是它的，只是没排进去
        self.assertEqual(len(placed), 4)

    def test_nothing_fits_leaves_everything_untouched(self):
        """四个窗口都比整块屏还大时一个都不排，也不硬塞。"""
        mins = {h: (3000, 2000)
                for h in ("A::1", "B::1", "C::1", "D::1")}
        ok, placed = _run(list(self.WINS), [_Mon()], min_sizes=mins)
        self.assertTrue(ok)
        self.assertEqual(placed, [])
        self.assertIn("只排出 0/4", "；".join(wm._last_quad_notes))

    def test_row_shrinks_other_row_instead_of_overflowing(self):
        """一排被最小尺寸抬高时，另一排让出空间，不允许两侧叠加越界。"""
        # 屏高 1020：上排要 800、下排要 200，再加 12px 间距 = 1012，
        # 1020 才装得下（1000 装不下，见 test_all_four_stay_on_one_screen）
        ww, wh = 1600, 1020
        mins = {"A::1": (0, 800), "B::1": (0, 800),
                "C::1": (0, 200), "D::1": (0, 200)}
        ok, placed = _run(list(self.WINS),
                          [_Mon(width=ww, height=wh)], min_sizes=mins)
        self.assertTrue(ok)
        # 新语义：格子仍铺满工作区，但切分按实测最小尺寸校正——上排要 800
        # 高就给它 800（外加 6px 间距），下排只需要 200 就拿剩下的，两排都
        # 不溢出、不重叠。
        cells = wm._last_quad_boxes
        self.assertEqual(cells[0], (0, 0, ww // 2, 810))
        self.assertEqual(cells[2], (0, 810, ww // 2, 210))
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

    def test_oversized_writeback_drops_window_and_restores_it(self):
        """写入后实测超容要减载：放不下的窗口放回原状，只排装得下的。

        真机 Shadowrocket：探测最小高 319，写入后被 App 钳在 492。预检按 319
        说这块屏装得下，写进去窗口被撑大，两排需求随之超过工作区——照原样切
        格子只能按需求占比硬分，上下两排互相压住，用户看到的就是「不准」。
        写入后必须复检一次：装不下就减载，并把减载的窗口放回探测前的原状，
        不停在排了一半的位置上。
        """
        ww, wh = 1600, 900
        # 探测值都说放得下：四格需求 1312x572，屏 1600x900 装得下
        mins = {"A::1": (500, 300), "B::1": (400, 280),
                "C::1": (700, 260), "D::1": (600, 240)}
        # D 写入后被 App 撑到 700 高。夹具的 readback 同时充当「排列前的当前
        # 几何」：中心必须落在屏内（否则被判成钉死），且与 wins 里的初始几何
        # 不同（否则写入被判成无效）
        orig = (900, 100, 600, 700)
        wins = [_win("A::1", x=10, y=20), _win("B::1", x=2000, y=30),
                _win("C::1", x=10, y=2000),
                _win("D::1", x=3000, y=20, w=600, h=700)]
        ok, placed = _run(wins, [_Mon(width=ww, height=wh)],
                          tl_hwnd="A::1", tr_hwnd="B::1",
                          bl_hwnd="C::1", br_hwnd="D::1",
                          min_sizes=mins, readback={"D::1": orig})
        self.assertTrue(ok)
        # 只排装得下的三个，D 退出排列——不是四个硬排成重叠成一团
        self.assertEqual([h for _c, h in wm._last_quad_slots],
                         ["A::1", "B::1", "C::1"])
        # 说明里点名谁没排、为什么：用户据此知道该关谁、换谁
        notes = "；".join(wm._last_quad_notes)
        self.assertIn("只排出 3/4", notes)
        self.assertIn("D(最小600x700)", notes)
        # 被 App 钳制不是写不动：D 不该被报成「写入无效」
        self.assertNotIn("D::1", wm._last_quad_unwritable)
        # 减载窗口的最后一次写入 = 放回探测前的原状
        last = {}
        for hwnd, x, y, w, h in placed:
            last[hwnd] = (x, y, w, h)
        self.assertEqual(last["D::1"], orig)
        # 排下的三个互不压盖，且一个都不越出工作区
        keep = [last[h] for h in ("A::1", "B::1", "C::1")]
        for x, y, w, h in keep:
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, ww)
            self.assertLessEqual(y + h, wh)
        for p, q in ((keep[0], keep[1]), (keep[0], keep[2]),
                     (keep[1], keep[2])):
            dx = min(p[0] + p[2], q[0] + q[2]) - max(p[0], q[0])
            dy = min(p[1] + p[3], q[1] + q[3]) - max(p[1], q[1])
            self.assertEqual(max(0, dx) * max(0, dy), 0)

    def test_writeback_within_gap_tolerance_keeps_all_four(self):
        """读回只比目标大、但不比格子大时不减载：四个照排，不留任何说明。

        超容判据是「比格子还大 8px 以上」而不是「比目标大」：目标已让出窗口
        间距，拿间距噪声当超容会触发无谓的重排与减载——用户看到的就是「该排
        四个却只排了三个」。
        """
        ww, wh = 1600, 900
        mins = {"A::1": (500, 300), "B::1": (400, 280),
                "C::1": (700, 260), "D::1": (600, 240)}
        # 读回 = 格子原大：比让出间距后的目标大 6px，但不比格子大
        readback = {"A::1": (0, 0, 800, 450), "B::1": (800, 0, 800, 450),
                    "C::1": (0, 450, 800, 450), "D::1": (800, 450, 800, 450)}
        ok, placed = _run(list(self.WINS), [_Mon(width=ww, height=wh)],
                          tl_hwnd="A::1", tr_hwnd="B::1",
                          bl_hwnd="C::1", br_hwnd="D::1",
                          min_sizes=mins, readback=readback)
        self.assertTrue(ok)
        self.assertEqual([p[0] for p in placed],
                         ["A::1", "B::1", "C::1", "D::1"])
        self.assertEqual(len(wm._last_quad_slots), 4)
        # 不减载就不留「放不下」的说明
        self.assertEqual(wm._last_quad_notes, [])
        # 每个窗口只写一次，落在让出间距后的格子里：左列贴左、上排贴顶、
        # 右列贴右、下排贴底，相邻两窗之间正好隔一个窗口间距（12px）
        self.assertEqual(placed, [
            ("A::1", 0, 0, 794, 444), ("B::1", 806, 0, 794, 444),
            ("C::1", 0, 456, 794, 444), ("D::1", 806, 456, 794, 444)])


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
