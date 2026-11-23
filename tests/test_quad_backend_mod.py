#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面读后端状态的模块探测：_quad_backend_mod 必须按平台落到干活的那个。

原来的写法是 try import windows_mac，失败才 import windows。问题是
windows_mac 在 Windows 上**也能导入**（CoreGraphics 找不到时会退化成
AppleScript），于是界面读的 _last_quad_slots / _last_quad_report /
_last_quad_splits、调用的 quad_capacity / quad_predict 全是 macOS 侧那一
份——Windows 上刚排完四宫格，状态栏的「被应用最小尺寸钳制」「写入无效」
明细整个消失，容量预检那一行也永远是空的。

判据就一条：它返回的模块必须和 backend 实际 import 的窗口模块是同一个。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend as be  # noqa: E402

try:
    import ui  # noqa: E402
except Exception:  # noqa: BLE001  无 tkinter / 无显示环境：跳过
    ui = None


@unittest.skipIf(ui is None, "无 tkinter，跳过界面层测试")
class QuadBackendModTest(unittest.TestCase):
    def test_returns_the_module_backend_uses(self):
        mod = ui.App._quad_backend_mod()
        self.assertIs(mod, be.windows)

    def test_mac_side_returns_windows_mac(self):
        """macOS 上必须返回 windows_mac（同一个判据，平台无关）。"""
        if sys.platform != "darwin":
            self.skipTest("仅 macOS 需要验这一侧")
        import windows_mac
        self.assertIs(ui.App._quad_backend_mod(), windows_mac)


if __name__ == "__main__":
    unittest.main()
