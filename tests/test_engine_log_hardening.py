import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from control.app import config


class ProductionDebugTests(unittest.TestCase):
    def _paths(self, temp):
        return patch.multiple(
            config, DATA_DIR=temp, CONFIG_PATH=os.path.join(temp, "config.yaml"))

    def test_legacy_saved_asterisk_debug_is_disabled_on_load(self):
        with tempfile.TemporaryDirectory() as temp, self._paths(temp):
            with open(config.CONFIG_PATH, "w", encoding="utf-8") as handle:
                yaml.safe_dump({
                    "settings": {"debug": {"asterisk": True, "charon": True}},
                    "instances": {"1": {
                        "id": "1", "debug": {"asterisk": True, "charon": True},
                    }},
                }, handle)

            loaded = config.load()

            self.assertFalse(loaded["instances"]["1"]["debug"]["asterisk"])
            self.assertTrue(loaded["instances"]["1"]["debug"]["charon"])

    def test_upsert_cannot_persist_asterisk_debug(self):
        with tempfile.TemporaryDirectory() as temp, self._paths(temp), \
                patch.object(config, "alloc_ports_auto", return_value=config._alloc_ports(0)):
            saved = config.upsert_instance({
                "id": "1", "debug": {"asterisk": True, "charon": True},
            })

            self.assertFalse(saved["debug"]["asterisk"])
            self.assertTrue(saved["debug"]["charon"])

    def test_engine_contract_forces_debug_off_even_for_imported_config(self):
        inst = {
            "id": "1", "imsi": "001010000000001", "mcc": "001", "mnc": "01",
            "imei": "123456789012345", "ami_secret": "secret",
            "sip": {"webrtc": {"password": "password"}},
            "debug": {"asterisk": True, "charon": True},
        }
        settings = {**config.DEFAULTS["settings"],
                    "debug": {"asterisk": True, "charon": False}}
        with tempfile.TemporaryDirectory() as temp, self._paths(temp):
            rendered = config.render_instance_json(inst, settings)

        self.assertFalse(rendered["debug"]["asterisk"])
        self.assertTrue(rendered["debug"]["charon"])

    def test_tls_domain_is_not_used_as_an_ice_host_candidate(self):
        settings = {**config.DEFAULTS["settings"],
                    "tls": {"domain": "gateway.example.test"}}
        with patch.dict(os.environ, {"MDD_ADVERTISE_ADDR": "192.0.2.25"}, clear=False):
            self.assertEqual(config.advertise_address(settings), "gateway.example.test")
            self.assertEqual(config.ice_advertise_address(settings), "192.0.2.25")

    def test_non_ip_ice_override_falls_back_to_detected_lan_ip(self):
        settings = {**config.DEFAULTS["settings"], "advertise_address": "not-an-ip"}
        with patch.dict(os.environ, {"MDD_ADVERTISE_ADDR": "also-not-an-ip"}, clear=False), \
                patch.object(config, "_host_lan_ipv4", return_value="198.51.100.8"):
            self.assertEqual(config.ice_advertise_address(settings), "198.51.100.8")


class AsteriskModulePolicyTests(unittest.TestCase):
    def test_unused_error_generating_modules_are_excluded(self):
        root = Path(__file__).resolve().parent.parent
        policy = (root / "engine" / "templates" / "modules.conf.j2").read_text()

        self.assertIn("autoload = yes", policy)
        for module in (
                "app_adsiprog.so", "app_getcpeid.so", "codec_vevs.so", "res_adsi.so",
                "res_ari.so", "res_config_ldap.so",
                "res_odbc.so", "res_phoneprov.so", "res_pjsip_config_wizard.so"):
            self.assertIn(f"noload => {module}", policy)
        for required in ("chan_pjsip.so", "codec_amr.so", "res_http_websocket.so",
                         "res_pjsip_messaging.so", "res_rtp_asterisk.so"):
            self.assertNotIn(f"noload => {required}", policy)

    def test_catch_all_dialplan_avoids_bare_dot_wildcard(self):
        root = Path(__file__).resolve().parent.parent
        dialplan = (root / "engine" / "templates" / "extensions.conf.j2").read_text()

        self.assertIn("{% set any_extension = '_[!-~]!' %}", dialplan)
        self.assertNotIn("exten => _.,", dialplan)
        self.assertEqual(dialplan.count("exten => {{ any_extension }},1"), 2)

    def test_private_resolve_fields_use_nodoc_registration(self):
        root = Path(__file__).resolve().parent.parent
        patcher = (root / "engine" / "patches" / "asterisk" /
                   "resolve_config_docs.py").read_text()

        self.assertIn("ast_sorcery_object_field_register_nodoc", patcher)
        self.assertIn("expected 3 resolve field registrations", patcher)

    def test_missing_security_server_reauth_keeps_established_sas(self):
        root = Path(__file__).resolve().parent.parent
        patcher = (root / "engine" / "patches" / "asterisk" /
                   "reauth_missing_security_server.py").read_text()

        self.assertIn("handle_volte_unauthorized", patcher)
        self.assertIn("transport_state->volte.registered", patcher)
        self.assertIn("VOLTE_STATE_RESPONSE", patcher)
        # The fallback must only swallow an ABSENT header; parse failures stay fatal.
        self.assertIn("pjsip_msg_find_hdr_by_name", patcher)

    def test_swu_workers_keep_fork_semantics_on_python_314(self):
        root = Path(__file__).resolve().parent.parent
        swu = (root / "engine" / "swu_ike.py").read_text()

        self.assertIn('multiprocessing.set_start_method("fork", force=True)', swu)


class RegisteredIdentityLogTests(unittest.TestCase):
    """Reading a line's phone number must not require SIP tracing or an extra REGISTER.

    The packet logger writes authentication headers to the container log, and the forced
    re-registration it existed to produce is answered with 503 by some IMS cores (issue #8),
    which Asterisk reports as a rejected registration and the health policy acts on. The
    patched engine announces the identity of the registrations it makes anyway.
    """

    PATCHER = (Path(__file__).resolve().parent.parent / "engine" / "patches" / "asterisk"
               / "ims_public_identity_log.py")

    PROLOGUE = (
        "static int store_volte_p_associated_uri(struct registration_response *response)\n"
        "{\n"
        "\tstruct ast_sip_transport_state *transport_state = NULL;\n"
        "\tint ret = -1;\n"
        "\n"
    )
    REST = (
        "\tif (get_endpoint_transport_transport_state(response->client_state, NULL, NULL, "
        "&transport_state))\n"
        "\t\tgoto out;\n"
        "out:\n"
        "\treturn ret;\n"
        "}\n"
    )

    def _apply(self, source):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "res" / "res_pjsip_outbound_registration.c"
            target.parent.mkdir(parents=True)
            target.write_text(source)
            first = subprocess.run([sys.executable, str(self.PATCHER)],
                                   env={**os.environ, "AST_SRC": temp},
                                   capture_output=True, text=True)
            patched = target.read_text() if target.exists() else source
            second = subprocess.run([sys.executable, str(self.PATCHER)],
                                    env={**os.environ, "AST_SRC": temp},
                                    capture_output=True, text=True)
            return first, patched, target.read_text() if target.exists() else source, second

    def test_the_registered_identity_is_logged_on_the_ordinary_path(self):
        first, patched, twice, second = self._apply(self.PROLOGUE + self.REST)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('ast_log(LOG_NOTICE, "IMS public identity: %s', patched)
        # Every associated identity, not just the first: carriers list the IMSI-derived IMPU
        # ahead of the dialable number.
        self.assertIn("pjsip_msg_find_hdr_by_name", patched)
        self.assertIn("pau ? pau->next : NULL", patched)
        # Nothing may be sent: the log line is emitted from the response handler itself.
        self.assertNotIn("pjsip_endpt_send_request", patched)
        # Re-running the build must not stack a second copy.
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(twice, patched)

    def test_an_upstream_refactor_fails_the_build_instead_of_being_skipped(self):
        first, _patched, _twice, _second = self._apply(
            "static int store_volte_p_associated_uri(void)\n{\n\treturn -1;\n}\n")

        self.assertEqual(first.returncode, 1)
        self.assertIn("prologue not found", first.stderr)


if __name__ == "__main__":
    unittest.main()
