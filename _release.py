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
TAG = "v0.3.0"
TITLE = "v0.3.0 — 壁纸轮换 / 指定屏移动 / 配置备份"
REPO = "cpufreestyle/multimon-manager"
BRANCH = "master"
API = f"https://api.github.com/repos/{REPO}"

# 代理：沙箱/墙内直连 GitHub 常不可达，按顺序探测本地端口
PROXY_CANDIDATES = [os.environ.get("HTTPS_PROXY"), os.environ.get("https_proxy"),
                    "http://127.0.0.1:7897", "http://127.0.0.1:10809", None]

RELEASE_BODY = """## v0.3.0 更新内容

> 本版合并了 v0.2.3 ~ v0.2.5 三个内部版本的全部改动（此前未单独发布），是 v0.2.2 之后的一次大版本更新。

### 新增功能
- **壁纸轮换（幻灯片）**：指定图片目录与间隔（分钟）自动轮换壁纸。「每屏不同」模式下同一时刻各屏显示不同图片，「统一单图」模式下所有屏同步切换；目录与间隔会被记住
- **移到指定屏**：窗口工具区新增目标屏下拉与按钮，多屏时一次跳到指定显示器，不用再连点「上一屏 / 下一屏」
- **布局方案绑定显示器配置**：保存窗口布局时记录显示器指纹（数量 / 分辨率 / 相对位置），配置变化后应用前会提示确认，避免窗口按旧坐标落到错误的屏幕
- **配置备份 / 恢复**：一键把快捷键偏好、热插拔开关、壁纸轮换设置、窗口布局方案与壁纸方案导出为单个 JSON，换机或重装后导入恢复（合并，同名覆盖）
- **并排左右 / 竖排上中下**（v0.2.3 起）：两个窗口排到左右半屏、三个窗口堆到上 / 中 / 下三栏；可选目标显示器，并可显式指定参与排列的窗口。Windows 与 macOS 同款
- **目标窗口下拉框**（v0.2.3 起）：「自动判断」经常猜错，可手动固定要操作的窗口
- **置顶切换**（v0.2.5）：把目标窗口设为 always-on-top 或取消
- **窗口布局方案**：一键保存当前所有窗口的位置 / 大小并整体还原（按「应用::窗口」匹配，标题变化自动按应用名兜底）
- **开机自启**：支持「静默启动」——开机后只进托盘、不弹主窗口
- **托盘菜单**：新增「应用最近窗口布局」

### 修复与优化
- **界面改为单页 + 垂直滚动**（v0.2.4）：原先的分页面板在窗口不够高时下方面板会被截断
- **修复 Windows 120 DPI 下界面横向截断**：窗口工具区的「并排左右」按钮与整个「竖排上中下」按钮被挤出可视区（画布只做纵向滚动，横向溢出无法补救）；现已收窄相关下拉宽度、默认窗口改为 820×740 并抬高最小宽度
- **修复版本号漂移**：`build.py` 一直读不到 `version_info.py` 里的版本号（该文件是 PyInstaller 的 `eval()` 表达式，不含 `VERSION` 属性），macOS `Info.plist` 版本号被硬编码成 0.2.3、安装器注册表版本号停留在 0.2.2；现统一以新增的 `app_version.py` 为唯一来源，打包前还会校验与 `version_info.py` 一致
- **统一配置存储路径**：壁纸方案原先固定写在 `%APPDATA%`，而设置 / 窗口布局在源码运行时写在仓库目录，三份配置分居两处；现统一由 `settings.data_dir()` 决定，并保留一次旧位置回退读取，升级后老方案不丢
- **壁纸轮换扫描时校验文件头**：目录里混进损坏 / 空文件时 `SetWallpaper` 会成功返回但把壁纸清空，现在直接把这类文件剔除
- 修复独立打包（PyInstaller）后无法启动：托盘改用环境变量区分、冻结时跳过运行时图标、构建流程避开批量删除
- 修复 macOS 启动即崩溃、以及全局快捷键从未真正生效（事件掩码 / 字段号 / tap 位置三处常量错误）
- `windows.py` 的 `GetWindowLongPtr` 参数声明提到模块加载期一次性设置，避免热键线程与主线程并发调用时互相改写 ctypes 的全局函数签名
- 仓库整洁：构建调试产物 `release.err` / `release.log` 移出版本库

### 附件
- MultiMonManager.exe：主程序（单文件，可直接运行）
- MultiMonManager-Setup.exe：安装包（安装到 `%LOCALAPPDATA%\\MultiMonManager`，含开始菜单 / 桌面快捷方式与卸载项）

> Windows 版「每屏不同壁纸」需 Windows 8+；macOS 需在「系统设置 → 隐私与安全性 → 辅助功能」中授权，否则窗口控制与全局快捷键不可用。
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
