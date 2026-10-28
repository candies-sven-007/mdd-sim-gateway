"""Share links are read with urlsplit for every protocol, and a bad one is a 400, not a 500.

Seen on the device (1.14.0-slim.5): testing a pasted ss:// node answered 500 with
`int('20450/')` deep in clash_outbound. The ss branch cut the port by hand and kept the "/"
of a `host:port/?query` link. Every shape below must parse (or fail with a readable
ValueError) for every protocol the gateway accepts.
"""
import asyncio
import base64
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from control.app import egress
from control.app.routers import egress as egress_routes
from host.mdd_orchestrator import clash_outbound, link_port, parse_manual_outbound, parse_share_link

HOST = "node.example.net"
PORT = 20450


def _b64(text: str, *, urlsafe=False, pad=True) -> str:
    raw = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(text.encode()).decode()
    return raw if pad else raw.rstrip("=")


# A standard-alphabet base64 credential that contains "/" — legal in older ss links, and
# a "/" is exactly what must not end the authority early.
SLASHED_PASSWORD = "?>?>?>"
SLASHED = _b64(f"aes-256-gcm:{SLASHED_PASSWORD}")
assert "/" in SLASHED

# The parts after host:port each link shape carries; "{q}" is the protocol's own query.
SHAPES = {
    "port, slash, query": "{hp}/?{q}#remark",
    "port, query": "{hp}?{q}#remark",
    "port, slash": "{hp}/#remark",
    "port, remark only": "{hp}#%E5%A4%87%E6%B3%A8",
    "bare port": "{hp}",
}

# scheme prefix (with userinfo) -> (query, expected node type)
PROTOCOLS = {
    "vless": ("vless://3f6e1c2a-0000-4000-8000-000000000001@", "type=tcp&security=tls&sni=a.example",
              "vless"),
    "trojan": ("trojan://secret-pass@", "type=tcp&sni=a.example", "trojan"),
    "hysteria2": ("hysteria2://user:pass@", "sni=a.example&obfs=salamander&obfs-password=x",
                  "hysteria2"),
    "hy2": ("hy2://user:pass@", "sni=a.example", "hysteria2"),
    "ss SIP002 base64url": (f"ss://{_b64('chacha20-ietf-poly1305:pw', urlsafe=True, pad=False)}@",
                            "plugin=", "ss"),
    "ss SIP002 base64 with slash": (f"ss://{SLASHED}@", "type=tcp", "ss"),
    "ss SIP002 plain 2022": ("ss://2022-blake3-aes-128-gcm:a%2Bb%3D@", "type=tcp", "ss"),
}


class ShareLinkShapeTests(unittest.TestCase):
    def test_every_protocol_reads_host_and_port_in_every_shape(self):
        for name, (prefix, query, kind) in PROTOCOLS.items():
            for shape, template in SHAPES.items():
                for host, expected_host in ((HOST, HOST), ("[2001:db8::7]", "2001:db8::7")):
                    link = prefix + template.format(hp=f"{host}:{PORT}", q=query)
                    with self.subTest(protocol=name, shape=shape, host=host):
                        node = parse_share_link(link)
                        self.assertEqual(node["type"], kind)
                        self.assertEqual(node["server"], expected_host)
                        self.assertEqual(node["port"], PORT)
                        self.assertIsInstance(node["port"], int)
                        # And the converter the device crashed in accepts it.
                        self.assertEqual(clash_outbound(node, "t")["server_port"], PORT)

    def test_shadowsocks_credentials_survive_every_shape(self):
        for shape, template in SHAPES.items():
            with self.subTest(shape=shape):
                node = parse_share_link(f"ss://{SLASHED}@" + template.format(
                    hp=f"{HOST}:{PORT}", q="plugin="))
                self.assertEqual((node["cipher"], node["password"]),
                                 ("aes-256-gcm", SLASHED_PASSWORD))
        plain = parse_share_link(f"ss://2022-blake3-aes-128-gcm:a%2Bb%3D@{HOST}:{PORT}/?x=1")
        self.assertEqual((plain["cipher"], plain["password"]),
                         ("2022-blake3-aes-128-gcm", "a+b="))

    def test_legacy_shadowsocks_whole_body_base64(self):
        for suffix in ("", "#remark", "/#remark", "/?plugin=#remark"):
            with self.subTest(suffix=suffix):
                body = _b64(f"aes-256-gcm:pw@{HOST}:{PORT}")
                node = parse_share_link(f"ss://{body}{suffix}")
                self.assertEqual((node["server"], node["port"], node["password"]),
                                 (HOST, PORT, "pw"))
        node = parse_share_link("ss://" + _b64(f"aes-256-gcm:pw@[2001:db8::7]:{PORT}"))
        self.assertEqual((node["server"], node["port"]), ("2001:db8::7", PORT))

    def test_the_device_link_shape(self):
        # The failing paste had the form xxx://...@host:20450/?type=...
        outbound = parse_manual_outbound(
            f"ss://{_b64('aes-256-gcm:pw')}@{HOST}:20450/?type=tcp#node", "test-out")
        self.assertEqual((outbound["type"], outbound["server_port"]), ("shadowsocks", 20450))

    def test_vmess_port_is_checked_too(self):
        def link(**payload):
            return "vmess://" + _b64(json.dumps({"add": HOST, "id": "u", **payload}))
        self.assertEqual(parse_share_link(link(port="443"))["port"], 443)
        self.assertEqual(parse_share_link(link(port=443))["port"], 443)
        for bad in ("443/", "", None, "0", "70000"):
            with self.subTest(port=bad), self.assertRaisesRegex(ValueError, "not a valid port"):
                parse_share_link(link(port=bad))
        with self.assertRaisesRegex(ValueError, "missing a host"):
            parse_share_link("vmess://" + _b64(json.dumps({"port": 443, "id": "u"})))
        with self.assertRaisesRegex(ValueError, "base64-encoded JSON"):
            parse_share_link("vmess://not-base64-json")


class InvalidLinkTests(unittest.TestCase):
    def test_bad_ports_and_missing_parts_are_readable_errors(self):
        cases = {
            f"trojan://p@{HOST}:20450x/?sni=a": "not a valid port",
            f"trojan://p@{HOST}:70000": "not a valid port",
            f"trojan://p@{HOST}:0": "not a valid port",
            f"trojan://p@{HOST}/?sni=a": "missing a port",
            "trojan://p@:443": "missing a host",
            f"vless://u@{HOST}:-1": "not a valid port",
            f"ss://{_b64('aes-256-gcm:pw')}@{HOST}:20450x/": "not a valid port",
            f"ss://{_b64('aes-256-gcm:pw')}@{HOST}": "missing a port",
            f"ss://{_b64('no-colon')}@{HOST}:{PORT}": "method:password",
            "ss://!!!": "neither SIP002 nor base64",
            f"hy2://p@[2001:db8::7]:99999": "not a valid port",
        }
        for link, message in cases.items():
            with self.subTest(link=link), self.assertRaisesRegex(ValueError, message):
                parse_manual_outbound(link, "t")

    def test_link_port(self):
        self.assertEqual(link_port("443"), 443)
        self.assertEqual(link_port(65535), 65535)
        for bad in ("20450/", "", None, "0", "65536", "4 43", "-1", "1e3"):
            with self.subTest(port=bad), self.assertRaisesRegex(ValueError, "not a valid port"):
                link_port(bad)

    def test_a_subscription_node_with_a_bad_port_is_a_value_error(self):
        with self.assertRaisesRegex(ValueError, "not a valid port"):
            clash_outbound({"type": "trojan", "server": HOST, "port": "443/"}, "t")
        with self.assertRaisesRegex(ValueError, "lacks a server"):
            clash_outbound({"type": "trojan", "port": 443}, "t")


class ProfileTestRouteTests(unittest.TestCase):
    """POST /api/egress/profile/{id}/test answers an unreadable link with 400 and a message."""

    def call(self, profile):
        with patch.object(egress.shutil, "which", return_value="/usr/bin/true"):
            return asyncio.run(egress_routes.api_egress_profile_test("p1", profile))

    def test_unreadable_links_are_400_with_a_message(self):
        for value in (f"ss://{_b64('aes-256-gcm:pw')}@{HOST}:20450x/?type=tcp",
                      f"trojan://p@{HOST}/?sni=a",
                      f"vless://u@{HOST}:99999?security=reality&pbk=k",
                      "vmess://not-base64-json",
                      "ftp://example.net:21"):
            with self.subTest(value=value), self.assertRaises(HTTPException) as caught:
                self.call({"type": "node", "value": value})
            self.assertEqual(caught.exception.status_code, 400)
            self.assertIn("node link could not be read", caught.exception.detail["message"])
            self.assertIn("parsed", caught.exception.detail)

    def test_empty_link_and_bad_socks_port_are_400(self):
        for profile in ({"type": "node", "value": " "},
                        {"type": "socks5", "server": HOST, "port": "10x"},
                        {"type": "socks5", "server": HOST, "port": 70000}):
            with self.subTest(profile=profile), self.assertRaises(HTTPException) as caught:
                self.call(profile)
            self.assertEqual(caught.exception.status_code, 400)

    def test_a_missing_sing_box_is_still_503(self):
        # Not the operator's input: the gateway itself lacks the tool.
        with patch.object(egress.shutil, "which", return_value=None), \
                self.assertRaises(HTTPException) as caught:
            asyncio.run(egress_routes.api_egress_profile_test(
                "p1", {"type": "node", "value": f"trojan://p@{HOST}:{PORT}"}))
        self.assertEqual(caught.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
