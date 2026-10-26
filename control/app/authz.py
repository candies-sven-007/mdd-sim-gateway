"""What an authenticated caller may ask for, decided in one place.

gate.py answers "who is this"; this module answers "may they". Kept free of FastAPI so the
rules can be tested as a table.

* The administrator may do anything, so a new management route needs no entry here.
* The engine may deliver its callback and nothing else.
* A client app (clients.py) may use a line's communication features -- texts, MMS, calls,
  voicemail, the softphone relay, and a read-only view of the line -- and nothing else. The list
  is an allow-list: a route added later is refused to clients until somebody decides a client
  needs it, and the pull request that adds such a route adds its row here.
* Anything else is refused.

Events on the live WebSocket follow the same idea (``event_for``): a client hears about its
lines' conversations and calls, not about the host. A line's status is cut to what a client
needs wherever it appears -- the status route, the line list and the status event
(``status_for``).
"""
from __future__ import annotations

import re

LINE_PREFIX = re.compile(r"^/api/instances/(?P<iid>[^/]+)(?P<rest>/.*)$")
ENGINE_EVENT_PATH = "/api/engine/event"


def _rules(*rows: tuple[str, str]) -> tuple[tuple[frozenset[str], re.Pattern[str]], ...]:
    return tuple((frozenset(methods.split()), re.compile(pattern)) for methods, pattern in rows)


# Suffixes under /api/instances/<line>/ a client may use, by method. "WEBSOCKET" is a handshake.
CLIENT_LINE_RULES = _rules(
    # A read-only view of the line itself.
    ("GET", r"^/(status|availability|allowance)$"),
    # Texts and MMS: messages/threads, messages/unread and messages/<peer>. Not messages/binary,
    # the filed raw PDUs and SIM OTA payloads, which are the administrator's to diagnose. The
    # MMS settings tell the composer the line's size limit (changing them stays administrative).
    ("GET", r"^/messages/(?!binary$)[^/]+$"),
    ("GET", r"^/messages/[^/]+/mms/parts/[^/]+$"),
    ("POST", r"^/(sms/send|mms/send|messages/delete|messages/read)$"),
    ("GET", r"^/mms/settings$"),
    ("POST", r"^/messages/[^/]+/mms/download$"),
    # Composing an MMS: attachments are uploaded as they are added, fitted to the limit and
    # previewed as they will be sent, and removed again, as the WebUI does.
    ("POST", r"^/mms/attachments(/fit)?$"),
    ("GET", r"^/mms/attachments/[^/]+/preview$"),
    ("DELETE", r"^/mms/attachments/[^/]+$"),
    # Calls, through the softphone relay and over the modem.
    ("GET", r"^/(calls|cellular-call/status)$"),
    ("POST", r"^/(call|hangup|calls/delete|cellular-call|cellular-call/hangup)$"),
    # Voicemail.

    # The softphone: its provisioning and the signalling relay.
    ("GET", r"^/softphone$"),
    ("WEBSOCKET", r"^/softphone/ws$"),
)

# Paths outside /api/instances/<line>/ a client may use. The line list is answered with a
# client's cut of each line (see main.api_instances).
CLIENT_GLOBAL_RULES = _rules(
    ("GET", r"^/api/instances$"),
    ("GET", r"^/api/auth/status$"),
    ("POST", r"^/api/auth/client/logout$"),
    ("GET", r"^/api/messages/unread$"),
    # The address book is the administrator's, and a client acts for the administrator: the
    # names a phone shows are the ones the WebUI shows.
    ("GET", r"^/api/contacts(/export)?$"),
    ("POST", r"^/api/contacts(/resolve|/import)?$"),
    ("PUT DELETE", r"^/api/contacts/[0-9]+$"),
    ("WEBSOCKET", r"^/ws$"),
)

# Live events a client receives, and only for a line: its conversations, calls and the line's
# own state. Host alerts, hardware, cards and engine maintenance stay with the administrator.
CLIENT_EVENTS = frozenset({"sms", "call", "status", "line"})



def allowed(principal, method: str, path: str) -> bool:
    """Whether this caller may make this request. Fails closed for anything unlisted."""
    kind = getattr(principal, "kind", "")
    method = str(method or "").upper()
    path = path or ""
    if kind == "admin":
        return True
    if kind == "engine":
        return method == "POST" and path == ENGINE_EVENT_PATH
    if kind == "client":
        match = LINE_PREFIX.match(path)
        if match:
            rest = match.group("rest")
            return any(method in methods and pattern.match(rest)
                       for methods, pattern in CLIENT_LINE_RULES)
        return any(method in methods and pattern.match(path)
                   for methods, pattern in CLIENT_GLOBAL_RULES)
    return False


# What a client is told about a line's state: enough to show whether it can text and call, not
# the diagnostics (P-CSCF, DNS, PIN state, IKE reasons) the administrator troubleshoots with.
CLIENT_STATUS_FIELDS = ("state", "label", "reason_code", "reason")


def status_for(principal, status: dict) -> dict:
    """A line's status as this caller may see it. Every route that shows one goes through here."""
    if getattr(principal, "kind", "") == "admin":
        return status
    return {key: status.get(key) for key in CLIENT_STATUS_FIELDS}


def event_for(principal, message: dict) -> dict | None:
    """The broadcast event as this caller's live WebSocket receives it, or None. Fails closed."""
    kind = getattr(principal, "kind", "")
    if kind == "admin":
        return message
    if kind != "client" or message.get("type") not in CLIENT_EVENTS or \
            not message.get("instance"):
        return None
    if message.get("type") == "status":
        # A status event carries the whole status at its top level, beside type and instance.
        return {"type": "status", "instance": message["instance"],
                **status_for(principal, message)}
    return message
