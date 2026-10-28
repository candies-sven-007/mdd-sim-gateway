#!/usr/bin/env python3
"""Per-line network namespaces, routing and nftables for the VoWiFi engines.

Each SIM line N (1-10) runs its engine in network namespace ``mdd-N``. A veth pair joins it to
the host: ``mddvN`` (10.88.N.1/30) on the host, ``eth0`` (10.88.N.2/30) inside. Nothing the
engine sends may leave except through its country's TUN, or the uplink when direct access was
chosen explicitly. Two independent gates enforce that (docs/SLIM_PLAN.md section 12):

* the routing gate. ``mdd-netns@N.service`` (``up``) adds ``iif mddvN lookup 1000+N`` and an
  ``unreachable default`` to that table before the engine starts. The orchestrator only ever
  adds a better default route through the TUN, or a rule to the main table for a direct line.
  When sing-box exits its TUN vanishes, the kernel drops the route with it, and the line falls
  back to unreachable;
* the filter gate. The orchestrator replaces the ``inet mdd`` table atomically; its forward
  chain accepts traffic from a line only towards that line's TUN (or anywhere for a direct
  line) and drops the rest.

Browser call media is the one exception, and it never leaves the host's own private networks:
RTP sent to the host is DNAT'd to the line, and the line's answers -- plus the ICE checks
Asterisk sends from its RTP ports to private subnets the host is attached to -- are marked and
routed by the main table.

Everything here is a pure function of its inputs except ``Runner`` and the two entry points
(``up``/``down`` for the systemd unit, ``reconcile`` for the orchestrator), so the rules can be
tested as text. The ruleset must load on nftables 0.9.8 (Debian 11) without nft_fib: no ``fib``,
NAT only in an ``ip`` family table, numeric chain priorities.
"""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from dataclasses import dataclass

MAX_LINE = 10
NET_PREFIX = "10.88"
SUBNET = ipaddress.ip_network("10.88.0.0/16")
MARK_BASE = 0x6d640000
TABLE_BASE = 1000
PREF_TABLE_BASE = 1000
PREF_FWMARK_BASE = 900
PREF_DIRECT_BASE = 950
UNREACHABLE_METRIC = 4000
TUN_METRIC = 100
# Country TUNs are "mdd-<cc>" (host/mdd_orchestrator.tun_name); veths are "mddv<N>", so the
# wildcard "mdd-*" can never match a veth.
TUN_PREFIX = "mdd-"
VETH_PREFIX = "mddv"
RTP_MAX_SPAN = 60
PRIVATE4 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12",
                                                    "192.168.0.0/16"))

TUN, DIRECT, BLOCKED = "tun", "direct", "blocked"

# The six settings the engine container always had, plus: the inner tunnel (ipsec0) needs
# IPv6, the veth does not.
NETNS_SYSCTLS = (
    "net.ipv6.conf.all.accept_ra=0", "net.ipv6.conf.default.accept_ra=0",
    "net.ipv6.conf.all.autoconf=0", "net.ipv6.conf.default.autoconf=0",
    "net.ipv6.conf.all.use_tempaddr=0", "net.ipv6.conf.default.use_tempaddr=0",
    "net.ipv6.conf.all.disable_ipv6=0", "net.ipv6.conf.eth0.disable_ipv6=1",
)


@dataclass(frozen=True)
class Line:
    """Every name and number belonging to one line, derived from its id alone."""

    n: int

    @property
    def iid(self) -> str:
        return str(self.n)

    @property
    def netns(self) -> str:
        return f"mdd-{self.n}"

    @property
    def veth(self) -> str:
        return f"{VETH_PREFIX}{self.n}"

    @property
    def host_addr(self) -> str:
        return f"{NET_PREFIX}.{self.n}.1"

    @property
    def ns_addr(self) -> str:
        return f"{NET_PREFIX}.{self.n}.2"

    @property
    def table(self) -> int:
        return TABLE_BASE + self.n

    @property
    def mark(self) -> int:
        return MARK_BASE + self.n

    @property
    def mark_hex(self) -> str:
        return f"0x{self.mark:08x}"

    @property
    def pref_table(self) -> int:
        return PREF_TABLE_BASE + self.n

    @property
    def pref_fwmark(self) -> int:
        return PREF_FWMARK_BASE + self.n

    @property
    def pref_direct(self) -> int:
        return PREF_DIRECT_BASE + self.n


def line(iid) -> Line:
    """The line for an id "1".."10"; anything else is refused."""
    text = str(iid)
    if not re.fullmatch(r"[1-9][0-9]?", text) or int(text) > MAX_LINE:
        raise ValueError(f"line id must be 1-{MAX_LINE}")
    return Line(int(text))


# ----------------------------------------------------------------------------- netns setup

def gate_commands(ln: Line) -> list[list[str]]:
    """The routing gate: from here on the line's traffic is unreachable unless allowed."""
    return [
        ["ip", "route", "replace", "unreachable", "default", "metric",
         str(UNREACHABLE_METRIC), "table", str(ln.table)],
        ["ip", "rule", "add", "pref", str(ln.pref_table), "iif", ln.veth,
         "lookup", str(ln.table)],
        ["ip", "rule", "add", "pref", str(ln.pref_fwmark), "iif", ln.veth,
         "fwmark", ln.mark_hex, "lookup", "main"],
    ]


def up_commands(ln: Line) -> list[list[str]]:
    """Create the namespace, the veth and the routing gate. The gate comes first."""
    return gate_commands(ln) + [
        ["ip", "netns", "add", ln.netns],
        ["ip", "link", "add", ln.veth, "type", "veth", "peer", "name", "eth0",
         "netns", ln.netns],
        ["ip", "addr", "add", f"{ln.host_addr}/30", "dev", ln.veth],
        ["sysctl", "-qw", f"net.ipv4.conf.{ln.veth}.rp_filter=2"],
        ["sysctl", "-qw", f"net.ipv6.conf.{ln.veth}.disable_ipv6=1"],
        ["ip", "link", "set", ln.veth, "up"],
        ["ip", "-n", ln.netns, "link", "set", "lo", "up"],
        ["ip", "-n", ln.netns, "addr", "add", f"{ln.ns_addr}/30", "dev", "eth0"],
        ["ip", "-n", ln.netns, "link", "set", "eth0", "up"],
        ["ip", "-n", ln.netns, "route", "add", "default", "via", ln.host_addr],
        ["ip", "netns", "exec", ln.netns, "sysctl", "-qw", *NETNS_SYSCTLS],
    ]


def down_commands(ln: Line) -> list[list[str]]:
    """Undo up_commands. Every step may fail harmlessly when its object is already gone."""
    return [
        ["ip", "link", "del", ln.veth],
        ["ip", "netns", "del", ln.netns],
        ["ip", "rule", "del", "pref", str(ln.pref_direct)],
        ["ip", "rule", "del", "pref", str(ln.pref_fwmark)],
        ["ip", "rule", "del", "pref", str(ln.pref_table)],
        ["ip", "route", "flush", "table", str(ln.table)],
    ]


# ----------------------------------------------------------------------- route decision

@dataclass(frozen=True)
class RouteState:
    route: str              # TUN | DIRECT | BLOCKED
    interface: str = ""     # the TUN, for TUN
    reason: str = ""


def line_route_state(line_doc: dict, *, proxy_enabled: bool, exits: dict,
                     interfaces: set[str] | frozenset[str]) -> RouteState:
    """Where one line's traffic may go. Anything not positively allowed is BLOCKED.

    ``exits`` is the orchestrator's per-country state (ready / mode / interface / error).
    """
    if not line_doc.get("enabled", True):
        return RouteState(BLOCKED, reason="line disabled")
    if not proxy_enabled:
        return RouteState(DIRECT, reason="country proxy routing is off")
    country = str(line_doc.get("country") or "").lower()
    if not country:
        return RouteState(BLOCKED, reason="SIM country unknown")
    exit_state = exits.get(country)
    if not isinstance(exit_state, dict):
        return RouteState(BLOCKED, reason=f"no enabled {country.upper()} exit")
    if not exit_state.get("ready"):
        return RouteState(BLOCKED, reason=str(exit_state.get("error")
                                              or f"{country.upper()} exit not ready"))
    if exit_state.get("mode") == "direct":
        return RouteState(DIRECT, reason=f"{country.upper()} exit set to direct")
    interface = str(exit_state.get("interface") or "")
    if not interface.startswith(TUN_PREFIX) or interface not in interfaces:
        return RouteState(BLOCKED, reason=f"{country.upper()} exit interface is missing")
    return RouteState(TUN, interface=interface)


def rtp_ranges(lines: list[dict], control_port: int) -> dict[str, tuple[int, int] | str]:
    """Each valid line's RTP range, or the reason it was refused.

    The ranges come from the control plane, so they are checked here before they become DNAT
    rules: integers, 1024-65535, at most RTP_MAX_SPAN ports, not the control port, no overlap.
    """
    result: dict[str, tuple[int, int] | str] = {}
    taken: list[tuple[int, int]] = []
    ordered = []
    for doc in lines:
        try:
            ordered.append((line(doc.get("id")), doc))
        except ValueError:
            continue
    for ln, doc in sorted(ordered, key=lambda item: item[0].n):
        rtp = doc.get("rtp")
        if not (isinstance(rtp, (list, tuple)) and len(rtp) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) for v in rtp)):
            result[ln.iid] = "RTP range missing"
            continue
        start, end = rtp
        if not 1024 <= start <= end <= 65535 or end - start + 1 > RTP_MAX_SPAN:
            result[ln.iid] = "RTP range invalid"
        elif start <= int(control_port) <= end:
            result[ln.iid] = "RTP range includes the control port"
        elif any(start <= e and s <= end for s, e in taken):
            result[ln.iid] = "RTP range overlaps another line"
        else:
            taken.append((start, end))
            result[ln.iid] = (start, end)
    return result


# ------------------------------------------------------------------- host address views

def parse_interfaces(link_text: str) -> set[str]:
    """Interface names from ``ip -o link show``."""
    names = set()
    for row in link_text.splitlines():
        match = re.match(r"\s*\d+:\s+([^:@\s]+)", row)
        if match:
            names.add(match.group(1))
    return names


def _ours(interface: str) -> bool:
    return interface.startswith(TUN_PREFIX) or interface.startswith(VETH_PREFIX)


def parse_local4(addr_text: str) -> list[str]:
    """The host's own IPv4 addresses (``ip -4 -o addr show``) that browsers may send RTP to."""
    found = []
    for row in addr_text.splitlines():
        fields = row.split()
        if len(fields) < 4 or fields[2] != "inet" or _ours(fields[1]):
            continue
        try:
            address = ipaddress.ip_interface(fields[3]).ip
        except ValueError:
            continue
        if address.is_loopback or address in SUBNET:
            continue
        if str(address) not in found:
            found.append(str(address))
    return found


def subnet_conflict(addr_text: str) -> str:
    """An address of someone else's inside 10.88.0.0/16, which would make lines ambiguous."""
    for row in addr_text.splitlines():
        fields = row.split()
        if len(fields) < 4 or fields[2] != "inet" or _ours(fields[1]):
            continue
        try:
            network = ipaddress.ip_interface(fields[3]).network
        except ValueError:
            continue
        if network.overlaps(SUBNET):
            return f"{fields[1]} uses {network}, which overlaps the line subnet {SUBNET}"
    return ""


def parse_lan4(route_text: str) -> list[str]:
    """Private subnets the host is directly attached to (``ip -4 route show table main
    scope link``), collapsed so the nft interval set accepts them."""
    networks = []
    for row in route_text.splitlines():
        fields = row.split()
        if not fields or "dev" not in fields:
            continue
        device = fields[fields.index("dev") + 1] if fields.index("dev") + 1 < len(fields) else ""
        if _ours(device):
            continue
        try:
            network = ipaddress.ip_network(fields[0], strict=False)
        except ValueError:
            continue
        if network.version != 4 or network.overlaps(SUBNET):
            continue
        if any(network.subnet_of(private) for private in PRIVATE4):
            networks.append(network)
    return [str(n) for n in ipaddress.collapse_addresses(networks)]


# ------------------------------------------------------------------------------ nftables

@dataclass(frozen=True)
class LinePlan:
    line: Line
    state: RouteState
    rtp: tuple[int, int] | None


def _set(name: str, kind: str, elements: list[str], interval: bool) -> str:
    flags = " flags interval;" if interval else ""
    body = f" elements = {{ {', '.join(elements)} }}" if elements else ""
    return f"  set {name} {{ type {kind};{flags}{body} }}\n"


def ruleset(plans: list[LinePlan], *, control_port: int, local4: list[str],
            lan4: list[str]) -> str:
    """Both tables, as one ``nft -f`` transaction that replaces whatever was there."""
    mangle, forward, nat_pre, nat_post = [], [], [], []
    for plan in sorted(plans, key=lambda p: p.line.n):
        ln, state, rtp = plan.line, plan.state, plan.rtp
        veth, mark = f'"{ln.veth}"', ln.mark_hex
        mangle.append(f"iifname {veth} ct mark {mark} meta mark set {mark}")
        if rtp:
            ports = f"{rtp[0]}-{rtp[1]}"
            mangle.append(f"iifname {veth} ip daddr @lan4 udp sport {ports} meta mark set {mark}")
            nat_pre.append(f'iifname != "{VETH_PREFIX}*" iifname != "{TUN_PREFIX}*" '
                           f"ip daddr @local4 udp dport {ports} ct mark set {mark} "
                           f"dnat to {ln.ns_addr}")
            nat_post.append(f"ip saddr {ln.ns_addr} ip daddr @lan4 udp sport {ports} masquerade")
        forward.append(f"iifname {veth} meta mark {mark} accept")
        if state.route == TUN:
            forward.append(f'iifname {veth} oifname "{state.interface}" accept')
            nat_post.append(f'ip saddr {ln.ns_addr} oifname "{state.interface}" masquerade')
        elif state.route == DIRECT:
            forward.append(f"iifname {veth} accept")
            nat_post.append(f"ip saddr {ln.ns_addr} oifname != {veth} masquerade")
        forward.append(f"oifname {veth} ct mark {mark} accept")
        forward.append(f"oifname {veth} ct state established,related accept")
    forward.append(f'iifname "{VETH_PREFIX}*" counter drop')
    forward.append(f'oifname "{VETH_PREFIX}*" counter drop')

    def chain(name: str, hook: str, priority: int, kind: str, rules: list[str]) -> str:
        lines = [f"  chain {name} {{\n",
                 f"    type {kind} hook {hook} priority {priority}; policy accept;\n"]
        lines += [f"    {rule}\n" for rule in rules]
        lines.append("  }\n")
        return "".join(lines)

    text = ["table inet mdd\n", "delete table inet mdd\n", "table inet mdd {\n",
            _set("lan4", "ipv4_addr", lan4, True),
            chain("mangle_pre", "prerouting", -150, "filter", mangle),
            chain("forward", "forward", 0, "filter", forward),
            chain("input", "input", 0, "filter", [
                f'iifname "{VETH_PREFIX}*" ct state established,related accept',
                f'iifname "{VETH_PREFIX}*" tcp dport {int(control_port)} accept',
                f'iifname "{VETH_PREFIX}*" counter drop']),
            "}\n",
            "table ip mdd_nat\n", "delete table ip mdd_nat\n", "table ip mdd_nat {\n",
            _set("local4", "ipv4_addr", local4, False),
            _set("lan4", "ipv4_addr", lan4, True),
            chain("pre", "prerouting", -100, "nat", nat_pre),
            chain("post", "postrouting", 100, "nat", nat_post),
            "}\n"]
    return "".join(text)


# --------------------------------------------------------------------------- reconcile

@dataclass(frozen=True)
class TableView:
    device: str = ""                    # the TUN of the default route, or ""
    unreachable: bool = False           # the fallback is in place
    throws: frozenset[str] = frozenset()


def parse_table(table_text: str) -> TableView:
    """What ``ip route show table 1000+N`` holds that this module manages."""
    device, unreachable, throws = "", False, set()
    for row in table_text.splitlines():
        fields = row.split()
        if fields[:2] == ["unreachable", "default"]:
            unreachable = True
        elif fields[:1] == ["default"] and "dev" in fields:
            index = fields.index("dev") + 1
            if index < len(fields):
                device = fields[index]
        elif fields[:1] == ["throw"] and len(fields) > 1:
            try:
                throws.add(str(ipaddress.ip_network(fields[1], strict=False)))
            except ValueError:
                pass
    return TableView(device, unreachable, frozenset(throws))


def parse_rule_prefs(rule_text: str) -> set[int]:
    """Preferences present in ``ip rule show``."""
    prefs = set()
    for row in rule_text.splitlines():
        match = re.match(r"\s*(\d+):", row)
        if match:
            prefs.add(int(match.group(1)))
    return prefs


def tighten_commands(ln: Line, state: RouteState, table: TableView, prefs: set[int],
                     lan4: list[str]) -> list[list[str]]:
    """Restore the gate and withdraw whatever this cycle no longer allows."""
    commands = []
    current_device = table.device
    if not table.unreachable or ln.pref_table not in prefs or ln.pref_fwmark not in prefs:
        # Re-adding an existing rule duplicates it; delete first, quietly.
        commands += [["ip", "rule", "del", "pref", str(ln.pref_table)],
                     ["ip", "rule", "del", "pref", str(ln.pref_fwmark)]]
        commands += gate_commands(ln)
    if current_device and (state.route != TUN or state.interface != current_device):
        commands.append(["ip", "route", "del", "default", "dev", current_device,
                         "metric", str(TUN_METRIC), "table", str(ln.table)])
    if state.route != DIRECT and ln.pref_direct in prefs:
        commands.append(["ip", "rule", "del", "pref", str(ln.pref_direct)])
    for network in sorted(table.throws - set(lan4)):
        commands.append(["ip", "route", "del", "throw", network, "table", str(ln.table)])
    return commands


def loosen_commands(ln: Line, state: RouteState, table: TableView, prefs: set[int],
                    lan4: list[str]) -> list[list[str]]:
    """What this cycle allows, added only after the filter gate is in place.

    The throw routes send the host's own private subnets back to the main table. Without them
    the kernel's reverse-path check, which looks a forwarded packet's source up as if it came
    in on the outgoing interface (mddvN, so through the line's own table), finds the TUN and
    drops the browser's answers under strict rp_filter. Only marked RTP is forwarded there;
    the forward chain drops everything else, so nothing else of the line reaches the LAN.
    """
    commands = [["ip", "route", "replace", "throw", network, "table", str(ln.table)]
                for network in lan4 if network not in table.throws]
    if state.route == TUN and table.device != state.interface:
        commands.append(["ip", "route", "replace", "default", "dev", state.interface,
                         "metric", str(TUN_METRIC), "table", str(ln.table)])
    if state.route == DIRECT and ln.pref_direct not in prefs:
        commands.append(["ip", "rule", "add", "pref", str(ln.pref_direct), "iif", ln.veth,
                         "lookup", "main"])
    return commands


class Runner:
    """Runs commands; tests substitute one that records them."""

    def run(self, command: list[str], input: str | None = None):
        return subprocess.run(command, input=input, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15)


def _out(runner: Runner, command: list[str]) -> str:
    result = runner.run(command)
    return result.stdout if result.returncode == 0 else ""


def ensure_forwarding(path: str = "/proc/sys/net/ipv4/ip_forward", runner=None) -> None:
    """Docker used to switch IPv4 forwarding on; without it no line can reach its TUN."""
    try:
        with open(path) as handle:
            if handle.read().strip() == "1":
                return
    except OSError:
        return
    (runner or Runner()).run(["sysctl", "-qw", "net.ipv4.ip_forward=1"])


def reconcile(runner: Runner, desired_lines: list[dict], *, proxy_enabled: bool,
              exits: dict, control_port: int, log=lambda message: None) -> dict[str, dict]:
    """Bring routes and nftables in line with the desired lines. Returns per-line status.

    Order matters: first everything that narrows (gate repair, withdrawn routes and rules),
    then the nft tables, then whatever widens. A line therefore never passes through a state
    where it may reach a TUN or the uplink it is no longer allowed to.
    """
    interfaces = parse_interfaces(_out(runner, ["ip", "-o", "link", "show"]))
    addr_text = _out(runner, ["ip", "-4", "-o", "addr", "show"])
    route_text = _out(runner, ["ip", "-4", "route", "show", "table", "main", "scope", "link"])
    prefs = parse_rule_prefs(_out(runner, ["ip", "rule", "show"]))
    conflict = subnet_conflict(addr_text)
    ranges = rtp_ranges(desired_lines, control_port)

    plans, status = [], {}
    for doc in desired_lines:
        try:
            ln = line(doc.get("id"))
        except ValueError:
            continue
        state = line_route_state(doc, proxy_enabled=proxy_enabled, exits=exits,
                                 interfaces=interfaces)
        rtp = ranges.get(ln.iid)
        if conflict:
            state = RouteState(BLOCKED, reason=conflict)
        elif isinstance(rtp, str) and state.route != BLOCKED:
            state = RouteState(BLOCKED, reason=rtp)
        plans.append(LinePlan(ln, state, rtp if isinstance(rtp, tuple) else None))

    lan4 = parse_lan4(route_text)
    present = {plan.line.n: plan for plan in plans if plan.line.veth in interfaces}
    current = {}
    for n, plan in present.items():
        current[n] = parse_table(
            _out(runner, ["ip", "route", "show", "table", str(plan.line.table)]))
        for command in tighten_commands(plan.line, plan.state, current[n], prefs, lan4):
            runner.run(command)

    rules = ruleset(plans, control_port=control_port, local4=parse_local4(addr_text),
                    lan4=lan4)
    applied = runner.run(["nft", "-f", "-"], input=rules)
    nft_error = "" if applied.returncode == 0 else \
        ("nftables rules not loaded: " + (applied.stderr or "").strip())[:300]
    if nft_error:
        log(nft_error)

    tuns = set()
    for n, plan in present.items():
        if nft_error:
            continue        # without the filter gate nothing is widened
        for command in loosen_commands(plan.line, plan.state, current[n], prefs, lan4):
            runner.run(command)
        if plan.state.route == TUN:
            tuns.add(plan.state.interface)
    if tuns:
        runner.run(["sysctl", "-qw", *sorted(f"net.ipv4.conf.{t}.rp_filter=2" for t in tuns)])

    for plan in plans:
        route = plan.state.route if not nft_error else BLOCKED
        status[plan.line.iid] = {
            "route": route, "interface": plan.state.interface if route == TUN else "",
            "netns": plan.line.netns, "netns_present": plan.line.n in present,
            "reason": nft_error or plan.state.reason}
    return status


# ------------------------------------------------------------------------- entry points

def _ipv6_only(command: list[str]) -> bool:
    """A sysctl command that sets nothing but IPv6 keys."""
    keys = [arg for arg in command if "=" in arg]
    return "sysctl" in command and bool(keys) and all(k.startswith("net.ipv6.") for k in keys)


def up(iid, runner: Runner | None = None) -> int:
    """``mdd-netns@N.service`` start: a fresh namespace, whatever was left before."""
    runner = runner or Runner()
    ln = line(iid)
    for command in down_commands(ln):
        runner.run(command)
    for command in up_commands(ln):
        result = runner.run(command)
        if result.returncode != 0:
            # The IPv6 settings fail only on a kernel without IPv6, where nothing else here
            # depends on them; every other step is required.
            if _ipv6_only(command):
                print(f"mdd-netns: {' '.join(command)}: {result.stderr.strip()}",
                      file=sys.stderr)
                continue
            print(f"mdd-netns: {' '.join(command)} failed: {result.stderr.strip()}",
                  file=sys.stderr)
            return 1
    return 0


def down(iid, runner: Runner | None = None) -> int:
    runner = runner or Runner()
    for command in down_commands(line(iid)):
        runner.run(command)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in ("up", "down"):
        print("usage: mdd_netns.py {up|down} <line 1-10>", file=sys.stderr)
        return 64
    try:
        line(argv[1])
    except ValueError as exc:
        print(f"mdd-netns: {exc}", file=sys.stderr)
        return 64
    os.umask(0o022)
    return up(argv[1]) if argv[0] == "up" else down(argv[1])


if __name__ == "__main__":
    sys.exit(main())
