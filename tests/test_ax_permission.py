#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""辅助功能（Accessibility）授权丢失的检测与文案回归。

macOS 的 TCC 授权可撤销：用户在系统设置里关掉开关、或 App 签名变化后，
osascript 会突然全部返回 "not allowed assistive access (-1719/-25211)"。
此时所有几何写入静默失败，界面若只说「需要至少 N 个可操作窗口」会把用户
引向错误方向（去找窗口，而真正要修的是系统设置里的授权）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import windows_mac as wm  # noqa: E402


class TestAxPermissionDetection(unittest.TestCase):
    def setUp(self):
        self._prev = wm._ax_permission_lost

    def tearDown(self):
        wm._ax_permission_lost = self._prev

    def test_not_lost_by_default(self):
        wm._ax_permission_lost = False
        self.assertFalse(wm.ax_permission_lost())

    def test_detect_assistive_access_denied(self):
        """stderr 含 assistive access 时置位。"""
        wm._ax_permission_lost = False
        # 直接调 _osa 会真跑 osascript；这里只验证判定逻辑本身：
        # 通过把 subprocess.run 换成假实现来喂错误输出。
        import subprocess

        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = ("79:313: execution error: System Events got an error: "
                      "osascript is not allowed assistive access. (-25211)\n")

        orig = subprocess.run
        subprocess.run = lambda *a, **k: FakeResult()
        try:
            wm._osa("tell application \"System Events\" to count windows")
        finally:
            subprocess.run = orig
        self.assertTrue(wm.ax_permission_lost())

    def test_other_errors_do_not_flag_permission(self):
        """普通 AppleScript 语法错误不应误判成授权丢失。"""
        import subprocess

        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "0:1: syntax error\n"

        orig = subprocess.run
        subprocess.run = lambda *a, **k: FakeResult()
        try:
            wm._osa("bogus script")
        finally:
            subprocess.run = orig
        self.assertFalse(wm.ax_permission_lost())

    def test_success_does_not_flag(self):
        import subprocess

        class FakeResult:
            returncode = 0
            stdout = "3"
            stderr = ""

        orig = subprocess.run
        subprocess.run = lambda *a, **k: FakeResult()
        try:
            wm._osa("tell application \"System Events\" to count windows")
        finally:
            subprocess.run = orig
        self.assertFalse(wm.ax_permission_lost())

    def test_reset_clears_flag(self):
        wm._ax_permission_lost = True
        wm.reset_ax_permission_lost()
        self.assertFalse(wm.ax_permission_lost())


if __name__ == "__main__":
    unittest.main()
