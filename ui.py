"""多屏管理器 GUI（tkinter，零第三方依赖）。"""
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import autostart
import backend
from backend import monitors, wallpaper, windows, hotkeys as hotkeys_mod
import profiles

VK_LEFT = backend.VK_LEFT
VK_UP = backend.VK_UP
VK_RIGHT = backend.VK_RIGHT
VK_DOWN = backend.VK_DOWN


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
        self.hk = None
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        self._build()
        self.refresh_monitors()

    # ---------- 构建 ----------
    def _build(self):
        canvas = tk.Canvas(self.root)
        scroll = ttk.Scrollbar(self.root, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.content = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

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
        ttk.Combobox(fit, textvariable=self.fit_var, values=list(wallpaper.POSITION.keys()),
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
        f = ttk.LabelFrame(self.content, text="窗口工具 (作用于当前活动窗口)")
        f.pack(fill="x", padx=8, pady=6)
        btns = [
            ("移到上一屏", lambda: windows.move_active_to_next_monitor(-1)),
            ("移到下一屏", lambda: windows.move_active_to_next_monitor(1)),
            ("左半", lambda: windows.snap_active("left")),
            ("右半", lambda: windows.snap_active("right")),
            ("上半", lambda: windows.snap_active("top")),
            ("下半", lambda: windows.snap_active("bottom")),
            ("最大化", lambda: windows.snap_active("maximize")),
            ("居中", lambda: windows.snap_active("center")),
        ]
        row = ttk.Frame(f)
        row.pack(fill="x", pady=4)
        for text, cmd in btns:
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=3)

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
        info = ("Ctrl+Alt+←/→ : 活动窗口移到上一/下一屏\n"
                "Ctrl+Alt+1/2/3/4 : 左/右/上/下半屏\n"
                "Ctrl+Alt+5/6 : 最大化 / 居中")
        ttk.Label(f, text=info, justify="left").pack(anchor="w", padx=4)
        ttk.Checkbutton(f, text="启用全局快捷键", variable=self.hk_enabled,
                        command=self._toggle_hk).pack(anchor="w", padx=4, pady=4)

    def _build_autostart(self):
        f = ttk.LabelFrame(self.content, text="启动选项")
        f.pack(fill="x", padx=8, pady=6)
        ttk.Checkbutton(f, text="开机自动启动", variable=self.autostart_var,
                        command=self._toggle_autostart).pack(anchor="w", padx=4, pady=4)

    # ---------- 逻辑 ----------
    def refresh_monitors(self):
        self.monitors = monitors.enum_monitors()
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
        try:
            if self.mode_var.get() == "single":
                ok = wallpaper.apply_single(self.single_var.get(), position)
            else:
                ok = wallpaper.apply_per_monitor(mapping, position)
            messagebox.showinfo("完成", "壁纸已应用" if ok else "已用回退方式应用(单屏)")
            self.status_var.set("壁纸已应用" if ok else "壁纸已应用(回退单屏)")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"应用壁纸失败:\n{e}")
            self.status_var.set("应用壁纸失败")

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
            return
        mapping = p.get("mapping", {})
        position = p.get("position", "fill")
        self.fit_var.set(position)
        try:
            if mapping and len(set(mapping.values())) <= 1:
                self.mode_var.set("single")
                self._on_mode()
                self.single_var.set(next(iter(mapping.values())))
                ok = wallpaper.apply_single(self.single_var.get(), position)
            else:
                self.mode_var.set("per")
                self._on_mode()
                for r in self.mon_rows:
                    r["var"].set(mapping.get(r["device_path"], ""))
                ok = wallpaper.apply_per_monitor(mapping, position)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"应用方案失败:\n{e}")
            return
        self._refresh_profile_list()
        messagebox.showinfo("完成", f"已应用方案「{name}」" + ("" if ok else "（已用回退方式应用）"))
        self.status_var.set(f"已应用方案「{name}」")

    def delete_profile(self):
        name = self.profile_var.get()
        if not name:
            return
        profiles.delete_profile(name)
        self._refresh_profile_list()

    def _toggle_hk(self):
        if self.hk_enabled.get():
            if self.hk is None:
                try:
                    self.hk = hotkeys_mod.HotkeyManager()
                    self.hk.register(hotkeys_mod.MOD_CONTROL | hotkeys_mod.MOD_ALT, VK_RIGHT,
                                     lambda: windows.move_active_to_next_monitor(1))
                    self.hk.register(hotkeys_mod.MOD_CONTROL | hotkeys_mod.MOD_ALT, VK_LEFT,
                                     lambda: windows.move_active_to_next_monitor(-1))
                    self.hk.register(hotkeys_mod.MOD_CONTROL | hotkeys_mod.MOD_ALT, ord("1"),
                                     lambda: windows.snap_active("left"))
                    self.hk.register(hotkeys_mod.MOD_CONTROL | hotkeys_mod.MOD_ALT, ord("2"),
                                     lambda: windows.snap_active("right"))
                    self.hk.register(hotkeys_mod.MOD_CONTROL | hotkeys_mod.MOD_ALT, ord("3"),
                                     lambda: windows.snap_active("top"))
                    self.hk.register(hotkeys_mod.MOD_CONTROL | hotkeys_mod.MOD_ALT, ord("4"),
                                     lambda: windows.snap_active("bottom"))
                    self.hk.register(hotkeys_mod.MOD_CONTROL | hotkeys_mod.MOD_ALT, ord("5"),
                                     lambda: windows.snap_active("maximize"))
                    self.hk.register(hotkeys_mod.MOD_CONTROL | hotkeys_mod.MOD_ALT, ord("6"),
                                     lambda: windows.snap_active("center"))
                except Exception as e:  # noqa: BLE001
                    messagebox.showerror("错误", f"快捷键初始化失败:\n{e}")
                    self.hk_enabled.set(False)
                    return
            self.hk.start()
            self.status_var.set("全局快捷键已启用")
        else:
            if self.hk:
                self.hk.stop()
                self.status_var.set("全局快捷键已禁用")

    def _toggle_autostart(self):
        try:
            autostart.set_enabled(self.autostart_var.get())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"设置开机自启失败:\n{e}")
            self.autostart_var.set(autostart.is_enabled())

    # ---------- 窗口管理 ----------
    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        self.root.withdraw()

    def quit(self):
        if self.hk:
            self.hk.stop()
        self.root.destroy()
