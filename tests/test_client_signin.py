"""A client app signs in, works, is audited by name, and is thrown off -- through the real app."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from control.app import auth, clients, main, store

PASSWORD = "correct horse battery"
HOST = "gateway.example.net"


class ClientSignInTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = lambda name: os.path.join(self.temp.name, name)  # noqa: E731
        for p in (
            patch.object(auth, "AUTH_PATH", path("auth.json")),
            patch.object(auth, "SESSIONS_PATH", path("sessions.json")),
            patch.object(clients, "CLIENTS_PATH", path("clients.json")),
            patch.object(main.cfg, "DATA_DIR", self.temp.name),
            patch.multiple(store, DATA_DIR=self.temp.name, DB_PATH=path("history.sqlite")),
            patch.object(main.cfg, "get_settings", return_value={}),
            patch.object(main.cfg, "list_instances", return_value=[
                {"id": "sim1", "name": "Work", "msisdn": "+61400000000", "enabled": True,
                 "pin": "1234", "sip": {"webrtc": {"password": "secret"}}}]),
        ):
            p.start()
            self.addCleanup(p.stop)
        store.init()
        auth._sessions.clear()
        auth._failures.clear()
        clients._load()
        self.addCleanup(clients._load)
        auth.setup(PASSWORD)
        self.cookie, self.csrf = auth.login("admin", PASSWORD, "192.0.2.1")

    def call(self, method, path, body=None, *, token=None, browser=False):
        headers = [(b"host", HOST.encode()), (b"content-type", b"application/json")]
        if token:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        if browser:
            headers.append((b"cookie", f"{auth.SESSION_COOKIE}={self.cookie}".encode()))
            headers.append((b"x-mdd-csrf-token", self.csrf.encode()))
        scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
                 "query_string": b"", "headers": headers, "client": ("192.0.2.1", 1234),
                 "server": (HOST, 443), "scheme": "https", "http_version": "1.1",
                 "root_path": ""}
        payload = json.dumps(body).encode() if body is not None else b""
        messages = [{"type": "http.request", "body": payload, "more_body": False}]
        sent = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        asyncio.run(main.app(scope, receive, send))
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, json.loads(body or b"null")

    def sign_in(self, **extra):
        return self.call("POST", "/api/auth/client/login",
                         {"username": "admin", "password": PASSWORD, "name": "Phone",
                          "platform": "ios", "app_version": "1.0", **extra})

    def audit(self):
        with open(os.path.join(self.temp.name, "audit", "operations.jsonl"),
                  encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def test_sign_in_needs_the_administrator_password(self):
        status, _ = self.call("POST", "/api/auth/client/login",
                              {"username": "admin", "password": "wrong password"})
        self.assertEqual(status, 401)
        status, body = self.sign_in()
        self.assertEqual(status, 200)
        self.assertTrue(body["token"].startswith(clients.TOKEN_PREFIX))
        self.assertEqual(body["client"]["name"], "Phone")

    def test_status_tells_a_client_it_is_signed_in_and_what_the_gateway_offers(self):
        token = self.sign_in()[1]["token"]
        status, body = self.call("GET", "/api/auth/status", token=token)
        self.assertEqual(status, 200)
        self.assertEqual((body["authenticated"], body["kind"]), (True, "client"))
        self.assertEqual(body["csrf"], "")
        self.assertIn("client_tokens", body["features"])

    def test_a_client_sees_a_cut_of_each_line(self):
        token = self.sign_in()[1]["token"]
        with patch.object(main, "_cached_line_status", return_value={
                "state": "REGISTERED", "label": "Registered", "reason_code": "ok",
                "reason": "", "detail": {"internal": "x"}}):
            status, body = self.call("GET", "/api/instances", token=token)
        self.assertEqual(status, 200)
        (line,) = body["instances"]
        self.assertEqual(set(line), {"id", "name", "msisdn", "enabled", "status"})
        self.assertNotIn("detail", line["status"])


    def test_a_client_is_told_whether_a_line_works_not_why(self):
        token = self.sign_in()[1]["token"]
        full = {"state": "REGISTERED", "label": "Registered", "reason_code": "ok", "reason": "",
                "detail": {"pcscf": "10.0.0.1"}}
        with patch.object(main.cfg, "get_instance", return_value={"id": "sim1"}), \
                patch.object(main, "_cached_line_status", return_value=full):
            _, mine = self.call("GET", "/api/instances/sim1/status", token=token)
            _, admins = self.call("GET", "/api/instances/sim1/status", browser=True)
        self.assertNotIn("detail", mine)
        self.assertEqual(admins["detail"], {"pcscf": "10.0.0.1"})

    def test_a_client_cannot_read_filed_binary_messages(self):
        token = self.sign_in()[1]["token"]
        self.assertEqual(self.call("GET", "/api/instances/sim1/messages/binary", token=token)[0],
                         403)

    def test_resetting_the_administrator_ends_every_sign_in(self):
        token = self.sign_in()[1]["token"]
        # `install.sh reset-admin` moves auth.json away and restarts nothing.
        os.rename(auth.AUTH_PATH, auth.AUTH_PATH + ".reset")
        self.assertEqual(self.call("GET", "/api/instances/sim1/status", token=token)[0], 401)
        self.assertEqual(self.call("GET", "/api/auth/clients", browser=True)[0], 401)
        status, _ = self.call("POST", "/api/auth/setup",
                              {"username": "admin", "password": "a brand new password"})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/auth/status", token=token)[0], 401)
        self.assertEqual(clients.list_clients(), [])
        self.assertIsNone(auth.session(self.cookie))

    def test_the_administrator_lists_and_revokes_clients(self):
        token = self.sign_in()[1]["token"]
        status, body = self.call("GET", "/api/auth/clients", browser=True)
        (listed,) = body["clients"]
        self.assertEqual(self.call("GET", "/api/auth/clients", token=token)[0], 403)
        status, _ = self.call("DELETE", f"/api/auth/clients/{listed['id']}", browser=True)
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/instances", token=token)[0], 401)
        self.assertEqual(self.call("DELETE", f"/api/auth/clients/{listed['id']}",
                                   browser=True)[0], 404)

    def test_a_client_signs_itself_out_and_the_audit_names_it(self):
        body = self.sign_in()[1]
        status, _ = self.call("POST", "/api/auth/client/logout", {}, token=body["token"])
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/auth/status", token=body["token"])[0], 401)
        actors = {(record["path"], record["actor"]) for record in self.audit()}
        self.assertIn(("/api/auth/client/logout", f"client:{body['client']['id']}"), actors)
        self.assertIn(("/api/auth/client/login", "anonymous"), actors)

    def test_changing_the_password_signs_every_client_out(self):
        token = self.sign_in()[1]["token"]
        status, _ = self.call("POST", "/api/auth/password",
                              {"current_password": PASSWORD,
                               "new_password": "another long password"}, browser=True)
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/instances", token=token)[0], 401)
        self.assertEqual(clients.list_clients(), [])
        self.assertIn(("/api/auth/password", "admin"),
                      {(record["path"], record["actor"]) for record in self.audit()})


if __name__ == "__main__":
    unittest.main()
