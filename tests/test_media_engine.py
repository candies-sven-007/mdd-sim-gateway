"""What the engine renders for browser call media (direct mode only), where it writes, and
that Asterisk debug stays off unless a line asks for it."""
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jinja2 import Environment, FileSystemLoader

from control.app import config

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "engine" / "templates"


def engine_render(env=None):
    with patch.dict(os.environ, env or {}):
        spec = importlib.util.spec_from_file_location("mdd_render_media",
                                                      ROOT / "engine" / "render.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def instance_json(**extra) -> dict:
    return {
        "id": "3", "imsi": "001010000000000", "mcc": "001", "mnc": "01",
        "ami_secret": "test-secret", "local_addr": "10.88.3.2", "pcscf": "10.0.0.9",
        "msisdn": "61400000000", "rtp_start": 12000, "rtp_end": 12011,
        "sip": {"webrtc": {"enable": True, "password": "test-password"},
                "advertise_address": "192.168.1.5", "ice_advertise_address": "192.168.1.5"},
        **extra}


def render(names=("rtp", "pjsip"), env=None, **extra) -> dict:
    module = engine_render(env)
    ctx = module.build_context(instance_json(**extra))
    jinja = Environment(loader=FileSystemLoader(str(TEMPLATES)), trim_blocks=True,
                        lstrip_blocks=True, keep_trailing_newline=True)
    return {name: jinja.get_template(f"{name}.conf.j2").render(**ctx) for name in names}


def section(text: str, header: str, type_: str) -> str:
    """The body of the pjsip section ``[header]`` whose type= is ``type_``."""
    for block in re.split(r"(?m)^(?=\[)", text):
        if (block.startswith(f"[{header}]") and f"type={type_}\n" in block
                or block.startswith(f"[{header}]({type_}-local)")):
            return block
    raise AssertionError(f"no [{header}] type={type_}")


class MediaRenderTests(unittest.TestCase):
    def test_direct_mode_rewrites_the_host_candidate_and_publishes_the_lan_address(self):
        out = render()
        self.assertIn("[ice_host_candidates]\n10.88.3.2 => 192.168.1.5,include_local_address",
                      out["rtp"])
        self.assertIn("rtpstart=12000\nrtpend=12011", out["rtp"])
        self.assertIn("external_media_address=192.168.1.5", out["pjsip"])
        self.assertNotIn("media_address=", section(out["pjsip"], "webrtc", "endpoint")
                         .replace("external_media_address", ""))

    def test_no_relay_leftovers(self):
        out = render(names=("rtp", "pjsip", "http"))
        joined = "\n".join(out.values())
        self.assertNotIn("bind_rtp_to_media_address", joined)
        self.assertNotRegex(joined, r"(?m)^media_address=")
        self.assertNotIn("media relay", joined.lower())
        self.assertNotRegex(joined.lower(), r"\bturn:")

    def test_softphone_listener_is_the_veth_address(self):
        self.assertIn("bindaddr=10.88.3.2\n", render(names=("http",))["http"])


class EngineAddressTests(unittest.TestCase):
    def address(self, listed, exclude="", gateway=""):
        module = engine_render()
        with patch.object(module, "_default_gateway_ipv4", return_value=gateway), \
                patch.object(module.subprocess, "check_output", return_value=listed):
            return module.container_ipv4(exclude)

    def test_takes_the_first_listed_address_without_a_gateway(self):
        self.assertEqual(self.address("10.88.3.2 192.0.2.7 "), "10.88.3.2")

    def test_an_excluded_network_is_skipped(self):
        self.assertEqual(self.address("172.30.0.3 10.88.3.2 ", "172.30.0.0/16"), "10.88.3.2")

    def test_only_an_excluded_address_is_refused_rather_than_used(self):
        module = engine_render()
        fake = Mock()
        fake.getsockname.return_value = ("172.30.0.3", 1)
        with patch.object(module, "_default_gateway_ipv4", return_value=""), \
                patch.object(module.subprocess, "check_output", return_value="172.30.0.3"), \
                patch.object(module.socket, "socket", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "outside the excluded network"):
                module.container_ipv4("172.30.0.0/16")


class EnginePathTests(unittest.TestCase):
    ENV = {"MDD_RUNDIR": "/var/lib/mdd/instances/3/run",
           "MDD_LOGDIR": "/var/lib/mdd/instances/3/logs",
           "MDD_ENGINE_DIR": "/opt/mdd/current/engine"}

    def test_asterisk_writes_into_the_lines_directories(self):
        out = render(names=("asterisk",), env=self.ENV)["asterisk"]
        self.assertIn("astlogdir = /var/lib/mdd/instances/3/logs/asterisk", out)
        self.assertIn("astrundir = /var/lib/mdd/instances/3/run/asterisk", out)
        self.assertIn("astctlpermissions = 0660", out)

    def test_dialplan_logs_and_notifies_through_the_lines_paths(self):
        out = render(names=("extensions",), env=self.ENV)["extensions"]
        self.assertIn("FILE(/var/lib/mdd/instances/3/logs/calls.txt", out)
        self.assertRegex(out, r"TrySystem\(\S*python\S* /opt/mdd/current/engine/notify\.py "
                              r"call_in ")
        self.assertNotIn("/usr/local/bin", out)
        self.assertNotIn("FILE(/logs/", out)

    def test_render_reads_and_writes_where_the_unit_says(self):
        with tempfile.TemporaryDirectory() as temp:
            run, etc = Path(temp, "run"), Path(temp, "etc")
            run.mkdir()
            (run / "pcscf").write_text("fd00::9")
            module = engine_render({"MDD_RUNDIR": str(run), "MDD_ENGINE_ETC": str(etc),
                                    "MDD_CONFIG": str(Path(temp, "instance.json"))})
            self.assertEqual(module.CFG_PATH, str(Path(temp, "instance.json")))
            ctx = module.build_context({k: v for k, v in instance_json().items()
                                        if k != "pcscf"})
            self.assertEqual(ctx["pcscf"], "fd00::9")
            self.assertEqual(module.ENGINE_ETC, str(etc))


class DebugOffByDefaultTests(unittest.TestCase):
    """DEBUG output holds SIP MESSAGE bodies, network SMS among them as RP-DATA hex."""

    def test_default_render_has_no_debug_anywhere(self):
        out = render(names=("asterisk", "logger"))
        self.assertIn("debug = 0", out["asterisk"])
        self.assertNotRegex(out["logger"], r"(?m)^[^;\n]*\bdebug\b")
        self.assertNotIn("reload_debug", out["logger"])

    def test_debug_appears_only_when_the_line_asks(self):
        out = render(names=("asterisk", "logger"), debug={"asterisk": True})
        self.assertIn("debug = 5", out["asterisk"])
        self.assertIn("reload_debug => debug", out["logger"])

    def test_pjsip_logger_is_never_switched_on(self):
        for path in list((ROOT / "engine").rglob("*.py")) + list(TEMPLATES.glob("*.j2")) + \
                [ROOT / "engine" / "entrypoint.sh"]:
            if "patches" in path.parts:
                continue
            text = path.read_text()
            with self.subTest(path=path.name):
                self.assertNotRegex(text, r"pjsip set logger on")
                self.assertNotIn("logger_enable", text)

    def test_the_pcscf_debug_window_is_off_unless_debug_is_on(self):
        source = (ROOT / "engine" / "swu_ike.py").read_text()
        self.assertIn('os.environ.get("SWU_PCSCF_DEBUG_SECONDS", "0")', source)
        for debug, expected in (({}, "SWU_PCSCF_DEBUG_SECONDS=0"),
                                ({"asterisk": True}, "SWU_PCSCF_DEBUG_SECONDS=90")):
            with tempfile.TemporaryDirectory() as temp:
                cfg_path = Path(temp, "instance.json")
                cfg_path.write_text(json.dumps(instance_json(debug=debug)))
                env = {"MDD_CONFIG": str(cfg_path), "MDD_RUNDIR": str(Path(temp, "run")),
                       "MDD_LOGDIR": str(Path(temp, "logs")), "MDD_TPL": str(TEMPLATES),
                       "MDD_ENGINE_ETC": str(Path(temp, "etc")),
                       "MDD_AST_ETC": str(Path(temp, "asterisk"))}
                module = engine_render(env)
                with patch.dict(os.environ, env):
                    module.main()
                lines = Path(temp, "run", "engine.env").read_text().splitlines()
                self.assertIn(expected, lines)
                self.assertTrue(Path(temp, "asterisk", "asterisk.conf").exists())
                self.assertTrue(Path(temp, "etc", "ami_usim.ini").exists())


class EngineEnvInjectionTests(unittest.TestCase):
    """instance.json is written by the unprivileged control plane and turned into engine.env,
    which the engine (root) sources. No value may become a command."""

    HOSTILE = ["$(touch {canary})", "`touch {canary}`", "a\ntouch {canary}", "'; touch {canary}; '",
               "\"; touch {canary}; \"", "x y", "$HOME", "\\"]

    def test_values_survive_sourcing_without_running_anything(self):
        module = engine_render()
        with tempfile.TemporaryDirectory() as temp:
            canary = os.path.join(temp, "pwned")
            for template in self.HOSTILE:
                value = template.format(canary=canary)
                with self.subTest(value=value):
                    env_file = os.path.join(temp, "engine.env")
                    with open(env_file, "w") as handle:
                        handle.write(f"USIM_PIN={module.env_value(value)}\n")
                    out = subprocess.run(
                        ["bash", "-c", f'set -a; . "{env_file}"; set +a; printf %s "$USIM_PIN"'],
                        text=True, stdout=subprocess.PIPE, timeout=10).stdout
                    self.assertEqual(out, value)
                    self.assertFalse(os.path.exists(canary))


class MediaInstanceJsonTests(unittest.TestCase):
    def test_the_lines_own_rtp_block_reaches_the_engine(self):
        inst = {"id": "3", "index": 1, "imsi": "001010000000000", "mcc": "001", "mnc": "01",
                "ami_secret": "a", "ports": config._alloc_ports(1),
                "sip": {"webrtc": {"enable": True, "password": "p"}}}
        with patch.object(config, "internal_event_token", return_value="t"):
            rendered = config.render_instance_json(inst, {})
        self.assertNotIn("media", rendered)
        self.assertNotIn("engine_subnet", rendered)
        self.assertEqual(rendered["rtp_start"], config._alloc_ports(1)["rtp_start"])


if __name__ == "__main__":
    unittest.main()
