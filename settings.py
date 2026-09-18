"""轻量设置存储（单个 JSON 文件，与 profiles.json 同目录）。

用于持久化用户偏好（如全局快捷键的修饰键组合），读写失败时静默降级，
不影响主流程。
"""
import copy
import json
import os
import tempfile

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

# 读缓存：(mtime_ns, size) -> 已解析数据。
# UI 轮询（1.5s）里频繁调用 load()，缓存可避免反复读盘/解析；用 (mtime_ns, size)
# 作失效依据，能感知托盘子进程等其它进程对文件的写入。
_cache = {"stat": None, "data": None}


def _file_stat():
    try:
        st = os.stat(_PATH)
        return (st.st_mtime_ns, st.st_size)
    except Exception:  # noqa: BLE001
        return None


def load():
    """返回设置字典；文件不存在或解析失败时返回空字典。

    带 (mtime_ns, size) 缓存，**始终返回深拷贝**：调用方常直接原地修改返回值
    (load → 改 → save)，若共享同一对象会污染缓存，导致未保存的改动提前生效。
    """
    st = _file_stat()
    if st is not None and st == _cache["stat"] and _cache["data"] is not None:
        return copy.deepcopy(_cache["data"])
    data = {}
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:  # noqa: BLE001
        data = {}
    _cache["stat"] = st
    _cache["data"] = data
    return copy.deepcopy(data)


def save(data):
    """原子写入设置字典：先写临时文件再替换，避免半写导致配置损坏。

    失败时回退为直接写；两种情况都静默忽略异常（不影响主流程）。
    """
    try:
        d = os.path.dirname(_PATH)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PATH)
    except Exception:  # noqa: BLE001
        try:
            with open(_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            return
    _cache["stat"] = _file_stat()
    _cache["data"] = copy.deepcopy(data)


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
