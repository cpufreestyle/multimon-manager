#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量单测入口：带看门狗的 `unittest discover`。

2026-11-13 之前，`python3 -m unittest discover -s tests` 在真机上会挂死，
外层只能等 75 秒后 SIGTERM（exit 137），既拿不到失败原因也拿不到栈。
这里用 `signal.alarm` 给整套套件一个硬上限：超时打印所有线程栈、按
`TEST_SUITE_TIMEOUT` 这个具体拒因退出 124，**不静默、不无限等**。

Windows 没有 `signal.SIGALRM`（Posix 专有），那边改用后台定时线程装
同样的看门狗：到点照样打印所有线程栈、以 124 退出。否则这个入口在
Windows 上直接 AttributeError 跑不起来，套件只能裸跑——挂死时又退化成
无限等。

用法（一般由 run_tests.sh 调用）：
    python3 run_tests_guarded.py                  # 默认 240s
    MMM_TEST_TIMEOUT=60 python3 run_tests_guarded.py
    python3 run_tests_guarded.py --selfcheck     # 反向验证看门狗真会响

`--selfcheck` 会造一个故意 sleep 的假套件，要求守卫在 1 秒内以 124 退出；
若有人删掉 alarm，假套件会卡住，自测会明确报 FAIL（而不是等成假绿）。
"""
import faulthandler
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

DEFAULT_TIMEOUT = 240
TIMEOUT_MARKER = "TEST_SUITE_TIMEOUT"
TIMEOUT_RC = 124
_HAS_ALARM = hasattr(signal, "SIGALRM")


def _fire_timeout():
    """超时回调：打印所有线程栈并以 124 硬退。"""
    sys.stderr.write(
        "\n%s: unittest discover 超过 %s 秒未完成，判为挂起（不是慢）。"
        "下面打印所有线程栈。\n" % (TIMEOUT_MARKER, _timeout))
    sys.stderr.flush()
    faulthandler.dump_traceback()
    sys.stderr.flush()
    # 必须 os._exit：SystemExit 会被 unittest 当成用例异常吞掉，跑完整套后
    # 只报 rc=1，看门狗就退化成「看起来失败了但没有超时拒因」。
    os._exit(TIMEOUT_RC)


def _on_alarm(_signum, _frame):
    _fire_timeout()


def _arm(timeout):
    """装上看门狗，返回解除用的句柄（POSIX 下是 None）。

    Posix 用 signal.alarm（信号打断主线程，最直接）；Windows 没有
    SIGALRM，退化成 daemon 定时线程——到点调同一个 _fire_timeout。
    """
    if _HAS_ALARM:
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(timeout)
        return None
    timer = threading.Timer(timeout, _fire_timeout)
    timer.daemon = True
    timer.start()
    return timer


def _disarm(handle):
    """解除看门狗。"""
    if handle is None:
        if _HAS_ALARM:
            signal.alarm(0)
        return
    handle.cancel()


def selfcheck():
    """负例：故意挂住的假套件必须被看门狗按 124 掐掉。"""
    with tempfile.TemporaryDirectory(prefix="mmm_guard_selfcheck_") as td:
        tests = Path(td) / "tests"
        tests.mkdir()
        (tests / "test_hang.py").write_text(
            "import time, unittest\n"
            "class Hangs(unittest.TestCase):\n"
            "    def test_hang(self):\n"
            "        time.sleep(30)\n",
            encoding="utf-8")
        env = dict(os.environ, MMM_TEST_TIMEOUT="1")
        try:
            p = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--root", td],
                capture_output=True, text=True, env=env, timeout=20,
                # 显式指定编码：text=True 默认用系统 locale（本机中文
                # Windows 是 GBK），而子进程按 PYTHONIOENCODING 写 UTF-8，
                # 解码线程会 UnicodeDecodeError，p.stderr 直接变 None。
                encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            print("SELFCHECK FAIL: 外层 20s 也超时，看门狗没生效")
            return 1
    marker = TIMEOUT_MARKER in p.stderr
    ok = p.returncode == TIMEOUT_RC and marker
    print("SELFCHECK %s: rc=%s marker=%s" %
          ("PASS" if ok else "FAIL", p.returncode, marker))
    if not ok:
        print((p.stdout + p.stderr)[-1200:])
    return 0 if ok else 1


def main(argv):
    if "--selfcheck" in argv:
        return selfcheck()

    root = "."
    if "--root" in argv:
        i = argv.index("--root")
        root = argv[i + 1]
    global _timeout
    _timeout = int(os.environ.get("MMM_TEST_TIMEOUT", str(DEFAULT_TIMEOUT)))
    if _timeout <= 0:
        print("MMM_TEST_TIMEOUT 必须是正整数", file=sys.stderr)
        return 2

    watchdog = _arm(_timeout)
    start_dir = os.path.join(root, "tests")
    suite = unittest.defaultTestLoader.discover(start_dir, pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    _disarm(watchdog)
    return 0 if result.wasSuccessful() else 1


_timeout = DEFAULT_TIMEOUT

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
