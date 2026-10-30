"""Receive-only SMS: the acknowledgement path, the engine hand-off and the control plane.

The IMS network delivers SMS to the registration (receive_sms=yes) and the SMSC keeps re-sending
anything it gets no RP-ACK for, so Asterisk answers every MT SMS and sends the RP-ACK. Since
2026-10-30 (SLIM_PLAN, change of D2) the text is also handed to the control plane, which keeps
it for 30 days. There is no sending, MMS or USSD. What must stay true:

* nothing but the control plane's database ever holds a message body: not a file written by the
  dialplan, not notify.py's local event log, not a log line, not the audit record;
* a machine payload (8-bit data, SIM-addressed) is dropped, a long text is joined from its
  parts, a repeated delivery is stored once;
* only an administrator session can read or delete messages.

How the acknowledgement works, from sysmocom's res_pjsip_messaging.c module_on_rx_request():
the 200 OK and the RP-ACK are sent only when ast_msg_has_destination() finds an extension for
the message in the endpoint's message_context; otherwise the MESSAGE is answered 404 and no
RP-ACK leaves. mt_rpack_routing.py makes that RP-ACK reach the carrier (right target, the
arrival transport, no DNS on an IMS-internal FQDN).
"""
import base64
import os
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


class DialplanHandsTheTextToTheControlPlane(unittest.TestCase):
    def test_the_message_context_gives_every_mt_sms_a_destination(self):
        body = _context(_render("extensions.conf.j2"), "volte_ims_msg")
        # A catch-all extension is what makes res_pjsip_messaging answer 200 and send RP-ACK.
        self.assertRegex(body, r"^exten => _\[!-~\]!,1,", "catch-all extension missing")
        self.assertTrue(body.rstrip().endswith("same => n,Hangup()"))

    def test_the_text_goes_to_notify_and_nowhere_else(self):
        body = _context(_render("extensions.conf.j2"), "volte_ims_msg")
        self.assertIn('notify.py sms_in "${MESSAGE(from)}" "${BASE64_ENCODE(${MESSAGE(body)})}"', body)
        for variable in ("SMS_CONCAT_REF", "SMS_CONCAT_TOTAL", "SMS_CONCAT_SEQ", "SMS_TP_PID",
                         "SMS_TP_DCS", "SMS_UDH_HEX", "SMS_TPDU_HEX"):
            self.assertIn("${MESSAGE_DATA(" + variable + ")}", body)
        for forbidden in ("FILE(", "MessageSend", "Dial("):
            self.assertNotIn(forbidden, body)

    def test_no_file_gets_the_body_and_there_is_no_sending(self):
        dialplan = _render("extensions.conf.j2")
        self.assertNotIn("messages.txt", dialplan)
        self.assertNotIn("notify.py sms_out", dialplan)
        self.assertNotIn("notify.py ussd", dialplan)
        # A browser MESSAGE finds no extension and is answered 404: this build sends nothing.
        self.assertEqual(_context(dialplan, "msg-from-local"), "")

    def test_the_ims_endpoint_routes_messages_there_and_stays_sms_capable(self):
        pjsip = _render("pjsip.conf.j2")
        self.assertIn("message_context=volte_ims_msg", pjsip)
        self.assertIn("receive_sms=yes", pjsip)
        self.assertNotIn("smsc_uri", pjsip)

    def test_notify_keeps_the_body_out_of_its_local_event_log(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, "MDD_LOGDIR": temp, "MDD_RUNDIR": temp, "MDD_ENV": str(Path(temp) / "none")}
            body = base64.b64encode(b"Your code is 482913").decode()
            for event, args in (("sms_in", ["+447700900123", body]), ("call_in", ["+447700900123"])):
                subprocess.run([sys.executable, "-I", str(ROOT / "engine" / "notify.py"), event,
                                *args], env=env, check=True, timeout=20)
            log = (Path(temp) / "events.jsonl").read_text()
            self.assertIn("call_in", log)
            self.assertNotIn("sms_in", log)
            self.assertNotIn(body, log)
            self.assertNotIn("+447700900123 ", log.replace("call_in", ""))


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
        """Run the patcher the way the build does: AST_SRC names the Asterisk tree."""
        self.assertEqual(target.parent.name, "res")
        done = subprocess.run([sys.executable, "-I", str(PATCHES / "mt_rpack_routing.py")],
                              capture_output=True, text=True, check=True,
                              env={**os.environ, "AST_SRC": str(target.parent.parent)})
        return done.stdout

    def test_the_receive_patches_ship_and_the_send_ones_do_not(self):
        shipped = {path.name for path in PATCHES.glob("*.py")}
        self.assertIn("mt_rpack_routing.py", shipped)
        self.assertIn("mt_concat_udh.py", shipped)        # long texts and the PDU header
        for removed in ("mo_submit_report.py", "ussd_body_capture.py"):
            self.assertNotIn(removed, shipped)

    def test_send_rpack_is_replaced_and_nothing_else_is_touched(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "res" / "res_pjsip_messaging.c"
            target.parent.mkdir()
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


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class ControlPlaneKeepsTheText(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for patcher in (patch.multiple(store, DATA_DIR=temp.name,
                                       DB_PATH=str(Path(temp.name) / "h.sqlite")),
                        patch.object(engine_routes.cfg, "get_instance",
                                     side_effect=lambda iid: {"id": iid} if iid in ("1", "2") else None),
                        patch.object(app_state.hub, "broadcast", new=AsyncMock())):
            patcher.start()
            self.addCleanup(patcher.stop)
        store.init()
        self.broadcast = app_state.hub.broadcast

    async def deliver(self, body, *extra, sender="+447700900123", iid="1"):
        return await engine_routes.api_engine_event(
            {"instance": iid, "event": "sms_in", "args": [sender, _b64(body), *extra]})

    async def test_a_text_is_stored_and_announced(self):
        result = await self.deliver("Your code is 482913")
        self.assertEqual(result["ok"], True)
        [message] = store.list_sms()
        self.assertEqual((message["sender"], message["body"], message["instance"], message["read"]),
                         ("+447700900123", "Your code is 482913", "1", False))
        sent = self.broadcast.await_args.args[0]
        self.assertEqual((sent["type"], sent["instance"], sent["message"]["body"]),
                         ("sms", "1", "Your code is 482913"))
        self.assertEqual(store.unread_sms_count(), {"1": 1})

    async def test_nothing_is_sent_for_sms_out_ussd_or_voicemail(self):
        for event in ("sms_out", "ussd", "voicemail_new"):
            result = await engine_routes.api_engine_event(
                {"instance": "1", "event": event, "args": ["+447700900123", _b64("x")]})
            self.assertEqual(result, {"ok": True, "ignored": True})
        self.assertEqual(store.list_sms(), [])
        self.broadcast.assert_not_awaited()

    async def test_a_repeated_delivery_is_stored_once(self):
        # Same service-centre timestamp (a TPDU: SMS-DELIVER, OA, PID, DCS, SCTS).
        tpdu = "040a81" + "7774097000" + "00" + "00" + "52019120000000" + "00"
        from control.app import sms_pdu
        self.assertIsNotNone(sms_pdu.deliver_timestamp(tpdu))    # the dedupe really uses it
        for _ in range(2):
            await self.deliver("Hello", "", "", "", "0", "0", "", tpdu)
        self.assertEqual(len(store.list_sms()), 1)
        self.assertIsNotNone(store.list_sms()[0]["sent_ts"])
        await self.deliver("Hello again")
        await self.deliver("Hello again")                        # no timestamp: same minute
        self.assertEqual(len(store.list_sms()), 2)

    async def test_machine_payloads_and_signalling_are_dropped(self):
        cases = [("8-bit data", "\x06\x05\x04\x0b\x84\x23\xf0", "", "", "", "0", "4", "", ""),
                 ("SIM class 2", "text", "", "", "", "0", "246", "", ""),
                 ("SIM data download", "text", "", "", "", "127", "0", "", ""),
                 ("no header, binary-looking", "\x01\x02\x9f\x80", ),
                 ("empty body", "   ")]
        for name, body, *extra in cases:
            with self.subTest(name):
                result = await self.deliver(body, *extra)
                self.assertTrue(result.get("dropped"), result)
        self.assertEqual(store.list_sms(), [])

    async def test_non_ascii_text_is_kept_whole(self):
        await self.deliver("验证码：908172 ✓ café")
        self.assertEqual(store.list_sms()[0]["body"], "验证码：908172 ✓ café")

    async def test_the_parts_of_a_long_text_are_joined_in_order(self):
        parts = {2: "world", 3: "!", 1: "Hello "}
        results = []
        for seq in (2, 3, 1):                                    # arrival order is not sequence
            results.append(await self.deliver(parts[seq], "77", "3", str(seq)))
        self.assertEqual([r.get("buffered") for r in results], ["2/3", "3/3", None])
        [message] = store.list_sms()
        self.assertEqual((message["body"], message["parts"], message["complete"]),
                         ("Hello world!", 3, True))
        with sqlite3.connect(store.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sms_parts").fetchone()[0], 0)

    async def test_parts_that_never_complete_are_saved_as_far_as_they_got(self):
        await self.deliver("first half, ", "9", "2", "1")
        self.assertEqual(store.list_sms(), [])
        self.assertEqual(store.flush_stale_sms_parts(now=int(__import__("time").time()) + 60), [])
        saved = store.flush_stale_sms_parts(
            now=int(__import__("time").time()) + store.SMS_SEGMENT_WAIT_SECONDS + 60)
        self.assertEqual([(m["body"], m["complete"], m["parts"]) for m in saved],
                         [("first half, ", False, 2)])

    async def test_an_unknown_line_and_a_bad_body_are_dropped(self):
        self.assertEqual((await self.deliver("x", iid="9"))["dropped"], "unknown_line")
        result = await engine_routes.api_engine_event(
            {"instance": "1", "event": "sms_in", "args": ["+4477", "\udc80"]})
        self.assertTrue(result.get("dropped"))


class StoreRules(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.multiple(store, DATA_DIR=temp.name,
                                 DB_PATH=str(Path(temp.name) / "h.sqlite"))
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()

    def test_read_delete_and_per_line_clear(self):
        a = store.add_sms("1", "A", "one", now=1000)
        b = store.add_sms("1", "B", "two", now=1001)
        c = store.add_sms("2", "C", "three", now=1002)
        self.assertEqual([m["id"] for m in store.list_sms()], [c["id"], b["id"], a["id"]])
        self.assertEqual([m["id"] for m in store.list_sms("1")], [b["id"], a["id"]])
        self.assertEqual(store.mark_sms_read([a["id"]]), 1)
        self.assertEqual(store.unread_sms_count(), {"1": 1, "2": 1})
        self.assertEqual(store.mark_sms_read(instance="2"), 1)
        self.assertEqual(store.delete_sms([b["id"]]), 1)
        self.assertEqual(store.delete_sms(None, "2"), 1)
        self.assertEqual([m["id"] for m in store.list_sms()], [a["id"]])
        self.assertEqual(store.delete_sms(), 1)
        self.assertEqual(store.list_sms(), [])

    def test_messages_older_than_thirty_days_are_deleted(self):
        now = 10_000_000
        store.add_sms("1", "A", "old", now=now - 30 * 86400 - 1)
        store.add_sms("1", "A", "edge", now=now - 30 * 86400 + 60)
        store.add_sms("1", "A", "new", now=now)
        self.assertEqual(store.prune_sms(now=now), 1)
        self.assertEqual(sorted(m["body"] for m in store.list_sms()), ["edge", "new"])
        self.assertEqual(store.SMS_RETENTION_SECONDS, 30 * 86400)

    def test_only_the_sms_tables_are_added(self):
        with sqlite3.connect(store.DB_PATH) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables - {"sqlite_sequence"},
                         {"calls", "line_states", "sms", "sms_parts"})


class NothingLogsTheBody(unittest.TestCase):
    def test_no_log_call_in_the_receive_path_names_the_message(self):
        for path in (ROOT / "control" / "app" / "routers" / "sms.py",):
            text = path.read_text()
            self.assertNotRegex(text, r"log\.\w+\(")
        engine = (ROOT / "control" / "app" / "routers" / "engine.py").read_text()
        receive = engine[engine.index("async def _receive_sms"):engine.index('@router.post("/api/engine/event")')]
        self.assertNotRegex(receive, r"log\.\w+\(")

    def test_the_audit_record_has_no_body_field(self):
        text = (ROOT / "control" / "app" / "main.py").read_text()
        record = text[text.index("_write_audit_record({"):text.index("return response")]
        self.assertNotIn("body", record)


class RoutesAreAdminOnly(unittest.TestCase):
    def test_every_sms_route_is_in_the_pinned_set(self):
        from tests.test_route_set import ADDED_HTTP
        sms = {(m, p) for m, p in ADDED_HTTP if p.startswith("/api/sms")}
        self.assertEqual(sms, {("GET", "/api/sms"), ("POST", "/api/sms/read"),
                               ("DELETE", "/api/sms/{message_id}"), ("DELETE", "/api/sms")})

    def test_the_page_offers_no_sending(self):
        api = (ROOT / "webui" / "src" / "api.js").read_text(encoding="utf-8")
        page = (ROOT / "webui" / "src" / "views" / "Sms.jsx").read_text(encoding="utf-8")
        self.assertNotRegex(api, r"smsSend|/sms/send|/messages|/mms/")
        self.assertNotRegex(page, r"<textarea|smsSend|compose", )
        app = (ROOT / "webui" / "src" / "App.jsx").read_text(encoding="utf-8")
        self.assertNotIn("['messages',", app)


if __name__ == "__main__":
    unittest.main()
