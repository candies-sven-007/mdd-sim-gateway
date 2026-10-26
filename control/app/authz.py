"""What an authenticated caller may ask for, decided in one place.

gate.py answers "who is this"; this module answers "may they". Kept free of FastAPI so the
rules can be tested as a table.

* The administrator (a signed-in browser) may do anything, so a new management route needs no
  entry here.
* The engine may deliver its callback and nothing else.
* Anything else is refused. This build has no client-app tokens: the browser WebUI is the only
  interactive caller.

Events on the live WebSocket follow the same rule (``event_for``), and a line's status is shown
whole only to the administrator (``status_for``).
"""
from __future__ import annotations

ENGINE_EVENT_PATH = "/api/engine/event"

# What anyone other than the administrator would be told about a line's state, should a
# non-administrator caller ever reach a status view: not the diagnostics (P-CSCF, DNS, PIN
# state, IKE reasons).
PUBLIC_STATUS_FIELDS = ("state", "label", "reason_code", "reason")


def allowed(principal, method: str, path: str) -> bool:
    """Whether this caller may make this request. Fails closed for anything unlisted."""
    kind = getattr(principal, "kind", "")
    method = str(method or "").upper()
    if kind == "admin":
        return True
    if kind == "engine":
        return method == "POST" and (path or "") == ENGINE_EVENT_PATH
    return False


def status_for(principal, status: dict) -> dict:
    """A line's status as this caller may see it. Every route that shows one goes through here."""
    if getattr(principal, "kind", "") == "admin":
        return status
    return {key: status.get(key) for key in PUBLIC_STATUS_FIELDS}


def event_for(principal, message: dict) -> dict | None:
    """The broadcast event as this caller's live WebSocket receives it, or None. Fails closed."""
    return message if getattr(principal, "kind", "") == "admin" else None
