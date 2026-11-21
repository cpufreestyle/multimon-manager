#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验证四宫格 2×2：四扇窗是否真的落在同一块屏的四个格子里。

检查项（都是用户直接能看到的）：
  1. 四个窗口都在目标屏的工作区内；
  2. 任意两个窗口不重叠（容差 1px）；
  3. 下排底边收在工作区底部；
  4. 上排不越出工作区顶部（菜单栏那条由实现自行扣掉）。

跑完把四个窗口的原始几何全部还原。文件名不是 test_*.py，`unittest discover`
不会加载；只在手动运行时执行。

用法：
    /opt/homebrew/bin/python3.14 tests/verify_quad_real.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend as b  # noqa: E402
import windows_mac as wm  # noqa: E402

# Terminal/iTerm 跑脚本时几何读写会被自身窗口打断；本程序自己也不能当被试。
AVOID = ("多屏管理器", "Python", "CodeBuddy", "UU远程", "ToDesk", "Sidecar",
         "Terminal", "iTerm",
         # 浏览器窗口标题随页面变、且常有额外辅助窗口（输入法/悬浮条），
         # 探测期间标题一变就会被当成「窗口已关闭」换替身，测出来全是假 FAIL。
         "Google Chrome", "Safari", "Microsoft Edge", "Firefox",
         # ChatGPT 的窗口标题跟着会话名变（zen/ringdeck/…），同样会在
         # 「探测 → 排列」之间失效。
         "ChatGPT")


def read(hwnd):
    wm._last_rect.pop(hwnd, None)
    return wm.get_window_rect(hwnd)


def overlap(a, c):
    ax, ay, aw, ah = a
    cx, cy, cw, ch = c
    ow = min(ax + aw, cx + cw) - max(ax, cx)
    oh = min(ay + ah, cy + ch) - max(ay, cy)
    return (ow, oh)


def main():
    mons = b.enum_monitors(force=True)
    if not mons:
        print("SKIP: 没有可用显示器")
        return 0
    land = [m for m in mons if m.height <= m.width]
    if not land:
        print("SKIP: 没有横屏（四宫格不主动送竖屏）")
        return 0
    mon = max(land, key=lambda m: m.work_width * m.work_height)
    idx = mons.index(mon)
    wl, wt, ww, wh = mon.work_rect
    print("目标屏 %r work=%s（%dx%d）" % (mon.device_name, mon.work_rect, ww, wh))

    wins, seen = [], set()
    for w in b.list_target_windows():
        owner = (w.get("owner") or "")
        hw = w.get("hwnd")
        if not hw or not w.get("name") or any(a in owner for a in AVOID):
            continue
        if owner in seen or w.get("w", 0) < 160 or w.get("h", 0) < 120:
            continue
        if w.get("w", 0) >= ww * 0.98 and w.get("h", 0) >= wh * 0.98:
            continue
        # 四格每格大约只有工作区的一半；原窗口本身就超过一半面积的，
        # 几乎必然被最小尺寸挡下，选进来只会测到「放不下」。
        if w.get("w", 0) * w.get("h", 0) > ww * wh * 0.55:
            continue
        r = read(hw)
        if not r or r == (0, 0, 0, 0):
            continue
        if wm._ax_is_fullscreen(hw) is True:
            continue
        seen.add(owner)
        wins.append(w)
        if len(wins) >= 4:
            break
    if len(wins) < 4:
        print("SKIP: 可用窗口不足 4 个（%d）" % len(wins))
        return 0

    hwnds = ["%s::%s" % (w["owner"], w["name"]) for w in wins]
    orig = {}
    print("\n使用窗口:")
    for h in hwnds:
        orig[h] = read(h)
        print("  %-26s 原几何 %s" % (h.split("::")[0], orig[h]))

    try:
        wm.probe_min_sizes(hwnds)
        # 探测要好几秒，期间标题可能变（浏览器/聊天应用尤甚）。四个窗口
        # 必须都还在，否则 backend 会拿别的窗口顶上，验证对象就不是原来那四个。
        missing = [h for h in hwnds if wm._find_window(h) is None]
        if missing:
            print("SKIP: 探测期间窗口已变化（%s），重跑一次即可" % missing)
            return 0
        unreadable_now = [h for h in hwnds
                          if (read(h) or (0, 0, 0, 0)) == (0, 0, 0, 0)]
        if unreadable_now:
            print("SKIP: 探测后读不到几何（%s），窗口可能在被切走的 Space"
                  % [h.split("::")[0] for h in unreadable_now])
            return 0
        print("最小尺寸: %s" % {h.split("::")[0]: wm._QUAD_MIN_CACHE.get(h)
                             for h in hwnds})
        ok = wm.snap_four_quad(tl_hwnd=hwnds[0], tr_hwnd=hwnds[1],
                               bl_hwnd=hwnds[2], br_hwnd=hwnds[3],
                               monitor=idx, smart=False)
        print("\nsnap_four_quad 返回:", ok)
        for n in (getattr(wm, "_last_quad_notes", None) or []):
            print("  说明:", n)

        got = {h: read(h) for h in hwnds}
        # 只检查「真正被排上」的窗口：backend 明确报告「放不下、已保持原位」
        # 的那些留在别的屏上是预期行为，不该算成越界。
        placed = {h for _c, h in (getattr(wm, "_last_quad_slots", None) or [])}
        if not placed:
            placed = set(hwnds)
        unreadable = [h for h in placed
                      if (got.get(h) or (0, 0, 0, 0)) == (0, 0, 0, 0)]
        if unreadable:
            print("SKIP: 排列后读不到几何（%s），跳过判定"
                  % [h.split("::")[0] for h in unreadable])
            return 0
        skipped = [h for h in hwnds if h not in placed]
        if skipped:
            print("未排（保持原位）: %s"
                  % [h.split("::")[0] for h in skipped])
        print("\n%-20s %-26s %s" % ("窗口", "实测", "在屏内"))
        problems = []
        for h in [x for x in hwnds if x in placed]:
            r = got[h]
            inside = (r[0] >= wl and r[1] >= wt
                      and r[0] + r[2] <= wl + ww
                      and r[1] + r[3] <= wt + wh)
            print("%-20s %-26s %s" % (h.split("::")[0], r,
                                      "是" if inside else "否"))
            if not inside:
                problems.append("%s 越界 %s" % (h.split("::")[0], r))
        live = [h for h in hwnds if h in placed]
        for i in range(len(live)):
            for j in range(i + 1, len(live)):
                ow, oh = overlap(got[live[i]], got[live[j]])
                if ow > 1 and oh > 1:
                    problems.append("%s 与 %s 重叠 %dx%d"
                                    % (live[i].split("::")[0],
                                       live[j].split("::")[0], ow, oh))
        # 下排底边：取左下/右下里更靠下的那个
        bottoms = [got[h][1] + got[h][3] for h in live]
        if max(bottoms) < wt + wh - 8:
            problems.append("下排底边 %d 未收到 %d" % (max(bottoms), wt + wh))
        print("\n结论: %s" % ("PASS" if ok and not problems
                             else "FAIL " + "；".join(problems)))
        return 0 if (ok and not problems) else 1
    finally:
        for h, r in orig.items():
            if r and r != (0, 0, 0, 0):
                wm.set_window_rect(h, r[0], r[1], r[2], r[3], activate=False)
        print("已还原 %d 个窗口" % len(orig))


if __name__ == "__main__":
    sys.exit(main())
