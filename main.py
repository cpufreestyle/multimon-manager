"""入口：启动多屏管理器（GUI + 系统托盘），跨 Windows / macOS。"""
import atexit
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import tkinter as tk


# ── 已修复：主进程随机 abort（真根因是拖拽吸附的事件 tap）─────────────
#
# 现象：进程启动 10-16 秒后静默死亡，系统崩溃报告 SIGABRT / SIGSEGV，
#   Fatal Python error: PyEval_RestoreThread: the GIL is released
#   (the current Python thread state is NULL)
# 用户可见后果：托盘按钮除「打开主界面」外全部失效（命令无人消费）。
#
# 真根因（二分定位，勿再怀疑别处）：DragSnapWatcher 把 CGEventTap 的
# runloop source 注册到 **Tk 主线程** CFRunLoop（CFRunLoopGetMain），
# 由 Tk mainloop 驱动。mouse dragged 事件极频繁，每次都在 Tcl 释放 GIL 的
# 窗口期回调进 Python -> PyEval_RestoreThread 拿到 NULL tstate -> abort。
# 判据：只禁掉拖拽吸附，同一份代码从 rc=134（必崩）变 rc=124（存活）。
#
# 修法：tap 改建在**专用线程**自己的 CFRunLoop 上（与 hotkeys_mac 同一
# 套路），回调只读坐标/入队、不碰 tkinter；stop() 用 CFRunLoopStop 唤醒。
# 字段访问加锁——stop() 在调用方线程置 None 而 _handler 在 tap 线程读，
# 不加锁踩出过 SIGSEGV。
#
# 排除过的错误方向（都做过实验，勿重复）：Python/Tcl 版本（3.9/3.13/3.14
# 与 Tcl 8.5/9.1 都崩过也都活过）、启动方式（shell / nohup / open .app /
# pty.fork 无差别）、ttk.Notebook、全局热键 tap、辅助功能提示。
# 教训：这个崩溃是概率性的，小样本（2-3 次）结论全部不可信，必须多次重复。
#
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
import settings
import resources
import ui
import mac_glass


_LOCK_PATH = os.path.join(tempfile.gettempdir(), "multimon_manager.lock")


def _lock_holder_pid():
    """找出锁的持有者 PID；读不出返回 None。

    优先读锁文件里记的 PID（新版本启动时写的）；旧版本创建的空锁文件
    没有 PID，退回用 lsof 反查——macOS 自带，不引第三方依赖。
    """
    try:
        with open(_LOCK_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip() or 0) or None
        if pid:
            return pid
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(["lsof", "-t", _LOCK_PATH], capture_output=True,
                             text=True, timeout=10).stdout
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:  # noqa: BLE001
        return None


def _acquire_lock():
    """尝试拿单例锁；拿到返回文件对象，否则 None。"""
    import fcntl
    try:
        # 不能用 "w"：那会先把锁文件清空，把在跑实例记的 PID 抹掉，
        # 后面就没法判断旧实例是不是还活着了。
        fd = os.open(_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    f = os.fdopen(fd, "r+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    try:
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
    except Exception:  # noqa: BLE001
        pass
    return f


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
    global _lock_fd
    f = _acquire_lock()
    if f is None:
        return False
    _lock_fd = f
    try:
        # 给锁 fd 置 FD_CLOEXEC：托盘面板由 tray_mac.Popen 派生，macOS 上
        # Popen 走 posix_spawn 路径时不会逐个 close_fds，子进程会连带继承
        # 这个 fd；主进程退出后 fd 仍活在托盘面板里，flock 锁就一直不释放，
        # 表现为「重启 App 提示已有实例在运行」而其实谁都没在跑（实测）。
        # 置 CLOEXEC 后子进程一律拿不到该 fd，锁随主进程退出立即释放。
        try:
            fcntl.fcntl(_lock_fd.fileno(), fcntl.FD_CLOEXEC)
        except Exception:  # noqa: BLE001
            pass
        return True
    except (OSError, ImportError):
        return False


_lock_fd = None


def _pid_is_ours(pid):
    """确认 PID 仍是本程序的主进程，避免误杀别的进程（PID 可能被回收）。"""
    if not pid:
        return False
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return False
    cmd = out.strip()
    return bool(cmd) and ("main.py" in cmd or "MultiMonManager" in cmd)


def _wait_for_open_ack(timeout=3.0):
    """等旧实例回唤醒回执；返回 ok / fail / None（无响应）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ack = cmd_channel.read_open_ack()
        if ack:
            return ack.get("status")
        time.sleep(0.15)
    return None


def _main_window_visible():
    """主窗口当前是否在屏上（用来独立确认唤醒是否真的生效）。"""
    try:
        import windows_mac as wm
        for w in wm.list_windows_front_to_back(min_size=200):
            name = w.get("name") or ""
            # 主窗口标题带「michaelqiu 出品」；托盘面板只叫「多屏管理器」。
            # 只按前缀会把托盘也算成主窗口，导致「窗口在屏=True」的误判。
            if name.startswith("多屏管理器 ·") and w.get("w", 0) >= 600:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _take_over_stale_instance():
    """僵死的旧实例占着锁：杀掉它，让当前进程接管。"""
    pid = _lock_holder_pid()
    if not _pid_is_ours(pid):
        return False
    logging.getLogger(__name__).warning(
        "旧实例无响应（pid=%s），结束它并接管", pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            break
        time.sleep(1.0)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    # 托盘子进程和 bash 守护脚本是旧实例派生的，不清理会继续占着窗口/抢锁。
    # 按 PPID/PID 精确点名，不用 pkill -f 的宽松模式——它会把「命令行里
    # 提到这个字符串的别的进程」（包括调用方 shell）一起带走。
    try:
        out = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=10).stdout
        targets = []
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            child_pid, parent_pid, command = int(parts[0]), int(parts[1]), parts[2]
            if parent_pid != pid:
                continue
            if ("_tray_panel.py" in command or
                    "Contents/MacOS/MultiMonManager" in command):
                targets.append(child_pid)
        for child_pid in targets:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    return True


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


def _set_appusermodelid():
    """给进程设显式 AppUserModelID（Windows 任务栏身份）。

    不设的话，任务栏用 exe 路径哈希当身份，图标按旧缓存取——换了图标后
    任务栏还显示老的羽毛图。设成稳定 ID（exe 路径无关）后，任务栏按新身份
    重新取窗口图标。必须在创建任何窗口之前调用。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [
            ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID(
            "cpufreestyle.MultiMonManager")
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("设置 AppUserModelID 失败: %s", e)


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
    _set_appusermodelid()
    if not _single_instance():
        # 已有实例在跑：不要静默退出——用户在主窗口被关掉（on_close 只是
        # withdraw 缩到托盘，进程仍活着并持锁）之后再双击 App，标准做法是
        # **唤醒已有实例的窗口**，而不是让新进程悄悄死掉。
        # 早先的实现是「记一行日志就 return」，用户看到的就是「双击了却
        # 什么都没发生」= 无法打开程序，而主窗口其实躲在托盘里。
        # 通道正是主进程和托盘子进程之间用的那条（cmd_channel），主进程
        # 每 800ms 轮询一次，收到 "open" 就 deiconify + lift。
        logging.getLogger(__name__).info("已有实例在运行，尝试唤醒其窗口")
        # 清掉上一次的旧回执，避免把上次的 fail 当成本次的结果
        try:
            os.remove(cmd_channel.ACK_FILE)
        except OSError:
            pass
        woke = False
        if cmd_channel.send("open"):
            logging.getLogger(__name__).info("已向已有实例发送 open 命令")
            status = _wait_for_open_ack()
            # 双保险：回执说是 ok，但再独立查一次窗口是否真在屏上。
            # 回执也可能来自上一轮的旧文件，所以两者都要满足才算唤醒成功。
            if status == "ok" and _main_window_visible():
                logging.getLogger(__name__).info("已有实例已恢复显示窗口")
                woke = True
            else:
                logging.getLogger(__name__).warning(
                    "已有实例无法恢复窗口（回执=%s，窗口在屏=%s）",
                    status, _main_window_visible())
        else:
            logging.getLogger(__name__).warning("唤醒已有实例失败：命令通道写入错误")
        if woke:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, "多屏管理器已在运行。", "多屏管理器", 0x40)
            except Exception:  # noqa: BLE001
                pass
            return
        # 旧实例僵死（轮询链断掉 / 进程卡住 / 窗口被系统回收），它还占着
        # 单例锁：不接管的话用户永远只能看到「无法打开程序」。
        if _take_over_stale_instance():
            time.sleep(1.0)
            if _single_instance():
                logging.getLogger(__name__).info("已接管僵死实例，继续启动")
            else:
                logging.getLogger(__name__).error("接管失败：锁仍被占用")
                return
        else:
            logging.getLogger(__name__).error("旧实例无法接管，退出")
            return
    logging.getLogger(__name__).info("单实例锁已获取，准备创建窗口…")
    # 四宫格尺寸缓存（上次实测的最小尺寸 / 尺寸上限）读入内存：排列预演
    # （点「2×2 排列」前的空白预告）与容量预检消费它。macOS 侧与 Windows 侧
    # 都有对应模块，读不到就按无缓存走——预演静默放行，不影响排布。
    try:
        if sys.platform == "darwin":
            import windows_mac as _wm_cache
            _wm_cache._quad_cache_load()
        elif sys.platform.startswith("win"):
            import windows as _win_cache
            _win_cache._quad_cache_load()
    except Exception:  # noqa: BLE001
        pass

    root = tk.Tk()
    # 诊断开关：关掉 Tk 的输入法支持（见下方说明）
    if os.environ.get("MMM_TK_NOIMK") == "1":
        try:
            root.tk.eval("option add *Tk.useInputMethods 0 interactive")
        except Exception:  # noqa: BLE001
            pass
    logging.getLogger(__name__).info("Tk 窗口已创建")

    # 图标要按平台分发：
    # - macOS 独立打包(.app) 时 Dock 图标由 bundle 的 app.icns / Info.plist 提供，
    #   运行时不再往包内写文件（包可能已签名或只读）；
    # - Windows 打包成 exe 后**仍然**要设窗口图标：exe 的图标只是资源管理器里
    #   那个文件的图标，Tk 顶层窗口默认还是解释器的羽毛图标，必须显式 iconbitmap。
    #   历史版本这里套了一层 `not frozen` 判断，导致 exe 里整段被跳过，用户看到
    #   的就是「软件图标没显示」。
    frozen = getattr(sys, "frozen", False)
    icon_path = resources.asset_path("app.ico")
    if icon_path:
        # macOS 上 iconbitmap 会导致 tkinter 窗口黑屏，仅 Windows 使用
        if sys.platform.startswith("win"):
            try:
                root.iconbitmap(icon_path)
            except Exception as e:  # noqa: BLE001
                logging.getLogger(__name__).warning("窗口图标设置失败: %s", e)
        # macOS：Tk 默认沿用 Python 解释器图标，替换成程序自己的图标（Dock 显示）
        if sys.platform == "darwin" and not frozen:
            try:
                png = resources.ensure_transparent_icon(
                    resources.asset_path("app.png") or "app.png")
                if png:
                    import dock_icon_mac
                    ok = dock_icon_mac.set_dock_icon(png)
                    logging.getLogger(__name__).info(
                        "Dock 图标%s: %s", "已设置" if ok else "设置失败", png)
            except Exception as e:  # noqa: BLE001
                logging.getLogger(__name__).warning("设置 Dock 图标失败: %s", e)
    else:
        # 源码运行且图标缺失时才生成兜底图标；打包后必须由构建脚本带进来，
        # 不在 _MEIPASS 里凭空写文件（那是每次启动都会清掉的临时目录）。
        if not frozen:
            here = os.path.dirname(os.path.abspath(__file__))
            icon_path = resources.create_ico(os.path.join(here, "app.ico"))
            if icon_path and sys.platform.startswith("win"):
                try:
                    root.iconbitmap(icon_path)
                except Exception:  # noqa: BLE001
                    pass
        else:
            logging.getLogger(__name__).warning(
                "打包后找不到 app.ico，窗口/托盘图标将缺失（构建时需 --add-data）")
    logging.getLogger(__name__).info("图标处理完成，开始构建界面…")

    # 界面构造期的异常必须落到日志：日志配置在 import backend 之前，
    # 而 ui.App 内部任何一处抛错都只会让进程静默退出——用户看到的是
    # 「点了没反应」，日志里连一行都没有，无从排查。
    try:
        app = ui.App(root)
    except Exception:
        import traceback
        logging.getLogger(__name__).exception("界面构造失败")
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
        raise
    if sys.platform == "darwin":
        mac_glass.apply(root, enabled=os.environ.get("MMM_GLASS", "1") != "0")
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

    # 防休眠：恢复上次选的模式（挂任务的人希望开机就生效，不想每次手动开）。
    # 合盖档需要管理员授权，启动时静默尝试一次（sudo -n），失败就只持断言
    # 并在状态栏说明「系统级没开成」——不弹框打断启动。
    try:
        _ka_mode = settings.load().get("keep_awake_mode", backend.keep_awake_default)
        if _ka_mode and _ka_mode != backend.keep_awake_default:
            _ok, _text, _d = backend.keep_awake_apply(_ka_mode, allow_prompt=False)
            logging.getLogger(__name__).info("防休眠恢复: mode=%s ok=%s %s",
                                             _ka_mode, _ok, _text)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("防休眠恢复失败: %s", e)

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
        on_toggle_rotate=lambda: cmd_channel.send("toggle_rotate"),
    )
    # 托盘面板也是本应用的窗口，登记后窗口工具会跳过它，避免误操作自己
    if getattr(t, "proc", None) is not None:
        backend.windows.register_own_pid(t.proc.pid)

    try:
        root.mainloop()
    finally:
        t.destroy()
        # 防休眠必须在这里归还：断言随进程死亡自动释放，但系统级
        # `pmset -a disablesleep 1` 不会——不还原就会把用户的 Mac
        # 永久改成不休眠（重启后仍在）。绝不静默留着。
        try:
            backend.keep_awake_release()
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).warning("防休眠释放失败: %s", e)


if __name__ == "__main__":
    main()
