"""显示器配置变化监听（非 macOS 占位实现）。

Windows 后续可用 WM_DISPLAYCHANGE 消息实现；当前返回 None 表示未启用，
UI 会据此提示"当前平台不支持"。
"""


def register(on_change):
    """未实现，返回 None。"""
    return None
