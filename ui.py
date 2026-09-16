"""多屏管理器 GUI（tkinter，零第三方依赖）。"""
import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import autostart
import backend as b
import profiles
import settings
import layouts
import cmd_channel

VK_LEFT = b.VK_LEFT
VK_UP = b.VK_UP
VK_RIGHT = b.VK_RIGHT
VK_DOWN = b.VK_DOWN

# 目标窗口下拉框的"自动"选项：由程序判断最前面的非本程序窗口
AUTO_TARGET = "自动（上次活动窗口）"
# 并排/竖排显示器下拉框的"自动"选项：按窗口当前所在屏决定
AUTO_MON = "自动（按窗口所在屏）"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("多屏管理器")
        self.root.geometry("760x720")
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
        # 显示器热插拔监听（主线程轮询，避免后台 CFRunLoop 与 Tk 主循环冲突）
        self.monitor_watch = None
        self._display_poll_id = None
        self._last_mon_sig = None
        self._build()
        self.refresh_monitors()
        self._start_command_poll()
        # 上次会话启用过全局快捷键则自动恢复（免每次启动手动勾选；失败静默降级）
        if self.hk_enabled.get():
            self.root.after(600, lambda: self._toggle_hk(quiet=True))
        # 窗口大小变化时防抖重绘布局图，并设最小尺寸避免控件被压坏
        self.root.minsize(660, 520)
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
        ttk.Button(top, text="刷新显示器", command=self.refresh_monitors).pack(side="left")
        self.status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=10)

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
        self._build_window_tools(self.body)
        self._build_wallpaper(self.body)
        self._build_profiles(self.body)
        self._build_layouts(self.body)
        self._build_hotkeys(self.body)
        self._build_autostart(self.body)
        self._build_display_watch(self.body)

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
        ttk.Button(sel, text="刷新列表", command=self._refresh_targets).pack(side="left")
        ttk.Checkbutton(sel, text="常驻置顶", variable=self.topmost,
                        command=self._apply_topmost).pack(side="left", padx=(8, 0))

        # activate=False：只移动窗口、不激活它，管理器才不会被挤到后面
        btns = [
            ("移到上一屏", lambda: b.move_active_to_next_monitor(-1, activate=False)),
            ("移到下一屏", lambda: b.move_active_to_next_monitor(1, activate=False)),
            ("左半", lambda: b.snap_active("left", activate=False)),
            ("右半", lambda: b.snap_active("right", activate=False)),
            ("上半", lambda: b.snap_active("top", activate=False)),
            ("下半", lambda: b.snap_active("bottom", activate=False)),
            ("最大化", lambda: b.snap_active("maximize", activate=False)),
            ("居中", lambda: b.snap_active("center", activate=False)),
            ("左1/3", lambda: b.snap_active("left-third", activate=False)),
            ("中1/3", lambda: b.snap_active("middle-third", activate=False)),
            ("右1/3", lambda: b.snap_active("right-third", activate=False)),
            ("左上", lambda: b.snap_active("quad-tl", activate=False)),
            ("右上", lambda: b.snap_active("quad-tr", activate=False)),
            ("左下", lambda: b.snap_active("quad-bl", activate=False)),
            ("右下", lambda: b.snap_active("quad-br", activate=False)),
        ]
        # 窗口置顶切换（当前仅 Windows 提供实现，其它平台自动隐藏入口）
        if getattr(b, "toggle_topmost", None) is not None:
            btns.append(("置顶切换", lambda: self._toggle_topmost_action()))
        # 15 个按钮分三行排列（6 + 6 + 3），避免超出窗口宽度被截断。
        for group in (btns[:6], btns[6:12], btns[12:]):
            row = ttk.Frame(f)
            row.pack(fill="x", pady=2)
            for text, cmd in group:
                ttk.Button(row, text=text,
                           command=self._keep_front_after(cmd)).pack(side="left", padx=3)

        # 并排左右：可选目标显示器 + 可指定左右两个窗口（留"自动"则由程序取最前面两个）
        sbs = ttk.Frame(f)
        sbs.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Label(sbs, text="并排:").pack(side="left")
        self.sbs_mon_var = tk.StringVar(value=AUTO_MON)
        self.sbs_mon_cb = ttk.Combobox(sbs, textvariable=self.sbs_mon_var,
                                       state="readonly", width=20)
        self.sbs_mon_cb.pack(side="left", padx=(4, 2))
        self.sbs_left_var = tk.StringVar(value=AUTO_TARGET)
        self.sbs_right_var = tk.StringVar(value=AUTO_TARGET)
        ttk.Label(sbs, text="左").pack(side="left", padx=(8, 2))
        self.sbs_left_cb = ttk.Combobox(sbs, textvariable=self.sbs_left_var,
                                        state="readonly", width=16)
        self.sbs_left_cb.pack(side="left")
        ttk.Label(sbs, text="右").pack(side="left", padx=(8, 2))
        self.sbs_right_cb = ttk.Combobox(sbs, textvariable=self.sbs_right_var,
                                         state="readonly", width=16)
        self.sbs_right_cb.pack(side="left")
        ttk.Button(sbs, text="并排左右",
                   command=self._snap_two_side_by_side).pack(
            side="left", padx=(10, 0))

        # 竖排上中下：可选目标显示器 + 可指定上/中/下三个窗口（留"自动"则由程序取最前面三个）
        stack = ttk.Frame(f)
        stack.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Label(stack, text="竖排:").pack(side="left")
        self.stack_mon_var = tk.StringVar(value=AUTO_MON)
        self.stack_mon_cb = ttk.Combobox(stack, textvariable=self.stack_mon_var,
                                         state="readonly", width=18)
        self.stack_mon_cb.pack(side="left", padx=(4, 2))
        self.stack_top_var = tk.StringVar(value=AUTO_TARGET)
        self.stack_mid_var = tk.StringVar(value=AUTO_TARGET)
        self.stack_bot_var = tk.StringVar(value=AUTO_TARGET)
        ttk.Label(stack, text="上").pack(side="left", padx=(8, 2))
        self.stack_top_cb = ttk.Combobox(stack, textvariable=self.stack_top_var,
                                         state="readonly", width=14)
        self.stack_top_cb.pack(side="left")
        ttk.Label(stack, text="中").pack(side="left", padx=(6, 2))
        self.stack_mid_cb = ttk.Combobox(stack, textvariable=self.stack_mid_var,
                                         state="readonly", width=14)
        self.stack_mid_cb.pack(side="left")
        ttk.Label(stack, text="下").pack(side="left", padx=(6, 2))
        self.stack_bot_cb = ttk.Combobox(stack, textvariable=self.stack_bot_var,
                                         state="readonly", width=14)
        self.stack_bot_cb.pack(side="left")
        ttk.Button(stack, text="竖排上中下",
                   command=self._snap_three_stack).pack(
            side="left", padx=(10, 0))

        # 台前调度开启时，两个不同 App 的窗口必须处于同一个"台前组"才会同时显示，
        # 而 macOS 无公开 API 建组，只能用户先手动拖到一起。
        if b.stage_manager_enabled():
            ttk.Label(
                f,
                text="提示：已开启「台前调度」。两个窗口需先手动拖到同一个组，"
                     "再用「并排左右」，否则另一个会被收进侧边。",
                foreground="#a15c00",
                wraplength=620,
                justify="left",
            ).pack(fill="x", padx=6, pady=(0, 6))

        self._target_map = {AUTO_TARGET: None}
        self._refresh_targets()
        self._apply_topmost()

    # ---------- 目标窗口 ----------
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
        # 并排窗口下拉与目标窗口共用同一份列表（含"自动"）
        if hasattr(self, "sbs_left_cb"):
            for cb, var in ((self.sbs_left_cb, self.sbs_left_var),
                            (self.sbs_right_cb, self.sbs_right_var),
                            (self.stack_top_cb, self.stack_top_var),
                            (self.stack_mid_cb, self.stack_mid_var),
                            (self.stack_bot_cb, self.stack_bot_var)):
                cb["values"] = labels
                if var.get() and var.get() not in self._target_map:
                    var.set(AUTO_TARGET)
        if self.target_var.get() not in self._target_map:
            self.target_var.set(AUTO_TARGET)
            b.set_target(None)

    def _refresh_monitor_choices(self):
        """刷新并排/竖排所用的显示器下拉（含"自动"），尽量保留当前选择。"""
        labels = [AUTO_MON]
        self._monitor_map = {AUTO_MON: None}
        for i, m in enumerate(self.monitors):
            label = (f"显示器{i + 1}"
                     + ("（主屏）" if m.is_primary else "")
                     + f" {m.width}x{m.height}")
            labels.append(label)
            self._monitor_map[label] = i
        for cb, var in ((self.sbs_mon_cb, self.sbs_mon_var),
                        (self.stack_mon_cb, self.stack_mon_var)):
            cb["values"] = labels
            if var.get() not in self._monitor_map:
                var.set(AUTO_MON)

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
                 "标题变化会自动按应用名兜底）。",
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

    def _save_layout(self):
        wins = b.list_target_windows()
        if not wins:
            messagebox.showwarning("提示", "当前没有可保存的窗口")
            return
        name = simpledialog.askstring("保存布局", "方案名称：", parent=self.root)
        if not name:
            return
        data = [{"hwnd": w["hwnd"], "owner": w["owner"], "name": w["name"],
                 "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"]}
                for w in wins]
        layouts.save_layout(name, data)
        self._refresh_layout_list()
        self.layout_var.set(name)
        s = settings.load()
        s["last_layout"] = name
        settings.save(s)
        self.status_var.set(f"已保存布局：{name}（{len(data)} 个窗口）")

    def _apply_layout(self):
        name = self.layout_var.get()
        if not name:
            messagebox.showwarning("提示", "请先选择或保存一个布局方案")
            return
        layout = layouts.load_layout(name)
        if not layout:
            messagebox.showwarning("提示", f"布局方案不存在：{name}")
            return
        ok, miss = 0, 0
        current = {w["hwnd"]: w["owner"] for w in b.list_target_windows()}
        for item in layout["windows"]:
            hwnd = item["hwnd"]
            # 精确匹配；否则按应用名兜底（窗口标题可能已变化）
            target = hwnd if hwnd in current else next(
                (h for h, o in current.items() if o == item["owner"]), None)
            if not target:
                miss += 1
                continue
            try:
                b.set_window_rect(target, item["x"], item["y"], item["w"], item["h"],
                                 activate=False)
                ok += 1
            except Exception:  # noqa: BLE001
                miss += 1
        s = settings.load()
        s["last_layout"] = name
        settings.save(s)
        self.status_var.set(f"已还原布局「{name}」：成功 {ok}，未找到 {miss}")
        if miss:
            messagebox.showinfo(
                "完成",
                f"布局「{name}」还原完成：成功 {ok} 个，未找到 {miss} 个"
                "（窗口可能已关闭或标题已变）。")
        else:
            messagebox.showinfo("完成", f"布局「{name}」已全部还原（{ok} 个窗口）")

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

    # ---------- 显示器热插拔 ----------
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
                self._handle_display_change()
        except Exception:  # noqa: BLE001
            pass
        if self.watch_enabled.get():
            self._display_poll_id = self.root.after(1500, self._poll_display)

    def _handle_display_change(self):
        """显示器配置变化后：刷新列表；可选自动重应用上次壁纸方案。"""
        self.refresh_monitors()
        name = settings.load().get("last_wallpaper_profile")
        if self.auto_apply_watch.get() and name and profiles.load_profiles().get(name):
            try:
                profiles.apply_profile(name)
                self.status_var.set(f"检测到显示器变化：已刷新并重应用方案「{name}」")
            except Exception as e:  # noqa: BLE001
                self.status_var.set(f"检测到显示器变化：已刷新（重应用失败：{e}）")
        else:
            self.status_var.set("检测到显示器变化：已刷新显示器列表")

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
            label = f"{m.device_name}  {m.width}x{m.height}" + ("  [主屏]" if m.is_primary else "")
            ttk.Label(row, text=label, width=30).pack(side="left")
            var = tk.StringVar()
            ttk.Button(row, text="选择", command=lambda v=var: self._pick(v)).pack(side="left")
            ttk.Entry(row, textvariable=var, width=32).pack(side="left", padx=4, fill="x", expand=True)
            self.mon_rows.append({"device_path": m.device_path, "var": var})
        # 刷新后重绘布局可视化（点击屏幕可直接为该屏选壁纸）
        self._draw_layout()
        # 同步并排/竖排的显示器下拉选项
        self._refresh_monitor_choices()

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
            name = (m.device_name or "").strip()
            res = f"{m.width}x{m.height}"
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
        try:
            if self.mode_var.get() == "single":
                ok = b.apply_single(self.single_var.get(), position)
            else:
                ok = b.apply_per_monitor(mapping, position)
            messagebox.showinfo("完成", "壁纸已应用" if ok else "已用回退方式应用(单屏)")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"应用壁纸失败:\n{e}")

    def _refresh_profile_list(self):
        self.profile_combo["values"] = list(profiles.load_profiles().keys())

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
        if self.hk:
            self.hk.stop()
        self.root.destroy()
