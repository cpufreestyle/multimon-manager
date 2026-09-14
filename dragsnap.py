"""拖拽吸附监听（Windows 占位实现）。

F3 的吸附预览以 macOS 优先（overlay 用 tkinter，点击穿透走 objc）。
Windows 端暂未实现全局拖拽监听，这里提供同名空实现以保持 backend 接口一致：
启用后 start() 返回 False，UI 会提示不可用，不影响其它功能。
"""


class DragSnapWatcher:
    def set_callback(self, cb):
        self._cb = cb

    def start(self):
        return False

    def stop(self):
        pass
