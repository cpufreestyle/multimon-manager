#!/bin/bash
# 启动 multimon-manager：按候选顺序选「真的能 import tkinter」的解释器。
# 只判可执行不够——Homebrew 升级后 /opt/homebrew/bin/python3 可能指向没装
# python-tk 的版本（实测 python@3.15 就是），那时 exec 下去第一行
# import tkinter 就崩，表现为「双击 App 没反应」。
# Homebrew 各版本自带 Tk 9.0，优先；系统 /usr/bin/python3 的 Tcl/Tk 在
# macOS 上会导致 tkinter 窗口黑屏，只作最后兜底。
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 清理旧进程
pkill -f "main.py" 2>/dev/null || true
pkill -f "_tray_panel.py" 2>/dev/null || true
sleep 1

CANDIDATES="$MMM_PY_CANDIDATES"
if [ -z "$CANDIDATES" ]; then
  CANDIDATES="/opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3"
fi

pick_python() {
  if [ -f "$HOME/.multimon_python" ]; then
    p="$(cat "$HOME/.multimon_python")"
    if [ -n "$p" ] && [ -x "$p" ] && "$p" -c "import tkinter" >/dev/null 2>&1; then
      echo "$p"
      return
    fi
  fi
  for c in $CANDIDATES; do
    if [ -x "$c" ] && "$c" -c "import tkinter" >/dev/null 2>&1; then
      echo "$c"
      return
    fi
  done
  echo python3
}

PY="$(pick_python)"
echo "使用解释器: $PY"
export MAIN_PID=$$
exec "$PY" main.py "$@"
