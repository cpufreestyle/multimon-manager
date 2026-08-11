"""配置（壁纸方案）的保存与加载。"""
import json
import os
import sys
import tempfile

import backend

# macOS 用 ~/Library/Application Support，Windows 用 %APPDATA%
if sys.platform == "darwin":
    APP_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "MultiMonManager")
else:
    APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MultiMonManager")
PROFILES_FILE = os.path.join(APP_DIR, "profiles.json")


def _ensure_dir():
    os.makedirs(APP_DIR, exist_ok=True)


def _atomic_write(data):
    """原子写入 JSON：先写临时文件，再 rename 覆盖，防止并发损坏。"""
    _ensure_dir()
    tmp = os.path.join(APP_DIR, ".profiles.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PROFILES_FILE)  # 跨平台原子替换
    except Exception:  # noqa: BLE001
        # 回退方案：直接写入
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def load_profiles():
    if not os.path.exists(PROFILES_FILE):
        return {}
    try:
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("profiles", {})
    except Exception:  # noqa: BLE001
        return {}


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


def apply_profile(name):
    profiles = load_profiles()
    if name not in profiles:
        return False
    p = profiles[name]
    return backend.wallpaper.apply_per_monitor(p.get("mapping", {}), p.get("position", "fill"))


if __name__ == "__main__":
    print("profiles dir:", APP_DIR)
    print("existing:", list(load_profiles().keys()))
