"""入口：启动多屏管理器（GUI + 系统托盘），跨 Windows / macOS。"""
import atexit
import logging
import os
import subprocess
import sys
import tempfile
import tkinter as tk

# 日志必须在 import backend 之前配置：backend 导入期（如 Quartz 框架加载结果）
# 会打印 INFO/WARNING，若此时 root logger 尚未设置，这些日志会丢失。
LOG_PATH = os.path.join(tempfile.gettempdir(), "multimon-manager.log")


def _setup_logging():
    """日志同时输出到终端和固定文件，便于在没有终端时回溯问题。"""
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


_setup_logging()

import backend
import resources
import ui


def _single_instance():
    """防止多开。返回 True 表示当前是唯一实例。"""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.GetLastError.restype = ctypes.c_ulong
        kernel32.CreateMutexW(None, False, "Local\\MultiMonManager")
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    # macOS：fcntl 文件锁
    import tempfile
    try:
        import fcntl
        global _lock_fd
        _lock_fd = open(os.path.join(tempfile.gettempdir(), "multimon_manager.lock"), "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, ImportError):
        return False


_lock_fd = None


def _set_dpi_aware():
    """在高 DPI 多屏环境下，让窗口坐标与显示器坐标处于同一物理像素空间。

    不设置时，系统会把本进程按 96 DPI 虚拟化，导致 GetWindowRect /
    EnumDisplayMonitors 的坐标错位，跨屏移动与分屏吸附出错。
    """
    if sys.platform != "win32":
        return
    import ctypes
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness.restype = ctypes.c_int
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _cleanup():
    """进程退出时释放已缓存的 COM 资源（仅在实际用过壁纸时才有意义）。"""
    if sys.platform == "win32":
        try:
            close = getattr(backend.wallpaper, "close", None)
            if close is not None:
                close()
            else:  # 兼容旧实现
                dw = backend.wallpaper.get_desktop_wallpaper()
                if hasattr(dw, "close"):
                    dw.close()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_cleanup)


def _activate_frontmost(root, delay_ms=400):
    """启动后把主窗口置顶显示。

    macOS 下从后台上下文（nohup / 脚本）启动时，Tk 窗口会被创建但不会自动
    前置——它被压在其它窗口后面，且进程不是 frontmost 应用，所以看起来像
    "没打开主界面"。这里延迟一拍后唤醒窗口，并按**自身 PID** 精确把本进程
    设为 frontmost（按进程名 "Python" 匹配会误激活其它 Python 进程）。
    """
    def _do():
        try:
            root.deiconify()
            root.lift()
            root.focus_force()
        except Exception:  # noqa: BLE001
            pass

        if not sys.platform.startswith("darwin"):
            return
        try:
            subprocess.run(
                ['osascript', '-e',
                 'tell application "System Events" to set frontmost of '
                 f'(first process whose unix id is {os.getpid()}) to true'],
                check=False,
                timeout=5,
            )
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).warning("启动时置顶失败: %s", e)

    root.after(delay_ms, _do)


def main():
    _set_dpi_aware()
    if not _single_instance():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "多屏管理器已在运行。", "多屏管理器", 0x40)
        except Exception:  # noqa: BLE001
            pass
        return

    root = tk.Tk()

    here = os.path.dirname(os.path.abspath(__file__))
    icon_path = resources.create_ico(os.path.join(here, "app.ico"))
    # macOS 上 iconbitmap 会导致 tkinter 窗口黑屏，仅 Windows 使用
    if icon_path and sys.platform.startswith("win"):
        try:
            root.iconbitmap(icon_path)
        except Exception:  # noqa: BLE001
            pass
    # macOS：Tk 默认沿用 Python 解释器图标，这里替换成程序自己的图标（Dock 显示）
    if sys.platform == "darwin":
        try:
            png = resources.create_png(os.path.join(here, "app.png"))
            if png:
                import dock_icon_mac
                ok = dock_icon_mac.set_dock_icon(png)
                logging.getLogger(__name__).info(
                    "Dock 图标%s: %s", "已设置" if ok else "设置失败", png)
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).warning("设置 Dock 图标失败: %s", e)

    app = ui.App(root)

    # 启动即显示主界面（否则后台启动的窗口会被压在其它窗口后面）
    _activate_frontmost(root)

    root.protocol("WM_DELETE_WINDOW", app.on_close)

    # 托盘运行在独立 Tk 子进程，避免与主窗口共用事件循环导致 macOS 黑屏
    os.environ["MAIN_PID"] = str(os.getpid())
    t = backend.tray.TrayIcon()
    t.create(
        icon_path,
        "多屏管理器",
        on_open=app.show,
        on_exit=app.quit,
        on_refresh=app.refresh_monitors,
    )
    # 托盘面板也是本应用的窗口，登记后窗口工具会跳过它，避免误操作自己
    if getattr(t, "proc", None) is not None:
        backend.windows.register_own_pid(t.proc.pid)

    try:
        root.mainloop()
    finally:
        t.destroy()


if __name__ == "__main__":
    main()
