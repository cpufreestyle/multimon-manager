"""跨平台打包脚本：生成可直接双击的 macOS .app / Windows exe，并自动准备图标。

用法（在项目根目录）：
    python3 build.py            # 为当前平台打包
    python3 build.py --clean    # 清理构建产物

说明：
- macOS：生成 dist/多屏管理器.app —— 纯系统工具（sips/iconutil）+ 系统自带
  python3，零第三方依赖。应用图标优先取 assets/ 下的 PNG，否则用 resources 生成。
- Windows：调用 PyInstaller 生成单文件 exe（需先 `pip install pyinstaller`），
  图标用 app.ico（若 assets 里有 PNG 且装了 Pillow，会自动转换）。
"""
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")
APP_NAME = "多屏管理器"
EXE_NAME = "MultiMonManager"
BUNDLE_ID = "com.cpufreestyle.multimonmanager"


def _source_png():
    """优先返回 assets/ 里最新的 PNG（精美图标），没有则 None。"""
    files = sorted(glob.glob(os.path.join(HERE, "assets", "*.png")))
    return files[-1] if files else None


def _py_files():
    return sorted(glob.glob(os.path.join(HERE, "*.py")))


def prepare_icon():
    """准备统一的应用图标 PNG（app.png）并返回其路径。

    app.png 已存在时直接复用（与 resources.create_png 的"不覆盖"策略一致），
    但最终会确保四角透明，避免 AI 生成图标常带的圆角白底。
    """
    out = os.path.join(HERE, "app.png")
    src = _source_png()
    if src:
        shutil.copy2(src, out)      # assets 里的精美图标优先，始终覆盖派生产物
        print(f"[icon] 使用 assets 图标 -> {out}")
    elif not os.path.exists(out):
        import resources
        resources.create_png(out)
        print(f"[icon] 程序化生成 -> {out}")
    else:
        print(f"[icon] 复用已有 -> {out}")
    # 确保无论哪种来源，最终 app.png 圆角外无白底
    import resources
    resources.ensure_transparent_icon(out, size=1024)
    print(f"[icon] 已确保透明圆角 -> {out}")
    return out


def _make_icns(src_png, out_icns):
    """用系统 sips + iconutil 把 PNG 转成多尺寸 .icns。"""
    iconset = os.path.join(BUILD, "app.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset, exist_ok=True)
    specs = [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
             (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
             (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")]
    for size, name in specs:
        dest = os.path.join(iconset, f"icon_{name}.png")
        subprocess.run(["sips", "-z", str(size), str(size), src_png, "--out", dest],
                       check=True, capture_output=True)
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out_icns],
                   check=True, capture_output=True)
    return out_icns


_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>{name}</string>
    <key>CFBundleDisplayName</key><string>{name}</string>
    <key>CFBundleIdentifier</key><string>{bundle_id}</string>
    <key>CFBundleVersion</key><string>{version}</string>
    <key>CFBundleShortVersionString</key><string>{version}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>{exe}</string>
    <key>CFBundleIconFile</key><string>app.icns</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict>
</plist>
"""

_LAUNCHER = """#!/bin/bash
# 由 build.py 生成：启动本应用（使用系统 python3，零第三方依赖）。
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
cd "$DIR" || exit 1
if [ -f "$HOME/.multimon_python" ]; then
  PY="$(cat "$HOME/.multimon_python")"
else
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$c" ]; then PY="$c"; break; fi
  done
fi
[ -z "$PY" ] && PY="python3"
exec "$PY" main.py "$@"
"""


def _version():
    try:
        import version_info
        for attr in ("VERSION", "version", "__version__"):
            v = getattr(version_info, attr, None)
            if v:
                return str(v)
    except Exception:  # noqa: BLE001
        pass
    return "0.2.3"


def build_macos(icon_png):
    if sys.platform != "darwin":
        print("[macOS] 当前不是 macOS，跳过 .app 构建")
        return None
    app = os.path.join(DIST, APP_NAME + ".app")
    shutil.rmtree(app, ignore_errors=True)
    contents = os.path.join(app, "Contents")
    res = os.path.join(contents, "Resources")
    macos = os.path.join(contents, "MacOS")
    os.makedirs(res, exist_ok=True)
    os.makedirs(macos, exist_ok=True)

    # 源码（全部 .py）+ 图标 PNG（供运行时再设置 Dock 图标用）
    for f in _py_files():
        shutil.copy2(f, res)
    shutil.copy2(icon_png, os.path.join(res, "app.png"))
    for extra in ("README.md",):
        p = os.path.join(HERE, extra)
        if os.path.exists(p):
            shutil.copy2(p, res)

    _make_icns(icon_png, os.path.join(res, "app.icns"))

    exe = EXE_NAME
    with open(os.path.join(contents, "Info.plist"), "w", encoding="utf-8") as f:
        f.write(_INFO_PLIST.format(name=APP_NAME, bundle_id=BUNDLE_ID,
                                   version=_version(), exe=exe))
    launcher = os.path.join(macos, exe)
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(_LAUNCHER)
    os.chmod(launcher, 0o755)
    print(f"[macOS] 已生成: {app}")
    return app


def build_macos_frozen(icon_png):
    """用 PyInstaller 打成**自带 Python 运行时**的独立 .app（需先安装 pyinstaller）。

    与默认的 shim 方案不同，这个 .app 不依赖系统 python3，可直接分发给别人。
    输出先落在本次唯一的临时目录，再用覆盖方式同步到 dist/，避免 PyInstaller
    删除上一次的大量输出（批量删除可能被环境的安全策略拦截）。
    """
    if sys.platform != "darwin":
        print("[macOS] 当前不是 macOS，跳过")
        return None
    os.makedirs(BUILD, exist_ok=True)
    icns = _make_icns(icon_png, os.path.join(BUILD, "app.icns"))
    tmp_dist = os.path.join(BUILD, f"pyinst-{os.getpid()}")     # 每次唯一，无需删除
    tmp_work = os.path.join(BUILD, f"work-{os.getpid()}")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed",
           f"--icon={icns}", f"--name={APP_NAME}",
           f"--osx-bundle-identifier={BUNDLE_ID}",
           "--hidden-import=_tray_panel",
           f"--distpath={tmp_dist}", f"--workpath={tmp_work}",
           f"--specpath={BUILD}", "main.py"]
    print("[macOS-frozen]", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, cwd=HERE)
    except Exception as e:  # noqa: BLE001
        print("[macOS-frozen] 构建失败（请先 `pip install pyinstaller`）:", e)
        return None
    src_app = os.path.join(tmp_dist, APP_NAME + ".app")
    final = os.path.join(DIST, APP_NAME + ".app")
    os.makedirs(DIST, exist_ok=True)
    # 复制到全新暂存目录，再把旧包「移走」而非删除（批量删除会被安全策略拦截，
    # 且直接覆盖会因符号链接冲突失败）。
    staging = final + ".new"
    if os.path.exists(staging):
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src_app, staging, symlinks=True)
    if os.path.exists(final):
        trash = os.path.join(BUILD, f"old-{os.getpid()}")
        os.replace(final, trash)
    os.replace(staging, final)
    print(f"[macOS-frozen] 已生成: {final}")
    return final


def _make_ico(icon_png, out_ico):
    """把 PNG 转成 .ico（优先 Pillow；否则回退 resources.create_ico）。"""
    try:
        from PIL import Image
        img = Image.open(icon_png).convert("RGBA")
        img.save(out_ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                                 (128, 128), (256, 256)])
        print(f"[win] PNG -> ICO: {out_ico}")
        return out_ico
    except Exception:  # noqa: BLE001
        import resources
        p = resources.create_ico(out_ico, size=64)
        print(f"[win] 未装 Pillow，回退到内置 ICO: {p}")
        return p


def build_windows(icon_png):
    if not sys.platform.startswith("win"):
        print("[win] 当前不是 Windows，跳过 exe 构建（请在 Windows 上运行本脚本）")
        return None
    ico = _make_ico(icon_png, os.path.join(HERE, "app.ico"))
    cmd = [sys.executable, "-m", "PyInstaller", "--noconsole", "--onefile",
           f"--icon={ico}", f"--name={EXE_NAME}", "main.py"]
    print("[win]", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, cwd=HERE)
    except Exception as e:  # noqa: BLE001
        print("[win] PyInstaller 构建失败（先执行 `pip install pyinstaller`）:", e)
        return None
    out = os.path.join(DIST, EXE_NAME + ".exe")
    print(f"[win] 已生成: {out}")
    return out


def clean():
    for p in (os.path.join(DIST, APP_NAME + ".app"), BUILD,
              os.path.join(HERE, EXE_NAME + ".spec")):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)
    print("[clean] 已清理构建产物")


def main():
    if "--clean" in sys.argv:
        clean()
        return
    os.makedirs(DIST, exist_ok=True)
    icon = prepare_icon()
    if sys.platform == "darwin":
        if "--frozen" in sys.argv:
            build_macos_frozen(icon)
        else:
            build_macos(icon)
    elif sys.platform.startswith("win"):
        build_windows(icon)
    else:
        print("未知平台，仅准备了图标")


if __name__ == "__main__":
    main()
