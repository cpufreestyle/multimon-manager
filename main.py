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
import cmd_channel
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
    # 独立打包（PyInstaller 冻结）后，托盘面板以「同一可执行文件」启动，用环境变量
    # MMM_TRAY=1 区分（比命令行参数可靠，避免 bootloader 吞参导致无限派生）。
    # 必须在单实例检查之前处理，否则会被误判为重复启动而直接退出。
    if os.environ.get("MMM_TRAY") == "1":
        import _tray_panel
        _tray_panel.main(os.environ.get("MMM_TRAY_TIP"))
        return

    logging.getLogger(__name__).info("启动: frozen=%s argv=%s",
                                     getattr(sys, "frozen", False), sys.argv[1:])
    # 清掉上次运行可能残留的托盘命令，避免新实例启动即执行旧的 open/exit
    cmd_channel.take()
    # --minimized：开机自启静默模式（启动后不显示主窗口，仅托盘）
    start_minimized = "--minimized" in sys.argv
    _set_dpi_aware()
    if not _single_instance():
        logging.getLogger(__name__).info("已有实例在运行，退出")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "多屏管理器已在运行。", "多屏管理器", 0x40)
        except Exception:  # noqa: BLE001
            pass
        return
    logging.getLogger(__name__).info("单实例锁已获取，准备创建窗口…")

    root = tk.Tk()
    logging.getLogger(__name__).info("Tk 窗口已创建")

    # 独立打包(.app)时，Dock 图标由 bundle 的 app.icns / Info.plist 提供：
    # 既不需要运行时设置，也避免往包内写文件（包可能已签名或只读）。
    icon_path = None
    if not getattr(sys, "frozen", False):
        here = os.path.dirname(os.path.abspath(__file__))
        icon_path = resources.create_ico(os.path.join(here, "app.ico"))
        # macOS 上 iconbitmap 会导致 tkinter 窗口黑屏，仅 Windows 使用
        if icon_path and sys.platform.startswith("win"):
            try:
                root.iconbitmap(icon_path)
            except Exception:  # noqa: BLE001
                pass
        # macOS：Tk 默认沿用 Python 解释器图标，替换成程序自己的图标（Dock 显示）
        if sys.platform == "darwin":
            try:
                png = resources.ensure_transparent_icon(
                    os.path.join(here, "app.png"))
                if png:
                    import dock_icon_mac
                    ok = dock_icon_mac.set_dock_icon(png)
                    logging.getLogger(__name__).info(
                        "Dock 图标%s: %s", "已设置" if ok else "设置失败", png)
            except Exception as e:  # noqa: BLE001
                logging.getLogger(__name__).warning("设置 Dock 图标失败: %s", e)
    logging.getLogger(__name__).info("图标处理完成，开始构建界面…")

    app = ui.App(root)
    logging.getLogger(__name__).info("界面构建完成")

    if start_minimized:
        # 静默启动：隐藏主窗口，只留托盘（双击托盘图标再打开）
        try:
            root.withdraw()
        except Exception:  # noqa: BLE001
            pass
    else:
        # 启动即显示主界面（否则后台启动的窗口会被压在其它窗口后面）
        _activate_frontmost(root)

    # macOS 首次运行需辅助功能授权；未授权时主动引导授权。
    # 注意：系统授权框（AXIsProcessTrustedWithOptions(prompt)）在很多场景下不会弹
    # （非用户点击触发 / 曾被拒绝 / 打包解释器），故额外直接打开设置页 + 界面提示，
    # 确保用户一定被引导到要开启的开关（最可靠）。
    if sys.platform == "darwin" and not start_minimized:
        def _maybe_prompt_accessibility():
            try:
                if not backend.is_accessibility_trusted():
                    backend.request_accessibility()          # 尽力弹系统框（常无效，静默忽略）
                    backend.open_accessibility_settings()     # 直接跳到辅助功能设置页（必现引导）
                    app.show_accessibility_hint()
            except Exception:  # noqa: BLE001
                pass
        root.after(800, _maybe_prompt_accessibility)

    root.protocol("WM_DELETE_WINDOW", app.on_close)

    # 托盘运行在独立 Tk 子进程，避免与主窗口共用事件循环导致 macOS 黑屏
    os.environ["MAIN_PID"] = str(os.getpid())
    t = backend.tray.TrayIcon()
    # 托盘动作统一走命令通道：Windows 托盘回调在托盘消息线程里执行，直接调
    # Tk 方法跨线程不安全；macOS 托盘是独立子进程，同样依赖该通道。命令由
    # 主界面 _poll_command 在 Tk 主线程消费（open/refresh/apply_layout/exit）。
    t.create(
        icon_path,
        "多屏管理器",
        on_open=lambda: cmd_channel.send("open"),
        on_exit=lambda: cmd_channel.send("exit"),
        on_refresh=lambda: cmd_channel.send("refresh"),
        on_apply_layout=lambda: cmd_channel.send("apply_layout"),
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
