"""窗口布局方案的保存与加载（单个 JSON 文件，按名称索引）。

每个方案记录若干窗口的几何信息（位置/大小）与来源标识（"应用::窗口"），
还原时按标识匹配当前窗口并调用 backend.set_window_rect 复位。

还会记录保存时的**显示器配置指纹**，以及每个窗口在**当时所在屏工作区上的
相对框**（0~1 比例，Moom 同款思路）：屏幕数量或分辨率变化后，不再只按绝对
坐标硬套，而是先按相对框重映射到新屏再钉回工作区——布局可以跨显示器组合
自适应，而不是一换屏就提示“会错位”。
"""
import json
import os
from datetime import datetime

import screen_math
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
    windows = [dict(w) for w in windows]
    for w in windows:
        rel = w.get("rel")
        if not (isinstance(rel, (list, tuple)) and len(rel) == 4):
            w.pop("rel", None)
    data[name] = {
        "windows": windows,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "monitor_sig": [list(item) for item in monitor_sig] if monitor_sig else None,
    }
    _save(data)


def apply_relative_frames(name, monitors):
    """给老布局补算相对框：按窗口矩形当时所在的屏（交集最长）归一化。

    monitors 为当前显示器对象列表（带 left/top/width/height 与
    work_left/work_top/work_width/work_height）。老布局保存时没记 rel，
    在还原前用此函数升级一次并写回，后续还原即可走自适应路径。
    返回 (更新条数, 跳过条数)。
    """
    layout = _load().get(name)
    if not isinstance(layout, dict):
        return (0, 0)
    windows = layout.get("windows")
    if not isinstance(windows, list) or not monitors:
        return (0, 0)
    updated, skipped = 0, 0
    changed = False
    for w in windows:
        if not isinstance(w, dict):
            continue
        rel = w.get("rel")
        if isinstance(rel, (list, tuple)) and len(rel) == 4:
            norm = [float(v) for v in rel]
            if norm != list(rel):
                w["rel"] = norm
                changed = True
            continue
        try:
            wl, wt, ww, wh = screen_math.best_work_for_rect(
                monitors, w["x"], w["y"], w["w"], w["h"])
            w["rel"] = [
                round(v, 6) for v in screen_math.normalize_to_work(
                    w["x"], w["y"], w["w"], w["h"], wl, wt, ww, wh)]
            updated += 1
            changed = True
        except Exception:  # noqa: BLE001
            skipped += 1
    if changed:
        data = _load()
        if isinstance(data.get(name), dict):
            data[name]["windows"] = windows
            _save(data)
    return (updated, skipped)


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
