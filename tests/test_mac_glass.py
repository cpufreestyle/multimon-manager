#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 毛玻璃增强的降级行为。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    import mac_glass
except Exception:  # noqa: BLE001
    tk = None
    mac_glass = None


@unittest.skipIf(tk is None or mac_glass is None, "无 tkinter")
class MacGlassTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.title("MMM_GLASS_TEST")

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def test_disabled_never_touches_window(self):
        self.assertFalse(mac_glass.apply(self.root, enabled=False))
        self.assertFalse(hasattr(self.root, "_mmm_glass_view"))

    def test_non_mac_returns_false(self):
        old_platform = mac_glass.IS_MAC
        mac_glass.IS_MAC = False
        try:
            self.assertFalse(mac_glass.apply(self.root, enabled=True))
        finally:
            mac_glass.IS_MAC = old_platform


if __name__ == "__main__":
    unittest.main()
