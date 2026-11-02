# -*- coding: utf-8 -*-
"""单实例锁的 fd 必须带 CLOEXEC，否则托盘子进程继承它、锁永不释放。

回归背景：主进程退出后托盘面板（tray_mac 用 Popen 派生）仍持有锁 fd，
flock 锁因此一直不释放。用户手动退出 App 再启动，日志写
「已有实例在运行，退出」，而其实没有任何进程在跑，只能靠 lsof 找到托盘
面板再手杀。macOS 上 Popen 走 posix_spawn 路径，close_fds 语义不可靠，
所以在拿锁后直接给 fd 置 FD_CLOEXEC。
"""
import fcntl
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


class SingleInstanceTest(unittest.TestCase):

    def _release(self):
        if main._lock_fd is not None:
            main._lock_fd.close()
            main._lock_fd = None

    def test_lock_fd_has_cloexec(self):
        """拿锁后 _lock_fd 的 FD_CLOEXEC 必须已置位（子进程不会继承）。"""
        main._lock_fd = None
        try:
            self.assertTrue(main._single_instance(), "应当能拿到锁")
            fd = main._lock_fd.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            self.assertTrue(
                flags & fcntl.FD_CLOEXEC,
                "锁 fd 未置 FD_CLOEXEC：子进程会继承它，"
                "导致主进程退出后锁不释放")
        finally:
            self._release()

    def test_lock_released_after_close(self):
        """关掉 fd 后锁必须能再次拿到（证明锁确实跟着 fd 走）。"""
        main._lock_fd = None
        try:
            self.assertTrue(main._single_instance())
            self._release()
            self.assertTrue(
                main._single_instance(), "关闭 fd 后应能重新拿到锁")
        finally:
            self._release()

    def test_second_holder_blocked(self):
        """同一把锁不能被第二个实例同时持有。"""
        main._lock_fd = None
        other = None
        try:
            self.assertTrue(main._single_instance())
            lock_path = os.path.join(tempfile.gettempdir(),
                                     "multimon_manager.lock")
            other = open(lock_path, "w")
            with self.assertRaises(OSError):
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if other is not None:
                other.close()
            self._release()


if __name__ == "__main__":
    unittest.main()
