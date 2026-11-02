"""macOS Dock 自动隐藏设置。

macOS 没有稳定公开的「彻底移除 Dock」接口；系统设置里的「自动隐藏 Dock」
对应 `com.apple.dock` 的 `autohide` 键。这里只负责读写这个系统设置，
不把它当成程序自己的临时状态，也不会在退出时擅自还原——用户关掉本程序后，
Dock 的偏好仍应保持。
"""
import logging
import re
import subprocess
import time


logger = logging.getLogger(__name__)

DOMAIN = "com.apple.dock"
KEY = "autohide"
TIMEOUT = 5
PANEL_TIMEOUT = 2

# Dock 面板位置只用于计算工作区。显示器枚举本身已有 2 秒缓存，这里再留一个
# 很短的缓存，避免用户连续点排列时反复拉起 osascript；Dock 设置切换后会主动
# 清理，下一次刷新立即读到新面板。
_PANEL_CACHE_TTL = 2.0
_panel_cache = {"ts": 0.0, "checked": False, "rect": None}

_PANEL_SCRIPT = r'''
tell application "System Events"
  tell process "Dock"
    if (count of lists) is 0 then return ""
    set d to list 1
    set p to position of d
    set s to size of d
    return ((item 1 of p) as integer as text) & "|" & ¬
      ((item 2 of p) as integer as text) & "|" & ¬
      ((item 1 of s) as integer as text) & "|" & ¬
      ((item 2 of s) as integer as text)
  end tell
end tell
'''


def _error_text(result):
    """取 defaults / killall 的错误摘要，避免把整段输出塞进界面。"""
    text = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    if not text:
        return ""
    return text.splitlines()[0][:200]


def get_autohide():
    """读取系统当前的 Dock 自动隐藏状态；读不到返回 None。"""
    try:
        result = subprocess.run(
            ["defaults", "read", DOMAIN, KEY],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.warning("读取 Dock 自动隐藏状态超时")
        return None
    except OSError as exc:
        logger.warning("读取 Dock 自动隐藏状态失败: %s", exc)
        return None

    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip().lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return None


def _parse_panel_rect(text):
    """解析 Dock 面板的 `x|y|宽|高` 输出；无效时返回 None。

    让 AppleScript 自己用竖线拼数字，避免当前 locale 给上千的坐标插入千位
    分隔符。负数仍可能出现（主屏左侧/上方的副屏），所以按完整整数匹配。
    """
    nums = re.findall(r"-?\d+", text or "")
    if len(nums) != 4:
        return None
    rect = tuple(int(v) for v in nums)
    if rect[2] <= 0 or rect[3] <= 0:
        return None
    return rect


def invalidate_panel_cache():
    """让下一次 get_panel_rect() 重新读取 Dock 面板。"""
    _panel_cache["ts"] = 0.0
    _panel_cache["checked"] = False
    _panel_cache["rect"] = None


def get_panel_rect():
    """返回当前可见 Dock 面板的屏幕矩形 `(x, y, w, h)`；读不到返回 None。

    坐标与 CoreGraphics / System Events 一致：左上原点，y 向下。Dock 自动
    隐藏时面板仍可能返回一个屏幕外或贴边矩形，调用方应结合 get_autohide()
    决定是否预留。这里不把失败写进日志：未授权辅助功能是常见情况，工作区
    还有 visibleFrame 的兜底，反复刷警告只会淹没真正的窗口错误。
    """
    now = time.monotonic()
    if (_panel_cache["checked"]
            and now - _panel_cache["ts"] < _PANEL_CACHE_TTL):
        return _panel_cache["rect"]

    rect = None
    try:
        result = subprocess.run(
            ["osascript", "-e", _PANEL_SCRIPT],
            capture_output=True, text=True, timeout=PANEL_TIMEOUT,
        )
        if result.returncode == 0:
            rect = _parse_panel_rect(result.stdout)
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        pass

    _panel_cache["ts"] = now
    _panel_cache["checked"] = True
    _panel_cache["rect"] = rect
    return rect


def set_autohide(enabled):
    """设置 Dock 自动隐藏并复验。

    返回 `(ok, 说明)`。写入成功但 Dock 重启失败、超时或读回值与目标不一致，
    都返回 False；调用方应根据说明提示用户，而不是静默当作成功。
    """
    wanted = bool(enabled)
    value = "true" if wanted else "false"
    try:
        result = subprocess.run(
            ["defaults", "write", DOMAIN, KEY, "-bool", value],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "写入 Dock 设置超时，系统可能仍保持原状态"
    except OSError as exc:
        return False, "写入 Dock 设置失败：%s" % exc

    if result.returncode != 0:
        detail = _error_text(result)
        return False, "写入 Dock 设置失败" + (("：" + detail) if detail else "")

    # defaults 只改了偏好文件，Dock 进程需要重启才会按新设置重绘和重新布局。
    try:
        restart = subprocess.run(
            ["killall", "Dock"],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "Dock 设置已写入，但重启 Dock 超时"
    except OSError as exc:
        return False, "Dock 设置已写入，但重启 Dock 失败：%s" % exc

    if restart.returncode != 0:
        detail = _error_text(restart)
        return False, "Dock 设置已写入，但重启 Dock 失败" + (("：" + detail) if detail else "")

    actual = get_autohide()
    if actual is None:
        invalidate_panel_cache()
        return False, "Dock 设置已写入，但无法读回系统状态进行复验"
    if actual != wanted:
        invalidate_panel_cache()
        return False, "Dock 设置未生效（目标是%s，系统读回为%s）" % (
            "自动隐藏" if wanted else "始终显示",
            "自动隐藏" if actual else "始终显示",
        )
    invalidate_panel_cache()
    return True, ("Dock 栏已自动隐藏" if wanted else "Dock 栏已固定显示")
