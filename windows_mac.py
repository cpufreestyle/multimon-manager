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
import logging
import os
import subprocess
import threading
import time

import monitors_mac as monitors
from monitors_mac import CGRect

import quad_plan
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
_QUAD_MAX_PASSES = 3


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
        x, y, w, h = (int(float(v)) for v in out.split(","))
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
        parts = item.split(",")
        if len(parts) != 4:
            continue
        try:
            nums = [abs(int(float(v))) for v in parts]
        except ValueError:  # noqa: BLE001
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
    if not _suppress_undo:
        try:
            push_undo(hwnd, get_window_rect(hwnd))
        except Exception:  # noqa: BLE001
            pass
    app2, nm, _sq = parse_hwnd(hwnd)
    known = _last_rect.get(hwnd)
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

# ── 四宫格：最小尺寸实测与几何 ─────────────────────────────────────────

# 探测最小尺寸时请求的尺寸：宽高都明显小于常见 App 的下限，读回的就是下限
_PROBE_W, _PROBE_H = 320, 120

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
        x, y, w, h = (int(float(v)) for v in out.split(","))
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


def _probe_min_size(hwnd, at_x, at_y):
    """实测一个窗口的最小可用尺寸：写一个小尺寸，读回被 App 钳制后的几何。

    macOS 上查不到这个值——真机实测 ChatGPT / Qoder CN 等窗口根本没有
    AXMinSize 属性，只能写小再读回。返回 (min_w, min_h)；读不到时返回 (0, 0)。

    位置先挪到目标屏工作区左上角再缩小：App 的最小尺寸会把窗口撑回去，
    贴着屏幕底边缩小时撑回去的那部分就出了屏，macOS 会把整个窗口判给别的屏
    （真机实测 RV200 Pro 上 ChatGPT y=-200 被搬到主屏）——探测不能反过来把
    窗口搬走。写完留下的几何无所谓，紧接着的正式排布会把它放进自己的格子。
    """
    app, name, _seq = parse_hwnd(hwnd)
    # 探测用的选择子在探测前算好：探测会把窗口缩到 320x120，之后任何按几何
    # 匹配都会因偏差过大而失效（真机实测偏差 2939），序号选择又会落到别的
    # 同名窗口上——这正是「四宫格把窗口送到 Sidecar」的根因。
    sel = (_ax_window_by_geometry(app, _last_rect.get(hwnd), name)
           or _ax_window_selector(hwnd) or "window 1")
    script = (
        'tell application "System Events"\n'
        f'  tell process "{app}"\n'
        f'    set position of {sel} to {{{int(at_x)}, {int(at_y)}}}\n'
        f'    set size of {sel} to {{{_PROBE_W}, {_PROBE_H}}}\n'
        '  end tell\n'
        'end tell\n'
    )
    # 真机实测：同一个 App 用同一个小尺寸写入，读回值不稳定——有时是钳制后的
    # 下限、有时是写入过程里的中间值（还带着旧的大尺寸）。旧的「探两次取逐维
    # 更小」因此取到了收缩中间帧：访达探出 322、实际下限 404，四宫格按 322
    # 留空间，写入时窗口被撑回 404，上下两排重叠 40px——「不准」的来源之一。
    # 改成连续轮询：连续两次读数逐维一致（±2px）才认，读满 6 轮仍不一致就取
    # 最后一次（至少比中间帧接近真实下限）。
    stable = None
    last = None
    for i in range(_PROBE_MAX_READS):
        _osa(script)
        time.sleep(_probe_settle_secs if i == 0 else _probe_poll_secs)
        got = _readback_rect(hwnd, (int(at_x), int(at_y),
                                    _PROBE_W, _PROBE_H))
        if not got or got == (0, 0, 0, 0):
            continue
        cur = (got[2], got[3])
        if cur[0] <= 0 or cur[1] <= 0:
            continue
        last = cur
        if stable is not None and all(abs(a - b) <= 2
                                      for a, b in zip(cur, stable)):
            return cur
        stable = cur
    if last is None:
        logger.warning("最小尺寸探测读不到几何: %s", hwnd)
        return (0, 0)
    return last


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


def _quad_zone(idx, row_full, col_full):
    """按「哪一排/列有窗口」选间距方位。

    某一排整排空着时，剩下那一排撑满工作区高：这时候它贴的是屏幕上/下边
    （外边），不再是格子之间的缝，得按 left/right 的方位让间距——否则窗口
    底部会平白让出半个缝、看起来没贴到底边。列同理（top/bottom）。
    """
    if row_full:
        return "left" if idx in (0, 2) else "right"
    if col_full:
        return "top" if idx in (0, 1) else "bottom"
    return _QUAD_GAP_ZONE[idx]


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


def _quad_cells(placed_slots, mins, wl, wt, ww, wh, gap, edge_gap,
                sx, sy):
    """按比例 + 实测最小尺寸切出四个格子，并算每个窗口的写入目标。

    placed_slots: [(corner, hwnd, win_dict), ...] 实际排放的窗口（1~4 个）
    mins: {hwnd: (min_w, min_h)} 实测最小尺寸
    返回 (cells, finals, row_full, col_full)：cells 是四个格子的原始矩形，
    finals 是让出窗口间距后的写入目标 [(corner, hwnd, x, y, w, h), ...]。

    格子严格按 sx/sy 铺满工作区：最小尺寸只用来把某一排/列切大（装得下才调），
    不改变「谁去哪个角」。整排 / 整列空着时，有窗口的那排 / 列撑满整个工作区
    ——选两个窗口就该铺满屏幕，留半屏空白看起来就是「没排好」。

    抽成函数是为了二趟重排：第一遍按探测到的最小尺寸切，写入后读回的是窗口
    **实际**占的尺寸，调用方把实测尺寸并回 mins 后再调一次，格子随之收敛。
    """
    c1, c2, r1, r2, ov_x, ov_y = quad_plan.grid_geometry(
        ww, wh, sx, sy)

    # 四个格子严格按 sx/sy 铺满工作区：不再用最小尺寸把某一排/列顶高。
    # 顶高的直接后果是两排都装不下、必然重叠（真机实测 UU远程 最小高 744
    # + 下排 570 = 1314 > 898 工作区，怎么切都重叠 400+px）。
    # Moom 的做法是格子铺满 + 窗口在格子里等比缩放（fit_frame），窗口被
    # App 的最小尺寸顶大时只会朝「不贴边的那一侧」溢出，贴边的一侧仍然准确。
    col_x = (wl, wl + c1)
    row_y = (wt, wt + r1)
    # 用实测最小尺寸校正切分：两排/两列都装得下时，按各自需求重切，
    # 剩余空间仍按原比例分。只在「装得下」时调整，所以不会把某一边压成
    # 几个像素；装不下才允许溢出（由后面的贴边 + 如实报告处理）。
    # 没有这一层，ChatGPT 最小高 600 会被塞进 358 高的格子，向下溢出 240px
    # 压住下排——真机实测四宫格上下重叠 246px 就是这么来的。
    # 需求只按「实际排放的窗口」算：放不下的窗口已经把角让出来了，它的最小
    # 尺寸再抬高某一排/列，只会把留给活窗口的空间压小。空着的那排/列不参与
    # 余量分配——它没有窗口，分到空间只会在排好的那一排里留一块死区。
    half = gap / 2.0
    pad = half + (half if edge_gap else 0)   # 每格每轴要让出的窗口间距
    need_col = [0, 0]      # 左列 / 右列 的最小宽度需求
    need_row = [0, 0]      # 上排 / 下排 的最小高度需求
    for corner, h, _w in placed_slots:
        ci = 0 if corner in (0, 2) else 1      # 0 左列 / 1 右列
        ri = 0 if corner in (0, 1) else 1      # 0 上排 / 1 下排
        mw, mh = mins.get(h, (0, 0))
        need_col[ci] = max(need_col[ci], mw)
        need_row[ri] = max(need_row[ri], mh)
    # 哪一排/列有窗口由「选中了哪些角」定，不由最小尺寸定：最小尺寸探测失败
    # （全 0）时也不能退化成「只排上面半屏、下面半屏空白」。
    has_row = [False, False]
    has_col = [False, False]
    for corner, _h, _w in placed_slots:
        has_row[0 if corner in (0, 1) else 1] = True
        has_col[0 if corner in (0, 2) else 1] = True
    lo_r1 = need_row[0] + pad if need_row[0] else 0
    lo_r2 = need_row[1] + pad if need_row[1] else 0
    # 只有一排有窗口时，这一排撑满整个工作区高：选两个窗口就该铺满屏幕，留
    # 半屏空白看起来就是「没排好」。只有一个窗口时不撑满（那是 maximize，不是
    # 四宫格）；空着的那排高度为 0，界面上的横向分割线随之贴到底边。
    row_full = len(placed_slots) >= 2 and has_row[0] != has_row[1]
    if row_full:
        # 哪一排有窗口看 has_row，不看 lo_r1：最小尺寸探测不到（全 0）时
        # lo_r1 也是 0，拿它判会把有窗口的那排判成 0 高——窗口被压成 1px
        r1 = wh if has_row[0] else 0
        r2 = wh - r1
        row_y = (wt, wt + r1)
        logger.info("四宫格：只有%s有窗口，%s撑满工作区高 %d",
                    "上排" if has_row[0] else "下排",
                    "上排" if has_row[0] else "下排", wh)
    elif lo_r1 + lo_r2 <= wh and (r1 < lo_r1 or r2 < lo_r2):
        if lo_r1 and lo_r2:
            slack = wh - lo_r1 - lo_r2
            share = (r1 / float(r1 + r2)) if (r1 + r2) > 0 else 0.5
            r1 = min(wh - lo_r2, lo_r1 + int(round(slack * share)))
            r1 = max(r1, lo_r1)
        else:
            # 只有一个窗口落在某一排：那一排拿足自己的需求，剩下的留给空排
            r1 = lo_r1 or (wh - lo_r2)
        r2 = wh - r1
        row_y = (wt, wt + r1)
        logger.info("四宫格：按最小高度重切上排 %d / 下排 %d（需求 %d / %d）",
                    r1, r2, need_row[0], need_row[1])
    lo_c1 = need_col[0] + pad if need_col[0] else 0
    lo_c2 = need_col[1] + pad if need_col[1] else 0
    # 与行同理：只有一列有窗口时，这一列撑满整个工作区宽
    col_full = len(placed_slots) >= 2 and has_col[0] != has_col[1]
    if col_full:
        # 同行：哪一列有窗口看 has_col，不看 lo_c1（理由同行）
        c1 = ww if has_col[0] else 0
        c2 = ww - c1
        col_x = (wl, wl + c1)
        logger.info("四宫格：只有%s有窗口，%s撑满工作区宽 %d",
                    "左列" if has_col[0] else "右列",
                    "左列" if has_col[0] else "右列", ww)
    elif lo_c1 + lo_c2 <= ww and (c1 < lo_c1 or c2 < lo_c2):
        if lo_c1 and lo_c2:
            slack = ww - lo_c1 - lo_c2
            share = (c1 / float(c1 + c2)) if (c1 + c2) > 0 else 0.5
            c1 = min(ww - lo_c2, lo_c1 + int(round(slack * share)))
            c1 = max(c1, lo_c1)
        else:
            # 只有一个窗口落在某一列：那一列拿足自己的需求，剩下的留给空列
            c1 = lo_c1 or (ww - lo_c2)
        c2 = ww - c1
        col_x = (wl, wl + c1)
        logger.info("四宫格：按最小宽度重切左列 %d / 右列 %d（需求 %d / %d）",
                    c1, c2, need_col[0], need_col[1])
    cells = [(col_x[0], row_y[0], c1, r1),
             (col_x[1], row_y[0], c2, r1),
             (col_x[0], row_y[1], c1, r2),
             (col_x[1], row_y[1], c2, r2)]
    # 窗口在格子里的最终矩形：直接填满自己那一格 + 让出窗口间距
    # （fit_frame 只按需传入 corner，早先的等比缩放语义见下面的说明）
    finals = []
    for corner, h, _w in placed_slots:
        # 先让出窗口间距：间距吸收 App 最小尺寸造成的溢出，四格也清清楚楚
        # 分开（Rectangle Pro 的 GapCalculation）
        bx, by, bw, bh = cells[corner]
        bx, by, bw, bh = gap_rect(_quad_zone(corner, row_full, col_full),
                                  bx, by, bw, bh, wl, wt, ww, wh)
        # 窗口直接填满自己那一格，与单窗 quad-* 吸附（snap）同一套语义：
        # 用户点「2×2 排列」要的就是四块铺满。早先在这里按原宽高比等比缩放
        # （fit_frame 只缩小、不放大），真机实测窗口比格子小时停在格子里偏在
        # 一角、留一块空区——那正是「四格不准」的观感来源。窗口被 App 的最小
        # 尺寸顶大时，由后面的贴边 / 居中逻辑收尾。
        finals.append((corner, h, bx, by, max(1, bw), max(1, bh)))
    return cells, finals, row_full, col_full


def _quad_write(finals, cells, wl, wt, ww, wh, smart, unwritable, notes,
                detect_unwritable=True):
    """把 finals 写进窗口，读回实测几何，再按方位贴边收尾。

    返回 {hwnd: 实测矩形}：调用方据此判断「窗口被 App 钳得比格子大」，用实测
    尺寸重算格子再写一遍（见 snap_four_quad 的二趟重排）。

    detect_unwritable=False 时跳过「写入无效」判定：第二趟的「写入前几何」就是
    第一趟的结果，被钳制的窗口两趟读回相同，会被误判成「一步没动、写不动」。
    """
    measured = {}
    for corner, h, fx, fy, fw, fh in finals:
        # 写入前先取 CG 几何：判「写入有没有生效」用它，不用 _last_rect（探测
        # 失败时它可能停留在很久以前的枚举值），也不用 AX 读回（App 不开放
        # 自动化时读回直接失败，真机微信、CrossPaste 恒报 -1719）
        pre = _cg_rect(h) if detect_unwritable else None
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
        # 窗口被 App 顶大后，按方位把该贴的边贴住：左上格贴左上、右上格贴
        # 右上、左下格贴左下、右下格贴右下。溢出只发生在远离贴边的那一侧。
        ax, ay = gx, gy
        if corner in (1, 3):
            ax = max(cells[corner][0], cells[corner][0] + cells[corner][2] - gw)
        if corner in (2, 3):
            # 底缘优先：窗口比格子高时，贴格子底边会越出工作区底边，
            # macOS 随即将整个窗口搬到别的屏（真机实测 ChatGPT 被搬到
            # RV200 Pro）。所以先保证不越工作区底边，再尽量贴近格子底边。
            ay = min(max(cells[corner][1] + cells[corner][3] - gh, wt),
                     max(cells[corner][1], wt + wh - gh))
        if (ax, ay) != (gx, gy):
            set_window_rect(h, ax, ay, gw, gh, activate=False)
    return measured


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
    _missing_windows = []

    cands = [w for w in list_target_windows()
             if w["pid"] not in _OWN_PIDS and w["owner"]]
    cands_by_hwnd = {w["hwnd"]: w for w in cands}

    # 选几个就排几个：在网格里点掉不想要的格（或下拉里选「自动」）即可只排
    # 两三个。这块屏放不下四个是常态（真机 Built-in 1512x896，四个窗口的
    # App 最小尺寸总和动辄 1800x1300），能让用户主动选 2 个 / 3 个来排，比
    # 每次放不下再解释一遍有用。显式选满 2 个及以上时不再自动补足；只选 1 个
    # 或一个都没选时按老行为补足四个（用户还在选，或想用固定的目标窗口）。
    wants = (tl_hwnd, tr_hwnd, bl_hwnd, br_hwnd)
    keep_only_selected = len([x for x in wants if x]) >= 2
    picked, picked_corners, seen = [], [], set()
    for corner, want in enumerate(wants):
        w = None
        if want:
            w = cands_by_hwnd.get(want) or _find_window(want)
            if w is None and want not in seen:
                # 下拉里选中的窗口已经关掉/改名：如实说明，不能静默换一个
                # 别的窗口顶上（真机实测选 CodeBuddy 的格子被换成了 ChatGPT，
                # 用户看到的就是「智能/非智能都不准」）
                logger.warning("四宫格：指定的窗口 %s 已不可用", want)
                _missing_windows.append(want)
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
        # 登记 CG 侧几何：set_window_rect 靠它在 AX 窗口列表里认出这个窗口
        # （CG 枚举序与 AX 序不一致时，纯按序号会选到别的窗口）
        _last_rect[h] = (w["x"], w["y"], w["w"], w["h"])
        picked.append((h, w))
        picked_corners.append(corner)
        seen.add(h)
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
            plan_wins = [dict(picked[i][1], hwnd=picked[i][0],
                              min_w=plan_mins[picked[i][0]][0],
                              min_h=plan_mins[picked[i][0]][1])
                         for i in order_idx]
            # 用户在界面上给每个窗口点选的格：传给 plan_quad_layout，它就只在
            # 这些角里分配，不会把「左上」的窗口排到右上。
            plan_corners = sorted(set(picked_corners[i] for i in order_idx))
            planned, sx2, sy2 = quad_plan.plan_quad_layout(
                plan_wins, ww, wh, corners=plan_corners)
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
    pool = [picked[i] for i in pool_idx]
    # 每个窗口原本认哪个角：用户点掉几格后 picked 不满四个，角必须对得上，
    # 否则「左上」的窗口会被排到右上
    pool_corners = [picked_corners[i] for i in pool_idx]
    placed_slots, unplaced = quad_plan.choose_quad_slots(
        pool, mins, ww, wh, gap, edge_gap, corners=pool_corners)
    # 没点名屏幕时，换一块能多排几个的横屏：放不下四个是常态，能多排一个是
    # 一个。竖屏不主动送——用户明确说过不适合竖屏的窗口不要发到竖屏屏幕
    if len(placed_slots) < len(pool) and monitor is None:
        for i, m in enumerate(ms):
            if i == idx or m.height > m.width:
                continue
            _ml, _mt, mw2, mh2 = m.work_rect
            slots2, unplaced2 = quad_plan.choose_quad_slots(
                pool, mins, mw2, mh2, gap, edge_gap, corners=pool_corners)
            if len(slots2) > len(placed_slots):
                logger.info("屏幕 %s 只能排 %d 个，%s 能排 %d 个，改排到 %s",
                            mon.device_name, len(placed_slots),
                            m.device_name, len(slots2), m.device_name)
                idx, placed_slots, unplaced = i, slots2, unplaced2
                mon = ms[idx]
                wl, wt, ww, wh = mon.work_rect
    logger.info("四宫格：%d/%d 个窗口放得下 %s（工作区 %dx%d）",
                len(placed_slots), len(picked), mon.device_name, ww, wh)
    # 格子严格按 sx/sy 铺满：不把最小尺寸传进去顶高某一排/列。
    # 最小尺寸只影响「窗口在格子里 fit 后能被 App 顶多大」，以及
    # 顶大后按方位贴边——格子本身永远铺满，两排永不重叠。
    notes = []
    # 写入无效的窗口（App 不开放自动化 / 被系统钉住）：写入前后几何一步没变
    unwritable = []

    cells, finals, row_full, col_full = _quad_cells(
        placed_slots, mins, wl, wt, ww, wh, gap, edge_gap, sx, sy)
    # 二趟重排：第一遍按探测到的最小尺寸切格子，写入后读回的是窗口**实际**占的
    # 尺寸。探测偏小（取到收缩中间帧）时窗口被 App 撑得比格子大，上下两排必然
    # 重叠——把实测尺寸并进最小尺寸（只升不降）再切一遍格子，单调收敛，最多三遍。
    for attempt in range(_QUAD_MAX_PASSES):
        measured = _quad_write(finals, cells, wl, wt, ww, wh, smart,
                               unwritable, notes,
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
            if (nw, nh) != (mw, mh):
                mins[h] = (nw, nh)
                grew = True
        if not grew:
            break
        cells2, finals2, rf2, cf2 = _quad_cells(
            placed_slots, mins, wl, wt, ww, wh, gap, edge_gap, sx, sy)
        if cells2 == cells and finals2 == finals:
            break
        cells, finals, row_full, col_full = cells2, finals2, rf2, cf2
        logger.info("四宫格：第 %d 遍按实测尺寸重排（最小尺寸已更新）",
                    attempt + 1)
    # 实际生效的比例必须按**校正后**的格子算：早先按 grid_geometry 的毛值算，
    # 真机实测把 62%/29% 报成 45%/50%，界面上的分割线与实际排出来的位置对不
    # 上，用户看到的就是「不准」。上下限放到 0.05~0.95：最小尺寸把某一排顶到
    # 85% 以上时，按 0.85 钳同样会让分割线与实际位置不符。
    global _last_quad_splits
    _last_quad_splits = (min(max(cells[0][2] / float(ww or 1), 0.05), 1.0),
                         min(max(cells[0][3] / float(wh or 1), 0.05), 1.0))
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
            logger.info("四宫格偏差 %s：目标=%s 实测=%s（最大偏差 %d px，"
                        "可能被应用的最小窗口尺寸钳制）", h, want, got, dev)
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
    _last_quad_notes[:] = notes
    return True


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
