# 交接 — multimon-manager（2026-11-07 深夜轮：顶边下压补偿——「窗口压住拖不动」的根因修复）

> 本文件是**当前**交接入口，读完即可接管。
> 代码根：`/Users/a1-6/AI Shared/repo/multimon-manager/`
> 版本 0.3.2 · **442** 个单测全绿 · 已推 `origin/master`（HEAD `4826d07`），本轮改动未提交
> 运行形态：`dist/多屏管理器.app`（桌面快捷方式 `~/Desktop/多屏管理器`，Finder alias）

## 0. 一分钟速览

| 项 | 值 |
|---|---|
| 本轮根因 A | `snap_three_stack` 用「上一段**实测**底部」推下一段起点，把 macOS 标题栏造成的 31px 偏移逐级传导、放大成缝隙 |
| 本轮根因 B | 辅助功能（Accessibility）授权被撤销后，所有 AppleScript 几何写入静默失败，界面只显示误导性的「需要至少 N 个可操作窗口」 |
| 修复 A | [windows_mac.py:1594](file:///Users/a1-6/AI%20Shared/repo/multimon-manager/windows_mac.py#L1594) 理想等分（余数摊前若干段）+ 起点按理想高度累加，不再读实测 |
| 修复 B | [windows_mac.py:456](file:///Users/a1-6/AI%20Shared/repo/multimon-manager/windows_mac.py#L456) `_osa()` 识别授权拒绝并置位；[ui.py:1570](file:///Users/a1-6/AI%20Shared/repo/multimon-manager/ui.py#L1570) 失败文案优先报授权失效 |
| 测试 | `bash run_tests.sh` → `Ran 436 tests / OK (skipped=1)`；新增 10 个用例 |
| git | `master` 与 `origin/master` **已同步**（HEAD `4826d07`）；本轮改动尚未提交，见 §4 |

## 1. 环境事实（接手前必读，能省几小时）

### 1.1 跑测试前必须先停掉 App 后台实例

`bash run_tests.sh` 会有 3 个 `test_single_instance` 失败，报 `应当能拿到锁`。**这不是代码 bug**——是 launchd 守护的 App 实例持着单例锁：

```
$ launchctl list | grep -i multimon
25389  0  application.com.cpufreestyle.multimonmanager.*
```

这些进程**不是** `Python main.py` 命令行形式，`pkill -f "main.py"` 匹配不到，`ps aux | grep 多屏管理器` 也常看不到（命令行被 launchd 改写）。清场方式：

```bash
pkill -9 -f "多屏管理器"; sleep 1
lsof $TMPDIR/multimon_manager.lock   # 必须为空
bash run_tests.sh
```

若仍有残留，用 `lsof` 出的 PID 直接 `kill -9`。锁文件在
`/private/var/folders/rz/y1d6t55x5y90zd4q09s_9x000000gn/T/multimon_manager.l`
（**不是** `/tmp/`，macOS 的 per-user TMPDIR）。同理，日志在
`/private/var/folders/rz/y1d6t55x5y90zd4q09s_9x000000gn/T/multimon-manager.log`
——排查运行时问题先看这里，比复现快得多。

### 1.2 Python 解释器必须选对

`run.sh` / `build.py` / `run_tests.sh` 都按候选顺序挑「真的能 `import tkinter`」的解释器。当前机器上：

- `/opt/homebrew/bin/python3` → **3.15，没有 tkinter**（`ModuleNotFoundError: Unsupported platform for standard library module '_tkinter'`），用它会静默少跑一整批 UI 测试
- `/opt/homebrew/bin/python3.14` → **可用**，跑 UI 相关脚本一律用它

`~/.multimon_python` 若存在会优先生效；**不要**把它设成 `/usr/bin/python3`（系统 3.9 + Tcl 8.5 会导致 tkinter 窗口黑屏，这正是历史上「页面空白」事故的根因）。

### 1.3 改了代码必须重新打包才生效

App 从 `dist/多屏管理器.app/Contents/Resources/` 运行，不是仓库根。改完 `.py` 后：

```bash
python3 build.py          # 会保留 settings.json / layouts.json / quad_cache.json 等用户数据
open ~/Desktop/多屏管理器
```

`build.py` 的用户数据快照/恢复逻辑是刻意做的（见 commit `2445444`），别改成「全量拷贝目录」——那会把内置模板文件当用户数据搬来搬去。

## 2. 本轮改动的技术细节

### 2.1 竖排三栏精度（用户报「不准」）

**现象**：竖屏（Mi Monitor 1152×2048，`work_rect=(1512,-1066,1152,2048)`）上点「竖排上中下」，三段之间有缝、高度不等。

**根因**：原实现写完后读回实测，用「上一段实测底部 +1px」作下一段起点。macOS 有系统级钳制——标题栏占住顶部约 30px，窗口物理上贴不到 `work_top`，于是顶段被整体下压 31px；这个偏移被当作下一段的起点，一级级传下去。实测偏差：

| 段 | 修复前 | 修复后 |
|---|---|---|
| 上 | y +31, h=682 | y +31（物理下限，无法消除）, h=683 |
| 中 | y **+32**, h=681 | y **0**, h=683 |
| 下 | y **+32**, h **652** | y **0**, h=682 |

另有一个独立缺陷：`2048 % 3 = 2` 的余数只补给了末段，三段高度不等也加重观感。

**修法**：理想等分（`base = wh//3`，余数摊到前 `rem` 段，每段最多差 1px），中/下段起点按上一段**理想**高度累加。单段自身的钳制交给 `set_window_rect` 已有的幂等重设（`_suppress_undo` / `_internal`）消化。

顶段那 31px 是 macOS 的物理限制，**不要再试图用坐标消掉它**——历史上试过反向补偿，反而引入了更严重的漂移。

### 2.2 辅助功能授权失效可见化（用户报「按钮没反应 + 下拉选不到程序」）

这两个表象是**同一个根因**：macOS 的 TCC 授权被撤销后，`osascript` 全部返回
`osascript is not allowed assistive access (-1719/-25211)`，而窗口枚举和几何写入**都**走 AppleScript。

日志里的铁证（1183 次）：
```
2026-11-07 20:09:50,482 WARNING windows_mac: AppleScript 执行失败:
  osascript is not allowed assistive access. (-25211)
```

**触发场景**：本轮我在 20:09 跑了 `run_tests.sh`，测试里的 monkeypatch 加上我多次用不同方式启动 Python，让 macOS 重新评估并撤销了授权。**下次自己跑测试也可能触发同样的事**，届时用户的 App 会突然「所有排列按钮失效」。

**修法**：不是恢复授权（那需要用户在系统设置里点），而是让失败可见——
- `windows_mac.py:456` `_osa()` 识别授权拒绝并置位，另加 `ax_permission_lost()` / `reset_ax_permission_lost()`
- `backend.py` 转发这两个函数给 UI
- `ui.py:1570` `_snap_failed_msg(what, need)` 优先返回「macOS 辅助功能授权已失效，请在『隐私与安全性→辅助功能』中重新勾选本程序」，而不是那句把人引向错误方向的「需要至少 N 个可操作窗口」

**给维护者**：若以后加新的排列入口，失败分支一律走 `self._snap_failed_msg(...)`，不要另写文案。

### 2.2.1 更正：§2.2 的根因判断对不上用户报的那次故障

**§2.2 说「按钮没反应 + 下拉选不到程序」是辅助功能授权被撤销导致的。这个结论对用户
 2026-11-07 深夜报的那次不成立**，此处显式更正，不去改上面的原文。

本轮复测拿到的反证：

- `windows_mac.ax_permission_lost()` 返回 `False`；`list_target_windows()` 当场返回
  7 个真实窗口；日志 00:45:13–00:45:19 有成功的 `移动窗口` / `四宫格排列完成`。
  授权没丢，窗口枚举和几何写入都是通的。
- 全新构建一次 UI，dump 所有 Combobox 的 `values`：目标窗口 / 并排左右 / 竖排上中下 /
  四格四角**全部是 `nvals=0`**——控件在，但候选列表是空的，点开自然「看不到程序」。
- `self._target_map` 只在 `_refresh_targets()` 里创建，而 `_snap_two_side_by_side`、
  `_snap_three_stack`、`_on_target_selected` 是无保护直读。映射不存在时这三个处理器
  一执行就 `AttributeError`，被 Tk 回调吞掉——表象正是「排列按钮点了没反应」。

**真正的根因**：`ui.App.__init__` 里那次 `self._refresh_targets()` 被弄丢了。
`git show 696a511:ui.py` 第 147 行还在 `_build()` 之后调它；`9a7b118`
（极简/高级双模式）重写 `__init__` 时漏掉，之后再没人补上。启动路径上
`_refresh_targets()` 只剩三个调用点：「刷新列表」按钮、`show()`（托盘唤醒）、
显示器变化回调——用户不碰这三处，窗口下拉就永远是空的。

**影响范围**：只影响「新启动后没点过刷新列表/没从托盘唤醒过」的会话。
已经点过一次的会话不受影响，所以这个问题在手工测试里很难复现。

**修法**：[ui.py:166](file:///Users/a1-6/AI%20Shared/repo/multimon-manager/ui.py#L166) 在
`_build()` / `refresh_monitors()` 之后补回 `self._refresh_targets()`。
注意**不要**顺手加 `self._apply_topmost()`——那个方法早已改成「切换目标窗口置顶」的
按钮处理器，启动时调一次会真的去动别人的窗口。

**回归测试**：`tests/test_startup_window_lists.py`（4 个用例）。反向验证过：把这次调用
删掉，4 个用例里 3 个 failure + 1 个 error。

**给维护者**：§2.2 那套授权失效可见化本身仍然有用（授权确实可能被撤销，那是另一条
真实故障路径），保留。但再遇到「按钮没反应 + 下拉空的」组合，先查
`_refresh_targets()` 在不在启动路径上，别再往授权上靠。

### 2.3 本轮新增/改动的文件

| 文件 | 改动 |
|---|---|
| `windows_mac.py` | 三栏理想等分；`_osa()` 授权检测 + `ax_permission_lost()` / `reset_ax_permission_lost()` |
| `backend.py` | 转发 `ax_permission_lost` / `reset_ax_permission_lost` |
| `ui.py` | `_snap_failed_msg()` / `_open_ax_settings()`；并排/竖排/四宫格三个失败分支改走前者 |
| `tests/test_three_stack_geometry.py` | **新增** 5 个用例（理想几何 + 中下段不吃实测偏移） |
| `tests/test_ax_permission.py` | **新增** 5 个用例（授权拒绝识别，且不误判普通语法错误） |

`test_mid_and_bottom_use_ideal_origins` 是本次修复的核心回归：它 monkeypatch `set_window_rect` 模拟「顶段被下压 31px」，断言中/下段起点仍按理想高度算。修复前它是失败的（三段总高 2015 ≠ 2048）。

## 2.5 顶边下压补偿（本轮核心，接手者必读）

**用户报的现象**：「有些窗口无法手动移动 / 拖不动」「排列不准」。

**根因**：macOS 不让窗口盖住菜单栏。在非主屏（如 Mi 竖屏 1152x2048，
work_top=-1066）贴顶放置时，窗口顶边被系统整体下压 **31px**（实测
WorkBuddy / Comet / OneNote / ChatGPT / 系统设置 / Marvis 全部一样——是
**屏级**限制，与窗口无关；主屏 work_top=34 不压）。此前三栏/四格只按理想
格子写坐标，最上面那块实际比目标低 31px 且高度没变 → 底边比格子低 31px →
**压住第二块** → 下层窗口的标题栏被上层的画面盖住，用户点标题栏抓到的是
上面那扇，表现就是「这扇窗拖不动」。

**修复**：新增 `windows_mac.py:place_tile()`——写入目标后读回，若顶边被
下压 dy px，就把高度从 h 减到 h-dy 并**保持底边不动**再写一次。消费方：
`snap()`（所有单窗分区）、`snap_two_side_by_side()`（左右/上下并排）、
`snap_three_stack()`（顶段）、`_quad_write()`（四宫格贴顶的格子）。偏差
报告用 `_last_quad_written`（补偿后的实际写入值）作基准，菜单栏占位不再
被误报成「不准」。

**防误判**（都在 place_tile 里）：App 把尺寸钳在别处时读回的宽高与请求
不一致 → 不补偿；CG 几何写前写后完全一致（写入没生效）→ 不补偿；补完后
高度 < 120px（格子被压成一条缝）→ 保持原高度，宁可轻微重叠。

**测试**：`tests/test_top_push_compensation.py` 新增 6 例（补偿/不补偿/
钳制/写入无效/缝保护/三栏无重叠）。`run_tests.sh` → `Ran 442 tests / OK`。

**真机验证**：`python3 tests/verify_stack_portrait_real.py` 三段首尾相接、
无重叠、末段收在屏幕底（顶边内缩 31px 属菜单栏占位，判据已按「不重叠」
重写）。`tests/verify_portrait_zones_real.py` 把「App 自身尺寸钳制」单独
归类不计入几何偏差（真机 Marvis 最小 900 宽、系统设置 754，塞不进窄格是
App 限制不是程序不准），合计不准 0。

## 3. 已知平台限制（不要「修」，已文档化）

`README.md:141` 记了一条：**窗口置顶切换是 Windows 独有**。macOS 的 `AXFloating` 写入被系统拒绝（-10006），SkyLight/CGS 接口从非 WindowServer 客户端调用无效。曾有人想「修好它」，结论是做不到，已在 README 明示不提供该按钮。

另：tkinter 的 `ttk::button` 不向 macOS 辅助功能暴露 `AXTitle`，所以 `osascript ... click button "竖排上中下"` 会失败（报 -1728 找不到）。**做 UI 自动化时不要走 AppleScript 点按钮**，用 `btn.invoke()`（在 Tk 实例内）或 CGEventPost 合成点击。

## 4. 未决 / 下一步

1. **本轮改动尚未提交**。`master` 与 `origin/master` 已同步在 `4826d07`，工作区有 3 个改动文件（`windows_mac.py` / `backend.py` / `ui.py`）+ 2 个新测试文件未提交。历史上推送被 Shadowrocket 的 TLS DPI 阻断过（`LibreSSL SSL_connect: SSL_ERROR_SYSCALL`），20:1x 有一次推送成功说明网络会恢复。**推送前若再遇 SSL 失败，让用户把 GitHub 设成 DIRECT 或退出代理**，别在应用层找原因。
2. **`verify_stack_portrait_real.py` / `verify_portrait_zones_real.py` 两个真机校验脚本写死了「小米屏」**（`"Mi" in m.device_name`）。当前竖屏可能是 DELL U2414H，跑出来必然 FAIL。应改成「自动找 `height > width` 的屏，找不到就 SKIP」。这两个是脚本不是 TestCase（入口有 `if __name__` 保护），`run_tests.sh` 的 discover 不会加载它们，要手动跑：

```bash
python3 tests/verify_stack_portrait_real.py
```
3. **真人鼠标点击未闭环**。本轮我用 `btn.invoke()` 验证了绑定与逻辑（确实重排了窗口），CGEventPost 合成点击送不到 App（macOS 限制）。**「用户双击桌面快捷方式后点按钮」这一环仍待真人确认**。
4. **四窗 2×2 已存在**，叫 `snap_four_quad`（`windows_mac.py:3214`），不是 `snap_four_grid`。带容量预检（`quad_capacity()`）、最小尺寸缓存（`quad_cache.json`）、智能分配（按宽高比重排四角）、排列预演（`quad_predict`）。改四宫格行为前先读这几个函数，别新写一套数学。

## 5. 快速命令

```bash
cd "/Users/a1-6/AI Shared/repo/multimon-manager"

# 跑测试（先按 §1.1 清场）
pkill -9 -f "多屏管理器"; sleep 1
bash run_tests.sh

# 重新打包 + 启动
python3 build.py && open ~/Desktop/多屏管理器

# 看运行时日志（排障第一站）
tail -f /private/var/folders/rz/y1d6t55x5y90zd4q09s_9x000000gn/T/multimon-manager.log

# 枚举显示器 / 窗口（调试几何）
python3 -c "import monitors_mac as m; [print(i,x.device_name,x.work_rect) for i,x in enumerate(m.enum_monitors())]"
python3 -c "import windows_mac as w; [print(x['hwnd'],x['w'],'x',x['h']) for x in w.list_target_windows()]"
```
