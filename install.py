"""MultiMonManager 安装器（纯 Python，自解压 exe）。

运行时从 PyInstaller 单文件包（sys._MEIPASS）或当前目录提取：
- MultiMonManager.exe
- app.ico

安装到 %LOCALAPPDATA%\MultiMonManager，并创建开始菜单/桌面快捷方式。
"""
import os
import shutil
import subprocess
import sys
import tempfile

APP_NAME = "MultiMonManager"
VERSION = "0.2.2"


def _meipass_or_here(name):
    """PyInstaller 单文件运行时资源在 _MEIPASS；开发测试时在脚本同目录。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _install_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)


def _make_shortcut(lnk_path, target, icon=None, working_dir=None):
    """通过 Windows Script Host 创建 .lnk 快捷方式。"""
    vbs = tempfile.NamedTemporaryFile("w", suffix=".vbs", delete=False, encoding="mbcs")
    try:
        vbs.write('Set WshShell = WScript.CreateObject("WScript.Shell")\n')
        vbs.write('Set oLink = WshShell.CreateShortcut("%s")\n' % lnk_path)
        vbs.write('oLink.TargetPath = "%s"\n' % target)
        if working_dir:
            vbs.write('oLink.WorkingDirectory = "%s"\n' % working_dir)
        if icon:
            vbs.write('oLink.IconLocation = "%s"\n' % icon)
        vbs.write('oLink.Save\n')
        vbs.close()
        subprocess.run(["cscript", "//NOLOGO", vbs.name], check=True, shell=False)
    finally:
        try:
            os.remove(vbs.name)
        except OSError:
            pass


def _write_uninstall_bat(install_dir):
    """生成卸载批处理，删除目录、快捷方式和注册表项。"""
    bat = os.path.join(install_dir, "uninstall.bat")
    lines = [
        "@echo off",
        f'rmdir /s /q "{install_dir}"',
        f'del "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\{APP_NAME}.lnk"',
        f'del "%USERPROFILE%\\Desktop\\{APP_NAME}.lnk"',
        r'reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\MultiMonManager" /f',
        "echo 卸载完成。",
    ]
    with open(bat, "w", encoding="ascii") as f:
        f.write("\n".join(lines))
    return bat


def _register_uninstall(install_dir, exe_path):
    """在 HKCU 注册卸载信息（程序和功能）。"""
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MultiMonManager"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, f"MultiMonManager {VERSION}")
        winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, exe_path)
        winreg.SetValueEx(
            k, "UninstallString", 0, winreg.REG_SZ,
            f'"{os.path.join(install_dir, "uninstall.bat")}"',
        )
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "cpufreestyle")
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)


def main():
    print(f"MultiMonManager v{VERSION} 安装程序")
    install_dir = _install_dir()
    print(f"安装目录: {install_dir}")
    ans = input("按回车开始安装，输入 N 取消: ").strip().lower()
    if ans == "n":
        return

    exe_src = _meipass_or_here("MultiMonManager.exe")
    ico_src = _meipass_or_here("app.ico")
    if not os.path.exists(exe_src):
        print("错误：未找到 MultiMonManager.exe", file=sys.stderr)
        sys.exit(1)

    os.makedirs(install_dir, exist_ok=True)
    exe_dst = os.path.join(install_dir, "MultiMonManager.exe")
    ico_dst = os.path.join(install_dir, "app.ico")
    shutil.copy2(exe_src, exe_dst)
    if os.path.exists(ico_src):
        shutil.copy2(ico_src, ico_dst)

    _write_uninstall_bat(install_dir)

    start_menu = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "Microsoft", "Windows", "Start Menu", "Programs",
    )
    os.makedirs(start_menu, exist_ok=True)
    _make_shortcut(
        os.path.join(start_menu, f"{APP_NAME}.lnk"),
        exe_dst, icon=ico_dst, working_dir=install_dir,
    )

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    _make_shortcut(
        os.path.join(desktop, f"{APP_NAME}.lnk"),
        exe_dst, icon=ico_dst, working_dir=install_dir,
    )

    try:
        _register_uninstall(install_dir, exe_dst)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 注册卸载信息失败: {e}")

    print("安装完成。可在开始菜单或桌面启动。")
    input("按回车退出...")


if __name__ == "__main__":
    main()
