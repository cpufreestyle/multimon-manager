#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""虚拟桌面网格：把若干块屏当成一块大桌面，按 m×n 或加权比例网格批排窗口。

与四宫格（quad_plan）的分工：四宫格是**单屏内**的 2×2，本模块是**跨屏**的
任意 m×n。两者共用一个思路——格子按比例铺满、窗口按宽高比找格子——但跨屏
多出三个必须处理的问题：

1. **格子不能跨屏**。macOS 会把横跨两块屏的窗口判给其中一块（真机实测右下
   窗口 x+w=2880 越过 DELL 右界，整个窗口被搬到 Sidecar），这就是「四个窗口
   不在一个屏幕上」的根因。所以每格先按虚拟桌面切出来，再**归到交集最大的
   那块屏并裁进它的工作区**，裁完可能比网格格小——那是屏之间的物理间隙，
   如实裁掉，绝不把窗口摆在跨屏位置上。

2. **屏的大小/位置不同**。虚拟桌面是各屏工作区的包围盒，中间可能有空隙
   （屏摆放不齐、菜单栏高度不同）。与任何屏都没有交集的格子直接丢掉，
   不拿它去摆窗口。

3. **间距要按实际贴边算**。网格线落在屏正中间时是窗间缝；格子被裁到屏边界
   后，那条「网格线」已经变成屏幕外缘，再留缝就是「没吸附到边上」。所以
   每格的四个边按「裁完还贴不贴所在屏的工作区边界」重新判，而不是照网格
   行列号一刀切。

布局用两组比例描述：列宽比例 col_fracs 与行高比例 row_fracs，各自求和为 1。
等分网格就是全 1/n；「2/3+1/3」这类加权布局让某一侧吃掉三分之二，比
只支持等分更接近实际用法（左栏放终端想窄一点、右栏放浏览器想宽一点）。

本模块全是纯函数（不碰窗口、不碰显示器枚举），便于单测；windows 侧与
界面预览共用同一套几何，两边算出来必须一致。
"""
import itertools

# 每个格子至少要达到窗口原尺寸的这个比例（宽、高各自判断）。与
# quad_plan.MIN_CELL_FRAC 同值：从那里 import 会形成跨模块常量耦合，
# 而两边语义必须一致（改一个不改另一个，智能分配就会漂）。
MIN_CELL_FRAC = 0.35

# 格子装不下窗口 App 最小尺寸时的软惩罚权重。同 quad_plan.MIN_PEN_WEIGHT。
MIN_PEN_WEIGHT = 3.0

# 预设布局：(显示名, 列宽比例, 行高比例)。等分的直接全 1/n；
# 2/3+1/3 一组四个方向，覆盖「主区在左/右/上/下」的常见摆法。
LAYOUTS = [
    ("2×2 四等分", (0.5, 0.5), (0.5, 0.5)),
    ("3×2 六等分", (1.0 / 3, 1.0 / 3, 1.0 / 3), (0.5, 0.5)),
    ("2×3 六等分", (0.5, 0.5), (1.0 / 3, 1.0 / 3, 1.0 / 3)),
    ("2/3+1/3 左右", (2.0 / 3, 1.0 / 3), (0.5, 0.5)),
    ("1/3+2/3 左右", (1.0 / 3, 2.0 / 3), (0.5, 0.5)),
    ("2/3+1/3 上下", (0.5, 0.5), (2.0 / 3, 1.0 / 3)),
    ("1/3+2/3 上下", (0.5, 0.5), (1.0 / 3, 2.0 / 3)),
    ("3×3 九等分", (1.0 / 3, 1.0 / 3, 1.0 / 3),
     (1.0 / 3, 1.0 / 3, 1.0 / 3)),
    ("4×2 八等分", (0.25, 0.25, 0.25, 0.25), (0.5, 0.5)),
    ("2×4 八等分", (0.5, 0.5), (0.25, 0.25, 0.25, 0.25)),
]

# 等分排列的行列上限：再多格子每格只剩几十像素，摆出来没有意义
MAX_ROWS = 4
MAX_COLS = 4

# 智能分配穷举上限。n! 在 n=6 时 720 种、n=7 时 5040 种，都还快；
# 超过就退到按宽高比排序配对（确定性启发式），不引入匈牙利算法的复杂度。
_BRUTE_FORCE_MAX = 6

# 最小尺寸容差（px）：探测有 1~2px 抖动，零容差会把差几像素就能放下的
# 窗口全判成「装不下」。与 quad_plan._SLACK_TOL 一致。
_SLACK_TOL = 6


def equal_fracs(n):
    """n 等分的比例元组（各自 1/n）。"""
    n = max(1, int(n))
    return tuple(1.0 / n for _ in range(n))


def layout_by_label(label):
    """按显示名取预设的 (列宽比例, 行高比例)；找不到返回 None。"""
    for name, cols, rows in LAYOUTS:
        if name == label:
            return cols, rows
    return None


def layout_labels():
    """全部预设显示名（界面下拉用）。"""
    return [name for name, _c, _r in LAYOUTS]


def _norm_fracs(fracs, n):
    """把比例列表规整成 n 个、求和为 1 的元组；None 时按 n 等分。"""
    if not fracs:
        return equal_fracs(n)
    vals = [float(f) for f in fracs if float(f) > 0]
    if not vals:
        return equal_fracs(n)
    total = sum(vals)
    if total <= 0:
        return equal_fracs(n)
    return tuple(v / total for v in vals)


def virtual_rect(monitors):
    """选中屏工作区的包围盒 (x, y, w, h)；没有屏时返回 None。

    用**工作区**而不是屏幕全框：窗口摆不进菜单栏/Dock 占掉的那条，包围盒
    从工作区算，格子才不会落在摆不上的地方。屏之间有空隙时包围盒包含空隙，
    由 assign_cells 把落在空隙里的格子丢掉。
    """
    ms = [m for m in (monitors or []) if m is not None]
    if not ms:
        return None
    left = min(int(m.work_left) for m in ms)
    top = min(int(m.work_top) for m in ms)
    right = max(int(m.work_left) + int(m.work_width) for m in ms)
    bottom = max(int(m.work_top) + int(m.work_height) for m in ms)
    return (left, top, max(1, right - left), max(1, bottom - top))


def grid_cells(vx, vy, vw, vh, col_fracs, row_fracs):
    """按列宽/行高比例切出格子，返回 [(row, col, x, y, w, h), ...] 行优先。

    行优先 = 阅读顺序：第 k 个窗口去第 k 格，结果才可预期。最后一列/行用
    减法补齐（而不是各列独立 round），宽高为奇数时不出现 1px 缝隙——四宫格
    当年就是按「左右各 round 一半」切的，奇数宽下右列短 1px。
    """
    cols = _norm_fracs(col_fracs, 2)
    rows = _norm_fracs(row_fracs, 2)
    if vw <= 0 or vh <= 0:
        return []
    xs, acc = [], 0.0
    for f in cols[:-1]:
        acc += f
        xs.append(int(round(vw * acc)))
    xs.append(vw)
    ys, acc = [], 0.0
    for f in rows[:-1]:
        acc += f
        ys.append(int(round(vh * acc)))
    ys.append(vh)
    cells = []
    prev_y = 0
    for r in range(len(rows)):
        cy = ys[r]
        prev_x = 0
        for c in range(len(cols)):
            cx = xs[c]
            cells.append((r, c, vx + prev_x, vy + prev_y,
                          cx - prev_x, cy - prev_y))
            prev_x = cx
        prev_y = cy
    return cells


def assign_cells(cells, monitors):
    """每格归到交集最大的屏，并裁进该屏工作区。

    返回 [(row, col, mon_index, x, y, w, h), ...]（顺序不变，丢掉的格子直接
    缺席）。交集为 0 的格子（落在屏间空隙里）丢掉——那是物理上摆不了窗口的
    地方，硬摆会让 macOS 把窗口判给别的屏。

    交集取**面积**而不是中心点：格子被切得很碎时中心点可能落在屏外，
    面积判据才与「窗口落在哪块屏上」一致。
    """
    ms = [m for m in (monitors or []) if m is not None]
    if not ms:
        return []
    work = [(int(m.work_left), int(m.work_top),
             int(m.work_width), int(m.work_height)) for m in ms]
    out = []
    for r, c, x, y, w, h in cells:
        best_i, best_a = None, 0
        for i, (wl, wt, ww, wh) in enumerate(work):
            ox = max(0, min(x + w, wl + ww) - max(x, wl))
            oy = max(0, min(y + h, wt + wh) - max(y, wt))
            area = ox * oy
            if area > best_a:
                best_i, best_a = i, area
        if best_i is None:
            continue
        wl, wt, ww, wh = work[best_i]
        nx, ny = max(x, wl), max(y, wt)
        nr, nb = min(x + w, wl + ww), min(y + h, wt + wh)
        nw, nh = nr - nx, nb - ny
        if nw <= 0 or nh <= 0:
            continue
        out.append((r, c, best_i, nx, ny, nw, nh))
    return out


def cell_gap_rect(row, col, n_rows, n_cols, x, y, w, h, mon, gap, edge_gap):
    """按「这条边是窗间缝还是屏幕外缘」让出窗口间距，返回 (x, y, w, h)。

    判据不看网格行列号，看**裁完以后贴不贴所在屏的工作区边界**：
    格子被裁到屏左界后，原本的列分割线已经变成屏幕外缘，再留半条缝就是
    「没吸附到边上」。所以：

    - 内部边（col > 0 且左边仍在屏内）→ 让 half；
    - 屏幕外缘（贴住 work 边界）→ 只有勾选「屏幕边也留」才让 half。

    gap 的语义与 gap_rect 一致：**可见缝隙**等于 window_gap，每窗让一半。
    """
    if gap <= 0 or w <= gap or h <= gap:
        return (x, y, w, h)
    half = gap / 2.0
    wl, wt, ww, wh = (int(mon.work_left), int(mon.work_top),
                      int(mon.work_width), int(mon.work_height))
    # 容差 0.5px：整数取整后「贴边」可能差一丝，按丝判会把屏幕外缘误判成
    # 内部边，平白多留一条缝
    eps = 0.5

    def _side(inner, at_edge):
        return half if inner or (edge_gap and at_edge) else 0.0

    l = _side(col > 0 and x > wl + eps, x <= wl + eps)
    r = _side(col < n_cols - 1 and x + w < wl + ww - eps,
              x + w >= wl + ww - eps)
    t = _side(row > 0 and y > wt + eps, y <= wt + eps)
    b = _side(row < n_rows - 1 and y + h < wt + wh - eps,
              y + h >= wt + wh - eps)
    nx = int(round(x + l))
    ny = int(round(y + t))
    nw = int(round(w - l - r))
    nh = int(round(h - t - b))
    if nw < 1 or nh < 1:
        return (x, y, w, h)
    return (nx, ny, nw, nh)
def _ratio(win):
    """窗口宽高比；尺寸异常按 1.0（不收窄任何一侧）。"""
    w = float(win.get("w") or 0)
    h = float(win.get("h") or 0)
    if w <= 0 or h <= 0:
        return 1.0
    return w / h


def _ratio_of_size(size):
    """(w, h) 的宽高比；异常按 1.0。"""
    w, h = float(size[0] or 0), float(size[1] or 0)
    if w <= 0 or h <= 0:
        return 1.0
    return w / h


def _fit_cost(win, cw, ch):
    """窗口放进 cw x ch 的格子有多委屈，0 为完全合适。

    与 quad_plan._fit_cost 同口径（比例差 + 面积不足 + 最小尺寸软惩罚），
    只是判据从「四宫格的一个角」换成「虚拟网格的一格」。口径必须一致：
    同一扇窗在 2×2 里算合适、在 3×2 里算不合适，用户看到的就是乱跳。
    """
    w = float(win.get("w") or 0)
    h = float(win.get("h") or 0)
    mw = float(win.get("min_w") or 0)
    mh = float(win.get("min_h") or 0)
    min_pen = 0.0
    if mw > 0 and cw < mw:
        min_pen += (mw - cw) / mw
    if mh > 0 and ch < mh:
        min_pen += (mh - ch) / mh
    if w <= 0 or h <= 0 or cw <= 0 or ch <= 0:
        return abs(cw / ch - 1.0) + MIN_PEN_WEIGHT * min_pen
    # 格子至少要有窗口的 min_frac（宽和高各自判断）：达不到直接判不合适，
    # 成本抬到很高，让搜索主动避开「把小窗塞进极小格」的切法
    if cw < w * MIN_CELL_FRAC or ch < h * MIN_CELL_FRAC:
        return (100.0 + (w * MIN_CELL_FRAC - cw) / w
                + (h * MIN_CELL_FRAC - ch) / h)
    ratio_pen = abs(cw / ch - w / h) / (w / h)
    area_pen = max(0.0, (w * h - cw * ch) / (w * h))
    return ratio_pen + area_pen + MIN_PEN_WEIGHT * min_pen


def _sorted_matching(wins, sizes):
    """按宽高比排序配对（n 大时的确定性启发式）。

    返回 perm：perm[i] = 窗口 i 分到的格子下标。窗口与格子各按宽高比升序
    一一对应——最横的窗去最横的格。穷举在 n>6 时组合数爆炸，而加权网格的
    格子比例跨度通常远小于窗口跨度，排序配对已经够用。
    """
    n = len(wins)
    w_order = sorted(range(n), key=lambda i: _ratio(wins[i]))
    c_order = sorted(range(n), key=lambda i: _ratio_of_size(sizes[i]))
    perm = [0] * n
    for k, wi in enumerate(w_order):
        perm[wi] = c_order[k]
    return perm


def assign_windows(wins, sizes, smart=True):
    """把窗口分到格子，返回 perm：perm[i] = 窗口 i 所占格子的下标。

    smart=False 时按顺序一一对应（第 k 个窗口去第 k 格）——用户明确选了
    「哪个窗去哪个格」时不该被重排。
    smart=True 时按宽高比找最贴的格子：n<=6 穷举全部排列取总成本最小，
    n>6 退到排序配对。总成本最小而不是逐个取最优：逐个贪心会在中途把
    剩下的窗全挤去差格（四宫格当年实测贪心把 chat 甩去左上、term 甩去右上）。
    """
    n = len(wins)
    if n == 0:
        return []
    if not smart or n == 1:
        return list(range(n))
    sizes = list(sizes)
    if len(sizes) < n:
        sizes = sizes + [(0, 0)] * (n - len(sizes))
    if n <= _BRUTE_FORCE_MAX:
        best, best_perm = None, None
        for perm in itertools.permutations(range(n)):
            total = 0.0
            for i in range(n):
                cw, ch = sizes[perm[i]]
                total += _fit_cost(wins[i], cw, ch)
            if best is None or total < best - 1e-12:
                best, best_perm = total, perm
        return list(best_perm)
    return _sorted_matching(wins, sizes[:n])


def pick_windows(cands, n, skip_owners=(), own_pids=()):
    """按 z 序取前 n 个可操作窗口；不足 n 个就有几个取几个。

    跳过自身进程（不能把自己摆进格子）与 AX 够不着的 owner（wine 之类：
    CG 层看得见但 AppleScript 读写全失败，选进来整轮排列都卡在重试里）。
    """
    out, seen = [], set()
    for w in (cands or []):
        if not isinstance(w, dict):
            continue
        if w.get("pid") in own_pids:
            continue
        owner = (w.get("owner") or "").strip()
        if owner in skip_owners:
            continue
        h = w.get("hwnd")
        if not h or h in seen:
            continue
        seen.add(h)
        out.append(w)
        if len(out) >= n:
            break
    return out


def resolve_hwnds(wants, cands, finder=None):
    """把显式 hwnd 列表解析成窗口 dict 列表（找不到的跳过）。

    finder: hwnd -> win_dict 的回退查找（后端用来兜「枚举里没有但窗口还在」）。
    返回 (wins, missing)：missing 是已不可用的 hwnd，由调用方如实报告——
    静默用别的窗口顶上正是「智能/非智能都不准」的观感来源之一。
    """
    by_hwnd = {w.get("hwnd"): w for w in (cands or []) if isinstance(w, dict)}
    wins, missing, seen = [], [], set()
    for want in (wants or []):
        if not want or want in seen:
            continue
        w = by_hwnd.get(want)
        if w is None and finder is not None:
            try:
                w = finder(want)
            except Exception:  # noqa: BLE001  查找失败按「不可用」处理
                w = None
        if w is None:
            missing.append(want)
            continue
        seen.add(want)
        wins.append(w)
    return wins, missing


def _rows_of(assignments):
    return max([a[0] for a in assignments], default=0) + 1


def _cols_of(assignments):
    return max([a[1] for a in assignments], default=0) + 1


def fit_report(assignments, wins, mins, gap, edge_gap, monitors):
    """按最小尺寸逐格判装不装得下，返回 (slots, too_small)。

    slots:     [(assignment, hwnd, win_dict), ...] 装得下的，顺序 = 传入顺序
    too_small: [(hwnd, win_dict, rect), ...] 装不下的，调用方让它们保持原位
               并如实报告——硬排的结局是窗口被 App 顶出格子、压住邻居，
               用户看到的就是「不准」。

    assignment: (row, col, mon_index, x, y, w, h)，其中 x/y/w/h 是**让出
    间距后**的格子矩形（写窗口的目标）。
    mins: {hwnd: (min_w, min_h)}；没探过的按 (0, 0) 计（乐观，与
    quad_capacity 同口径）。容差与探测抖动一致：差几像素就能放下的窗口不该
    被判成装不下。
    """
    n_rows = _rows_of(assignments)
    n_cols = _cols_of(assignments)
    slots, too_small = [], []
    for i, (row, col, mi, x, y, w, h) in enumerate(assignments):
        if i >= len(wins):
            break
        win = wins[i]
        hwnd = win.get("hwnd")
        mon = monitors[mi]
        gx, gy, gw, gh = cell_gap_rect(row, col, n_rows, n_cols, x, y, w, h,
                                       mon, gap, edge_gap)
        mw, mh = (int(v or 0) for v in mins.get(hwnd, (0, 0)))
        if (mw > 0 and mw > gw + _SLACK_TOL) or (mh > 0 and mh > gh + _SLACK_TOL):
            too_small.append((hwnd, win, (gx, gy, gw, gh)))
        else:
            slots.append(((row, col, mi, gx, gy, gw, gh), hwnd, win))
    return slots, too_small


def grid_summary(assignments, wins, mins, gap, edge_gap, monitors):
    """给界面/日志用的一行摘要：每格谁、多大、装不装得下。

    返回 [(row, col, hwnd, (x, y, w, h), fits), ...]。
    """
    slots, too_small = fit_report(assignments, wins, mins, gap,
                                  edge_gap, monitors)
    bad = {h for h, _w, _r in too_small}
    out = []
    for (row, col, _mi, gx, gy, gw, gh), hwnd, _w in slots:
        out.append((row, col, hwnd, (gx, gy, gw, gh), hwnd not in bad))
    return out


if __name__ == "__main__":
    # 自检：三块屏（一块竖屏）的 3×2 网格，打印每格归到哪块屏
    class _M:
        def __init__(self, l, t, w, h, n):
            self.work_left, self.work_top = l, t
            self.work_width, self.work_height = w, h
            self.device_name = n

    ms = [_M(0, 0, 1512, 887, "Built-in"),
          _M(0, -900, 1600, 870, "RV200"),
          _M(-1080, -1100, 1080, 1920, "DELL-竖")]
    vr = virtual_rect(ms)
    print("virtual:", vr)
    for label, cols, rows in LAYOUTS[:3]:
        cells = grid_cells(vr[0], vr[1], vr[2], vr[3], cols, rows)
        asg = assign_cells(cells, ms)
        print(label, "->", [(a[0], a[1], a[2], a[5]) for a in asg])
