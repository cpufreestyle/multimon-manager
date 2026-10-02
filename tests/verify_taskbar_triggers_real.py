"""真机回归：多屏任务栏 + 触发器判定层。

手动运行：`python3 tests/verify_taskbar_triggers_real.py`
（不命名为 test_* 是避免被 unittest discover 自动收集——它会创建真实的 Tk 窗口。）

需要真机多屏 + 辅助功能授权。不使用 time.sleep（会被工具启发式跳过），
改用 root.after + mainloop 收尾。不移动用户的真实窗口：触发器只验证判定层。
"""
import logging
import os
import sys
import tkinter as tk

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import backend as b  # noqa: E402
import taskbar  # noqa: E402
import triggers  # noqa: E402

logging.basicConfig(level=logging.WARNING)
_results = []


def check(name, cond, extra=""):
    _results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name + ("  |  " + extra if extra else ""))


# ---------- 1. 真机显示器 ----------
mons = b.enum_monitors()
check("枚举到多屏", len(mons) >= 2,
      "屏数=%d  各屏=%s" % (len(mons), [(m.device_name, m.left, m.top, m.width, m.height)
                                    for m in mons]))
neg = [m for m in mons if m.left < 0 or m.top < 0]
check("存在负坐标显示器（左侧/上方副屏）", len(neg) >= 1, "负坐标屏=%d" % len(neg))

# ---------- 2. 任务栏几何：负坐标必须生成合法 Tk geometry ----------
for m in mons:
    for pos in ("bottom", "top"):
        g = taskbar.geometry_string(*taskbar.bar_geometry(m, pos))
        check("几何字符串无 '+-'（%s / %s）" % (m.device_name, pos),
              "+-" not in g, g)

# 对照观察：Tk 对旧写法 '+-N' 的实际态度
# 2026-09-20 实测：Tk **容忍** '+-N'，故此处只记录、不作通过/失败判据。
if neg:
    legacy = "%dx%d+%d+%d" % taskbar.bar_geometry(neg[0], "bottom")
    if "+-" in legacy:
        try:
            t = tk.Toplevel()
            t.geometry(legacy)
            t.update_idletasks()
            print("   观察：Tk 接受旧写法 %s（Tk 容忍 '+-N'）" % legacy)
            t.destroy()
        except Exception as exc:  # noqa: BLE001
            print("   观察：Tk 拒绝旧写法 %s -> %s" % (legacy, type(exc).__name__))

# ---------- 3. 真机创建任务栏：每屏都要能建出来 ----------
wins = b.list_target_windows()
check("枚举到真实窗口", len(wins) >= 1, "窗口数=%d" % len(wins))

root = tk.Tk()
root.withdraw()
tb = taskbar.TaskBar(root, position="bottom")
tb.show()
tb.refresh(mons, wins)
created = len(tb._bars)
check("每屏都创建了任务栏", created == len(mons),
      "已创建 %d / 屏数 %d" % (created, len(mons)))

for dev, bar in tb._bars.items():
    try:
        bar["top"].update_idletasks()
        geo = bar["top"].geometry()
        check("Tk 实际接受该屏几何（%s）" % dev[-6:], bool(geo) and "+-" not in geo, geo)
    except Exception as exc:  # noqa: BLE001
        check("Tk 实际接受该屏几何（%s）" % dev[-6:], False, repr(exc))

# ---------- 4. 窗口→屏幕分组 ----------
groups = taskbar.split_windows_by_monitor(mons, wins)
total = sum(len(v) for v in groups.values())
check("所有窗口都被归属到某一屏", total == len(wins),
      "已归属 %d / 窗口 %d" % (total, len(wins)))

misassigned = []
for dev, ws in groups.items():
    mon = next((m for m in mons if m.device_path == dev), None)
    if mon is None:
        continue
    for w in ws:
        cx, cy = w["x"] + w["w"] / 2.0, w["y"] + w["h"] / 2.0
        inside = (mon.left <= cx < mon.left + mon.width
                  and mon.top <= cy < mon.top + mon.height)
        if not inside:
            best = max(((taskbar._overlap_area(w, m), m) for m in mons),
                       key=lambda p: p[0])[1]
            if best.device_path != dev:
                misassigned.append((w.get("owner"), w.get("name")))
check("分组归属正确（中心点命中或最大重叠兜底）", not misassigned,
      "异常=%s" % misassigned[:3])

# ---------- 5. 触发器判定层（真机数据，零持久化污染）----------
if wins:
    w0 = wins[0]
    geo0 = (w0["x"], w0["y"], w0["w"], w0["h"])
    check("is_moved：位移 60px 判为移动",
          triggers.is_moved(geo0, (geo0[0] + 60, geo0[1], geo0[2], geo0[3])))
    check("is_moved：位移 5px 不判为移动",
          not triggers.is_moved(geo0, (geo0[0] + 5, geo0[1], geo0[2], geo0[3])))
    check("is_moved：仅缩放不判为移动",
          not triggers.is_moved(geo0, (geo0[0], geo0[1], geo0[2] + 200, geo0[3] + 200)))
    check("is_moved：首次见到（prev=None）不算移动",
          not triggers.is_moved(None, geo0))
    trig_hit = {"field": "owner", "pattern": w0["owner"], "regex": False,
                "event": "moved", "action": "snap", "enabled": True}
    check("match：命中真实窗口 owner", triggers.match(trig_hit, w0),
          "owner=%s" % w0["owner"])
    check("match：不匹配无关关键词",
          not triggers.match({"field": "owner", "pattern": "__nope__"}, w0))
    check("match：非法正则安全返回 False",
          not triggers.match({"field": "owner", "pattern": "([", "regex": True}, w0))
    mon0 = triggers.monitor_of_window(mons, w0)
    check("monitor_of_window：真实窗口能定位到屏", mon0 is not None,
          "屏=%s" % (mon0.device_name if mon0 else None))

check("事件/动作集合完整",
      set(triggers.EVENTS) == {"moved", "created"}
      and set(triggers.ACTIONS) == {"snap", "monitor", "scenario", "rules"})
check("防误触发常量合理",
      triggers.MOVE_THRESHOLD >= 10 and triggers.COOLDOWN_SEC >= 5,
      "THRESHOLD=%s COOLDOWN=%s" % (triggers.MOVE_THRESHOLD, triggers.COOLDOWN_SEC))


def _finish():
    tb.hide()
    root.quit()


# 停留 3 秒（肉眼可见每屏一条）后收尾
root.after(3000, _finish)
root.mainloop()
root.destroy()

ok = sum(1 for _, c in _results if c)
print("\n%d/%d 通过" % (ok, len(_results)))
sys.exit(0 if ok == len(_results) else 1)
