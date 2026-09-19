"""配置（壁纸方案）的保存与加载。

存放位置与 settings / layouts 保持一致，统一由 `settings.data_dir()` 决定：
- 源码运行：与代码同目录（仓库内，便于调试）；
- 打包运行：用户数据目录（Windows `%APPDATA%\\MultiMonManager`，
  macOS `~/Library/Application Support/MultiMonManager`）。

历史版本把壁纸方案固定写在 `%APPDATA%\\MultiMonManager`，与 settings/layouts
分居两处。这里保留一次「旧位置回退读取」：新位置没有文件时读旧文件，一旦保存
即迁移到新位置，老用户的方案不会丢。
"""
import json
import os
import sys

import backend
import settings

APP_DIR = settings.data_dir()
PROFILES_FILE = os.path.join(APP_DIR, "profiles.json")


def _legacy_file():
    """返回旧版本固定使用的位置（与当前一致时返回 None）。"""
    if sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    old = os.path.join(base, "MultiMonManager", "profiles.json")
    if os.path.abspath(old) == os.path.abspath(PROFILES_FILE):
        return None
    return old


def _read_file():
    """读取配置文件内容；优先新位置，缺失时回退旧位置。"""
    for path in (PROFILES_FILE, _legacy_file()):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            continue
    return {}


def _atomic_write(data):
    """原子写入 JSON：先写临时文件，再 replace 覆盖，防止并发损坏。"""
    try:
        os.makedirs(APP_DIR, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    tmp = os.path.join(APP_DIR, ".profiles.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PROFILES_FILE)  # 跨平台原子替换
    except Exception:  # noqa: BLE001
        # 回退方案：直接写入
        try:
            with open(PROFILES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass


def load_profiles():
    data = _read_file()
    profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
    return profiles if isinstance(profiles, dict) else {}


def save_profile(name, mapping, position="fill"):
    """mapping: {device_path: image_path}。"""
    profiles = load_profiles()
    profiles[name] = {"mapping": mapping, "position": position}
    _atomic_write({"profiles": profiles})


def delete_profile(name):
    profiles = load_profiles()
    if name in profiles:
        del profiles[name]
        _atomic_write({"profiles": profiles})


def export_all():
    """返回全部壁纸方案（供配置备份 / 还原使用）。"""
    return load_profiles()


def import_all(profiles, merge=True):
    """导入壁纸方案；merge=True 时同名覆盖、其它保留。返回写入条数。"""
    if not isinstance(profiles, dict):
        return 0
    target = load_profiles() if merge else {}
    n = 0
    for name, item in profiles.items():
        if isinstance(item, dict) and isinstance(item.get("mapping", {}), dict):
            target[name] = item
            n += 1
    _atomic_write({"profiles": target})
    return n


def apply_profile(name):
    profiles = load_profiles()
    if name not in profiles:
        return False
    p = profiles[name]
    return backend.wallpaper.apply_per_monitor(p.get("mapping", {}), p.get("position", "fill"))


if __name__ == "__main__":
    print("profiles file:", PROFILES_FILE)
    print("existing:", list(load_profiles().keys()))
