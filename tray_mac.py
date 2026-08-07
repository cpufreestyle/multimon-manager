"""系统托盘 / 菜单栏（macOS 实现，零第三方依赖）。

macOS 没有 Python 标准库级别的菜单栏图标 API。这里采用两种零依赖方案：
1. Dock 菜单：通过 tkinter 的 createcommand 注册到应用 Dock 右键菜单
   （仅当用 pyobjc 增强时有效，普通情况退化）。
2. 主方案：用一个常驻右上角的轻量 tkinter 面板作为"快速访问面板"，
   等效于 Windows 的托盘菜单（打开主界面 / 退出）。

DPanel 类对外暴露与 Windows 端 TrayIcon 一致的 create()/destroy() 接口。
"""
import tkinter as tk


class TrayIcon:
    def __init__(self):
        self.panel = None
        self.on_open = None
        self.on_exit = None

    def create(self, icon_path, tip="多屏管理器", on_open=None, on_exit=None):
        self.on_open = on_open
        self.on_exit = on_exit

        # 注册 Dock 菜单命令（tkinter 在 macOS 上支持 createcommand）
        try:
            root = tk._default_root
            if root is not None:
                root.createcommand("::tk::mac::OpenPreferences", lambda: on_open() if on_open else None)
                root.createcommand("::tk::mac::Quit", lambda: on_exit() if on_exit else None)
                root.createcommand("::tk::mac::ShowMainWindow", lambda: on_open() if on_open else None)
        except Exception:  # noqa: BLE001
            pass

        # 常驻右上角快速面板（等效托盘）
        self.panel = tk.Toplevel()
        self.panel.title(tip)
        self.panel.geometry("180x90+1400+20")
        self.panel.resizable(False, False)
        try:
            if icon_path:
                self.panel.iconbitmap(icon_path)
        except Exception:  # noqa: BLE001
            pass
        tk.Label(self.panel, text=tip, font=("Arial", 12, "bold")).pack(pady=8)
        tk.Button(self.panel, text="打开主界面", command=self._open).pack(fill="x", padx=12, pady=2)
        tk.Button(self.panel, text="退出", command=self._exit).pack(fill="x", padx=12, pady=2)
        self.panel.protocol("WM_DELETE_WINDOW", self._hide)

    def _open(self):
        if self.on_open:
            self.on_open()

    def _exit(self):
        if self.on_exit:
            self.on_exit()

    def _hide(self):
        # 关闭面板不直接退出，保持程序运行（点击打开可再次显示）
        if self.panel:
            self.panel.withdraw()

    def show_panel(self):
        if self.panel:
            self.panel.deiconify()

    def destroy(self):
        if self.panel:
            try:
                self.panel.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.panel = None


if __name__ == "__main__":
    t = TrayIcon()
    t.create(None, "测试", on_open=lambda: print("open"), on_exit=lambda: print("exit"))
    tk.mainloop()
