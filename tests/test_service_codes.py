"""Dialling carrier service codes (*21*<n>#, *#21#, #225#) over the VoWiFi path.

Three layers used to reject them independently: the browser dialler's number validation,
JsSIP's SIP URI construction, and the dialplan's outgoing extension pattern. Each layer is
pinned here because a regression in any one of them silently makes the codes undialable
again, and the failure looks identical to a carrier rejection.
"""
import unittest
from pathlib import Path

try:
    from control.app import main
except ImportError:                      # control-plane deps absent (fastapi et al.)
    main = None

ROOT = Path(__file__).resolve().parent.parent


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


class DialplanServiceCodeTests(unittest.TestCase):
    def setUp(self):
        self.dialplan = _read("engine", "templates", "extensions.conf.j2")

    def test_outgoing_pattern_accepts_service_codes(self):
        self.assertIn("exten => _[+0-9*#].,1,NoOp(Outgoing call", self.dialplan)

    def test_pattern_still_shields_the_special_extensions(self):
        # h/i/t are pure letters, so the widened class cannot match them. A bare _. would.
        self.assertNotIn("exten => _.,", self.dialplan)
        self.assertIn("exten => h,1,", self.dialplan)

    def test_notify_arguments_are_quoted_against_shell_comment_stripping(self):
        # TrySystem runs the command through a shell, where '#' starting a word begins a
        # comment. Unquoted, a leading-'#' code such as #225# swallowed its own argument and
        # every one after it, so no call record was ever created for it.
        self.assertIn('notify.py call_out "${EXTEN}"', self.dialplan)
        self.assertNotIn("notify.py call_out ${EXTEN}", self.dialplan)
        self.assertIn('notify.py call_in "${CALLERID(num)}"', self.dialplan)

    def test_hash_is_escaped_before_the_outbound_invite(self):
        # An unescaped '#' truncates the user part of the outgoing request URI.
        self.assertIn("Set(DIALTARGET=${STRREPLACE(EXTEN,#,%23)})", self.dialplan)
        self.assertIn("Dial(PJSIP/${DIALTARGET}@volte_ims", self.dialplan)
        self.assertNotIn("Dial(PJSIP/${EXTEN}@volte_ims", self.dialplan)

    def test_call_log_records_the_code_the_user_dialled(self):
        # The escaped form is a transport detail; logs and notifications keep the raw code.
        self.assertIn('notify.py call_out "${EXTEN}"', self.dialplan)
        self.assertIn("Set(CALLPEER=${EXTEN})", self.dialplan)


class BrowserDiallerServiceCodeTests(unittest.TestCase):
    def setUp(self):
        self.view = _read("webui", "src", "views", "Softphone.jsx")
        self.phone = _read("webui", "src", "softphone.js")

    def test_validation_accepts_service_codes(self):
        self.assertIn(r"if (/^[*#][*#\d]{1,180}$/.test(number)) return number", self.view)

    def test_plain_national_numbers_are_still_rejected(self):
        # The E.164 guard is what stops a number being sent without its country code.
        self.assertIn(r"return /^\+[1-9]\d{6,14}$/.test(number) ? number : ''", self.view)

    def test_local_mmi_codes_are_answered_instead_of_dialled(self):
        self.assertIn("export const LOCAL_MMI = { '*#06#': 'imei' }", self.view)
        self.assertIn("const localField = LOCAL_MMI[target]", self.view)

    def test_there_is_no_cellular_dial_path(self):
        # This build has no modem: every call, service codes included, goes over VoWiFi.
        self.assertNotIn("cellularCall", self.view)
        self.assertNotIn("callTransport", self.view)

    def test_a_locally_rejected_code_is_not_blamed_on_the_carrier(self):
        # The dialplan records a call the moment its pattern matches, so an absent record
        # means the code never matched — our own Asterisk answered 404, the request never
        # reached the network. An engine image predating service-code support does exactly
        # that, and reporting it as "the carrier does not recognise this code" sends the user
        # to their operator over a stale image on their own machine.
        self.assertIn("if (!rawVerdict) {", self.view)
        self.assertIn("The gateway did not send this code.", self.view)
        # It must be checked before the SIP-cause fallback, which is what used to answer here.
        self.assertLess(self.view.index("if (!rawVerdict) {"),
                        self.view.index("return <div style={{ fontSize: 14, color: 'var(--text-mute)' }}>{endLabel(call.endCause, true)}"))

    def test_blocked_webrtc_is_reported_instead_of_timing_out(self):
        # A privacy extension replaces RTCPeerConnection with a non-constructor rather than
        # removing it, so JsSIP stalls inside connect() with no error: no SDP, no INVITE, and
        # the call screen runs its full course before blaming the carrier for a request that
        # never left the browser. Detect it up front and say so.
        self.assertIn("const WEBRTC_AVAILABLE = typeof RTCPeerConnection === 'function'", self.view)
        self.assertIn("if (!WEBRTC_AVAILABLE) {", self.view)
        # Refused before dialling, not after: the check must precede the JsSIP call.
        self.assertLess(self.view.index("!WEBRTC_AVAILABLE"),
                        self.view.index("phone.current.call(target)"))

    def test_sip_uri_escapes_hash_because_jssip_rejects_it(self):
        self.assertIn(r"const user = String(number).replace(/#/g, '%23')", self.phone)
        self.assertIn("this.ua.call(`sip:${user}@${domain}`", self.phone)
        self.assertNotIn("this.ua.call(`sip:${number}@${domain}`", self.phone)


@unittest.skipIf(main is None, "control plane dependencies are not installed")
class ServiceCodeOutcomeTests(unittest.TestCase):
    """How the carrier's answer is reported back to the operator.

    A service code cannot be scored like a call: it is answered and torn down at once, so
    'busy'/'no answer' would describe none of what actually happened. What matters is only
    whether this carrier serves the code, and the Q.850 cause is the only evidence for that.
    """

    def test_accepted_code_is_reported_as_accepted(self):
        self.assertEqual(
            main._call_disposition("ANSWER", 0, "out", "#225#"), "code accepted")

    def test_unknown_code_reads_as_unsupported(self):
        # 404 Not Found -> Q.850 1: this carrier has no such code.
        self.assertEqual(
            main._call_disposition("CONGESTION", 1, "out", "#225#"), "code unsupported")
        # 484 Address Incomplete -> Q.850 28, and 501 Not Implemented -> Q.850 79.
        self.assertEqual(
            main._call_disposition("CHANUNAVAIL", 28, "out", "*#21#"), "code unsupported")
        self.assertEqual(
            main._call_disposition("CONGESTION", 79, "out", "#225#"), "code unsupported")

    def test_refusal_is_distinguished_from_lack_of_support(self):
        # 403/603 -> Q.850 21. The code reached the carrier, which declined to serve it —
        # an account-policy restriction, not a missing feature. Conflating the two would
        # send the operator looking for the wrong fix.
        self.assertEqual(
            main._call_disposition("BUSY", 21, "out", "*21*13800138000#"), "code rejected")

    def test_silence_is_never_reported_as_unsupported(self):
        # Nothing came back at all; asserting the carrier lacks the code would be invention.
        self.assertEqual(
            main._call_disposition("NOANSWER", 0, "out", "#225#"), "code failed")

    def test_ambiguous_causes_seen_in_the_field_are_not_called_unsupported(self):
        # Observed on real carriers: Vodafone UK returned cause 38 (network out of order,
        # from SIP 503) and EE returned cause 127 (interworking, unspecified — Asterisk's
        # fallback when a response has no Q.850 mapping). Neither says the code is absent
        # from the network, so neither may be reported as unsupported.
        self.assertEqual(
            main._call_disposition("CHANUNAVAIL", 38, "out", "*#21#"), "code failed")
        self.assertEqual(
            main._call_disposition("CHANUNAVAIL", 127, "out", "*#21#"), "code failed")

    def test_ordinary_numbers_keep_call_shaped_outcomes(self):
        self.assertEqual(main._call_disposition("ANSWER", 0, "out", "+441234567890"), "answered")
        self.assertEqual(main._call_disposition("BUSY", 17, "out", "+441234567890"), "busy")
        self.assertEqual(main._call_disposition("NOANSWER", 19, "out", "611"), "no answer")

    def test_incoming_calls_are_never_scored_as_service_codes(self):
        # An inbound caller ID can contain '*'; only what we dialled out can be a code.
        self.assertEqual(main._call_disposition("BUSY", 21, "in", "*67"), "rejected")

    def test_service_code_detection(self):
        for code in ("#225#", "*#06#", "*21*13800138000#", "*#21#"):
            self.assertTrue(main._is_service_code(code), code)
        for number in ("+441234567890", "611", "13800138000", ""):
            self.assertFalse(main._is_service_code(number), number)


if __name__ == "__main__":
    unittest.main()
