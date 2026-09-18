"""多屏任务栏（README「可作练手扩展的方向」之一）。

在每块显示器边缘显示一个细条，列出该屏的窗口，可点击激活、一键移到相邻屏。
纯 tkinter + 标准库，零第三方依赖；**由 UI 主线程周期性调用 refresh() 驱动更新**，
不自建线程（沿用既有「主线程 runloop」约束）。

窗口→显示器的归属判定与条几何计算都是纯函数（split_windows_by_monitor /
bar_geometry / short_title），可在不创建 Tk 窗口的情况下单测。
"""
import logging
import tkinter as tk

logger = logging.getLogger(__name__)

BAR_HEIGHT = 30
BAR_MAX_WIDTH = 900
BAR_MARGIN = 8
MAX_TITLE = 18
MAX_ITEMS = 8

_BG = "#20242b"
_ITEM_BG = "#2d333b"
_ACTIVE_BG = "#3d4757"
_FG = "#e6edf3"
_MUTED = "#8b949e"


def short_title(win):
    """窗口标题（优先窗口名，回退应用名），超长截断。"""
    name = (win.get("name") or "").strip() or (win.get("owner") or "").strip()
    name = name.replace("\n", " ")
    return name if len(name) <= MAX_TITLE else name[:MAX_TITLE - 1] + "…"


def _center_in(win, mon):
    """窗口中心点是否落在显示器矩形内（CG 全局坐标，可为负）。"""
    x, y, w, h = win.get("x"), win.get("y"), win.get("w"), win.get("h")
    if None in (x, y, w, h):
        return False
    cx, cy = x + w / 2.0, y + h / 2.0
    return (mon.left <= cx < mon.left + mon.width
            and mon.top <= cy < mon.top + mon.height)


def _overlap_area(win, mon):
    """窗口与显示器的重叠面积（用于跨屏窗口取最大者）。"""
    x, y = win.get("x") or 0, win.get("y") or 0
    w, h = win.get("w") or 0, win.get("h") or 0
    ox = max(0, min(x + w, mon.left + mon.width) - max(x, mon.left))
    oy = max(0, min(y + h, mon.top + mon.height) - max(y, mon.top))
    return ox * oy


def split_windows_by_monitor(monitors, wins):
    """把窗口按所属显示器分组。

    优先用窗口中心点命中；跨屏（中心点不在任何屏内）时取重叠面积最大的屏。
    返回 {device_path: [win, ...]}，只包含有窗口的显示器。
    """
    result = {}
    for win in wins or []:
        target = None
        for mon in monitors or []:
            if _center_in(win, mon):
                target = mon
                break
        if target is None:
            best_area = 0
            for mon in monitors or []:
                area = _overlap_area(win, mon)
                if area > best_area:
                    best_area, target = area, mon
        if target is None:
            continue
        result.setdefault(target.device_path, []).append(win)
    return result


def bar_geometry(mon, position="bottom", height=BAR_HEIGHT,
                 max_width=BAR_MAX_WIDTH, margin=BAR_MARGIN):
    """计算任务栏几何 (x, y, w, h)：水平居中，贴显示器底部或顶部。"""
    width = int(min(max_width, max(200, mon.width - 2 * margin)))
    x = int(mon.left + (mon.width - width) / 2.0)
    if position == "top":
        y = int(mon.top + margin + 24)  # 让开菜单栏
    else:
        y = int(mon.top + mon.height - height - margin)
    return x, y, width, height


def geometry_string(x, y, w, h):
    """生成 Tk geometry 字符串，**必须正确支持负坐标**。

    Tk 语法为 `WxH±X±Y`：坐标可直接带负号，但**不能**写成 `+-38`。
    副屏位于主屏左侧 / 上方时（macOS 极常见）x 或 y 为负，用 `+%d` 拼接会得到
    `900x30+350+-38`，Tk 会抛 TclError —— 真机验证时正是这个原因导致两个屏
    的任务栏根本没被创建出来。
    """
    xs = "+%d" % x if x >= 0 else "%d" % x
    ys = "+%d" % y if y >= 0 else "%d" % y
    return "%dx%d%s%s" % (w, h, xs, ys)


class TaskBar:
    """每块显示器一个 tkinter 细条；由 UI 主线程周期性 refresh() 更新。"""

    def __init__(self, root, on_focus=None, on_move=None, position="bottom"):
        self.root = root
        self.on_focus = on_focus
        self.on_move = on_move
        self.position = position
        self._bars = {}       # device_path -> {"top", "frame", "sig"}
        self._mons = []       # 最近一次 refresh 的显示器列表（供相邻屏查找）
        self._cache = {}      # device_path -> 最近渲染的窗口列表（供 ◀▶ 使用）
        self._selected = {}   # device_path -> 选中窗口的 hwnd
        self._visible = False

    # ---------- 生命周期 ----------
    def show(self):
        self._visible = True

    def hide(self):
        self._visible = False
        for dev in list(self._bars):
            self._destroy_bar(dev)

    def destroy(self):
        self.hide()

    def set_position(self, position):
        """切换贴底 / 贴顶；清空签名强制重排（几何在 _fill 时一并更新）。"""
        if position != self.position:
            self.position = position
            for bar in self._bars.values():
                bar["sig"] = None

    def _destroy_bar(self, dev):
        bar = self._bars.pop(dev, None)
        if not bar:
            return
        try:
            bar["top"].destroy()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 更新 ----------
    def refresh(self, monitors, wins):
        """按当前显示器与窗口刷新各屏任务栏（幂等；内容无变化时不重建按钮）。"""
        if not self._visible:
            return
        self._mons = list(monitors or [])
        groups = split_windows_by_monitor(self._mons, wins)
        alive = {m.device_path for m in self._mons}
        for dev in list(self._bars):
            if dev not in alive:
                self._destroy_bar(dev)
                self._cache.pop(dev, None)
        for mon in self._mons:
            items = groups.get(mon.device_path, [])
            sig = self._signature(items)
            bar = self._bars.get(mon.device_path)
            if bar is None:
                bar = self._create_bar(mon)
                if bar is None:
                    continue
                self._bars[mon.device_path] = bar
            if bar.get("sig") == sig:
                continue
            self._fill(bar, mon, items)
            bar["sig"] = sig

    @staticmethod
    def _signature(items):
        return "|".join("%s:%s" % (w.get("hwnd"), short_title(w)) for w in items)

    def _create_bar(self, mon):
        try:
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.geometry(geometry_string(*bar_geometry(mon, self.position)))
            try:
                top.attributes("-topmost", True)
                top.attributes("-alpha", 0.94)
            except Exception:  # noqa: BLE001
                pass
            frame = tk.Frame(top, bg=_BG)
            frame.pack(fill="both", expand=True)
            return {"top": top, "frame": frame, "sig": None}
        except Exception:  # noqa: BLE001
            # 不能静默吞掉：负坐标 / 权限等问题会让该屏任务栏不出现且毫无提示
            logger.warning("创建任务栏失败（该屏将不显示任务栏）", exc_info=True)
            return None

    def _fill(self, bar, mon, items):
        top, frame = bar["top"], bar["frame"]
        try:
            top.geometry(geometry_string(*bar_geometry(mon, self.position)))
        except Exception:  # noqa: BLE001
            pass
        for child in frame.winfo_children():
            child.destroy()
        shown = items[:MAX_ITEMS]
        self._cache[mon.device_path] = shown
        if not shown:
            tk.Label(frame, text="（此屏没有窗口）", bg=_BG, fg=_MUTED,
                     font=("Helvetica", 10)).pack(side="left", padx=8)
        else:
            sel = self._selected.get(mon.device_path)
            for win in shown:
                active = win.get("hwnd") == sel
                label = tk.Label(frame, text=short_title(win), font=("Helvetica", 10),
                                 padx=6, pady=2,
                                 bg=(_ACTIVE_BG if active else _ITEM_BG), fg=_FG)
                label.pack(side="left", padx=2, pady=2)
                label.bind("<Button-1>", lambda e, w=win, m=mon: self._pick(w, m))
        # 跨屏移动：把选中（或第一个）窗口移到左 / 右相邻显示器
        for text, direction in (("◀", -1), ("▶", 1)):
            btn = tk.Label(frame, text=text, bg=_BG, fg=_FG,
                           font=("Helvetica", 11), padx=6)
            btn.pack(side="right", padx=2)
            btn.bind("<Button-1>", lambda e, m=mon, d=direction: self._shift(m, d))

    # ---------- 交互 ----------
    def _pick(self, win, mon):
        """点击窗口标题：记为选中并请求激活。"""
        self._selected[mon.device_path] = win.get("hwnd")
        if self.on_focus is not None:
            self.on_focus(win)

    def _shift(self, mon, direction):
        """把选中（或第一个）窗口移到相邻显示器。"""
        items = self._cache.get(mon.device_path) or []
        if not items:
            return
        sel = self._selected.get(mon.device_path)
        win = next((w for w in items if w.get("hwnd") == sel), items[0])
        ordered = sorted(self._mons, key=lambda m: m.left)
        idx = next((i for i, m in enumerate(ordered)
                    if m.device_path == mon.device_path), None)
        if idx is None:
            return
        nxt = idx + direction
        if not (0 <= nxt < len(ordered)):
            return
        if self.on_move is not None:
            self.on_move(win, ordered[nxt])
