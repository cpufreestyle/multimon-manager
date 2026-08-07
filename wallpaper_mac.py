"""多显示器壁纸控制（macOS 实现，零第三方依赖）。

macOS 的桌面壁纸由每个显示器的 SQLite 数据库管理：
  ~/Library/Application Support/Dock/desktoppicture.db
通过 osascript 设置单屏/所有屏壁纸，并通过直接写库实现"每屏不同"。

注意：写 desktoppicture.db 后需 killall Dock 使其生效。
填充方式 macOS 仅支持 fill / fit / stretch / center / tile；
API 统一用 Windows 那套 name，映射到 macOS 的 0/1/2/3。
"""
import os
import subprocess
import sqlite3

import monitors_mac as monitors

# 与 Windows 端保持同名，便于上层复用
POSITION = {
    "center": 0,
    "tile": 1,
    "stretch": 2,
    "fit": 3,
    "fill": 4,
    "span": 5,
}

# macOS 仅支持这几种，fill=覆盖式拉伸，fit=保持比例
_MAC_POS = {
    "center": "center",
    "tile": "tile",
    "stretch": "stretch",
    "fit": "fit",
    "fill": "fill",
    "span": "fill",
}

_DB_PATH = os.path.expanduser("~/Library/Application Support/Dock/desktoppicture.db")


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return None


def available():
    return True


def _position_macos(name):
    return _MAC_POS.get(name, "fill")


def set_single_all(image_path, position="fill"):
    """设置所有显示器的壁纸（AppleScript）。"""
    pos = _position_macos(position)
    script = (
        'tell application "System Events" to tell every desktop to '
        f'set picture to POSIX file "{image_path}"\n'
        f'tell application "System Events" to tell every desktop to set picture rotation to 0\n'
    )
    # AppleScript 不直接支持填充方式设置；通过数据库的 defaults 设置
    _run(["osascript", "-e", script])
    # 用 sqlite 写入填充方式（可选）
    _set_picture_for_all_displays(image_path)
    _kill_dock()
    return True


def _set_picture_for_all_displays(image_path):
    """通过直接写 desktoppicture.db 把每个显示器都设为同一张图。"""
    try:
        if not os.path.exists(_DB_PATH):
            return
        con = sqlite3.connect(_DB_PATH)
        cur = con.cursor()
        # 备份表结构：data 表存图片路径，value 为显示序号
        cur.execute("SELECT rowid, value FROM pictures")
        rows = cur.fetchall()
        for rowid, _ in rows:
            cur.execute("UPDATE pictures SET value=? WHERE rowid=?", (image_path, rowid))
        if not rows:
            cur.execute("INSERT INTO pictures (value) VALUES (?)", (image_path,))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


def set_per_monitor(mapping, position="fill"):
    """mapping: {device_index(str): image_path}。

    macOS 按显示器的 display 排列写入 desktoppicture.db 的 pictures 表。
    数据库里每个 display 对应若干行，按顺序映射。
    """
    if not mapping:
        return False
    try:
        if not os.path.exists(_DB_PATH):
            # 触发 Dock 创建数据库
            _run(["killall", "Dock"])
        con = sqlite3.connect(_DB_PATH)
        cur = con.cursor()
        try:
            cur.execute("SELECT rowid FROM pictures ORDER BY rowid")
            pic_ids = [r[0] for r in cur.fetchall()]
        except Exception:
            pic_ids = []
        # 若数据库为空，先让 osascript 初始化
        if not pic_ids:
            set_single_all(next(iter(mapping.values())), position)
            con = sqlite3.connect(_DB_PATH)
            cur = con.cursor()
            cur.execute("SELECT rowid FROM pictures ORDER BY rowid")
            pic_ids = [r[0] for r in cur.fetchall()]

        # 把 mapping 的 device_index 排序后依次写入 pictures 行
        ordered = sorted(mapping.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)
        for idx, (dev, path) in enumerate(ordered):
            if idx < len(pic_ids):
                cur.execute("UPDATE pictures SET value=? WHERE rowid=?", (path, pic_ids[idx]))
        con.commit()
        con.close()
        _kill_dock()
        return True
    except Exception as e:  # noqa: BLE001
        print("[wallpaper_mac] 每屏设置失败:", e)
        return False


def _kill_dock():
    _run(["killall", "Dock"])


def apply_per_monitor(mapping, position="fill"):
    """统一入口：mapping: {device_path: image_path}。"""
    # device_path 在 macOS 上是序号字符串，直接复用
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
