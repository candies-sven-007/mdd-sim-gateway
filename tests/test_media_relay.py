import base64
import json
import hashlib
import hmac
import importlib
import importlib.util
import ipaddress
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def _docker():
    not_found = type("NotFound", (Exception,), {})
    return SimpleNamespace(
        from_env=lambda **_: None,
        errors=SimpleNamespace(NotFound=not_found,
                               ImageNotFound=type("ImageNotFound", (not_found,), {}),
                               APIError=type("APIError", (Exception,), {})),
    )


def media_module():
    with patch.dict(sys.modules, {"docker": _docker()}):
        for name in ("control.app.media", "control.app.engine"):
            sys.modules.pop(name, None)
        media = importlib.import_module("control.app.media")
        # patch.dict drops both modules again on exit; keep the engine the media module saw.
        media._test_engine = importlib.import_module("control.app.engine")
        return media


class _Network:
    def __init__(self, subnet="172.30.0.0/16", gateway="172.30.0.1", internal=True,
                 managed=True, containers=None):
        self.attrs = {
            "Internal": internal,
            "Labels": {"io.mdd-sim-gateway.managed": "true"} if managed else {},
            "IPAM": {"Config": [{"Subnet": subnet, "Gateway": gateway}]},
            "Containers": containers or {},
        }
        self.connected = []
        self.removed = False

    def reload(self):
        pass

    def connect(self, container):
        self.connected.append(container)

    def remove(self):
        self.removed = True


class _Container:
    def __init__(self, fingerprint="", status="running", exit_code=0):
        self.attrs = {"Config": {"Labels": {"io.mdd-sim-gateway.managed": "true",
                                            "io.mdd-sim-gateway.relay-config": fingerprint}}}
        self.status = status
        self.exit_code = exit_code
        self.removed = False
        self.started = False

    def remove(self, force=False, v=False):
        self.removed = True
        self.volumes_removed = v

    def start(self):
        self.started = True
        self.status = "running"

    def exec_run(self, cmd, demux=False):
        return SimpleNamespace(exit_code=self.exit_code, output=b"")


class _Client:
    def __init__(self, media, network=None, container=None, image_id="sha256:relay"):
        self.media = media
        self.network = network
        self.container = container
        self.created = []
        client = self

        class Networks:
            def get(self, name):
                if client.network is None:
                    raise media.docker.errors.NotFound(name)
                return client.network

            def create(self, name, **kwargs):
                client.network = _Network()
                client.network.create_kwargs = kwargs
                return client.network

        class Containers:
            def get(self, name):
                if client.container is None or client.container.removed:
                    raise media.docker.errors.NotFound(name)
                return client.container

            def create(self, image, **kwargs):
                fingerprint = kwargs["labels"][media.CONFIG_LABEL]
                client.container = _Container(fingerprint, status="created")
                client.created.append(kwargs)
                return client.container

        class Images:
            def get(self, reference):
                return SimpleNamespace(id=image_id)

            def pull(self, reference):
                raise AssertionError("no pull expected")

        self.networks = Networks()
        self.containers = Containers()
        self.images = Images()


class MediaRelayTests(unittest.TestCase):
    def setUp(self):
        self.media = media_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(self.media.cfg, "DATA_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        host = patch.object(self.media._test_engine, "HOST_DATA_DIR", "/host/data")
        host.start()
        self.addCleanup(host.stop)
        engine_module = patch.dict(sys.modules, {"control.app.engine": self.media._test_engine})
        engine_module.start()
        self.addCleanup(engine_module.stop)

    def test_no_recorded_mode_is_direct_and_hands_out_no_ice_servers(self):
        self.assertEqual(self.media.mode(), "direct")
        self.assertEqual(self.media.provisioning("1", "gw.example"), {
            "media_mode": "direct", "ice_servers": [], "ice_transport_policy": "all",
            "relay_ready": None})

    def test_the_relay_may_reach_the_media_network_but_not_the_host(self):
        ranges = self.media.peer_ranges(ipaddress.ip_network("172.30.0.0/16"),
                                        ipaddress.ip_address("172.30.0.1"))
        self.assertEqual(ranges, [("172.30.0.2", "172.30.255.254")])
        ranges = self.media.peer_ranges(ipaddress.ip_network("10.9.8.0/24"),
                                        ipaddress.ip_address("10.9.8.100"))
        self.assertEqual(ranges, [("10.9.8.1", "10.9.8.99"), ("10.9.8.101", "10.9.8.254")])

    def test_relay_config_denies_everything_but_udp_to_the_media_network(self):
        config = self.media.render_config("s3cret", ipaddress.ip_network("172.30.0.0/16"),
                                          ipaddress.ip_address("172.30.0.1"))
        lines = config.splitlines()
        self.assertIn("no-tcp-relay", lines)
        self.assertIn("denied-peer-ip=0.0.0.0-255.255.255.255", lines)
        self.assertIn("denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", lines)
        self.assertEqual([line for line in lines if line.startswith("allowed-peer-ip")],
                         ["allowed-peer-ip=172.30.0.2-172.30.255.254"])
        self.assertIn("static-auth-secret=s3cret", lines)
        self.assertFalse(any(line.startswith(("relay-ip", "listening-ip")) for line in lines))

    def test_credentials_follow_the_turn_rest_scheme(self):
        creds = self.media.credentials("3", "s3cret", ttl=60, now=1000)
        self.assertEqual(creds["username"], "1060:3")
        expected = base64.b64encode(
            hmac.new(b"s3cret", b"1060:3", hashlib.sha1).digest()).decode()
        self.assertEqual(creds["credential"], expected)

    def test_relay_provisioning_names_the_host_clients_reached(self):
        self.media.save_state({"mode": "relay", "port": 8478, "public_port": 3479,
                               "secret": "s3cret"})
        prov = self.media.provisioning("2", "fd00::5")
        self.assertEqual(prov["media_mode"], "relay")
        self.assertEqual(prov["ice_transport_policy"], "relay")
        self.assertEqual(prov["ice_servers"][0]["urls"],
                         ["turn:[fd00::5]:3479?transport=udp", "turn:[fd00::5]:3479?transport=tcp"])
        self.media.save_state({"mode": "relay", "port": 8478, "public_host": "gw.example",
                               "secret": "s3cret"})
        prov = self.media.provisioning("2", "192.168.1.5")
        self.assertEqual(prov["ice_servers"][0]["urls"][0], "turn:gw.example:8478?transport=udp")

    def test_state_file_is_private(self):
        self.media.save_state({"mode": "relay", "secret": "x"})
        mode = os.stat(os.path.join(self.tmp.name, "media", "state.json")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_relay_container_holds_no_capability_and_is_not_replaced_needlessly(self):
        network = _Network()
        client = _Client(self.media, network=network)
        state = {"mode": "relay", "port": 8478, "secret": "s3cret", "image": "relay:test"}
        first = self.media.ensure_relay(client, state)
        kwargs = client.created[0]
        self.assertEqual(kwargs["cap_drop"], ["ALL"])
        # Only so the stock turnserver may be executed; no-new-privileges never grants it.
        self.assertEqual(kwargs["cap_add"], ["NET_BIND_SERVICE"])
        self.assertIn("no-new-privileges:true", kwargs["security_opt"])
        self.assertEqual(kwargs["entrypoint"], ["sh", "-c", self.media.ENTRYPOINT])
        self.assertTrue(kwargs["read_only"])
        # Upstream's VOLUME is covered, so no anonymous volume piles up per relay.
        self.assertIn("/var/lib/coturn", kwargs["tmpfs"])
        self.assertEqual(kwargs["network"], "bridge")
        self.assertEqual(kwargs["ports"], {"3478/udp": 8478, "3478/tcp": 8478})
        self.assertEqual(kwargs["environment"], {"MDD_MEDIA_SUBNET": "172.30.0.0/16"})
        # The TURN secret is in a read-only file, not in what `docker inspect` shows.
        self.assertEqual(kwargs["volumes"], {"/host/data/media/relay.conf": {
            "bind": "/etc/mdd-relay/turnserver.conf", "mode": "ro"}})
        written = Path(self.tmp.name, "media", "relay.conf")
        self.assertIn("static-auth-secret=s3cret", written.read_text())
        self.assertEqual(os.stat(written).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(written.parent).st_mode & 0o777, 0o700)
        self.assertEqual(network.connected, [first])
        self.assertTrue(first.started)

        again = self.media.ensure_relay(client, state)
        self.assertIs(again, first)
        self.assertEqual(len(client.created), 1)

        self.media.ensure_relay(client, {**state, "port": 9478})
        self.assertTrue(first.removed)
        self.assertTrue(first.volumes_removed)
        self.assertEqual(len(client.created), 2)

    def test_a_foreign_network_of_the_same_name_is_refused(self):
        client = _Client(self.media, network=_Network(managed=False))
        with self.assertRaisesRegex(self.media.MediaError, "not created by MDD"):
            self.media.ensure_network(client)
        client = _Client(self.media, network=_Network(internal=False))
        with self.assertRaisesRegex(self.media.MediaError, "not internal"):
            self.media.ensure_network(client)

    def test_the_media_network_is_created_internal_without_a_fixed_subnet(self):
        client = _Client(self.media)
        self.media.ensure_network(client)
        kwargs = client.network.create_kwargs
        self.assertTrue(kwargs["internal"])
        self.assertNotIn("ipam", kwargs)

    def test_a_host_that_cannot_filter_engine_media_is_refused_before_anything_moves(self):
        client = _Client(self.media)
        with patch.object(self.media, "probe_engine_firewall",
                          side_effect=self.media.MediaError("no socket match")):
            with self.assertRaisesRegex(self.media.MediaError, "no socket match"):
                self.media.enable(client, port=8478, image="relay:test")
        self.assertEqual(self.media.load_state(), {})
        self.assertEqual(client.created, [])
        self.assertTrue(client.network.removed)

    def probe(self, output):
        client = _Client(self.media, network=_Network())
        client.containers.run = Mock(return_value=output)
        engine = self.media._test_engine
        with patch.dict(sys.modules, {"control.app.engine": engine}), \
                patch.object(engine, "ensure_image", return_value=SimpleNamespace(id="sha256:eng")):
            kind = self.media.probe_engine_firewall(client, SimpleNamespace(name="media-net"))
        return kind, client.containers.run.call_args

    def test_the_probe_loads_the_engine_ruleset_in_a_throwaway_engine(self):
        kind, (args, kwargs) = self.probe(b"filter=nft\n")
        self.assertEqual(kind, "nft")
        self.assertEqual(args[0], "sha256:eng")
        self.assertTrue(kwargs["remove"])
        self.assertEqual(kwargs["network"], "media-net")
        self.assertIn("socket wildcard 0 accept", kwargs["environment"]["RULES"])
        self.assertIn("--dport 10000:10199", kwargs["environment"]["LEGACY_RULES"])

    def test_the_probe_reports_the_iptables_legacy_fallback(self):
        # stderr is part of the output: nft's refusal comes before the result.
        kind, _call = self.probe(b"Error: Could not process rule: No such file or directory\n"
                                 b"nftables ruleset refused, trying iptables-legacy\n"
                                 b"filter=iptables-legacy\n")
        self.assertEqual(kind, "iptables-legacy")
        self.assertFalse(self.media.filter_separates_legs(kind))
        self.assertTrue(self.media.filter_separates_legs("nft"))

    def test_the_probe_output_survives_a_daemon_that_does_not_log_to_json(self):
        # Synology's Docker logs to its own `db` driver, and docker-py then hands back None
        # for a finished container's output: relay mode was refused on every DSM host.
        _kind, (_args, kwargs) = self.probe(b"filter=iptables-legacy\n")
        self.assertEqual(kwargs["log_config"]["type"], "json-file")

    def test_a_probe_without_a_result_is_a_failure(self):
        with self.assertRaisesRegex(self.media.MediaError, "no result"):
            self.probe(b"")

    def test_enabling_records_the_filter_the_probe_found(self):
        client = _Client(self.media)
        with patch.object(self.media, "probe_engine_firewall", return_value="iptables-legacy"), \
                patch.object(self.media, "wait_ready", return_value=(True, "")):
            self.media.enable(client, port=8478, image="relay:test")
        self.assertEqual(self.media.load_state()["filter"], "iptables-legacy")
        self.assertEqual(self.media.recorded_filter({}), "nft")

    def test_a_relay_that_never_answers_leaves_direct_mode_and_nothing_behind(self):
        client = _Client(self.media)
        with patch.object(self.media, "probe_engine_firewall"), \
                patch.object(self.media, "wait_ready", return_value=(False, "relay_not_answering")):
            with self.assertRaisesRegex(self.media.MediaError, "did not become ready"):
                self.media.enable(client, port=8478, image="relay:test")
        self.assertEqual(self.media.mode(), "direct")
        self.assertEqual(self.media.load_state(), {})
        self.assertTrue(client.container.removed)
        self.assertTrue(client.network.removed)

    def test_enabling_records_relay_mode_only_after_the_relay_answers(self):
        client = _Client(self.media)
        probe = patch.object(self.media, "probe_engine_firewall", return_value="nft")
        probe.start()
        self.addCleanup(probe.stop)
        with patch.object(self.media, "wait_ready", return_value=(True, "")):
            state = self.media.enable(client, port=8478, image="relay:test")
        self.assertEqual(self.media.mode(), "relay")
        self.assertEqual(state["public_port"], 8478)
        self.assertTrue(state["secret"])
        # The secret survives a later change of port, so issued credentials stay valid.
        with patch.object(self.media, "wait_ready", return_value=(True, "")):
            again = self.media.enable(client, port=9000, image="relay:test")
        self.assertEqual(again["secret"], state["secret"])

    def test_the_media_network_outlives_engines_still_attached(self):
        network = _Network(containers={"abc": {"Name": "mdd-sim-gateway-engine-1"}})
        client = _Client(self.media, network=network)
        self.assertFalse(self.media.remove_network(client))
        self.assertFalse(network.removed)
        network.attrs["Containers"] = {}
        self.assertTrue(self.media.remove_network(client))
        self.assertTrue(network.removed)

    def test_engines_join_nothing_in_direct_mode(self):
        self.assertIsNone(self.media.engine_attachment(_Client(self.media)))

    def test_engines_learn_the_media_subnet_and_shared_rtp_range_in_relay_mode(self):
        self.media.save_state({"mode": "relay", "secret": "x"})
        attachment = self.media.engine_attachment(_Client(self.media, network=_Network()))
        self.assertEqual(attachment["instance"], {
            "mode": "relay", "subnet": "172.30.0.0/16", "rtp_start": 10000, "rtp_end": 10199})

    def test_supervision_keeps_the_previous_image_when_the_new_one_cannot_be_fetched(self):
        self.media.save_state({"mode": "relay", "secret": "x", "image": "relay:old"})
        client = _Client(self.media, network=_Network())
        self.media._image_retry_at = 0.0
        fetch = Mock(side_effect=[self.media.MediaError("offline"),
                                  SimpleNamespace(id="sha256:old")])
        with patch.object(self.media, "ensure_image", fetch), \
                patch.object(self.media, "default_image", return_value="relay:new"), \
                patch.object(self.media, "check_relay", return_value=(True, "")):
            status = self.media.supervise(client)
        self.assertEqual(status["state"], "ready")
        self.assertEqual(self.media.load_state()["image"], "relay:old")

    def test_a_failed_image_refresh_is_tried_again_an_hour_later(self):
        """Review of #181: a single failure used to stop retries until Control restarted."""
        self.media.save_state({"mode": "relay", "secret": "x", "image": "relay:old"})
        self.media._image_retry_at = 0.0
        tried = []

        def ensure_image(client, reference):
            tried.append(reference)
            if reference == "relay:new" and len(tried) == 1:
                raise self.media.MediaError("offline")
            return SimpleNamespace(id="sha256:x")

        clock = [1000.0]
        with patch.object(self.media, "ensure_image", side_effect=ensure_image), \
                patch.object(self.media, "default_image", return_value="relay:new"), \
                patch.object(self.media, "ensure_relay"), \
                patch.object(self.media, "check_relay", return_value=(True, "")), \
                patch.object(self.media.time, "monotonic", side_effect=lambda: clock[0]):
            self.media.supervise(Mock())
            clock[0] += 60
            self.media.supervise(Mock())
            self.assertEqual(tried.count("relay:new"), 1)
            clock[0] += self.media.IMAGE_RETRY_SECONDS
            self.media.supervise(Mock())
        self.assertEqual(tried.count("relay:new"), 2)
        self.assertEqual(self.media.load_state()["image"], "relay:new")

    def test_refreshing_the_image_never_writes_back_a_mode(self):
        """Review of #181: the image refresh used to save the whole state it had read."""
        self.media.save_state({"mode": "relay", "secret": "x", "image": "relay:old"})
        # The operator switched to direct after this pass read the state.
        with patch.object(self.media, "load_state",
                          return_value={"mode": "direct", "secret": "x", "image": "relay:old"}), \
                patch.object(self.media, "ensure_image"), \
                patch.object(self.media, "default_image", return_value="relay:new"):
            self.media._image_retry_at = 0.0
            with self.media.switch_lock():
                self.media._refresh_image(Mock(), {"mode": "relay", "image": "relay:old"})
        state = json.loads(Path(self.tmp.name, "media", "state.json").read_text())
        self.assertEqual((state["mode"], state["image"]), ("direct", "relay:new"))

    def test_direct_mode_removes_a_relay_left_behind(self):
        """Review of #181: a relay recreated by a supervision pass racing `direct` stayed up."""
        self.media.save_state({"mode": "direct", "secret": "x"})
        client = _Client(self.media, network=_Network(), container=_Container("x"))
        self.media.supervise(client)
        self.assertTrue(client.container.removed)
        self.assertTrue(client.network.removed)

    def test_once_direct_mode_is_clean_it_is_not_checked_again(self):
        """Review of #181: after one use of relay mode, direct mode used to ask Docker for a
        leftover relay every 30 s for good."""
        self.media.save_state({"mode": "direct", "secret": "x"})
        self.media._direct_clean_mtime = None
        client = Mock()
        client.containers.get.side_effect = self.media.docker.errors.NotFound("gone")
        client.networks.get.side_effect = self.media.docker.errors.NotFound("gone")
        self.media.supervise(client)
        calls = len(client.mock_calls)
        self.assertGreater(calls, 0)
        self.media.supervise(client)
        self.assertEqual(len(client.mock_calls), calls)
        # A new switch (the state file changes) is checked again.
        os.utime(Path(self.tmp.name, "media", "state.json"), (1, 1))
        self.media.supervise(client)
        self.assertGreater(len(client.mock_calls), calls)

    def test_direct_mode_keeps_checking_until_the_network_is_gone(self):
        self.media.save_state({"mode": "direct", "secret": "x"})
        self.media._direct_clean_mtime = None
        network = _Network(containers={"e": {"Name": "mdd-sim-gateway-engine-1"}})
        client = _Client(self.media, network=network)
        self.media.supervise(client)
        self.assertIsNone(self.media._direct_clean_mtime)   # an engine still holds it
        network.attrs["Containers"] = {}
        self.media.supervise(client)
        self.assertTrue(network.removed)
        self.assertIsNotNone(self.media._direct_clean_mtime)

    def test_a_gateway_that_never_used_relay_mode_makes_no_docker_call(self):
        client = Mock()
        self.assertEqual(self.media.supervise(client)["state"], "off")
        self.assertEqual(client.mock_calls, [])

    def test_supervision_stands_aside_while_a_switch_is_in_progress(self):
        self.media.save_state({"mode": "direct", "secret": "x"})
        client = _Client(self.media, network=_Network(), container=_Container("x"))
        with self.media.switch_lock():
            self.media.supervise(client)       # e.g. `enable` still waiting for its relay
        self.assertFalse(client.container.removed)

    def test_the_switch_holds_the_lock_while_it_prepares_the_relay(self):
        held = []

        def probe(client, network):
            with self.media.switch_lock(blocking=False) as free:
                held.append(not free)

        client = _Client(self.media)
        with patch.object(self.media, "probe_engine_firewall", side_effect=probe), \
                patch.object(self.media, "wait_ready", return_value=(True, "")):
            self.media.enable(client, port=8478, image="relay:test")
        self.assertEqual(held, [True])

    def test_a_probe_that_never_started_is_cleaned_up(self):
        leftover = Mock(attrs={"Config": {"Labels": {"io.mdd-sim-gateway.managed": "true"}}})
        client = Mock()
        client.containers.get.return_value = leftover
        client.containers.run.side_effect = RuntimeError("start failed")
        with patch.object(self.media._test_engine, "ensure_image",
                          return_value=SimpleNamespace(id="sha256:eng")):
            with self.assertRaises(self.media.MediaError):
                self.media.probe_engine_firewall(client, SimpleNamespace(name="media-net"))
        self.assertEqual(client.containers.run.call_args[1]["name"],
                         "mdd-sim-gateway-media-probe")
        self.assertEqual(leftover.remove.call_count, 2)    # before, and after the failure


class RelayImageTests(unittest.TestCase):
    def setUp(self):
        self.media = media_module()

    def client(self, available):
        pulled, tagged = [], []

        class Image:
            id = "sha256:relay"

            def tag(self, repository, tag):
                tagged.append(f"{repository}:{tag}")

        class Images:
            def get(self, reference):
                if reference in available or reference in pulled:
                    return Image()
                raise self_media.docker.errors.ImageNotFound(reference)

            def pull(self, reference):
                if reference not in available:
                    raise RuntimeError("unreachable")
                pulled.append(reference)

        self_media = self.media
        available = set(available)
        return SimpleNamespace(images=Images()), pulled, tagged

    def test_the_relay_is_the_pinned_upstream_image(self):
        self.assertRegex(self.media.UPSTREAM_IMAGE,
                         r"^coturn/coturn:4\.17\.2-alpine@sha256:[0-9a-f]{64}$")
        self.assertEqual(self.media.default_image(),
                         f"mdd-sim-gateway/relay:v{self.media.VERSION}")

    def test_a_loaded_release_asset_is_used_as_is(self):
        client, pulled, _tagged = self.client({self.media.default_image()})
        self.media.ensure_image(client, self.media.default_image())
        self.assertEqual(pulled, [])

    def test_without_it_the_release_registry_then_upstream_are_tried(self):
        client, pulled, tagged = self.client({self.media.UPSTREAM_IMAGE})
        self.media.ensure_image(client, self.media.default_image())
        self.assertEqual(pulled, [self.media.UPSTREAM_IMAGE])
        self.assertEqual(tagged, [self.media.default_image()])

    def test_nothing_reachable_says_what_was_tried(self):
        client, _pulled, _tagged = self.client(set())
        with self.assertRaisesRegex(self.media.MediaError, "ghcr.io.*coturn/coturn"):
            self.media.ensure_image(client, self.media.default_image())


class RelayEntrypointTests(unittest.TestCase):
    """The entrypoint runs in the stock alpine image: busybox sh and awk, iproute's ip."""

    def run_entrypoint(self, addresses):
        media = media_module()
        ip_output = "\n".join(
            f"{n}: eth{n}    inet {address}/16 brd 0.0.0.0 scope global eth{n}\\       "
            "valid_lft forever preferred_lft forever"
            for n, address in enumerate(addresses.split()))
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp, "bin")
            bin_dir.mkdir()
            Path(bin_dir, "ip").write_text(
                "#!/bin/sh\nprintf '%s\\n' '1: lo    inet 127.0.0.1/8 scope host lo'\n"
                f"printf '%s\\n' '{ip_output}'\n")
            Path(bin_dir, "turnserver").write_text("#!/bin/sh\ncat \"$2\"\n")
            for name in ("ip", "turnserver"):
                os.chmod(Path(bin_dir, name), 0o755)
            Path(tmp, "relay.conf").write_text("realm=x\n")
            script = media.ENTRYPOINT.replace("/tmp/turnserver.conf", f"{tmp}/turnserver.conf") \
                .replace(media.RELAY_CONFIG_MOUNT, f"{tmp}/relay.conf")
            return subprocess.run(
                ["sh", "-c", script],
                env={"PATH": f"{bin_dir}:/usr/bin:/bin", "MDD_MEDIA_SUBNET": "172.30.0.0/16"},
                capture_output=True, text=True)

    def test_allocations_live_on_the_media_network_and_listeners_everywhere_else(self):
        result = self.run_entrypoint("172.17.0.5 172.30.0.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "realm=x")
        self.assertIn("relay-ip=172.30.0.3", lines)
        self.assertIn("listening-ip=172.17.0.5", lines)
        self.assertEqual(lines.count("listening-ip=127.0.0.1"), 1)
        self.assertNotIn("listening-ip=172.30.0.3", lines)

    def test_without_a_media_address_the_relay_does_not_start(self):
        result = self.run_entrypoint("172.17.0.5")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no address on the media network", result.stderr)


class ProbeScriptTests(unittest.TestCase):
    """The engine firewall probe's script run for real, with stub nft and iptables tools on
    PATH: it tries what the engine entrypoint tries, in the same order."""

    def run_probe(self, *, nft, legacy4, legacy6, ipv6=True):
        media = media_module()
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp, "calls")
            for name, code in (("nft", nft), ("iptables-legacy-restore", legacy4),
                               ("ip6tables-legacy-restore", legacy6)):
                tool = Path(tmp, name)
                tool.write_text(f'#!/bin/sh\ncat > "{tmp}/{name}.in"\n'
                                f'echo {name} >> "{calls}"\nexit {code}\n')
                tool.chmod(0o755)
            inet6 = Path(tmp, "if_inet6")
            if ipv6:
                inet6.write_text("")
            result = subprocess.run(
                ["sh", "-c", media.PROBE_SCRIPT.replace("/proc/net/if_inet6", str(inet6))],
                capture_output=True, text=True,
                env={"PATH": f"{tmp}:/usr/bin:/bin", "RULES": media.PROBE_RULESET,
                     "LEGACY_RULES": media.PROBE_LEGACY_RULESET})
            ran = calls.read_text().split() if calls.exists() else []
            fed = {name: Path(tmp, f"{name}.in").read_text() for name in ran}
        return result, ran, fed, media

    def test_nftables_alone_runs_where_the_kernel_takes_it(self):
        result, ran, fed, media = self.run_probe(nft=0, legacy4=0, legacy6=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "filter=nft")
        self.assertEqual(ran, ["nft"])
        self.assertEqual(fed["nft"], media.PROBE_RULESET)

    def test_iptables_legacy_is_loaded_for_ipv4_and_ipv6_when_nftables_is_refused(self):
        result, ran, fed, media = self.run_probe(nft=1, legacy4=0, legacy6=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "filter=iptables-legacy")
        self.assertEqual(ran, ["nft", "iptables-legacy-restore", "ip6tables-legacy-restore"])
        self.assertEqual(fed["iptables-legacy-restore"], media.PROBE_LEGACY_RULESET)
        self.assertEqual(fed["ip6tables-legacy-restore"], media.PROBE_LEGACY_RULESET)

    def test_a_kernel_without_ipv6_needs_only_the_ipv4_rules(self):
        result, ran, _fed, _media = self.run_probe(nft=1, legacy4=0, legacy6=1, ipv6=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ran, ["nft", "iptables-legacy-restore"])

    def test_neither_loading_fails_the_probe(self):
        for legacy4, legacy6 in ((1, 0), (0, 1)):
            result, _ran, _fed, _media = self.run_probe(nft=1, legacy4=legacy4, legacy6=legacy6)
            self.assertNotEqual(result.returncode, 0, (legacy4, legacy6))
            self.assertNotIn("filter=", result.stdout)

    def test_the_probe_loads_what_the_engine_renders(self):
        spec = importlib.util.spec_from_file_location("mdd_render_probe",
                                                      ROOT / "engine" / "render.py")
        render = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(render)
        media = media_module()
        self.assertEqual(media.PROBE_LEGACY_RULESET,
                         render.media_legacy_ruleset("eth0", *media.RTP_PORTS))


if __name__ == "__main__":
    unittest.main()
