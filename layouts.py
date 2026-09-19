"""窗口布局方案的保存与加载（单个 JSON 文件，按名称索引）。

每个方案记录若干窗口的几何信息（位置/大小）与来源标识（"应用::窗口"），
还原时按标识匹配当前窗口并调用 backend.set_window_rect 复位。

还会记录保存时的**显示器配置指纹**：显示器数量/分辨率/相对位置变化后，
按绝对坐标还原必然错位，应用前据此提示用户。
"""
import json
import os
from datetime import datetime

import settings

# 源码运行在仓库内；打包运行用用户数据目录（_MEIPASS 是临时目录，会丢配置）
_PATH = os.path.join(settings.data_dir(), "layouts.json")


def list_layouts():
    """返回方案名称列表（按名称排序）。"""
    return sorted(_load().keys())


def load_layout(name):
    """返回指定方案的完整数据，不存在时返回 None。"""
    return _load().get(name)


def save_layout(name, windows, monitor_sig=None):
    """保存（覆盖）一个名为 name 的布局方案。

    windows: [{"hwnd", "owner", "name", "x", "y", "w", "h"}, ...]
    monitor_sig: 保存时的显示器配置指纹（可迭代对象，可为 None）
    """
    data = _load()
    data[name] = {
        "windows": windows,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "monitor_sig": [list(item) for item in monitor_sig] if monitor_sig else None,
    }
    _save(data)


def monitor_signature(name):
    """返回方案保存时的显示器指纹（list of list）；老方案没有该字段时为 None。"""
    layout = _load().get(name)
    if not isinstance(layout, dict):
        return None
    sig = layout.get("monitor_sig")
    if isinstance(sig, list) and sig:
        return sig
    return None


def delete_layout(name):
    """删除指定方案，不存在时静默忽略。"""
    data = _load()
    if name in data:
        del data[name]
        _save(data)


def export_all():
    """返回全部布局方案（供配置备份 / 还原使用）。"""
    return _load()


def import_all(layouts, merge=True):
    """导入布局方案；merge=True 时同名覆盖、其它保留。返回写入条数。"""
    if not isinstance(layouts, dict):
        return 0
    target = _load() if merge else {}
    n = 0
    for name, item in layouts.items():
        if isinstance(item, dict) and isinstance(item.get("windows"), list):
            target[name] = item
            n += 1
    _save(target)
    return n


def _load():
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(data):
    try:
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
