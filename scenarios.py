"""情景模式（F2）：按"显示器组合签名"绑定壁纸方案 + 窗口布局，插拔时自动套用。

签名仅由各屏分辨率的**排序集合**构成（与位置、连接顺序、系统屏 ID 无关），
因此同一组显示器无论摆在不同位置、还是重启/重连后系统屏 ID 变化，都能稳定匹配。
情景数据持久化在 settings.json 的 "scenarios" 键下。
"""
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
