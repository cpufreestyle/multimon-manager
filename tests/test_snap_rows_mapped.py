#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口工具的按钮在**最小窗口宽度**下必须全部映射。

Tk 的 packer 在容器装不下时不映射后面的子控件——按钮还在对象树里、
command 也绑着，但用户看不到也点不到，表象就是「按钮点了没反应」。
本项目已因此踩过两次（四宫格容量文案挤掉「自动四窗」、跨屏铺满整行
被删）。

吸附按钮第二行一度堆了 13 个按钮（8 个分区 + 撤销移动 + 回摆放位 +
释放窗口 + 暂存 + 取回），自然宽 1005px，而最小尺寸下窗口工具只有
约 775px 可视宽：末尾的「释放窗口 / 暂存 / 取回」直接掉映射。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ui_harness as harness  # noqa: E402 - 抑制构造期真机热键/轮询

os.environ.setdefault("MMM_ADVANCED_UI", "1")

try:
    import tkinter as tk
    import ui
except Exception:  # noqa: BLE001
    tk = None
    ui = None

# 第二行末尾这几个是最容易被挤掉的，逐个点名，避免「只查前几个」漏掉
SNAP_BUTTONS = (
    "移到上一屏", "移到下一屏", "拉到本屏", "左半", "右半", "上半", "下半",
    "最大化", "居中", "左1/3", "中1/3", "右1/3",
    "左上", "右上", "左下", "右下",
    "撤销移动", "回摆放位", "释放窗口", "暂存", "取回", "测最小尺寸",
)


class _Mon:
    def __init__(self, left, top, w, h, name, primary=False):
        self.left, self.top, self.width, self.height = left, top, w, h
        self.is_primary = primary
        self.device_name = self.device_path = name
        self.work_rect = (left, top, w, h)
        self.work_left, self.work_top = left, top
        self.work_width, self.work_height = w, h


@unittest.skipIf(tk is None or ui is None, "无 tkinter，跳过界面层测试")
class SnapRowsMappedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 整个类只建一个 Tk root：macOS 的 Tk 在同一进程里反复建/销毁
        # 真实（非 withdraw）窗口会 SIGSEGV。
        # 也不能 withdraw()：窗口未映射时 packer 不受窗口宽度约束，
        # 所有按钮都「放得下」，映射与越界判定全部失真。
        cls.root = tk.Tk()
        cls.app = harness.build_app(ui, cls.root)
        cls.app.monitors = [_Mon(0, 33, 1512, 891, "Built-in", True),
                            _Mon(1512, -1066, 1152, 2048, "Mi Monitor"),
                            _Mon(-1920, 0, 1920, 1080, "DELL U2414H")]
        cls.app._last_monitors = list(cls.app.monitors)
        cls.app._rebuild_spread_checks()
        cls.app._rebuild_grid_checks()
        w, h = cls.root.minsize()
        cls.root.geometry("%dx%d" % (w, h))
        cls.root.update_idletasks()
        # 这里绝不能调 cls.root.update()：本类刻意不 withdraw（窗口未映射时
        # packer 不受宽度约束，映射/越界判定会失真），而 macOS Tk 对已映射
        # root 的 update() 会持续处理 Expose/Configure 事件、永不返回。
        # 2026-11-13 faulthandler 实测栈停在 tkinter/__init__.py:1314。
        # update_idletasks() 足以完成几何与 packer 映射，实测 0.1s 返回。

    @classmethod
    def tearDownClass(cls):
        harness.close_app(cls.root, cls.app)

    def _buttons(self):
        hits = {}

        def walk(w):
            for c in w.winfo_children():
                try:
                    t = c.cget("text")
                except Exception:  # noqa: BLE001
                    t = None
                if t in SNAP_BUTTONS:
                    hits[t] = c
                walk(c)

        walk(self.app.body)
        return hits

    def test_all_snap_buttons_exist(self):
        """先确认这些按钮真的建出来了，否则映射断言会因为找不着而空过。"""
        found = self._buttons()
        missing = [t for t in SNAP_BUTTONS if t not in found]
        self.assertEqual(missing, [], "这些按钮没建出来：%s" % missing)

    def test_all_snap_buttons_mapped_at_min_width(self):
        """最小窗口尺寸下每个按钮都要映射，否则用户点不到。"""
        found = self._buttons()
        unmapped = [t for t in SNAP_BUTTONS
                    if t in found and not found[t].winfo_ismapped()]
        self.assertEqual(
            unmapped, [],
            "最小尺寸下这些按钮没映射（被 packer 挤出可视区）：%s" % unmapped)

    def test_no_snap_button_overflows_canvas(self):
        """映射了也不能超出画布右边缘，否则按钮只露一半。"""
        cv = self.app._canvas
        limit = cv.winfo_rootx() + cv.winfo_width()
        over = []
        for t, c in self._buttons().items():
            if not c.winfo_ismapped():
                continue
            right = c.winfo_rootx() + c.winfo_width()
            if right > limit + 1:
                over.append("%s(+%dpx)" % (t, right - limit))
        self.assertEqual(over, [], "这些按钮超出可视右边缘：%s" % over)


if __name__ == "__main__":
    unittest.main()
