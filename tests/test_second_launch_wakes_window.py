#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""已有实例运行时再次启动必须唤醒其窗口，不能静默退出。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cmd_channel  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
