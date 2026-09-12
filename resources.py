"""生成简单的程序图标（纯 Python 写 ICO，无第三方依赖）。"""
import struct
import os


def create_ico(path, size=32):
    """生成一个纯色渐变方块图标，返回路径。失败返回 None。

    注意：本函数仅作为「内置图标缺失时的兜底生成器」。若目标路径已存在
    （无论是 AI 生成的精美图标还是打包内置图标），一律不覆盖，直接复用，
    避免把高质量图标误写成低质占位图。
    """
    if not path:
        return None
    if os.path.exists(path):
        return path
    # 确保父目录存在
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        xor = bytearray()
        for y in range(size - 1, -1, -1):
            for x in range(size):
                b = int(255 * x / max(size - 1, 1))
                g = int(150 * y / max(size - 1, 1))
                r = 40
                xor += bytes((b, g, r, 255))
        andmask = b"\x00" * ((size * size) // 8)
        xor_bytes = bytes(xor)
        bmi_size = 40 + len(xor_bytes) + len(andmask)
        icon_dir = struct.pack("<HHH", 0, 1, 1)
        entry = struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, bmi_size, 22)
        bmi = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        with open(path, "wb") as f:
            f.write(icon_dir)
            f.write(entry)
            f.write(bmi)
            f.write(xor_bytes)
            f.write(andmask)
        return path
    except Exception as e:  # noqa: BLE001
        print("[resources] 图标生成失败:", e)
        return None


# ── PNG 图标（macOS Dock 用，纯 Python 绘制 + zlib 编码）──────────────────

def _rounded_hit(px, py, x0, y0, x1, y1, r):
    """点 (px,py) 是否落在圆角矩形内。"""
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    cx = x0 + r if px < x0 + r else (x1 - r if px > x1 - r else px)
    cy = y0 + r if py < y0 + r else (y1 - r if py > y1 - r else py)
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= r * r


def _render_icon(size, ss=2):
    """渲染"多屏"主题图标，返回每行字节（含 PNG 过滤字节）。

    画面：圆角方形底 + 深蓝→青竖向渐变，上面叠三块显示器（左竖屏 + 右侧上下两屏，
    主屏用亮青色高亮），呼应本工具的多屏管理用途。
    """
    S = size * ss
    bg_r = 0.225 * S                                   # macOS 图标圆角比例
    c0, c1 = (30, 58, 138), (14, 165, 164)             # #1E3A8A → #0EA5A4
    # 三块屏（比例坐标 + 颜色 + 不透明度）
    screens = [
        (0.145, 0.245, 0.415, 0.735, (255, 255, 255), 0.92),
        (0.475, 0.245, 0.855, 0.475, (127, 227, 255), 1.00),   # 主屏高亮
        (0.475, 0.525, 0.855, 0.735, (255, 255, 255), 0.92),
    ]
    scr = [(a * S, b * S, c * S, d * S, 0.032 * S, col, al)
           for (a, b, c, d, col, al) in screens]
    rows = []
    for y in range(S):
        t = y / (S - 1)
        br = c0[0] + (c1[0] - c0[0]) * t
        bg = c0[1] + (c1[1] - c0[1]) * t
        bb = c0[2] + (c1[2] - c0[2]) * t
        # 背景圆角矩形在该行的 x 范围（角部收窄，顺带省掉大量像素）
        if y < bg_r:
            dx = (bg_r * bg_r - (bg_r - y) ** 2) ** 0.5
            xmin, xmax = int(bg_r - dx), int(S - bg_r + dx)
        elif y > S - bg_r:
            dx = (bg_r * bg_r - (y - (S - bg_r)) ** 2) ** 0.5
            xmin, xmax = int(bg_r - dx), int(S - bg_r + dx)
        else:
            xmin, xmax = 0, S - 1
        row = bytearray(b"\x00")
        for x in range(S):
            if x < xmin or x > xmax:
                row += b"\x00\x00\x00\x00"          # 圆角外：透明
                continue
            r, g, b = br, bg, bb
            for (sx0, sy0, sx1, sy1, sr, col, al) in scr:
                if x < sx0 or x > sx1 or y < sy0 or y > sy1:
                    continue
                if _rounded_hit(x, y, sx0, sy0, sx1, sy1, sr):
                    r = col[0] * al + r * (1 - al)
                    g = col[1] * al + g * (1 - al)
                    b = col[2] * al + b * (1 - al)
            row += bytes((int(r), int(g), int(b), 255))
        rows.append(bytes(row))
    if ss <= 1:
        return rows
    # 降采样 ss×ss 得到抗锯齿（含 alpha，圆角边缘平滑）
    out = []
    n = ss * ss
    for y in range(size):
        row = bytearray(b"\x00")
        for x in range(size):
            acc = [0, 0, 0, 0]
            for dy in range(ss):
                src = rows[y * ss + dy]
                base = 1 + (x * ss) * 4
                for dx in range(ss):
                    o = base + dx * 4
                    acc[0] += src[o]
                    acc[1] += src[o + 1]
                    acc[2] += src[o + 2]
                    acc[3] += src[o + 3]
            row += bytes((acc[0] // n, acc[1] // n, acc[2] // n, acc[3] // n))
        out.append(bytes(row))
    return out


def create_png(path, size=512):
    """生成"多屏"主题 PNG 图标，返回路径（失败返回 None）。

    与 create_ico 一致：目标已存在时不覆盖，直接复用。
    """
    if not path:
        return None
    if os.path.exists(path):
        return path
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        import zlib
        rows = _render_icon(size)
        raw = b"".join(rows)

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw, 9))
               + chunk(b"IEND", b""))
        with open(path, "wb") as f:
            f.write(png)
        return path
    except Exception as e:  # noqa: BLE001
        print("[resources] PNG 图标生成失败:", e)
        return None


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    print(create_ico(os.path.join(here, "app.ico")))
    print(create_png(os.path.join(here, "app.png")))
