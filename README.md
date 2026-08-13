# 多屏管理器（DisplayFusion 风格仿制 · 跨平台）

一个用纯 Python 实现的多显示器管理小工具，对标 DisplayFusion 的核心功能：
**每屏不同壁纸、窗口跨屏移动、分屏吸附、壁纸方案、全局快捷键、托盘/菜单栏**。

**零第三方依赖**：仅用 Python 标准库 + `ctypes` + 内置 `tkinter`，外加各系统自带命令/框架。
运行时自动按平台选择实现，Windows 与 macOS 共用同一套 GUI 与逻辑。

## 支持平台
| 平台 | 显示器枚举 | 壁纸 | 窗口管理 | 快捷键 | 托盘/菜单 |
|---|---|---|---|---|---|
| Windows | `EnumDisplayMonitors` | `IDesktopWallpaper` COM | `SetWindowPos` | `RegisterHotKey` | Shell 托盘 |
| macOS | `system_profiler` | `desktoppicture.db` + `osascript` | System Events AppleScript | `Quartz` CGEventTap | Dock 菜单 + 常驻面板 |

## 运行
无需联网安装任何依赖，仅需 Python 3.8+（macOS 用系统自带 `/usr/bin/python3` 或任意 venv 均可）。

```bash
# Windows
python main.py

# macOS
python3 main.py
```

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

## 功能
- **每屏不同壁纸 / 统一单图**
- **窗口跨屏移动**：当前活动窗口移到上一/下一屏（保持相对位置）
- **分屏吸附**：左/右/上/下半屏、最大化、居中
- **壁纸方案**：保存/加载多套配置（JSON）
- **全局快捷键**：`Ctrl+Alt+←/→` 移屏，`Ctrl+Alt+1~6` 分屏
- **托盘 / 菜单栏**：打开主界面、刷新显示器、退出
- **开机自启**：主界面一键开关（Windows 注册表 / macOS LaunchAgent）
- **单实例**：重复启动自动提示并退出

## 文件结构
| 文件 | 职责 |
|---|---|
| `backend.py` | 平台分发：按 `sys.platform` 选择 Windows / macOS 实现 |
| `monitors.py` / `monitors_mac.py` | 显示器枚举（Windows / macOS） |
| `wallpaper.py` / `wallpaper_mac.py` | 壁纸设置（Windows COM / macOS db） |
| `windows.py` / `windows_mac.py` | 窗口跨屏移动与分屏（Windows / macOS） |
| `hotkeys.py` / `hotkeys_mac.py` | 全局快捷键（Windows / macOS） |
| `tray.py` / `tray_mac.py` | 托盘/菜单（Windows / macOS） |
| `profiles.py` | 壁纸方案保存/加载（跨平台） |
| `autostart.py` | 开机自启（Windows / macOS） |
| `resources.py` | 纯 Python 生成程序图标 |
| `ui.py` | tkinter 主界面（跨平台） |
| `main.py` | 入口（跨平台） |

## 与 DisplayFusion 的差距（可作练手扩展）
- 多屏任务栏、窗口标题栏按钮
- 显示器配置/分辨率切换方案
- 触发器（窗口移动自动触发动作）、壁纸幻灯片
- 更多平台（Linux 可加 `monitors_linux.py` 等，在 `backend.py` 注册分支即可）
