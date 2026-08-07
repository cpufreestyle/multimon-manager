"""显示器枚举（macOS 实现，零第三方依赖）。

使用系统自带的 `system_profiler` 命令获取显示器信息。
macOS 坐标系原点在左下角，且与图像坐标 y 轴相反；这里统一转换为
左上角为原点的像素坐标系（与 Windows 端保持一致），方便上层逻辑复用。
"""
import json
import subprocess

try:
    from monitors import MonitorInfo
except ImportError:  # 独立运行
    from dataclasses import dataclass

    @dataclass
    class MonitorInfo:
        index: int = 0
        device_name: str = ""
        device_path: str = ""
        is_primary: bool = False
        left: int = 0
        top: int = 0
        width: int = 0
        height: int = 0
        work_left: int = 0
        work_top: int = 0
        work_width: int = 0
        work_height: int = 0

        @property
        def rect(self):
            return (self.left, self.top, self.width, self.height)

        @property
        def work_rect(self):
            return (self.work_left, self.work_top, self.work_width, self.work_height)


def _run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return out.stdout
    except Exception:  # noqa: BLE001
        return ""


def enum_monitors():
    """枚举所有显示器，返回 MonitorInfo 列表（按系统排列顺序）。"""
    raw = _run(["system_profiler", "SPDisplaysDataType", "-json"])
    monitors = []
    if not raw:
        return monitors
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return monitors

    displays = data.get("SPDisplaysDataType", [])
    # 合并所有 displays 条目里的 _displays 数组
    all_disp = []
    for d in displays:
        for sub in d.get("_displays", []):
            all_disp.append(sub)
    if not all_disp:  # 某些版本结构不同
        all_disp = displays

    # 用 system_profiler 的 resolution 与 position 重建布局
    # 计算全局包围盒以确定 y 翻转
    parsed = []
    max_y = 0
    for i, d in enumerate(all_disp):
        res = d.get("spdisplays_resolution", d.get("_resolution", ""))
        # 例: "2560 x 1440 @ 60.00Hz"
        w = h = 0
        try:
            dims = res.split("@")[0].strip()
            parts = dims.lower().replace("x", " ").split()
            nums = [int(float(p)) for p in parts if p.replace(".", "").isdigit()]
            if len(nums) >= 2:
                w, h = nums[0], nums[1]
        except Exception:  # noqa: BLE001
            pass
        name = d.get("spdisplays_display-type", d.get("_name", f"Display {i+1}"))
        if not name or name == "spdisplays_display-type":
            name = d.get("display_type", f"Display {i+1}")
        main = d.get("spdisplays_main", "yes") == "yes"
        pos = d.get("spdisplays_position", d.get("_position", ""))
        px, py = 0, 0
        try:
            if pos:
                p = pos.strip("()").split(",")
                px = int(float(p[0]))
                py = int(float(p[1]))
        except Exception:  # noqa: BLE001
            pass
        parsed.append((name, w, h, px, py, main))
        max_y = max(max_y, py + h)

    for i, (name, w, h, px, py, main) in enumerate(parsed):
        # macOS 原点左下，转为左上原点
        top = max_y - (py + h)
        monitors.append(MonitorInfo(
            index=i,
            device_name=name,
            device_path=str(i),         # macOS 无稳定设备路径，用序号作 key
            is_primary=main or i == 0,
            left=px,
            top=top,
            width=w,
            height=h,
            work_left=px,
            work_top=top,
            work_width=w,
            work_height=h,
        ))
    return monitors


def get_virtual_screen():
    ms = enum_monitors()
    if not ms:
        return (0, 0, 0, 0)
    xs = [m.left for m in ms]
    ys = [m.top for m in ms]
    x2 = [m.left + m.width for m in ms]
    y2 = [m.top + m.height for m in ms]
    x, y = min(xs), min(ys)
    return (x, y, max(x2) - x, max(y2) - y)


def get_primary_monitor():
    ms = enum_monitors()
    for m in ms:
        if m.is_primary:
            return m
    return ms[0] if ms else None


if __name__ == "__main__":
    for m in enum_monitors():
        print(m.device_name, m.width, "x", m.height, "pos", m.left, m.top, "primary", m.is_primary)
    print("virtual:", get_virtual_screen())
