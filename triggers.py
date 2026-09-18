"""窗口事件触发器（README「可作练手扩展的方向」之一）。

与 F5 规则引擎的分工（避免重复造轮子）：
- **规则**：作用于「新出现的窗口」，负责把它归位到指定屏 / 分区；
- **触发器**：作用于「被移动的窗口」（也可选新出现），可执行更丰富的动作
  —— 吸附到分区、移到指定显示器、套用情景、应用窗口规则。

匹配逻辑直接复用 `rules.match_window`（字段兼容：`field` / `pattern` / `regex`）。
"""
import rules
import settings

_KEY = "window_triggers"

EVENTS = ("moved", "created")
EVENT_LABELS = {"moved": "被移动", "created": "新出现"}
ACTIONS = ("snap", "monitor", "scenario", "rules")
ACTION_LABELS = {
    "snap": "吸附到分区",
    "monitor": "移到显示器",
    "scenario": "套用情景",
    "rules": "应用窗口规则",
}

# 位置变化超过该像素才算「被移动」：避免窗口被系统微调 1~2px 就误触发
MOVE_THRESHOLD = 40
# 同一窗口 + 同一触发器的冷却秒数：动作本身会移动窗口，不冷却会自我反复触发
COOLDOWN_SEC = 10


def list_triggers():
    return list(settings.load().get(_KEY) or [])


def save_triggers(items):
    s = settings.load()
    s[_KEY] = list(items)
    settings.save(s)


def add_trigger(trigger):
    items = list_triggers()
    items.append(trigger)
    save_triggers(items)
    return len(items)


def delete_trigger(index):
    items = list_triggers()
    if 0 <= index < len(items):
        del items[index]
        save_triggers(items)
    return items


def toggle_trigger(index):
    items = list_triggers()
    if 0 <= index < len(items):
        items[index]["enabled"] = not items[index].get("enabled", True)
        save_triggers(items)
    return items


def any_enabled(items=None):
    """是否有启用中的触发器（轮询据此决定要不要做检测，省掉无谓开销）。"""
    return any(t.get("enabled", True)
               for t in (list_triggers() if items is None else items))


def match(trigger, win):
    """窗口是否命中触发器（复用规则引擎的匹配实现）。"""
    return rules.match_window(trigger, win)


def monitor_of_window(monitors, win):
    """窗口当前所在显示器（按中心点判断）；不在任何屏内时返回 None。"""
    return rules._monitor_of_window(monitors, win)


def is_moved(prev, cur, threshold=MOVE_THRESHOLD):
    """位置是否发生显著变化；prev / cur 均为 (x, y, w, h)。

    prev 为空（首次见到该窗口）时返回 False —— 那属于 created 而非 moved。
    只比较左上角坐标，避免缩放被误判为移动。
    """
    if not prev or not cur:
        return False
    try:
        return abs(prev[0] - cur[0]) > threshold or abs(prev[1] - cur[1]) > threshold
    except Exception:  # noqa: BLE001
        return False
