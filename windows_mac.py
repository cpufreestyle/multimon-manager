"""窗口跨屏移动与分屏吸附（macOS 实现，零第三方依赖）。

读取窗口几何：优先用 CoreGraphics 的 CGWindowListCopyWindowInfo —— 一次调用即可
拿到**按 z 序（从前到后）**排列的窗口列表，含 owner PID / 应用名 / bounds。
这样既能拿到准确坐标，又能**跳过本程序自己的窗口**（点击本程序按钮时前台就是
自己，旧实现因此会把"当前前台窗口"误判成本程序，去移动自己的窗口）。

写入窗口几何：用 System Events 的 position / size。
注意：较新 macOS 的 System Events window 类**没有 bounds 属性**，取 bounds 恒报
-1728「不能获得」。旧实现依赖 bounds，导致：
  - 读取恒失败 → get_window_rect 返回 (0,0,0,0)
  - 写入 `set bounds of window 1 to {...}` 静默失败 → 按钮按了完全没反应
改用 position + size 后读写均可用。

macOS 坐标系原点在左上，与 Windows 转换后的坐标系一致。
"""
import ctypes
import ctypes.util
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time

import monitors_mac as monitors
from monitors_mac import CGRect

import quad_plan
import grid_plan
import screen_math

logger = logging.getLogger(__name__)

# ── 写入前临时关掉目标 App 的窗口动画 ─────────────────────────────
# 来源：Rectangle Pro 3.94 静态分析（字符串 "AXEnhancedUserInterface was
# enabled, will disable before resizing"）。System Events 写入后系统会播放
# 移动/缩放动画，一次写入只「部分应用」，于是要靠幂等重设反复推；中间态越多，
# 越容易在某一帧越出屏幕底边，被 macOS 把整个窗口判给别的屏（真机实测
# ChatGPT 被搬到 RV200 Pro）。关掉动画后写入立即生效：实测 2.96s -> 0.88s，
# 且一次命中目标。用完立刻恢复原值，异常也恢复。
_AX_LIB = None
_AX_ATTR_ENHANCED = None


def _load_ax():
    """懒加载 ApplicationServices 与 AXEnhancedUserInterface 的 CFString。"""
    global _AX_LIB, _AX_ATTR_ENHANCED
    if _AX_LIB is not None:
        return _AX_LIB
    try:
        from ctypes import cdll, util
        lib = cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        lib.AXUIElementCreateApplication.restype = ctypes.c_void_p
        lib.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        lib.AXUIElementCopyAttributeValue.restype = ctypes.c_int
        lib.AXUIElementCopyAttributeValue.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.AXUIElementSetAttributeValue.restype = ctypes.c_int
        lib.AXUIElementSetAttributeValue.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf = cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        _AX_ATTR_ENHANCED = cf.CFStringCreateWithCString(
            None, b"AXEnhancedUserInterface", 0x08000100)
        lib._kCFBooleanTrue = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        lib._kCFBooleanFalse = ctypes.c_void_p.in_dll(cf, "kCFBooleanFalse")
        _AX_LIB = lib
    except Exception as e:  # noqa: BLE001
        logger.warning("AX 动画开关不可用，退回默认写入: %s", e)
        _AX_LIB = False
    return _AX_LIB


class _AppAnimationOff:
    """写入期间临时关掉目标 App 的窗口动画；退出时恢复原值。

    读不到原始值时不动它（不猜），只在我们确实拿到布尔值时才关/复。
    """

    def __init__(self, app_name):
        self.app_name = app_name
        self.pid = None
        self.was_on = None

    def __enter__(self):
        lib = _load_ax()
        if not lib or not self.app_name:
            return self
        try:
            pid = None
            for w in list_windows_front_to_back():
                if w.get("owner") == self.app_name:
                    pid = w.get("pid")
                    break
            if not pid:
                return self
            el = lib.AXUIElementCreateApplication(int(pid))
            if not el:
                return self
            out = ctypes.c_void_p()
            if lib.AXUIElementCopyAttributeValue(
                    ctypes.c_void_p(el), _AX_ATTR_ENHANCED,
                    ctypes.byref(out)) != 0 or not out.value:
                return self
            # 取不到布尔本值时不当成 True：只在确实是 kCFBooleanTrue 时才关
            if out.value != lib._kCFBooleanTrue.value:
                return self
            self.pid = int(pid)
            self.was_on = True
            lib.AXUIElementSetAttributeValue(
                ctypes.c_void_p(el), _AX_ATTR_ENHANCED, lib._kCFBooleanFalse)
        except Exception as e:  # noqa: BLE001
            logger.info("关闭窗口动画失败（不影响写入）: %s", e)
        return self

    def __exit__(self, *_exc):
        if self.pid is None or not self.was_on:
            return False
        lib = _load_ax()
        if not lib:
            return False
        try:
            el = lib.AXUIElementCreateApplication(self.pid)
            lib.AXUIElementSetAttributeValue(
                ctypes.c_void_p(el), _AX_ATTR_ENHANCED, lib._kCFBooleanTrue)
        except Exception as e:  # noqa: BLE001
            logger.info("恢复窗口动画失败: %s", e)
        return False

# ── 撤销栈：记录每次移动/缩放前的窗口几何，供"撤销移动"使用（F7）──
_undo_stack = []


# ── 窗口间距（借鉴 Rectangle Pro 的 GapCalculation / EdgeGapField）────────
# 「格子之间留缝、窗口不顶边」有两个直接好处：
#   1. 观感：四格清清楚楚分开，不再是四个窗口挤成一片；
#   2. 容错：App 的最小尺寸把窗口顶大时，超出的部分先吃掉间距，不会立刻
#      越出屏幕边界触发 macOS 搬屏。
# 语义与 Rectangle Pro 一致：窗口之间隔 gap，屏幕边是否也隔由
# gap_screen_edges 决定。
_GAP_ZONES = {
    # zone: (left, top, right, bottom) 哪些边需要让出间距
    "left": (0, 0, 1, 0),          # 左半屏：右边让缝
    "right": (1, 0, 0, 0),         # 右半屏：左边让缝
    "top": (0, 0, 0, 1),
    "bottom": (0, 1, 0, 0),
    "left-third": (0, 0, 1, 0),
    "middle-third": (1, 0, 1, 0),
    "right-third": (1, 0, 0, 0),
    "quad-tl": (0, 0, 1, 1),
    "quad-tr": (1, 0, 0, 1),
    "quad-bl": (0, 1, 1, 0),
    "quad-br": (1, 1, 0, 0),
    # 水平三分屏：横屏直接用；竖屏上 portrait_zone 会把左右三分旋转成这三项
    "top-third": (0, 0, 1, 1),
    "middle-third-h": (0, 1, 1, 1),
    "bottom-third": (0, 1, 1, 0),
}


# 四宫格的四个方位对应的 zone 名（与 snap() 的 quad-* 一致）
_QUAD_GAP_ZONE = ("quad-tl", "quad-tr", "quad-bl", "quad-br")
# 同上，给人看的角名（日志 / 说明用）
_QUAD_NAMES = ("左上", "右上", "左下", "右下")


# 「写入 → 读回实测尺寸 → 用实测重算格子 → 再写入」的遍数上限。第一遍按探测
# 到的最小尺寸切格子，写入后读回的是窗口**实际**占的尺寸：App 的最小尺寸探测
# 偏小时，窗口被钳得比格子大，上下两排必然重叠。多写几遍、每遍把实测尺寸并进
# 最小尺寸（只升不降，单调收敛），格子随之长到「装得下实测尺寸」为止。
_QUAD_MAX_PASSES = 5

# 写入后实测钳制做二趟重排的容差：与 quad_plan._SLACK_TOL 同语义，
# 1~6px 的读回噪声不把格子判成「装不下」。
_QUAD_SLACK_TOL = 6


def _gap_settings():
    """读取窗口间距设置：(gap, gap_screen_edges)。异常时按关闭处理。"""
    try:
        import settings
        s = settings.load()
        gap = int(s.get("window_gap", 0) or 0)
        edges = bool(s.get("gap_screen_edges", False))
    except Exception:  # noqa: BLE001
        return (0, False)
    return (max(0, gap), edges)


def gap_rect(zone, x, y, w, h, work_left, work_top, work_width, work_height):
    """按 zone 把矩形向内让出间距；间距关闭时原样返回。

    work_* 用来判断哪些边贴在工作区边界上——只有 gap_screen_edges 打开时
    外边才让间距，否则四格仍贴住屏幕四边，只在窗口之间留缝。

    window_gap 的语义是「可见缝隙」：窗口之间、窗口与屏幕边之间的实际缝隙
    等于 window_gap。因此每个窗口在相应方向只让出一半，两边各让一半才凑成
    一个完整缝隙（与 yabai 的 window margin 模型一致）。
    """
    gap, edges = _gap_settings()
    half = gap / 2.0
    if half <= 0 or w <= gap or h <= gap:
        return (x, y, w, h)
    ins = _GAP_ZONES.get(zone)
    if ins is None:
        # maximize / center 没有邻居：四边都算外边，只有勾选屏幕边才让
        ins = (0, 0, 0, 0)
    l, t, r, b = ins
    if x <= work_left:
        l = l or edges
    if y <= work_top:
        t = t or edges
    if x + w >= work_left + work_width:
        r = r or edges
    if y + h >= work_top + work_height:
        b = b or edges
    nx = int(round(x + (half if l else 0)))
    ny = int(round(y + (half if t else 0)))
    nw = int(round(w - (half if l else 0) - (half if r else 0)))
    nh = int(round(h - (half if t else 0) - (half if b else 0)))
    if nw < 1 or nh < 1:
        return (x, y, w, h)
    return (nx, ny, nw, nh)


# 竖屏上自动旋转成水平切分的方位（Rectangle Pro 的 OrientationAware /
# PortraitSideThirdsCompoundCalculation：竖屏三分屏改为横向三等分）。
_PORTRAIT_ZONES = {
    "left": "top",             # 左半屏 -> 上半屏
    "right": "bottom",         # 右半屏 -> 下半屏
    "top": "left",
    "bottom": "right",
    "left-third": "top-third",
    "middle-third": "middle-third",
    "right-third": "bottom-third",
    "top-third": "left-third",
    "bottom-third": "right-third",
}


def portrait_zone(zone, work_width, work_height):
    """竖屏上把方位旋转 90 度；横屏原样返回。

    竖屏（高 > 宽）上左右切分只会得到两条窄条，上下切分才符合视觉。
    输入 work_width/work_height 用于判断朝向，无需额外传显示器对象。
    """
    if work_height <= work_width:
        return zone
    return _PORTRAIT_ZONES.get(zone, zone)
_undo_lock = threading.Lock()
_suppress_undo = False
# 「上次摆放位」记忆（Moom 的 Last Moom Frame 思路）：窗口被本程序摆过
# 之后又被用户拖走时，记住它上次被程序摆到的位置，撤销/吸附恢复时可回去。
_last_arranged = {}
_last_arranged_lock = threading.Lock()

# snap() 最近一次的实测报告：[(hwnd, want, got)]，供 UI 状态栏如实提示
# 「被应用最小/固定尺寸钳制，贴的是哪条边」。
_last_snap_report = []


def push_undo(hwnd, rect):
    """记录一次移动前几何（hwnd, (x, y, w, h)）；保留最近 20 步。"""
    with _undo_lock:
        _undo_stack.append((hwnd, tuple(rect)))
        if len(_undo_stack) > 20:
            _undo_stack.pop(0)


def remember_arranged(hwnd, rect):
    """记录窗口被程序摆到的位置（hwnd -> (x, y, w, h)），最多 40 个。"""
    with _last_arranged_lock:
        _last_arranged[hwnd] = tuple(rect)
        while len(_last_arranged) > 40:
            _last_arranged.pop(next(iter(_last_arranged)))


def last_arranged(hwnd):
    """返回该窗口上次被程序摆到的位置；没记录时返回 None。"""
    with _last_arranged_lock:
        return _last_arranged.get(hwnd)


# ── AppleScript 执行 ─────────────────────────────────────────────────

def _osa(script):
    """执行 AppleScript，失败时记录日志（旧实现静默吞错，导致问题无法定位）。"""
    try:
        r = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0 and r.stderr.strip():
            logger.warning("AppleScript 执行失败: %s", r.stderr.strip()[:200])
        return r.stdout.strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("AppleScript 调用异常: %s", e)
        return ""


def _parse_nums(text):
    """从 AppleScript 输出里抽出全部整数（含负数），忽略分隔符噪声。

    AppleScript 的数字经 as text 会按当前 locale 插入千位分隔符：坐标 1054
    可能输出成 "1,054"，于是 "546,1,054,-870,870" 被 split(",") 切成 5 段
    而解析失败。真机日志里见过 "546, ,, -870, ,, 1054, ,, 870" 这种 8 段
    输出，几何读取直接报废，四宫格退化成按序号选窗。因此这里不按逗号
    切分，而是扫描整段文本取所有数字 token。
    """
    if not text:
        return []
    return [int(t) for t in re.findall(r"-?\d+", text)]


# ── Quartz（CoreGraphics + CoreFoundation）ctypes 绑定 ───────────────

_cg = None
_cf = None
_K = {}

_KCF_STRING_ENCODING_UTF8 = 0x08000100
_KCF_NUMBER_INT_TYPE = 9

_K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY = 1 << 0
_K_CG_WINDOW_LIST_EXCLUDE_DESKTOP_ELEMENTS = 1 << 4


def _load_quartz():
    global _cg, _cf
    try:
        cg_path = ctypes.util.find_library("CoreGraphics")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not cg_path or not cf_path:
            logger.warning("未找到 CoreGraphics/CoreFoundation，窗口几何将退化为 AppleScript")
            return
        cg = ctypes.CDLL(cg_path)
        cf = ctypes.CDLL(cf_path)

        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p

        cg.CGRectMakeWithDictionaryRepresentation.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(CGRect)
        ]
        cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool

        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFDictionaryGetValueIfPresent.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        ]
        cf.CFDictionaryGetValueIfPresent.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None

        _cg, _cf = cg, cf
        logger.info("Quartz(CoreGraphics/CoreFoundation) 加载成功")
    except Exception as e:  # noqa: BLE001
        logger.warning("加载 Quartz 失败: %s", e)


_load_quartz()


def _k(name):
    """取 CoreGraphics 导出的 CFString 常量（如 kCGWindowBounds）。"""
    if name not in _K:
        ref = None
        if _cg is not None:
            try:
                ref = ctypes.c_void_p.in_dll(_cg, name).value
            except Exception:  # noqa: BLE001
                ref = None
        _K[name] = ref
    return _K[name]


def _cfstring_to_str(ref):
    if not ref or _cf is None:
        return ""
    try:
        n = _cf.CFStringGetLength(ctypes.c_void_p(ref))
        if n <= 0:
            return ""
        buf = ctypes.create_string_buffer(n * 4 + 1)
        if _cf.CFStringGetCString(
            ctypes.c_void_p(ref), buf, len(buf), _KCF_STRING_ENCODING_UTF8
        ):
            return buf.value.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _cfnumber_to_int(ref):
    if not ref or _cf is None:
        return None
    v = ctypes.c_int(0)
    try:
        if _cf.CFNumberGetValue(
            ctypes.c_void_p(ref), _KCF_NUMBER_INT_TYPE, ctypes.byref(v)
        ):
            return v.value
    except Exception:  # noqa: BLE001
        pass
    return None


# 系统浮层/后台代理进程：它们偶尔会以 layer 0 出现在窗口列表里（如 WindowManager 的
# "Gesture Blocking Overlay"），若被当成"最前窗口"会导致操作打空，故一律排除。
_SKIP_OWNERS = {
    "WindowManager",
    "Window Server",
    "Dock",
    "SystemUIServer",
    "loginwindow",
    "ControlCenter",
    "NotificationCenter",
    "TextInputMenuAgent",
    "universalaccessd",
    "AccessibilityUIServer",
    "ViewBridgeAuxiliary",
    "WallpaperAgent",
    "MenuBarAgent",
    "UIKitSystem",
    "System Events",
}


# 四宫格自动选窗时要跳过的 owner：这些进程在 CG 层有窗口（会被枚举到），
# 但 AppleScript / AX 完全操作不了——wine 跑的是 Windows 程序，System Events
# 拿不到它的 window，写几何永远失败。真机日志见过连续 8 次
# 「读取窗口几何失败: 'wine::' -> ''」，整个四宫格流程被拖到 20 秒以上。
# 只影响自动补足，不影响界面下拉框（用户显式选它就照排，失败有提示）。
_QUAD_SKIP_OWNERS = {
    "wine",
    "wine64",
    "wine64-preloader",
}


def _dict_get(d, key_name):
    """从 CFDictionary 取值；不存在返回 None。"""
    k = _k(key_name)
    if not k or not d or _cf is None:
        return None
    out = ctypes.c_void_p(0)
    try:
        if _cf.CFDictionaryGetValueIfPresent(
            ctypes.c_void_p(d), ctypes.c_void_p(k), ctypes.byref(out)
        ):
            return out.value
    except Exception:  # noqa: BLE001
        pass
    return None


# ── 本程序自身的进程白名单（窗口操作时应跳过自己） ────────────────────

_OWN_PIDS = set()


def register_own_pid(pid):
    """登记本应用的进程 PID（主进程自动登记，托盘子进程由 main.py 登记）。"""
    if pid:
        _OWN_PIDS.add(int(pid))


register_own_pid(os.getpid())


# ── 窗口枚举（CoreGraphics，按 z 序从前到后） ────────────────────────

def list_windows_front_to_back(min_size=80):
    """返回按 z 序（最前在前）排列的屏幕窗口列表。

    每项: {"pid", "owner", "name", "x", "y", "w", "h"}
    仅保留普通窗口层（layer 0）且尺寸合理的项。
    """
    if _cg is None or _cf is None:
        return []
    arr = None
    try:
        opt = (
            _K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY
            | _K_CG_WINDOW_LIST_EXCLUDE_DESKTOP_ELEMENTS
        )
        arr = _cg.CGWindowListCopyWindowInfo(opt, 0)
        if not arr:
            return []
        count = _cf.CFArrayGetCount(ctypes.c_void_p(arr))
        result = []
        for i in range(count):
            d = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(arr), i)
            if not d:
                continue
            layer = _cfnumber_to_int(_dict_get(d, "kCGWindowLayer"))
            if layer not in (0, None):  # 跳过 Dock / 菜单栏等非普通层
                continue
            bounds_ref = _dict_get(d, "kCGWindowBounds")
            if not bounds_ref:
                continue
            r = CGRect()
            if not _cg.CGRectMakeWithDictionaryRepresentation(
                ctypes.c_void_p(bounds_ref), ctypes.byref(r)
            ):
                continue
            w, h = int(r.size.width), int(r.size.height)
            if w < min_size or h < min_size:
                continue
            owner = _cfstring_to_str(_dict_get(d, "kCGWindowOwnerName"))
            if owner in _SKIP_OWNERS:
                continue
            result.append({
                "pid": _cfnumber_to_int(_dict_get(d, "kCGWindowOwnerPID")),
                "owner": owner,
                "name": _cfstring_to_str(_dict_get(d, "kCGWindowName")),
                "wid": _cfnumber_to_int(_dict_get(d, "kCGWindowNumber")),
                "x": int(r.origin.x),
                "y": int(r.origin.y),
                "w": w,
                "h": h,
            })
        return result
    except Exception as e:  # noqa: BLE001
        logger.warning("枚举窗口失败: %s", e)
        return []
    finally:
        if arr and _cf is not None:
            try:
                _cf.CFRelease(ctypes.c_void_p(arr))
            except Exception:  # noqa: BLE001
                pass


def front_external_window(min_reasonable=(160, 120)):
    """最前面的**非本程序**窗口；没有则返回 None（避免误操作自己）。

    分两轮：先找尺寸正常的窗口，避免自动选中最小化/其它 Space 的缩略小窗
    （实测列表里混有 87×108、117×121 之类的小窗）；找不到再放宽条件。
    """
    wins = [w for w in list_windows_front_to_back()
            if w["pid"] not in _OWN_PIDS and w["owner"]]
    mw, mh = min_reasonable
    for w in wins:
        if w["w"] >= mw and w["h"] >= mh:
            return w
    return wins[0] if wins else None


# ── 对外接口 ─────────────────────────────────────────────────────────

# hwnd 格式：「应用名::窗口名#序号」。
# 序号是该应用同名窗口按 z 序的 1 基编号（唯一时省略，向后兼容旧格式
# 「应用名::窗口名」）。UU远程 这类应用所有窗口同名（多为空串），只靠
# 「应用::窗口名」会全部撞成一个标识：枚举去重后 6 个窗口只剩 1 个，
# 且 set_window_rect 的 AppleScript 永远写 window 1——这就是"自动排列
# 不准 / 只显示一个窗口"的根因。带序号后，规则引擎 / 并排 / 三栏 / 任务栏
# 都能区分并精确操作每个窗口。

def make_hwnd(owner, name, seq=None):
    """生成窗口标识；seq 为同名窗口的 1 基序号（唯一/首个时不带 #n）。"""
    base = f"{owner}::{name}"
    return f"{base}#{seq}" if seq and seq > 1 else base


def parse_hwnd(hwnd):
    """拆出 (应用名, 窗口名, 序号)；旧格式（无 #n）序号返回 None。"""
    app, _, rest = hwnd.partition("::")
    seq = None
    if "#" in rest:
        rest, _, tail = rest.rpartition("#")
        try:
            seq = int(tail)
        except ValueError:  # noqa: BLE001
            rest = f"{rest}#{tail}"  # 标题本身含 #，原样保留
    return app, rest, seq


def _same_name_windows(app, name):
    """该应用下同窗口名的全部窗口（z 序），按 hwnd 序号语义排列。"""
    return [w for w in list_windows_front_to_back()
            if w["owner"] == app and (w["name"] or "") == (name or "")]


_last_rect = {}  # "app::winname#seq" -> (x, y, w, h)，来自 CG 的精确几何

# 界面上固定的目标窗口。自动判断（"最前面的非本程序窗口"）经常猜错：
# 例如用户停在 IDE 聊天窗口时点按钮，最前面的就是 IDE，结果把 IDE 分屏了。
# 因此提供显式指定；为 None 时回到自动判断。
_pinned_target = None


def set_target(hwnd):
    """固定要操作的窗口；传 None 表示恢复自动判断。"""
    global _pinned_target
    _pinned_target = hwnd
    logger.info("目标窗口设为: %s", hwnd or "自动")


def get_target():
    return _pinned_target


def list_target_windows():
    """列出可选窗口（供界面下拉框），已排除本程序与系统浮层。

    返回 [{"hwnd", "label", "owner", "name", "x", "y", "w", "h"}, ...]

    同名窗口（如 UU远程 的多个会话窗口）按 z 序编号区分，不再被去重吞掉。
    """
    out = []
    name_count = {}
    name_seen = {}
    raw = list_windows_front_to_back()
    for w in raw:
        key = (w["owner"], w["name"] or "")
        name_count[key] = name_count.get(key, 0) + 1
    for w in raw:
        key = (w["owner"], w["name"] or "")
        n = name_seen.get(key, 0) + 1
        name_seen[key] = n
        seq = n if name_count[key] > 1 else None
        hwnd = make_hwnd(w["owner"], w["name"] or "", seq)
        title = f'{w["owner"]} — {w["name"]}' if w["name"] else w["owner"]
        # 带上尺寸，便于区分同名/小窗口
        label = f'{title}  ({w["w"]}×{w["h"]})'
        if seq:
            # 序号放最前：下拉框宽度有限会截尾，放前面才看得到区分
            label = f"#{seq} {label}"
        out.append({**w, "hwnd": hwnd, "seq": seq, "label": label})
    return out


def _find_window(hwnd):
    """按 hwnd 重新定位窗口，拿到最新几何（窗口可能被移动过）。

    兼容旧格式「app::name」：同名多窗口时取最前面那个。
    """
    if not hwnd:
        return None
    app, name, seq = parse_hwnd(hwnd)
    same = _same_name_windows(app, name)
    if not same:
        return None
    idx = min((seq or 1) - 1, len(same) - 1)
    return same[idx]


def get_foreground_window(use_pinned=True):
    """返回 '应用名::窗口名' 标识。

    use_pinned=True（界面按钮）时优先用界面固定的目标窗口；
    use_pinned=False（全局快捷键）时始终作用于真正的活动窗口。
    """
    if use_pinned and _pinned_target:
        w = _find_window(_pinned_target)
        if w:
            _last_rect[_pinned_target] = (w["x"], w["y"], w["w"], w["h"])
            logger.info("目标窗口(已固定): %s  几何=%s",
                        _pinned_target, _last_rect[_pinned_target])
            return _pinned_target
        logger.warning("固定的目标窗口已关闭或不可见: %s，临时回退自动判断",
                       _pinned_target)

    w = front_external_window()
    if w:
        # 同名多窗口时补上 z 序编号，避免后续操作落到别的同名窗口上
        same = _same_name_windows(w["owner"], w["name"] or "")
        seq = None
        if len(same) > 1:
            for i, s in enumerate(same, 1):
                if s.get("wid") is not None and s.get("wid") == w.get("wid"):
                    seq = i
                    break
        key = make_hwnd(w["owner"], w["name"] or "", seq)
        _last_rect[key] = (w["x"], w["y"], w["w"], w["h"])
        logger.info("目标窗口: %s  几何=%s", key, _last_rect[key])
        return key

    logger.warning(
        "CG 未找到可操作窗口（已排除本程序 PID %s），回退 AppleScript",
        sorted(_OWN_PIDS),
    )

    # 回退：CoreGraphics 不可用时用 System Events。
    # 注意必须带上 unix id —— 点按钮时前台就是本程序自己，若不按 PID 排除，
    # 会去移动自己的窗口，表现为"按了没反应"。
    script = (
        'tell application "System Events"\n'
        '  set p to first application process whose frontmost is true\n'
        '  set pid to unix id of p\n'
        '  set appName to name of p\n'
        '  if (count of windows of p) > 0 then\n'
        '    return (pid as text) & "|" & appName & "::" & (name of window 1 of p)\n'
        '  end if\n'
        'end tell\n'
        'return ""\n'
    )
    res = _osa(script)
    if not res or "|" not in res:
        return None
    pid_str, rest = res.split("|", 1)
    try:
        if int(pid_str) in _OWN_PIDS:
            logger.warning("前台就是本程序自己（PID %s），放弃操作", pid_str)
            return None
    except ValueError:  # noqa: BLE001
        pass
    return rest


def get_window_rect(hwnd):
    """返回 (x, y, w, h)。

    优先用 CoreGraphics 枚举按 (owner + name) 精确匹配，完全不经过 AppleScript，
    因此不受窗口标题特殊字符（如全角引号）影响（修复 B 债：OneNote / 微信开发者工具
    等标题含全角引号的窗口几何读取失败）。CG 取不到时再回退 System Events 的
    position+size。
    """
    if not hwnd:
        return (0, 0, 0, 0)
    if hwnd in _last_rect:
        return _last_rect[hwnd]

    app, name, seq = parse_hwnd(hwnd)
    # 优先走 CG：按 owner 匹配，精确匹配窗口名。
    # 带序号的 hwnd 只认「同名第 N 个」，绝不回退到该应用最前面的窗口——
    # 同名多窗口（UU远程）时回退会让不同 hwnd 都读成同一个窗口的几何，
    # 闭环读回/写入全乱，表现为"几个窗口跟着一起动"。
    try:
        ordinal = 0
        for w in list_windows_front_to_back():
            if w["owner"] != app:
                continue
            if (w["name"] or "") == (name or ""):
                ordinal += 1
                if seq and ordinal != seq:
                    continue  # 带序号的 hwnd 只认对应那个同名窗口
                rect = (w["x"], w["y"], w["w"], w["h"])
                _last_rect[hwnd] = rect
                return rect
        if seq:
            # 序号牌没匹配到（窗口可能已关闭/改名），不拿别的窗口顶替
            logger.warning("序号牌 %s 未匹配到同名窗口，返回零几何", hwnd)
            return (0, 0, 0, 0)
        # 无序号（唯一窗口）：允许按 owner 兜底到该应用最前窗口
        for w in list_windows_front_to_back():
            if w["owner"] == app:
                rect = (w["x"], w["y"], w["w"], w["h"])
                _last_rect[hwnd] = rect
                return rect
    except Exception:  # noqa: BLE001
        pass

    # 回退：System Events 的 position+size（注意 bounds 在 macOS 当前不可用，恒报 -1728）
    # 带序号时按「同名第 N 个」选择窗口，避免总是读到 window 1
    win_sel = _ax_window_selector(hwnd)
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    set p to position of {win_sel}\n'
        f'    set s to size of {win_sel}\n'
        f'    return (item 1 of p & "," & item 2 of p & "," '
        f'& item 1 of s & "," & item 2 of s)\n'
        f'  end tell\n'
        f'end tell\n'
    )
    out = _osa(script)
    try:
        x, y, w, h = _parse_nums(out)
        rect = (x, y, w, h)
    except Exception:  # noqa: BLE001
        logger.warning("读取窗口几何失败: %r -> %r", hwnd, out)
        return (0, 0, 0, 0)
    _last_rect[hwnd] = rect
    return rect


def _ax_window_selector(hwnd):
    """按 hwnd 生成 AppleScript 的窗口选择子。

    默认是 "window 1"；同名多窗口的 hwnd 带 #n 序号时用 "window N"。
    AX 窗口顺序与 CG z 序一致（实测 UU远程 6 个窗口），因此 z 序第 N 个
    同名窗口就是 AX 的 window N。
    """
    _app, _name, seq = parse_hwnd(hwnd)
    if not seq:
        return "window 1"
    return f"window {seq}"


def _ax_window_by_geometry(app, rect, name=None):
    """按几何在 AX 窗口列表里找出对应的 window N 选择子。

    CG 的窗口枚举（list_windows_front_to_back）与 AppleScript 的 AX 窗口
    顺序**不一定一致**：QClaw 有两个 AX 窗口，window 1 是 430x40 的悬浮小条、
    window 2 才是 1200x800 的主窗口，而 CG 只列出主窗口。此时 hwnd 不带 #n
    （CG 侧只有一个同名窗口），_ax_window_selector 退化成 "window 1"，
    结果把几何写到小条上、读回的却是主窗口——表现为「这个窗口完全推不动」。

    做法：枚举该应用所有 AX 窗口的位置/尺寸，取与目标矩形最接近的那个，
    返回 "window N"。找不到时返回 None，让调用方回退到按序号。
    """
    if not rect or rect == (0, 0, 0, 0):
        return None
    tx, ty, tw, th = rect
    script = (
        'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        '    set out to ""\n'
        '    repeat with w in windows\n'
        '      set p to position of w\n'
        '      set s to size of w\n'
        '      set out to out & (item 1 of p as text) & "," & '
        '(item 2 of p as text) & "," & (item 1 of s as text) & "," & '
        '(item 2 of s as text) & ";"\n'
        '    end repeat\n'
        '  end tell\n'
        'end tell\n'
        'return out'
    )
    raw = _osa(script)
    if not raw:
        return None
    best, best_dev = None, None
    for i, item in enumerate(raw.split(";"), 1):
        item = item.strip()
        if not item:
            continue
        # 不能按逗号切分：坐标过千时 as text 会插入千位分隔符，切出来不是 4 段
        # 不能用 abs()：副屏窗口的 x/y 是负数（如 RV200 Pro 的 y=-870），
        # 取绝对值后目标 y=-870 与实测 y=870 相差 1740，副屏上所有窗口的
        # 几何匹配全部失效，退化成按序号选窗——窗口就跑到别人身上去了。
        nums = _parse_nums(item)
        if len(nums) != 4:
            continue
        dev = (abs(nums[0] - tx) + abs(nums[1] - ty)
               + abs(nums[2] - tw) + abs(nums[3] - th))
        if best_dev is None or dev < best_dev:
            best, best_dev = i, dev
    if best is None:
        return None
    # 偏差过大（超过 64px 总和）说明几何已变（窗口被移动过），匹配不可信
    if best_dev > 64:
        by_name = _ax_window_by_name(app, name)
        if by_name:
            logger.info("AX 几何匹配偏差过大 (%d)，改用窗口名匹配 -> %s",
                        best_dev, by_name)
            return by_name
        logger.info("AX 几何匹配偏差过大 (%d)，回退按序号", best_dev)
        return None
    return f"window {best}"


def _ax_window_by_name(app, name):
    """按窗口名在 AX 窗口列表里找 window N（几何匹配失效时的兜底）。

    几何匹配失效时不能直接回退 window 1：真机 Chrome 的 EchoID 主窗口在
    y=-870 的副屏，CG 几何与 AX 对不上就回退 window 1，几何便写到了
    「要保存密码吗」对话框上，主窗口看着完全推不动。

    AX 的 name 与 CG 的窗口标题未必字字相同，所以用「互相包含」判断，
    并优先取名字更长（更具体）的那个，避免都含空串时误配。
    """
    want = (name or "").strip()
    if not want:
        return None
    # 语句之间必须有换行符：AppleScript 不接受把多条语句拼在同一行（缺了 \n
    # 时 osascript 一律报 -2741 语法错误，这条按窗口名兜底的分支等于死代码）
    script = (
        'tell application "System Events"\n'
        '  tell process "' + app + '"\n'
        '    set out to ""\n'
        '    repeat with w in windows\n'
        '      set out to out & (name of w as text) & ";"\n'
        '    end repeat\n'
        '  end tell\n'
        'end tell\n'
        'return out'
    )
    raw = _osa(script)
    if not raw:
        return None
    names = [n.strip() for n in raw.split(";")]
    best, best_len = None, 0
    for i, n in enumerate(names, 1):
        if not n:
            continue
        if want in n or n in want:
            if len(n) > best_len:
                best, best_len = i, len(n)
    if best is None and len(names) == 1:
        return "window 1"
    return f"window {best}" if best else None


def _wait_rect_stable(hwnd, timeout=1.5, interval=0.08, need=3):
    """等窗口几何稳定后再读回；始终不稳时返回最后一次读数。

    System Events 写入 position/size 后系统会播放移动 / 缩放动画，此时 CG 读到
    的是**动画中间帧**。闭环若把中间帧当实测值，会高估偏差并反向补偿过头，
    把窗口越推越远，4 次重试很快耗尽，最终落位偏差可达数百 px。

    竖屏（portrait）尤其明显：纵向跨度 2000+px，一次「上半屏 → 下半屏」要移动
    近千 px，动画时间长、中间帧与目标相差极远，这就是「竖屏上中下不准」的根因。
    小位移（主屏内微调）动画短，才显得准。

    判定方式：连续 need 次读回完全一致才认为动画结束。只要求「连续 2 次」是不够的——
    动画卡顿时会连续采到同一个中间帧而误判稳定，闭环随即按中间帧大幅反向补偿，
    窗口被越推越远（实测下半屏会被推到屏幕外）。
    """
    last = None
    same = 0
    waited = 0.0
    while waited < timeout:
        _last_rect.pop(hwnd, None)
        try:
            cur = get_window_rect(hwnd)
        except Exception:  # noqa: BLE001
            return last
        if cur and cur != (0, 0, 0, 0):
            if cur == last:
                same += 1
                if same >= need:
                    return cur
            else:
                same = 1
            last = cur
        time.sleep(interval)
        waited += interval
    return last


def set_window_rect(hwnd, x, y, w, h, activate=True):
    """移动/缩放窗口：反复写入同一目标值推进到位（幂等重设），而非反向补偿。

    坐标系约定：传入的 (x, y, w, h) 是窗口应占据的屏幕矩形，与 zone_rect / snap
    的分区计算保持一致。

    早期实现是「读回实测偏差后反向补偿」，但 System Events 对一次写入只做部分应用
    （变化量越大动画越长），读回值取决于写入前的窗口状态、并不稳定，据此补偿会把
    窗口推飞——竖屏（小米 portrait 1152x2048）上「上半屏 / 整屏高度」就是这么跑偏
    数百 px 的（用户反馈「竖屏上中下不准」）。

    现改为幂等重设：每次都写入同一个目标值，等动画结束后读回，只要还在变好就继续；
    读回不再变化即认为已触达系统 / App 可达边界（例如标题栏占住的顶部约 30px，
    窗口永远贴不到 work_top），此时停止，不再徒劳补偿。
    """
    if not hwnd:
        return
    # 「上次摆放位」只记程序摆放，不记撤销/回位这类内部复位
    _internal = _suppress_undo
    # 先用**当前真实几何**刷新缓存：四宫格连排时前一个窗口刚被移走，
    # _last_rect 里还是移动前的旧值，拿它去 AX 列表匹配必然偏差过大
    # （真机日志 1696px），只能退化成按序号选窗——窗口就写到别人身上。
    _fresh = None
    if not _suppress_undo:
        try:
            # 必须绕过 _last_rect 缓存读：get_window_rect 命中缓存就直接返回
            # 缓存值，前一个窗口刚搬完时这里拿到的是移动前的旧几何，拿它去
            # AX 列表匹配必然「偏差过大」而退化成按序号选窗——几何就写到
            # 同名/同序的别的窗口上（真机日志偏差 1293~2354px）。先 pop 再读
            # 才是当下的真实几何（_fresh_rect 就是干这个的）。
            _fresh = _fresh_rect(hwnd)
            if _fresh and _fresh != (0, 0, 0, 0):
                _last_rect[hwnd] = _fresh
            push_undo(hwnd, _fresh)
        except Exception:  # noqa: BLE001
            pass
    app2, nm, _sq = parse_hwnd(hwnd)
    known = _fresh if (_fresh and _fresh != (0, 0, 0, 0)) else _last_rect.get(hwnd)
    win_sel = (_ax_window_by_geometry(app2, known, nm)
               or _ax_window_selector(hwnd))
    # 优先按几何在 AX 窗口列表里定位：CG 枚举顺序与 AX 顺序未必一致，
    # 只按 hwnd 序号选窗口会把几何写到别的窗口上（见 _ax_window_by_geometry）
    app = app2
    # 原始目标（content 坐标），闭环全程以此为基准，偏差按「实测 - 原始目标」累计
    tx, ty, tw, th = (int(round(v)) for v in (x, y, w, h))
    # 幂等重设次数上限：下半屏约 3 次命中；整屏高度类（变化量最大）需更多次才触顶
    max_retry = 16
    prev = None
    best_dev = None
    stall = 0
    logger.info("移动窗口: 进程=%s -> content x=%d y=%d w=%d h=%d", app, tx, ty, tw, th)
    # 写入期间关掉该 App 的窗口动画（Rectangle Pro 同款做法）：动画会让一次
    # 写入只部分应用、读回读到中间帧，中间态越界还会触发 macOS 搬屏。
    anim = _AppAnimationOff(app)
    anim.__enter__()
    # 退出 zoom/全屏态，否则 size 变更可能被忽略
    _osa(
        f'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    if (count of windows) > 0 then\n'
        f'      try\n'
        f'        tell {win_sel} to if zoomed then set zoomed to false\n'
        f'      end try\n'
        f'    end if\n'
        f'  end tell\n'
        f'end tell\n'
    )
    # 幂等重设推进：System Events 对一次写入只「部分应用」——变化量越大动画越长，
    # 单次写入常常只走一部分（竖屏整屏高度实测只到 1465/2002）。反复写入同一个目标
    # 会持续推进，直到命中或触到系统上限（下半屏实测 3 次即精确命中）。
    #
    # 早期实现是「按读回偏差反向补偿」，但读回值依赖写入前的窗口状态、并不稳定，
    # 据此算出的补偿量会把窗口推飞（竖屏实测偏差被放大到数百 px），故改为幂等重设：
    # 每次都写同一个目标值，只判断「是否还在变好」，不做数值补偿。
    for attempt in range(max_retry):
        _osa(
            f'tell application "System Events"\n'
            f'  tell process "{app}"\n'
            f'    set position of {win_sel} to {{{tx}, {ty}}}\n'
            f'    set size of {win_sel} to {{{tw}, {th}}}\n'
            f'  end tell\n'
            f'end tell\n'
        )
        # 必须等动画结束再读，否则读到中间帧会误判。这里用较短的超时：重设是「反复
        # 推进」，每轮只需判断是否还在变好，不必等到完全静止，好把时间留给下一轮
        c = _wait_rect_stable(hwnd, timeout=0.5)
        if not c or c == (0, 0, 0, 0):
            break
        dev = max(abs(a - t) for a, t in zip(c, (tx, ty, tw, th)))
        if dev <= 2:
            break
        # 停止条件用「偏差是否还在改善」而不是「读回是否变化」：整屏高度这类大变化
        # 每次推进都很小，读回看着像没动，其实仍在靠近；提前停会卡在半路。
        if best_dev is None or dev < best_dev - 1:
            best_dev = dev
            stall = 0
        else:
            stall += 1
            if stall >= 2:
                # 连续两次都没再改善 → 已触达系统/App 可达边界（例如标题栏占住的
                # 顶部约 30px，窗口永远贴不到 work_top），再写也只是白等
                logger.info("已达可达边界：实测=%s 目标=(%d,%d,%d,%d) 偏差=%d",
                            c, tx, ty, tw, th, dev)
                break
        prev = c
        logger.info("幂等重设 第%d次 实测=%s 偏差=%d", attempt + 1, c, dev)
    anim.__exit__()
    if not _internal:
        # 记住「上次摆放位」：窗口之后被用户拖乱时，可一键回到程序摆好的
        # 位置（撤销/回位本身不覆盖这个记忆）
        try:
            remember_arranged(hwnd, (tx, ty, tw, th))
        except Exception:  # noqa: BLE001
            pass
    if activate:
        _osa(f'tell application "{app}" to activate')


def undo_last_move():
    """撤销最近一次窗口移动/缩放（恢复该窗口移动前的几何）。

    返回被还原的 hwnd；无可撤销项时返回 None。恢复过程自身不再次入栈。
    """
    global _suppress_undo
    with _undo_lock:
        if not _undo_stack:
            return None
        hwnd, rect = _undo_stack.pop()
    _suppress_undo = True
    try:
        set_window_rect(hwnd, rect[0], rect[1], rect[2], rect[3], activate=False)
    finally:
        _suppress_undo = False
    return hwnd


def return_to_last_arranged(hwnd):
    """把窗口送回它上次被程序摆到的位置（第二层撤销 / snap back）。

    与 undo_last_move 的分工：undo 栈是「还原你动手前的样子」；
    这里是「回到程序上次摆好的样子」——窗口被摆好后被拖乱，一键回去。
    """
    rect = last_arranged(hwnd)
    if not rect:
        return False
    global _suppress_undo
    _suppress_undo = True
    try:
        set_window_rect(hwnd, rect[0], rect[1], rect[2], rect[3], activate=False)
    finally:
        _suppress_undo = False
    return True


def _monitor_by_relative(monitors_list, src_hwnd):
    """窗口归属屏索引：交集最长优先（Moom 的 Intersection Length 思路）。

    中心点法在窗口横跨两块屏交界时会随机判给其中一块；按窗口与哪块屏
    交集面积最大判，才符合用户看到的真实归属。中心点完全脱离所有屏
    （读回异常）时由 screen_math 退回中心点法/首屏，行为只强不弱。
    """
    x, y, w, h = get_window_rect(src_hwnd)
    return screen_math.monitor_index_by_intersection(monitors_list, x, y, w, h)


def move_to_monitor(hwnd, monitor, activate=True):
    x, y, w, h = get_window_rect(hwnd)
    src = monitors.enum_monitors()
    idx = _monitor_by_relative(src, hwnd)
    src_m = src[idx] if idx < len(src) else src[0]
    rel_x = (x - src_m.left) / max(src_m.width, 1)
    rel_y = (y - src_m.top) / max(src_m.height, 1)
    new_x = monitor.work_left + rel_x * max(monitor.work_width - w, 0)
    new_y = monitor.work_top + rel_y * max(monitor.work_height - h, 0)
    set_window_rect(hwnd, new_x, new_y, w, h, activate=activate)


def snap(hwnd, monitor, zone, activate=True):
    wl, wt, ww, wh = monitor.work_rect
    _x, _y, w, h = get_window_rect(hwnd)
    # 竖屏自动旋转方位（Rectangle Pro 的 OrientationAware）
    zone = portrait_zone(zone, ww, wh)
    targets = {
        "left": (wl, wt, ww // 2, wh),
        "right": (wl + ww // 2, wt, ww - ww // 2, wh),
        "top": (wl, wt, ww, wh // 2),
        "bottom": (wl, wt + wh // 2, ww, wh - wh // 2),
        "maximize": (wl, wt, ww, wh),
        "center": (wl + (ww - w) // 2, wt + (wh - h) // 2, w, h),
        "left-third": (wl, wt, ww // 3, wh),
        "middle-third": (wl + ww // 3, wt, ww // 3, wh),
        "right-third": (wl + 2 * (ww // 3), wt, ww - 2 * (ww // 3), wh),
        "top-third": (wl, wt, ww, wh // 3),
        "middle-third-h": (wl, wt + wh // 3, ww, wh // 3),
        "bottom-third": (wl, wt + 2 * (wh // 3), ww, wh - 2 * (wh // 3)),
        "quad-tl": (wl, wt, ww // 2, wh // 2),
        "quad-tr": (wl + ww // 2, wt, ww - ww // 2, wh // 2),
        "quad-bl": (wl, wt + wh // 2, ww // 2, wh - wh // 2),
        "quad-br": (wl + ww // 2, wt + wh // 2, ww - ww // 2, wh - wh // 2),
    }
    if zone not in targets:
        logger.warning("snap: 未知区域 %s", zone)
        return
    tx, ty, tw, th = targets[zone]
    # 窗口之间留缝（默认 0，行为与以前完全一致）
    tx, ty, tw, th = gap_rect(zone, tx, ty, tw, th, wl, wt, ww, wh)
    set_window_rect(hwnd, tx, ty, tw, th, activate=activate)
    # App 的最小/固定尺寸会把请求尺寸钳制（真机实测 UU远程固定 921x680、
    # CrossPaste 固定 600x700、Marvis 最小宽 900）。尺寸到不了目标格子时，
    # 「用户要贴的那条边」必须重新钉到工作区边界上——右列贴右缘、下排贴底缘
    # ——否则窗口停在格子起点上悬空，看起来就是「没有吸附到边上」。
    got = _readback_rect(hwnd, (tx, ty, tw, th))
    if not got or got == (0, 0, 0, 0):
        _last_snap_report[:] = [(hwnd, (tx, ty, tw, th), None)]
        return
    ax, ay = screen_math.reanchor_edge(zone, got[0], got[1], got[2], got[3],
                                       wl, wt, ww, wh)
    if (ax, ay) != (got[0], got[1]):
        set_window_rect(hwnd, ax, ay, got[2], got[3], activate=activate)
        # 重锚后再读一次：报告里存「最终实际几何」，不能存重锚前被系统
        # 搬到别的屏的中间态（真机实测 y=-471 的过渡值会把状态栏吓到人）
        final = _readback_rect(hwnd, (ax, ay, got[2], got[3]))
        if final and final != (0, 0, 0, 0):
            got = final
    _last_snap_report[:] = [(hwnd, (tx, ty, tw, th), tuple(got))]


def adjust_adjacent(hwnd, old_rect, new_rect, activate=False):
    """窗口被移动/缩放后，让真正相邻的窗口跟着让位。

    复刻 Rectangle Pro 的 adjustAdjacentWindows(toResized:withInitialRect:)：
    把左半屏的窗口拖宽时，右半屏的窗口自动左移让位，不会互相压盖，也不留缝。
    只有沿移动轴投影重叠的窗口才算相邻；让位量受工作区边界限制，推不动就停。
    返回被调整的窗口数。
    """
    if not old_rect or not new_rect:
        return 0
    if old_rect == new_rect:
        return 0
    ms = monitors.enum_monitors()
    if not ms:
        return 0
    idx = _monitor_index_by_rect(ms, new_rect[0], new_rect[1],
                                 new_rect[2], new_rect[3])
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect
    # 取位移较大的那个轴作为联动轴
    dx = new_rect[0] - old_rect[0]
    dy = new_rect[1] - old_rect[1]
    if abs(dx) < abs(dy):
        axis, delta = "y", dy
    else:
        axis, delta = "x", dx
    if delta == 0:
        return 0
    others = []
    for w in list_target_windows():
        h = w.get("hwnd")
        if not h or h == hwnd:
            continue
        if w.get("pid") in _OWN_PIDS:
            continue
        r = (w["x"], w["y"], w["w"], w["h"])
        if not (mon.left <= r[0] + r[2] / 2.0 < mon.left + mon.width
                and mon.top <= r[1] + r[3] / 2.0 < mon.top + mon.height):
            continue  # 只联动同屏窗口
        others.append((h, r))
    if not others:
        return 0
    moved = 0
    for h, r in screen_math.adjust_adjacent_rects(
            new_rect, others, axis, delta, wl, wt, ww, wh):
        try:
            set_window_rect(h, r[0], r[1], r[2], r[3], activate=activate)
            moved += 1
        except Exception as e:  # noqa: BLE001
            logger.info("联动相邻窗口失败 %s: %s", h, e)
    if moved:
        logger.info("相邻窗口联动：%s 移动 %d，带动 %d 个窗口",
                    hwnd, delta, moved)
    return moved


def _monitor_index_by_rect(monitors_list, x, y, w, h):
    """按给定矩形（而非 hwnd）判断所在显示器索引：交集最长优先。"""
    return screen_math.monitor_index_by_intersection(monitors_list, x, y, w, h)


def _resolve_monitor_index(monitors_list, monitor):
    """把 monitor 参数（int 索引 / device_name 字符串 / None）解析为显示器索引。

    无法解析或越界时返回 None，调用方应回退到「按窗口所在屏」逻辑。
    """
    if monitor is None:
        return None
    if isinstance(monitor, int):
        return monitor if 0 <= monitor < len(monitors_list) else None
    if isinstance(monitor, str):
        for i, m in enumerate(monitors_list):
            if m.device_name == monitor or m.device_path == monitor:
                return i
    return None


def snap_two_side_by_side(left_hwnd=None, right_hwnd=None, use_pinned=True, monitor=None):
    """把两个窗口并排到同一屏的左右半屏（左/右可显式指定）。

    left_hwnd / right_hwnd 为 "应用::窗口" 标识；未指定时自动取最前面的两个
    窗口（左侧优先用界面固定的目标窗口）。monitor 可指定目标显示器
    （int 索引或 device_name 字符串）；为 None 时以左侧窗口当前所在屏幕为准。
    并排完成后把左右窗口激活到最前，确保并排结果可见、不被其他窗口遮挡。

    竖屏（portrait，高>宽）上自动改为「上/下各半屏」堆叠：竖屏宽度有限，
    左右并排两个窗口会挤成窄条、几乎不可用；上下堆叠才是竖屏的自然用法。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return False

    cands = [w for w in list_target_windows()
             if w["pid"] not in _OWN_PIDS and w["owner"]]
    # 自动补足时跳过 AX 够不着的 owner（如 wine）：它们会被选进四宫格，
    # 然后每一次读写几何都失败，整轮排列卡在重试里。
    cands = [w for w in cands if w["owner"] not in _QUAD_SKIP_OWNERS]
    cands_by_hwnd = {w["hwnd"]: w for w in cands}

    # 解析左窗口：显式指定 > 界面固定目标 > 最前面窗口
    left_w = None
    if left_hwnd:
        left_w = cands_by_hwnd.get(left_hwnd) or _find_window(left_hwnd)
    elif use_pinned and _pinned_target:
        left_w = _find_window(_pinned_target)
    if left_w is None:
        if not cands:
            logger.warning("没有可用于并排的窗口")
            return False
        left_w = cands[0]
    left_hwnd = left_w.get("hwnd") or make_hwnd(left_w["owner"], left_w["name"] or "")

    # 解析右窗口：显式指定 > 与左窗口不同的最前面窗口
    right_w = None
    if right_hwnd and right_hwnd != left_hwnd:
        right_w = cands_by_hwnd.get(right_hwnd) or _find_window(right_hwnd)
    if right_w is None:
        for w in cands:
            if w["hwnd"] != left_hwnd:
                right_w = w
                break
    if right_w is None:
        logger.warning("只找到一个可用窗口，无法并排")
        return False
    right_hwnd = right_w.get("hwnd") or make_hwnd(right_w["owner"], right_w["name"] or "")

    # 以左侧窗口当前所在的屏幕为准；若显式指定了显示器则用指定的
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, left_w["x"], left_w["y"], left_w["w"], left_w["h"])
    mon = ms[idx]

    # 登记几何，供后续 get_window_rect 使用
    _last_rect[left_hwnd] = (left_w["x"], left_w["y"], left_w["w"], left_w["h"])
    _last_rect[right_hwnd] = (right_w["x"], right_w["y"], right_w["w"], right_w["h"])

    wl, wt, ww, wh = mon.work_rect
    if mon.height > mon.width:
        # 竖屏：左右并排改为上/下堆叠（上半屏 / 下半屏）
        half_h = wh // 2
        set_window_rect(left_hwnd, wl, wt, ww, half_h, activate=True)
        set_window_rect(right_hwnd, wl, wt + half_h, ww, wh - half_h, activate=True)
        logger.info("竖屏并排(上下堆叠): 上=%s 下=%s（屏幕 %s）",
                    left_hwnd, right_hwnd, mon.device_name)
    else:
        half = ww // 2
        # 并排后激活左右两个窗口，让它们显示到所有窗口最前面（可见、不被遮挡）
        set_window_rect(left_hwnd, wl, wt, half, wh, activate=True)
        set_window_rect(right_hwnd, wl + half, wt, ww - half, wh, activate=True)
        logger.info("并排完成: 左=%s 右=%s（屏幕 %s）", left_hwnd, right_hwnd,
                    mon.device_name)
    return True


def snap_three_stack(top_hwnd=None, mid_hwnd=None, bot_hwnd=None, use_pinned=True, monitor=None):
    """把三个窗口堆叠到同一屏的上/中/下三栏（竖屏排列）。

    top/mid/bot_hwnd 为 "应用::窗口" 标识；未指定时自动取最前面的三个窗口
    （顶部优先用界面固定的目标窗口）。monitor 可指定目标显示器
    （int 索引或 device_name 字符串）；为 None 时以顶部窗口当前所在屏幕为准。
    排列完成后把三个窗口激活到最前，确保结果可见、不被遮挡。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return False

    cands = [w for w in list_target_windows()
             if w["pid"] not in _OWN_PIDS and w["owner"]]
    cands_by_hwnd = {w["hwnd"]: w for w in cands}

    # 解析顶部窗口：显式指定 > 界面固定目标 > 最前面窗口
    top_w = None
    if top_hwnd:
        top_w = cands_by_hwnd.get(top_hwnd) or _find_window(top_hwnd)
    elif use_pinned and _pinned_target:
        top_w = _find_window(_pinned_target)
    if top_w is None:
        if not cands:
            logger.warning("没有可用于三栏排列的窗口")
            return False
        top_w = cands[0]
    top_hwnd = top_w.get("hwnd") or make_hwnd(top_w["owner"], top_w["name"] or "")

    # 解析中部窗口：显式指定 > 与顶部不同的最前面窗口
    mid_w = None
    if mid_hwnd and mid_hwnd != top_hwnd:
        mid_w = cands_by_hwnd.get(mid_hwnd) or _find_window(mid_hwnd)
    if mid_w is None:
        for w in cands:
            if w["hwnd"] != top_hwnd:
                mid_w = w
                break
    if mid_w is None:
        logger.warning("只找到一个可用窗口，无法三栏排列")
        return False
    mid_hwnd = mid_w.get("hwnd") or make_hwnd(mid_w["owner"], mid_w["name"] or "")

    # 解析底部窗口：显式指定 > 与顶部/中部都不同的最前面窗口
    bot_w = None
    if bot_hwnd and bot_hwnd not in (top_hwnd, mid_hwnd):
        bot_w = cands_by_hwnd.get(bot_hwnd) or _find_window(bot_hwnd)
    if bot_w is None:
        for w in cands:
            h = w["hwnd"]
            if h != top_hwnd and h != mid_hwnd:
                bot_w = w
                break
    if bot_w is None:
        logger.warning("只找到两个可用窗口，无法三栏排列")
        return False
    bot_hwnd = bot_w.get("hwnd") or make_hwnd(bot_w["owner"], bot_w["name"] or "")

    # 以顶部窗口当前所在的屏幕为准；若显式指定了显示器则用指定的
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, top_w["x"], top_w["y"], top_w["w"], top_w["h"])
    mon = ms[idx]

    wl, wt, ww, wh = mon.work_rect
    third = wh // 3
    set_window_rect(top_hwnd, wl, wt, ww, third, activate=False)
    # 实测闭环：System Events 对窗口几何有系统级钳制（标题栏占住的顶部约 30px
    # 让窗口永远贴不到 work_top，竖屏大高度变化也常只应用一部分）。若按理想
    # thirds 继续写中/下两栏，三栏之间会出现缝隙或重叠——表现为「竖排不准」。
    # 因此上栏写完后读回实测几何，用它推导中/下栏的真实起点。
    t_rect = get_window_rect(top_hwnd)
    t_y = t_rect[1] if (t_rect and t_rect != (0, 0, 0, 0)) else wt
    t_h = t_rect[3] if (t_rect and t_rect[3] > 0) else third
    used = max(1, (t_y + t_h) - wt)
    m_y = wt + used + 1
    m_h = max(1, third - 1)
    set_window_rect(mid_hwnd, wl, m_y, ww, m_h, activate=False)
    m_rect = get_window_rect(mid_hwnd)
    m_h_used = m_rect[3] if (m_rect and m_rect[3] > 0) else m_h
    b_y = m_y + m_h_used + 1
    b_h = max(1, (wt + wh) - b_y)
    set_window_rect(bot_hwnd, wl, b_y, ww, b_h, activate=False)
    _osa('tell application "' + top_w['owner'] + '" to activate')
    logger.info("三栏排列完成: 上=%s 中=%s 下=%s（屏幕 %s）", top_hwnd, mid_hwnd,
                bot_hwnd, mon.device_name)
    return True


def _is_portrait(mon):
    """屏幕是否为竖屏（高大于宽）。"""
    return mon.height > mon.width


# 最近一次四宫格排列的实测报告：[(hwnd, 目标矩形, 实测矩形或None), ...]
# 供界面说明「哪些格子被应用的最小窗口尺寸钳制了」
_last_quad_report = []
# 写入无效的窗口（App 不开放自动化 / 被系统钉住）：几何一步都没变。
# 与「被最小尺寸钳制」是两回事——钳制会变，写不动是一步不动，状态栏要分开说。
_last_quad_unwritable = []


# 最近一次四宫格实际使用的比例 (split_x, split_y)，智能模式下即程序算出的值，
# 界面读它把拖拽分割线同步到真实比例
_last_quad_splits = (0.5, 0.5)


# 最近一次四宫格的实际分配顺序 [左上, 右上, 左下, 右下]（hwnd）。
# 智能模式会按窗口宽高比重排四个角，界面必须回报「谁最终去了哪个角」，
# 否则用户看到的下拉顺序与实际摆放不一致，就是「不准」。
_last_quad_order = [None, None, None, None]
_last_quad_boxes = [None, None, None, None]


# 最近一次四宫格「角 → hwnd」的实际分配。智能模式会按宽高比重排角的归属，
# 手动模式用户点掉几格后也不满四个，界面上的四个下拉必须按这个回填——否则
# 下拉显示的窗口与实际被摆到那个角的窗口不一致，就是「不准」。
_last_quad_slots = []


# 上次排布探到的 App 最小窗口尺寸 {hwnd: (w, h)}。容量预检 quad_capacity()
# 消费它：探测最小尺寸要把窗口缩小再读回，只能挂在正式排布上做，预检里探头
# 会把用户窗口无故缩小再还原（闪烁、白占一步撤销栈），所以这里只缓存只读。
_QUAD_MIN_CACHE = {}

# 上次排布探到的 App 尺寸上限 {hwnd: (max_w, max_h)}（0 = 该轴无上限）。
# 与最小尺寸同源：写入方向是「要求放大」却被读回更小，说明 App 把窗口钳在
# 更小处（真机「系统设置」最大窗宽 754、iPhone镜像 宽高比锁定）。四宫格
# 预演 quad_predict 消费它：点排列前就能说出「这一格会留多少空白」。
_QUAD_MAX_CACHE = {}

# 缓存文件条目上限：窗口关掉后条目只增不减，钉个顶，超出丢最早的
_QUAD_CACHE_MAX_ENTRIES = 500


def _quad_cache_file():
    """四宫格尺寸缓存文件路径（quad_cache.json）。

    与 settings.json 同目录：frozen 后在 ~/Library/Application Support/
    MultiMonManager，开发时在仓库目录。import settings 失败（极少数打包
    场景）时退回本文件所在目录。测试可整体替换此函数，把缓存指到临时
    目录，避免往仓库里写文件。
    """
    try:
        import settings
        base = settings.data_dir()
    except Exception:  # noqa: BLE001  缓存只是优化，读不到目录就退回本目录
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "quad_cache.json")


def _quad_cache_save():
    """把最小尺寸 / 尺寸上限缓存合并写盘（原子写，静默失败）。

    缓存的意义全在「下次点排列之前就能说话」：App 的最小尺寸与钳制上限是
    真机物理限制、跨会话不变，写盘后重启 App 的第一次预演就有据可依。写
    失败不该影响排布本身，一切异常只进日志。
    """
    try:
        data = {
            "version": 1,
            "mins": {h: [int(v[0]), int(v[1])]
                     for h, v in _QUAD_MIN_CACHE.items() if v},
            "maxes": {h: [int(v[0]), int(v[1])]
                      for h, v in _QUAD_MAX_CACHE.items() if v},
        }
        for _d in (data["mins"], data["maxes"]):
            while len(_d) > _QUAD_CACHE_MAX_ENTRIES:
                _d.pop(next(iter(_d)))     # dict 保序，先插入的就是最早的
        path = _quad_cache_file()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                   prefix=".quad_cache.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001  原子写中途失败：清掉半成品再抛
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:  # noqa: BLE001
        logger.warning("四宫格缓存写盘失败: %s", e)


def _quad_cache_load():
    """App 启动时调一次：把上次的尺寸缓存读进内存（静默失败）。

    不在 import 时调用：导入 windows_mac 的路径很多（测试、托盘面板），
    启动时显式调一次即可。文件损坏 / 格式不对当没有缓存——预演退化成
    静默放行（现行为），不该拦着用户排窗。
    """
    try:
        path = _quad_cache_file()
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        mins, maxes = {}, {}

        def _pair(v):
            return (isinstance(v, list) and len(v) == 2
                    and all(isinstance(x, int) and not isinstance(x, bool)
                            for x in v))

        for h, v in (data.get("mins") or {}).items():
            if _pair(v):
                mins[h] = (v[0], v[1])
        for h, v in (data.get("maxes") or {}).items():
            if _pair(v):
                maxes[h] = (v[0], v[1])
        _QUAD_MIN_CACHE.update(mins)
        _QUAD_MAX_CACHE.update(maxes)
        logger.info("四宫格缓存已读入：%d 个最小尺寸、%d 个尺寸上限",
                    len(mins), len(maxes))
    except Exception as e:  # noqa: BLE001
        logger.warning("四宫格缓存读盘失败，按无缓存处理: %s", e)


# 最近一次四宫格需要如实告知用户的情况（屏幕装不下四个最小尺寸、某窗口
# 被系统判给了别的屏……）。界面读它在状态栏里提示，而不是只说「排列完成」。
_last_quad_notes = []


def fill_all_monitors(hwnds=None, use_pinned=True, mode="max",
                      monitor_indices=None):
    """把若干窗口分别铺满各自的显示器，一个窗口一块屏。

    典型场景是四屏环境：四个窗口各占一块屏。已有能力都是「单屏内」排布
    （并排 / 竖排 / 四宫格），缺的是跨屏——要逐屏铺满得先跨屏移动再最大化，
    点好几次。

    hwnds: 显式指定窗口列表；为 None 时取最前面的若干个（数量 = 屏幕数，
    首个优先用界面固定的目标窗口）。窗口数少于屏幕数时按窗口数铺（多出来的屏
    不动），多于屏幕数时忽略多余窗口，绝不超屏摆放。

    mode: "max" 每块屏铺满整个工作区；"quad" 每块屏左上角四分之一。

    monitor_indices: 只铺这些下标的屏（按给定顺序）。None 时铺全部屏。
    竖屏常常不适合放全屏窗口（竖屏上的横屏应用会被压成一条），所以在界面上
    默认只勾横屏，用户可自行增删。

    窗口按枚举顺序与显示器一一对应（第 k 个窗口去第 k 块屏），这样「屏一 →
    屏二 → 屏三」的次序与界面显示器列表一致，结果可预期。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return 0

    if monitor_indices is not None:
        picked = []
        for i in monitor_indices:
            if isinstance(i, int) and 0 <= i < len(ms) and i not in picked:
                picked.append(i)
        ms = [ms[i] for i in picked]
        if not ms:
            logger.warning("没有可铺的目标屏")
            return 0

    if hwnds is None:
        cands = [w for w in list_target_windows()
                 if w["pid"] not in _OWN_PIDS and w["owner"]]
        picked = []
        seen = set()
        # 界面固定的目标窗口优先放在第一位（只有它，多个固定目标没有意义）
        first = _find_window(_pinned_target) if use_pinned and _pinned_target else None
        if first is not None:
            h = first.get("hwnd") or make_hwnd(first["owner"], first["name"] or "")
            picked.append(h)
            seen.add(h)
        for w in cands:
            if len(picked) >= len(ms):
                break
            h = w.get("hwnd") or make_hwnd(w["owner"], w["name"] or "")
            if h in seen:
                continue
            picked.append(h)
            seen.add(h)
        hwnds = picked

    if not hwnds:
        logger.warning("没有可用于跨屏铺满的窗口")
        return 0

    done = 0
    for hwnd, mon in zip(list(hwnds), ms):
        if not hwnd:
            continue
        try:
            if mode == "quad":
                wl, wt, ww, wh = mon.work_rect
                set_window_rect(hwnd, wl, wt, ww // 2, wh // 2, activate=False)
            else:
                set_window_rect(hwnd, mon.work_left, mon.work_top,
                                mon.work_width, mon.work_height, activate=False)
            done += 1
        except Exception:  # noqa: BLE001
            logger.warning("跨屏铺满失败: hwnd=%s 屏=%s", hwnd, mon.device_name,
                           exc_info=True)
    logger.info("跨屏铺满完成: %d 个窗口 -> %d 块屏（mode=%s）",
                done, len(ms), mode)
    return done

# ── 虚拟桌面网格（跨屏 m×n 批排）─────────────────────────────────────

# 最近一次虚拟网格需要如实告知用户的情况（哪个窗口放不下它那一格、哪个窗口
# 写入无效、哪个被应用钳制）。界面读它在状态栏里提示，而不是只说「排列完成」
# ——用户历史上反复抱怨「不准」，而多数不准其实是这三类物理限制。
_last_grid_notes = []

# 最近一次虚拟网格的实测报告 [(hwnd, 目标矩形, 实测矩形或None), ...]，
# 与 _last_quad_report 同用途：供界面说清「哪一格留了空白、为什么」。
_last_grid_report = []


def fill_grid(hwnds=None, cols=None, rows=None, layout=None, smart=False,
              monitor_indices=None, use_pinned=True):
    """把若干窗口排进跨屏虚拟桌面的 m×n（或加权比例）网格。

    与 fill_all_monitors（一个窗口一块屏）互补：那个是「屏多窗口少」，这个是
    「窗口多、屏要合起来用」——把勾选的多块屏当成一块大桌面，按比例切格子，
    一个格子一个窗口。等分（2×2 / 3×2 / 3×3）与加权（2/3+1/3）走同一条路，
    由 grid_plan 的比例元组描述：界面下拉选预设，Spinbox 自定义行列。

    三条不可动的约束（都是真机踩过的坑）：

    1. **格子绝不跨屏**。macOS 会把横跨两屏的窗口判给其中一块（真机右下窗
       x+w=2880 越过 DELL 右界，整个窗被搬到 Sidecar），用户看到的就是「四个
       窗口不在一个屏幕上」。grid_plan.assign_cells 把每格归到交集最大的屏并
       裁进那块屏的工作区，与任何屏无交集的格子（屏间空隙）直接丢掉。
    2. **装不下的窗口不硬排**。按实测最小尺寸逐格判，装不下的保持原位并在
       说明里逐个列出。硬排的结局是窗口被 App 顶出格子、压住邻居。
    3. **间距按贴边算**。网格线落在屏中间是窗间缝；格子被裁到屏边界后那条线
       已是屏幕外缘，再留缝就是「没吸附到边上」（cell_gap_rect 的判据）。

    layout 是 grid_plan.LAYOUTS 的显示名（界面下拉），优先于 cols/rows；
    两者都没给时按 2×2。

    monitor_indices: 只排这些下标的屏（按给定顺序）。None 时取全部屏——竖屏
    不适合放横屏应用，界面默认不勾，但用户显式勾了就尊重（函数层不猜）。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return 0

    if monitor_indices is not None:
        keep = []
        for i in monitor_indices:
            if isinstance(i, int) and 0 <= i < len(ms) and i not in keep:
                keep.append(i)
        ms = [ms[i] for i in keep]
        if not ms:
            logger.warning("虚拟网格：没有可用的目标屏")
            return 0

    # 上一轮的说明先清空：界面读 _last_grid_notes 做提示，留着旧值会把上次
    # 的「装不下」报在这一次成功的排列上
    global _last_grid_notes, _last_grid_report
    _last_grid_notes.clear()
    _last_grid_report = []

    # 1) 比例：预设（layout）优先，其次自定义行列，再退回 2×2
    if layout:
        got = grid_plan.layout_by_label(layout)
        if got is None:
            logger.warning("虚拟网格：未知布局 %r，按自定义行列处理", layout)
            got = (grid_plan.equal_fracs(cols or 2),
                   grid_plan.equal_fracs(rows or 2))
    else:
        got = (grid_plan.equal_fracs(cols or 2), grid_plan.equal_fracs(rows or 2))
    col_fracs, row_fracs = got
    n_cols, n_rows = len(col_fracs), len(row_fracs)
    if n_cols > grid_plan.MAX_COLS or n_rows > grid_plan.MAX_ROWS:
        logger.warning("虚拟网格：%d×%d 超出上限（最多 %d列×%d行）",
                       n_cols, n_rows, grid_plan.MAX_COLS, grid_plan.MAX_ROWS)
        return 0

    # 2) 虚拟桌面 = 勾选屏工作区的包围盒；按比例切格子；每格裁进各自的屏
    vr = grid_plan.virtual_rect(ms)
    if vr is None:
        return 0
    cells = grid_plan.grid_cells(vr[0], vr[1], vr[2], vr[3],
                                 col_fracs, row_fracs)
    asg = grid_plan.assign_cells(cells, ms)
    if not asg:
        _last_grid_notes.append("勾选的屏摆不出可用的网格（屏之间有重叠？）")
        logger.warning("虚拟网格：格子与任何屏都没有交集")
        return 0

    # 3) 选窗。显式 hwnd 优先（找不到的如实报，不静默换窗口——那正是
    #    「智能/非智能都不准」的观感来源之一）；没给就按 z 序取前 len(asg) 个，
    #    滤掉自身进程与 AX 够不着的 owner（wine 之类：选进来整轮排列都卡在
    #    读写失败上）。
    wins = []
    if hwnds:
        cands = [w for w in list_target_windows()
                 if w["pid"] not in _OWN_PIDS and w["owner"]]
        wins, missing = grid_plan.resolve_hwnds(hwnds, cands, _find_window)
        if missing:
            names = "、".join(x.split("::")[0] for x in missing[:3])
            _last_grid_notes.append(
                "指定的窗口 %s 已关闭/改名，未参与排列" % names)
            logger.warning("虚拟网格：指定的窗口 %s 已不可用", names)
    if not wins:
        cands = [w for w in list_target_windows()
                 if w["pid"] not in _OWN_PIDS and w["owner"]]
        cands = [w for w in cands if w["owner"] not in _QUAD_SKIP_OWNERS]
        wins = grid_plan.pick_windows(cands, len(asg),
                                      skip_owners=_QUAD_SKIP_OWNERS,
                                      own_pids=_OWN_PIDS)
        # 界面固定的目标窗口优先（与四宫格、跨屏铺满同一语义）：只有它时
        # 也排——单窗网格等价于把它铺到第一格
        if use_pinned and _pinned_target and not wins:
            pinned = _find_window(_pinned_target)
            if pinned is not None:
                wins = [pinned]
    if not wins:
        _last_grid_notes.append("没有可操作的窗口")
        logger.warning("虚拟网格：没有可操作的窗口")
        return 0

    # 4) 实测每个窗口的最小尺寸。格子装不下 App 的最小尺寸时窗口会被顶出格
    #    子、压住邻居，甚至被 macOS 判给别的屏。探测前记下当前几何，探测完放回
    #    原状——带着探测后的残局排，窗口会停在探测位置（真机 UU远程 就这么被
    #    判去过 Sidecar）。
    mins, orig_rect = {}, {}
    for w in wins:
        h = w.get("hwnd")
        try:
            orig_rect[h] = _readback_rect(h, (w["x"], w["y"], w["w"], w["h"]))
        except Exception:  # noqa: BLE001
            orig_rect[h] = None
        try:
            mins[h] = _probe_min_size(h, w["x"], w["y"])
        except Exception:  # noqa: BLE001
            mins[h] = (0, 0)
    for w in wins:
        h = w.get("hwnd")
        was = orig_rect.get(h)
        if (was and was != (0, 0, 0, 0)
                and (w["x"], w["y"], w["w"], w["h"]) != was):
            try:
                set_window_rect(h, was[0], was[1], was[2], was[3],
                                activate=False)
                _last_rect[h] = was
            except Exception:  # noqa: BLE001
                pass

    # 5) 装不下的窗口退出排列：fit_report 按最小尺寸逐格判。格子与窗口都按
    #    「格子的行主序号」对齐——第 k 个窗口去第 k 个格子，用户才可预期。
    #    窗口多于格子时，多出的窗口没有格子可去（保持原位，如实报告）。
    gap, edge_gap = _gap_settings()
    slots, too_small = grid_plan.fit_report(asg, wins, mins, gap, edge_gap, ms)
    if not slots:
        _last_grid_notes.append(
            "这些窗口的最小尺寸都放不进网格格子，未做排列")
        logger.warning("虚拟网格：%d 个窗口全部装不下各自格子", len(wins))
        return 0

    # 6) 智能分配：按宽高比把窗口贴到最合适的格子。口径与四宫格一致（比例差
    #    + 面积不足 + 最小尺寸软惩罚），n≤6 穷举、n>6 排序配对；perm[i] =
    #    窗口 i 占第几格。非智能时严格按顺序对应。
    #
    # 必须拿 **slots 里的窗口**去分配，不能拿 wins[:n]：fit_report 会把装不下
    # 的窗口从 slots 里剔掉，wins 与 slots 的下标从此错开——用 wins[:n] 会
    # 把一个已经被剔除的窗口分到格子上，而真正该排的窗口反而没格可去
    # （真机「智能/非智能都不准」的一部分来源）。slots[i][2] 就是与第 i 格
    # 配对的那个窗口 dict（fit_report 按 i 一并写入）。
    n = len(slots)
    slot_wins = [s[2] for s in slots]
    order = list(range(n))
    if smart and n > 1:
        sizes = [(a[5], a[6]) for a, _h, _w in slots]
        perm = grid_plan.assign_windows(slot_wins, sizes, smart=True)
        # assign_windows 返回 perm[i] = 窗口 i 占第几格；反排成
        # order[k] = 第 k 格放哪个窗口（在 slot_wins 里的下标）
        order = [0] * n
        for wi, ci in enumerate(perm):
            if 0 <= ci < n:
                order[ci] = wi

    # 7) 写入：每格一个窗口，贴所在屏的锚定边；读回实测，被 App 顶出格子时
    #    按锚定边收尾（绝不居中：居中写完不更新尺寸，且居中坐标常越出工作区，
    #    用户看到的就是「没吸附到边上」）。
    #
    # order 必须真的用上：智能分配算完的 order 若不被写入循环读，格子与窗口
    # 就仍是 fit_report 的顺序对应，勾不勾「智能」排出来一模一样——那就是
    # 用户说的「智能模式没效果」。
    report = []
    unwritable = []
    for k in range(n):
        row, col, mi, gx, gy, gw, gh = slots[k][0]
        hwnd = slot_wins[order[k]].get("hwnd") or slots[k][1]
        mon = ms[mi]
        try:
            pre = _cg_rect(hwnd)
            set_window_rect(hwnd, gx, gy, gw, gh, activate=False)
            got = _settled_readback(hwnd, (gx, gy, gw, gh))
            post = _cg_rect(hwnd)
            # 要求了一个有意义的变化、CG 几何却一步没变 = 窗口动不了：App 不
            # 开放自动化（真机微信、CrossPaste 恒报 -1719）或被系统钉死
            # （真机 Sidecar 上的 UU远程）。与「被最小尺寸钳制」分开说。
            if (pre and post and hwnd not in unwritable
                    and any(abs(p - t) > 2
                            for p, t in zip(pre, (gx, gy, gw, gh)))
                    and all(abs(p - t) <= 2 for p, t in zip(pre, post))):
                unwritable.append(hwnd)
                _last_grid_notes.append(
                    "%s 写入后位置尺寸都没变，无法移动"
                    "（不开放自动化或被系统钉住）" % hwnd.split("::")[0])
                logger.warning("虚拟网格：%s 写入无效：要求 %s，CG 几何仍是 %s",
                               hwnd, (gx, gy, gw, gh), post)
            if not got or got == (0, 0, 0, 0):
                report.append((hwnd, (gx, gy, gw, gh), None))
                _last_grid_notes.append("%s 读不到几何" % hwnd.split("::")[0])
                continue
            report.append((hwnd, (gx, gy, gw, gh), got))
            if abs(got[2] - gw) <= 8 and abs(got[3] - gh) <= 8:
                continue
            # 被 App 顶出格子：只重锚定贴边的那一侧（右列贴右、下行贴底），
            # 溢出只发生在远离锚定边的那一侧（BTT windowAnchor 同语义）。
            if hwnd in unwritable:
                continue
            ax, ay = gx, gy
            if col >= n_cols - 1:
                ax = gx + gw - got[2]
            if row >= n_rows - 1:
                ay = gy + gh - got[3]
            ax = max(int(mon.work_left),
                     min(ax, int(mon.work_left) + int(mon.work_width) - got[2]))
            ay = max(int(mon.work_top),
                     min(ay, int(mon.work_top) + int(mon.work_height) - got[3]))
            if (ax, ay) != (got[0], got[1]):
                set_window_rect(hwnd, ax, ay, got[2], got[3], activate=False)
            _last_grid_notes.append(
                "%s 实际 %dx%d（被应用最小尺寸顶大，格子 %dx%d）"
                % (hwnd.split("::")[0], got[2], got[3], gw, gh))
        except Exception:  # noqa: BLE001
            logger.warning("虚拟网格：写入 %s 失败", hwnd, exc_info=True)
            unwritable.append(hwnd)

    # 8) 装不下的逐个说明：排了几个、谁没排、为什么。用户才知道该关谁、换谁。
    for h, _w, r in too_small:
        mw, mh = mins.get(h, (0, 0))
        _last_grid_notes.append(
            "%s(最小%dx%d) 放不下它那一格（%dx%d），已保持原位"
            % (h.split("::")[0], mw, mh, r[2], r[3]))
    _last_grid_report = report
    logger.info("虚拟网格完成: %d/%d 个窗口 -> %d 格（%d列×%d行，%d 块屏）",
                len(report), len(wins), len(asg), n_cols, n_rows, len(ms))
    return len(report)


# ── 四宫格：最小尺寸实测与几何 ─────────────────────────────────────────

# 探测最小尺寸时请求的尺寸：宽高都明显小于常见 App 的下限，读回的就是下限
_PROBE_W, _PROBE_H = 320, 120

# 读回值必须比请求值大多少才承认是 App / 系统下限。差值不足说明写入已到达
# 目标尺寸——读回的是「这次写入的结果」，不是可缩空间的下限。
_PROBE_MIN_SLACK = 4

# 按住缩放角逐轮逼近的容差：读回连续两轮逐维一致才停。
_PROBE_RESIZE_TOL = 2

# 探测写入后等动画的时间（真机实测 0.3s 已稳定，留点余量）
_probe_settle_secs = 0.45


# 首轮之后每轮的轮询间隔：读回值要连续两次一致才认（见 _probe_min_size）
_probe_poll_secs = 0.15


# 最小尺寸探测最多读几轮。动画拖尾通常 2~3 轮内稳定；读满仍不一致取最后一次，
# 也远好过旧的「探两次取逐维更小」——那个会取到收缩中间帧。
_PROBE_MAX_READS = 6


def _fresh_rect(hwnd):
    """绕过 _last_rect 缓存读一次真实几何。

    get_window_rect 命中缓存时直接返回缓存值，而缓存可能是写入过程中的旧值 /
    动画中间帧。四宫格的闭环读回必须用实测值，否则读到的是自己刚写的目标值，
    察觉不到「窗口被系统搬到了别的屏」。
    """
    _last_rect.pop(hwnd, None)
    try:
        got = get_window_rect(hwnd)
    except Exception:  # noqa: BLE001
        return (0, 0, 0, 0)
    return got if got else (0, 0, 0, 0)


def _ax_read_rect(app, win_sel):
    """经 AX 选择子直接读一个窗口的 position/size；失败返回 None。"""
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    set p to position of {win_sel}\n'
        f'    set s to size of {win_sel}\n'
        f'    return (item 1 of p as text) & "," & (item 2 of p as text) '
        f'& "," & (item 1 of s as text) & "," & (item 2 of s as text)\n'
        f'  end tell\n'
        f'end tell\n'
    )
    out = _osa(script)
    try:
        x, y, w, h = _parse_nums(out)
        return (x, y, w, h)
    except Exception:  # noqa: BLE001
        return None


def _settled_readback(hwnd, target):
    """等窗口动画结束后再读回几何。

    System Events 写入后系统会播放移动/缩放动画，CG 立刻读到的是中间帧
    （真机实测写入 600 高立刻读回 297）。四宫格的闭环用中间帧算下排起点，
    会把下排顶到上排中间去——两排重叠 302px 就是这么来的。
    做法：先读一次，与目标偏差超过 8px 就再等 0.35s 读一次，取更接近
    目标的那次（最多两次，避免把四宫格拖得太慢）。
    """
    got = _readback_rect(hwnd, target)
    if not got or got == (0, 0, 0, 0) or not target:
        return got
    dev = max(abs(a - t) for a, t in zip(got, target))
    if dev <= 8:
        return got
    time.sleep(0.35)
    got2 = _readback_rect(hwnd, target)
    if not got2 or got2 == (0, 0, 0, 0):
        return got
    dev2 = max(abs(a - t) for a, t in zip(got2, target))
    return got2 if dev2 <= dev else got


def _readback_rect(hwnd, target):
    """写入后读回「刚被写入的那个窗口」的几何。

    get_window_rect 对无序号 hwnd 按 owner 兜底到该应用最前窗口——应用有多
    个同名窗口时（真机 UU远程 拉起 5 个空名窗口）会读到另一个窗口，把假几何
    喂给重锚/报告，甚至据此把正确的窗口搬到别的屏（真机实测 y=840 被拖去
    Sidecar）。这里按写入目标做消歧：多个同名窗口时取与目标矩形偏差最小者；
    CG 全列表都没有时（窗口在别的 Space）经 AX 几何选择子直读。
    """
    app, name, seq = parse_hwnd(hwnd)
    try:
        # 与 list_target_windows 用同一个枚举阈值：seq 序号是按那批窗口的
        # z 序分配的，这里若用 min_size=1 会把小窗也数进去，#3 就会指到
        # 另一个窗口（真机实测 #3 想读 1133x600 的主窗，读到 1133x44 的小条）
        cands = [w for w in list_windows_front_to_back()
                 if w["owner"] == app and (w["name"] or "") == (name or "")]
    except Exception:  # noqa: BLE001
        cands = []
    if cands and seq:
        if len(cands) >= seq:
            w = cands[seq - 1]
            return (w["x"], w["y"], w["w"], w["h"])
    elif cands:
        if len(cands) == 1:
            w = cands[0]
            return (w["x"], w["y"], w["w"], w["h"])
        # 多个同名窗口：优先用**写入前的已知几何**定位（写坏的正是那个），
        # 已知几何不可用时才退回按目标矩形找最近——目标矩形可能恰好等于
        # 另一个同名窗口的位置（真机实测 #1 的写入目标正好是 #3 的所在），
        # 那时按目标找会把 #1 读成 #3。
        known = _last_rect.get(hwnd)
        anchor = known if (known and known != (0, 0, 0, 0)) else target
        if anchor:
            ax_, ay_, aw_, ah_ = anchor

            def dev(w):
                return (abs(w["x"] - ax_) + abs(w["y"] - ay_)
                        + abs(w["w"] - aw_) + abs(w["h"] - ah_))

            best = min(cands, key=dev)
            rect = (best["x"], best["y"], best["w"], best["h"])
            _last_rect[hwnd] = rect
            return rect
    # CG 没有该窗口（可能在别的 Space / 刚被系统隐藏）：走 AX 直读，
    # 选择子与 set_window_rect 相同——按已知几何匹配，读的就是写的那个。
    known = _last_rect.get(hwnd) or target
    sel = (_ax_window_by_geometry(app, known, name)
           or _ax_window_selector(hwnd) or "window 1")
    rect = _ax_read_rect(app, sel)
    if rect:
        _last_rect[hwnd] = rect
        return rect
    return _fresh_rect(hwnd)


def _probe_resize(hwnd, app, sel, at_x, at_y, target):
    """把窗口缩到 target 并读回稳定后的 (w, h)。

    反复写同一个目标尺寸，直到动画稳定。返回读不到几何时是 None。
    """
    script = (
        'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    set position of {sel} to {{{int(at_x)}, {int(at_y)}}}\n'
        f'    set size of {sel} to {{{int(target[0])}, {int(target[1])}}}\n'
        '  end tell\n'
        'end tell\n'
    )
    last = None
    same = 0
    for i in range(_PROBE_MAX_READS):
        _osa(script)
        time.sleep(_probe_settle_secs if i == 0 else _probe_poll_secs)
        got = _readback_rect(hwnd, (int(at_x), int(at_y),
                                    int(target[0]), int(target[1])))
        if not got or got == (0, 0, 0, 0):
            continue
        cur = (got[2], got[3])
        if cur[0] <= 0 or cur[1] <= 0:
            continue
        if last is not None and all(abs(a - b) <= _PROBE_RESIZE_TOL
                                    for a, b in zip(cur, last)):
            same += 1
            if same >= 2:
                return cur
        else:
            same = 0
        last = cur
    return last


def _probe_min_size(hwnd, at_x, at_y):
    """实测一个窗口的最小可用尺寸：按住缩放角逐步收紧，直到读回不再变小。

    macOS 上查不到这个值——真机实测 ChatGPT / Qoder CN 等窗口根本没有
    AXMinSize 属性，只能写小再读回。返回 (min_w, min_h)；读不到时返回 (0, 0)。

    旧探针只把窗口压到 (_PROBE_W, _PROBE_H) 然后读回。问题是 App 不给下限时
    macOS 会照常执行缩放：稳定值就是这次写入的结果，不是可缩空间的下限。真机
    实测「系统设置」被探成 754x540，四宫格留出 754 宽，但正式写入能收进 488
    高的格子；「iPhone镜像」反过来探成 204x452。二者都造成几百像素偏差。
    正确做法是模拟人手拖缩放角：一次比一次小，读回不再变小（或小于合理值）才
    承认是真实下限。
    """
    app, name, _seq = parse_hwnd(hwnd)
    # 探测前刷新一次真实几何：_last_rect 可能是上一个窗口刚搬完留下的旧值，
    # 拿它算 AX 选择子必然偏差过大（真机日志 999~1176px），选择子落到别的
    # 同名窗口上，整轮最小尺寸探测就是错的。
    _fresh = _fresh_rect(hwnd)
    if _fresh and _fresh != (0, 0, 0, 0):
        _last_rect[hwnd] = _fresh
    # 探测用的选择子在探测前算好：探测会把窗口缩得很小，之后任何按几何
    # 匹配都会因偏差过大而失效（真机实测偏差 2939），序号选择又会落到别的
    # 同名窗口上——这正是「四宫格把窗口送到 Sidecar」的根因。
    sel = (_ax_window_by_geometry(app, _last_rect.get(hwnd), name)
           or _ax_window_selector(hwnd) or "window 1")
    # 第一次必须读到一个明显大于目标的值；没到说明这条路探不出真实下限。
    first = _probe_resize(hwnd, app, sel, at_x, at_y,
                          (_PROBE_W, _PROBE_H))
    if not first:
        logger.warning("最小尺寸探测读不到几何: %s", hwnd)
        return (0, 0)
    # 多窗串读保险：AX 选择子可能按旧几何错位，写入小尺寸后读到另一个窗口。
    # 探测期间先回到被写窗口再确认一次；如果读回仍在屏幕远处，说明不是
    # 被探测窗口，放弃下限推断以免把别人的几何当成自己的下限。
    pos = _readback_rect(hwnd, (int(at_x), int(at_y),
                                _PROBE_W, _PROBE_H))
    if pos and pos[:2] != (int(at_x), int(at_y)):
        logger.warning("最小尺寸探测读回串窗: %s", hwnd)
        return (0, 0)
    if any(b < a + _PROBE_MIN_SLACK
           for a, b in zip((_PROBE_W, _PROBE_H), first)):
        return first
    # 缩放角逼近：宽/高交替收紧到上轮的 0.7，最少留 24px。读回不再变小
    # （连续两次相同）说明触到了 App 或系统下限；目标过小则停在合理值。
    prev = first
    for i in range(6):
        tw = max(24, int(round(prev[0] * 0.7)))
        th = max(24, int(round(prev[1] * 0.7)))
        if (tw, th) == prev:
            break
        got = _probe_resize(hwnd, app, sel, at_x, at_y, (tw, th))
        if not got:
            break
        if got[0] > prev[0] + _PROBE_RESIZE_TOL or got[1] > prev[1] + _PROBE_RESIZE_TOL:
            # 往小写反而变大：App 把窗口撑回去了，上一轮就是下限。
            break
        if (got[0] < prev[0] - _PROBE_RESIZE_TOL
                or got[1] < prev[1] - _PROBE_RESIZE_TOL):
            prev = got
            continue
        prev = got
        break
    return prev


def _cg_rect(hwnd):
    """窗口的当前几何（CoreGraphics 枚举，不走 AppleScript）。

    判断「写入有没有生效」必须用 CG：App 不开放自动化时 AX 读写都失败（真机
    微信、CrossPaste 的 AppleScript 恒报 -1719），那时候只有 CG 还看得见这个
    窗口。get_window_rect 带 _last_rect 缓存，会把上一次的几何当成当前值，
    所以这里直接枚举、按 (owner + name [+ 序号]) 精确匹配。
    """
    if not hwnd:
        return None
    app, name, seq = parse_hwnd(hwnd)
    ordinal = 0
    for w in list_windows_front_to_back():
        if w.get("owner") != app:
            continue
        if (w.get("name") or "") != (name or ""):
            continue
        ordinal += 1
        if seq and ordinal != seq:
            continue      # 带序号的 hwnd 只认同名第 N 个
        return (w["x"], w["y"], w["w"], w["h"])
    return None
def _quad_split(total, need_a, need_b, base_a, base_b):
    """把 total 切成 (a, b)：先满足两侧需求，再按原始占比分余量。

    need_a/need_b 是两侧的最小需求（已含窗口间距），base_a/base_b 是 plan 给出
    的原始比例切分毛值。装得下就不动（保留 plan 精心算出的比例）；装不下先各自
    满足需求，剩余按 base 占比分；需求本身就超过 total 时只能按需求占比硬分
    （允许溢出，由后面的贴边逻辑收尾，并在说明里如实报告）。
    """
    if need_a + need_b <= total:
        if base_a >= need_a and base_b >= need_b:
            return base_a, total - base_a
        if need_a and need_b:
            slack = total - need_a - need_b
            share = ((base_a / float(base_a + base_b))
                     if (base_a + base_b) > 0 else 0.5)
            a = min(total - need_b, need_a + int(round(slack * share)))
            return max(a, need_a), total - max(a, need_a)
        if need_a:
            a = min(need_a, total)
            return a, total - a
        b = min(need_b, total)
        return total - b, b
    if need_a + need_b <= 0:
        share = (0.5 if (base_a + base_b) <= 0
                 else base_a / float(base_a + base_b))
        a = int(round(total * share))
        return a, total - a
    a = int(round(total * need_a / float(need_a + need_b)))
    return a, total - a


def _quad_gap_zone(corner, cells, ww, wh):
    """按格子实际形状选间距方位（替代按 row_full/col_full 整排判的旧逻辑）。

    格子被拉通整宽（bw>=ww，三窗 T 形某排只占一格）时它贴上/下屏幕边，不再是
    格子之间的横缝；格子撑满高（bh>=wh，某列只占一格上下堆叠）时同理贴左/右
    屏幕边。按死角的四宫格方位会让窗口平白让出半条缝、看起来没贴到边。否则按
    标准四宫格方位留缝（quad-tl/tr/bl/br）。
    """
    _x, _y, bw, bh = cells[corner]
    if bw >= ww:
        return "top" if corner in (0, 1) else "bottom"
    if bh >= wh:
        return "left" if corner in (0, 2) else "right"
    return _QUAD_GAP_ZONE[corner]


def _quad_gap_insets(shape, corner, half, edge_gap):
    """这一格四边各让出多少间距：(左, 上, 右, 下)，与 gap_rect 同语义。

    判据与 _quad_gap_zone 一致，拆成轴用：拉通整宽的格子只让左右（它贴上/
    下屏幕边，上下没有邻居），撑满高的格子只让上下。算「格子至少要多大才装得
    下窗口的最小尺寸」时必须用这个值——整宽格的上下根本没有缝，为不存在的缝
    预留高度只会把这一排无谓顶高。
    """
    ri = 0 if corner in (0, 1) else 1
    ci = 0 if corner in (0, 2) else 1
    if shape["wide"][ri] or shape["col_full"]:
        zone = "top" if ri == 0 else "bottom"
    elif shape["row_full"]:
        zone = "left" if ci == 0 else "right"
    else:
        zone = _QUAD_GAP_ZONE[corner]
    l, t, r, b = _GAP_ZONES[zone]
    # 贴工作区外边：只有勾选屏幕边间距时才让（gap_rect 里的 l or edges）
    if edge_gap:
        if ci == 0:
            l = 1
        if ci == 1:
            r = 1
        if ri == 0:
            t = 1
        if ri == 1:
            b = 1
    return (half if l else 0.0, half if t else 0.0,
            half if r else 0.0, half if b else 0.0)


def _pick_quad_screen(ms, cur_idx, corners):
    """给四宫格挑一块放得下四格最小尺寸的屏；没有合适的就返回当前屏。

    当前屏放得下就不换，避免窗口白跑一趟。要换时只在横屏里挑——竖屏上排四个
    窗口并不好用（用户明确说过「不适合竖屏的窗口不要发到竖屏屏幕」）。
    优先换到完全放得下的横屏；一块都放不下时，也要离开竖屏，退而选面积最大
    的横屏——留一点重叠（允许上下重叠即可排下）也比把四个窗口挤在竖屏上好，
    这正是用户报的场景：竖屏 DELL 上四宫格挤成一团。
    """
    need_w, need_h = quad_plan.quad_requirements(corners)
    cur = ms[cur_idx]
    cur_wl, _t, cur_ww, cur_wh = cur.work_rect
    cur_fits = need_w <= cur_ww and need_h <= cur_wh
    if cur_fits and cur.height <= cur.width:
        return cur_idx
    fits, landscape = [], []
    for i, m in enumerate(ms):
        if m.height > m.width:      # 竖屏：不主动送
            continue
        _ml, _mt, mw, mh = m.work_rect
        landscape.append(i)
        if need_w <= mw and need_h <= mh:
            fits.append(i)
    # 完全放得下的横屏优先；都放不下就退到面积最大的横屏（离开竖屏）。
    # 横屏一块都没有（例如只有一块竖屏外接）才维持原地。
    pool = fits or landscape
    if not pool:
        return cur_idx
    return max(pool, key=lambda i: ms[i].work_rect[2] * ms[i].work_rect[3])


def _quad_place(pool, mins, pool_corners, ms, idx, gap, edge_gap,
                allow_switch, beat=None):
    """选子集，并在装不下时换一块能多排几个的横屏。返回 (idx, slots, unplaced)。

    抽出成函数是为了两处共用：排布前的子集选择，以及写入后发现「这块屏其实装
    不下」时的减载（见 snap_four_quad 的装载复检）。两处口径必须一致，否则预检
    说装得下、减载时又按另一套算法挑子集，用户看到的就是「说不排又不排」。

    beat: 别的屏至少要排几个才值得换过去。缺省 = 当前屏排得下的数量，也就是
    「当前屏已经装不下全部候选时才去看别的屏」；装载复检传 len(placed)-1：
    那块屏只要能排同样多就不必丢窗口，优先换屏而不是减载。
    """
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect
    placed_slots, unplaced = quad_plan.choose_quad_slots(
        pool, mins, ww, wh, gap, edge_gap, corners=pool_corners,
        origin=(wl, wt))
    if not allow_switch:
        return idx, placed_slots, unplaced
    bar = len(placed_slots) if beat is None else beat
    cur, best = idx, (idx, placed_slots, unplaced)
    for i, m in enumerate(ms):
        if i == idx or m.height > m.width:      # 竖屏不主动送
            continue
        _ml, _mt, mw2, mh2 = m.work_rect
        slots2, unplaced2 = quad_plan.choose_quad_slots(
            pool, mins, mw2, mh2, gap, edge_gap, corners=pool_corners,
            origin=(_ml, _mt))
        # >= len(best[1]) 而不是 >：装载复检传的 bar 比当前数量少 1，另一块屏
        # 「排得一样多」也值得换——当前屏已经装不下了，换过去照样排这么多个
        if len(slots2) > bar and len(slots2) >= len(best[1]):
            logger.info("屏幕 %s 只能排 %d 个，%s 能排 %d 个，改排到 %s",
                        ms[cur].device_name, len(best[1]),
                        m.device_name, len(slots2), m.device_name)
            cur, best = i, (i, slots2, unplaced2)
    return best


def _quad_needs(placed_slots, mins, shape, gap, edge_gap):
    """每列/排的最小尺寸需求（含窗口间距）：返回 (need_col, need_row)。

    「这块屏放不放得下」和「格子切多大」必须用同一个口径：预检说放得下、切出来
    却装不下，就是用户看到的「不准」。所以需求算法只留这一份，容量预检
    （choose_quad_slots）与 _quad_cells 都调它。

    只按实际排放的窗口算：放不下的窗口已经把角让出来了，它的最小尺寸再抬高
    某一排/列只会压小活窗口的空间。探测不到最小尺寸（0）的窗口不设需求——
    窗口间距不是硬约束（格子比间距还窄时间距逻辑整体放弃），给未知窗口记上
    6px 幽灵需求会让 _quad_split 走「按占比分余量」分支，把实测 606 的上排
    顶到 840。需求按格子形状拆轴算：整宽格的上下没有邻居，撑满高的格子左右
    没有邻居。
    """
    half = gap / 2.0
    need_col = [0, 0]
    need_row = [0, 0]
    for corner, h, _w in placed_slots:
        ci = 0 if corner in (0, 2) else 1
        ri = 0 if corner in (0, 1) else 1
        mw, mh = mins.get(h, (0, 0))
        gl, gt, gr, gb = _quad_gap_insets(shape, corner, half, edge_gap)
        if mw > 0:
            need_col[ci] = max(need_col[ci], int(round(mw + gl + gr)))
        if mh > 0:
            need_row[ri] = max(need_row[ri], int(round(mh + gt + gb)))
    return need_col, need_row


def _quad_cells(placed_slots, mins, wl, wt, ww, wh, gap, edge_gap,
                sx, sy, maxes=None, manual_split=False):
    """按形状 + 需求 + 比例切出格子与写入目标。

    placed_slots: [(corner, hwnd, win_dict), ...] 实际排放的窗口（1~4 个）
    mins: {hwnd: (min_w, min_h)} 实测最小尺寸
    返回 (cells, finals, splits)：cells 是四个格子的原始矩形（空格子宽或高为
    0），finals 是让出窗口间距后的写入目标 [(corner, hwnd, x, y, w, h)]，
    splits 是实际生效的 (col_split, row_split)——界面据此画分割线，必须按校正
    后的真实格子算，否则「分割线与实际位置对不上」。

    形状由 quad_plan.quad_shape 定：三窗 T 形时那一格拉通整宽、两窗斜角上下
    堆叠——旧实现把空角留成整块四分之一屏，用户看到的就是「没排好」的死区。
    格子严格铺满工作区：最小尺寸只通过 _quad_split 把某一排/列切大（装得下才
    调），不改变「谁去哪个角」。整排/整列空着时有窗口的那排/列撑满整个工作区
    （选两个窗口就该铺满屏幕，留半屏空白就是「没排好」）。

    抽成函数是为了二趟重排：第一遍按探测到的最小尺寸切，写入后读回的是窗口
    **实际**占的尺寸，调用方把实测尺寸并回 mins 后再调一次，格子随之收敛。
    """
    occ = [c for c, _h, _w in placed_slots]
    shape = quad_plan.quad_shape(occ)
    need_col, need_row = _quad_needs(placed_slots, mins, shape, gap, edge_gap)
    # 实测上限：App 拒绝长到目标时（真机「系统设置」最大窗宽 754），
    # 把多出来的空间让给另一侧，而不是留出一个 450px 的空格子让用户看成
    # 「不准」。maxes[h] = (max_w, max_h)；没有实测上限的轴按原逻辑走。
    # manual_split 由调用方传：智能模式算出的 sx/sy 不是 0.5，不能据此
    # 误判成手动比例。真正的手动 split_x/split_y 会破坏上限重分配契约。
    maxes = maxes or {}
    cap_col = [None, None]
    cap_row = [None, None]
    if maxes and not manual_split:
        half = gap / 2.0
        for corner, h, _w in placed_slots:
            mx = maxes.get(h)
            if not mx:
                continue
            gl, gt, gr, gb = _quad_gap_insets(shape, corner, half, edge_gap)
            ci = 0 if corner in (0, 2) else 1
            ri = 0 if corner in (0, 1) else 1
            if mx[0] > 0:
                cap_col[ci] = max(cap_col[ci] or 0, int(round(mx[0] + gl + gr)))
            if mx[1] > 0:
                cap_row[ri] = max(cap_row[ri] or 0, int(round(mx[1] + gt + gb)))
    base_c1 = int(round(ww * sx))
    base_r1 = int(round(wh * sy))
    # row_full / col_full / wide 都由 quad_shape 定，quad_cells 按 c1/r1 落地：
    # 只有一排/列有窗口时那一排/列撑满；某排只占一格那一格拉通整宽（T 形）
    if shape["row_full"]:
        # 一排撑满时没有上下邻居，不能被 cap_row 硬切小；但 cap_row 仍然有用：
        # 下排若有窗口、高度被钳住，上排可以先按 cap_row[0] 切，余量给下排。
        if (cap_row[0] is not None and cap_row[1] is not None
                and cap_row[0] + cap_row[1] > wh + _QUAD_SLACK_TOL
                and cap_row[1] <= wh):
            r1 = max(0, wh - cap_row[1])
        else:
            r1 = wh if shape["cnt_row"][0] else 0
    elif (cap_row[0] is not None
          and cap_row[0] + need_row[1] <= wh + _QUAD_SLACK_TOL):
        # 与列同理：两排都有上限时按上限贴边，剩余给另一排。双上限时上排
        # 是限制因素就取其上限当格高，下排在格内贴上排锚定，余量落屏幕
        # 外缘（下排窗口贴邻居，不贴屏幕外缘——与列分支同一语义）。
        if cap_row[1] is not None:
            r1 = max(0, min(cap_row[0], wh - cap_row[1]))
        else:
            r1 = min(wh, cap_row[0])
        # 上限不能让需求落空：本排有窗口实测要 606 高、上限却只有 576（上限
        # 来自同排另一个窗口的最大高度）——压到 576，要 606 的窗口必溢出到
        # 下排，用户看到的就是「上下重叠」。需求优先，上限只用来收余量。
        if need_row[0] > r1:
            r1 = min(wh, need_row[0])
    elif (cap_row[1] is not None
          and need_row[0] + cap_row[1] <= wh + _QUAD_SLACK_TOL):
        r1 = max(0, wh - cap_row[1])
    else:
        r1, _r2 = _quad_split(wh, need_row[0], need_row[1], base_r1,
                              wh - base_r1)
    if shape["col_full"]:
        c1 = ww if shape["cnt_col"][0] else 0
    elif (cap_col[0] is not None
          and cap_col[0] + need_col[1] <= ww + _QUAD_SLACK_TOL):
        # 左列有上限。若右列也有上限，两列都按各自上限贴边：右侧窗口贴右边
        # 界、吃自己的上限，剩余全给左列（左列的上限只是「不超过」，不是
        # 「必须等于」——真机系统设置 754 是最大窗宽，格子给 1264 它也只占
        # 754，多出来的 510px 会变成右列的空白）。只有右列没有上限时才把
        # 左列压到 cap，否则左列格子比窗口大一大截，用户看到的就是「不准」。
        if cap_col[1] is not None:
            # 双上限贴邻居：左列是限制因素时左格=其上限，右窗在格内贴左
            # 界锚定，余量落屏幕右缘；右列上限更大时左窗撑满、右窗贴右
            # 缘。旧实现取 ww - cap_col[1]，左窗够不到上限时间隙留在两
            # 个窗口之间——用户看到的就是「不准」（真机 516px 缝）。
            c1 = max(0, min(cap_col[0], ww - cap_col[1]))
        else:
            c1 = min(ww, cap_col[0])
        # 与排同款：左列需求大于上限时（上限可能来自右列窗口的最大宽）
        # 需求优先，否则左窗必溢出到右列——用户看到的就是「左右重叠」。
        if need_col[0] > c1:
            c1 = min(ww, need_col[0])
    elif (cap_col[1] is not None
          and need_col[0] + cap_col[1] <= ww + _QUAD_SLACK_TOL):
        # 只有右列有上限：把它紧贴右边界，左列吃剩余空间。
        c1 = max(0, ww - cap_col[1])
    else:
        c1, _c2 = _quad_split(ww, need_col[0], need_col[1], base_c1,
                              ww - base_c1)
    cells = quad_plan.quad_cells(shape, c1, r1, wl, wt, ww, wh)
    # 窗口在格子里直接填满自己那一格 + 让出窗口间距（与单窗 quad-* 吸附同语义：
    # 用户点「2×2 排列」要的就是四块铺满）。间距方位按格子实际形状判。
    finals = []
    for corner, h, _w in placed_slots:
        bx, by, bw, bh = cells[corner]
        bx, by, bw, bh = gap_rect(
            _quad_gap_zone(corner, cells, ww, wh),
            bx, by, bw, bh, wl, wt, ww, wh)
        finals.append((corner, h, bx, by, max(1, bw), max(1, bh)))
    # 实际生效的分割比例按**占用格子的真实几何**算。T 形下某个格子拉通整宽，
    # 直接拿 cells[0] 会把真正的列切分丢掉（cells[0] 可能就是整宽格）。
    occ_cells = [cells[c] for c, _h, _w in placed_slots]
    col_split = (1.0 if all(bw >= ww for _x, _y, bw, _bh in occ_cells)
                 else c1 / float(ww or 1))
    row_split = (1.0 if all(bh >= wh for _x, _y, _bw, bh in occ_cells)
                 else r1 / float(wh or 1))
    splits = (min(max(col_split, 0.05), 1.0),
              min(max(row_split, 0.05), 1.0))
    return cells, finals, splits


def _quad_overload(placed_slots, mins, ww, wh, gap, edge_gap):
    """按当前最小尺寸，这个子集还装不装得下这块屏（两个方向都要装下）。

    判据与 choose_quad_slots 的容量预检同源（都走 _quad_needs），只差在用的是
    哪一份最小尺寸：预检用「写入前探测值」，这里用「写入后实测值」。探测偏小
    （取到收缩中间帧）时两者能差出 170px 以上——预检说装得下，写进去窗口被
    App 撑大，两个方向的需求随之超过工作区，这时候照原样切格子只能按需求占比
    硬分，上下两排互相压住。用户看到的就是「不准」，所以写入后必须再判一次。

    探测有 1~2px 抖动，总和只超屏几个像素时允许微重叠——零容差会把差几像素
    就能放下四格的窗口全丢掉。与 quad_plan._SLACK_TOL 保持一致。
    """
    if not placed_slots:
        return False
    shape = quad_plan.quad_shape([c for c, _h, _w in placed_slots])
    need_col, need_row = _quad_needs(placed_slots, mins, shape, gap, edge_gap)
    tol = getattr(quad_plan, "_SLACK_TOL", 6)
    return (need_col[0] + need_col[1] > ww + tol
            or need_row[0] + need_row[1] > wh + tol)


def _quad_smart_ratio(placed_slots, mins, ww, wh, sx, sy, origin=(0, 0)):
    """智能比例按**实际排放**的子集重算，返回 (sx, sy)。失败时原样返回。

    第一轮的 sx/sy 里放不上的那几个窗口也占了比例，拿它切两个窗口的屏必然歪
    （用户报的「2×2 不准」多半源于此）。减载之后子集又少一个，必须按新的子集
    再算一次——否则大窗口按三窗的比例切两格，格子还是歪的。
    """
    if len(placed_slots) < 2:
        return sx, sy
    try:
        sub_wins = []
        for corner, h, w in placed_slots:
            d = dict(w)
            d.update(hwnd=h, min_w=mins.get(h, (0, 0))[0],
                     min_h=mins.get(h, (0, 0))[1])
            sub_wins.append(d)
        sub_corners = [c for c, _h, _w in placed_slots]
        _sub, sx_s, sy_s = quad_plan.plan_quad_layout(
            sub_wins, ww, wh, corners=sub_corners, origin=origin)
        sx = min(max(sx_s, 0.15), 0.85)
        sy = min(max(sy_s, 0.15), 0.85)
        logger.info("智能比例按实际排放的 %d 个窗口重算：横向 %.0f%% / 竖向 %.0f%%",
                    len(placed_slots), sx * 100, sy * 100)
    except ValueError as e:  # noqa: BLE001
        logger.warning("智能比例重算失败，沿用原值: %s", e)
    return sx, sy


def _gap_rect_raw(r, wl, wt, ww, wh):
    """矩形与工作区交集，绕过 _last_gap_all。

    _quad_anchor 的右/下边界计算必须保留被 App 撑大后越出工作区的原始
    负值（用于取「贴远侧」的 min），不能被 gap_rect 全局缓存改成 0。
    """
    return (max(r[0], wl), max(r[1], wt),
            max(1, min(r[0] + r[2], wl + ww) - max(r[0], wl)),
            max(1, min(r[1] + r[3], wt + wh) - max(r[1], wt)))


def _quad_anchor(corner, fx, fy, fw, fh, gw, gh, wl, wt, ww, wh):
    """窗口被 App 最小尺寸顶大/顶小后，按方位把该贴的边贴住的目标位置。

    锚定基准是 finals 里让出间距后的目标矩形（fx, fy, fw, fh），不是原始
    格子：屏幕边留缝（edge_gap）时格子右边就是屏幕边，照它贴会把缝吃成
    0——用户看到的「没吸附到边上」正是这么来的。语义同 BTT 的
    windowAnchor：锚定角永远贴住，溢出只发生在远离锚定角的那一侧；同时
    不越出工作区，越界会被 macOS 判到别的屏（真机实测 ChatGPT 被搬到
    RV200 Pro）。
    """
    ax, ay = fx, fy
    # 被钳小（读回比目标小）与被顶大（读回比目标大）锚定语义相反：钳小
    # 时窗口贴目标矩形的内侧边（挨着邻居），顶大时才贴屏幕外缘防越界
    # 搬屏。两轴独立：宽高比联动的窗口（真机 iPhone镜像）宽钳缩、高
    # 不钳，各轴各贴各的。
    shrink_w = gw < fw - 8
    shrink_h = gh < fh - 8
    # 目标矩形可能因 App 上限撑出工作区（真机系统设置最大化后仍被钳在
    # 754，撑大目标时右缘可到 1526）。gap_rect 会把越界裁到 0，导致
    # min/max 顺序失效、窗被推到屏幕左侧。这里用交集绕过 _last_gap_all，
    # 保留原值以维持 BTT 语义：贴边角永远贴住，溢出只发生在远离角的一侧。
    if corner in (1, 3) and not shrink_w:   # 右列未被钳小：贴目标矩形右边
        _rx = _gap_rect_raw((fx, fy, fw, fh), wl, wt, ww, wh)
        ax = min(max(_rx[0] + _rx[2] - gw, wl),
                 max(fx, _gap_rect_raw((fx, fy, fw, fh), wl, wt, ww, wh)[0]
                     + _rx[2] - gw))
    else:                          # 左列：贴目标矩形左边
        ax = max(min(fx, wl + ww - gw), wl)
    if corner in (2, 3) and not shrink_h:   # 下排未被钳小：贴目标矩形底边
        _by = _gap_rect_raw((fx, fy, fw, fh), wl, wt, ww, wh)
        ay = min(max(_by[1] + _by[3] - gh, wt),
                 max(fy, _by[1] + _by[3] - gh))
    else:                          # 上排：贴目标矩形上边
        ay = max(min(fy, wt + wh - gh), wt)
    return ax, ay


def _quad_write(finals, cells, wl, wt, ww, wh, smart, unwritable, notes,
                detect_unwritable=True):
    """把 finals 写进窗口，读回实测几何，再按方位贴边收尾。

    返回 {hwnd: 实测矩形}：调用方据此判断「窗口被 App 钳得比格子大」，用实测
    尺寸重算格子再写一遍（见 snap_four_quad 的二趟重排）。

    detect_unwritable=False 时跳过「写入无效」判定：第二趟的「写入前几何」就是
    第一趟的结果，被钳制的窗口两趟读回相同，会被误判成「一步没动、写不动」。
    """
    measured = {}
    clamped = {}
    for corner, h, fx, fy, fw, fh in finals:
        # 写入前先取 CG 几何：判「写入有没有生效」用它，不用 _last_rect（探测
        # 失败时它可能停留在很久以前的枚举值），也不用 AX 读回（App 不开放
        # 自动化时读回直接失败，真机微信、CrossPaste 恒报 -1719）
        # 写入前几何始终读：钳制判定要看「这次写入是放大请求还是缩小请求」
        # ——宽高比锁定的窗口（真机 iPhone镜像 ~0.455）收缩时宽高联动，
        # 读回比目标小是几何约束不是尺寸上限；当上限记进 maxes 会永久
        # 卡死二趟重排（min(old,new) 只收紧不放宽）。CG 枚举不走
        # AppleScript，便宜；detect_unwritable 仍只门「写入无效」判定。
        pre = _cg_rect(h)
        set_window_rect(h, fx, fy, fw, fh, activate=False)
        got = _settled_readback(h, (fx, fy, fw, fh))
        post = _cg_rect(h) if detect_unwritable else None
        # 要求了一个有意义的变化、CG 几何却一步没变 = 这个窗口动不了：App 根本
        # 不开放自动化（真机微信、CrossPaste），或被系统钉住（真机 Sidecar 上的
        # UU远程）。必须与「被最小尺寸钳制」分开——钳制会变，写不动是一步不动。
        # 判据放在「读回失败」之前：微信那种 AX 读写全失败的窗口也得报出来。
        if (detect_unwritable and pre and post
                and h not in unwritable
                and any(abs(a - b) > 2
                        for a, b in zip(pre, (fx, fy, fw, fh)))
                and all(abs(a - b) <= 2 for a, b in zip(pre, post))):
            unwritable.append(h)
            notes.append("%s 写入后位置尺寸都没变，无法移动"
                         "（不开放自动化或被系统钉住）"
                         % h.split("::")[0])
            logger.warning("四宫格：%s 写入无效：要求 %s，CG 几何仍是 %s",
                           h, (fx, fy, fw, fh), post)
        if not got or got == (0, 0, 0, 0):
            continue
        gx, gy, gw, gh = got
        measured[h] = got
        # 上限钳制：App 拒绝长到目标（真机「系统设置」的最大窗宽 754，
        # AXMinSize/MaxSize 都是 missing value）。这不是间距噪声，必须
        # 交给二趟重排当硬上限用，否则那 450px 偏差会一直留在报告里。
        # 钳制判定看写入方向：只有「要求放大却被读回更小」才是 App 的最大
        # 尺寸上限。缩小请求下读回更小只是宽高比联动（真机 iPhone镜像锁
        # ~0.455，要求 296 读回 242），当上限记会让格子只收紧不放宽。
        _grow_w = pre is not None and pre[2] > 0 and pre[2] < fw - 8
        _grow_h = pre is not None and pre[3] > 0 and pre[3] < fh - 8
        _cap_w = _grow_w and gw < fw - 8
        _cap_h = _grow_h and gh < fh - 8
        if (_cap_w or _cap_h) and h not in unwritable:
            # 一次读回可能是收缩中间帧（真机 iPhone镜像 411/234/242 不定）。
            # 用**只读**方式多确认几次（不重写、不多动窗口）：连续两次读数
            # 一致、或读数逐次更小并开始收敛，就取当前最小值当上限。单次读
            # 会把中间帧当成真实上限，格子来回跳不收敛；真机 AppleScript 每
            # 次写入上百毫秒，这里绝不多写。
            cap = (int(gw), int(gh))
            for _retry in range(2):
                again = _settled_readback(h, (fx, fy, fw, fh))
                if not again or again == (0, 0, 0, 0):
                    break
                aw, ah = int(again[2]), int(again[3])
                settled = abs(aw - cap[0]) <= 2 and abs(ah - cap[1]) <= 2
                cap = (min(cap[0], aw), min(cap[1], ah))
                if settled:
                    break
            # 0 = 该轴没有实测上限（不是放大请求，或读回没更小）。主循环
            # 按轴合并时避 0，_quad_cells 按 >0 消费。
            clamped[h] = (cap[0] if _cap_w else 0,
                          cap[1] if _cap_h else 0)
            if _cap_w:
                gw = cap[0]
            if _cap_h:
                gh = cap[1]
        # 被 App 最小尺寸顶大时不居中：居中坐标常越出工作区（左上格算出的
        # cy 会被 max(wt, ...) 夹回 wt，等价于贴顶；右上格窄窗被推到工作区
        # 左边界外），而且居中写完 gw/gh 没更新，下面的四角锚定仍会把居中
        # 结果整个覆盖掉——两道逻辑互相打架，用户看到的就是「没吸附到边」。
        # 贴角是唯一契约：贴边的一侧永远准确，溢出只发生在远离贴边的那一
        # 侧（BTT windowAnchor 同此语义：锚定角永远贴住，其余边听其自然）。
        # 只有尺寸被 App 明显顶小（>8px）时才重排贴边：读回宽度比目标
        # 少 1~2px 是常态（AX 读回噪声），不该触发重排。
        if abs(gw - fw) <= 8 and abs(gh - fh) <= 8:
            continue
        # 反向钳制不需要补写同一目标：上面的只读轮询已经把 gw/gh 收敛到稳定
        # 值，再写一次目标只会让 App 再拒绝一次、窗口再抖一下（真机每个 AX
        # 写入上百毫秒）。贴边收尾在下面按 gw/gh 写，锚定角永远贴住目标边。
        # 刚判过「写入无效」的窗口不再补刀：它动不了，再写一次只是白等一个
        # AppleScript 往返（真机每个 AX 写入上百毫秒），报告里已经有了
        if h in unwritable:
            continue
        # 窗口被 App 顶大后，按方位把该贴的边贴住：左上格贴左上、右上格贴
        # 右上、左下格贴左下、右下格贴右下。溢出只发生在远离贴边的那一侧。
        # 贴角基准是 finals 的目标矩形而不是 cells：窗口被 App 顶大/顶小
        # 后，锚定的一侧永远贴住目标边，溢出只发生在远离锚定角的那一侧
        # （语义与边界保护见 _quad_anchor）。
        ax, ay = _quad_anchor(corner, fx, fy, fw, fh, gw, gh,
                              wl, wt, ww, wh)
        if (ax, ay) != (gx, gy):
            set_window_rect(h, ax, ay, gw, gh, activate=False)
    return measured, clamped


def _fresh_win(entry, pre_probe):
    """把探测前读回的当前几何并进窗口 dict，返回 (hwnd, win_dict)。

    list_target_windows 的 x/y 是枚举时刻的值，用户刚拖过窗口就过期了。
    proximity（Moom 4 快照匹配）拿旧坐标算，会把「刚才就在左上」的窗口判成
    「离左上最远」——那就是用户说的「乱跑」「不准」。探测前的读回
    （pre_probe）是当下最新的几何。
    """
    h, w = entry
    cur = pre_probe.get(h)
    if cur and cur != (0, 0, 0, 0):
        w = dict(w, x=cur[0], y=cur[1], w=cur[2], h=cur[3])
    return h, w


def _quad_pick_windows(wants, use_pinned=True):
    """按四角选出要排的窗口：显式 hwnd 优先，空格自动补足（前几个未占用的）。

    snap_four_quad 与 quad_predict（排列预演）共用这一套：预演算的必须和
    正式排布动的是同一批窗口，两边选得不一样，预告本身就是错的——那正是
    「预告说排四个、实际动了三个」的不准来源。

    wants: (左上, 右上, 左下, 右下) 的 hwnd，None = 该格「自动」。
    use_pinned: 空格是否优先用界面固定的目标窗口（与 snap_four_quad 同义）。
    返回 (picked, picked_corners, missing)：
      picked         [(hwnd, win_dict)]，按角序排好的待排窗口
      picked_corners 每个窗口对应的角序号 0..3
      missing        显式选中但已不可用（关掉/改名）的 hwnd。如实报出来，
                     不能静默用别的窗口顶上——真机实测选 CodeBuddy 的格子
                     被换成 ChatGPT，用户看到的就是「不准」
    """
    cands = [w for w in list_target_windows()
             if w["pid"] not in _OWN_PIDS and w["owner"]]
    # 自动补足时跳过 AX 够不着的 owner（如 wine）：它们会被选进四宫格，
    # 然后每一次读写几何都失败，整轮排列卡在重试里。
    cands = [w for w in cands if w["owner"] not in _QUAD_SKIP_OWNERS]
    cands_by_hwnd = {w["hwnd"]: w for w in cands}

    # 选几个就排几个：在网格里点掉不想要的格（或下拉里选「自动」）即可只排
    # 两三个。这块屏放不下四个是常态（真机 Built-in 1512x896，四个窗口的
    # App 最小尺寸总和动辄 1800x1300），能让用户主动选 2 个 / 3 个来排，比
    # 每次放不下再解释一遍有用。显式选满 2 个及以上时不再自动补足；只选 1 个
    # 或一个都没选时按老行为补足四个（用户还在选，或想用固定的目标窗口）。
    keep_only_selected = len([x for x in wants if x]) >= 2
    picked, picked_corners, seen, missing = [], [], set(), []
    for corner, want in enumerate(wants):
        w = None
        if want:
            w = cands_by_hwnd.get(want) or _find_window(want)
            if w is None and want not in seen:
                logger.warning("四宫格：指定的窗口 %s 已不可用", want)
                missing.append(want)
        elif (not keep_only_selected and use_pinned and _pinned_target
              and _pinned_target not in seen):
            w = _find_window(_pinned_target)
        if w is None:
            if keep_only_selected:
                continue          # 用户点掉了这个角：留空，不补
            for c in cands:
                h = c.get("hwnd") or make_hwnd(c["owner"], c["name"] or "")
                if h not in seen:
                    w = c
                    break
        if w is None:
            # 桌面上一共没那么多窗口：排已有的，少于两个才失败
            continue
        h = w.get("hwnd") or make_hwnd(w["owner"], w["name"] or "")
        # 四宫格候选必须一对一：同一个 hwnd 占两个角时 AX 只会写/读同一扇窗，
        # 两格互相串读。这里给第二份同键候选补上真实序号。
        if h in seen:
            same = _same_name_windows(w["owner"], w["name"] or "")
            seq = None
            for si, s in enumerate(same, 1):
                if (s.get("wid") is not None and s.get("wid") == w.get("wid")):
                    seq = si
                    break
            h = make_hwnd(w["owner"], w["name"] or "", seq or 1)
            _last_rect[h] = (w["x"], w["y"], w["w"], w["h"])
            logger.info("四宫格：同名候选 %s 补序号为 %s", h, h)
        # 登记 CG 侧几何：set_window_rect 靠它在 AX 窗口列表里认出这个窗口
        # （CG 枚举序与 AX 序不一致时，纯按序号会选到别的窗口）
        _last_rect[h] = (w["x"], w["y"], w["w"], w["h"])
        picked.append((h, w))
        picked_corners.append(corner)
        seen.add(h)
    return picked, picked_corners, missing


def snap_four_quad(tl_hwnd=None, tr_hwnd=None, bl_hwnd=None, br_hwnd=None,
                   use_pinned=True, monitor=None, split_x=0.5, split_y=0.5,
                   smart=False):
    """把四个窗口排到同一屏的 2×2 四宫格（左上/右上/左下/右下）。

    与单窗的四等分吸附（snap 的 quad-*）配套：那四个按钮一次只动一个窗口，
    本函数一次摆四个窗口。

    split_x / split_y 给出分割比例（0~1，0.5 = 四等分）。等分并不总是最合适：
    左栏放终端想窄一点、右栏放浏览器想宽一点是常见需求，所以界面上做了可
    拖拽的分割线，把这两个比例一起传进来。

    smart=True 时改用智能分配：传入哪些窗口都行，谁去哪个角、用什么比例
    都由 quad_plan.plan_quad_smart 按各窗口的宽高比算出来。此时 tl/tr/bl/br
    只是「参与排列的四个窗口」（顺序不限），split_x/split_y 作为兜底值。

    四个 hwnd 均可显式指定；未指定时自动取最前面的四个窗口（左上优先用界面
    固定的目标窗口）。显式选中 2 个及以上时不再自动补足——界面上点掉不想要的
    格就只排选中的两三个，放不下四个时从「意外」变成「选择」。monitor 可指定
    目标显示器（int 索引或 device_name 字符串）；为 None 时以左上窗口当前所在
    屏幕为准，该屏放不下时自动换一块放得下的横屏。

    排布前先实测每个窗口的最小尺寸：App 的最小尺寸（真机 ChatGPT 高 600、
    Qoder CN 540）会把窗口撑出我们给的格子，撑过屏幕底边后 macOS 会把整个
    窗口判给别的屏——这就是「四个窗口排完不在一个屏幕上」的根因，不是坐标算错。
    四格按「比例切分值」与「窗口最小尺寸」取大者，并钉在工作区内：上排贴顶、
    下排贴底、左列贴左、右列贴右，最小尺寸把哪一格撑大就往外撑，绝不越界。
    工作区实在装不下四个最小尺寸时，才允许上下 / 左右重叠，并在
    _last_quad_notes 里如实说明（界面据此提示用户）。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return False

    # 上一轮的说明先清空：界面读 _last_quad_notes 做提示，留着旧值会把上次的
    # 「装不下 / 被搬屏」报在这一次成功的排列上
    global _last_quad_notes
    _last_quad_notes.clear()
    # 用户在下拉里选过但已不可用（关掉/改名）的窗口：如实报出来，不能静默
    # 用别的窗口顶上——那正是「智能/非智能都不准」的观感来源之一
    picked, picked_corners, _missing_windows = _quad_pick_windows(
        (tl_hwnd, tr_hwnd, bl_hwnd, br_hwnd), use_pinned=use_pinned)
    if len(picked) < 2:
        logger.warning("可排列的窗口不足两个（选中 %d 个）", len(picked))
        return False

    tl_hwnd, tl_w = picked[0]
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, tl_w["x"], tl_w["y"],
                                     tl_w["w"], tl_w["h"])
    if idx is None or not (0 <= idx < len(ms)):
        idx = 0
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect

    # 1) 实测四个窗口的最小尺寸
    mins = {}
    pre_probe = {}
    pinned_windows = []
    for h, _w in picked:
        # 探测前先记下当前几何：探测会把窗口缩得很小，排布前要放回原状，
        # 但只在几何真的被探测改动过才放（无谓的复位会白占一步撤销栈）
        try:
            pre_probe[h] = _readback_rect(h, (_w["x"], _w["y"], _w["w"], _w["h"]))
        except Exception:  # noqa: BLE001
            pre_probe[h] = None
        mins[h] = _probe_min_size(h, wl + 1, wt + 1)
        # 探测前后几何完全没变 = 窗口被系统钉死（真机实测 Sidecar 上的
        # UU远程 窗口，任何 AX 写入都被忽略）。这类窗口不参与比例计算，
        # 否则智能分配会把活窗口的空间分给一个动不了窗口。
        try:
            post = _readback_rect(h, pre_probe.get(h))
            was = pre_probe.get(h)
            if was and post == was and post != (0, 0, 0, 0):
                cx = post[0] + post[2] / 2.0
                if not (mon.left <= cx < mon.left + mon.width):
                    pinned_windows.append(h)
        except Exception:  # noqa: BLE001
            pass
    # 探测最小尺寸会把窗口缩到很小，这会让 App 的最小尺寸把窗口撑出格子、
    # 甚至触发 macOS 搬屏。探测完把每个窗口放回它**原来的位置**：后面正式
    # 排布按当前几何重新定位，不会带着探测后的残局排（真机实测 UU远程 探测后
    # 停在工作区外，直接排就把它判给了 Sidecar）。
    for h, w in picked:
        try:
            was = pre_probe.get(h)
            cur = _readback_rect(h, was or (w["x"], w["y"], w["w"], w["h"]))
            if (cur and cur != (0, 0, 0, 0)
                    and was and was != (0, 0, 0, 0)
                    and cur != was):
                set_window_rect(h, w["x"], w["y"], w["w"], w["h"],
                                activate=False)
                # 放回后把探测前的几何重新登记：正式排布的 AX 几何匹配靠它
                _last_rect[h] = was
        except Exception:  # noqa: BLE001
            pass
    if pinned_windows:
        # 钉死的窗口（系统拒绝任何 AX 写入）不参与比例计算：它的最小
        # 尺寸就是当前尺寸，参与计算只会把活窗口的空间挤掉。
        for h in pinned_windows:
            cur = _readback_rect(h, pre_probe.get(h)) or (0, 0, 0, 0)
            mins[h] = (cur[2], cur[3])
            logger.warning("四宫格：%s 被系统钉死，按当前尺寸 %dx%d 参与",
                        h, cur[2], cur[3])
    # 每个窗口的原始几何（探测前读回的那一份）：装载复检要把装不下的窗口放回
    # 原状，而探测会把窗口缩得很小，原状只能是探测前这一份
    orig_rect = {h: (pre_probe.get(h) or (w["x"], w["y"], w["w"], w["h"]))
                 for h, w in picked}
    # 四格需求按「用户选中的角」算：点掉了几格，空格子按 0 计入。不能把选中
    # 的窗口挤到别的角上去算——quad_requirements 假定顺序是 左上/右上/左下/右下
    corners = [(0, 0)] * 4
    for (h, _w), c in zip(picked, picked_corners):
        corners[c] = (mins[h][0], mins[h][1])
    need_w, need_h = quad_plan.quad_requirements(corners)
    logger.info("四格最小尺寸需求 %dx%d（屏幕 %s %dx%d）：%s",
                need_w, need_h, mon.device_name, ww, wh,
                "、".join("%s=%dx%d" % (h.split("::")[0], mins[h][0], mins[h][1])
                          for h, _w in picked))

    # 2) 选屏：用户点名了屏就尊重它（放不下也尽力排在上面并如实说明）；
    #    「自动」时该屏放不下就换一块放得下的横屏，竖屏不主动送。
    # 不仅「装不下」要换屏：当前屏是竖屏时也换——用户明确说过不适合竖屏的
    # 窗口不要发到竖屏屏幕，四宫格在竖屏上必挤（真机 DELL U2414H 上实测）。
    if monitor is None and (need_w > ww or need_h > wh or wh > ww):
        better = _pick_quad_screen(ms, idx, corners)
        if better != idx:
            logger.info("屏幕 %s（%dx%d）不适合四宫格（四格最小尺寸 %dx%d），"
                        "改排到 %s",
                        mon.device_name, ww, wh, need_w, need_h,
                        ms[better].device_name)
            idx = better
            mon = ms[idx]
            wl, wt, ww, wh = mon.work_rect

    # 3) 比例与四个角的归属
    sx, sy = float(split_x), float(split_y)
    if smart:
        # 智能分配：按各窗口的宽高比重排四个角的归属，并重算分割比例。
        # 直接用 picked 里的 w（带 x/y/w/h）做判据，不再重新枚举——重新拉列表
        # 会拿到不同的顺序与几何，重排时按 (owner, name) 匹配就会错位
        # （真机实测 plan 说豆包去 TL，实际 ChatGPT 去了 TR）。
        # 选几个就排几个：2~3 个窗口也走智能分配。旧实现严格要求四个窗口，
        # n<4 直接抛 ValueError、退化成手动顺序——「智能模式不准」的直接来源。
        try:
            # 钉死的窗口仍算一个角（保持 4 个），但把它的最小尺寸记为 0：
            # 智能分配按活窗口的宽高比优化，钉死的窗口排最后，写不动就在原地
            # 并如实说明。
            plan_mins = dict(mins)
            for h in pinned_windows:
                plan_mins[h] = (0, 0)
            order_idx = sorted(range(len(picked)),
                               key=lambda i: (picked[i][0] in pinned_windows, i))
            plan_wins = []
            for i in order_idx:
                h_i, w_i = _fresh_win(picked[i], pre_probe)
                w_i.update(hwnd=h_i,
                           min_w=plan_mins[h_i][0], min_h=plan_mins[h_i][1])
                plan_wins.append(w_i)
            # 用户在界面上给每个窗口点选的格：传给 plan_quad_layout，它就只在
            # 这些角里分配，不会把「左上」的窗口排到右上。
            plan_corners = sorted(set(picked_corners[i] for i in order_idx))
            planned, sx2, sy2 = quad_plan.plan_quad_layout(
                plan_wins, ww, wh, corners=plan_corners, origin=(wl, wt))
        except ValueError as e:  # noqa: BLE001
            logger.warning("智能分配失败，退回手动比例: %s", e)
        else:
            # planned 里的元素就是传入的 dict（已带 hwnd 键），直接取
            reordered = [(w.get("hwnd"), w) for _c, w in planned]
            # 换序必须连角标一起换：picked_corners 不跟着换，后面按角算格子需求
            # 时窗口与角就错位了（真机实测左上格的窗口被排到右上）。
            # 旧代码在 try 里就地重排 picked，异常时 picked 也已是重排态——
            # 那正是「智能分配失败却按错的顺序排」的来源。
            if (len(reordered) == len(picked)
                    and all(h for h, _w in reordered)):
                picked = reordered
                picked_corners = [c for c, _w in planned]
                sx, sy = sx2, sy2
                logger.info("智能分配结果: %s（横向 %.0f%% / 竖向 %.0f%%）",
                            " ".join("%s=%s" % (_QUAD_NAMES[c],
                                                h.split("::")[0])
                                     for c, (h, _w) in zip(picked_corners,
                                                           picked)),
                            sx * 100, sy * 100)
            else:
                logger.warning("智能分配结果异常，保留手动顺序")
    # 比例钳在安全区间：贴边会让某一格只剩几个像素，等于把窗口藏起来
    sx = min(max(sx, 0.15), 0.85)
    sy = min(max(sy, 0.15), 0.85)

    # 4) 子集选择：放不下的窗口不硬排。真机实测这四个窗口的 App 最小尺寸
    #    总和 1824x1318 超过 Built-in 的 1512x896，照排四个的结局是上下
    #    重叠 503px、左右重叠 757px，还有一个窗被 macOS 判到别的屏——用户
    #    看到的就是「不准」。改成只排放得下的：格子几何不变，放不下的窗口
    #    原封不动，并在说明里逐个列出（用户据此知道该关谁、换谁）。
    gap, edge_gap = _gap_settings()
    # 被系统钉死的窗口（任何 AX 写入都被忽略）不参与占角：它动不了，占一个
    # 角只会把活窗口的空间挤掉
    pool_idx = [i for i, (h, _w) in enumerate(picked)
                if h not in pinned_windows]
    # proximity 要拿「窗口现在在哪」算：探测前读回的几何盖掉枚举时刻的值
    pool = [_fresh_win(picked[i], pre_probe) for i in pool_idx]
    # 每个窗口原本认哪个角：用户点掉几格后 picked 不满四个，角必须对得上，
    # 否则「左上」的窗口会被排到右上
    pool_corners = [picked_corners[i] for i in pool_idx]
    # 没点名屏幕时，换一块能多排几个的横屏：放不下四个是常态，能多排一个是一
    # 个。竖屏不主动送——用户明确说过不适合竖屏的窗口不要发到竖屏屏幕
    idx, placed_slots, unplaced = _quad_place(
        pool, mins, pool_corners, ms, idx, gap, edge_gap,
        allow_switch=monitor is None)
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect
    logger.info("四宫格：%d/%d 个窗口放得下 %s（工作区 %dx%d）",
                len(placed_slots), len(picked), mon.device_name, ww, wh)
    # 智能比例按**实际排放**的子集重算：放不上的那几个窗口在第一轮的 sx/sy 里
    # 也占了比例，拿它切两个窗口的屏必然歪（用户报的「2×2 不准」多半源于此）
    if smart:
        sx, sy = _quad_smart_ratio(placed_slots, mins, ww, wh, sx, sy,
                                   origin=(wl, wt))
    notes = []
    # 写入无效的窗口（App 不开放自动化 / 被系统钉住）：写入前后几何一步没变
    unwritable = []

    maxes = {}
    cells, finals, splits = _quad_cells(
        placed_slots, mins, wl, wt, ww, wh, gap, edge_gap, sx, sy,
        maxes=maxes, manual_split=(split_x != 0.5 or split_y != 0.5))
    # 二趟重排：第一遍按探测到的最小尺寸切格子，写入后读回的是窗口**实际**占的
    # 尺寸。探测偏小（取到收缩中间帧）时窗口被 App 撑得比格子大，上下两排必然
    # 重叠——把实测尺寸并进最小尺寸（只升不降）再切一遍格子，单调收敛，最多三遍。
    for attempt in range(_QUAD_MAX_PASSES):
        measured, clamped = _quad_write(finals, cells, wl, wt, ww, wh,
                                        smart, unwritable, notes,
                                        detect_unwritable=attempt == 0)
        grew = False
        for corner, h, _fx, _fy, _fw, _fh in finals:
            got = measured.get(h)
            if not got:
                continue
            cw, ch = cells[corner][2], cells[corner][3]
            mw, mh = mins.get(h, (0, 0))
            # 与**格子**比，不与目标比：目标已让出窗口间距，间距噪声会触发无谓
            # 的重排；比格子还大 8px 以上，说明这一格真的装不下这个窗口。
            nw = max(mw, got[2]) if got[2] > cw + 8 else mw
            nh = max(mh, got[3]) if got[3] > ch + 8 else mh
            # 上限钳制：旧代码只在「实测比格子大」时把下限抬上去。真机
            # 「系统设置」相反——写 1204 宽被 App 卡回 754，实测比目标小。
            # 不把它记为有效上限，二趟会继续切出装不下的宽格，450px 偏差
            # 一直留在报告里。钳制值 >8px 才记（1~2px 是 AX 读回噪声）。
            if h in clamped:
                old_cap = maxes.get(h)
                # 每轮只收紧不放宽：读回可能取到收缩中间帧（真机 iPhone镜像
                # 第一轮 411、第二轮才稳定 242），放宽会让格子来回跳。
                new_cap = clamped[h]
                if old_cap:
                    # 按轴合并：0 表示该轴没有实测上限，min 会把另一轴已探到
                    # 的上限清掉（真机第二遍只确认高度上限，宽度上限来自
                    # 第一遍——第一遍宽度不是放大请求，记的就是 0）
                    merged = []
                    for _o, _n in zip(old_cap, new_cap):
                        merged.append(min(_o, _n) if (_o and _n) else (_o or _n))
                    new_cap = tuple(merged)
                if old_cap != new_cap:
                    grew = True
                maxes[h] = new_cap
            if (nw, nh) != (mw, mh):
                mins[h] = (nw, nh)
                grew = True
        # 装载复检：探测值偏小时预检说「这块屏装得下」，写入后窗口被 App 撑大，
        # 实测需求跟着超过工作区。这时候照原样切格子只能按需求占比硬分——上下
        # 两排互相压住、宽窗漂到别处，用户看到的就是「不准」。装不下就减载：
        # 放不下的窗口退出排列并放回原状，只排装得下的。与 choose_quad_slots
        # 是同一套语义，判据从「写入前探测值」换成「写入后实测值」。
        reduced = False
        if _quad_overload(placed_slots, mins, ww, wh, gap, edge_gap):
            idx2, slots2, unplaced2 = _quad_place(
                pool, mins, pool_corners, ms, idx, gap, edge_gap,
                allow_switch=monitor is None, beat=len(placed_slots) - 1)
            if len(slots2) < len(placed_slots) or idx2 != idx:
                if idx2 != idx:
                    # 另一块屏能排同样多：换屏而不是丢窗口
                    idx = idx2
                    mon = ms[idx]
                    wl, wt, ww, wh = mon.work_rect
                keep = {h for _c, h, _w in slots2}
                dropped = [(h, w) for _c, h, w in placed_slots
                           if h not in keep]
                for h, _w in dropped:
                    orig = orig_rect.get(h)
                    if not orig or orig == (0, 0, 0, 0):
                        continue
                    # 这一轮已经写进去过，不撤回来用户看到的就是「排好了又
                    # 停在半路上」的窗口
                    set_window_rect(h, orig[0], orig[1], orig[2], orig[3],
                                    activate=False)
                    _last_rect[h] = orig
                placed_slots, unplaced = slots2, unplaced2
                reduced = True
                logger.warning(
                    "四宫格：写入后实测装不下 %s（工作区 %dx%d），%s 放回原位，"
                    "只排 %d 个", mon.device_name, ww, wh,
                    "、".join(h.split("::")[0] for h, _w in dropped),
                    len(placed_slots))
        if not grew and not reduced:
            break
        if reduced and smart:
            sx, sy = _quad_smart_ratio(placed_slots, mins, ww, wh, sx, sy,
                                       origin=(wl, wt))
        cells2, finals2, splits2 = _quad_cells(
            placed_slots, mins, wl, wt, ww, wh, gap, edge_gap, sx, sy,
            maxes=maxes, manual_split=(split_x != 0.5 or split_y != 0.5))
        if cells2 == cells and finals2 == finals and not reduced:
            break
        cells, finals, splits = cells2, finals2, splits2
        logger.info("四宫格：第 %d 遍按实测尺寸重排（最小尺寸已更新）",
                    attempt + 1)
    global _last_quad_splits
    # splits 已由 _quad_cells 按**占用格子的真实几何**算好并钳到 0.05~1.0。
    # 拿 cells[0] 单格反算会把 T 形下拉通整宽的列切分整个丢掉，界面上的分割
    # 线与实际排出来的位置对不上——这正是用户看到的「不准」
    _last_quad_splits = splits
    logger.info("四宫格排列完成: %s（屏幕 %s，横向 %.0f%% / 竖向 %.0f%%）",
                "、".join("%s=%s" % (_QUAD_NAMES[corner], h.split("::")[0])
                         for corner, h, _x, _y, _fw, _fh in finals)
                or "一个都没排",
                mon.device_name, _last_quad_splits[0] * 100,
                _last_quad_splits[1] * 100)

    report = []
    global _last_quad_boxes
    _last_quad_boxes = cells
    # 目标用「让出窗口间距后的格子」——那才是真正写进去的目标值。早先拿原始
    # 格子比，间距（默认 12px，每窗让一半）会被记成「被最小尺寸钳制」，间距
    # 开到 20px 以上时状态栏就开始乱报。
    # 只报实际排放的窗口：放不下的窗口根本没动，报出来只会被当成「偏差」
    targets = {h: (fx, fy, fw, fh) for _idx, h, fx, fy, fw, fh in finals}
    for idx, h, _w in placed_slots:
        want = targets[h]
        got = _readback_rect(h, want)
        if not got or got == (0, 0, 0, 0):
            report.append((h, want, None))
            # 已经报过「写入无效」就不再报「读不到几何」：同一个窗口占两条说明，
            # 会把更有用的那条挤出状态栏（状态栏只显示前两条）
            if h not in unwritable:
                notes.append("%s 读不到几何" % h.split("::")[0])
            continue
        # 写入后几何完全没变、且窗口中心不在目标屏：这个窗口被系统钉住了
        # （真机实测 Sidecar 上的 UU远程 窗口，任何 AX 写入都被忽略）。
        # 必须与「被最小尺寸钳制」区分开——钳制会变，钉死不变。
        if got == want:
            pass
        elif (abs(got[0] - want[0]) <= 2 and abs(got[1] - want[1]) <= 2
                and abs(got[2] - want[2]) <= 2 and abs(got[3] - want[3]) <= 2):
            pass
        dev = max(abs(g - t) for g, t in zip(got, want))
        report.append((h, want, got))
        # 窗口被系统判给了别的屏：中心点不在目标屏范围内。这是「四窗不在
        # 一个屏幕上」的直接证据，必须报出来，不能记成「被最小尺寸钳制」。
        cx, cy = got[0] + got[2] / 2.0, got[1] + got[3] / 2.0
        if (h not in unwritable
                and not (mon.left <= cx < mon.left + mon.width
                         and mon.top <= cy < mon.top + mon.height)):
            notes.append("%s 被系统移到了别的屏（实测 %s）"
                         % (h.split("::")[0], got))
            logger.warning("四宫格：%s 被移到别的屏 -> %s", h, got)
        elif dev > 8:
            # 偏差方向说明限制来自哪一侧：比目标小是 App 的最大尺寸或
            # 宽高比锁死（真机 iPhone镜像宽高比 ~0.455，写宽必缩高），
            # 比目标大是最小窗口尺寸顶大。说反了用户按错误的方向去调。
            _why = []
            if any(g < t - 8 for g, t in zip(got[2:], want[2:])):
                _why.append("被应用的最大尺寸或宽高比限制")
            if any(g > t + 8 for g, t in zip(got[2:], want[2:])):
                _why.append("被应用的最小窗口尺寸顶大")
            _why_s = "、".join(_why) or "被应用钳制"
            logger.info("四宫格偏差 %s：目标=%s 实测=%s（最大偏差 %d px，"
                        "%s）", h, want, got, dev, _why_s)
            # 物理限制要摆到用户眼前：只进日志的话，状态栏显示「排列
            # 完成」，旁边却留着几百 px 空白，用户不知道是 App 锁的
            notes.append("%s 实际 %dx%d（%s）"
                         % (h.split("::")[0], got[2], got[3], _why_s))
    global _last_quad_report
    _last_quad_report = report
    global _last_quad_unwritable
    _last_quad_unwritable[:] = unwritable
    global _last_quad_order
    _last_quad_order = [h for _idx, h, _w in placed_slots]
    # 角 -> hwnd 的实际分配：界面据此把四个下拉回填成「谁真的在这个角」。
    # 智能模式重排过、手动模式点掉过几格，两边都与用户输入的顺序不同，
    # 不回填就会出现「下拉显示排好了、实际摆的是另一个」的观感。
    _last_quad_slots[:] = [(corner, h) for corner, h, _w in placed_slots]
    if _missing_windows:
        # 说明用了别的窗口顶上：界面据此提示用户重新选择
        names = "、".join(x.split("::")[0] for x in _missing_windows[:3])
        notes.insert(0, "指定的窗口 %s 已关闭/改名，用了其它窗口顶上" % names)
    # 重叠量用实测值算：前面 ov_x / ov_y 是理想几何的估计，而 App 的最小尺寸、
    # 标题栏占位都会让窗口实际比格子大，只有实测才反映用户看到的重叠。
    # 上排 = 顶边较小的两格、下排 = 其余两格（摆放时就是按这个顺序写的）。
    live = [r for _h, _w, r in report if r]
    if len(live) == 4:
        rows = sorted(live, key=lambda r: r[1])
        cols = sorted(live, key=lambda r: r[0])
        real_ov_y = max(0, max(r[1] + r[3] for r in rows[:2])
                        - min(r[1] for r in rows[2:]))
        real_ov_x = max(0, max(r[0] + r[2] for r in cols[:2])
                        - min(r[0] for r in cols[2:]))
        if real_ov_x or real_ov_y:
            notes.append("四格没完全排开：实测上下重叠 %dpx、左右重叠 %dpx"
                         % (real_ov_y, real_ov_x))
            logger.warning("四宫格：%s", notes[-1])
    # 放不下的窗口逐个说明：排了多少、谁没排、为什么。这块屏的四宫格容量是
    # 硬上限（App 的最小尺寸改不了），说清楚用户才知道该关谁、换谁。
    pos = 1 if _missing_windows else 0
    if unplaced:
        detail = "、".join(
            "%s(最小%dx%d)" % (h.split("::")[0], mins[h][0], mins[h][1])
            for h, _w in unplaced)
        notes.insert(pos, "只排出 %d/%d 个：%s 放不下这块屏的四宫格"
                    "（工作区 %dx%d），已保持原位"
                    % (len(placed_slots), len(picked), detail, ww, wh))
        logger.warning("四宫格：%d/%d 个放得下，未排：%s",
                       len(placed_slots), len(picked), detail)
        pos += 1
    if pinned_windows:
        notes.insert(pos, "%s 被系统钉死无法移动，未参与排列"
                    % "、".join(x.split("::")[0] for x in pinned_windows[:3]))
    # 就地清空再填充（而不是重新绑定）：本函数顶部已用 global 声明过它，
    # 这里再声明一次会报「name used prior to global declaration」。就地改内容
    # 让界面持有的引用（若有）也同步更新。
    # 最小尺寸缓存给容量预检（quad_capacity）用：下次改选择 / 换屏时立刻能
    # 说出「这块屏最多排几个」，不必等真的排一遍。只缓存探测到的正值——
    # (0,0) 是探测失败，按 0 计会把容量说得过于乐观。
    for _h, _m in mins.items():
        if _m and _m[0] > 0 and _m[1] > 0:
            _QUAD_MIN_CACHE[_h] = (int(_m[0]), int(_m[1]))
    # 尺寸上限缓存给预演（quad_predict）用：点排列前就能说出「这一格会留
    # 多少空白」。0 = 该轴没有实测上限，与最小尺寸一样只存至少一轴有值的
    # 条目——全 0 记上去会让 _quad_cells 把格子按 0 切。
    for _h, _c in maxes.items():
        if _c and (_c[0] > 0 or _c[1] > 0):
            _QUAD_MAX_CACHE[_h] = (int(_c[0]), int(_c[1]))
    _quad_cache_save()
    _last_quad_notes[:] = notes
    return True


def quad_capacity(hwnds, monitor=None):
    """预检：当前选择在一块屏上最多排得下几个窗口（不动任何窗口）。

    为什么需要它：四宫格的容量是硬上限——App 的最小窗口尺寸改不了，放不下
    四个时「照排四个」的结局只能是重叠或越界（真机 RV200 Pro 上四个窗口最小
    尺寸合计需 1539x1266，工作区只有 1600x870，硬排必有 396px 上下重叠，
    用户看到的就是「不准」）。把容量在点「2×2 排列」之前摆出来，用户才知道
    该关谁、换谁，而不是排完看到重叠以为程序算错。

    最小尺寸消费上次排布的缓存（_QUAD_MIN_CACHE），原因见该缓存处的注释。
    没探过的窗口按 0 计——0 是乐观值，fit 因此是上限；返回值的 uncached 列
    出没探过的窗口名，界面据此说明「容量偏乐观」。

    hwnds: 按 左上/右上/左下/右下 顺序的 hwnd 列表（可少于四个，顺序即角序）。
    monitor: int 索引 / device_name 字符串 / None（自动）。None 时在横屏里挑
        排得最多的（竖屏不算——用户明确说过不适合竖屏的窗口不要发到竖屏），
        与正式排布的换屏策略一致；并列时取第一个窗口所在屏。

    返回 None 表示一个窗口的最小尺寸都没缓存（从没排过），界面据此不显示；
    否则返回 dict：screen / work (w, h) / fit（最多排几个）/ total（选了几个）
    / need（全排所需最小工作区，有未缓存窗口时为 None）/ uncached（窗口名）。
    """
    hwnds = [h for h in (hwnds or []) if h]
    if not hwnds:
        return None
    cached = {h: _QUAD_MIN_CACHE[h] for h in hwnds if h in _QUAD_MIN_CACHE}
    if not cached:
        return None
    gap, edge_gap = _gap_settings()
    ms = monitors.enum_monitors()
    if not ms:
        return None
    wins = [(h, None) for h in hwnds]
    mins_all = {h: _QUAD_MIN_CACHE.get(h, (0, 0)) for h in hwnds}

    def _fit(m):
        _wl, _wt, mw, mh = m.work_rect
        slots, _unplaced = quad_plan.choose_quad_slots(
            wins, mins_all, mw, mh, gap, edge_gap, origin=(_wl, _wt))
        return len(slots)

    # 点名屏就尊重它（放不下也照样报这块屏的容量）；否则横屏里挑最优
    idx = _resolve_monitor_index(ms, monitor) if monitor is not None else None
    if idx is not None and 0 <= idx < len(ms):
        best = ms[idx]
    else:
        # 第一个窗口所在屏：并列时的优先项（与「自动 = 左上窗口所在屏」一致）
        first_idx = None
        try:
            r = _cg_rect(hwnds[0]) or _last_rect.get(hwnds[0])
            if r:
                first_idx = _monitor_index_by_rect(ms, r[0], r[1], r[2], r[3])
        except Exception:  # noqa: BLE001  预检只是建议，读不到几何就按序取
            first_idx = None
        best, best_key = None, None
        for i, m in enumerate(ms):
            if m.height > m.width:
                continue          # 竖屏不送
            key = (_fit(m), 1 if i == first_idx else 0)
            if best is None or key > best_key:
                best, best_key = m, key
        if best is None:          # 一台横屏都没有：退回第一块，容量照算
            best = ms[0]
    # 全排需求按用户角标算（与 snap_four_quad 里记日志的口径一致）：点掉几格
    # 的空格按 0 计入。有窗口没探过最小尺寸时需求会偏小，宁可不报。
    need = None
    if len(cached) == len(hwnds):
        corners = [(0, 0)] * 4
        for corner, h in enumerate(hwnds[:4]):
            corners[corner] = mins_all[h]
        need = quad_plan.quad_requirements(corners)
    return {"screen": best.device_name,
            "work": (best.work_rect[2], best.work_rect[3]),
            "fit": _fit(best), "total": len(hwnds),
            "need": need,
            "uncached": [h.split("::")[0] for h in hwnds if h not in cached]}


def quad_predict(tl_hwnd=None, tr_hwnd=None, bl_hwnd=None, br_hwnd=None,
                 use_pinned=True, monitor=None, smart=False, split_x=0.5,
                 split_y=0.5, blank_tol=100):
    """预演：用上次实测缓存的尺寸推算一次四宫格，**不动任何窗口**。

    为什么需要它：有两类「不准」在点下按钮之前就能知道。一是 App 把窗口
    钳在更小处（真机「系统设置」最大窗宽 754、iPhone镜像 宽高比锁定），
    格子却按满屏切，排完留几百 px 空白；二是这块屏按上次实测的最小尺寸
    其实装不下这么多窗口，硬排只能重叠、或把窗口判给别的屏。预演把这两类
    结果提前摆出来，用户点头再动窗口。

    与 snap_four_quad 的关系：**同一套数学**（_quad_place 选子集 + 智能
    分配 + _quad_cells 切格子），只把「现场实测」换成「上次排布的缓存」
    （_QUAD_MIN_CACHE / _QUAD_MAX_CACHE，排完写盘 quad_cache.json，App
    启动时读入），一个 set_window_rect 都不调。

    任一窗口没有最小尺寸缓存（从没排过）时返回空警告——预演只能闭嘴，正式
    排布会现场实测。这是已知限制：首次排列没有预告，行为与以前一致。

    参数与 snap_four_quad 一一对应（角序、目标屏、比例、智能模式）。
    返回 (warnings, detail)：warnings 是给用户看的一句话列表（空 = 没什么
    要预告的）；detail 是 dict（screen / work / fit / total / splits /
    blank / uncached），供界面与日志使用。
    """
    # 选窗与 snap_four_quad 同一套（「自动」补足 / 钉住窗口 / 同名补序号）：
    # 预演必须算正式排布会动的那批窗口，选得不一样，预告本身就是错的
    picked, picked_corners, missing = _quad_pick_windows(
        (tl_hwnd, tr_hwnd, bl_hwnd, br_hwnd), use_pinned=use_pinned)
    detail = {"screen": "", "work": (0, 0), "fit": 0, "total": len(picked),
              "splits": None, "blank": [], "uncached": []}
    warnings = []
    if missing:
        # 下拉里选中的窗口已关掉/改名：正式排列会跳过它，预演照说
        names = "、".join(x.split("::")[0] for x in missing[:3])
        warnings.append("「%s」已不可用，将只排列剩下的窗口" % names)
    if len(picked) < 2:
        return warnings, detail
    # 最小尺寸必须全部有缓存：没探过的窗口只能瞎猜，而猜错比不猜更糟——
    # 把「装得下」说成「装不下」，用户白关一个窗口
    mins = {}
    for h, _w in picked:
        m = _QUAD_MIN_CACHE.get(h)
        if not m or m[0] <= 0 or m[1] <= 0:
            # 没探过最小尺寸的窗口全都记下来再闭嘴：detail 是给日志/排障用的，
            # 只记第一个会让人以为「只有一个窗口没探过」
            detail["uncached"].append(h)
            continue
        mins[h] = m
    if detail["uncached"]:
        return warnings, detail
    try:
        ms = monitors.enum_monitors()
    except Exception:  # noqa: BLE001  预演只是预告，读不到屏就闭嘴
        ms = []
    if not ms:
        return [], detail
    _h0, w0 = picked[0]
    idx = _resolve_monitor_index(ms, monitor)
    if idx is None:
        idx = _monitor_index_by_rect(ms, w0["x"], w0["y"], w0["w"], w0["h"])
    if idx is None or not (0 <= idx < len(ms)):
        idx = 0
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect
    sx, sy = float(split_x), float(split_y)
    if smart:
        # 与 snap_four_quad 的智能分配同一条路径：按宽高比重排角的归属。
        # 预演不探测（探测是写操作），所以没有「钉死的窗口」那一档。
        try:
            plan_wins = []
            for h_i, w_i in picked:
                d = dict(w_i)
                d.update(hwnd=h_i, min_w=mins[h_i][0], min_h=mins[h_i][1])
                plan_wins.append(d)
            planned, sx2, sy2 = quad_plan.plan_quad_layout(
                plan_wins, ww, wh, corners=sorted(set(picked_corners)),
                origin=(wl, wt))
        except ValueError as e:  # noqa: BLE001  算不出就退回手动比例
            logger.warning("预演：智能分配失败，按手动比例推算: %s", e)
        else:
            reordered = [(w.get("hwnd"), w) for _c, w in planned]
            if (len(reordered) == len(picked)
                    and all(h for h, _w in reordered)):
                picked = reordered
                picked_corners = [c for c, _w in planned]
                sx, sy = sx2, sy2
    # 比例钳在安全区间：与 snap_four_quad 同一口径，否则预演算的分割线与
    # 正式排布的不是一条
    sx = min(max(sx, 0.15), 0.85)
    sy = min(max(sy, 0.15), 0.85)
    gap, edge_gap = _gap_settings()
    idx, placed_slots, unplaced = _quad_place(
        picked, mins, picked_corners, ms, idx, gap, edge_gap,
        allow_switch=monitor is None)
    mon = ms[idx]
    wl, wt, ww, wh = mon.work_rect
    if smart:
        sx, sy = _quad_smart_ratio(placed_slots, mins, ww, wh, sx, sy,
                                   origin=(wl, wt))
    # 上限缓存只取参与排列的窗口：_quad_cells 按轴消费（0 = 该轴无上限）
    maxes = {h: _QUAD_MAX_CACHE[h] for _c, h, _w in placed_slots
             if h in _QUAD_MAX_CACHE}
    _cells, finals, splits = _quad_cells(
        placed_slots, mins, wl, wt, ww, wh, gap, edge_gap, sx, sy,
        maxes=maxes, manual_split=(split_x != 0.5 or split_y != 0.5))
    for _corner, h, _fx, _fy, fw, fh in finals:
        cap = maxes.get(h) or (0, 0)
        # 这一格窗口实际能占多大：有上限的轴按上限，没上限的轴按格子
        pw = min(fw, cap[0]) if cap[0] > 0 else fw
        ph = min(fh, cap[1]) if cap[1] > 0 else fh
        bw, bh = fw - pw, fh - ph
        if bw <= blank_tol and bh <= blank_tol:
            continue
        app = h.split("::")[0]
        cap_s = []
        if cap[0] > 0:
            cap_s.append("宽最多 %d" % cap[0])
        if cap[1] > 0:
            cap_s.append("高最多 %d" % cap[1])
        side = []
        if bw > blank_tol:
            side.append("右侧")
        if bh > blank_tol:
            side.append("下方")
        warnings.append(
            "「%s」上次实测%s，这一格是 %d×%d，排完%s约 %dpx 空白"
            % (app, "、".join(cap_s), fw, fh, "、".join(side), max(bw, bh)))
        detail["blank"].append((h, bw, bh))
    if unplaced:
        names = "、".join(
            "%s(最小%dx%d)" % (h.split("::")[0], mins[h][0], mins[h][1])
            for h, _w in unplaced)
        warnings.insert(
            0, "只排 %d/%d 个：%s 按上次实测的最小尺寸放不下这块屏的四宫格，"
               "将保持原位" % (len(placed_slots), len(picked), names))
    detail["screen"] = mon.device_name
    detail["work"] = (ww, wh)
    detail["fit"] = len(placed_slots)
    detail["splits"] = splits
    if warnings:
        logger.info("四宫格预演：%s（%s %dx%d，预计 %d/%d 个）",
                    "；".join(warnings), mon.device_name, ww, wh,
                    len(placed_slots), len(picked))
    return warnings, detail


def monitors_list_snapshot():
    return monitors.enum_monitors()


def distribute_app_windows(owner_pattern, counts=None, activate=True):
    """把指定应用的所有窗口分配到各显示器，每屏内上下等分堆叠。

    owner_pattern: 应用名（不区分大小写子串匹配），如 "UU远程"。
    counts: 每屏分几个窗口（按显示器枚举顺序），如 [3, 3] 表示
            第 1 块屏 3 个、第 2 块屏 3 个；为 None 时按窗口数平均分配。
    返回 (移动成功的窗口数, 失败的窗口数)。

    每屏内部的堆叠方向按屏幕朝向自动选择：
    - 竖屏（高>宽）：上下等分（一列多行）
    - 横屏：左右等分（一行多列）
    """
    ms = monitors.enum_monitors()
    if not ms:
        return 0, 0
    pat = (owner_pattern or "").lower()
    wins = [w for w in list_target_windows()
            if w["owner"] and pat in w["owner"].lower()
            and w["pid"] not in _OWN_PIDS]
    if not wins:
        logger.warning("没有找到应用 %r 的窗口", owner_pattern)
        return 0, 0

    # 分配每屏窗口数
    if counts is None:
        n = len(wins)
        base, extra = divmod(n, len(ms))
    counts = list(counts)

    ok, miss = 0, 0
    wi = 0
    for mi, mon in enumerate(ms):
        if wi >= len(wins):
            break
        k = counts[mi] if mi < len(counts) else 0
        group = wins[wi:wi + k]
        wi += k
        if not group:
            continue
        wl, wt, ww, wh = mon.work_rect
        portrait = mon.height > mon.width
        n = len(group)
        rects = []
        for j, w in enumerate(group):
            if portrait:
                # 竖屏：上下等分（第 j 行）
                slot_h = wh // n
                y = wt + j * slot_h
                h = slot_h if j < n - 1 else wh - j * slot_h
                rects.append((wl, y, ww, h))
            else:
                # 横屏：左右等分（第 j 列）
                slot_w = ww // n
                x = wl + j * slot_w
                wid = slot_w if j < n - 1 else ww - j * slot_w
                rects.append((x, wt, wid, wh))
        # 分两轮写：先统一改尺寸、再统一改位置。
        # 一次写入里 position+size 同时下发时，系统按写入前的旧位置/旧尺寸
        # 做边界钳制（例如窗口在屏外时宽度钳到 680、贴边后没再拉到目标宽），
        # 导致竖屏整宽 1152 落到 680、横屏 504 落到最小宽度 698。拆开两轮后
        # 第二轮写入时窗口已在新位置/新尺寸，钳制基准正确。
        for w, rect in zip(group, rects):
            try:
                set_window_rect(w["hwnd"], w["x"], w["y"], rect[2], rect[3],
                                activate=False)
            except Exception:  # noqa: BLE001
                pass
        for w, rect in zip(group, rects):
            try:
                set_window_rect(w["hwnd"], rect[0], rect[1], rect[2], rect[3],
                                activate=False)
                ok += 1
            except Exception:  # noqa: BLE001
                miss += 1
    if activate and wins:
        app = wins[0]["owner"]
        _osa(f'tell application "{app}" to activate')
    logger.info("跨屏分配: 应用=%s 分配=%s 成功=%d 失败=%d",
                owner_pattern, counts, ok, miss)
    return ok, miss


def move_active_to_next_monitor(direction=1, use_pinned=True, activate=True):
    ms = monitors.enum_monitors()
    if not ms:
        return
    hwnd = get_foreground_window(use_pinned=use_pinned)
    if not hwnd:
        logger.warning("未找到可操作的前台窗口（可能只剩本程序自己）")
        return
    idx = _monitor_by_relative(ms, hwnd)
    # 优先按几何邻接找左右屏（Rectangle Pro 的 AdjacentScreens）：三块屏摆成
    # L 形时下标循环会把「往右移」绕到上面的屏，几何邻接才不会。
    # 竖屏不参与左右邻接（用户明确要求不适合竖屏的窗口不发到竖屏）；
    # 找不到几何邻居时才退化成下标循环，保证总有反应。
    ignore = {i for i, m in enumerate(ms) if m.height > m.width}
    nxt = screen_math.adjacent_monitor_index(ms, idx, direction, ignore=ignore)
    if nxt is None:
        n = len(ms)
        nxt = (idx + direction) % n
    move_to_monitor(hwnd, ms[nxt], activate=activate)


def snap_active(zone, use_pinned=True, activate=True):
    """分屏。

    activate=False 时只移动窗口、不把它激活到最前 —— 界面按钮用此模式，
    这样管理器窗口不会被目标窗口挤到后面，也就无需常驻置顶。
    """
    ms = monitors.enum_monitors()
    if not ms:
        return
    hwnd = get_foreground_window(use_pinned=use_pinned)
    if not hwnd:
        logger.warning("未找到可操作的前台窗口（可能只剩本程序自己）")
        return
    idx = _monitor_by_relative(ms, hwnd)
    # 吸附前记下原几何：吸附后若窗口左/上缘移动了，让相邻窗口跟着让位
    # （Rectangle Pro 的 adjustAdjacentWindows），避免左右两窗互相压盖
    old_rect = get_window_rect(hwnd)
    snap(hwnd, ms[idx], zone, activate=activate)
    try:
        import settings
        if settings.load().get("adjacent_resize", False):
            adjust_adjacent(hwnd, old_rect, get_window_rect(hwnd),
                            activate=activate)
    except Exception as e:  # noqa: BLE001
        logger.info("相邻窗口联动跳过: %s", e)


def stash_active(use_pinned=True, activate=False):
    """把当前/目标窗口暂存到屏幕顶边（复刻 Rectangle Pro 的 Stash）。

    返回 True 表示已暂存。已经暂存过的窗口不重复登记。
    """
    import stash
    ms = monitors.enum_monitors()
    if not ms:
        return False
    hwnd = get_foreground_window(use_pinned=use_pinned)
    if not hwnd:
        logger.warning("暂存：未找到可操作的前台窗口")
        return False
    if any(i.get("hwnd") == hwnd for i in stash.list_stashed()):
        logger.info("暂存：%s 已在暂存列表，跳过", hwnd)
        return False
    x, y, w, h = get_window_rect(hwnd)
    if not (x or y or w or h):
        return False
    idx = _monitor_by_relative(ms, hwnd)
    sx, sy, sw, sh = stash.stash_rect(ms[idx])
    set_window_rect(hwnd, sx, sy, sw, sh, activate=activate)
    stash.push({"hwnd": hwnd, "owner": hwnd.split("::")[0],
                "x": x, "y": y, "w": w, "h": h})
    logger.info("已暂存 %s（原位 %s）", hwnd, (x, y, w, h))
    return True


def unstash_first(activate=False):
    """取回最早暂存的窗口到它原来的位置（FIFO）。

    返回 (hwnd, True) 或 (None, False)。窗口已关闭时返回 None。
    """
    import stash
    item = stash.pop()
    if not item:
        return (None, False)
    hwnd = item.get("hwnd")
    if not hwnd:
        return (None, False)
    if _find_window(hwnd) is None:
        logger.info("取回：%s 已关闭，跳过", hwnd)
        return (None, False)
    try:
        set_window_rect(hwnd, item["x"], item["y"], item["w"], item["h"],
                        activate=activate)
    except Exception as e:  # noqa: BLE001
        logger.warning("取回 %s 失败: %s", hwnd, e)
        return (hwnd, False)
    logger.info("已取回 %s -> %s", hwnd, (item["x"], item["y"], item["w"], item["h"]))
    return (hwnd, True)


if __name__ == "__main__":
    print("own pids:", _OWN_PIDS)
    print("front external:", front_external_window())
    hwnd = get_foreground_window()
    print("front window:", hwnd)
    if hwnd:
        print("rect:", get_window_rect(hwnd))
