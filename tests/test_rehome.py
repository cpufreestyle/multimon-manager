#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rehome 的单测：插拔归位的屏身份、采集/冻结、策略机与归位几何。

覆盖四类事实，前两类是真机踩过的坑：

1. **拔屏后窗口绝不落屏外**。绝对坐标会因坐标系整体平移而失效（拔掉左边的屏，
   右边屏左移 1080），此时必须按相对框重映射 + clamp_to_work 钉回工作区。
   真机见过 y=-870 停在屏外的窗口，那是用户投诉最多的一条。
2. **只搬记录里有的窗口**。拔屏后新开的窗当时不在那块屏上，搬过去就是抢窗口
   （LayoutKeeper 用 CGWindowID 天花板判这件事，本项目用「不在记录里」判）。
3. **UUID 匹配按原框还原，UUID 不匹配走相对框重映射**。两块同为 1920x1080 的屏
   指纹分不清、UUID 可以（layouts 已用同一 API）。
4. **连报多次只归位一次**。macOS 插拔常连报多次，每次都归位会让窗口反复跳；
   freeze 只在该 key 没有冻结记录时写，重复调用不改写 frozen_at。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rehome  # noqa: E402
import settings  # noqa: E402


class _Mon:
    """最小显示器替身：UUID 由 device_path 映射而来，便于测试「换屏」场景。"""

    def __init__(self, left=0, top=0, width=1920, height=1080, name="M1",
                 work=None, dp="", vendor=0, model=0, serial=0):
        self.left, self.top = left, top
        self.width, self.height = width, height
        self.device_name = name
        self.device_path = dp
        self.vendor_id, self.model_id, self.serial = vendor, model, serial
        if work:
            self.work_left, self.work_top, self.work_width, self.work_height = work
        else:
            self.work_left, self.work_top = left, top
            self.work_width, self.work_height = width, height


# 真机三屏几何：Built-in 1512x982 主屏 work (0,33,1512,887)；
# DELL U2414H 1920x1080 竖屏 (-1080,-1100)；RV200 Pro 1600x900 (0,-900)。
def _real_mons():
    return [
        _Mon(0, 0, 1512, 982, "Built-in", (0, 33, 1512, 887), dp="1"),
        _Mon(0, -900, 1600, 900, "RV200 Pro", (0, -870, 1600, 870), dp="2"),
        _Mon(-1080, -1100, 1080, 1920, "DELL U2414H",
             (-1080, -1100, 1080, 1860), dp="3"),
    ]


class _FakeUUIDS(dict):
    pass


class RehomeTestBase(unittest.TestCase):
    """把 rehome 的存储与 UUID 探针都指到临时/可控位置。"""

    def setUp(self):
        self._orig_path = rehome._path
        self._tmp = os.path.join(
            settings.data_dir(), "_test_rehome_%d.json" % id(self))
        rehome._path = lambda: self._tmp
        self._uuids = {}
        self._orig_uuids = rehome.screen_math.display_uuids
        rehome.screen_math.display_uuids = self._fake_uuids
        self._orig_settings = rehome.settings.load
        rehome.settings.load = lambda: {}

    def tearDown(self):
        rehome._path = self._orig_path
        rehome.screen_math.display_uuids = self._orig_uuids
        rehome.settings.load = self._orig_settings
        try:
            os.remove(self._tmp)
        except OSError:
            pass

    def _fake_uuids(self, monitors):
        out = {}
        for m in monitors or []:
            u = self._uuids.get(getattr(m, "device_path", ""))
            if u:
                out[m.device_path] = u
        return out


class TestMonitorKey(RehomeTestBase):
    """屏 key：UUID 优先，其次硬件三元组，最后分辨率+位置。"""

    def test_uuid_wins(self):
        self._uuids["2"] = "AAAA-BBBB"
        m = _Mon(dp="2", vendor=1, model=2, serial=3)
        self.assertEqual(rehome.monitor_key(m), "uuid:AAAA-BBBB")

    def test_hwid_when_no_uuid(self):
        m = _Mon(dp="", vendor=19149, model=10048, serial=0)
        self.assertEqual(rehome.monitor_key(m), "hwid:19149-10048-0")

    def test_geo_fallback(self):
        # geo key 用 "#" 分隔尺寸与位置：指纹串用 "@"，key 自身不能再含 "@"，
        # 否则指纹解析会把 key 的位置段切错，同一块屏重排后被当成两块屏。
        self.assertEqual(rehome.monitor_key(_Mon(0, -900, 1600, 900)),
                         "geo:1600x900#0,-900")

    def test_none_monitor(self):
        self.assertEqual(rehome.monitor_key(None), "")


class TestFingerprintDiff(RehomeTestBase):
    """指纹差异：拔掉/插上哪些屏；只重排时两个列表都空。"""

    def test_unplug(self):
        old = "uuid:A@0,0|uuid:B@-1080,-1100"
        new = "uuid:A@0,0"
        removed, added = rehome.fingerprint_diff(old, new)
        self.assertEqual(removed, ["uuid:B"])
        self.assertEqual(added, [])

    def test_replug(self):
        old = "uuid:A@0,0"
        new = "uuid:A@0,0|uuid:B@-1080,-1100"
        removed, added = rehome.fingerprint_diff(old, new)
        self.assertEqual(added, ["uuid:B"])
        self.assertEqual(removed, [])

    def test_rearrange_only(self):
        """位置变了但屏没变：归位该按相对框重算，不该当成插拔。"""
        old = "uuid:A@0,0|uuid:B@-1080,-1100"
        new = "uuid:A@0,0|uuid:B@1080,0"
        removed, added = rehome.fingerprint_diff(old, new)
        self.assertEqual((removed, added), ([], []))


class TestCaptureFreeze(RehomeTestBase):
    """采集只记归属该屏的窗口；冻结只在该 key 没有记录时写（连报不重复归位）。"""

    def test_window_attributed_by_intersection(self):
        ms = _real_mons()
        wins = [{"hwnd": "App::W1", "owner": "App", "name": "W1",
                 "x": 0, "y": -870, "w": 800, "h": 600}]
        got = rehome.capture(ms, wins, now=1000.0)
        keys = [k for k, v in got.items() if v["wins"]]
        self.assertEqual(len(keys), 1)
        self.assertEqual(got[keys[0]]["wins"][0]["hwnd"], "App::W1")

    def test_throttle(self):
        ms = _real_mons()
        self.assertIsNotNone(rehome.capture(ms, [], now=1000.0))
        # 不足 CAPTURE_MIN_INTERVAL：跳过
        self.assertIsNone(rehome.capture(ms, [], now=1000.5))

    def test_freeze_is_idempotent(self):
        """连报多次只归位一次：重复 freeze 不改写 frozen_at（拔屏只冻结一次）。"""
        ms = _real_mons()
        rehome.capture(ms, [{"hwnd": "A::1", "owner": "A", "name": "1",
                             "x": 0, "y": 33, "w": 600, "h": 400}], now=1000.0)
        keys = [rehome.monitor_key(m) for m in ms]
        self.assertEqual(rehome.freeze(ms, keys, now=2000.0), 3)
        first = rehome.frozen_screens()
        # 再冻结一次（macOS 插拔常连报）：记录不动
        rehome.freeze(ms, keys, now=5000.0)
        second = rehome.frozen_screens()
        for k in keys:
            self.assertEqual(first[k]["frozen_at"], second[k]["frozen_at"])
        self.assertEqual(rehome.clear_frozen(keys), 3)
        self.assertEqual(rehome.frozen_screens(), {})


class TestPlanSameScreen(RehomeTestBase):
    """原屏还在：按原坐标精确还原。"""

    def test_exact_restore(self):
        ms = _real_mons()
        rehome.capture(ms, [{"hwnd": "A::1", "owner": "A", "name": "1",
                             "x": -1000, "y": -1000, "w": 900, "h": 700}],
                       now=1000.0)
        k = rehome.monitor_key(ms[2])
        rehome.freeze(ms, [k], now=2000.0)
        rows = rehome.plan(ms, [k], windows=["A::1"], now=2500.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["where"], "same")
        w = rows[0]["wins"][0]
        self.assertEqual((w["x"], w["y"], w["w"], w["h"]),
                         (-1000, -1000, 900, 700))


class TestPlanRemapped(RehomeTestBase):
    """原屏不在了：按相对框重映射到现存屏，并钉回工作区内（绝不落屏外）。"""

    def test_remap_to_remaining_screen(self):
        """拔掉左侧竖屏 DELL，它的窗口按比例落到主屏工作区内。"""
        ms = _real_mons()
        dell = ms[2]
        # 竖屏工作区 (-1080,-1100,1080,1860) 上占 80% 宽、60% 高的窗口
        wins = [{"hwnd": "A::1", "owner": "A", "name": "1",
                 "x": -1000, "y": -1000, "w": 864, "h": 1116}]
        rehome.capture(ms, wins, now=1000.0)
        k = rehome.monitor_key(dell)
        rehome.freeze(ms, [k], now=2000.0)
        # 现在只剩主屏和 RV200
        remaining = [ms[0], ms[1]]
        rows = rehome.plan(remaining, [k], windows=["A::1"], now=2500.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["where"], "remapped")
        w = rows[0]["wins"][0]
        x, y, ww, hh = w["x"], w["y"], w["w"], w["h"]
        # 必须完整落在某块现存屏的工作区内
        inside = any(
            m.work_left <= x and x + ww <= m.work_left + m.work_width
            and m.work_top <= y and y + hh <= m.work_top + m.work_height
            for m in remaining)
        self.assertTrue(inside, "归位矩形越出了所有现存屏的工作区：%s" % (
            (x, y, ww, hh),))
        # 宽高按比例缩放（不是把 864 宽原样搬去 1080 宽的 RV200）
        self.assertLessEqual(ww, 1600)

    def test_fallback_when_no_intersection(self):
        """原屏与所有现存屏都不相交：退回首屏工作区，而不是丢出屏外。"""
        ms = _real_mons()
        far = _Mon(-5000, -5000, 1000, 1000, "Far", (-5000, -5000, 1000, 1000),
                   dp="9")
        rehome.capture([far] + ms, [{"hwnd": "A::1", "owner": "A", "name": "1",
                                     "x": -4900, "y": -4900, "w": 800, "h": 600}],
                       now=1000.0)
        k = rehome.monitor_key(far)
        rehome.freeze([far] + ms, [k], now=2000.0)
        rows = rehome.plan(ms, [k], windows=["A::1"], now=2500.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["where"], "fallback")
        w = rows[0]["wins"][0]
        mon = ms[0]
        self.assertGreaterEqual(w["x"], mon.work_left)
        self.assertLessEqual(w["x"] + w["w"], mon.work_left + mon.work_width)
        self.assertGreaterEqual(w["y"], mon.work_top)
        self.assertLessEqual(w["y"] + w["h"], mon.work_top + mon.work_height)

    def test_clamped_inside_work_area(self):
        """相对框越界（负值 / >1）也要被钉回工作区内。"""
        ms = _real_mons()
        rehome.capture(ms, [{"hwnd": "A::1", "owner": "A", "name": "1",
                             "x": -1200, "y": -1300, "w": 2000, "h": 2600}],
                       now=1000.0)
        k = rehome.monitor_key(ms[2])
        rehome.freeze(ms, [k], now=2000.0)
        rows = rehome.plan([ms[0]], [k], windows=["A::1"], now=2500.0)
        w = rows[0]["wins"][0]
        self.assertGreaterEqual(w["x"], ms[0].work_left)
        self.assertLessEqual(w["x"] + w["w"], ms[0].work_left + ms[0].work_width)
        self.assertGreaterEqual(w["y"], ms[0].work_top)
        self.assertLessEqual(w["y"] + w["h"], ms[0].work_top + ms[0].work_height)


class TestPlanWindowMatching(RehomeTestBase):
    """只搬记录里有的窗口；拔屏后新开的窗不动。"""

    def test_new_window_not_moved(self):
        ms = _real_mons()
        rehome.capture(ms, [{"hwnd": "A::1", "owner": "A", "name": "1",
                             "x": 0, "y": 33, "w": 600, "h": 400}], now=1000.0)
        k = rehome.monitor_key(ms[0])
        rehome.freeze(ms, [k], now=2000.0)
        # 现在只有 A::1 和 B::2 开着；B::2 是拔屏后新开的
        rows = rehome.plan(ms, [k], windows=["A::1", "B::2"], now=2500.0)
        hwnds = [w["hwnd"] for w in rows[0]["wins"]]
        self.assertEqual(hwnds, ["A::1"])

    def test_stale_record_skipped(self):
        """记录超过 CEILING_TTL：不再自动搬窗（用户已开了一圈新窗）。"""
        ms = _real_mons()
        rehome.capture(ms, [{"hwnd": "A::1", "owner": "A", "name": "1",
                             "x": 0, "y": 33, "w": 600, "h": 400}], now=1000.0)
        k = rehome.monitor_key(ms[0])
        rehome.freeze(ms, [k], now=1000.0)
        rows = rehome.plan(ms, [k], windows=["A::1"],
                           now=1000.0 + rehome.CEILING_TTL + 60)
        self.assertEqual(rows[0]["where"], "stale")
        self.assertEqual(rows[0]["wins"], [])

    def test_windows_none_means_no_filter(self):
        """windows=None 时不过滤（调用方自己匹配窗口）。"""
        ms = _real_mons()
        rehome.capture(ms, [{"hwnd": "A::1", "owner": "A", "name": "1",
                             "x": 0, "y": 33, "w": 600, "h": 400}], now=1000.0)
        k = rehome.monitor_key(ms[0])
        rehome.freeze(ms, [k], now=2000.0)
        rows = rehome.plan(ms, [k], windows=None, now=2500.0)
        self.assertEqual(len(rows[0]["wins"]), 1)


class TestPolicy(RehomeTestBase):
    """策略机：每屏优先于全局，暂停压制一切。"""

    def test_global_off(self):
        self.assertEqual(rehome.policy_action("off", None), rehome.MODE_OFF)

    def test_global_auto(self):
        self.assertEqual(rehome.policy_action("auto", None), rehome.MODE_AUTO)

    def test_global_ask_default(self):
        self.assertEqual(rehome.policy_action("ask", None), rehome.MODE_ASK)
        self.assertEqual(rehome.policy_action(None, None), rehome.MODE_ASK)

    def test_per_monitor_overrides_global(self):
        self.assertEqual(rehome.policy_action("auto", "off"), rehome.MODE_OFF)
        self.assertEqual(rehome.policy_action("off", "auto"), rehome.MODE_AUTO)
        self.assertEqual(rehome.policy_action("ask", "ask"), rehome.MODE_ASK)

    def test_paused_suppresses(self):
        now = 1000.0
        self.assertEqual(
            rehome.policy_action("auto", "auto", now + 100, now=now), "paused")
        self.assertEqual(
            rehome.policy_action("auto", "auto", now - 100, now=now), "auto")


class TestApplyPlan(RehomeTestBase):
    """执行层：限流、失败如实记录。"""

    def _rows(self, n=3):
        return [{"key": "k", "screen": "S", "where": "same",
                 "wins": [{"hwnd": "A::%d" % i, "x": 0, "y": 0, "w": 10, "h": 10,
                           "where": "same"} for i in range(n)],
                 "skipped": []}]

    def test_all_applied(self):
        calls = []

        def _set(hwnd, x, y, w, h, activate=False):
            calls.append((hwnd, x, y, w, h))

        ok, detail = rehome.apply_plan(self._rows(2), _set)
        self.assertEqual(ok, 2)
        self.assertEqual(len(detail), 2)
        self.assertEqual(calls[0], ("A::0", 0, 0, 10, 10))

    def test_limit(self):
        ok, detail = rehome.apply_plan(self._rows(3), lambda *a, **k: None,
                                       limit=2)
        self.assertEqual(ok, 2)
        self.assertEqual(len(detail), 2)

    def test_failure_recorded(self):
        def _set(hwnd, x, y, w, h, activate=False):
            raise RuntimeError("AX 写入失败")

        ok, detail = rehome.apply_plan(self._rows(1), _set)
        self.assertEqual(ok, 0)
        self.assertEqual(detail[0]["ok"], False)
        self.assertIn("AX 写入失败", detail[0]["err"])

    def test_summary_reports_remap(self):
        rows = [{"key": "k", "screen": "DELL", "where": "remapped",
                 "wins": [{"hwnd": "A::1", "where": "remapped"},
                          {"hwnd": "A::2", "where": "remapped"}],
                 "skipped": []}]
        text = rehome.summary(rows)
        self.assertIn("DELL 已不存在", text)
        self.assertIn("2 个窗口", text)


if __name__ == "__main__":
    unittest.main()
