#!/usr/bin/env python3
import os, sys, json, zipfile, urllib.parse
import subprocess, urllib.request, urllib.error

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
TAG = "v0.3.0"
REPO = "cpufreestyle/multimon-manager"
API = f"https://api.github.com/repos/{REPO}"

RELEASE_BODY = """## Multi-Monitor Manager v0.3.0

跨平台多屏管理器，Windows + macOS 双实现，零第三方依赖。

### New Features
- **壁纸轮换（幻灯片）**：指定图片目录 + 间隔（分钟）自动轮换壁纸。
  「每屏不同」模式下同一时刻各屏显示不同图片，「统一单图」模式下所有屏同步切换；
  目录与间隔会被记住。
- **移到指定屏**：窗口工具区新增目标屏下拉与按钮，多屏时不用再"上一屏/下一屏"连点，
  一次跳到指定显示器。
- **布局方案绑定显示器配置**：保存布局时记录显示器指纹（数量/分辨率/相对位置），
  配置变化后应用前会提示并确认，避免窗口按旧坐标落到错误的屏幕。
- **配置备份 / 恢复**：一键把快捷键偏好、热插拔开关、壁纸轮换设置、窗口布局方案与
  壁纸方案导出为单个 JSON，换机或重装后导入恢复（合并，同名覆盖）。

### Fixes & Optimizations
- **修复版本号漂移**：`build.py` 一直读不到 `version_info.py` 里的版本号（该文件是
  PyInstaller 的 `eval()` 表达式，不含 `VERSION` 属性），macOS `Info.plist` 版本号
  被硬编码成 0.2.3；`install.py` 的注册表版本号停留在 0.2.2。现统一以新增的
  `app_version.py` 为唯一来源，打包前还会校验与 `version_info.py` 是否一致。
- **统一配置存储路径**：壁纸方案原先固定写在 `%APPDATA%`，而 settings / 窗口布局在源码
  运行时写在仓库目录，三份配置分居两处。现在统一由 `settings.data_dir()` 决定，并保留
  一次旧位置回退读取（升级后老方案不丢）。
- **修复 Windows 120 DPI 下界面横向截断**：窗口工具区的「并排左右」按钮与整个
  「竖排上中下」按钮会被挤出可视区（画布只有纵向滚动）。已收窄相关下拉宽度、
  把默认窗口调整为 820×740 并抬高最小宽度。
- `windows.py` 的 `GetWindowLongPtr` 参数声明改为模块加载时一次性设置，避免热键线程
  与主线程并发调用时互相改写 ctypes 的全局函数签名。
- 仓库整洁：构建调试产物 `release.err` / `release.log` 移出版本库并加入 `.gitignore`。

### Features
- **每屏壁纸 / 壁纸轮换** — 为每个显示器设置独立壁纸或按目录自动轮换
- **窗口跨屏 / 吸附** — 移动到相邻屏或指定屏，左右/上下半屏、三分屏、四象限
- **并排左右 / 竖排上中下** — 两 / 三个窗口一键排列
- **布局方案** — 窗口位置一键保存与还原（含显示器配置校验）
- **配置备份** — 全部配置导出 / 导入
- **全局快捷键** — 可自定义修饰键（macOS ⌘⌥ / Windows Ctrl+Alt）+ 数字键 1~9 与方向键
- **系统托盘** — Windows 托盘 / macOS Dock 菜单 + 面板
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
| `profiles.py` | 壁纸方案管理 |
| `layouts.py` | 窗口布局方案 + 显示器指纹 |
| `config_io.py` | 配置导出 / 导入 |
| `settings.py` | 偏好存储与配置目录定位 |
| `app_version.py` | 版本号唯一来源 |
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
    "name": "v0.3.0 — Wallpaper Rotation, Monitor Targeting & Config Backup",
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
