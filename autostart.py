"""开机自启管理（跨平台）。

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
        <string>{script}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
"""


def _app_command():
    """返回启动命令。打包 exe 时用 sys.executable，否则 python + main.py。"""
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    here = os.path.dirname(os.path.abspath(__file__))
    return '"%s" "%s"' % (sys.executable, os.path.join(here, "main.py"))


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


def set_enabled(enabled):
    if sys.platform == "win32":
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        except OSError as e:
            raise RuntimeError(f"无法打开开机自启注册表项: {e}") from e
        try:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _app_command())
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
            with open(LAUNCH_AGENT, "w", encoding="utf-8") as f:
                f.write(_PLIST.format(python=python, script=script))
        else:
            if os.path.exists(LAUNCH_AGENT):
                os.remove(LAUNCH_AGENT)
    except OSError as e:
        raise RuntimeError(f"设置 macOS 开机自启失败: {e}") from e
    return True
