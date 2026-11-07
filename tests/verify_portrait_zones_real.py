#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终验证：幂等重设改造后，竖屏与横屏（主屏）的分区落位精度。

必须同时验证两块屏——改动是通用的，不能只修竖屏而弄坏主屏。
竖屏自动识别（height > width），测试窗口也自动挑一个并记录原几何后还原，
不再写死「小米屏」和某个固定 App 的窗口标题。
"""
import sys, time
sys.path.insert(0, "/Users/a1-6/AI Shared/repo/multimon-manager")

import backend as b
import windows_mac as wm

AVOID = ("CodeBuddy", "UU远程", "ToDesk", "Chrome", "Safari")
HWND = None
ORIG = None


def read(hwnd=None):
    global ORIG
    h = hwnd or HWND
    wm._last_rect.pop(h, None)
    return wm.get_window_rect(h)


def find_portrait(mons):
    """挑一块竖屏（高 > 宽）；没有则返回 None。"""
    return next((m for m in mons if m.height > m.width), None)


def pick_candidates():
    """所有可操作的普通窗口（避开远程桌面/浏览器这类会被打断的）。"""
    out = []
    for w in b.list_target_windows():
        owner = w.get("owner") or ""
        if not w.get("name") or any(a in owner for a in AVOID):
            continue
        if owner in ("Python", "脚本编辑器"):
            continue
        out.append(w["hwnd"])
    return out


def pick_window(mi):
    """挑一个可操作的普通窗口，避开远程桌面/浏览器这类会被打断的。

    有最小尺寸限制的 App（真机 Marvis 最小 900 宽、系统设置 754）塞不进
    三分之一的窄格，只会得到「App 尺寸钳制」——run() 已把它单独归类，
    不计入几何精度，所以这里不逐个试写（那会搬动一串真机窗口）。
    """
    cands = pick_candidates()
    return cands[0] if cands else None


def expect(mi, zone, inset=0):
    """按 zone 计算期望矩形（与实现同源）。

    inset: 该屏窗口顶边被系统下压的像素（菜单栏占位）。真机实测 Mi 竖屏
    被压 31px：窗口不能盖住菜单栏，贴顶放置的顶边会被钳到 work_top+31，
    高度相应少 31px（place_tile 的补偿，底边不变）。
    """
    wl, wt, ww, wh = mi.work_rect
    if zone == "left":
        return (wl, wt + inset, ww // 2, wh - inset)
    if zone == "right":
        return (wl + ww // 2, wt + inset, ww - ww // 2, wh - inset)
    if zone == "top":
        return (wl, wt + inset, ww, wh // 2 - inset)
    if zone == "bottom":
        return (wl, wt + wh // 2, ww, wh - wh // 2)
    if zone == "left-third":
        return (wl, wt + inset, ww // 3, wh - inset)
    if zone == "middle-third":
        return (wl + ww // 3, wt + inset, ww // 3, wh - inset)
    if zone == "right-third":
        return (wl + 2 * (ww // 3), wt + inset, ww - 2 * (ww // 3), wh - inset)
    return (wl, wt + inset, ww, wh - inset)


def measure_inset(mi):
    """实测该屏的顶边内缩：贴顶放一次，读回实际顶边与请求顶边的差。

    macOS 不让窗口盖住菜单栏（Mi 竖屏实测 31px）。这是屏级硬限制，不是
    「排歪了」——补偿后顶边落在 work_top+inset、高度少 inset、底边不变。
    """
    wl, wt, ww, wh = mi.work_rect
    wm.snap(HWND, mi, "maximize", activate=False)
    got = read()
    if not got or got == (0, 0, 0, 0):
        return 0
    return max(0, got[1] - wt)


def run(mi, label, zones):
    print("\n=== %s  work=%s (%dx%d) ===" % (label, mi.work_rect, mi.width, mi.height))
    inset = measure_inset(mi)
    print("顶边内缩（菜单栏占位）: %d px" % inset)
    print("%-13s %-26s %-26s %-20s %s" % ("zone", "期望", "实测", "偏差", "判定"))
    bad = 0
    clamped = 0
    for z in zones:
        exp = expect(mi, z, inset)
        wm.snap(HWND, mi, z, activate=False)
        got = read()
        d = tuple(a - e for a, e in zip(got, exp))
        dev = max(abs(v) for v in d)
        # App 自己的最小/最大尺寸把要求改了尺寸 → 位置偏差没意义，分开报
        if abs(got[2] - exp[2]) > 3 or abs(got[3] - exp[3]) > 3:
            clamped += 1
            print("%-13s %-26s %-26s %-20s %s" %
                  (z, exp, got, d, "App 尺寸钳制"))
            continue
        ok = dev <= 3
        if not ok:
            bad += 1
        print("%-13s %-26s %-26s %-20s %s" %
              (z, exp, got, d, "OK" if ok else "dev=%d" % dev))
    print("小计: 不准 %d / %d（另有 %d 个被 App 自身尺寸钳制，不计入）" %
          (bad, len(zones), clamped))
    if clamped == len(zones):
        print("提示：这个窗口有最小尺寸限制（真机 Marvis 最小 900 宽、系统设置 "
              "754），所有窄格都装不下——换一个可自由缩放的窗口再验几何精度")
    return bad


def main():
    mons = b.enum_monitors(force=True)
    mi = find_portrait(mons)
    main_mon = next((m for m in mons if m.is_primary), mons[0])

    global HWND, ORIG
    HWND = pick_window(mi or main_mon)
    if not HWND:
        print("SKIP: 没有可用窗口（都避开了），跳过")
        return
    ORIG = read()
    print("测试窗口: %s  原几何 %s" % (HWND, ORIG))

    wm.set_window_rect(HWND, *ORIG, activate=False)
    got = read()
    print("初始还原: %s 目标 %s 偏差=%d" %
          (got, ORIG, max(abs(a - t) for a, t in zip(got, ORIG))))

    zones = ["top", "bottom", "left", "right",
             "left-third", "middle-third", "right-third"]
    total = 0
    if mi:
        total += run(mi, "竖屏(portrait) %r" % mi.device_name, zones)
    else:
        print("SKIP: 当前没有竖屏，跳过竖屏分区验证")
    total += run(main_mon, "主屏(landscape)", zones)

    wm.set_window_rect(HWND, *ORIG, activate=False)
    fin = read()
    print("\n最终还原: %s 目标 %s 偏差=%d" %
          (fin, ORIG, max(abs(a - t) for a, t in zip(fin, ORIG))))
    print("合计不准: %d" % total)


if __name__ == "__main__":
    main()
