import unittest
from types import SimpleNamespace
from unittest.mock import patch

from control.app import sysinfo


class ThrottlingDecodeTests(unittest.TestCase):
    """get_throttled is the only place the firmware admits to a brown-out: the NIC reports no
    errors, the link stays up, and the symptom surfaces minutes later as packet loss."""

    def _decode(self, raw):
        with patch.object(sysinfo, "_vcgencmd", return_value=raw):
            return sysinfo.throttling()

    def test_a_clean_board_reports_nothing(self):
        self.assertEqual(self._decode("throttled=0x0"),
                         {"raw": "0x0", "now": [], "since_boot": []})

    def test_current_and_historical_bits_are_separated(self):
        # 0x70002 was observed live: frequency-capped now, and undervoltage/capping/throttling
        # have all happened since boot.
        decoded = self._decode("throttled=0x70002")
        self.assertEqual(decoded["now"], ["frequency_capped"])
        self.assertEqual(decoded["since_boot"],
                         ["frequency_capped", "throttled", "undervoltage"])

    def test_undervoltage_right_now_is_distinguished_from_history(self):
        self.assertIn("undervoltage", self._decode("throttled=0x1")["now"])
        self.assertNotIn("undervoltage", self._decode("throttled=0x10000")["now"])
        self.assertIn("undervoltage", self._decode("throttled=0x10000")["since_boot"])

    def test_a_platform_without_vcgencmd_reports_nothing_rather_than_guessing(self):
        self.assertEqual(self._decode(""), {})


class AlertTests(unittest.TestCase):
    """Only conditions that mean 'the hardware cannot do its job'. An alert that fires on
    ordinary load teaches people to ignore the banner that would explain a real outage."""

    def codes(self, snapshot):
        return [item["code"] for item in sysinfo.alerts(snapshot)]

    def test_a_healthy_host_raises_nothing(self):
        self.assertEqual(self.codes({
            "throttling": {"now": [], "since_boot": []},
            "temperature_c": 45.0,
            "disk": {"used_percent": 40.0},
            "memory": {"swap_used_percent": 0.0},
            "network": {"primary": "eth0"}}), [])

    def test_active_undervoltage_is_critical_and_ranked_first(self):
        alerts = sysinfo.alerts({
            "throttling": {"now": ["undervoltage", "throttled"], "since_boot": ["undervoltage"]},
            "temperature_c": 82.0, "undervoltage": {"count": 95}})
        self.assertEqual(alerts[0]["code"], "undervoltage_now")
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertEqual(alerts[0]["detail"]["events"], 95)

    def test_past_undervoltage_still_warns_because_it_recurs(self):
        codes = self.codes({"throttling": {"now": [], "since_boot": ["undervoltage"]},
                            "undervoltage": {"count": 12, "last": "Wed Aug 5 16:16:41 2026"}})
        self.assertEqual(codes, ["undervoltage_seen"])

    def test_undervoltage_is_not_reported_twice(self):
        codes = self.codes({"throttling": {"now": ["undervoltage"],
                                           "since_boot": ["undervoltage"]}})
        self.assertEqual(codes, ["undervoltage_now"])

    def test_a_full_disk_outranks_a_merely_low_one(self):
        self.assertEqual(self.codes({"disk": {"used_percent": 97.0}}), ["disk_critical"])
        self.assertEqual(self.codes({"disk": {"used_percent": 91.0}}), ["disk_low"])
        self.assertEqual(self.codes({"disk": {"used_percent": 79.0}}), [])

    def test_a_second_uplink_is_not_by_itself_a_problem(self):
        """Two default routes is normal redundancy: the kernel picks by metric and nothing is
        wrong until the choice actually moves."""
        self.assertEqual(self.codes({"network": {"multiple_default_routes": True,
                                                 "primary": "eth0",
                                                 "default_interfaces": ["eth0", "wlan0"]}}), [])

    def test_a_default_route_that_moved_is_reported(self):
        before = {"ts": 1000, "network": {"primary": "eth0"}}
        after = {"ts": 1060, "network": {"primary": "wlan0"}}
        alerts = sysinfo.alerts(after, before)
        self.assertEqual([a["code"] for a in alerts], ["default_route_changed"])
        self.assertEqual(alerts[0]["detail"], {"from": "eth0", "to": "wlan0"})

    def test_an_unchanged_default_route_is_silent(self):
        same = {"ts": 1000, "network": {"primary": "eth0"}}
        self.assertEqual(sysinfo.alerts({"ts": 1060, "network": {"primary": "eth0"}}, same), [])

    def test_occupied_swap_alone_is_not_an_alert(self):
        """Pages parked since boot and never touched cost nothing; alerting on occupancy
        fires on a perfectly healthy box and teaches people to ignore the indicator."""
        idle = {"ts": 1000, "memory": {"swap_used_percent": 95.0,
                                       "swap_in_pages": 500, "swap_out_pages": 500}}
        later = {"ts": 1060, "memory": {"swap_used_percent": 95.0,
                                        "swap_in_pages": 532, "swap_out_pages": 500}}
        # 32 pages over 60s — what the real host actually does while healthy.
        self.assertEqual(sysinfo.alerts(later, idle), [])

    def test_active_paging_is_an_alert(self):
        before = {"ts": 1000, "memory": {"swap_in_pages": 0, "swap_out_pages": 0,
                                         "swap_used_percent": 40.0}}
        after = {"ts": 1060, "memory": {"swap_in_pages": 4000, "swap_out_pages": 2000,
                                        "swap_used_percent": 40.0}}
        alerts = sysinfo.alerts(after, before)
        self.assertEqual([a["code"] for a in alerts], ["swap_pressure"])
        self.assertEqual(alerts[0]["detail"]["pages_per_second"], 100)

    def test_a_single_sample_cannot_produce_a_rate(self):
        self.assertIsNone(sysinfo.swap_paging_rate({"ts": 10, "memory": {}}, None))
        # Counters reset by a reboot must not read as a huge negative or positive rate.
        self.assertIsNone(sysinfo.swap_paging_rate(
            {"ts": 60, "memory": {"swap_in_pages": 1, "swap_out_pages": 0}},
            {"ts": 0, "memory": {"swap_in_pages": 900, "swap_out_pages": 900}}))

    def test_critical_conditions_sort_above_warnings(self):
        alerts = sysinfo.alerts({"throttling": {"now": ["undervoltage"], "since_boot": []},
                                 "disk": {"used_percent": 97.0},
                                 "temperature_c": 80.0})
        self.assertEqual([a["severity"] for a in alerts][:2], ["critical", "critical"])


class CollectionTests(unittest.TestCase):
    def test_disk_reports_complete_byte_totals_for_the_host_panel(self):
        usage = SimpleNamespace(total=20 * 1024 ** 3, used=15 * 1024 ** 3,
                                free=5 * 1024 ** 3)
        with patch.object(sysinfo.shutil, "disk_usage", return_value=usage):
            value = sysinfo.disk("/data")
        self.assertEqual(value["total_bytes"], 20 * 1024 ** 3)
        self.assertEqual(value["used_bytes"], 15 * 1024 ** 3)
        self.assertEqual(value["free_bytes"], 5 * 1024 ** 3)
        self.assertEqual(value["used_percent"], 75.0)

    def test_project_storage_counts_the_gateways_own_files(self):
        with patch.object(sysinfo, "_project_paths", return_value=["/a", "/b"]), \
                patch.object(sysinfo, "_path_usage_bytes", side_effect=[100, 23]):
            storage = sysinfo.project_storage("/data")
        self.assertEqual(storage, {"files_bytes": 123, "known_total_bytes": 123,
                                   "known_total_is_logical": False})
        self.assertFalse(any(key.startswith("docker") for key in storage))

    def test_absent_platform_fields_are_omitted_rather_than_faked(self):
        with patch.object(sysinfo, "_vcgencmd", return_value=""), \
                patch.object(sysinfo, "undervoltage_events", return_value={}), \
                patch.object(sysinfo, "usb_devices", return_value=[]), \
                patch.object(sysinfo, "project_storage", return_value={}):
            snapshot = sysinfo.collect("/")
        for key in ("throttling", "undervoltage", "usb_devices"):
            self.assertNotIn(key, snapshot)
        # The portable facts are still present on any Linux host.
        for key in ("memory", "load", "disk", "project_storage", "network", "uptime_seconds"):
            self.assertIn(key, snapshot)

    def test_a_usb_attached_nic_is_flagged(self):
        # On a Pi 3 the NIC shares its bus and power rail with the modem and card reader, so
        # it fails at the same instant they do.
        with patch.object(sysinfo, "default_route_interfaces", return_value=["eth0"]), \
                patch.object(sysinfo.os.path, "realpath",
                             return_value="/sys/devices/platform/soc/3f980000.usb/usb1/1-1/net/eth0"), \
                patch.object(sysinfo, "_interface_counters", return_value={}):
            self.assertTrue(sysinfo.network()["usb_attached"])

    def test_temperature_rejects_implausible_readings(self):
        with patch.object(sysinfo, "_read", return_value="80100"):
            self.assertEqual(sysinfo.temperature_c(), 80.1)
        with patch.object(sysinfo, "_read", return_value="0"):
            self.assertIsNone(sysinfo.temperature_c())
        with patch.object(sysinfo, "_read", return_value="not-a-number"):
            self.assertIsNone(sysinfo.temperature_c())


if __name__ == "__main__":
    unittest.main()
