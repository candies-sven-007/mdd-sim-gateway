"""Telling a text message apart from a machine payload that merely looks like one.

Not every SMS is meant for a person. Carriers and services also send binary ones — SIM
OTA data-download, silent app pushes, WAP push — whose user data is arbitrary bytes, not
characters. The PDU says which is which in TP-DCS (3GPP TS 23.038 section 4) and TP-PID
(TS 23.040 9.2.3.9), but Asterisk's parse_tpdu() unpacks an 8-bit payload one byte per
character and hands back a string, so by the time the manager sees it a binary payload is
indistinguishable from a text that happens to be mojibake. Line 1 collected 16 of these
from short-codes 2942/2939 before the engine started forwarding the header fields.

The engine's Asterisk patch (engine/patches/asterisk/mt_concat_udh.py) now exposes TP-PID,
TP-DCS, the UDH and the raw PDU; this module turns those into the one decision the manager
needs — is this a text to show, or a payload to drop — and falls back to inspecting the
decoded body for an engine image that predates the patch.

Receive-only build: a machine payload is dropped (it was acknowledged inside Asterisk), never
stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Characters a real text can legitimately contain despite being control codes. Everything
# else below 0x20 is a byte that happened to land in a string, not something a person typed.
_TEXT_CONTROLS = "\n\r\t\v\f"


def _is_stray_byte(ch: str) -> bool:
    """A code point that only a widened payload byte can put in a string.

    Two ranges, and the second one matters more than it looks: C0 below 0x20, and DEL plus
    the C1 block (0x7F-0x9F). Neither of Asterisk's text paths can produce a C1 code point —
    the GSM 7-bit table maps to printable characters and Greek letters, and real UCS-2 text
    does not contain C1 controls — whereas a random payload byte lands there 1 time in 8.
    Without the C1 range an 8-byte payload has roughly a one-in-three chance of containing no
    marker at all and passing as text (payload c5e79b8fb7572767 on line 1 did exactly that).
    """
    o = ord(ch)
    return (o < 0x20 and ch not in _TEXT_CONTROLS) or 0x7F <= o <= 0x9F


def dcs_is_8bit(dcs: int) -> bool:
    """Does this TP-DCS say the user data is 8-bit binary rather than characters?

    Two of the coding groups in TS 23.038 4.3 can carry 8-bit data:
      00xxxxxx  general data coding — the character set is bits 3-2 (01 = 8-bit)
      1111xxxx  data coding / message class — bit 2 (1 = 8-bit)
    The groups in between (0100-1110: compressed, reserved, message-waiting) are all text.
    """
    if dcs >> 6 == 0b00:
        return (dcs >> 2) & 0b11 == 0b01
    if dcs >> 4 == 0b1111:
        return bool(dcs & 0b100)
    return False


def dcs_message_class(dcs: int) -> int | None:
    """The message class in TP-DCS, or None when the DCS does not carry one.

    Class 2 is the interesting one: TS 23.038 calls it "(U)SIM specific message", and a
    handset hands it to the card instead of showing it. Present only when bit 4 is set in
    the general group, and always present in the 1111 group.
    """
    if dcs >> 6 == 0b00 and dcs & 0b10000:
        return dcs & 0b11
    if dcs >> 4 == 0b1111:
        return dcs & 0b11
    return None


# TP-PID 0x7F = "(U)SIM Data download": the payload is addressed to the card, not the user.
PID_SIM_DATA_DOWNLOAD = 0x7F


@dataclass(frozen=True)
class PduMeta:
    """What the engine reported about one inbound PDU. Every field is optional because an
    engine image built before the patch sends none of them."""
    tp_pid: int | None = None
    tp_dcs: int | None = None
    udh_hex: str = ""
    tpdu_hex: str = ""

    @property
    def known(self) -> bool:
        return self.tp_dcs is not None

    @property
    def message_class(self) -> int | None:
        return None if self.tp_dcs is None else dcs_message_class(self.tp_dcs)

    @property
    def is_machine_payload(self) -> bool:
        """True for a PDU that was never meant to be displayed.

        Three independent markers, any one of which is enough: 8-bit user data (it is not
        text at all), message class 2 (addressed to the SIM), and TP-PID 0x7F (SIM data
        download). A carrier setting only one of them is normal, so they are OR-ed.
        """
        if self.tp_dcs is None:
            return False
        return (dcs_is_8bit(self.tp_dcs)
                or self.message_class == 2
                or self.tp_pid == PID_SIM_DATA_DOWNLOAD)


def parse_event_args(args: list) -> PduMeta:
    """Read the PDU header fields trailing an sms_in event.

    Argument layout, appended by the dialplan in this order:
        0 from  1 body-b64  2 concat-ref  3 concat-total  4 concat-seq
        5 tp-pid  6 tp-dcs  7 udh-hex  8 tpdu-hex
    An older engine sends only the first five (or only the first two), and Asterisk passes an
    empty string for a variable it never set — so anything missing or unparseable yields an
    empty PduMeta and the caller falls back to looks_binary().
    """
    def number(index: int) -> int | None:
        if len(args) <= index:
            return None
        try:
            return int(str(args[index]).strip())
        except (TypeError, ValueError):
            return None

    def hexed(index: int) -> str:
        if len(args) <= index:
            return ""
        value = str(args[index]).strip().lower()
        if not value or len(value) % 2 or any(ch not in "0123456789abcdef" for ch in value):
            return ""
        return value

    dcs = number(6)
    if dcs is not None and not 0 <= dcs <= 0xFF:
        dcs = None
    pid = number(5)
    if pid is not None and not 0 <= pid <= 0xFF:
        pid = None
    return PduMeta(tp_pid=pid, tp_dcs=dcs, udh_hex=hexed(7), tpdu_hex=hexed(8))


def looks_binary(text: str) -> bool:
    """Fallback classification from the decoded body alone, for an engine that does not
    report TP-DCS yet — and for the messages already in the database from before it did.

    Asterisk's 8-bit path widens each payload byte to one character, so a binary payload
    arrives as a string whose code points are all below 0x100 and which is peppered with
    code points no text would contain. Requiring such a marker keeps this off ordinary text:
    accented Latin, Greek and CJK all pass through the first test only.

    This stays a heuristic. A short payload made entirely of printable bytes is
    indistinguishable from a text here and will be shown as a message — which is precisely
    why the engine now reports TP-DCS, and why this runs only when it does not.
    """
    if not text:
        return False
    if any(ord(ch) > 0xFF for ch in text):
        return False               # a real character set — nothing a byte widen could make
    return any(_is_stray_byte(ch) for ch in text)


def _semi_octet(byte: int) -> int:
    return (byte & 0x0F) * 10 + (byte >> 4)


def deliver_timestamp(tpdu_hex: str) -> int | None:
    """TP-SCTS of an SMS-DELIVER as epoch seconds, or None when absent or malformed.

    The service-centre timestamp (TS 23.040 9.2.3.11) is what a handset shows as the time of
    a text, and the one ModemManager reports for a message the modem received. Reading it
    from the VoWiFi PDU too gives one text the same time -- and so the same identity -- on
    both transports. Seven semi-octets: year, month, day, hour, minute, second, then the
    zone in quarter hours whose sign is bit 3 of the first (low) nibble.
    """
    try:
        pdu = bytes.fromhex(str(tpdu_hex or ""))
    except ValueError:
        return None
    if len(pdu) < 2 or pdu[0] & 0x03 != 0x00:      # TP-MTI 00 = SMS-DELIVER
        return None
    index = 3 + (pdu[1] + 1) // 2                   # TP-OA: digit count, TON/NPI, digits
    index += 2                                      # TP-PID, TP-DCS
    if len(pdu) < index + 7:
        return None
    octets = pdu[index:index + 7]
    zone = octets[6]
    quarters = ((zone & 0x07) * 10) + (zone >> 4)
    if zone & 0x08:
        quarters = -quarters
    try:
        local = datetime(2000 + _semi_octet(octets[0]), _semi_octet(octets[1]),
                         _semi_octet(octets[2]), _semi_octet(octets[3]),
                         _semi_octet(octets[4]), _semi_octet(octets[5]),
                         tzinfo=timezone(timedelta(minutes=15 * quarters)))
    except ValueError:
        return None
    return int(local.timestamp())


def concat_triplet(args: list) -> tuple[int, int, int] | None:
    """(ref, total, seq) of a segment of a multi-part text, or None for an ordinary SMS.

    sms_in arguments 2-4, empty strings when the message is a single part.
    """
    try:
        ref, total, seq = (int(str(args[i]).strip()) for i in (2, 3, 4))
    except (IndexError, TypeError, ValueError):
        return None
    if total < 2 or total > 255 or not 1 <= seq <= total or not 0 <= ref <= 0xFFFF:
        return None
    return ref, total, seq
