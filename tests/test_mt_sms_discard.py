"""Network-delivered SMS is acknowledged and discarded.

This build has no SMS feature, but the IMS network still delivers SMS to the registration
(receive_sms=yes) and the SMSC keeps re-sending anything it gets no RP-ACK for. So Asterisk
must still answer every MT SMS and send the RP-ACK, while nothing about the message -- sender,
body or the fact that it arrived -- is written to disk, stored, broadcast or offered by the API
and WebUI.

How the acknowledgement works, from sysmocom's res_pjsip_messaging.c module_on_rx_request():
the 200 OK and the RP-ACK are sent only when ast_msg_has_destination() finds an extension for
the message in the endpoint's message_context; otherwise the MESSAGE is answered 404 and no
RP-ACK leaves. mt_rpack_routing.py makes that RP-ACK reach the carrier (right target, the
arrival transport, no DNS on an IMS-internal FQDN).
"""
import base64
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from jinja2 import Environment, FileSystemLoader

from control.app import main, store
from tests.route_listing import api_routes
from control.app import state as app_state
from control.app.routers import engine as engine_routes

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "engine" / "templates"
PATCHES = ROOT / "engine" / "patches" / "asterisk"
CTX = dict(webrtc_enable=True, webrtc_user="webrtc", ring_timeout=35, msisdn="+447700900001",
           realm="ims.mnc030.mcc234.3gppnetwork.org", imsi="234300000000001",
           pcscf="203.0.113.10", user_eq_phone=False)


def _render(name: str, **overrides) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)),
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    return env.get_template(name).render(**{**CTX, **overrides})


def _context(dialplan: str, name: str) -> str:
    """The body of one [context] up to the next context header."""
    match = re.search(r"^\[" + re.escape(name) + r"\]\n(.*?)(?=^\[|\Z)", dialplan,
                      re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError(f"context [{name}] not rendered")
    return "\n".join(line for line in match.group(1).splitlines()
                     if line.strip() and not line.lstrip().startswith(";"))


class DialplanAcknowledgesAndDiscards(unittest.TestCase):
    def test_the_message_context_gives_every_mt_sms_a_destination(self):
        body = _context(_render("extensions.conf.j2"), "volte_ims_msg")
        # A catch-all extension is what makes res_pjsip_messaging answer 200 and send RP-ACK.
        self.assertRegex(body, r"^exten => _\[!-~\]!,1,", "catch-all extension missing")

    def test_nothing_about_the_message_is_read_stored_or_forwarded(self):
        body = _context(_render("extensions.conf.j2"), "volte_ims_msg")
        for forbidden in ("MESSAGE(", "MESSAGE_DATA(", "FILE(", "notify.py", "System(",
                          "MessageSend", "BASE64"):
            self.assertNotIn(forbidden, body)
        self.assertTrue(body.rstrip().endswith("same => n,Hangup()"))

    def test_a_browser_message_finds_no_extension(self):
        dialplan = _render("extensions.conf.j2")
        self.assertEqual(_context(dialplan, "msg-from-local"), "")
        self.assertNotIn("messages.txt", dialplan)
        self.assertNotIn("notify.py sms", dialplan)
        self.assertNotIn("notify.py ussd", dialplan)

    def test_the_ims_endpoint_routes_messages_there_and_stays_sms_capable(self):
        pjsip = _render("pjsip.conf.j2")
        self.assertIn("message_context=volte_ims_msg", pjsip)
        # Without the SMS feature tag the network would stop delivering over IMS; with it and
        # no RP-ACK the SMSC would retry forever. Both halves are kept on purpose.
        self.assertIn("receive_sms=yes", pjsip)
        self.assertNotIn("smsc_uri", pjsip)


class RpAckPatchIsKeptAndApplies(unittest.TestCase):
    STOCK = (
        "static int before(void) { return 0; }\n"
        "static pj_status_t send_rpack(pjsip_rx_data *rdata, unsigned char ack_ref)\n"
        "{\n"
        "\tif (ack_ref) { do_something(); }\n"
        "\treturn PJ_SUCCESS;\n"
        "}\n"
        "static int after(void) { return 1; }\n"
    )

    def _apply(self, target: Path) -> str:
        source = (PATCHES / "mt_rpack_routing.py").read_text()
        default = "'/home/asterisk-build/asterisk/res/res_pjsip_messaging.c'"
        self.assertEqual(source.count(default), 1)
        script = source.replace(default, repr(str(target)))
        done = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True,
                              text=True, check=True)
        return done.stdout

    def test_only_the_acknowledgement_patch_of_the_messaging_set_ships(self):
        shipped = {path.name for path in PATCHES.glob("*.py")}
        self.assertIn("mt_rpack_routing.py", shipped)
        for removed in ("mo_submit_report.py", "mt_concat_udh.py", "ussd_body_capture.py"):
            self.assertNotIn(removed, shipped)

    def test_send_rpack_is_replaced_and_nothing_else_is_touched(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "res_pjsip_messaging.c"
            target.write_text(self.STOCK)
            self.assertIn("patched OK", self._apply(target))
            patched = target.read_text()
            self.assertIn("PATCH_RPACK_ROUTING2", patched)
            self.assertIn("pjsip_tx_data_set_transport", patched)   # reuse the IMS socket
            self.assertIn("P-Asserted-Identity", patched)          # target the SMSC address
            self.assertNotIn("do_something", patched)
            self.assertTrue(patched.startswith("static int before(void) { return 0; }\n"))
            self.assertTrue(patched.endswith("static int after(void) { return 1; }\n"))
            self.assertIn("already patched", self._apply(target))


class ControlPlaneNeverSeesTheMessage(unittest.IsolatedAsyncioTestCase):
    async def test_an_sms_event_from_an_older_engine_is_dropped(self):
        body = base64.b64encode("Your code is 482913".encode()).decode()
        with tempfile.TemporaryDirectory() as temp, \
                patch.multiple(store, DATA_DIR=temp, DB_PATH=str(Path(temp) / "h.sqlite")), \
                patch.object(app_state.hub, "broadcast", new=AsyncMock()) as broadcast:
            store.init()
            for event in ("sms_in", "sms_out", "ussd"):
                result = await engine_routes.api_engine_event(
                    {"instance": "1", "event": event, "args": ["+447700900123", body]})
                self.assertEqual(result, {"ok": True, "ignored": True})
            broadcast.assert_not_awaited()
            with sqlite3.connect(store.DB_PATH) as db:
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(tables - {"sqlite_sequence"}, {"calls", "line_states"})
                self.assertEqual(db.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)
            raw = Path(store.DB_PATH).read_bytes()
            self.assertNotIn(b"482913", raw)
            self.assertNotIn(b"+447700900123", raw)

    def test_no_route_offers_messages(self):
        paths = [getattr(route, "path", "") for route in api_routes(main.app)]
        for path in paths:
            self.assertIsNone(re.search(r"/(messages|sms|mms|ussd)(/|$)", path), path)


class WebUiHasNoMessagesPage(unittest.TestCase):
    def test_no_messages_view_or_navigation(self):
        views = ROOT / "webui" / "src" / "views"
        for name in ("Messages.jsx", "MmsSettings.jsx"):
            self.assertFalse((views / name).exists(), name)
        app = (ROOT / "webui" / "src" / "App.jsx").read_text(encoding="utf-8")
        self.assertNotIn("['messages',", app)
        api = (ROOT / "webui" / "src" / "api.js").read_text(encoding="utf-8")
        self.assertNotRegex(api, r"/messages|/sms/|/mms/")


if __name__ == "__main__":
    unittest.main()
