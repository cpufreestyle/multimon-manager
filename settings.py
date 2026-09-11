"""轻量设置存储（单个 JSON 文件，与 profiles.json 同目录）。

用于持久化用户偏好（如全局快捷键的修饰键组合），读写失败时静默降级，
不影响主流程。
"""
import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def load():
    """返回设置字典；文件不存在或解析失败时返回空字典。"""
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save(data):
    """覆盖写入设置字典；失败时静默忽略。"""
    try:
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
