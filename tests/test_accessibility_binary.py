#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""辅助功能授权：responsible_binary 必须给出 TCC 真正认账的那个二进制。

回归背景：UI 原来的授权提示写「源码运行版为 python3」，照着去 系统设置 →
隐私与安全性 → 辅助功能 加列表，用户会发现加了没反应。原因是 macOS 按「真正
负责的进程」记账：Homebrew 的 framework Python 跑起来会把自己重新 exec 进
.../Versions/3.14/Resources/Python.app/Contents/MacOS/Python，而
sys.executable 只是 .../opt/python@3.14/bin/python3.14 这个软链。

第一版实现还踩过另一个坑：函数里用了 ctypes.CDLL，但 accessibility_mac 顶层
只 from ctypes import 具体名字、没有 import ctypes，NameError 被 except 吞掉后
静默退回 sys.executable 的软链路径——正好是要避免的错路径。这里钉住它。
"""
import ctypes
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import accessibility_mac as am  # noqa: E402

IS_MAC = (sys.platform == "darwin")


@unittest.skipUnless(IS_MAC, "responsible_binary 只在 macOS 有意义")
class TestResponsibleBinary(unittest.TestCase):

    def test_returns_existing_file(self):
        p = am.responsible_binary()
        self.assertTrue(p, "没取到路径")
        self.assertTrue(os.path.exists(p), "路径不存在: %s" % p)
        self.assertTrue(os.path.isabs(p), "不是绝对路径: %s" % p)

    def _oracle_executable_path(self):
        """独立实现一遍 _NSGetExecutablePath，作为基准值。

        不复用被测代码，否则两边一起错也看不出来。
        """
        from ctypes import CDLL, byref, c_uint32, create_string_buffer
        libc = CDLL(None)
        size = c_uint32(0)
        libc._NSGetExecutablePath(None, byref(size))
        buf = create_string_buffer(size.value)
        self.assertEqual(libc._NSGetExecutablePath(buf, byref(size)), 0)
        return buf.value.decode("utf-8", "replace")

    def test_matches_the_real_process_executable(self):
        """关键断言：必须等于本进程真实的可执行文件路径。

        这是 oracle 对比——直接用 _NSGetExecutablePath 独立算一遍再比。曾经的
        bug 是函数里写了 ctypes.CDLL 而模块顶层没 import ctypes，NameError 被
        except 吞掉后静默退回 sys.executable 的软链路径。带条件断言的旧写法会在
        bug 触发时跳过检查，这条无条件对比才抓得住。
        """
        self.assertEqual(am.responsible_binary(),
                         self._oracle_executable_path())

    def test_not_the_symlink_path(self):
        """不能是 sys.executable 的那个软链路径。

        framework Python 下真实进程是 Python.app 里的二进制，而
        sys.executable 是 .../opt/python@3.14/bin/python3.14 这个软链。两者
        确实不同时，返回值必须是前者——照软链去授权，用户会看到「加了没反应」。
        """
        p = am.responsible_binary()
        link_path = os.path.realpath(sys.executable)
        if os.path.realpath(p) != link_path:
            self.assertNotEqual(
                os.path.realpath(p), link_path,
                "返回了 sys.executable 的软链路径，TCC 不认这个")
            self.assertTrue(p.endswith("Python.app/Contents/MacOS/Python"),
                            "framework Python 下应给出 Python.app 里的二进制，"
                            "实际: %s" % p)


@unittest.skipUnless(IS_MAC, "平台相关")
class TestResponsibleBinaryFallback(unittest.TestCase):

    def test_ctypes_failure_falls_back_to_realpath(self):
        """_NSGetExecutablePath 拿不到时必须退回 sys.executable 的 realpath。

        同时这条保证兜底路径也是「存在且绝对」的，不会返回 None 让 UI 去显示
        一句没有路径的空提示。
        """
        with mock.patch.object(ctypes, "CDLL", side_effect=OSError("boom")):
            p = am.responsible_binary()
        self.assertEqual(p, os.path.realpath(sys.executable))
        self.assertTrue(os.path.exists(p))

    def test_empty_result_falls_back(self):
        """_NSGetExecutablePath 返回空串时也不能把空串当路径交出去。"""
        real_cdll = ctypes.CDLL

        class FakeLibc:
            def _NSGetExecutablePath(self, buf, size):
                return 0  # 成功，但不往 buf 里写东西

        def fake_cdll(*a, **k):
            return FakeLibc()

        with mock.patch.object(ctypes, "CDLL", fake_cdll):
            p = am.responsible_binary()
        self.assertTrue(p, "空串被当成路径返回了")
        self.assertTrue(os.path.exists(p), "兜底路径不存在: %s" % p)


@unittest.skipUnless(IS_MAC, "reveal_binary 只在 macOS 有意义")
class TestRevealBinary(unittest.TestCase):

    def test_reveals_the_responsible_binary(self):
        """reveal 必须在 Finder 里选中 responsible_binary() 指向的那个文件。"""
        # 用一个真实存在的文件：reveal_binary 会先校验路径存在再开 Finder，
        # 塞不存在的假路径会被它自己挡掉（那条行为由下面两个用例覆盖）。
        import tempfile
        fd, real = tempfile.mkstemp(prefix="mmm_reveal_")
        os.close(fd)
        self.addCleanup(os.unlink, real)
        with mock.patch.object(am, "responsible_binary", return_value=real), \
                mock.patch.object(am.subprocess, "run") as run:
            self.assertTrue(am.reveal_binary())
        run.assert_called_once_with(["open", "-R", real],
                                    check=False, timeout=5)

    def test_no_path_returns_false_without_opening_finder(self):
        with mock.patch.object(am, "responsible_binary", return_value=None), \
                mock.patch.object(am.subprocess, "run") as run:
            self.assertFalse(am.reveal_binary())
        run.assert_not_called()

    def test_nonexistent_path_returns_false(self):
        gone = os.path.join(os.sep, "definitely", "not", "here", "Python")
        with mock.patch.object(am, "responsible_binary", return_value=gone), \
                mock.patch.object(am.subprocess, "run") as run:
            self.assertFalse(am.reveal_binary())
        run.assert_not_called()

    def test_open_failure_returns_false_not_raise(self):
        """Finder 打不开不能把异常抛到 UI 线程里。"""
        import tempfile
        fd, real = tempfile.mkstemp(prefix="mmm_reveal_")
        os.close(fd)
        self.addCleanup(os.unlink, real)
        with mock.patch.object(am, "responsible_binary", return_value=real), \
                mock.patch.object(am.subprocess, "run",
                                  side_effect=OSError("no finder")):
            self.assertFalse(am.reveal_binary())


class TestOpenSettings(unittest.TestCase):

    def test_opens_accessibility_pane(self):
        with mock.patch.object(am.subprocess, "run") as run:
            am.open_settings()
        args = run.call_args[0][0]
        self.assertEqual(args[0], "open")
        self.assertIn("Privacy_Accessibility", args[1])


if __name__ == "__main__":
    unittest.main()
