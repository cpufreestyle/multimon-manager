#!/usr/bin/env python3
"""发布脚本：打 tag + 创建 GitHub Release + 上传附件。

用法（仓库根目录）：
    set GITHUB_TOKEN=<token>      # Windows
    export GITHUB_TOKEN=<token>   # macOS / Linux
    python _release.py

说明：
- tag 通过 GitHub API 在远端创建（绕开本机 git 凭证/keychain），本地也会打一份；
- Release body 沿用线上既有风格（`## vX.Y.Z 更新内容` + `### 小节`）；
- 附件按存在性上传：dist/MultiMonManager.exe、dist/MultiMonManager-Setup.exe、
  dist/多屏管理器.app.zip；同名附件会先删除再上传，脚本可重复执行（幂等）。
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import subprocess

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
TAG = "v0.3.2"
TITLE = "v0.3.2 — 跨屏尺寸自适应 / 缩放比显示"
REPO = "cpufreestyle/multimon-manager"
BRANCH = "master"
API = f"https://api.github.com/repos/{REPO}"

# 代理：沙箱/墙内直连 GitHub 常不可达，按顺序探测本地端口
PROXY_CANDIDATES = [os.environ.get("HTTPS_PROXY"), os.environ.get("https_proxy"),
                    "http://127.0.0.1:7897", "http://127.0.0.1:10809", None]

RELEASE_BODY = """## v0.3.2 更新内容

> 接着 v0.3.1 把「窗口挪到另一块屏」这件事做完整：缩放比不同的屏之间保持肉眼大小，
> 分辨率差太多时自动缩小，顺便修掉一个会让窗口每次跨屏都缩一圈的问题。

### 新增（Windows）
- **跨屏缩放比自适应**：壁纸可视化现在会标出每块屏的缩放比（`1920x1080 @125%`）。
  窗口跨屏移动时，若目标屏缩放比不同、且该窗口是 per-monitor DPI aware
  （这类窗口的物理尺寸不随缩放变化），会按 DPI 比例缩放，**保持肉眼大小一致**。
  DPI-unaware / system-aware 的窗口由系统自己做拉伸，程序不干预，避免双重缩放
- **装不下就缩小**：目标屏工作区比窗口小（例如从 4K 屏挪到 1080p 笔记本屏），
  会等比缩到留 4% 边距，不再让窗口溢出到屏幕外

### 修复
- **修掉跨屏累积缩小**：`move_to_monitor` 原来用 `GetWindowRect` 判断窗口是否装得进
  目标屏。但 `GetWindowRect` 含一圈透明阴影边框（v0.3.1 已确认约 8px），铺满工作区的
  窗口用它比大小会「高」出几个像素，于是每次跨屏都被判定装不下而缩一圈——
  实测一个半屏窗口 `976x1028 → 929x979`，连挪几次会越来越小。
  现在位置与尺寸统一在**可见矩形**空间里计算，不再累积缩小

### 实现要点
- `monitors.py`：记录每块屏的 `HMONITOR` 与有效 DPI（`GetDpiForMonitor`），
  新增 `effective_dpi()` 与 `MonitorInfo.scale_percent`
- `windows.py`：新增 `window_is_per_monitor_aware()`、`fit_size_for_monitor()`，
  以及开关 `set_resize_on_monitor_change()`（默认开启）

### 附件
- MultiMonManager.exe：主程序（单文件，可直接运行）
- MultiMonManager-Setup.exe：安装包（安装到 `%LOCALAPPDATA%\\MultiMonManager`，含开始菜单 / 桌面快捷方式与卸载项）

> Windows 版「每屏不同壁纸」需 Windows 8+；macOS 需在「系统设置 → 隐私与安全性 → 辅助功能」中授权，否则窗口控制与全局快捷键不可用。
> 本版改动集中在 Windows 端；macOS 侧逻辑未变动，待真机回归后再一并验证。
"""


def _opener(proxy):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _pick_proxy():
    """返回第一个能访问 GitHub API 的代理（None 表示直连可用）。"""
    for cand in PROXY_CANDIDATES:
        try:
            req = urllib.request.Request(f"{API}/releases?per_page=1")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("User-Agent", "mmm-release")
            with _opener(cand).open(req, timeout=15) as r:
                if r.status == 200:
                    print(f"[proxy] 使用 {cand or '直连'}")
                    return cand
        except Exception as e:  # noqa: BLE001
            print(f"[proxy] {cand or '直连'} 不可用: {str(e)[:80]}")
    return None


def gh(method, url, data=None, proxy=None):
    """统一 GitHub API 调用，出错也返回 (status, json)。"""
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "mmm-release")
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    try:
        with _opener(proxy).open(req, timeout=60) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"message": raw}


def upload_asset(upload_base, data, path, label, proxy):
    name = os.path.basename(path)
    for a in data.get("assets", []):
        if a.get("name") == name:
            gh("DELETE", f"{API}/releases/assets/{a['id']}", proxy=proxy)
            print(f"[asset] 已删除同名旧附件 {name}")
            break
    url = upload_base + "?" + urllib.parse.urlencode({"name": name, "label": label})
    with open(path, "rb") as f:
        blob = f.read()
    req = urllib.request.Request(url, method="POST", data=blob)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("User-Agent", "mmm-release")
    req.add_header("Content-Type", "application/octet-stream")
    try:
        with _opener(proxy).open(req, timeout=600) as resp:
            ad = json.loads(resp.read().decode())
        print(f"[asset] OK {name} ({len(blob)/1048576:.2f} MB) -> {ad.get('browser_download_url')}")
        return True
    except urllib.error.HTTPError as e:
        print(f"[asset] ERROR {name} {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[asset] ERROR {name}: {e}", file=sys.stderr)
    return False


def zip_app():
    """把 macOS .app 目录压成 zip（GitHub 不支持上传目录）。返回路径或 None。"""
    app_dir = "dist/多屏管理器.app"
    if not os.path.isdir(app_dir):
        return None
    zip_path = "dist/多屏管理器.app.zip"
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(app_dir):
            for f in files:
                fp = os.path.join(root, f)
                zf.write(fp, os.path.relpath(fp, "dist"))
    print(f"[asset] 已打包 {zip_path} ({os.path.getsize(zip_path)//1024} KB)")
    return zip_path


def main():
    if not TOKEN:
        print("缺少 GITHUB_TOKEN 环境变量", file=sys.stderr)
        return 1
    proxy = _pick_proxy()

    # 幂等检查：已存在则停下，不重复创建
    st, existing = gh("GET", f"{API}/releases/tags/{TAG}", proxy=proxy)
    if st == 200:
        print(f"[release] {TAG} 已存在：{existing.get('html_url')}")
        print("如需更新正文，请用 PATCH /releases/<id>；本脚本不覆盖已发布内容。")
        return 0

    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    print(f"[git] HEAD = {sha}")
    t = subprocess.run(["git", "tag", TAG], capture_output=True, text=True)
    if t.stderr and "already exists" not in t.stderr:
        print(t.stderr.strip(), file=sys.stderr)

    st, data = gh("POST", f"{API}/releases", {
        "tag_name": TAG,
        "target_commitish": BRANCH,
        "name": TITLE,
        "body": RELEASE_BODY,
        "draft": False,
        "prerelease": False,
    }, proxy=proxy)
    if st != 201:
        print(f"[release] ERROR {st}: {data.get('message')}", file=sys.stderr)
        return 1
    print(f"[release] OK: {data.get('html_url')}")

    upload_base = data["upload_url"].split("{")[0]
    uploads = [
        ("dist/MultiMonManager.exe", "Windows 主程序（单文件）"),
        ("dist/MultiMonManager-Setup.exe", "Windows 安装包"),
    ]
    mac_zip = zip_app()
    if mac_zip:
        uploads.append((mac_zip, "macOS 应用 (.app.zip)"))
    for path, label in uploads:
        if os.path.exists(path):
            upload_asset(upload_base, data, path, label, proxy)
        else:
            print(f"[asset] 跳过（不存在）: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
