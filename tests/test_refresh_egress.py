"""POST /api/egress/refresh: an undeletable cache file is no reason for a 500.

Seen on 1.14.0-slim.14: the orchestrator (root) had created orchestrator/subscriptions as root,
so the control plane (user mdd) got PermissionError removing the cached subscription.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import config as cfg
from control.app import egress
from control.app import state as app_state


class RefreshEgressTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cache = self.root / "orchestrator" / "subscriptions"
        self.cache.mkdir(parents=True)
        for name in ("a.yaml", "b.yaml", "keep.txt"):
            (self.cache / name).write_text("x")
        patcher = patch.object(cfg, "DATA_DIR", str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        publish = patch.object(egress, "publish")
        self.publish = publish.start()
        self.addCleanup(publish.stop)

    def test_cached_subscriptions_are_removed_and_the_exit_republished(self):
        result = app_state.refresh_egress()
        self.assertEqual(sorted(os.listdir(self.cache)), ["keep.txt"])
        self.assertTrue(result["ok"])
        self.assertNotIn("kept", result)
        self.publish.assert_called_once_with()

    def test_an_undeletable_cache_does_not_fail_the_refresh(self):
        real_remove = os.remove

        def remove(path):
            if str(path).endswith("a.yaml"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_remove(path)

        with patch.object(app_state.os, "remove", side_effect=remove):
            result = app_state.refresh_egress()
        self.assertEqual(result["kept"], ["a.yaml"])
        self.assertFalse((self.cache / "b.yaml").exists())      # the others still go
        self.publish.assert_called_once_with()

    def test_no_cache_directory_is_fine(self):
        for item in self.cache.iterdir():
            item.unlink()
        self.cache.rmdir()
        self.assertTrue(app_state.refresh_egress()["ok"])


if __name__ == "__main__":
    unittest.main()
