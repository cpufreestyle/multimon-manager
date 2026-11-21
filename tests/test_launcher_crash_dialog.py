#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动器必须挡住 macOS 的「上次 Python 意外退出」恢复弹窗。

回归背景：本应用跑在 `Python.app/Contents/MacOS/Python` 上，进程归属
bundle `org.python.python`。系统把**任何** python 进程的崩溃记到这个 bundle
头上，下次启动本应用就弹「The last time you opened Python, it unexpectedly
quit while reopening windows」，而且这个弹窗会**阻塞启动**（log 停在「缓存
已读入」，界面 1 分钟后才出现）。主进程 abort 被守护脚本重启时，用户看到
的就是「反复弹」。

启动器的对策（build._LAUNCHER）：
   1) 禁用窗口状态保存/恢复（NSQuitAlwaysKeepsWindows 等三个键）；
   2) 清掉遗留的 `org.python.python.savedState`——恢复弹窗就是它触发的；
   3) 每次拉起 python 前都清一遍，主进程重启链上每一步都不漏。

这里用假 HOME + 假 `defaults` 做行为断言：状态目录被删、三个键都写过、
`--print-python` 不碰用户偏好。
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build  # noqa: E402


def _write_stub_python(path):
    """假 python：import tkinter 探测成功，直接退出 0（模拟正常启动）。"""
    body = "#!/bin/bash\nexit 0\n"
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP
             | stat.S_IXOTH)


def _write_stub_defaults(path, record):
    """假 defaults：把每次调用的参数记到 record 文件里。"""
    body = '#!/bin/bash\necho "$@" >> "%s"\nexit 0\n' % record
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP
             | stat.S_IXOTH)


@unittest.skipIf(os.name == "nt", "启动器是 bash 脚本，Windows 上不适用")
@unittest.skipIf(shutil.which("bash") is None, "没有 bash")
class TestLauncherBlocksCrashRestoreDialog(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mmm_crashdlg_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        macos = os.path.join(self.tmp, "bundle", "Contents", "MacOS")
        resources = os.path.join(self.tmp, "bundle", "Contents", "Resources")
        os.makedirs(macos)
        os.makedirs(resources)
        self.launcher = os.path.join(macos, "MultiMonManager")
        with open(self.launcher, "w") as fh:
            fh.write(build._LAUNCHER)
        os.chmod(self.launcher, 0o755)

        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.tmpdir = os.path.join(self.tmp, "tmp")
        os.makedirs(self.tmpdir)

        self.record = os.path.join(self.tmp, "defaults_record.txt")
        self.defaults_stub = os.path.join(self.tmp, "defaults_stub")
        _write_stub_defaults(self.defaults_stub, self.record)

        self.py_stub = os.path.join(self.tmp, "py_stub")
        _write_stub_python(self.py_stub)

    def _run(self, extra_env=None, args=()):
        env = dict(os.environ)
        env["HOME"] = self.home
        env["TMPDIR"] = self.tmpdir
        env["MMM_PY_CANDIDATES"] = self.py_stub
        env["MMM_DEFAULTS_BIN"] = self.defaults_stub
        env.pop("MMM_SKIP_RESTART", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run([self.launcher] + list(args),
                              capture_output=True, text=True, env=env, timeout=60)

    def _saved_state_dir(self):
        return os.path.join(self.home, "Library", "Saved Application State",
                            "org.python.python.savedState")

    def test_removes_stale_saved_state_and_writes_disable_keys(self):
        """遗留的 savedState 必须被删，且三个「禁用恢复」的键都要写过。"""
        saved = self._saved_state_dir()
        os.makedirs(saved)
        with open(os.path.join(saved, "window.plist"), "w") as fh:
            fh.write("stale")

        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(saved),
                         "遗留的窗口恢复状态没被清掉，弹窗还会再出现")

        with open(self.record) as fh:
            calls = fh.read()
        self.assertIn("NSQuitAlwaysKeepsWindows -bool false", calls)
        self.assertIn("NSDisableRelaunchOnReopen -bool true", calls)
        self.assertIn("ApplePersistenceIgnoreState -bool true", calls)
        self.assertIn("org.python.python", calls)

    def test_cleanup_runs_before_every_python_start(self):
        """守护脚本重启主进程时也要再清一遍（否则重启链上会反复弹）。"""
        # 假 python 第一次返回 1（模拟 abort），并在退出前「留下」一份
        # 窗口恢复状态——重启链上的第二轮必须把它清掉。
        body = (
            "#!/bin/bash\n"
            # 解释器探测（-c "import tkinter"）必须立刻成功，否则启动器会
            # 认定这个解释器不可用而换一个，那条分支就测不到了。
            'if [ "$1" = "-c" ]; then exit 0; fi\n'
            'if [ ! -f "$MARKER" ]; then\n'
            '  touch "$MARKER"\n'
            '  SAVED="$HOME/Library/Saved Application State/'
            'org.python.python.savedState"\n'
            '  mkdir -p "$SAVED"\n'
            '  echo stale > "$SAVED/window.plist"\n'
            "  exit 1\n"
            "fi\n"
            "exit 0\n"
        )
        with open(self.py_stub, "w") as fh:
            fh.write(body)
        os.chmod(self.py_stub, os.stat(self.py_stub).st_mode | stat.S_IEXEC
                 | stat.S_IXGRP | stat.S_IXOTH)
        marker = os.path.join(self.tmp, "marker")

        r = self._run(extra_env={"MARKER": marker})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(marker),
                        "守护脚本没有重启主进程，这条用例没测到东西")
        saved = self._saved_state_dir()
        self.assertFalse(os.path.exists(saved),
                         "重启链上的第二轮没有再清理恢复状态")

    def test_print_python_does_not_touch_user_preferences(self):
        """--print-python 只探测解释器，不该改用户的系统偏好。"""
        r = self._run(args=["--print-python"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), self.py_stub)
        self.assertFalse(os.path.exists(self.record),
                         "--print-python 不该写 defaults")


if __name__ == "__main__":
    unittest.main()
