"""多屏管理器 GUI（tkinter，零第三方依赖）。"""
import os
import sys
import time
import threading
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import autostart
import backend as b
import config_io
import profiles
import settings
import layouts
import screen_math
import scenarios
import snap_preview
import rules
import slideshow
import taskbar
import triggers
import title_match
import cmd_channel

# 多屏任务栏：界面显示值 → 内部位置值
TASKBAR_POS = {"底部": "bottom", "顶部": "top"}
# 触发器：界面显示值 → 内部键值
TRIG_EVENT_KEY = {v: k for k, v in triggers.EVENT_LABELS.items()}
TRIG_ACTION_KEY = {v: k for k, v in triggers.ACTION_LABELS.items()}
ZONE_KEY = {v: k for k, v in rules.ZONE_LABELS.items()}
# 吸附方位 -> 状态栏短文案
_SNAP_ZONE_LABELS = {
    "left": "左半屏", "right": "右半屏", "top": "上半屏", "bottom": "下半屏",
    "maximize": "整屏", "center": "居中",
    "left-third": "左1/3", "middle-third": "中1/3", "right-third": "右1/3",
    "quad-tl": "左上角", "quad-tr": "右上角", "quad-bl": "左下角", "quad-br": "右下角",
}

VK_LEFT = b.VK_LEFT
VK_UP = b.VK_UP
VK_RIGHT = b.VK_RIGHT
VK_DOWN = b.VK_DOWN

# 目标窗口下拉框的"自动"选项：由程序判断最前面的非本程序窗口
AUTO_TARGET = "自动（上次活动窗口）"
# 后端给同名窗口加的「#n 」序号前缀（如「#2 UU远程」）。四宫格短标签重新
# 编号前先剥掉它，避免出现「#3 #2 UU远程」这种叠编号。
_QUAD_SEQ_RE = re.compile(r"^#\d+\s*")
# 并排/竖排显示器下拉框的"自动"选项：按窗口当前所在屏决定
AUTO_MON = "自动（按窗口所在屏）"
# 跨屏铺满的两种铺法（界面显示值，同时作为 spread_mode_var 的取值）。
# 前缀「每屏」由标签「跨屏铺满:」承担：文案太长会把模式、按钮和屏勾选框
# 挤出一行放不下（Combobox 要 width=14 才不截断「每屏左上角四分之一」）。
SPREAD_MODE_MAX = "整屏最大化"
SPREAD_MODE_QUAD = "左上四分之一"
# 规则引擎（F5）：目标屏"不换屏"选项 + 位置预设（中文 → 内部键）
AUTO_RULE_MON = "当前屏（不换屏）"
_ZONE_BY_LABEL = {
    "整屏": "full", "左半屏": "left", "右半屏": "right",
    "上半屏": "top", "下半屏": "bottom",
}

# 壁纸轮换支持的图片扩展名
ROTATE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff")

# 常见图片格式的文件头（扩展名可以骗人，损坏文件交给系统会把壁纸置空）
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"BM",
                b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*")


def _looks_like_image(path):
    """按文件头判断文件是否真的是图片，读不到或格式不符返回 False。

    轮换是无人值守的：目录里混进一个空文件/半截下载的图，SetWallpaper 会
    成功返回但把壁纸清空，用户只会看到黑屏。所以扫描时就把这类文件剔掉。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    if len(head) < 8:
        return False
    if any(head.startswith(magic) for magic in _IMAGE_MAGIC):
        return True
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("多屏管理器 · michaelqiu 出品")
        # 默认宽度取 820：窗口工具里有「并排」「竖排」两组下拉，120 DPI 下
        # 760 宽会把最右侧的按钮挤出可视区（横向没有滚动条）。
        self.root.geometry("820x740")
        self.monitors = []
        self.mon_rows = []
        self.fit_var = tk.StringVar(value="fill")
        self.mode_var = tk.StringVar(value="per")
        self.single_var = tk.StringVar()
        # 全局快捷键开关：持久化到 settings，重启后自动恢复上次状态
        self.hk_enabled = tk.BooleanVar(
            value=bool(settings.load().get("hk_enabled", False)))
        # 全局快捷键修饰键：按平台默认（macOS ⌘⌥，Windows Ctrl+Alt），用户可改并持久化
        self.hk_mods_var = tk.StringVar(value=self._default_hk_mods())
        # 开机自动启动（v0.2.1）
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        # 常驻置顶：默认关闭。按钮已改为不激活目标窗口，管理器不会被挤走，
        # 无需常驻置顶；需要始终压住其它窗口时可手动勾选。
        self.topmost = tk.BooleanVar(value=False)
        self.hk = None
        # 显示器别名（F6）：device_path -> 别名，持久化在 settings.json
        self.aliases = settings.load().get("monitor_aliases", {})
        # 显示器热插拔监听（主线程轮询，避免后台 CFRunLoop 与 Tk 主循环冲突）
        self.monitor_watch = None
        self._display_poll_id = None
        self._last_mon_sig = None
        # 显示器插拔后的稳定等待：签名停止变化后才套用情景（Moom wake 思路）
        self._display_settle_id = None
        self._pending_mon_sig = None
        # 拖拽吸附预览（F3）：浮层懒创建，监听器按开关启停
        self._snap_preview = None
        self._drag_watcher = None
        self._drag_zone = None
        self._last_zone_ts = 0.0
        # 拖拽事件在 CGEventTap 原生回调里只记录坐标，真正的 UI/窗口操作延迟到主循环
        self._drag_pending = None
        self._drag_job = None
        # 规则引擎（F5）：记录已知窗口，用于识别"新出现的窗口"
        self._rule_seen = None
        self._build()
        self.refresh_monitors()
        self._start_command_poll()
        self.root.after(1500, self._poll_rules)
        # 壁纸幻灯片：恢复上次的启用状态（若有图片则续跑调度）
        self._slideshow_job = None
        self.root.after(2000, self._slideshow_restore)
        # 上次会话启用过全局快捷键则自动恢复（免每次启动手动勾选；失败静默降级）
        if self.hk_enabled.get():
            self.root.after(600, lambda: self._toggle_hk(quiet=True))
        # 窗口大小变化时防抖重绘布局图，并设最小尺寸避免控件被压坏
        # 最小宽度按 body 的自然宽度（120 DPI 下约 773）取齐，缩到这一步即停，
        # 再窄就会把窗口工具里的下拉/按钮挤出可视区。
        self.root.minsize(810, 560)
        self._resize_job = None
        self.root.bind("<Configure>", self._on_window_resize)

    # ---------- 构建 ----------
    def _build(self):
        # 所有分区放在同一个页面里：纵向内容超出窗口高度时，用画布 + 垂直滑块
        # 上下滚动浏览（不再分页）。
        self.content = ttk.Frame(self.root)
        self.content.pack(fill="both", expand=True)

        top = ttk.Frame(self.content)
        top.pack(fill="x", padx=8, pady=(6, 2))
        rm_btn = ttk.Button(top, text="刷新显示器", command=self.refresh_monitors)
        rm_btn.pack(side="left")
        self._tooltip(rm_btn, "重新检测当前连接的显示器（插拔屏幕后点这里）")
        self.status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=10)
        ttk.Label(top, text="michaelqiu 出品", foreground="#666666").pack(side="right", padx=(4, 2))

        # 辅助功能授权提示（默认隐藏，未授权时由 main 调用 show 显示）
        self._perm_frame = ttk.Frame(self.content)
        ttk.Label(
            self._perm_frame,
            text="未授权「辅助功能」：窗口控制与全局快捷键不可用。\n"
                 "请到 系统设置 → 隐私与安全性 → 辅助功能，开启本程序"
                 "（打包版为『多屏管理器』，源码运行版为『python3』）。",
            foreground="#a15c00", wraplength=700, justify="left",
        ).pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(self._perm_frame, text="打开系统设置",
                   command=self._open_accessibility_settings).pack(side="left", padx=4)
        ttk.Button(self._perm_frame, text="已授权/知道了",
                   command=self.hide_accessibility_hint).pack(side="left", padx=4)

        # ---- 可滚动主区域：单页 + 垂直滑块 ----
        self._scroll_area = ttk.Frame(self.content)
        self._scroll_area.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        # 画布背景取当前主题底色，避免与周围 ttk 控件出现色差
        try:
            canvas_bg = ttk.Style(self.root).lookup("TFrame", "background") or "#f0f0f0"
        except Exception:  # noqa: BLE001
            canvas_bg = "#f0f0f0"

        self._canvas = tk.Canvas(self._scroll_area, highlightthickness=0, bg=canvas_bg)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._vscroll = ttk.Scrollbar(self._scroll_area, orient="vertical",
                                      command=self._canvas.yview)
        self._vscroll.pack(side="right", fill="y")
        self._canvas.configure(yscrollcommand=self._vscroll.set)

        # 真正承载各分区的内部容器
        self.body = ttk.Frame(self._canvas)
        self._body_win = self._canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._bind_mousewheel()

        # 所有分区统一挂到同一个容器（不再分页）
        self._build_getting_started(self.body)
        self._build_window_tools(self.body)
        self._build_taskbar(self.body)
        self._build_triggers(self.body)
        self._build_wallpaper(self.body)
        self._build_profiles(self.body)
        self._build_rotate(self.body)
        self._build_layouts(self.body)
        self._build_hotkeys(self.body)
        self._build_autostart(self.body)
        self._build_backup(self.body)
        self._build_display_watch(self.body)
        self._build_scenarios(self.body)
        self._build_drag_snap(self.body)
        self._build_rules(self.body)
        self._build_settings_io(self.body)

    # ---------- 滚动支持 ----------
    def _on_body_configure(self, event=None):
        """内容尺寸变化时同步滚动区域（决定滑块可拖动范围）。"""
        try:
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        except Exception:  # noqa: BLE001
            pass

    def _on_canvas_configure(self, event):
        """内容宽度跟随画布宽度，避免出现横向滚动。"""
        try:
            self._canvas.itemconfigure(self._body_win, width=event.width)
        except Exception:  # noqa: BLE001
            pass
        self._on_body_configure()

    def _bind_mousewheel(self):
        """绑定滚轮：macOS/Windows 用 <MouseWheel>，X11 另有 Button-4/5。"""
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_mousewheel_button, add="+")
        self.root.bind_all("<Button-5>", self._on_mousewheel_button, add="+")

    def _pointer_in_scroll_area(self, event):
        """指针是否落在滚动区域内；自带滚动的控件交还给它们自己处理。"""
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:  # noqa: BLE001
            return False
        if w is None:
            return False
        if isinstance(w, (tk.Listbox, tk.Text, ttk.Treeview)):
            return False
        while w is not None:
            if w == self._scroll_area:
                return True
            w = getattr(w, "master", None)
        return False

    def _on_mousewheel(self, event):
        if not self._pointer_in_scroll_area(event):
            return
        delta = getattr(event, "delta", 0) or 0
        if not delta:
            return
        if abs(delta) >= 120:  # Windows / X11：一格 120
            units = int(-delta / 120) or (-1 if delta > 0 else 1)
        else:                  # macOS：量级很小，按方向滚一格
            units = -1 if delta > 0 else 1
        self._canvas.yview_scroll(units, "units")
        return "break"

    def _on_mousewheel_button(self, event):
        if not self._pointer_in_scroll_area(event):
            return
        self._canvas.yview_scroll(-1 if event.num == 4 else 1, "units")
        return "break"

    def _build_wallpaper(self, parent):
        f = ttk.LabelFrame(parent, text="壁纸")
        f.pack(fill="x", padx=8, pady=6)

        mode = ttk.Frame(f)
        mode.pack(fill="x", pady=4)
        ttk.Radiobutton(mode, text="每屏不同", variable=self.mode_var, value="per",
                        command=self._on_mode).pack(side="left")
        ttk.Radiobutton(mode, text="统一单图(所有屏)", variable=self.mode_var, value="single",
                        command=self._on_mode).pack(side="left")

        # 显示器布局可视化：直观看到哪块屏在左/右/上/下（几何数据已由 monitors_mac 提供）
        self.layout_canvas = tk.Canvas(f, height=272, bg="#fafafa",
                                       relief="sunken", borderwidth=1)
        self.layout_canvas.pack(fill="x", padx=6, pady=(2, 6))

        self.mon_frame = ttk.Frame(f)
        self.single_frame = ttk.Frame(f)
        ttk.Button(self.single_frame, text="选择图片",
                   command=lambda: self._pick(self.single_var)).pack(side="left")
        ttk.Entry(self.single_frame, textvariable=self.single_var, width=45).pack(
            side="left", padx=4, fill="x", expand=True)
        self._on_mode()

        fit = ttk.Frame(f)
        fit.pack(fill="x", pady=4)
        ttk.Label(fit, text="填充方式:").pack(side="left")
        ttk.Combobox(fit, textvariable=self.fit_var, values=list(b.POSITION.keys()),
                     width=10, state="readonly").pack(side="left", padx=4)
        ttk.Button(f, text="应用壁纸", command=self.apply_wallpaper).pack(anchor="e", pady=4)

        # ---- 壁纸幻灯片：按间隔自动轮换一组图片（纯标准库，主线程定时器驱动）----
        sf = ttk.LabelFrame(f, text="幻灯片")
        sf.pack(fill="x", padx=4, pady=(2, 4))
        row1 = ttk.Frame(sf)
        row1.pack(fill="x", padx=4, pady=2)
        ttk.Button(row1, text="添加图片", command=self._slideshow_add).pack(side="left")
        ttk.Button(row1, text="清空", command=self._slideshow_clear).pack(side="left", padx=4)
        ttk.Button(row1, text="立即切换",
                   command=self._slideshow_next_now).pack(side="left", padx=4)
        ttk.Label(row1, text="间隔(分):").pack(side="left", padx=(8, 2))
        self.slideshow_interval_var = tk.StringVar(value="5")
        ttk.Entry(row1, textvariable=self.slideshow_interval_var, width=4).pack(side="left")
        self.slideshow_shuffle_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="随机",
                        variable=self.slideshow_shuffle_var).pack(side="left", padx=6)
        self.slideshow_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="启用", variable=self.slideshow_enabled_var,
                        command=self._slideshow_toggle).pack(side="left", padx=6)

        self.slideshow_list = tk.Listbox(sf, height=3, activestyle="none")
        self.slideshow_list.pack(fill="x", padx=4, pady=2)
        row2 = ttk.Frame(sf)
        row2.pack(fill="x", padx=4, pady=(0, 2))
        ttk.Button(row2, text="移除选中",
                   command=self._slideshow_remove_selected).pack(side="left")
        self.slideshow_status_var = tk.StringVar(value="")
        ttk.Label(row2, textvariable=self.slideshow_status_var,
                  foreground="#666").pack(side="left", padx=8)
        self._slideshow_refresh()

    def _on_mode(self):
        if self.mode_var.get() == "per":
            self.mon_frame.pack(fill="x", pady=4)
            self.single_frame.pack_forget()
        else:
            self.mon_frame.pack_forget()
            self.single_frame.pack(fill="x", pady=4)

    def _pick(self, var):
        p = filedialog.askopenfilename(
            title="选择图片", filetypes=[("图片", "*.jpg;*.jpeg;*.png;*.bmp")]
        )
        if p:
            var.set(p)

    def _monitor_display_name(self, m):
        """显示器展示名：优先别名，否则回退系统名。"""
        return self.aliases.get(m.device_path) or m.device_name

    def _tooltip(self, widget, text):
        """给控件加悬停提示（tk 无内置 tooltip，用 Toplevel 实现）。

        悬停 ~0.5s 后在控件下方弹出淡黄色说明气泡，移开即消失。
        """
        tip = {"win": None, "job": None}

        def _show(_e=None):
            if tip["win"] is not None:
                return
            w = tk.Toplevel(widget)
            tip["win"] = w
            w.wm_overrideredirect(True)
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            w.wm_geometry(f"+{x}+{y}")
            lbl = tk.Label(
                w, text=text, justify="left", background="#ffffe0",
                foreground="#333333", relief="solid", borderwidth=1,
                font=("Arial", 11), padx=8, pady=5, wraplength=360,
            )
            lbl.pack()

        def _hide(_e=None):
            if tip["job"] is not None:
                try:
                    widget.after_cancel(tip["job"])
                except Exception:  # noqa: BLE001
                    pass
                tip["job"] = None
            if tip["win"] is not None:
                try:
                    tip["win"].destroy()
                except Exception:  # noqa: BLE001
                    pass
                tip["win"] = None

        def _schedule(_e=None):
            _hide()
            tip["job"] = widget.after(500, _show)

        widget.bind("<Enter>", _schedule)
        widget.bind("<Leave>", _hide)
        widget.bind("<ButtonPress>", _hide)

    def _tip(self, widget, text):
        """_tooltip 的简短别名，返回传入的 widget 便于链式调用。"""
        self._tooltip(widget, text)
        return widget

    def _on_alias_change(self, device_path, var):
        """显示器别名变更：更新内存与 settings.json，并刷新相关展示。"""
        val = (var.get() or "").strip()
        if val:
            self.aliases[device_path] = val
        else:
            self.aliases.pop(device_path, None)
        s = settings.load()
        s["monitor_aliases"] = self.aliases
        settings.save(s)
        try:
            self._refresh_monitor_choices()
            self._draw_layout()
        except Exception:  # noqa: BLE001
            pass

    def _undo_move(self):
        """撤销上一次窗口移动（F7）；快捷键由工作线程回调，UI 更新切回主线程。"""
        try:
            hwnd = b.undo_last_move()
        except Exception as e:  # noqa: BLE001
            self.root.after(0, lambda: messagebox.showerror("错误", f"撤销失败:\n{e}"))
            return
        if hwnd is None:
            self.root.after(0, lambda: self.status_var.set("没有可撤销的窗口移动"))
        else:
            self.root.after(0, lambda: self.status_var.set("已撤销上一次窗口移动"))
            self.root.after(0, self._keep_front)

    def _back_to_arranged(self):
        """把目标窗口送回上次被程序摆好的位置（第二层撤销 / snap back）。"""
        if getattr(b, "return_to_last_arranged", None) is None:
            self.status_var.set("当前平台暂不支持回到上次摆放位")
            return
        hwnd = self._current_target_hwnd()
        if not hwnd:
            self.status_var.set("请先在「目标窗口」选择一个窗口")
            return
        try:
            ok = b.return_to_last_arranged(hwnd)
        except Exception as e:  # noqa: BLE001
            self.status_var.set(f"回到上次摆放位失败：{e}")
            return
        if ok:
            self.status_var.set("已回到上次摆放位")
        else:
            self.status_var.set("该窗口没有被程序摆放过，没有可回的位置")

    def _current_target_hwnd(self):
        """当前界面选中的目标窗口 hwnd；「自动」时返回 None。"""
        target_map = getattr(self, "_target_map", None) or {}
        label = self.target_var.get()
        return target_map.get(label) if label else None

    def _back_to_active_arranged(self):
        """快捷键版「回到摆放位」：作用于当前活动窗口（不依赖界面选择）。"""
        if getattr(b, "return_to_last_arranged", None) is None:
            return
        try:
            hwnd = b.get_foreground_window(False)
        except Exception:  # noqa: BLE001
            hwnd = None
        if not hwnd:
            self.root.after(0, lambda: self.status_var.set("没有可回摆放位的活动窗口"))
            return
        try:
            ok = b.return_to_last_arranged(hwnd)
        except Exception:  # noqa: BLE001
            ok = False
        msg = "已回到上次摆放位" if ok else "该窗口没有被程序摆放过"
        self.root.after(0, lambda m=msg: self.status_var.set(m))

    def _stash_active_shortcut(self):
        """快捷键版暂存：作用于当前活动窗口。"""
        if getattr(b, "stash_active", None) is None:
            return
        try:
            ok = b.stash_active(use_pinned=False)
        except Exception:  # noqa: BLE001
            ok = False
        msg = ("已暂存到屏幕顶边（⌘⌥+D 取回）" if ok
               else "暂存失败：没有可操作的活动窗口")
        self.root.after(0, lambda m=msg: self.status_var.set(m))

    def _unstash_active_shortcut(self):
        """快捷键版取回：取回最早暂存的窗口。"""
        if getattr(b, "unstash_first", None) is None:
            return
        try:
            hwnd, ok = b.unstash_first()
        except Exception:  # noqa: BLE001
            ok, hwnd = False, None
        if ok:
            msg = "已取回最早暂存的窗口，位置尺寸原样恢复"
        elif hwnd is None:
            msg = "暂存列表是空的（或最早的窗口已关闭）"
        else:
            msg = "取回失败：窗口可能已关闭"
        self.root.after(0, lambda m=msg: self.status_var.set(m))

    def _stash_action(self):
        """暂存目标窗口到屏幕顶边。"""
        if getattr(b, "stash_active", None) is None:
            self.status_var.set("当前平台暂不支持暂存窗口")
            return
        try:
            ok = b.stash_active(use_pinned=True)
        except Exception as e:  # noqa: BLE001
            self.status_var.set(f"暂存失败：{e}")
            return
        if ok:
            self.status_var.set("已暂存到屏幕顶边（「取回」可还原，快捷键 ⌘⌥+D）")
        else:
            self.status_var.set("暂存失败：请先在「目标窗口」选择一个窗口")

    def _unstash_action(self):
        """取回最早暂存的窗口。"""
        if getattr(b, "unstash_first", None) is None:
            self.status_var.set("当前平台暂不支持取回窗口")
            return
        try:
            hwnd, ok = b.unstash_first()
        except Exception as e:  # noqa: BLE001
            self.status_var.set(f"取回失败：{e}")
            return
        if ok:
            self.status_var.set("已取回最早暂存的窗口，位置尺寸原样恢复")
        elif hwnd is None:
            self.status_var.set("暂存列表是空的（或最早的窗口已关闭）")
        else:
            self.status_var.set("取回失败：窗口可能已关闭")

    def _snap_btn(self, zone):
        """吸附按钮的统一入口：摆完后在状态栏如实报告钳制情况。

        App 的最小/固定尺寸会吃掉一部分吸附请求（真机 UU远程固定 921x680、
        CrossPaste 固定 600x700），windows 层已把该贴的边重新钉到边界上，
        这里把实测几何与目标格子的差值报给用户，避免「点了没反应」的错觉。
        """
        def run():
            try:
                b.snap_active(zone, activate=False)
                self._report_snap_status(zone)
            finally:
                self._keep_front()
        return run

    def _report_snap_status(self, zone):
        report = getattr(b, "last_snap_report", None) or []
        if not report:
            return
        hwnd, want, got = report[-1]
        app = (hwnd or "").split("::")[0] or "窗口"
        label = _SNAP_ZONE_LABELS.get(zone, zone)
        if got is None:
            self.status_var.set(f"吸附「{app}」到{label}失败：读不到窗口几何")
            return
        gx, gy, gw, gh = got
        tx, ty, tw, th = want
        dev = max(abs(g - t) for g, t in zip((gx, gy, gw, gh), (tx, ty, tw, th)))
        if dev <= 2:
            # 与 set_window_rect 的收敛容差一致：2px 内视为完全贴合，
            # 不用「被钳制」的文案吓用户
            self.status_var.set(f"已吸附「{app}」到{label}，与目标一致")
            return
        self.status_var.set(
            f"已吸附「{app}」到{label}：该贴的边已贴住；"
            f"应用把尺寸钳制为 {gw}×{gh}（与目标差 {dev}px）")

    def _build_window_tools(self, parent):
        f = ttk.LabelFrame(parent, text="窗口工具")
        f.pack(fill="x", padx=8, pady=6)

        # 目标窗口显式选择：自动判断（"最前面的非本程序窗口"）常猜错，
        # 例如停在 IDE 聊天窗口时点按钮会把 IDE 分屏，故改为可手动指定。
        sel = ttk.Frame(f)
        sel.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(sel, text="目标窗口:").pack(side="left")
        self.target_var = tk.StringVar(value=AUTO_TARGET)
        self.target_cb = ttk.Combobox(sel, textvariable=self.target_var,
                                      state="readonly", width=34)
        self.target_cb.pack(side="left", fill="x", expand=True, padx=4)
        self.target_cb.bind("<<ComboboxSelected>>", self._on_target_selected)
        self._tooltip(
            self.target_cb,
            "选择要操作的窗口。「自动」取上次活动窗口；同名窗口（如多个 UU远程）"
            "按 #序号 区分，可分别选中。下面所有移动/分屏按钮都作用在这里选中的窗口上。")
        rf_btn = ttk.Button(sel, text="刷新列表", command=self._refresh_targets)
        rf_btn.pack(side="left")
        self._tooltip(rf_btn, "重新枚举当前所有可选窗口（新开/关闭窗口后点这里）")
        topmost_cb = ttk.Checkbutton(sel, text="常驻置顶", variable=self.topmost,
                                     command=self._apply_topmost)
        topmost_cb.pack(side="left", padx=(8, 0))
        self._tooltip(
            topmost_cb,
            "勾选后本管理器窗口始终浮在最前，操作别的窗口时不会被盖住。")

        # activate=False：只移动窗口、不激活它，管理器才不会被挤到后面
        btns = [
            ("移到上一屏", lambda: b.move_active_to_next_monitor(-1, activate=False),
             "把目标窗口按相对位置移到上一块显示器"),
            ("移到下一屏", lambda: b.move_active_to_next_monitor(1, activate=False),
             "把目标窗口按相对位置移到下一块显示器"),
            ("左半", self._snap_btn("left"),
             "目标窗口贴到当前屏幕的左半屏"),
            ("右半", self._snap_btn("right"),
             "目标窗口贴到当前屏幕的右半屏"),
            ("上半", self._snap_btn("top"),
             "目标窗口贴到当前屏幕的上半屏"),
            ("下半", self._snap_btn("bottom"),
             "目标窗口贴到当前屏幕的下半屏"),
            ("最大化", self._snap_btn("maximize"),
             "目标窗口铺满当前屏幕的工作区"),
            ("居中", self._snap_btn("center"),
             "目标窗口在当前屏幕居中，尺寸不变"),
            ("左1/3", self._snap_btn("left-third"),
             "目标窗口占当前屏幕左三分之一"),
            ("中1/3", self._snap_btn("middle-third"),
             "目标窗口占当前屏幕中间三分之一"),
            ("右1/3", self._snap_btn("right-third"),
             "目标窗口占当前屏幕右三分之一"),
            ("左上", self._snap_btn("quad-tl"),
             "目标窗口占当前屏幕左上角四分之一"),
            ("右上", self._snap_btn("quad-tr"),
             "目标窗口占当前屏幕右上角四分之一"),
            ("左下", self._snap_btn("quad-bl"),
             "目标窗口占当前屏幕左下角四分之一"),
            ("右下", self._snap_btn("quad-br"),
             "目标窗口占当前屏幕右下角四分之一"),
        ]
        # 窗口置顶切换（当前仅 Windows 提供实现，其它平台自动隐藏入口）
        if getattr(b, "toggle_topmost", None) is not None:
            btns.append(("置顶切换", lambda: self._toggle_topmost_action(),
                         "切换目标窗口是否置顶显示"))
        # 吸附按钮两行（8 + 7），间距收到 1px；「撤销移动」并到第二行末尾。
        # 撤销原先独占一行、白占一行高；第二行 7 个按钮加撤销实测 592px，
        # 在窗口工具框的 775px 可视宽内仍有余量（缩到最小尺寸 810 也放得下）。
        snap_rows = []
        for _ in range(2):
            r = ttk.Frame(f)
            r.pack(fill="x", pady=1)
            snap_rows.append(r)
        for text, cmd, tip in btns[:8]:
            btn = ttk.Button(snap_rows[0], text=text,
                             command=self._keep_front_after(cmd))
            btn.pack(side="left", padx=1)
            self._tooltip(btn, tip)
        for text, cmd, tip in btns[8:]:
            btn = ttk.Button(snap_rows[1], text=text,
                             command=self._keep_front_after(cmd))
            btn.pack(side="left", padx=1)
            self._tooltip(btn, tip)
        undo_btn = ttk.Button(snap_rows[1], text="撤销移动",
                              command=self._undo_move)
        undo_btn.pack(side="left", padx=(8, 1))
        self._tooltip(undo_btn, "回退上一次窗口的移动/缩放（快捷键 F7）")
        back_btn = ttk.Button(snap_rows[1], text="回摆放位",
                              command=self._back_to_arranged)
        back_btn.pack(side="left", padx=(8, 1))
        self._tooltip(back_btn,
                      "把目标窗口送回上次被本程序摆好的位置"
                      "（窗口被拖乱后一键归位）")
        # 暂存 / 取回（复刻 Rectangle Pro 的 Stash）：把不用的窗口收到屏幕顶边，
        # 再按一次取回最早那个，位置尺寸原样恢复。
        stash_btn = ttk.Button(snap_rows[1], text="暂存",
                               command=self._stash_action)
        stash_btn.pack(side="left", padx=(8, 1))
        self._tooltip(stash_btn,
                      "把目标窗口收到当前屏幕顶边（只露一小条），"
                      "位置尺寸会记住，之后用「取回」还原（快捷键 ⌘⌥+S）")
        unstash_btn = ttk.Button(snap_rows[1], text="取回",
                                 command=self._unstash_action)
        unstash_btn.pack(side="left", padx=1)
        self._tooltip(unstash_btn,
                      "取回最早暂存的窗口到它原来的位置（快捷键 ⌘⌥+D）")
        # 窗口间距：吸附/排列时窗口之间、窗口与屏幕边之间留出缝隙。
        # 借鉴 Rectangle Pro 的 GapCalculation：间距一是让四格清清楚楚分开，
        # 二是吸收 App 最小尺寸把窗口顶大时的溢出，避免立刻越界被搬屏。
        gap_row = ttk.Frame(f)
        gap_row.pack(fill="x", padx=6, pady=(1, 1))
        ttk.Label(gap_row, text="间距:").pack(side="left")
        self.window_gap_var = tk.IntVar(
            value=int(settings.load().get("window_gap", 0) or 0))
        gap_spin = ttk.Spinbox(gap_row, from_=0, to=60, width=4,
                               textvariable=self.window_gap_var,
                               command=self._on_gap_change)
        gap_spin.pack(side="left", padx=(4, 2))
        gap_spin.bind("<FocusOut>", lambda _e: self._on_gap_change())
        gap_spin.bind("<Return>", lambda _e: self._on_gap_change())
        self._tooltip(gap_spin,
                      "窗口之间的可见缝隙（像素）。0 = 不留缝，与以前一致。"
                      "每个窗口在相应方向让出一半，两边凑成一个完整缝隙。")
        self.gap_edges_var = tk.BooleanVar(
            value=bool(settings.load().get("gap_screen_edges", False)))
        gap_edges_cb = ttk.Checkbutton(
            gap_row, text="屏幕边也留", variable=self.gap_edges_var,
            command=self._on_gap_change)
        gap_edges_cb.pack(side="left", padx=(4, 0))
        self._tooltip(gap_edges_cb,
                      "勾选后窗口与屏幕四边之间也留出间距；不勾则四格仍贴住屏幕边，"
                      "只在窗口之间留缝。")
        # 相邻窗口联动（复刻 Rectangle Pro 的 adjustAdjacentWindows）：
        # 吸附时若窗口左/上缘移动，相邻窗口自动让位，不会互相压盖。
        self.adjacent_var = tk.BooleanVar(
            value=bool(settings.load().get("adjacent_resize", False)))
        adj_cb = ttk.Checkbutton(gap_row, text="联动相邻",
                                 variable=self.adjacent_var,
                                 command=self._on_adjacent_change)
        adj_cb.pack(side="left", padx=(8, 0))
        self._tooltip(adj_cb,
                      "吸附时若窗口左/上缘移动，真正相邻的窗口自动让位"
                      "（不会互相压盖，也不留缝）。默认关闭。")

        # 移到指定屏：只有"上一屏/下一屏"时，三屏以上要连点好几次才能到位，
        # 这里直接选目标屏一次跳过去（下拉与并排/竖排共用同一份显示器列表）。
        mon_row = ttk.Frame(f)
        mon_row.pack(fill="x", padx=6, pady=(2, 1))
        ttk.Label(mon_row, text="指定屏:").pack(side="left")
        self.move_mon_var = tk.StringVar(value=AUTO_MON)
        self.move_mon_cb = ttk.Combobox(mon_row, textvariable=self.move_mon_var,
                                        state="readonly", width=13)
        self.move_mon_cb.pack(side="left", padx=(4, 2))
        self._tooltip(
            self.move_mon_cb,
            "选择目标显示器。「自动」按窗口当前所在屏；要跨屏一次跳到位时，"
            "在这里选定具体某块屏，再点右侧「移到该屏」。")
        mv_btn = ttk.Button(mon_row, text="移到该屏",
                            command=self._keep_front_after(
                                self._move_to_selected_monitor))
        mv_btn.pack(side="left")
        self._tooltip(mv_btn, "把目标窗口一次移动到左侧下拉框选中的显示器")

        # 并排左右：可选目标显示器 + 可指定左右两个窗口（留"自动"则由程序取最前面两个）
        sbs = ttk.Frame(f)
        sbs.pack(fill="x", padx=6, pady=(1, 1))
        ttk.Label(sbs, text="并排:").pack(side="left")
        self.sbs_mon_var = tk.StringVar(value=AUTO_MON)
        # 下拉宽度按 120 DPI 下的实际像素收窄：原先并排/竖排整行会超出窗口，
        # 最右侧按钮被横向截断（画布只做纵向滚动，横向溢出无法补救）。
        self.sbs_mon_cb = ttk.Combobox(sbs, textvariable=self.sbs_mon_var,
                                       state="readonly", width=13)
        self.sbs_mon_cb.pack(side="left", padx=(4, 2))
        self._tooltip(self.sbs_mon_cb, "并排到哪块屏。「自动」按左窗口当前所在屏。")
        self.sbs_left_var = tk.StringVar(value=AUTO_TARGET)
        self.sbs_right_var = tk.StringVar(value=AUTO_TARGET)
        ttk.Label(sbs, text="左").pack(side="left", padx=(8, 2))
        self.sbs_left_cb = ttk.Combobox(sbs, textvariable=self.sbs_left_var,
                                        state="readonly", width=11)
        self.sbs_left_cb.pack(side="left")
        self._tooltip(self.sbs_left_cb, "并排的左窗口。竖屏时改作上半屏。")
        ttk.Label(sbs, text="右").pack(side="left", padx=(8, 2))
        self.sbs_right_cb = ttk.Combobox(sbs, textvariable=self.sbs_right_var,
                                         state="readonly", width=11)
        self.sbs_right_cb.pack(side="left")
        self._tooltip(self.sbs_right_cb, "并排的右窗口。竖屏时改作下半屏。")
        sbs_btn = ttk.Button(sbs, text="并排左右",
                             command=self._snap_two_side_by_side)
        sbs_btn.pack(side="left", padx=(10, 0))
        self._tooltip(
            sbs_btn,
            "把「左」「右」两个窗口并排到同一屏。竖屏上自动改成上/下各半屏堆叠。"
            "留「自动」时取最前面的两个窗口。")

        # 竖排上中下：可选目标显示器 + 可指定上/中/下三个窗口（留"自动"则由程序取最前面三个）
        stack = ttk.Frame(f)
        stack.pack(fill="x", padx=6, pady=(1, 1))
        ttk.Label(stack, text="竖排:").pack(side="left")
        self.stack_mon_var = tk.StringVar(value=AUTO_MON)
        self.stack_mon_cb = ttk.Combobox(stack, textvariable=self.stack_mon_var,
                                         state="readonly", width=7)
        self.stack_mon_cb.pack(side="left", padx=(4, 2))
        self._tooltip(self.stack_mon_cb, "竖排到哪块屏。「自动」按上窗口当前所在屏。")
        self.stack_top_var = tk.StringVar(value=AUTO_TARGET)
        self.stack_mid_var = tk.StringVar(value=AUTO_TARGET)
        self.stack_bot_var = tk.StringVar(value=AUTO_TARGET)
        ttk.Label(stack, text="上").pack(side="left", padx=(8, 2))
        self.stack_top_cb = ttk.Combobox(stack, textvariable=self.stack_top_var,
                                         state="readonly", width=11)
        self.stack_top_cb.pack(side="left")
        self._tooltip(self.stack_top_cb, "竖排最上面的窗口。同名窗口按 #序号 选。")
        ttk.Label(stack, text="中").pack(side="left", padx=(6, 2))
        self.stack_mid_cb = ttk.Combobox(stack, textvariable=self.stack_mid_var,
                                         state="readonly", width=11)
        self.stack_mid_cb.pack(side="left")
        self._tooltip(self.stack_mid_cb, "竖排中间的窗口。同名窗口按 #序号 选。")
        ttk.Label(stack, text="下").pack(side="left", padx=(6, 2))
        self.stack_bot_cb = ttk.Combobox(stack, textvariable=self.stack_bot_var,
                                         state="readonly", width=11)
        self.stack_bot_cb.pack(side="left")
        self._tooltip(self.stack_bot_cb, "竖排最下面的窗口。同名窗口按 #序号 选。")
        stack_btn = ttk.Button(stack, text="竖排上中下",
                               command=self._snap_three_stack)
        stack_btn.pack(side="left", padx=(10, 0))
        self._tooltip(
            stack_btn,
            "把「上」「中」「下」三个窗口竖向堆叠到同一屏，各占三分之一。"
            "同名窗口（如多个 UU远程）请在下拉框按 #序号 选不同窗口。")

        # 四宫格 2×2：四个窗口一次排满一屏（左上/右上/左下/右下）。
        # 分两行排：第 1 行 = 四个方位下拉 + 排列按钮 + 智能开关；
        # 第 2 行 = 目标屏下拉 + 2×2 点格网格 + 操作提示。
        #
        # 原先四个下拉被塞进一个硬写 390px 宽的容器里（为了省行宽），而 Tk
        # 的 packer 在容器装不下时**不会映射**后面的子控件——真机实测只有前
        # 三个下拉可见可点，右下角那个 x=0 w=1 根本没被映射，用户永远选不到
        # 第四个窗口；第 4 格于是由程序随便补一个「最靠前的未占用窗口」，
        # 这正是「4 格模式只能选三个窗口」且「智能 / 非智能都不准」的主因。
        # 改成按自然宽度分两行后，行宽实测 720px / 583px，都在 793px 可视宽内。
        quad = ttk.Frame(f)
        quad.pack(fill="x", padx=6, pady=(1, 1))

        quad_row = ttk.Frame(quad)
        quad_row.pack(fill="x")
        ttk.Label(quad_row, text="四格:").pack(side="left")
        self.quad_tl_var = tk.StringVar(value=AUTO_TARGET)
        self.quad_tr_var = tk.StringVar(value=AUTO_TARGET)
        self.quad_bl_var = tk.StringVar(value=AUTO_TARGET)
        self.quad_br_var = tk.StringVar(value=AUTO_TARGET)
        self.quad_smart_var = tk.BooleanVar(value=False)
        # 四个下拉不重复写「左上/右上/左下/右下」文字标签：下方 2×2 网格本身
        # 就是方位图，格子与下拉按 TL/TR/BL/BR 同序对应（tooltip 里说明）。
        for key, var, pos in (
            ("tl", self.quad_tl_var, "左上"),
            ("tr", self.quad_tr_var, "右上"),
            ("bl", self.quad_bl_var, "左下"),
            ("br", self.quad_br_var, "右下"),
        ):
            cb = ttk.Combobox(quad_row, textvariable=var, state="readonly",
                              width=11)
            cb.pack(side="left", padx=(4, 0))
            self._tooltip(cb, f"四宫格{pos}角的窗口。自动 = 取最前面的未占用窗口。")
            setattr(self, "quad_%s_cb" % key, cb)
        quad_btn = ttk.Button(quad_row, text="2×2 排列",
                              command=self._snap_four_quad)
        quad_btn.pack(side="left", padx=(6, 0))
        self._tooltip(
            quad_btn,
            "把四个窗口按 2×2 四宫格排到同一屏（即上下左右四分屏）。"
            "留「自动」时取最前面的四个窗口。")
        # 智能分配：用户只管选哪四个窗口，谁去哪个角、用什么比例由程序算。
        # 判据是各窗口的宽高比——把窗口放进宽高比最接近的格子，反之会被
        # App 的最小尺寸钳制（真机 ChatGPT 请求高 448 实测 600）。
        smart_cb = ttk.Checkbutton(
            quad_row, text="智能", variable=self.quad_smart_var,
            command=self._on_quad_smart_toggle)
        smart_cb.pack(side="left", padx=(6, 0))
        self._tooltip(
            smart_cb,
            "勾选后不用管四个下拉的顺序：程序按每个窗口的宽高比，"
            "决定谁去哪个角、左右上下怎么切，让每格都贴合窗口。"
            "下拉此时只表示「参与排列的四个窗口」。")

        # 第 2 行：目标屏 + 四宫格可视化。
        quad_row2 = ttk.Frame(quad)
        quad_row2.pack(fill="x", pady=(1, 0))
        ttk.Label(quad_row2, text="屏:").pack(side="left")
        self.quad_mon_var = tk.StringVar(value=AUTO_MON)
        self.quad_mon_cb = ttk.Combobox(quad_row2,
                                        textvariable=self.quad_mon_var,
                                        state="readonly", width=13)
        self.quad_mon_cb.pack(side="left", padx=(4, 2))
        self._tooltip(self.quad_mon_cb,
                      "四宫格排到哪块屏。「自动」按左上窗口当前所在屏。")

        # 把「左上/右上/左下/右下」四个下拉对应到一块 2×2 网格，
        # 点格子直接填/清空那一格，不用记住下拉框和方位的对应关系。
        # 两条分割线可拖拽改比例——四等分并不总是合适（左栏放终端想窄一点、
        # 右栏放浏览器想宽一点），拖动时实时重画，比例随「2×2 排列」一起生效。
        self.quad_grid_canvas = tk.Canvas(
            quad_row2, width=self._quad_grid_w, height=self._quad_grid_h,
            bg="#fafafa", relief="sunken", borderwidth=1, highlightthickness=0)
        # 必须 pack：画布只 create_* 而不 pack 就永远不显示，网格、点格、
        # 分割线全都看不见（这正是「新功能没有渲染出来」的原因）。
        self.quad_grid_canvas.pack(side="left", padx=(4, 4))
        self._quad_split_x = tk.DoubleVar(value=0.5)
        self._quad_split_y = tk.DoubleVar(value=0.5)
        self._quad_drag = None
        self._quad_cells = {}
        self.quad_grid_canvas.bind("<Button-1>", self._on_quad_grid_press)
        self.quad_grid_canvas.bind("<B1-Motion>", self._on_quad_grid_drag)
        self.quad_grid_canvas.bind("<ButtonRelease-1>", self._on_quad_grid_release)
        for key in self._QUAD_KEYS:
            rid = self.quad_grid_canvas.create_rectangle(
                0, 0, 1, 1, fill="#e9e9e9", outline="#5a5a5a", width=1.2,
                activefill="#d0d0d0")
            self.quad_grid_canvas.tag_bind(
                rid, "<Button-1>",
                lambda e, k=key: self._on_quad_cell_click(k))
            self._quad_cells[key] = rid
        self._quad_labels = {}
        for key in self._QUAD_KEYS:
            self._quad_labels[key] = self.quad_grid_canvas.create_text(
                0, 0, text="空", font=("Helvetica", 7), fill="#333")
        # 分割线一横一竖 + 命中加粗：命中区宽 4px，方便鼠标点中
        self._quad_vline = self.quad_grid_canvas.create_line(
            0, 0, 0, 0, fill="#3b6ea5", width=1)
        self._quad_hline = self.quad_grid_canvas.create_line(
            0, 0, 0, 0, fill="#3b6ea5", width=1)
        self._quad_vhit = self.quad_grid_canvas.create_rectangle(
            0, 0, 0, 0, fill="", outline="")
        self._quad_hhit = self.quad_grid_canvas.create_rectangle(
            0, 0, 0, 0, fill="", outline="")
        self._quad_title_id = self.quad_grid_canvas.create_text(
            self._quad_grid_w // 2, 1, text="",
            font=("Helvetica", 6), fill="#777", anchor="n")
        self.quad_grid_canvas.bind("<Configure>", self._on_quad_grid_resize)
        self._refresh_quad_grid()
        self._tooltip(self.quad_grid_canvas,
                      "点格子 = 把最靠前的未占用窗口放进该方位，"
                      "再点一次 = 清空该方位（与左侧下拉框联动）。")
        _quad_hint = ttk.Label(
            quad_row2,
            text="点格=填/清空 · 拖分割线=改比例",
            font=("Helvetica", 8))
        _quad_hint.pack(side="left")
        self._tooltip(_quad_hint, "拖两条分割线可改左右/上下比例，"
                      "四等分不总是合适——比例随「2×2 排列」一起生效。")

        # 跨屏铺满：若干窗口分别铺满各自勾选的屏（一个窗口一块屏）。
        # 模式、按钮与屏勾选框同排一行：勾选框按屏数动态重建，宽度实测
        # 743px（模式下拉用短文案换来的空间），在窗口 820 时 775px 的可视
        # 宽内、缩到最小尺寸 810 时 765px 也放得下。
        #
        # 注意 _rebuild_spread_checks() 只删自己记下的勾选框控件：早先它
        # 直接 destroy 父 Frame 的**全部**子控件，而「跨屏铺满:」标签、模式
        # 下拉和「铺所选屏」按钮就在同一个 Frame 里——于是每次
        # refresh_monitors() 都把这一行整行删掉，界面上只剩勾选框，这才是
        # 「跨屏铺满的选项没有出现」的真因（与四宫格那次的 packer 不映射不同）。
        spread = ttk.Frame(f)
        spread.pack(fill="x", padx=6, pady=(1, 1))
        ttk.Label(spread, text="跨屏铺满:").pack(side="left")
        self._spread_checks_parent = spread
        self._last_monitors = list(self.monitors)
        self.spread_mode_var = tk.StringVar(value=SPREAD_MODE_MAX)
        self.spread_mode_cb = ttk.Combobox(
            spread, textvariable=self.spread_mode_var, state="readonly",
            width=9, values=[SPREAD_MODE_MAX, SPREAD_MODE_QUAD])
        self.spread_mode_cb.pack(side="left", padx=(4, 2))
        self._tooltip(self.spread_mode_cb,
                     "铺到每块屏的方式：整屏铺满工作区，或只占左上四分之一。")
        spread_btn = ttk.Button(spread, text="铺所选屏",
                                command=self._fill_all_monitors)
        spread_btn.pack(side="left", padx=(6, 0))
        self._tooltip(
            spread_btn,
            "把窗口依次铺到勾选的屏，第 1 个窗口去第 1 块勾选的屏、依此类推。"
            "窗口比勾选的屏少时有几屏铺几屏，多出来的屏不动。")
        # 勾选框按当前屏数生成（屏数为 1 时跨屏铺满等价于最大化，故不显示）
        self._rebuild_spread_checks()

    def _on_gap_change(self):
        """窗口间距改动即存盘：吸附/排列函数每次都会重新读设置。"""
        try:
            gap = int(self.window_gap_var.get())
        except Exception:  # noqa: BLE001
            gap = 0
        s = settings.load()
        s["window_gap"] = max(0, min(60, gap))
        s["gap_screen_edges"] = bool(self.gap_edges_var.get())
        settings.save(s)

    def _on_adjacent_change(self):
        """相邻窗口联动开关即存盘。"""
        s = settings.load()
        s["adjacent_resize"] = bool(self.adjacent_var.get())
        settings.save(s)
        self.status_var.set(
            "相邻窗口联动已" + ("开启" if self.adjacent_var.get() else "关闭"))

    def _refresh_targets(self):
        """刷新可选窗口列表，尽量保留当前选择。"""
        try:
            wins = b.list_target_windows()
        except Exception:  # noqa: BLE001
            wins = []
        labels = [AUTO_TARGET]
        self._target_map = {AUTO_TARGET: None}
        for w in wins:
            if w["label"] in self._target_map:
                continue
            labels.append(w["label"])
            self._target_map[w["label"]] = w["hwnd"]
        self.target_cb["values"] = labels
        # 四宫格那四个下拉单独用短标签：完整标签形如
        # 「ChatGPT — ChatGPT  (1512×895)」，Combobox 会按最长项把自己
        # 撑到 121px/个，四个加网格和按钮整行溢出可视区（这是四宫格选项
        # 在界面里看不见的直接原因）。压缩成「#1 应用名」后宽度可控，
        # 也与网格里显示的文字一致。
        short = [AUTO_TARGET]
        known = [w for w in wins if w["label"] in self._target_map]
        short, self._quad_short_map, self._quad_hwnd_short, \
            self._quad_short_rev = self.build_quad_short_labels(known)
        # 并排窗口下拉与目标窗口共用同一份列表（含"自动"）
        if hasattr(self, "sbs_left_cb"):
            for cb, var in ((self.sbs_left_cb, self.sbs_left_var),
                            (self.sbs_right_cb, self.sbs_right_var),
                            (self.stack_top_cb, self.stack_top_var),
                            (self.stack_mid_cb, self.stack_mid_var),
                            (self.stack_bot_cb, self.stack_bot_var),
                            ):
                cb["values"] = labels
                if var.get() and var.get() not in self._target_map:
                    var.set(AUTO_TARGET)
        if hasattr(self, "quad_tl_cb"):
            short_map = self._quad_short_map
            for cb, var in ((self.quad_tl_cb, self.quad_tl_var),
                            (self.quad_tr_cb, self.quad_tr_var),
                            (self.quad_bl_cb, self.quad_bl_var),
                            (self.quad_br_cb, self.quad_br_var)):
                cb["values"] = short
                # 现存的可能是短标签（自己写的）或完整标签（别处写的），
                # 都要归一到短标签，否则下面按 short_map 查不到会掉成「自动」
                raw = var.get()
                cur = raw if raw in short_map else self._quad_short_rev.get(raw)
                if cur not in short_map:
                    cur = AUTO_TARGET
                var.set(cur)
        # 窗口列表变了：网格上显示的窗口名可能已失效，按新选择重画
        if hasattr(self, "quad_grid_canvas"):
            self._refresh_quad_grid()
        if self.target_var.get() not in self._target_map:
            self.target_var.set(AUTO_TARGET)
            b.set_target(None)
        self._refresh_trigger_pattern_candidates(wins)

    def _refresh_trigger_pattern_candidates(self, wins=None):
        """刷新触发器「匹配文字」下拉的候选：按匹配依据列出当前应用名或窗口标题。"""
        cb = getattr(self, "trig_pattern_cb", None)
        if cb is None:
            return
        if wins is None:
            try:
                wins = b.list_target_windows()
            except Exception:  # noqa: BLE001
                wins = []
        field = getattr(self, "trig_field_var", None)
        by_title = field is not None and field.get() == "title"
        seen, vals = set(), []
        for w in wins:
            v = (w.get("name") or "") if by_title else (w.get("owner") or "")
            v = v.strip()
            if v and v not in seen:
                seen.add(v)
                vals.append(v)
        cb["values"] = vals

    def _refresh_monitor_choices(self):
        """刷新并排/竖排所用的显示器下拉（含"自动"），尽量保留当前选择。"""
        labels = [AUTO_MON]
        self._monitor_map = {AUTO_MON: None}
        for i, m in enumerate(self.monitors):
            label = (f"显示器{i + 1}"
                     + ("（主屏）" if m.is_primary else "")
                     + f" {self._monitor_display_name(m)}"
                     + f" {m.width}x{m.height}")
            labels.append(label)
            self._monitor_map[label] = i
        for cb, var in ((self.move_mon_cb, self.move_mon_var),
                        (self.sbs_mon_cb, self.sbs_mon_var),
                        (self.stack_mon_cb, self.stack_mon_var),
                        (self.quad_mon_cb, self.quad_mon_var)):
            cb["values"] = labels
            if var.get() not in self._monitor_map:
                var.set(AUTO_MON)
        # 跨屏铺满的勾选框按 device_path 保留勾选（显示器增减后仍对得上）
        self._rebuild_spread_checks()

    def _rebuild_spread_checks(self):
        """重建跨屏铺满的屏勾选框，尽量保留原有勾选。

        热插拔会改变屏的数量与顺序，勾选框是构建期建的静态控件，
        这里按 device_path 迁移旧状态，再对新屏按「横屏勾、竖屏不勾」给默认值。
        """
        parent = getattr(self, "_spread_checks_parent", None)
        if parent is None:
            return
        old = {}
        for i, var in getattr(self, "_spread_checks", []):
            if i < len(self._last_monitors):
                old[self._last_monitors[i].device_path] = bool(var.get())
        for child in list(getattr(self, "_spread_check_widgets", [])):
            try:
                child.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._spread_check_widgets = []
        self._spread_checks = []
        # 单屏时跨屏铺满等价于最大化，没有可选项，勾选区整个不显示
        if len(self.monitors) < 2:
            self._last_monitors = list(self.monitors)
            return
        head = ttk.Label(parent, text="屏:")
        head.pack(side="left", padx=(6, 2))
        self._spread_check_widgets.append(head)
        for i, m in enumerate(self.monitors):
            portrait = m.height > m.width
            if m.device_path in old:
                val = old[m.device_path]
            else:
                val = not portrait
            var = tk.BooleanVar(value=val)
            label = (f"{i + 1}"
                     + ("★" if m.is_primary else "")
                     + ("竖" if portrait else "横")
                     + f" {m.width}x{m.height}")
            cb = ttk.Checkbutton(parent, text=label, variable=var,
                                 command=self._on_spread_check)
            cb.pack(side="left", padx=(1, 0))
            self._tooltip(
                cb,
                f"勾选则把窗口铺到这块屏（{self._monitor_display_name(m)}）。"
                "竖屏上的横屏应用会被压扁，默认不勾。")
            self._spread_checks.append((i, var))
            self._spread_check_widgets.append(cb)
        self._last_monitors = list(self.monitors)

    def _move_to_selected_monitor(self):
        """把目标/活动窗口一次移动到下拉框选中的显示器。"""
        idx = self._monitor_map.get(self.move_mon_var.get())
        if idx is None:
            self.status_var.set("请先在下拉框选择目标显示器（「自动」无法一次定位）")
            return
        try:
            ok = b.move_target_to_monitor(idx, activate=False)
        except Exception as e:  # noqa: BLE001
            self.status_var.set(f"移动失败：{e}")
            return
        if ok:
            self.status_var.set(f"窗口已移到 {self.move_mon_var.get()}")
        else:
            self.status_var.set("移动失败：未找到可操作窗口")

    def _on_target_selected(self, _event=None):
        label = self.target_var.get()
        b.set_target(self._target_map.get(label))
        self.status_var.set(f"目标窗口: {label}")

    def _snap_two_side_by_side(self):
        """按选择的左右窗口并排；选"自动"则由程序取最前面两个窗口。

        并排后两个窗口需要显示到最前面，故临时取消主窗口置顶（若开启），
        让出 z 序给被并排的窗口，不再沿用其它按钮的 _keep_front_after。
        """
        if self.topmost.get():
            try:
                self.root.attributes("-topmost", False)
            except Exception:  # noqa: BLE001
                pass
        left = self._target_map.get(self.sbs_left_var.get())
        right = self._target_map.get(self.sbs_right_var.get())
        if left and right and left == right:
            messagebox.showwarning("提示", "左右窗口不能选同一个")
            return
        mon_idx = self._monitor_map.get(self.sbs_mon_var.get())
        mon_label = self.sbs_mon_var.get()
        if b.snap_two_side_by_side(left_hwnd=left, right_hwnd=right, monitor=mon_idx):
            # 竖屏上自动变成上下堆叠，状态栏如实反映
            mon = (self.monitors[mon_idx]
                   if mon_idx is not None and mon_idx < len(self.monitors) else None)
            if mon is not None and mon.height > mon.width:
                self.status_var.set(f"竖屏已上下堆叠（{mon_label}）")
            else:
                self.status_var.set("已并排左右" + (f"（{mon_label}）" if mon_idx is not None else ""))
        else:
            self.status_var.set("并排失败：需要至少两个可操作窗口")

    def _snap_three_stack(self):
        """按选择的上/中/下窗口竖排；选"自动"则由程序取最前面三个窗口。

        排列后三个窗口需要显示到最前面，故临时取消主窗口置顶（若开启），
        让出 z 序给被排列的窗口，不再沿用其它按钮的 _keep_front_after。
        """
        if self.topmost.get():
            try:
                self.root.attributes("-topmost", False)
            except Exception:  # noqa: BLE001
                pass
        top = self._target_map.get(self.stack_top_var.get())
        mid = self._target_map.get(self.stack_mid_var.get())
        bot = self._target_map.get(self.stack_bot_var.get())
        chosen = [x for x in (top, mid, bot) if x]
        if len(set(chosen)) < len(chosen):
            messagebox.showwarning("提示", "上/中/下窗口不能重复选择")
            return
        mon_idx = self._monitor_map.get(self.stack_mon_var.get())
        mon_label = self.stack_mon_var.get()
        if b.snap_three_stack(top_hwnd=top, mid_hwnd=mid, bot_hwnd=bot, monitor=mon_idx):
            self.status_var.set("已竖排上中下" + (f"（{mon_label}）" if mon_idx is not None else ""))
        else:
            self.status_var.set("竖排失败：需要至少三个可操作窗口")

    def _snap_four_quad(self):
        """按选择的左上/右上/左下/右下窗口做 2×2 四宫格排列。

       排列后四个窗口需要显示到最前面，故临时取消主窗口置顶（若开启），
       与并排 / 竖排一致，不沿用 _keep_front_after。
        """
        if self.topmost.get():
            try:
                self.root.attributes("-topmost", False)
            except Exception:  # noqa: BLE001
                pass
        quad_vars = (self.quad_tl_var, self.quad_tr_var,
                     self.quad_bl_var, self.quad_br_var)
        # 四宫格下拉存的是短标签，经 _quad_short_map 换回 hwnd
        short_map = getattr(self, "_quad_short_map", {AUTO_TARGET: None})
        chosen = [short_map.get(v.get()) for v in quad_vars]
        smart = bool(self.quad_smart_var.get())
        filled = [x for x in chosen if x]
        if len(set(filled)) < len(filled):
            messagebox.showwarning("提示", "四宫格四个窗口不能重复选择")
            return
        mon_idx = self._monitor_map.get(self.quad_mon_var.get())
        mon_label = self.quad_mon_var.get()
        if b.snap_four_quad(tl_hwnd=chosen[0], tr_hwnd=chosen[1],
                            bl_hwnd=chosen[2], br_hwnd=chosen[3],
                            monitor=mon_idx,
                            split_x=self._quad_split_x.get(),
                            split_y=self._quad_split_y.get(),
                            smart=smart):
            bm = self._quad_backend_mod()
            # 比例同步：智能模式下程序自己定了比例，把它同步回分割线，
            # 网格显示才与实际排出来的一致
            sx2, sy2 = getattr(bm, "_last_quad_splits", (None, None))
            if sx2 is not None:
                self._quad_split_x.set(sx2)
                self._quad_split_y.set(sy2)
            # 把**实际分配**写回四个下拉/网格。智能模式会按窗口宽高比重排
            # 四个角的归属，若界面仍显示用户排的顺序，看到的就是「不准」；
            # 写回实测归属后，下次再点「2×2 排列」也是确定性的同一结果，
            # 不会再由程序随机补一个「最靠前的未占用窗口」。
            order = getattr(bm, "_last_quad_order", None) or []
            rev = getattr(self, "_quad_hwnd_short", {})
            names = []
            if len(order) == 4 and all(order):
                for h in order:
                    nm = rev.get(h)
                    names.append(nm if nm in short_map else AUTO_TARGET)
                for v, nm in zip(quad_vars, names):
                    v.set(nm)
            self._refresh_quad_grid()
            assigned = "、".join(f"{k}={v}" for k, v in
                              zip(("左上", "右上", "左下", "右下"), names)
                              if v and v != AUTO_TARGET)
            head = "已 2×2 四宫格排列"
            if mon_idx is not None:
                head += f"（{mon_label}）"
            head += (f" 左{self._quad_split_x.get():.0%}/"
                     f"上{self._quad_split_y.get():.0%}")
            if assigned:
                head += " · " + assigned
            # 逐格实测 vs 目标：App 有最小窗口尺寸时某一格缩不到目标值，
            # 呈现为「不准」，但那是应用侧钳制、不是坐标算错。如实报出来。
            # 「写入无效」与「被最小尺寸钳制」必须分开说：前者是窗口一步都没
            # 动（App 不开放自动化 / 被系统钉住，真机 CrossPaste、Sidecar 上的
            # UU远程），后者是动了但被 App 的最小尺寸顶到目标值之外。混在一起
            # 报，用户会以为是自己应用的最小尺寸太大。
            dead = set(getattr(bm, "_last_quad_unwritable", None) or [])
            clamped = []
            for hwnd, want, got in (getattr(bm, "_last_quad_report", None)
                                    or []):
                if got is None:
                    clamped.append("读不到")
                    continue
                if hwnd in dead:
                    continue
                dev = max(abs(g - t) for g, t in zip(got, want))
                if dev > 8:
                    app = (hwnd or "").split("::")[0]
                    clamped.append(f"{app}差{dev}px")
            # 后端如实记录的异常情况（屏装不下四个最小尺寸、某窗口被系统判给
            # 了别的屏）。「四个窗口不在一个屏幕上」是根因级问题，必须报出来，
            # 不能只靠「被最小尺寸钳制」这么说——那是现象不是原因。
            notes = [n for n in (getattr(bm, "_last_quad_notes", None) or []) if n]
            parts = [head]
            if notes:
                parts.append("｜ " + "；".join(notes[:2]))
            if dead:
                names = "、".join((h or "").split("::")[0]
                                 for h in list(dead)[:4])
                parts.append("｜ 写入无效（不开放自动化/被系统钉住）：" + names)
            if clamped:
                parts.append("｜ 被应用最小尺寸钳制：" + "、".join(clamped[:4]))
            self.status_var.set(" ".join(parts))
        else:
            self.status_var.set("四宫格失败：至少需要两个可操作窗口"
                                "（网格里点掉几格可以只排两三个）")

    # ---------- 四宫格可视化 ----------
    _QUAD_KEYS = ("TL", "TR", "BL", "BR")
    # 网格画布尺寸：2×2 网格要能放得下短标签（每格约 95×50，够放
    # 「Chrome」「CodeBuddy」这类 11 字内的短名），也留出拖分割线的余量。
    _quad_grid_w = 200
    _quad_grid_h = 104

    @staticmethod
    def _quad_backend_mod():
        """当前平台真正干活的窗口模块（mac 与 Windows 的模块名不同）。"""
        try:
            import windows_mac as wm
            return wm
        except ImportError:  # noqa: BLE001
            import windows as ww
            return ww

    def _quad_var(self, key):
        return {"TL": self.quad_tl_var, "TR": self.quad_tr_var,
                "BL": self.quad_bl_var, "BR": self.quad_br_var}[key]

    @staticmethod
    def _quad_key_names():
        return {"TL": "左上", "TR": "右上", "BL": "左下", "BR": "右下"}

    def _on_quad_cell_click(self, key):
        """点格子：未填则把最靠前的未占用窗口放进该方位，已填则清空。

        与下拉框共用同一批 StringVar，这里改完下拉框同步跟着变，
        「2×2 排列」按钮读的还是同一份状态。
        """
        var = self._quad_var(key)
        cur = var.get()
        if cur and cur != AUTO_TARGET:
            var.set(AUTO_TARGET)          # 再点一次 = 清空该方位
            self._refresh_quad_grid()
            return
        taken = set()
        for k in self._QUAD_KEYS:
            v = self._quad_var(k).get()
            if v and v != AUTO_TARGET:
                taken.add(v)
        # 按界面窗口列表顺序取第一个未被其它方位占用的窗口
        pick = None
        for short in getattr(self, "_quad_short_map", {}):
            if short == AUTO_TARGET or short in taken:
                continue
            pick = short
            break
        if pick is None:
            self.status_var.set("没有更多窗口可填了（先清空某一格）")
            return
        var.set(pick)
        self._refresh_quad_grid()
        self.status_var.set(f"{self._quad_key_names()[key]} ← {pick}")

    def _refresh_quad_grid(self):
        """按比例与四个方位的当前选择重画网格：格子、分割线、文字一起更新。"""
        cv = getattr(self, "quad_grid_canvas", None)
        if cv is None:
            return
        # 0.05~0.95：App 的最小尺寸会把某一排顶到 85% 以上（真机 ChatGPT
        # 最小高 600），按 0.85 钳会让画出来的分割线与实际排出来的位置不符
        # 上限放到 1.0：放不下四个窗口时剩下的一排会撑满整高，比例报 100%，
        # 横向分割线贴在底边（看起来就是两列铺满）。按 0.95 钳会多画一条线。
        sx = min(max(self._quad_split_x.get(), 0.05), 1.0)
        sy = min(max(self._quad_split_y.get(), 0.05), 1.0)
        cw, ch = cv.winfo_width(), cv.winfo_height()
        if cw < 4 or ch < 4:                # 还没布局完，取请求尺寸兜底
            cw, ch = self._quad_grid_w, self._quad_grid_h
        # 顶部留给「已填 n/4」小标题，正文从 10px 开始
        top = 10
        col_x = int(round(cw * sx))
        row_y = top + int(round((ch - top) * sy))
        boxes = {
            "TL": (0, top, col_x, row_y),
            "TR": (col_x, top, cw, row_y),
            "BL": (0, row_y, col_x, ch),
            "BR": (col_x, row_y, cw, ch),
        }
        for key in self._QUAD_KEYS:
            val = self._quad_var(key).get()
            filled = bool(val) and val != AUTO_TARGET
            rid = self._quad_cells.get(key)
            if rid is not None:
                x0, y0, x1, y1 = boxes[key]
                cv.coords(rid, x0 + 1, y0 + 1, x1, y1)
                cv.itemconfigure(rid,
                                 fill="#d8e6ff" if filled else "#e9e9e9")
            lid = self._quad_labels.get(key)
            if lid is not None:
                x0, y0, x1, y1 = boxes[key]
                cv.coords(lid, (x0 + x1) // 2, (y0 + y1) // 2)
                cv.itemconfigure(lid,
                                 text=(self._short_label(val) if filled else "空"),
                                 fill="#1a3d6d" if filled else "#999")
        # 分割线贴在格子边界上，命中区加宽到 4px 方便鼠标抓
        if getattr(self, "_quad_vline", None) is not None:
            cv.coords(self._quad_vline, col_x, top, col_x, ch)
            cv.coords(self._quad_vhit, col_x - 2, top, col_x + 2, ch)
        if getattr(self, "_quad_hline", None) is not None:
            cv.coords(self._quad_hline, 0, row_y, cw, row_y)
            cv.coords(self._quad_hhit, 0, row_y - 2, cw, row_y + 2)
        n = sum(1 for k in self._QUAD_KEYS
                if self._quad_var(k).get() not in ("", AUTO_TARGET, None))
        cv.itemconfigure(self._quad_title_id,
                         text=f"已填 {n}/4 · {sx:.0%}/{sy:.0%}")

    # ---------- 四宫格分割线拖拽 ----------
    def _quad_canvas_size(self):
        """网格画布的实际可用尺寸。

        命中判断与拖拽必须按实际尺寸算：画布尺寸一变（改常量、拉伸窗口）
        还拿旧常量比对，分割线就会与鼠标位置错开，既点不中也拖不动。
        还没布局完时退回请求尺寸。
        """
        cv = self.quad_grid_canvas
        cw, ch = cv.winfo_width(), cv.winfo_height()
        if cw < 4 or ch < 4:
            cw, ch = self._quad_grid_w, self._quad_grid_h
        return cw, ch

    def _quad_hit_which(self, event):
        """判断鼠标落在哪条分割线的命中区里；不在命中区返回 None。"""
        sx = min(max(self._quad_split_x.get(), 0.05), 1.0)
        sy = min(max(self._quad_split_y.get(), 0.05), 1.0)
        cw, ch = self._quad_canvas_size()
        top = 10
        col_x = int(round(cw * sx))
        row_y = top + int(round((ch - top) * sy))
        if abs(event.x - col_x) <= 3:
            return "v"
        if abs(event.y - row_y) <= 3:
            return "h"
        return None

    def _on_quad_grid_press(self, event):
        """按下：抓分割线并改成可拖拽光标。"""
        if self.quad_smart_var.get():
            # 智能模式下比例由程序算，不接受手动拖
            self.status_var.set("智能分配已开，比例由程序计算；"
                                "想手动拖分割线请先取消勾选")
            return
        which = self._quad_hit_which(event)
        if which is None:
            return
        self._quad_drag = which
        self.quad_grid_canvas.configure(cursor="sb_h_double_arrow"
                                        if which == "v" else "sb_v_double_arrow")

    def _on_quad_grid_drag(self, event):
        """拖动：按鼠标位置更新比例并重画（实时预览）。"""
        if self._quad_drag is None:
            return
        cw, ch = self._quad_canvas_size()
        top = 10
        x = min(max(event.x, int(cw * 0.05)), int(cw * 0.95))
        y = min(max(event.y, top + int((ch - top) * 0.05)),
                top + int((ch - top) * 0.95))
        if self._quad_drag == "v":
            self._quad_split_x.set(x / cw)
        else:
            self._quad_split_y.set((y - top) / (ch - top))
        self._refresh_quad_grid()

    def _on_quad_grid_release(self, _event=None):
        """松手：还原光标并提示当前比例。"""
        if self._quad_drag is None:
            return
        self._quad_drag = None
        self.quad_grid_canvas.configure(cursor="")
        self.status_var.set(
            "四宫格比例：横向 %.0f%%（左栏）/ %.0f%%（右栏），"
            "竖向 %.0f%%（上排）/ %.0f%%（下排）——点「2×2 排列」生效" % (
                self._quad_split_x.get() * 100,
                (1 - self._quad_split_x.get()) * 100,
                self._quad_split_y.get() * 100,
                (1 - self._quad_split_y.get()) * 100))

    def _on_quad_smart_toggle(self):
        """勾选/取消智能分配。智能模式下分割线由程序计算，不接受手动拖。"""
        smart = bool(self.quad_smart_var.get())
        if smart:
            # 比例将被程序算出，先提示用户下拉框 semantics 的变化
            self.status_var.set(
                "智能分配已开：四个下拉只表示参与排列的窗口，"
                "谁去哪个角、比例怎么切都由程序按窗口宽高比算；"
                "想手动指定就取消勾选")
        else:
            self.status_var.set("智能分配已关：按四个下拉的位置摆放，"
                                "比例用分割线的当前位置")

    @staticmethod
    def _short_label(label):
        """把窗口标签压短，让 96px 宽的格子里放得下。

        标签形如「ChatGPT — ChatGPT  (1512×895)」或「#1 UU远程  (1080×647)」。
        先去掉末尾的分辨率后缀，再只留「应用 — 窗口」里的应用名；同名窗口的
        #序号 要保留——它正是区分多个同类窗口的依据。
        """
        s = (label or "").strip()
        if not s:
            return "空"
        # 去掉末尾的「(宽×高)」后缀
        s = s.split("(", 1)[0].strip()
        # 只留「应用 — 窗口」里的应用名
        if "—" in s:
            s = s.split("—", 1)[0].strip() or s
        s = s.replace("::", " ").strip() or s
        return s if len(s) <= 11 else s[:10] + "…"

    @classmethod
    def build_quad_short_labels(cls, wins):
        """把窗口列表压成四宫格下拉/网格用的短标签。

        返回 (values, short_map, hwnd_short, short_rev)：
        - values:     下拉框候选（首个是「自动」），每个窗口都有唯一一项
        - short_map:  短标签 -> hwnd，按钮据此把选择换回窗口
        - hwnd_short: hwnd -> 短标签，排列完成后把实际分配写回界面用
        - short_rev:  完整标签 -> 短标签，别处写入完整标签时归一用

        压缩后会同名撞车：一个应用开多个窗口（两个 Chrome）时短标签都是
        「Chrome」，四个下拉可选项只剩一个，用户没法把不同窗口放进不同角，
        选了还会被「不能重复选择」挡住——这正是四宫格「只能选三个」之外的
        另一半症状。撞车时按出现顺序补 #n 后缀，保证每项唯一。
        """
        items = []
        for w in wins:
            lb = w.get("label")
            hwnd = w.get("hwnd")
            if not lb or not hwnd:
                continue
            base = cls._short_label(lb)
            # 后端已给同名窗口加过序号（「#1 UU远程」）。剥掉序号再统计，
            # 否则「#2 #1 UU远程」这种叠编号会一路套下去。
            core = _QUAD_SEQ_RE.sub("", base)
            items.append((lb, hwnd, base, core))
        # 撞车的（同一应用多窗口）统一重编号：不带序号的会变成「#1 名字」，
        # 与「#2 名字」配对，读起来一致，也不会出现孤零零的 #2。
        total = {}
        for _lb, _h, _b, core in items:
            total[core] = total.get(core, 0) + 1
        values = [AUTO_TARGET]
        short_map = {AUTO_TARGET: None}
        hwnd_short = {}
        short_rev = {}
        seen_n = {}
        for lb, hwnd, base, core in items:
            if total[core] > 1:
                seen_n[core] = seen_n.get(core, 0) + 1
                s = f"#{seen_n[core]} {core}"
            else:
                s = base
            if s in short_map:      # 兜底：理论上不会撞，真撞了往后顺延
                n = seen_n.get(core, 0)
                while s in short_map:
                    n += 1
                    s = f"#{n} {core}"
                seen_n[core] = n
            short_map[s] = hwnd
            hwnd_short[hwnd] = s
            short_rev[lb] = s
            values.append(s)
        return values, short_map, hwnd_short, short_rev

    def _on_quad_grid_resize(self, _event=None):
        """格子是固定像素网格，尺寸变化不影响布局，只需重画内容。"""
        self._refresh_quad_grid()

    # ---------- 跨屏铺满 ----------
    def _spread_indices(self):
        """返回勾选的屏下标（按界面顺序）。"""
        out = []
        for i, var in getattr(self, "_spread_checks", []):
            try:
                if var.get():
                    out.append(i)
            except Exception:  # noqa: BLE001
                continue
        return out

    def _on_spread_check(self):
        """勾选/取消某块屏后即时提示当前选了哪些屏。"""
        idx = self._spread_indices()
        if not idx:
            self.status_var.set("未选择任何屏（至少勾一块才能铺）")
        else:
            self.status_var.set(
                "已选 %d 块屏: %s" % (
                    len(idx),
                    "、".join(self._monitor_short_name(self.monitors[i])
                              for i in idx)))

    def _monitor_short_name(self, m):
        """屏的短名，用于状态栏（避免长设备名把状态栏挤爆）。"""
        name = (self._monitor_display_name(m) or "").strip()
        return name or f"{m.width}x{m.height}"

    def _fill_all_monitors(self):
        """把最前面的若干窗口依次铺到勾选的屏（一个窗口一块屏）。

        窗口比勾选的屏少时有几屏铺几屏、多出来的屏不动——开着 2 个应用
        却勾了 3 块屏是常见情况，不当作失败，状态栏如实说明铺了几块。
        竖屏默认不勾，用户想往竖屏铺就自己勾上。
        """
        if self.topmost.get():
            try:
                self.root.attributes("-topmost", False)
            except Exception:  # noqa: BLE001
                pass
        indices = self._spread_indices()
        if not indices:
            self.status_var.set("铺屏失败：请先勾选至少一块目标屏")
            return
        mode = ("quad" if self.spread_mode_var.get() == SPREAD_MODE_QUAD
                else "max")
        try:
            done = b.fill_all_monitors(mode=mode, monitor_indices=indices)
        except Exception as e:  # noqa: BLE001
            self.status_var.set(f"逐屏铺满失败：{e}")
            return
        if not done:
            self.status_var.set("铺屏失败：没有可操作的窗口")
            return
        total = len(indices)
        if done < total:
            self.status_var.set(
                f"已铺 {done} 屏：窗口不足 {total} 个，勾选的屏还剩 %d 块未动"
                % (total - done))
        else:
            self.status_var.set(f"已铺满勾选的 {done} 块屏")


    def _toggle_topmost_action(self):
        """切换目标窗口的置顶（always-on-top）状态并反馈。"""
        try:
            res = b.toggle_topmost(use_pinned=True)
        except Exception as e:  # noqa: BLE001
            self.status_var.set(f"置顶切换失败：{e}")
            return
        if res is True:
            self.status_var.set("目标窗口已置顶")
        elif res is False:
            self.status_var.set("目标窗口已取消置顶")
        else:
            self.status_var.set("置顶切换失败：未找到可操作窗口")

    # ---------- 置顶 ----------
    def _apply_topmost(self):
        """应用/取消主窗口置顶。"""
        try:
            self.root.attributes("-topmost", bool(self.topmost.get()))
        except Exception:  # noqa: BLE001
            pass

    # ---------- 辅助功能授权提示 ----------
    def show_accessibility_hint(self):
        """未获得辅助功能授权时，在界面顶部显示引导提示。"""
        try:
            self._perm_frame.pack(fill="x", padx=4, pady=(0, 4),
                                  before=self._scroll_area)
        except Exception:  # noqa: BLE001
            pass

    def hide_accessibility_hint(self):
        """关闭授权提示条。"""
        try:
            self._perm_frame.pack_forget()
        except Exception:  # noqa: BLE001
            pass

    def _open_accessibility_settings(self):
        """跳转到系统设置的辅助功能页面。"""
        try:
            b.open_accessibility_settings()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 窗口事件触发器 ----------
    def _build_triggers(self, parent):
        """窗口被移动（或新出现）时自动执行动作。"""
        self._trig_last = {}    # hwnd -> (x, y, w, h) 上次几何快照
        self._trig_fired = {}   # (hwnd, 触发器序号) -> 上次触发时刻（冷却用）
        self._trig_rows = []

        f = ttk.LabelFrame(parent, text="触发器（窗口移动自动触发动作）")
        f.pack(fill="x", padx=6, pady=(4, 8), anchor="n")

        form = ttk.Frame(f)
        form.pack(fill="x", padx=4, pady=2)
        self.trig_field_var = tk.StringVar(value="owner")
        trig_field_cb = ttk.Combobox(form, textvariable=self.trig_field_var, width=6,
                                     state="readonly", values=["owner", "title"])
        trig_field_cb.pack(side="left")
        trig_field_cb.bind("<<ComboboxSelected>>",
                           lambda _e: self._refresh_trigger_pattern_candidates())
        self._tooltip(trig_field_cb,
                      "匹配依据：owner=应用名，title=窗口标题。")
        self.trig_pattern_var = tk.StringVar()
        # 用可编辑下拉框替代纯文本框：下拉列出当前各应用名/标题便于直接选，
        # 也可手动输入（如要匹配尚未打开的窗口）。候选在刷新窗口列表时更新。
        self.trig_pattern_cb = ttk.Combobox(form, textvariable=self.trig_pattern_var,
                                            width=14)
        self.trig_pattern_cb.pack(side="left", padx=2)
        self._tooltip(self.trig_pattern_cb,
                      "要匹配的文字（包含即可）。下拉列出当前应用名可直接选，"
                      "也可手动输入；留空则匹配所有窗口。")
        self.trig_event_var = tk.StringVar(value=triggers.EVENT_LABELS["moved"])
        trig_ev_cb = ttk.Combobox(form, textvariable=self.trig_event_var, width=8,
                                  state="readonly",
                                  values=[triggers.EVENT_LABELS[e] for e in triggers.EVENTS])
        trig_ev_cb.pack(side="left", padx=2)
        self._tooltip(trig_ev_cb,
                      "触发时机：「被移动」= 窗口被拖动后；「新出现」= 窗口刚打开。")
        self.trig_action_var = tk.StringVar(value=triggers.ACTION_LABELS["snap"])
        act_cb = ttk.Combobox(form, textvariable=self.trig_action_var, width=12,
                              state="readonly",
                              values=[triggers.ACTION_LABELS[a] for a in triggers.ACTIONS])
        act_cb.pack(side="left", padx=2)
        act_cb.bind("<<ComboboxSelected>>", self._on_trig_action_change)
        self.trig_param_var = tk.StringVar()
        self.trig_param_cb = ttk.Combobox(form, textvariable=self.trig_param_var,
                                          width=16, state="readonly")
        self.trig_param_cb.pack(side="left", padx=2)
        ttk.Button(form, text="添加", command=self._add_trigger).pack(side="left", padx=2)
        ttk.Button(form, text="删除", command=self._delete_trigger).pack(side="left", padx=2)
        ttk.Button(form, text="启/停", command=self._toggle_trigger).pack(side="left", padx=2)

        ttk.Label(f,
                  text="匹配应用名/标题的窗口「被移动」或「新出现」时自动执行动作。"
                       "与「窗口规则」的分工：规则管新窗口归位，触发器管被移动的窗口。",
                  foreground="#666", wraplength=720, justify="left").pack(anchor="w", padx=4)
        self.trig_list = tk.Listbox(f, height=4, activestyle="none")
        self.trig_list.pack(fill="x", padx=4, pady=2)
        self._refresh_trigger_params()
        self._refresh_trigger_list()
        self._refresh_trigger_pattern_candidates()

    def _on_trig_action_change(self, event=None):
        """动作类型变化时刷新参数候选。"""
        self._refresh_trigger_params()

    def _refresh_trigger_params(self):
        """按当前动作类型刷新参数下拉的候选值与默认选中。"""
        try:
            action = TRIG_ACTION_KEY.get(self.trig_action_var.get(), "snap")
            if action == "snap":
                values = [rules.ZONE_LABELS[z] for z in rules.ZONES]
            elif action == "monitor":
                values = [self._monitor_label(m) for m in (self.monitors or [])]
            elif action == "scenario":
                values = self._scenario_names()
            else:  # rules：无参数
                values = []
            self.trig_param_cb["values"] = values
            if values:
                if self.trig_param_var.get() not in values:
                    self.trig_param_var.set(values[0])
            else:
                self.trig_param_var.set("")
        except Exception:  # noqa: BLE001
            pass

    def _monitor_label(self, mon):
        """显示器的可读名（回退设备路径）。"""
        return getattr(mon, "device_name", None) or mon.device_path

    def _scenario_names(self):
        """情景显示名列表。"""
        try:
            return [(scen.get("name") or scenarios.describe(s))
                    for s, scen in sorted(scenarios.list_scenarios().items())]
        except Exception:  # noqa: BLE001
            return []

    def _device_path_by_label(self, label):
        """按显示名反查显示器 device_path。"""
        for m in (self.monitors or []):
            if self._monitor_label(m) == label:
                return m.device_path
        return None

    def _add_trigger(self):
        pattern = (self.trig_pattern_var.get() or "").strip()
        if not pattern:
            self.status_var.set("请先填写匹配关键词")
            return
        action = TRIG_ACTION_KEY.get(self.trig_action_var.get(), "snap")
        label = self.trig_param_var.get()
        if action == "snap":
            param = ZONE_KEY.get(label, "full")
        elif action == "monitor":
            param = self._device_path_by_label(label) or ""
        elif action == "scenario":
            param = self._signature_by_name(label) or ""
        else:  # rules
            param = ""
        if action in ("monitor", "scenario") and not param:
            self.status_var.set("请先选择有效的目标（显示器 / 情景）")
            return
        triggers.add_trigger({
            "enabled": True,
            "field": self.trig_field_var.get(),
            "pattern": pattern,
            "regex": False,
            "event": TRIG_EVENT_KEY.get(self.trig_event_var.get(), "moved"),
            "action": action,
            "param": param,
        })
        self._refresh_trigger_list()
        self.status_var.set(f"已添加触发器：{pattern}")

    def _delete_trigger(self):
        idx = self._selected_index(self.trig_list)
        if idx is None:
            self.status_var.set("请先在触发器列表中选中一条")
            return
        triggers.delete_trigger(idx)
        self._refresh_trigger_list()
        self.status_var.set("已删除该触发器")

    def _toggle_trigger(self):
        idx = self._selected_index(self.trig_list)
        if idx is None:
            self.status_var.set("请先在触发器列表中选中一条")
            return
        triggers.toggle_trigger(idx)
        self._refresh_trigger_list()
        self.status_var.set("已切换该触发器的启用状态")

    def _trigger_param_label(self, t):
        """把触发器参数（内部值）转成可读文本。"""
        action, param = t.get("action"), t.get("param") or ""
        if action == "snap":
            return rules.ZONE_LABELS.get(param, param)
        if action == "monitor":
            for m in (self.monitors or []):
                if m.device_path == param:
                    return self._monitor_label(m)
            return param
        if action == "scenario":
            try:
                scen = scenarios.get(param)
                if scen:
                    return scen.get("name") or scenarios.describe(param)
            except Exception:  # noqa: BLE001
                pass
            return param
        return ""

    def _refresh_trigger_list(self):
        self._trig_rows = []
        try:
            self.trig_list.delete(0, "end")
            for t in triggers.list_triggers():
                label = (f'{t.get("pattern")} · '
                         f'{triggers.EVENT_LABELS.get(t.get("event"), t.get("event"))} → '
                         f'{triggers.ACTION_LABELS.get(t.get("action"), t.get("action"))}'
                         f' {self._trigger_param_label(t)}')
                if not t.get("enabled", True):
                    label += "  [停用]"
                self.trig_list.insert("end", label)
                self._trig_rows.append(t)
        except Exception:  # noqa: BLE001
            pass

    def _check_triggers(self, wins):
        """窗口移动 / 出现时执行触发器动作（复用规则轮询的同一次枚举结果）。"""
        items = [t for t in triggers.list_triggers() if t.get("enabled", True)]
        if not items:
            return
        last = getattr(self, "_trig_last", None) or {}
        fired = getattr(self, "_trig_fired", None) or {}
        if not last:
            # 首次只记录快照：否则会把已经打开的所有窗口当成「新出现」全触发一遍
            self._trig_last = {w.get("hwnd"): (w.get("x"), w.get("y"),
                                               w.get("w"), w.get("h"))
                               for w in wins or []}
            return
        now = time.monotonic()
        snapshot = {}
        for w in wins or []:
            hwnd = w.get("hwnd")
            geo = (w.get("x"), w.get("y"), w.get("w"), w.get("h"))
            snapshot[hwnd] = geo
            prev = last.get(hwnd)
            moved = triggers.is_moved(prev, geo)
            created = prev is None
            for i, t in enumerate(items):
                event = t.get("event", "moved")
                if event == "moved" and not moved:
                    continue
                if event == "created" and not created:
                    continue
                if not triggers.match(t, w):
                    continue
                key = (hwnd, i)
                if now - fired.get(key, 0) < triggers.COOLDOWN_SEC:
                    continue  # 冷却中：避免动作自身造成的位移反复触发
                fired[key] = now
                self._run_trigger_action(t, w)
                break  # 一个窗口一次轮询只执行首个命中的触发器
        self._trig_last = snapshot
        self._trig_fired = fired

    def _run_trigger_action(self, t, win):
        """执行触发器动作。"""
        action = t.get("action", "snap")
        try:
            if action == "snap":
                mon = triggers.monitor_of_window(self.monitors, win)
                if mon is None:
                    return
                zone = t.get("param") or "full"
                rect = rules.zone_rect(mon, zone)
                b.set_window_rect(win["hwnd"], rect[0], rect[1], rect[2], rect[3],
                                  activate=False)
                self.status_var.set(
                    f'触发器：已吸附窗口到「{rules.ZONE_LABELS.get(zone, zone)}」')
            elif action == "monitor":
                mon = rules.find_monitor(self.monitors, t.get("param"))
                if mon is None:
                    return
                b.move_to_monitor(win["hwnd"], mon)
                self.status_var.set("触发器：已把窗口移到目标显示器")
            elif action == "scenario":
                sig = t.get("param")
                if not scenarios.get(sig):
                    # 情景不存在时不能报"已套用"，那是误导
                    self.status_var.set(
                        f"触发器：情景不存在（{scenarios.describe(sig)}）")
                elif self._apply_scenario_by_signature(sig):
                    self.status_var.set("触发器：已套用情景")
                else:
                    # 情景存在但壁纸方案/布局都被删了：实际什么都没套用
                    self.status_var.set(
                        "触发器：情景无可用预设（壁纸方案/布局可能已被删除）")
            elif action == "rules":
                self._apply_rules_now()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 多屏任务栏 ----------
    def _build_taskbar(self, parent):
        """每块显示器一个细条：列出该屏窗口，可点击激活、◀ ▶ 移到相邻屏。"""
        s = settings.load()
        enabled = bool(s.get("taskbar_enabled", False))
        position = s.get("taskbar_position", "底部")
        self.taskbar_enabled_var = tk.BooleanVar(value=enabled)
        self.taskbar_pos_var = tk.StringVar(
            value=position if position in TASKBAR_POS else "底部")
        self._taskbar = None
        self._taskbar_job = None

        f = ttk.LabelFrame(parent, text="多屏任务栏")
        f.pack(fill="x", padx=6, pady=(4, 8), anchor="n")
        tb_cb = ttk.Checkbutton(f, text="启用（每块显示器显示一条窗口列表）",
                                variable=self.taskbar_enabled_var,
                                command=self._taskbar_toggle)
        tb_cb.pack(anchor="w", padx=4, pady=2)
        self._tooltip(
            tb_cb,
            "勾选后，每块显示器边缘各显示一条该屏的窗口列表，"
            "点窗口名可直接把它提到最前。")
        row = ttk.Frame(f)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Label(row, text="位置:").pack(side="left")
        pos_cb = ttk.Combobox(row, textvariable=self.taskbar_pos_var, width=6,
                              state="readonly", values=list(TASKBAR_POS))
        pos_cb.pack(side="left", padx=2)
        self._tooltip(pos_cb, "任务栏贴在屏幕的底部还是顶部。")
        hint_lbl = ttk.Label(
            row, text="点窗口名激活；◀ ▶ 把选中窗口移到相邻显示器",
            foreground="#666")
        hint_lbl.pack(side="left", padx=8)
        self._tooltip(
            hint_lbl,
            "任务栏用法：点窗口名把它激活到最前；点 ◀ ▶ 箭头把该窗口"
            "移到相邻的显示器。")
        if enabled:
            self._taskbar_toggle()
        # 轮询刷新（仅启用时才真正枚举窗口）
        self.root.after(3000, self._poll_taskbar)

    def _taskbar_toggle(self):
        """启用 / 停用任务栏，并把偏好写入设置。"""
        enabled = self.taskbar_enabled_var.get()
        st = settings.load()
        st["taskbar_enabled"] = enabled
        st["taskbar_position"] = self.taskbar_pos_var.get()
        settings.save(st)
        if not enabled:
            if self._taskbar is not None:
                self._taskbar.destroy()
                self._taskbar = None
            self.status_var.set("多屏任务栏已停用")
            return
        self._ensure_taskbar()
        self.status_var.set("多屏任务栏已启用（列出窗口需辅助功能授权）")

    def _ensure_taskbar(self):
        """确保任务栏实例存在并可见。"""
        if self._taskbar is None:
            self._taskbar = taskbar.TaskBar(
                self.root, on_focus=self._taskbar_focus, on_move=self._taskbar_move,
                position=TASKBAR_POS.get(self.taskbar_pos_var.get(), "bottom"))
        self._taskbar.set_position(TASKBAR_POS.get(self.taskbar_pos_var.get(), "bottom"))
        self._taskbar.show()

    def _poll_taskbar(self):
        """主线程轮询：刷新多屏任务栏（仅启用时才枚举窗口，避免无谓开销）。"""
        self._taskbar_job = None
        try:
            if (getattr(self, "taskbar_enabled_var", None) is not None
                    and self.taskbar_enabled_var.get()):
                self._ensure_taskbar()
                self._taskbar.refresh(self.monitors, b.list_target_windows())
        except Exception:  # noqa: BLE001
            pass
        self.root.after(3000, self._poll_taskbar)

    def _taskbar_focus(self, win):
        """激活任务栏上点击的窗口（沿用原几何，仅前置）。"""
        try:
            b.set_window_rect(win["hwnd"], win["x"], win["y"], win["w"], win["h"],
                              activate=True)
        except Exception:  # noqa: BLE001
            pass

    def _taskbar_move(self, win, target_mon):
        """把窗口移到相邻显示器。"""
        try:
            b.move_to_monitor(win["hwnd"], target_mon)
            self.status_var.set("已移动窗口到其他显示器（可用「撤销」还原）")
        except Exception:  # noqa: BLE001
            self.status_var.set("移动窗口失败")

    # ---------- 上手建议卡片 ----------
    def _build_getting_started(self, parent):
        """单页顶部的动态「上手建议」卡片：根据授权状态与屏数给出实用建议。"""
        self._gs_frame = ttk.LabelFrame(parent, text="上手建议")
        self._gs_frame.pack(fill="x", padx=6, pady=(4, 8), anchor="n")

        self._gs_text = tk.StringVar(value="")
        self._gs_label = ttk.Label(self._gs_frame, textvariable=self._gs_text,
                                   wraplength=720, justify="left")
        self._gs_label.pack(fill="x", padx=8, pady=(4, 2), anchor="w")

        btn_row = ttk.Frame(self._gs_frame)
        btn_row.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(btn_row, text="刷新状态",
                   command=self._refresh_getting_started).pack(side="left", padx=4)
        ttk.Button(btn_row, text="去授权辅助功能",
                   command=self._open_accessibility_settings).pack(side="left", padx=4)
        ttk.Button(btn_row, text="收起",
                   command=lambda: self._gs_frame.pack_forget()).pack(side="left", padx=4)

        self._refresh_getting_started()

    def _refresh_getting_started(self):
        """根据辅助功能授权状态与显示器数量，动态给出实用建议。"""
        try:
            trusted = b.is_accessibility_trusted()
        except Exception:  # noqa: BLE001
            trusted = False
        n = len(self.monitors or [])
        lines = []
        if b.IS_MAC and not trusted:
            lines.append("• 尚未授权「辅助功能」：窗口控制、全局快捷键、拖拽吸附会静默失效。"
                         "点「去授权辅助功能」，在系统设置中开启本程序后点「刷新状态」。")
        if n <= 1:
            lines.append("• 当前仅 1 块显示器：情景(F2)、拖拽吸附(F3)、窗口规则(F5) 的多屏效果"
                         "需在多屏真机验证；UI 与配置读写均可正常使用。")
        if not lines:
            lines.append("• 环境就绪（已授权、多屏）。三步上手：")
            lines.append("  F6 给显示器起别名 → F2 绑定当前组合的情景（壁纸+布局）→ F5 加窗口规则自动归位。")
            lines.append("  F3 拖拽吸附 + ⌘⌥/Ctrl+Alt+Z 撤销误移动；F8 导出设置以便换机备份。")
        else:
            lines.append("• 通用建议：F6 起别名 → F2 建情景 → F5 加规则；F3 拖拽吸附 + 撤销；F8 备份设置。")
        self._gs_text.set("\n".join(lines))
        try:
            if b.IS_MAC and not trusted:
                color = "#a15c00"
            elif n <= 1:
                color = "#1a6bb3"
            else:
                color = "#1f7a1f"
            self._gs_label.configure(foreground=color)
        except Exception:  # noqa: BLE001
            pass

    def _keep_front_after(self, cmd):
        """包装窗口操作：执行后重新置顶，避免被激活的目标窗口盖住。"""
        def run():
            try:
                cmd()
            finally:
                self._keep_front()
        return run

    def _keep_front(self):
        """操作后保持主窗口可见。

        按钮已改用 activate=False，目标窗口不会被激活到最前，本窗口自然保持在
        前面；因此默认无需常驻置顶，仅勾选时才强制浮动。
        """
        try:
            if self.topmost.get():
                self.root.attributes("-topmost", True)
            else:
                self.root.attributes("-topmost", False)
                self.root.lift()
        except Exception:  # noqa: BLE001
            pass

    def _build_profiles(self, parent):
        f = ttk.LabelFrame(parent, text="壁纸方案")
        f.pack(fill="x", padx=8, pady=6)
        top = ttk.Frame(f)
        top.pack(fill="x", pady=4)
        self.profile_name = tk.StringVar()
        ttk.Label(top, text="名称:").pack(side="left")
        ttk.Entry(top, textvariable=self.profile_name, width=15).pack(side="left", padx=4)
        ttk.Button(top, text="保存当前", command=self.save_profile).pack(side="left", padx=3)
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(top, textvariable=self.profile_var, width=15, state="readonly")
        self.profile_combo.pack(side="left", padx=4)
        ttk.Button(top, text="应用", command=self.apply_profile).pack(side="left", padx=3)
        ttk.Button(top, text="删除", command=self.delete_profile).pack(side="left", padx=3)
        self._refresh_profile_list()

    # ---------- 窗口布局方案 ----------
    def _build_layouts(self, parent):
        f = ttk.LabelFrame(parent, text="窗口布局方案")
        f.pack(fill="x", padx=8, pady=6)
        ttk.Label(
            f,
            text="保存当前所有窗口的位置/大小，之后一键还原（按「应用::窗口」匹配，"
                 "标题变化会自动按应用名兜底）。方案会记下保存时的显示器配置，"
                 "显示器数量或排列变化后应用会先提示。",
            justify="left", wraplength=620,
        ).pack(anchor="w", padx=4, pady=(0, 4))
        row = ttk.Frame(f)
        row.pack(fill="x", padx=4, pady=4)
        ttk.Label(row, text="方案:").pack(side="left")
        self.layout_var = tk.StringVar()
        self.layout_cb = ttk.Combobox(row, textvariable=self.layout_var,
                                      state="readonly", width=26)
        self.layout_cb.pack(side="left", padx=4, fill="x", expand=True)
        ttk.Button(row, text="保存当前…", command=self._save_layout).pack(side="left", padx=2)
        ttk.Button(row, text="应用", command=self._apply_layout).pack(side="left", padx=2)
        ttk.Button(row, text="删除", command=self._delete_layout).pack(side="left", padx=2)
        self._refresh_layout_list()

    def _refresh_layout_list(self):
        names = layouts.list_layouts()
        self.layout_cb["values"] = names
        if names and self.layout_var.get() not in names:
            self.layout_var.set(names[0])
        elif not names:
            self.layout_var.set("")
        self._sync_scenario_options()

    def _save_layout(self):
        wins = b.list_target_windows()
        if not wins:
            messagebox.showwarning("提示", "当前没有可保存的窗口")
            return
        name = simpledialog.askstring("保存布局", "方案名称：", parent=self.root)
        if not name:
            return
        data = []
        for w in wins:
            item = {"hwnd": w["hwnd"], "owner": w["owner"], "name": w["name"],
                    "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"]}
            # 记下窗口在所在屏工作区上的相对框：屏幕组合变化后还原时按
            # 比例重映射（Moom 的 Relative Frame 思路），不再一换屏就错位
            try:
                wl, wt, ww, wh = screen_math.best_work_for_rect(
                    list(self.monitors or []), w["x"], w["y"], w["w"], w["h"])
                item["rel"] = [round(v, 6) for v in screen_math.normalize_to_work(
                    w["x"], w["y"], w["w"], w["h"], wl, wt, ww, wh)]
            except Exception:  # noqa: BLE001
                pass
            data.append(item)
        # 同时存分辨率指纹与显示器 UUID：指纹用于「配置整体变化」的粗判，
        # UUID 用于精确认出「这块屏还是原来那块」（两台同分辨率也能区分）。
        layouts.save_layout(name, data,
                            monitor_sig=self._monitor_signature(),
                            monitor_uuids=screen_math.display_uuids(
                                list(self.monitors or [])))
        self._refresh_layout_list()
        self.layout_var.set(name)
        s = settings.load()
        s["last_layout"] = name
        settings.save(s)
        self.status_var.set(f"已保存布局：{name}（{len(data)} 个窗口）")

    def _layout_monitor_changed(self, name):
        """布局保存时的显示器配置是否与当前不同（老方案无指纹时返回 False）。"""
        saved = layouts.monitor_signature(name)
        if not saved:
            return False
        try:
            current = self._monitor_signature() or ()
            return (tuple(tuple(item) for item in saved)
                    != tuple(tuple(item) for item in current))
        except Exception:  # noqa: BLE001
            return False

    def _layout_uuid_matched(self, name):
        """按显示器 UUID 判断「保存时的屏现在是否都还在」。

        返回 (matched, saved, current)：
          matched=True 表示每块保存过的屏都能按 UUID 找到，可以按绝对坐标
          精确还原——这是 UUID 绑定比分辨率指纹强的地方：两台同为 1920x1080
          的屏换个位置，指纹说「变了」，UUID 说「还是那两块」。
        保存时没有 UUID（老方案 / 非 macOS）时 matched=False。
        """
        saved = layouts.monitor_uuids(name)
        if not saved:
            return (False, None, None)
        current = screen_math.display_uuids(list(self.monitors or []))
        if not current:
            return (False, saved, None)
        cur_by_uuid = {v: k for k, v in current.items()}
        for _path, u in saved.items():
            if u not in cur_by_uuid:
                return (False, saved, current)
        return (True, saved, current)

    def _apply_layout(self):
        self._apply_layout_by_name(self.layout_var.get())

    def _apply_layout_by_name(self, name, silent=False):
        """还原指定布局。silent=True（如情景自动套用）时不弹窗，仅更新状态栏。"""
        if not name:
            if not silent:
                messagebox.showwarning("提示", "请先选择或保存一个布局方案")
            return False
        layout = layouts.load_layout(name)
        if not layout:
            if not silent:
                messagebox.showwarning("提示", f"布局方案不存在：{name}")
            return False
        # 显示器配置变了（数量/分辨率/相对位置）时：老布局（没记相对框）才提示
        # 并继续按绝对坐标还原；新布局按相对框重映射到新屏，自适应不再错位。
        monitors_now = list(self.monitors or [])
        if self._layout_monitor_changed(name):
            layouts.apply_relative_frames(name, monitors_now)
        # UUID 能对上时按绝对坐标还原（最精确）；对不上才走相对框重映射。
        uuid_ok, _saved_u, _cur_u = self._layout_uuid_matched(name)
        has_rel = any(isinstance(w.get("rel"), (list, tuple)) and len(w.get("rel")) == 4
                      for w in layout.get("windows", [])
                      if isinstance(w, dict))
        changed = self._layout_monitor_changed(name)
        if changed and not has_rel and not uuid_ok:
            if not silent:
                self.show()   # 托盘触发时主窗口可能已隐藏，弹窗前先亮出来
                if not messagebox.askyesno(
                        "显示器配置已变化",
                        f"布局「{name}」保存时的显示器配置与当前不一致"
                        "（数量 / 分辨率 / 相对位置已变），\n"
                        "还原后窗口位置可能不在预期的屏幕上。\n\n仍要继续吗？"):
                    self.status_var.set("已取消：显示器配置与布局保存时不一致")
                    return False
        if changed and uuid_ok and not silent:
            # 指纹变了但 UUID 对得上：不弹警告，按绝对坐标还原
            self.status_var.set(
                f"布局「{name}」：显示器排列有变，但 UUID 匹配，按原坐标还原")
        ok, miss = 0, 0
        current = {w["hwnd"]: w["owner"] for w in b.list_target_windows()}
        # 标题宽松匹配要用「窗口名」而不是应用名，单独取一份
        cur_names = {w["hwnd"]: (w.get("name") or "")
                     for w in b.list_target_windows()}
        adapt_n = 0
        fuzzy_n = 0
        for item in layout["windows"]:
            hwnd = item["hwnd"]
            # 精确匹配；否则按应用名兜底（窗口标题可能已变化）
            target = hwnd if hwnd in current else next(
                (h for h, o in current.items() if o == item["owner"]), None)
            # 应用名兜底仍可能选错：同一 App 开多个窗口时第一个不一定是保存的
            # 那个。加一层标题宽松匹配（Rectangle Pro 的 lenientMatch /
            # TitleMatchingMode）：标题变了但实词还在，也能认出来。
            if not target or not target.startswith(hwnd.split("::")[0] + "::"):
                same_app = [(h, cur_name.get(h) or "")
                            for h, cur_name in cur_names.items()
                            if cur_name and (h.split("::")[0]
                                             == hwnd.split("::")[0])]
                if same_app:
                    cand_h, mode, score = title_match.best_candidate(
                        hwnd.split("::", 1)[1], same_app)
                    if cand_h and (not target or cand_h != target
                                   or mode == "exact"):
                        target = cand_h
                        fuzzy_n += 1
            # 匹配不上时按邻近度兜底：窗口标题全变了但还开着，也能按
            # 「当前哪个窗口离这个格子最近」归位（Moom 的 Proximity 匹配思路）
            if not target:
                target = self._nearest_layout_window(item, current)
            if not target:
                miss += 1
                continue
            try:
                tx, ty, tw, th = item["x"], item["y"], item["w"], item["h"]
                rel = item.get("rel")
                if (self._layout_monitor_changed(name)
                        and isinstance(rel, (list, tuple)) and len(rel) == 4
                        and monitors_now):
                    wl, wt, ww, wh = screen_math.best_work_for_rect(
                        monitors_now, tx, ty, tw, th)
                    tx, ty, tw, th = screen_math.denormalize_to_work(
                        rel, wl, wt, ww, wh)
                    tx, ty, tw, th = screen_math.clamp_to_work(
                        tx, ty, tw, th, wl, wt, ww, wh)
                    adapt_n += 1
                b.set_window_rect(target, tx, ty, tw, th, activate=False)
                ok += 1
            except Exception:  # noqa: BLE001
                miss += 1
        s = settings.load()
        s["last_layout"] = name
        settings.save(s)
        status = f"已还原布局「{name}」：成功 {ok}，未找到 {miss}"
        if adapt_n:
            status += f"，按比例重映射 {adapt_n} 个（显示器配置已变化）"
        if fuzzy_n:
            status += f"，其中 {fuzzy_n} 个按标题宽松匹配认出"
        self.status_var.set(status)
        if silent:
            return True
        if miss:
            messagebox.showinfo(
                "完成",
                f"布局「{name}」还原完成：成功 {ok} 个，未找到 {miss} 个"
                "（窗口可能已关闭或标题已变）。")
        else:
            messagebox.showinfo("完成", f"布局「{name}」已全部还原（{ok} 个窗口）")
        return True

    def _nearest_layout_window(self, item, current):
        """布局格没匹配到窗口时，按中心点距离取最近的未占用窗口（邻近度兜底）。"""
        try:
            tx = item["x"] + item["w"] // 2
            ty = item["y"] + item["h"] // 2
        except Exception:  # noqa: BLE001
            return None
        best, best_d = None, None
        wins = {w["hwnd"]: w for w in b.list_target_windows()
                if w["hwnd"] in current and w.get("x") is not None}
        for h, w in wins.items():
            try:
                d = ((w["x"] + w["w"] // 2 - tx) ** 2
                     + (w["y"] + w["h"] // 2 - ty) ** 2)
            except Exception:  # noqa: BLE001
                continue
            if best_d is None or d < best_d:
                best, best_d = h, d
        return best

    def _delete_layout(self):
        name = self.layout_var.get()
        if not name:
            messagebox.showwarning("提示", "请先选择要删除的布局方案")
            return
        if not messagebox.askyesno("确认", f"删除布局方案「{name}」？"):
            return
        layouts.delete_layout(name)
        self._refresh_layout_list()
        self.status_var.set(f"已删除布局：{name}")

    def _build_hotkeys(self, parent):
        f = ttk.LabelFrame(parent, text="全局快捷键")
        f.pack(fill="x", padx=8, pady=6)
        # 修饰键按平台可选：macOS 习惯 ⌘⌥，Windows 习惯 Ctrl+Alt
        is_mac = sys.platform.startswith("darwin")
        self._mod_options = (
            ["⌘⌥ (Cmd+Option)", "⌃⌥ (Ctrl+Option)"] if is_mac
            else ["Ctrl+Alt", "Ctrl+Win"]
        )
        self._mod_value_map = {
            "⌘⌥ (Cmd+Option)": "cmd+alt",
            "⌃⌥ (Ctrl+Option)": "ctrl+alt",
            "Ctrl+Alt": "ctrl+alt",
            "Ctrl+Win": "ctrl+win",
        }
        self._mod_symbol = {
            "⌘⌥ (Cmd+Option)": "⌘⌥",
            "⌃⌥ (Ctrl+Option)": "⌃⌥",
            "Ctrl+Alt": "Ctrl+Alt",
            "Ctrl+Win": "Ctrl+Win",
        }
        cur_val = self.hk_mods_var.get()
        cur_label = next((k for k, v in self._mod_value_map.items() if v == cur_val),
                         self._mod_options[0])
        self.hk_mod_label = tk.StringVar(value=cur_label)
        self.hk_info = ttk.Label(f, text=self._hk_help_text(cur_label), justify="left")
        self.hk_info.pack(anchor="w", padx=4)
        hk_row = ttk.Frame(f)
        hk_row.pack(anchor="w", padx=4, pady=4)
        ttk.Label(hk_row, text="修饰键:").pack(side="left")
        ttk.OptionMenu(
            hk_row, self.hk_mod_label, cur_label, *self._mod_options,
            command=self._on_hk_mod_change,
        ).pack(side="left", padx=(4, 8))
        ttk.Checkbutton(hk_row, text="启用全局快捷键", variable=self.hk_enabled,
                        command=self._toggle_hk).pack(side="left")

    def _build_autostart(self, parent):
        f = ttk.LabelFrame(parent, text="启动选项")
        f.pack(fill="x", padx=8, pady=6)
        ttk.Checkbutton(f, text="开机自动启动", variable=self.autostart_var,
                        command=self._toggle_autostart).pack(anchor="w", padx=4, pady=2)
        # 静默启动：自启命令带 --minimized，开机后只进托盘不弹主窗口
        self.autostart_min_var = tk.BooleanVar(value=autostart.is_minimized())
        ttk.Checkbutton(f, text="开机自启时最小化到托盘（静默启动）",
                        variable=self.autostart_min_var,
                        command=self._toggle_autostart).pack(anchor="w", padx=4, pady=2)

    def _toggle_autostart(self):
        try:
            autostart.set_enabled(self.autostart_var.get(),
                                  minimized=self.autostart_min_var.get())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"设置开机自启失败:\n{e}")
            self.autostart_var.set(autostart.is_enabled())
            self.autostart_min_var.set(autostart.is_minimized())

    # ---------- 配置备份 ----------
    def _build_backup(self, parent):
        f = ttk.LabelFrame(parent, text="配置备份")
        f.pack(fill="x", padx=8, pady=6)
        ttk.Label(
            f,
            text="把快捷键偏好、热插拔开关、壁纸轮换设置、窗口布局方案与壁纸方案"
                 "导出为单个 JSON；换机或重装后导入即可恢复（同名覆盖，其它保留）。",
            justify="left", wraplength=620,
        ).pack(anchor="w", padx=4, pady=(0, 4))
        row = ttk.Frame(f)
        row.pack(fill="x", padx=4, pady=4)
        ttk.Button(row, text="导出配置…", command=self._export_config).pack(side="left", padx=2)
        ttk.Button(row, text="导入配置…", command=self._import_config).pack(side="left", padx=2)

    def _export_config(self):
        path = filedialog.asksaveasfilename(
            title="导出配置", defaultextension=".json",
            initialfile="multimon-config.json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            n_layouts, n_profiles = config_io.collect_and_write(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"导出配置失败:\n{e}")
            return
        self.status_var.set(
            f"已导出配置：{n_layouts} 个窗口布局 / {n_profiles} 个壁纸方案")
        messagebox.showinfo(
            "完成",
            f"已导出到：\n{path}\n\n"
            f"窗口布局 {n_layouts} 个，壁纸方案 {n_profiles} 个。")

    def _import_config(self):
        path = filedialog.askopenfilename(
            title="导入配置", filetypes=[("JSON", "*.json")])
        if not path:
            return
        if not messagebox.askyesno(
                "确认", "导入会把备份里的方案合并到当前配置（同名覆盖），继续吗？"):
            return
        try:
            n_settings, n_layouts, n_profiles = config_io.read_and_restore(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"导入配置失败:\n{e}")
            return
        self._refresh_layout_list()
        self._refresh_profile_list()
        self._reload_settings_into_ui()
        self.status_var.set(
            f"已导入配置：{n_layouts} 个布局 / {n_profiles} 个壁纸方案")
        messagebox.showinfo(
            "完成",
            "配置已导入并合并：\n"
            f"· 窗口布局 {n_layouts} 个\n"
            f"· 壁纸方案 {n_profiles} 个\n"
            f"· 偏好项 {n_settings} 项\n\n"
            "开机自启属于系统设置，不随备份迁移；全局快捷键开关如已变化，"
            "取消再勾选「启用全局快捷键」即可立即生效。")

    def _reload_settings_into_ui(self):
        """导入配置后把受影响的控件同步成新值。"""
        s = settings.load()
        try:
            self.hk_enabled.set(bool(s.get("hk_enabled", self.hk_enabled.get())))
            self.rotate_dir_var.set(s.get("rotate_dir", self.rotate_dir_var.get()) or "")
            self.rotate_interval_var.set(
                max(1, int(s.get("rotate_interval_min", self.rotate_interval_var.get()))))
            self.watch_enabled.set(bool(s.get("watch_displays", self.watch_enabled.get())))
            self.auto_apply_watch.set(
                bool(s.get("watch_auto_apply", self.auto_apply_watch.get())))
            self._update_rotate_status()
            self._toggle_display_watch()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 显示器热插拔 ----------
    def _build_settings_io(self, parent):
        f = ttk.LabelFrame(parent, text="配置备份")
        f.pack(fill="x", padx=8, pady=6)
        ttk.Button(f, text="导出配置…", command=self._export_settings).pack(
            side="left", padx=4, pady=4)
        ttk.Button(f, text="导入配置…", command=self._import_settings).pack(
            side="left", padx=4, pady=4)
        ttk.Label(f, text="把 settings/profiles/layouts 打包成单个文件，便于备份或迁移",
                  wraplength=360, foreground="#555").pack(side="left", padx=8)

    def _export_settings(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".zip", filetypes=[("配置包", "*.zip")],
            title="导出配置")
        if not p:
            return
        try:
            out = settings.export_all(p)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"导出失败:\n{e}")
            return
        if out:
            messagebox.showinfo("完成", f"配置已导出到:\n{out}")
            self.status_var.set("已导出配置")
        else:
            messagebox.showwarning("提示", "当前没有可导出的配置文件")

    def _import_settings(self):
        p = filedialog.askopenfilename(
            filetypes=[("配置包", "*.zip")], title="导入配置")
        if not p:
            return
        if not messagebox.askyesno("确认",
                                   "导入将覆盖当前配置（原配置会自动备份）。继续？"):
            return
        try:
            ok, msg = settings.import_all(p)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"导入失败:\n{e}")
            return
        if ok:
            self.aliases = settings.load().get("monitor_aliases", {})
            self.refresh_monitors()
            self._refresh_profile_list()
            self.status_var.set("已导入配置")
            messagebox.showinfo("完成", msg)
        else:
            messagebox.showerror("错误", msg)

    def _build_display_watch(self, parent):
        f = ttk.LabelFrame(parent, text="显示器热插拔")
        f.pack(fill="x", padx=8, pady=6)
        self.watch_enabled = tk.BooleanVar(
            value=bool(settings.load().get("watch_displays", True)))
        self.auto_apply_watch = tk.BooleanVar(
            value=bool(settings.load().get("watch_auto_apply", False)))
        ttk.Checkbutton(f, text="显示器增减/重排时自动刷新列表",
                        variable=self.watch_enabled,
                        command=self._toggle_display_watch).pack(anchor="w", padx=4, pady=2)
        ttk.Checkbutton(f, text="并自动重应用上次使用的壁纸方案",
                        variable=self.auto_apply_watch,
                        command=self._save_watch_prefs).pack(anchor="w", padx=4, pady=2)
        self._toggle_display_watch()

    def _save_watch_prefs(self):
        s = settings.load()
        s["watch_displays"] = self.watch_enabled.get()
        s["watch_auto_apply"] = self.auto_apply_watch.get()
        settings.save(s)

    def _toggle_display_watch(self):
        self._save_watch_prefs()
        if self.watch_enabled.get():
            if self._display_poll_id is None:
                self._last_mon_sig = self._monitor_signature()
                self._display_poll_id = self.root.after(1500, self._poll_display)
        else:
            if self._display_poll_id is not None:
                try:
                    self.root.after_cancel(self._display_poll_id)
                except Exception:  # noqa: BLE001
                    pass
                self._display_poll_id = None

    def _monitor_signature(self):
        """生成当前显示器配置的指纹，用于检测热插拔/重排。"""
        try:
            ms = b.enum_monitors(force=True)
        except Exception:  # noqa: BLE001
            return None
        return tuple(sorted(
            (m.width, m.height, m.left, m.top, bool(m.is_primary)) for m in ms))

    def _poll_display(self):
        """主线程轮询：显示器配置变化则刷新（不依赖后台 CFRunLoop，避免 GIL 崩溃）。"""
        if not self.watch_enabled.get():
            self._display_poll_id = None
            return
        try:
            sig = self._monitor_signature()
            if sig is not None and sig != self._last_mon_sig:
                self._last_mon_sig = sig
                # 插拔后屏幕配置往往还要再变几次（分辨率握手、主屏切换）。
                # Moom 唤醒恢复的做法是先等屏稳定再套布局（standing by for
                # additional displays）；这里同样延迟 2.5s，签名再变就重排
                # 等待，稳定在同一个签名后才真正套用情景/布局。
                self._pending_mon_sig = sig
                if self._display_settle_id is not None:
                    try:
                        self.root.after_cancel(self._display_settle_id)
                    except Exception:  # noqa: BLE001
                        pass
                self._display_settle_id = self.root.after(
                    2500, self._apply_display_change_if_settled)
        except Exception:  # noqa: BLE001
            pass
        if self.watch_enabled.get():
            self._display_poll_id = self.root.after(1500, self._poll_display)

    def _apply_display_change_if_settled(self):
        """延迟到期且签名未再变化时才处理显示器变化（等待配置稳定）。"""
        self._display_settle_id = None
        if not self.watch_enabled.get():
            self._pending_mon_sig = None
            return
        try:
            sig = self._monitor_signature()
        except Exception:  # noqa: BLE001
            sig = None
        if sig is not None and sig != self._pending_mon_sig:
            # 等待期间配置又变了：重置等待窗口，避免在中间态上套布局
            self._pending_mon_sig = sig
            self._display_settle_id = self.root.after(
                2500, self._apply_display_change_if_settled)
            return
        self._pending_mon_sig = None
        self._handle_display_change()

    def _handle_display_change(self):
        """显示器配置变化后：刷新列表，并按当前组合套用匹配情景（F2）。

        优先级：① 匹配到且开启自动应用的情景 → 套用；② 旧的"重应用上次壁纸方案"；
        ③ 仅刷新列表。
        """
        self.refresh_monitors()
        try:
            self._refresh_scenario_ui()
        except Exception:  # noqa: BLE001
            pass
        if getattr(self, "scen_auto", None) is not None and self.scen_auto.get():
            try:
                _sig, scen = scenarios.match(self.monitors)
            except Exception:  # noqa: BLE001
                scen = None
            if scen and scen.get("auto_apply", True):
                applied = self._apply_scenario(_sig, scen, silent=True)
                if applied:
                    self.status_var.set(
                        f"检测到显示器变化：已套用情景「{scen.get('name')}」"
                        f"（{' + '.join(applied)}）")
                    return
        name = settings.load().get("last_wallpaper_profile")
        if self.auto_apply_watch.get() and name and profiles.load_profiles().get(name):
            try:
                profiles.apply_profile(name)
                self.status_var.set(f"检测到显示器变化：已刷新并重应用方案「{name}」")
            except Exception as e:  # noqa: BLE001
                self.status_var.set(f"检测到显示器变化：已刷新（重应用失败：{e}）")
        else:
            self.status_var.set("检测到显示器变化：已刷新显示器列表")

    # ---------- 情景模式（F2）----------
    def _sync_scenario_options(self):
        """同步情景分区"壁纸方案/布局"下拉候选（构建早期属性未就绪时静默忽略）。"""
        try:
            self.scen_profile_cb["values"] = list(profiles.load_profiles().keys())
            self.scen_layout_cb["values"] = layouts.list_layouts()
        except Exception:  # noqa: BLE001
            pass

    def _build_scenarios(self, parent):
        f = ttk.LabelFrame(parent, text="情景模式（按显示器组合自动套用）")
        f.pack(fill="x", padx=8, pady=6)
        ttk.Label(
            f,
            text="把「壁纸方案 + 窗口布局」绑定到一组显示器；插拔或重排后自动套用对应情景。",
            justify="left", wraplength=620,
        ).pack(anchor="w", padx=4, pady=(0, 4))

        self._scenario_sig_var = tk.StringVar()
        cur = ttk.Frame(f)
        cur.pack(fill="x", padx=4, pady=2)
        ttk.Label(cur, text="当前组合:").pack(side="left")
        ttk.Label(cur, textvariable=self._scenario_sig_var,
                  foreground="#0a5").pack(side="left", padx=4)

        bind_row = ttk.Frame(f)
        bind_row.pack(fill="x", padx=4, pady=2)
        ttk.Label(bind_row, text="壁纸方案:").pack(side="left")
        self.scen_profile_var = tk.StringVar()
        self.scen_profile_cb = ttk.Combobox(
            bind_row, textvariable=self.scen_profile_var, width=14, state="readonly")
        self.scen_profile_cb.pack(side="left", padx=4)
        ttk.Label(bind_row, text="布局:").pack(side="left")
        self.scen_layout_var = tk.StringVar()
        self.scen_layout_cb = ttk.Combobox(
            bind_row, textvariable=self.scen_layout_var, width=14, state="readonly")
        self.scen_layout_cb.pack(side="left", padx=4)
        ttk.Button(bind_row, text="绑定当前组合",
                   command=self._bind_scenario).pack(side="left", padx=3)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=4, pady=2)
        self.scen_list = tk.Listbox(row, height=4, activestyle="none")
        self.scen_list.pack(side="left", fill="x", expand=True)
        btns = ttk.Frame(row)
        btns.pack(side="left", padx=4, fill="y")
        ttk.Button(btns, text="应用", command=self._apply_scenario_selected).pack(fill="x", pady=1)
        ttk.Button(btns, text="删除", command=self._delete_scenario).pack(fill="x", pady=1)

        self.scen_auto = tk.BooleanVar(
            value=bool(settings.load().get("scenario_auto_apply", True)))
        ttk.Checkbutton(f, text="显示器变化时自动套用匹配情景",
                        variable=self.scen_auto,
                        command=self._save_scenario_prefs).pack(anchor="w", padx=4, pady=2)

        # ---- 定时套用：到点自动套用指定情景（每天一次）----
        tf = ttk.Frame(f)
        tf.pack(fill="x", padx=4, pady=(2, 2))
        ttk.Label(tf, text="定时套用").pack(side="left")
        self.scen_time_sig_var = tk.StringVar()
        self.scen_time_sig_cb = ttk.Combobox(tf, textvariable=self.scen_time_sig_var,
                                             width=18, state="readonly")
        self.scen_time_sig_cb.pack(side="left", padx=2)
        self.scen_time_var = tk.StringVar(value="09:00")
        ttk.Entry(tf, textvariable=self.scen_time_var, width=6).pack(side="left", padx=2)
        ttk.Button(tf, text="添加", command=self._add_scenario_time).pack(side="left", padx=2)
        ttk.Button(tf, text="删除", command=self._delete_scenario_time).pack(side="left", padx=2)

        ttk.Label(f, text="到点自动套用对应情景（每天一次，时间格式 HH:MM）",
                  foreground="#666").pack(anchor="w", padx=4)
        self.scen_time_list = tk.Listbox(f, height=3, activestyle="none")
        self.scen_time_list.pack(fill="x", padx=4, pady=2)
        self._refresh_scenario_ui()

    def _save_scenario_prefs(self):
        s = settings.load()
        s["scenario_auto_apply"] = self.scen_auto.get()
        settings.save(s)

    def _current_signature(self):
        """当前显示器组合签名：优先用已枚举结果，避免重复拉起 system_profiler。"""
        ms = getattr(self, "monitors", None) or []
        if not ms:
            try:
                ms = b.enum_monitors(force=True)
            except Exception:  # noqa: BLE001
                ms = []
        return scenarios.monitors_signature(ms)

    def _refresh_scenario_ui(self):
        """刷新"当前组合"文本、下拉候选与已绑定情景列表。"""
        sig = self._current_signature()
        try:
            self._scenario_sig_var.set(scenarios.describe(sig))
        except Exception:  # noqa: BLE001
            pass
        self._sync_scenario_options()
        self.scen_list.delete(0, "end")
        self._scen_rows = []
        for s, scen in sorted(scenarios.list_scenarios().items()):
            bits = []
            if scen.get("profile"):
                bits.append(f"壁纸「{scen['profile']}」")
            if scen.get("layout"):
                bits.append(f"布局「{scen['layout']}」")
            label = f"{scen.get('name') or scenarios.describe(s)}  →  " + (
                " + ".join(bits) if bits else "（未绑定预设）")
            if not scen.get("auto_apply", True):
                label += "  [手动]"
            if s == sig:
                label = "★ " + label
            self.scen_list.insert("end", label)
            self._scen_rows.append(s)
        self._refresh_scenario_time_ui()

    def _refresh_scenario_time_ui(self):
        """刷新"定时套用"的情景下拉候选与已设定列表。"""
        try:
            sigs = scenarios.list_scenarios()
            names = [(scen.get("name") or scenarios.describe(s))
                     for _s, scen in sorted(sigs.items())]
            self.scen_time_sig_cb["values"] = names
            if names and self.scen_time_sig_var.get() not in names:
                self.scen_time_sig_var.set(names[0])
        except Exception:  # noqa: BLE001
            pass
        self._scen_time_rows = []
        try:
            self.scen_time_list.delete(0, "end")
            for t in scenarios.list_time_triggers():
                sig = t.get("signature")
                scen = scenarios.get(sig)
                if scen:
                    label = scen.get("name") or scenarios.describe(sig)
                else:
                    label = sig or "?"
                if not t.get("enabled", True):
                    label += "  [停用]"
                self.scen_time_list.insert("end", f"{label}  {t.get('time')}")
                self._scen_time_rows.append(sig)
        except Exception:  # noqa: BLE001
            pass

    def _signature_by_name(self, name):
        """按情景显示名反查签名；找不到返回 None。"""
        try:
            for s, scen in scenarios.list_scenarios().items():
                if (scen.get("name") or scenarios.describe(s)) == name:
                    return s
        except Exception:  # noqa: BLE001
            pass
        return None

    def _selected_index(self, listbox):
        """Listbox 当前选中索引；无选中返回 None。"""
        try:
            sel = listbox.curselection()
            return int(sel[0]) if sel else None
        except Exception:  # noqa: BLE001
            return None

    def _add_scenario_time(self):
        """添加一个定时套用（情景 + HH:MM）。"""
        name = self.scen_time_sig_var.get()
        hhmm = (self.scen_time_var.get() or "").strip()
        sig = self._signature_by_name(name) or self._current_signature()
        try:
            ok, _items = scenarios.add_time_trigger(sig, hhmm)
        except Exception:  # noqa: BLE001
            ok = False
        self._refresh_scenario_time_ui()
        self.status_var.set(
            f"已添加定时：{name} @ {hhmm}" if ok else f"添加失败（时间需为 HH:MM）：{hhmm}")

    def _delete_scenario_time(self):
        idx = self._selected_index(self.scen_time_list)
        if idx is None:
            self.status_var.set("请先在定时列表中选中一条")
            return
        scenarios.remove_time_trigger(idx)
        self._refresh_scenario_time_ui()
        self.status_var.set("已删除该定时套用")

    def _bind_scenario(self):
        sig = self._current_signature()
        if not sig:
            messagebox.showwarning("提示", "未检测到显示器，无法绑定情景")
            return
        profile = (self.scen_profile_var.get() or "").strip() or None
        layout = (self.scen_layout_var.get() or "").strip() or None
        if not profile and not layout:
            messagebox.showwarning("提示", "请至少选择壁纸方案或窗口布局其中之一")
            return
        scen = scenarios.bind(sig, layout=layout, profile=profile,
                              auto_apply=self.scen_auto.get())
        self._refresh_scenario_ui()
        self.status_var.set(f"已绑定情景：{scen['name']}")

    def _scenario_selected_signature(self):
        sel = self.scen_list.curselection()
        rows = getattr(self, "_scen_rows", [])
        if not sel or sel[0] >= len(rows):
            return None
        return rows[sel[0]]

    def _apply_scenario_selected(self):
        sig = self._scenario_selected_signature()
        if not sig:
            messagebox.showwarning("提示", "请先在列表中选择一个情景")
            return
        scen = scenarios.get(sig)
        if scen:
            self._apply_scenario(sig, scen)

    def _apply_scenario(self, signature, scen, silent=False):
        """套用一个情景：先壁纸方案，后窗口布局。返回已应用的描述列表。"""
        applied = []
        profile = scen.get("profile")
        if profile and profiles.load_profiles().get(profile):
            try:
                profiles.apply_profile(profile)
                applied.append(f"壁纸「{profile}」")
            except Exception as e:  # noqa: BLE001
                if not silent:
                    messagebox.showerror("错误", f"应用壁纸方案失败：\n{e}")
        layout = scen.get("layout")
        if layout and self._apply_layout_by_name(layout, silent=True):
            applied.append(f"布局「{layout}」")
        if applied:
            self.status_var.set(
                f"已套用情景「{scen.get('name')}」：{' + '.join(applied)}")
        elif not silent:
            self.status_var.set(
                f"情景「{scen.get('name')}」暂无可用预设（方案/布局可能已被删除）")
        return applied

    def _delete_scenario(self):
        sig = self._scenario_selected_signature()
        if not sig:
            messagebox.showwarning("提示", "请先在列表中选择一个情景")
            return
        scen = scenarios.get(sig) or {}
        if not messagebox.askyesno(
                "确认", f"删除情景「{scen.get('name') or scenarios.describe(sig)}」？"):
            return
        scenarios.unbind(sig)
        self._refresh_scenario_ui()
        self.status_var.set("已删除情景")

    # ---------- 拖拽吸附预览（F3）----------
    def _is_own_window(self, owner):
        """判断窗口是否属于本程序（避免把自己的浮层/主窗当成吸附目标）。"""
        o = (owner or "").lower()
        return "python" in o or "多屏管理器" in o or "multimon" in o

    def _build_drag_snap(self, parent):
        f = ttk.LabelFrame(parent, text="拖拽吸附预览")
        f.pack(fill="x", padx=8, pady=6)
        ttk.Label(
            f,
            text="开启后，把窗口拖到屏幕左/右/上/下边缘时，半透明高亮将吸附的区域；"
                 "松手即吸附到该区域（可用「撤销」还原）。需辅助功能授权。",
            justify="left", wraplength=620,
        ).pack(anchor="w", padx=4, pady=(0, 4))
        self.drag_snap_enabled = tk.BooleanVar(
            value=bool(settings.load().get("drag_snap_enabled", False)))
        ttk.Checkbutton(f, text="启用拖拽吸附预览（拖到边缘 → 高亮 → 松手吸附）",
                        variable=self.drag_snap_enabled,
                        command=self._toggle_drag_snap).pack(anchor="w", padx=4, pady=2)
        self._toggle_drag_snap()

    def _toggle_drag_snap(self):
        s = settings.load()
        s["drag_snap_enabled"] = self.drag_snap_enabled.get()
        settings.save(s)
        if self.drag_snap_enabled.get():
            self._start_drag_snap()
        else:
            self._stop_drag_snap()

    def _start_drag_snap(self):
        if self._drag_watcher is not None:
            return
        try:
            watcher = b.DragSnapWatcher()
            watcher.set_callback(self._on_mouse_event)
            if not watcher.start():
                self.status_var.set("拖拽吸附：无法启动鼠标监听（请确认已授予辅助功能权限）")
                return
            self._drag_watcher = watcher
            self.status_var.set("拖拽吸附预览已启用")
        except Exception as e:  # noqa: BLE001
            self.status_var.set(f"拖拽吸附启动失败：{e}")

    def _stop_drag_snap(self):
        if self._drag_watcher is not None:
            try:
                self._drag_watcher.stop()
            except Exception:  # noqa: BLE001
                pass
            self._drag_watcher = None
        if self._snap_preview is not None:
            self._snap_preview.hide()

    def _snap_overlay(self):
        if self._snap_preview is None:
            try:
                self._snap_preview = snap_preview.SnapPreview(self.root)
            except Exception:  # noqa: BLE001
                self._snap_preview = None
        return self._snap_preview

    def _snap_zone_at(self, x, y):
        """鼠标位置 → 吸附目标区域 (x, y, w, h)；不在边缘命中时返回 None。

        只用内存里的 self.monitors：事件回调必须极快返回，不能触发枚举或子进程，
        否则事件 tap 会因超时被系统禁用。
        """
        for m in (self.monitors or []):
            if m.left <= x < m.left + m.width and m.top <= y < m.top + m.height:
                rx = (x - m.left) / max(m.width, 1)
                ry = (y - m.top) / max(m.height, 1)
                wl, wt = m.work_left, m.work_top
                ww, wh = m.work_width, m.work_height
                half_w, half_h = ww // 2, wh // 2
                if rx <= 0.12:
                    return (wl, wt, half_w, wh)
                if rx >= 0.88:
                    return (wl + ww - half_w, wt, half_w, wh)
                if ry <= 0.08:
                    return (wl, wt, ww, half_h)
                if ry >= 0.92:
                    return (wl, wt + wh - half_h, ww, half_h)
                return None
        return None

    def _on_mouse_event(self, stage, x, y):
        """拖拽监听回调：**运行在 CGEventTap 原生回调上下文中**。

        这里只记录最新事件并调度一次主循环任务，绝不能做 Tk 操作或窗口枚举：
        1) 原生回调必须立刻返回，否则事件 tap 会因超时被系统禁用；
        2) 在 tap 回调里重入 Tk（deiconify/lift/update_idletasks）或调用 objc
           会触发原生崩溃（Segmentation fault，Python 的 try/except 抓不到）。
        """
        try:
            self._drag_pending = (stage, int(x), int(y))
            if self._drag_job is None:
                self._drag_job = self.root.after_idle(self._process_drag_event)
        except Exception:  # noqa: BLE001
            pass

    def _process_drag_event(self):
        """在 Tk 主循环里处理暂存的拖拽事件（唯一允许做 UI/窗口操作的地方）。"""
        self._drag_job = None
        try:
            stage, x, y = self._drag_pending
        except Exception:  # noqa: BLE001
            return
        try:
            if stage == "down":
                self._drag_zone = None
                if self._snap_preview is not None:
                    self._snap_preview.hide()
                return
            if stage == "drag":
                now = time.monotonic()
                if now - self._last_zone_ts < 0.05:  # 节流到约 20fps
                    # 未到节流窗口：稍后重调度，避免吞掉最后一次移动
                    try:
                        self._drag_job = self.root.after(20, self._process_drag_event)
                    except Exception:  # noqa: BLE001
                        pass
                    return
                self._last_zone_ts = now
                zone = self._snap_zone_at(x, y)
                self._drag_zone = zone
                ov = self._snap_overlay()
                if ov is None:
                    return
                if zone:
                    ov.show(zone[0], zone[1], zone[2], zone[3])
                else:
                    ov.hide()
                return
            if stage == "up":
                zone = self._drag_zone
                self._drag_zone = None
                if self._snap_preview is not None:
                    self._snap_preview.hide()
                if not zone:
                    return
                # 枚举窗口可能较慢/起子进程：只能在主循环里做，不能在 tap 回调里做
                wins = [w for w in b.list_target_windows()
                        if not self._is_own_window(w.get("owner"))]
                if not wins:
                    return
                b.set_window_rect(wins[0]["hwnd"], zone[0], zone[1], zone[2], zone[3],
                                  activate=False)
                self.status_var.set("已吸附窗口到预览区域（可用「撤销」还原）")
        except Exception:  # noqa: BLE001
            pass

    # ---------- 窗口规则引擎（F5）----------
    def _build_rules(self, parent):
        f = ttk.LabelFrame(parent, text="窗口规则")
        f.pack(fill="x", padx=8, pady=6)
        ttk.Label(
            f,
            text="按应用名/标题把窗口自动放到指定屏与位置；规则自上而下匹配，先命中先执行。",
            justify="left", wraplength=620,
        ).pack(anchor="w", padx=4, pady=(0, 4))

        self.rule_rows = tk.Listbox(f, height=4, activestyle="none")
        self.rule_rows.pack(fill="x", padx=4, pady=2)
        self.rule_rows.bind("<Double-Button-1>", lambda _e: self._toggle_rule())

        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=4, pady=2)
        ttk.Button(btns, text="启用/停用", command=self._toggle_rule).pack(side="left", padx=2)
        ttk.Button(btns, text="上移", command=lambda: self._move_rule(-1)).pack(side="left", padx=2)
        ttk.Button(btns, text="下移", command=lambda: self._move_rule(1)).pack(side="left", padx=2)
        ttk.Button(btns, text="删除", command=self._delete_rule).pack(side="left", padx=2)
        ttk.Button(btns, text="立即应用", command=self._apply_rules_now).pack(side="left", padx=2)

        form = ttk.Frame(f)
        form.pack(fill="x", padx=4, pady=2)
        ttk.Label(form, text="匹配").pack(side="left")
        self.rule_field_var = tk.StringVar(value="应用名")
        ttk.Combobox(form, textvariable=self.rule_field_var, width=7, state="readonly",
                     values=["应用名", "标题"]).pack(side="left", padx=2)
        self.rule_pattern_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.rule_pattern_var, width=16).pack(side="left", padx=2)
        self.rule_regex_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="正则", variable=self.rule_regex_var).pack(side="left", padx=2)

        form2 = ttk.Frame(f)
        form2.pack(fill="x", padx=4, pady=2)
        ttk.Label(form2, text="目标屏").pack(side="left")
        self.rule_mon_var = tk.StringVar(value=AUTO_RULE_MON)
        self.rule_mon_cb = ttk.Combobox(form2, textvariable=self.rule_mon_var, width=16,
                                        state="readonly", values=[AUTO_RULE_MON])
        self.rule_mon_cb.pack(side="left", padx=2)
        ttk.Label(form2, text="位置").pack(side="left", padx=(8, 0))
        self.rule_zone_var = tk.StringVar(value="整屏")
        ttk.Combobox(form2, textvariable=self.rule_zone_var, width=8, state="readonly",
                     values=["整屏", "左半屏", "右半屏", "上半屏", "下半屏"]).pack(side="left", padx=2)
        ttk.Button(form2, text="添加规则", command=self._add_rule).pack(side="left", padx=4)

        # 内置规则模板：一键导入常用组合（跨平台关键词，不依赖具体显示器）
        form3 = ttk.Frame(f)
        form3.pack(fill="x", padx=4, pady=2)
        ttk.Label(form3, text="模板:").pack(side="left")
        self.rule_tpl_var = tk.StringVar()
        self.rule_tpl_cb = ttk.Combobox(form3, textvariable=self.rule_tpl_var,
                                        width=26, state="readonly",
                                        values=list(rules.TEMPLATES))
        self.rule_tpl_cb.pack(side="left", padx=2)
        if rules.TEMPLATES:
            self.rule_tpl_var.set(next(iter(rules.TEMPLATES)))
        ttk.Button(form3, text="追加导入",
                   command=lambda: self._import_rule_template(False)).pack(side="left", padx=2)
        ttk.Button(form3, text="替换导入",
                   command=lambda: self._import_rule_template(True)).pack(side="left", padx=2)
        ttk.Button(form3, text="从当前窗口学习",
                   command=self._learn_rules_from_windows).pack(side="left", padx=(10, 2))

        self.rules_auto = tk.BooleanVar(
            value=bool(settings.load().get("rules_auto_apply", False)))
        ttk.Checkbutton(f, text="自动应用（新窗口出现时）",
                        variable=self.rules_auto,
                        command=self._save_rules_prefs).pack(anchor="w", padx=4, pady=2)
        self._refresh_rule_list()

    def _save_rules_prefs(self):
        s = settings.load()
        s["rules_auto_apply"] = self.rules_auto.get()
        settings.save(s)

    def _import_rule_template(self, replace=False):
        """一键导入内置规则模板（replace=True 时先清空现有规则）。"""
        name = self.rule_tpl_var.get()
        if not name:
            self.status_var.set("请先选择一个模板")
            return
        try:
            added, total = rules.apply_template(name, replace=replace)
        except Exception:  # noqa: BLE001
            self.status_var.set("导入模板失败")
            return
        self._refresh_rule_list()
        self.status_var.set(f"已导入模板「{name}」：新增 {added} 条，当前共 {total} 条")

    def _learn_rules_from_windows(self):
        """从当前窗口布局一键生成规则（同应用名只取一次，按所在屏 + 位置推断）。"""
        try:
            wins = [w for w in b.list_target_windows()
                    if not self._is_own_window(w.get("owner"))]
        except Exception:  # noqa: BLE001
            wins = []
        learned = rules.learn_from_windows(self.monitors, wins)
        if not learned:
            self.status_var.set("未发现可学习的窗口（需已授权、且能枚举到窗口）")
            return
        added, total = rules.add_rules(learned)
        self._refresh_rule_list()
        self.status_var.set(
            f"已从当前窗口学习：识别 {len(learned)} 个应用，新增 {added} 条规则（共 {total} 条）")

    # ---------- 壁纸幻灯片 ----------
    def _slideshow_refresh(self):
        """把幻灯片配置同步到 UI 控件。"""
        try:
            cfg = slideshow.get_config()
        except Exception:  # noqa: BLE001
            return
        self.slideshow_enabled_var.set(cfg["enabled"])
        self.slideshow_shuffle_var.set(cfg["shuffle"])
        try:
            self.slideshow_interval_var.set(str(max(1, cfg["interval"] // 60)))
        except Exception:  # noqa: BLE001
            pass
        try:
            self.slideshow_list.delete(0, "end")
            cur = slideshow.current_path()
            for p in cfg["items"]:
                mark = "★ " if (cur and p == cur) else "   "
                self.slideshow_list.insert("end", mark + os.path.basename(p))
            state = "已启用" if cfg["enabled"] else "未启用"
            extra = f"，当前：{os.path.basename(cur)}" if cur else ""
            self.slideshow_status_var.set(f"{len(cfg['items'])} 张｜{state}{extra}")
        except Exception:  # noqa: BLE001
            pass

    def _slideshow_minutes(self):
        """读取间隔输入框（分钟），非法时回退 5 分钟。"""
        try:
            return max(1, int(float(self.slideshow_interval_var.get() or "5")))
        except Exception:  # noqa: BLE001
            return 5

    def _slideshow_schedule(self, secs):
        """（重新）安排下一次切换：主线程 after，安全且随窗口关闭自动取消。"""
        self._slideshow_cancel()
        try:
            self._slideshow_job = self.root.after(max(10, int(secs)) * 1000,
                                                  self._slideshow_tick)
        except Exception:  # noqa: BLE001
            self._slideshow_job = None

    def _slideshow_cancel(self):
        if self._slideshow_job is not None:
            try:
                self.root.after_cancel(self._slideshow_job)
            except Exception:  # noqa: BLE001
                pass
            self._slideshow_job = None

    def _apply_single_async(self, path, position, on_done=None):
        """后台应用单张壁纸（macOS 走线程，Windows 同步），完成后回主线程回调。

        壁纸应用是 osascript 子进程（macOS）或 COM 调用，耗时 0.3~1s；放后台可避免
        幻灯片轮播 / 立即切换时主界面冻结。Windows 端 COM 跨线程不安全，保持同步。
        """
        def _work():
            try:
                b.apply_single(path, position)
                ok, err = True, None
            except Exception as e:  # noqa: BLE001
                ok, err = False, e
            self.root.after(0, self._on_apply_single_done, ok, err, on_done)
        if b.IS_MAC:
            threading.Thread(target=_work, daemon=True).start()
        else:
            _work()

    def _on_apply_single_done(self, ok, err, on_done):
        if err is not None:
            self.status_var.set("应用壁纸失败")
        if on_done is not None:
            try:
                on_done(ok, err)
            except Exception:  # noqa: BLE001
                pass

    def _slideshow_toggle(self):
        """启用/停用自动轮换。"""
        enabled = self.slideshow_enabled_var.get()
        cfg = slideshow.set_config(
            enabled=enabled,
            interval=self._slideshow_minutes() * 60,
            shuffle=self.slideshow_shuffle_var.get(),
            position=self.fit_var.get() or "fill",
        )
        if enabled and cfg["items"]:
            self._slideshow_schedule(cfg["interval"])
            self.status_var.set(f"幻灯片已启用：每 {cfg['interval'] // 60} 分钟切换")
        else:
            self._slideshow_cancel()
            self.status_var.set("幻灯片无图片，请先「添加图片」" if enabled else "幻灯片已停用")
        self._slideshow_refresh()

    def _slideshow_tick(self):
        """定时切换壁纸：切换动作放后台线程，避免主界面冻结。"""
        self._slideshow_job = None
        try:
            cfg = slideshow.get_config()
        except Exception:  # noqa: BLE001
            return
        if not cfg["enabled"] or not cfg["items"]:
            return
        p = slideshow.next_path()
        position = cfg["position"]
        interval = cfg["interval"]
        if not p:
            self._slideshow_refresh()
            self._slideshow_schedule(interval)
            return

        def _after(ok, err):
            self._slideshow_refresh()
            self._slideshow_schedule(interval)

        self._apply_single_async(p, position, on_done=_after)

    def _slideshow_next_now(self):
        """立即切到下一张（不影响既有调度）；切换动作放后台线程。"""
        p = slideshow.next_path()
        if not p:
            self.status_var.set("幻灯片没有图片，请先添加")
            return
        position = slideshow.get_config()["position"]
        self.status_var.set(f"正在切换：{os.path.basename(p)}")

        def _after(ok, err):
            self._slideshow_refresh()
            self.status_var.set(f"已切换到：{os.path.basename(p)}")

        self._apply_single_async(p, position, on_done=_after)

    def _slideshow_add(self):
        """选择图片加入幻灯片。"""
        paths = filedialog.askopenfilenames(
            title="选择幻灯片图片",
            filetypes=[("图片", "*.jpg;*.jpeg;*.png;*.bmp")])
        if not paths:
            return
        try:
            cfg = slideshow.add_items(list(paths))
        except Exception:  # noqa: BLE001
            self.status_var.set("添加图片失败")
            return
        self._slideshow_refresh()
        self.status_var.set(f"已添加：共 {len(cfg['items'])} 张")

    def _slideshow_clear(self):
        """清空幻灯片图片并停止调度。"""
        slideshow.clear_items()
        self._slideshow_cancel()
        self._slideshow_refresh()
        self.status_var.set("幻灯片已清空")

    def _slideshow_remove_selected(self):
        """移除列表中选中的图片。"""
        idx = self._selected_index(self.slideshow_list)
        if idx is None:
            self.status_var.set("请先在幻灯片列表中选中一张")
            return
        slideshow.remove_item(idx)
        self._slideshow_refresh()
        self.status_var.set("已移除该图片")

    def _slideshow_restore(self):
        """启动后恢复：同步 UI，并在上次为启用状态时续跑调度。"""
        self._slideshow_refresh()
        try:
            cfg = slideshow.get_config()
        except Exception:  # noqa: BLE001
            return
        if cfg["enabled"] and cfg["items"]:
            self._slideshow_schedule(cfg["interval"])

    def _refresh_rule_list(self):
        self.rule_rows.delete(0, "end")
        self._rule_mon_map = {AUTO_RULE_MON: None}
        for m in (self.monitors or []):
            self._rule_mon_map[self._monitor_display_name(m)] = m.device_path
        for r in rules.list_rules():
            device = r.get("monitor")
            if device:
                m = rules.find_monitor(self.monitors, device)
                mon_label = self._monitor_display_name(m) if m else "（已断开）"
            else:
                mon_label = "当前屏"
            zone = rules.ZONE_LABELS.get(r.get("zone", "full"), "整屏")
            mark = "✓" if r.get("enabled", True) else "✗"
            field = "应用名" if r.get("field", "owner") == "owner" else "标题"
            self.rule_rows.insert(
                "end", f"[{mark}] {field}含「{r.get('pattern', '')}」→ {mon_label} / {zone}")
        try:
            self.rule_mon_cb["values"] = list(self._rule_mon_map.keys())
        except Exception:  # noqa: BLE001
            pass

    def _selected_rule_index(self):
        sel = self.rule_rows.curselection()
        return sel[0] if sel else None

    def _add_rule(self):
        pattern = (self.rule_pattern_var.get() or "").strip()
        if not pattern:
            messagebox.showwarning("提示", "请填写要匹配的应用名或标题关键词")
            return
        rule = {
            "enabled": True,
            "field": "owner" if self.rule_field_var.get() == "应用名" else "title",
            "pattern": pattern,
            "regex": bool(self.rule_regex_var.get()),
            "monitor": self._rule_mon_map.get(self.rule_mon_var.get()),
            "zone": _ZONE_BY_LABEL.get(self.rule_zone_var.get(), "full"),
        }
        rules.add_rule(rule)
        self.rule_pattern_var.set("")
        self._refresh_rule_list()
        self.status_var.set(f"已添加规则：{pattern}")

    def _toggle_rule(self):
        i = self._selected_rule_index()
        if i is None:
            return
        rules.toggle_rule(i)
        self._refresh_rule_list()

    def _move_rule(self, delta):
        i = self._selected_rule_index()
        if i is None:
            return
        rules.move_rule(i, delta)
        self._refresh_rule_list()
        j = i + delta
        try:
            self.rule_rows.selection_clear(0, "end")
            self.rule_rows.selection_set(j)
        except Exception:  # noqa: BLE001
            pass

    def _delete_rule(self):
        i = self._selected_rule_index()
        if i is None:
            messagebox.showwarning("提示", "请先选择一条规则")
            return
        rules.delete_rule(i)
        self._refresh_rule_list()
        self.status_var.set("已删除规则")

    def _apply_rules_now(self):
        """对当前所有窗口立即应用规则（手动触发）。"""
        wins = [w for w in b.list_target_windows()
                if not self._is_own_window(w.get("owner"))]
        active = [r for r in rules.list_rules() if r.get("enabled", True)]
        if not wins or not active:
            self.status_var.set("规则引擎：没有可应用的窗口或已启用的规则")
            return
        moved = 0
        for w in wins:
            for r in active:
                if rules.match_window(r, w):
                    try:
                        if rules.apply_to_window(r, w, self.monitors):
                            moved += 1
                    except Exception:  # noqa: BLE001
                        pass
                    break  # 先命中先执行
        self.status_var.set(f"规则引擎：已应用规则，移动 {moved} 个窗口")

    def _poll_rules(self):
        """主线程轮询：新窗口套规则；窗口移动/出现触发触发器；并检查情景定时触发。

        Moom 用 AXObserver 事件驱动代替全量轮询；macOS 的 AX 回调与
        Tk 主循环并发容易踩原生崩溃，这里第一版先保留轮询兜底，
        后续版本再做事件驱动。
        """
        try:
            auto = (getattr(self, "rules_auto", None) is not None
                    and self.rules_auto.get())
            has_trig = triggers.any_enabled()
            if auto or has_trig:
                # 一次枚举供规则与触发器共用，避免每轮重复调用窗口枚举
                wins = [w for w in b.list_target_windows()
                        if not self._is_own_window(w.get("owner"))]
                if auto:
                    self._scan_new_windows(wins)
                if has_trig:
                    self._check_triggers(wins)
        except Exception:  # noqa: BLE001
            pass
        # 情景定时套用：到点（分钟级）且当天未触发过则自动套用
        try:
            for sig in scenarios.due_time_triggers():
                self._apply_scenario_by_signature(sig)
                self.status_var.set("定时触发：已套用情景")
        except Exception:  # noqa: BLE001
            pass
        self.root.after(1500, self._poll_rules)

    def _scan_new_windows(self, wins=None):
        """扫描新出现的窗口并套用规则；wins 可传入已枚举结果以复用（省一次枚举）。"""
        if wins is None:
            wins = [w for w in b.list_target_windows()
                    if not self._is_own_window(w.get("owner"))]
        current = {w["hwnd"] for w in wins}
        if self._rule_seen is None:
            self._rule_seen = current  # 首次只记录，避免把已开窗口全移一遍
            return
        new = [w for w in wins if w["hwnd"] not in self._rule_seen]
        self._rule_seen = current
        if not new:
            return
        active = [r for r in rules.list_rules() if r.get("enabled", True)]
        if not active:
            return
        moved = 0
        for w in new:
            for r in active:
                if rules.match_window(r, w):
                    try:
                        if rules.apply_to_window(r, w, self.monitors):
                            moved += 1
                    except Exception:  # noqa: BLE001
                        pass
                    break
        if moved:
            self.status_var.set(f"规则引擎：已自动分配 {moved} 个新窗口")

    # ---------- 托盘命令通道 ----------
    def _start_command_poll(self):
        """定时轮询托盘子进程发来的命令（每 800ms）。"""
        self._poll_command()
        self.root.after(800, self._start_command_poll)

    def _poll_command(self):
        cmd = cmd_channel.take()
        if not cmd:
            return
        if cmd == "refresh":
            self.refresh_monitors()
        elif cmd == "apply_layout":
            self._apply_last_layout()
        elif cmd == "open":
            self.show()
        elif cmd == "toggle_slideshow":
            # 托盘「开始/停止幻灯片」：走 UI 开关，无模态框（状态栏反馈结果）
            enabled = not bool(slideshow.get_config()["enabled"])
            self.slideshow_enabled_var.set(enabled)
            self._slideshow_toggle()
        elif cmd.startswith("apply_scenario:"):
            self._apply_scenario_by_signature(cmd.split(":", 1)[1])
        elif cmd == "exit":
            # 托盘「退出」：命令发回 Tk 主线程安全退出（托盘线程只发命令）
            self.quit()

    def _apply_last_layout(self):
        """应用最近保存/使用的窗口布局（供托盘一键调用）。"""
        name = settings.load().get("last_layout")
        if not name or not layouts.load_layout(name):
            self.status_var.set("托盘：尚无窗口布局方案，请先在主界面保存")
            self.show()
            return
        self.layout_var.set(name)
        self._apply_layout()

    def _apply_scenario_by_signature(self, signature):
        """套用指定签名对应的情景（供托盘"切换情景"与触发器调用）。

        返回实际应用的描述列表（为空表示情景存在但没有可用预设）。
        """
        scen = scenarios.get(signature)
        if not scen:
            self.status_var.set(
                f"托盘：情景不存在（{scenarios.describe(signature)}）")
            return []
        return self._apply_scenario(signature, scen)

    # ---------- 逻辑 ----------
    def refresh_monitors(self):
        self.monitors = b.enum_monitors(force=True)
        for w in self.mon_frame.winfo_children():
            w.destroy()
        self.mon_rows = []
        self.status_var.set(f"检测到 {len(self.monitors)} 块显示器")
        for m in self.monitors:
            row = ttk.Frame(self.mon_frame)
            row.pack(fill="x", pady=2)
            label = f"{self._monitor_display_name(m)}  {m.width}x{m.height}" + (
                "  [主屏]" if m.is_primary else "")
            ttk.Label(row, text=label, width=30).pack(side="left")
            var = tk.StringVar()
            ttk.Button(row, text="选择", command=lambda v=var: self._pick(v)).pack(side="left")
            ttk.Entry(row, textvariable=var, width=32).pack(side="left", padx=4, fill="x", expand=True)
            # 显示器别名（F6）：直接在此行编辑，回车/失焦即保存
            avar = tk.StringVar(value=self.aliases.get(m.device_path, ""))
            ttk.Entry(row, textvariable=avar, width=16,
                      foreground="#555").pack(side="left", padx=4)
            avar.trace_add(
                "write",
                lambda *_a, dp=m.device_path, v=avar: self._on_alias_change(dp, v),
            )
            self.mon_rows.append({"device_path": m.device_path, "var": var})
        # 刷新后重绘布局可视化（点击屏幕可直接为该屏选壁纸）
        self._draw_layout()
        # 同步并排/竖排的显示器下拉选项
        self._refresh_monitor_choices()
        # 显示器数量变化后，同步「上手建议」卡片的屏数提示
        self._refresh_getting_started()

    def _draw_layout(self):
        """在 Canvas 上按比例画出各显示器相对位置。

        monitors_mac 返回的是 CoreGraphics 的全局拼接坐标：原点在主屏左上、
        x 向右为正、y 向下为正（与 Canvas 方向一致，故不做 y 翻转）。

        画布高度随宽度自适应（尽量铺满宽度），内容再在画布内居中，这样窗口
        放大/缩小时布局图都保持合适大小、不偏在角落。标签按矩形尺寸自适应
        （分级内容 + 动态字号 + 强制折行），避免文字溢出边界或与相邻屏重叠。
        """
        cv = self.layout_canvas
        cv.delete("all")
        ms = self.monitors
        if not ms:
            return
        min_x = min(m.left for m in ms)
        min_y = min(m.top for m in ms)
        world_w = max(m.left + m.width for m in ms) - min_x
        world_h = max(m.top + m.height for m in ms) - min_y
        cw = max(400, cv.winfo_width())
        pad = 14
        avail_w = max(cw - 2 * pad, 80)
        # 高度自适应：按宽度铺满算出的理想高度，限制在 [180, 320]，
        # 避免过矮看不清、过高把下面的面板挤出窗口。
        ideal_h = pad * 2 + (world_h / world_w) * avail_w
        target_h = int(max(180, min(ideal_h, 320)))
        if abs(cv.winfo_height() - target_h) > 4:
            cv.configure(height=target_h)
        ch = target_h
        scale = min(avail_w / world_w, (ch - 2 * pad) / world_h)
        # 内容整体在画布内居中（左右、上下留白均匀）
        off_x = (cw - world_w * scale) / 2
        off_y = (ch - world_h * scale) / 2
        for i, m in enumerate(ms):
            x1 = off_x + (m.left - min_x) * scale
            y1 = off_y + (m.top - min_y) * scale
            x2 = x1 + m.width * scale
            y2 = y1 + m.height * scale
            bw, bh = x2 - x1, y2 - y1
            fill = "#d8e6ff" if m.is_primary else "#e9e9e9"
            rid = cv.create_rectangle(x1, y1, x2, y2, fill=fill,
                                      outline="#5a5a5a", width=1.5)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            # 按矩形大小分级：太小只显示编号，否则编号 + 屏幕名 + 分辨率。
            # 主屏用 ★ 前缀（省宽度）；create_text 的 width 限制折行，保证不越界。
            head = f"★{i + 1}" if m.is_primary else f"{i + 1}"
            name = (self._monitor_display_name(m) or "").strip()
            res = f"{m.width}x{m.height}"
            pct = getattr(m, "scale_percent", 100)
            if pct and pct != 100:
                res += f" @{pct}%"
            if bw < 52 or bh < 22:
                txt, size = head, 8
            elif bw < 128 or bh < 66:
                txt, size = f"{head}. {name}\n{res}", 8
            else:
                txt, size = f"{head}. {name}\n{res}", 10
            cv.create_text(cx, cy, text=txt, font=("Helvetica", size), fill="#222",
                           width=max(int(bw) - 8, 16), justify="center")
            cv.tag_bind(rid, "<Button-1>",
                        lambda e, idx=i: self._on_screen_click(idx))

    def _on_window_resize(self, event):
        """窗口尺寸变化后防抖重绘布局图，让其随窗口大小合理适配。"""
        if event.widget is not self.root:
            return
        if getattr(self, "_resize_job", None) is not None:
            try:
                self.root.after_cancel(self._resize_job)
            except Exception:  # noqa: BLE001
                pass
        self._resize_job = self.root.after(120, self._draw_layout)

    def _on_screen_click(self, index):
        """点击可视化中的屏幕，直接为该屏选择壁纸。"""
        if 0 <= index < len(self.mon_rows):
            self._pick(self.mon_rows[index]["var"])

    def _current_mapping(self):
        position = self.fit_var.get()
        if self.mode_var.get() == "single":
            p = self.single_var.get()
            mapping = {m.device_path: p for m in self.monitors} if p else {}
            return mapping, position
        mapping = {}
        for r in self.mon_rows:
            p = r["var"].get()
            if p:
                mapping[r["device_path"]] = p
        return mapping, position

    def apply_wallpaper(self):
        """应用壁纸。耗时操作（macOS 上 osascript 子进程约 0.3~1s）放后台线程，
        避免按钮点击时主界面冻结；Windows 因 COM 跨线程不安全故保持同步。
        """
        mapping, position = self._current_mapping()
        if not mapping:
            messagebox.showwarning("提示", "请先为显示器选择图片")
            return
        # 校验图片路径存在
        for dev, img in list(mapping.items()):
            if img and not os.path.exists(img):
                messagebox.showwarning("提示", f"图片文件不存在:\n{img}")
                return
        # 部分屏幕未选图时提醒（per 模式）：已选的照常应用，未选的保持原样
        if self.mode_var.get() == "per" and len(mapping) < len(self.monitors):
            if not getattr(self, "_partial_hinted", False):
                self._partial_hinted = True
                messagebox.showwarning(
                    "提示",
                    f"有 {len(self.monitors) - len(mapping)} 块显示器未选择图片，"
                    "将保持当前壁纸。")
        use_single = self.mode_var.get() == "single"
        # 在主线程读取参数值后传入后台线程（线程内只读，避免共享 UI 状态）
        src = self.single_var.get() if use_single else mapping
        self.status_var.set("正在应用壁纸…")

        def _work():
            try:
                if use_single:
                    ok = b.apply_single(src, position)
                else:
                    ok = b.apply_per_monitor(src, position)
                err = None
            except Exception as e:  # noqa: BLE001
                ok, err = False, e
            self.root.after(0, self._wallpaper_applied, ok, err)

        if b.IS_MAC:
            threading.Thread(target=_work, daemon=True).start()
        else:
            _work()

    def _wallpaper_applied(self, ok, err):
        """壁纸应用完成的回调（主线程执行）。"""
        if err is not None:
            messagebox.showerror("错误", f"应用壁纸失败:\n{err}")
            self.status_var.set("应用壁纸失败")
            return
        messagebox.showinfo("完成", "壁纸已应用" if ok else "已用回退方式应用(单屏)")
        self.status_var.set("壁纸已应用")

    def _refresh_profile_list(self):
        self.profile_combo["values"] = list(profiles.load_profiles().keys())
        self._sync_scenario_options()

    def save_profile(self):
        name = self.profile_name.get().strip()
        if not name:
            messagebox.showwarning("提示", "请输入方案名称")
            return
        mapping, position = self._current_mapping()
        profiles.save_profile(name, mapping, position)
        self._refresh_profile_list()
        messagebox.showinfo("完成", f"已保存方案「{name}」")

    def apply_profile(self):
        name = self.profile_var.get()
        if not name:
            return
        allp = profiles.load_profiles()
        p = allp.get(name)
        if not p:
            messagebox.showwarning("提示", f"方案「{name}」不存在，可能已被删除。")
            self._refresh_profile_list()
            return
        mapping = p.get("mapping", {})
        position = p.get("position", "fill")
        # profile 保存后原图可能被移动/删除，应用前先校验路径（与 apply_wallpaper 一致）
        missing = [img for img in mapping.values() if img and not os.path.exists(img)]
        if missing:
            messagebox.showwarning(
                "提示",
                f"方案「{name}」中有 {len(missing)} 张图片已不存在，已取消应用。\n"
                "请重新选择图片后再次保存方案。")
            return
        self.fit_var.set(position)
        try:
            if mapping and len(set(mapping.values())) <= 1:
                self.mode_var.set("single")
                self._on_mode()
                self.single_var.set(next(iter(mapping.values())))
                ok = b.apply_single(self.single_var.get(), position)
            else:
                self.mode_var.set("per")
                self._on_mode()
                for r in self.mon_rows:
                    r["var"].set(mapping.get(r["device_path"], ""))
                ok = b.apply_per_monitor(mapping, position)
            self._refresh_profile_list()
            # 记录最近应用的方案，供显示器热插拔时自动重应用
            s = settings.load()
            s["last_wallpaper_profile"] = name
            settings.save(s)
            messagebox.showinfo("完成", "壁纸已应用" if ok else "已用回退方式应用(单屏)")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"应用方案「{name}」失败:\n{e}")

    def delete_profile(self):
        name = self.profile_var.get()
        if not name:
            return
        profiles.delete_profile(name)
        self._refresh_profile_list()

    # ---------- 壁纸轮换（幻灯片） ----------
    def _build_rotate(self, parent):
        f = ttk.LabelFrame(parent, text="壁纸轮换（幻灯片）")
        f.pack(fill="x", padx=8, pady=6)
        s = settings.load()
        self.rotate_dir_var = tk.StringVar(value=s.get("rotate_dir", "") or "")
        try:
            interval = int(s.get("rotate_interval_min", 10))
        except Exception:  # noqa: BLE001
            interval = 10
        self.rotate_interval_var = tk.IntVar(value=max(1, interval))
        self.rotate_status_var = tk.StringVar(value="未运行")
        self._rotate_running = False
        self._rotate_job = None
        self._rotate_index = 0
        self._rotate_count = 0

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row, text="图片目录:").pack(side="left")
        ttk.Entry(row, textvariable=self.rotate_dir_var, width=40).pack(
            side="left", padx=4, fill="x", expand=True)
        ttk.Button(row, text="选择目录", command=self._pick_rotate_dir).pack(side="left")

        row2 = ttk.Frame(f)
        row2.pack(fill="x", padx=6, pady=2)
        ttk.Label(row2, text="间隔(分钟):").pack(side="left")
        ttk.Spinbox(row2, from_=1, to=1440, width=6,
                    textvariable=self.rotate_interval_var).pack(side="left", padx=4)
        self.rotate_btn = ttk.Button(row2, text="开始轮换", command=self._toggle_rotate)
        self.rotate_btn.pack(side="left", padx=(8, 6))
        ttk.Label(row2, textvariable=self.rotate_status_var).pack(side="left", padx=4)

        ttk.Label(
            f,
            text="「每屏不同」模式下同一时刻各屏显示不同图片；「统一单图」模式下所有屏"
                 "同步切换。填充方式沿用壁纸区的设置，路径与间隔会被记住。",
            justify="left", wraplength=620,
        ).pack(anchor="w", padx=6, pady=(0, 6))

    def _pick_rotate_dir(self):
        d = filedialog.askdirectory(title="选择图片目录")
        if d:
            self.rotate_dir_var.set(d)
            self._save_rotate_prefs()
            self._update_rotate_status()

    def _rotate_images(self):
        """扫描轮换目录下的图片文件（按文件名排序，保证顺序稳定）。

        只收真正像图片的文件（校验文件头），避免把损坏/空文件喂给系统把壁纸清空。
        """
        d = (self.rotate_dir_var.get() or "").strip()
        if not d or not os.path.isdir(d):
            return []
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return []
        out = []
        for n in names:
            p = os.path.join(d, n)
            if (n.lower().endswith(ROTATE_EXTS) and os.path.isfile(p)
                    and _looks_like_image(p)):
                out.append(p)
        return out

    def _save_rotate_prefs(self):
        s = settings.load()
        s["rotate_dir"] = (self.rotate_dir_var.get() or "").strip()
        try:
            s["rotate_interval_min"] = max(1, int(self.rotate_interval_var.get()))
        except Exception:  # noqa: BLE001
            s["rotate_interval_min"] = 10
        settings.save(s)

    def _update_rotate_status(self):
        if self._rotate_running:
            return
        n = len(self._rotate_images())
        self.rotate_status_var.set(f"未运行（目录内 {n} 张图片）" if n else "未运行")

    def _toggle_rotate(self):
        if self._rotate_running:
            self._stop_rotate()
        else:
            self._start_rotate()

    def _start_rotate(self):
        total = len(self._rotate_images())
        if total == 0:
            messagebox.showwarning("提示", "请先选择一个包含图片的目录")
            return
        self._save_rotate_prefs()
        self._rotate_running = True
        self._rotate_index = 0
        self._rotate_count = 0
        try:
            self.rotate_btn.configure(text="停止轮换")
        except Exception:  # noqa: BLE001
            pass
        self._rotate_step()

    def _stop_rotate(self, quiet=False):
        self._rotate_running = False
        if self._rotate_job is not None:
            try:
                self.root.after_cancel(self._rotate_job)
            except Exception:  # noqa: BLE001
                pass
            self._rotate_job = None
        try:
            self.rotate_btn.configure(text="开始轮换")
        except Exception:  # noqa: BLE001
            pass
        if not quiet:
            self.rotate_status_var.set(f"已停止（本次切换 {self._rotate_count} 次）")

    def _rotate_step(self):
        """定时切一张；目录为空或应用失败时自动停止并给出原因。"""
        if not self._rotate_running:
            return
        # 触发本次的定时器已经消费掉，先清空句柄，避免手动调用时重复累积
        self._rotate_job = None
        files = self._rotate_images()
        if not files:
            self._stop_rotate(quiet=True)
            self.rotate_status_var.set("目录内已无可用图片，轮换已停止")
            return
        try:
            self._apply_rotate(files)
        except Exception as e:  # noqa: BLE001
            self._stop_rotate(quiet=True)
            self.rotate_status_var.set(f"轮换失败已停止：{e}")
            return
        self._rotate_count += 1
        self._rotate_index = (self._rotate_index + 1) % len(files)
        self.rotate_status_var.set(
            f"轮换中：已切换 {self._rotate_count} 次（共 {len(files)} 张）")
        try:
            minutes = max(1, int(self.rotate_interval_var.get()))
        except Exception:  # noqa: BLE001
            minutes = 10
        self._rotate_job = self.root.after(minutes * 60000, self._rotate_step)

    def _apply_rotate(self, files):
        """按当前模式套用这一轮图片；同时把路径回填到界面，便于再保存为方案。"""
        position = self.fit_var.get()
        if self.mode_var.get() == "single":
            img = files[self._rotate_index % len(files)]
            self.single_var.set(img)
            b.apply_single(img, position)
            return
        mapping = {}
        for i, r in enumerate(self.mon_rows):
            img = files[(self._rotate_index + i) % len(files)]
            r["var"].set(img)
            mapping[r["device_path"]] = img
        if mapping:
            b.apply_per_monitor(mapping, position)

    # ---------- 快捷键修饰键 ----------
    def _default_hk_mods(self):
        """读取已保存的修饰键选择，否则按平台返回默认值。"""
        saved = settings.load().get("hk_mods")
        if saved:
            return saved
        return "cmd+alt" if sys.platform.startswith("darwin") else "ctrl+alt"

    def _hk_help_text(self, label):
        sym = self._mod_symbol.get(label, label)
        return (f"{sym}+←/→ : 活动窗口移到上一/下一屏\n"
                f"{sym}+1/2/3/4 : 左/右/上/下半屏\n"
                f"{sym}+5/6 : 最大化 / 居中\n"
                f"{sym}+Z : 撤销上一次移动 · {sym}+B : 回到上次摆放位\n"
                f"{sym}+S : 暂存窗口 · {sym}+D : 取回最早暂存的窗口\n"
                f"{sym}+7/8/9 : 三分屏（左/中/右）\n"
                f"{sym}+Shift+1/2/3/4 : 四象限（左上/右上/左下/右下）")

    def _current_hk_mods(self):
        """把用户选择的修饰键组合映射为 backend 的 MOD_* 位掩码。"""
        v = self.hk_mods_var.get()
        if v == "cmd+alt":
            return b.MOD_WIN | b.MOD_ALT        # macOS 上 MOD_WIN 即 ⌘
        if v == "ctrl+alt":
            return b.MOD_CONTROL | b.MOD_ALT
        if v == "ctrl+win":
            return b.MOD_CONTROL | b.MOD_WIN
        return b.MOD_CONTROL | b.MOD_ALT       # 兜底

    def _on_hk_mod_change(self, label):
        """用户切换修饰键：持久化并（若已启用）热重载快捷键。"""
        value = self._mod_value_map.get(
            label, "cmd+alt" if sys.platform.startswith("darwin") else "ctrl+alt")
        self.hk_mods_var.set(value)
        s = settings.load()
        s["hk_mods"] = value
        settings.save(s)
        self.hk_info["text"] = self._hk_help_text(label)
        if self.hk_enabled.get() and self.hk is not None:
            try:
                self.hk.stop()
            except Exception:  # noqa: BLE001
                pass
            self.hk = None
            self._toggle_hk()

    def _toggle_hk(self, quiet=False):
        if self.hk_enabled.get():
            if self.hk is None:
                try:
                    self.hk = b.HotkeyManager()
                except Exception as e:  # noqa: BLE001
                    if quiet:
                        # 启动自动恢复失败：保留勾选状态，下次启动再试
                        self.status_var.set(f"全局快捷键恢复失败：{e}")
                    else:
                        messagebox.showerror("错误", f"快捷键初始化失败:\n{e}")
                        self.hk_enabled.set(False)
                    return
                # 快捷键语义是"作用于当前活动窗口"，故忽略界面固定的目标
                mods = self._current_hk_mods()
                dk = b.DIGIT_KEYS
                self.hk.register(mods, VK_RIGHT,
                                 lambda: b.move_active_to_next_monitor(1, use_pinned=False))
                self.hk.register(mods, VK_LEFT,
                                 lambda: b.move_active_to_next_monitor(-1, use_pinned=False))
                self.hk.register(mods, dk[1],
                                 lambda: b.snap_active("left", use_pinned=False))
                self.hk.register(mods, dk[2],
                                 lambda: b.snap_active("right", use_pinned=False))
                self.hk.register(mods, dk[3],
                                 lambda: b.snap_active("top", use_pinned=False))
                self.hk.register(mods, dk[4],
                                 lambda: b.snap_active("bottom", use_pinned=False))
                self.hk.register(mods, dk[5],
                                 lambda: b.snap_active("maximize", use_pinned=False))
                self.hk.register(mods, dk[6],
                                 lambda: b.snap_active("center", use_pinned=False))
                self.hk.register(mods, dk[7],
                                 lambda: b.snap_active("left-third", use_pinned=False))
                self.hk.register(mods, dk[8],
                                 lambda: b.snap_active("middle-third", use_pinned=False))
                self.hk.register(mods, dk[9],
                                 lambda: b.snap_active("right-third", use_pinned=False))
                # F7：⌘⌥+Z 撤销上一次窗口移动
                self.hk.register(mods, VK_Z,
                                 lambda: self._undo_move())
                # ⌘⌥+B：活动窗口回到上次被程序摆好的位置（snap back）
                self.hk.register(mods, getattr(b, "VK_B", 0),
                                 lambda: self._back_to_active_arranged())
                # ⌘⌥+S：暂存活动窗口；⌘⌥+D：取回最早暂存的窗口
                self.hk.register(mods, getattr(b, "VK_S", 0),
                                 lambda: self._stash_active_shortcut())
                self.hk.register(mods, getattr(b, "VK_D", 0),
                                 lambda: self._unstash_active_shortcut())
                # 四象限：修饰键+Shift+数字 1-4（左上/右上/左下/右下）
                for vk, zone in ((dk[1], "quad-tl"), (dk[2], "quad-tr"),
                                 (dk[3], "quad-bl"), (dk[4], "quad-br")):
                    self.hk.register(mods | b.MOD_SHIFT, vk,
                                     lambda z=zone: b.snap_active(z, use_pinned=False))
            self.hk.start()
        else:
            if self.hk:
                self.hk.stop()
        # 记住开关状态，下次启动自动恢复
        s = settings.load()
        s["hk_enabled"] = bool(self.hk_enabled.get())
        settings.save(s)

    # ---------- 窗口管理 ----------
    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        # 从托盘唤醒时刷新一次窗口列表，避免操作已关闭的窗口
        self._refresh_targets()
        self._apply_topmost()

    def on_close(self):
        self.root.withdraw()
        # 首次关闭时告知用户：窗口只是最小化到托盘，而非退出程序
        if not getattr(self, "_close_hinted", False):
            self._close_hinted = True
            messagebox.showinfo(
                "提示",
                "窗口已最小化到系统托盘。\n"
                "双击托盘图标可重新打开，右键托盘图标可退出。")

    def quit(self):
        self._stop_rotate(quiet=True)
        if self._display_poll_id is not None:
            try:
                self.root.after_cancel(self._display_poll_id)
            except Exception:  # noqa: BLE001
                pass
            self._display_poll_id = None
        if self._display_settle_id is not None:
            try:
                self.root.after_cancel(self._display_settle_id)
            except Exception:  # noqa: BLE001
                pass
            self._display_settle_id = None
        if self.hk:
            self.hk.stop()
        self.root.destroy()
