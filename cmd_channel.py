"""托盘子进程与主进程之间的极简命令通道（基于临时文件）。

托盘是独立进程，无法直接调用主进程的函数；这里用一个约定路径的文件传命令：
托盘写入命令，主进程定时读取并清空。
命令取值：open / refresh / apply_layout / apply_scenario:<情景签名>（F4）/
snap_quad（一键 2×2 排最近四个窗口）。
"""
import os
import tempfile

CMD_FILE = os.path.join(tempfile.gettempdir(), "multimon-manager.cmd")
# 唤醒回执：第二次启动时发 open 命令，旧实例处理完会在这里回一条状态。
# 没有回执就说明旧实例已经僵死（轮询链断掉或进程卡住），新实例可以接管。
ACK_FILE = os.path.join(tempfile.gettempdir(), "multimon-manager.open.ack")


def send(command):
    """写入一条命令（覆盖上一条）；成功返回 True，失败静默返回 False。"""
    try:
        with open(CMD_FILE, "w", encoding="utf-8") as f:
            # 带上发送者 pid：消费方据此判断「这条是新实例发的唤醒请求」
            f.write("%s %d" % (command, os.getpid()))
        return True
    except Exception:  # noqa: BLE001
        return False


def take():
    """读取并清空命令；无命令时返回 None。"""
    try:
        with open(CMD_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        with open(CMD_FILE, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:  # noqa: BLE001
        pass
    parts = raw.split(None, 1)
    return parts[0] if parts else None


def ack_open(status="ok"):
    """写一条唤醒回执（状态 ok / fail）。失败静默忽略。"""
    try:
        with open(ACK_FILE, "w", encoding="utf-8") as f:
            f.write("%s %d" % (status, os.getpid()))
    except Exception:  # noqa: BLE001
        pass


def read_open_ack():
    """读取唤醒回执；没有或读不出返回 None。"""
    try:
        with open(ACK_FILE, "r", encoding="utf-8") as f:
            parts = f.read().strip().split()
    except Exception:  # noqa: BLE001
        return None
    if not parts:
        return None
    status = parts[0]
    pid = None
    if len(parts) > 1:
        try:
            pid = int(parts[1])
        except ValueError:
            pid = None
    return {"status": status, "pid": pid}
