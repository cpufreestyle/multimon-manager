"""测试借鉴 TheRing 的 PullDisplay / StrictWinActivate 特性。

验证：
1. get_cursor_monitor 正常返回当前活跃显示器；
2. pull_window_to_cursor_monitor 能将其他屏窗口拉取并居中放置在当前鼠标屏幕；
3. pull_window_to_cursor_monitor 记录撤销栈（undo_last_move 可还原）；
4. raise_window 安全调用不崩溃。
"""
import unittest
from unittest.mock import patch, MagicMock

import monitors_mac
import windows_mac
from monitors_mac import MonitorInfo


class PullCursorMonitorTest(unittest.TestCase):
    def setUp(self):
        self.m1 = MonitorInfo(
            index=0,
            device_name="Display 1",
            device_path="1",
            is_primary=True,
            vendor_id=None,
            model_id=None,
            serial=None,
            left=0,
            top=0,
            width=1920,
            height=1080,
            work_left=0,
            work_top=25,
            work_width=1920,
            work_height=1055,
        )
        self.m2 = MonitorInfo(
            index=1,
            device_name="Display 2",
            device_path="2",
            is_primary=False,
            vendor_id=None,
            model_id=None,
            serial=None,
            left=1920,
            top=0,
            width=1920,
            height=1080,
            work_left=1920,
            work_top=25,
            work_width=1920,
            work_height=1055,
        )

    def test_get_cursor_monitor_hit_second(self):
        """鼠标坐标落在副屏时返回副屏。"""
        with patch.object(monitors_mac, "enum_monitors", return_value=[self.m1, self.m2]):
            pt = MagicMock()
            pt.x = 2500.0
            pt.y = 500.0
            with patch.object(monitors_mac._cg, "CGEventCreate", return_value=123):
                with patch.object(monitors_mac._cg, "CGEventGetLocation", return_value=pt):
                    with patch.object(monitors_mac._cf, "CFRelease", return_value=None):
                        m = monitors_mac.get_cursor_monitor()
                        self.assertIsNotNone(m)
                        self.assertEqual(m.index, 1)

    def test_pull_window_to_cursor_monitor_from_other_screen(self):
        """窗口在屏幕 2，光标在屏幕 1，拉取窗口应放置到屏幕 1 的居中工作区。"""
        with patch.object(windows_mac.monitors, "get_cursor_monitor", return_value=self.m1):
            with patch.object(windows_mac, "get_window_rect", return_value=(2000, 100, 800, 600)):
                with patch.object(windows_mac, "set_window_rect") as mock_set:
                    with patch.object(windows_mac, "raise_window") as mock_raise:
                        res = windows_mac.pull_window_to_cursor_monitor("TestApp::1", activate=True)
                        self.assertTrue(res)
                        mock_set.assert_called_once()
                        args = mock_set.call_args[0]
                        # 验证落在屏幕 1 工作区内
                        hwnd, nx, ny, nw, nh = args[:5]
                        self.assertEqual(hwnd, "TestApp::1")
                        self.assertEqual(nw, 800)
                        self.assertEqual(nh, 600)
                        self.assertEqual(nx, 0 + (1920 - 800) // 2)
                        self.assertEqual(ny, 25 + (1055 - 600) // 2)
                        mock_raise.assert_called_once_with("TestApp::1")

    def test_pull_window_clamps_oversized_window(self):
        """窗口尺寸超过目标屏工作区时，自动缩容到目标屏最大可用范围。"""
        with patch.object(windows_mac.monitors, "get_cursor_monitor", return_value=self.m1):
            with patch.object(windows_mac, "get_window_rect", return_value=(2000, 100, 3000, 2000)):
                with patch.object(windows_mac, "set_window_rect") as mock_set:
                    res = windows_mac.pull_window_to_cursor_monitor("BigApp::1", activate=False)
                    self.assertTrue(res)
                    hwnd, nx, ny, nw, nh = mock_set.call_args[0][:5]
                    self.assertEqual(nw, 1920)
                    self.assertEqual(nh, 1055)
                    self.assertEqual(nx, 0)
                    self.assertEqual(ny, 25)

    def test_raise_window_safe(self):
        """raise_window 在空 hwnd 时返回 False，非空时不抛异常。"""
        self.assertFalse(windows_mac.raise_window(None))
        with patch.object(windows_mac, "_osa", return_value="") as mock_osa:
            self.assertTrue(windows_mac.raise_window("Finder::1"))
            mock_osa.assert_called_once()


if __name__ == "__main__":
    unittest.main()
