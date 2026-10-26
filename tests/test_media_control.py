"""The control plane's part in the media modes: moving running lines and telling clients."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from control.app import main, media
from control.app import state as app_state

INSTANCE = {"id": "1", "mcc": "001", "mnc": "01",
            "sip": {"webrtc": {"enable": True, "username": "webrtc", "password": "p"}}}


def _request(host="gw.example:10443"):
    return SimpleNamespace(headers={"host": host},
                           url=SimpleNamespace(hostname=host.rsplit(":", 1)[0]))


class MediaControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(media.cfg, "DATA_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def converge(self, modes, failing=(), now=1000.0, pending=()):
        """One pass. A line that starts lands in the recorded mode, or relay-pending when
        it is in ``pending`` (the media network could not be prepared)."""
        lines = [{"id": iid} for iid in modes]
        started, order = [], []

        def start(inst, *args):
            order.append(("start", inst["id"]))
            if inst["id"] in failing:
                raise RuntimeError("exit unavailable")
            started.append((inst["id"], args))
            modes[inst["id"]] = media.RELAY_PENDING if inst["id"] in pending else media.mode()

        async def drop_ami(iid):
            order.append(("drop_ami", iid))

        with patch.object(main.cfg, "list_instances", return_value=lines), \
                patch.object(main.cfg, "get_instance", side_effect=lambda iid: {"id": iid}), \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main.engine, "media_mode_of", side_effect=modes.get), \
                patch.object(app_state, "_start_engine_checked", side_effect=start), \
                patch.object(app_state, "_record_lifecycle"), \
                patch.object(main.time, "monotonic", return_value=now), \
                patch.object(app_state.hub, "drop_ami", side_effect=drop_ami), \
                patch.object(app_state.hub, "reset_health"):
            rebuilt = asyncio.run(main._media_converge_once())
        return rebuilt, started, order

    def setUp_retry(self):
        main._media_retry.clear()
        self.addCleanup(main._media_retry.clear)

    def test_lines_already_in_the_recorded_mode_are_left_alone(self):
        self.setUp_retry()
        rebuilt, started, _order = self.converge({"1": "direct", "2": None})
        self.assertFalse(rebuilt)
        self.assertEqual(started, [])

    def test_one_line_at_a_time_moves_to_the_recorded_mode(self):
        self.setUp_retry()
        media.save_state({"mode": "relay", "secret": "x"})
        rebuilt, started, order = self.converge({"1": "relay", "2": "direct", "3": "direct"})
        self.assertTrue(rebuilt)
        self.assertEqual([iid for iid, _ in started], ["2"])
        self.assertEqual(started[0][1][-1], "media_mode")
        # The AMI connection is dropped once the old container is actually gone.
        self.assertEqual(order, [("start", "2"), ("drop_ami", "2")])

    def test_a_line_that_cannot_be_rebuilt_backs_off_and_the_next_one_goes_ahead(self):
        """Review of #181: a failure before the old container is removed left the line in
        the old mode, so every pass picked it again and the lines after it never moved."""
        self.setUp_retry()
        media.save_state({"mode": "relay", "secret": "x"})
        modes = {"1": "direct", "2": "direct"}
        rebuilt, started, order = self.converge(modes, failing={"1"})
        self.assertTrue(rebuilt)
        self.assertEqual([iid for iid, _ in started], ["2"])
        self.assertNotIn(("drop_ami", "1"), order)
        self.assertEqual(main._media_retry["1"]["failures"], 1)

        modes["2"] = "relay"
        _rebuilt, _started, order = self.converge(modes, failing={"1"}, now=1030.0)
        self.assertEqual(order, [])                       # still backing off
        _rebuilt, _started, order = self.converge(modes, failing={"1"}, now=1061.0)
        self.assertEqual(order, [("start", "1")])
        self.assertEqual(main._media_retry["1"]["failures"], 2)
        self.assertEqual(main._media_retry["1"]["at"], 1061.0 + 120)

    def test_the_backoff_is_bounded(self):
        self.setUp_retry()
        media.save_state({"mode": "relay", "secret": "x"})
        main._media_retry["1"] = {"at": 0.0, "failures": 9}
        self.converge({"1": "direct"}, failing={"1"})
        self.assertEqual(main._media_retry["1"]["at"], 1000.0 + main.MEDIA_RETRY_MAX_SECONDS)

    def test_a_line_left_pending_is_not_rebuilt_on_every_pass(self):
        """Review of #181: a start that lands in relay-pending succeeded, so the line used to
        be rebuilt, and re-register, every 15 s for as long as the network could not be had."""
        self.setUp_retry()
        media.save_state({"mode": "relay", "secret": "x"})
        modes = {"1": "direct"}
        rebuilds = []
        for second in range(0, 60, 15):                    # four passes, 15 s apart
            _rebuilt, started, order = self.converge(modes, pending={"1"},
                                                     now=1000.0 + second)
            rebuilds += started
            if started:
                self.assertIn(("drop_ami", "1"), order)   # its old container is gone
        self.assertEqual(len(rebuilds), 1)
        self.assertEqual(main._media_retry["1"]["failures"], 1)
        _rebuilt, started, _order = self.converge(modes, pending={"1"}, now=1061.0)
        self.assertEqual(len(started), 1)
        self.assertEqual(main._media_retry["1"]["failures"], 2)
        # Once the network can be had the line lands in relay mode and its backoff is gone.
        _rebuilt, started, _order = self.converge(modes, now=1061.0 + 121)
        self.assertEqual((len(started), modes["1"]), (1, "relay"))
        self.assertNotIn("1", main._media_retry)

    def test_a_line_started_without_the_media_network_is_tried_again(self):
        self.setUp_retry()
        media.save_state({"mode": "relay", "secret": "x"})
        rebuilt, started, _order = self.converge({"1": media.RELAY_PENDING})
        self.assertTrue(rebuilt)
        self.assertEqual([iid for iid, _ in started], ["1"])

    def test_stopped_lines_are_not_started_by_a_mode_change(self):
        self.setUp_retry()
        media.save_state({"mode": "relay", "secret": "x"})
        rebuilt, started, _order = self.converge({"1": None})
        self.assertFalse(rebuilt)

    def test_media_status_names_lines_missing_the_media_network(self):
        media.save_state({"mode": "relay", "port": 8478, "secret": "x"})
        modes = {"1": media.RELAY, "2": media.RELAY_PENDING, "3": None}
        with patch.object(main.cfg, "list_instances",
                          return_value=[{"id": iid} for iid in modes]), \
                patch.object(main.engine, "media_mode_of", side_effect=modes.get), \
                patch.object(main, "_line_media_report",
                             return_value={"state": "ready", "filter": "nft"}):
            status = main.api_media()
        self.assertEqual(status["lines"], {"1": "ready", "2": "no_media_network"})

    def test_media_status_says_which_filter_the_engines_use(self):
        modes = {"1": media.RELAY}
        for recorded, report, separates in (
                # Recorded before the fallback existed: only nftables could have enabled it.
                ({}, {"state": "ready"}, True),
                ({"filter": "nft"}, {"state": "ready", "filter": "nft"}, True),
                ({"filter": "iptables-legacy"},
                 {"state": "ready", "filter": "iptables-legacy"}, False)):
            media.save_state({"mode": "relay", "port": 8478, "secret": "x", **recorded})
            with patch.object(main.cfg, "list_instances", return_value=[{"id": "1"}]), \
                    patch.object(main.engine, "media_mode_of", side_effect=modes.get), \
                    patch.object(main, "_line_media_report", return_value=report):
                status = main.api_media()
            self.assertEqual(status["filter"], recorded.get("filter", "nft"))
            self.assertEqual(status["filter_separates_legs"], separates)
            self.assertEqual(status["line_filters"], {"1": report.get("filter", "")})

    def test_direct_mode_status_carries_no_filter(self):
        self.assertNotIn("filter", main.api_media())

    def test_direct_mode_provisioning_carries_no_ice_servers(self):
        with patch.object(main.cfg, "get_instance", return_value=INSTANCE):
            prov = main.api_softphone("1", _request())
        self.assertEqual(prov["media_mode"], "direct")
        self.assertEqual(prov["ice_servers"], [])
        self.assertEqual(prov["ice_transport_policy"], "all")

    def test_relay_provisioning_is_ready_only_when_relay_and_line_both_are(self):
        media.save_state({"mode": "relay", "port": 8478, "secret": "x"})
        for relay_state, line_state, ready in (("ready", "ready", True),
                                               ("ready", "firewall_failed", False),
                                               ("unavailable", "ready", False)):
            with patch.object(main.cfg, "get_instance", return_value=INSTANCE), \
                    patch.object(media, "relay_status", return_value={"state": relay_state}), \
                    patch.object(main, "_line_media_state", return_value=line_state):
                prov = main.api_softphone("1", _request())
            self.assertEqual(prov["relay_ready"], ready, (relay_state, line_state))
            self.assertEqual(prov["ice_transport_policy"], "relay")
            self.assertEqual(prov["ice_servers"][0]["urls"][0],
                             "turn:gw.example:8478?transport=udp")


if __name__ == "__main__":
    unittest.main()
