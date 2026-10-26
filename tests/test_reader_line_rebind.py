"""A line on an ordinary reader follows its SIM to whichever reader holds it now.

Seen on a DS1621+: switching the eSIM in an SCR Prime reader to a profile last used in another
reader enabled that profile's line, which still carried the old USB port. The engine found no
reader there, fell back to an index holding another line's card, refused to authenticate, and
retried every minute until the line was saved again by hand.
"""
import unittest
from unittest.mock import patch

from control.app import main

ICCID = "89440000000000000315"
SCR = {"name": "SCR Prime CCID Reader (000000000001) 01 00", "index": 5, "present": True,
       "iccid": ICCID, "reader_port": "1-2.2"}
OTHER = {"name": "Alcor Link AK9563 00 00", "index": 4, "present": True,
         "iccid": "8944100000000000001", "reader_port": "1-2.4.1"}
MODEM = {"name": "VoWiFi Modem 2c7c-0125-1-2.1 00 01", "index": 1, "present": True,
         "iccid": ICCID, "reader_port": "1-2.1"}


class LiveReaderBindingTests(unittest.TestCase):
    def binding(self, cards, **inst):
        with patch.object(main.hub, "cards", {c["name"]: c for c in cards}):
            return main._live_reader_binding_for_instance({"iccid": ICCID, **inst})

    def test_the_reader_holding_the_sim_is_found(self):
        self.assertEqual(self.binding([OTHER, SCR]), {"reader_port": "1-2.2", "reader_index": 5})

    def test_nothing_is_guessed(self):
        # Not seen, seen but absent, or claimed by two readers: leave the saved binding alone.
        self.assertEqual(self.binding([OTHER]), {})
        self.assertEqual(self.binding([{**SCR, "present": False}]), {})
        self.assertEqual(self.binding([SCR, {**SCR, "name": "second", "reader_port": "1-3"}]), {})
        self.assertEqual(main._live_reader_binding_for_instance({}), {})

    def test_modem_readers_are_left_to_the_modem_binding(self):
        self.assertEqual(self.binding([MODEM]), {})
        self.assertEqual(self.binding([SCR], swu_reader="VoWiFi Modem x 00 01"), {})


class StartRebindsTests(unittest.TestCase):
    @patch.object(main.engine, "start", return_value="container-id")
    @patch.object(main, "_apply_current_hardware_imei", side_effect=lambda inst: inst)
    @patch.object(main.cfg, "line_allowed", return_value=True)
    def test_a_stale_port_is_corrected_before_the_engine_starts(self, _allowed, _imei, start):
        stale = {"id": "4", "iccid": ICCID, "reader_port": "1-3.2", "reader_index": 1}
        corrected = {**stale, "reader_port": "1-2.2", "reader_index": 5}
        with patch.object(main.hub, "cards", {SCR["name"]: SCR}), \
                patch.object(main.cfg, "upsert_instance", return_value=corrected) as upsert:
            main._start_engine_checked(stale, {}, reason="card-inserted")
        upsert.assert_called_once_with({"id": "4", "reader_port": "1-2.2", "reader_index": 5})
        start.assert_called_once_with(corrected, {}, dev_mounts=False, reason="card-inserted")

    @patch.object(main.engine, "start", return_value="container-id")
    @patch.object(main, "_apply_current_hardware_imei", side_effect=lambda inst: inst)
    @patch.object(main.cfg, "line_allowed", return_value=True)
    def test_a_correct_binding_is_not_rewritten(self, _allowed, _imei, start):
        current = {"id": "4", "iccid": ICCID, "reader_port": "1-2.2", "reader_index": 5}
        with patch.object(main.hub, "cards", {SCR["name"]: SCR}), \
                patch.object(main.cfg, "upsert_instance") as upsert:
            main._start_engine_checked(current, {}, reason="manual")
        upsert.assert_not_called()
        start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
