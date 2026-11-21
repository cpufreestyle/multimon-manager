# 开源窗口管理器拆解笔记（Rectangle / Amethyst / yabai）

> 目的：把成熟实现里**可验证的算法与工程取舍**抽出来，对照本项目
> `multimon-manager` 的现状，给出可落地的改法与优先级。
> 只读公开源码，不涉及任何闭源二进制逆向；这些项目的许可都允许阅读源码
> 并借鉴实现思路（Rectangle MIT、Amethyst MIT、yabai MIT）。

源码位置（本机临时副本）：`/tmp/wmstudy/{Rectangle,yabai,Amethyst}`

| 项目 | 语言 | 规模 | 与本项目的关系 |
|---|---|---|---|
| Rectangle | Swift | ~8.6 MB | 单窗吸附 + 多窗批排（GridTiling），最贴近本项目 |
| Amethyst | Swift | ~5.5 MB | 常驻平铺（Tall / ThreeColumn / BSP），布局比例模型值得抄 |
| yabai | C | ~4.7 MB | 网格坐标算法与 padding/gap 语义，最严谨的取整处理 |
| Loop | Swift | 克隆未取到源码 | 径向菜单交互，暂不涉及 |

---

## 一、Rectangle：一次「移动窗口」被拆成四个可替换的 Mover

`Rectangle/WindowMover/` 里没有一个大函数，而是按「窗口答不答应」分流：

| Mover | 什么时候用 | 关键动作 |
|---|---|---|
| `StandardWindowMover` | 普通可缩放窗口 | 计算**先改尺寸还是先挪位置**，再写一次 |
| `BestEffortWindowMover` | 写完仍越界（App 最小尺寸顶大） | 只平移，把整窗推回屏内 |
| `FixedSizeWindowMover` | 窗口尺寸完全锁死 | 按「原窗口与屏幕共享哪条边」对齐，否则居中 |
| `EdgeAlignmentWindowMover` | 尺寸被 App 部分钳制 | 写完再按共享边微调一次位置 |

### 1.1 先 resize 还是先 move：`ImmediateFrameOrder.sizeFirst`

```swift
// StandardWindowMover.swift
static func sizeFirst(from current: CGRect, to target: CGRect, bounds: CGRect,
                      acrossScreens: Bool, preferSizeFirst: Bool) -> Bool {
    // 跨屏、或当前窗口已经在屏外 -> 必须先改尺寸
    if acrossScreens || !bounds.insetBy(dx: -1, dy: -1).contains(current) { return true }
    func overflow(_ frame: CGRect) -> CGFloat { /* 四边越界像素之和 */ }
    let resized = overflow(CGRect(origin: current.origin, size: target.size))
    let moved   = overflow(CGRect(origin: target.origin, size: current.size))
    if abs(resized - moved) > 1 { return resized < moved }
    return preferSizeFirst
}
```

注释写得很直白：**先把已经在屏外的窗口挪过去，系统会拿它去撞 Dock 并顺手改尺寸**。
所以写入顺序不是风格问题，而是「会不会被系统偷偷改掉」的问题。

本项目的现状：`windows_mac.set_window_rect` 在同一个 AppleScript 里
`set position` 然后 `set size`，顺序固定。对跨屏 / 屏外窗口没有专门分支。

### 1.2 被钳制后按「共享边」对齐：`ClampedWindowAligner`

```swift
static let alignmentTolerance: CGFloat = 1.0   // 差 1px 不算被钳制

if abs(window.width - zone.width) > tolerance {
    if sharedEdges.contains(.left), !sharedEdges.contains(.right) {
        result.origin.x = zone.minX
    } else if sharedEdges.contains(.right), !sharedEdges.contains(.left) {
        result.origin.x = zone.maxX - window.width
    } else {
        result.origin.x = round((zone.width - window.width) / 2.0) + zone.minX
    }
}
```

三个细节值得抄：

1. **1px 容差**。注释解释了原因：macOS 把窗口放大到 Dock 边时会少给 1pt，
   这不是「被钳制」。没有容差就会把窗口从贴边挪到居中，把看不见的缝
   挪到看得见的那一侧。
2. **共享边来自几何**（初始窗口矩形 vs 屏幕可见框），不是来自「用户点了
   左半屏」这种语义。同一个方位在不同初始位置下，贴的边并不一样。
3. **两边都不共享就居中**，而不是硬贴左上。浮在屏幕中间的固定尺寸窗口
   被要求「左半屏」时，硬贴左上会显得像 bug。

本项目的现状：`screen_math.reanchor_edge(zone, ...)` 用**方位名**决定贴哪条边
（`RIGHT_ZONES` / `BOTTOM_ZONES`）。对本项目自己的 14 个方位是对的，但
四格 / 网格的格子是几何切出来的，没有方位名可用。

### 1.3 越界兜底：`WindowFrameBounds.constrained`

```swift
if result.maxY > screenFrame.maxY { result.origin.y = screenFrame.maxY - result.height }
else if result.minY < screenFrame.minY { result.origin.y = screenFrame.minY + gap }
```

「底边优先」与 Rectangle 的坐标翻转有关；本项目 `screen_math.clamp_to_work`
已经是「保尺寸、左/顶优先、右/底兜底」，语义等价，无需改。

---

## 二、Rectangle：多窗网格 `GridTiling` 的两个算法

`Rectangle/MultiWindow/GridTiling.swift`（275 行）是整个仓库里最值得读的文件。

### 2.1 `balancedBandLengths`：带最小/最大尺寸的等分

问题：6 个窗口分 3 条带，每条带等宽 = W/3；但某条带里有个窗口最小宽 900，
W/3 只有 600。怎么分？

```swift
// 二分找「每条带至少能拿到的基准长度」middle
var low = 0, high = totalPixels
while low < high {
    let middle = low + (high - low + 1) / 2
    let required = zip(lower, upper).reduce(0) { $0 + min($1.1, max($1.0, middle)) }
    if required <= totalPixels { low = middle } else { high = middle - 1 }
}
var lengths = zip(lower, upper).map { min($0.1, max($0.0, low)) }
// 余数补给还低于上限的带，每次 +1px
```

性质：

- 求和恰好等于总长（不会因为取整少 1px 或多 1px）；
- 每条带都在自己的 `[min, max]` 内，除非整体不可行；
- 返回 `feasible` 布尔值——不可行时退回**全等分**并让调用方如实报告，
  而不是硬给出一个越界的解。

本项目的现状：`quad_plan.grid_geometry` / `windows_mac._quad_cells` 用
「按比例切 -> 被最小尺寸顶到 -> 从另一侧让出 -> 两边都被顶住才允许重叠」。
方向是对的，但：

- 余数在两侧之间手工分配，没有统一的可行性判据；
- 三条以上（网格 m×n）时没有等价物，只按比例切；
- 没有 `feasible` 返回，界面只能事后从读回偏差去猜。

### 2.2 `apply`：把 App 的真实答复当成约束，做有界不动点

```swift
for _ in 0..<(4 * constraints.count + 1) {          // 重试上限与窗口数线性
    var allocation = layout(bounds:..., constraints: constraints)
    for index in constraints.indices {
        if lastRequestedFrames[index] != target && achievedFrames[index] != target {
            let achieved = setFrame(index, target)   // 真正写窗口
            // 用「实际得到的尺寸」收紧该窗口的 [min,max]
            revised.width = revised.width.observing(achieved: achieved.width,
                                                    requested: target.width)
            // 只有当新约束仍然可行时才接受，否则标记不可行并停止
            if candidate.feasible { constraints = candidateConstraints }
            else { infeasibleConstraint = true }
        }
    }
    if !allocation.feasible || !learnedConstraint { break }
}
```

三个关键点：

1. **只在目标变了才写**（`lastRequestedFrames[index] != target`），避免对同一个
   窗口反复写同一组坐标——AX 写入是异步动画，重复写会读到中间帧。
2. **新约束先验证再采纳**。学到了一个会让整体不可行的约束就停止，不会把
   其它窗口挤坏。
3. **上限与窗口数成正比**（`4n+1`），既保证收敛，又不会在大窗口数下卡死。

本项目的现状：`windows_mac.snap_four_quad` 里有「二趟重排 + 最多
`_QUAD_MAX_PASSES` 遍」，思路一致但没有「先验证可行性再采纳约束」这一步。

---

## 三、Rectangle：Dock 与菜单栏的工作区纠偏（本轮已落地）

`Rectangle/General/ScreenDetection.swift` 的 `DockUtil`（约 200 行）解决的正是
本项目反复踩到的那一类问题：**`NSScreen.visibleFrame` 会过期**。

### 3.1 自动隐藏时要「收回」而不是「不处理」

```swift
if dockAutoHideEnabled {
    // AppKit 可能故意保留一条用来唤出 Dock 的窄边距
    return reclaimDockEdges(from: visibleFrame, screenFrame: screenFrame,
                            preservingSmallInsets: true)
}
```

```swift
let preservedLeftInset   = preservingSmallInsets && leftInset   <= minimumReliableInset ? leftInset   : 0
let preservedRightInset  = preservingSmallInsets && rightInset  <= minimumReliableInset ? rightInset  : 0
let preservedBottomInset = preservingSmallInsets && bottomInset <= minimumReliableInset ? bottomInset : 0
// Dock 不可能占用顶边，顶部保留 AppKit 给的菜单栏/刘海边界
```

`minimumReliableInset = 8`。语义：

- 小于 8pt 的边距**保留**——那是 macOS 留给「鼠标触边唤出 Dock」的感应条；
- 大于 8pt 的边距**收回**——Dock 已经隐藏了，那是过期值。

本项目的现状（改前）：`monitors_mac._apply_dock_avoidance` 在 `autohide is True`
时直接 `return`，保留了可能过期的 `visibleFrame`。过期时窗口与屏幕边之间会留
一条永远填不上的缝；用户看到的是「排完没铺满」。

### 3.2 固定 Dock：宁可相信 AppKit，只用 AX 补「缺失」

```swift
let reportedInset = reportedInset(for: visibleFrame, screenFrame: screenFrame, edge: edge)
// AX list 的 bounds 描述的是 Dock 图标块，不是它精确预留的区域。
// 保留一个合理的 AppKit 边距，只在它缺失时才用 AX 补。
let correctedInset = reportedInset > minimumReliableInset ? reportedInset : dockInset
```

还有两道保险：

```swift
guard dockInset > 0.5, dockInset <= maximumInsetRatio * dimension else { return visibleFrame }
```

- 面板与屏幕的交集必须 >= 面板自身面积的 50%，且**不能与第二块屏打平**——
  否则那多半是 Dock 的右键菜单/图标预览，不是 Dock 本体；
- Dock 不可能占掉一条边的三分之一以上。

本项目的现状（改前）：`monitors_mac._apply_dock_avoidance` 只用「面板与完整屏的
最大交集」挑归属屏，没有 50% / 唯一性 / 33% 这三道保险；取整一律以 AX 面板为准
再减 `DOCK_SAFETY_PT = 6`。

---

## 四、Amethyst：布局是「Pane 模型 + 比例」，不是「一堆坐标」

`Amethyst/Layout/Layouts/ThreeColumnLayout.swift` 用一个 `TriplePaneArrangement`
描述三列：

```swift
// 主区占 ratio（默认 0.5），另外两列平分剩余
let mainWindowWidth = secondaryPaneCount == 0 ? screenWidth : round(screenWidth * mainPaneRatio)
let nonMainWindowWidth = round((screenWidth - mainWindowWidth) / 2)
// 每列内按窗口数纵向等分，高度各自 round 到整像素
paneWindowHeight = [.main: round(screenHeight / CGFloat(mainPaneCount)), ...]
```

两个细节：

- **只有主区提供比例**，次区永远是「剩余的一半」。用户调比例时只有一个
  旋钮，不会出现「三列加起来不等于 100%」。
- `mainPaneCount` 与 `mainPaneRatio` 分开存：主区放几个窗口、主区占多宽，
  是两个独立的用户意图。

`Amethyst/Layout/ReflowOperation.swift` 的 `ResizeRules` 更进一步：记录
「这一次缩放允许哪个轴变化、scaleFactor 是多少」，把用户拖拽边界的结果
反算回布局比例，而不是记住绝对像素。所以插拔屏幕 / 增删窗口后比例仍在。

本项目的现状：`grid_plan.LAYOUTS` 已经用「列比例 + 行比例」描述布局，方向一致；
但比例是**预设常量**，没有从一次真实拖拽/排布里反算并持久化的闭环
（`layouts.py` 存的是绝对几何 + 相对框，不是网格比例）。

---

## 五、yabai：格子坐标从「远边锚定」，误差不累积

`yabai/src/window_manager.c` 的 `window_manager_apply_grid`：

```c
float cw = bounds.size.width / c;
float ch = bounds.size.height / r;
float fx = bounds.origin.x + bounds.size.width  - cw * (c - x);
float fy = bounds.origin.y + bounds.size.height - ch * (r - y);
```

关键在最后两行：每个格子的起点是**从右/下边界倒推**（`总宽 - 单元宽 × 剩余列数`），
而不是 `origin + 前面所有格子宽度的累加`。浮点乘法没有累加误差，
所以最后一列一定精确贴住边界，不会出现「最后一列少 1px」。

gap 的语义也很干净：

```c
if (x > 0)      { bounds.origin.x += gap; bounds.size.width -= gap; }  // 左边不是外缘才让
if (c > x+w)    bounds.size.width -= gap;                              // 右边不是外缘才让
```

即「只有内部边才留缝」，与本项目 `grid_plan.cell_gap_rect` 的判据完全一致——
这一条本项目已经做对了，可以作为交叉验证。

本项目的现状：`grid_plan.grid_cells` 用浮点累加器 `acc` 求切点
（`round(vw * acc)`），最后一列/行用减法补齐。因为累加的是**比例浮点数**
而不是已经取整的像素，误差不会逐列累积，末列也一定精确贴边——这一点比
「每列独立 round(vw/n)」更稳，不需要改。

yabai 的倒推写法真正有价值的地方是**跨列格子**（`w > 1`）：它的宽度是
`cw * w`，起点由远边倒推，所以「跨 2 列」的格子不会因为前面某列的取整
而少 1px。本项目目前没有跨列格子（每格恰好 1 列 × 1 行），所以这条只在
将来做「合并格子」时才需要。

---

## 六、落到本项目：按性价比排序的改动清单

| 优先级 | 改动 | 目标文件 | 验收口径 |
|---|---|---|---|
| P0（本轮已做） | Dock 自动隐藏时收回过期边距；面板归属加 50% / 唯一 / 33% 三道保险 | `monitors_mac.py` | `tests/test_dock_avoidance.py` 新增 4 例 |
| P2 | 若将来支持「跨列/跨行合并格子」，切点改用 yabai 式「从远边倒推」 | `grid_plan.grid_cells` | 跨列格子的宽度不因前序取整少 1px |
| P1（本轮已做） | `balancedBandLengths` 已移植为 `grid_plan.balanced_band_lengths()`（返回 `(lengths, feasible)`，和恰好等于总长）；`bands_shortfall()` 接进四格容量与网格批排，缺口按「宽差/高差像素」报出 | `grid_plan.py` / `windows_mac.py` / `ui.py` | `tests/test_grid_plan.py` 12 例 + `test_quad_capacity` 3 例 + `test_snap_four_quad` 缺口断言 |
| P1（本轮已做） | 网格切格接入加权带长：某列/排的下限超过比例份额时，撑宽那一列再按比例分剩余（`grid_plan.balanced_grid_cells`），且只在**确实多排到一个窗口**时采用 | `grid_plan.py` / `windows_mac.py` | `test_snap_four_quad::test_min_size_widens_its_column` |
| P1（下一步） | 让四格 `_quad_cells` 的 cap 分支也走同一个带长算法（需要改调用契约，单独一轮 + 真机验证） | `windows_mac.py` | 同一次排布的「预算 / 实排 / 报告」用同一份长度 |
| P2 | `screen_math.shared_edges()` + `align_clamped()`：按初始几何决定贴边，1px 容差，两边都不贴则居中 | `screen_math.py` | 纯函数，独立单测 |
| P2 | `set_window_rect` 增加「尺寸优先 / 位置优先」判定，跨屏或已越界时先 resize | `windows_mac.py` | 真机跨屏吸附不再被系统改尺寸 |
| P2（本轮已做） | 「目标已是当前几何就不写」：`apply` 里 `lastRequestedFrames != target` 那条判据的移植，2px 容差 | `windows_mac.set_window_rect` | `test_set_window_rect_idempotent` 新增 3 例 |
| P2（本轮已做） | 网格容量预检：`grid_capacity()` 与 `fill_grid` 共用 `_grid_reslice` 判定，界面在点之前显示「N/M 可排 · 差多少」 | `windows_mac.py` / `ui.py` | `tests/test_grid_capacity.py` 11 例 |
| P2（本轮已做） | 并排/竖排容量预检 `line_capacity()`：排完在状态栏补一句「最小高合计 1812px > 屏 870px」 | `windows_mac.py` / `ui.py` | `tests/test_line_capacity.py` 10 例 |
| P2（本轮已做） | 「测最小尺寸」按钮 + `probe_min_sizes()`：缩→读回→复位→写盘，让容量提示首次使用就有依据 | `windows_mac.py` / `ui.py` | `tests/test_probe_min_sizes.py` 8 例 |
| P1（真机发现） | 全屏窗口写不动（AX 返回成功但几何不变）：并排/竖排/单窗吸附/网格统一先 `_prepare_for_layout` 退出全屏 | `windows_mac.py` | `tests/test_layout_prepare.py` 5 例 + 真机 ChatGPT 全屏复现 |
| P1（真机发现） | 三段按最小高度分配，并先扣掉顶边不可达条带（菜单栏 31px），否则顶段必然压住中段 | `windows_mac.py` | `tests/verify_stack_portrait_real.py`：zenclean 752/520/496 → 753/568/568，无重叠 |
| P1（本轮已做） | 并排/上下堆叠同样按最小尺寸分配（`_two_band_lengths`），竖屏分支扣掉顶边条带 | `windows_mac.py` | `tests/verify_two_side_real.py`：A 竖屏上下 / B 横屏左右 均 OK |
| P1（真机发现） | 四格两趟后仍实测重叠时，剔除「最小面积最大」的一扇、复原并重排其余（不再只写一条「没能排开」） | `windows_mac.py` | `tests/verify_quad_real.py`：Messages 最小高实测 502>探测 320，被剔除后 2/4 无重叠 |
| P1（本轮已做） | 网格批排同款兜底：写入后仍重叠就剔除「最小面积最大」的一扇并复原，宁可空一格 | `windows_mac.py` | `test_snap_four_quad::test_overlap_after_write_drops_largest` |
| P3 | 网格比例从一次真实排布反算并持久化（Amethyst `ResizeRules` 思路） | `grid_plan.py` / `layouts.py` | 插拔屏幕后比例仍在 |

本轮只落地 P0：它是唯一一条**能立刻复现、能纯函数验证、且不会改变现有
排列语义**的改动。P1 的两条算法都需要改 `_quad_cells` 的调用契约，留到
单独一轮做，避免和当前未提交的省电守护改动混在一起。

> 后续进展：P1 的网格切格与 P2 的「不重复写窗口」「网格容量预检」已完成，
> 细节见上表。真机对照脚本在 `tests/verify_grid_min_size_real.py`（会把
> 两个窗口摆进 2×1 网格、量完还原），用来确认新路径在真实 App 上确实撑宽了
> 那一列。

---

## 七、许可与引用

- Rectangle：MIT（仓库根 `LICENSE`），可自由借鉴实现；本文引用的是公开源码片段。
- Amethyst：MIT。
- yabai：MIT。
- 本文不包含任何闭源二进制的反编译结果；Moom / Rectangle Pro 等商业版
  的行为只在早前会话里以黑盒方式对比过，不在本文范围。
