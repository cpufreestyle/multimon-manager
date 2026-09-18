"""配置备份与恢复。

把三份本地配置打包成一个 JSON，方便换机、重装或回滚：

- `settings.json`          偏好（快捷键开关/修饰键、热插拔、壁纸轮换等）
- `layouts.json`           窗口布局方案
- `profiles.json`          壁纸方案

导入采用「合并」语义：同名方案覆盖，其它保留，不会清掉本机既有配置。
"""
import json
from datetime import datetime

import layouts
import profiles
import settings

APP = "MultiMonManager"
FORMAT = 1

# 允许随备份迁移的偏好键（不含 last_* 之类会随本机状态漂移的运行时记录）
_MIGRATABLE_KEYS = (
    "hk_enabled", "hk_mods",
    "watch_displays", "watch_auto_apply",
    "rotate_dir", "rotate_interval_min",
)


def export_all():
    """返回一份完整配置快照（可直接 json.dump）。"""
    return {
        "app": APP,
        "format": FORMAT,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "settings": settings.load(),
        "layouts": layouts.export_all(),
        "wallpaper_profiles": profiles.export_all(),
    }


def collect_and_write(path):
    """把当前配置写入 path，返回 (布局数, 壁纸方案数)。"""
    data = export_all()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return len(data["layouts"]), len(data["wallpaper_profiles"])


def read_and_restore(path):
    """读取备份并合并回当前配置。

    返回 (写入的偏好项数, 布局数, 壁纸方案数)；文件不是本程序的备份、
    或来自更新格式时抛 ValueError。
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("app") != APP:
        raise ValueError("这不是本程序导出的配置文件")
    try:
        fmt = int(data.get("format", 1))
    except (TypeError, ValueError):
        fmt = 1
    if fmt > FORMAT:
        raise ValueError(f"备份格式（v{fmt}）比当前程序支持的（v{FORMAT}）更新，"
                         "请先升级本程序")

    n_settings = 0
    incoming = data.get("settings")
    if isinstance(incoming, dict):
        cur = settings.load()
        for key in _MIGRATABLE_KEYS:
            if key in incoming:
                cur[key] = incoming[key]
                n_settings += 1
        settings.save(cur)

    n_layouts = layouts.import_all(data.get("layouts") or {}, merge=True)
    n_profiles = profiles.import_all(data.get("wallpaper_profiles") or {}, merge=True)
    return n_settings, n_layouts, n_profiles
