#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""keep_awake 的单测：模式计划、满足判据、断言名解析、系统状态读取。

防休眠是**会改系统全局状态**的功能（pmset -a disablesleep 1），所以这里重点
盯三件不能错的事：

1. **plan 的档位语义**。.systemOnly 在合盖时必须自动升级持 display 断言——
   合盖后 macOS 会在外接屏关闭 5 秒强制进 Clamshell Sleep，即使 sleep = 0。
2. **退出必须还原**。disablesleep 是本程序设的才还原，不是本程序设的绝不碰
   （用户自己开的不能被杀掉）。
3. **断言名不能解析错**。pmset 输出里 `pid 123(App): [0x...] 00:00:00 Type`
   同时含冒号和方括号，按冒号切会拿到断言 ID 而不是类型名——那样校验永远
   判「未持上」，界面就一直报失败（实测踩过）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keep_awake as ka  # noqa: E402


class TestPlan(unittest.TestCase):
    """档位 → 断言集合 / 是否改系统级。"""

    def test_off_holds_nothing(self):
        p = ka.plan(ka.MODE_OFF)
        self.assertFalse(p["active"])
        self.assertEqual(p["assertions"], ())
        self.assertFalse(p["disable_clamshell_sleep"])

    def test_system_without_clamshell(self):
        p = ka.plan(ka.MODE_SYSTEM, clamshell=False)
        self.assertTrue(p["active"])
        self.assertEqual(set(p["assertions"]),
                         {ka._A_IDLE_SYSTEM, ka._A_SYSTEM})
        self.assertFalse(p["disable_clamshell_sleep"])

    def test_system_upgrades_when_clamshell(self):
        """合盖时 .systemOnly 必须升级持 display 断言。"""
        p = ka.plan(ka.MODE_SYSTEM, clamshell=True)
        self.assertIn(ka._A_IDLE_DISPLAY, p["assertions"])

    def test_display_always_holds_display(self):
        for cl in (True, False):
            p = ka.plan(ka.MODE_DISPLAY, clamshell=cl)
            self.assertIn(ka._A_IDLE_DISPLAY, p["assertions"])

    def test_clamshell_needs_system_setting(self):
        p = ka.plan(ka.MODE_CLAMSHELL)
        self.assertTrue(p["disable_clamshell_sleep"])
        self.assertIn(ka._A_IDLE_SYSTEM, p["assertions"])

    def test_unknown_mode_falls_back(self):
        self.assertEqual(ka.plan("bogus"), ka.plan(ka.MODE_OFF))

    def test_expected_assertions_drops_empty(self):
        """元组里的空串要滤掉（.systemOnly 未合盖时带一个 ()）。"""
        p = ka.plan(ka.MODE_SYSTEM, clamshell=False)
        self.assertEqual(ka.expected_assertions(p),
                         (ka._A_IDLE_SYSTEM, ka._A_SYSTEM))


class TestSatisfied(unittest.TestCase):
    def test_all_present(self):
        p = ka.plan(ka.MODE_SYSTEM)
        self.assertTrue(ka.satisfied(p, [ka._A_IDLE_SYSTEM, ka._A_SYSTEM], False))

    def test_missing_assertion(self):
        p = ka.plan(ka.MODE_SYSTEM)
        self.assertFalse(ka.satisfied(p, [ka._A_IDLE_SYSTEM], False))

    def test_clamshell_needs_sleep_disabled(self):
        p = ka.plan(ka.MODE_CLAMSHELL)
        self.assertFalse(ka.satisfied(
            p, [ka._A_IDLE_SYSTEM, ka._A_SYSTEM, ka._A_IDLE_DISPLAY], False))
        self.assertTrue(ka.satisfied(
            p, [ka._A_IDLE_SYSTEM, ka._A_SYSTEM, ka._A_IDLE_DISPLAY], True))

    def test_off_is_always_satisfied(self):
        self.assertTrue(ka.satisfied(ka.plan(ka.MODE_OFF), [], False))


class TestParseAssertions(unittest.TestCase):
    """pmset 输出解析：必须拿到类型名，不能拿到断言 ID。"""

    def setUp(self):
        self._orig_run = ka._run
        self._orig_getpid = os.getpid

    def tearDown(self):
        ka._run = self._orig_run
        os.getpid = self._orig_getpid

    def _fake(self, text, pid=4242):
        os.getpid = lambda: pid
        ka._run = lambda cmd, timeout=8: (0, text)

    def test_types_not_ids(self):
        """回归：按冒号切会拿到 [0x...] 里的 ID，必须按类型名匹配。"""
        self._fake(
            "Assertion status system-wide:\n"
            "   pid 4242(Python): [0x0000feb90001a0f1] 00:00:00 "
            "PreventUserIdleSystemSleep named: \"\"\n"
            "   pid 4242(Python): [0x0000feb90005a0f0] 00:00:00 "
            "PreventUserIdleDisplaySleep named: \"\"\n"
            "   pid 999(Other): [0x0000fead0001a0e5] 00:00:11 "
            "PreventUserIdleSystemSleep named: \"Installing\"\n")
        got = ka.active_assertion_types()
        self.assertEqual(got, {ka._A_IDLE_SYSTEM, ka._A_IDLE_DISPLAY})
        for t in got:
            self.assertNotIn("0x", t, "把断言 ID 当成类型名了")

    def test_other_pids_ignored(self):
        self._fake(
            "   pid 999(Other): [0x1] 00:00:11 PreventSystemSleep named: \"x\"\n")
        self.assertEqual(ka.active_assertion_types(), set())

    def test_no_match_returns_empty(self):
        self._fake("garbage output")
        self.assertEqual(ka.active_assertion_types(), set())

    def test_run_failure_returns_empty(self):
        ka._run = lambda cmd, timeout=8: (1, "")
        self.assertEqual(ka.active_assertion_types(), set())


class TestCfBool(unittest.TestCase):
    """CFProperty -> bool：取不到要返回 None，不能与 false 混淆。"""

    def test_none_ref(self):
        self.assertIsNone(ka._cf_bool(None))

    def test_non_mac_returns_none(self):
        """非 macOS 上 _cf 为 None，所有状态读取返回 None（不适用）。"""
        if ka.IS_MAC:
            self.skipTest("仅非 macOS 平台验证")
        self.assertIsNone(ka.clamshell_state())
        self.assertIsNone(ka.sleep_disabled_state())


class TestKeepAwakeApply(unittest.TestCase):
    """编排层：非 macOS / 加载失败时必须安全 no-op，不能崩。"""

    def test_apply_off_is_ok(self):
        g = ka.KeepAwake()
        ok, text, detail = g.apply(ka.MODE_OFF)
        self.assertTrue(ok)
        self.assertEqual(detail.get("held"), [])

    def test_release_all_safe(self):
        g = ka.KeepAwake()
        g.release_all()          # 没持过任何断言也要能调
        self.assertEqual(g.status().get("held"), [])

    def test_unknown_mode_treated_as_off(self):
        g = ka.KeepAwake()
        ok, _text, _d = g.apply("bogus")
        self.assertTrue(ok)

    def test_does_not_touch_foreign_disablesleep(self):
        """不是本程序设的 disablesleep，绝不能去改它。

        守在两处：① _restore_disablesleep_if_needed 开头就看
        _did_set_disablesleep，不是自己设的直接 return；② release_all 走的
        是那个方法，不会绕过。这里把真正会碰系统的 _set_sleep_disabled 换成
        记录器，验证一次都没被调用。
        """
        g = ka.KeepAwake()
        self.assertFalse(g._did_set_disablesleep)
        calls = []
        g._set_sleep_disabled = lambda disable, allow_prompt=False: \
            calls.append(disable) or (False, False)
        g.release_all()
        self.assertEqual(calls, [], "试图修改不是自己设的系统级禁止睡眠")

    def test_clamshell_claims_existing_disablesleep(self):
        """clamshell 档发现 SleepDisabled 已开时也要认领，退出才归还。

        真机闭环验证时踩到：别的会话（或本程序上一次没还原的实例）把
        SleepDisabled 置成 1 后，apply 走「已经是开的、不用再设」分支，
        _did_set_disablesleep 不会被置位——release_all 判「不是自己设的」
        直接跳过，系统级「禁止睡眠」永久留在用户机器上。认领之后退出时
        会还原（还原动作本身有「系统确实还开着」这道守门，不误关别人
        正在用的开关之外的状态）。
        """
        g = ka.KeepAwake()
        self.assertFalse(g._did_set_disablesleep)
        self._orig_sd = ka.sleep_disabled_state
        self._orig_set = ka._set_sleep_disabled
        self._orig_cs = ka.clamshell_state
        self._orig_aa = ka.active_assertion_types
        self._orig_sync = ka.KeepAwake._sync_assertions
        self._orig_satis = ka.satisfied
        # sleep_disabled=True 让 apply 走「已开」分支
        ka.sleep_disabled_state = lambda: True
        ka.clamshell_state = lambda: False
        ka.active_assertion_types = lambda: set(ka._ASSERTION_TYPES)
        ka.KeepAwake._sync_assertions = lambda self, types: None
        ka.satisfied = lambda p, active, sd: True
        try:
            ok, _text, detail = g.apply(ka.MODE_CLAMSHELL, allow_prompt=False)
        finally:
            ka.sleep_disabled_state = self._orig_sd
            ka._set_sleep_disabled = self._orig_set
            ka.clamshell_state = self._orig_cs
            ka.active_assertion_types = self._orig_aa
            ka.KeepAwake._sync_assertions = self._orig_sync
            ka.satisfied = self._orig_satis
        self.assertTrue(ok)
        self.assertTrue(g._did_set_disablesleep,
                        "SleepDisabled 已开时没认领，退出将不会还原")

    def test_restores_own_disablesleep(self):
        """自己设的、且系统里还开着，才去还原。

        注意要 patch **模块级** _set_sleep_disabled（方法体是 LOAD_GLOBAL 找它），
        patch 实例属性不生效——那会让测试走到真实的 sudo pmset 上。
        """
        g = ka.KeepAwake()
        g._did_set_disablesleep = True
        self._orig_sd = ka.sleep_disabled_state
        self._orig_set = ka._set_sleep_disabled
        ka.sleep_disabled_state = lambda: True
        calls = []
        ka._set_sleep_disabled = lambda disable, allow_prompt=False: \
            calls.append(disable) or (True, False)
        try:
            ok = g._restore_disablesleep_if_needed()
        finally:
            ka.sleep_disabled_state = self._orig_sd
            ka._set_sleep_disabled = self._orig_set
        self.assertTrue(ok)
        self.assertEqual(calls, [False], "自己设的系统级禁止睡眠没被还原")
        self.assertFalse(g._did_set_disablesleep, "还原后应清掉自己的标记")

    def test_no_restore_when_system_already_off(self):
        """系统本来就没开：不调 pmset（没必要，也免得多弹一次授权）。"""
        g = ka.KeepAwake()
        g._did_set_disablesleep = True
        self._orig_sd = ka.sleep_disabled_state
        self._orig_set = ka._set_sleep_disabled
        ka.sleep_disabled_state = lambda: False
        calls = []
        ka._set_sleep_disabled = lambda disable, allow_prompt=False: \
            calls.append(disable) or (True, False)
        try:
            ok = g._restore_disablesleep_if_needed()
        finally:
            ka.sleep_disabled_state = self._orig_sd
            ka._set_sleep_disabled = self._orig_set
        self.assertFalse(ok)
        self.assertEqual(calls, [], "系统没开还去调 pmset 了")
        self.assertFalse(g._did_set_disablesleep)


class TestTexts(unittest.TestCase):
    """失败说明必须指出缺什么，不能只说「失败」。"""

    def test_fail_text_lists_missing(self):
        detail = {"want": [ka._A_IDLE_SYSTEM, ka._A_SYSTEM],
                  "active": [ka._A_SYSTEM],
                  "sleep_disabled": False}
        text = ka._fail_text(ka.MODE_SYSTEM, detail)
        self.assertIn(ka._A_IDLE_SYSTEM, text)
        self.assertIn("未持上", text)

    def test_fail_text_mentions_auth(self):
        detail = {"want": [ka._A_IDLE_DISPLAY], "active": [],
                  "sleep_disabled": False}
        text = ka._fail_text(ka.MODE_CLAMSHELL, detail)
        self.assertIn("系统级禁止睡眠未开启", text)


if __name__ == "__main__":
    unittest.main()
