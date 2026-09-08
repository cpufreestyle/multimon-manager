#!/bin/bash
# 使用 Homebrew 的 Python（自带 Tk 9.0）启动 multimon-manager
# 注意：系统自带 /usr/bin/python3 的 Tcl/Tk 在 macOS 上会导致 tkinter 窗口黑屏，必须用本脚本的 python3
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 清理旧进程
pkill -f "main.py" 2>/dev/null || true
pkill -f "_tray_panel.py" 2>/dev/null || true
sleep 1

export MAIN_PID=$$
exec /opt/homebrew/bin/python3 main.py
