import tempfile
import unittest
import yaml
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

    def test_line_past_the_limit_is_refused_but_existing_lines_remain_editable(self):
        self.assertEqual(config.MAX_SIM_LINES, 10)
        temp, paths = self.temp_config()
        with temp, paths:
            for iid in range(1, config.MAX_SIM_LINES + 1):
                config.upsert_instance({"id": str(iid), "name": f"SIM {iid}"})
            extra = str(config.MAX_SIM_LINES + 1)
            with self.assertRaises(config.LineLimitError):
                config.upsert_instance({"id": extra, "name": f"SIM {extra}"})
            edited = config.upsert_instance({"id": "5", "name": "kept"})
            self.assertEqual(edited["name"], "kept")

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


    def test_only_the_first_legacy_lines_up_to_the_limit_are_startable(self):
        temp, paths = self.temp_config()
        limit = config.MAX_SIM_LINES
        with temp, paths:
            config.save({"instances": {
                str(iid): {"id": str(iid), "index": iid}
                for iid in range(1, limit + 3)
            }})
            self.assertTrue(config.line_allowed(str(limit)))
            self.assertFalse(config.line_allowed(str(limit + 1)))


if __name__ == "__main__":
    unittest.main()
