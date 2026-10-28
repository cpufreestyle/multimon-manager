# -*- coding: utf-8 -*-
"""插拔屏幕归位：拔掉/插回副屏后，把窗口送回它原来所在的那块屏。

思路学自开源项目 LayoutKeeper（GPL-3.0，tumingke0/LayoutKeeper）。它解决同一个
问题的四个做法里，本项目只缺后三个：

1. **记住每扇窗原来在哪块屏**。按屏的稳定身份记，不按绝对坐标——拔掉左边的屏，
   右边屏的坐标系整体左移，绝对坐标全错。
2. **连续采集**。它有个 LiveLayoutRecord：外接屏插着且稳定时就一直记着这块屏上
   有哪些窗口、各自在哪，拔屏那一刻把这份记录**冻结**（frozenAt + 窗口 ID 天花板）
   当还原依据。本项目以前只有用户手动「保存布局」这一个快照，中间开的新窗口、
   挪过的位置全不认。
3. **显示器身份用 vendor/model/serial**。它用 MonitorIdentity(vendor, model,
   serial) 当屏的 key；本项目 layout 里已有 device_path->UUID 映射（同为
   1920x1080 的两块屏 UUID 能区分，重启/重插不变），UUID 优先，没有 UUID 才退回
   (vendor, model, serial)，再不行才退回分辨率指纹。
4. **归位策略机**。它的 ReconnectPolicy 把「全局模式 + 每屏模式 + 暂停」合成一个
   动作：自动 / 每次询问 / 询问一次后记住 / 关闭。本项目原先只有「自动套用情景」
   一个布尔开关，用户对某块屏不想要归位时没有退路。

边界与既有约定保持一致：
- 窗口绝不落到屏幕外：目标屏不存在时按相对框重映射到现存屏，再 clamp_to_work
  钉回工作区（screen_math 的职责），并如实报告「落到了别的屏」。
- 归位失败要如实说：静默把窗口丢在屏外是用户投诉最多的（真机见过 y=-870）。
- 去抖由调用方（ui._poll_display 已有 2.5s 等待）负责；本模块另有一层
  记录过期判断，供没有 UI 去抖的路径（托盘/命令行）复用。

本模块是纯函数 + 一个薄编排层（不碰显示器枚举、不碰窗口后端），便于单测；
真实动作由调用方注入的 set_rect（backend.set_window_rect）执行。
"""
import copy
import json
import os
import time

import screen_math
import settings

_SCHEMA = 1
_KEY = "rehome"

# 归位模式：与 LayoutKeeper 的 AutoRestoreMode 对齐（off / ask / auto）
MODE_OFF = "off"
MODE_ASK = "ask"
MODE_AUTO = "auto"
MODES = (MODE_OFF, MODE_ASK, MODE_AUTO)
# 默认「先询问」：与 LayoutKeeper 的 defaultMode 一致。插拔是低频动作，
# 默认不问会让用户某次插屏时窗口被莫名搬走，比多点一下更糟。
DEFAULT_MODE = MODE_ASK

# 采集节流：两次采集间隔小于这个值就跳过（采集要列全部窗口 + 逐窗读回几何，
# 无节流时插拔期间会连采十几次）。
CAPTURE_MIN_INTERVAL = 1.0

# 采集记录的有效期（秒）。LayoutKeeper 用 CGWindowID 单调递增判断
# 「这扇窗是拔屏之后才开的」，本项目没有窗口 ID，改用「拔屏时刻的窗口集合 +
# 时间戳」：超过这个时限后，无法再断定某扇新窗当时不在那块屏上，就不再认它。
CEILING_TTL = 3600.0


def _path():
    return os.path.join(settings.data_dir(), "rehome.json")


# ---------- 屏身份 ----------

def monitor_key(monitor):
    """一块屏的稳定 key：优先 UUID，其次 (vendor, model, serial)，最后分辨率。

    LayoutKeeper 的 MonitorIdentity.key 是 vendor-model-serial 三元组；本项目
    已有更稳的 CGDisplayCreateUUIDFromDisplayID（layouts 的 monitor_uuids 用的
    就是它），两块同为 1920x1080 的屏 UUID 也能区分，重启/重插不变。UUID 取不到
    （非 macOS / API 缺失）时退回 vendor-model-serial（设备身份，仍比分辨率指纹
    稳），最后才退回 "WxH@L,T"（会随排列变化，只保证不崩）。
    """
    if monitor is None:
        return ""
    dp = str(getattr(monitor, "device_path", "") or "").strip()
    if dp:
        try:
            u = screen_math.display_uuids([monitor]).get(dp)
        except Exception:  # noqa: BLE001
            u = None
        if u:
            return "uuid:" + str(u)
    vendor = getattr(monitor, "vendor_id", None)
    model = getattr(monitor, "model_id", None)
    serial = getattr(monitor, "serial", None)
    if vendor or model or serial:
        return "hwid:%s-%s-%s" % (vendor or 0, model or 0, serial or 0)
    try:
        # 尺寸与位置之间用 "#"：指纹串用 "@" 分隔 key 与位置（见
        # screen_fingerprint），geo key 若也用 "@" 会让指纹解析出现歧义
        # （"geo:1080x1920@-1080,-1050@-1080,-1050"），同一块屏重排后会被
        # 误判成两块不同的屏。
        return "geo:%dx%d#%d,%d" % (int(monitor.width), int(monitor.height),
                                    int(monitor.left), int(monitor.top))
    except Exception:  # noqa: BLE001
        return "geo:unknown"


def monitor_identity(monitor):
    """屏的身份元组（key, 名称, 分辨率），用于界面展示与指纹比对。"""
    if monitor is None:
        return None
    name = (getattr(monitor, "device_name", "") or "").strip()
    try:
        res = "%dx%d" % (int(monitor.width), int(monitor.height))
    except Exception:  # noqa: BLE001
        res = "?"
    return {"key": monitor_key(monitor), "name": name, "res": res}


def screen_fingerprint(monitors):
    """当前屏组合指纹：各屏 key + 位置的排序集合。

    用来判「插了/拔了哪块屏」：插拔后 key 集合变，重排后也变（位置进指纹），
    所以重排同样会触发归位——LayoutKeeper 的 ReconnectWatch 也是这么判的
    （monitorKey != lastStableKey）。

    格式为 "key@left,top"，用 "@" 分隔。注意 geo key 自身就含 "@"
    （"geo:1080x1920@-1080,-1050"），所以取 key 时必须**从右往左**切最后一次
    "@"——从左切会把 geo key 的位置信息一起切掉，同一块屏重排后被误判成两块。
    """
    if not monitors:
        return ""
    parts = []
    for m in monitors:
        if m is None:
            continue
        k = monitor_key(m)
        if not k:
            continue
        try:
            parts.append("%s@%d,%d" % (k, int(m.left), int(m.top)))
        except Exception:  # noqa: BLE001
            parts.append(k)
    return "|".join(sorted(parts))


def _fingerprint_keys(fp):
    """指纹串 → key 集合（去掉追加在末尾的 "@left,top"）。"""
    out = set()
    for part in (fp or "").split("|"):
        if not part:
            continue
        out.add(part.rsplit("@", 1)[0] if "@" in part else part)
    return out


def fingerprint_diff(old_fp, new_fp):
    """两个指纹的差异：返回 (拔掉的 key 列表, 插上的 key 列表)。

    位置变了但屏没变时，两边 key 相同，两个列表都为空——调用方据此知道
    「只是重排」，归位计划会按相对框重算而不是按绝对坐标硬搬。
    """
    old, new = _fingerprint_keys(old_fp), _fingerprint_keys(new_fp)
    return (sorted(old - new), sorted(new - old))


# ---------- 实时采集记录 ----------

def capture(monitors, windows, now=None, min_interval=CAPTURE_MIN_INTERVAL):
    """给每块屏记下「现在有哪些窗口、各自在哪」（LayoutKeeper 的 LiveLayoutRecord）。

    windows 是 backend.list_target_windows() 的结果（带 hwnd/owner/name/x/y/w/h）。
    每块屏只记**归属它**的窗口（按交集最长判，screen_math.monitor_index_by_intersection），
    并额外记下窗口在所在屏工作区上的相对框（0~1）——拔屏后重映射到另一块屏时
    按比例缩放，而不是把 1920 宽的窗口硬塞进 1080 宽的竖屏。

    节流：距上次采集不足 min_interval 秒时返回 None（调用方当「没采到」）。
    返回 {屏 key: {"name", "res", "work", "at", "wins": [{hwnd, owner, name,
    x, y, w, h, rel}]}}
    """
    if not monitors:
        return None
    now = time.time() if now is None else float(now)
    data = _load()
    last = float(data.get("last_capture_ts") or 0.0)
    if now - last < float(min_interval):
        return None
    out = {}
    for i, m in enumerate(monitors):
        if m is None:
            continue
        k = monitor_key(m)
        if not k:
            continue
        ident = monitor_identity(m) or {}
        try:
            work = [int(m.work_left), int(m.work_top),
                    int(m.work_width), int(m.work_height)]
        except Exception:  # noqa: BLE001
            work = None
        wins = []
        for w in windows or []:
            if not isinstance(w, dict) or w.get("x") is None:
                continue
            try:
                x, y, ww, hh = int(w["x"]), int(w["y"]), int(w["w"]), int(w["h"])
            except (KeyError, TypeError, ValueError):
                continue
            if screen_math.monitor_index_by_intersection(
                    list(monitors), x, y, ww, hh, fallback=-1) != i:
                continue
            rel = None
            if work:
                try:
                    rel = [round(v, 6) for v in screen_math.normalize_to_work(
                        x, y, ww, hh, work[0], work[1], work[2], work[3])]
                except Exception:  # noqa: BLE001
                    rel = None
            wins.append({
                "hwnd": str(w.get("hwnd") or ""),
                "owner": str(w.get("owner") or ""),
                "name": str(w.get("name") or ""),
                "x": x, "y": y, "w": ww, "h": hh,
                "rel": rel,
            })
        out[k] = {
            "name": ident.get("name", ""),
            "res": ident.get("res", "?"),
            "work": work,
            "at": now,
            "wins": wins,
        }
    data["schema"] = _SCHEMA
    data["last_capture_ts"] = now
    data["screens"] = out
    _save(data)
    return out


def freeze(monitors, keys, now=None):
    """拔屏时把被拔掉的屏的记录冻结下来（frozenAt）。

    LayoutKeeper 在真正拔屏那一刻冻结 LiveLayoutRecord，之后插回时「采集记录 +
    冻结记录」一起当依据；它还用窗口 ID 天花板判断「这扇窗是拔屏之后才开的，
    从来不在那块屏上」。本项目没有窗口 ID，改用「拔屏时刻在屏上的窗口集合」：
    归位只搬记录里有的窗口，拔屏后新开的窗不在记录里，自然不会被动。

    只冻结 keys 里的屏；没被冻结的记录继续当普通采集记录（屏还在的情况）。
    """
    if not keys:
        return 0
    now = time.time() if now is None else float(now)
    data = _load()
    screens = data.get("screens") or {}
    frozen = data.get("frozen") or {}
    n = 0
    for k in keys:
        k = str(k)
        # 已经冻结过就不改：macOS 插拔常连报多次（真机一次插拔连报 3 次以上），
        # 每次都改写 frozen_at 会让记录有效期不断后移，也可能在中间态上抓到
        # 「窗口已经被搬过一次」的假现场。LayoutKeeper 同样只在真正拔屏那刻
        # 冻结一次。
        if k in frozen or k not in screens:
            continue
        rec = copy.deepcopy(screens[k])
        rec["frozen_at"] = now
        frozen[k] = rec
        n += 1
    if n:
        data["frozen"] = frozen
        _save(data)
    return n


def clear_frozen(keys=None):
    """插回成功后清掉冻结记录（keys=None 清全部）。返回清掉的条数。"""
    data = _load()
    frozen = data.get("frozen") or {}
    if not frozen:
        return 0
    if keys is None:
        n = len(frozen)
        data["frozen"] = {}
    else:
        n = 0
        for k in keys:
            k = str(k)
            if k in frozen:
                del frozen[k]
                n += 1
    if n:
        _save(data)
    return n


def frozen_screens():
    """返回冻结记录 {key: record}（只读副本）。"""
    return copy.deepcopy((_load().get("frozen") or {}))


def known_screens():
    """返回所有记录过的屏 {key: {"name", "res", "last_seen", "frozen"}}。"""
    data = _load()
    out = {}
    for k, rec in (data.get("screens") or {}).items():
        out[k] = {"name": rec.get("name", ""), "res": rec.get("res", "?"),
                  "last_seen": rec.get("at", 0), "frozen": False}
    for k, rec in (data.get("frozen") or {}).items():
        if k in out:
            out[k]["frozen"] = True
        else:
            out[k] = {"name": rec.get("name", ""), "res": rec.get("res", "?"),
                      "last_seen": rec.get("frozen_at", 0), "frozen": True}
    return out


# ---------- 归位策略机（LayoutKeeper 的 ReconnectPolicy）----------

def policy_action(global_mode, per_monitor, paused_until_value=None, now=None):
    """全局模式 + 每屏模式 + 暂停 → 一个动作（"auto" / "ask" / "none" / "paused"）。

    每屏模式优先于全局（LayoutKeeper 同款语义：用户在设置里给某块屏单独选了
    「永不归位」，就一直生效）；暂停压制一切；全局为 ask 且每屏没有选择时返回
    "ask"（询问一次后记住的语义由调用方实现：询问结果写回每屏模式）。
    """
    if paused_until_value:
        now = time.time() if now is None else float(now)
        try:
            if float(paused_until_value) > now:
                return "paused"
        except (TypeError, ValueError):
            pass
    if per_monitor in (MODE_OFF, MODE_ASK, MODE_AUTO):
        return per_monitor
    gm = global_mode if global_mode in MODES else DEFAULT_MODE
    if gm == MODE_OFF:
        return MODE_OFF
    if gm == MODE_AUTO:
        return MODE_AUTO
    return MODE_ASK


def set_mode(key, mode):
    """给某块屏单独设归位模式（None 表示删除该屏的单独选择，回到全局）。"""
    if not key:
        return False
    k = str(key)
    if mode in MODES:
        s = settings.load()
        prefs = s.get(_KEY) or {}
        prefs[k] = mode
        s[_KEY] = prefs
        settings.save(s)
        return True
    if mode is None:
        s = settings.load()
        prefs = s.get(_KEY) or {}
        if k in prefs:
            del prefs[k]
            s[_KEY] = prefs
            settings.save(s)
        return True
    return False


def per_monitor_modes():
    """返回 {屏 key: 模式}（设置里单独选过的那些屏）。"""
    prefs = settings.load().get(_KEY) or {}
    return {str(k): v for k, v in prefs.items() if v in MODES}


def pause(minutes=60):
    """暂停归位 N 分钟（LayoutKeeper 的 AutoRestorePause：1 小时 / 直到明天）。"""
    s = settings.load()
    s[_KEY + "_paused_until"] = time.time() + max(1, int(minutes)) * 60
    settings.save(s)


def resume():
    """解除暂停。"""
    s = settings.load()
    if s.pop(_KEY + "_paused_until", None) is not None:
        settings.save(s)


def paused_until_value():
    v = settings.load().get(_KEY + "_paused_until")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- 归位几何（纯函数）----------

def resolve_monitor(monitors, key):
    """按 key 找当前屏；找不到返回 None。"""
    if not monitors or not key:
        return None
    for m in monitors:
        if m is not None and monitor_key(m) == str(key):
            return m
    return None


def target_monitor(monitors, key, prev_work=None):
    """窗口该回哪块屏：原屏还在就用原屏；不在则重映射到交集最长的现存屏。

    prev_work 是原屏的工作区 (left, top, w, h)，用来在「换了屏」时按交集挑最
    像的那块。都挑不到（现存屏与原屏完全不相交）时退回首块——绝不能返回 None，
    否则调用方会把窗口丢在屏外。
    """
    m = resolve_monitor(monitors, key)
    if m is not None:
        return m, "same"
    if not monitors:
        return None, "none"
    if prev_work:
        pl, pt, pw, ph = prev_work
        # 「换屏」时挑哪块现存屏来接这些窗口。用**投影重叠 + 间隙**判据，
        # 与 screen_math.adjacent_monitor_index 同源：两块屏算相邻，看的是它们
        # 在某个方向上的投影是否重叠、以及沿该方向的间隙，不是矩形交集面积。
        # 为什么不用交集：真机三屏里 DELL 竖屏右界正好是 0、主屏左界也是 0，
        # 两块屏矩形交集为 0（它们只是挨着，没有重叠），按交集挑永远挑不到。
        best, best_cost = None, None
        for cand in monitors:
            if cand is None:
                continue
            ox = min(pl + pw, cand.left + cand.width) - max(pl, cand.left)
            oy = min(pt + ph, cand.top + cand.height) - max(pt, cand.top)
            if ox <= 0 and oy <= 0:
                continue  # 两个方向都不相邻，不算接盘候选
            # 代价 = 间隙（不相邻方向按屏尺寸归一，越小越近）
            gap_x = max(0, cand.left - (pl + pw), pl - (cand.left + cand.width))
            gap_y = max(0, cand.top - (pt + ph), pt - (cand.top + cand.height))
            scale = max(1, max(pw, ph))
            cost = (gap_x + gap_y) / float(scale)
            if best_cost is None or cost < best_cost:
                best, best_cost = cand, cost
        if best is not None:
            return best, "remapped"
    for cand in monitors:
        if cand is not None:
            return cand, "fallback"
    return None, "none"


def plan_window(win, monitors, key, prev_work=None, max_frac=2.0):
    """算一扇窗的归位矩形 (x, y, w, h, where)。

    where: "same"（原屏还在，按原坐标）/ "remapped"（换屏，按相对框重映射）
           / "fallback"（挑不到屏，钉回首块工作区）
    rel 存在时按比例缩放到目标屏工作区；否则按绝对坐标。两种结果都过
    clamp_to_work 钉回工作区内——窗口绝不落到屏幕外（真机见过 y=-870）。
    """
    if not isinstance(win, dict):
        return None
    mon, where = target_monitor(monitors, key, prev_work)
    if mon is None:
        return None
    try:
        x, y = int(win["x"]), int(win["y"])
        w, h = int(win["w"]), int(win["h"])
    except (KeyError, TypeError, ValueError):
        return None
    rel = win.get("rel")
    if where != "same" and isinstance(rel, (list, tuple)) and len(rel) == 4:
        # 换了屏：相对框整体缩放，但封顶 max_frac（屏工作区的 max_frac 倍），
        # 避免 1080 宽的竖屏上把相对框 1.2 的窗口拉成 1296 宽
        rx, ry, rw, rh = [float(v) for v in rel]
        mw, mh = int(mon.work_width), int(mon.work_height)
        rw = min(rw, max_frac)
        rh = min(rh, max_frac)
        w = max(1, min(int(round(rw * mw)), int(round(max_frac * mw))))
        h = max(1, min(int(round(rh * mh)), int(round(max_frac * mh))))
        x = int(round(mon.work_left + rx * mw))
        y = int(round(mon.work_top + ry * mh))
    x, y, w, h = screen_math.clamp_to_work(
        x, y, w, h, mon.work_left, mon.work_top, mon.work_width, mon.work_height)
    return (x, y, w, h, where)


def plan(monitors, keys, windows=None, now=None, max_age=CEILING_TTL):
    """给一组拔掉的屏算归位计划。

    keys: 刚拔掉的屏 key 列表（frozen_screens() 里有的那些）。
    windows: 当前窗口集合（{hwnd: ...} 或 [hwnd, ...]），用来按 hwnd 认出
    「原来在那些屏上的窗口现在还开着」。传 None 时不做匹配过滤（调用方自己匹配）。

    窗口匹配规则（从 LayoutKeeper 的窗口匹配简化而来）：
    - hwnd 相同 → 就是那扇窗（本项目的 hwnd 是 "应用::标题" 串，同 App 多窗时带
      #n 序号，稳定）；
    - 拔屏后新开的窗（hwnd 不在记录里）**不搬**——它当时不在那块屏上，
      搬过去就是抢用户的窗口。

    返回 [{"key", "screen", "where", "wins": [{hwnd, owner, name, x, y, w, h,
    where}], "skipped": [...]}]，每块屏一条。
    """
    frozen = frozen_screens()
    if windows is not None and not isinstance(windows, dict):
        windows = {str(w): True for w in windows}
    out = []
    for k in keys or []:
        k = str(k)
        rec = frozen.get(k)
        if not rec:
            continue
        now = time.time() if now is None else float(now)
        frozen_at = float(rec.get("frozen_at") or 0.0)
        if max_age and now - frozen_at > float(max_age):
            out.append({"key": k, "screen": rec.get("name") or k,
                        "where": "stale", "wins": [], "skipped": [
                            {"hwnd": "", "owner": "", "name": "",
                             "reason": "记录超过 %.0f 秒，不再自动搬窗" % max_age}]})
            continue
        _mon, where = target_monitor(monitors, k, _rec_work(rec))
        prev_work = _rec_work(rec)
        wins = []
        for w in rec.get("wins") or []:
            if not isinstance(w, dict):
                continue
            hwnd = str(w.get("hwnd") or "")
            if windows is not None and hwnd and hwnd not in windows:
                continue
            p = plan_window(w, monitors, k, prev_work)
            if not p:
                continue
            wins.append({"hwnd": hwnd, "owner": w.get("owner", ""),
                         "name": w.get("name", ""),
                         "x": p[0], "y": p[1], "w": p[2], "h": p[3],
                         "where": p[4]})
        out.append({"key": k, "screen": rec.get("name") or k,
                    "where": where, "wins": wins, "skipped": []})
    return out


def _rec_work(rec):
    """采集记录里的原屏工作区（老记录没有时返回 None，退化为绝对坐标）。"""
    work = rec.get("work")
    if isinstance(work, (list, tuple)) and len(work) == 4:
        try:
            return tuple(int(v) for v in work)
        except (TypeError, ValueError):
            return None
    return None


def apply_plan(plan_rows, set_rect, activate=False, limit=None):
    """执行归位计划；set_rect(hwnd, x, y, w, h) 由调用方注入（backend.set_window_rect）。

    返回 (成功数, 明细列表)。limit 限制本次搬运的窗口数（0/None 不限）：
    一次归位搬十几个窗口时，用户看到的是「窗口集体飞走」，限流能让过程可观察。
    明细每项 {"hwnd", "screen", "where", "ok", "err"}，供界面逐条报告。
    """
    ok, detail = 0, []
    n = 0
    for row in plan_rows or []:
        for w in row.get("wins") or []:
            if limit and n >= limit:
                return ok, detail
            n += 1
            item = {"hwnd": w.get("hwnd", ""), "screen": row.get("screen", ""),
                    "where": w.get("where", ""), "ok": False, "err": ""}
            try:
                set_rect(w["hwnd"], w["x"], w["y"], w["w"], w["h"],
                         activate=activate)
                item["ok"] = True
                ok += 1
            except Exception as e:  # noqa: BLE001
                item["err"] = str(e)
            detail.append(item)
    return ok, detail


def summary(plan_rows):
    """计划的人类可读摘要（界面状态栏用，一行）。"""
    if not plan_rows:
        return "没有需要归位的窗口"
    parts = []
    for r in plan_rows:
        if r.get("where") == "stale":
            parts.append(f"{r.get('screen')} 的记录已过期")
            continue
        n = len(r.get("wins") or [])
        if not n:
            continue
        if r.get("where") == "remapped":
            parts.append(f"{r.get('screen')} 已不存在，{n} 个窗口按比例落到现存屏")
        elif r.get("where") == "fallback":
            parts.append(f"{r.get('screen')} 已不存在，{n} 个窗口落到首屏工作区")
        else:
            parts.append(f"{r.get('screen')} 归位 {n} 个")
    if not parts:
        return "归位 0 个（记录里的窗口现在都没开着）"
    return "；".join(parts)


# ---------- 持久化 ----------

def _load():
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(data):
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
