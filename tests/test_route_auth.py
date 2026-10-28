"""Signed out, every baseline API route and WebSocket is refused, except the few that sign one in.

Each route of the 9fcbe5c baseline (tests/test_route_set.py) is requested through the whole
application -- the gate middleware, the audit middleware and the routers -- with no session
cookie. Everything outside PUBLIC must answer 401 or 403; a WebSocket must be closed with 4401
or 4403, the gate's WebSocket forms of the same two answers.

Both states of the install are covered: before the first administrator is set up (the gateway
is most exposed then) and after.

starlette.testclient needs httpx, which is a test-only dependency (tests/requirements.txt); the
gateway itself does not install it.
"""
import logging
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

import anyio
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from control.app import auth, gate, main
from control.app import config as cfg
from tests.test_route_set import BASELINE_WEBSOCKET, SERVED_HTTP as BASELINE_HTTP

# Reachable without a session, and why. Read from gate.py and routers/auth.py:
PUBLIC = {
    # gate.PUBLIC_PATHS: the login screen asks this first, to know whether to show "set up the
    # administrator" or "sign in". It reports configured / authenticated / username and the
    # caller's own CSRF token, which is empty for a caller without a session.
    ("GET", "/api/auth/status"),
    # gate.SIGN_IN_PATHS: creates the first administrator. The handler answers 409 once
    # auth.configured() is true, so it is open only on an install nobody has set up yet.
    ("POST", "/api/auth/setup"),
    # gate.SIGN_IN_PATHS: exchanges the password for a session. Throttled per peer
    # (auth.throttled), 409 before setup, 401 for a wrong password.
    ("POST", "/api/auth/login"),
}

# Not public, though it takes no session either: the gate's "engine" source requires the
# per-install engine token in x-mdd-engine-token and refuses with 401 without it. It is in the
# refused set below; the test for it also sends a wrong token.
ENGINE_EVENT = ("POST", "/api/engine/event")

REFUSED_HTTP = {401, 403}
REFUSED_WEBSOCKET = {gate.WS_UNAUTHENTICATED, gate.WS_FORBIDDEN}

HOST = "gateway.example.net"
# A socket the gate wrongly let through may sit waiting for the client; give up on it after this.
WEBSOCKET_WAIT_SECONDS = 5
ENGINE_TOKEN = "test-engine-token"
PASSWORD = "correct horse battery"


# main.py sets the root logger to INFO; one line per request would bury the test output.
logging.getLogger("httpx").setLevel(logging.WARNING)


async def _first_message(socket):
    """The first message the application sent on an accepted socket, or None if it sent none.

    WebSocketTestSession.receive() waits without a limit, so an endpoint that the gate wrongly
    let through and that waits for the client would hang the test instead of failing it.
    """
    with anyio.move_on_after(WEBSOCKET_WAIT_SECONDS):
        return await socket._send_rx.receive()
    return None


def concrete(path):
    """The template with every {parameter} filled in; the gate decides before any validation."""
    return re.sub(r"\{[^}]+\}", "1", path)


class _SignedOut(unittest.TestCase):
    configured = False

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="mdd-route-auth-")
        self.addCleanup(shutil.rmtree, self.data, ignore_errors=True)
        for patcher in (
                patch.object(cfg, "DATA_DIR", self.data),
                patch.object(auth, "AUTH_PATH", os.path.join(self.data, "auth.json")),
                patch.object(auth, "SESSIONS_PATH", os.path.join(self.data, "sessions.json")),
                patch.object(auth, "_sessions", {}),
                patch.object(auth, "_failures", {}),
                patch.object(cfg, "internal_event_token", lambda: ENGINE_TOKEN)):
            patcher.start()
            self.addCleanup(patcher.stop)
        if self.configured:
            auth.setup(PASSWORD, "admin")
        self.assertEqual(auth.configured(), self.configured)
        # No `with`: the lifespan (background pollers, engine start-up) stays out of this test.
        self.client = TestClient(main.app, base_url=f"https://{HOST}",
                                 raise_server_exceptions=False)

    def request(self, method, path, **kwargs):
        if method in gate.MUTATING_METHODS:
            kwargs.setdefault("json", {})
        return self.client.request(method, concrete(path), **kwargs)

    def test_the_public_set_is_what_the_gate_lets_through(self):
        public_paths = {path for _, path in PUBLIC}
        self.assertEqual(public_paths, set(gate.PUBLIC_PATHS))
        self.assertTrue(PUBLIC <= BASELINE_HTTP)
        self.assertIn(ENGINE_EVENT, BASELINE_HTTP)

    def test_every_other_baseline_route_is_refused(self):
        for method, path in sorted(BASELINE_HTTP - PUBLIC):
            with self.subTest(method=method, path=path):
                response = self.request(method, path)
                self.assertIn(response.status_code, REFUSED_HTTP, response.text)

    def test_a_cookie_that_names_no_session_is_refused_too(self):
        self.client.cookies.set(auth.SESSION_COOKIE, "not-a-session")
        for method, path in sorted(BASELINE_HTTP - PUBLIC):
            with self.subTest(method=method, path=path):
                self.assertIn(self.request(method, path).status_code, REFUSED_HTTP)

    def test_the_engine_callback_needs_its_token(self):
        method, path = ENGINE_EVENT
        for headers in ({}, {gate.ENGINE_TOKEN_HEADER: "wrong"}):
            with self.subTest(headers=headers):
                response = self.request(method, path, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["detail"], "invalid engine token")

    def test_public_routes_reach_their_handler(self):
        """The gate lets them through; what the handler answers depends on the install state."""
        for method, path in sorted(PUBLIC):
            with self.subTest(method=method, path=path):
                response = self.request(method, path)
                self.assertNotEqual(response.json().get("detail"), "authentication required")
        status = self.request("GET", "/api/auth/status").json()
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["csrf"], "")
        self.assertEqual(status["configured"], self.configured)

    def test_public_routes_do_not_sign_anyone_in_without_a_password(self):
        self.assertIn(self.request("POST", "/api/auth/login").status_code,
                      {401, 409} if self.configured else {409})
        self.assertEqual(self.request("POST", "/api/auth/setup").status_code,
                         409 if self.configured else 400)
        self.assertEqual(auth.configured(), self.configured)
        self.assertEqual(auth._sessions, {})

    def test_no_api_schema_for_anyone(self):
        for path in ("/openapi.json", "/docs", "/redoc"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotIn('"paths"', response.text)
                self.assertNotIn("swagger", response.text.lower())

    def connect(self, path, **kwargs):
        """The close code the gate answers a handshake with, or None if it was accepted."""
        headers = {"origin": f"https://{HOST}", **kwargs.pop("headers", {})}
        try:
            # A relative URL would be sent with Host "testserver", not the base URL's host.
            with self.client.websocket_connect(f"wss://{HOST}{concrete(path)}",
                                               headers=headers, **kwargs) as socket:
                message = socket.portal.call(_first_message, socket) or {}
        except WebSocketDisconnect as closed:
            # Refused before it was accepted (a handshake that asked for a subprotocol).
            return closed.code
        # Accepted, then closed straight away: the close arrives as the first message.
        return message.get("code") if message.get("type") == "websocket.close" else None

    def test_every_baseline_websocket_is_refused(self):
        for path in sorted(BASELINE_WEBSOCKET):
            # The softphone asks for the "sip" subprotocol, which the gate refuses before
            # accepting; the event socket is accepted and then closed so the page sees 4401.
            for subprotocols in ((), ("sip",)):
                with self.subTest(path=path, subprotocols=subprotocols):
                    code = self.connect(path, subprotocols=list(subprotocols))
                    self.assertIn(code, REFUSED_WEBSOCKET)

    def test_a_websocket_with_an_unknown_cookie_is_refused(self):
        self.client.cookies.set(auth.SESSION_COOKIE, "not-a-session")
        for path in sorted(BASELINE_WEBSOCKET):
            with self.subTest(path=path):
                self.assertIn(self.connect(path), REFUSED_WEBSOCKET)


class BeforeSetupTests(_SignedOut):
    configured = False


class AfterSetupTests(_SignedOut):
    configured = True

    def test_a_signed_in_session_does_get_through(self):
        """The refusals above come from the missing session, not from a broken test client."""
        token, csrf = auth.login("admin", PASSWORD, "192.0.2.10")
        self.client.cookies.set(auth.SESSION_COOKIE, token)
        status = self.request("GET", "/api/auth/status").json()
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["csrf"], csrf)
        response = self.request("POST", "/api/auth/password",
                                headers={gate.CSRF_HEADER: csrf})
        self.assertEqual(response.status_code, 400)   # reached the handler: empty passwords


# The shared cases live on _SignedOut; only the two concrete classes run them.
del _SignedOut


if __name__ == "__main__":
    unittest.main()
