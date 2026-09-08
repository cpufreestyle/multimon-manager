"""托盘快速访问面板（独立 Tk 进程运行，避免影响主窗口渲染）。

仅一个极简窗口：打开主界面（激活 Dock 图标）/ 退出（终止主进程）。
主进程 PID 通过环境变量 MAIN_PID 传入；若未传入则仅退出本面板。
"""
import os
import signal
import subprocess
import sys

import tkinter as tk


def main():
    tip = sys.argv[1] if len(sys.argv) > 1 else "多屏管理器"
    main_pid = int(os.environ.get("MAIN_PID", "0"))

    root = tk.Tk()
    root.title(tip)
    root.geometry("190x110+1400+20")
    root.resizable(False, False)

    tk.Label(root, text=tip, font=("Arial", 12, "bold")).pack(pady=8)

    def open_main():
        # 通过 AppleScript 激活 Dock 中的 Python 应用窗口
        try:
            subprocess.run(
                ['osascript', '-e',
                 'tell application "System Events" to set frontmost of '
                 'every process whose name is "Python" to true'],
                check=False,
            )
        except Exception:  # noqa: BLE001
            pass

    def do_exit():
        if main_pid:
            try:
                os.kill(main_pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
        root.destroy()

    tk.Button(root, text="打开主界面", command=open_main).pack(fill="x", padx=14, pady=2)
    tk.Button(root, text="退出", command=do_exit).pack(fill="x", padx=14, pady=2)

    root.mainloop()


if __name__ == "__main__":
    main()
