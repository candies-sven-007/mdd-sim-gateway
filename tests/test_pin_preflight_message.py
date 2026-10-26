"""The PIN-preflight 409 must always carry a human-readable detail.message.

Issue #60: without it the WebUI's generic error path falls back to the bare HTTP
status text and the operator only sees "能力切换失败: Conflict" with no way to know
the line needs a PIN (or that the reader holds another card/eSIM profile).
"""
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from control.app.routers import lines as lines_routes
from control.app import state as app_state


class PinPreflightMessageTests(unittest.TestCase):
    def test_every_code_carries_message(self):
        for pf in ({"ok": False, "code": "no_card"},
                   {"ok": False, "code": "pin_required", "tries": 3},
                   {"ok": False, "code": "pin_invalid", "clear": True, "tries": 2},
                   {"ok": False, "code": "card_unreadable", "error": "ADF.USIM select failed"}):
            exc = lines_routes._pin_preflight_http(pf)
            self.assertEqual(exc.status_code, 409)
            self.assertEqual(exc.detail["code"], pf["code"])
            self.assertEqual(exc.detail.get("tries"), pf.get("tries"))
            self.assertTrue(exc.detail.get("message"), pf["code"])

    def test_tries_are_rendered_when_known(self):
        exc = lines_routes._pin_preflight_http({"ok": False, "code": "pin_invalid", "tries": 1})
        self.assertIn("1 tries left", exc.detail["message"])
        exc = lines_routes._pin_preflight_http({"ok": False, "code": "pin_required"})
        self.assertNotIn("tries left", exc.detail["message"])

    def test_unknown_code_still_readable(self):
        exc = lines_routes._pin_preflight_http({"ok": False, "code": "mystery"})
        self.assertIn("mystery", exc.detail["message"])

    def test_card_unreadable_names_the_read_failure_and_rules_out_a_pin(self):
        """Issue #60: an unreadable card used to fall through to 'pin_required' with an
        unknown retry counter, so the UI asked for a PIN that could not help. The message
        must carry the real read error and say a PIN is not the answer."""
        exc = lines_routes._pin_preflight_http({"ok": False, "code": "card_unreadable",
                                        "error": "ADF.USIM select failed"})
        self.assertIn("ADF.USIM select failed", exc.detail["message"])
        self.assertIn("not a PIN problem", exc.detail["message"])

    def test_card_unreadable_without_a_detail_still_reads(self):
        exc = lines_routes._pin_preflight_http({"ok": False, "code": "card_unreadable"})
        self.assertTrue(exc.detail["message"])
        self.assertNotIn("()", exc.detail["message"])

    def test_no_card_carries_expected_iccid(self):
        exc = lines_routes._pin_preflight_http({"ok": False, "code": "no_card",
                                        "line_iccid": "8944000000000000001"})
        self.assertIn("8944000000000000001", exc.detail["message"])


class PreflightBlockLifecycleTests(unittest.TestCase):
    def _capture(self, pf):
        events = []
        with patch.object(app_state, "_record_lifecycle",
                          side_effect=lambda iid, event, **kw: events.append((event, kw))):
            with self.assertRaises(HTTPException) as ctx:
                lines_routes._raise_preflight_block("3", pf)
        return events, ctx.exception

    def test_records_closed_event_without_identifier(self):
        events, exc = self._capture({"ok": False, "code": "no_card",
                                     "line_iccid": "8944000000000000001"})
        self.assertEqual(len(events), 1)
        event, kw = events[0]
        self.assertEqual(event, "preflight_blocked")
        self.assertEqual(kw["reason_code"], "no_card")
        self.assertIs(kw["card_present"], False)
        # The identifier must never enter the (public) lifecycle record.
        self.assertNotIn("8944000000000000001", json.dumps(kw))
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["code"], "no_card")

    def test_card_unreadable_records_a_present_card(self):
        events, exc = self._capture({"ok": False, "code": "card_unreadable",
                                     "error": "ADF.USIM select failed"})
        event, kw = events[0]
        self.assertEqual(kw["reason_code"], "card_unreadable")
        self.assertIs(kw["card_present"], True)
        self.assertEqual(exc.detail["code"], "card_unreadable")

    def test_card_mismatch_maps_to_mismatch_error(self):
        events, exc = self._capture({"ok": False, "code": "card_mismatch",
                                     "card_iccid": "8944000000000000002",
                                     "line_iccid": "8944000000000000001",
                                     "reader": "Alcor 00 00"})
        event, kw = events[0]
        self.assertEqual(kw["reason_code"], "card_mismatch")
        self.assertIs(kw["iccid_matches"], False)
        self.assertNotIn("8944000000000000002", json.dumps(kw))
        # Operator-facing error keeps the identifiers so the mismatch is actionable.
        self.assertEqual(exc.detail["code"], "card_mismatch")
        self.assertIn("8944000000000000002", exc.detail["message"])
        self.assertIn("8944000000000000001", exc.detail["message"])
