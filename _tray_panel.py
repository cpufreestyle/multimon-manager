"""托盘快速访问面板（独立 Tk 进程运行，避免影响主窗口渲染）。

提供：打开主界面 / 应用窗口布局 / 刷新显示器 / 退出。前三个动作中，除"打开
主界面"外都通过命令文件通知主进程执行（见 cmd_channel），退出直接终止主进程。
主进程 PID 通过环境变量 MAIN_PID 传入；若未传入则仅退出本面板。
"""
import os
import signal
import subprocess
import sys

import tkinter as tk

# 允许以任意 cwd 启动时仍能 import 同目录的 cmd_channel
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cmd_channel  # noqa: E402


def main(tip=None):
    if tip is None:
        tip = sys.argv[1] if len(sys.argv) > 1 else "多屏管理器"
    main_pid = int(os.environ.get("MAIN_PID", "0"))

    root = tk.Tk()
    root.title(tip)
    root.geometry("200x170+1400+20")
    root.resizable(False, False)

    tk.Label(root, text=tip, font=("Arial", 12, "bold")).pack(pady=8)

    def activate_main():
        """通过 AppleScript 激活 Dock 中的 Python 应用窗口。"""
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
        cmd_channel.send(cmd)
        activate_main()

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
    tk.Button(root, text="刷新显示器",
              command=lambda: send_and_activate("refresh")).pack(fill="x", padx=14, pady=2)
    tk.Button(root, text="退出", command=do_exit).pack(fill="x", padx=14, pady=2)

    root.mainloop()


if __name__ == "__main__":
    main()
