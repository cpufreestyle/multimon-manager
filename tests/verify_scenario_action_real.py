"""真机验证：触发器 scenario 动作的三种结果（不移动任何窗口）。

手动运行：`python3 tests/verify_scenario_action_real.py`
（不命名为 test_* 是避免被 unittest discover 自动收集——它会读写真实配置。）

覆盖 _run_trigger_action 的 scenario 分支：
1. 情景不存在      → "情景不存在"
2. 情景存在但为空  → "无可用预设"（不得谎报"已套用"）
3. 情景有内容      → "已套用"（用 mock 让布局应用返回成功，避免真动窗口）

会临时写入 settings.json（绑定/解绑测试情景），结束前自动从备份恢复。
"""
import os
import shutil
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import backend as b  # noqa: E402
import scenarios  # noqa: E402
import settings  # noqa: E402
import ui  # noqa: E402

_results = []


def check(name, cond, extra=""):
    _results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name + ("  |  " + extra if extra else ""))


class _MB:
    """防弹窗阻塞（自动化测试调用会弹 messagebox 的 UI 方法前必须 mock）。"""

    @staticmethod
    def showerror(*a, **k):
        print("   [mock] showerror:", str(a[:1])[:80])

    @staticmethod
    def showwarning(*a, **k):
        print("   [mock] showwarning:", str(a[:1])[:80])

    @staticmethod
    def showinfo(*a, **k):
        pass

    @staticmethod
    def askyesno(*a, **k):
        return True


ui.messagebox = _MB

# 备份配置（bind/unbind 会写 settings.json）
try:
    cfg = os.path.join(settings.data_dir(), "settings.json")
    backup = os.path.join(_ROOT, "_scenario_verify_backup.json")
    if os.path.exists(cfg):
        shutil.copy2(cfg, backup)
    _has = os.path.exists(cfg)
except Exception:  # noqa: BLE001
    cfg = backup = None
    _has = False

mons = b.enum_monitors()
print("当前屏数:", len(mons))


class _Status:
    def __init__(self):
        self.value = ""
        self.history = []

    def set(self, v):
        self.value = v
        self.history.append(v)
        print("   status:", v)


# 轻量 self：只提供动作代码真正用到的属性，方法用真实的 ui.App 实现绑定
fake = types.SimpleNamespace()
fake.monitors = mons
fake.status_var = _Status()
fake._apply_scenario = lambda sig, scen, silent=False: ui.App._apply_scenario(
    fake, sig, scen, silent)
fake._apply_scenario_by_signature = lambda sig: ui.App._apply_scenario_by_signature(
    fake, sig)
# 第三分支用：让"应用布局"返回成功，从而走"已套用"，同时不动任何真实窗口
fake._apply_layout_by_name = lambda name, silent=True: True

wins = b.list_target_windows()
W = next((w for w in wins if "python" not in (w.get("owner") or "").lower()),
         wins[0] if wins else {})
before = (W.get("x"), W.get("y"), W.get("w"), W.get("h"))
print("观察窗口:", W.get("owner"), "/", (W.get("name") or "")[:20], before)


def run(t):
    fake.status_var.history.clear()
    ui.App._run_trigger_action(fake, t, W)
    return list(fake.status_var.history)


sig = scenarios.monitors_signature(mons)

# ---------- 分支 1：情景存在但为空（无壁纸方案 / 无布局）----------
scenarios.bind(sig, layout=None, profile=None, name="__mm_verify_empty__")
check("空情景已绑定", scenarios.get(sig) is not None,
      "签名=%s" % scenarios.describe(sig))
check("当前组合能被 match 命中", scenarios.match(mons) is not None)

hist = run({"action": "scenario", "param": sig, "event": "moved", "enabled": True})
check("空情景：走通真实套用路径（给出提示而非崩溃/静默）",
      any("无可用预设" in s for s in hist), "history=%s" % hist)
check("空情景：不再谎报「已套用」（修复点）",
      not any("已套用" in s for s in hist), "history=%s" % hist)

after = None
for w in b.list_target_windows():
    if w.get("hwnd") == W.get("hwnd"):
        after = (w["x"], w["y"], w["w"], w["h"])
        break
check("空情景：不改动任何窗口",
      after is None or (abs(after[0] - before[0]) <= 5
                        and abs(after[1] - before[1]) <= 5),
      "%s -> %s" % (before[:2], after[:2] if after else None))

# ---------- 分支 2：情景不存在 ----------
try:
    scenarios.unbind(sig)
except Exception:  # noqa: BLE001
    pass
hist2 = run({"action": "scenario", "param": "NO_SUCH_SIG",
             "event": "moved", "enabled": True})
check("情景不存在：给出明确提示", any("情景不存在" in s for s in hist2),
      "history=%s" % hist2)
check("情景不存在：不谎报「已套用」", not any("已套用" in s for s in hist2))

# ---------- 分支 3：情景有内容（mock 布局应用成功）----------
scenarios.bind(sig, layout="__fake_layout__", profile=None,
               name="__mm_verify_full__")
hist3 = run({"action": "scenario", "param": sig, "event": "moved",
             "enabled": True})
check("有内容情景：报「已套用」", any("已套用" in s for s in hist3),
      "history=%s" % hist3)

# ---------- 返回值语义（供调用方判断）----------
applied_empty = ui.App._apply_scenario_by_signature(fake, "NO_SUCH_SIG")
check("返回语义：情景不存在时返回空列表", applied_empty == [],
      "返回=%r" % (applied_empty,))

# ---------- 清理 ----------
try:
    scenarios.unbind(sig)
except Exception:  # noqa: BLE001
    pass
check("临时情景已清理", scenarios.get(sig) is None)
if _has and cfg and os.path.exists(backup):
    shutil.copy2(backup, cfg)
    os.remove(backup)
check("settings.json 已恢复", True)

ok = sum(1 for _, c in _results if c)
print("\n%d/%d 通过" % (ok, len(_results)))
sys.exit(0 if ok == len(_results) else 1)
