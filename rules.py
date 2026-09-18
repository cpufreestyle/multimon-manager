"""窗口规则引擎（F5）：按应用名/标题匹配窗口，自动分配到指定屏与位置。

规则持久化在 settings.json 的 "window_rules" 键（列表，顺序即优先级）。
匹配自上而下，**先命中先执行**，命中即停止（避免同一窗口被多条规则反复移动）。

规则字段：
    {
      "enabled": bool,               # 是否启用
      "field":   "owner" | "title",  # 匹配应用名还是窗口标题
      "pattern": str,                # 关键词（默认不区分大小写）或正则
      "regex":   bool,               # True 时 pattern 按正则解释
      "monitor": str | None,         # 目标屏 device_path；None=保持窗口当前所在屏
      "zone":    "full"|"left"|"right"|"top"|"bottom",
    }
"""
import re

import settings

_KEY = "window_rules"

ZONES = ("full", "left", "right", "top", "bottom")
ZONE_LABELS = {
    "full": "整屏",
    "left": "左半屏",
    "right": "右半屏",
    "top": "上半屏",
    "bottom": "下半屏",
}


def list_rules():
    """返回规则列表（顺序即优先级）；结构异常时返回空列表。"""
    data = settings.load().get(_KEY)
    return list(data) if isinstance(data, list) else []


def save_rules(rules):
    s = settings.load()
    s[_KEY] = rules
    settings.save(s)


def add_rule(rule):
    rules = list_rules()
    rules.append(dict(rule))
    save_rules(rules)
    return rules


def delete_rule(index):
    rules = list_rules()
    if 0 <= index < len(rules):
        del rules[index]
        save_rules(rules)
    return rules


def move_rule(index, delta):
    """把第 index 条规则上移/下移 delta 位（调整优先级）。"""
    rules = list_rules()
    j = index + delta
    if 0 <= index < len(rules) and 0 <= j < len(rules):
        rules[index], rules[j] = rules[j], rules[index]
        save_rules(rules)
    return rules


def toggle_rule(index):
    rules = list_rules()
    if 0 <= index < len(rules):
        rules[index]["enabled"] = not rules[index].get("enabled", True)
        save_rules(rules)
    return rules


def match_window(rule, win):
    """窗口是否命中规则。win 为含 "owner"/"name" 的字典。"""
    pattern = rule.get("pattern") or ""
    if not pattern:
        return False
    field = rule.get("field", "owner")
    text = (win.get("owner") if field == "owner" else win.get("name")) or ""
    if rule.get("regex"):
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern.lower() in text.lower()


def find_monitor(monitors, device_path):
    for m in monitors:
        if m.device_path == device_path:
            return m
    return None


def _monitor_of_window(monitors, win):
    """按窗口中心点判断它当前所在显示器。"""
    x, y = win.get("x"), win.get("y")
    if x is None or y is None:
        return None
    cx = x + (win.get("w") or 0) // 2
    cy = y + (win.get("h") or 0) // 2
    for m in monitors:
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return m
    return None


def zone_rect(mon, zone):
    """按位置预设计算目标矩形（基于该屏工作区，自动扣除菜单栏/Dock）。"""
    wl, wt = mon.work_left, mon.work_top
    ww, wh = mon.work_width, mon.work_height
    half_w, half_h = ww // 2, wh // 2
    if zone == "left":
        return (wl, wt, half_w, wh)
    if zone == "right":
        return (wl + ww - half_w, wt, half_w, wh)
    if zone == "top":
        return (wl, wt, ww, half_h)
    if zone == "bottom":
        return (wl, wt + wh - half_h, ww, half_h)
    return (wl, wt, ww, wh)


def rule_target(rule, win, monitors):
    """计算规则要施加的目标矩形；无法确定时返回 None。"""
    device = rule.get("monitor")
    mon = find_monitor(monitors, device) if device else None
    if mon is None:
        mon = _monitor_of_window(monitors, win)
    if mon is None:
        return None
    return zone_rect(mon, rule.get("zone", "full"))


def apply_to_window(rule, win, monitors):
    """把规则施加到窗口；窗口已在目标位置时不重复移动。返回是否移动。"""
    target = rule_target(rule, win, monitors)
    if not target:
        return False
    current = (win.get("x"), win.get("y"), win.get("w"), win.get("h"))
    if target == current:
        return False
    import backend as b  # 延迟导入，避免平台模块与 UI 层的导入环
    b.set_window_rect(win["hwnd"], target[0], target[1], target[2], target[3],
                      activate=False)
    return True


# ---------- 内置规则模板 ----------
# 模板用「应用名子串（不区分大小写）」匹配，跨平台通用：macOS / Windows 的应用名
# 不同（如 "Code" vs "Code.exe"），但 code / chrome / terminal 这类关键词两边都命中。
# monitor=None 表示「不换屏」：按窗口中心点所在屏分区，不绑定具体显示器，
# 因此换机器、换接口、改系统屏号都不会失效。
TEMPLATES = {
    "开发：编辑器左 / 浏览器右 / 终端下": [
        ("code", "left"),
        ("chrome", "right"), ("safari", "right"), ("edge", "right"),
        ("terminal", "bottom"), ("iterm", "bottom"), ("powershell", "bottom"),
    ],
    "写作：编辑器左 / 资料右": [
        ("code", "left"), ("typora", "left"), ("notion", "left"), ("obsidian", "left"),
        ("chrome", "right"), ("safari", "right"), ("preview", "right"), ("acrobat", "right"),
    ],
    "会议：会议软件整屏 / 浏览器右": [
        ("zoom", "full"), ("teams", "full"), ("tencent", "full"), ("meeting", "full"),
        ("chrome", "right"), ("safari", "right"),
    ],
}


def apply_template(name, replace=False):
    """把内置模板导入用户规则列表；replace=True 时先清空现有规则。

    返回 (新增条数, 导入后总条数)；模板名不存在时返回 (0, 现有条数)。
    """
    tpl = TEMPLATES.get(name)
    if not tpl:
        return 0, len(list_rules())
    items = [] if replace else list_rules()
    for pattern, zone in tpl:
        items.append({
            "enabled": True,
            "field": "owner",
            "pattern": pattern,
            "regex": False,
            "monitor": None,   # 不换屏：按窗口所在屏分区
            "zone": zone,
        })
    save_rules(items)
    return len(tpl), len(items)


# ---------- 从当前窗口布局学习规则 ----------
def _guess_zone(win, mon):
    """按窗口在显示器工作区内的相对位置推断位置预设。"""
    x, y = win.get("x"), win.get("y")
    w, h = win.get("w") or 0, win.get("h") or 0
    if x is None or y is None or w <= 0 or h <= 0:
        return "full"
    ww = max(mon.work_width, 1)
    wh = max(mon.work_height, 1)
    rx = (x - mon.work_left) / ww
    ry = (y - mon.work_top) / wh
    rw = w / ww
    rh = h / wh
    tol = 0.12
    if rw <= 0.58 and rx <= tol:
        return "left"
    if rw <= 0.58 and rx + rw >= 1 - tol:
        return "right"
    if rh <= 0.58 and ry <= tol:
        return "top"
    if rh <= 0.58 and ry + rh >= 1 - tol:
        return "bottom"
    return "full"


def learn_from_windows(monitors, wins):
    """从当前窗口布局反推一组规则（同一应用名只取一次）。

    返回规则列表（**不持久化**），字段与规则引擎一致；`monitor` 取该窗口所在屏，
    因此规则会把它钉回原来的屏，`zone` 按当前位置推断。
    """
    learned = []
    seen = set()
    for win in wins or []:
        owner = (win.get("owner") or "").strip()
        if not owner:
            continue
        key = owner.lower()
        if key in seen:
            continue
        mon = _monitor_of_window(monitors, win)
        if mon is None:
            continue
        seen.add(key)
        learned.append({
            "enabled": True,
            "field": "owner",
            "pattern": owner,
            "regex": False,
            "monitor": mon.device_path,
            "zone": _guess_zone(win, mon),
        })
    return learned


def add_rules(new_rules):
    """批量追加规则，跳过与现有规则完全重复的条目。返回 (新增条数, 总条数)。"""
    items = list_rules()
    added = 0
    for r in new_rules or []:
        dup = any(
            it.get("field") == r.get("field")
            and (it.get("pattern") or "").lower() == (r.get("pattern") or "").lower()
            and it.get("monitor") == r.get("monitor")
            and it.get("zone") == r.get("zone")
            for it in items
        )
        if not dup:
            items.append(dict(r))
            added += 1
    save_rules(items)
    return added, len(items)
