#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dock 自动隐藏模块：读系统状态、写入、重启 Dock 与复验。

这个功能会改用户的 macOS 全局偏好，测试绝不能真的执行
`killall Dock`。所有子进程调用都换成替身，只验证命令、结果判断和
「写后读回不一致必须报失败」这条关键契约。
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dock_autohide_mac as da  # noqa: E402


def _result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestGetAutohide(unittest.TestCase):
    def test_reads_true(self):
        with mock.patch.object(da.subprocess, "run", return_value=_result(stdout="1\n")) as run:
            self.assertIs(da.get_autohide(), True)
        self.assertEqual(
            run.call_args.args[0],
            ["defaults", "read", "com.apple.dock", "autohide"])
        self.assertEqual(run.call_args.kwargs["timeout"], da.TIMEOUT)

    def test_reads_false(self):
        with mock.patch.object(da.subprocess, "run", return_value=_result(stdout="0\n")):
            self.assertIs(da.get_autohide(), False)

    def test_missing_key_is_unknown(self):
        """系统里没有这个键时不能把「读不到」误报成关闭。"""
        with mock.patch.object(
                da.subprocess, "run",
                return_value=_result(returncode=1, stderr="The domain/default pair does not exist")):
            self.assertIsNone(da.get_autohide())

    def test_timeout_is_unknown(self):
        with mock.patch.object(
                da.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(["defaults"], da.TIMEOUT)):
            self.assertIsNone(da.get_autohide())


class TestGetPanelRect(unittest.TestCase):
    def setUp(self):
        da.invalidate_panel_cache()

    def tearDown(self):
        da.invalidate_panel_cache()

    def test_reads_real_dock_panel_shape(self):
        """真机 Dock 实测 `25|926|1462|46`：必须解析成左上原点矩形。"""
        with mock.patch.object(
                da.subprocess, "run",
                return_value=_result(stdout="25|926|1462|46\n")) as run:
            self.assertEqual(da.get_panel_rect(), (25, 926, 1462, 46))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:2], ["osascript", "-e"])
        self.assertIn('process "Dock"', cmd[2])
        self.assertEqual(run.call_args.kwargs["timeout"], da.PANEL_TIMEOUT)

    def test_accepts_locale_noise_but_needs_four_numbers(self):
        self.assertEqual(da._parse_panel_rect("25, ,, 926, ,, 1462, ,, 46"),
                         (25, 926, 1462, 46))
        self.assertIsNone(da._parse_panel_rect(""))
        self.assertIsNone(da._parse_panel_rect("25|926|1462"))
        self.assertIsNone(da._parse_panel_rect("25|926|0|46"))

    def test_query_failure_returns_none_without_raising(self):
        with mock.patch.object(
                da.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(["osascript"],
                                                      da.PANEL_TIMEOUT)):
            self.assertIsNone(da.get_panel_rect())

    def test_short_cache_and_invalidation(self):
        """连续点排列不该每次拉起 osascript；Dock 设置切换要能立刻失效。"""
        with mock.patch.object(
                da.subprocess, "run",
                return_value=_result(stdout="25|926|1462|46")) as run:
            self.assertEqual(da.get_panel_rect(), (25, 926, 1462, 46))
            self.assertEqual(da.get_panel_rect(), (25, 926, 1462, 46))
            self.assertEqual(run.call_count, 1)
            da.invalidate_panel_cache()
            self.assertEqual(da.get_panel_rect(), (25, 926, 1462, 46))
            self.assertEqual(run.call_count, 2)


class TestSetAutohide(unittest.TestCase):
    def _run(self, read_result, restart_result=None):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:2] == ["defaults", "read"]:
                return read_result
            if cmd[:2] == ["defaults", "write"]:
                return _result()
            if cmd == ["killall", "Dock"]:
                return restart_result or _result()
            raise AssertionError("意外的子进程命令: %r" % (cmd,))

        return calls, fake_run

    def test_enable_writes_restarts_and_verifies(self):
        calls, fake_run = self._run(_result(stdout="1\n"))
        with mock.patch.object(da.subprocess, "run", side_effect=fake_run):
            ok, text = da.set_autohide(True)
        self.assertTrue(ok, text)
        self.assertIn("自动隐藏", text)
        self.assertEqual(
            [c[0] for c in calls],
            [["defaults", "write", "com.apple.dock", "autohide", "-bool", "true"],
             ["killall", "Dock"],
             ["defaults", "read", "com.apple.dock", "autohide"]])
        for _cmd, kwargs in calls:
            self.assertEqual(kwargs["timeout"], da.TIMEOUT)

    def test_disable_writes_false(self):
        calls, fake_run = self._run(_result(stdout="0\n"))
        with mock.patch.object(da.subprocess, "run", side_effect=fake_run):
            ok, text = da.set_autohide(False)
        self.assertTrue(ok, text)
        self.assertIn("固定显示", text)
        self.assertIn("false", calls[0][0])

    def test_verify_mismatch_fails(self):
        """写入后读回仍是旧值：必须失败，不能显示「已隐藏」。"""
        _calls, fake_run = self._run(_result(stdout="0\n"))
        with mock.patch.object(da.subprocess, "run", side_effect=fake_run):
            ok, text = da.set_autohide(True)
        self.assertFalse(ok)
        self.assertIn("未生效", text)

    def test_write_failure_is_reported(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _result(returncode=1, stderr="permission denied")

        with mock.patch.object(da.subprocess, "run", side_effect=fake_run):
            ok, text = da.set_autohide(True)
        self.assertFalse(ok)
        self.assertIn("permission denied", text)
        self.assertEqual(len(calls), 1, "写入失败后不该继续重启 Dock")

    def test_restart_failure_is_reported(self):
        _calls, fake_run = self._run(
            _result(stdout="1\n"),
            restart_result=_result(returncode=1, stderr="Dock: No such process"))
        with mock.patch.object(da.subprocess, "run", side_effect=fake_run):
            ok, text = da.set_autohide(True)
        self.assertFalse(ok)
        self.assertIn("重启 Dock 失败", text)

    def test_verify_read_failure_is_reported(self):
        _calls, fake_run = self._run(
            _result(returncode=1, stderr="The domain/default pair does not exist"))
        with mock.patch.object(da.subprocess, "run", side_effect=fake_run):
            ok, text = da.set_autohide(True)
        self.assertFalse(ok)
        self.assertIn("无法读回", text)


if __name__ == "__main__":
    unittest.main()
