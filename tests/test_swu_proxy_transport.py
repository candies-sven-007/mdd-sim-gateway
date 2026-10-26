import ast
from pathlib import Path
import socket
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "engine" / "swu_ike.py"


def proxy_methods():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    swu = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "swu")
    wanted = {"create_socket", "create_socket_nat", "create_socket_esp",
              "_inner_mtu_from"}
    methods = [node for node in swu.body
               if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "socket": socket,
        "UDP": 17,
        "ESP_PROTOCOL": 50,
        "SWU_MTU_MARGIN": 0,
        "proxy_udp_socket": Mock(),
    }
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class FakeSocket:
    def __init__(self):
        self.binds = []
        self.timeouts = []

    def bind(self, address):
        self.binds.append(address)

    def settimeout(self, value):
        self.timeouts.append(value)


class SwuProxyTransportTests(unittest.TestCase):
    def app(self):
        ns = proxy_methods()
        app = type("Harness", (), {})()
        for name in ("create_socket", "create_socket_nat", "create_socket_esp",
                     "_inner_mtu_from"):
            setattr(app, name, ns[name].__get__(app))
        app.socket_type = ns["UDP"]
        app.timeout = 5
        app.egress_proxy = "socks5://mdd-egress:22157"
        app.server_address = ("198.51.100.10", 500)
        app.server_address_nat = ("198.51.100.10", 4500)
        app.proxy_udp_overhead = 10
        app._enable_outer_pmtud = Mock()
        app._esp_overhead = Mock(return_value=81)
        return ns, app

    def test_ike_and_natt_use_separate_fail_closed_proxy_associations(self):
        ns, app = self.app()
        ike, natt = FakeSocket(), FakeSocket()
        ns["proxy_udp_socket"].side_effect = [ike, natt]
        app.create_socket(("0.0.0.0", 500))
        app.create_socket_nat(("0.0.0.0", 4500))
        self.assertIs(app.socket, ike)
        self.assertIs(app.socket_nat, natt)
        self.assertEqual(ns["proxy_udp_socket"].call_args_list[0].args[1],
                         ("198.51.100.10", 500))
        self.assertEqual(ns["proxy_udp_socket"].call_args_list[1].args[1],
                         ("198.51.100.10", 4500))
        self.assertEqual(ike.binds + natt.binds, [])

    def test_proxy_mode_never_opens_a_raw_esp_socket(self):
        ns, app = self.app()
        dummy = FakeSocket()
        factory = Mock(return_value=dummy)
        with patch.object(socket, "socket", factory):
            app.create_socket_esp(("192.0.2.2", 0))
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        self.assertEqual(dummy.binds, [("127.0.0.1", 0)])

    def test_direct_mode_keeps_raw_esp_transport(self):
        _ns, app = self.app()
        app.egress_proxy = ""
        raw = FakeSocket()
        factory = Mock(return_value=raw)
        with patch.object(socket, "socket", factory):
            app.create_socket_esp(("192.0.2.2", 0))
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_RAW, 50)
        self.assertEqual(raw.binds, [("192.0.2.2", 0)])

    def test_proxy_header_is_included_in_fragmentation_threshold(self):
        _ns, app = self.app()
        self.assertEqual(app._inner_mtu_from(1500), 1409)


    def test_proxy_forces_natt_after_ike_authentication(self):
        text = SOURCE.read_text(encoding="utf-8")
        keying = text.index("self.generate_keying_material()")
        forced_natt = text.index("self.userplane_mode = NAT_TRAVERSAL", keying)
        state_return = text.index("return OK,''", keying)
        self.assertLess(forced_natt, state_return)


if __name__ == "__main__":
    unittest.main()
