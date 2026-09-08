"""多显示器壁纸控制（macOS 实现，零第三方依赖）。

macOS 桌面壁纸通过 System Events 的 AppleScript 设置：
  tell application "System Events" to tell desktop N to set picture to POSIX file "..."

- 统一单图：遍历 every desktop 设同一张图。
- 每屏不同：按 enum_monitors() 的顺序给 desktop 1..N 分别设图（顺序与界面列表一致）。

不再用 desktoppicture.db 写"图片路径"：其表结构与显示器顺序并非简单对应，
按行写入极易把图片错配到错误的屏（旧实现即此 bug）。图片统一由 AppleScript 设置。

填充方式（fill/fit/stretch/center/tile）AppleScript 无法设置，仅写入
desktoppicture.db 的 Placement 键，采用自适应探测：仅当表结构与预期一致时才写，
否则记录告警并跳过，绝不破坏原有数据。写入后 killall Dock 使设置生效。
"""
import logging
import os
import sqlite3
import subprocess
import time

import monitors_mac as monitors

logger = logging.getLogger(__name__)

# 与 Windows 端保持同名，便于上层复用
POSITION = {
    "center": 0,
    "tile": 1,
    "stretch": 2,
    "fit": 3,
    "fill": 4,
    "span": 5,
}

_DB_PATH = os.path.expanduser("~/Library/Application Support/Dock/desktoppicture.db")

# macOS desktoppicture.db 中 Placement 键的取值
_PLACEMENT = {
    "fill": "FillScreen",
    "fit": "FitToScreen",
    "stretch": "StretchToFillScreen",
    "center": "Centered",
    "tile": "Tiled",
    "span": "FillScreen",
}


def _table_columns(con, table):
    """返回表的列名列表；表不存在时返回空列表。"""
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:  # noqa: BLE001
        return []


def _apply_placement(position="fill"):
    """best-effort 设置填充方式：写 desktoppicture.db 的 Placement 键。

    自适应探测：不同 macOS 版本表结构有差异，仅当 data 表含 key/value 列时才写；
    若支持 picture_id 则逐显示器写入，否则写全局。任何异常都只告警不抛出。
    """
    placement = _PLACEMENT.get(position)
    if not placement:
        return False
    if not os.path.exists(_DB_PATH):
        logger.info("desktoppicture.db 尚不存在，跳过填充方式设置")
        return False

    con = None
    try:
        con = sqlite3.connect(_DB_PATH)
        cols = _table_columns(con, "data")
        if not {"key", "value"}.issubset(cols):
            logger.warning("desktoppicture.db 结构非预期（data 列：%s），跳过填充方式设置", cols)
            return False

        if "picture_id" in cols:
            rows = con.execute("SELECT DISTINCT picture_id FROM data").fetchall()
            picture_ids = [r[0] for r in rows] or [0]
        else:
            picture_ids = [None]

        for pid in picture_ids:
            if pid is None:
                cur = con.execute("UPDATE data SET value=? WHERE key=?", (placement, "Placement"))
                if cur.rowcount == 0:
                    con.execute("INSERT INTO data (key, value) VALUES (?,?)",
                                ("Placement", placement))
            else:
                cur = con.execute(
                    "UPDATE data SET value=? WHERE key=? AND picture_id=?",
                    (placement, "Placement", pid),
                )
                if cur.rowcount == 0:
                    con.execute(
                        "INSERT INTO data (key, value, picture_id) VALUES (?,?,?)",
                        ("Placement", placement, pid),
                    )
        con.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("设置填充方式失败: %s", e)
        return False
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass


def _finish(position="fill"):
    """收尾：确保数据库存在 → 写填充方式 → 重启 Dock 统一生效。"""
    if not os.path.exists(_DB_PATH):
        # 首次运行时 Dock 可能尚未建库，先重启一次让其创建，再写 Placement
        _kill_dock()
        time.sleep(1.5)
    _apply_placement(position)
    _kill_dock()


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        logger.warning("执行命令失败 %s: %s", cmd, e)
        return None


def available():
    return True


def _esc(path):
    """转义 AppleScript 双引号字符串中的反斜杠与双引号，避免路径断句。"""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _kill_dock():
    _run(["killall", "Dock"])


def set_single_all(image_path, position="fill"):
    """设置所有显示器的壁纸为同一张图。"""
    script = (
        'tell application "System Events"\n'
        '  repeat with d in every desktop\n'
        f'    set picture of d to POSIX file "{_esc(image_path)}"\n'
        '  end repeat\n'
        '  repeat with d in every desktop\n'
        '    set picture rotation of d to 0\n'
        '  end repeat\n'
        'end tell\n'
    )
    r = _run(["osascript", "-e", script])
    if r is None or r.returncode != 0:
        logger.warning("设置统一壁纸失败: %s", (r.stderr if r else "无返回"))
        return False
    _finish(position)
    return True


def set_per_monitor(mapping, position="fill"):
    """mapping: {device_path(str CG id): image_path}。

    按 enum_monitors() 的顺序，把每张显示器对应的图片写入 desktop 1..N，
    保证界面里第 N 个显示器拿到第 N 张图，避免错配。
    """
    if not mapping:
        return False
    mons = monitors.enum_monitors()
    if not mons:
        logger.warning("未检测到显示器，无法设置每屏壁纸")
        return False

    lines = []
    for i, m in enumerate(mons):
        p = mapping.get(m.device_path)
        if p:
            lines.append(f'  set picture of desktop {i + 1} to POSIX file "{_esc(p)}"')
    if not lines:
        return False

    script = 'tell application "System Events"\n' + "\n".join(lines) + '\nend tell\n'
    r = _run(["osascript", "-e", script])
    if r is None or r.returncode != 0:
        logger.warning("设置每屏壁纸失败: %s", (r.stderr if r else "无返回"))
        # 回退：退而求其次，统一用第一张图
        first = next(iter(mapping.values()))
        set_single_all(first, position)
        return False
    _finish(position)
    return True


def apply_per_monitor(mapping, position="fill"):
    """统一入口：mapping: {device_path: image_path}。"""
    return set_per_monitor(mapping, position)


def apply_single(image_path, position="fill"):
    return set_single_all(image_path, position)


def get_desktop_wallpaper():
    # macOS 不维护 COM 对象，返回一个轻量兼容对象
    class _Stub:
        def available(self):
            return True

    return _Stub()


if __name__ == "__main__":
    print("monitors:", [m.device_name for m in monitors.enum_monitors()])
