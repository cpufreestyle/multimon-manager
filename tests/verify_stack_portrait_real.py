#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验证「竖排上中下」：小米竖屏上三窗口是否按上/中/下三等分落位。

指定 3 个窗口（避开 CodeBuddy / 远程桌面），记录原几何，验证后全部还原。
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


def main():
    mons = b.enum_monitors(force=True)
    mi = next((m for m in mons if "Mi" in m.device_name), None)
    if mi is None:
        print("FAIL: 未找到小米屏")
        return
    wl, wt, ww, wh = mi.work_rect
    third = wh // 3
    exp = {
        "上": (wl, wt, ww, third),
        "中": (wl, wt + third, ww, third),
        "下": (wl, wt + 2 * third, ww, wh - 2 * third),
    }
    print("小米屏 work=%s  third=%d" % (mi.work_rect, third))
    for k, v in exp.items():
        print("  期望 %s: %s" % (k, v))

    wins = [w for w in b.list_target_windows()
            if w.get("name") and not any(a in (w.get("owner") or "") for a in AVOID)]
    if len(wins) < 3:
        print("FAIL: 可用窗口不足 3 个（%d）" % len(wins))
        return
    hwnds = ['%s::%s' % (w["owner"], w["name"]) for w in wins[:3]]
    print("\n使用窗口:")
    for h in hwnds:
        ORIG_GEO[h] = read(h)
        print("  %s  原几何 %s" % (h, ORIG_GEO[h]))

    ok = wm.snap_three_stack(hwnds[0], hwnds[1], hwnds[2],
                             use_pinned=False, monitor="Mi Monitor")
    print("\nsnap_three_stack 返回:", ok)

    print("\n%-11s %-26s %-26s %-20s %s" %
          ("位置", "期望", "实测", "偏差", "判定"))
    for label, h in zip(("上", "中", "下"), hwnds):
        got = read(h)
        e = exp[label]
        d = tuple(a - t for a, t in zip(got, e))
        dev = max(abs(v) for v in d)
        print("%-11s %-26s %-26s %-20s %s" %
              ("%s|%s" % (label, h.split("::")[0][:8]), e, got, d,
               "OK" if dev <= 3 else "dev=%d" % dev))

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
