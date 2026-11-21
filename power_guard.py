# -*- coding: utf-8 -*-
"""省电守护 + Dock 弹出速度（macOS）。

从 PommeToys 借鉴的两点，落到「多屏管理器」而不是 RingDeck / Smoose：

1. **Dock 速度**：macOS 没有公开的 Dock 动画 API，但可以调 `com.apple.dock`
   的 `autohide-delay`（悬停到弹出的延迟，秒）与 `autohide-time-modifier`
   （动画时长倍率）。改的是**系统全局偏好**，所以本模块：
   - 只在用户点「应用」时写，**默认绝不自动写**；
   - 写之前先读原值并生成备份，键原本不存在就记 `None`，恢复时 `defaults delete`；
   - 读原值失败（超时/异常）直接拒绝应用，避免改了却还原不回去；
   - 提供 `restore()` 精确还原，退出程序**不自动还原**（与 dock_autohide_mac.py
     的既有约定一致：关掉本程序后 Dock 偏好应保持用户的选择）。

2. **过热 / 低电量收手**：电池 ≤ 阈值且未接电，或热状态达到 serious(2)，
   就暂停壁纸轮换并降低 1.5s 窗口轮询频率。恢复供电/降温后自动继续。
   - 电量读 `pmset -g batt`；热状态用 ctypes 调 `NSProcessInfo.thermalState`
     （纯标准库，不引入 PyObjC，符合本项目零第三方依赖硬约束）。
   - **读不到绝不谎报正常**：未知项单列在 `unknown` 里、`degraded=True`，
     界面显示「状态未知」，但不会因此误停用户功能（fail-safe，不是 fail-open）。
   - 不动防休眠档位：那是用户主动开的，不该被省电逻辑悄悄降级。

纯规则（`evaluate` / `validate_dock_speed` / `build_*_plan`）不碰系统、不碰
磁盘，可在 Windows / CI 上完整测试；采集与执行层全部走可注入的 runner。
"""
import ctypes
import re
import subprocess
import sys
from typing import NamedTuple, Optional

IS_MAC = (sys.platform == "darwin")

DOMAIN = "com.apple.dock"
DELAY_KEY = "autohide-delay"
SPEED_KEY = "autohide-time-modifier"
DOCK_KEYS = (DELAY_KEY, SPEED_KEY)

# preset -> (delay_seconds, time_modifier)。None 表示「恢复系统默认」。
DOCK_PRESETS = {
    "default": None,
    "fast": (0.0, 0.5),
    "instant": (0.0, 0.15),
}
DOCK_PRESET_LABELS = {
    "default": "系统默认",
    "fast": "较快（无延迟 · 半速动画）",
    "instant": "极快（无延迟 · 0.15× 动画）",
}
DEFAULT_PRESET = "default"

# NSProcessInfoThermalState: 0 nominal / 1 fair / 2 serious / 3 critical
THERMAL_SERIOUS = 2
DEFAULT_BATTERY_THRESHOLD = 20
READ_TIMEOUT = 3
WRITE_TIMEOUT = 5


# ── 纯规则：省电守护 ──────────────────────────────────────────────────────

class PowerSnapshot(NamedTuple):
    battery_percent: Optional[int]
    on_ac_power: Optional[bool]
    thermal_state: Optional[int]


class GuardDecision(NamedTuple):
    throttle: bool
    reasons: tuple          # "lowBattery" / "thermalSerious"
    unknown: tuple          # "battery" / "power_source" / "thermal"
    degraded: bool          # 有任何未知数据 -> 不能声称「一切正常」

    def summary(self) -> str:
        labels = {"lowBattery": "低电量且未接电", "thermalSerious": "设备过热"}
        if self.reasons:
            text = "省电守护：" + "、".join(labels.get(r, r) for r in self.reasons) + "，已收手"
        elif self.degraded:
            text = "省电守护：状态未知（读取失败），未收手、也未判定正常"
        else:
            text = "省电守护：正常"
        return text + ("；未知项 " + "/".join(self.unknown) if self.unknown else "")


def evaluate(snapshot: PowerSnapshot,
             battery_threshold: int = DEFAULT_BATTERY_THRESHOLD,
             thermal_threshold: int = THERMAL_SERIOUS) -> GuardDecision:
    """只做判断，不采集、不执行。未知数据既不当成正常，也不触发收手。"""
    unknown = []
    reasons = []

    battery = snapshot.battery_percent
    if battery is None or not isinstance(battery, int) or not (0 <= battery <= 100):
        unknown.append("battery")
        battery = None
    if snapshot.on_ac_power is None:
        unknown.append("power_source")
    thermal = snapshot.thermal_state
    if thermal is None or not isinstance(thermal, int) or not (0 <= thermal <= 3):
        unknown.append("thermal")
        thermal = None

    # 接电时不因电量低收手；供电状态未知也不收手（避免误停用户功能）。
    if (battery is not None and snapshot.on_ac_power is False
            and battery <= battery_threshold):
        reasons.append("lowBattery")
    if thermal is not None and thermal >= thermal_threshold:
        reasons.append("thermalSerious")

    return GuardDecision(throttle=bool(reasons), reasons=tuple(reasons),
                         unknown=tuple(unknown), degraded=bool(unknown))


# ── 纯规则：Dock 速度 ─────────────────────────────────────────────────────

def validate_dock_speed(delay, modifier):
    """返回 (ok, reason)。reason 是具体拒因，不是笼统的「参数错误」。"""
    try:
        d = float(delay)
        m = float(modifier)
    except (TypeError, ValueError):
        return False, "nonNumeric"
    if d < 0 or d > 5:
        return False, "delayOutOfRange"
    if m < 0.05 or m > 2.0:
        return False, "modifierOutOfRange"
    return True, "ok"


def parse_dock_value(text):
    """`defaults read` 的输出转成 float；读不到/非法返回 None。"""
    try:
        value = float((text or "").strip())
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def build_apply_plan(delay, modifier, current):
    """生成 (backup, commands)。backup 里 None = 该键原本不存在。"""
    ok, reason = validate_dock_speed(delay, modifier)
    if not ok:
        raise ValueError(reason)
    backup = {key: current.get(key) for key in DOCK_KEYS}
    commands = [
        ["defaults", "write", DOMAIN, DELAY_KEY, "-float", str(float(delay))],
        ["defaults", "write", DOMAIN, SPEED_KEY, "-float", str(float(modifier))],
    ]
    return backup, commands


def build_restore_plan(backup):
    """按备份精确还原：原本不存在的键用 delete，有值的写回原值。"""
    commands = []
    for key in DOCK_KEYS:
        original = backup.get(key)
        if original is None:
            commands.append(["defaults", "delete", DOMAIN, key])
        else:
            commands.append(["defaults", "write", DOMAIN, key,
                             "-float", str(float(original))])
    return commands


# ── 采集层 ────────────────────────────────────────────────────────────────

def parse_battery(text):
    """解析 `pmset -g batt`，返回 (percent, on_ac_power)；失败给 None。"""
    match = re.search(r"(\d{1,3})\s*%", text or "")
    percent = int(match.group(1)) if match else None
    if percent is not None and not (0 <= percent <= 100):
        percent = None
    on_ac = None
    if "AC Power" in text or "AC attached" in text:
        on_ac = True
    elif "Battery Power" in text or "discharging" in text:
        on_ac = False
    return percent, on_ac


def _default_runner(argv, timeout):
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)
    return result.returncode, result.stdout or "", result.stderr or ""


def read_thermal_state():
    """ctypes -> [NSProcessInfo processInfo].thermalState (0..3)，失败返回 None。"""
    if not IS_MAC:
        return None
    try:
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        send_id = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_void_p)(("objc_msgSend", objc))
        send_long = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                     ctypes.c_void_p)(("objc_msgSend", objc))
        cls = objc.objc_getClass(b"NSProcessInfo")
        info = send_id(cls, objc.sel_registerName(b"processInfo"))
        if not info:
            return None
        value = send_long(info, objc.sel_registerName(b"thermalState"))
        return value if 0 <= value <= 3 else None
    except Exception:  # noqa: BLE001 - 采集失败必须退化成「未知」，不能抛到 UI
        return None


def collect_snapshot(runner=None):
    """只读取系统状态。runner 可注入，便于测试完全不碰真实系统。"""
    run = runner or _default_runner
    if not IS_MAC:
        return PowerSnapshot(None, None, None)
    rc, out, _err = run(["pmset", "-g", "batt"], READ_TIMEOUT)
    percent, on_ac = parse_battery(out) if rc == 0 else (None, None)
    return PowerSnapshot(percent, on_ac, read_thermal_state())


def read_dock_values(runner=None):
    """返回 {key: float|None}；None 表示键不存在。读取异常时抛 RuntimeError。"""
    run = runner or _default_runner
    values = {}
    for key in DOCK_KEYS:
        rc, out, err = run(["defaults", "read", DOMAIN, key], READ_TIMEOUT)
        if rc == -1:
            raise RuntimeError(f"读取 {key} 失败：{err[:120]}")
        values[key] = parse_dock_value(out) if rc == 0 else None
    return values


# ── 执行层（只有显式调用才会改系统） ──────────────────────────────────────

def _run_commands(commands, runner):
    for argv in commands:
        rc, _out, err = runner(argv, WRITE_TIMEOUT)
        if rc not in (0,):
            return False, f"{' '.join(argv)} 失败：{(err or '').strip()[:120]}"
    return True, "ok"


def _restart_dock(runner):
    rc, _out, err = runner(["killall", "Dock"], WRITE_TIMEOUT)
    # Dock 重启失败不致命：偏好已写入，下次登录也会生效，但必须如实报告。
    return (True, "ok") if rc == 0 else (False, f"Dock 未重启：{(err or '').strip()[:120]}")


def apply_preset(name, runner=None):
    """返回 (ok, detail, backup)。**调用方负责持久化 backup**。"""
    if not IS_MAC:
        return False, "当前平台不支持 macOS Dock 设置", None
    if name not in DOCK_PRESETS:
        return False, f"未知预设：{name}", None
    preset = DOCK_PRESETS[name]
    if preset is None:
        return False, "系统默认档请用 restore(backup) 还原", None
    run = runner or _default_runner
    try:
        current = read_dock_values(run)
    except RuntimeError as exc:
        # 关键护栏：读不到原值就不能改，否则永远还原不回去。
        return False, f"拒绝应用（无法备份原值）：{exc}", None
    backup, commands = build_apply_plan(preset[0], preset[1], current)
    ok, detail = _run_commands(commands, run)
    if not ok:
        return False, detail, backup
    dock_ok, dock_detail = _restart_dock(run)
    if not dock_ok:
        return True, f"偏好已写入，但 {dock_detail}（下次登录生效）", backup
    return True, "已应用并重启 Dock", backup


def restore(backup, runner=None):
    """按备份还原。返回 (ok, detail)。"""
    if not IS_MAC:
        return False, "当前平台不支持 macOS Dock 设置"
    if not isinstance(backup, dict):
        return False, "拒绝还原：没有有效的原值备份"
    run = runner or _default_runner
    ok, detail = _run_commands(build_restore_plan(backup), run)
    if not ok:
        return False, detail
    dock_ok, dock_detail = _restart_dock(run)
    if not dock_ok:
        return True, f"偏好已还原，但 {dock_detail}（下次登录生效）"
    return True, "已还原并重启 Dock"
