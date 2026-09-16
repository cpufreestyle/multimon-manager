r"""开机自启管理（跨平台）。

Windows: 写 HKCU\Software\Microsoft\Windows\CurrentVersion\Run 注册表
macOS:   写 ~/Library/LaunchAgents/com.multimonmanager.plist
"""
import os
import sys

APP_NAME = "MultiMonManager"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAUNCH_AGENT = os.path.join(
    os.path.expanduser("~"),
    "Library", "LaunchAgents",
    "com.multimonmanager.plist",
)

_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.multimonmanager</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>{minimized_arg}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
"""


def _app_command(minimized=False):
    """返回启动命令。打包 exe 时用 sys.executable，否则 python + main.py。

    minimized=True 时追加 --minimized 参数（开机自启时静默启动到托盘）。
    """
    if getattr(sys, "frozen", False):
        cmd = '"%s"' % sys.executable
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        cmd = '"%s" "%s"' % (sys.executable, os.path.join(here, "main.py"))
    if minimized:
        cmd += " --minimized"
    return cmd


def is_enabled():
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
                winreg.QueryValueEx(k, APP_NAME)
                return True
        except OSError:
            return False
    return os.path.exists(LAUNCH_AGENT)


def get_command():
    """读取当前注册的自启动命令/配置；未注册返回 None。"""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
                value, _ = winreg.QueryValueEx(k, APP_NAME)
                return value
        except OSError:
            return None
    if os.path.exists(LAUNCH_AGENT):
        try:
            with open(LAUNCH_AGENT, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""
    return None


def is_minimized():
    """自启动是否配置为静默启动（--minimized）。未注册自启时返回 False。"""
    cmd = get_command()
    if not cmd:
        return False
    return "--minimized" in cmd


def set_enabled(enabled, minimized=False):
    if sys.platform == "win32":
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        except OSError as e:
            raise RuntimeError(f"无法打开开机自启注册表项: {e}") from e
        try:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                                  _app_command(minimized))
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except OSError:
                    pass
        except OSError as e:
            raise RuntimeError(f"写入开机自启注册表失败: {e}") from e
        finally:
            winreg.CloseKey(key)
        return True

    # macOS LaunchAgent
    try:
        if enabled:
            agent_dir = os.path.dirname(LAUNCH_AGENT)
            os.makedirs(agent_dir, exist_ok=True)
            python = sys.executable or "/usr/bin/python3"
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
            minimized_arg = "\n        <string>--minimized</string>" if minimized else ""
            with open(LAUNCH_AGENT, "w", encoding="utf-8") as f:
                f.write(_PLIST.format(python=python, script=script,
                                      minimized_arg=minimized_arg))
        else:
            if os.path.exists(LAUNCH_AGENT):
                os.remove(LAUNCH_AGENT)
    except OSError as e:
        raise RuntimeError(f"设置 macOS 开机自启失败: {e}") from e
    return True
