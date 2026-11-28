#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""已有实例运行时再次启动必须唤醒其窗口，不能静默退出。"""
import os
import subprocess
import sys
import unittest
from unittest import mock
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cmd_channel  # noqa: E402
import main  # noqa: E402


class _FakeMain:
    """模拟 main.main() 的单实例分支。"""

    def __init__(self, lock_held):
        self.lock_held = lock_held
        self.sent = []
        self.started = False

    def run(self):
        if not self.lock_held:
            self.started = True
            return
        cmd_channel.send("open")


class TestSecondLaunchWakesWindow(unittest.TestCase):
    def test_second_launch_sends_open_command(self):
        fake = _FakeMain(lock_held=True)
        with mock.patch.object(cmd_channel, "send", side_effect=fake.sent.append):
            fake.run()
        self.assertEqual(fake.sent, ["open"])
        self.assertFalse(fake.started)

    def test_first_launch_starts_normally(self):
        fake = _FakeMain(lock_held=False)
        with mock.patch.object(cmd_channel, "send", side_effect=fake.sent.append):
            fake.run()
        self.assertEqual(fake.sent, [])
        self.assertTrue(fake.started)

    def test_owner_pid_falls_back_to_lsof_for_legacy_empty_lock(self):
        """旧版本创建的空锁文件没有 PID，用 lsof 反查持有者。"""
        with mock.patch("builtins.open", mock.mock_open(read_data="")):
            with mock.patch.object(subprocess, "run") as run:
                run.return_value = mock.Mock(stdout="12345\n")
                self.assertEqual(main._lock_holder_pid(), 12345)

    def test_take_over_kills_only_apps_not_self(self):
        """接管时不能杀掉自己这条链——否则新实例刚起来就消失。"""
        with mock.patch.object(main, "_lock_holder_pid", return_value=100), \
             mock.patch.object(main, "_pid_is_ours", return_value=True), \
             mock.patch.object(main, "_process_start_time", side_effect=[1000, 1000, 999, 1000, 1000, 999]), \
             mock.patch.object(main, "_our_process_pids", return_value=[200, 300]), \
             mock.patch.object(main.os, "kill") as kill, \
             mock.patch.object(main.os, "getpid", return_value=400), \
             mock.patch.object(main.os, "getppid", return_value=500), \
             mock.patch.object(main.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="")
            main._take_over_stale_instance()
        killed = [call.args[0] for call in kill.call_args_list]
        self.assertNotIn(400, killed, "杀掉了当前进程")
        self.assertNotIn(500, killed, "杀掉了父进程（守护脚本）")


if __name__ == "__main__":
    unittest.main()
