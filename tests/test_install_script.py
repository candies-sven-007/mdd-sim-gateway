"""install.sh (SLIM_PLAN section 7 and 12): offline, apt for system packages only, and every
change it makes has an undo in `uninstall`."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
TEXT = INSTALL.read_text()


def function(name):
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", TEXT, re.M | re.S)
    assert match, name
    return match.group(0)


class InstallScriptTests(unittest.TestCase):
    def test_posix_syntax(self):
        subprocess.run(["sh", "-n", str(INSTALL)], check=True, timeout=10)

    def test_no_network_fetches_of_project_code(self):
        code = "\n".join(line for line in TEXT.lower().splitlines()
                         if not line.lstrip().startswith("#"))
        for needle in ("github.com", "pypi", "pip install", "npm ", "curl ", "wget ", "docker run", "docker pull"):
            self.assertNotIn(needle, code)

    def test_apt_sources_are_left_alone(self):
        self.assertNotRegex(TEXT, r"(>|>>|tee|sed -i)[^\n]*sources\.list")

    def test_release_is_verified_before_anything_is_installed(self):
        body = function("cmd_install")
        self.assertLess(body.index("verify_release"), body.index("install_packages"))
        self.assertIn("sha256sum -c", function("verify_release"))

    def test_sudoers_is_validated_and_tight(self):
        body = function("install_units")
        self.assertIn("visudo -cf", body)
        self.assertIn("-m 0440", body)
        self.assertIn('-m 0755 -o root -g root "$CURRENT/packaging/sbin/mdd-enginectl"', body)

    def test_every_change_has_an_undo(self):
        undo = function("cmd_uninstall")
        for name in ("SUDOERS", "ENGINECTL", "MODULES_FILE", "SYSCTL_FILE", "UNIT_DIR",
                     "PREFIX_ROOT", "ZRAM_UNIT"):
            self.assertIn(name, undo, name)
        self.assertIn("unmask pcscd.socket pcscd.service", undo)
        self.assertIn("removed_packages", undo)
        self.assertIn("nft delete table inet mdd", undo)
        self.assertIn("--purge", undo)

    def test_removals_cannot_expand_to_the_root(self):
        for line in TEXT.splitlines():
            if re.search(r"\brm -r?f\b", line) and "$" in line:
                for target in re.findall(r'"(\$[^"]+)"', line):
                    with self.subTest(line=line.strip()):
                        self.assertRegex(target, r":\?\}|^\$temporary$")

    def test_engines_are_never_started_by_the_installer(self):
        self.assertNotRegex(TEXT, r"systemctl (start|enable)[^\n]*mdd-engine@")

    def test_distro_pcscd_is_replaced_and_recorded(self):
        body = function("replace_distro_pcscd")
        self.assertIn("mask", body)
        self.assertIn("state_set removed_packages", body)

    def test_installs_the_units_it_ships(self):
        shipped = sorted(p.name for p in (ROOT / "packaging" / "systemd").glob("*.service"))
        listed = sorted(re.search(r'^UNITS="([^"]+)"', TEXT, re.M).group(1).split())
        self.assertEqual(shipped, listed)

    def test_logs_refuses_odd_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp, "journalctl")
            fake.write_text("#!/bin/sh\necho \"$@\"\n")
            fake.chmod(0o755)
            env = {**os.environ, "PATH": f"{temp}:{os.environ['PATH']}"}

            def logs(*args):
                return subprocess.run(["sh", str(INSTALL), "logs", *args], env=env, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

            self.assertEqual(logs("engine", "2", "50").stdout.strip(),
                             "-u mdd-engine@2.service -n 50 --no-pager")
            self.assertEqual(logs("control").stdout.strip(),
                             "-u mdd-control.service -n 200 --no-pager")
            for bad in (("engine", "11"), ("engine", "1;id"), ("nginx",), ("control", "5x")):
                with self.subTest(args=bad):
                    self.assertNotEqual(logs(*bad).returncode, 0)

    @unittest.skipUnless(shutil.which("bash"), "bash missing")
    def test_build_script_syntax(self):
        subprocess.run(["bash", "-n", str(ROOT / "build" / "build-arm64.sh")], check=True,
                       timeout=10)


class BuildScriptTests(unittest.TestCase):
    TEXT = (ROOT / "build" / "build-arm64.sh").read_text()

    def test_every_download_is_pinned(self):
        for line in self.TEXT.splitlines():
            if line.strip().startswith("fetch "):
                with self.subTest(line=line.strip()):
                    self.assertRegex(line, r'"\$\w+SHA256"\s*$')
        for name in ("PJPROJECT_COMMIT", "ASTERISK_COMMIT", "LPAC_COMMIT"):
            self.assertRegex(self.TEXT, rf"{name}=[0-9a-f]{{40}}\n")

    def test_every_asterisk_patch_follows_ast_src(self):
        """The patchers default to the old image's /home/asterisk-build; the build sets AST_SRC."""
        self.assertIn('export AST_SRC="$WORK/asterisk"', self.TEXT)
        self.assertLess(self.TEXT.index("export AST_SRC="),
                        self.TEXT.index("engine/patches/asterisk/*.py"))
        for script in sorted((ROOT / "engine" / "patches" / "asterisk").glob("*.py")):
            with self.subTest(script=script.name):
                self.assertIn("AST_SRC", script.read_text())
                # Against an empty tree each one must stop on the missing source file, not on
                # a Python error of its own.
                with tempfile.TemporaryDirectory() as tree:
                    done = subprocess.run([sys.executable, "-I", str(script)], cwd=tree,
                                          capture_output=True, text=True, timeout=30,
                                          env={**os.environ, "AST_SRC": tree})
                self.assertNotRegex(done.stderr, r"(NameError|ImportError|SyntaxError)")
                self.assertNotIn("/home/asterisk-build", done.stdout + done.stderr)

    def test_bundles_what_the_units_expect(self):
        for needle in ('ln -sfn python "$PREFIX/venv"', '"$PREFIX/bin/sing-box"',
                       '"$PREFIX/lpac/"', "apt-packages.txt", "asterisk-keep-modules.txt",
                       "SHA256SUMS", "mdd-slim-xray-arm64-"):
            self.assertIn(needle, self.TEXT)

    def test_no_debug_or_sounds_in_asterisk(self):
        self.assertIn("--disable BUILD_NATIVE", self.TEXT)
        self.assertIn("--disable-category MENUSELECT_CORE_SOUNDS", self.TEXT)


if __name__ == "__main__":
    unittest.main()
