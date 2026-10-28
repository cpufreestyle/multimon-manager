#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""防休眠的界面接线单测：控件必须映射、切模式必须真的套用、状态必须刷新。

防休眠会碰系统全局状态（pmset -a disablesleep），界面层最怕两件事：
1. **点了没反应**——控件没被 packer 排上，或命令没接到后端。
2. **静默改系统**——切到合盖档时不说清楚就弹授权框，用户被吓到；
   取消时也不该把模式静默改成「关闭」，要退回上一档。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    import ui  # noqa: E402
    import keep_awake  # noqa: E402
    import backend as b  # noqa: E402
    import settings  # noqa: E402
except Exception:  # noqa: BLE001  无 tkinter / 无显示环境：整类跳过
    tk = None
    ui = None
    keep_awake = None
    b = None
    settings = None

if tk is None:
    raise unittest.SkipTest("tkinter 不可用，跳过界面单测")


class _FakeBox:
    """替身 messagebox：记录调用，askyesno 可配置，不弹窗。"""

    def __init__(self, sink, answer=True):
        self._sink = sink
        self._answer = answer

    def showinfo(self, title, msg=None, **k):
        self._sink.append(("info", msg or title))

    def showwarning(self, title, msg=None, **k):
        self._sink.append(("warn", msg or title))

    def showerror(self, title, msg=None, **k):
        self._sink.append(("error", msg or title))

    def askyesno(self, title, msg=None, **k):
        self._sink.append(("ask", msg or title))
        return self._answer


class TestKeepAwakeUi(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ui.App(self.root)
        self.root.update()
        # 备份 settings，隔离用例之间的写入
        self._set_file = os.path.join(settings.data_dir(), "settings.json")
        try:
            with open(self._set_file, "rb") as f:
                self._set_had = f.read()
        except OSError:
            self._set_had = None
        settings._cache["stat"] = None
        settings._cache["data"] = None
        # 弹窗替身（不能真弹，会挂住整批测试）
        self._msg = []
        self._orig_mb = ui.messagebox
        ui.messagebox = _FakeBox(self._msg, answer=True)

    def tearDown(self):
        ui.messagebox = self._orig_mb
        try:
            if self._set_had is None:
                os.remove(self._set_file)
            else:
                with open(self._set_file, "wb") as f:
                    f.write(self._set_had)
        except OSError:
            pass
        settings._cache["stat"] = None
        settings._cache["data"] = None
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _packed(self, w):
        if w is None:
            return False
        if w.winfo_manager() != "pack":
            return False
        return w.winfo_reqwidth() > 1 and w.winfo_reqheight() > 1

    def _find_btn(self, text):
        found = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.cget("text") == text:
                        found.append(c)
                except Exception:  # noqa: BLE001
                    pass
                walk(c)
        walk(self.root)
        return found[0] if found else None

    # ---------- 控件映射 ----------

    def test_controls_packed(self):
        """模式下拉与两个按钮都要被 packer 排上（否则点了没反应）。"""
        self.assertTrue(self._packed(self.app.keep_awake_cb),
                        "模式下拉没被排上")
        for text in ("立即生效", "全部释放"):
            btn = self._find_btn(text)
            self.assertIsNotNone(btn, "找不到按钮「%s」" % text)
            self.assertTrue(self._packed(btn), "按钮「%s」没被排上" % text)

    def test_combo_has_all_modes(self):
        """四档都要在下拉里，顺序与 keep_awake.MODES 一致。"""
        vals = list(self.app.keep_awake_cb["values"])
        self.assertEqual(vals, [keep_awake.MODE_LABELS[m]
                                for m in keep_awake.MODES])

    def test_mode_from_label_roundtrip(self):
        for m in keep_awake.MODES:
            self.assertEqual(
                self.app._keep_awake_mode_from_label(keep_awake.MODE_LABELS[m]), m)
        # 乱值退回默认档，不能崩
        self.assertEqual(self.app._keep_awake_mode_from_label("不存在"),
                         keep_awake.DEFAULT_MODE)

    # ---------- 切模式必须真的套用 ----------

    def test_apply_calls_backend(self):
        """切模式必须真的调后端套用，并把结果写进状态。"""
        seen = {}
        self._orig = b.keep_awake_apply

        def _fake(mode, allow_prompt=False):
            seen["mode"] = mode
            seen["prompt"] = allow_prompt
            return True, "已生效（测试）", {"held": [], "sleep_disabled": False}

        b.keep_awake_apply = _fake
        try:
            self.app.keep_awake_var.set(keep_awake.MODE_LABELS[keep_awake.MODE_SYSTEM])
            self.app._on_keep_awake_change()
        finally:
            b.keep_awake_apply = self._orig
        self.assertEqual(seen.get("mode"), keep_awake.MODE_SYSTEM)
        self.assertEqual(self.app._keep_awake_status.get(), "已生效（测试）")
        # 要持久化：下次启动才恢复得了
        self.assertEqual(settings.load().get("keep_awake_mode"), keep_awake.MODE_SYSTEM)

    def test_clamshell_asks_first(self):
        """合盖档必须先问清楚（会改系统级设置），不能直接授权。"""
        seen = {}
        self._orig = b.keep_awake_apply

        def _fake(mode, allow_prompt=False):
            seen["mode"] = mode
            return True, "ok", {"held": [], "sleep_disabled": True}

        b.keep_awake_apply = _fake
        try:
            self.app.keep_awake_var.set(
                keep_awake.MODE_LABELS[keep_awake.MODE_CLAMSHELL])
            self.app._on_keep_awake_change()
        finally:
            b.keep_awake_apply = self._orig
        asks = [m for kind, m in self._msg if kind == "ask"]
        self.assertTrue(asks, "合盖档没先询问就套用了")
        self.assertIn("disablesleep", asks[0])
        self.assertEqual(seen.get("mode"), keep_awake.MODE_CLAMSHELL)

    def test_clamshell_cancel_restores_previous(self):
        """用户取消授权：退回上一档，不静默改成「关闭」。"""
        ui.messagebox = _FakeBox(self._msg, answer=False)
        self._orig = b.keep_awake_apply
        b.keep_awake_apply = lambda mode, allow_prompt=False:             (_ for _ in ()).throw(AssertionError("取消后不该套用"))
        try:
            # 先设一个非 off 的当前档
            settings.save(dict(settings.load(), keep_awake_mode=keep_awake.MODE_SYSTEM))
            settings._cache["stat"] = None
            settings._cache["data"] = None
            self.app.keep_awake_var.set(
                keep_awake.MODE_LABELS[keep_awake.MODE_CLAMSHELL])
            self.app._on_keep_awake_change()
        finally:
            b.keep_awake_apply = self._orig
        self.assertEqual(self.app.keep_awake_var.get(),
                         keep_awake.MODE_LABELS[keep_awake.MODE_SYSTEM],
                         "取消后模式被静默改掉了")

    def test_release_button_resets(self):
        """「全部释放」要调后端、把模式设回 off 并给反馈。"""
        seen = {}
        self._orig = b.keep_awake_release
        b.keep_awake_release = lambda: seen.setdefault("called", True)
        try:
            self.app._keep_awake_release()
        finally:
            b.keep_awake_release = self._orig
        self.assertTrue(seen.get("called"), "「全部释放」没调后端")
        self.assertEqual(self.app.keep_awake_var.get(),
                         keep_awake.MODE_LABELS[keep_awake.MODE_OFF])
        self.assertEqual(settings.load().get("keep_awake_mode"), keep_awake.MODE_OFF)
        self.assertIn("已全部释放", self.app.status_var.get())

    def test_status_detail_shows_state(self):
        """状态行要说出持没持断言、盖子情况。"""
        self.app._refresh_keep_awake_status()
        detail = self.app._keep_awake_detail.get()
        self.assertTrue(detail, "状态行是空的")
        self.assertIn("断言", detail)

    def test_no_lid_machine_says_so(self):
        """台式机（无盖子）状态行要明说，不显示「合盖中」。"""
        self._orig = keep_awake.has_lid
        keep_awake.has_lid = lambda: False
        try:
            self.app._refresh_keep_awake_status()
        finally:
            keep_awake.has_lid = self._orig
        self.assertIn("无盖子", self.app._keep_awake_detail.get())


if __name__ == "__main__":
    unittest.main()
