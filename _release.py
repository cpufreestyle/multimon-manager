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
TAG = "v0.3.1"
TITLE = "v0.3.1 — 分屏无缝贴合 / 窗口列表过滤 / 最大化跨屏保持"
REPO = "cpufreestyle/multimon-manager"
BRANCH = "master"
API = f"https://api.github.com/repos/{REPO}"

# 代理：沙箱/墙内直连 GitHub 常不可达，按顺序探测本地端口
PROXY_CANDIDATES = [os.environ.get("HTTPS_PROXY"), os.environ.get("https_proxy"),
                    "http://127.0.0.1:7897", "http://127.0.0.1:10809", None]

RELEASE_BODY = """## v0.3.1 更新内容

> Windows 端一轮针对性优化：把分屏真正做「贴合」，并把窗口列表里的噪音清掉。

### 修复与优化（Windows）
- **分屏 / 并排不再留缝**：Windows 10/11 的 `GetWindowRect` 含一圈约 8px 的透明阴影边框，
  以前直接按它摆放，并排的两个窗口之间会露出十几像素的缝，贴边时也会离屏幕边缘一截。
  现在用 `DWMWA_EXTENDED_FRAME_BOUNDS` 取真实可见矩形做补偿（新增 `frame_insets` /
  `visible_rect`，`set_window_rect(..., exact=True)`）——实测左右半屏间隙从 16px 降到 0
- **最大化窗口跨屏仍是最大化**：以前 `SetWindowPos` 会把最大化「打破」成一个铺满的普通
  大窗口；现在先落到目标屏再重新最大化
- **跨屏相对位置不再偏移**：源屏与目标屏此前混用「全屏矩形」和「工作区」计算比例，
  任务栏占的那条被当成可移动范围；现在两边统一用工作区
- **分屏对最大化 / 最小化窗口生效**：先还原再摆放；不抢焦点模式下会把焦点还给原前台窗口
- **窗口列表清掉噪音**：剔除 15 类系统壳窗口（任务栏 / 桌面 / 开始菜单 / `CoreWindow` 等）、
  被 DWM 隐藏的挂起窗口（UWP 后台应用）以及最小化窗口
- **固定目标被最小化后仍可操作**：`_find_window` 不再走过滤后的列表，避免目标「消失」
- **枚举更快**：进程名按 PID 缓存，不再每次刷新都对每个窗口 `OpenProcess`
- **壁纸更稳**：`SetWallpaper` / `SetPosition` 现在校验 HRESULT，失败时回退
  `SystemParametersInfoW` 单屏方案（以前会静默失败）
- **热键即时生效**：消息线程运行中新增的键位会立即补注册，不用重启程序
- 摆放结果会校验可见矩形并在异常时记日志（最小尺寸限制 / UIPI 权限拦截可据此识别）

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
