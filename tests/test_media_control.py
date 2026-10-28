"""Browser call media: direct mode only (docs/SLIM_PLAN.md D4)."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from control.app import main, media
from control.app.routers import softphone as softphone_routes

INSTANCE = {"id": "1", "mcc": "001", "mnc": "01",
            "sip": {"webrtc": {"enable": True, "username": "webrtc", "password": "p"}}}


def _request(host="gw.example:10443"):
    return SimpleNamespace(headers={"host": host},
                           url=SimpleNamespace(hostname=host.rsplit(":", 1)[0]))


class MediaControlTests(unittest.TestCase):
    def test_media_status_is_direct(self):
        self.assertEqual(softphone_routes.api_media(), {"mode": "direct"})
        self.assertEqual(media.mode(), media.DIRECT)

    def test_direct_mode_provisioning_carries_no_ice_servers(self):
        with patch.object(main.cfg, "get_instance", return_value=INSTANCE):
            prov = softphone_routes.api_softphone("1", _request())
        self.assertEqual(prov["media_mode"], "direct")
        self.assertEqual(prov["ice_servers"], [])
        self.assertEqual(prov["ice_transport_policy"], "all")
        self.assertIsNone(prov["relay_ready"])
        self.assertEqual(prov["ws_path"], "/api/instances/1/softphone/ws")

    def test_no_relay_machinery_is_left(self):
        for name in ("RELAY", "load_state", "relay_status", "supervise", "enable",
                     "engine_attachment"):
            self.assertFalse(hasattr(media, name), name)


if __name__ == "__main__":
    unittest.main()
