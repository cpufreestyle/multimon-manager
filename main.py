"""入口：启动多屏管理器（GUI + 系统托盘），跨 Windows / macOS。"""
import atexit
import ctypes
import os
import sys
import tkinter as tk

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

    不设置时，系统会把本进程按 96 DPI 虚拟化，导致 GetWindowRect/
    EnumDisplayMonitors 的坐标错位，跨屏移动与分屏吸附出错。
    """
    if sys.platform != "win32":
        return
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness.restype = ctypes.c_int
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _cleanup():
    """进程退出时释放已缓存的 COM 资源（仅在实际用过壁纸时才有意义）。"""
    if sys.platform == "win32":
        try:
            backend.wallpaper.close()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_cleanup)


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
    if icon_path:
        try:
            root.iconbitmap(icon_path)
        except Exception:  # noqa: BLE001
            pass

    app = ui.App(root)
    app.tray = backend.tray.TrayIcon()

    app.tray.create(
        icon_path,
        "多屏管理器",
        on_open=app.show,
        on_exit=app.on_exit,
        on_refresh=app.refresh_monitors,
    )

    root.protocol("WM_DELETE_WINDOW", app.on_close)

    try:
        root.mainloop()
    finally:
        try:
            app.tray.destroy()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
