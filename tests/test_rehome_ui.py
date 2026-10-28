#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插拔归位的界面接线单测：控件必须映射、指纹差异必须拿得到、计划必须真的执行。

这里防的是本项目历史上反复出现的界面故障类型（都不是算法错）：
1. **packer 不映射 / rebuild 删整行**——控件建了却不可点，用户看到「点了没效果」。
2. **拿错对比基准**——_handle_display_change 若用 _last_mon_sig 当 old（它已被
   _poll_display 改成新签名），diff 恒为空，归位永远不触发。这是真机推演时发现
   的 bug，必须有回归测试钉住。
3. **计划算了不执行**——apply_plan 的 set_rect 必须真的被调用，参数必须是对的。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    import ui  # noqa: E402
    import rehome  # noqa: E402
    import backend as b  # noqa: E402
    import settings  # noqa: E402
except Exception:  # noqa: BLE001  无 tkinter / 无显示环境：整类跳过
    tk = None
    ui = None
    rehome = None
    b = None

if tk is None:
    raise unittest.SkipTest("tkinter 不可用，跳过界面单测")


class _FakeBox:
    """替身 messagebox：记录调用，askyesno 恒真，不弹窗。"""

    def __init__(self, sink):
        self._sink = sink

    def showinfo(self, title, msg=None, **k):
        self._sink.append(("info", msg or title))

    def showwarning(self, title, msg=None, **k):
        self._sink.append(("warn", msg or title))

    def showerror(self, title, msg=None, **k):
        self._sink.append(("error", msg or title))

    def askyesno(self, title, msg=None, **k):
        self._sink.append(("ask", msg or title))
        return True


class _Mon:
    """替身显示器。device_path 必须是**非数字**串。

    rehome.monitor_key 见 device_path 是数字就去查真实的
    CGDisplayCreateUUIDFromDisplayID——替身若用 "1"/"2" 会撞上真机的 CG
    display id，拿到真机的 UUID，于是「同一块屏」在采集与策略机里算成两个
    不同 key（geo:... 与 uuid:...），归位因此永不触发。
    """

    def __init__(self, left, top, width, height, name, dp, work=None):
        self.left, self.top = left, top
        self.width, self.height = width, height
        self.device_name = name
        self.device_path = dp
        if work:
            self.work_left, self.work_top, self.work_width, self.work_height = work
        else:
            self.work_left, self.work_top = left, top
            self.work_width, self.work_height = width, height


class TestRehomeUi(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ui.App(self.root)
        self.mons = [_Mon(0, 33, 1512, 887, "Built-in", "test-builtin"),
                     _Mon(-1080, -1050, 1080, 1920, "DELL", "test-dell")]
        self.app.monitors = list(self.mons)
        self.root.update()
        # 采集与设置都指向临时文件，避免污染真实配置，也避免用例之间互相影响
        # （上一个用例写下的「暂停 1 小时」会让下一个用例的动作变成 paused）。
        self._tmp = os.path.join(
            settings.data_dir(), "_test_rehome_ui_%d.json" % id(self))
        self._orig_path = rehome._path
        rehome._path = lambda: self._tmp
        # settings 不换 _PATH：它的读缓存按 (mtime_ns, size) 失效，换路径后缓存
        # 里还是旧文件的数据，会出现「写了读不到」。改为备份真实 settings.json，
        # 用例结束后原样恢复——既隔离又不会踩缓存。
        self._set_file = os.path.join(settings.data_dir(), "settings.json")
        self._set_bak = self._tmp + ".settings.bak"
        try:
            with open(self._set_file, "rb") as f:
                self._set_had = f.read()
        except OSError:
            self._set_had = None
        settings._cache["stat"] = None
        settings._cache["data"] = None
        # 真实 settings.json 里可能留着上次运行写下的「暂停 1 小时」或每屏模式
        # （测试污染），会让本用例的策略判断变成 paused / off。先清干净。
        _s = settings.load()
        _s.pop("rehome_paused_until", None)
        _s.pop("rehome", None)
        settings.save(_s)
        settings._cache["stat"] = None
        settings._cache["data"] = None
        self._calls = []
        self._orig_apply = self.app._rehome_run
        # 测试里绝不能弹模态框：messagebox 会阻塞到有人点按钮，整批测试挂住。
        # ui.py 是 from tkinter import messagebox，必须打**模块属性**才生效
        # （打实例属性无效——那只会新建一个没人看的属性）。
        self._msg = []
        self._orig_mb = ui.messagebox
        ui.messagebox = _FakeBox(self._msg)

    def tearDown(self):
        ui.messagebox = self._orig_mb
        rehome._path = self._orig_path
        # 还原真实 settings.json：用例里写过的 rehome 开关/模式/暂停都不该留下
        try:
            if self._set_had is None:
                os.remove(self._set_file)
            else:
                with open(self._set_file, "wb") as f:
                    f.write(self._set_had)
        except OSError:
            pass
        settings._cache["stat"] = None
        settings._cache["data"] = None
        try:
            os.remove(self._tmp)
        except OSError:
            pass
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _packed(self, widget):
        """packer 是否真的给这个控件排了位置。

        不能只查 winfo_ismapped()：withdraw 状态下整个滚动区里的控件都是
        mapped=0（连既有的「每屏不同」单选也是），那是窗口没显示，不是控件没排。
        可靠的判据是 packer 记录了它（winfo_manager）且给了非零尺寸——
        窗口一显示就会被映射，用户点得到。
        """
        if widget is None:
            return False
        if widget.winfo_manager() != "pack":
            return False
        return (widget.winfo_reqwidth() > 1 and widget.winfo_reqheight() > 1)

    # ---------- 控件映射 ----------

    def test_rehome_controls_mapped(self):
        """归位开关、方式下拉、三个按钮都必须被 packer 排上（否则点了没效果）。"""
        self.assertTrue(hasattr(self.app, "rehome_var"), "归位开关不存在")
        btns = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.cget("text") in ("立即归位", "暂停 1 小时", "每屏设置…"):
                        btns.append(c)
                except Exception:  # noqa: BLE001
                    pass
                walk(c)
        walk(self.root)
        self.assertEqual(len(btns), 3, "归位按钮应有 3 个，实际 %d 个" % len(btns))
        for btn in btns:
            self.assertTrue(self._packed(btn),
                            "按钮「%s」没被 packer 排上：点了没反应" % btn.cget("text"))
        # 方式下拉的候选必须含三种模式
        labels = self.app._rehome_mode_labels()
        self.assertEqual(len(labels), 3)

    def test_mode_menu_has_current_value(self):
        """OptionMenu 的初始值必须在候选列表里，否则界面显示空白。"""
        for mode in rehome.MODES:
            self.app.rehome_mode_var.set(mode)
            self.assertIn(mode, rehome.MODES)
            labels = self.app._rehome_mode_labels()
            self.assertTrue(labels)

    # ---------- 指纹差异必须拿得到 ----------

    def _freeze_monitors(self, mons):
        """挡住 refresh_monitors / 枚举：测试里只认我们给的这批屏。

        不挡的话 _handle_display_change 里的 refresh_monitors() 会用真实枚举结果
        覆盖 self.app.monitors，diff 算的是真机几何，与用例设的场景无关。
        """
        self.app.refresh_monitors = lambda: None
        self.app.monitors = list(mons)
        self._orig_enum = b.enum_monitors
        b.enum_monitors = lambda force=False: list(mons)

    def _unfreeze(self):
        b.enum_monitors = self._orig_enum

    def test_diff_uses_pending_signature(self):
        """回归：_handle_display_change 必须用 _pending_mon_sig 当 old。

        _poll_display 检测到变化时就把 _last_mon_sig 改成了新签名，若这里还拿
        _last_mon_sig 当基准，两者相等、diff 恒空，归位永远不触发。
        """
        old_fp = b.rehome_fingerprint(self.mons)          # 两块屏
        self.app._last_mon_sig = (0, 0, 0)                # 已被轮询改成新签名
        self.app._pending_mon_sig = old_fp                # 真正的前一态
        # 现在只剩一块屏（DELL 被拔掉）
        self._freeze_monitors([self.mons[0]])
        try:
            captured = {}
            self.app._rehome_capture_now = lambda: captured.setdefault("cap", True)
            self.app._rehome_on_display_change = \
                lambda removed, added: captured.update(removed=removed,
                                                       added=added)
            self.app._handle_display_change()
            # 用「拔掉的那块屏的 key」比对：替身的 device_path 不是 CG id，
            # display_uuids 取不到 UUID，key 会退化成 geo:...——两侧同源即可比。
            self.assertEqual(captured.get("removed"),
                             [b.rehome_monitor_key(self.mons[1])],
                             "没识别出拔掉的那块屏——归位不会触发")
            self.assertEqual(captured.get("added"), [])
            self.assertTrue(captured.get("cap"), "没做采集")
        finally:
            self._unfreeze()

    def test_old_sig_not_last_sig(self):
        """钉住 bug 本身：拿 _last_mon_sig 当基准时 diff 恒为空。"""
        # 前后两块屏的 key 集合必须真的不同，否则这个用例证明不了任何事
        before = set(b.rehome_fingerprint([self.mons[0], self.mons[1]]).split("|"))
        after = set(b.rehome_fingerprint([self.mons[0]]).split("|"))
        self.assertNotEqual(before, after, "用例前提不成立：两块屏的 key 集合没变")
        self.app._last_mon_sig = (9, 9, 9)
        self.app._pending_mon_sig = b.rehome_fingerprint(self.mons)
        self._freeze_monitors([self.mons[0]])
        try:
            captured = {}
            self.app._rehome_capture_now = lambda: None
            self.app._rehome_on_display_change = \
                lambda r, a: captured.update(removed=r, added=a)
            self.app._handle_display_change()
            # 拔掉的那块屏的 key（同源于 rehome_monitor_key，与 UUID/geo 无关）
            self.assertEqual(captured.get("removed"),
                             [b.rehome_monitor_key(self.mons[1])])
        finally:
            self._unfreeze()

    def test_no_diff_when_rearranged_only(self):
        """只是重排（屏没变）：不该触发插拔归位。"""
        # 位置变了，但两块屏都还在
        self.mons[1].left = 1080
        self._freeze_monitors(self.mons)
        try:
            fp = b.rehome_fingerprint(self.mons)
            self.app._last_mon_sig = (1, 2, 3)
            self.app._pending_mon_sig = fp
            captured = {}
            self.app._rehome_capture_now = lambda: None
            self.app._rehome_on_display_change = \
                lambda r, a: captured.update(hit=True)
            self.app._handle_display_change()
            self.assertNotIn("hit", captured, "重排也触发了插拔归位")
        finally:
            self._unfreeze()

    # ---------- 计划真的执行 ----------

    def test_rehome_run_calls_set_rect(self):
        """归位必须真的调 set_window_rect，参数就是计划里的矩形。"""
        calls = []
        orig = b.set_window_rect

        def _set(hwnd, x, y, w, h, activate=False):
            calls.append((hwnd, x, y, w, h, activate))

        b.set_window_rect = _set
        try:
            rows = [{"key": "k", "screen": "DELL", "where": "same",
                     "wins": [{"hwnd": "A::1", "x": -1000, "y": -1000,
                               "w": 900, "h": 700, "where": "same"}],
                     "skipped": []}]
            self.app._rehome_run(rows)
        finally:
            b.set_window_rect = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("A::1", -1000, -1000, 900, 700, False))
        # 状态栏必须给出可读反馈
        self.assertIn("归位完成", self.app._rehome_status.get())

    def test_rehome_run_reports_failure(self):
        """写入失败要如实报出来，不能静默。"""
        def _set(*a, **k):
            raise RuntimeError("AX 写入失败")

        orig = b.set_window_rect
        b.set_window_rect = _set
        try:
            rows = [{"key": "k", "screen": "DELL", "where": "same",
                     "wins": [{"hwnd": "A::1", "x": 0, "y": 0, "w": 10,
                               "h": 10, "where": "same"}],
                     "skipped": []}]
            self.app._rehome_run(rows)
        finally:
            b.set_window_rect = orig
        status = self.app._rehome_status.get()
        self.assertIn("失败 1", status)
        self.assertIn("AX 写入失败", status)

    def test_rehome_now_without_records(self):
        """没有记录时点「立即归位」要弹提示，不能静默什么都不做。"""
        rehome.clear_frozen()
        self.app._rehome_now()
        self.assertTrue(self._msg, "没有记录时点了没反馈")
        self.assertIn("没有可归位的记录", self._msg[-1][1])

    def test_pause_sets_status(self):
        self.app._rehome_pause()
        self.assertIn("暂停 1 小时", self.app._rehome_status.get())
        self.assertIsNotNone(b.rehome_paused_until())

    def _seed_frozen(self, key, wins):
        """造一份冻结记录，让 plan 真的产出计划行。

        时间基准必须用真实 now：capture 的节流判据是「与上次采集间隔」，
        而上次采集的 last_capture_ts 是 time.time() 写的。若这里传 1000.0
        这类小数值，间隔为负、必然被节流跳过，什么都写不进去（freeze 随后
        冻结一份空记录，plan 返回空——用例会莫名失败）。
        """
        import time as _t
        self._freeze_monitors(self.mons)
        try:
            # 先清历史：rehome.json 里可能留着真机采集的旧 screens，
            # 不清会让 plan 读到上一次的窗口集合，用例断言全部对不上。
            rehome.clear_frozen()
            _now = _t.time()
            b.rehome_capture(self.mons, wins, now=_now, min_interval=0.0)
            b.rehome_freeze(self.mons, [key], now=_now + 1.0)
        finally:
            self._unfreeze()

    def test_off_mode_skips(self):
        """全局关闭时不该搬窗口。"""
        k = b.rehome_monitor_key(self.mons[1])
        self._seed_frozen(k, [{"hwnd": "A::1", "owner": "A", "name": "1",
                               "x": -1000, "y": -1000, "w": 900, "h": 700}])
        self.app.rehome_mode_var.set(rehome.MODE_OFF)
        self.app.rehome_var.set(True)
        captured = {}
        self.app._rehome_run = lambda rows: captured.update(run=True)
        self.app._rehome_on_display_change([k], [k])
        self.assertNotIn("run", captured, "全局关闭还在搬窗口")
        self.assertIn("归位已关闭", self.app._rehome_status.get())

    def test_per_monitor_off_beats_global_auto(self):
        """每屏设了「永不归位」，全局即使是自动也不搬（LayoutKeeper 语义）。"""
        k = b.rehome_monitor_key(self.mons[1])
        self._seed_frozen(k, [{"hwnd": "A::1", "owner": "A", "name": "1",
                               "x": -1000, "y": -1000, "w": 900, "h": 700}])
        self.app.rehome_mode_var.set(rehome.MODE_AUTO)
        self.app.rehome_var.set(True)
        b.rehome_set_mode(k, rehome.MODE_OFF)
        captured = {}
        self.app._rehome_run = lambda rows: captured.update(run=True)
        try:
            self.app._rehome_on_display_change([k], [k])
        finally:
            b.rehome_set_mode(k, None)
        self.assertNotIn("run", captured, "每屏已关，全局自动却还在搬窗口")
        self.assertIn("归位已关闭", self.app._rehome_status.get())

    def test_no_records_says_nothing_moved(self):
        """没有任何记录时：不搬、不报错，状态栏留空（没有可归位的）。"""
        rehome.clear_frozen()
        self.app.rehome_mode_var.set(rehome.MODE_AUTO)
        self.app.rehome_var.set(True)
        captured = {}
        self.app._rehome_run = lambda rows: captured.update(run=True)
        self.app._rehome_on_display_change(["nope"], ["nope"])
        self.assertNotIn("run", captured)
        self.assertEqual(self.app._rehome_status.get(), "")


if __name__ == "__main__":
    unittest.main()
