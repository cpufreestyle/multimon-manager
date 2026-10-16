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

入口有两个：plan_quad_smart 是「恰好四个、四角全占」的严格包装；
plan_quad_layout 接受 1~4 个窗口与「用户在界面上点选的角」——2~3 个窗口
也能按宽高比分配（只占被选中的格，空格子不参与成本与比例搜索）。
"""
import itertools


# 每个格子至少要达到窗口原尺寸的这个比例（宽、高各自判断）。
# 低于此值说明这一格被切得太小，窗口放进去也施展不开——「合适」不只是
# 比例对上，还得有可用大小。
MIN_CELL_FRAC = 0.35


# Moom 4 的 snapshotMatchingSelector 兜底判据：窗口中心点到布局格中心点的
# 距离。主判据（格子与窗口的宽高比、面积、占比均衡）先比，距离作为一项小幅
# 成本参与——它不推翻「谁合适去哪个角」，只在一组摆法都说得通时选出「窗口
# 不用横穿屏幕」的那个。少了它，本来靠在左上的窗口会被甩到右列，用户看到的
# 就是「不准」「乱跑」。权重 0.25：典型摆法之间的契合成本差在 0.5 以上，
# 这项只够打破平局，不够颠倒结论。
PROX_WEIGHT = 0.25


# 格子装不下窗口 App 最小尺寸时的软惩罚权重。写入后窗口会被 App 顶大：要么
# 溢出压住邻居，要么被贴角逻辑挤到别处——两者都是「不准」。用软惩罚而不是
# 硬约束：旧实现把最小尺寸当硬约束时，真机实测 sy 被压到 0.2。
MIN_PEN_WEIGHT = 3.0


# 面积均衡项权重：被占格子的「窗口面积占比」与「格子面积占比」越接近越好。
# 0.65 是实测调出来的：两窗同排时（宽扁 A 1600x400 + 瘦高 B 600x1200，
# 1920x1080 屏），权重 1.0 会让每 5% 的比例差被均衡项的 0.10 抵消，算法
# 永远停在 sx=0.65——但纯契合成本与 fit 利用率都在 0.70 更优（B 等比
# 缩放后只需 540px 宽，672px 的格子浪费 20%）。降到 0.65 后 0.70 胜出，
# 3 窗 T 形与 4 窗场景的既有结论不动（0.60 以下才会扰动 3 窗选择）。
# 均衡项的原始职责——防小窗挤进极小格——由 _fit_cost 里的
# MIN_CELL_FRAC 硬约束与 area_pen 项继续兜底。
BALANCE_WEIGHT = 0.65


def _center(win):
    """窗口当前中心点（全局坐标）；没有几何信息时返回 None。

    容量预检传进来的 win_dict 是 None，单测桩只给宽高不给 x/y——这两种情况
    下 proximity 恒为 0，任何既有结果都不受影响。
    """
    if not isinstance(win, dict):
        return None
    try:
        x, y = float(win["x"]), float(win["y"])
        w, h = float(win.get("w") or 0), float(win.get("h") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (x + w / 2.0, y + h / 2.0)


def _cell_centers(cells):
    """四个格子的中心点（全局坐标），按实际矩形算。

    T 形下有格子拉通整宽，四格不再是整齐的 2x2：按 sizes 元组推会把宽格子的
    中心算到工作区外，proximity 的排序整个反过来。cells 即 quad_cells 的返回，
    空格子也算进去（不参与成本，只是字典里多几个键）。
    """
    return {c: (x + w / 2.0, y + h / 2.0)
            for c, (x, y, w, h) in enumerate(cells)}


def _prox_cost(pairs, cell_centers, diag):
    """窗口中心到所去格子中心的平均距离，按工作区对角线归一化。

    取平均而不是求和：四个窗口里只有一个没几何信息时，求和会把该项整体缩小
    一半，等于「有一个窗口在哪不重要」——那不是想要的语义。
    """
    if not pairs or diag <= 0:
        return 0.0
    total, n = 0.0, 0
    for w, c in pairs:
        ctr = _center(w)
        if ctr is None:
            continue
        gx, gy = cell_centers.get(c, ctr)
        total += ((gx - ctr[0]) ** 2 + (gy - ctr[1]) ** 2) ** 0.5
        n += 1
    return total / diag / max(1, n)


def _quad_prox(idxs, perm, wins, work_w, work_h, origin):
    """choose_quad_slots 版的 proximity：窗口中心到**象限**中心的平均距离。

    这里不算格子几何（该函数只判「放不放得下」），所以用工作区四等分的象限
    中心近似格子中心——够在「都放得下」的几种摆法之间挑不乱跑的那个。
    """
    ox, oy = origin
    diag = (work_w ** 2 + work_h ** 2) ** 0.5 or 1.0
    centers = {c: (ox + (0.25 if c in (0, 2) else 0.75) * work_w,
                   oy + (0.25 if c in (0, 1) else 0.75) * work_h)
               for c in range(4)}
    total, n = 0.0, 0
    for i, c in zip(idxs, perm):
        ctr = _center(wins[i][1])
        if ctr is None:
            continue
        gx, gy = centers[c]
        total += ((gx - ctr[0]) ** 2 + (gy - ctr[1]) ** 2) ** 0.5
        n += 1
    return total / diag / max(1, n)


def _ratio(win):
    """窗口的宽高比；尺寸异常时按 1.0 处理（不收窄任何一侧）。"""
    w = float(win.get("w") or 0)
    h = float(win.get("h") or 0)
    if w <= 0 or h <= 0:
        return 1.0
    return w / h


def quad_shape(occ):
    """按被占用的角推导四宫格形状。纯函数，windows 侧与界面预览共用。

    occ: 被占用的角（0~3 的可迭代）。返回 dict：
      occ      被占用的角（set）
      cnt_row  [上排占用数, 下排占用数]
      cnt_col  [左列占用数, 右列占用数]
      row_full 只有一排有窗口（n>=2）：该排撑满整个工作区高
      col_full 只有一列有窗口（n>=2）：该列撑满整个工作区宽
      wide     [上排只占一格, 下排只占一格]（两排都非空时）：那一格拉通
               整宽，排成 T 形；空格子落在被拉通的那排时宽度记 0

    一个窗口时不撑满（那是 maximize，不是四宫格），n>=2 才论形状。
    """
    occ = set(int(c) for c in occ)
    cnt_row = [sum(1 for c in occ if c in (0, 1)),
               sum(1 for c in occ if c in (2, 3))]
    cnt_col = [sum(1 for c in occ if c in (0, 2)),
               sum(1 for c in occ if c in (1, 3))]
    both_rows = cnt_row[0] > 0 and cnt_row[1] > 0
    return {
        "occ": occ,
        "cnt_row": cnt_row,
        "cnt_col": cnt_col,
        "row_full": len(occ) >= 2 and (cnt_row[0] == 0 or cnt_row[1] == 0),
        "col_full": len(occ) >= 2 and (cnt_col[0] == 0 or cnt_col[1] == 0),
        "wide": [both_rows and cnt_row[0] == 1,
                 both_rows and cnt_row[1] == 1],
    }


def quad_cells(shape, c1, r1, wl, wt, ww, wh):
    """按形状与生效切分算四个格子的绝对矩形。纯函数，windows 侧与界面共用。

    c1/r1 是「两排两列都非空」时的列切分 / 排切分毛值；shape 决定哪些格子
    拉通整宽（wide）、哪一排/列撑满（row_full/col_full）。这样「三窗 T 形」
    「两窗斜角上下堆」都不会在工作区里留死区——旧实现把空着的那角留成整块的
    四分之一屏，用户看到的就是「没排好」。

    空格子的矩形约定（既有测试口径）：该排被拉通整宽或该列为空 -> 宽 0；
    该排为空 -> 高 0；位置仍取该列/排的起点（空格不占地方，分割线才准）。
    """
    cnt_row, cnt_col = shape["cnt_row"], shape["cnt_col"]
    wide, occ = shape["wide"], shape["occ"]
    if shape["row_full"]:
        r1 = wh if cnt_row[0] else 0
    if shape["col_full"]:
        c1 = ww if cnt_col[0] else 0
    r2 = wh - r1
    c2 = ww - c1
    col_x = (wl, wl + c1)
    row_y = (wt, wt + r1)
    cells = []
    for corner in range(4):
        ri = 0 if corner in (0, 1) else 1
        ci = 0 if corner in (0, 2) else 1
        bx, by = col_x[ci], row_y[ri]
        bw = c1 if ci == 0 else c2
        bh = r1 if ri == 0 else r2
        if corner in occ:
            if wide[ri]:
                # 这一排只占一格：拉通整宽，排成 T 形。起点必须回到工作区
                # 左边——宽格落在右列时 col_x[1] 是列切分点，照它排这一格会
                # 整个伸出工作区右边界（实测右下窗口跑到 x+w=2880，macOS
                # 随即将整个窗口判给别的屏，就是「四个窗口不在一个屏幕上」）
                bx, bw = wl, ww
        else:
            if wide[ri] or cnt_col[ci] == 0:
                bw = 0
            if cnt_row[ri] == 0:
                bh = 0
        cells.append((bx, by, bw, bh))
    return cells


def plan_quad_layout(wins, work_w, work_h, corners=None,
                     min_ratio=0.15, max_ratio=0.85, origin=(0, 0)):
    """算出 1~4 个窗口的四宫格布局：谁去哪个角、用什么分割比例。

    corners: 与 wins 等长的角标列表（0~3，元素不重复）——用户在界面上给每个
        窗口点选的格；None 表示四角全占（此时 wins 必须恰好 4 个）。
    origin: 工作区左上角的全局坐标（默认 (0, 0)）。proximity 成本要算
        「窗口中心到格子中心的距离」，两边不在一个坐标系里这个距离就是
        错的——第二块屏 RV200 Pro 的工作区在 (0,-900)，不传 origin 时
        它的窗口会被算成「离所有格子一样远」，距离排序整个反过来。
    返回 (slots, split_x, split_y)：slots = [(corner, win_dict), ...] 按 corner
    升序，win_dict 就是传入的 dict（键不变，调用方取 hwnd 用）。

    与 plan_quad_smart 的关系：后者是「恰好四个、四角全占」的严格包装，
    n=4 时两者搜索结果完全一致。「选几个就排几个」落地后 2~3 个窗口走智能
    分配是常态，所以比例候选按「哪些排/列有窗口」分流：某一整排空着时纵向
    比例没有选择余地（有窗的那排撑满，sy 取 1.0 或 0.0），整列空时同理；
    成本只在被占用的角上累计——空格子不罚，面积均衡项也只在被占格子之间
    归一化。
    """
    n = len(wins)
    if n == 0 or n > 4:
        raise ValueError("窗口数量必须是 1~4 个，当前 %d 个" % n)
    if work_w <= 0 or work_h <= 0:
        raise ValueError("屏幕工作区尺寸无效: %sx%s" % (work_w, work_h))
    if corners is None:
        if n != 4:
            raise ValueError("未指定角标时需要恰好 4 个窗口，当前 %d 个" % n)
        corner_list = [0, 1, 2, 3]
    else:
        corner_list = list(corners)
        if len(corner_list) != n:
            raise ValueError("角标数量 %d 与窗口数 %d 不一致"
                             % (len(corner_list), n))
        if len(set(corner_list)) != n:
            raise ValueError("角标有重复: %s" % (corner_list,))
        if any(c < 0 or c > 3 for c in corner_list):
            raise ValueError("角标越界（应为 0~3）: %s" % (corner_list,))
        corner_list = sorted(corner_list)

    # 搜索区间与 snap_four_quad 对手动比例的钳制 [0.15, 0.85] 对齐。旧实现
    # 把 hi 钉在 0.5，智能模式反倒搜不到「窗口确实需要」的比例：真机 ChatGPT
    # 最小高 600，需要 600/982 = 61% 的排高，0.5 以内怎么搜都准不了。
    lo = min(max(min_ratio, 0.15), 0.85)
    hi = min(0.85, max_ratio)
    if hi < lo:
        hi = lo

    # 哪些排/列有窗口：整排空 -> sy 只能取 1.0（上排占）或 0.0（下排占）；
    # 整列空 -> sx 同理。两排两列都有时才在全区间扫描（同旧计划）。
    has_row = [any(c in (0, 1) for c in corner_list),
               any(c in (2, 3) for c in corner_list)]
    has_col = [any(c in (0, 2) for c in corner_list),
               any(c in (1, 3) for c in corner_list)]
    # 形状只跟「占了哪些角」有关，候选循环里不变。T 形（某排只占一格）时那一格
    # 拉通整宽，成本模型必须按实际矩形算——否则优化出来的几何与 windows 侧
    # 真正写出去的几何不是同一个，搜得再「优」也是纸上准。
    shape = quad_shape(corner_list)

    # hint 适配 n<4：按宽高比升序后前 half_n 个当「竖排候选」、其余当「横排
    # 候选」。n=4 时 half_n=2，与旧计划逐字一致。
    order = sorted(wins, key=_ratio)
    half_n = max(1, n // 2)
    r_tall = sum(_ratio(w) for w in order[:half_n]) / float(half_n)
    r_wide = (sum(_ratio(w) for w in order[half_n:])
              / float(max(1, n - half_n)))
    sx_hint = 0.4 if (r_tall + r_wide) <= 0 else r_tall / (r_tall + r_wide)
    h_all = sum(float(w.get("h") or 0) for w in wins)
    h_wide = sum(float(w.get("h") or 0) for w in order[half_n:])
    sy_hint = 0.4 if h_all <= 0 else max(0.2, 1.0 - h_wide / h_all)

    if has_col[0] and has_col[1]:
        sx_grid = _grid(sx_hint, lo, hi)
    else:
        sx_grid = [1.0] if has_col[0] else [0.0]
    if has_row[0] and has_row[1]:
        sy_grid = _grid(sy_hint, lo, hi)
    else:
        sy_grid = [1.0] if has_row[0] else [0.0]

    ox, oy = origin
    best = None
    for sx in sx_grid:
        for sy in sy_grid:
            for perm in itertools.permutations(corner_list):
                # 格子严格按比例铺满（与 windows 侧一致）：最小尺寸不抬格，
                # 只通过 _fit_cost 的惩罚影响「谁去哪个角 / 用什么比例」。
                pairs = list(zip(wins, perm))
                # 按形状算实际格子：T 形下拉通整宽的那格、堆叠的那格，尺寸
                # 与 windows 侧 _quad_cells 写出去的完全一致
                cells = quad_cells(shape, int(round(work_w * sx)),
                                   int(round(work_h * sy)), ox, oy,
                                   work_w, work_h)
                sizes = [(c[2], c[3]) for c in cells]
                # 只用被占的角算成本：空格子不罚
                cost = sum(_fit_cost(w, *sizes[c]) for w, c in pairs)
                # 面积均衡项：被占格子的「窗口面积占比」应与其「格子面积占比」
                # 接近。少了这一项，算法会把小窗口挤进极小格、剩下的全给大
                # 窗口——纯凑比例的话这样总成本更低，但四格大小失衡，
                # 不是用户要的「合适」。
                tot_win = sum(float(w.get("w") or 0) * float(w.get("h") or 0)
                              for w, _c in pairs) or 1.0
                tot_cell = sum(sizes[c][0] * sizes[c][1]
                               for _w, c in pairs) or 1.0
                for w, c in pairs:
                    a_w = float(w.get("w") or 0) * float(w.get("h") or 0)
                    share_win = a_w / tot_win
                    share_cell = (sizes[c][0] * sizes[c][1]) / tot_cell
                    cost += BALANCE_WEIGHT * abs(share_win - share_cell)
                # Moom 4 的 snapshotMatchingSelector 兜底判据：窗口中心点到
                # 所去格子中心的平均距离。契合成本打平的几种摆法之间，选
                # 「窗口不用横穿屏幕」的那个——权重见 PROX_WEIGHT，只打破
                # 平局，不颠倒「谁合适去哪个角」的结论。
                cost += PROX_WEIGHT * _prox_cost(
                    pairs, _cell_centers(cells),
                    (work_w ** 2 + work_h ** 2) ** 0.5)
                if best is None or cost < best[0]:
                    best = (cost, pairs, sx, sy)
    if best is None:
        # 理论上不会发生（候选网格非空、窗口数已校验）
        raise ValueError("没有可用的布局候选")
    _cost, pairs, split_x, split_y = best
    slots = [(c, w) for w, c in sorted(pairs, key=lambda p: p[1])]
    return slots, split_x, split_y


def plan_quad_smart(wins, work_w, work_h, min_ratio=0.15, max_ratio=0.85):
    """算出智能四宫格布局（严格四窗口、四角全占）。

    返回 ([TL, TR, BL, BR], split_x, split_y)；四个元素是传入 wins 里的
    原始 dict（键不变，调用方取 hwnd 用）。内部转调 plan_quad_layout，
    n=4 时的搜索结果与旧实现逐项一致。1~3 个窗口的智能分配直接用
    plan_quad_layout（corners 传用户在界面上点选的格）——「选几个就排几个」
    之后 2~3 窗是常态，旧实现在这里抛 ValueError 退化成手动顺序，正是
    「智能模式不准」的来源之一。
    """
    if len(wins) != 4:
        raise ValueError("智能分配需要恰好 4 个窗口，当前 %d 个" % len(wins))
    if work_w <= 0 or work_h <= 0:
        raise ValueError("屏幕工作区尺寸无效: %sx%s" % (work_w, work_h))

    slots, sx, sy = plan_quad_layout(wins, work_w, work_h, corners=None,
                                     min_ratio=min_ratio, max_ratio=max_ratio)
    return [w for _c, w in slots], sx, sy


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

    3. 最小尺寸不足：格子装不下窗口的 App 最小尺寸时按缺口比例罚
       （权重 MIN_PEN_WEIGHT，软惩罚）

    第 2 项让算法不会为了比例好看把格子切得过窄——窄格子装不下宽窗口，
    面积不足会被狠狠罚。
    """
    w = float(win.get("w") or 0)
    h = float(win.get("h") or 0)
    # App 最小尺寸软惩罚：格子装不下 min_w/min_h 时，写入后窗口会被 App 顶大,
    # 要么溢出压住邻居、要么被贴角逻辑挤到别处——两者都是「不准」。软惩罚
    # （权重 MIN_PEN_WEIGHT）而不是硬约束：硬约束会把算法逼去「把大窗口全塞进
    # 高排」的极端比例（真机实测 sy 被压到 0.2）。
    mw = float(win.get("min_w") or 0)
    mh = float(win.get("min_h") or 0)
    min_pen = 0.0
    if mw > 0 and cw < mw:
        min_pen += (mw - cw) / mw
    if mh > 0 and ch < mh:
        min_pen += (mh - ch) / mh
    if w <= 0 or h <= 0:
        # 尺寸未知：只按比例 1.0 估，不做面积惩罚
        return abs(cw / ch - 1.0) + MIN_PEN_WEIGHT * min_pen
    # 硬约束：格子至少要有窗口的 min_frac（宽和高各自都要达到）。
    # 达不到就直接判为「不合适」，成本抬到很高，让搜索主动避开这种切法。
    # 判据用窗口当前尺寸，不用 App 最小尺寸：格子铺满语义下窗口 fit 进格子、
    # 被 App 顶大是常态（由 reanchor 贴边处理），拿最小尺寸罚会把算法逼去
    # 「把大窗口全塞进高排」的极端比例（真机实测 sy 被压到 0.2）。
    if cw < w * MIN_CELL_FRAC or ch < h * MIN_CELL_FRAC:
        return 100.0 + (w * MIN_CELL_FRAC - cw) / w + (h * MIN_CELL_FRAC - ch) / h
    ratio_pen = abs(cw / ch - w / h) / (w / h)
    area_pen = max(0.0, (w * h - cw * ch) / (w * h))
    return ratio_pen + area_pen + MIN_PEN_WEIGHT * min_pen


def fit_frame(win, cw, ch, corner=0):
    """窗口等比缩放进格子后，在格子里占的相对框 (rx, ry, rw, rh)。

    这是 Moom 的 Relative Frame 语义：窗口放进格子不变形、也不被拉伸到
    格子大小，而是取「能塞进格子的最大等比尺寸」。位置贴方位：
    corner=0 左上 / 1 右上 / 2 左下 / 3 右下，窗口落在格子的对应角。
    返回的四个值都是 0~1（相对格子，不是相对屏幕）；调用方再叠加格子
    在工作区中的位置即可得到最终矩形。

    只缩小、不放大小窗口（100x100 放大 5.4 倍填满格子很离谱）。
    """
    w = float(win.get("w") or 0)
    h = float(win.get("h") or 0)
    if w <= 0 or h <= 0 or cw <= 0 or ch <= 0:
        return (0.0, 0.0, 1.0, 1.0)
    # 位置贴方位而不是居中：左上格贴左上、右上格贴右上、左下贴左下、
    # 右下贴右下。小窗口（不放大时）落在格子的对应角，符合方位直觉。
    scale = min(1.0, cw / w, ch / h)
    anchor_x = 1.0 if corner in (1, 3) else 0.0
    anchor_y = 1.0 if corner in (2, 3) else 0.0
    fw, fh = w * scale, h * scale
    return (round((cw - fw) * anchor_x / cw, 6),
            round((ch - fh) * anchor_y / ch, 6),
            round(fw / cw, 6),
            round(fh / ch, 6))


def grid_geometry(work_w, work_h, sx, sy, mins=None):
    """把一次横向/纵向切分算成四格的实际几何（含各窗口最小尺寸）。

    mins: [(min_w, min_h)] * 4，顺序 [左上, 右上, 左下, 右下]，缺省视为 0。

    每格尺寸取「按比例切出来的值」与「该列 / 该行内窗口的最小尺寸」的较大者。
    这一步是本 bug 的核心：格子小于窗口下限时，系统会为了满足下限把窗口撑出
    格子、越出屏幕，macOS 随即把整个窗口搬到别的屏——真机实测 RV200 Pro
    (1600x900) 上 ChatGPT 最小高 600 + Qoder CN 540 = 1140 > 900，下排必被
    挤出屏幕，于是「四个窗口不在一个屏幕上」。

    余量补给另一侧，网格仍铺满工作区；两侧之和超出工作区时，右列 / 下排改为
    贴住工作区右 / 下边（四角锚定），中间允许重叠，重叠量一并返回，调用方据此
    如实告知用户「这块屏放不下四格最小尺寸」。

    返回 (c1, c2, r1, r2, overlap_x, overlap_y)。
    """
    m = list(mins or [(0, 0)] * 4)
    while len(m) < 4:
        m.append((0, 0))
    # 每列 / 每排的最小尺寸取内部较长者：同一列的两个窗口共享列宽，窄的那个
    # 也要能放下较宽的那个的下限，否则总有一格被撑出格子。
    min_l = max(int(m[0][0] or 0), int(m[2][0] or 0))
    min_r = max(int(m[1][0] or 0), int(m[3][0] or 0))
    min_t = max(int(m[0][1] or 0), int(m[1][1] or 0))
    min_b = max(int(m[2][1] or 0), int(m[3][1] or 0))

    base1 = int(round(work_w * sx))
    c1 = max(base1, min_l)
    c2 = max(work_w - base1, min_r)
    # 一侧被最小尺寸抬高后，另一侧必须让出空间——不能两侧都按比例切完就把
    # 多出来的部分伸到工作区外（伸出去就是「窗口跑到别的屏」）。让的时候先
    # 让没被最小尺寸钉住的那一侧；两侧都被钉住（真装不下）才允许重叠。
    if c1 + c2 > work_w and c1 > min_l:
        c1 = max(min_l, work_w - c2)
    if c1 + c2 > work_w and c2 > min_r:
        c2 = max(min_r, work_w - c1)
    if c1 + c2 <= work_w:
        extra = work_w - (c1 + c2)
        c1 += extra // 2
        c2 += extra - extra // 2

    base_r1 = int(round(work_h * sy))
    r1 = max(base_r1, min_t)
    r2 = max(work_h - base_r1, min_b)
    if r1 + r2 > work_h and r1 > min_t:
        r1 = max(min_t, work_h - r2)
    if r1 + r2 > work_h and r2 > min_b:
        r2 = max(min_b, work_h - r1)
    if r1 + r2 <= work_h:
        extra = work_h - (r1 + r2)
        r1 += extra // 2
        r2 += extra - extra // 2
    return c1, c2, r1, r2, max(0, c1 + c2 - work_w), max(0, r1 + r2 - work_h)


def quad_requirements(corners):
    """四格摆满一块屏所需的最小工作区尺寸。

    corners 顺序 [左上, 右上, 左下, 右下]，元素是 (min_w, min_h)。左右两列
    各自要装下自己那列里较宽的那个窗口，上下两排各要装下自己那排里较高的那个。
    用来判断「这块屏放不放得下四宫格」，放不下才允许换屏 / 重叠。
    """
    m = list(corners or [])
    while len(m) < 4:
        m.append((0, 0))
    # 每列取该列两个窗口里更宽的、每排取该排两个窗口里更高的（与 grid_geometry
    # 的取 max 规则一致），否则算出来的需求比实际需要的还大，会误判「放不下」
    need_w = (max(int(m[0][0] or 0), int(m[2][0] or 0))
              + max(int(m[1][0] or 0), int(m[3][0] or 0)))
    need_h = (max(int(m[0][1] or 0), int(m[1][1] or 0))
              + max(int(m[2][1] or 0), int(m[3][1] or 0)))
    return need_w, need_h


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


def choose_quad_slots(wins, mins, work_w, work_h, gap=0, edge_gap=False,
                      corners=None, origin=(0, 0)):
    """挑「放得下的窗口子集」以及它们各自去哪个角。

    真机现场（Built-in 工作区 1512x896）：CC Switch 900x600 + CrossPaste
    700x600 + Perplexity 800x652 + Tuanjie 896x600 = 1824x1318，宽高两个
    方向都超屏。旧实现照排四个，结局是上下重叠 503px、左右重叠 757px，还有
    一个窗被 macOS 判到别的屏——用户看到的就是「不准」。这里改成：只排放得
    下的，放不下的原封不动并如实报告，格子几何不变。

    wins: [(hwnd, win_dict), ...]，顺序即请求的角落顺序 [左上, 右上, 左下, 右下]
    mins: {hwnd: (min_w, min_h)}，App 的最小窗口尺寸（探测不到按 0）
    corners: wins[i] 原本该去的角（0~3）。用户在界面上点掉了几格时 wins 不满
        四个，得靠它才知道每个窗口原本认哪个角；缺省按顺序 0,1,2,...
    origin: 工作区左上角的全局坐标（默认 (0, 0)），用途同 plan_quad_layout
        ——窗口中心与象限中心必须在同一坐标系里比，非主屏尤其如此。
    返回 (slots, unplaced)：
      slots    = [(corner, hwnd, win_dict), ...]，corner 0..3，按 corner 升序
      unplaced = [(hwnd, win_dict), ...]，放不下、保持原状的窗口

    优劣口径：排得多最优先；其后按 honor_requested 分两种——传入的窗口不足
    四个（用户在界面上明确只选了这几个角）时「不挪他选的角」优先，传入四个、
    要从中挑时「靠前的角」优先（左上→右上→左下→右下依次填，剩下的读起来才
    是完整的一块，斜对角放两个像出了 bug）；最后看格子利用得满与下标。
    """
    half = gap / 2.0
    # 每个被占的列/排要让出的间距：格子之间的缝由两侧各让一半凑成，勾选
    # 「屏幕边也留缝」时外侧再让一半。窗口被 App 的最小尺寸顶大时先吃掉这条
    # 缝，所以需求按 need + pad 算——缝吃完才刚刚好不压盖邻居、不越出工作区。
    pad = half + (half if edge_gap else 0)
    n = len(wins)
    want_corner = list(corners)[:n] if corners else list(range(n))
    while len(want_corner) < n:
        want_corner.append(len(want_corner))
    # 用户明确只选了这几个角（不足四个）时，他点的角优先——点掉别的格就是不
    # 要那些角。四个里挑几个时才优先「靠前的角」：剩下的窗口读起来是完整的
    # 一块，斜对角放两个像出了 bug（真机四窗放不下两个时就这么调过）。
    honor_requested = n < 4
    best = None
    for mask in range(1, 1 << n):
        idxs = [i for i in range(n) if mask & (1 << i)]
        # 这一组窗口的全部占角方式（不同窗口去不同角，injective）
        for perm in itertools.permutations(range(4), len(idxs)):
            need = [0, 0, 0, 0]      # 左列 / 右列 / 上排 / 下排 的需求
            occ = [False, False, False, False]
            for i, c in zip(idxs, perm):
                mw, mh = (int(v or 0) for v in mins.get(wins[i][0], (0, 0)))
                # need/occ 下标：0 左列、1 右列、2 上排、3 下排
                col = 0 if c in (0, 2) else 1
                row = 2 if c in (0, 1) else 3
                need[col] = max(need[col], mw)
                need[row] = max(need[row], mh)
                occ[col] = occ[row] = True
            req = [need[k] + pad if occ[k] else 0 for k in range(4)]
            if req[0] + req[1] > work_w or req[2] + req[3] > work_h:
                continue              # 这一组摆法在这块屏上必然重叠或越界
            displaced = sum(1 for i, c in zip(idxs, perm)
                            if c != want_corner[i])
            cols, rows = (req[0], req[1]), (req[2], req[3])
            unused = 0
            for i, c in zip(idxs, perm):
                mw, mh = (int(v or 0) for v in mins.get(wins[i][0], (0, 0)))
                cw = cols[0] if c in (0, 2) else cols[1]
                ch = rows[0] if c in (0, 1) else rows[1]
                unused += max(0, cw * ch - mw * mh)
            # 四个里挑：排得多 > 靠前的角 > 不挪用户指定的角 > 利用得满。
            # 明确只选几个：排得多 > 不挪用户选中的角 > 靠前的角 > 利用得满。
            # Moom 4 的 snapshotMatchingSelector 兜底判据：窗口中心到所去
            # 象限中心的平均距离（按工作区对角线归一化）。「都放得下」的几种
            # 摆法之间挑不乱跑的那个；权重见 PROX_WEIGHT。
            prox = PROX_WEIGHT * _quad_prox(idxs, perm, wins, work_w,
                                            work_h, origin)
            # 四个里挑：排得多 > 靠前的角 > 不挪用户指定的角 > 不乱跑 >
            # 利用得满。明确只选几个：排得多 > 不挪用户选中的角 > 靠前的角
            # > 不乱跑 > 利用得满。
            if honor_requested:
                key = (-len(idxs), displaced, sum(perm), prox, unused,
                       tuple(idxs))
            else:
                key = (-len(idxs), sum(perm), displaced, prox, unused,
                       tuple(idxs))
            if best is None or key < best[0]:
                best = (key, idxs, perm)
    if best is None:
        # 一个都放不下（窗口比整块屏还大）：全部保持原状，由调用方如实报告
        return [], list(wins)
    _key, idxs, perm = best
    slots = sorted(((c, wins[i][0], wins[i][1]) for i, c in zip(idxs, perm)),
                   key=lambda s: s[0])
    chosen = set(idxs)
    return slots, [w for i, w in enumerate(wins) if i not in chosen]


if __name__ == "__main__":
    demo = [{"hwnd": "chat", "w": 1500, "h": 900},
            {"hwnd": "term", "w": 400, "h": 900},
            {"hwnd": "code", "w": 1200, "h": 700},
            {"hwnd": "doc", "w": 700, "h": 1000}]
    lay, sx, sy = plan_quad_smart(demo, 1920, 1080)
    print("layout:", [w["hwnd"] for w in lay])
    print("splits: %.3f %.3f" % (sx, sy))
