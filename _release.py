#!/usr/bin/env python3
import os, sys, json, zipfile, urllib.parse
import subprocess, urllib.request, urllib.error

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
TAG = "v0.2.4"
REPO = "cpufreestyle/multimon-manager"
API = f"https://api.github.com/repos/{REPO}"

RELEASE_BODY = """## Multi-Monitor Manager v0.2.4

跨平台多屏管理器，Windows + macOS 双实现，零第三方依赖。

### What's Changed
- **修复 macOS 启动即崩溃**（`Fatal Python error: PyEval_RestoreThread: GIL is released`）：
  移除与 Tk 主循环并存的后台 `CFRunLoop`，显示器监听改为主线程轮询，
  全局热键的事件 tap 改挂主线程 CFRunLoop
- **修复全局快捷键在 macOS 上从未真正生效**（三处常量错误）：
  - `CGEventTapCreate` 的 `eventsOfInterest` 是位掩码，须传 `1 << kCGEventKeyDown`（1024），
    此前误传 10，导致键盘事件被全部过滤
  - `CGEventField` 字段号修正：键码 `9`、修饰键 `59`（此前误为 113 / 115，键码恒读 0）
  - tap 位置与插入位常量修正为 `kCGSessionEventTap=1` / `kCGHeadInsertEventTap=0`
- 显示器变化监听 `display_notify_mac` 改为主线程注册回调，彻底消除后台 runloop 隐患
- **界面改为单页 + 垂直滚动**：原先的两个分页面板合并为一页，右侧滑块可上下滑动，
  并支持滚轮（macOS / Windows / X11 量级自动换算）
- 日志降噪：工作区计算日志由 INFO 降为 DEBUG，不再每 2 秒刷屏
- 新增「竖排上中下」：把三个窗口堆叠到上 / 中 / 下三栏
- 「并排左右」「竖排上中下」现已支持 Windows（此前仅 macOS）
- 应用图标圆角外的白色背景改为透明

### Features
- **每屏壁纸** — 为每个显示器设置独立壁纸
- **窗口跨屏 / 吸附** — 窗口在多屏间移动与边缘吸附
- **全局快捷键** — 可自定义修饰键（macOS ⌘⌥ / Windows Ctrl+Alt）+ 数字键 1~9 与方向键
- **系统托盘** — Windows 托盘 / macOS Dock 菜单 + 面板
- **竖排上中下 / 并排左右** — 两 / 三个窗口排到左右半屏或上中下三栏（macOS + Windows）
- **显示器热插拔** — 自动检测显示器变化
- **零第三方依赖** — 纯 Python 标准库 + ctypes 实现

### Download
- **macOS**：`多屏管理器.app.zip`（已作为下方附件，解压即用）
- **Windows**：在 Windows 上执行 `python3 build.py`（需 PyInstaller）自行打包 exe

### Files
| 模块 | 说明 |
|------|------|
| `backend.py` | 平台分发入口 |
| `wallpaper.py` / `wallpaper_mac.py` | 壁纸管理 |
| `windows.py` / `windows_mac.py` | 窗口管理 |
| `monitors.py` / `monitors_mac.py` | 显示器信息 |
| `hotkeys.py` / `hotkeys_mac.py` | 全局快捷键 |
| `tray.py` / `tray_mac.py` | 系统托盘 |
| `display_notify_mac.py` | 显示器变化监听（主线程） |
| `profiles.py` | 配置管理 |
| `resources.py` | 资源文件 |
| `ui.py` | 用户界面 |
| `main.py` | 主入口 |
"""


def gh(method, url, data=None):
    """统一 GitHub API 调用，出错也返回 (status, json)。"""
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("User-Agent", "deploy")
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw}


# Step 1: 本地打 tag + 通过 GitHub API 在远程创建 tag（绕过本机 git 凭证/keychain）
print(f"[git] git tag {TAG}")
t = subprocess.run(["git", "tag", TAG], capture_output=True, text=True, timeout=30)
if t.stderr and "already exists" not in t.stderr:
    print(t.stderr.strip(), file=sys.stderr)

sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
s1, rd = gh("POST", f"{API}/git/refs", {"ref": f"refs/tags/{TAG}", "sha": sha})
if s1 == 201:
    print(f"[git] 远程 tag {TAG} 已创建 (sha {sha[:8]})")
elif s1 == 422:
    print(f"[git] 远程 tag {TAG} 已存在，跳过")
else:
    print(f"[git] 远程 tag 创建失败 status={s1}: {rd.get('message')}", file=sys.stderr)
    sys.exit(1)

# Step 2: 创建（或复用已存在的）Release
body = {
    "tag_name": TAG,
    "name": "v0.2.4 — Stability, Hotkeys & UI",
    "body": RELEASE_BODY,
    "draft": False,
    "prerelease": False,
}
status, data = gh("POST", f"{API}/releases", body)
if status == 201:
    print(f"\n[release] OK: {data.get('html_url')}")
elif status == 422:
    s2, existing = gh("GET", f"{API}/releases/tags/{TAG}")
    data = existing
    print(f"\n[release] 已存在，复用: {data.get('html_url')}")
else:
    print(f"[release] ERROR {status}: {data.get('message')}", file=sys.stderr)
    sys.exit(1)

# Step 3: 打包 .app 并作为附件上传（GitHub 不支持直接上传目录，需先压成 zip）
upload_base = data["upload_url"].split("{")[0]
app_dir = "dist/多屏管理器.app"
zip_name = "多屏管理器.app.zip"
if not os.path.isdir(app_dir):
    print(f"[asset] 跳过：未找到 {app_dir}", file=sys.stderr)
else:
    zip_path = os.path.join("dist", zip_name)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(app_dir):
            for f in files:
                fp = os.path.join(root, f)
                zf.write(fp, os.path.relpath(fp, "dist"))
    print(f"[asset] 已打包 {zip_path} ({os.path.getsize(zip_path)//1024} KB)")
    # 若同名资产已存在则先删除，避免重复上传 422
    for a in data.get("assets", []):
        if a.get("name") == zip_name:
            gh("DELETE", f"{API}/releases/assets/{a['id']}")
            print(f"[asset] 已删除旧资产 {zip_name}")
            break
    asset_url = (upload_base + "?" +
                 urllib.parse.urlencode({"name": zip_name, "label": "macOS 应用 (.app.zip)"}))
    with open(zip_path, "rb") as f:
        blob = f.read()
    req = urllib.request.Request(asset_url, method="POST", data=blob)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("User-Agent", "deploy")
    req.add_header("Content-Type", "application/zip")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=120) as resp:
            ad = json.loads(resp.read().decode())
        print(f"[asset] OK: {ad.get('browser_download_url')}")
    except urllib.error.HTTPError as e:
        print(f"[asset] ERROR {e.code}: {e.read().decode()}", file=sys.stderr)
