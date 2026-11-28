#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关窗缩到托盘时不能用模态对话框。

回归背景：on_close 原来在首次关闭时弹 messagebox.showinfo。那是**模态**
对话框，会阻塞整个 Tk 事件循环——after 定时器停摆，托盘命令轮询收不到，
窗口再也唤不醒。用户看到的是「程序无法打开」，而进程还好端端活着并
持着单例锁。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    import ui
except Exception:  # noqa: BLE001
    tk = None
    ui = None


@unittest.skipIf(tk is None or ui is None, "无 tkinter")
class CloseDoesNotBlockTest(unittest.TestCase):
    def test_on_close_never_uses_modal_messagebox(self):
        root = mock.Mock()
        app = ui.App.__new__(ui.App)
        app.root = root
        app.status_var = mock.Mock()
        with mock.patch.object(ui, "messagebox") as box:
            app.on_close()
            app.on_close()
        # 关键断言：一次都不能弹模态框（showinfo/showerror/warning 都算）
        self.assertEqual(
            box.mock_calls, [],
            "on_close 弹了模态对话框，会阻塞事件循环：%s" % box.mock_calls)
        self.assertEqual(root.withdraw.call_count, 2)


if __name__ == "__main__":
    unittest.main()
