"""轻量设置存储（单个 JSON 文件）。

存放位置：
- 源码运行：与代码同目录（仓库内，便于开发调试，向后兼容旧行为）；
- 打包运行（PyInstaller frozen）：__file__ 指向每次启动都会清空的临时解包
  目录 _MEIPASS，直接写在那里配置会丢，因此改用用户数据目录
  （Windows %APPDATA%\MultiMonManager，macOS ~/Library/Application Support/MultiMonManager）。

用于持久化用户偏好（全局快捷键开关/修饰键、热插拔开关、启动选项等），
读写失败时静默降级，不影响主流程。
"""
import json
import os
import sys


def data_dir():
    """用户配置目录（settings.json / layouts.json 等的存放位置）。"""
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library",
                                "Application Support")
        else:
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "MultiMonManager")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:  # noqa: BLE001
            d = os.path.expanduser("~")
        return d
    return os.path.dirname(os.path.abspath(__file__))


_PATH = os.path.join(data_dir(), "settings.json")


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
