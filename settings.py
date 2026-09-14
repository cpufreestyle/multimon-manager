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


# ── 配置导入 / 导出（F8）──
# 与本程序相关的持久化配置文件（均位于本模块同目录）。
_CONFIG_FILES = ("settings.json", "profiles.json", "layouts.json")
import datetime  # noqa: E402  (放在文件尾以与上方 import 顺序一致)
import shutil  # noqa: E402
import zipfile  # noqa: E402


def export_all(dest_zip):
    """把三份配置文件打包成一个 zip。返回目标路径；无文件可导出时返回 None。"""
    src = os.path.dirname(os.path.abspath(__file__))
    found = [f for f in _CONFIG_FILES if os.path.exists(os.path.join(src, f))]
    if not found:
        return None
    try:
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for f in found:
                z.write(os.path.join(src, f), f)
    except Exception:  # noqa: BLE001
        return None
    return dest_zip


def import_all(src_zip, backup_dir=None):
    """从 zip 覆盖导入三份配置文件；导入前自动备份当前配置。

    返回 (ok, msg)。损坏或不含可识别文件的包会失败且不破坏现有配置。
    """
    if not zipfile.is_zipfile(src_zip):
        return False, "文件不是有效的配置文件包（.zip）"
    src = os.path.dirname(os.path.abspath(__file__))
    try:
        with zipfile.ZipFile(src_zip) as z:
            names = z.namelist()
            if not any(n in _CONFIG_FILES for n in names):
                return False, "压缩包内没有可识别的配置文件"
            if backup_dir is None:
                backup_dir = os.path.join(src, "_settings_backup")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            for f in _CONFIG_FILES:
                p = os.path.join(src, f)
                if os.path.exists(p):
                    shutil.copy2(p, os.path.join(backup_dir, f"{stamp}_{f}"))
            for n in names:
                if n in _CONFIG_FILES:
                    z.extract(n, src)
    except Exception as e:  # noqa: BLE001
        return False, f"导入失败：{e}"
    return True, "已导入；原配置已自动备份到 _settings_backup/"
