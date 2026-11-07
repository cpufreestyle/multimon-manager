# -*- coding: utf-8 -*-
"""启动就要把窗口下拉填满。

回归的是一个把两个症状绑在一起的漏调用：`ui.App.__init__` 曾经在
`_build()` 之后调 `_refresh_targets()`，`9a7b118`（极简/高级双模式）重写
`__init__` 时把它弄丢了。而 `self._target_map` / `self._quad_short_map` 只在
`_refresh_targets()` 里创建，于是：

  ① 目标窗口 / 并排 / 竖排 / 四格 各下拉的 values 全是空列表 —— 点开看不到
     任何程序可选；
  ② `self._target_map` 不存在，`_on_target_selected`、并排左右、竖排上中下
     一执行就 AttributeError，被 Tk 吞掉后表现为「排列按钮点了没反应」。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend as b  # noqa: E402
import settings  # noqa: E402

try:  # ui 依赖 tkinter；无显示环境下降级跳过
    import tkinter as tk  # noqa: E402
    import ui  # noqa: E402
except Exception:  # noqa: BLE001
    tk = None

os.environ.setdefault("MMM_ADVANCED_UI", "1")

FAKE_WINDOWS = [
    {"pid": 1, "owner": "AppA", "name": "WinA", "wid": 11,
     "x": 0, "y": 40, "w": 800, "h": 600,
     "hwnd": "AppA::WinA", "seq": None, "label": "AppA — WinA  (800×600)"},
    {"pid": 2, "owner": "AppB", "name": "WinB", "wid": 12,
     "x": 820, "y": 40, "w": 800, "h": 600,
     "hwnd": "AppB::WinB", "seq": None, "label": "AppB — WinB  (800×600)"},
    {"pid": 3, "owner": "AppC", "name": "WinC", "wid": 13,
     "x": 0, "y": 700, "w": 800, "h": 600,
     "hwnd": "AppC::WinC", "seq": None, "label": "AppC — WinC  (800×600)"},
]


@unittest.skipIf(tk is None, "本机没有可用的 tkinter")
class TestStartupWindowLists(unittest.TestCase):

    def setUp(self):
        self._set_file = os.path.join(settings.data_dir(), "settings.json")
        try:
            with open(self._set_file, "rb") as f:
                self._had = f.read()
        except OSError:
            self._had = None
        settings._cache["stat"] = None
        settings._cache["data"] = None

        self.root = tk.Tk()
        self.root.withdraw()
        with mock.patch.object(b, "list_target_windows",
                               return_value=list(FAKE_WINDOWS)):
            self.app = ui.App(self.root)

    def tearDown(self):
        self.root.destroy()
        if self._had is not None:
            with open(self._set_file, "wb") as f:
                f.write(self._had)
        else:
            try:
                os.remove(self._set_file)
            except OSError:
                pass
        settings._cache["stat"] = None
        settings._cache["data"] = None

    def test_target_map_built_at_startup(self):
        """_target_map 在构造期间就该存在，否则并排/竖排按钮直接 AttributeError。"""
        m = getattr(self.app, "_target_map", None)
        self.assertIsInstance(m, dict)
        self.assertIn(ui.AUTO_TARGET, m)
        for w in FAKE_WINDOWS:
            self.assertEqual(m.get(w["label"]), w["hwnd"])

    def test_window_comboboxes_have_values_at_startup(self):
        """每个窗口选择下拉启动后都必须有可选项，不能是空列表。"""
        # 目标窗口 / 并排 / 竖排 用完整标签；四宫格四角用压缩后的短标签
        full = [w["label"] for w in FAKE_WINDOWS]
        short = ["AppA", "AppB", "AppC"]
        cases = [
            ("target_cb", full),
            ("sbs_left_cb", full), ("sbs_right_cb", full),
            ("stack_top_cb", full), ("stack_mid_cb", full),
            ("stack_bot_cb", full),
            ("quad_tl_cb", short), ("quad_tr_cb", short),
            ("quad_bl_cb", short), ("quad_br_cb", short),
        ]
        for name, expect in cases:
            cb = getattr(self.app, name)
            values = list(cb["values"])
            self.assertGreater(len(values), 1,
                               f"{name} 启动后只有「自动」，没有窗口可选")
            for label in expect:
                self.assertIn(label, values)

    def test_short_map_matches_labels(self):
        """短标签 -> hwnd 的映射要和窗口一一对应，四宫格据此换回窗口。"""
        sm = getattr(self.app, "_quad_short_map", {})
        self.assertIn(ui.AUTO_TARGET, sm)
        self.assertEqual(len(sm), 1 + len(FAKE_WINDOWS))
        for short, w in zip(("AppA", "AppB", "AppC"), FAKE_WINDOWS):
            self.assertEqual(sm.get(short), w["hwnd"])

    def test_snap_buttons_do_not_raise_on_missing_map(self):
        """回归 ②：_target_map 缺失时这几个处理器会 AttributeError。"""
        for name in ("_on_target_selected", "_snap_two_side_by_side",
                     "_snap_three_stack"):
            self.assertTrue(hasattr(self.app, name))
        # 真调一次「选同一个窗口」的守卫路径：它只碰 _target_map，不动真窗口
        self.app.sbs_left_var.set(FAKE_WINDOWS[0]["label"])
        self.app.sbs_right_var.set(FAKE_WINDOWS[0]["label"])
        with mock.patch.object(ui.messagebox, "showwarning") as warn:
            self.app._snap_two_side_by_side()
        warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
