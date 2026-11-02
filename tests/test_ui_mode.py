#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 / 高级模式：卡片显隐、标签隐藏、持久化都必须真的发生。

极简模式防的是「界面太多、点不到想用的」：进阶卡必须整卡消失（不是禁用），
只剩窗口工具等日常卡片；高级模式必须完整还原所有卡与三个标签。
切换结果写进 settings.json，下次启动保持用户的选择。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    import ui  # noqa: E402
    import settings  # noqa: E402
except Exception:  # noqa: BLE001
    tk = None
    ui = None

if tk is None:
    raise unittest.SkipTest("tkinter 不可用，跳过界面单测")


def _cards(frame):
    out = {}
    for w in frame.winfo_children():
        if w.winfo_class() == "TLabelframe":
            out[str(w.cget("text"))] = w
    return out


class TestUiMode(unittest.TestCase):
    """注意：本类必须看到真实默认（极简），故不设 MMM_ADVANCED_UI。"""

    def setUp(self):
        os.environ.pop("MMM_ADVANCED_UI", None)
        self._set_file = os.path.join(settings.data_dir(), "settings.json")
        try:
            with open(self._set_file, "rb") as f:
                self._had = f.read()
        except OSError:
            self._had = None
        settings._cache["stat"] = None
        settings._cache["data"] = None
        s = settings.load()
        s.pop("advanced_mode", None)
        settings.save(s)
        settings._cache["stat"] = None
        settings._cache["data"] = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ui.App(self.root)

    def tearDown(self):
        self.root.destroy()
        if self._had is not None:
            with open(self._set_file, "wb") as f:
                f.write(self._had)
        else:
            try:
                os.remove(self._set_file)
            except OSError:
                pass
        settings._cache["stat"] = None
        settings._cache["data"] = None

    def test_simple_mode_hides_advanced_cards(self):
        """极简（默认）：进阶卡整卡消失，常用卡保留。"""
        common = _cards(self.app._tab_common)
        config = _cards(self.app._tab_config)
        st = _cards(self.app._tab_settings)
        for t in ("多屏任务栏", "拖拽吸附预览"):
            self.assertEqual(common[t].winfo_manager(), "", f"极简下「{t}」应隐藏")
        self.assertNotEqual(common["窗口工具"].winfo_manager(), "", "窗口工具必须可见")
        self.assertEqual(config["情景模式（按显示器组合自动套用）"].winfo_manager(), "")
        self.assertNotEqual(config["壁纸轮换（幻灯片）"].winfo_manager(), "", "轮换是日常功能")
        self.assertEqual(st["防休眠"].winfo_manager(), "")
        self.assertNotEqual(st["配置备份"].winfo_manager(), "", "备份是一次性但必达的功能")
        # 只剩常用标签；当前页停在常用
        self.assertEqual(self.app._tab_btns["config"][0].winfo_manager(), "")
        self.assertEqual(self.app._tab_btns["settings"][0].winfo_manager(), "")
        self.assertEqual(self.app._tab_active, "common")

    def test_advanced_mode_restores_everything(self):
        """勾上高级：所有卡与标签恢复，选择持久化。"""
        self.app.advanced_mode.set(True)
        self.app._apply_ui_mode()
        common = _cards(self.app._tab_common)
        config = _cards(self.app._tab_config)
        st = _cards(self.app._tab_settings)
        self.assertNotEqual(common["多屏任务栏"].winfo_manager(), "")
        self.assertNotEqual(config["触发器（窗口移动自动触发动作）"].winfo_manager(), "")
        self.assertNotEqual(st["显示器热插拔"].winfo_manager(), "")
        self.assertNotEqual(self.app._tab_btns["config"][0].winfo_manager(), "")
        self.assertEqual(settings.load().get("advanced_mode"), True)

    def test_switch_back_persists_false(self):
        """高级切回极简：卡再次隐藏，settings 写回 False。"""
        self.app.advanced_mode.set(True)
        self.app._apply_ui_mode()
        self.app.advanced_mode.set(False)
        self.app._apply_ui_mode()
        common = _cards(self.app._tab_common)
        self.assertEqual(common["多屏任务栏"].winfo_manager(), "")
        self.assertEqual(settings.load().get("advanced_mode"), False)


if __name__ == "__main__":
    unittest.main()
