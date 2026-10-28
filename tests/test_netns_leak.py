"""A line's IKE never leaves through the uplink while its country exit is down.

Privileged integration test for host/mdd_netns.py (docs/SLIM_PLAN.md section 12.8). Runs only as
root with MDD_NETNS_IT=1; otherwise skipped. Nothing touches the real host's routes: the whole
topology lives in namespaces of its own.

    it-lan (browser, 192.168.77.2) --lan0-- it-host --up0-- it-wan (uplink; owns the "ePDG")
                                              |  \\
                                           mddv1  mdd-us (TUN, served by a reader process
                                              |           that plays sing-box)
                                            mdd-1 (the line, 10.88.1.2)

The rules are the ones mdd_netns generates, applied inside it-host. it-wan counts every
datagram that reaches the ePDG address: that counter is the leak.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from host import mdd_netns

HOST, WAN, LAN = "it-host", "it-wan", "it-lan"
EPDG = "198.51.100.10"
CONTROL_PORT = 8443
LINE_DOC = {"id": "1", "enabled": True, "country": "us", "rtp": [10000, 10011]}
TUN_EXIT = {"us": {"ready": True, "mode": "subscription", "interface": "mdd-us"}}

# Plays sing-box: owns the TUN, counts datagrams to UDP 4500, and echoes each one back with
# addresses and ports swapped, so the return path (conntrack, rp_filter) is exercised too.
READER = r"""
import fcntl, os, struct, sys
TUNSETIFF, IFF_TUN, IFF_NO_PI = 0x400454ca, 0x0001, 0x1000
fd = os.open("/dev/net/tun", os.O_RDWR)
fcntl.ioctl(fd, TUNSETIFF, struct.pack("16sH", b"mdd-us", IFF_TUN | IFF_NO_PI))
count_path = sys.argv[1]
open(count_path, "w").write("0")
print("ready", flush=True)
count = 0
def checksum(data):
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return ~total & 0xffff
while True:
    packet = bytearray(os.read(fd, 65535))
    if len(packet) < 28 or packet[0] >> 4 != 4 or packet[9] != 17:
        continue
    ihl = (packet[0] & 15) * 4
    sport, dport = struct.unpack("!HH", packet[ihl:ihl + 4])
    if dport != 4500:
        continue
    count += 1
    open(count_path, "w").write(str(count))
    packet[12:16], packet[16:20] = packet[16:20], packet[12:16]
    packet[ihl:ihl + 4] = struct.pack("!HH", dport, sport)
    packet[ihl + 6:ihl + 8] = b"\0\0"
    packet[10:12] = b"\0\0"
    packet[10:12] = struct.pack("!H", checksum(bytes(packet[:ihl])))
    os.write(fd, bytes(packet))
"""

# Sends three datagrams to the ePDG from UDP 4500 and reports how many echoes came back.
SENDER = r"""
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 4500))
s.settimeout(0.5)
echoes = 0
for _ in range(3):
    try:
        s.sendto(b"ike", (sys.argv[1], 4500))
        s.recvfrom(100)
        echoes += 1
    except OSError:
        pass
print(echoes)
"""

UDP_SERVER = r"""
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", int(sys.argv[1])))
s.settimeout(float(sys.argv[2]))
print("ready", flush=True)
try:
    data, peer = s.recvfrom(100)
    print(peer[0])
    s.sendto(b"answer", peer)
except OSError:
    print("none")
"""

UDP_CLIENT = r"""
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
if len(sys.argv) > 3:
    s.bind(("0.0.0.0", int(sys.argv[3])))
s.settimeout(1.0)
s.sendto(b"hello", (sys.argv[1], int(sys.argv[2])))
try:
    print(s.recvfrom(100)[1][0])
except OSError:
    print("none")
"""


def sh(*command, check=True, input=None):
    result = subprocess.run(command, input=input, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=20)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(command)}: {result.stderr.strip()}")
    return result


def ns(name, *command, check=True, input=None):
    return sh("ip", "netns", "exec", name, *command, check=check, input=input)


def py(name, script, *args):
    return ns(name, sys.executable, "-c", script, *args).stdout.strip()


class HostRunner(mdd_netns.Runner):
    """Runs mdd_netns's commands inside the emulated host."""

    def run(self, command, input=None):
        # A namespace added from inside `ip netns exec` exists only in that command's private
        # mount namespace; names are global, so add and delete them from outside.
        inside = [] if command[:2] == ["ip", "netns"] and command[2:3] in (["add"], ["del"]) \
            else ["ip", "netns", "exec", HOST]
        return subprocess.run([*inside, *command], input=input, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)


@unittest.skipUnless(os.environ.get("MDD_NETNS_IT") == "1" and os.geteuid() == 0
                     and shutil.which("ip") and shutil.which("nft"),
                     "privileged netns integration test (set MDD_NETNS_IT=1, run as root)")
class NetnsLeakTests(unittest.TestCase):
    def setUp(self):
        self.cleanup_namespaces()
        self.addCleanup(self.cleanup_namespaces)
        self.tmp = tempfile.mkdtemp(prefix="mdd-netns-it-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reader = None
        self.addCleanup(self.stop_reader)
        for name in (HOST, WAN, LAN):
            sh("ip", "netns", "add", name)
            ns(name, "ip", "link", "set", "lo", "up")
        sh("ip", "-n", HOST, "link", "add", "up0", "type", "veth", "peer", "name", "wan0",
           "netns", WAN)
        sh("ip", "-n", HOST, "link", "add", "lan0", "type", "veth", "peer", "name", "br0",
           "netns", LAN)
        for name, dev, addr in ((HOST, "up0", "203.0.113.1/24"), (WAN, "wan0", "203.0.113.254/24"),
                                (HOST, "lan0", "192.168.77.1/24"), (LAN, "br0", "192.168.77.2/24")):
            sh("ip", "-n", name, "addr", "add", addr, "dev", dev)
            sh("ip", "-n", name, "link", "set", dev, "up")
        sh("ip", "-n", HOST, "route", "add", "default", "via", "203.0.113.254")
        sh("ip", "-n", LAN, "route", "add", "default", "via", "192.168.77.1")
        sh("ip", "-n", WAN, "addr", "add", f"{EPDG}/32", "dev", "lo")
        sh("ip", "-n", WAN, "route", "add", "default", "via", "203.0.113.1")
        # Strict reverse-path filtering, the case R-2 is about.
        ns(HOST, "sysctl", "-qw", "net.ipv4.ip_forward=1", "net.ipv4.conf.all.rp_filter=1")
        ns(WAN, "nft", "-f", "-", input=(
            "table inet leak {\n  chain input {\n"
            "    type filter hook input priority 0; policy accept;\n"
            f"    ip daddr {EPDG} udp dport {{ 500, 4500 }} counter\n  }}\n}}\n"))
        self.runner = HostRunner()
        self.assertEqual(mdd_netns.up("1", self.runner), 0)

    @staticmethod
    def cleanup_namespaces():
        for name in ("mdd-1", HOST, WAN, LAN):
            subprocess.run(["ip", "netns", "del", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

    # ------------------------------------------------------------------ helpers
    def start_reader(self):
        count_path = os.path.join(self.tmp, "tun-count")
        self.reader = subprocess.Popen(
            ["ip", "netns", "exec", HOST, sys.executable, "-c", READER, count_path],
            stdout=subprocess.PIPE, text=True)
        self.assertEqual(self.reader.stdout.readline().strip(), "ready")
        sh("ip", "-n", HOST, "addr", "add", "172.29.20.1/30", "dev", "mdd-us")
        sh("ip", "-n", HOST, "link", "set", "mdd-us", "up")
        self.count_path = count_path

    def stop_reader(self):
        if self.reader and self.reader.poll() is None:
            self.reader.kill()
            self.reader.wait()
        self.reader = None

    def tun_count(self):
        with open(self.count_path) as handle:
            return int(handle.read() or 0)

    def leaked(self):
        out = ns(WAN, "nft", "list", "table", "inet", "leak").stdout
        return int(out.split("packets", 1)[1].split()[0])

    def reconcile(self, proxy_enabled=True, exits=None):
        return mdd_netns.reconcile(self.runner, [LINE_DOC], proxy_enabled=proxy_enabled,
                                   exits=TUN_EXIT if exits is None else exits,
                                   control_port=CONTROL_PORT)

    def send_ike(self):
        return int(py("mdd-1", SENDER, EPDG))

    def route_get(self):
        return ns(HOST, "ip", "route", "get", EPDG, "from", "10.88.1.2", "iif", "mddv1",
                  check=False)

    # ------------------------------------------------------------------ scenarios
    def test_a_exit_up_traffic_goes_through_the_tun_and_comes_back(self):
        self.start_reader()
        self.assertEqual(self.reconcile()["1"]["route"], mdd_netns.TUN)
        self.assertEqual(self.send_ike(), 3)
        self.assertEqual(self.tun_count(), 3)
        self.assertEqual(self.leaked(), 0)

    def test_b_singbox_exit_removes_the_tun_and_the_line_is_unreachable(self):
        self.start_reader()
        self.reconcile()
        self.stop_reader()      # the TUN vanishes with its process
        time.sleep(0.2)
        result = self.route_get()
        self.assertNotEqual(result.returncode, 0)
        # An unreachable route answers EHOSTUNREACH: "No route to host".
        self.assertRegex((result.stdout + result.stderr).lower(),
                         r"unreachable|no route to host")
        self.assertEqual(self.send_ike(), 0)
        self.assertEqual(self.leaked(), 0)
        self.assertEqual(self.reconcile()["1"]["route"], mdd_netns.BLOCKED)
        self.assertEqual(self.send_ike(), 0)
        self.assertEqual(self.leaked(), 0)

    def test_c_exit_not_ready_withdraws_the_route_though_the_tun_is_there(self):
        self.start_reader()
        self.reconcile()
        status = self.reconcile(exits={"us": {"ready": False, "error": "probe failed"}})
        self.assertEqual(status["1"]["route"], mdd_netns.BLOCKED)
        self.assertNotEqual(self.route_get().returncode, 0)
        self.assertEqual(self.send_ike(), 0)
        self.assertEqual(self.tun_count(), 0)
        self.assertEqual(self.leaked(), 0)

    def test_d_routing_gate_holds_without_the_nft_tables(self):
        self.start_reader()
        self.reconcile()
        self.stop_reader()
        ns(HOST, "nft", "delete", "table", "inet", "mdd")
        ns(HOST, "nft", "delete", "table", "ip", "mdd_nat")
        self.assertEqual(self.send_ike(), 0)
        self.assertEqual(self.leaked(), 0)

    def test_e_filter_gate_holds_without_the_routing_gate(self):
        self.start_reader()
        self.reconcile()
        self.stop_reader()
        ln = mdd_netns.line("1")
        ns(HOST, "ip", "rule", "del", "pref", str(ln.pref_table))
        # Now the main table would send it out of up0; only the forward chain stands between.
        self.assertEqual(self.route_get().returncode, 0)
        self.assertEqual(self.send_ike(), 0)
        self.assertEqual(self.leaked(), 0)
        drops = ns(HOST, "nft", "list", "chain", "inet", "mdd", "forward").stdout
        self.assertRegex(drops, r'iifname "mddv\*" counter packets [1-9]')

    def test_f_direct_line_reaches_the_uplink_masqueraded(self):
        status = self.reconcile(proxy_enabled=False)
        self.assertEqual(status["1"]["route"], mdd_netns.DIRECT)
        self.assertEqual(self.send_ike(), 0)       # the ePDG does not answer; it only counts
        self.assertEqual(self.leaked(), 3)
        # Back to proxy mode with no exit: blocked again, nothing more arrives.
        self.reconcile(exits={})
        self.send_ike()
        self.assertEqual(self.leaked(), 3)

    def test_g_browser_media_reaches_the_line_and_answers_stay_on_the_lan(self):
        self.start_reader()
        self.reconcile()
        server = subprocess.Popen(["ip", "netns", "exec", "mdd-1", sys.executable, "-c",
                                   UDP_SERVER, "10000", "3"], stdout=subprocess.PIPE, text=True)
        self.assertEqual(server.stdout.readline().strip(), "ready")
        answered_from = py(LAN, UDP_CLIENT, "192.168.77.1", "10000")
        seen_from = server.stdout.readline().strip()
        server.wait(5)
        self.assertEqual(seen_from, "192.168.77.2")      # DNAT, no source rewrite
        self.assertEqual(answered_from, "192.168.77.1")  # the answer came back over the LAN
        # Asterisk's own ICE check from an RTP port reaches the browser from the host address.
        browser = subprocess.Popen(["ip", "netns", "exec", LAN, sys.executable, "-c",
                                    UDP_SERVER, "5555", "3"], stdout=subprocess.PIPE, text=True)
        self.assertEqual(browser.stdout.readline().strip(), "ready")
        reply_from = py("mdd-1", UDP_CLIENT, "192.168.77.2", "5555", "10001")
        self.assertEqual(browser.stdout.readline().strip(), "192.168.77.1")
        browser.wait(5)
        self.assertEqual(reply_from, "192.168.77.2")
        self.assertEqual(self.tun_count(), 0)
        # A port outside the line's range does not get to the LAN.
        self.assertEqual(py("mdd-1", UDP_CLIENT, "192.168.77.2", "5556", "20000"), "none")

    def test_h_no_ipv6_on_the_veth(self):
        out = sh("ip", "-n", "mdd-1", "-6", "addr", "show", "dev", "eth0").stdout
        self.assertNotIn("inet6", out)
        out = sh("ip", "-n", HOST, "-6", "addr", "show", "dev", "mddv1").stdout
        self.assertNotIn("inet6", out)

    def test_country_change_never_passes_through_the_other_tun(self):
        self.start_reader()
        self.assertEqual(self.reconcile()["1"]["route"], mdd_netns.TUN)
        # Moved to GB, whose exit is ready but whose TUN does not exist (yet): the line must
        # end up blocked, not left on mdd-us.
        moved = dict(LINE_DOC, country="gb")
        status = mdd_netns.reconcile(self.runner, [moved], proxy_enabled=True,
                                     exits={"gb": {"ready": True, "mode": "subscription",
                                                   "interface": "mdd-gb"}, **TUN_EXIT},
                                     control_port=CONTROL_PORT)
        self.assertEqual(status["1"]["route"], mdd_netns.BLOCKED)
        self.assertEqual(self.send_ike(), 0)
        self.assertEqual(self.tun_count(), 0)
        self.assertEqual(self.leaked(), 0)


if __name__ == "__main__":
    unittest.main()
