"""The systemd units, mdd-enginectl and the sudoers fragment (docs/SLIM_PLAN.md 12.3, 12.7)."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "packaging"
ENGINE = ROOT / "systemd" / "mdd-engine@.service"
NETNS = ROOT / "systemd" / "mdd-netns@.service"
ENGINECTL = ROOT / "sbin" / "mdd-enginectl"
SUDOERS = ROOT / "sudoers.d" / "mdd"


def unit(path: Path) -> dict[str, list[str]]:
    """``Section.Key`` -> every value it is given, in order (systemd allows repeats)."""
    values: dict[str, list[str]] = {}
    section = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        key, _, value = line.partition("=")
        values.setdefault(f"{section}.{key}", []).append(value)
    return values


class EngineUnitTests(unittest.TestCase):
    def setUp(self):
        self.u = unit(ENGINE)

    def one(self, key):
        self.assertEqual(len(self.u.get(key, [])), 1, key)
        return self.u[key][0]

    def environment(self):
        env = {}
        for value in self.u["Service.Environment"]:
            for pair in value.split():
                name, _, setting = pair.partition("=")
                env[name] = setting
        return env

    def test_runs_in_the_lines_namespace_bound_to_it(self):
        self.assertEqual(self.one("Service.NetworkNamespacePath"), "/run/netns/mdd-%i")
        self.assertEqual(self.one("Unit.BindsTo"), "mdd-netns@%i.service")
        self.assertIn("mdd-netns@%i.service", self.one("Unit.After").split())

    def test_restricted_root(self):
        self.assertEqual(self.one("Service.User"), "root")
        self.assertEqual(self.one("Service.Group"), "mdd")
        self.assertEqual(self.one("Service.UMask"), "0007")
        self.assertEqual(set(self.one("Service.CapabilityBoundingSet").split()),
                         {"CAP_NET_ADMIN", "CAP_NET_RAW", "CAP_NET_BIND_SERVICE"})
        self.assertNotIn("CAP_DAC_OVERRIDE", self.one("Service.CapabilityBoundingSet"))
        self.assertEqual(self.one("Service.AmbientCapabilities"), "")
        for key, value in (("NoNewPrivileges", "yes"), ("ProtectSystem", "strict"),
                           ("ProtectHome", "yes"), ("PrivateTmp", "yes"),
                           ("DevicePolicy", "closed"), ("DeviceAllow", "/dev/net/tun rw"),
                           ("LimitCORE", "0"), ("KillMode", "control-group"),
                           ("Restart", "always")):
            self.assertEqual(self.one(f"Service.{key}"), value, key)

    def test_kernel_tunables_stay_writable_for_the_tunnel_sysctls(self):
        # swu_ike sets net.ipv6.conf.ipsec0.* inside the namespace.
        self.assertNotIn("Service.ProtectKernelTunables", self.u)

    def test_writes_only_its_own_line_directories(self):
        self.assertEqual(set(self.one("Service.ReadWritePaths").split()),
                         {"/var/lib/mdd/instances/%i/run", "/var/lib/mdd/instances/%i/logs",
                          "/run/pcscd"})

    def test_no_environment_from_files_the_control_plane_can_write(self):
        self.assertNotIn("Service.EnvironmentFile", self.u)
        self.assertFalse(any(value.startswith("/var/lib") for value in
                             self.u.get("Service.ExecStart", [])))

    def test_paths_point_at_the_lines_directory(self):
        env = self.environment()
        self.assertEqual(env["MDD_CONFIG"], "/var/lib/mdd/instances/%i/instance.json")
        self.assertEqual(env["MDD_RUNDIR"], "/var/lib/mdd/instances/%i/run")
        self.assertEqual(env["MDD_LOGDIR"], "/var/lib/mdd/instances/%i/logs")
        self.assertEqual(env["MDD_ID"], "%i")
        self.assertEqual(env["MDD_ASTERISK_CORE_DUMP"], "0")
        self.assertTrue(env["PATH"].startswith("/opt/mdd/current/venv/bin:"))

    def test_asterisk_gets_private_directories(self):
        mounts = {item.split(":")[0] for item in self.one("Service.TemporaryFileSystem").split()}
        self.assertEqual(mounts, {"/etc/asterisk", "/var/lib/asterisk", "/var/spool/asterisk"})

    def test_starts_the_entrypoint(self):
        self.assertEqual(self.one("Service.ExecStart"), "/opt/mdd/current/engine/entrypoint.sh")


class NetnsUnitTests(unittest.TestCase):
    def test_oneshot_up_and_down(self):
        u = unit(NETNS)
        self.assertEqual(u["Service.Type"], ["oneshot"])
        self.assertEqual(u["Service.RemainAfterExit"], ["yes"])
        self.assertTrue(u["Service.ExecStart"][0].endswith("host/mdd_netns.py up %i"))
        self.assertTrue(u["Service.ExecStop"][0].endswith("host/mdd_netns.py down %i"))
        self.assertIn(" -I ", u["Service.ExecStart"][0])


@unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not installed")
class SystemdVerifyTests(unittest.TestCase):
    def test_units_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            for source in (ENGINE, NETNS):
                shutil.copy(source, tmp)
                instance = source.name.replace("@.", "@1.")
                os.symlink(source.name, os.path.join(tmp, instance))
            result = subprocess.run(
                ["systemd-analyze", "verify", os.path.join(tmp, "mdd-engine@1.service"),
                 os.path.join(tmp, "mdd-netns@1.service")],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        # The executables live on the gateway, not here; anything else is a real complaint.
        complaints = [line for line in result.stdout.splitlines()
                      if line.strip() and "is not executable" not in line]
        self.assertEqual(complaints, [])


class EnginectlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        fake = os.path.join(self.tmp, "bin")
        os.mkdir(fake)
        self.log = os.path.join(self.tmp, "calls")
        with open(os.path.join(fake, "systemctl"), "w") as handle:
            handle.write(f'#!/bin/sh\necho "$@" >> {self.log}\n')
        os.chmod(os.path.join(fake, "systemctl"), 0o755)
        # The script pins PATH; point the copy under test at the fake systemctl instead.
        text = ENGINECTL.read_text()
        pinned = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"
        self.assertIn(pinned, text)
        self.script = os.path.join(self.tmp, "mdd-enginectl")
        with open(self.script, "w") as handle:
            handle.write(text.replace(pinned, f"PATH={fake}:/usr/bin:/bin"))

    def run_ctl(self, *args):
        return subprocess.run(["sh", self.script, *args], text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=10)

    def calls(self):
        try:
            with open(self.log) as handle:
                return handle.read().splitlines()
        except FileNotFoundError:
            return []

    def test_valid_requests_reach_systemctl(self):
        for args, expected in ((("start", "1"), "start mdd-engine@1.service"),
                               (("stop", "10"), "stop mdd-engine@10.service"),
                               (("restart", "2"), "restart mdd-engine@2.service"),
                               (("reset-failed", "3"), "reset-failed mdd-engine@3.service"),
                               (("down", "4"),
                                "stop mdd-engine@4.service mdd-netns@4.service")):
            with self.subTest(args=args):
                result = self.run_ctl(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls()[-1], expected)

    def test_anything_else_is_refused_without_calling_systemctl(self):
        bad = [(), ("start",), ("start", "0"), ("start", "11"), ("start", "01"),
               ("start", "1;id"), ("start", "../1"), ("start", " 1"), ("start", "1 "),
               ("start", "1", "2"), ("enable", "1"), ("start", "*"), ("start", "-1"),
               ("kill", "1"), ("start", "1\n2"), ("", "1"), ("start", "")]
        for args in bad:
            with self.subTest(args=args):
                result = self.run_ctl(*args)
                self.assertEqual(result.returncode, 64)
        self.assertEqual(self.calls(), [])

    def test_shipped_executable_and_pins_path(self):
        self.assertTrue(ENGINECTL.stat().st_mode & stat.S_IXUSR)
        text = ENGINECTL.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("set -eu", text)
        # It must not read any file the control plane can write.
        self.assertNotIn("/var/lib/mdd", text)
        self.assertNotRegex(text, r"(?m)^\s*(\.|source)\s")


class SudoersTests(unittest.TestCase):
    def test_only_enginectl(self):
        rules = [line for line in SUDOERS.read_text().splitlines()
                 if line.strip() and not line.startswith("#") and not line.startswith("Defaults")]
        self.assertEqual(rules, ["mdd ALL=(root) NOPASSWD: /usr/local/sbin/mdd-enginectl"])
        self.assertIn("env_reset", SUDOERS.read_text())

    @unittest.skipUnless(shutil.which("visudo"), "visudo not installed")
    def test_parses(self):
        result = subprocess.run(["visudo", "-cf", str(SUDOERS)], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
