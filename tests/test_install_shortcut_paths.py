#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""install.py 的桌面路径解析与卸载批处理。

回归背景：安装器原来用 `os.path.join(os.path.expanduser("~"), "Desktop")`
硬拼桌面路径。中文 Windows 上用户的桌面常常被重定向到别处（本机是
`D:\\michael\\桌面`），`%USERPROFILE%\\Desktop` 只是个没人看的遗留空目录，
于是「快捷方式放桌面」这个需求实际落空——快捷方式建到了一个用户永远不会
打开的目录里。卸载批处理里同一处硬拼也让卸载后留下孤儿 .lnk。

这里锁定三件事：
1. 桌面目录优先读资源管理器自己记录的 `User Shell Folders!Desktop`，
   而不是猜；
2. 读不到（键不存在 / 值为空 / 指向的目录不存在）时退回硬拼，不能崩；
3. 卸载批处理把真实桌面路径和遗留路径都写进去，且中文能正常编码。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import install  # noqa: E402

# 整份用例都是 Windows 专属：读注册表、写 .lnk 快捷方式和卸载 .bat。
# macOS 上 install.py 没有 winreg 分支，这些断言注定不成立，按平台跳过。
IS_WIN = sys.platform == "win32"

_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"


@unittest.skipUnless(IS_WIN, "Windows 专属")
class DesktopDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.desktop = os.path.join(self._tmp.name, "桌面")
        os.makedirs(self.desktop)

    def _registry(self, value):
        """让 QueryValueEx 返回指定 Desktop 值。"""
        return mock.patch("winreg.QueryValueEx", return_value=(value, 2))

    def test_reads_real_desktop_from_registry(self):
        with self._registry(self.desktop):
            self.assertEqual(install._desktop_dir(), self.desktop)

    def test_expands_env_vars_and_strips_quotes(self):
        home = os.path.expanduser("~")
        with self._registry('"%USERPROFILE%"'):
            self.assertEqual(install._desktop_dir(), home)

    def test_falls_back_when_key_missing(self):
        with mock.patch("winreg.OpenKey", side_effect=OSError):
            self.assertEqual(
                install._desktop_dir(),
                os.path.join(os.path.expanduser("~"), "Desktop"),
            )

    def test_falls_back_when_value_empty(self):
        with self._registry(""):
            self.assertEqual(
                install._desktop_dir(),
                os.path.join(os.path.expanduser("~"), "Desktop"),
            )

    def test_falls_back_when_pointed_dir_missing(self):
        with self._registry(os.path.join(self._tmp.name, "gone", "Desktop")):
            self.assertEqual(
                install._desktop_dir(),
                os.path.join(os.path.expanduser("~"), "Desktop"),
            )

    def test_real_machine_desktop_is_an_existing_directory(self):
        # 不假设桌面在哪，只要求解析结果是个真实存在的目录：
        # 快捷方式建进去才点得到。
        self.assertTrue(os.path.isdir(install._desktop_dir()))


@unittest.skipUnless(IS_WIN, "Windows 专属")
class DesktopLnkCandidatesTest(unittest.TestCase):
    def test_lists_real_desktop_first_then_legacy_profile_desktop(self):
        real = r"D:\michael\桌面"
        legacy = r"C:\Users\michael\Desktop"
        with mock.patch.object(install, "_desktop_dir", return_value=real):
            got = install._desktop_lnk_candidates()
        self.assertEqual(
            got,
            [os.path.join(real, "MultiMonManager.lnk"),
             os.path.join(legacy, "MultiMonManager.lnk")],
        )

    def test_dedupes_when_desktop_is_not_redirected(self):
        home = os.path.expanduser("~")
        with mock.patch.object(
            install, "_desktop_dir",
            return_value=os.path.join(home, "Desktop"),
        ):
            got = install._desktop_lnk_candidates()
        self.assertEqual(
            got, [os.path.join(home, "Desktop", "MultiMonManager.lnk")])


@unittest.skipUnless(IS_WIN, "Windows 专属")
class UninstallBatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install_dir = os.path.join(self._tmp.name, "MultiMonManager")
        os.makedirs(self.install_dir)

    def _bat_text(self):
        bat = os.path.join(self.install_dir, "uninstall.bat")
        with open(bat, "rb") as fh:
            return fh.read().decode("mbcs")

    def test_writes_both_desktop_paths_and_start_menu(self):
        real = r"D:\michael\桌面"
        legacy = r"C:\Users\michael\Desktop"
        with mock.patch.object(install, "_desktop_dir", return_value=real):
            install._write_uninstall_bat(self.install_dir)
        text = self._bat_text()
        self.assertIn('del "D:\\michael\\桌面\\MultiMonManager.lnk"', text)
        self.assertIn('del "C:\\Users\\michael\\Desktop\\MultiMonManager.lnk"', text)
        self.assertIn(
            'del "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs'
            '\\MultiMonManager.lnk"', text)
        self.assertIn('rmdir /s /q "%s"' % self.install_dir, text)

    def test_chinese_echo_survives_encoding(self):
        with mock.patch.object(install, "_desktop_dir", return_value=self._tmp.name):
            install._write_uninstall_bat(self.install_dir)
        self.assertIn("卸载完成", self._bat_text())

    def test_bat_stays_ascii_safe_when_desktop_is_ascii(self):
        with mock.patch.object(install, "_desktop_dir", return_value="C:\\Users\\x\\Desktop"):
            install._write_uninstall_bat(self.install_dir)
        self.assertIn('del "C:\\Users\\x\\Desktop\\MultiMonManager.lnk"', self._bat_text())


if __name__ == "__main__":
    unittest.main()
