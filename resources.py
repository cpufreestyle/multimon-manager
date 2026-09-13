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


def remove_white_corners_png(path):
    """把 PNG 图标四角/边缘的白色背景区域设为透明（保留内部白色图形）。

    通过从边缘 flood-fill 背景色实现：仅标记与边缘相连、且颜色接近背景
    的像素为透明；内部白色线条/图形因被非背景色包围而不会受影响。
    使用 Pillow；未安装则原样返回。
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return path
    try:
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        px = img.load()
        corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
        bg = tuple(sum(px[x, y][c] for x, y in corners) // 4 for c in range(4))
        tol = 80  # 与背景色的 RGB 欧氏距离容差

        from collections import deque
        keep = [[True] * h for _ in range(w)]  # True = 保留
        q = deque()
        for x in range(w):
            q.append((x, 0))
            q.append((x, h - 1))
        for y in range(h):
            q.append((0, y))
            q.append((w - 1, y))

        while q:
            x, y = q.popleft()
            if not (0 <= x < w and 0 <= y < h):
                continue
            if not keep[x][y]:
                continue
            r, g, b, a = px[x, y]
            dist = ((r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2) ** 0.5
            if a > 20 and dist <= tol:
                keep[x][y] = False
                q.append((x - 1, y))
                q.append((x + 1, y))
                q.append((x, y - 1))
                q.append((x, y + 1))

        changed = False
        for y in range(h):
            for x in range(w):
                if not keep[x][y]:
                    px[x, y] = (0, 0, 0, 0)
                    changed = True
        if changed:
            img.save(path)
        return path
    except Exception as e:  # noqa: BLE001
        print("[resources] 图标去白失败:", e)
        return path


def ensure_transparent_icon(path, size=512):
    """确保 path 指向的图标四角透明。

    优先用 Pillow 处理原图（保留用户自定义设计）；未安装 Pillow 时，删除
    旧图并重新生成内置的透明圆角图标。
    """
    remove_white_corners_png(path)
    try:
        from PIL import Image  # noqa: F401
    except Exception:  # noqa: BLE001
        if os.path.exists(path):
            os.remove(path)
        return create_png(path, size=size)
    return path


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    print(create_ico(os.path.join(here, "app.ico")))
    print(create_png(os.path.join(here, "app.png")))
