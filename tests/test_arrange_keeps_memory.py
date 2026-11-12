#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排列之后必须保留「撤销移动」与「回摆放位」两份记忆。

背景：set_window_rect 在真正搬窗口时会 push_undo（移动前几何）与
remember_arranged（摆到的位置）。这两份记忆分别是「撤销移动」和
「回摆放位」按钮的唯一数据源。

曾经在四个排列处理器（并排 / 竖排 / 四宫格 / 虚拟网格）成功后立刻对刚排过
的窗口调 release_window，理由是「让用户手动拖动不被吸附回去」。但
_last_arranged 只被显式的 return_to_last_arranged 读取，没有任何自动路径会
拉窗口回去——那次释放毫无收益，却把刚写好的两份记忆删干净，等于排列一次
就永久废掉「撤销移动」和「回摆放位」。

这里的假后端 snap_* 复刻真实副作用（写两份记忆），release_window 用真的，
所以断言的是真实记忆的存亡，不是 mock 的调用次数。
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend as b  # noqa: E402

try:  # ui 依赖 tkinter；无显示环境下降级跳过界面层测试
    import ui  # noqa: E402
except Exception:  # noqa: BLE001
    ui = None

os.environ.setdefault("MMM_ADVANCED_UI", "1")

# 排列记忆的真实归属模块（macOS 是 windows_mac，Windows 是 windows）
wm = sys.modules[b.release_window.__module__] if b.release_window else None

PRE = (0, 0, 100, 100)          # 移动前几何 -> 撤销栈
ARRANGED = (10, 20, 800, 600)   # 摆到的位置 -> 回摆放位


class _Var:
    def __init__(self, v=""):
        self.v = v

    def get(self):
        return self.v

    def set(self, x):
        self.v = x


class _Flag:
    def __init__(self, v=False):
        self.v = v

    def get(self):
        return self.v


def _remember(hwnds):
    """复刻 set_window_rect 的记忆副作用：两份都写。"""
    for h in hwnds:
        if h:
            wm.push_undo(h, PRE)
            wm.remember_arranged(h, ARRANGED)


def _undo_hwnds():
    with wm._undo_lock:
        return [h for h, _ in wm._undo_stack]


def _base_self():
    s = types.SimpleNamespace()
    s.topmost = _Flag(False)
    s.root = types.SimpleNamespace(attributes=lambda *a, **k: None)
    s.status_var = _Var("")
    s.monitors = []
    s._monitor_map = {"主屏": 0}
    return s


@unittest.skipIf(ui is None, "无 tkinter，跳过界面层测试")
@unittest.skipIf(wm is None, "当前平台无 release_window，跳过")
class ArrangeKeepsMemoryTest(unittest.TestCase):
    def setUp(self):
        self._orig = (b.snap_two_side_by_side, b.snap_three_stack,
                      b.snap_four_quad, b.fill_grid,
                      getattr(b, "quad_predict", None),
                      ui.messagebox.showwarning)
        ui.messagebox.showwarning = lambda *a, **k: None
        b.quad_predict = lambda **kw: ([], {})
        with wm._undo_lock:
            self._undo_backup = list(wm._undo_stack)
            wm._undo_stack.clear()
        with wm._last_arranged_lock:
            self._arranged_backup = dict(wm._last_arranged)
            wm._last_arranged.clear()

    def tearDown(self):
        (b.snap_two_side_by_side, b.snap_three_stack,
         b.snap_four_quad, b.fill_grid, b.quad_predict,
         ui.messagebox.showwarning) = self._orig
        with wm._undo_lock:
            wm._undo_stack[:] = self._undo_backup
        with wm._last_arranged_lock:
            wm._last_arranged.clear()
            wm._last_arranged.update(self._arranged_backup)

    def _assert_memory_intact(self, hwnds):
        for h in hwnds:
            self.assertEqual(
                wm.last_arranged(h), ARRANGED,
                f"{h} 的「回摆放位」记忆被排列本身删掉了")
            self.assertIn(
                h, _undo_hwnds(),
                f"{h} 的「撤销移动」记录被排列本身删掉了")

    def test_side_by_side_keeps_memory(self):
        """并排左右之后，两个窗口仍可撤销、仍可回摆放位。"""
        hwnds = ["A::left", "B::right"]

        def snap(**kw):
            _remember(hwnds)
            return True

        b.snap_two_side_by_side = snap
        s = _base_self()
        s._target_map = {"左窗": hwnds[0], "右窗": hwnds[1]}
        s.sbs_left_var = _Var("左窗")
        s.sbs_right_var = _Var("右窗")
        s.sbs_mon_var = _Var("主屏")
        ui.App._snap_two_side_by_side(s)
        self._assert_memory_intact(hwnds)

    def test_three_stack_keeps_memory(self):
        """竖排上中下之后，三个窗口仍可撤销、仍可回摆放位。"""
        hwnds = ["A::top", "B::mid", "C::bot"]

        def snap(**kw):
            _remember(hwnds)
            return True

        b.snap_three_stack = snap
        s = _base_self()
        s._target_map = {"上": hwnds[0], "中": hwnds[1], "下": hwnds[2]}
        s.stack_top_var = _Var("上")
        s.stack_mid_var = _Var("中")
        s.stack_bot_var = _Var("下")
        s.stack_mon_var = _Var("主屏")
        ui.App._snap_three_stack(s)
        self._assert_memory_intact(hwnds)

    def test_four_quad_keeps_memory(self):
        """2×2 四宫格之后，四个窗口仍可撤销、仍可回摆放位。"""
        hwnds = ["A::tl", "B::tr", "C::bl", "D::br"]
        labels = ["左上窗", "右上窗", "左下窗", "右下窗"]
        short_map = dict(zip(labels, hwnds))
        short_map[ui.AUTO_TARGET] = None
        bm = types.SimpleNamespace(
            _last_quad_splits=(0.5, 0.5),
            _last_quad_slots=list(enumerate(hwnds)),
            _last_quad_unwritable=[], _last_quad_report=[],
            _last_quad_notes=[])

        def snap(**kw):
            _remember(hwnds)
            return True

        b.snap_four_quad = snap
        s = _base_self()
        s.quad_tl_var, s.quad_tr_var = _Var(labels[0]), _Var(labels[1])
        s.quad_bl_var, s.quad_br_var = _Var(labels[2]), _Var(labels[3])
        s._quad_short_map = short_map
        s._quad_hwnd_short = {h: l for l, h in zip(labels, hwnds)}
        s.quad_mon_var = _Var("主屏")
        s.quad_smart_var = _Flag(False)
        s._quad_split_x = _Var(0.5)
        s._quad_split_y = _Var(0.5)
        s._refresh_quad_grid = lambda: None
        s._update_quad_capacity = lambda: None
        s._quad_backend_mod = lambda: bm
        ui.App._snap_four_quad(s)
        self._assert_memory_intact(hwnds)

    def test_fill_grid_keeps_memory(self):
        """虚拟网格批排之后，被排的窗口仍可撤销、仍可回摆放位。"""
        hwnds = ["A::1", "B::2", "C::3", "D::4"]
        bm = types.SimpleNamespace(
            _last_grid_notes=[],
            _last_grid_report=[(h, ARRANGED, ARRANGED) for h in hwnds])

        def fill(**kw):
            _remember(hwnds)
            return len(hwnds)

        b.fill_grid = fill
        s = _base_self()
        s.grid_smart_var = _Flag(False)
        s._grid_indices = lambda: [0]
        s._grid_layout_args = lambda: (2, 2, None)
        s._quad_backend_mod = lambda: bm
        ui.App._fill_grid(s)
        self._assert_memory_intact(hwnds)


if __name__ == "__main__":
    unittest.main()