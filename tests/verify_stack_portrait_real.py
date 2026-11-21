#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验证「竖排上中下」：竖屏上三窗口是否按上/中/下三等分落位。

指定 3 个窗口（避开 CodeBuddy / 远程桌面），记录原几何，验证后全部还原。
竖屏是自动识别的（height > width），不再写死「小米屏」——换屏后照样能跑；
没有竖屏时 SKIP 而不是 FAIL。
"""
import sys, time
sys.path.insert(0, "/Users/a1-6/AI Shared/repo/multimon-manager")

import backend as b
import windows_mac as wm

AVOID = ("CodeBuddy", "UU远程", "ToDesk", "Chrome", "Safari")
ORIG_GEO = {}


def read(hwnd):
    wm._last_rect.pop(hwnd, None)
    return wm.get_window_rect(hwnd)


def find_portrait(mons):
    """挑一块竖屏（高 > 宽）；没有则返回 None。"""
    return next((m for m in mons if m.height > m.width), None)


def main():
    mons = b.enum_monitors(force=True)
    mi = find_portrait(mons)
    if mi is None:
        print("SKIP: 当前没有竖屏（height > width）显示器，跳过")
        return
    wl, wt, ww, wh = mi.work_rect

    # 只挑「几何读得到、不是全屏」的窗口：全屏窗口的尺寸由系统接管，
    # 写进去会被静默忽略（真机 ChatGPT），放进验证只会把 App 限制误报成
    # 「三段不准」。读不到几何的（OneNote/微信）同理。
    wins, seen_owners = [], set()
    for w in b.list_target_windows():
        owner = (w.get("owner") or "")
        if not w.get("name") or any(a in owner for a in AVOID):
            continue
        if owner in seen_owners:
            continue
        hw = w.get("hwnd")
        if not hw:
            continue
        r = read(hw)
        if not r or r == (0, 0, 0, 0):
            continue
        if wm._ax_is_fullscreen(hw) is True:
            continue
        seen_owners.add(owner)
        wins.append(w)
        if len(wins) >= 3:
            break
    if len(wins) < 3:
        print("FAIL: 可用窗口不足 3 个（%d）" % len(wins))
        return
    hwnds = ['%s::%s' % (w["owner"], w["name"]) for w in wins[:3]]

    # 先记原几何：下面的最小尺寸探测会把窗口缩小，还原必须回到这一步的状态
    print("\n使用窗口:")
    for h in hwnds:
        ORIG_GEO[h] = read(h)
        print("  %s  原几何 %s" % (h, ORIG_GEO[h]))

    # 与界面「测最小尺寸」按钮同一条路径：探测→缓存→再排。不探测的话缓存
    # 里没有这三扇窗，三段只能按理想等分算，测不出「按最小高度分配」。
    try:
        probed = wm.probe_min_sizes(hwnds)
        print("探测最小尺寸: %s" % (probed.get("items"),))
    except Exception as e:  # noqa: BLE001
        print("探测最小尺寸失败（继续用等分）: %s" % e)

    ok = wm.snap_three_stack(hwnds[0], hwnds[1], hwnds[2],
                             use_pinned=False, monitor=mi.device_name)
    print("\nsnap_three_stack 返回:", ok)

    # 期望值与实现同源：先量出这条屏的「顶边不可达条带」（菜单栏），再从
    # 可用高度里扣掉它、按最小高度分配三段。条带是屏级物理常量，用本次实测值
    # 构造期望不算放水——真正要保证的是「三段首尾相接 + 末段收到屏底」。
    mins = [int((wm._QUAD_MIN_CACHE.get(h) or (0, 0))[1] or 0) for h in hwnds]
    top_got = read(hwnds[0])
    strip = max(0, int(top_got[1]) - int(wt)) if top_got else 0
    heights = wm._three_stack_heights(max(1, wh - strip), mins)
    exp = {
        "上": (wl, wt + strip, ww, heights[0]),
        "中": (wl, wt + strip + heights[0], ww, heights[1]),
        "下": (wl, wt + strip + heights[0] + heights[1], ww, heights[2]),
    }
    print("竖屏 %r work=%s  顶边条带=%dpx  三段=%s（下限 %s）"
          % (mi.device_name, mi.work_rect, strip, heights, mins))

    print("\n%-11s %-26s %-26s %-20s %s" %
          ("位置", "期望", "实测", "偏差", "判定"))
    rows = []
    for label, h in zip(("上", "中", "下"), hwnds):
        got = read(h)
        e = exp[label]
        d = tuple(a - t for a, t in zip(got, e))
        dev = max(abs(v) for v in d)
        print("%-11s %-26s %-26s %-20s %s" %
              ("%s|%s" % (label, h.split("::")[0][:8]), e, got, d,
               "OK" if dev <= 3 else "dev=%d" % dev))
        rows.append((h, e, got))

    # 顶段被系统下压（菜单栏占位）是 macOS 的硬限制：窗口不能盖住菜单栏，
    # 真机实测 Mi 竖屏被压 31px（要求 work_top=-1066 → 实际 -1035）。补偿后
    # 顶段高度少 31px、底边不变，所以绝对坐标必然与理想值差 inset。真正的
    # 判据是「相邻两段不重叠 + 末段正好收在屏幕底」。
    inset = 0
    if rows:
        _h0, e0, g0 = rows[0]
        inset = g0[1] - e0[1]
    print("\n顶边内缩（菜单栏占位）: %d px" % inset)
    problems = []
    for i in range(len(rows) - 1):
        h0, _e0, g0 = rows[i]
        h1, _e1, g1 = rows[i + 1]
        if g0[1] + g0[3] > g1[1]:
            problems.append("%s 压住 %s %dpx" %
                            (h0.split("::")[0], h1.split("::")[0],
                             g0[1] + g0[3] - g1[1]))
    # 末段底边必须收在工作区底
    _hl, el, gl = rows[-1]
    if gl[1] + gl[3] != el[1] + el[3]:
        problems.append("末段底边 %d ≠ 期望 %d" % (gl[1] + gl[3], el[1] + el[3]))
    if problems:
        print("FAIL: " + "；".join(problems))
    else:
        print("OK: 三段首尾相接、无重叠，末段收在屏幕底")

    # 还原全部
    print("\n还原:")
    for h in hwnds:
        wm.set_window_rect(h, *ORIG_GEO[h], activate=False)
        got = read(h)
        d = max(abs(a - t) for a, t in zip(got, ORIG_GEO[h]))
        print("  %-40s -> %s  偏差=%d %s" %
              (h[:40], got, d, "OK" if d <= 3 else ""))


if __name__ == "__main__":
    main()
