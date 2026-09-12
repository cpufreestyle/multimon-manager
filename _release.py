#!/usr/bin/env python3
import os, sys, json, zipfile, urllib.parse
import subprocess, urllib.request, urllib.error

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
TAG = "v0.2.3"
REPO = "cpufreestyle/multimon-manager"
API = f"https://api.github.com/repos/{REPO}"


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
    "name": "v0.2.3 — Bugfix & UI Refinement",
    "body": (
        "## Multi-Monitor Manager v0.2.3\n\n"
        "跨平台多屏管理器，Windows + macOS 双实现。\n\n"
        "### What's Changed\n"
        "- 修复 macOS 启动时因 Dock 图标设置（NSImage 需 NSString）导致的段错误崩溃\n"
        "- 修复「并排左右」后窗口未置顶到最前的问题\n"
        "- 增强窗口缩放：退出 zoom 态、先定位再缩放、设置后校验重试\n"
        "- 新增「竖排上中下」：把三个窗口堆叠到上/中/下三栏（竖屏排列，macOS）\n"
        "- 界面重构为分页：窗口操作 / 壁纸与设置\n\n"
        "### Features\n"
        "- **每屏壁纸** — 为每个显示器设置独立壁纸\n"
        "- **窗口跨屏/吸附** — 窗口在多屏间移动与边缘吸附\n"
        "- **全局快捷键** — 可自定义的快捷键控制\n"
        "- **系统托盘** — Windows 托盘 / macOS Dock 菜单+面板\n"
        "- **竖排上中下** — 将三个窗口排到屏幕的上/中/下三栏（macOS）\n"
        "- **零第三方依赖** — 纯标准库实现\n\n"
        "### Download\n"
        "- **macOS**：`多屏管理器.app.zip`（已打包为下方附件，解压即用）\n\n"
        "### Files\n"
        "| 模块 | 说明 |\n"
        "|------|------|\n"
        "| `backend.py` | 平台分发入口 |\n"
        "| `wallpaper.py` / `wallpaper_mac.py` | 壁纸管理 |\n"
        "| `windows.py` / `windows_mac.py` | 窗口管理 |\n"
        "| `monitors.py` / `monitors_mac.py` | 显示器信息 |\n"
        "| `hotkeys.py` / `hotkeys_mac.py` | 快捷键 |\n"
        "| `tray.py` / `tray_mac.py` | 系统托盘 |\n"
        "| `profiles.py` | 配置管理 |\n"
        "| `resources.py` | 资源文件 |\n"
        "| `ui.py` | 用户界面 |\n"
        "| `main.py` | 主入口 |\n"
    ),
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
