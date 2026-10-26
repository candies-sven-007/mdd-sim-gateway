"""Who may ask for what, as a table, and checked against the routes that really exist."""
import re
import unittest

from starlette.routing import Route, WebSocketRoute

from control.app import authz, gate, main
from tests.route_listing import api_routes

ADMIN = gate.Principal("admin", csrf="c", credential="session:k")
# A kind of caller this build does not issue; it must get nothing.
CLIENT = gate.Principal("client", credential="client:3")


def routes():
    """Every (method, example path) the application serves under /api/ or as a socket."""
    found = []
    for route in api_routes(main.app):
        # "1" fits a parameter whether a rule expects any segment or a numeric id.
        example = re.sub(r"\{[^}]+\}", "1", route.path)
        if isinstance(route, WebSocketRoute):
            found.append(("WEBSOCKET", example))
        elif isinstance(route, Route) and example.startswith("/api/"):
            found.extend((method, example) for method in route.methods - {"HEAD"})
    return found


class AuthorizationTableTests(unittest.TestCase):
    def test_the_administrator_may_do_anything(self):
        for method, path in routes():
            with self.subTest(method=method, path=path):
                self.assertTrue(authz.allowed(ADMIN, method, path))

    def test_the_engine_may_only_deliver_its_callback(self):
        self.assertTrue(authz.allowed(gate.ENGINE, "POST", "/api/engine/event"))
        for method, path in routes():
            if path != "/api/engine/event":
                with self.subTest(method=method, path=path):
                    self.assertFalse(authz.allowed(gate.ENGINE, method, path))

    def test_nobody_else_is_allowed_anything(self):
        for who in (gate.ANONYMOUS, gate.Principal("someone-new"), None):
            self.assertFalse(authz.allowed(who, "GET", "/api/instances"))

    def test_a_former_client_kind_is_refused_everything(self):
        for method, path in routes():
            with self.subTest(method=method, path=path):
                self.assertFalse(authz.allowed(CLIENT, method, path))

    def test_no_client_sign_in_routes_remain(self):
        paths = {path for _method, path in routes()}
        self.assertFalse({p for p in paths if p.startswith("/api/auth/client")})


STATUS = {"state": "REGISTERED", "label": "Registered", "reason_code": "ok", "reason": "",
          "detail": {"pcscf": "10.0.0.1", "dns": ["10.0.0.2"], "pin": "verified"}}


class EventScopeTests(unittest.TestCase):
    def test_the_administrator_hears_everything_and_nobody_else_anything(self):
        event = {"type": "status", "instance": "sim1", **STATUS}
        self.assertIs(authz.event_for(ADMIN, event), event)
        for who in (CLIENT, gate.ENGINE, gate.ANONYMOUS, None):
            with self.subTest(who=who):
                self.assertIsNone(authz.event_for(who, {"type": "call", "instance": "sim1"}))


class StatusViewTests(unittest.TestCase):
    def test_only_the_administrator_sees_the_diagnostics(self):
        self.assertIs(authz.status_for(ADMIN, STATUS), STATUS)
        cut = authz.status_for(CLIENT, STATUS)
        self.assertEqual(set(cut), {"state", "label", "reason_code", "reason"})


if __name__ == "__main__":
    unittest.main()
