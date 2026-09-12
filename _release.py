import os, subprocess, json, urllib.request, urllib.error, sys

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
TAG = "v0.2.3"
REPO = "cpufreestyle/multimon-manager"

# Step 1: 打轻量 tag 并推送（本地无 tag 时先创建，避免 'src refspec does not match'）
env = {k: v for k, v in os.environ.items() if k not in ("HTTP_PROXY", "HTTPS_PROXY")}
tag_cmd = ["git", "tag", TAG]
print(f"[git] {' '.join(tag_cmd)}")
t = subprocess.run(tag_cmd, env=env, capture_output=True, text=True, timeout=30)
if t.stderr and "already exists" not in t.stderr:
    print(t.stderr.strip(), file=sys.stderr)
cmd = ["git", "push", "origin", f"refs/tags/{TAG}"]
print(f"[git] {' '.join(cmd)}")
r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
print(r.stdout.strip())
if r.stderr:
    print(r.stderr.strip(), file=sys.stderr)
if r.returncode != 0:
    sys.exit(r.returncode)

# Step 2: Create GitHub Release via API
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
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
        "- 界面重构为分页：窗口操作 / 壁纸与设置\n\n"
        "### Features\n"
        "- **每屏壁纸** — 为每个显示器设置独立壁纸\n"
        "- **窗口跨屏/吸附** — 窗口在多屏间移动与边缘吸附\n"
        "- **全局快捷键** — 可自定义的快捷键控制\n"
        "- **系统托盘** — Windows 托盘 / macOS Dock 菜单+面板\n"
        "- **零第三方依赖** — 纯标准库实现\n\n"
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
url = f"https://api.github.com/repos/{REPO}/releases"
req = urllib.request.Request(url, method="POST")
req.add_header("Authorization", f"Bearer {TOKEN}")
req.add_header("User-Agent", "deploy")
req.add_header("Content-Type", "application/json")
req.data = json.dumps(body).encode()

try:
    with op.open(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    print(f"\n[release] OK: {data.get('html_url')}")
except urllib.error.HTTPError as e:
    err = json.loads(e.read().decode())
    print(f"[release] ERROR {e.code}: {err.get('message')}", file=sys.stderr)
    sys.exit(1)
