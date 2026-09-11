"""窗口布局方案的保存与加载（单个 JSON 文件，按名称索引）。

每个方案记录若干窗口的几何信息（位置/大小）与来源标识（"应用::窗口"），
还原时按标识匹配当前窗口并调用 backend.set_window_rect 复位。
"""
import json
import os
from datetime import datetime

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layouts.json")


def list_layouts():
    """返回方案名称列表（按名称排序）。"""
    return sorted(_load().keys())


def load_layout(name):
    """返回指定方案的完整数据，不存在时返回 None。"""
    return _load().get(name)


def save_layout(name, windows):
    """保存（覆盖）一个名为 name 的布局方案。

    windows: [{"hwnd", "owner", "name", "x", "y", "w", "h"}, ...]
    """
    data = _load()
    data[name] = {
        "windows": windows,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save(data)


def delete_layout(name):
    """删除指定方案，不存在时静默忽略。"""
    data = _load()
    if name in data:
        del data[name]
        _save(data)


def _load():
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save(data):
    try:
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
