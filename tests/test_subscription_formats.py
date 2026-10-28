"""Subscriptions come as Clash YAML or as a (base64) list of share links.

Seen on the device: a Sub Store link answered with base64 share links, which loaded as a YAML
string and broke the exit with "'str' object has no attribute 'get'".
"""
import base64
import json
import unittest

from host.mdd_orchestrator import parse_subscription

TROJAN = "trojan://pass@gb1.example.net:443?sni=gb1.example.net#GB%20London%2001"
SS = ("ss://" + base64.urlsafe_b64encode(b"aes-256-gcm:pw").decode().rstrip("=")
      + "@gb2.example.net:20450/?plugin=#%E8%8B%B1%E5%9B%BD%2002")
VMESS = "vmess://" + base64.b64encode(json.dumps({
    "ps": "United Kingdom 03", "add": "gb3.example.net", "port": "443", "id": "u",
    "net": "ws", "path": "/v", "tls": "tls"}).encode()).decode()
GRPC = "vless://u@gb4.example.net:443?type=grpc&security=tls#GB%2004"


class SubscriptionFormatTests(unittest.TestCase):
    def names(self, doc):
        return [node["name"] for node in doc["proxies"]]

    def test_base64_share_link_list(self):
        body = base64.b64encode("\n".join([TROJAN, SS, VMESS, GRPC]).encode()).decode()
        doc = parse_subscription(body + "\n")
        # The grpc node is not carried by this gateway and is skipped, not fatal.
        self.assertEqual(self.names(doc), ["GB London 01", "英国 02", "United Kingdom 03"])
        self.assertEqual([n["port"] for n in doc["proxies"]], [443, 20450, 443])

    def test_plain_share_link_list_and_wrapped_base64(self):
        self.assertEqual(self.names(parse_subscription(f"{TROJAN}\r\n\r\n{SS}\n")),
                         ["GB London 01", "英国 02"])
        raw = base64.b64encode(f"{TROJAN}\n{SS}".encode()).decode()
        wrapped = "\n".join(raw[i:i + 60] for i in range(0, len(raw), 60))
        self.assertEqual(len(parse_subscription(wrapped)["proxies"]), 2)

    def test_clash_yaml_still_works_and_drops_non_mappings(self):
        doc = parse_subscription("proxies:\n  - {name: GB A, type: trojan, server: a, port: 443,"
                                 " password: x}\n  - just a string\n")
        self.assertEqual(self.names(doc), ["GB A"])

    def test_unusable_bodies_are_a_readable_error(self):
        for body in ("", "hello world", "<html>login</html>", base64.b64encode(b"nothing").decode(),
                     base64.b64encode(GRPC.encode()).decode(), "proxies: not-a-list\n"):
            with self.subTest(body=body[:20]), self.assertRaisesRegex(
                    ValueError, "neither Clash YAML with proxies nor a list"):
                parse_subscription(body)


if __name__ == "__main__":
    unittest.main()
