"""The engine's side of the two media modes: what each renders and how its container is made.

Direct mode must render exactly what it did before relay mode existed; relay mode changes only
the browser leg, never the IMS leg inside the tunnel.
"""
import importlib
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
RELAY = {"mode": "relay", "subnet": "172.30.0.0/16", "rtp_start": 10000, "rtp_end": 10199}


def engine_render():
    spec = importlib.util.spec_from_file_location("mdd_render_media", ROOT / "engine" / "render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def instance_json(media=None) -> dict:
    cfg = {
        "id": "3", "imsi": "001010000000000", "mcc": "001", "mnc": "01",
        "ami_secret": "test-secret", "local_addr": "172.17.0.2", "pcscf": "10.0.0.9",
        "msisdn": "61400000000", "rtp_start": 12000, "rtp_end": 12011,
        "sip": {"webrtc": {"enable": True, "password": "test-password"},
                "advertise_address": "192.168.1.5", "ice_advertise_address": "192.168.1.5"},
    }
    if media:
        cfg["media"] = media
        cfg["rtp_start"], cfg["rtp_end"] = media["rtp_start"], media["rtp_end"]
    return cfg


def render(media=None, interface=("eth1", "172.30.0.3")) -> dict:
    module = engine_render()
    with patch.object(module, "media_interface", return_value=interface):
        ctx = module.build_context(instance_json(media))
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), trim_blocks=True,
                      lstrip_blocks=True, keep_trailing_newline=True)
    return {name: env.get_template(f"{name}.conf.j2").render(**ctx) for name in ("rtp", "pjsip")}


def section(text: str, header: str, type_: str) -> str:
    """The body of the pjsip section ``[header]`` whose type= is ``type_``."""
    for block in re.split(r"(?m)^(?=\[)", text):
        # [webrtc](endpoint-local) takes its type from the template it names.
        if (block.startswith(f"[{header}]") and f"type={type_}\n" in block
                or block.startswith(f"[{header}]({type_}-local)")):
            return block
    raise AssertionError(f"no [{header}] type={type_}")


class MediaRenderTests(unittest.TestCase):
    def test_direct_mode_rewrites_the_host_candidate_and_publishes_the_lan_address(self):
        out = render()
        self.assertIn("[ice_host_candidates]\n172.17.0.2 => 192.168.1.5,include_local_address",
                      out["rtp"])
        self.assertIn("rtpstart=12000\nrtpend=12011", out["rtp"])
        self.assertIn("external_media_address=192.168.1.5", out["pjsip"])
        self.assertNotIn("media_address=", section(out["pjsip"], "webrtc", "endpoint")
                         .replace("external_media_address", ""))

    def test_relay_mode_offers_only_the_media_address_to_the_browser(self):
        out = render(RELAY)
        self.assertIn("icesupport=yes", out["rtp"])
        self.assertNotIn("ice_host_candidates", out["rtp"])
        self.assertIn("rtpstart=10000\nrtpend=10199", out["rtp"])
        self.assertNotIn("external_media_address", out["pjsip"])
        # Signalling still names the host, exactly as in direct mode.
        self.assertIn("external_signaling_address=192.168.1.5", out["pjsip"])
        webrtc = section(out["pjsip"], "webrtc", "endpoint")
        self.assertIn("media_address=172.30.0.3\nbind_rtp_to_media_address=yes", webrtc)

    def test_relay_mode_leaves_the_ims_leg_as_it_was(self):
        direct, relay = render(), render(RELAY)
        for type_ in ("transport", "registration", "endpoint", "aor", "identify"):
            self.assertEqual(section(direct["pjsip"], "volte_ims", type_),
                             section(relay["pjsip"], "volte_ims", type_))
        self.assertNotIn("media_address", section(relay["pjsip"], "volte_ims", "endpoint"))

    def test_relay_mode_without_a_media_address_binds_nothing_specific(self):
        out = render(RELAY, interface=("", ""))
        self.assertNotIn("bind_rtp_to_media_address", out["pjsip"])
        self.assertNotIn("ice_host_candidates", out["rtp"])

    def test_the_media_interface_admits_only_rtp_to_specifically_bound_sockets(self):
        rules = engine_render().media_ruleset("eth1", 10000, 10199)
        self.assertIn('iifname "eth1" udp dport 10000-10199 socket wildcard 0 accept', rules)
        self.assertIn('iifname "eth1" drop', rules)
        self.assertIn("table inet mdd_media", rules)
        # Reloadable in place: the table is created, dropped and defined again.
        self.assertLess(rules.index("delete table inet mdd_media"),
                        rules.index("table inet mdd_media {"))

    def test_the_legacy_fallback_admits_only_the_lines_rtp_range_on_the_media_interface(self):
        rules = engine_render().media_legacy_ruleset("eth1", 12000, 12011)
        lines = rules.splitlines()
        self.assertEqual(lines[0], "*filter")
        self.assertEqual(lines[-1], "COMMIT")
        self.assertEqual([line for line in lines if line.startswith("-A")], [
            "-A INPUT -i eth1 -p udp -m udp --dport 12000:12011 -j ACCEPT",
            "-A INPUT -i eth1 -j DROP",
        ])
        # The socket match is what a 4.4 kernel lacks; nothing here may need it.
        self.assertNotIn("socket", rules)
        # Replaces the table rather than appending, so loading it again changes nothing.
        self.assertIn(":INPUT ACCEPT [0:0]", lines)

    def test_render_takes_the_legacy_port_range_from_the_line(self):
        module = engine_render()
        ctx = module.build_context(instance_json())
        self.assertIn(f"--dport {ctx['rtp_start']}:{ctx['rtp_end']} ",
                      module.media_legacy_ruleset("eth1", ctx["rtp_start"], ctx["rtp_end"]))
        self.assertEqual((ctx["rtp_start"], ctx["rtp_end"]), (12000, 12011))

    def test_the_media_interface_is_found_by_subnet(self):
        module = engine_render()
        out = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
               "40: eth0@if41    inet 172.17.0.2/16 brd 172.17.255.255 scope global eth0\n"
               "42: eth1@if43    inet 172.30.0.3/16 brd 172.30.255.255 scope global eth1\n"
               "44: ipsec0    inet 10.44.1.2/32 scope global ipsec0\n")
        with patch.object(module.subprocess, "check_output", return_value=out):
            self.assertEqual(module.media_interface("172.30.0.0/16"), ("eth1", "172.30.0.3"))
            self.assertEqual(module.media_interface("10.9.0.0/16"), ("", ""))

    def test_render_writes_the_ruleset_and_mode_only_in_relay_mode(self):
        for media, expected in ((None, False), (RELAY, True)):
            module = engine_render()
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp, "instance.json")
                cfg_path.write_text(__import__("json").dumps(instance_json(media)))
                env_path = Path(tmp, "run", "engine.env")
                outputs = Path(tmp, "out")
                with patch.object(module, "CFG_PATH", str(cfg_path)), \
                        patch.object(module, "TPL_DIR", str(TEMPLATES)), \
                        patch.object(module, "media_interface",
                                     return_value=("eth1", "172.30.0.3")), \
                        patch.dict(os.environ, {"MDD_ENV": str(env_path)}), \
                        patch.object(module.os, "makedirs", lambda *a, **k: None), \
                        patch("builtins.open", _redirect_open(outputs, tmp)):
                    Path(tmp, "run").mkdir()
                    outputs.mkdir()
                    module.main()
                env_text = env_path.read_text()
                self.assertEqual(Path(tmp, "run", "media.nft").exists(), expected)
                self.assertEqual(Path(tmp, "run", "media.iptables").exists(), expected)
                if expected:
                    self.assertIn("-A INPUT -i eth1 -p udp -m udp --dport 10000:10199 -j ACCEPT",
                                  Path(tmp, "run", "media.iptables").read_text())
                self.assertEqual("MDD_MEDIA_MODE=relay" in env_text, expected)
                self.assertEqual("MDD_MEDIA_IF=eth1" in env_text, expected)


def _redirect_open(outputs: Path, tmp: str):
    """Send render.main's writes to /etc and /usr into a scratch directory."""
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        path = str(path)
        if not path.startswith(tmp) and ("w" in mode):
            path = str(outputs / path.strip("/").replace("/", "_"))
        return real_open(path, mode, *args, **kwargs)
    return fake_open


class EntrypointMediaFirewallTests(unittest.TestCase):
    """The entrypoint's relay-mode block, extracted and run for real with stub nft, iptables
    and ip tools: nftables first, iptables-legacy only when the kernel refuses it."""

    def run_block(self, *, nft, legacy4=0, legacy6=0, ipv6=True, media_if="eth1"):
        text = (ROOT / "engine" / "entrypoint.sh").read_text()
        block = text[text.index("load_media_firewall() {"):text.index("# --- 2. ")]
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp, "calls")
            for name, code in (("nft", nft), ("iptables-legacy-restore", legacy4),
                               ("ip6tables-legacy-restore", legacy6), ("ip", 0)):
                tool = Path(tmp, name)
                tool.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit {code}\n')
                tool.chmod(0o755)
            for name in ("media.nft", "media.iptables"):
                Path(tmp, name).write_text("# rendered\n")
            inet6 = Path(tmp, "if_inet6")
            if ipv6:
                inet6.write_text("")
            script = ("set -u\nlog() { echo \"$*\"; }\n"
                      + block.replace("/proc/net/if_inet6", str(inet6)))
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                env={"PATH": f"{tmp}:/usr/bin:/bin", "MDD_RUNDIR": tmp,
                     "MDD_MEDIA_MODE": "relay", "MDD_MEDIA_IF": media_if})
            self.assertEqual(result.returncode, 0, result.stderr)
            ran = [line.split()[0] for line in calls.read_text().splitlines()] \
                if calls.exists() else []
            report = json.loads(Path(tmp, "media.json").read_text())
        return report, ran, result.stdout

    def test_a_kernel_that_takes_nftables_never_touches_iptables(self):
        report, ran, _out = self.run_block(nft=0)
        self.assertEqual(report, {"mode": "relay", "state": "ready", "filter": "nft"})
        self.assertEqual(ran, ["nft"])

    def test_a_kernel_without_nftables_falls_back_to_iptables_legacy_for_both_families(self):
        report, ran, out = self.run_block(nft=1)
        self.assertEqual(report, {"mode": "relay", "state": "ready",
                                  "filter": "iptables-legacy"})
        self.assertEqual(ran, ["nft", "iptables-legacy-restore", "ip6tables-legacy-restore"])
        self.assertIn("trying iptables-legacy", out)

    def test_a_kernel_without_ipv6_loads_only_the_ipv4_rules(self):
        report, ran, _out = self.run_block(nft=1, legacy6=1, ipv6=False)
        self.assertEqual(report["filter"], "iptables-legacy")
        self.assertEqual(ran, ["nft", "iptables-legacy-restore"])

    def test_when_neither_loads_the_media_interface_goes_down(self):
        for legacy4, legacy6 in ((1, 0), (0, 1)):
            report, ran, out = self.run_block(nft=1, legacy4=legacy4, legacy6=legacy6)
            self.assertEqual(report, {"mode": "relay", "state": "firewall_failed", "filter": ""})
            self.assertEqual(ran[-1], "ip")
            self.assertIn("relay media: firewall_failed", out)

    def test_without_a_media_address_nothing_is_loaded(self):
        report, ran, _out = self.run_block(nft=0, media_if="")
        self.assertEqual(report["state"], "no_media_address")
        self.assertEqual(ran, [])


class EngineAddressTests(unittest.TestCase):
    """On the container stack the engine network is internal and the uplink is joined after
    start, so the first render has no default route: the address comes from the list, where
    the media network may come first (review of #181)."""

    def address(self, listed, exclude, gateway=""):
        module = engine_render()
        with patch.object(module, "_default_gateway_ipv4", return_value=gateway), \
                patch.object(module.subprocess, "check_output", return_value=listed):
            return module.container_ipv4(exclude)

    def test_the_media_address_is_skipped_when_it_is_listed_first(self):
        self.assertEqual(self.address("172.30.0.3 172.18.0.5 ", "172.30.0.0/16"), "172.18.0.5")

    def test_direct_mode_takes_the_first_address_as_before(self):
        self.assertEqual(self.address("172.30.0.3 172.18.0.5 ", ""), "172.30.0.3")

    def test_only_a_media_address_is_refused_rather_than_used(self):
        module = engine_render()
        fake = Mock()
        fake.getsockname.return_value = ("172.30.0.3", 1)
        with patch.object(module, "_default_gateway_ipv4", return_value=""), \
                patch.object(module.subprocess, "check_output", return_value="172.30.0.3"), \
                patch.object(module.socket, "socket", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "outside the media network"):
                module.container_ipv4("172.30.0.0/16")

    def test_relay_mode_keeps_ike_and_sip_off_the_media_network(self):
        module = engine_render()
        cfg = instance_json(RELAY)
        cfg.pop("local_addr")
        with patch.object(module, "media_interface", return_value=("eth0", "172.30.0.3")), \
                patch.object(module, "_default_gateway_ipv4", return_value=""), \
                patch.object(module.subprocess, "check_output",
                             return_value="172.30.0.3 172.18.0.5"):
            ctx = module.build_context(cfg)
        self.assertEqual((ctx["local_addr"], ctx["rtp_bind_addr"], ctx["media_addr"]),
                         ("172.18.0.5", "172.18.0.5", "172.30.0.3"))


class SoftphoneListenerAddressTests(unittest.TestCase):
    """#195: a line going direct joins the uplink after start. Its default route then makes
    local_addr the uplink address, but the relay connects on the Engine network."""

    def context(self, engine_subnet, addresses):
        module = engine_render()
        cfg = instance_json()
        cfg["local_addr"] = "172.19.0.3"
        if engine_subnet:
            cfg["engine_subnet"] = engine_subnet

        def interface(subnet):
            return addresses.get(subnet, ("", ""))

        with patch.object(module, "media_interface", side_effect=interface):
            return module.build_context(cfg)

    def test_direct_line_listens_on_the_engine_network_not_the_uplink(self):
        ctx = self.context("172.21.0.0/16", {"172.21.0.0/16": ("eth0", "172.21.0.4")})
        self.assertEqual(ctx["webrtc_ws_addr"], "172.21.0.4")
        # IKE and the IMS leg still leave through the uplink.
        self.assertEqual((ctx["local_addr"], ctx["rtp_bind_addr"]), ("172.19.0.3", "172.19.0.3"))

    def test_without_an_engine_network_the_listener_stays_on_the_bridge_address(self):
        for subnet, addresses in (("", {}), ("172.21.0.0/16", {})):
            with self.subTest(subnet=subnet):
                self.assertEqual(self.context(subnet, addresses)["webrtc_ws_addr"], "172.19.0.3")


class MediaInstanceJsonTests(unittest.TestCase):
    def instance(self, **extra):
        return {"id": "3", "index": 1, "imsi": "001010000000000", "mcc": "001", "mnc": "01",
                "ami_secret": "a", "ports": config._alloc_ports(1),
                "sip": {"webrtc": {"enable": True, "password": "p"}}, **extra}

    def test_direct_mode_keeps_the_lines_own_rtp_block(self):
        rendered = config.render_instance_json(self.instance(), {})
        self.assertNotIn("media", rendered)
        self.assertEqual(rendered["rtp_start"], config._alloc_ports(1)["rtp_start"])


if __name__ == "__main__":
    unittest.main()
