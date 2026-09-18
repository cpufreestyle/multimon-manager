# 多屏管理器（跨平台）

一个用纯 Python 实现的多显示器管理小工具，主要能力包括：
**每屏不同壁纸、窗口跨屏移动、分屏吸附（含三分屏/四等分）、壁纸与窗口布局方案、
全局快捷键、显示器热插拔响应、托盘/菜单栏**。

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

## 快速上手（实用建议）

首次使用建议按以下顺序配置，能让后续功能立刻"用出效果"：

1. **授权辅助功能**（macOS，最关键）
   系统设置 → 隐私与安全性 → 辅助功能，开启运行本程序的 Python/终端。
   未授权时窗口控制、全局快捷键、拖拽吸附会**静默失效**（不报错也不生效），
   极易误判为 bug。主界面顶部的「上手建议」卡片会实时提示授权状态与屏数，可点
   「刷新状态」复查（授权后不必重启）。
2. **给显示器起别名**：在显示器列表行内直接输入（回车即存），如「主屏 / 副屏 / 竖屏」，
   之后所有列表、下拉、布局图与情景都更易辨认。
3. **建一个情景模式**：把当前显示器组合绑定一套「壁纸方案 + 布局」，插拔显示器时自动套用。
   组合按分辨率签名，与位置/系统屏号无关，跨会话稳定。

### 高频技巧
- **拖拽吸附**：拖动窗口时显示目标分区浮层，松手即归位；误移动可用撤销还原
  （macOS `⌘⌥Z`、Windows `Ctrl+Alt+Z`）。
- **窗口规则**：让某类窗口出现即归位（如 IDE→主屏、浏览器→副屏、终端→第三屏）。
  「目标屏」选**当前屏（不换屏）**时按窗口中心点所在屏分区，最稳，不绑定具体显示器。
  规则**顺序即优先级**，靠前先命中；只对"新出现的窗口"生效，不会一启动就重排全部窗口。
- **布局快照**：复杂排布一键存/取，重装后秒还原。
- **设置导入导出**：换机/重装前导出，导入前自动备份到 `_settings_backup/`。
- **托盘常驻**：不用开主窗口即可切换情景。

### 常见场景推荐
| 场景 | 推荐配置 |
|---|---|
| 开发 | IDE→主屏、浏览器→副屏、终端/日志→竖屏；情景「编码」 |
| 演示 | 情景「演示」套低分辨率布局，托盘一键切回「编码」 |
| 交易 | 按窗口标题把各平台钉到固定分区；情景「交易」 |
| 写作 | 浏览器 / PDF→副屏；情景「写作」 |

### 常见坑
- **单屏环境**：情景、拖拽吸附、窗口规则的多屏效果需**多屏真机**验证；单屏下 UI 与配置读写正常。
- **静默失效**：窗口控制/热键没反应时，优先排查辅助功能授权，而非代码。
- **导入覆盖**：导入设置会覆盖现有配置（已自动备份，可从 `_settings_backup/` 还原）。

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

## 功能
- **每屏不同壁纸 / 统一单图**：壁纸区顶部会**可视化显示器布局**，点击某块屏可直接为它选图
- **窗口跨屏移动**：当前活动窗口移到上一/下一屏（保持相对位置）
- **分屏吸附**：左/右/上/下半屏、最大化、居中，以及**三分屏（左/中/右）**与**四等分（2×2）**
- **壁纸方案**：保存/加载多套配置（JSON）
- **窗口布局方案**：一键保存当前所有窗口的位置/大小，之后整体还原（按「应用::窗口」匹配，标题变化自动按应用名兜底）
- **显示器热插拔**：显示器增减/重排时自动刷新列表，可选自动重应用上次壁纸方案
- **全局快捷键**（修饰键默认 macOS `⌘⌥`、Windows `Ctrl+Alt`，可切换）：
  - `←/→`：活动窗口移到上一/下一屏
  - `1/2/3/4`：左/右/上/下半屏；`5/6`：最大化 / 居中；`7/8/9`：三分屏（左/中/右）
- **托盘 / 菜单栏**：打开主界面、**应用窗口布局**、刷新显示器、退出
- **开机自启**：主界面一键开关（Windows 注册表 / macOS LaunchAgent）
- **单实例**：重复启动自动提示并退出
- **显示器别名**：为每块屏起名（如「主屏 / 副屏 / 竖屏」），列表、下拉与布局图优先显示别名
- **撤销窗口移动**：误移动/吸附后一键回退（macOS `⌘⌥Z`、Windows `Ctrl+Alt+Z`）
- **情景模式**：把「壁纸方案 + 布局」绑定到显示器组合签名，插拔/重排时自动套用
- **情景定时套用**：到点（`HH:MM`）自动套用指定情景，每天一次
- **分屏吸附预览**：拖拽窗口时高亮"将落入的区域"，松手即吸附；浮层点击穿透、不打断拖拽
- **窗口规则引擎**：按应用名/标题把窗口自动分配到指定屏与位置；支持**内置模板一键导入**与**从当前窗口一键学习**
- **壁纸幻灯片**：把一组图片加入幻灯片，按间隔自动轮换（可随机、可立即切换）
- **设置导入 / 导出**：打包与还原 `settings.json` / `profiles.json` / `layouts.json`

## 文件结构
| 文件 | 职责 |
|---|---|
| `backend.py` | 平台分发：按 `sys.platform` 选择 Windows / macOS 实现 |
| `monitors.py` / `monitors_mac.py` | 显示器枚举（Windows / macOS） |
| `wallpaper.py` / `wallpaper_mac.py` | 壁纸设置（Windows COM / macOS db） |
| `windows.py` / `windows_mac.py` | 窗口跨屏移动与分屏（Windows / macOS） |
| `hotkeys.py` / `hotkeys_mac.py` | 全局快捷键（Windows / macOS） |
| `tray.py` / `tray_mac.py` | 托盘/菜单（Windows / macOS） |
| `display_notify.py` / `display_notify_mac.py` | 显示器热插拔监听（macOS 用 CoreGraphics 回调，其他平台占位） |
| `profiles.py` | 壁纸方案保存/加载（跨平台） |
| `layouts.py` | 窗口布局方案保存/加载（跨平台） |
| `scenarios.py` | 情景模式（组合签名匹配）与定时套用 |
| `rules.py` | 窗口规则引擎 + 内置模板 + 从当前窗口学习 |
| `slideshow.py` | 壁纸幻灯片（选下一张 + 状态持久化） |
| `snap_preview.py` | 分屏吸附预览浮层（半透明、点击穿透） |
| `dragsnap.py` / `dragsnap_mac.py` | 全局拖拽监听（macOS `CGEventTap` / Windows 占位） |
| `accessibility_mac.py` | macOS 辅助功能授权检测与引导 |
| `settings.py` | 轻量偏好存储（原子写 + 缓存；快捷键、规则、情景、幻灯片等） |
| `cmd_channel.py` | 托盘子进程 ↔ 主进程的命令通道（临时文件） |
| `_tray_panel.py` | 托盘快速面板（独立子进程运行） |
| `autostart.py` | 开机自启（Windows / macOS） |
| `resources.py` | 纯 Python 生成程序图标 |
| `ui.py` | tkinter 主界面（跨平台） |
| `main.py` | 入口（跨平台） |

## 可作练手扩展的方向
- 多屏任务栏、窗口标题栏按钮
- 显示器配置/分辨率切换方案（分辨率、主屏切换）
- 窗口布局按显示器配置自动匹配（当前按窗口标识匹配）
- 触发器（窗口移动自动触发动作）
- 更多平台（Linux 可加 `monitors_linux.py` 等，在 `backend.py` 注册分支即可）
