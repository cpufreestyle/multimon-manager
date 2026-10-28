#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2×2 一键模式单测：force_auto / 托盘命令路由 / 快捷键说明 / VK_Q 常量。

「尽量方便用 2×2」落成三个入口：⌘⌥+Q 全局热键、托盘「2×2 排最近四窗」
按钮、主界面「自动四窗」按钮。三者是同一动作——_snap_four_quad(
force_auto=True)：先把四个下拉复位成「自动」，再走与「2×2 排列」完全
相同的排布/预演链路（选中窗口可能已关闭，一键要的是现在最靠前的四个）。

这里锁住：① 一键真的复位四个下拉；② 复位后预演/排列收到全 None
（= 后端取最前面四个，不是 UI 里残留的旧选择）；③ 不选一键时用户选择
原样保留；④ 智能勾选在一键模式下依然生效；⑤ 托盘 snap_quad 命令路由
到 force_auto；⑥ 快捷键说明里有 Q；⑦ VK_Q 常量与平台键码一致。
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend as b  # noqa: E402

try:  # ui 依赖 tkinter；无显示环境下降级跳过界面层测试
    import ui  # noqa: E402
except Exception:  # noqa: BLE001
    ui = None

H = {"tl": "11::访达", "tr": "22::iPhone镜像", "bl": "33::系统设置", "br": "44::OneNote"}
LABELS = {"tl": "访达", "tr": "iPhone镜像", "bl": "系统设置", "br": "OneNote"}


class _Var:
    def __init__(self, v=""):
        self.v = v

    def get(self):
        return self.v

    def set(self, x):
        self.v = x


class _Flag:
    def __init__(self, v=False):
        self.v = v

    def get(self):
        return self.v


def _fake_self(smart=False):
    """最小假 self：只备齐 _snap_four_quad 这条链路用到的属性。"""
    bm = types.SimpleNamespace(
        _last_quad_splits=(0.5, 0.5),
        _last_quad_slots=[(0, H["tl"]), (1, H["tr"]),
                          (2, H["bl"]), (3, H["br"])],
        _last_quad_unwritable=[], _last_quad_report=[], _last_quad_notes=[])
    s = types.SimpleNamespace()
    s.topmost = _Flag(False)
    s.root = types.SimpleNamespace(attributes=lambda *a, **k: None)
    s.quad_tl_var = _Var(LABELS["tl"])
    s.quad_tr_var = _Var(LABELS["tr"])
    s.quad_bl_var = _Var(LABELS["bl"])
    s.quad_br_var = _Var(LABELS["br"])
    s._quad_short_map = {ui.AUTO_TARGET: None,
                         LABELS["tl"]: H["tl"], LABELS["tr"]: H["tr"],
                         LABELS["bl"]: H["bl"], LABELS["br"]: H["br"]}
    s._monitor_map = {"主屏": 0}
    s.quad_mon_var = _Var("主屏")
    s.quad_smart_var = _Flag(smart)
    s._quad_split_x = _Var(0.5)
    s._quad_split_y = _Var(0.5)
    s.status_var = _Var("")
    s._quad_hwnd_short = {H[k]: LABELS[k] for k in H}
    s._refresh_quad_grid = lambda: None
    s._update_quad_capacity = lambda: None
    s._quad_backend_mod = lambda: bm
    return s


@unittest.skipIf(ui is None, "无 tkinter，跳过界面层测试")
class QuadQuickAccessTest(unittest.TestCase):
    def setUp(self):
        self.calls = {"predict": [], "snap": [], "ask": []}
        self._orig = (b.quad_predict, b.snap_four_quad,
                      ui.messagebox.askyesno, ui.cmd_channel.take)

        def fake_predict(**kw):
            self.calls["predict"].append(kw)
            return ([], {})

        def fake_snap(**kw):
            self.calls["snap"].append(kw)
            return True

        def fake_ask(title, message=None, **kw):
            self.calls["ask"].append((title, message))
            return True

        b.quad_predict = fake_predict
        b.snap_four_quad = fake_snap
        ui.messagebox.askyesno = fake_ask
        ui.cmd_channel.take = lambda: None

    def tearDown(self):
        (b.quad_predict, b.snap_four_quad,
         ui.messagebox.askyesno, ui.cmd_channel.take) = self._orig

    def test_force_auto_resets_four_dropdowns(self):
        """一键模式：调用一刻四个下拉已复位「自动」，预演/排列收到全 None。

        排列成功后的写回不在此断言范围内：成功后下拉显示的是后端回报的
        实际归属（_last_quad_slots），「界面显示 ≠ 实际排的」正是要修掉的
        不准——所以事后下拉不再是「自动」是设计使然，不是复位失效。
        """
        s = _fake_self()
        seen = {}

        def snap_spy(**kw):
            seen["kw"] = kw
            seen["vals"] = [getattr(s, "quad_%s_var" % k).get()
                            for k in ("tl", "tr", "bl", "br")]
            return True

        orig_snap = b.snap_four_quad
        b.snap_four_quad = snap_spy
        try:
            ui.App._snap_four_quad(s, force_auto=True)
        finally:
            b.snap_four_quad = orig_snap
        self.assertEqual(seen["vals"], [ui.AUTO_TARGET] * 4)
        kw = self.calls["predict"][0]
        self.assertEqual([kw[k] for k in
                          ("tl_hwnd", "tr_hwnd", "bl_hwnd", "br_hwnd")],
                         [None] * 4)
        self.assertIsNone(seen["kw"]["tl_hwnd"])
        self.assertIsNone(seen["kw"]["br_hwnd"])

    def test_default_keeps_user_selection(self):
        """不按一键：用户选的四个窗口原样传给预演/排列。"""
        s = _fake_self()
        ui.App._snap_four_quad(s)
        self.assertEqual(s.quad_tl_var.get(), LABELS["tl"])
        kw = self.calls["predict"][0]
        self.assertEqual(kw["tl_hwnd"], H["tl"])
        self.assertEqual(kw["br_hwnd"], H["br"])
        self.assertEqual(kw["smart"], False)

    def test_smart_flag_survives_auto_mode(self):
        """智能勾选在一键模式下依然生效（两个入口都拿到 smart=True）。"""
        s = _fake_self(smart=True)
        ui.App._snap_four_quad(s, force_auto=True)
        self.assertTrue(self.calls["predict"][0]["smart"])
        self.assertTrue(self.calls["snap"][0]["smart"])

    def test_tray_command_routes_to_force_auto(self):
        """托盘 snap_quad 命令 → _snap_four_quad(force_auto=True)。"""
        s = _fake_self()
        seen = {}
        s._snap_four_quad = lambda force_auto=False: seen.update(
            force_auto=force_auto)
        ui.cmd_channel.take = lambda: "snap_quad"
        ui.App._poll_command(s)
        self.assertIs(seen.get("force_auto"), True)

    def test_help_text_lists_quick_quad(self):
        """快捷键说明里能查到一键 2×2（用户 discovery 的唯一入口）。"""
        s = types.SimpleNamespace(
            _mod_symbol={"⌘⌥ (Cmd+Option)": "⌘⌥"})
        text = ui.App._hk_help_text(s, "⌘⌥ (Cmd+Option)")
        self.assertIn("+Q", text)
        self.assertIn("2×2", text)


class VkQConstTest(unittest.TestCase):
    """VK_Q 必须与各平台键码一致，否则热键注册了也永远匹配不上。"""

    def test_vk_q_nonzero(self):
        self.assertTrue(getattr(b, "VK_Q", 0))

    def test_vk_q_matches_platform_keycode(self):
        if sys.platform.startswith("darwin"):
            self.assertEqual(b.VK_Q, 12)      # kVK_ANSI_Q（CGKeycode）
        else:
            self.assertEqual(b.VK_Q, ord("q"))  # Windows 虚拟键码


if __name__ == "__main__":
    unittest.main()
