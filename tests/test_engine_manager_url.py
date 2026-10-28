"""Engine -> Control event delivery.

The engine lives in its own network namespace; the only host address it can reach is its
veth peer, 10.88.<line>.1. A host.docker.internal URL saved by a Docker install would make
every event time out while notify.py swallowed the error, so it is ignored.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from control.app import config

ROOT = Path(__file__).resolve().parents[1]

LEGACY = "https://host.docker.internal:8443"


class ManagerUrlTests(unittest.TestCase):
    def url(self, settings, env, iid="2"):
        with patch.dict(os.environ, env, clear=False):
            if "MDD_MANAGER_URL" not in env:
                os.environ.pop("MDD_MANAGER_URL", None)
            return config._engine_manager_url(settings, iid)

    def test_default_is_the_lines_veth_peer_on_the_control_port(self):
        self.assertEqual(self.url({}, {}), "https://10.88.2.1:8443")
        self.assertEqual(self.url({"http_port": 9443}, {}, iid="1"), "https://10.88.1.1:9443")

    def test_explicit_setting_then_environment_win(self):
        self.assertEqual(self.url({"manager_url": "https://10.0.0.5:9443"},
                                  {"MDD_MANAGER_URL": "https://e:1"}), "https://10.0.0.5:9443")
        self.assertEqual(self.url({}, {"MDD_MANAGER_URL": "https://e:1"}), "https://e:1")

    def test_a_saved_docker_url_is_ignored(self):
        self.assertEqual(self.url({"manager_url": LEGACY}, {}), "https://10.88.2.1:8443")

    def test_rendered_into_instance_json(self):
        inst = {"id": "1", "imsi": "001010000000000", "mcc": "001", "mnc": "01",
                "ami_secret": "a", "sip": {"webrtc": {"enable": True, "password": "p"}}}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MDD_MANAGER_URL", None)
            with patch.object(config, "internal_event_token", return_value="t"):
                rendered = config.render_instance_json(inst, {"http_port": 8443})
        self.assertEqual(rendered["manager_url"], "https://10.88.1.1:8443")


def _load_notify():
    spec = importlib.util.spec_from_file_location("engine_notify_under_test",
                                                  ROOT / "engine" / "notify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NotifyDeliveryTests(unittest.TestCase):
    def run_notify(self, post):
        notify = _load_notify()
        requests = types.ModuleType("requests")
        requests.post = post
        urllib3 = types.ModuleType("urllib3")
        urllib3.disable_warnings = lambda *a, **k: None
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(sys.modules, {"requests": requests, "urllib3": urllib3}), \
                patch.dict(os.environ, {"MANAGER_URL": LEGACY, "MDD_ID": "5",
                                        "MDD_ENV": str(Path(temp) / "none")}), \
                patch.object(notify.os, "makedirs"), \
                patch("builtins.open", side_effect=OSError), \
                patch.object(sys, "argv", ["notify.py", "sms_in", "+44700", "aGk="]), \
                redirect_stderr(err):
            notify.main()
        return err.getvalue()

    def test_an_unreachable_manager_is_reported_not_swallowed(self):
        def post(*a, **k):
            raise TimeoutError("timed out")
        out = self.run_notify(post)
        self.assertIn("event not delivered", out)
        self.assertIn("sms_in", out)
        self.assertIn(LEGACY, out)

    def test_a_rejected_event_is_reported(self):
        out = self.run_notify(MagicMock(return_value=MagicMock(status_code=401)))
        self.assertIn("HTTP 401", out)

    def test_a_delivered_event_is_silent(self):
        post = MagicMock(return_value=MagicMock(status_code=200))
        self.assertEqual(self.run_notify(post), "")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(json.loads(json.dumps(payload))["event"], "sms_in")


if __name__ == "__main__":
    unittest.main()
