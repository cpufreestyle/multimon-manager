"""系统托盘 / 菜单栏（macOS 实现，零第三方依赖）。

macOS 没有 Python 标准库级别的菜单栏图标 API，且单 Tk 进程内创建
第二个 Toplevel 窗口在 macOS 上极易触发主窗口黑屏的渲染 bug。

因此本实现改为：**托盘运行在完全独立的 Tk 子进程**中，仅负责一个
常驻右上角的轻量"快速访问面板"（等效 Windows 托盘菜单：打开主界面 / 退出）。
主进程通过进程管理与之解耦，互不干扰事件循环。
"""
import os
import subprocess
import sys


class TrayIcon:
    def __init__(self):
        self.proc = None
        self.tip = "多屏管理器"

    def create(self, icon_path, tip="多屏管理器", on_open=None, on_exit=None,
               on_refresh=None):
        """启动独立托盘子进程。

        on_open / on_exit 是主进程回调，这里仅用于提示，真正动作由主进程
        通过退出码 / 信号等方式感知（为简单起见，面板按钮通过 AppleScript
        激活主窗口，退出按钮直接终止主进程）。

        on_refresh 仅为兼容 Windows 版托盘的统一调用签名：macOS 托盘是独立的
        子进程面板，暂不支持"刷新显示器"回调，接收后忽略（不传会 TypeError）。
        """
        self.tip = tip
        here = os.path.dirname(os.path.abspath(__file__))
        tray_script = os.path.join(here, "_tray_panel.py")
        if not os.path.exists(tray_script):
            return
        try:
            self.proc = subprocess.Popen(
                [sys.executable, tray_script, tip],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            self.proc = None

    def show_panel(self):
        # 面板常驻，无需额外操作
        pass

    def destroy(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.proc = None


if __name__ == "__main__":
    t = TrayIcon()
    t.create(None, "测试")
