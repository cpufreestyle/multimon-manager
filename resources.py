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


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    print(create_ico(os.path.join(here, "app.ico")))
