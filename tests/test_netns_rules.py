"""host/mdd_netns.py as text: names, the route decision, the nft ruleset and reconcile order.

The privileged counterpart, which loads these rules into real namespaces, is test_netns_leak.py.
"""
import unittest
from types import SimpleNamespace

from host import mdd_netns as m

READY_US = {"us": {"ready": True, "mode": "subscription", "interface": "mdd-us"}}


def doc(iid="1", country="us", rtp=(10000, 10011), enabled=True):
    return {"id": iid, "country": country, "rtp": list(rtp) if rtp else None,
            "enabled": enabled}


def plan(n, route, interface="", rtp=(10000, 10011)):
    return m.LinePlan(m.line(n), m.RouteState(route, interface), rtp)


def rules_for(*plans, local4=("192.168.1.50",), lan4=("192.168.1.0/24",)):
    return m.ruleset(list(plans), control_port=8443, local4=list(local4), lan4=list(lan4))


class LineTests(unittest.TestCase):
    def test_names_and_numbers(self):
        ln = m.line("2")
        self.assertEqual((ln.netns, ln.veth, ln.host_addr, ln.ns_addr),
                         ("mdd-2", "mddv2", "10.88.2.1", "10.88.2.2"))
        self.assertEqual((ln.table, ln.pref_table, ln.pref_fwmark, ln.pref_direct),
                         (1002, 1002, 902, 952))
        self.assertEqual(ln.mark_hex, "0x6d640002")

    def test_only_one_to_ten(self):
        for good in ("1", "9", "10", 3):
            m.line(good)
        for bad in ("0", "11", "01", "1;id", "../1", " 1", "", "1.0", None, "x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                m.line(bad)

    def test_veth_never_matches_the_tun_wildcard(self):
        for n in range(1, m.MAX_LINE + 1):
            self.assertFalse(m.line(n).veth.startswith(m.TUN_PREFIX))


class UpDownTests(unittest.TestCase):
    def test_the_routing_gate_comes_before_the_namespace(self):
        commands = m.up_commands(m.line(1))
        gate = commands.index(["ip", "route", "replace", "unreachable", "default",
                               "metric", "4000", "table", "1001"])
        rule = commands.index(["ip", "rule", "add", "pref", "1001", "iif", "mddv1",
                               "lookup", "1001"])
        create = commands.index(["ip", "netns", "add", "mdd-1"])
        self.assertLess(gate, create)
        self.assertLess(rule, create)
        self.assertIn(["ip", "rule", "add", "pref", "901", "iif", "mddv1", "fwmark",
                       "0x6d640001", "lookup", "main"], commands)

    def test_netns_sysctls_match_the_old_container_and_keep_ipv6_for_the_tunnel(self):
        command = m.up_commands(m.line(1))[-1]
        self.assertEqual(command[:6], ["ip", "netns", "exec", "mdd-1", "sysctl", "-qw"])
        for key in ("all.accept_ra=0", "default.accept_ra=0", "all.autoconf=0",
                    "default.autoconf=0", "all.use_tempaddr=0", "default.use_tempaddr=0",
                    "all.disable_ipv6=0", "eth0.disable_ipv6=1"):
            self.assertIn(f"net.ipv6.conf.{key}", command)

    def test_no_resolver_and_no_ipv6_address_is_configured(self):
        text = " ".join(" ".join(c) for c in m.up_commands(m.line(1)))
        self.assertNotIn("resolv", text)
        self.assertNotIn("-6 addr", text)

    def test_up_starts_from_scratch_and_stops_at_the_first_failure(self):
        calls = []

        class Runner:
            def run(self, command, input=None):
                calls.append(command)
                failed = command[:3] == ["ip", "netns", "add"]
                return SimpleNamespace(returncode=1 if failed else 0, stdout="", stderr="no")

        self.assertEqual(m.up("1", Runner()), 1)
        self.assertEqual(calls[:len(m.down_commands(m.line(1)))], m.down_commands(m.line(1)))
        self.assertEqual(calls[-1], ["ip", "netns", "add", "mdd-1"])

    def test_up_tolerates_a_kernel_without_ipv6(self):
        calls = []

        class Runner:
            def run(self, command, input=None):
                calls.append(command)
                ipv6 = any(arg.startswith("net.ipv6.") for arg in command)
                return SimpleNamespace(returncode=1 if ipv6 else 0, stdout="", stderr="")

        self.assertEqual(m.up("1", Runner()), 0)
        self.assertEqual(calls[-1][:4], ["ip", "netns", "exec", "mdd-1"])

    def test_rp_filter_failure_is_not_tolerated(self):
        class Runner:
            def run(self, command, input=None):
                return SimpleNamespace(returncode=1 if "rp_filter" in " ".join(command) else 0,
                                       stdout="", stderr="")

        self.assertEqual(m.up("1", Runner()), 1)

    def test_main_refuses_bad_arguments(self):
        for argv in ([], ["up"], ["up", "0"], ["up", "11"], ["sideways", "1"],
                     ["up", "1", "2"]):
            with self.subTest(argv=argv):
                self.assertEqual(m.main(argv), 64)


class RouteStateTests(unittest.TestCase):
    def state(self, line_doc=None, proxy=True, exits=READY_US, interfaces=("mdd-us",)):
        return m.line_route_state(line_doc or doc(), proxy_enabled=proxy, exits=exits,
                                  interfaces=set(interfaces))

    def test_truth_table(self):
        self.assertEqual(self.state(), m.RouteState(m.TUN, "mdd-us"))
        self.assertEqual(self.state(proxy=False).route, m.DIRECT)
        self.assertEqual(self.state(exits={"us": {"ready": True, "mode": "direct"}}).route,
                         m.DIRECT)
        blocked = [
            self.state(doc(enabled=False)),
            self.state(doc(country="")),
            self.state(exits={}),
            self.state(exits={"us": {"ready": False, "error": "sing-box exited"}}),
            self.state(exits={"us": {"ready": False}}),
            self.state(interfaces=()),
            self.state(exits={"us": {"ready": True, "mode": "manual", "interface": "eth0"}},
                       interfaces=("eth0",)),
            self.state(doc(country="gb")),
        ]
        for state in blocked:
            with self.subTest(state=state):
                self.assertEqual(state.route, m.BLOCKED)
                self.assertTrue(state.reason)

    def test_disabled_line_is_blocked_even_with_proxy_off(self):
        self.assertEqual(self.state(doc(enabled=False), proxy=False).route, m.BLOCKED)

    def test_the_exit_error_is_the_reason(self):
        state = self.state(exits={"us": {"ready": False, "error": "no UDP-capable node"}})
        self.assertEqual(state.reason, "no UDP-capable node")


class RtpRangeTests(unittest.TestCase):
    def test_valid_ranges_pass(self):
        self.assertEqual(m.rtp_ranges([doc("1"), doc("2", rtp=(10100, 10111))], 8443),
                         {"1": (10000, 10011), "2": (10100, 10111)})

    def test_refusals(self):
        cases = {
            (None,): "RTP range missing",
            ((1000, 1010),): "RTP range invalid",
            ((20000, 19990),): "RTP range invalid",
            ((10000, 10060),): "RTP range invalid",
            ((8440, 8450),): "RTP range includes the control port",
            (("10000", "10011"),): "RTP range missing",
            ((True, 10011),): "RTP range missing",
        }
        for (rtp,), reason in cases.items():
            with self.subTest(rtp=rtp):
                line_doc = doc()
                line_doc["rtp"] = list(rtp) if isinstance(rtp, tuple) else rtp
                self.assertEqual(m.rtp_ranges([line_doc], 8443)["1"], reason)

    def test_overlap_goes_to_the_higher_line(self):
        ranges = m.rtp_ranges([doc("2", rtp=(10005, 10015)), doc("1")], 8443)
        self.assertEqual(ranges["1"], (10000, 10011))
        self.assertEqual(ranges["2"], "RTP range overlaps another line")

    def test_invalid_ids_are_ignored(self):
        self.assertEqual(m.rtp_ranges([doc("11"), doc("abc")], 8443), {})


class AddressViewTests(unittest.TestCase):
    ADDR = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
            "2: eth0    inet 192.168.1.50/24 brd 192.168.1.255 scope global eth0\\    x\n"
            "3: wwan0    inet 100.70.1.2/30 scope global wwan0\\    x\n"
            "4: mdd-us    inet 172.29.20.1/30 scope global mdd-us\\    x\n"
            "5: mddv1    inet 10.88.1.1/30 scope global mddv1\\    x\n")
    ROUTES = ("192.168.1.0/24 dev eth0 proto kernel scope link src 192.168.1.50\n"
              "192.168.0.0/16 dev wlan0 proto kernel scope link src 192.168.9.1\n"
              "100.70.1.0/30 dev wwan0 proto kernel scope link src 100.70.1.2\n"
              "10.88.1.0/30 dev mddv1 proto kernel scope link src 10.88.1.1\n"
              "172.29.20.0/30 dev mdd-us proto kernel scope link src 172.29.20.1\n"
              "10.0.0.0/8 dev tun9 scope link\n")

    def test_local4_skips_loopback_and_our_interfaces(self):
        self.assertEqual(m.parse_local4(self.ADDR), ["192.168.1.50", "100.70.1.2"])

    def test_lan4_is_private_directly_attached_collapsed_and_not_ours(self):
        # 10.0.0.0/8 overlaps the line subnet and is left out; CGNAT is not private here.
        self.assertEqual(m.parse_lan4(self.ROUTES), ["192.168.0.0/16"])

    def test_subnet_conflict(self):
        self.assertEqual(m.subnet_conflict(self.ADDR), "")
        clash = self.ADDR + "6: eth1    inet 10.88.0.5/24 scope global eth1\\  x\n"
        self.assertIn("eth1", m.subnet_conflict(clash))

    def test_interfaces(self):
        text = ("1: lo: <LOOPBACK,UP> mtu 65536\n"
                "7: mddv1@if2: <BROADCAST> mtu 1500\n"
                "9: mdd-us: <POINTOPOINT> mtu 1400\n")
        self.assertEqual(m.parse_interfaces(text), {"lo", "mddv1", "mdd-us"})

    def test_table_view(self):
        view = m.parse_table("default dev mdd-us scope link metric 100\n"
                             "unreachable default metric 4000\n"
                             "throw 192.168.1.0/24\n")
        self.assertEqual(view, m.TableView("mdd-us", True, frozenset({"192.168.1.0/24"})))
        self.assertEqual(m.parse_table(""), m.TableView())


class RulesetTests(unittest.TestCase):
    def line_rules(self, text, n):
        return [row.strip() for row in text.splitlines()
                if f"mddv{n}\"" in row or f"10.88.{n}.2" in row]

    def test_blocked_line_has_no_way_out(self):
        text = rules_for(plan(1, m.BLOCKED))
        for rule in self.line_rules(text, 1):
            if "accept" in rule:
                self.assertTrue("meta mark" in rule or "ct mark" in rule
                                or "established" in rule, rule)
            if "masquerade" in rule:
                self.assertIn("ip daddr @lan4", rule)    # only browser media, on the LAN
        self.assertNotIn('oifname "mdd-', text.split("chain forward", 1)[1].split("}", 1)[0])
        self.assertNotRegex(text, r'iifname "mddv1" accept')

    def test_tun_line_may_reach_only_its_tun(self):
        text = rules_for(plan(1, m.TUN, "mdd-us"))
        self.assertIn('iifname "mddv1" oifname "mdd-us" accept', text)
        self.assertIn('ip saddr 10.88.1.2 oifname "mdd-us" masquerade', text)
        self.assertNotRegex(text, r'iifname "mddv1" accept')
        self.assertNotIn('oifname != "mddv1" masquerade', text)

    def test_direct_line(self):
        text = rules_for(plan(1, m.DIRECT))
        self.assertIn('iifname "mddv1" accept', text)
        self.assertIn('ip saddr 10.88.1.2 oifname != "mddv1" masquerade', text)

    def test_catch_all_drops_come_last(self):
        forward = rules_for(plan(1, m.TUN, "mdd-us"), plan(2, m.DIRECT)) \
            .split("chain forward", 1)[1].split("}", 1)[0].strip().splitlines()
        self.assertEqual([r.strip() for r in forward[-2:]],
                         ['iifname "mddv*" counter drop', 'oifname "mddv*" counter drop'])
        empty = rules_for().split("chain forward", 1)[1].split("}", 1)[0]
        self.assertIn('iifname "mddv*" counter drop', empty)

    def test_lines_do_not_mention_each_other(self):
        text = rules_for(plan(1, m.TUN, "mdd-us"), plan(2, m.DIRECT, rtp=(10100, 10111)))
        for row in text.splitlines():
            if "mddv1\"" in row or "10.88.1." in row or "0x6d640001" in row:
                self.assertNotIn("mddv2\"", row)
                self.assertNotIn("10.88.2.", row)
                self.assertNotIn("0x6d640002", row)

    def test_dnat_only_to_the_host_and_the_lines_own_range(self):
        text = rules_for(plan(1, m.TUN, "mdd-us"))
        self.assertIn('iifname != "mddv*" iifname != "mdd-*" ip daddr @local4 '
                      'udp dport 10000-10011 ct mark set 0x6d640001 dnat to 10.88.1.2', text)
        self.assertIn("ip daddr @lan4 udp sport 10000-10011 meta mark set 0x6d640001", text)
        self.assertIn("ip saddr 10.88.1.2 ip daddr @lan4 udp sport 10000-10011 masquerade",
                      text)

    def test_no_rtp_rules_without_a_valid_range(self):
        text = rules_for(plan(1, m.TUN, "mdd-us", rtp=None))
        self.assertNotIn("dnat", text)
        self.assertNotIn("@lan4 udp", text)

    def test_input_admits_only_the_control_port(self):
        chain = rules_for().split("chain input", 1)[1].split("}", 1)[0]
        self.assertIn('iifname "mddv*" tcp dport 8443 accept', chain)
        self.assertTrue(chain.strip().endswith('iifname "mddv*" counter drop'))

    def test_empty_sets_have_no_elements_clause(self):
        text = rules_for(local4=(), lan4=())
        self.assertIn("set local4 { type ipv4_addr; }", text)
        self.assertIn("set lan4 { type ipv4_addr; flags interval; }", text)

    def test_tables_are_replaced_in_one_transaction(self):
        text = rules_for()
        self.assertTrue(text.startswith("table inet mdd\ndelete table inet mdd\n"))
        self.assertIn("table ip mdd_nat\ndelete table ip mdd_nat\n", text)

    def test_only_nftables_0_9_8_syntax(self):
        text = rules_for(plan(1, m.TUN, "mdd-us"), plan(2, m.DIRECT, rtp=(10100, 10111)),
                         plan(3, m.BLOCKED, rtp=None))
        for word in ("fib", "typeof", "flowtable", "vmap", "meta time", "socket"):
            self.assertNotIn(word, text)
        self.assertNotRegex(text, r"priority (raw|mangle|dstnat|filter|security|srcnat)")
        # NAT only in the ip family table.
        inet = text.split("table ip mdd_nat", 1)[0]
        self.assertNotIn("type nat", inet)
        self.assertNotRegex(inet, r"\b(dnat|snat|masquerade)\b")


class Recorder:
    """A fake host: answers the queries reconcile makes and records what it runs."""

    def __init__(self, tables=None, prefs=(901, 1001), links=("mddv1", "mdd-us"),
                 nft_ok=True):
        self.tables = tables or {1001: "unreachable default metric 4000\n"}
        self.prefs = prefs
        self.links = links
        self.nft_ok = nft_ok
        self.calls = []

    def run(self, command, input=None):
        self.calls.append(command)
        out = ""
        if command == ["ip", "-o", "link", "show"]:
            out = "".join(f"{i}: {name}: <UP>\n" for i, name in enumerate(self.links, 2))
        elif command[:4] == ["ip", "-4", "-o", "addr"]:
            out = "2: eth0    inet 192.168.1.50/24 scope global eth0\\  x\n"
        elif command[:4] == ["ip", "-4", "route", "show"]:
            out = "192.168.1.0/24 dev eth0 proto kernel scope link src 192.168.1.50\n"
        elif command == ["ip", "rule", "show"]:
            out = "".join(f"{p}:\tfrom all lookup x\n" for p in self.prefs)
        elif command[:4] == ["ip", "route", "show", "table"]:
            out = self.tables.get(int(command[4]), "")
        elif command[:2] == ["nft", "-f"]:
            return SimpleNamespace(returncode=0 if self.nft_ok else 1, stdout="",
                                   stderr="" if self.nft_ok else "Error: no such file")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def position(self, predicate):
        return next(i for i, c in enumerate(self.calls) if predicate(c))


class ReconcileTests(unittest.TestCase):
    def reconcile(self, runner, lines=None, exits=READY_US, proxy=True):
        return m.reconcile(runner, lines or [doc()], proxy_enabled=proxy, exits=exits,
                           control_port=8443)

    def test_exit_lost_withdraws_the_route_before_nft(self):
        runner = Recorder(tables={1001: "default dev mdd-us metric 100\n"
                                        "unreachable default metric 4000\n"})
        status = self.reconcile(runner, exits={"us": {"ready": False, "error": "down"}})
        self.assertEqual(status["1"]["route"], m.BLOCKED)
        withdraw = runner.position(lambda c: c[:4] == ["ip", "route", "del", "default"])
        nft = runner.position(lambda c: c[:2] == ["nft", "-f"])
        self.assertLess(withdraw, nft)
        self.assertFalse(any(c[:4] == ["ip", "route", "replace", "default"]
                             for c in runner.calls))

    def test_exit_ready_adds_the_route_after_nft(self):
        runner = Recorder()
        status = self.reconcile(runner)
        self.assertEqual(status["1"], {"route": m.TUN, "interface": "mdd-us", "netns": "mdd-1",
                                       "netns_present": True, "reason": ""})
        nft = runner.position(lambda c: c[:2] == ["nft", "-f"])
        add = runner.position(lambda c: c[:4] == ["ip", "route", "replace", "default"])
        self.assertLess(nft, add)
        self.assertIn(["sysctl", "-qw", "net.ipv4.conf.mdd-us.rp_filter=2"], runner.calls)

    def test_country_change_passes_through_unreachable(self):
        runner = Recorder(links=("mddv1", "mdd-us", "mdd-gb"),
                          tables={1001: "default dev mdd-us metric 100\n"
                                        "unreachable default metric 4000\n"})
        exits = {"gb": {"ready": True, "mode": "subscription", "interface": "mdd-gb"},
                 **READY_US}
        self.reconcile(runner, lines=[doc(country="gb")], exits=exits)
        withdraw = runner.position(lambda c: c[:6] == ["ip", "route", "del", "default", "dev",
                                                       "mdd-us"])
        nft = runner.position(lambda c: c[:2] == ["nft", "-f"])
        add = runner.position(lambda c: c[:6] == ["ip", "route", "replace", "default", "dev",
                                                  "mdd-gb"])
        self.assertLess(withdraw, nft)
        self.assertLess(nft, add)

    def test_nft_failure_widens_nothing(self):
        runner = Recorder(nft_ok=False)
        status = self.reconcile(runner)
        self.assertEqual(status["1"]["route"], m.BLOCKED)
        self.assertIn("nftables rules not loaded", status["1"]["reason"])
        self.assertFalse(any(c[:3] == ["ip", "route", "replace"] and "unreachable" not in c
                             for c in runner.calls))

    def test_missing_gate_is_restored_before_anything_else(self):
        runner = Recorder(tables={1001: ""}, prefs=())
        self.reconcile(runner)
        restore = runner.position(lambda c: c[:5] == ["ip", "route", "replace", "unreachable",
                                                      "default"])
        nft = runner.position(lambda c: c[:2] == ["nft", "-f"])
        self.assertLess(restore, nft)
        self.assertIn(["ip", "rule", "add", "pref", "1001", "iif", "mddv1", "lookup", "1001"],
                      runner.calls)

    def test_direct_line_gets_the_main_table_rule(self):
        runner = Recorder()
        self.reconcile(runner, proxy=False)
        self.assertIn(["ip", "rule", "add", "pref", "951", "iif", "mddv1", "lookup", "main"],
                      runner.calls)

    def test_direct_rule_removed_when_no_longer_direct(self):
        runner = Recorder(prefs=(901, 951, 1001))
        self.reconcile(runner, exits={})
        self.assertIn(["ip", "rule", "del", "pref", "951"], runner.calls)

    def test_throw_routes_follow_the_lan(self):
        runner = Recorder(tables={1001: "unreachable default metric 4000\n"
                                        "throw 10.20.0.0/16\n"})
        self.reconcile(runner)
        self.assertIn(["ip", "route", "del", "throw", "10.20.0.0/16", "table", "1001"],
                      runner.calls)
        self.assertIn(["ip", "route", "replace", "throw", "192.168.1.0/24", "table", "1001"],
                      runner.calls)

    def test_line_without_netns_gets_rules_but_no_routes(self):
        runner = Recorder(links=("mdd-us",))
        status = self.reconcile(runner)
        self.assertFalse(status["1"]["netns_present"])
        self.assertFalse(any(c[:2] == ["ip", "route"] and c[2] != "show" for c in runner.calls))
        nft_input = next(c for c in runner.calls if c[:2] == ["nft", "-f"])
        self.assertTrue(nft_input)

    def test_subnet_conflict_blocks_every_line(self):
        class Clash(Recorder):
            def run(self, command, input=None):
                if command[:4] == ["ip", "-4", "-o", "addr"]:
                    self.calls.append(command)
                    return SimpleNamespace(returncode=0, stderr="",
                                           stdout="2: eth0    inet 10.88.0.5/16 scope global eth0\n")
                return super().run(command, input)

        status = self.reconcile(Clash())
        self.assertEqual(status["1"]["route"], m.BLOCKED)
        self.assertIn("overlaps", status["1"]["reason"])

    def test_invalid_rtp_blocks_the_line(self):
        status = self.reconcile(Recorder(), lines=[doc(rtp=(8440, 8450))])
        self.assertEqual(status["1"]["route"], m.BLOCKED)
        self.assertIn("control port", status["1"]["reason"])

    def test_ids_outside_one_to_ten_are_ignored(self):
        status = self.reconcile(Recorder(), lines=[doc("11"), doc("1")])
        self.assertEqual(set(status), {"1"})


class ForwardingTests(unittest.TestCase):
    def test_switched_on_only_when_off(self):
        import tempfile, os
        calls = []
        runner = SimpleNamespace(run=lambda command, input=None: calls.append(command))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ip_forward")
            for value, expected in (("1\n", []), ("0\n", [["sysctl", "-qw",
                                                            "net.ipv4.ip_forward=1"]])):
                calls.clear()
                with open(path, "w") as handle:
                    handle.write(value)
                m.ensure_forwarding(path, runner)
                self.assertEqual(calls, expected)


if __name__ == "__main__":
    unittest.main()
