#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App 启动器的解释器选择：必须选「真的能 import tkinter」的那个。

回归背景：Homebrew 装上 python@3.15 后 /opt/homebrew/bin/python3 指向它，而
python@3.15 没装 python-tk。启动器原来只判「可执行」就 exec 下去，App 起来
第一行 import tkinter 直接 ModuleNotFoundError——用户看到的是「双击 App 没
反应」。同一颗雷也埋在 run.sh 与测试命令里：tests/test_quad_capacity.py 的
QuadCapacityTextTest 要 import ui，用没 tkinter 的解释器跑会整类 skip，退出码
仍是 0，覆盖率静默少一截。

这里用假解释器桩（shell 脚本，按 import tkinter 的退出码区分）验证选择顺序、
兜底与用户覆盖，再对真实产物断言「选中的解释器确实导得进 tkinter」。
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

REAL_LAUNCHER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dist", "多屏管理器.app", "Contents", "MacOS", "MultiMonManager")


def _write_stub(path, has_tkinter):
    """造一个假 python：按 import tkinter 的退出码区分有没有 tkinter。"""
    body = "#!/bin/bash\n"
    if has_tkinter:
        body += 'if [ "$1" = "-c" ] && [ "$2" = "import tkinter" ]; then exit 0; fi\nexit 0\n'
    else:
        body += ('echo "ModuleNotFoundError: No module named _tkinter" >&2\n'
                 'if [ "$1" = "-c" ] && [ "$2" = "import tkinter" ]; then exit 1; fi\nexit 0\n')
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP
             | stat.S_IXOTH)


def _render_launcher(root):
    """把 build._LAUNCHER 落到 root/Contents/MacOS/ 下，返回可执行路径。

    启动器会 cd 到 ../Resources，那个目录必须存在，否则它在打印解释器之前
    就退出了。
    """
    macos = os.path.join(root, "Contents", "MacOS")
    resources = os.path.join(root, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(resources)
    path = os.path.join(macos, "MultiMonManager")
    with open(path, "w") as fh:
        fh.write(build._LAUNCHER)
    os.chmod(path, 0o755)
    return path


@unittest.skipIf(os.name == "nt", "启动器是 bash 脚本，Windows 上不适用")
@unittest.skipIf(shutil.which("bash") is None, "没有 bash")
class TestLauncherPicksTkinterCapablePython(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mmm_launcher_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "bundle")
        self.launcher = _render_launcher(self.root)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

    def _pick(self, candidates, home=None):
        env = dict(os.environ)
        env["MMM_PY_CANDIDATES"] = " ".join(candidates)
        env["HOME"] = home if home is not None else self.home
        r = subprocess.run([self.launcher, "--print-python"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_picks_first_candidate_with_tkinter(self):
        good = os.path.join(self.tmp, "py_good")
        _write_stub(good, True)
        self.assertEqual(self._pick([good]), good)

    def test_skips_candidates_without_tkinter(self):
        bad1 = os.path.join(self.tmp, "py_bad1")
        bad2 = os.path.join(self.tmp, "py_bad2")
        good = os.path.join(self.tmp, "py_good2")
        _write_stub(bad1, False)
        _write_stub(bad2, False)
        _write_stub(good, True)
        self.assertEqual(self._pick([bad1, bad2, good]), good)

    def test_none_with_tkinter_falls_back_to_python3(self):
        bad = os.path.join(self.tmp, "py_bad3")
        _write_stub(bad, False)
        self.assertEqual(self._pick([bad]), "python3")

    def test_missing_candidate_is_skipped_not_fatal(self):
        """候选不存在时不能崩：路径不在就不再试它。"""
        good = os.path.join(self.tmp, "py_good3")
        _write_stub(good, True)
        gone = os.path.join(self.tmp, "does_not_exist")
        self.assertEqual(self._pick([gone, good]), good)

    def test_user_override_with_tkinter_wins(self):
        good = os.path.join(self.tmp, "py_good4")
        _write_stub(good, True)
        override = os.path.join(self.tmp, "py_override")
        _write_stub(override, True)
        with open(os.path.join(self.home, ".multimon_python"), "w") as fh:
            fh.write(override)
        self.assertEqual(self._pick([good], home=self.home), override)

    def test_broken_user_override_falls_back_to_probing(self):
        """覆盖指向的解释器没有 tkinter 时必须继续探测，而不是照旧崩。"""
        good = os.path.join(self.tmp, "py_good5")
        _write_stub(good, True)
        broken = os.path.join(self.tmp, "py_broken")
        _write_stub(broken, False)
        with open(os.path.join(self.home, ".multimon_python"), "w") as fh:
            fh.write(broken)
        self.assertEqual(self._pick([good], home=self.home), good)

    def test_empty_override_file_falls_back_to_probing(self):
        good = os.path.join(self.tmp, "py_good6")
        _write_stub(good, True)
        with open(os.path.join(self.home, ".multimon_python"), "w") as fh:
            fh.write("")
        self.assertEqual(self._pick([good], home=self.home), good)


@unittest.skipIf(not os.path.exists(REAL_LAUNCHER),
                 "dist 里的 App 未构建")
class TestRealLauncherPicksUsablePython(unittest.TestCase):
    """对真实产物断言：选中的解释器确实导得进 tkinter。

    这条与机器上装了哪些 Python 无关——只约束「选中即可用」这个契约，
    正是被 python@3.15 打破的那一条。
    """

    def test_chosen_python_can_import_tkinter(self):
        env = dict(os.environ)
        env.pop("MMM_PY_CANDIDATES", None)
        r = subprocess.run([REAL_LAUNCHER, "--print-python"],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        chosen = r.stdout.strip()
        self.assertTrue(chosen, "启动器没选出解释器")
        # 兜底值 python3 无从验证（可能不在 PATH）；能定位到可执行文件就必须
        # 真的导得进 tkinter
        resolved = shutil.which(chosen)
        if resolved is None:
            self.skipTest("选中的 %s 不在 PATH，无从验证" % chosen)
        probe = subprocess.run([resolved, "-c", "import tkinter"],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(probe.returncode, 0,
                         "%s 导不进 tkinter：%s" % (resolved, probe.stderr))


if __name__ == "__main__":
    unittest.main()
