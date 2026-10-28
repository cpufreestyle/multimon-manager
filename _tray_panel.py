"""托盘快速访问面板（独立 Tk 进程运行，避免影响主窗口渲染）。

提供：打开主界面 / 应用窗口布局 / 2×2 排最近四窗 / 切换情景 / 开始·停止
幻灯片 / 刷新显示器 / 退出。除"打开主界面"外都通过命令文件通知主进程执行
（见 cmd_channel）；退出直接终止主进程。主进程 PID 通过环境变量 MAIN_PID
传入；若未传入则仅退出本面板。
"""
import os
import signal
import subprocess
import sys

import tkinter as tk
from tkinter import ttk

# 允许以任意 cwd 启动时仍能 import 同目录的 cmd_channel / scenarios
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cmd_channel  # noqa: E402


def _main_alive(pid):
    """主进程是否还活着（PID 复用也算死：用 starttime 校验）。"""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _relaunch_main():
    """主进程已死时重新拉起它（macOS：open .app；其它平台：直接跑 main.py）。

    托盘是独立进程，主进程崩溃后它会变成孤儿：命令照发，但没有任何进程
    消费，用户看到的就是「按钮点了没效果」。这里在发命令前补一次自愈。
    """
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-a", "多屏管理器"], check=False, timeout=5)
        else:
            here = os.path.dirname(os.path.abspath(__file__))
            subprocess.Popen([sys.executable, os.path.join(here, "main.py")],
                             close_fds=True)
    except Exception:  # noqa: BLE001
        pass


def _load_scenarios():
    """读取已绑定情景，返回 [(signature, 显示名), ...]；失败返回空列表。"""
    try:
        import scenarios
        rows = []
        for sig, scen in sorted(scenarios.list_scenarios().items()):
            rows.append((sig, scen.get("name") or scenarios.describe(sig)))
        return rows
    except Exception:  # noqa: BLE001
        return []


def main(tip=None):
    if tip is None:
        tip = sys.argv[1] if len(sys.argv) > 1 else "多屏管理器"
    main_pid = int(os.environ.get("MAIN_PID", "0"))

    root = tk.Tk()
    root.title(tip)
    root.geometry("220x302+1400+20")
    root.resizable(False, False)

    tk.Label(root, text=tip, font=("Arial", 12, "bold")).pack(pady=6)

    def activate_main():
        """通过 AppleScript 激活主窗口所在进程（按进程名 Python 匹配）。"""
        try:
            subprocess.run(
                ['osascript', '-e',
                 'tell application "System Events" to set frontmost of '
                 'every process whose name is "Python" to true'],
                check=False,
            )
        except Exception:  # noqa: BLE001
            pass

    def send_and_activate(cmd):
        # 主进程死了就先拉起来：否则命令发出去没人消费，按钮像坏了
        if not _main_alive(main_pid):
            _relaunch_main()
            import time as _t
            _t.sleep(2.5)      # 等新实例建完界面、进入命令轮询
        cmd_channel.send(cmd)
        activate_main()

    def _send_quiet(cmd):
        """同 send_and_activate，但不激活主窗口。

        2×2 一键要的是「不动界面、只动窗口」：send_and_activate 会把主窗拉到
        最前，盖住刚排好的四个窗口。主进程已死时仍需先自愈，否则没人消费。
        """
        if not _main_alive(main_pid):
            _relaunch_main()
            import time as _t
            _t.sleep(2.5)
        cmd_channel.send(cmd)

    def do_exit():
        if main_pid:
            try:
                os.kill(main_pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
        root.destroy()

    tk.Button(root, text="打开主界面", command=activate_main).pack(fill="x", padx=14, pady=2)
    tk.Button(root, text="应用窗口布局",
              command=lambda: send_and_activate("apply_layout")).pack(fill="x", padx=14, pady=2)
    # 2×2 一键：只发命令、不激活主窗。send_and_activate 会把主窗拉到最前，
    # 盖住刚排好的四个窗口——这条命令要的就是「不动界面、只动窗口」。
    tk.Button(root, text="2×2 排最近四窗",
              command=lambda: _send_quiet("snap_quad")).pack(fill="x", padx=14, pady=2)

    # 切换情景（F4）：列出已绑定情景，一键套用（壁纸方案 + 窗口布局）
    scen_rows = _load_scenarios()
    scen_label = tk.StringVar(value=scen_rows[0][1] if scen_rows else "（无情景）")
    scen_box = ttk.Combobox(root, textvariable=scen_label, state="readonly",
                            values=[label for _sig, label in scen_rows])
    scen_box.pack(fill="x", padx=14, pady=(6, 2))

    def apply_selected_scenario():
        label = scen_label.get()
        for sig, lb in scen_rows:
            if lb == label:
                send_and_activate("apply_scenario:" + sig)
                return

    scen_btn = tk.Button(root, text="套用情景", command=apply_selected_scenario)
    scen_btn.pack(fill="x", padx=14, pady=2)
    if not scen_rows:
        scen_btn.configure(state="disabled")

    tk.Button(root, text="开始/停止幻灯片",
              command=lambda: send_and_activate("toggle_slideshow")).pack(fill="x", padx=14, pady=2)
    tk.Button(root, text="刷新显示器",
              command=lambda: send_and_activate("refresh")).pack(fill="x", padx=14, pady=2)
    tk.Button(root, text="退出", command=do_exit).pack(fill="x", padx=14, pady=2)

    # 主进程守护：主进程死了就自行退出。否则托盘变成孤儿——界面照旧显示、
    # 按钮照旧可点，但命令没有任何进程消费，用户看到的就是「点了没效果」。
    # 顺带避免每次 open .app 都新起一个托盘、旧的永远留着（托盘增殖）。
    def _watch_main():
        if main_pid and not _main_alive(main_pid):
            root.destroy()
            return
        root.after(2000, _watch_main)

    if main_pid:
        root.after(2000, _watch_main)

    root.mainloop()


if __name__ == "__main__":
    main()
