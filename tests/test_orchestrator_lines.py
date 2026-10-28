"""The orchestrator drives each line's network through host/mdd_netns.py (SLIM_PLAN 12.11-3)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from host import mdd_netns
from host import mdd_orchestrator as orch
from host.mdd_orchestrator import Orchestrator

LINE = {"id": "1", "enabled": True, "country": "us", "epdg": "epdg.example.net",
        "rtp": [10000, 10011]}
TROJAN = {"enabled": True, "profiles": {"t": {"name": "T", "type": "node",
                                              "value": "trojan://pw@t.example.net:443"}},
          "exits": {"us": {"enabled": True, "profile_id": "t"}}}


class _App:
    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Orchestrator(Path(self.temp.name), Path.cwd(), dry_run=True)
        return self.app

    def __exit__(self, *exc):
        self.temp.cleanup()

    def published(self):
        return json.loads((self.app.root / "proxy-status.json").read_text())


class ReconcileProxyTests(unittest.TestCase):
    def test_line_network_follows_the_exit_state(self):
        ctx = _App()
        with ctx as app, patch.object(Orchestrator, "reconcile_lines",
                                      return_value={"1": {"route": "tun", "interface": "mdd-us",
                                                          "netns": "mdd-1",
                                                          "netns_present": True,
                                                          "reason": ""}}) as lines:
            app.reconcile_proxy({"proxy": TROJAN, "lines": [LINE], "control_port": 8443})
            published = ctx.published()
        desired, enabled, exits = lines.call_args.args
        self.assertTrue(enabled)
        self.assertTrue(exits["us"]["ready"])
        state = published["lines"]["1"]
        self.assertTrue(state["ready"])
        self.assertEqual((state["route"], state["netns"]), ("tun", "mdd-1"))
        self.assertNotIn("routed_addresses", state)
        self.assertNotIn("_member_names", json.dumps(published))

    def test_blocked_line_is_not_ready_and_says_why(self):
        ctx = _App()
        with ctx as app, patch.object(Orchestrator, "reconcile_lines",
                                      return_value={"1": {"route": "blocked", "netns": "mdd-1",
                                                          "reason": "US exit interface is "
                                                                    "missing"}}):
            app.reconcile_proxy({"proxy": TROJAN, "lines": [LINE]})
            state = ctx.published()["lines"]["1"]
        self.assertFalse(state["ready"])
        self.assertEqual(state["error"], "US exit interface is missing")

    def test_proxy_off_still_reconciles_lines_as_direct(self):
        ctx = _App()
        with ctx as app, patch.object(Orchestrator, "reconcile_lines",
                                      return_value={"1": {"route": "direct"}}) as lines:
            app.reconcile_proxy({"proxy": {"enabled": False}, "lines": [LINE]})
            published = ctx.published()
        self.assertFalse(lines.call_args.args[1])
        self.assertFalse(published["enabled"])
        self.assertTrue(published["lines"]["1"]["ready"])
        self.assertEqual(published["lines"]["1"]["route"], "direct")

    def test_a_failed_singbox_blocks_the_lines(self):
        ctx = _App()
        with ctx as app, \
                patch.object(Orchestrator, "apply_singbox",
                             side_effect=RuntimeError("sing-box exited during startup")), \
                patch.object(Orchestrator, "reconcile_lines",
                             return_value={"1": {"route": "blocked", "reason": "x"}}) as lines:
            app.reconcile_proxy({"proxy": TROJAN, "lines": [LINE]})
        self.assertFalse(lines.call_args.args[2]["us"]["ready"])

    def test_a_failed_line_reconcile_publishes_every_line_blocked(self):
        ctx = _App()
        with ctx as app, patch.object(Orchestrator, "reconcile_lines",
                                      side_effect=OSError("nft missing")):
            app.reconcile_proxy({"proxy": TROJAN, "lines": [LINE]})
            state = ctx.published()["lines"]["1"]
        self.assertFalse(state["ready"])
        self.assertEqual(state["route"], "blocked")
        self.assertIn("nft missing", state["error"])

    def test_disabled_line(self):
        ctx = _App()
        with ctx as app, patch.object(Orchestrator, "reconcile_lines",
                                      return_value={"1": {"route": "blocked",
                                                          "reason": "line disabled"}}):
            app.reconcile_proxy({"proxy": TROJAN, "lines": [dict(LINE, enabled=False)]})
            state = ctx.published()["lines"]["1"]
        self.assertEqual(state["mode"], "disabled")
        self.assertFalse(state["ready"])
        self.assertNotIn("error", state)

    def test_dry_run_executes_nothing(self):
        with _App() as app, patch.object(subprocess, "run") as run:
            app.reconcile_lines({"lines": [LINE]}, True, {})
        run.assert_not_called()

    def test_control_port_reaches_the_rules(self):
        with _App() as app, patch.object(mdd_netns, "reconcile", return_value={}) as rec:
            app.reconcile_lines({"lines": [LINE], "control_port": 9443}, True, {})
            self.assertEqual(rec.call_args.kwargs["control_port"], 9443)
            app.reconcile_lines({"lines": [LINE], "control_port": "junk"}, True, {})
            self.assertEqual(rec.call_args.kwargs["control_port"], 8443)


class WakeTests(unittest.TestCase):
    def test_wakes_when_singbox_exits(self):
        class Dead:
            def poll(self):
                return 1

        with _App() as app:
            app.singbox = Dead()
            with patch("host.mdd_orchestrator.time.sleep") as sleep:
                app._sleep_for_work(600.0)
            self.assertEqual(sleep.call_count, 1)

    def test_running_engines_do_not_wake_it(self):
        class Alive:
            def poll(self):
                return None

        with _App() as app:
            app.singbox = Alive()
            self.assertFalse(app._engine_died())


class DefaultsTests(unittest.TestCase):
    def test_country_socks_listens_on_loopback(self):
        self.assertEqual(orch.COUNTRY_PROXY_LISTEN, "127.0.0.1")

    def test_service_restart_uses_the_new_unit_names(self):
        with _App() as app:
            app.dry_run = False
            calls = []
            with patch.object(orch, "run",
                              side_effect=lambda args, **kw: calls.append(args) or
                              subprocess.CompletedProcess(args, 0, "", "")):
                for scope in ("control", "services"):
                    (app.root).mkdir(parents=True, exist_ok=True)
                    (app.root / "service-restart-request.json").write_text(
                        json.dumps({"scope": scope}))
                    app.process_service_restart_request()
        flat = [" ".join(c) for c in calls]
        self.assertIn("systemctl restart mdd-control.service", flat)
        self.assertTrue(any("systemctl restart mdd-orchestrator.service mdd-control.service"
                            in c for c in flat))
        self.assertFalse(any("install.sh" in c or "mdd-sim-gateway" in c for c in flat))


if __name__ == "__main__":
    unittest.main()
