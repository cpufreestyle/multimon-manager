# 多屏管理器（跨平台）

一个用纯 Python 实现的多显示器管理小工具，主要能力包括：
**每屏不同壁纸与壁纸轮换、窗口跨屏移动（含跳到指定屏）、分屏吸附（含三分屏/四等分）、
壁纸与窗口布局方案、配置备份恢复、全局快捷键、显示器热插拔响应、托盘/菜单栏**。

**零第三方依赖**：仅用 Python 标准库 + `ctypes` + 内置 `tkinter`，外加各系统自带命令/框架。
运行时自动按平台选择实现，Windows 与 macOS 共用同一套 GUI 与逻辑。

## 支持平台
| 平台 | 显示器枚举 | 壁纸 | 窗口管理 | 快捷键 | 托盘/菜单 |
|---|---|---|---|---|---|
| Windows | `EnumDisplayMonitors` | `IDesktopWallpaper` COM | `SetWindowPos` | `RegisterHotKey` | Shell 托盘 |
| macOS | `CoreGraphics` + `system_profiler` | `desktoppicture.db` + `osascript` | System Events AppleScript | `Quartz` CGEventTap | Dock 菜单 + 常驻面板 |

## 运行
无需联网安装任何依赖，仅需 Python 3.8+（macOS 用系统自带 `/usr/bin/python3` 或任意 venv 均可）。

```bash
# Windows
python main.py

# macOS
python3 main.py
```

## 打包（生成可双击的应用）

```bash
python3 build.py          # 为当前平台打包
python3 build.py --clean  # 清理构建产物
```

- **macOS**：生成 `dist/多屏管理器.app`，**双击即可运行**。用系统 `sips`/`iconutil`
  把 `assets/` 里的图标转成 `.icns`，使用系统 `python3`，零第三方依赖。
  若系统默认 `python3` 不是你要的解释器，可执行 `echo /your/python3 > ~/.multimon_python` 指定。
- **macOS 独立版**：`python3 build.py --frozen` 用 PyInstaller 生成**自带 Python 运行时**
  的 `.app`（不依赖系统 python3，可直接分发）。需先 `pip install pyinstaller`。
- **Windows**：先 `pip install pyinstaller`，脚本生成单文件 `dist/MultiMonManager.exe`；
  图标优先用 Pillow 从 `assets/` 转换（未装 Pillow 时回退到内置图标）。

## 平台注意事项
### Windows
- 每屏壁纸需 **Windows 8+**（依赖 `IDesktopWallpaper`），旧系统回退单屏壁纸。
- 填充方式为全局设置（Windows 接口限制）。
- 分屏 / 跨屏摆放会补偿系统的透明阴影边框：`GetWindowRect` 含一圈约 8px 的不可见
  区域，直接按它摆放会让并排的两个窗口之间露出十几像素的缝、贴边时也会离屏幕边缘
  一截。程序用 `DWMWA_EXTENDED_FRAME_BOUNDS` 取真实可见矩形做补偿。
- 窗口列表会剔除任务栏 / 桌面 / 开始菜单等系统壳窗口、被 DWM 隐藏的挂起窗口
  （UWP 后台应用）以及最小化窗口，只列屏幕上真正在用的窗口。

### macOS
- **首次运行需授权辅助功能**：系统设置 → 隐私与安全性 → 辅助功能，把运行本程序的
  Python/终端加入列表，否则全局快捷键与窗口控制不可用。
- 每屏不同壁纸通过直接写 `~/Library/Application Support/Dock/desktoppicture.db` 实现，
  写后自动 `killall Dock` 使设置生效。
- 托盘在 macOS 上表现为 **Dock 右键菜单 + 一个常驻右上角的快速面板**（原生菜单栏
  extra 是私有 API，标准库无法零依赖实现）。
- 窗口管理基于 System Events，对多数 App 有效；部分沙盒 App（如某些全屏游戏）可能受限。
- **显示器热插拔**：通过 `CGDisplayRegisterReconfigurationCallback` 监听显示器增减/重排，
  可在「显示器热插拔」面板开关"自动刷新列表"与"自动重应用上次壁纸方案"。
- **全局快捷键**：修饰键默认 `⌘⌥`（也可切换为 `⌃⌥`），数字键 1–9 对应各分屏动作（见下）。

## 配置文件位置

| 运行方式 | 位置 |
|---|---|
| 源码运行 | 仓库根目录（`settings.json` / `layouts.json` / `profiles.json`） |
| 打包运行 | Windows `%APPDATA%\MultiMonManager\`；macOS `~/Library/Application Support/MultiMonManager/` |

三份配置可用主界面「配置备份」一键导出为单个 JSON，换机后导入恢复。
旧版本把壁纸方案固定写在 `%APPDATA%\MultiMonManager\`，升级后读取会自动回退到旧位置，
并在下次保存时迁移，老方案不会丢。

## 功能
- **每屏不同壁纸 / 统一单图**：壁纸区顶部会**可视化显示器布局**，点击某块屏可直接为它选图
- **壁纸轮换（幻灯片）**：指定一个图片目录与间隔（分钟），自动轮换壁纸；「每屏不同」模式下同一时刻各屏显示不同图，「统一单图」模式下所有屏同步切换
- **窗口跨屏移动**：当前活动窗口移到上一/下一屏（保持相对位置），或**一次跳到指定屏幕**（下拉选择目标屏）；最大化窗口跨屏后仍是最大化状态（Windows）
- **窗口置顶切换**：把目标/活动窗口设为 always-on-top 或取消（Windows）
- **分屏吸附**：左/右/上/下半屏、最大化、居中，以及**三分屏（左/中/右）**与**四等分（2×2）**；Windows 下自动补偿系统透明边框，两半屏之间不留缝、贴边不留白
- **壁纸方案**：保存/加载多套配置（JSON）
- **窗口布局方案**：一键保存当前所有窗口的位置/大小，之后整体还原（按「应用::窗口」匹配，标题变化自动按应用名兜底）；方案会记录保存时的**显示器配置指纹**，显示器数量或排列变化后应用前会先提示
- **显示器热插拔**：显示器增减/重排时自动刷新列表，可选自动重应用上次壁纸方案
- **配置备份**：把偏好、窗口布局与壁纸方案导出为单个 JSON，换机/重装后导入即可恢复（合并，同名覆盖）
- **全局快捷键**（修饰键默认 macOS `⌘⌥`、Windows `Ctrl+Alt`，可切换）：
  - `←/→`：活动窗口移到上一/下一屏
  - `1/2/3/4`：左/右/上/下半屏；`5/6`：最大化 / 居中；`7/8/9`：三分屏（左/中/右）
  - `Shift+1/2/3/4`：四象限（左上/右上/左下/右下）
  - 快捷键开关状态会记住，重启后自动恢复
- **托盘 / 菜单栏**：打开主界面、**应用窗口布局**、刷新显示器、退出
- **开机自启**：主界面一键开关（Windows 注册表 / macOS LaunchAgent），可选静默启动（最小化到托盘）
- **单实例**：重复启动自动提示并退出

## 文件结构
| 文件 | 职责 |
|---|---|
| `main.py` | 入口（跨平台，含单实例、DPI 感知、托盘启动） |
| `backend.py` | 平台分发：按 `sys.platform` 选择 Windows / macOS 实现 |
| `ui.py` | tkinter 主界面（跨平台，单页 + 垂直滚动） |
| `monitors.py` / `monitors_mac.py` | 显示器枚举（Windows / macOS） |
| `wallpaper.py` / `wallpaper_mac.py` | 壁纸设置（Windows COM / macOS db） |
| `windows.py` / `windows_mac.py` | 窗口跨屏移动与分屏（Windows / macOS） |
| `hotkeys.py` / `hotkeys_mac.py` | 全局快捷键（Windows / macOS） |
| `tray.py` / `tray_mac.py` | 托盘/菜单（Windows / macOS） |
| `display_notify.py` / `display_notify_mac.py` | 显示器热插拔监听（macOS 用 CoreGraphics 回调，其他平台占位） |
| `accessibility_mac.py` | macOS 辅助功能授权检测与引导 |
| `dock_icon_mac.py` | macOS Dock 图标设置 |
| `profiles.py` | 壁纸方案保存/加载（跨平台） |
| `layouts.py` | 窗口布局方案保存/加载 + 显示器配置指纹（跨平台） |
| `config_io.py` | 全部配置的导出/导入（备份与恢复） |
| `settings.py` | 轻量偏好存储（快捷键修饰键、热插拔开关、壁纸轮换等）与配置目录定位 |
| `cmd_channel.py` | 托盘子进程 ↔ 主进程的命令通道（临时文件） |
| `_tray_panel.py` | 托盘快速面板（独立子进程运行） |
| `autostart.py` | 开机自启（Windows / macOS） |
| `resources.py` | 纯 Python 生成程序图标 |
| `app_version.py` | 版本号唯一来源（打包/安装脚本共用） |
| `version_info.py` | Windows exe 版本资源（PyInstaller `--version-file`，需保持单表达式） |
| `build.py` | 打包脚本（macOS `.app` / Windows exe） |
| `install.py` | Windows 安装器（复制 exe、建快捷方式、注册卸载信息） |
| `_release.py` | 发布脚本（打 tag、创建 Release、上传附件） |

## 可作练手扩展的方向
- 多屏任务栏、窗口标题栏按钮
- 显示器配置/分辨率切换方案（分辨率、主屏切换）
- 触发器（窗口移动自动触发动作）
- 更多平台（Linux 可加 `monitors_linux.py` 等，在 `backend.py` 注册分支即可）
