#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无头 UI 夹具的回归测试：钉住「不碰真机」与「不泄漏定时器」两件事。

这里不测业务，只测夹具本身有没有被绕过。每条断言都卡具体原因：
哪 7 个定时器被冻结、热键对象是不是 None、pending after 是不是清零、
8 个界面测试文件有没有逐个接上夹具、看门狗是不是还接在 run_tests.sh 上。

反假绿：`--selfcheck` / mutant 会逐条删掉这些保护，本文件必须相应变红。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ui_harness as harness  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(ROOT, "tests")

# 10 个会构造真实 App 的界面测试；新增第 11 个也必须接夹具，否则下面变红。
UI_TEST_FILES = (
    "test_dock_autohide_ui.py", "test_drag_target_window.py",
    "test_grid_ui.py", "test_keep_awake_ui.py", "test_rehome_ui.py",
    "test_snap_rows_mapped.py", "test_startup_window_lists.py",
    "test_top_bar_fit.py", "test_ui_hwnd_type.py", "test_ui_mode.py",
)

EXPECTED_TIMERS = {
    "_poll_taskbar", "_poll_display", "_poll_rehome_capture",
    "_start_command_poll", "_poll_rules", "_slideshow_restore", "<lambda>",
}


def expected_timers():
    """构造期应当被冻结的定时器集合。

    「恢复上次会话的全局快捷键」那条（<lambda>）**只在启用了快捷键时才
    登记**——ui.App.__init__ 里是 `if self.hk_enabled.get():`。设定里没开
    快捷键的机器上构造期只有 6 个定时器，硬编码 7 个会让这条测试在别人
    机器上无故变红（实测：本机 settings.json 没 hk_enabled）。
    """
    import ui
    names = set(EXPECTED_TIMERS)
    if not bool(ui.settings.load().get("hk_enabled", False)):
        names.discard("<lambda>")
    return names


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


class TestHarnessRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import ui
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.addClassCleanup(
            lambda: harness.close_app(cls.root, getattr(cls, "app", None)))
        cls.set_file = ui.settings._PATH
        cls.set_mtime_before = (os.stat(cls.set_file).st_mtime_ns
                                if os.path.exists(cls.set_file) else None)
        cls.app = harness.build_app(ui, cls.root)
        cls.set_mtime_after = (os.stat(cls.set_file).st_mtime_ns
                               if os.path.exists(cls.set_file) else None)
        cls.frozen = getattr(cls.app, harness.FROZEN_ATTR)

    def tearDown(self):
        # 每个用例单独兜底：即使 setUpClass 之后有人排了定时器也不外溢。
        harness.cancel_pending_after(self.root)

    def test_frozen_jobs_are_the_seven_startup_timers(self):
        names = {name for _ms, name in self.frozen}
        self.assertEqual(names, expected_timers(),
                         "构造期定时器集合变了：夹具可能漏冻结新的后台循环")
        self.assertTrue(all(ms > 0 for ms, _n in self.frozen),
                        "冻结的必须是延时定时器，不能把 after(0) 的即时回调也吞了")

    def test_no_real_hotkeys_or_display_poll_started(self):
        self.assertIsNone(self.app.hk,
                          "构造期注册了真机全局热键（_toggle_hk 被触发）")
        poll_id = getattr(self.app, "_display_poll_id", None)
        self.assertTrue(isinstance(poll_id, str)
                        and poll_id.startswith("harness-frozen#"),
                        "显示器轮询拿到了真实 Tcl after id：定时器没被冻结")

    def test_build_does_not_write_user_settings(self):
        self.assertEqual(
            self.set_mtime_after, self.set_mtime_before,
            "构造期 _toggle_display_watch 写了 settings.json：夹具没挡住 "
            "settings.save，跑一次全套测试就会改用户配置")

    def test_no_pending_after_after_build(self):
        jobs = list(self.root.tk.splitlist(self.root.tk.call("after", "info")))
        self.assertEqual(jobs, [],
                         "构造完 Tcl 解释器里还有 pending after，会跨用例刷屏")

    def test_after_is_restored_for_the_test_itself(self):
        # 夹具只在构造期冻结；用例自己要能正常排/取消定时器。
        job = self.root.after(10000, lambda: None)
        self.assertIsNotNone(job)
        self.root.after_cancel(job)
        left = list(self.root.tk.splitlist(self.root.tk.call("after", "info")))
        self.assertEqual(left, [], "after_cancel 没恢复或没生效")

    def test_close_app_cancels_pending_after(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.after(10000, lambda: None)
        root.after(10000, lambda: None)
        cancelled = harness.close_app(root)
        self.assertGreaterEqual(cancelled, 2,
                                "close_app 没取消待执行的 after，销毁后仍会刷屏")


class TestHarnessSourceContracts(unittest.TestCase):
    def test_every_ui_test_file_uses_the_harness(self):
        for name in UI_TEST_FILES:
            text = read(os.path.join("tests", name))
            self.assertIn("harness.build_app(", text,
                          "%s 没用无头构造，会注册真机热键" % name)
            self.assertIn("harness.close_app(", text,
                          "%s 没用带清理的销毁，会泄漏 after" % name)

    def test_no_raw_app_construction_left(self):
        # 拼字符串，避免本文件里的字面量被自己扫到。
        needle = "= ui." + "App("
        raw = []
        for name in sorted(os.listdir(TESTS_DIR)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            if name == os.path.basename(__file__):
                continue
            if needle in read(os.path.join("tests", name)):
                raw.append(name)
        self.assertEqual(raw, [],
                         "这些界面测试绕过了夹具直接构造 App：%s" % raw)

    def test_mapped_root_does_not_call_update(self):
        import re
        text = read(os.path.join("tests", "test_snap_rows_mapped.py"))
        # 只认代码行；文档注释里解释「绝不能调 update()」不算调用。
        code_hits = re.findall(r"^\s*cls\.root\.update\(\)\s*(?:#.*)?$",
                               text, flags=re.MULTILINE)
        # 「已映射 root 的 update() 永不返回」是 macOS Tk 的坑。Windows 的
        # 窗口管理器是异步的：geometry() 之后不 update()，子控件会停在旧
        # 布局上（实测画布宽度卡在 1px），越界/映射判定量的是错尺寸——
        # 那一次 update() 必须由平台守住，只放行 win32 分支里那一次。
        guarded = re.findall(
            r'^\s*if sys\.platform == "win32":\s*\n\s*cls\.root\.update\(\)',
            text, flags=re.MULTILINE)
        self.assertEqual(len(code_hits) - len(guarded), 0,
                         "已映射 root 调 update() 会永不返回（全量套件挂死的真因）；"
                         "Windows 分支那次必须写在 sys.platform == \"win32\" 里面")
        self.assertIn("cls.root.update_idletasks()", text,
                      "必须保留一次 update_idletasks 完成 packer 映射")

    def test_guard_runner_is_wired(self):
        sh = read("run_tests.sh")
        self.assertIn("run_tests_guarded.py", sh,
                      "run_tests.sh 没接看门狗，挂起又会退化成无限等")
        guard = read("run_tests_guarded.py")
        for needle in ("signal.alarm(", "faulthandler.dump_traceback()",
                       "def selfcheck("):
            self.assertIn(needle, guard, "看门狗缺了 %s" % needle)


if __name__ == "__main__":
    unittest.main()
