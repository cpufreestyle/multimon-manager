#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包时保留用户运行时数据。

`build_macos` 会先 rmtree 整个 .app 再重建，而 settings.json / layouts.json /
quad_cache.json 这些**只存在于包内**（settings.data_dir() 在非 frozen 模式
下就是 Resources 目录）。不搬的话每次重新打包都把用户偏好、布局方案、最小
尺寸学习缓存一起清空——真机上表现为「升级后设置全没了」。

这个用例锁住两条：
1. 构建前的快照能读到全部用户数据文件；
2. 构建后的恢复会把快照写回新包，且内容逐字节一致。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build  # noqa: E402


class TestUserDataPreservedAcrossBuild(unittest.TestCase):
    def test_snapshot_then_restore_roundtrip(self):
        """快照 -> 清空目录（模拟 rmtree）-> 恢复，内容逐字节一致。"""
        payloads = {
            "settings.json": b'{"hk_enabled": true, "window_gap": 12}',
            "layouts.json": b'{"L1": [1, 2, 3]}',
            "quad_cache.json": b'{"mins": {"A::1": [600, 400]}}',
        }
        with tempfile.TemporaryDirectory() as old, \
                tempfile.TemporaryDirectory() as new:
            for name, blob in payloads.items():
                with open(os.path.join(old, name), "wb") as f:
                    f.write(blob)

            saved = build._snapshot_user_data(old)
            # 新建的包目录是空的（模拟 rmtree 之后的重建）
            restored = build._restore_user_data(saved, new)

            self.assertEqual(sorted(restored), sorted(payloads))
            for name, blob in payloads.items():
                with open(os.path.join(new, name), "rb") as f:
                    self.assertEqual(f.read(), blob,
                                     "%s 恢复后内容不一致" % name)

    def test_missing_files_are_skipped(self):
        """文件不存在时快照为空，且不能让构建失败。"""
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(build._snapshot_user_data(empty), {})
        # 空快照恢复同样不报错
        with tempfile.TemporaryDirectory() as res:
            self.assertEqual(build._restore_user_data({}, res), [])

    def test_restore_overwrites_stale_template(self):
        """新包自带同名模板时，用户实测值必须覆盖它。

        旧构建曾把仓库里的样例 settings.json 带进包，若不覆盖，用户恢复后
        看到的仍是默认值。
        """
        with tempfile.TemporaryDirectory() as res:
            with open(os.path.join(res, "settings.json"), "wb") as f:
                f.write(b'{"window_gap": 12}')
            build._restore_user_data({"settings.json": b'{"window_gap": 20}'},
                                     res)
            with open(os.path.join(res, "settings.json"), "rb") as f:
                self.assertEqual(f.read(), b'{"window_gap": 20}')

    def test_user_data_list_covers_runtime_files(self):
        """清单要覆盖真机上实际产生的全部运行时数据文件。"""
        for name in ("settings.json", "layouts.json", "profiles.json",
                     "stash.json", "quad_cache.json", "rehome.json"):
            self.assertIn(name, build.USER_DATA_FILES,
                          "%s 不在保留清单里：升级会丢" % name)


if __name__ == "__main__":
    unittest.main()
