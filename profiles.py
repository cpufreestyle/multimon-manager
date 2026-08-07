"""配置（壁纸方案）的保存与加载。"""
import json
import os

import backend
import backend as wallpaper

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MultiMonManager")
PROFILES_FILE = os.path.join(APP_DIR, "profiles.json")


def _ensure_dir():
    os.makedirs(APP_DIR, exist_ok=True)


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
    _ensure_dir()
    profiles = load_profiles()
    profiles[name] = {"mapping": mapping, "position": position}
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump({"profiles": profiles}, f, ensure_ascii=False, indent=2)


def delete_profile(name):
    profiles = load_profiles()
    if name in profiles:
        del profiles[name]
        _ensure_dir()
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump({"profiles": profiles}, f, ensure_ascii=False, indent=2)


def apply_profile(name):
    profiles = load_profiles()
    if name not in profiles:
        return False
    p = profiles[name]
    return wallpaper.apply_per_monitor(p.get("mapping", {}), p.get("position", "fill"))


if __name__ == "__main__":
    print("profiles dir:", APP_DIR)
    print("existing:", list(load_profiles().keys()))
