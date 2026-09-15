"""情景模式（F2）：按"显示器组合签名"绑定壁纸方案 + 窗口布局，插拔时自动套用。

签名仅由各屏分辨率的**排序集合**构成（与位置、连接顺序、系统屏 ID 无关），
因此同一组显示器无论摆在不同位置、还是重启/重连后系统屏 ID 变化，都能稳定匹配。
情景数据持久化在 settings.json 的 "scenarios" 键下。
"""
import datetime

import settings

_KEY = "scenarios"


def monitors_signature(monitors):
    """由显示器列表生成组合签名，如 '1920x1080|2560x1440'；无显示器返回 ''。"""
    if not monitors:
        return ""
    parts = sorted(f"{int(m.width)}x{int(m.height)}" for m in monitors)
    return "|".join(parts)


def describe(signature):
    """把签名转成人类可读摘要，如 '2 屏：1920x1080 + 2560x1440'。"""
    if not signature:
        return "（无显示器）"
    parts = signature.split("|")
    return f"{len(parts)} 屏：" + " + ".join(parts)


def list_scenarios():
    """返回 {signature: {"name", "layout", "profile", "auto_apply"}}。"""
    return dict(settings.load().get(_KEY) or {})


def get(signature):
    """返回指定签名的情景，不存在返回 None。"""
    return list_scenarios().get(signature)


def bind(signature, layout=None, profile=None, name=None, auto_apply=True):
    """把 (壁纸方案, 窗口布局) 绑定到签名；已存在则覆盖。返回该情景。"""
    data = list_scenarios()
    data[signature] = {
        "name": (name or "").strip() or describe(signature),
        "layout": layout or None,
        "profile": profile or None,
        "auto_apply": bool(auto_apply),
    }
    _save(data)
    return data[signature]


def unbind(signature):
    """删除某签名的情景；不存在时静默忽略。"""
    data = list_scenarios()
    if signature in data:
        del data[signature]
        _save(data)


def match(monitors):
    """返回 (signature, scenario_or_None)：当前显示器组合是否有绑定情景。"""
    sig = monitors_signature(monitors)
    return sig, list_scenarios().get(sig)


def _save(data):
    s = settings.load()
    s[_KEY] = data
    settings.save(s)


# ---------- 定时套用：到点自动套用指定情景（每天一次）----------
_TIME_KEY = "scenario_time_triggers"


def list_time_triggers():
    """返回触发器列表：[{"signature", "time": "HH:MM", "enabled", "last_fired"}]。"""
    return list(settings.load().get(_TIME_KEY) or [])


def add_time_trigger(signature, hhmm):
    """添加定时触发（同一情景同一时间不重复）。返回 (是否新增, 列表)。"""
    hhmm = (hhmm or "").strip()
    if not signature or not _valid_hhmm(hhmm):
        return False, list_time_triggers()
    data = settings.load()
    items = list(data.get(_TIME_KEY) or [])
    for it in items:
        if it.get("signature") == signature and it.get("time") == hhmm:
            return False, items
    items.append({"signature": signature, "time": hhmm, "enabled": True,
                  "last_fired": ""})
    data[_TIME_KEY] = items
    settings.save(data)
    return True, items


def remove_time_trigger(index):
    """按索引删除触发器；返回删除后的列表。"""
    data = settings.load()
    items = list(data.get(_TIME_KEY) or [])
    if 0 <= index < len(items):
        del items[index]
        data[_TIME_KEY] = items
        settings.save(data)
    return items


def toggle_time_trigger(index):
    """启用/停用某个触发器；返回更新后的列表。"""
    data = settings.load()
    items = list(data.get(_TIME_KEY) or [])
    if 0 <= index < len(items):
        items[index]["enabled"] = not items[index].get("enabled", True)
        data[_TIME_KEY] = items
        settings.save(data)
    return items


def due_time_triggers(now=None):
    """返回当前分钟应触发、且当天尚未触发过的情景签名列表（并立即标记为已触发）。

    由 UI 主线程轮询调用（约 1.5s 一次），因此"到点"精度在分钟级。
    """
    now = now or datetime.datetime.now()
    hhmm = f"{now.hour:02d}:{now.minute:02d}"
    today = now.strftime("%Y-%m-%d")
    data = settings.load()
    items = list(data.get(_TIME_KEY) or [])
    due, changed = [], False
    for it in items:
        if not it.get("enabled", True) or it.get("time") != hhmm:
            continue
        if it.get("last_fired") == today:
            continue
        it["last_fired"] = today
        changed = True
        sig = it.get("signature")
        if sig:
            due.append(sig)
    if changed:
        data[_TIME_KEY] = items
        settings.save(data)
    return due


def _valid_hhmm(value):
    """校验 HH:MM 格式。"""
    try:
        h, m = str(value).split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:  # noqa: BLE001
        return False
