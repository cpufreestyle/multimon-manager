"""多屏管理器 GUI（tkinter，零第三方依赖）。"""
import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import autostart
import backend as b
import profiles
import settings

VK_LEFT = b.VK_LEFT
VK_UP = b.VK_UP
VK_RIGHT = b.VK_RIGHT
VK_DOWN = b.VK_DOWN

# 目标窗口下拉框的"自动"选项：由程序判断最前面的非本程序窗口
AUTO_TARGET = "自动（上次活动窗口）"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("多屏管理器  (DisplayFusion 风格)")
        self.root.geometry("660x760")
        self.monitors = []
        self.mon_rows = []
        self.fit_var = tk.StringVar(value="fill")
        self.mode_var = tk.StringVar(value="per")
        self.single_var = tk.StringVar()
        self.hk_enabled = tk.BooleanVar(value=False)
        # 全局快捷键修饰键：按平台默认（macOS ⌘⌥，Windows Ctrl+Alt），用户可改并持久化
        self.hk_mods_var = tk.StringVar(value=self._default_hk_mods())
        # 开机自动启动（v0.2.1）
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        # 常驻置顶：默认关闭。按钮已改为不激活目标窗口，管理器不会被挤走，
        # 无需常驻置顶；需要始终压住其它窗口时可手动勾选。
        self.topmost = tk.BooleanVar(value=False)
        self.hk = None
        self._build()
        self.refresh_monitors()

    # ---------- 构建 ----------
    def _build(self):
        # macOS 上直接用普通 Frame，避免 Canvas/Scrollbar 组合的黑屏问题
        self.content = ttk.Frame(self.root)
        self.content.pack(fill="both", expand=True)

        top = ttk.Frame(self.content)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Button(top, text="刷新显示器", command=self.refresh_monitors).pack(side="left")
        self.status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=10)

        self._build_wallpaper()
        self._build_window_tools()
        self._build_profiles()
        self._build_hotkeys()
        self._build_autostart()

    def _build_wallpaper(self):
        f = ttk.LabelFrame(self.content, text="壁纸")
        f.pack(fill="x", padx=8, pady=6)

        mode = ttk.Frame(f)
        mode.pack(fill="x", pady=4)
        ttk.Radiobutton(mode, text="每屏不同", variable=self.mode_var, value="per",
                        command=self._on_mode).pack(side="left")
        ttk.Radiobutton(mode, text="统一单图(所有屏)", variable=self.mode_var, value="single",
                        command=self._on_mode).pack(side="left")

        # 显示器布局可视化：直观看到哪块屏在左/右/上/下（几何数据已由 monitors_mac 提供）
        self.layout_canvas = tk.Canvas(f, height=172, bg="#fafafa",
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

    def _build_window_tools(self):
        f = ttk.LabelFrame(self.content, text="窗口工具")
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
            ("并排左右", lambda: b.snap_two_side_by_side()),
        ]
        # 9 个按钮挤在单行总宽约 700+px，超出 660 的窗口宽度会导致右侧按钮
        # 被截断或压扁，故分两行排列（5 + 4）。
        for group in (btns[:5], btns[5:]):
            row = ttk.Frame(f)
            row.pack(fill="x", pady=2)
            for text, cmd in group:
                ttk.Button(row, text=text,
                           command=self._keep_front_after(cmd)).pack(side="left", padx=3)

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
        if self.target_var.get() not in self._target_map:
            self.target_var.set(AUTO_TARGET)
            b.set_target(None)

    def _on_target_selected(self, _event=None):
        label = self.target_var.get()
        b.set_target(self._target_map.get(label))
        self.status_var.set(f"目标窗口: {label}")

    # ---------- 置顶 ----------
    def _apply_topmost(self):
        """应用/取消主窗口置顶。"""
        try:
            self.root.attributes("-topmost", bool(self.topmost.get()))
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

    def _build_profiles(self):
        f = ttk.LabelFrame(self.content, text="壁纸方案")
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

    def _build_hotkeys(self):
        f = ttk.LabelFrame(self.content, text="全局快捷键")
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

    def _build_autostart(self):
        f = ttk.LabelFrame(self.content, text="启动选项")
        f.pack(fill="x", padx=8, pady=6)
        ttk.Checkbutton(f, text="开机自动启动", variable=self.autostart_var,
                        command=self._toggle_autostart).pack(anchor="w", padx=4, pady=4)

    def _toggle_autostart(self):
        try:
            autostart.set_enabled(self.autostart_var.get())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"设置开机自启失败:\n{e}")
            self.autostart_var.set(autostart.is_enabled())

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

    def _draw_layout(self):
        """在 Canvas 上按比例画出各显示器相对位置。

        monitors_mac 返回的是 Cocoa 全局拼接坐标（原点主屏左下、y 轴向上），
        绘制时统一翻转 y 轴并缩放适配 Canvas 宽度。
        """
        cv = self.layout_canvas
        cv.delete("all")
        ms = self.monitors
        if not ms:
            return
        min_x = min(m.left for m in ms)
        max_y = max(m.top + m.height for m in ms)        # Cocoa 坐标系最高顶部
        world_w = max(m.left + m.width for m in ms) - min_x
        world_h = max_y - min(m.top for m in ms)
        cw = max(620, cv.winfo_width())
        ch = cv.winfo_height() or 172
        pad = 14
        scale = min((cw - 2 * pad) / world_w, (ch - 2 * pad) / world_h)
        for i, m in enumerate(ms):
            x1 = pad + (m.left - min_x) * scale
            y1 = pad + (max_y - (m.top + m.height)) * scale   # 翻转 y 轴
            x2 = x1 + m.width * scale
            y2 = y1 + m.height * scale
            fill = "#d8e6ff" if m.is_primary else "#e9e9e9"
            rid = cv.create_rectangle(x1, y1, x2, y2, fill=fill,
                                      outline="#5a5a5a", width=1.5)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            txt = f"{i + 1}. {m.width}x{m.height}"
            if m.is_primary:
                txt += "  主屏"
            cv.create_text(cx, cy, text=txt, font=("Helvetica", 11), fill="#222")
            cv.tag_bind(rid, "<Button-1>",
                        lambda e, idx=i: self._on_screen_click(idx))

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
                f"{sym}+5/6 : 最大化 / 居中")

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

    def _toggle_hk(self):
        if self.hk_enabled.get():
            if self.hk is None:
                try:
                    self.hk = b.HotkeyManager()
                except Exception as e:  # noqa: BLE001
                    messagebox.showerror("错误", f"快捷键初始化失败:\n{e}")
                    self.hk_enabled.set(False)
                    return
                # 快捷键语义是"作用于当前活动窗口"，故忽略界面固定的目标
                mods = self._current_hk_mods()
                self.hk.register(mods, VK_RIGHT,
                                 lambda: b.move_active_to_next_monitor(1, use_pinned=False))
                self.hk.register(mods, VK_LEFT,
                                 lambda: b.move_active_to_next_monitor(-1, use_pinned=False))
                self.hk.register(mods, ord("1"),
                                 lambda: b.snap_active("left", use_pinned=False))
                self.hk.register(mods, ord("2"),
                                 lambda: b.snap_active("right", use_pinned=False))
                self.hk.register(mods, ord("3"),
                                 lambda: b.snap_active("top", use_pinned=False))
                self.hk.register(mods, ord("4"),
                                 lambda: b.snap_active("bottom", use_pinned=False))
                self.hk.register(mods, ord("5"),
                                 lambda: b.snap_active("maximize", use_pinned=False))
                self.hk.register(mods, ord("6"),
                                 lambda: b.snap_active("center", use_pinned=False))
            self.hk.start()
        else:
            if self.hk:
                self.hk.stop()

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
