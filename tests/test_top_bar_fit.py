#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""顶栏让位：状态栏再长也不能把右侧控件挤没。

历史故障（真机复现）
--------------------
顶栏是「刷新显示器 | 状态栏 | michaelqiu 出品」一行。状态栏的文案长度随
事件变：四宫格排完的明细能到 170 多字（实测请求 1950px）。

Tk 的 packer 在容器装不下时**不映射后 pack 的控件**，而且一声不吭——于是
「michaelqiu 出品」整个消失，界面上像凭空少了一块。同一个病根在这个仓库
反复出现：四宫格第四个下拉、跨屏铺满的屏勾选框，都是这么不见的。

修法
----
1. 状态栏加写死的宽度上限（width=90 字符）。超出的正文不显示，但悬停
   0.5s 有全文气泡，信息不丢。
2. 窗口变窄时按「其余控件的自然宽度合计」反推状态栏上限，让状态栏先
   瘦身——它是顶栏里唯一有弹性的东西（纯文字）。右侧控件永远拿满自然尺寸。
3. 窗口宽度掉到下限以下时拉回来：横向没有滚动条可救，用户没法自救。

这些断言全部针对**真实控件几何**，不 mock：packer 的行为只能真跑出来。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_harness as harness  # noqa: E402 - 抑制构造期真机热键/轮询

try:
    import tkinter as tk
    import ui  # noqa: E402
except Exception:  # noqa: BLE001  无 tkinter / 无显示环境：整类跳过
    tk = None
    ui = None

if tk is None:
    raise unittest.SkipTest("tkinter 不可用，跳过界面单测")


# 四宫格排完的明细：实测最长 179 字，请求宽度 1950px
LONG_STATUS = (
    "四宫格完成：4 个窗口 → 2×2 · 左上 ChatGPT、右上 #2 UU远程、"
    "左下 Notepad、右下 Code ｜ 这块屏最多排 3 个；某窗口被系统判给"
    "别的屏 ｜ 写入无效（不开放自动化/被系统钉住）：CrossPaste、"
    "UU远程 ｜ 被应用最小尺寸钳制：ChatGPT差50px、Code差20px、"
    "Notepad差8px、Edge差12px")


class TestTopBarFit(unittest.TestCase):
    """顶栏在窄窗口与超长文案下的表现。"""

    def setUp(self):
        # 刻意**不** withdraw：窗口未映射时 packer 不受宽度约束，所有控件
        # 都「放得下」，映射与越界判定全部失真——那这条测试就白写了。
        # （同 tests/test_snap_rows_mapped.py 的做法）
        self.root = tk.Tk()
        self.app = harness.build_app(ui, self.root)
        self.addCleanup(lambda: harness.close_app(self.root, self.app))

    # ---------- 小工具 ----------

    def _resize(self, width):
        """把窗口摆到指定宽度并让它完成一次几何计算。

        挪到 +6000+6000 是为了不占用用户屏幕；构造期定时器已被 harness
        冻结。
        """
        self.root.geometry("%dx680+6000+6000" % width)
        self._pump(width)

    def _pump(self, want_w=None):
        """让窗口完成一次几何计算与 packer 映射。

        Windows 的窗口管理器是异步的：geometry() 之后要等 WM 回一个
        Configure 事件，update_idletasks() 不等它。注意此时 Tk 自己的
        winfo_width() 已经是目标值（它只是记下了请求），但子控件还停在旧
        布局上（实测画布宽度卡在 1px）。macOS 上几何是同步生效的，
        update_idletasks() 就够了。

        已映射 root 的 update() 在 macOS Tk 上会持续处理 Expose/Configure
        事件、永不返回（同 tests/test_snap_rows_mapped.py 的告警），所以
        只在 Windows 这一侧补。
        """
        self.root.update_idletasks()
        if sys.platform == "win32" or not self.app.content.winfo_ismapped():
            self.root.update()
            self.root.update_idletasks()

    def _brand_label(self):
        """顶栏右侧的「michaelqiu 出品」。"""
        for k in self.app._top_bar.winfo_children():
            try:
                if str(k.cget("text")) == "michaelqiu 出品":
                    return k
            except Exception:  # noqa: BLE001  不是 Label 的控件会炸 cget
                pass
        return None

    # ---------- 断言 ----------

    def test_long_status_never_eats_the_brand_label(self):
        """超长状态文案：右侧署名必须还在、且是自然宽度。"""
        self._resize(1000)
        brand = self._brand_label()
        self.assertIsNotNone(brand, "顶栏里找不到「michaelqiu 出品」")
        natural = brand.winfo_reqwidth()
        self.app.status_var.set(LONG_STATUS)
        self._pump()
        self.assertTrue(brand.winfo_ismapped(),
                        "状态栏一长，「michaelqiu 出品」就被 packer 整个吞了")
        self.assertEqual(brand.winfo_width(), natural,
                         "「michaelqiu 出品」被压缩了（%d < %d）"
                         % (brand.winfo_width(), natural))

    def test_status_label_gives_way_when_window_narrows(self):
        """窗口变窄：先缩状态栏，右侧控件保持自然宽度。"""
        self._resize(1000)
        brand = self._brand_label()
        natural = brand.winfo_reqwidth()
        wide = int(self.app._status_lbl.cget("width"))
        self.app.status_var.set(LONG_STATUS)
        self._pump()
        self._resize(700)
        self._pump()
        self.assertLess(int(self.app._status_lbl.cget("width")), wide,
                        "窗口变窄后状态栏没有让位")
        self.assertTrue(brand.winfo_ismapped(),
                        "窗口变窄后右侧署名被 packer 吞了")
        self.assertEqual(brand.winfo_width(), natural,
                         "窗口变窄后右侧署名被压缩了")

    def test_status_tooltip_carries_the_full_text(self):
        """限宽裁掉的正文，悬停气泡里必须看得到。"""
        self._resize(1000)
        lbl = self.app._status_lbl
        full = ("已 2×2 四宫格排列 ｜ 写入无效：Windows 11 更新 ｜ "
                "被应用最小尺寸钳制：Notepad差50px")
        self.app.status_var.set(full)
        lbl.event_generate("<Enter>")
        self.root.update_idletasks()
        self.root.after(700, self.root.quit)
        self.root.mainloop()
        self.root.update_idletasks()
        texts = [c.cget("text") for t in lbl.winfo_children()
                 for c in t.winfo_children()]
        self.assertTrue(any(full in t for t in texts),
                        "悬停气泡里没有状态全文：%r" % (texts,))

    def test_window_width_clamped_back_to_minimum(self):
        """宽度掉到下限以下要被拉回来：贴边分屏/跨 DPI 拖动都绕得过 minsize。"""
        self._resize(1000)
        self.assertGreaterEqual(
            self.root.winfo_width(), ui.App._MIN_WIN_W,
            "窗口被压到 %dpx 宽，低于下限 %dpx 却没被拉回来"
            % (self.root.winfo_width(), ui.App._MIN_WIN_W))


if __name__ == "__main__":
    unittest.main()
