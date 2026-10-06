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

logger = logging.getLogger(__name__)

# ── 撤销栈：记录每次移动/缩放前的窗口几何，供"撤销移动"使用（F7）──
_undo_stack = []
_undo_lock = threading.Lock()
_suppress_undo = False


def push_undo(hwnd, rect):
    """记录一次移动前几何（hwnd, (x, y, w, h)）；保留最近 20 步。"""
    with _undo_lock:
        _undo_stack.append((hwnd, tuple(rect)))
        if len(_undo_stack) > 20:
            _undo_stack.pop(0)


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
    if not _suppress_undo:
        try:
            push_undo(hwnd, get_window_rect(hwnd))
        except Exception:  # noqa: BLE001
            pass
    app, _name, _seq = parse_hwnd(hwnd)
    win_sel = _ax_window_selector(hwnd)
    # 原始目标（content 坐标），闭环全程以此为基准，偏差按「实测 - 原始目标」累计
    tx, ty, tw, th = (int(round(v)) for v in (x, y, w, h))
    # 幂等重设次数上限：下半屏约 3 次命中；整屏高度类（变化量最大）需更多次才触顶
    max_retry = 16
    prev = None
    best_dev = None
    stall = 0
    logger.info("移动窗口: 进程=%s -> content x=%d y=%d w=%d h=%d", app, tx, ty, tw, th)
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


def _monitor_by_relative(monitors_list, src_hwnd):
    x, y, w, h = get_window_rect(src_hwnd)
    cx, cy = x + w // 2, y + h // 2
    for i, m in enumerate(monitors_list):
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return i
    return 0


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
    if zone == "left":
        set_window_rect(hwnd, wl, wt, ww // 2, wh, activate=activate)
    elif zone == "right":
        set_window_rect(hwnd, wl + ww // 2, wt, ww - ww // 2, wh, activate=activate)
    elif zone == "top":
        set_window_rect(hwnd, wl, wt, ww, wh // 2, activate=activate)
    elif zone == "bottom":
        set_window_rect(hwnd, wl, wt + wh // 2, ww, wh - wh // 2, activate=activate)
    elif zone == "maximize":
        set_window_rect(hwnd, wl, wt, ww, wh, activate=activate)
    elif zone == "center":
        set_window_rect(hwnd, wl + (ww - w) // 2, wt + (wh - h) // 2, w, h,
                        activate=activate)
    elif zone == "left-third":
        set_window_rect(hwnd, wl, wt, ww // 3, wh, activate=activate)
    elif zone == "middle-third":
        set_window_rect(hwnd, wl + ww // 3, wt, ww // 3, wh, activate=activate)
    elif zone == "right-third":
        set_window_rect(hwnd, wl + 2 * (ww // 3), wt, ww - 2 * (ww // 3), wh,
                        activate=activate)
    elif zone == "quad-tl":
        set_window_rect(hwnd, wl, wt, ww // 2, wh // 2, activate=activate)
    elif zone == "quad-tr":
        set_window_rect(hwnd, wl + ww // 2, wt, ww - ww // 2, wh // 2, activate=activate)
    elif zone == "quad-bl":
        set_window_rect(hwnd, wl, wt + wh // 2, ww // 2, wh - wh // 2, activate=activate)
    elif zone == "quad-br":
        set_window_rect(hwnd, wl + ww // 2, wt + wh // 2, ww - ww // 2, wh - wh // 2,
                        activate=activate)


def _monitor_index_by_rect(monitors_list, x, y, w, h):
    """按给定矩形（而非 hwnd）判断所在显示器索引。"""
    cx, cy = x + w // 2, y + h // 2
    for i, m in enumerate(monitors_list):
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return i
    return 0


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
        counts = [base + (1 if i < extra else 0) for i in range(len(ms))]
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
    snap(hwnd, ms[idx], zone, activate=activate)


if __name__ == "__main__":
    print("own pids:", _OWN_PIDS)
    print("front external:", front_external_window())
    hwnd = get_foreground_window()
    print("front window:", hwnd)
    if hwnd:
        print("rect:", get_window_rect(hwnd))
