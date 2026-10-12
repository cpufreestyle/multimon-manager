"""窗口暂存（复刻 Rectangle Pro 的 Stash）。

把暂时不用的窗口搬到屏幕外（工作区上方一点点），之后按同一个快捷键循环取回。
比最小化好：窗口仍是可见的（停在屏幕顶边，看得见一条），取回时位置尺寸原样
恢复，不需要 App 支持「取消最小化到原位」。

两条实测约束（真机 + Rectangle Pro 文档都提到）：
   1. macOS 不允许窗口越过工作区顶边，所以 stash 位置只能贴到 work_top，
      不能让窗口真正消失——停在顶边露一条，正是「暂存」的可视线索。
   2. 循环取回用 FIFO：第一个 stash 的窗口第一个回来，符合直觉。
"""
import json
import os

import settings

# 暂存时窗口露在屏幕外的比例：0.06 表示只留 6% 的高度在屏内可见。
_VISIBLE_FRAC = 0.06
_MAX_STASH = 40


def _path():
    return os.path.join(settings.data_dir(), "stash.json")


def _load():
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(items):
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def list_stashed():
    """返回暂存列表：[{"hwnd", "owner", "x", "y", "w", "h"}, ...]，先进先出。"""
    return _load()


def count():
    return len(_load())


def stash_rect(mon):
    """窗口暂存后应落的位置：贴工作区顶边，只留一小条可见。"""
    wl, wt, ww, wh = mon.work_rect
    vis = max(1, int(round(wh * _VISIBLE_FRAC)))
    return (wl, wt, ww, vis)


def push(item):
    """登记一个暂存窗口；超过上限时丢掉最早的一条（返回被丢的）。"""
    items = _load()
    dropped = None
    items.append(dict(item))
    while len(items) > _MAX_STASH:
        dropped = items.pop(0)
    _save(items)
    return dropped


def pop():
    """取出最早暂存的窗口（FIFO）；空列表返回 None。"""
    items = _load()
    if not items:
        return None
    item = items.pop(0)
    _save(items)
    return item


def clear():
    _save([])


def export_all():
    return _load()


def import_all(items):
    if isinstance(items, list):
        _save([dict(i) for i in items if isinstance(i, dict)])
        return True
    return False
