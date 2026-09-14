"""托盘子进程与主进程之间的极简命令通道（基于临时文件）。

托盘是独立进程，无法直接调用主进程的函数；这里用一个约定路径的文件传命令：
托盘写入命令，主进程定时读取并清空。
命令取值：open / refresh / apply_layout / apply_scenario:<情景签名>（F4）。
"""
import os
import tempfile

CMD_FILE = os.path.join(tempfile.gettempdir(), "multimon-manager.cmd")


def send(command):
    """写入一条命令（覆盖上一条）；成功返回 True，失败静默返回 False。"""
    try:
        with open(CMD_FILE, "w", encoding="utf-8") as f:
            f.write(command)
        return True
    except Exception:  # noqa: BLE001
        return False


def take():
    """读取并清空命令；无命令时返回 None。"""
    try:
        with open(CMD_FILE, "r", encoding="utf-8") as f:
            cmd = f.read().strip()
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None
    if not cmd:
        return None
    try:
        with open(CMD_FILE, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:  # noqa: BLE001
        pass
    return cmd
