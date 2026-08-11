"""入口：启动多屏管理器（GUI + 系统托盘），跨 Windows / macOS。"""
import atexit
import os
import sys
import tkinter as tk

import backend
import resources
import ui


def _cleanup():
    """进程退出时释放 COM 资源。"""
    if sys.platform == "win32":
        try:
            dw = backend.wallpaper.get_desktop_wallpaper()
            if hasattr(dw, "close"):
                dw.close()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_cleanup)


def main():
    root = tk.Tk()

    here = os.path.dirname(os.path.abspath(__file__))
    icon_path = resources.create_ico(os.path.join(here, "app.ico"))
    if icon_path:
        try:
            root.iconbitmap(icon_path)
        except Exception:  # noqa: BLE001
            pass

    app = ui.App(root)

    t = backend.tray.TrayIcon()
    t.create(
        icon_path,
        "多屏管理器",
        on_open=app.show,
        on_exit=app.quit,
    )

    root.protocol("WM_DELETE_WINDOW", app.on_close)

    try:
        root.mainloop()
    finally:
        t.destroy()


if __name__ == "__main__":
    main()
