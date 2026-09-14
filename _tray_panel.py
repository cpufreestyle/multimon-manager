"""托盘快速访问面板（独立 Tk 进程运行，避免影响主窗口渲染）。

提供：打开主界面 / 应用窗口布局 / 切换情景 / 刷新显示器 / 退出。
除"打开主界面"外都通过命令文件通知主进程执行（见 cmd_channel）；退出直接终止
主进程。主进程 PID 通过环境变量 MAIN_PID 传入；若未传入则仅退出本面板。
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
    root.geometry("220x235+1400+20")
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

    tk.Button(root, text="刷新显示器",
              command=lambda: send_and_activate("refresh")).pack(fill="x", padx=14, pady=2)
    tk.Button(root, text="退出", command=do_exit).pack(fill="x", padx=14, pady=2)

    root.mainloop()


if __name__ == "__main__":
    main()
