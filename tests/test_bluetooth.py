"""Bluetooth speaker page: the root helper mdd-btctl and the /api/bluetooth routes.

Parsing is checked against real output captured on the device (bluez 5.55, PulseAudio 14.2).
The helper runs against fake bluetoothctl/pactl programs; it pins PATH, so the fakes are put on
the module's PATH constant rather than the environment.
"""
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from control.app.routers import bluetooth as bt_routes

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "packaging" / "sbin" / "mdd-btctl"
SUDOERS = ROOT / "packaging" / "sudoers.d" / "mdd"
btctl = SourceFileLoader("mdd_btctl", str(HELPER)).load_module()

SPEAKER = "98:FC:84:8A:B9:AD"
BLE = "98:FC:84:01:5C:1B"

DEVICES = f"Device {SPEAKER} MI Portable Speaker​\nDevice {BLE} 98-FC-84-01-5C-1B\n"
INFO_SPEAKER = textwrap.dedent(f"""\
    Device {SPEAKER} (public)
    \tName: MI Portable Speaker​
    \tAlias: MI Portable Speaker​
    \tClass: 0x00240414
    \tIcon: audio-card
    \tPaired: yes
    \tTrusted: yes
    \tBlocked: no
    \tConnected: yes
    \tLegacyPairing: no
    \tUUID: Audio Sink                (0000110b-0000-1000-8000-00805f9b34fb)
    \tUUID: A/V Remote Control Target (0000110c-0000-1000-8000-00805f9b34fb)
    """)
INFO_BLE = textwrap.dedent(f"""\
    Device {BLE} (random)
    \tAlias: 98-FC-84-01-5C-1B
    \tPaired: no
    \tTrusted: no
    \tBlocked: no
    \tConnected: no
    \tLegacyPairing: no
    \tRSSI: -82
    """)
SHOW = textwrap.dedent("""\
    Controller 00:1A:7D:DA:71:13 (public)
    \tName: openstick
    \tAlias: openstick
    \tPowered: yes
    \tDiscoverable: no
    \tPairable: yes
    \tDiscovering: no
    """)
SINKS = textwrap.dedent("""\
    Sink #0
    \tState: SUSPENDED
    \tName: auto_null
    \tDescription: Dummy Output
    \tMute: no
    \tVolume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
    \t        balance 0.00
    \tBase Volume: 65536 / 100% / 0.00 dB

    Sink #1
    \tState: RUNNING
    \tName: bluez_sink.98_FC_84_8A_B9_AD.a2dp_sink
    \tDescription: MI Portable Speaker
    \tMute: yes
    \tVolume: front-left: 39322 /  60% / -13.31 dB,   front-right: 36045 /  55% / -15.58 dB
    \t        balance 0.00
    \tBase Volume: 65536 / 100% / 0.00 dB
    """)


class ParsingTests(unittest.TestCase):
    def test_device_lines_and_names(self):
        self.assertEqual(btctl.parse_devices(DEVICES),
                         [(SPEAKER, "MI Portable Speaker"), (BLE, "98-FC-84-01-5C-1B")])
        self.assertEqual(btctl.clean_name("MI Portable Speaker​"), "MI Portable Speaker")
        self.assertEqual(btctl.parse_devices("\x1b[0;94m[bluetooth]\x1b[0m# junk\n"), [])

    def test_info_fields(self):
        info, uuids = btctl.parse_kv(INFO_SPEAKER)
        self.assertEqual((info["Paired"], info["Connected"], info["Icon"]),
                         ("yes", "yes", "audio-card"))
        self.assertEqual(len(uuids), 2)
        self.assertIn("0000110b", uuids[0])

    def test_device_entries(self):
        speaker = btctl.device_entry(SPEAKER, "MI Portable Speaker", INFO_SPEAKER)
        self.assertEqual(speaker, {"mac": SPEAKER, "name": "MI Portable Speaker", "named": True,
                                   "paired": True, "trusted": True, "connected": True,
                                   "audio": True})
        ble = btctl.device_entry(BLE, "98-FC-84-01-5C-1B", INFO_BLE)
        self.assertFalse(ble["named"])
        self.assertFalse(ble["audio"] or ble["paired"] or ble["connected"])
        # An audio sink announced only by its UUID still counts as audio.
        by_uuid = btctl.device_entry(SPEAKER, "x", "\tUUID: Audio Sink (0000110B-0000-1000)\n")
        self.assertTrue(by_uuid["audio"])

    def test_sink_volume_is_the_volume_line_not_base_volume(self):
        sinks = btctl.parse_sinks(SINKS)
        self.assertEqual([s["name"] for s in sinks],
                         ["auto_null", "bluez_sink.98_FC_84_8A_B9_AD.a2dp_sink"])
        sink = btctl.bluetooth_sink(sinks)
        self.assertEqual((sink["volume"], sink["muted"], sink["description"]),
                         (58, True, "MI Portable Speaker"))     # mean of 60 % and 55 %
        self.assertEqual(sinks[0]["volume"], 100)
        self.assertIsNone(btctl.bluetooth_sink(sinks[:1]))


class ArgumentTests(unittest.TestCase):
    def run_main(self, *argv):
        with patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as out, \
                patch.object(btctl, "perform", side_effect=AssertionError("must not run")):
            code = btctl.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_refused_requests_never_reach_a_command(self):
        bad = [(), ("",), ("STATUS",), ("shell",), ("status", "x"), ("scan", "x"),
               ("pair",), ("pair", SPEAKER, SPEAKER), ("pair", SPEAKER.lower()),
               ("pair", f"{SPEAKER};id"), ("pair", "$(id)"), ("pair", f"{SPEAKER}\n"),
               ("pair", "98:FC:84:8A:B9"), ("pair", "98-FC-84-8A-B9-AD"),
               ("connect", "`reboot`"), ("remove", "../../etc"),
               ("volume", "101"), ("volume", "-1"), ("volume", "5.5"), ("volume", "50;"),
               ("volume", " 50"), ("volume", ""), ("mute", "2"), ("mute", "yes"),
               ("mute", "1 "), ("autoconnect", "None"), ("autoconnect", "x"),
               ("autoconnect", f"{SPEAKER} {BLE}"), ("pair", "--help")]
        for argv in bad:
            with self.subTest(argv=argv):
                code, answer = self.run_main(*argv)
                self.assertEqual(code, 64)
                self.assertFalse(answer["ok"])

    def test_valid_requests_parse(self):
        self.assertEqual(btctl.parse_args(["pair", SPEAKER]), ("pair", SPEAKER))
        self.assertEqual(btctl.parse_args(["volume", "0"]), ("volume", 0))
        self.assertEqual(btctl.parse_args(["volume", "100"]), ("volume", 100))
        self.assertEqual(btctl.parse_args(["mute", "1"]), ("mute", True))
        self.assertEqual(btctl.parse_args(["autoconnect", "none"]), ("autoconnect", None))
        self.assertEqual(btctl.parse_args(["autoconnect", BLE]), ("autoconnect", BLE))


FAKE_BLUETOOTHCTL = r'''#!/usr/bin/env python3
"""bluetoothctl stand-in: one-shot commands from argv, an interactive session from stdin."""
import json, os, sys
LOG, DATA = os.environ.get("FAKE_LOG"), json.load(open(os.environ["FAKE_DATA"]))
def log(text):
    with open(DATA["log"], "a") as handle:
        handle.write(text + "\n")
args = sys.argv[1:]
log("argv " + json.dumps(args))
if args:
    if args[0] == "--timeout":
        sys.exit(0)
    key = " ".join(args)
    sys.stdout.write(DATA["answers"].get(key, DATA["answers"].get(args[0], "")))
    sys.exit(0)
for line in sys.stdin:
    command = line.strip()
    log("stdin " + command)
    reply = DATA["session"].get(command.split(" ")[0], "")
    if reply:
        print(reply.replace("{arg}", command.partition(" ")[2]), flush=True)
    if command == "quit":
        break
'''

FAKE_PACTL = r'''#!/usr/bin/env python3
import json, os, sys
DATA = json.load(open(os.environ["FAKE_DATA"]))
with open(DATA["log"], "a") as handle:
    handle.write("pactl " + json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:3] == ["list", "sinks"]:
    sys.stdout.write(DATA["sinks"])
'''


class HelperCommandTests(unittest.TestCase):
    """The helper's actions against fake tools; checks argument lists, files and JSON."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdd-btctl-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(bindir)
        for name, text in (("bluetoothctl", FAKE_BLUETOOTHCTL), ("pactl", FAKE_PACTL)):
            path = os.path.join(bindir, name)
            Path(path).write_text(text.replace("/usr/bin/env python3", sys.executable))
            os.chmod(path, 0o755)
        self.log = os.path.join(self.tmp, "calls.log")
        self.data = {"log": self.log, "sinks": SINKS, "answers": {
            "show": SHOW, "devices": DEVICES, f"info {SPEAKER}": INFO_SPEAKER,
            f"info {BLE}": INFO_BLE, "connect": "Attempting to connect\nConnection successful\n",
            "disconnect": "Attempting to disconnect\nSuccessful disconnected\n",
            "remove": "Device has been removed\n", "power": "Changing power on succeeded\n"},
            "session": {"scan": f"Discovery started\n[NEW] Device {SPEAKER} MI Portable Speaker",
                        "pair": "Attempting to pair with {arg}\nPairing successful",
                        "trust": "Changing {arg} trust succeeded",
                        "connect": "Attempting to connect to {arg}\nConnection successful"}}
        self.data_path = os.path.join(self.tmp, "data.json")
        self.save()
        env = {**btctl.ENV, "PATH": bindir, "FAKE_DATA": self.data_path}
        self.autoconnect = os.path.join(self.tmp, "bt-speaker", "autoconnect")
        for patcher in (patch.object(btctl, "PATH", bindir), patch.object(btctl, "ENV", env),
                        patch.object(btctl, "LOCK_PATH", os.path.join(self.tmp, "lock")),
                        patch.object(btctl, "SCAN_STATE", os.path.join(self.tmp, "scan")),
                        patch.object(btctl, "AUTOCONNECT_FILE", self.autoconnect)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self):
        Path(self.data_path).write_text(json.dumps(self.data))

    def calls(self):
        try:
            return Path(self.log).read_text().splitlines()
        except FileNotFoundError:
            return []

    def test_status(self):
        state = btctl.perform("status", None)
        self.assertTrue(state["available"] and state["powered"])
        self.assertFalse(state["scanning"])
        self.assertIsNone(state["autoconnect"])
        self.assertEqual([d["mac"] for d in state["devices"]], [SPEAKER, BLE])
        self.assertEqual(state["sink"]["volume"], 58)
        json.dumps(state)                       # the control plane reads it as JSON

    def test_no_controller_or_no_bluetoothctl(self):
        self.data["answers"]["show"] = "No default controller available\n"
        self.save()
        self.assertEqual(btctl.perform("status", None),
                         {"available": False, "reason": "no Bluetooth controller"})
        with patch.object(btctl, "PATH", "/nonexistent"):
            self.assertFalse(btctl.perform("status", None)["available"])

    def test_pair_runs_one_session_while_scanning(self):
        self.data["answers"][f"info {SPEAKER}"] = f"Device {SPEAKER} not available\n"
        self.save()
        self.assertEqual(btctl.perform("pair", SPEAKER),
                         {"ok": True, "message": "paired and connected"})
        session = [c[6:] for c in self.calls() if c.startswith("stdin ")]
        self.assertEqual(session[:4], ["agent NoInputNoOutput", "default-agent", "power on",
                                       "scan on"])
        self.assertEqual(session[4:7], [f"pair {SPEAKER}", f"trust {SPEAKER}",
                                        f"connect {SPEAKER}"])
        self.assertIn("quit", session)

    def test_pair_failure_reports_bluez(self):
        self.data["session"]["pair"] = "Failed to pair: org.bluez.Error.AuthenticationFailed"
        self.save()
        answer = btctl.perform("pair", SPEAKER)
        self.assertFalse(answer["ok"])
        self.assertIn("AuthenticationFailed", answer["message"])

    def test_connect_disconnect_and_remove(self):
        for action in ("connect", "disconnect"):
            with self.subTest(action=action):
                self.assertTrue(btctl.perform(action, SPEAKER)["ok"])
                self.assertEqual(self.calls()[-1], "argv " + json.dumps([action, SPEAKER]))
        btctl.perform("autoconnect", SPEAKER)
        self.assertEqual(Path(self.autoconnect).read_text(), SPEAKER + "\n")
        self.assertTrue(btctl.perform("remove", SPEAKER)["ok"])
        self.assertEqual(Path(self.autoconnect).read_text().strip(), "")   # forgotten too

    def test_failed_connect_returns_the_output(self):
        self.data["answers"]["connect"] = "Failed to connect: org.bluez.Error.Failed\n"
        self.save()
        answer = btctl.perform("connect", SPEAKER)
        self.assertEqual(answer, {"ok": False,
                                  "message": "Failed to connect: org.bluez.Error.Failed"})

    def test_volume_and_mute_go_to_the_bluetooth_sink(self):
        self.assertTrue(btctl.perform("volume", 35)["ok"])
        self.assertTrue(btctl.perform("mute", False)["ok"])
        sink = "bluez_sink.98_FC_84_8A_B9_AD.a2dp_sink"
        self.assertIn("pactl " + json.dumps(["set-sink-volume", sink, "35%"]), self.calls())
        self.assertIn("pactl " + json.dumps(["set-sink-mute", sink, "0"]), self.calls())

    def test_volume_without_a_speaker(self):
        self.data["sinks"] = SINKS.split("\n\n")[0]
        self.save()
        self.assertEqual(btctl.perform("volume", 10),
                         {"ok": False, "message": "no Bluetooth speaker is connected"})

    def test_autoconnect_is_written_whole_and_can_be_cleared(self):
        btctl.perform("autoconnect", BLE)
        self.assertEqual(Path(self.autoconnect).read_text(), BLE + "\n")
        self.assertEqual(btctl.read_autoconnect(), BLE)
        btctl.perform("autoconnect", None)
        self.assertEqual(Path(self.autoconnect).read_text(), "\n")
        self.assertIsNone(btctl.read_autoconnect())
        self.assertEqual([p for p in os.listdir(os.path.dirname(self.autoconnect))],
                         ["autoconnect"])               # no temporary file left behind

    def test_scan_starts_in_the_background_and_only_once(self):
        first = btctl.perform("scan", None)
        self.assertEqual(first, {"ok": True, "message": "scanning for 30 s"})
        pid, until = Path(btctl.SCAN_STATE).read_text().split()
        self.assertGreater(float(until), 0)
        os.waitpid(int(pid), 0)                       # the fake scan exits at once
        self.assertIn("argv " + json.dumps(["--timeout", "30", "scan", "on"]), self.calls())
        # While a scan is running (this process stands in for it) a second one is not started.
        Path(btctl.SCAN_STATE).write_text(f"{os.getpid()} {__import__('time').time() + 30}\n")
        before = len(self.calls())
        self.assertEqual(btctl.perform("scan", None),
                         {"ok": True, "message": "already scanning"})
        self.assertEqual(len(self.calls()), before)
        self.assertTrue(btctl.scanning())

    def test_helper_is_shipped_safely(self):
        text = HELPER.read_text()
        self.assertTrue(text.startswith("#!/opt/mdd/current/venv/bin/python3 -I\n"))
        self.assertTrue(HELPER.stat().st_mode & stat.S_IXUSR)
        self.assertNotIn("shell=True", text)
        self.assertNotIn("os.system", text)
        self.assertNotIn("/var/lib/mdd", text)          # reads nothing mdd can write
        self.assertIn('"LC_ALL": "C"', text)
        self.assertIn('"PULSE_SERVER": "unix:/run/pulse/native"', text)


class SudoersTests(unittest.TestCase):
    def test_btctl_is_allowed_alongside_enginectl_and_nothing_else(self):
        rules = [line for line in SUDOERS.read_text().splitlines()
                 if line.strip() and not line.startswith(("#", "Defaults"))]
        self.assertEqual(rules, ["mdd ALL=(root) NOPASSWD: /usr/local/sbin/mdd-enginectl",
                                 "mdd ALL=(root) NOPASSWD: /usr/local/sbin/mdd-btctl"])

    def test_install_script_installs_and_removes_it(self):
        text = (ROOT / "install.sh").read_text()
        self.assertIn('install -m 0755 -o root -g root "$CURRENT/packaging/sbin/mdd-btctl" '
                      '"$BTCTL"', text)
        self.assertIn('"${BTCTL:?}"', text)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def helper(self, stdout="", returncode=0, stderr="", raises=None):
        calls = []

        def fake(command, **kwargs):
            calls.append((command, kwargs["timeout"]))
            if raises:
                raise raises
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)
        patcher = patch.object(bt_routes.subprocess, "run", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    async def test_status_passes_the_helper_state_through(self):
        state = {"available": True, "powered": True, "devices": [], "sink": None}
        calls = self.helper(json.dumps(state))
        self.assertEqual(await bt_routes.api_bluetooth_status(), state)
        self.assertEqual(calls[0][0], ["sudo", "-n", bt_routes.BTCTL, "status"])

    async def test_status_without_sudo_rule_or_helper_is_unavailable(self):
        for kwargs in ({"raises": FileNotFoundError("sudo")},
                       {"stdout": "", "stderr": "sudo: a password is required", "returncode": 1},
                       {"stdout": "", "stderr": "sudo: /usr/local/sbin/mdd-btctl: command not "
                        "found", "returncode": 1},
                       {"raises": subprocess.TimeoutExpired("sudo", 20)},
                       {"stdout": json.dumps({"available": False,
                                              "reason": "no Bluetooth controller"})}):
            with self.subTest(kwargs=kwargs):
                self.helper(**kwargs)
                answer = await bt_routes.api_bluetooth_status()
                self.assertFalse(answer["available"])
                self.assertTrue(answer["reason"])

    async def test_device_actions(self):
        calls = self.helper(json.dumps({"ok": True, "message": "paired and connected"}))
        self.assertTrue((await bt_routes.api_bluetooth_device(SPEAKER.lower(), "pair"))["ok"])
        self.assertEqual(calls[-1], (["sudo", "-n", bt_routes.BTCTL, "pair", SPEAKER], 45))
        await bt_routes.api_bluetooth_device(SPEAKER, "disconnect")
        self.assertEqual(calls[-1], (["sudo", "-n", bt_routes.BTCTL, "disconnect", SPEAKER], 20))

    async def test_invalid_input_is_400_before_the_helper(self):
        calls = self.helper(json.dumps({"ok": True}))
        for mac, action in ((f"{SPEAKER};id", "pair"), ("$(reboot)", "connect"),
                            ("98:FC:84:8A:B9", "remove"), (SPEAKER, "rm"), (SPEAKER, "status")):
            with self.subTest(mac=mac, action=action), self.assertRaises(HTTPException) as caught:
                await bt_routes.api_bluetooth_device(mac, action)
            self.assertEqual(caught.exception.status_code, 400)
        for body in ({"volume": 101}, {"volume": -1}, {"volume": "50"}, {"volume": True},
                     {"volume": 5.5}, {"muted": "yes"}):
            with self.subTest(body=body), self.assertRaises(HTTPException) as caught:
                await bt_routes.api_bluetooth_volume(body)
            self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException) as caught:
            await bt_routes.api_bluetooth_autoconnect({"mac": "x; reboot"})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(calls, [])

    async def test_failures_are_reported(self):
        self.helper(json.dumps({"ok": False, "message": "pairing failed: AuthenticationFailed"}))
        with self.assertRaises(HTTPException) as caught:
            await bt_routes.api_bluetooth_device(SPEAKER, "pair")
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("AuthenticationFailed", caught.exception.detail)
        self.helper(json.dumps({"ok": False, "message": "invalid MAC address"}), returncode=64)
        with self.assertRaises(HTTPException) as caught:
            await bt_routes.api_bluetooth_device(SPEAKER, "connect")
        self.assertEqual(caught.exception.status_code, 400)
        self.helper(raises=FileNotFoundError("sudo"))
        with self.assertRaises(HTTPException) as caught:
            await bt_routes.api_bluetooth_scan()
        self.assertEqual(caught.exception.status_code, 503)

    async def test_volume_and_autoconnect(self):
        sink = {"name": "bluez_sink.x", "volume": 40, "muted": False}
        answers = iter([{"ok": True}, {"ok": True},
                        {"available": True, "sink": sink, "devices": []}])
        calls = []

        def fake(command, **kwargs):
            calls.append(command[3:])
            return subprocess.CompletedProcess(command, 0, json.dumps(next(answers)), "")
        with patch.object(bt_routes.subprocess, "run", side_effect=fake):
            self.assertEqual(await bt_routes.api_bluetooth_volume({"volume": 40, "muted": False}),
                             sink)
        self.assertEqual(calls, [["volume", "40"], ["mute", "0"], ["status"]])
        calls = self.helper(json.dumps({"ok": True, "message": "saved"}))
        self.assertEqual(await bt_routes.api_bluetooth_autoconnect({"mac": None}),
                         {"autoconnect": None})
        self.assertEqual(calls[-1][0][3:], ["autoconnect", "none"])


@unittest.skipUnless(os.environ.get("MDD_SUDO_IT") == "1" and os.geteuid() == 0
                     and shutil.which("sudo") and shutil.which("runuser"),
                     "privileged integration test: root, sudo, runuser and MDD_SUDO_IT=1")
class SudoIntegrationTests(unittest.TestCase):
    """As user mdd: `sudo -n mdd-btctl status` works, any other root command does not.

    Installs the real sudoers fragment and the helper (with its shebang pointed at this
    interpreter) for the duration of the test; needs an existing user mdd.
    """

    def setUp(self):
        subprocess.run(["id", "mdd"], check=True, capture_output=True, timeout=5)
        for path in ("/etc/sudoers.d/mdd", "/usr/local/sbin/mdd-btctl"):
            self.assertFalse(os.path.exists(path), f"{path} exists; not overwriting it")
        shutil.copy(SUDOERS, "/etc/sudoers.d/mdd")
        os.chmod("/etc/sudoers.d/mdd", 0o440)
        text = HELPER.read_text().replace("/opt/mdd/current/venv/bin/python3", sys.executable, 1)
        Path("/usr/local/sbin/mdd-btctl").write_text(text)
        os.chmod("/usr/local/sbin/mdd-btctl", 0o755)
        self.addCleanup(os.remove, "/etc/sudoers.d/mdd")
        self.addCleanup(os.remove, "/usr/local/sbin/mdd-btctl")

    def as_mdd(self, *command):
        return subprocess.run(["runuser", "-u", "mdd", "--", *command], text=True,
                              capture_output=True, timeout=30)

    def test_only_the_helper_runs_as_root(self):
        ok = self.as_mdd("sudo", "-n", "/usr/local/sbin/mdd-btctl", "status")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("available", json.loads(ok.stdout))
        refused = self.as_mdd("sudo", "-n", "/usr/local/sbin/mdd-btctl", "pair", "x;id")
        self.assertEqual(refused.returncode, 64)
        for command in (["/usr/bin/id"], ["/bin/sh", "-c", "id"],
                        ["/usr/bin/python3", "-c", "print(1)"]):
            with self.subTest(command=command):
                denied = self.as_mdd("sudo", "-n", *command)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("password is required", denied.stderr)


if __name__ == "__main__":
    unittest.main()
