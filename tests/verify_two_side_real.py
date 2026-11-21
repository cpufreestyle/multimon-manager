#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验证「并排左右 / 竖屏上下堆叠」按最小尺寸分配。

两条分支各验一次：
  A. 竖屏 → 上下堆叠：先满足两扇窗的最小高，再扣掉顶边不可达条带（菜单栏），
     上半屏不能压住下半屏，下半屏要收在屏幕底。
  B. 横屏 → 左右并排：某一侧最小宽超过半屏时，该侧要被撑开，右缘贴住屏幕右边。

两个窗口都先记原几何、每个阶段结束立即还原。只在手动运行时执行
（文件名不是 test_*.py，`unittest discover` 不会加载）。

用法：
    /opt/homebrew/bin/python3.14 tests/verify_two_side_real.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend as b  # noqa: E402
import windows_mac as wm  # noqa: E402

# Terminal/iTerm 跑脚本时几何读写会互相打断（脚本自己就挂在那个窗口上），
# 真机实测它的 AX 读回时好时坏——把它当被试只会得到假 FAIL。
AVOID = ("多屏管理器", "Python", "CodeBuddy", "UU远程", "ToDesk", "Sidecar",
         "Terminal", "iTerm")


def read(hwnd):
    wm._last_rect.pop(hwnd, None)
    return wm.get_window_rect(hwnd)


def pick_screen(mons, portrait):
    pool = [m for m in mons
            if (m.height > m.width) == portrait]
    if not pool:
        return None
    return max(pool, key=lambda m: m.work_width * m.work_height)


def pick_windows(work):
    """两个「可写、非全屏、不同 App」的窗口。"""
    ww, wh = work[2], work[3]
    out, seen = [], set()
    for w in b.list_target_windows():
        owner = (w.get("owner") or "")
        hw = w.get("hwnd")
        if not hw or not w.get("name") or any(a in owner for a in AVOID):
            continue
        if owner in seen or w.get("w", 0) < 160 or w.get("h", 0) < 120:
            continue
        if w.get("w", 0) >= ww * 0.98 or w.get("h", 0) >= wh * 0.98:
            continue
        r = read(hw)
        if not r or r == (0, 0, 0, 0):
            continue
        if wm._ax_is_fullscreen(hw) is True:
            continue
        seen.add(owner)
        out.append(w)
        if len(out) >= 2:
            break
    return out


def overlap(a, c):
    ax, ay, aw, ah = a
    cx, cy, cw, ch = c
    return (min(ax + aw, cx + cw) - max(ax, cx) > 0
            and min(ay + ah, cy + ch) - max(ay, cy) > 0)


def check_portrait(mon):
    wl, wt, ww, wh = mon.work_rect
    wins = pick_windows(mon.work_rect)
    if len(wins) < 2:
        print("SKIP(A): 可用窗口不足 2 个")
        return None
    a = "%s::%s" % (wins[0]["owner"], wins[0]["name"])
    c = "%s::%s" % (wins[1]["owner"], wins[1]["name"])
    orig = {h: read(h) for h in (a, c)}
    print("A. 竖屏 %r work=%s" % (mon.device_name, mon.work_rect))
    for h in (a, c):
        print("   %-28s 原几何 %s" % (h.split("::")[0], orig[h]))
    try:
        wm.probe_min_sizes([a, c])
        mins = [(wm._QUAD_MIN_CACHE.get(h) or (0, 0)) for h in (a, c)]
        print("   最小尺寸 %s" % (mins,))
        wm.snap_two_side_by_side(left_hwnd=a, right_hwnd=c,
                                 monitor=mon.device_name)
        ra, rc = read(a), read(c)
        strip = max(0, int(ra[1]) - int(wt)) if ra else 0
        hs = wm._two_band_lengths(max(1, wh - strip), [mins[0][1], mins[1][1]])
        print("   条带=%d 期望高度=%s" % (strip, hs))
        problems = []
        if overlap(ra, rc):
            problems.append("上下重叠 %dpx" %
                            (ra[1] + ra[3] - rc[1]))
        if rc[1] + rc[3] != wt + wh:
            problems.append("下段底边 %d != %d" % (rc[1] + rc[3], wt + wh))
        for h, r in ((a, ra), (c, rc)):
            if not (wl <= r[0] and r[0] + r[2] <= wl + ww
                    and wt <= r[1] and r[1] + r[3] <= wt + wh):
                problems.append("%s 越界 %s" % (h.split("::")[0], r))
        print("   实测 %s / %s" % (ra, rc))
        print("   A 判定: %s" % ("OK" if not problems else "FAIL " + "；".join(problems)))
        return not problems
    finally:
        for h, r in orig.items():
            if r and r != (0, 0, 0, 0):
                wm.set_window_rect(h, r[0], r[1], r[2], r[3], activate=False)


def check_landscape(mon):
    wl, wt, ww, wh = mon.work_rect
    wins = pick_windows(mon.work_rect)
    if len(wins) < 2:
        print("SKIP(B): 可用窗口不足 2 个")
        return None
    a = "%s::%s" % (wins[0]["owner"], wins[0]["name"])
    c = "%s::%s" % (wins[1]["owner"], wins[1]["name"])
    orig = {h: read(h) for h in (a, c)}
    print("\nB. 横屏 %r work=%s" % (mon.device_name, mon.work_rect))
    for h in (a, c):
        print("   %-28s 原几何 %s" % (h.split("::")[0], orig[h]))
    try:
        wm.probe_min_sizes([a, c])
        mins = [(wm._QUAD_MIN_CACHE.get(h) or (0, 0)) for h in (a, c)]
        print("   最小尺寸 %s" % (mins,))
        wm.snap_two_side_by_side(left_hwnd=a, right_hwnd=c,
                                 monitor=mon.device_name)
        ra, rc = read(a), read(c)
        width = wm._two_band_lengths(ww, [mins[0][0], mins[1][0]])
        print("   期望宽度=%s" % (width,))
        problems = []
        if overlap(ra, rc):
            problems.append("左右重叠")
        if ra[3] < mins[0][1] - 8 or rc[3] < mins[1][1] - 8:
            problems.append("高度小于最小高度")
        if ra[2] + 4 < mins[0][0] or rc[2] + 4 < mins[1][0]:
            problems.append("宽度小于最小宽度")
        if rc[0] + rc[2] != wl + ww:
            problems.append("右缘 %d != %d" % (rc[0] + rc[2], wl + ww))
        print("   实测 %s / %s" % (ra, rc))
        print("   B 判定: %s" % ("OK" if not problems else "FAIL " + "；".join(problems)))
        return not problems
    finally:
        for h, r in orig.items():
            if r and r != (0, 0, 0, 0):
                wm.set_window_rect(h, r[0], r[1], r[2], r[3], activate=False)


def main():
    mons = b.enum_monitors(force=True)
    if not mons:
        print("SKIP: 没有可用显示器")
        return 0
    results = []
    portrait = pick_screen(mons, True)
    if portrait is None:
        print("SKIP(A): 没有竖屏")
    else:
        results.append(check_portrait(portrait))
    landscape = pick_screen(mons, False)
    if landscape is None:
        print("SKIP(B): 没有横屏")
    else:
        results.append(check_landscape(landscape))
    real = [r for r in results if r is not None]
    print("\n总判定: %s" % ("PASS" if real and all(real) else "FAIL/未验证"))
    return 0 if real and all(real) else 1


if __name__ == "__main__":
    sys.exit(main())
