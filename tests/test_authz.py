"""Who may ask for what, as a table, and checked against the routes that really exist."""
import re
import unittest

from starlette.routing import Route, WebSocketRoute

from control.app import authz, gate, main

ADMIN = gate.Principal("admin", csrf="c", credential="session:k")
CLIENT = gate.Principal("client", client_id=3, credential="client:3")


def routes():
    """Every (method, example path) the application serves under /api/ or as a socket."""
    found = []
    for route in main.app.routes:
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

    def test_a_client_may_talk_on_a_line(self):
        for method, path in (
            ("GET", "/api/instances"),
            ("GET", "/api/auth/status"),
            ("POST", "/api/auth/client/logout"),
            ("WEBSOCKET", "/ws"),
            ("GET", "/api/instances/sim1/status"),
            ("GET", "/api/instances/sim1/messages/threads"),
            ("GET", "/api/instances/sim1/messages/+61400000000"),
            ("POST", "/api/instances/sim1/sms/send"),
            ("POST", "/api/instances/sim1/mms/send"),
            ("POST", "/api/instances/sim1/messages/42/mms/download"),
            ("GET", "/api/instances/sim1/messages/42/mms/parts/1"),
            ("POST", "/api/instances/sim1/call"),
            ("POST", "/api/instances/sim1/hangup"),
            ("POST", "/api/instances/sim1/cellular-call"),
            ("GET", "/api/instances/sim1/softphone"),
            ("WEBSOCKET", "/api/instances/sim1/softphone/ws"),
            ("GET", "/api/instances/sim1/messages/unread"),
            ("POST", "/api/instances/sim1/messages/read"),
            ("GET", "/api/messages/unread"),
            ("GET", "/api/instances/sim1/mms/settings"),
            ("POST", "/api/instances/sim1/mms/attachments"),
            ("POST", "/api/instances/sim1/mms/attachments/fit"),
            ("GET", "/api/instances/sim1/mms/attachments/0123456789abcdef/preview"),
            ("DELETE", "/api/instances/sim1/mms/attachments/0123456789abcdef"),
            ("GET", "/api/contacts"),
            ("GET", "/api/contacts/export"),
            ("POST", "/api/contacts"),
            ("POST", "/api/contacts/resolve"),
            ("POST", "/api/contacts/import"),
            ("PUT", "/api/contacts/12"),
            ("DELETE", "/api/contacts/12"),
        ):
            with self.subTest(method=method, path=path):
                self.assertTrue(authz.allowed(CLIENT, method, path))

    def test_a_client_may_not_administer_anything(self):
        for method, path in (
            ("POST", "/api/instances"),
            ("DELETE", "/api/instances/sim1"),
            ("PUT", "/api/instances/sim1/country"),
            ("POST", "/api/instances/sim1/stop"),
            ("GET", "/api/instances/sim1/logs"),
            # Filed raw PDUs and SIM OTA payloads are the administrator's to diagnose.
            ("GET", "/api/instances/sim1/messages/binary"),
            ("PUT", "/api/instances/sim1/allowance"),
            ("GET", "/api/devices"),
            ("GET", "/api/auth/clients"),
            ("DELETE", "/api/auth/clients/4"),
            ("POST", "/api/auth/password"),
            ("POST", "/api/auth/logout"),
            ("POST", "/api/engine/event"),
            ("GET", "/api/readers"),
            ("GET", "/api/system/update/check"),
            ("PUT", "/api/instances/sim1/mms/settings"),
            ("GET", "/api/instances/sim1/mms/attachments"),
            ("DELETE", "/api/contacts/not-a-number"),
            # A method that is not listed for a listed path.
            ("DELETE", "/api/instances/sim1/messages/threads"),
            # Traversal-looking paths do not match a suffix rule.
            ("GET", "/api/instances/sim1/status/../logs"),
            ("GET", "/api/instances//status"),
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(authz.allowed(CLIENT, method, path))

    def test_every_client_rule_names_a_route_that_exists(self):
        # A rule left behind by a renamed or removed route would silently grant nothing today
        # and something unintended once a new route happens to match it.
        served = routes()
        prefix = "/api/instances/1"
        for methods, pattern in authz.CLIENT_LINE_RULES:
            for method in methods:
                with self.subTest(method=method, pattern=pattern.pattern):
                    self.assertTrue(any(m == method and p.startswith(prefix + "/")
                                        and pattern.match(p[len(prefix):]) for m, p in served))
        for methods, pattern in authz.CLIENT_GLOBAL_RULES:
            for method in methods:
                with self.subTest(method=method, pattern=pattern.pattern):
                    self.assertTrue(any(m == method and pattern.match(p) for m, p in served))


STATUS = {"state": "REGISTERED", "label": "Registered", "reason_code": "ok", "reason": "",
          "detail": {"pcscf": "10.0.0.1", "dns": ["10.0.0.2"], "pin": "verified"},
          "activity": {"current": "Checking", "retry_count": 0}, "frozen": False}


class EventScopeTests(unittest.TestCase):
    def test_a_client_hears_its_lines_conversations_and_calls_only(self):
        for event in ({"type": "sms", "instance": "sim1"}, {"type": "call", "instance": "sim1"},
                      {"type": "status", "instance": "sim1"}, {"type": "line", "instance": "sim1"}):
            with self.subTest(event=event):
                self.assertIsNotNone(authz.event_for(CLIENT, event))
        for event in ({"type": "host_alert"}, {"type": "cards"}, {"type": "hardware"},
                      {"type": "voicemail", "instance": "sim1"},
                      {"type": "engine", "instance": "sim1"}, {"type": "capability", "device": 1},
                      {"type": "sms", "instance": ""}, {"type": "something-added-later",
                                                        "instance": "sim1"}):
            with self.subTest(event=event):
                self.assertIsNone(authz.event_for(CLIENT, event))

    def test_the_administrator_hears_everything_and_nobody_else_anything(self):
        event = {"type": "status", "instance": "sim1", **STATUS}
        self.assertIs(authz.event_for(ADMIN, event), event)
        self.assertIsNone(authz.event_for(gate.ANONYMOUS, {"type": "sms", "instance": "sim1"}))

    def test_a_clients_status_event_carries_no_diagnostics(self):
        view = authz.event_for(CLIENT, {"type": "status", "instance": "sim1", **STATUS})
        self.assertEqual(view, {"type": "status", "instance": "sim1", "state": "REGISTERED",
                                "label": "Registered", "reason_code": "ok", "reason": ""})


class StatusViewTests(unittest.TestCase):
    def test_a_client_sees_whether_a_line_works_not_why(self):
        self.assertEqual(set(authz.status_for(CLIENT, STATUS)), set(authz.CLIENT_STATUS_FIELDS))
        self.assertIs(authz.status_for(ADMIN, STATUS), STATUS)


if __name__ == "__main__":
    unittest.main()
