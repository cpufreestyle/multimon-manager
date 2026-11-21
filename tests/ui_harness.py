#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面单测的无头夹具：**只作用于测试进程，不改生产代码**。

背景（2026-11-13 全量 unittest discover 挂起的真因）
--------------------------------------------------
`ui.App.__init__ / _build` 在构造期会排 7 个 `self.root.after` 定时器：

    _poll_taskbar(3000) / _poll_display(1500) / _poll_rehome_capture(5000)
    _start_command_poll(800) / _poll_rules(1500) / _slideshow_restore(2000)
    <lambda: _toggle_hk(quiet=True)>(600)

最后一个会在 `settings.json` 里 `hk_enabled=true` 时注册 21 组**真机全局
热键**并起 CGEventTap 线程——单测跑一次就动一次用户机器。这些定时器还会：

1. 在测试进程里反复自排，即使 `root.destroy()` 之后仍向已失效的 Tcl
   解释器投递回调，刷屏 `invalid command name "..._poll_rehome_capture"`；
2. 让**没 withdraw 的** Tk root 的 `root.update()` 永不返回（事件流不断），
   `test_snap_rows_mapped.py` 的 `setUpClass` 因此卡死，全量套件被 SIGTERM；
3. 构造期 `_toggle_display_watch()` 会 `settings.save()` 写真配置。

夹具做两件事
------------
* `build_app()`：构造期冻结 `root.after` / `root.after_cancel`（定时器只登记、
  不排进 Tcl 事件循环），并让 `settings.save` 空转；构造完立即恢复。界面仍
  **完整构建**（真 Tk、真控件、真显示器枚举），几何 / 映射断言照常有效。
* `close_app()`：先停后台对象，再取消该解释器里所有 pending `after`，最后
  `destroy()`。返回被取消的定时器数，供回归测试断言（防止静默跳过清理）。

生产代码零改动：`MMM_HEADLESS_TESTS` 之类的开关一律不进 ui.py，默认行为
（用户双击 App）与本夹具无关。
"""
import os
import sys

# 文件名不能写成 `_ui_harness.py`：项目 .gitignore 有 `_*.py`（临时脚本），
# 那样夹具不会被 git 跟踪，别人拉代码后全套界面测试直接 ImportError。
# `ui_harness.py` 不匹配 discover 的 `test_*.py`，不会被当成测试收集。

# 允许 `python3 tests/test_x.py` 直接跑：tests 目录本身要在 sys.path 上。
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS_DIR)
for _p in (_TESTS_DIR, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from unittest import mock  # noqa: E402

FROZEN_ATTR = "_harness_frozen_jobs"


def build_app(ui_module, root, *args, **kwargs):
    """构造真实 `ui.App`，但把构造期排的后台定时器全部冻结掉。

    返回 app；`app._harness_frozen_jobs` 是 `(ms, 回调名)` 元组，供回归测试
    逐条钉住具体定时器（而不是只断言「没报错」）。
    """
    frozen = []

    def _frozen_after(ms, callback=None, *a):
        frozen.append((ms, getattr(callback, "__name__", repr(callback))))
        return "harness-frozen#%d" % len(frozen)

    def _frozen_cancel(_job):
        return None

    # settings.save 空转只覆盖「构造期」；用例自己测持久化时 save 已恢复。
    with mock.patch.object(root, "after", _frozen_after), \
         mock.patch.object(root, "after_cancel", _frozen_cancel), \
         mock.patch.object(ui_module.settings, "save", lambda *_a, **_k: None):
        app = ui_module.App(root, *args, **kwargs)

    setattr(app, FROZEN_ATTR, tuple(frozen))
    return app


def cancel_pending_after(root):
    """取消 root 所在 Tcl 解释器里所有待执行的 after，返回取消数量。

    `tk.call("after", "info")` 在 macOS Tk 上返回的是**空格分隔的字符串**，
    必须用 `tk.splitlist` 解析；直接迭代会按字符遍历（本轮踩过的坑）。
    """
    try:
        jobs = list(root.tk.splitlist(root.tk.call("after", "info")))
    except Exception:  # noqa: BLE001 - 解释器已销毁时没有可取消的定时器
        return 0
    n = 0
    for job in jobs:
        try:
            root.after_cancel(job)
            n += 1
        except Exception:  # noqa: BLE001 - 已被别处取消的不算失败
            pass
    return n


def close_app(root, app=None):
    """停后台对象 → 取消全部 after → 销毁 root。返回取消的定时器数。"""
    if app is not None:
        hk = getattr(app, "hk", None)
        if hk is not None and hasattr(hk, "stop"):
            try:
                hk.stop()
            except Exception:  # noqa: BLE001
                pass
        for attr, method in (("_drag_watcher", "stop"),
                             ("_taskbar", "destroy"),
                             ("_snap_preview", "destroy")):
            obj = getattr(app, attr, None)
            fn = getattr(obj, method, None) if obj is not None else None
            if fn is not None:
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
    cancelled = cancel_pending_after(root)
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass
    return cancelled
