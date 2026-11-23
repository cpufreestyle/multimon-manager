#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""程序图标必须真的显示出来（Windows 窗口 + 托盘 + 构建打包）。

回归背景：用户反馈「软件图标没显示」，截图里标题栏左边是 Python 的羽毛
图标。根因有两处叠加：

1. main.py 里把设置窗口图标的整段代码套在 `if not sys.frozen` 里。源码运行
   正常，打包成 exe 后整段被跳过——而用户日常用的就是 exe。
2. build.py 的 PyInstaller 命令只有 `--icon`。它只设置 exe **文件本身**的
   图标（资源管理器里看到的那个），不会把 app.ico 放进 _MEIPASS，于是运行时
   `resources.asset_path("app.ico")` 找不到文件，想让窗口用上图标也没有源。

这里锁定三件事：
- asset_path 在源码运行 / 冻结运行 / 资源缺失三种情况下都给出正确结果；
- 构建命令必须带 --add-data（否则 exe 里永远没有 app.ico）；
- 冻结运行时也必须调用 iconbitmap，不能因为 frozen 就跳过。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build  # noqa: E402
import resources  # noqa: E402


class AssetPathTest(unittest.TestCase):
    def test_finds_resource_next_to_module_when_not_frozen(self):
        with mock.patch.object(sys, "frozen", False, create=True), \
                mock.patch.object(sys, "_MEIPASS", None, create=True):
            self.assertTrue(resources.asset_path("app.ico"))

    def test_prefers_meipass_when_frozen(self):
        """冻结运行时先找 _MEIPASS——那是 PyInstaller 解包资源的唯一位置。"""
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", r"C:\Temp\_MEI123", create=True), \
                mock.patch.object(os.path, "exists", return_value=True):
            got = resources.asset_path("app.ico")
        self.assertEqual(got, os.path.join(r"C:\Temp\_MEI123", "app.ico"))

    def test_falls_back_to_exe_dir(self):
        """_MEIPASS 里没有（构建漏带资源）时，退到 exe 同目录找。"""
        meipass = r"C:\Temp\_MEI999"
        exe = r"C:\Apps\MultiMonManager\MultiMonManager.exe"
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", meipass, create=True), \
                mock.patch.object(sys, "executable", exe, create=True), \
                mock.patch.object(
                    os.path, "exists",
                    side_effect=lambda p: p == os.path.join(
                        os.path.dirname(exe), "app.ico")):
            got = resources.asset_path("app.ico")
        self.assertEqual(got, os.path.join(os.path.dirname(exe), "app.ico"))

    def test_returns_none_when_absent(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", r"C:\Temp\_MEI1", create=True), \
                mock.patch.object(sys, "executable", r"C:\x\a.exe", create=True), \
                mock.patch.object(os.path, "exists", return_value=False):
            self.assertIsNone(resources.asset_path("nope.ico"))


class BuildCommandTest(unittest.TestCase):
    """构建命令必须把 app.ico 打进包里。"""

    def _capture(self):
        seen = []

        class _Done:
            returncode = 0

        def fake_run(cmd, **kw):
            seen.append(list(cmd))
            return _Done()

        ico = os.path.join(build.HERE, "app.ico")
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(build.sys, "platform", "win32"), \
                mock.patch.object(build, "_make_ico", return_value=ico), \
                mock.patch.object(build.subprocess, "run", side_effect=fake_run):
            build.build_windows(os.path.join(build.HERE, "app.png"))
        return seen[0] if seen else []

    def test_add_data_includes_ico(self):
        cmd = self._capture()
        joined = " ".join(cmd)
        self.assertIn("--add-data", joined,
                      "构建命令没带 --add-data，exe 里不会有 app.ico")
        marker = [c for c in cmd if c.startswith("--add-data")]
        self.assertTrue(marker, "找不到 --add-data 参数")
        self.assertIn("app.ico", marker[0])
        self.assertIn(os.pathsep, marker[0],
                      "--add-data 的分隔符必须是 os.pathsep（Windows 上为 ;）")

    def test_icon_flag_still_sets_exe_icon(self):
        cmd = self._capture()
        self.assertTrue(any(c.startswith("--icon=") for c in cmd),
                        "exe 文件本身的图标不能丢")


if __name__ == "__main__":
    unittest.main()
