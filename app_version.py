"""应用版本号（唯一来源）。

- `build.py` 把它写进 macOS 的 `Info.plist`；
- `install.py` 用它写 Windows 注册表的 DisplayVersion；
- `version_info.py` 是 PyInstaller 的 Windows 版本资源文件，PyInstaller 用
  `eval()` 解析整个文件，因此它必须保持「单个表达式」，无法 import 本模块，
  版本号只能在那里再字面量写一遍。`build.py` 打包前会校验二者一致，避免漂移。
"""

VERSION = "0.3.1"
