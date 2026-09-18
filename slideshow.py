"""壁纸幻灯片：按间隔自动轮换一组图片（零第三方依赖）。

配置持久化在 settings.json 的 "slideshow" 键：
    {
      "enabled": bool,          # 是否启用自动轮换
      "interval": int,          # 切换间隔（秒），下限 _MIN_INTERVAL
      "position": str,          # 填充方式（fill/fit/...，与壁纸区一致）
      "shuffle": bool,          # 随机切换
      "items": [str, ...],      # 图片绝对路径
      "index": int,             # 当前序号
    }

本模块只负责"选下一张 + 持久化状态"；真正设置壁纸由 UI 调 backend.wallpaper，
保持平台解耦（Windows / macOS 共用）。
"""
import os
import random

import settings

_KEY = "slideshow"
_MIN_INTERVAL = 10
_DEFAULT_INTERVAL = 300


def _normalize(raw):
    """把任意输入收敛成合法配置结构（补齐缺省值、过滤非法项）。"""
    if not isinstance(raw, dict):
        raw = {}
    try:
        interval = int(raw.get("interval", _DEFAULT_INTERVAL) or _DEFAULT_INTERVAL)
    except Exception:  # noqa: BLE001
        interval = _DEFAULT_INTERVAL
    raw_items = raw.get("items")
    # 必须校验类型：字符串是可迭代的，若直接遍历 "abc" 会被拆成单字符
    items = [p for p in (raw_items if isinstance(raw_items, list) else [])
             if isinstance(p, str) and p.strip()]
    try:
        index = int(raw.get("index", 0) or 0)
    except Exception:  # noqa: BLE001
        index = 0
    return {
        "enabled": bool(raw.get("enabled", False)),
        "interval": max(_MIN_INTERVAL, interval),
        "position": raw.get("position") or "fill",
        "shuffle": bool(raw.get("shuffle", False)),
        "items": items,
        "index": index % len(items) if items else 0,
    }


def get_config():
    """读取并规范化配置。"""
    return _normalize(settings.load().get(_KEY))


def _save(cfg):
    s = settings.load()
    s[_KEY] = cfg
    settings.save(s)


def set_config(**kwargs):
    """部分更新配置（None 值忽略）。返回更新后的配置。"""
    cfg = get_config()
    for k, v in kwargs.items():
        if v is not None:
            cfg[k] = v
    cfg = _normalize(cfg)
    _save(cfg)
    return cfg


def add_items(paths):
    """追加图片（去重、仅保留存在的文件）。返回更新后的配置。"""
    cfg = get_config()
    known = set(cfg["items"])
    for p in paths or []:
        if not isinstance(p, str) or not p.strip():
            continue
        ap = os.path.abspath(p)
        if ap not in known and os.path.isfile(ap):
            cfg["items"].append(ap)
            known.add(ap)
    _save(cfg)
    return cfg


def remove_item(index):
    """按索引移除图片。返回更新后的配置。"""
    cfg = get_config()
    if 0 <= index < len(cfg["items"]):
        del cfg["items"][index]
        cfg["index"] = 0
        _save(cfg)
    return cfg


def clear_items():
    """清空图片列表。返回更新后的配置。"""
    cfg = get_config()
    cfg["items"] = []
    cfg["index"] = 0
    _save(cfg)
    return cfg


def next_path():
    """推进到下一张并返回其路径；列表为空返回 None。

    仅更新索引与持久化，不实际设置壁纸（由调用方执行）。
    """
    cfg = get_config()
    items = cfg["items"]
    if not items:
        return None
    if cfg["shuffle"] and len(items) > 1:
        idx = random.randrange(len(items))
    else:
        idx = (cfg["index"] + 1) % len(items)
    cfg["index"] = idx
    _save(cfg)
    return items[idx]


def current_path():
    """返回当前图片路径；列表为空返回 None。"""
    cfg = get_config()
    items = cfg["items"]
    if not items:
        return None
    return items[cfg["index"] % len(items)]
