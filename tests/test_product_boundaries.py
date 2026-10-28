import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import config


class ProductBoundaryTests(unittest.TestCase):
    def temp_config(self):
        temp = tempfile.TemporaryDirectory()
        paths = patch.multiple(
            config,
            DATA_DIR=temp.name,
            CONFIG_PATH=str(Path(temp.name) / "config.yaml"),
        )
        return temp, paths

    def test_the_ceiling_is_ten_and_the_default_one(self):
        # A source-level ceiling: no setting can turn the gateway into a bulk-SIM service.
        # Ten since 2026-10-28 (the pre-slim ceiling, at the operator's request); the engine
        # side (mdd-enginectl, netns 10.88.N, address ranges) has always covered lines 1-10.
        self.assertEqual(config.PRODUCT_MAX_LINES, 10)
        self.assertEqual(config.DEFAULT_MAX_LINES, 1)
        self.assertEqual(config.DEFAULTS["settings"]["max_lines"], 1)
        for raw, expected in ((None, 1), (1, 1), (2, 2), (3, 3), (10, 10), (11, 10), (99, 10),
                              (0, 1), (-1, 1), ("2", 2), ("x", 1)):
            settings = {} if raw is None else {"max_lines": raw}
            with self.subTest(raw=raw):
                self.assertEqual(config.max_lines(settings), expected)

    def test_line_past_the_limit_is_refused_but_existing_lines_remain_editable(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.upsert_instance({"id": "1", "name": "SIM 1"})
            with self.assertRaises(config.LineLimitError):
                config.upsert_instance({"id": "2", "name": "SIM 2"})
            self.assertEqual(config.upsert_instance({"id": "1", "name": "kept"})["name"], "kept")
            config.update_settings({"max_lines": 2})
            config.upsert_instance({"id": "2", "name": "SIM 2"})
            with self.assertRaises(config.LineLimitError):
                config.upsert_instance({"id": "3", "name": "SIM 3"})
            # Even a hand-edited setting above the ceiling allows no more than ten.
            config.update_settings({"max_lines": 99})
            for iid in range(3, 11):
                config.upsert_instance({"id": str(iid), "name": f"SIM {iid}"})
            with self.assertRaises(config.LineLimitError):
                config.upsert_instance({"id": "11", "name": "SIM 11"})

    def test_stale_remote_controls_are_removed_on_load_and_save(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({
                "settings": {},
                "instances": {"1": {"id": "1", "sip": {
                    "external": [{"username": "remote", "password": "secret"}]}}},
            })
            loaded = config.load()
            self.assertEqual(loaded["instances"]["1"]["sip"]["external"], [])

            saved = config.upsert_instance({"id": "1", "sip": {
                "external": [{"username": "remote", "password": "secret"}]}})
            self.assertEqual(saved["sip"]["external"], [])


    def test_explicit_empty_feishu_channels_do_not_restore_legacy_channel(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({"settings": {"feishu": {
                "enabled": True,
                "url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
                "channels": [],
            }}, "instances": {}})
            self.assertEqual(config.load()["settings"]["feishu"]["channels"], [])


    def test_only_the_first_lines_up_to_the_limit_are_startable(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({"settings": {}, "instances": {
                str(iid): {"id": str(iid), "index": iid} for iid in range(1, 5)}})
            self.assertTrue(config.line_allowed("1"))
            self.assertFalse(config.line_allowed("2"))
            config.update_settings({"max_lines": 2})
            self.assertTrue(config.line_allowed("2"))
            self.assertFalse(config.line_allowed("3"))


if __name__ == "__main__":
    unittest.main()
