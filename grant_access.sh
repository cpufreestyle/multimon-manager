#!/bin/bash
# 一键引导辅助功能授权：找出本程序真正需要授权的那个二进制，在 Finder 里显示出来，
# 并打开系统设置的辅助功能页面。开关必须用户手动打开，App 无法代劳。
#
# 为什么要这个脚本：macOS 的辅助功能授权按「真正负责的进程」记账。Homebrew 的
# framework Python 跑起来会把自己变成 .../Python.app/Contents/MacOS/Python，
# 而不是 sys.executable 指向的那个软链。照软链去加列表，用户会看到「加了却没
# 反应」。这里用 _NSGetExecutablePath 取真实路径，避免指错文件。
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

CANDIDATES="$MMM_PY_CANDIDATES"
if [ -z "$CANDIDATES" ]; then
  CANDIDATES="/opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3"
fi

PY=""
for c in $CANDIDATES; do
  if [ -x "$c" ] && "$c" -c "import tkinter" >/dev/null 2>&1; then
    PY="$c"
    break
  fi
done
[ -z "$PY" ] && PY="python3"

echo "使用解释器: $PY"
"$PY" - << 'INNEREOF'
import accessibility_mac as a

p = a.responsible_binary()
if not p:
    print("取不到本进程的可执行文件路径，请手动到 系统设置 -> 隐私与安全性 -> 辅助功能 添加")
else:
    print("")
    print("需要授权的文件（把它拖进下面的列表）：")
    print("  " + p)
    print("")
    a.reveal_binary()
a.open_settings()
print("已在 Finder 中选中该文件，并打开系统设置的辅助功能页面。")
print("把 Finder 里选中的文件拖进 辅助功能 列表，然后打开它右侧的开关。")
INNEREOF
