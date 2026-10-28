import json
import os
import tempfile
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from control.app import config, operations
from control.app import state as app_state
from control.app.routers import logs as logs_routes

try:
    from control.app import main
except ImportError:
    main = None


class OperationsTests(unittest.TestCase):
    def test_engine_sources_do_not_log_authentication_secrets(self):
        root = Path(__file__).resolve().parents[1]
        sources = "\n".join(
            (root / name).read_text(errors="replace")
            for name in ("engine/swu_ike.py", "engine/ami_usim.py",
                         "control/app/lpa.py")
        )
        forbidden = (
            "print('CK'", "print('IK'", "print('MSK'", "print('EMSK'",
            "print('KENCR'", "print('KAUT'", "DIFFIE-HELLMAN KEY",
            "IKEv2 DECRYPTION TABLE INFO", "ESP SA INFO (wireshark)",
            "AuthResponse sent: RES=",
            "print(a.get_imsi())", "device identity set: IMEI=",
            'AT <-- %s" % response.hex()',
            '" ".join(cmd)', 'lpac non-json stdout: %s',
        )
        self.assertEqual([item for item in forbidden if item in sources], [])

    def test_redaction_removes_identities_credentials_and_key_material(self):
        value = operations.redact({
            "pin": "1234",
            "nested": {"token": "secret"},
            "note": "call +441234567890",
            "subscription_url": "https://example.test/sub?token=secret",
            "headers_json": '{"Authorization":"Bearer secret"}',
            "activation_code": "LPA:1$smdp.example$MATCHING-ID",
        })
        self.assertEqual(value["pin"], "<redacted>")
        self.assertEqual(value["nested"]["token"], "<redacted>")
        self.assertNotIn("441234567890", value["note"])
        self.assertTrue(all("secret" not in str(value[key]).lower()
                            for key in ("subscription_url", "headers_json")))
        self.assertEqual(value["activation_code"], "<redacted>")
        log = operations.redact_log(
            "IKEv2 DECRYPTION TABLE INFO (Wireshark):\n"
            "aabbccddeeff00112233445566778899\n"
            "00112233445566778899aabbccddeeff\n"
            "CK=00112233445566778899aabbccddeeff\nnormal"
        )
        self.assertNotIn("001122", log)
        self.assertTrue(log.endswith("normal"))

    def test_redaction_preserves_closed_identity_health_fields(self):
        value = operations.redact({
            "imei_valid": True, "iccid_valid": False, "imei_source_matches": True,
            "imei": "123456789012345", "iccid": "8944000000000000000",
            "nested": {"imei_valid": "not-a-boolean-secret"},
        })
        self.assertEqual(value["imei_valid"], True)
        self.assertEqual(value["iccid_valid"], False)
        self.assertEqual(value["imei_source_matches"], True)
        self.assertEqual(value["imei"], "<redacted>")
        self.assertEqual(value["iccid"], "<redacted>")
        self.assertEqual(value["nested"]["imei_valid"], "<redacted>")

    def test_redaction_preserves_registration_failure_evidence(self):
        # The classified reg-failure verdict is digits and enum words only; losing it would
        # re-open the #33 gap where a bundle could not say WHY registration was rejected.
        value = operations.redact({
            "registration_evidence": {"kind": "rejected", "sip_status": 403},
            "sip_status": 403,
        })
        self.assertEqual(value["registration_evidence"],
                         {"kind": "rejected", "sip_status": 403})
        self.assertEqual(value["sip_status"], 403)

    def test_redaction_preserves_non_secret_eap_aka_diagnostics(self):
        diagnostic = (
            "IKE_AUTH rejected with AUTHENTICATION_FAILED before any EAP-AKA challenge "
            "(SIM not queried); the SIM may not be provisioned for VoWiFi"
        )
        self.assertEqual(operations.redact_log(diagnostic), diagnostic)

    def test_redaction_removes_proxy_labels_nodes_and_host_addresses_by_path(self):
        document = {
            "proxy": {
                "profiles": {"private-provider": {
                    "name": "My private provider", "type": "node",
                    "value": "vless://opaque-share-value",
                }},
                "exits": {"gb": {"profile_id": "private-provider",
                                   "pinned_node": "Residential London"}},
            },
            "egress": {"node": "Residential London",
                       "candidates": ["Residential London", "Backup London"],
                       "ready": True},
            "host": {"network": {"addresses": [
                {"interface": "eth0", "family": "ipv4", "address": "192.0.2.44/24"}
            ]}},
        }

        redacted = operations.redact(document)
        encoded = json.dumps(redacted)

        for private in ("private-provider", "My private provider", "opaque-share-value",
                        "Residential London", "Backup London", "192.0.2.44"):
            self.assertNotIn(private, encoded)
        self.assertTrue(redacted["egress"]["ready"])
        self.assertEqual(list(redacted["proxy"]["profiles"]), ["profile-1"])

    def test_apdu_trace_fallback_does_not_repeat_failed_unpack(self):
        source = (Path(__file__).resolve().parents[1] / "engine/swu_ike.py").read_text(
            errors="replace")
        self.assertNotIn("_data, _sw1, _sw2 = res", source)
        self.assertIn("unexpected response type=%s", source)

    def test_local_backup_is_not_exposed_as_file_contents(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            Path(temp, "config.yaml").write_text("settings: {}\ninstances: {}\n")
            result = operations.create_local_backup("Test Gateway")
            self.assertEqual(result["location"], "gateway-local")
            self.assertNotIn("path", result)
            self.assertTrue(Path(temp, "backups", result["name"]).is_file())

    def test_local_backup_can_be_deleted_by_its_listed_name(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            Path(temp, "config.yaml").write_text("settings: {}\ninstances: {}\n")
            created = operations.create_local_backup("Test Gateway")
            result = operations.delete_local_backup(created["name"])
            self.assertTrue(result["ok"])
            self.assertFalse(Path(temp, "backups", created["name"]).exists())

    def test_local_backup_delete_rejects_paths_and_non_backups(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            outside = Path(temp, "keep.txt")
            outside.write_text("keep")
            for name in ("../keep.txt", "/tmp/keep.txt", "not-a-backup.txt"):
                with self.assertRaises(ValueError):
                    operations.delete_local_backup(name)
            self.assertEqual(outside.read_text(), "keep")

    def test_support_bundle_contains_only_redacted_documents(self):
        settings_value = {
            "telegram": {"bot_token": "secret"},
            "proxy": {"subscription_url": "https://example.test/sub?token=url-secret"},
            "webhook": {"headers_json": '{"Authorization":"Bearer header-secret"}'},
            "feishu": {
                "url": "https://open.feishu.cn/open-apis/bot/v2/hook/private-token",
                "secret": "feishu-signing-secret",
                "channels": [{
                    "id": "ops", "name": "Operations",
                    "url": "https://open.feishu.cn/open-apis/bot/v2/hook/second-private-token",
                    "secret": "second-feishu-secret", "instances": ["1"],
                }],
            },
        }
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), patch.object(
                config, "get_settings", return_value=settings_value):
            run = Path(temp, "instances", "sim1", "run")
            run.mkdir(parents=True)
            run.joinpath("charon.log").write_text(
                "ESP SA INFO (wireshark):\nsecret-table-row-1\nsecret-table-row-2\n"
                "CK=00112233445566778899aabbccddeeff\n")
            content = operations.support_bundle({"imei": "123456789012345"})
            with zipfile.ZipFile(BytesIO(content)) as archive:
                settings = archive.read("settings-redacted.yaml").decode()
                status = json.loads(archive.read("status-redacted.json"))
                log = archive.read("logs/sim1-charon.log").decode()
            self.assertNotIn("feishu-signing-secret", settings)
            self.assertNotIn("private-token", settings)
            self.assertNotIn("second-private-token", settings)
            self.assertNotIn("second-feishu-secret", settings)
            self.assertNotIn("Operations", settings)
            self.assertNotIn("header-secret", settings)
            self.assertNotIn("url-secret", settings)
            self.assertNotIn("001122", log)
            self.assertEqual(status["imei"], "<redacted>")

    def test_support_bundle_can_settle_a_service_code_verdict(self):
        """A user reporting "the carrier does not support this code" must be checkable.

        The verdict comes from the Q.850 cause on call_result, which lived only in
        events.jsonl and was not collected at all — the bundle showed the conclusion and
        never the evidence behind it.
        """
        events = "\n".join(json.dumps(r) for r in [
            {"instance": "sim1", "event": "call_out", "args": ["*#21#"]},
            {"instance": "sim1", "event": "call_result",
             "args": ["out", "*#21#", "CHANUNAVAIL", "79"]},
        ])
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "get_settings", return_value={}):
            logs = Path(temp, "instances", "sim1", "logs")
            logs.mkdir(parents=True)
            logs.joinpath("events.jsonl").write_text(events + "\n")
            with zipfile.ZipFile(BytesIO(operations.support_bundle({}))) as archive:
                captured = archive.read("logs/sim1-call-events.jsonl").decode()

        self.assertIn("*#21#", captured)     # which code was dialled
        self.assertIn("79", captured)        # and what the carrier answered

    def test_support_bundle_keeps_dialled_numbers_and_replies_out(self):
        """The evidence must not smuggle in what redaction elsewhere is careful to remove.

        A subscriber's number and a reply's text sit inside an `args` array, where the
        key-name redaction rules never reach them.
        """
        events = "\n".join(json.dumps(r) for r in [
            {"instance": "sim1", "event": "call_result",
             "args": ["out", "+8615001007220", "CANCEL", "127"]},
            {"instance": "sim1", "event": "call_out", "args": ["#225#"]},
            # Written by an older engine; USSD is not part of this build.
            {"instance": "sim1", "event": "ussd", "args": ["#225#", "WW91ciBiYWxhbmNl"]},
            {"instance": "sim1", "event": "sms_in", "args": ["6700", "cHJpdmF0ZQ=="]},
        ])
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "get_settings", return_value={}):
            logs = Path(temp, "instances", "sim1", "logs")
            logs.mkdir(parents=True)
            logs.joinpath("events.jsonl").write_text(events + "\n")
            with zipfile.ZipFile(BytesIO(operations.support_bundle({}))) as archive:
                captured = archive.read("logs/sim1-call-events.jsonl").decode()

        self.assertNotIn("8615001007220", captured)   # a dialled number
        self.assertNotIn("WW91ciBiYWxhbmNl", captured)  # the reply's own text
        self.assertNotIn("cHJpdmF0ZQ==", captured)      # an SMS body
        self.assertIn("<number>", captured)
        self.assertIn("#225#", captured)              # the code itself still diagnosable
        self.assertNotIn("sms_in", captured)          # message events are not call evidence
        self.assertNotIn("ussd", captured)

    def test_support_bundle_keeps_the_hangup_side_but_nothing_else_in_its_slot(self):
        events = "\n".join(json.dumps(r) for r in [
            {"instance": "sim1", "event": "call_result",
             "args": ["out", "+447700900123", "ANSWER", "16", "carrier"]},
            {"instance": "sim1", "event": "call_result",
             "args": ["out", "+447700900123", "ANSWER", "16", "+447700900999"]},
        ])
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "get_settings", return_value={}):
            logs = Path(temp, "instances", "sim1", "logs")
            logs.mkdir(parents=True)
            logs.joinpath("events.jsonl").write_text(events + "\n")
            with zipfile.ZipFile(BytesIO(operations.support_bundle({}))) as archive:
                captured = [json.loads(l) for l in
                            archive.read("logs/sim1-call-events.jsonl").decode().splitlines()]
        self.assertEqual(captured[0]["args"], ["out", "<number>", "ANSWER", "16", "carrier"])
        # The slot is a closed vocabulary; anything else is not let through as evidence.
        self.assertEqual(captured[1]["args"][4], "<unknown>")

    def test_support_bundle_carries_asterisk_problems_without_the_identity(self):
        """An answered call that drops at once is explained only by Asterisk's own warnings.

        `messages` also names the IMS public identity on every registration, so only the
        WARNING/ERROR lines are taken, and every SIP/tel user part is removed from them.
        """
        messages = "\n".join([
            "[Sep 21 22:05:00] NOTICE[100] res_pjsip_outbound_registration.c: Registered "
            "'sip:234870000000001@ims.mnc087.mcc234.3gppnetwork.org'",
            "[Sep 21 22:06:43] WARNING[2987][C-00000009] res_pjsip_sdp_rtp.c: No joint "
            "capabilities for 'audio' from <sip:+447700900123@ims.example;user=phone>",
            "[Sep 21 22:06:43] ERROR[2987][C-00000009] app_stack.c:390 return_exec: "
            "Return without Gosub: stack is unallocated",
            "[Sep 21 22:06:44] WARNING[2988] chan_pjsip.c: peer tel:+447700900456",
        ])
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "get_settings", return_value={}):
            logs = Path(temp, "instances", "sim1", "logs", "asterisk")
            logs.mkdir(parents=True)
            logs.joinpath("messages").write_text(messages + "\n")
            logs.joinpath("full").write_text("[Sep 21] WARNING[1] full-log-must-stay-out\n")
            with zipfile.ZipFile(BytesIO(operations.support_bundle({}))) as archive:
                names = archive.namelist()
                captured = archive.read("logs/sim1-asterisk-problems.log").decode()
                manifest = json.loads(archive.read("manifest.json"))

        self.assertIn("No joint capabilities", captured)          # the evidence itself
        self.assertIn("[C-00000009]", captured)                   # still tied to its call
        self.assertIn("Return without Gosub", captured)
        self.assertNotIn("Registered", captured)                  # NOTICE lines stay out
        self.assertNotIn("234870000000001", captured)
        self.assertNotIn("447700900123", captured)
        self.assertNotIn("447700900456", captured)
        self.assertIn("sip:<user>@ims.example", captured)
        self.assertIn("tel:<number>", captured)
        self.assertNotIn("full-log-must-stay-out", "".join(names) + captured)
        self.assertEqual(manifest["files"]["logs/sim1-asterisk-problems.log"]
                         ["filtered_lines"], 1)

    def test_support_bundle_carries_the_host_view_the_control_plane_cannot_observe(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            orchestrator = Path(temp, "orchestrator")
            orchestrator.mkdir(parents=True)
            orchestrator.joinpath("host-diagnostics.json").write_text(json.dumps({
                "virtualization": "lxc",
                "modemmanager": {"unit_active": False,
                                 "unclaimed": {"unclaimed_ttys": ["/dev/ttyUSB2"]}},
                "recent_log": ["2026-08-14 12:09:16 waiting for ModemManager to claim "
                               "/dev/ttyUSB2"],
                "imei": "123456789012345",
            }))

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                host = json.loads(archive.read("host-diagnostics-redacted.json"))
            self.assertTrue(host["available"])
            self.assertEqual(host["virtualization"], "lxc")
            self.assertFalse(host["modemmanager"]["unit_active"])
            self.assertEqual(host["modemmanager"]["unclaimed"]["unclaimed_ttys"],
                             ["/dev/ttyUSB2"])
            self.assertIn("waiting for ModemManager", host["recent_log"][0])
            self.assertEqual(host["imei"], "<redacted>")

    def test_support_bundle_host_view_carries_bridge_activity_and_port_truth(self):
        """The three facts every card-path report needed by hand: what command each bridge
        runs, what it printed last, and whether pcscd actually listens on its ports."""
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            orchestrator = Path(temp, "orchestrator")
            orchestrator.mkdir(parents=True)
            orchestrator.joinpath("host-diagnostics.json").write_text(json.dumps({
                "modem_backend": "serial",
                "modemmanager": {"unit_active": False},
                "bridges": {"a": {"pid": 7, "running": True,
                                  "command": ["python", "bridge", "--modem", "/dev/ttyUSB2"],
                                  "log_tail": ["[bridge] allocated logical channels [1, 2, 3]"]}},
                "vpcd_ports_listening": {"a": {"15360": True, "15361": True, "15362": False}},
                "reader_definitions": ["libccidtwin", "mdd-sim-gateway-modems"],
            }))

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                host = json.loads(archive.read("host-diagnostics-redacted.json"))
            self.assertEqual(host["modem_backend"], "serial")
            self.assertIn("--modem", host["bridges"]["a"]["command"])
            self.assertIn("allocated logical channels",
                          host["bridges"]["a"]["log_tail"][0])
            self.assertFalse(host["vpcd_ports_listening"]["a"]["15362"])
            self.assertIn("mdd-sim-gateway-modems", host["reader_definitions"])

    def test_support_bundle_reports_a_missing_host_view_instead_of_omitting_it(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            content = operations.support_bundle({})
            with zipfile.ZipFile(BytesIO(content)) as archive:
                host = json.loads(archive.read("host-diagnostics-redacted.json"))
        # Silence here would read as a healthy host; a stopped orchestrator is a finding.
        self.assertFalse(host["available"])
        self.assertIn("orchestrator", host["note"])

    def test_support_bundle_includes_retained_ike_segment_tail(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            archive_dir = Path(temp) / "instances" / "sim1" / "logs" / "ike"
            archive_dir.mkdir(parents=True)
            archive_dir.joinpath("charon-20260807-110000.log").write_text(
                "[2026-08-07 11:00:00+0800] STATE 1\n"
                "[2026-08-07 11:00:01+0800] STATE 2\n")

            content = operations.support_bundle({}, log_lines=50)

            with zipfile.ZipFile(BytesIO(content)) as bundle:
                retained = bundle.read(
                    "logs/sim1-charon-20260807-110000.log").decode()
            self.assertIn("STATE 2", retained)

    def test_support_bundle_indexes_coverage_and_keeps_ike_head_and_tail(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "get_settings", return_value={}):
            logs = Path(temp, "instances", "sim1", "logs")
            archive_dir = logs / "ike"
            archive_dir.mkdir(parents=True)
            lifecycle = [
                {"ts": 100 + i, "instance": "sim1", "event": "recovery_started"}
                for i in range(3)]
            logs.joinpath("lifecycle.jsonl").write_text(
                "\n".join(json.dumps(item) for item in lifecycle) + "\n")
            ike = archive_dir / "charon-20260830-100000.log"
            ike.write_text("\n".join(f"line-{i:02d}" for i in range(60)) + "\n")

            with zipfile.ZipFile(BytesIO(operations.support_bundle({}, log_lines=50))) as bundle:
                retained = bundle.read("logs/sim1-charon-20260830-100000.log").decode()
                manifest = json.loads(bundle.read("manifest.json"))

        self.assertIn("line-00", retained)
        self.assertIn("line-59", retained)
        self.assertNotIn("line-15", retained)
        ike_meta = manifest["files"]["logs/sim1-charon-20260830-100000.log"]
        self.assertEqual(ike_meta["raw_lines"], 60)
        self.assertEqual(ike_meta["included_lines"], 50)
        self.assertTrue(ike_meta["truncated"])
        life_meta = manifest["files"]["logs/sim1-lifecycle.jsonl"]
        self.assertEqual((life_meta["first_ts"], life_meta["last_ts"]), (100, 102))

    def test_lifecycle_file_is_redacted_even_if_a_bad_record_reaches_disk(self):
        record = {
            "ts": 100, "instance": "sim1", "event": "recovery_failed",
            "reason_code": "engine_start_failed", "imei": "123456789012345",
            "iccid": "8944000000000000000", "error": "https://secret.example/token",
        }
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "get_settings", return_value={}):
            logs = Path(temp, "instances", "sim1", "logs")
            logs.mkdir(parents=True)
            logs.joinpath("lifecycle.jsonl").write_text(json.dumps(record) + "\n")
            with zipfile.ZipFile(BytesIO(operations.support_bundle({}))) as bundle:
                captured = bundle.read("logs/sim1-lifecycle.jsonl").decode()

        for secret in ("123456789012345", "8944000000000000000", "secret.example"):
            self.assertNotIn(secret, captured)
        parsed = json.loads(captured)
        self.assertEqual(parsed["reason_code"], "engine_start_failed")

    def test_support_bundle_enforces_its_archive_budget_and_reports_omissions(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "get_settings", return_value={}), \
                patch.object(operations, "SUPPORT_BUNDLE_CONTENT_BYTES", 900), \
                patch.object(operations, "SUPPORT_BUNDLE_MAX_BYTES", 4096):
            run = Path(temp, "instances", "sim1", "run")
            run.mkdir(parents=True)
            run.joinpath("charon.log").write_text(
                "\n".join(f"ordinary-line-{i}-" + "x" * 80 for i in range(100)))
            content = operations.support_bundle({})
            with zipfile.ZipFile(BytesIO(content)) as bundle:
                manifest = json.loads(bundle.read("manifest.json"))

        self.assertLessEqual(len(content), 4096)
        omitted = manifest["files"]["logs/sim1-charon.log"]
        self.assertTrue(omitted["omitted"])
        self.assertFalse(omitted["truncated"])
        self.assertEqual(omitted["included_lines"], 0)

    def test_call_event_coverage_uses_the_filtered_archived_records(self):
        records = [
            {"ts": index, "instance": "sim1", "event": "sms_in", "args": ["1", "secret"]}
            for index in range(1, 9)
        ] + [
            {"ts": 9, "instance": "sim1", "event": "call_out", "args": ["*#21#"]},
            {"ts": 10, "instance": "sim1", "event": "call_result",
             "args": ["out", "*#21#", "CHANUNAVAIL", "79"]},
        ]
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "get_settings", return_value={}):
            logs = Path(temp, "instances", "sim1", "logs")
            logs.mkdir(parents=True)
            logs.joinpath("events.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n")
            with zipfile.ZipFile(BytesIO(operations.support_bundle({}))) as bundle:
                manifest = json.loads(bundle.read("manifest.json"))

        coverage = manifest["files"]["logs/sim1-call-events.jsonl"]
        self.assertEqual(coverage["raw_lines"], 10)
        self.assertEqual(coverage["scanned_lines"], 10)
        self.assertEqual(coverage["unscanned_lines"], 0)
        self.assertEqual(coverage["eligible_lines"], 2)
        self.assertEqual(coverage["filtered_lines"], 8)
        self.assertEqual(coverage["included_lines"], 2)
        self.assertEqual((coverage["first_ts"], coverage["last_ts"]), (9, 10))
        self.assertFalse(coverage["truncated"])

    def test_call_event_scan_is_bounded_and_reports_the_unscanned_prefix(self):
        records = [
            {"ts": index, "instance": "sim1", "event": "sms_in", "args": ["1", "secret"]}
            for index in range(1, 6)
        ] + [
            {"ts": 6, "instance": "sim1", "event": "call_out", "args": ["*#21#"]},
            {"ts": 7, "instance": "sim1", "event": "call_result",
             "args": ["out", "*#21#", "CHANUNAVAIL", "79"]},
        ]
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "get_settings", return_value={}), \
                patch.object(operations, "CALL_EVENT_SCAN_LINES", 3):
            logs = Path(temp, "instances", "sim1", "logs")
            logs.mkdir(parents=True)
            logs.joinpath("events.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n")
            with zipfile.ZipFile(BytesIO(operations.support_bundle({}))) as bundle:
                manifest = json.loads(bundle.read("manifest.json"))

        coverage = manifest["files"]["logs/sim1-call-events.jsonl"]
        self.assertEqual(coverage["raw_lines"], 7)
        self.assertEqual(coverage["scanned_lines"], 3)
        self.assertEqual(coverage["unscanned_lines"], 4)
        self.assertEqual(coverage["eligible_lines"], 2)
        self.assertEqual(coverage["filtered_lines"], 1)
        self.assertTrue(coverage["truncated"])

    def test_diagnostic_records_survive_redaction_instead_of_being_blanked(self):
        # engine.capture_diagnostics embeds the tunnel log tail in each record, and swu_ike
        # prints "received decoded message" on every fragmented exchange. Under the line
        # rules that one phrase blanked the record and, via the two-line lookahead, the two
        # records after it — so every record of every bundle came out empty.
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            logs = Path(temp) / "instances" / "sim1" / "logs"
            logs.mkdir(parents=True)
            records = [
                {"ts": 1786894085, "reason": "health-freeze:reg_unanswered",
                 "registration": "unregistered", "pcscf": "fd00:976a:2:147::5",
                 "charon": {"retransmits": 3, "tail": ["received decoded message",
                                                       "SK_ei: 0011223344556677",
                                                       "tunnel CONNECTED"]},
                 "sip": ["SIP/2.0 401 Unauthorized"]},
                {"ts": 1786894200, "reason": "health-freeze:registering",
                 "registration": "unregistered", "charon": {"tail": ["STATE 4"]},
                 "sip": []},
                {"ts": 1786894300, "reason": "health-freeze:registering",
                 "registration": "unregistered", "charon": {"tail": ["STATE 4"]},
                 "sip": []},
            ]
            logs.joinpath("diagnostics.jsonl").write_text(
                "\n".join(json.dumps(item) for item in records) + "\n")

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                text = archive.read("logs/sim1-diagnostics.jsonl").decode()
        parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual(len(parsed), 3)
        # The evidence that names the failure is what has to survive.
        self.assertEqual(parsed[0]["registration"], "unregistered")
        self.assertEqual(parsed[0]["reason"], "health-freeze:reg_unanswered")
        self.assertEqual(parsed[0]["sip"], ["SIP/2.0 401 Unauthorized"])
        self.assertEqual(parsed[0]["charon"]["retransmits"], 3)
        # Only the offending tail lines go, and the lookahead never reaches later records.
        tail = parsed[0]["charon"]["tail"]
        self.assertEqual(tail[0], "<redacted cryptographic material>")
        self.assertEqual(tail[1], "<redacted cryptographic material>")
        self.assertEqual(tail[2], "tunnel CONNECTED")
        self.assertEqual(parsed[1]["charon"]["tail"], ["STATE 4"])
        self.assertEqual(parsed[2]["charon"]["tail"], ["STATE 4"])

    def test_diagnostic_redaction_still_removes_identifiers_and_bad_records(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            logs = Path(temp) / "instances" / "sim1" / "logs"
            logs.mkdir(parents=True)
            logs.joinpath("diagnostics.jsonl").write_text(
                json.dumps({"usim": {"imsi": "001010123456789", "iccid": "8944000000000000000"},
                            "note": "peer 0011223344556677889900aabbccddeeff"}) + "\n"
                "not json at all: SKEYSEED: 00112233445566778899\n")

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                text = archive.read("logs/sim1-diagnostics.jsonl").decode()
        self.assertNotIn("001010123456789", text)
        self.assertNotIn("8944000000000000000", text)
        self.assertNotIn("0011223344556677889900aabbccddeeff", text)
        # A line that will not parse must not become a hole in the redaction.
        self.assertNotIn("00112233445566778899", text)
        self.assertIn("<redacted cryptographic material>", text.splitlines()[-1])

    def test_diagnostic_redaction_removes_issue_21_support_bundle_leaks(self):
        record = {
            "reason": "health-freeze:reg_rejected",
            "egress": {"node": "Private exit name", "selection": "auto", "ready": True},
            "host": {"alerts": [], "network": {"addresses": [
                {"interface": "eth0", "family": "ipv4", "address": "198.51.100.8/24"}
            ]}},
        }

        parsed = json.loads(operations.redact_jsonl(json.dumps(record)))

        self.assertEqual(parsed["egress"]["node"], "<redacted>")
        self.assertEqual(parsed["host"]["network"]["addresses"][0]["address"], "<redacted>")
        self.assertEqual(parsed["reason"], "health-freeze:reg_rejected")
        self.assertTrue(parsed["egress"]["ready"])

    def test_plain_logs_keep_the_strict_whole_line_rules(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            run = Path(temp) / "instances" / "sim1" / "run"
            run.mkdir(parents=True)
            run.joinpath("charon.log").write_text(
                "IKEv2 DECRYPTION TABLE\n"
                "0011223344556677\n"
                "8899aabbccddeeff\n"
                "tunnel CONNECTED\n")

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                lines = archive.read("logs/sim1-charon.log").decode().splitlines()
        self.assertEqual(lines[:3], ["<redacted cryptographic material>"] * 3)
        self.assertEqual(lines[3], "tunnel CONNECTED")

    def test_sensitive_config_files_are_owner_only(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            config.save({"settings": {}, "instances": {}, "internal": {}})
            self.assertEqual(os.stat(config.CONFIG_PATH).st_mode & 0o777, 0o600)
            inst = {
                "id": "sim1", "imsi": "001010123456789", "mcc": "001", "mnc": "01",
                "ami_secret": "random-ami-secret",
                "sip": {"webrtc": {"enable": True, "password": "random-web-secret"}},
            }
            path = config.write_instance_json(inst, {})
            # The engine runs as root in group mdd without CAP_DAC_OVERRIDE (SLIM_PLAN 12.3):
            # it reaches its line only through the group, and nothing is open to others.
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
            self.assertEqual(os.stat(temp).st_mode & 0o777, 0o750)
            self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o770)

    def test_engine_configuration_fails_closed_without_generated_credentials(self):
        inst = {"id": "sim1", "imsi": "001010123456789", "mcc": "001", "mnc": "01"}
        with self.assertRaisesRegex(ValueError, "AMI credential"):
            config.render_instance_json(inst, {})
        inst["ami_secret"] = "random-ami-secret"
        with self.assertRaisesRegex(ValueError, "WebRTC credential"):
            config.render_instance_json(inst, {})


@unittest.skipIf(main is None, "manager runtime dependencies are unavailable")
class LineDiagnosticsTests(unittest.TestCase):
    """The per-line verdict the bundle carries beside the logs."""

    def setUp(self):
        app_state.hub.status_cache.clear()
        app_state.hub.health.clear()
        app_state.hub.ok_since.clear()

    def test_summary_names_the_reason_and_how_far_the_retry_budget_has_run(self):
        app_state.hub.status_cache["1"] = {
            "state": "REGISTERING", "reason_code": "reg_unanswered",
            "reason": "The carrier's IMS stopped answering registration.",
            "retry": {"count": 2, "max": 3},
            "detail": {"registration": "unregistered", "active_channels": 0},
        }
        app_state.hub.health["1"] = {"fail_start": time.monotonic() - 61.0, "frozen_code": None}

        entry = logs_routes._line_diagnostics({"id": "1", "name": "T-MOBILE"})

        self.assertEqual(entry["reason_code"], "reg_unanswered")
        self.assertEqual(entry["registration"], "unregistered")
        self.assertEqual(entry["active_channels"], 0)
        self.assertEqual(entry["retry"], {"count": 2, "max": 3})
        # Monotonic readings mean nothing to a reader; their distance from now is the story.
        self.assertGreaterEqual(entry["failing_for_seconds"], 61.0)
        self.assertIsNone(entry["ok_for_seconds"])

    def test_summary_carries_no_subscriber_identifiers(self):
        app_state.hub.status_cache["1"] = {
            "state": "OK", "reason_code": "ok", "reason": "", "retry": {},
            "detail": {"registration": "registered", "msisdn": "+12025550123",
                       "imsi": "001010123456789"},
        }

        entry = operations.redact(logs_routes._line_diagnostics({"id": "1", "name": "T-MOBILE"}))

        self.assertNotIn("+12025550123", json.dumps(entry))
        self.assertNotIn("001010123456789", json.dumps(entry))
        self.assertEqual(entry["registration"], "registered")

    def test_a_line_the_sampler_has_not_reached_says_so(self):
        entry = logs_routes._line_diagnostics({"id": "9", "name": "NEW"})
        # An absent verdict must not read as a healthy one.
        self.assertNotIn("state", entry)
        self.assertIn("no status sampled", entry["note"])


class ServiceRestartTests(unittest.TestCase):
    """The control plane states the intent; the root orchestrator carries it out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DATA_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = Path(self.tmp.name, "orchestrator")

    def test_a_request_is_published_privately_for_each_supported_scope(self):
        for scope in operations.SERVICE_RESTART_SCOPES:
            self.assertTrue(operations.request_service_restart(scope)["ok"])
            request = json.loads((self.root / "service-restart-request.json").read_text())
            self.assertEqual(request["scope"], scope)
            # The status is reset with the request so the previous restart's outcome cannot be
            # read as this one's while the orchestrator is still picking it up.
            status = json.loads((self.root / "service-restart-status.json").read_text())
            self.assertEqual(status, {"state": "requested", "scope": scope,
                                      "updated_at": request["requested_at"]})
            self.assertEqual(
                (self.root / "service-restart-request.json").stat().st_mode & 0o777, 0o600)

    def test_an_unknown_scope_publishes_nothing(self):
        result = operations.request_service_restart("reformat")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "restart.error.invalid_scope")
        self.assertFalse((self.root / "service-restart-request.json").exists())

    def test_a_request_nothing_consumes_is_reported_instead_of_waited_on(self):
        operations.request_service_restart("control")
        self.assertEqual(operations.service_restart_status()["state"], "requested")
        request_path = self.root / "service-restart-request.json"
        request_path.write_text(json.dumps({"scope": "control",
                                            "requested_at": int(time.time()) - 300}))
        status = operations.service_restart_status()
        self.assertEqual(status["state"], "stalled")
        self.assertEqual(status["error_code"], "restart.error.not_picked_up")

    def test_no_request_and_no_history_reads_as_idle(self):
        self.assertEqual(operations.service_restart_status()["state"], "idle")

    def test_host_reboot_is_always_requested_from_the_orchestrator(self):
        with patch.dict(os.environ, {"MDD_CONTAINER_STACK": "1"}):
            result = operations.request_service_restart("host")
        self.assertTrue(result["ok"])
        self.assertTrue((self.root / "service-restart-request.json").exists())

    def test_no_container_executor_is_left(self):
        for name in ("container_stack_enabled", "perform_container_service_restart",
                     "settle_container_service_restart", "prune_old_mdd_images",
                     "prune_dangling_build_cache"):
            self.assertFalse(hasattr(operations, name), name)


if __name__ == "__main__":
    unittest.main()
