"""跨平台屏幕几何工具（与窗口后端解耦，纯函数便于单测）。

三个职责（均从 Moom 的公开接口/二进制元数据分析中借鉴实现思路）：
1. 交集最长判屏：窗口横跨两块屏交界时，中心点法会随机判给其中一块；
   与哪块屏交集最长就归哪块，才是用户看到的真实归属。
2. 归一化相对框：布局保存时把窗口矩形换算成所在屏工作区上的 0~1 比例框；
   屏幕尺寸变化后按比例重映射，布局就能在不同显示器组合间自适应。
3. 贴边钉定：目标矩形越出工作区时按左/顶优先、右/底兜底钉回边界内，
   不允许把窗口摆到屏幕外（否则 macOS 会把它搬到主屏）。
"""

import sys


def rect_intersection_area(a, b):
    """两个矩形 (x, y, w, h) 的交集面积；不相交返回 0。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ox = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    oy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return ox * oy


def monitor_index_by_intersection(monitors, x, y, w, h, fallback=0):
    """按「交集面积最长」把矩形判给显示器索引。

    中心点不在任何屏内（例如窗口被系统移到坐标系外）时，退回中心点法，
    再不行用 fallback——行为比旧行为只强不弱。
    """
    best_i, best_area = None, 0
    for i, m in enumerate(monitors):
        area = rect_intersection_area((x, y, w, h),
                                      (m.left, m.top, m.width, m.height))
        if area > best_area:
            best_i, best_area = i, area
    if best_i is not None:
        return best_i
    cx, cy = x + w // 2, y + h // 2
    for i, m in enumerate(monitors):
        if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
            return i
    return fallback


def normalize_to_work(x, y, w, h, work_left, work_top, work_width, work_height):
    """窗口矩形 -> 工作区上的相对框 (rx, ry, rw, rh)，取值 0~1。

    尺寸为 0 的工作区返回全 0；越出工作区的矩形做软夹取（比例可为负或 >1），
    交由 clamp_to_work 钉回。
    """
    if work_width <= 0 or work_height <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        (x - work_left) / float(work_width),
        (y - work_top) / float(work_height),
        w / float(work_width),
        h / float(work_height),
    )


def denormalize_to_work(rel, work_left, work_top, work_width, work_height):
    """相对框 -> 窗口矩形（与 normalize_to_work 互逆）。"""
    rx, ry, rw, rh = rel
    return (
        int(round(work_left + rx * work_width)),
        int(round(work_top + ry * work_height)),
        int(round(rw * work_width)),
        int(round(rh * work_height)),
    )


def clamp_to_work(x, y, w, h, work_left, work_top, work_width, work_height):
    """把矩形钉回工作区内（宽度/高度优先保住，位置随后贴边）。

    顺序：先尺寸（可能被夹小）、后位置（左/顶优先，右/底兜底）。与 Moom 的
    sticksToRightEdge/sticksToBottomEdge 思路一致：宁可少露一截，也不越界。
    """
    if work_width <= 0 or work_height <= 0:
        return (int(x), int(y), int(w), int(h))
    w = max(1, min(int(w), work_width))
    h = max(1, min(int(h), work_height))
    x = int(x)
    y = int(y)
    if x < work_left:
        x = work_left
    elif x + w > work_left + work_width:
        x = work_left + work_width - w
    if y < work_top:
        y = work_top
    elif y + h > work_top + work_height:
        y = work_top + work_height - h
    return (x, y, w, h)


def best_work_for_rect(monitors, x, y, w, h):
    """返回矩形归属显示器的工作区 (work_left, work_top, work_width, work_height)。

    先按交集最长找归属屏；窗口完全脱离任何屏（读回异常）时退回首个屏。
    """
    idx = monitor_index_by_intersection(monitors, x, y, w, h)
    m = monitors[idx] if idx < len(monitors) else monitors[0]
    return (m.work_left, m.work_top, m.work_width, m.work_height)


def adjacent_monitor_index(monitors, cur_idx, direction, ignore=None):
    """按几何邻接关系找「左边/右边」的显示器。

    Rectangle Pro 的 AdjacentScreens 做法：两块屏算邻接，看的是它们在某个
    方向上的投影是否重叠、以及沿该方向的距离；不是按下标顺序循环。三块屏
    摆成 L 形时，右下角的屏往右没有邻居，下标顺序却会绕回左上角那块——
    用户看到的就是「往右移却跑到了上面」。

    direction: -1 左 / 1 右（也接受 "left"/"right"）。
    ignore: 要跳过的索引集合（例如竖屏不参与左右邻接）。
    找不到邻居时返回 None，由调用方决定是否退化成下标循环。
    """
    if not monitors or not (0 <= cur_idx < len(monitors)):
        return None
    if direction in ("left", -1):
        direction = -1
    else:
        direction = 1
    cur = monitors[cur_idx]
    # 当前屏沿该方向的「出发边」
    cur_a = cur.left if direction < 0 else cur.left + cur.width
    cur_lo, cur_hi = cur.top, cur.top + cur.height
    skip = set(ignore or ())
    best_idx, best_gap = None, None
    for i, m in enumerate(monitors):
        if i == cur_idx or i in skip:
            continue
        m_lo, m_hi = m.top, m.top + m.height
        # 沿运动方向的投影必须重叠，否则是上下前后的邻居，不算左右相邻
        overlap = min(cur_hi, m_hi) - max(cur_lo, m_lo)
        if overlap <= 0:
            continue
        edge = m.left + m.width if direction < 0 else m.left
        gap = (cur_a - edge) if direction < 0 else (edge - cur_a)
        if gap < -1:
            continue  # 在反方向，不算
        if best_gap is None or gap < best_gap:
            best_idx, best_gap = i, gap
    return best_idx


def display_uuids(monitors):
    """取每块显示器的稳定 UUID（macOS；其它平台返回空 dict）。

    Rectangle Pro 用 CGDisplayCreateUUIDFromDisplayID 做布局绑定：UUID 比
    分辨率指纹稳得多——两台同为 1920x1080 的显示器插在不同的位置，指纹
    无法区分，UUID 可以；重启 / 重插后 UUID 不变，指纹却可能因排列变化而变。

    monitors 需带 device_path（CG display id 字符串）。取不到 UUID 的屏
    （非 macOS / API 缺失）直接不进字典，调用方按「没有 UUID」处理。
    """
    out = {}
    if sys.platform != "darwin" or not monitors:
        return out
    try:
        import ctypes
        import ctypes.util
        lib = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/"
            "ApplicationServices")
        cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/"
            "CoreFoundation")
        lib.CGDisplayCreateUUIDFromDisplayID.restype = ctypes.c_void_p
        lib.CGDisplayCreateUUIDFromDisplayID.argtypes = [ctypes.c_uint32]
        cf.CFUUIDCreateString.restype = ctypes.c_void_p
        cf.CFUUIDCreateString.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    except Exception:  # noqa: BLE001
        return out
    for m in monitors:
        try:
            did = int(str(getattr(m, "device_path", "")).strip())
        except (TypeError, ValueError):
            continue
        ref = lib.CGDisplayCreateUUIDFromDisplayID(did)
        if not ref:
            continue
        s = cf.CFUUIDCreateString(None, ctypes.c_void_p(ref))
        if not s:
            continue
        buf = ctypes.create_string_buffer(128)
        if cf.CFStringGetCString(ctypes.c_void_p(s), buf, 128, 0x08000100):
            out[m.device_path] = buf.value.decode("utf-8", "replace")
    return out


# 吸附方位 -> 该方位必须贴住的那条边。App 的最小/固定尺寸把窗口钳小时，
# 尺寸到不了目标格子，但「用户要贴的边」必须仍然贴住工作区边界。
RIGHT_ZONES = ("right", "right-third", "quad-tr", "quad-br")
BOTTOM_ZONES = ("bottom", "quad-bl", "quad-br")


def reanchor_edge(zone, got_x, got_y, got_w, got_h,
                  work_left, work_top, work_width, work_height):
    """按吸附方位把被钳制的窗口重新钉到用户要贴的那条边上。

    返回应写入的 (x, y)——尺寸保持钳制后的 (got_w, got_h) 不变，只挪位置：
      - left / left-third / quad-tl / top  ：本来就贴左/贴顶，位置不动；
      - right / right-third / quad-tr      ：右缘钉到工作区右边界；
      - bottom / quad-bl                   ：底缘钉到工作区下边界；
      - maximize                           ：左上角钉到工作区原点；
      - middle-third                       ：在屏宽上水平居中；
      - center                             ：在屏宽高上居中。
    例：右半屏 756 宽的格子装不下 UU远程 固定的 921 宽，改写 x = 右边界 - 921，
    让右缘贴住屏幕右边缘，而不是停在 756 处悬空。
    """
    ax, ay = got_x, got_y
    if zone in RIGHT_ZONES:
        # 窗口已经比工作区还宽时再钉右缘会把左缘推出屏外，改为贴左
        ax = (work_left + work_width - got_w if got_w < work_width
              else work_left)
    if zone in BOTTOM_ZONES:
        ay = (work_top + work_height - got_h if got_h < work_height
              else work_top)
    if zone == "maximize":
        ax, ay = work_left, work_top
    elif zone == "middle-third":
        ax = work_left + (work_width - got_w) // 2
    elif zone == "center":
        ax = work_left + (work_width - got_w) // 2
        ay = work_top + (work_height - got_h) // 2
    return int(ax), int(ay)
