#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 整数句柄 vs macOS 复合串：界面消费后端报告的链路不能崩。

历史故障（真机复现）：macOS 侧窗口标识是「应用::窗口」复合串，Windows 侧
是整数 HWND。ui.py 里几处消费后端报告的地方直接 split("::")，在 Windows
上抛 AttributeError，而 Tk 把 traceback 吞在 --noconsole 里，表现为「点了
没反应」：

1. 吸附状态栏 _report_snap_status：每次吸附后崩，状态栏永远不更新；
2. 四宫格 _snap_four_quad：有窗口被最小尺寸钳制时崩，顺带把 800ms 的
   托盘命令轮询链整个带走（after 链断掉后打开/刷新/退出全部静默失效，
   只能重启程序）；
3. 布局还原 _apply_layout_by_name：一还原就崩，布局功能在 Windows 上
   完全不可用。

这里建真界面，喂整数句柄的假报告跑这三条链路，要求不抛异常且状态栏给出
可读反馈；轮询链还要能在单条命令出错后继续轮询。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_harness as harness  # noqa: E402 - 抑制构造期真机热键/轮询

try:
    import tkinter as tk
    import ui
    import cmd_channel
except Exception:  # noqa: BLE001  无 tkinter / 无显示环境：整类跳过
    tk = None
    ui = None
    cmd_channel = None

if tk is None:
    raise unittest.SkipTest("tkinter 不可用，跳过界面单测")


class _Mon:
    def __init__(self, left, top, width, height, name, primary=False):
        self.left, self.top = left, top
        self.width, self.height = width, height
        self.is_primary = primary
        self.device_name = name
        self.device_path = name
        self.work_rect = (left, top, width, height)
        self.work_left, self.work_top = left, top
        self.work_width, self.work_height = width, height


class TestUiHwndType(unittest.TestCase):
    """后端报告里的窗口标识：整数句柄（Windows）与复合串（macOS）都要能消化。"""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()      # 不抢焦点
        # 走无头夹具：构造期会注册 7 个 after 定时器（真机全局热键/轮询），
        # 直接构造会让单测碰到用户机器，也会把界面测试跑挂。
        self.app = harness.build_app(ui, self.root)
        self.addCleanup(lambda: harness.close_app(self.root, self.app))
        # 测试期间绝不让这个假界面的轮询去 take 真命令通道的命令——
        # 那条通道与正在运行的应用共享，误 take 会打乱真实程序
        self._saved = [(cmd_channel, "take", cmd_channel.take)]
        cmd_channel.take = lambda: None
        for mod, names in ((ui.b, ("list_target_windows", "set_window_rect",
                                   "snap_four_quad", "quad_predict")),
                           (ui.layouts, ("load_layout",)),
                           (ui.settings, ("load", "save"))):
            for n in names:
                self._saved.append((mod, n, getattr(mod, n)))
        self.addCleanup(self._restore)

    def _restore(self):
        for mod, name, fn in self._saved:
            setattr(mod, name, fn)

    # ---------- 名字转换 ----------

    def test_quad_hwnd_name_handles_both_id_shapes(self):
        """复合串拆应用名；整数句柄不能崩，且给出非空名字。"""
        self.assertEqual(self.app._quad_hwnd_name("Safari::文档"), "Safari")
        self.assertEqual(self.app._quad_hwnd_name(""), "窗口")
        self.assertEqual(self.app._quad_hwnd_name(None), "")
        name = self.app._quad_hwnd_name(424242)
        self.assertIsInstance(name, str)
        self.assertTrue(name)

    # ---------- 1. 吸附状态栏 ----------

    def test_report_snap_status_with_int_hwnd(self):
        """整数句柄的吸附报告：不抛异常，状态栏写出窗口与偏差。"""
        ui.b.last_snap_report = [(424242, (0, 0, 960, 540), (0, 0, 1000, 600))]
        self.app._report_snap_status("left")
        status = self.app.status_var.get()
        self.assertIn("424242", status)
        self.assertIn("钳制", status)

    # ---------- 2. 四宫格状态栏 + 轮询链 ----------

    def test_snap_four_quad_clamped_report_keeps_ui_alive(self):
        """有窗口被钳制/写入无效时：状态栏出明细，且轮询链不断。"""
        ui.b.quad_predict = lambda **kw: ([], {})
        ui.b.snap_four_quad = lambda **kw: True
        bm = self.app._quad_backend_mod()
        names = ("_last_quad_slots", "_last_quad_report",
                 "_last_quad_unwritable", "_last_quad_notes",
                 "_last_quad_splits", "_last_quad_want")
        saved = {n: getattr(bm, n, None) for n in names}
        self.addCleanup(lambda: [setattr(bm, n, v)
                                 for n, v in saved.items()])
        bm._last_quad_slots = [(0, 111), (1, 222), (2, 333), (3, 444)]
        # 111 被应用最小尺寸顶大（偏差 50px）；333 一步都没动（写入无效）
        bm._last_quad_report = [(111, (0, 0, 100, 100), (0, 0, 150, 130))]
        bm._last_quad_unwritable = [333]
        bm._last_quad_notes = []
        bm._last_quad_splits = (0.5, 0.5)

        self.app._snap_four_quad()      # 以前在这里 AttributeError

        status = self.app.status_var.get()
        self.assertIn("写入无效", status)
        self.assertIn("被应用最小尺寸钳制", status)
        self.assertIn("111", status)

    def test_command_poll_survives_bad_command(self):
        """单条命令处理抛异常，不能带走整条轮询链。"""
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("坏命令")

        self.app.refresh_monitors = boom
        cmd_channel.take = lambda: "refresh"
        self.app._start_command_poll()          # 以前异常直接冒出去
        self.assertEqual(calls["n"], 1)

        self.app.refresh_monitors = lambda: None
        self.app._start_command_poll()          # 链还活着：继续轮询
        self.assertEqual(calls["n"], 1)

    # ---------- 3. 布局还原 ----------

    def test_apply_layout_with_int_hwnds(self):
        """整数句柄的布局：开着的按精确/应用名还原，关上的不炸。"""
        layout = {"windows": [
            {"hwnd": 111, "owner": "App A", "name": "文档 — 主窗口",
             "x": 0, "y": 0, "w": 500, "h": 400},
            {"hwnd": 333, "owner": "App B", "name": "便签",
             "x": 500, "y": 0, "w": 500, "h": 400},
        ]}
        ui.layouts.load_layout = lambda name: layout
        ui.b.list_target_windows = lambda: [
            {"hwnd": 333, "owner": "App B", "name": "便签",
             "x": 0, "y": 0, "w": 100, "h": 100},
        ]
        moved = []
        ui.b.set_window_rect = (
            lambda h, x, y, w, hh, activate=True: moved.append((h, x, y, w, hh)))
        self.app._layout_monitor_changed = lambda name: False
        self.app._layout_uuid_matched = lambda name: (False, None, None)
        ui.settings.load = lambda: {}
        ui.settings.save = lambda s: None

        self.app._apply_layout_by_name("测试布局", silent=True)

        # 以前整数句柄在标题匹配处 AttributeError，整个还原死掉
        self.assertIn((333, 500, 0, 500, 400), moved)
        self.assertIn("成功", self.app.status_var.get())


    # ---------- 状态栏全文悬停可见 ----------

    def _find_status_label(self):
        stack = [self.app.root]
        while stack:
            w = stack.pop()
            try:
                if w.cget("textvariable") == str(self.app.status_var):
                    return w
            except Exception:  # noqa: BLE001  不是 Label 的控件会炸 cget
                pass
            stack.extend(w.winfo_children())
        return None

    def test_status_tooltip_shows_full_text(self):
        """被裁掉的状态栏全文：悬停后气泡里必须看得到。"""
        lbl = self._find_status_label()
        self.assertIsNotNone(lbl)
        full = ("已 2×2 四宫格排列 ｜ 写入无效：Windows 11 更新 ｜ "
                "被应用最小尺寸钳制：Notepad差50px")
        self.app.status_var.set(full)
        lbl.event_generate("<Enter>")
        self.root.update()
        self.root.after(700, self.root.quit)
        self.root.mainloop()
        self.root.update()
        texts = [c.cget("text") for t in lbl.winfo_children()
                 for c in t.winfo_children()]
        self.assertTrue(any(full in t for t in texts),
                        "悬停气泡里没有状态全文：%r" % (texts,))

    def test_status_tooltip_silent_when_empty(self):
        """没有状态时不该弹出空气泡。"""
        lbl = self._find_status_label()
        self.app.status_var.set("")
        lbl.event_generate("<Enter>")
        self.root.update()
        self.root.after(700, self.root.quit)
        self.root.mainloop()
        self.root.update()
        self.assertEqual(lbl.winfo_children(), [])


    # ---------- 启动后窗口下拉必须有值 ----------

    def test_refresh_monitors_populates_window_dropdowns(self):
        """刷新显示器的路径必须同时填充窗口下拉。

        历史故障（真机复现）：_refresh_targets 只在点「刷新列表」按钮或从
        托盘还原（show）时才跑，启动流程不调它——新启动后「目标窗口」和
        四宫格/并排那几个下拉全是空的，用户打开下拉只看到一个空列表，
        没法固定目标窗口，所有操作退回「自动」模式。
        """
        ui.b.list_target_windows = lambda: [
            {"hwnd": 4242, "owner": "App A", "name": "文档",
             "x": 0, "y": 0, "w": 800, "h": 600,
             "label": "文档  (800×600)"},
        ]
        self.app.refresh_monitors()
        vals = list(self.app.target_cb["values"])
        self.assertEqual(vals[0], ui.AUTO_TARGET)
        self.assertIn("文档  (800×600)", vals)
        for cb in (self.app.quad_tl_cb, self.app.sbs_left_cb):
            self.assertTrue(list(cb["values"]),
                            "四宫格/并排下拉启动后是空的")


if __name__ == "__main__":
    unittest.main()
