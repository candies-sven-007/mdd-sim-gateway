import socket
import unittest
from unittest.mock import AsyncMock, patch

from control.app import main, vowifi_support
from control.app import state as app_state


class VowifiSupportTests(unittest.TestCase):
    def setUp(self):
        vowifi_support._cache.clear()

    def test_mainland_china_is_unsupported_for_every_network(self):
        for mnc in ("00", "01", "011", "15"):
            verdict = vowifi_support.assess("460", mnc)
            self.assertEqual(verdict["status"], vowifi_support.UNSUPPORTED)
            self.assertEqual(verdict["source"], "carrier_table")
        self.assertIn("China Telecom", vowifi_support.assess("460", "11")["reason"])

    def test_nothing_known_is_unknown_not_unsupported(self):
        self.assertEqual(vowifi_support.assess("234", "33")["status"], vowifi_support.UNKNOWN)
        self.assertEqual(vowifi_support.assess("", "")["status"], vowifi_support.UNKNOWN)

    def test_no_such_name_is_unsupported_but_an_outage_proves_nothing(self):
        nxdomain = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        with patch.object(vowifi_support.socket, "getaddrinfo", side_effect=nxdomain):
            self.assertEqual(vowifi_support.lookup("epdg.example"), "nxdomain")
        outage = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        with patch.object(vowifi_support.socket, "getaddrinfo", side_effect=outage):
            self.assertEqual(vowifi_support.lookup("epdg.example"), "error")
        self.assertEqual(vowifi_support.assess("234", "33", dns="nxdomain")["status"],
                         vowifi_support.UNSUPPORTED)
        self.assertEqual(vowifi_support.assess("234", "33", dns="error")["status"],
                         vowifi_support.UNKNOWN)
        self.assertEqual(vowifi_support.assess("234", "33", dns="ok")["status"],
                         vowifi_support.SUPPORTED)

    def test_a_probe_answer_is_remembered(self):
        with patch.object(vowifi_support, "lookup", return_value="ok") as lookup:
            verdict = vowifi_support.for_instance({"mcc": "234", "mnc": "33"}, probe_now=True)
            self.assertEqual(verdict["status"], vowifi_support.SUPPORTED)
            self.assertEqual(vowifi_support.for_instance({"mcc": "234", "mnc": "33"})["status"],
                             vowifi_support.SUPPORTED)
        lookup.assert_called_once()

    def test_a_table_verdict_never_touches_dns(self):
        with patch.object(vowifi_support, "lookup") as lookup:
            vowifi_support.for_instance({"mcc": "460", "mnc": "01"}, probe_now=True)
        lookup.assert_not_called()


class UnsupportedCarrierHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_manual_try_on_an_unsupported_carrier_stops_without_a_retry_timer(self):
        st = {"state": "EPDG_UNRESOLVED", "label": "Cannot resolve ePDG",
              "reason_code": "epdg_unresolved", "reason": "x", "detail": {}}
        inst = {"id": "9", "enabled": True, "mcc": "460", "mnc": "11",
                "retry": {"max": 3, "interval": 30}}
        app_state.hub.reset_health("9", None)
        with patch.object(main.engine, "capture_and_stop") as capture_and_stop, \
                patch.object(app_state, "_record_lifecycle") as lifecycle, \
                patch.object(app_state.hub, "drop_ami", new=AsyncMock()):
            result = app_state.apply_health("9", inst, st, "generation-1")
            for _ in range(20):
                await __import__("asyncio").sleep(0.01)
                if capture_and_stop.called:
                    break
        self.assertTrue(result["frozen"])
        self.assertIsNone(result["automatic_retry_in"])
        self.assertIn("Mainland China", result["reason"])
        self.assertTrue(capture_and_stop.called)
        self.assertEqual(lifecycle.call_args.args[2], "carrier_unsupported")

    async def test_a_carrier_merely_missing_from_dns_keeps_the_normal_retries(self):
        st = {"state": "EPDG_UNRESOLVED", "label": "Cannot resolve ePDG",
              "reason_code": "epdg_unresolved", "reason": "x", "detail": {}}
        inst = {"id": "10", "enabled": True, "mcc": "234", "mnc": "33",
                "retry": {"max": 3, "interval": 30}}
        app_state.hub.reset_health("10", None)
        vowifi_support._cache.clear()
        with patch.object(vowifi_support, "probe_in_background"):
            result = app_state.apply_health("10", inst, st, "generation-1")
        self.assertNotIn("frozen", result)
        self.assertEqual(result["retry"], {"count": 1, "max": 3})


if __name__ == "__main__":
    unittest.main()
