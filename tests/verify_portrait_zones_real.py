#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终验证：幂等重设改造后，竖屏（小米）与横屏（主屏）的分区落位精度。

必须同时验证两块屏——改动是通用的，不能只修竖屏而弄坏主屏。
"""
import sys, time
sys.path.insert(0, "/Users/a1-6/AI Shared/repo/multimon-manager")

import backend as b
import windows_mac as wm

APP, NAME = "脚本编辑器", "未命名"
HWND = '%s::%s' % (APP, NAME)
ORIG = (-929, -921, 700, 734)


def read():
    wm._last_rect.pop(HWND, None)
    return wm.get_window_rect(HWND)


def expect(mi, zone):
    wl, wt, ww, wh = mi.work_rect
    if zone == "left":
        return (wl, wt, ww // 2, wh)
    if zone == "right":
        return (wl + ww // 2, wt, ww - ww // 2, wh)
    if zone == "top":
        return (wl, wt, ww, wh // 2)
    if zone == "bottom":
        return (wl, wt + wh // 2, ww, wh - wh // 2)
    if zone == "left-third":
        return (wl, wt, ww // 3, wh)
    if zone == "middle-third":
        return (wl + ww // 3, wt, ww // 3, wh)
    if zone == "right-third":
        return (wl + 2 * (ww // 3), wt, ww - 2 * (ww // 3), wh)
    return (wl, wt, ww, wh)


def run(mon, label, zones):
    print("\n=== %s  work=%s (%dx%d) ===" % (label, mon.work_rect, mon.width, mon.height))
    print("%-13s %-26s %-26s %-20s %s" % ("zone", "期望", "实测", "偏差", "判定"))
    bad = 0
    for z in zones:
        exp = expect(mon, z)
        wm.snap(HWND, mon, z, activate=False)
        got = read()
        d = tuple(a - e for a, e in zip(got, exp))
        dev = max(abs(v) for v in d)
        ok = dev <= 3
        if not ok:
            bad += 1
        print("%-13s %-26s %-26s %-20s %s" %
              (z, exp, got, d, "OK" if ok else "dev=%d" % dev))
    print("小计: 不准 %d / %d" % (bad, len(zones)))
    return bad


def main():
    mons = b.enum_monitors(force=True)
    mi = next((m for m in mons if "Mi" in m.device_name), None)
    main_mon = next((m for m in mons if m.is_primary), mons[0])

    wm.set_window_rect(HWND, *ORIG, activate=False)
    got = read()
    print("初始还原: %s 目标 %s 偏差=%d" %
          (got, ORIG, max(abs(a - t) for a, t in zip(got, ORIG))))

    zones = ["top", "bottom", "left", "right",
             "left-third", "middle-third", "right-third"]
    total = 0
    if mi:
        total += run(mi, "小米竖屏(portrait)", zones)
    total += run(main_mon, "主屏(landscape)", zones)

    wm.set_window_rect(HWND, *ORIG, activate=False)
    fin = read()
    print("\n最终还原: %s 目标 %s 偏差=%d" %
          (fin, ORIG, max(abs(a - t) for a, t in zip(fin, ORIG))))
    print("合计不准: %d" % total)


if __name__ == "__main__":
    main()
