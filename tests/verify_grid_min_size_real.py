#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验证「网格按最小尺寸撑宽列」：两个窗口一左一右，装不下的那列必须被撑开。

背景：网格批排原来只按比例切格，某列装不下窗口的 App 最小宽度时会把那个窗口
剔除、留一个空格子。现在改成先把那一列撑到下限、其余列按比例吃剩余
（`grid_plan.balanced_grid_cells`），而且只在**确实多排到一个窗口**时采用。

这个脚本回答两件事：

1. 真实 App 的最小宽度到底会不会超过比例份额（`min_w > 屏宽/2` 才算触发）；
2. 排完之后两个窗口是否都在屏内、互不重叠、且宽度不小于各自的最小宽度。

它会移动真实窗口：运行前先把原几何记下来，结束时**全部还原**。
只在手动运行时执行（文件名不是 test_*.py，`unittest discover` 不会加载）。

用法：
    /opt/homebrew/bin/python3.14 tests/verify_grid_min_size_real.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend as b  # noqa: E402
import grid_plan  # noqa: E402
import windows_mac as wm  # noqa: E402

# 这些 App 的窗口要么是本程序自己，要么历史上对 AX 写入不友好（读回会超时），
# 拉进来只会让验证结论含混。
AVOID = ("多屏管理器", "Python", "CodeBuddy", "UU远程", "ToDesk", "Sidecar")


def read(hwnd):
    wm._last_rect.pop(hwnd, None)
    return wm.get_window_rect(hwnd)


def pick_landscape(mons):
    """挑面积最大的横屏；竖屏不参与（横屏应用塞进竖屏必挤）。"""
    land = [m for m in mons if m.height <= m.width]
    if not land:
        return None
    return max(land, key=lambda m: m.work_width * m.work_height)


def inside(rect, work):
    x, y, w, h = rect
    wx, wy, ww, wh = work
    return (x >= wx and y >= wy and x + w <= wx + ww and y + h <= wy + wh)


def overlap(a, c):
    ax, ay, aw, ah = a
    cx, cy, cw, ch = c
    ow = min(ax + aw, cx + cw) - max(ax, cx)
    oh = min(ay + ah, cy + ch) - max(ay, cy)
    return ow > 0 and oh > 0


def main():
    mons = b.enum_monitors(force=True)
    mon = pick_landscape(mons)
    if mon is None:
        print("SKIP: 当前没有横屏显示器（height <= width），跳过")
        return 0
    idx = mons.index(mon)
    work = tuple(int(v) for v in mon.work_rect)
    wl, wt, ww, wh = work
    print("目标屏 %r work=%s（%dx%d）" % (mon.device_name, work, ww, wh))

    # 挑两个「可写、非满屏、不同 App」的窗口：
    #   - 满屏/接近满屏的多半是 zoomed 或全屏，探测缩不动，会得到
    #     「最小尺寸 = 当前尺寸」的假结论；
    #   - 读不到几何的 App（真机 OneNote / 微信，AX 不开放）连写都写不动，
    #     拉进来只会把「App 限制」误报成「排列不准」。
    wins, seen_owners = [], set()
    for w in b.list_target_windows():
        owner = (w.get("owner") or "")
        name = (w.get("name") or "")
        hw = w.get("hwnd")
        if not name or not hw or any(a in owner for a in AVOID):
            continue
        if w.get("w", 0) < 160 or w.get("h", 0) < 120:
            continue
        if owner in seen_owners:
            continue
        if w.get("w", 0) >= ww * 0.98 or w.get("h", 0) >= wh * 0.98:
            continue
        r = read(hw)
        if not r or r == (0, 0, 0, 0):
            continue
        seen_owners.add(owner)
        wins.append(w)
        if len(wins) >= 2:
            break
    if len(wins) < 2:
        print("SKIP: 可用窗口不足 2 个（%d）" % len(wins))
        return 0

    hwnds = ["%s::%s" % (w["owner"], w["name"]) for w in wins]
    orig = {}
    print("\n使用窗口:")
    for h in hwnds:
        orig[h] = read(h)
        print("  %-24s 原几何 %s" % (h, orig[h]))

    cached = {h: wm._QUAD_MIN_CACHE.get(h) for h in hwnds}
    print("\n缓存的最小尺寸（第一次跑可能为空，脚本会现场探测并缓存）: %s" % cached)

    done = 0
    try:
        # 必须显式传 hwnds：不传的话 fill_grid 会自己按 z 序再挑一批窗口，
        # 验证的就不是下面打印的这两个（第一次跑就踩了这个坑：表格里是
        # Marvis/MichaelClip，实际排的是另外两个）。
        done = wm.fill_grid(hwnds=hwnds, cols=2, rows=1,
                            monitor_indices=[idx])
        print("\nfill_grid 返回: %r" % done)
        for n in (getattr(wm, "_last_grid_notes", None) or []):
            print("  说明: %s" % n)

        got = {h: read(h) for h in hwnds}
        mins = {h: wm._QUAD_MIN_CACHE.get(h, (0, 0)) for h in hwnds}
        print("\n%-24s %-22s %-14s %-12s %s" %
              ("窗口", "实测", "最小宽x高", "在屏内", "宽度达标"))
        ok_all = True
        for h in hwnds:
            r = got[h]
            mw, mh = mins[h]
            in_ok = inside(r, work)
            wide_ok = (not mw) or r[2] + 4 >= mw
            ok_all = ok_all and in_ok and wide_ok and r[2] > 0
            print("%-24s %-22s %-14s %-12s %s" %
                  (h.split("::")[0], r, "%dx%d" % (mw, mh),
                   "是" if in_ok else "否", "是" if wide_ok else "否"))

        # 比例份额：撑宽只在「某窗最小宽 > 半屏」时才触发，先告诉用户这次
        # 到底有没有走到新路径，避免把「没触发」误读成「没生效」。
        half = ww // 2
        bind = [h for h in hwnds if (mins[h][0] or 0) > half + 1]
        if bind:
            print("\n触发撑宽: %s 的最小宽超过半屏 %dpx"
                  % ("、".join(x.split("::")[0] for x in bind), half))
        else:
            print("\n未触发撑宽：这些窗口的最小宽都 <= 半屏 %dpx，"
                  "本次只验到基础几何（换一个最小宽很大的 App 再试）" % half)

        if len(hwnds) >= 2:
            a, c = got[hwnds[0]], got[hwnds[1]]
            if overlap(a, c):
                ok_all = False
                print("重叠: %s 与 %s 互相压盖" % (hwnds[0], hwnds[1]))
            else:
                gap = max(c[0] - (a[0] + a[2]), a[0] - (c[0] + c[2]))
                print("两窗横向间距: %dpx（>0 即不重叠）" % gap)

        print("\n结论: %s" % ("PASS" if ok_all and done == 2 else "FAIL"))
        return 0 if (ok_all and done == 2) else 1
    finally:
        # 无论成败都还原：这个脚本排在用户真实窗口上，不留下残局。
        for h, r in orig.items():
            if r and r != (0, 0, 0, 0):
                try:
                    wm.set_window_rect(h, r[0], r[1], r[2], r[3],
                                       activate=False)
                    wm._last_rect[h] = r
                except Exception as e:  # noqa: BLE001
                    print("还原 %s 失败: %s" % (h, e))
        print("已还原 %d 个窗口的原始几何" % len(orig))


if __name__ == "__main__":
    sys.exit(main())
