import os
import unittest
from unittest.mock import patch

from control.app import lpa


class PackagedLpacTests(unittest.TestCase):
    def test_packaged_binary_replaces_the_absent_legacy_data_binary(self):
        legacy = os.path.join(lpa.cfg.DATA_DIR, "lpac", "lpac")
        with patch.object(lpa.cfg, "get_settings", return_value={
                "esim": {"lpac_bin": legacy}}), \
                patch.object(lpa.os.path, "isfile",
                             side_effect=lambda path: path == "/usr/local/bin/lpac"):
            self.assertEqual(lpa.lpac_bin(), "/usr/local/bin/lpac")

    def test_the_release_binary_from_the_unit_environment_is_found(self):
        """1.14.0-slim.13: the page said "lpac is not installed" with lpac in /opt/mdd/current."""
        legacy = os.path.join(lpa.cfg.DATA_DIR, "lpac", "lpac")
        packaged = "/opt/mdd/current/lpac/lpac"
        with patch.dict(lpa.os.environ, {"MDD_LPAC_BIN": packaged}), \
                patch.object(lpa.cfg, "get_settings", return_value={
                    "esim": {"lpac_bin": legacy}}), \
                patch.object(lpa.os.path, "isfile", side_effect=lambda path: path == packaged):
            self.assertEqual(lpa.lpac_bin(), packaged)
        with patch.dict(lpa.os.environ, {"MDD_LPAC_BIN": packaged}), \
                patch.object(lpa.cfg, "get_settings", return_value={}), \
                patch.object(lpa.os.path, "isfile", side_effect=lambda path: path == packaged), \
                patch.object(lpa.os, "access", return_value=True):
            self.assertEqual(lpa.lpac_bin(), packaged)
            self.assertTrue(lpa.lpac_available())

    def test_an_explicit_custom_binary_remains_authoritative(self):
        custom = "/opt/company/lpac"
        with patch.object(lpa.cfg, "get_settings", return_value={
                "esim": {"lpac_bin": custom}}), \
                patch.object(lpa.os.path, "isfile", return_value=False):
            self.assertEqual(lpa.lpac_bin(), custom)


if __name__ == "__main__":
    unittest.main()
