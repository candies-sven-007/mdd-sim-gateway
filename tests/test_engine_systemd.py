"""control/app/engine.py on systemd: mdd-enginectl, systemctl, journalctl, the CLI socket."""
import os
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

from control.app import egress, engine


def done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


INST = {"id": "1", "mcc": "310", "mnc": "260", "ports": {"rtp_start": 10000, "rtp_span": 12}}


class NamesTests(unittest.TestCase):
    def test_line_numbers(self):
        self.assertEqual(engine.unit_name("2"), "mdd-engine@2.service")
        self.assertEqual(engine.engine_address("10"), "10.88.10.2")
        self.assertEqual(engine.manager_address("1"), "10.88.1.1")
        for bad in ("0", "11", "01", "sim1", "1;x", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                engine.unit_name(bad)

    def test_addresses_match_the_namespace_module(self):
        from host import mdd_netns
        for n in range(1, 11):
            self.assertEqual(engine.engine_address(str(n)), mdd_netns.line(n).ns_addr)
            self.assertEqual(engine.manager_address(str(n)), mdd_netns.line(n).host_addr)


class EnginectlTests(unittest.TestCase):
    def test_runs_sudo_non_interactively(self):
        with patch.object(engine.subprocess, "run", return_value=done()) as run:
            engine._enginectl("start", "1")
        self.assertEqual(run.call_args.args[0],
                         ["sudo", "-n", engine.ENGINECTL, "start", "1"])
        self.assertIn("timeout", run.call_args.kwargs)

    def test_failure_carries_the_reason(self):
        with patch.object(engine.subprocess, "run",
                          return_value=done(returncode=1, stderr="sudo: a password is required")):
            with self.assertRaisesRegex(RuntimeError, "password is required"):
                engine._enginectl("start", "1")

    def test_never_asked_about_an_invalid_line(self):
        with patch.object(engine.subprocess, "run") as run, self.assertRaises(ValueError):
            engine._enginectl("start", "../1")
        run.assert_not_called()


class StartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(engine, "DATA_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, running=False, resolve="198.51.100.10", ensure=None):
        order = []
        with patch.object(egress, "ensure_line",
                          side_effect=ensure or (lambda inst, s: {"route": "tun"})), \
                patch.object(egress, "resolve_epdg",
                             side_effect=resolve if isinstance(resolve, Exception)
                             else (lambda inst, state: resolve)), \
                patch.object(egress, "publish") as publish, \
                patch.object(engine, "is_running", return_value=running), \
                patch.object(engine, "capture_diagnostics",
                             side_effect=lambda *a: order.append("capture")), \
                patch.object(engine, "_clear_runtime_state",
                             side_effect=lambda base: order.append("clear")), \
                patch.object(engine.cfg, "write_instance_json",
                             side_effect=lambda inst, s: order.append(("write", inst["epdg"]))), \
                patch.object(engine, "_enginectl",
                             side_effect=lambda action, iid: order.append(action)), \
                patch.object(engine, "unit_runtime", return_value={"generation": "g2"}):
            result = engine.start(dict(INST), {})
        return result, order, publish

    def test_first_start(self):
        result, order, publish = self.start()
        self.assertEqual(result, "g2")
        self.assertEqual(order, ["clear", ("write", "198.51.100.10"), "reset-failed", "start"])
        publish.assert_called_once()

    def test_restart_captures_and_stops_first(self):
        _, order, _ = self.start(running=True)
        self.assertEqual(order, ["capture", "stop", "clear", ("write", "198.51.100.10"),
                                 "reset-failed", "start"])

    def test_a_refused_exit_leaves_the_running_engine_alone(self):
        def refuse(inst, settings):
            raise egress.EgressError("US exit unavailable")
        with self.assertRaises(egress.EgressError):
            self.start(running=True, ensure=refuse)

    def test_a_failed_epdg_lookup_leaves_the_running_engine_alone(self):
        order = []
        with patch.object(egress, "ensure_line", return_value={"route": "tun"}), \
                patch.object(egress, "resolve_epdg",
                             side_effect=egress.EgressError("DNS through exit failed")), \
                patch.object(engine, "_enginectl", side_effect=lambda *a: order.append(a)), \
                patch.object(engine.cfg, "write_instance_json",
                             side_effect=lambda *a: order.append("write")):
            with self.assertRaises(egress.EgressError):
                engine.start(dict(INST), {})
        self.assertEqual(order, [])

    def test_an_invalid_line_id_is_refused_before_anything(self):
        with patch.object(egress, "ensure_line") as ensure, self.assertRaises(ValueError):
            engine.start({**INST, "id": "sim1"}, {})
        ensure.assert_not_called()


class StopTests(unittest.TestCase):
    def test_stops_a_running_engine(self):
        with patch.object(engine, "unit_runtime",
                          return_value={"running": True, "generation": "g",
                                        "active_state": "active"}), \
                patch.object(engine, "_enginectl") as ctl:
            self.assertTrue(engine.stop("1"))
        ctl.assert_called_once_with("stop", "1")

    def test_nothing_to_stop(self):
        with patch.object(engine, "unit_runtime",
                          return_value={"running": False, "active_state": "inactive"}), \
                patch.object(engine, "_enginectl") as ctl:
            self.assertFalse(engine.stop("1"))
        ctl.assert_not_called()

    def test_a_failed_unit_is_still_stopped(self):
        with patch.object(engine, "unit_runtime",
                          return_value={"running": False, "active_state": "failed"}), \
                patch.object(engine, "_enginectl") as ctl:
            self.assertTrue(engine.stop("1"))
        ctl.assert_called_once_with("stop", "1")

    def test_only_the_expected_generation(self):
        with patch.object(engine, "unit_runtime",
                          return_value={"running": True, "generation": "new",
                                        "active_state": "active"}), \
                patch.object(engine, "_enginectl") as ctl:
            self.assertFalse(engine.stop("1", expected_generation="old"))
        ctl.assert_not_called()

    def test_delete_takes_the_line_down_first(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(engine, "DATA_DIR", temp):
            os.makedirs(os.path.join(temp, "instances", "1", "run"))
            with patch.object(engine, "_enginectl") as ctl:
                self.assertTrue(engine.delete_instance_data("1"))
            ctl.assert_called_once_with("down", "1")
            self.assertFalse(os.path.exists(os.path.join(temp, "instances", "1")))


class ShowTests(unittest.TestCase):
    def test_one_systemctl_for_several_lines(self):
        out = ("ActiveState=active\nSubState=running\nInvocationID=a\nNRestarts=2\n\n"
               "ActiveState=inactive\nSubState=dead\nInvocationID=\nNRestarts=0\n")
        with patch.object(engine.subprocess, "run", return_value=done(out)) as run:
            result = engine.units_runtime(["1", "2", "sim9"])
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["systemctl", "show"])
        self.assertEqual(command[-2:], ["mdd-engine@1.service", "mdd-engine@2.service"])
        self.assertTrue(result["1"]["running"])
        self.assertEqual(result["1"]["generation"], "a")
        self.assertFalse(result["2"]["running"])
        self.assertFalse(result["sim9"]["running"])

    def test_systemctl_missing_reads_as_stopped(self):
        with patch.object(engine.subprocess, "run", side_effect=FileNotFoundError("systemctl")):
            self.assertFalse(engine.unit_runtime("1")["running"])


class CliAndLogTests(unittest.TestCase):
    def test_cli_goes_through_the_lines_control_socket(self):
        with patch.object(engine.subprocess, "run",
                          return_value=done("volte_ims/sip:x  Registered\n")) as run:
            self.assertEqual(engine.registration_state("1"), "Registered")
        command = run.call_args.args[0]
        self.assertEqual(command[0], engine.ASTERISK)
        self.assertEqual(command[1:3], ["-r", "-s"])
        self.assertTrue(command[3].endswith(os.path.join("instances", "1", "run", "asterisk",
                                                         "asterisk.ctl")))
        self.assertEqual(command[4:], ["-x", "pjsip show registrations"])

    def test_cli_failure_is_unknown_not_a_carrier_verdict(self):
        with patch.object(engine.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("asterisk", 5)):
            self.assertEqual(engine.registration_state("1"), "unknown")
            self.assertTrue(engine.exec_cli("1", "core show uptime").startswith("error: "))

    def test_logs_come_from_the_journal_in_the_ike_format(self):
        raw = ("2026-08-07T11:17:18+0800 gw entrypoint.sh[42]: [Aug  7 11:17:18] "
               "REGISTER sip:ims.example SIP/2.0\n"
               "2026-08-07T11:17:19+0800 gw asterisk[43]: Via: SIP/2.0/TCP example\n")
        with patch.object(engine.subprocess, "run", return_value=done(raw)) as run:
            rendered = engine.logs("1", 25, since=1700000000)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["journalctl", "-u", "mdd-engine@1.service"])
        self.assertIn("-n", command)
        self.assertEqual(command[command.index("--since") + 1], "@1700000000")
        self.assertEqual(rendered,
                         "[2026-08-07 11:17:18+0800] REGISTER sip:ims.example SIP/2.0\n"
                         "[2026-08-07 11:17:19+0800] Via: SIP/2.0/TCP example\n")

    def test_journal_error_is_reported_as_text(self):
        with patch.object(engine.subprocess, "run",
                          return_value=done(returncode=1, stderr="No journal files")):
            self.assertTrue(engine.logs("1").startswith("error: "))


class EpdgResolutionTests(unittest.TestCase):
    def test_an_address_is_used_as_it_is(self):
        self.assertEqual(egress.resolve_epdg({"epdg": "198.51.100.7"}, {}), "198.51.100.7")

    def test_a_tun_line_resolves_through_its_country_socks(self):
        with patch.object(egress, "status", return_value={"exits": {"us": {
                "proxy_host": "127.0.0.1", "proxy_port": 22123}}}), \
                patch.object(egress, "resolve_ipv4_via_socks",
                             return_value="198.51.100.9") as via:
            ip = egress.resolve_epdg({"mcc": "310", "mnc": "260"}, {"route": "tun"})
        self.assertEqual(ip, "198.51.100.9")
        self.assertEqual(via.call_args, call("socks5://127.0.0.1:22123",
                                             "epdg.epc.mnc260.mcc310.pub.3gppnetwork.org"))

    def test_a_tun_line_without_socks_refuses(self):
        with patch.object(egress, "status", return_value={"exits": {}}), \
                self.assertRaises(egress.EgressError):
            egress.resolve_epdg({"mcc": "310", "mnc": "260"}, {"route": "tun"})

    def test_a_direct_line_uses_the_host_resolver(self):
        with patch.object(egress.socket, "getaddrinfo",
                          return_value=[(2, 2, 17, "", ("198.51.100.4", 0))]) as gai:
            ip = egress.resolve_epdg({"epdg": "epdg.example"}, {"route": "direct"})
        self.assertEqual(ip, "198.51.100.4")
        self.assertEqual(gai.call_args.args[0], "epdg.example")

    def test_no_epdg_name(self):
        with self.assertRaises(egress.EgressError):
            egress.resolve_epdg({}, {"route": "direct"})

    def test_desired_document_carries_rtp_and_control_port(self):
        document = egress.desired_document([INST], {"http_port": 9443})
        self.assertEqual(document["control_port"], 9443)
        self.assertEqual(document["lines"][0]["rtp"], [10000, 10011])
        self.assertIsNone(egress.desired_document([{"id": "2"}], {})["lines"][0]["rtp"])


if __name__ == "__main__":
    unittest.main()
