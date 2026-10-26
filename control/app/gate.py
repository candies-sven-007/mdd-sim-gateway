"""Who is calling the control surface, answered once for HTTP and WebSocket alike.

Every management route lives under ``/api/`` and every WebSocket needs a signed-in browser, so
one ASGI middleware sits in front of both. It used to be an HTTP middleware, which never sees a
WebSocket handshake: each socket endpoint had to repeat the session check itself, and a socket
added later without that check would have been open to anyone.

The middleware resolves the caller to a :class:`Principal`, refuses what needs a credential
the caller does not have, and leaves the answer in ``scope["state"]["principal"]`` for handlers
that need to know more than "allowed".

A socket authenticated by the session cookie must also come from the gateway's own page. The
cookie is SameSite=Strict, but "site" means the registrable domain: a page on a sibling host
(``other.example.net`` beside ``gateway.example.net``) still gets the cookie attached. Browsers
always send ``Origin`` on a WebSocket handshake and a page cannot change it, so the handshake is
accepted only when that origin is the host the browser asked for.

Once the caller is known, authz.allowed decides whether they may make this request. Revoking a
credential also closes the WebSockets opened with it (``revoke``), so a signed-out browser
stops receiving events at once rather than at its next reconnect.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from starlette.requests import cookie_parser
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from . import auth, authz
from . import config as cfg

log = logging.getLogger("mdd.gate")

# Where one signs in.
SIGN_IN_PATHS = frozenset({"/api/auth/setup", "/api/auth/login"})
PUBLIC_PATHS = SIGN_IN_PATHS | {"/api/auth/status"}
ENGINE_EVENT_PATH = "/api/engine/event"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "x-mdd-csrf-token"
ENGINE_TOKEN_HEADER = "x-mdd-engine-token"

# WebSocket close codes. 4401: the credential is missing, expired or revoked -- sign in again.
# 4403: this caller may not open this socket, or the page is not one the gateway serves;
# signing in again would not change that, so the page does not retry.
WS_UNAUTHENTICATED = 4401
WS_FORBIDDEN = 4403

# The control surface is served only over TLS (run.py) and its cookie is Secure, so the page a
# signed-in socket comes from is always https; a Host without a port means 443.
HTTPS_PORT = 443


@dataclass(frozen=True)
class Principal:
    """The caller of one request or socket."""

    kind: str                   # "admin" | "engine" | "anonymous"
    csrf: str = ""              # the session's CSRF token; only a cookie session has one
    # Names the credential for revocation without being it: "session:<digest>".
    credential: str = ""

    @property
    def authenticated(self) -> bool:
        return self.kind != "anonymous"

    @property
    def label(self) -> str:
        """Who acted, for the audit log."""
        return self.kind


ANONYMOUS = Principal("anonymous")
ENGINE = Principal("engine")


def _headers(scope) -> dict[str, str]:
    """Request headers, lower-cased; a repeated header keeps its first value."""
    headers: dict[str, str] = {}
    for name, value in scope.get("headers") or ():
        headers.setdefault(name.decode("latin-1").lower(), value.decode("latin-1"))
    return headers


def _cookie(headers: dict[str, str], name: str) -> str:
    """One cookie's value, read the way browsers send them rather than the way RFC 6265 asks.

    A browser sends every cookie of the host name, whatever the port, so other services on the
    same host add theirs: values with spaces or quotes are common there. http.cookies drops such
    a cookie and every one after it without a word, which would lose the session and leave the
    administrator signed in yet refused. Starlette's parser, which handled this before the gate
    existed, splits on ";" and keeps going.
    """
    return cookie_parser(headers.get("cookie", "")).get(name, "")


def _session(headers: dict[str, str]) -> Principal | None:
    token = _cookie(headers, auth.SESSION_COOKIE) or None
    # Every browser session was issued by an administrator. `install.sh
    # reset-admin` removes the account without restarting anything, so until a new one is set
    # up there is nobody for them to speak for.
    if not token or not auth.configured():
        return None
    current = auth.session(token)
    if not current:
        return None
    return Principal("admin", csrf=str(current.get("csrf") or ""),
                     credential="session:" + auth.session_key(token))


def _engine(headers: dict[str, str]) -> Principal | None:
    expected = cfg.internal_event_token()
    supplied = headers.get(ENGINE_TOKEN_HEADER, "")
    return ENGINE if expected and hmac.compare_digest(supplied, expected) else None


def _nobody(headers: dict[str, str]) -> Principal | None:
    return None


def _api(scope, headers=None) -> bool:
    return (scope.get("path") or "").startswith("/api/")


@dataclass(frozen=True)
class Source:
    """Where one kind of caller's credential comes from, and what else it must prove.

    ``resolve`` reads the credential from the headers and returns None when there is none or it
    is not valid. ``required`` refuses the request then; otherwise it goes on as anonymous.
    ``csrf`` asks state-changing requests for the session's CSRF token, which a browser does not
    attach by itself. ``origin`` asks a WebSocket handshake for the gateway's own page.
    """

    name: str
    transport: str                          # "http" | "websocket"
    matches: Callable[[dict, dict[str, str]], bool]
    resolve: Callable[[dict[str, str]], Principal | None]
    required: bool = True
    refusal: str = "authentication required"
    csrf: bool = False
    origin: bool = False


# Checked in order; the first source whose ``matches`` accepts the scope decides alone, so a
# credential is honoured only where its own row says so -- the engine token nowhere but the
# engine callback, the session cookie nowhere on it.
SOURCES: tuple[Source, ...] = (
    # Static assets stay public so the browser can render the login screen.
    Source("static", "http", lambda scope, headers: not _api(scope), _nobody, required=False),
    # Reachable signed out; a session, if there is one, is still reported to the handler.
    Source("public", "http", lambda scope, headers: scope.get("path") in PUBLIC_PATHS, _session,
           required=False),
    Source("engine", "http", lambda scope, headers: scope.get("path") == ENGINE_EVENT_PATH,
           _engine, refusal="invalid engine token"),
    Source("session", "http", _api, _session, csrf=True),
    Source("socket", "websocket", lambda scope, headers: True, _session, origin=True),
)


def source_for(scope, headers: dict[str, str] | None = None) -> Source | None:
    """The one credential source that decides this request, if the gate handles it at all."""
    headers = headers if headers is not None else _headers(scope)
    for source in SOURCES:
        if source.transport == scope.get("type") and source.matches(scope, headers):
            return source
    return None


def principal(scope) -> Principal:
    """Who is calling, from the credential its source accepts.

    Only says who; whether that is enough for the path is the middleware's decision.
    """
    headers = _headers(scope)
    source = source_for(scope, headers)
    return (source.resolve(headers) if source else None) or ANONYMOUS


def current(connection) -> Principal:
    """The principal the middleware resolved, for a handler's Request or WebSocket."""
    found = connection.scope.get("state", {}).get("principal")
    return found if isinstance(found, Principal) else ANONYMOUS


def trusted_proxy(peer: str, settings: dict | None = None) -> bool:
    """Whether the direct peer is a reverse proxy whose forwarding headers are believed."""
    settings = settings if settings is not None else cfg.get_settings()
    trusted = (settings.get("security") or {}).get("trusted_proxies") or []
    try:
        address = ipaddress.ip_address(peer)
        return any(address in ipaddress.ip_network(str(item), strict=False) for item in trusted)
    except ValueError:
        return False


def _host_port(authority: str) -> tuple[str, int] | None:
    """``host[:port]`` normalised for comparison; the port defaults to HTTPS's."""
    try:
        parts = urlsplit("//" + authority.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    return host.lower(), port or HTTPS_PORT


def origin_allowed(scope, headers: dict[str, str] | None = None,
                   settings: dict | None = None) -> bool:
    """Whether a WebSocket handshake comes from a page the gateway itself served.

    A browser always sends ``Origin`` on a handshake, so a missing one, or the opaque ``null``
    of a sandboxed frame or a local file, is refused: nothing that uses the session cookie
    legitimately arrives without it. The gateway's own page is always https, so an http origin
    on the same host name is another page, not this one. Behind a reverse proxy that rewrites ``Host``, the name the
    browser used is in ``X-Forwarded-Host``, which is believed only from a configured trusted
    proxy -- the same rule the audit log applies to ``X-Forwarded-For``.
    """
    headers = headers if headers is not None else _headers(scope)
    origin = headers.get("origin", "").strip()
    if not origin or origin == "null":
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.path not in ("", "/"):
        return False
    wanted = _host_port(parts.netloc)
    if wanted is None:
        return False
    if _host_port(headers.get("host", "")) == wanted:
        return True
    forwarded = headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    peer = (scope.get("client") or ("",))[0] or ""
    return bool(forwarded) and trusted_proxy(peer, settings) and \
        _host_port(forwarded) == wanted


class _LiveSockets:
    """The open WebSockets per credential, so revoking a credential can close them."""

    def __init__(self):
        self._open: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}

    def add(self, credential: str) -> tuple[asyncio.AbstractEventLoop, asyncio.Event]:
        entry = (asyncio.get_running_loop(), asyncio.Event())
        self._open.setdefault(credential, set()).add(entry)
        return entry

    def discard(self, credential: str, entry) -> None:
        entries = self._open.get(credential)
        if entries is not None:
            entries.discard(entry)
            if not entries:
                self._open.pop(credential, None)

    def revoke(self, match: Callable[[str], bool]) -> int:
        """Close every socket whose credential matches; safe to call from any thread."""
        closed = 0
        for credential, entries in list(self._open.items()):
            if match(credential):
                for loop, event in list(entries):
                    loop.call_soon_threadsafe(event.set)
                    closed += 1
        return closed


_live = _LiveSockets()


def revoke(credential: str) -> int:
    """Close the WebSockets opened with this credential (a sign-out)."""
    return _live.revoke(lambda item: bool(credential) and item == credential)


def revoke_kind(kind: str) -> int:
    """Close every WebSocket of one kind of credential, e.g. "session"."""
    return _live.revoke(lambda item: item.startswith(kind + ":"))


class Gate:
    """ASGI middleware: dispatch every request and handshake to its credential source."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = _headers(scope) if scope["type"] in ("http", "websocket") else {}
        source = source_for(scope, headers)
        if source is None:
            return await self.app(scope, receive, send)
        who = await self._resolve(source, headers)
        if who is None and source.required:
            return await self._deny(scope, receive, send, 401, source.refusal,
                                    WS_UNAUTHENTICATED)
        who = who or ANONYMOUS
        method = "WEBSOCKET" if scope["type"] == "websocket" else \
            scope.get("method", "GET").upper()
        if source.csrf and method in MUTATING_METHODS:
            if not hmac.compare_digest(headers.get(CSRF_HEADER, ""), who.csrf):
                return await self._deny(scope, receive, send, 403, "invalid CSRF token",
                                        WS_FORBIDDEN)
        if source.origin and not origin_allowed(scope, headers):
            log.warning("refused WebSocket %s from origin %r (host %r, forwarded host %r)",
                        scope.get("path"), headers.get("origin", ""), headers.get("host", ""),
                        headers.get("x-forwarded-host", ""))
            return await self._deny(scope, receive, send, 403, "origin not allowed",
                                    WS_FORBIDDEN)
        # Anonymous callers only get this far on a source that does not require a credential,
        # which is what makes those paths public; everyone else asks authz.
        if who.authenticated and not authz.allowed(who, method, scope.get("path") or ""):
            return await self._deny(scope, receive, send, 403, "not permitted", WS_FORBIDDEN)
        scope.setdefault("state", {})["principal"] = who
        if scope["type"] == "websocket" and who.credential:
            return await self._revocable(scope, receive, send, who.credential, source, headers)
        await self.app(scope, receive, send)

    @staticmethod
    async def _resolve(source: Source, headers: dict[str, str]) -> Principal | None:
        """Check a credential off the event loop.

        Checking reads the credential stores and now and then writes a renewed expiry to disk;
        on an SD card that write can stall, and every other request and socket would wait on
        it. A source that reads no credential is answered in place.
        """
        if source.resolve is _nobody:
            return None
        return await asyncio.to_thread(source.resolve, headers)

    async def _revocable(self, scope, receive, send, credential: str, source: Source,
                         headers: dict[str, str]):
        """Run a socket that ends, with 4401, as soon as its credential is revoked."""
        entry = _live.add(credential)
        # A revocation between the check above and this registration found nothing to close;
        # checking again now that the socket is registered leaves no such window.
        if await self._resolve(source, headers) is None:
            _live.discard(credential, entry)
            return await self._refuse(scope, receive, send, WS_UNAUTHENTICATED)
        revoked = entry[1]
        closed = False

        async def guarded_receive():
            nonlocal closed
            if closed:
                return {"type": "websocket.disconnect", "code": WS_UNAUTHENTICATED}
            incoming = asyncio.ensure_future(receive())
            waiting = asyncio.ensure_future(revoked.wait())
            done, _ = await asyncio.wait({incoming, waiting}, return_when=asyncio.FIRST_COMPLETED)
            if incoming in done:
                waiting.cancel()
                return incoming.result()
            incoming.cancel()
            closed = True
            try:
                await send({"type": "websocket.close", "code": WS_UNAUTHENTICATED})
            except Exception:
                pass
            return {"type": "websocket.disconnect", "code": WS_UNAUTHENTICATED}

        async def guarded_send(message):
            if closed:
                # The gate already closed the socket; the application learns so from receive.
                return
            await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        finally:
            _live.discard(credential, entry)

    async def _deny(self, scope, receive, send, status: int, detail: str, close_code: int):
        if scope["type"] == "websocket":
            return await self._refuse(scope, receive, send, close_code)
        await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

    @staticmethod
    async def _refuse(scope, receive, send, code: int) -> None:
        """Close a handshake the way its client can read.

        A browser that asked for a subprotocol (the softphone's ``sip``) fails any handshake whose
        answer does not name one, so that socket is refused before it is accepted; the client sees
        a failed handshake (HTTP 403). (An ASGI denial response would carry the status itself,
        but uvicorn's default implementation logs every one as an error.) Anything else is
        accepted first, because a browser reports the close code only for an open socket: the
        WebUI's event socket relies on seeing 4401 to know the session has ended.
        """
        ws = WebSocket(scope, receive, send)
        if scope.get("subprotocols"):
            return await ws.close(code=code)
        await ws.accept()
        await ws.close(code=code)
