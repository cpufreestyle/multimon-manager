# 交接 — multimon-manager（2026-11-13：四格「排完就废」三处根因修复 + 真机验证）

> 本文件是**当前**交接入口，读完即可接管。
> 代码根：`/Users/a1-6/AI Shared/repo/multimon-manager/`
> 版本 0.3.2 · 全套单测通过 · `master` **领先 `origin/master` 1 个提交（未推送）**，HEAD `4187716`
> 运行形态：`dist/多屏管理器.app`（桌面快捷方式 `~/Desktop/多屏管理器`，Finder alias）
> 上一轮（2026-11-08：启动下拉、授权提示、顶边补偿、容量提示）的细节保留在 §2.1–§2.6。

## 0. 一分钟速览

| 项 | 值 |
|---|---|
| 本轮根因 A | 四个排列处理器排完立刻 `release_window()`，把自己刚写进 `push_undo` / `remember_arranged` 的记忆删掉 → 「撤销移动」「回摆放位」对刚排的窗口全部失效 |
| 本轮根因 B | AX 兜底无条件改用主窗口，把用户点名的 `#3` 覆盖成 `window 1` → UU远程等同名多窗口，排第 3 扇动的却是第 1 扇 |
| 本轮根因 C | 13 个吸附按钮挤在一行，自然宽 1005px > 最小尺寸下可视宽约 775px；**packer 装不下就不映射末尾控件** → 「释放窗口 / 暂存 / 取回」对象还在、command 还绑着，但用户看不见也点不到 |
| 修复 A | 删掉 `_snap_two_side_by_side` / `_snap_three_stack` / `_snap_four_quad` / `_fill_grid` 里的自动释放块；释放只由用户点「释放窗口」触发（`ui.py:574`） |
| 修复 B | `_ax_window_by_geometry` / `_ax_window_by_name` 增 `seq` 参数，带显式序号时禁用主窗口兜底（`windows_mac.py:1026` / `:1098`），4 个调用点全部传 `seq` |
| 修复 C | 吸附按钮拆成三行（8 整/半屏 + 8 三分之一/四角 + 5 记忆/暂存），`ui.py:791`；顺带删掉从未被调用的 `release_all_windows()` |
| 测试 | `python@3.14` 全套 **508 例 OK（skipped=1，约 40 个测试文件）**，用隔离 `TMPDIR` 跑，**没停正在运行的 App**（见 §1.1 + §2.8 看门狗） |
| 真机 | 重打包 + 重启后走托盘命令通道真排了一次 2×2：RV200 Pro 上 3 个窗口 0 重叠 0 越界，第 4 个被容量预检挡下（详见 §2.7） |
| git | 代码 HEAD `4187716`，**本地领先 origin/master 1 个提交，用户明确选择先不推送**；**工作区另有 15 个未提交项**（省电守护 `power_guard.py` + 全量单测挂死修复，见 §2.8），全部未 commit |

## 1. 环境事实（接手前必读，能省几小时）

### 1.1 跑测试前必须先停掉 App 后台实例

`bash run_tests.sh` 会有 3 个 `test_single_instance` 失败，报 `应当能拿到锁`。**这不是代码 bug**——是 launchd 守护的 App 实例持着单例锁：

```
$ launchctl list | grep -i multimon
25389  0  application.com.cpufreestyle.multimonmanager.*
```

这些进程**不是** `Python main.py` 命令行形式，`pkill -f "main.py"` 匹配不到，`ps aux | grep 多屏管理器` 也常看不到（命令行被 launchd 改写）。

**首选：不停 App 也能跑全套**——锁路径跟着 `TMPDIR` 走，给它一个临时目录即可与运行中的实例完全隔离（本轮 466 例就是这么跑绿的，用户桌面不受打扰）：

```bash
TD=$(mktemp -d); TMPDIR="$TD" bash run_tests.sh
```

**要清场时**（例如要验单例行为本身）：

```bash
pkill -9 -f "多屏管理器"; sleep 1
lsof $TMPDIR/multimon_manager.lock   # 必须为空
bash run_tests.sh
```

`pkill` 打中的可能只是外层 shell，**真正持锁的是它的 Python 子进程**：本轮杀完 bash 后 `lsof` 仍列出另一个 PID，按那个 PID 精确 `kill -9` 才释放。**别去删锁文件**——先 `lsof` 查清谁持着，删文件只会让下一个实例和残留进程同时跑。锁文件在
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

### 2.2 一次误判的纠正 + 授权失效可见化

**先纠正旧结论**：§2.2 早期把「按钮没反应 + 下拉选不到程序」归因于 TCC 授权被撤销，这个判断**不成立**。日志里的授权错误确实存在（1183 次），但窗口枚举失败时界面还能显示启动前的旧列表，真正让新会话按钮全哑的是下面 §2.3 的 `_refresh_targets()` 丢失。授权失效可见化本身仍保留——它是另一条真实故障路径，别再把它当作「下拉空 + 按钮哑」的首选解释。

**授权失效路径**：macOS 的 TCC 授权被撤销后，`osascript` 全部返回
`osascript is not allowed assistive access (-1719/-25211)`，几何写入静默失败。

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

**§2.2 早期把同一次故障归因于授权撤销，这个结论不成立**；上面 §2.2 已就地更正并保留证据链。

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
钳制/写入无效/缝保护/三栏无重叠）。当轮 `run_tests.sh` → `Ran 442 tests / OK`
（当前全套 466 例，见 §0 与 §2.7）。

**真机验证**：`python3 tests/verify_stack_portrait_real.py` 三段首尾相接、
无重叠、末段收在屏幕底（顶边内缩 31px 属菜单栏占位，判据已按「不重叠」
重写）。`tests/verify_portrait_zones_real.py` 把「App 自身尺寸钳制」单独
归类不计入几何偏差（真机 Marvis 最小 900 宽、系统设置 754，塞不进窄格是
App 限制不是程序不准），合计不准 0。

## 2.6 四格容量提示与 packer 行宽（2026-11-08 追加）

`quad_capacity()` 新增 `alt`：点名屏排不满时，实算别的屏能否排满（横屏优先），
界面直接报「换 Big 可排满」；没有合适的屏才退回「全排需 W×H」。容量文案不再
重复工作区尺寸。

`alt` 的候选屏只取横屏：自动选屏与 `_quad_place()` 都不会主动把四宫格窗口
送到竖屏，容量提示也不该给出一条与正式排布相反的建议。找不到可排满的横屏
时保持 `alt=None`，界面退回「需 W×H」或「最多排 N/M」。

这一行曾因容量文字过长把行尾「自动四窗」按钮挤出映射：带尺寸的旧文案 362px，
整行自然宽 1012px，而窗口最小尺寸下可视宽只有 765px。修法是屏下拉、网格、
按钮先 `pack`，容量标签最后 `pack(fill="x", expand=True)`；纯说明性的
「点格=填/清空 · 拖分割线=改比例」标签并进画布 tooltip。实测本机三组真实
选择文案 145~220px，容量标签可用 317px，全部完整可读，按钮 `mapped=True`。

## 2.7 四格「排完就废」的三处根因（2026-11-13，HEAD `4187716`）

接手的是一批**未提交的在制品**：2×2 功能（`snap_four_quad`）本身早已实现并提交，
但工作区里躺着三个让它「排完就废」的缺陷。三个都先写反向验证用例（红），再改（绿）。

### A. 排列处理器自己删掉刚写的记忆

`_snap_two_side_by_side` / `_snap_three_stack` / `_snap_four_quad` / `_fill_grid`
末尾各有一段「释放记录：让用户后续手动拖动不被触发器吸附回去」，对刚排的每个
窗口调 `release_window()`。但 `set_window_rect` 在写入时就同时写了**两份**记忆
（`push_undo` → `_undo_stack`，`remember_arranged` → `_last_arranged`），这段等于
排完立刻把两份都清掉：「撤销移动」和「回摆放位」对刚排的窗口一律失效。

理由本身也站不住：`_last_arranged` **没有任何自动消费方**（只有「回摆放位」
按钮读它），不存在「排完会被自动吸回去」这回事。所以整段删除，释放改由用户点
「释放窗口」显式触发（`ui.py:574`，上一提交 `0f34b47` 加的按钮）。
顺带删掉从未被任何地方调用的 `windows_mac.release_all_windows()` 及 `backend` 转发。

回归：`tests/test_arrange_keeps_memory.py`（4 例）。替身 snap **复刻真实副作用**
（两份记忆都写），再调真的 `release_window`，断言记忆仍在——不是数 mock 调用次数。

### B. 主窗口兜底覆盖用户点名的 `#序号`

`hwnd` 形如 `「应用名::窗口名#序号」`，序号是同名窗口里的 1-based z-order。
Electron 类 App 会把一个 84x77 的输入法面板放在 AX `window 1`，所以此前加了
`_ax_main_window()` 兜底（优先 `AXStandardWindow`，其次 `AXMain=true`）。
但兜底是**无条件**的：几何匹配偏差 > 64px 时，即使用户点的是 `#3`，也会被改写成
主窗口 → 排第 3 扇动的却是第 1 扇（UU远程 6 扇窗的场景直接踩中）。

修法：`_ax_window_by_geometry(app, rect, name=None, seq=None)` 与
`_ax_window_by_name(app, name, seq=None)` 增 `seq`；**带显式序号就禁用主窗口兜底**，
宁可回退按序号选，也不许换窗口。4 个调用点全部传 `seq`：`set_window_rect`、
`_readback_rect`、最小尺寸探测、`raise_window`。不带序号时兜底照旧生效。

回归：`tests/test_hwnd_seq.py::TestMainWindowFallback`（7 例，含「带序号不被劫持」
「不带序号仍享受兜底」「几何够准时仍优先几何」「AX 静默时返回 None」）。

### C. 一行塞 13 个按钮，packer 静默丢掉末尾三个

**这是最容易被误判成「功能没做」的一个**：Tk 的 packer 在容器放不下子控件时
**不报错、不映射末尾控件**。按钮对象存在、`command` 绑好、`btn.invoke()` 也能跑，
但界面上根本看不见——「释放窗口 / 暂存 / 取回」三个按钮就是这么消失的。
实测那一行自然宽 1005px，而窗口最小尺寸下可视宽只有约 775px。

修法：拆成三行（`ui.py:791`）——第一行 8 个（跨屏 3 + 半屏 4 + 最大化）、
第二行 8 个（居中 + 三分之一 3 + 四角 4）、第三行 5 个（撤销移动 / 回摆放位 /
释放窗口 / 暂存 / 取回）。`btns[:8]` 进第一行、`btns[8:]` 进第二行，第三行单独建。

回归：`tests/test_snap_rows_mapped.py`（3 例：21 个按钮都建出来了 / 最小宽度下
全部 `winfo_ismapped()` / 没有按钮超出画布右边缘）。**写这类测试有两个坑**：

- **不能 `withdraw()`**：窗口未映射时 packer 不受窗口宽度约束，所有按钮都「放得下」，
  映射与越界判定全部失真（本轮一度 21 个按钮全被误报越界）。
- **整个测试类只建一个 Tk root**（`setUpClass`）：macOS 的 Tk 在同一进程里反复
  建/销毁真实窗口会 SIGSEGV（退出码 139）。

### 真机验证（用户授权后做的，不是只跑测试）

重打包 → 重启 App → 走**出货路径**触发：`cmd_channel.send("snap_quad")`，由
`ui._poll_command`（`ui.py:4938`）在 Tk 主线程消费，转 `_snap_four_quad(force_auto=True)`。
注意 `CMD_FILE` 在 `tempfile.gettempdir()` 下，**必须用 App 真实的 TMPDIR**，
自己 export 一个临时目录会写到另一个文件、App 永远收不到。

排布前后用 `CGWindowListCopyWindowInfo` 读回几何对比。RV200 Pro（工作区
x=0 y=-1080 w=1920 h=1080）结果：ChatGPT `(0,-1049,960,660)`、
Qoder CN `(960,-1049,960,660)`、Tuanjie `(0,-389,1920,389)`——**0 实质重叠、0 越界**，
分割比例横向 50% / 竖向 63%。第 4 个候选 PommeToys（最小 540x760）被容量预检
挡在排布之外，而不是硬塞进去造成重叠——这正是 `quad_capacity()` 该有的行为。

### 2.8 全量单测挂死修复（2026-11-13 末，未提交）

**现象**：`unittest discover -p "test_*.py"` 全量会挂死——75s 后 SIGTERM / exit 137，
整套 CI 拿不到任何结果。根因用 `faulthandler.dump_traceback_later` 抓栈定位（非猜）：

- `ui.App.__init__` 构造期排 **7 个 `root.after`** 定时器
  （`_poll_taskbar` / `_poll_display` / `_poll_rehome_capture` / `_start_command_poll` /
  `_poll_rules` / `_slideshow_restore` / 600ms 的 `_toggle_hk(quiet=True)`）；
- `settings.json` 的 `hk_enabled=true` 让最后一个在 600ms 后注册 **21 组真机全局热键**并起 `CGEventTap` 线程；
- root 销毁时**没有取消这些 after** → 跨用例刷 `invalid command name`；
- 更致命：已映射（未 `withdraw`）的 Tk root 调 `root.update()` 在 macOS 上**永不返回**
  （栈停在 `tkinter/__init__.py:1314`），`test_snap_rows_mapped.py` 的 `setUpClass` 直接卡死 → 全量套件 SIGTERM。
  `update_idletasks()` 0.1s 返回且 packer 映射仍然有效，可替代。

**修法（生产 `ui.py` 零改动）**：

- 新增 `tests/ui_harness.py`：`build_app()` 在构造期**冻结** `root.after` / `after_cancel`
  （定时器只登记不排入 Tcl，并让 `settings.save` 空转），构造完恢复；
  `close_app()` 停后台对象 → `tk.splitlist(tk.call("after","info"))` 取消该解释器**全部** pending → `destroy`。
- 8 个界面测试改用该夹具；`test_snap_rows_mapped.py` 的 `update()` 换成 `update_idletasks()`。
- 新增 `run_tests_guarded.py` + `run_tests.sh` 接入看门狗：`signal.alarm(240，可 MMM_TEST_TIMEOUT 覆盖)`，
  超时打全线程栈 + 输出 `TEST_SUITE_TIMEOUT` 拒因 + `os._exit(124)`
  （**不能 `raise SystemExit`**——会被 unittest 当用例异常吞掉、退化成 rc=1，看门狗失效）。
- `--selfcheck` 用故意 sleep 的假套件反验看门狗 1s 内掐掉。

**三个坑（已踩过，复用价值高）**：

1. `tk.call("after","info")` 在 macOS Tk 返回**空格分隔字符串**，必须 `tk.splitlist`，
   直接 `for x in` 会按字符遍历 → 一条 pending 都取消不掉。
2. `raise SystemExit(124)` 被 unittest 框架吞成「用例错误」，rc 退化成 1，看门狗等于没装；
   必须用 `os._exit(124)`。
3. 夹具原名 `_ui_harness.py` 被项目 `.gitignore` 的 `_*.py` 忽略 → 改名 `ui_harness.py`
   （不匹配 `test_*.py`，所以不会被 discover 收集，专门给别的测试 import）。

**验证（全实测）**：

- 全量 `./run_tests.sh` → **508/508 OK（skipped=1，既有非 macOS skip）、89s、rc=0**（原挂死/exit 137）。
- 夹具回归 `tests/test_ui_harness.py` 10/10；看门狗 `--selfcheck` PASS（假套件 1s 内被 124 掐掉）。
- **mutant 8/8 全按指定用例变红**（定时器集合 / settings 写入 / after 取消 / 裸 App 构造 /
  update 回归 / close_app 接线 / 栈打印 / alarm），还原后基线仍绿——确认真绿非假绿。
- 副作用审计：全量日志 `invalid command name`=0、热键注册行=0；`settings.json` SHA256 不变；
  Dock 两个偏好键（`autohide-delay` / `autohide-time-modifier`）仍「不存在」→ **本轮没碰用户系统**。

**状态**：本节全部未提交，属 §0 所述 15 个未提交项的一部分。要跑全套现在用
`TD=$(mktemp -d); TMPDIR="$TD" bash run_tests.sh`（看门狗保护，不会再挂死）。

### 2.9 网格/并排容量预检 + 一键探测最小尺寸（2026-11-22，已提交）

- `grid_plan.capacity()`：按虚拟桌面与每格尺寸算「最多排几个、差多少像素、哪些窗口还没测过最小尺寸」。
- `ui._update_grid_capacity()`：换布局 / 改行列数 / 勾屏 / 排列完成后都会刷新容量行；
  容量标签**最后 pack 且 fill/expand**，否则长文案会把屏勾选框挤出可视区（老坑）。
- 「测最小尺寸」按钮（吸附第三行末尾）逐个缩小窗口读回再放回原位，让容量提示第一次就有依据。
- 并排/竖排的状态栏补 `_line_capacity_note` 差额提示。

### 2.10 macOS「上次 Python 意外退出」恢复弹窗（2026-11-22，已提交）

**现象**：反复弹
`The last time you opened Python, it unexpectedly quit while reopening windows. Do you want to try to reopen its windows again?`
而且**阻塞启动**——log 停在「四宫格缓存已读入」，界面要等有人点按钮才继续。

**根因**：本应用跑在 `Python.app/Contents/MacOS/Python` 上，进程归属 bundle
`org.python.python`。系统把**任何** python 进程的崩溃记到这个 bundle 头上
（实测崩溃报告 41 个历史 + 12 个新增，全由 Codex 启动的玻璃效果探针触发，
栈停在 pyobjc `id_to_python`，应用自身日志 0 错误）。只要记过一次崩溃，
下次启动就弹框；主进程 abort 被守护脚本拉起时再弹一次 → 「反复弹」。

**修法（全在 `build.py` 的 `_LAUNCHER`，不动应用代码）**：

1. `mmm_clear_python_restore_state()`：删掉
   `~/Library/Saved Application State/org.python.python.savedState`（弹窗触发源），
   并写三个偏好键 `NSQuitAlwaysKeepsWindows=false` / `NSDisableRelaunchOnReopen=true` /
   `ApplePersistenceIgnoreState=true`。
2. **每次**拉起 python 前都调一次——守护脚本重启链上每一步都不漏。
3. 写失败不再 `>/dev/null 2>&1` 吞掉，记进 `$TMPDIR/multimon-manager.log`（此前失败过但看不到）。
4. `--print-python` 分支跳过清理：单测跑它时不该改用户系统偏好。
5. `MMM_DEFAULTS_BIN` 可注入假 defaults，便于测试不碰真实系统。

**测试**：`tests/test_launcher_crash_dialog.py` 3 例（假 HOME + 假 defaults + 假 python）：
遗留 savedState 被删、三个键都写过、主进程 abort→重启链第二轮再清一次、
`--print-python` 不写偏好。全套 **583 例 OK（skipped=1）**。

**真机验证**：冷启动 5s 出界面（原要等 1 分钟）、无弹窗；手工造一个假 savedState 再重启，
日志出现 `[launcher] 已清理遗留的 Python 窗口恢复状态`，目录确实消失。

**注意**：`~/Library/Saved Application State/` 本身可能不存在，属正常；
`defaults domains` 里查不到 `org.python.python` 不代表没生效，那是 cfprefsd 缓存的域名列表。

## 3. 已知平台限制（不要「修」，已文档化）

`README.md:141` 记了一条：**窗口置顶切换是 Windows 独有**。macOS 的 `AXFloating` 写入被系统拒绝（-10006），SkyLight/CGS 接口从非 WindowServer 客户端调用无效。曾有人想「修好它」，结论是做不到，已在 README 明示不提供该按钮。

另：tkinter 的 `ttk::button` 不向 macOS 辅助功能暴露 `AXTitle`，所以 `osascript ... click button "竖排上中下"` 会失败（报 -1728 找不到）。**做 UI 自动化时不要走 AppleScript 点按钮**，用 `btn.invoke()`（在 Tk 实例内）或 CGEventPost 合成点击。

## 4. 未决 / 下一步

1. **竖屏真机脚本已修好**：`verify_stack_portrait_real.py` 与
   `verify_portrait_zones_real.py` 现在自动找 `height > width` 的屏，找不到就
   SKIP，不再写死「小米屏」。两者是独立脚本（入口有 `if __name__` 保护），
   `run_tests.sh` 的 discover 不会加载，换屏后想验几何要手动跑：

```bash
python3 tests/verify_stack_portrait_real.py
python3 tests/verify_portrait_zones_real.py
```
2. **真人鼠标点击仍未闭环**。2026-11-13 这轮走的是托盘命令通道（`snap_quad`）真机排布并读回几何，证明了「按钮背后的逻辑 + 出货路径」是通的；更早一轮用 `btn.invoke()` 验过绑定。**「用户双击桌面快捷方式后亲手点按钮」这一环仍待真人确认**（CGEventPost 合成点击送不到 App，macOS 限制）。
3. **四窗 2×2 已存在**，叫 `snap_four_quad`（`windows_mac.py:3420`），不是 `snap_four_grid`。带容量预检（`quad_capacity()`）、最小尺寸缓存（`quad_cache.json`）、智能分配（按宽高比重排四角）、排列预演（`quad_predict`）。改四宫格行为前先读这几个函数，别新写一套数学。
4. **未提交项比上一版多了一层**：`4187716` 这个提交仍在本地 `master`、用户明确选择先不推送；
   **此外工作区还压着 15 个未提交项**（`power_guard.py` + `tests/test_power_guard.py` +
   `tests/ui_harness.py` + `tests/test_ui_harness.py` + `run_tests_guarded.py` +
   8 个界面测试改用夹具的改动 + 本节 §2.8 的看门狗接线）。要推/要 commit 之前先问用户，
   别自作主张 `git push` 或 `git commit -A`。
5. **改吸附按钮那一行时先看 §2.7 C**：加按钮很容易再次把行宽顶爆，而 packer 是静默丢控件。加完跑 `tests/test_snap_rows_mapped.py`，它会在最小窗口宽度下逐个查映射。

## 5. 快速命令

```bash
cd "/Users/a1-6/AI Shared/repo/multimon-manager"

# 跑测试（隔离 TMPDIR，不用停正在运行的 App）
TD=$(mktemp -d); TMPDIR="$TD" bash run_tests.sh

# 只有要验单例行为本身时才清场（见 §1.1，注意持锁的可能是 Python 子进程）
pkill -9 -f "多屏管理器"; sleep 1
lsof $TMPDIR/multimon_manager.lock

# 重新打包 + 启动
python3 build.py && open ~/Desktop/多屏管理器

# 看运行时日志（排障第一站）
tail -f /private/var/folders/rz/y1d6t55x5y90zd4q09s_9x000000gn/T/multimon-manager.log

# 枚举显示器 / 窗口（调试几何）
python3 -c "import monitors_mac as m; [print(i,x.device_name,x.work_rect) for i,x in enumerate(m.enum_monitors())]"
python3 -c "import windows_mac as w; [print(x['hwnd'],x['w'],'x',x['h']) for x in w.list_target_windows()]"
```
