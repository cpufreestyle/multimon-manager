#!/bin/bash
# 跑全套单测。必须选「真的能 import tkinter」的解释器：tests/test_quad_capacity.py
# 的 QuadCapacityTextTest 要 import ui，用没装 python-tk 的解释器跑会整类跳过
# （实测 python@3.15 就是），覆盖率静默少一截而退出码仍是 0。
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

CANDIDATES="$MMM_PY_CANDIDATES"
if [ -z "$CANDIDATES" ]; then
  CANDIDATES="/opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3"
fi

pick_python() {
  for c in $CANDIDATES; do
    if [ -x "$c" ] && "$c" -c "import tkinter" >/dev/null 2>&1; then
      echo "$c"
      return
    fi
  done
  echo python3
}

PY="$(pick_python)"
echo "使用解释器: $PY ($("$PY" -V 2>&1))"
# 走带看门狗的入口：界面单测曾因构造期 after 定时器在真机上挂死，外层只能
# SIGTERM。超时现在是明确的 TEST_SUITE_TIMEOUT + 全线程栈，不是无限等待。
# 超时上限可用 MMM_TEST_TIMEOUT 覆盖；`"$PY" run_tests_guarded.py --selfcheck`
# 可反向验证看门狗没被人删掉。
exec "$PY" run_tests_guarded.py
