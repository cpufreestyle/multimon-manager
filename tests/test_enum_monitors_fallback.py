"""monitors_mac.enum_monitors 的单测（mock CG，不依赖真机显示器状态）。

可由 `python3 -m unittest discover -s tests -p "test_*.py"` 收集。

覆盖：
- 0 屏时用主屏 bounds 兜底（真机 2026-09-20 锁屏/休眠场景）
- CGMainDisplayID 抛异常时的降级（不能抛穿到无 try 的 refresh_monitors）
- is_builtin 真实判定而非硬编码（体现为 device_name）
- 正常多屏路径不受影响 + 返回值必须深拷贝（回归保护）
"""
import ctypes
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import monitors_mac as mm  # noqa: E402


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Size(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _Rect(ctypes.Structure):
    _fields_ = [("origin", _Point), ("size", _Size)]


def _rect(x, y, w, h):
    r = _Rect()
    r.origin.x, r.origin.y = x, y
    r.size.width, r.size.height = w, h
    return r


class _FakeCG:
    """可控的 CG 桩。"""

    def __init__(self, count, bounds, builtin=True, main_id_error=False,
                 builtin_error=False):
        self._count = count
        self._bounds = bounds
        self._builtin = builtin
        self._main_id_error = main_id_error
        self._builtin_error = builtin_error

    def CGGetActiveDisplayList(self, n, ids, cnt):
        p = ctypes.cast(cnt, ctypes.POINTER(ctypes.c_uint32))
        p.contents.value = self._count
        if ids is not None and self._count:
            for i in range(self._count):
                ids[i] = 100 + i
        return 0

    def CGMainDisplayID(self):
        if self._main_id_error:
            raise RuntimeError("CGMainDisplayID 模拟失败")
        return 100

    def CGDisplayBounds(self, did):
        return self._bounds

    def CGDisplayIsBuiltin(self, did):
        if self._builtin_error:
            raise RuntimeError("CGDisplayIsBuiltin 模拟失败")
        return self._builtin if did == 100 else False


class TestEnumFallback(unittest.TestCase):
    def setUp(self):
        self._real_cg = mm._cg
        self._real_names = mm._parse_display_names
        mm._parse_display_names = lambda: []          # 不走 system_profiler
        mm._mon_cache["data"] = None
        mm._mon_cache["ts"] = 0.0

    def tearDown(self):
        mm._cg = self._real_cg
        mm._parse_display_names = self._real_names
        mm._mon_cache["data"] = None

    # ---------- 0 屏兜底 ----------
    def test_zero_displays_falls_back_to_main(self):
        mm._cg = _FakeCG(0, _rect(0, 0, 1512, 982))
        ms = mm.enum_monitors(force=True, work=False)
        self.assertEqual(len(ms), 1)
        self.assertEqual((ms[0].width, ms[0].height), (1512, 982))
        self.assertTrue(ms[0].is_primary)

    def test_zero_displays_and_empty_bounds_stays_empty(self):
        mm._cg = _FakeCG(0, _rect(0, 0, 0, 0))
        self.assertEqual(mm.enum_monitors(force=True, work=False), [])

    # ---------- CGMainDisplayID 异常必须降级，不能抛穿 ----------
    def test_main_id_error_with_zero_displays_returns_empty_not_raise(self):
        mm._cg = _FakeCG(0, _rect(0, 0, 1512, 982), main_id_error=True)
        self.assertEqual(mm.enum_monitors(force=True, work=False), [])

    def test_main_id_error_with_displays_falls_back_to_first_as_primary(self):
        """拿不到主屏 ID 时按第一块屏当主屏，不能让所有屏都丢 is_primary。"""
        mm._cg = _FakeCG(3, _rect(0, 0, 1512, 982), main_id_error=True)
        ms = mm.enum_monitors(force=True, work=False)
        self.assertEqual(len(ms), 3)
        self.assertTrue(any(m.is_primary for m in ms))

    # ---------- is_builtin 必须真实判定 ----------
    def test_fallback_is_builtin_reflects_real_value(self):
        """外接屏作主屏时不得被硬编码成内建屏。

        MonitorInfo 并不保存 is_builtin，它只参与显示名 fallback：
        内建 → "Built-in Display"，外接 → "Display N"。
        """
        mm._cg = _FakeCG(0, _rect(0, 0, 2560, 1440), builtin=False)
        ms = mm.enum_monitors(force=True, work=False)
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0].device_name, "Display 1")

    def test_fallback_is_builtin_true_names_builtin(self):
        """内建屏仍应显示 Built-in Display（保证没改坏）。"""
        mm._cg = _FakeCG(0, _rect(0, 0, 1512, 982), builtin=True)
        ms = mm.enum_monitors(force=True, work=False)
        self.assertEqual(ms[0].device_name, "Built-in Display")

    def test_builtin_error_does_not_break_enumeration(self):
        mm._cg = _FakeCG(2, _rect(0, 0, 1512, 982), builtin_error=True)
        ms = mm.enum_monitors(force=True, work=False)
        self.assertEqual(len(ms), 2)

    # ---------- 回归保护 ----------
    def test_normal_path_unchanged(self):
        mm._cg = _FakeCG(3, _rect(-1080, -1080, 1080, 1920))
        ms = mm.enum_monitors(force=True, work=False)
        self.assertEqual(len(ms), 3)
        self.assertEqual(ms[0].device_path, "100")
        self.assertEqual(ms[0].width, 1080)

    def test_result_is_deep_copy(self):
        mm._cg = _FakeCG(0, _rect(0, 0, 1512, 982))
        ms = mm.enum_monitors(force=True, work=False)
        ms[0].width = 999999
        again = mm.enum_monitors(force=False, work=False)
        self.assertEqual(again[0].width, 1512)


if __name__ == "__main__":
    unittest.main(verbosity=2)
