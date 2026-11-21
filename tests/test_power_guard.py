#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""power_guard 的单测：省电判据、Dock 速度可逆性、以及「默认不碰系统」。

这个模块会改 macOS 全局 Dock 偏好并 killall Dock，所以测试用可注入的 fake
runner 完整覆盖执行层，同时用源码扫描钉住「只有用户显式调用才会写系统」。
任何一条断言都必须能说清具体拒因，不接受「抛了错就算通过」。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import power_guard as pg  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(relative):
    with open(os.path.join(ROOT, relative), "r", encoding="utf-8") as handle:
        return handle.read()


class TestEvaluate(unittest.TestCase):
    def test_normal_power_does_not_throttle(self):
        d = pg.evaluate(pg.PowerSnapshot(80, True, 0))
        self.assertFalse(d.throttle)
        self.assertEqual(d.unknown, ())
        self.assertFalse(d.degraded)
        self.assertIn("正常", d.summary())

    def test_low_battery_on_battery_throttles(self):
        d = pg.evaluate(pg.PowerSnapshot(18, False, 0))
        self.assertTrue(d.throttle)
        self.assertEqual(d.reasons, ("lowBattery",))

    def test_low_battery_on_ac_does_not_throttle(self):
        d = pg.evaluate(pg.PowerSnapshot(5, True, 0))
        self.assertFalse(d.throttle, "插着电就不该因为电量低收手")
        self.assertEqual(d.reasons, ())

    def test_boundary_exactly_at_threshold_throttles(self):
        self.assertTrue(pg.evaluate(pg.PowerSnapshot(20, False, 0)).throttle)
        self.assertFalse(pg.evaluate(pg.PowerSnapshot(21, False, 0)).throttle)

    def test_thermal_serious_and_critical_throttle(self):
        for state in (2, 3):
            d = pg.evaluate(pg.PowerSnapshot(90, True, state))
            self.assertTrue(d.throttle, f"thermal={state}")
            self.assertEqual(d.reasons, ("thermalSerious",))

    def test_thermal_fair_does_not_throttle(self):
        self.assertFalse(pg.evaluate(pg.PowerSnapshot(90, True, 1)).throttle)

    def test_both_reasons_are_reported(self):
        d = pg.evaluate(pg.PowerSnapshot(10, False, 3))
        self.assertEqual(d.reasons, ("lowBattery", "thermalSerious"))

    def test_unknown_data_is_degraded_but_never_silently_normal(self):
        d = pg.evaluate(pg.PowerSnapshot(None, None, None))
        self.assertFalse(d.throttle, "读不到数据不能误停用户功能")
        self.assertTrue(d.degraded)
        self.assertEqual(set(d.unknown), {"battery", "power_source", "thermal"})
        self.assertIn("状态未知", d.summary())

    def test_unknown_power_source_does_not_throttle_low_battery(self):
        d = pg.evaluate(pg.PowerSnapshot(5, None, 0))
        self.assertFalse(d.throttle)
        self.assertEqual(d.unknown, ("power_source",))

    def test_invalid_values_are_unknown_not_trusted(self):
        for snap in (pg.PowerSnapshot(150, False, 0),
                     pg.PowerSnapshot(-1, False, 0),
                     pg.PowerSnapshot(50, False, 9)):
            d = pg.evaluate(snap)
            self.assertTrue(d.degraded, f"snap={snap}")
        self.assertFalse(pg.evaluate(pg.PowerSnapshot(150, False, 0)).throttle)

    def test_custom_thresholds(self):
        self.assertTrue(pg.evaluate(pg.PowerSnapshot(40, False, 0),
                                    battery_threshold=50).throttle)
        self.assertTrue(pg.evaluate(pg.PowerSnapshot(90, True, 1),
                                    thermal_threshold=1).throttle)


class TestBatteryParsing(unittest.TestCase):
    def test_ac_power_output(self):
        p, ac = pg.parse_battery(
            "Now drawing from 'AC Power'\n"
            " -InternalBattery-0 (id=1)\t80%; AC attached; not charging present: true")
        self.assertEqual((p, ac), (80, True), "「not charging」不能误判成在充电状态里")

    def test_battery_power_output(self):
        p, ac = pg.parse_battery(
            "Now drawing from 'Battery Power'\n"
            " -InternalBattery-0 (id=1)\t18%; discharging; 1:23 remaining present: true")
        self.assertEqual((p, ac), (18, False))

    def test_missing_battery_is_none(self):
        self.assertEqual(pg.parse_battery("No batteries found"), (None, None))


class TestDockSpeedRules(unittest.TestCase):
    def test_valid_presets(self):
        for name, preset in pg.DOCK_PRESETS.items():
            if preset is None:
                continue
            ok, reason = pg.validate_dock_speed(*preset)
            self.assertEqual((ok, reason), (True, "ok"), f"preset={name}")

    def test_rejections_have_concrete_reasons(self):
        self.assertEqual(pg.validate_dock_speed("abc", 0.5)[1], "nonNumeric")
        self.assertEqual(pg.validate_dock_speed(-1, 0.5)[1], "delayOutOfRange")
        self.assertEqual(pg.validate_dock_speed(9, 0.5)[1], "delayOutOfRange")
        self.assertEqual(pg.validate_dock_speed(0, 0.01)[1], "modifierOutOfRange")
        self.assertEqual(pg.validate_dock_speed(0, 3)[1], "modifierOutOfRange")

    def test_parse_dock_value(self):
        self.assertEqual(pg.parse_dock_value("0.5\n"), 0.5)
        self.assertIsNone(pg.parse_dock_value("not a number"))
        self.assertIsNone(pg.parse_dock_value("nan"))

    def test_apply_plan_backs_up_both_keys(self):
        backup, commands = pg.build_apply_plan(0, 0.15,
                                               {pg.DELAY_KEY: 0.5, pg.SPEED_KEY: None})
        self.assertEqual(backup, {pg.DELAY_KEY: 0.5, pg.SPEED_KEY: None})
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(c[0:3] == ["defaults", "write", pg.DOMAIN] for c in commands))
        self.assertIn("-float", commands[0])

    def test_restore_deletes_keys_that_never_existed(self):
        commands = pg.build_restore_plan({pg.DELAY_KEY: None, pg.SPEED_KEY: 0.75})
        self.assertEqual(commands[0], ["defaults", "delete", pg.DOMAIN, pg.DELAY_KEY])
        self.assertEqual(commands[1][2:4], [pg.DOMAIN, pg.SPEED_KEY])
        self.assertEqual(commands[1][4:], ["-float", "0.75"])

    def test_invalid_speed_raises_with_reason(self):
        with self.assertRaises(ValueError) as ctx:
            pg.build_apply_plan(0, 0.001, {})
        self.assertEqual(str(ctx.exception), "modifierOutOfRange")


class FakeRunner:
    """记录每次调用；可让指定命令前缀失败。"""

    def __init__(self, readings=None, fail_on=None):
        self.calls = []
        self.readings = readings or {}
        self.fail_on = fail_on or {}

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[1] == "read":
            key = argv[3]
            if key in self.readings:
                return 0, self.readings[key], ""
            return 1, "", "The domain/default pair does not exist"
        for prefix, (rc, err) in self.fail_on.items():
            if argv[:len(prefix)] == list(prefix):
                return rc, "", err
        return 0, "", ""

    def writes(self):
        return [c for c in self.calls if c[:2] == ["defaults", "write"]]

    def deletes(self):
        return [c for c in self.calls if c[:2] == ["defaults", "delete"]]

    def dock_restarts(self):
        return [c for c in self.calls if c[:2] == ["killall", "Dock"]]


class TestExecutionLayer(unittest.TestCase):
    def test_apply_preset_writes_and_restarts_dock(self):
        with mock.patch.object(pg, "IS_MAC", True):
            runner = FakeRunner()
            ok, detail, backup = pg.apply_preset("instant", runner)
        self.assertTrue(ok, detail)
        self.assertEqual(len(runner.writes()), 2)
        self.assertEqual(len(runner.dock_restarts()), 1)
        self.assertEqual(backup, {pg.DELAY_KEY: None, pg.SPEED_KEY: None})

    def test_apply_preset_refuses_when_originals_cannot_be_read(self):
        runner = FakeRunner()
        original = runner.__call__

        def flaky(argv, timeout):
            if argv[1] == "read":
                return -1, "", "timed out"
            return original(argv, timeout)

        with mock.patch.object(pg, "IS_MAC", True):
            ok, detail, backup = pg.apply_preset("fast", flaky)
        self.assertFalse(ok)
        self.assertIn("无法备份原值", detail)
        self.assertIsNone(backup)
        self.assertEqual(runner.writes(), [], "读不到原值却写了系统 = 不可逆，必须拒绝")
        self.assertEqual(runner.dock_restarts(), [])

    def test_write_failure_does_not_restart_dock(self):
        with mock.patch.object(pg, "IS_MAC", True):
            runner = FakeRunner(fail_on={("defaults", "write"): (1, "permission denied")})
            ok, detail, _backup = pg.apply_preset("fast", runner)
        self.assertFalse(ok)
        self.assertIn("permission denied", detail)
        self.assertEqual(runner.dock_restarts(), [])

    def test_restore_uses_delete_for_absent_keys(self):
        with mock.patch.object(pg, "IS_MAC", True):
            runner = FakeRunner()
            ok, _detail = pg.restore({pg.DELAY_KEY: None, pg.SPEED_KEY: 0.5}, runner)
        self.assertTrue(ok)
        self.assertEqual(len(runner.deletes()), 1)
        self.assertEqual(len(runner.writes()), 1)
        self.assertEqual(len(runner.dock_restarts()), 1)

    def test_restore_refuses_without_backup(self):
        with mock.patch.object(pg, "IS_MAC", True):
            runner = FakeRunner()
            ok, detail = pg.restore(None, runner)
        self.assertFalse(ok)
        self.assertIn("没有有效的原值备份", detail)
        self.assertEqual(runner.calls, [])

    def test_unknown_preset_and_default_do_not_touch_system(self):
        with mock.patch.object(pg, "IS_MAC", True):
            for name in ("bogus", "default"):
                runner = FakeRunner()
                ok, _detail, _backup = pg.apply_preset(name, runner)
                self.assertFalse(ok)
                self.assertEqual(runner.calls, [], f"{name} 不该碰系统")

    def test_non_mac_is_noop(self):
        with mock.patch.object(pg, "IS_MAC", False):
            runner = FakeRunner()
            self.assertFalse(pg.apply_preset("fast", runner)[0])
            self.assertEqual(pg.collect_snapshot(runner), pg.PowerSnapshot(None, None, None))
            self.assertEqual(runner.calls, [])

    def test_collect_snapshot_uses_runner_only_for_reading(self):
        sample = ("Now drawing from 'Battery Power'\n"
                  " -InternalBattery-0 (id=1)\t17%; discharging; present: true")
        runner = FakeRunner()
        with mock.patch.object(pg, "IS_MAC", True), \
             mock.patch.object(pg, "read_thermal_state", return_value=2):
            snap = pg.collect_snapshot(lambda argv, timeout: (0, sample, "")
                                       if argv[:2] == ["pmset", "-g"]
                                       else runner(argv, timeout))
        self.assertEqual(snap.battery_percent, 17)
        self.assertFalse(snap.on_ac_power)
        self.assertEqual(snap.thermal_state, 2)
        self.assertEqual(runner.writes(), [])


class TestSourceContracts(unittest.TestCase):
    def test_no_implicit_system_write(self):
        text = read("power_guard.py")
        self.assertIn("def apply_preset(", text)
        self.assertIn("def restore(", text)
        # 模块级不能自己调用执行层；只有 UI 显式回调能触发。
        body = text.split("# ── 执行层")[1]
        self.assertNotIn("apply_preset(", body.split("def apply_preset")[0])
        self.assertIn("读不到原值就不能改", body)

    def test_ui_is_wired(self):
        text = read("ui.py")
        self.assertIn("import power_guard", text)
        self.assertIn("self._build_power_guard(self._tab_settings)", text)
        self.assertIn("power_guard.evaluate(", text)
        self.assertIn("power_guard.collect_snapshot(", text)

    def test_throttle_actually_changes_runtime_behaviour(self):
        text = read("ui.py")
        self.assertIn("throttle = self._power_guard_tick()", text,
                      "主轮询必须真的调用守护，不能只是显示一个状态")
        self.assertIn("self.root.after(5000, self._poll_rules)", text,
                      "收手时必须降频，否则只是改了个标签")
        self.assertIn('if getattr(self, "_power_throttle", False):', text,
                      "壁纸轮换必须真的跳过切换")

    def test_default_setting_is_off(self):
        text = read("ui.py")
        self.assertIn('"power_guard_enabled", False', text,
                      "省电守护默认必须关闭，不能升级后悄悄改变用户行为")


if __name__ == "__main__":
    unittest.main()
