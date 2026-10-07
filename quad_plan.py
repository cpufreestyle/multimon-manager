#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四宫格智能分配：给定四个窗口的几何，算出谁去哪个角、用什么比例。

判据只用窗口自身的「宽高比 w/h」，不猜窗口内容——协议是
**把窗口放进宽高比最接近的格子**。反过来放会被 App 的最小尺寸钳制：
真机 ChatGPT 请求高 448、实测 600，就是被塞进了一个宽扁格子。

格子由一次横向切分(split_x)和一次纵向切分(split_y)得到：
    C1 + C2 = 工作区宽        R1 + R2 = 工作区高
    TL = (C1, R1)    TR = (C2, R1)
    BL = (C1, R2)    BR = (C2, R2)

split_x 与摆放是耦合的：比例决定四个格子的宽高比，摆放好不好才谈得上。
所以这里**同时搜两者**——在 split_x/split_y 的候选网格上，对每个候选穷举
4! = 24 种摆放，取「格子比例 vs 窗口比例」相对偏差总和最小的组合。

为什么不分开算：只按「左列装竖窗、右列装横窗」定比例时，C1 会被压得过窄，
四格比例跨度远超窗口跨度，再怎么配对都错位。

比例仍在 min_ratio~max_ratio 内钳制，避免某一边只剩几个像素。
"""
import itertools


# 每个格子至少要达到窗口原尺寸的这个比例（宽、高各自判断）。
# 低于此值说明这一格被切得太小，窗口放进去也施展不开——「合适」不只是
# 比例对上，还得有可用大小。
MIN_CELL_FRAC = 0.35


def _ratio(win):
    """窗口的宽高比；尺寸异常时按 1.0 处理（不收窄任何一侧）。"""
    w = float(win.get("w") or 0)
    h = float(win.get("h") or 0)
    if w <= 0 or h <= 0:
        return 1.0
    return w / h


def plan_quad_smart(wins, work_w, work_h, min_ratio=0.15, max_ratio=0.85):
    """算出智能四宫格布局。

    返回 ([TL, TR, BL, BR], split_x, split_y)；四个元素是传入 wins 里的
    原始 dict（键不变，调用方取 hwnd 用）。
    """
    if len(wins) != 4:
        raise ValueError("智能分配需要恰好 4 个窗口，当前 %d 个" % len(wins))
    if work_w <= 0 or work_h <= 0:
        raise ValueError("屏幕工作区尺寸无效: %sx%s" % (work_w, work_h))

    lo = min(max(min_ratio, 0.15), 0.85)
    hi = min(0.5, max_ratio)
    if hi < lo:
        hi = lo
    # 在整个合法区间上扫比例，不只在一个粗值附近探：粗值由「左列装竖窗」
    # 推出，天然把 C1 压得过窄（实测 0.25），四格比例跨度远超窗口跨度，
    # 再怎么配对都错位。铺开到 lo~hi 才能找到真正贴合窗口的那一组。

    order = sorted(wins, key=_ratio)
    r_tall = sum(_ratio(w) for w in order[:2]) / 2.0
    r_wide = sum(_ratio(w) for w in order[2:]) / 2.0
    sx_hint = 0.4 if (r_tall + r_wide) <= 0 else r_tall / (r_tall + r_wide)
    h_all = sum(float(w.get("h") or 0) for w in wins)
    h_wide = sum(float(w.get("h") or 0) for w in order[2:])
    sy_hint = 0.4 if h_all <= 0 else max(0.2, 1.0 - h_wide / h_all)

    best = None
    for sx in _grid(sx_hint, lo, hi):
        for sy in _grid(sy_hint, lo, hi):
            c1 = round(work_w * sx)
            c2 = max(1, work_w - c1)
            r1 = round(work_h * sy)
            r2 = max(1, work_h - r1)
            cells = (c1 / r1, c2 / r1, c1 / r2, c2 / r2)
            sizes = ((c1, r1), (c2, r1), (c1, r2), (c2, r2))
            for perm in itertools.permutations(wins):
                cost = sum(_fit_cost(w, cw, ch)
                           for w, (cw, ch) in zip(perm, sizes))
                # 面积均衡项：四格的「窗口面积占比」应与其「格子面积占比」
                # 接近。少了这一项，算法会把小窗口挤进极小格、剩下的全给大
                # 窗口——纯凑比例的话这样总成本更低，但四格大小失衡，
                # 不是用户要的「合适」。
                tot_win = sum(float(w.get("w") or 0) * float(w.get("h") or 0)
                              for w in perm) or 1.0
                tot_cell = sum(cw * ch for cw, ch in sizes) or 1.0
                for w, (cw, ch) in zip(perm, sizes):
                    a_w = float(w.get("w") or 0) * float(w.get("h") or 0)
                    share_win = a_w / tot_win
                    share_cell = (cw * ch) / tot_cell
                    cost += abs(share_win - share_cell)
                if best is None or cost < best[0]:
                    best = (cost, list(perm), sx, sy)
    if best is None:
        # 理论上不会发生（候选网格非空、窗口数已校验为 4）
        raise ValueError("没有可用的布局候选")
    _cost, layout, split_x, split_y = best
    return layout, split_x, split_y


def _fit_cost(win, cw, ch):
    """窗口放进 cw x ch 的格子有多委屈，0 为完全合适。

    必须先满足「格子装得下窗口的最小可用尺寸」：否则算法会发现把小窗口
    塞进极小格、剩下的全给大窗口，总成本反而更低（实测 term 360000 面积
    塞进 288x648 几乎全额拟合，成本最低解把左列压到 288px 宽）。
    那不是用户要的「合适」——四格都该有可用大小。

    纯比例差有盲区：窄窗口塞进窄格子，比例上看很配，但格子可用面积被浪费、
    窗口自己也被最小尺寸顶住动不了。所以成本取两项之和：

    1. 比例差 |格子比 - 窗口比| / 窗口比
    2. 面积不足：格子装不下窗口时按缺口比例罚；格子比窗口大不罚
       （窗口可以有余量，但不该被挤扁）

    第 2 项让算法不会为了比例好看把格子切得过窄——窄格子装不下宽窗口，
    面积不足会被狠狠罚。
    """
    w = float(win.get("w") or 0)
    h = float(win.get("h") or 0)
    if w <= 0 or h <= 0:
        # 尺寸未知：只按比例 1.0 估，不做面积惩罚
        return abs(cw / ch - 1.0)
    # 硬约束：格子至少要有窗口的 min_frac（宽和高各自都要达到）。
    # 达不到就直接判为「不合适」，成本抬到很高，让搜索主动避开这种切法。
    if cw < w * MIN_CELL_FRAC or ch < h * MIN_CELL_FRAC:
        return 100.0 + (w * MIN_CELL_FRAC - cw) / w + (h * MIN_CELL_FRAC - ch) / h
    ratio_pen = abs(cw / ch - w / h) / (w / h)
    area_pen = max(0.0, (w * h - cw * ch) / (w * h))
    return ratio_pen + area_pen


def _grid(center, lo, hi, step=0.05):
    """比例候选网格：[lo, hi] 全区间按 step 铺开，并额外带上 center 本身。

    必须让 center 参与候选（它是按窗口比例推出的粗值），但不能只在它附近
    探——那样跳不出「左列过窄」的局部坑，见 plan_quad_smart 的注释。
    """
    vals = []
    n = 1
    v = lo
    while v <= hi + 1e-9:
        vals.append(min(v, hi))
        v = lo + step * n
        n += 1
    vals.append(min(max(center, lo), hi))
    return sorted(set(round(x, 6) for x in vals))


if __name__ == "__main__":
    demo = [{"hwnd": "chat", "w": 1500, "h": 900},
            {"hwnd": "term", "w": 400, "h": 900},
            {"hwnd": "code", "w": 1200, "h": 700},
            {"hwnd": "doc", "w": 700, "h": 1000}]
    lay, sx, sy = plan_quad_smart(demo, 1920, 1080)
    print("layout:", [w["hwnd"] for w in lay])
    print("splits: %.3f %.3f" % (sx, sy))
