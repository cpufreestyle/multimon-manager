"""显示器枚举（macOS 实现，零第三方依赖）。

通过 CoreGraphics (ctypes) 获取精确的显示器 bounds/位置，
通过 system_profiler 文本输出获取显示器名称。
macOS 坐标系原点在左上角（CGDisplayBounds 返回的即是左上坐标系）。
"""
import copy
import ctypes
import ctypes.util
import logging
import re
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── CoreGraphics ctypes 绑定 ──────────────────────────────────────────

class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def _load_cg():
    """加载 CoreGraphics 并绑定常用函数。"""
    path = ctypes.util.find_library("CoreGraphics")
    if not path:
        return None
    cg = ctypes.CDLL(path)
    # CGGetActiveDisplayList
    cg.CGGetActiveDisplayList.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    # CGDisplayBounds
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    cg.CGDisplayBounds.restype = CGRect
    # CGMainDisplayID
    cg.CGMainDisplayID.argtypes = []
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    # CGDisplayIsBuiltin (10.14+)
    try:
        cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
        cg.CGDisplayIsBuiltin.restype = ctypes.c_int32
    except AttributeError:
        pass
    return cg


_cg = _load_cg()


# ── 显示器名称解析 ────────────────────────────────────────────────────

# C 债修复：system_profiler 偶发超时（曾用 15s）会拖住 UI，且超时后屏名退化为
# "Display N"。这里把超时收紧到 5s，并缓存"上次成功解析结果"，超时/解析失败时退回
# 缓存，保证屏名不退化（配合 F6 别名可进一步兜底）。
_names_cache = None


def _parse_display_names_text(text):
    """从 system_profiler 输出文本解析显示器条目列表（不触碰缓存）。"""
    # 定位 Displays: 段落（最后一段，即 GPU 下的显示器列表）
    sections = text.split("Displays:")
    if len(sections) < 2:
        return []

    display_lines = sections[-1].split("\n")
    results = []
    current = None

    for line in display_lines:
        stripped = line.rstrip()
        if not stripped:
            continue

        # 显示器名称行：8个空格缩进 + 名称 + 冒号
        # 属性行：10+个空格缩进 + key: value
        indent = len(line) - len(line.lstrip())
        has_colon = ":" in stripped

        if indent == 8 and has_colon and not stripped.startswith(" " * 10):
            # 新的显示器条目
            name = stripped.strip().rstrip(":")
            current = {"name": name, "resolution": "", "ui_looks_like": "", "is_main": False}
            results.append(current)
        elif indent > 8 and current is not None:
            # 属性行
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip().lower()
                val = val.strip()
                if "resolution" in key:
                    current["resolution"] = val
                elif "ui looks like" in key:
                    current["ui_looks_like"] = val
                elif "main display" in key:
                    current["is_main"] = "yes" in val.lower()

    return results


def _parse_display_names():
    """从 system_profiler SPDisplaysDataType 文本输出解析显示器名称和属性。

    返回列表，每项: (name, resolution_str, ui_looks_like, is_main)
    超时或解析失败时退回上次成功结果（C 债），避免屏名退化为 "Display N"。
    始终返回深拷贝，避免调用方（匹配时给条目打标记）污染缓存。
    """
    global _names_cache
    try:
        r = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True, text=True, timeout=5,
        )
        text = r.stdout
    except Exception as e:  # noqa: BLE001
        logger.warning("调用 system_profiler 失败（退回上次结果）: %s", e)
        return copy.deepcopy(_names_cache) if _names_cache else []

    results = _parse_display_names_text(text)
    if results:
        _names_cache = results
        return copy.deepcopy(results)
    # 解析不到（输出异常）：同样退回上次成功结果
    return copy.deepcopy(_names_cache) if _names_cache else []


def _parse_resolution_to_wh(res_str):
    """从分辨率字符串（如 '3200 x 1800 (QHD+)' 或 '3024 x 1964 Retina'）提取宽高。"""
    if not res_str:
        return (0, 0)
    # 取第一个类似 "N x N" 的部分
    m = re.search(r"(\d+)\s*x\s*(\d+)", res_str, re.IGNORECASE)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (0, 0)


# ── 主接口 ────────────────────────────────────────────────────────────

@dataclass
class MonitorInfo:
    index: int = 0
    device_name: str = ""
    device_path: str = ""
    is_primary: bool = False
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    work_left: int = 0
    work_top: int = 0
    work_width: int = 0
    work_height: int = 0

    @property
    def rect(self):
        return (self.left, self.top, self.width, self.height)

    @property
    def work_rect(self):
        return (self.work_left, self.work_top, self.work_width, self.work_height)


# ── 每屏真实可见区域（扣除菜单栏 / Dock）──────────────────────────────
#
# CGDisplayBounds 只给屏幕完整矩形，不含菜单栏与 Dock 的避让。若直接用它当
# 工作区，分屏时窗口会被系统钳制（顶部被菜单栏挡、底部被 Dock 挡），表现为
# "分屏位置不准"。NSScreen.visibleFrame 是系统权威值，逐屏给出真正可用区域。

class NSRect(ctypes.Structure):
    """NSRect，与 CGRect 内存布局一致（4 个 double）。"""
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("w", ctypes.c_double), ("h", ctypes.c_double)]


_objc = None
_objc_failed = False


def _load_objc():
    """加载 AppKit + libobjc，用于调用 NSScreen。"""
    global _objc, _objc_failed
    if _objc is not None or _objc_failed:
        return _objc
    try:
        ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
        objc = ctypes.CDLL("/usr/lib/libobjc.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        _objc = objc
    except Exception as e:  # noqa: BLE001
        logger.warning("加载 AppKit/objc 失败，工作区退化为扣除菜单栏: %s", e)
        _objc_failed = True
    return _objc


def _ns_screen_frames():
    """返回 [(完整区域, 可见区域), ...]，Cocoa 坐标（原点主屏左下、y 向上）。"""
    objc = _load_objc()
    if not objc:
        return []
    try:
        cls = objc.objc_getClass(b"NSScreen")
        if not cls:
            return []

        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        arr = objc.objc_msgSend(cls, objc.sel_registerName(b"screens"))
        if not arr:
            return []

        objc.objc_msgSend.restype = ctypes.c_uint64
        count = objc.objc_msgSend(arr, objc.sel_registerName(b"count"))

        sel_at = objc.sel_registerName(b"objectAtIndex:")
        sel_frame = objc.sel_registerName(b"frame")
        sel_visible = objc.sel_registerName(b"visibleFrame")

        out = []
        for i in range(count):
            objc.objc_msgSend.restype = ctypes.c_void_p
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64]
            scr = objc.objc_msgSend(arr, sel_at, i)
            if not scr:
                continue
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            objc.objc_msgSend.restype = NSRect
            f = objc.objc_msgSend(scr, sel_frame)
            v = objc.objc_msgSend(scr, sel_visible)
            out.append(((f.x, f.y, f.w, f.h), (v.x, v.y, v.w, v.h)))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 NSScreen 失败: %s", e)
        return []


def stage_manager_enabled():
    """台前调度（Stage Manager）是否开启。

    开启时，不同 App 的窗口只有处于同一个"台前组"才会同时显示在桌面上；
    macOS 没有公开 API 创建或修改分组，只能由用户手动把窗口拖到一起。
    因此并排前需要先手动分组，否则另一个窗口会被收进侧边。
    """
    try:
        r = subprocess.run(
            ["defaults", "read", "com.apple.WindowManager", "GloballyEnabled"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() == "1"
    except Exception:  # noqa: BLE001
        return False


def _compute_work_areas(monitors):
    """按 NSScreen.visibleFrame 填充各屏可用工作区；失败则退化为扣除菜单栏。"""
    frames = _ns_screen_frames()
    main_h = None
    for full, _v in frames:
        if abs(full[0]) < 1 and abs(full[1]) < 1:  # Cocoa 下主屏 frame 原点为 (0,0)
            main_h = full[3]
            break

    if frames and main_h:
        def to_cg(r):
            """Cocoa(左下原点、y 向上) → CG(左上原点、y 向下)。"""
            x, y, w, h = r
            return (x, main_h - (y + h), w, h)

        applied = 0
        for m in monitors:
            best, best_d = None, None
            for full, vis in frames:
                fx, fy, fw, fh = to_cg(full)
                d = (abs(fx - m.left) + abs(fy - m.top)
                     + abs(fw - m.width) + abs(fh - m.height))
                if best_d is None or d < best_d:
                    best_d, best = d, to_cg(vis)
            if best and best_d is not None and best_d <= 4:
                vx, vy, vw, vh = best
                if vw > 0 and vh > 0:
                    m.work_left = int(round(vx))
                    m.work_top = int(round(vy))
                    m.work_width = int(round(vw))
                    m.work_height = int(round(vh))
                    applied += 1
        if applied:
            logger.debug("工作区取自 NSScreen.visibleFrame（%d/%d 屏）",
                         applied, len(monitors))
            return

    # 退化：所有屏统一扣除菜单栏高度
    for m in monitors:
        m.work_left = m.left
        m.work_width = m.width
        m.work_top = m.top + _MENU_BAR_PT
        m.work_height = max(m.height - _MENU_BAR_PT, 0)


# 显示器枚举带缓存：窗口工具会高频调用 enum_monitors()，而 system_profiler
# 子进程每次约耗时 1~2s，反复拉起会导致跨屏移动/分屏明显卡顿。加短时 TTL 缓存。
_CACHE_TTL = 2.0
_mon_cache = {"ts": 0.0, "data": None}

# macOS 主屏顶部菜单栏高度（点），用于推算可用工作区，避免顶部分屏被遮挡
_MENU_BAR_PT = 26


def invalidate_monitors_cache():
    """强制下次重新枚举（如用户点击"刷新显示器"）。"""
    _mon_cache["ts"] = 0.0
    _mon_cache["data"] = None


def enum_monitors(force=False):
    """枚举所有活跃显示器，返回 MonitorInfo 列表（带 2s 缓存）。"""
    now = time.monotonic()
    if not force and _mon_cache["data"] is not None and now - _mon_cache["ts"] < _CACHE_TTL:
        return _mon_cache["data"]

    monitors = []

    if _cg is None:
        return monitors

    # 1. 通过 CoreGraphics 获取所有显示器的 bounds
    count = ctypes.c_uint32(0)
    _cg.CGGetActiveDisplayList(0, None, ctypes.byref(count))
    if count.value == 0:
        return monitors

    display_ids = (ctypes.c_uint32 * count.value)()
    _cg.CGGetActiveDisplayList(count.value, display_ids, ctypes.byref(count))

    main_id = _cg.CGMainDisplayID()

    cg_displays = []
    for i in range(count.value):
        did = display_ids[i]
        bounds = _cg.CGDisplayBounds(did)
        w = int(bounds.size.width)
        h = int(bounds.size.height)
        x = int(bounds.origin.x)
        y = int(bounds.origin.y)
        is_builtin = False
        try:
            is_builtin = bool(_cg.CGDisplayIsBuiltin(did))
        except Exception:
            pass
        is_main = did == main_id

        cg_displays.append({
            "id": did,
            "x": x, "y": y,
            "width": w, "height": h,
            "is_builtin": is_builtin,
            "is_main": is_main,
        })

    # 2. 解析 system_profiler 获取显示器名称
    names_info = _parse_display_names()

    # 3. 匹配：按名称中的分辨率与 CGDisplay bounds 大小匹配
    #    例如 RV200 Pro "UI Looks like: 1600 x 900" → CGDisplay bounds 1600x900
    def _match_display(cg_info, names):
        """尝试将一个 CG 显示器匹配到 system_profiler 名称条目。"""
        for nm in names:
            if nm.get("matched"):
                continue
            # 尝试 UI Looks like 匹配
            if nm["ui_looks_like"]:
                uw, uh = _parse_resolution_to_wh(nm["ui_looks_like"])
                if uw == cg_info["width"] and uh == cg_info["height"]:
                    nm["matched"] = True
                    return nm
            # 尝试主分辨率匹配（非 Retina 时直接对得上）
            rw, rh = _parse_resolution_to_wh(nm["resolution"])
            if rw == cg_info["width"] and rh == cg_info["height"]:
                nm["matched"] = True
                return nm
        return None

    for i, cg in enumerate(cg_displays):
        matched = _match_display(cg, names_info)
        if matched:
            name = matched["name"]
            is_main = matched["is_main"] or cg["is_main"]
        else:
            # fallback: 使用 CG 信息构造名称
            if cg["is_builtin"]:
                name = "Built-in Display"
            else:
                name = f"Display {i + 1}"
            is_main = cg["is_main"]

        monitors.append(MonitorInfo(
            index=i,
            device_name=name,
            device_path=str(cg["id"]),
            is_primary=is_main,
            left=cg["x"],
            top=cg["y"],
            width=cg["width"],
            height=cg["height"],
            work_left=cg["x"],
            work_top=cg["y"],
            work_width=cg["width"],
            work_height=cg["height"],
        ))

    # 工作区：逐屏取系统真实可见区域（扣除菜单栏 / Dock）
    _compute_work_areas(monitors)

    _mon_cache["data"] = monitors
    _mon_cache["ts"] = now
    return monitors


def get_virtual_screen():
    """获取虚拟桌面总区域 (left, top, width, height)。"""
    ms = enum_monitors()
    if not ms:
        return (0, 0, 0, 0)
    xs = [m.left for m in ms]
    ys = [m.top for m in ms]
    x2 = [m.left + m.width for m in ms]
    y2 = [m.top + m.height for m in ms]
    x, y = min(xs), min(ys)
    return (x, y, max(x2) - x, max(y2) - y)


def get_primary_monitor():
    """获取主显示器。"""
    ms = enum_monitors()
    for m in ms:
        if m.is_primary:
            return m
    return ms[0] if ms else None


if __name__ == "__main__":
    for m in enum_monitors():
        print(m.device_name, m.width, "x", m.height, "pos", m.left, m.top, "primary", m.is_primary)
    print("virtual:", get_virtual_screen())
