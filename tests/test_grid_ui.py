#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""虚拟网格界面入口单测：控件必须真的映射出来、点按钮不得静默失败。

这个类要防的是历史上反复出现的两类界面故障，都不是算法错：

1. **packer 不映射**。Tk 的 packer 在容器装不下时不映射后面的子控件——
   真机实测四宫格只有前三个下拉可见可点，第四个 x=0 w=1 根本没映射，
   用户永远选不到第四个窗口（「4 格模式只能选三个窗口」的真因）。
2. **rebuild 删掉整行**。_rebuild_* 若 destroy 父 Frame 的全部子控件，
   同行里的标签、下拉、按钮会被一起删掉（「跨屏铺满的选项没有出现」的真因）。

所以这里建一次真界面，逐个控件查 winfo_ismapped，并按按钮看状态栏是否
给出可读反馈（而不是抛异常或什么都不说）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    import ui  # noqa: E402
except Exception:  # noqa: BLE001  无 tkinter / 无显示环境：整类跳过
    tk = None
    ui = None

if tk is None:
    raise unittest.SkipTest("tkinter 不可用，跳过界面单测")


class _Mons:
    def __init__(self, ms):
        self.ms = ms

    def enum_monitors(self, force=False):
        return self.ms


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


class TestGridUi(unittest.TestCase):
    """虚拟网格那一行：控件映射 + 按钮有反馈 + 勾选框与跨屏铺满同步。"""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()      # 不抢焦点
        self.app = ui.App(self.root)
        # 三块屏：横、横、竖（竖屏默认不勾）
        self.app.monitors = [_Mon(0, 33, 1512, 891, "Built-in", True),
                             _Mon(0, -900, 1600, 900, "RV200"),
                             _Mon(-1080, -1080, 1080, 1920, "DELL")]
        self.app._last_monitors = list(self.app.monitors)
        self.app._rebuild_spread_checks()
        self.app._rebuild_grid_checks()
        self.root.update()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _mapped(self, widget):
        return bool(widget and widget.winfo_ismapped())

    def test_grid_controls_mapped(self):
        """布局下拉、两个 Spinbox、智能勾选、按钮都必须被 packer 映射。"""
        self.assertTrue(self._mapped(self.app.grid_layout_cb),
                        "布局下拉没映射：用户选不了网格形状")
        self.assertTrue(self.app.grid_smart_var is not None)
        # Spinbox 没存属性，按类名找（界面里只有这两个）
        spins = []
        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class() == "Spinbox":
                    spins.append(c)
                walk(c)
        walk(self.root)
        self.assertEqual(len(spins), 2,
                         "应有列/行两个 Spinbox，实际 %d 个" % len(spins))
        for s in spins:
            self.assertTrue(self._mapped(s),
                            "Spinbox %s 没映射：自定义行列改不了" % s)
        # 按钮：按文本找
        btns = []
        def walk2(w):
            for c in w.winfo_children():
                try:
                    if c.cget("text") == "网格批排":
                        btns.append(c)
                except Exception:  # noqa: BLE001
                    pass
                walk2(c)
        walk2(self.root)
        self.assertEqual(len(btns), 1)
        self.assertTrue(self._mapped(btns[0]),
                        "「网格批排」按钮没映射：点了没反应")

    def test_monitor_checks_mapped_and_portrait_unchecked(self):
        """三块屏三个勾选框，全部映射；竖屏默认不勾。"""
        self.assertEqual(len(self.app._grid_checks), 3)
        widgets = getattr(self.app, "_grid_check_widgets", [])
        # 第一个是「同铺满:」标签，后面三个是勾选框
        # ttk 控件的 winfo_class() 带 T 前缀（TCheckbutton）
        checks = [w for w in widgets if w.winfo_class() == "TCheckbutton"]
        self.assertEqual(len(checks), 3)
        for c in checks:
            self.assertTrue(self._mapped(c), "屏勾选框没映射")
        by_idx = dict(self.app._grid_checks)
        self.assertTrue(by_idx[0].get(), "横屏应默认勾选")
        self.assertTrue(by_idx[1].get(), "横屏应默认勾选")
        self.assertFalse(by_idx[2].get(), "竖屏默认不该勾（横屏应用会被压扁）")

    def test_rebuild_spread_keeps_portrait_default_when_screens_swapped(self):
        """屏的枚举顺序变了，勾选默认值必须按**新的**横竖重新判，不能串味。

        真实场景：App 启动时用真机屏建过勾选框（本机是 横/竖/横），随后
        enum_monitors 的顺序变了（或测试换成另一批替身屏）。旧实现拿
        _last_monitors[i] 与 _spread_checks[i] 按下标配对来迁移旧值，两个
        列表分属两处维护，顺序一变就把「竖屏不勾」安到横屏头上、把「横屏勾」
        安到竖屏头上——横屏应用于是被发到竖屏上压成一条。
        """
        # 同一批屏（device_path 不变），只把顺序调成 横/横/竖：第 2 块现在是
        # 横屏，第 3 块是竖屏。rebuild 后第 2 块该按横屏默认勾、第 3 块按竖屏
        # 默认不勾。
        swapped = [_Mon(0, 33, 1512, 891, "Built-in", True),
                   _Mon(0, -900, 1600, 900, "RV200"),
                   _Mon(-1080, -1080, 1080, 1920, "DELL")]
        self.app.monitors = swapped
        self.app._last_monitors = list(swapped)
        self.app._rebuild_spread_checks()
        by_idx = dict(self.app._spread_checks)
        self.assertTrue(by_idx[0].get(), "横屏应默认勾选")
        self.assertTrue(by_idx[1].get(), "横屏应默认勾选")
        self.assertFalse(by_idx[2].get(),
                         "竖屏默认不该勾：顺序变了也不能把横屏的勾选继承过来")

        # 再换成全新 device_path（真机上就是拔掉旧屏、插上另一台）：
        # 没有可继承的状态，必须全按横竖重新判，而不是继承旧下标的值
        fresh = [_Mon(0, 33, 1512, 891, "Built-in", True),
                 _Mon(0, -900, 1600, 900, "RV200"),
                 _Mon(-1080, -1080, 1080, 1920, "OTHER")]
        self.app.monitors = fresh
        self.app._last_monitors = list(fresh)
        self.app._rebuild_spread_checks()
        by_idx = dict(self.app._spread_checks)
        self.assertTrue(by_idx[1].get(),
                        "新屏是横屏：没有旧状态可继承，应按横屏默认勾")
        self.assertFalse(by_idx[2].get(),
                         "新屏是竖屏：没有旧状态可继承，应按竖屏默认不勾")

    def test_rebuild_spread_keeps_own_choice_for_known_screen(self):
        """同一块屏（device_path 不变）的手动选择要留住，不因重建被默认值覆盖。"""
        by_idx = dict(self.app._spread_checks)
        by_idx[2].set(True)          # 竖屏 DELL：用户自己想用
        self.app._rebuild_spread_checks()
        again = dict(self.app._spread_checks)
        self.assertTrue(again[2].get(),
                        "同一块屏的手动勾选必须留住（按 device_path 迁移）")

    def test_grid_checks_mirror_spread(self):
        """虚拟网格与跨屏铺满选同一批屏：改一边另一边要跟着变。"""
        by_idx = dict(self.app._grid_checks)
        by_idx[1].set(False)
        self.app._on_grid_check()
        spread = dict(self.app._spread_checks)
        self.assertFalse(spread[1].get(),
                         "网格里取消第 2 块屏，跨屏铺满也该取消")

    def test_custom_dims_switch_layout(self):
        """改 Spinbox 应把布局切到「自定义」：否则预设会把这几个数盖掉。"""
        self.app.grid_cols_var.set(3)
        self.app._on_grid_dims_change()
        self.assertEqual(self.app.grid_layout_var.get(), ui.GRID_LAYOUT_CUSTOM)

    def test_layout_args(self):
        """界面选择 → 后端参数：预设走 layout，自定义走 cols/rows。"""
        self.app.grid_layout_var.set("3×2 六等分")
        self.assertEqual(self.app._grid_layout_args(), (None, None, "3×2 六等分"))
        self.app.grid_layout_var.set(ui.GRID_LAYOUT_CUSTOM)
        self.app.grid_cols_var.set(2)
        self.app.grid_rows_var.set(3)
        self.assertEqual(self.app._grid_layout_args(), (2, 3, None))

    def test_fill_grid_without_screens_says_so(self):
        """一块屏都没勾：按钮要说清原因，不是静默什么都不做。"""
        for _i, var in self.app._grid_checks:
            var.set(False)
        self.app._fill_grid()
        self.assertIn("请先勾选", self.app.status_var.get())

    def test_fill_grid_reports_result(self):
        """有勾选时按钮必须给出结果反馈（成功或失败都要有话说）。"""
        self.app._fill_grid()
        msg = self.app.status_var.get()
        self.assertTrue(msg, "点了「网格批排」状态栏居然没反馈")
        self.assertNotIn("Traceback", msg)


if __name__ == "__main__":
    unittest.main()
