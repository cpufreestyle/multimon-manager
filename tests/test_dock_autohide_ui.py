#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置页「自动隐藏 Dock 栏」接线：可见、读写系统状态、成功刷新、失败回滚。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    import ui  # noqa: E402
    import backend as b  # noqa: E402
except Exception:  # noqa: BLE001
    tk = None
    ui = None
    b = None

if tk is None:
    raise unittest.SkipTest("tkinter 不可用，跳过界面单测")
# 极简模式会藏起设置页的进阶卡；UI 接线单测必须看到完整界面
os.environ.setdefault("MMM_ADVANCED_UI", "1")
if not (b and b.IS_MAC):
    raise unittest.SkipTest("Dock 自动隐藏只适用于 macOS")


class _FakeBox:
    def __init__(self, sink):
        self._sink = sink

    def showinfo(self, title, msg=None, **k):
        self._sink.append(("info", msg or title))

    def showwarning(self, title, msg=None, **k):
        self._sink.append(("warn", msg or title))

    def showerror(self, title, msg=None, **k):
        self._sink.append(("error", msg or title))

    def askyesno(self, title, msg=None, **k):
        self._sink.append(("ask", msg or title))
        return True


class TestDockAutohideUi(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.get_patch = mock.patch.object(b, "dock_autohide_get", return_value=False)
        self.get_patch.start()
        self.app = ui.App(self.root)
        self.app._show_tab("settings")
        self.root.update()
        self.msg = []
        self._orig_mb = ui.messagebox
        ui.messagebox = _FakeBox(self.msg)

    def tearDown(self):
        ui.messagebox = self._orig_mb
        self.get_patch.stop()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def test_control_visible_and_initial_value_from_system(self):
        self.assertTrue(self.app.dock_autohide_cb.winfo_ismapped(),
                        "「自动隐藏 Dock 栏」没有被界面映射")
        self.assertFalse(self.app.dock_autohide_var.get())

    def test_enable_calls_backend_and_refreshes_monitors(self):
        calls = []
        with mock.patch.object(
                b, "dock_autohide_set",
                side_effect=lambda wanted: calls.append(wanted) or (True, "Dock 栏已自动隐藏")):
            with mock.patch.object(self.app, "refresh_monitors") as refresh:
                self.app.dock_autohide_var.set(True)
                self.app._on_dock_autohide_change()
        self.assertEqual(calls, [True])
        refresh.assert_called_once_with()
        self.assertTrue(self.app.dock_autohide_var.get())
        self.assertIn("已自动隐藏", self.app._dock_autohide_status.get())
        self.assertIn("已自动隐藏", self.app.status_var.get())

    def test_failure_rolls_back_to_real_state(self):
        with mock.patch.object(
                b, "dock_autohide_set",
                return_value=(False, "Dock 设置未生效（目标是自动隐藏，系统读回为始终显示）")):
            self.app.dock_autohide_var.set(True)
            self.app._on_dock_autohide_change()
        self.assertFalse(self.app.dock_autohide_var.get())
        self.assertIn("未生效", self.app._dock_autohide_status.get())
        self.assertTrue(any(kind == "error" for kind, _msg in self.msg),
                        "失败时没有给出可见错误提示")

    def test_exception_rolls_back(self):
        with mock.patch.object(b, "dock_autohide_set", side_effect=RuntimeError("boom")):
            self.app.dock_autohide_var.set(True)
            self.app._on_dock_autohide_change()
        self.assertFalse(self.app.dock_autohide_var.get())
        self.assertIn("boom", self.app._dock_autohide_status.get())


if __name__ == "__main__":
    unittest.main()
