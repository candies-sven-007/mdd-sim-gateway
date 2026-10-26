import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from control.app import config, main


class AutoProvisionTests(unittest.TestCase):
    def setUp(self):
        # Promotion asks DNS whether the carrier publishes an ePDG; never from a unit test.
        lookup = patch.object(main.vowifi_support, "lookup", return_value="error")
        lookup.start()
        self.addCleanup(lookup.stop)
        main.vowifi_support._cache.clear()
        self.draft = {
            "id": "2", "name": "234-33", "provisioning_state": "draft",
            "enabled": False, "imsi": "234330123456789", "mcc": "234", "mnc": "33",
            "iccid": "8944110000000000000", "smsc": "+447700900000",
            "debug": {"asterisk": True, "charon": False},
        }
        self.card = {
            "present": True, "index": 4, "name": "VoWiFi Modem test 00 01",
            "hardware_kind": "modem", "hardware_id": "test", "reader_port": "",
            "imsi": self.draft["imsi"], "mcc": "234", "mnc": "33",
            "iccid": self.draft["iccid"], "smsc": self.draft["smsc"],
            "pin_enabled": False,
            "virtual_slots": [
                {"index": 3, "name": "slot 0"},
                {"index": 4, "name": "slot 1"},
                {"index": 5, "name": "slot 2"},
            ],
        }


    @patch.object(main.egress, "publish")
    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_a_carrier_without_vowifi_is_provisioned_with_vowifi_off(
            self, hardware_imei, upsert, _publish):
        """A China Telecom SIM got a line that could only fail on ePDG DNS and marked a
        modem with working 4G as needing attention. Phones hide the switch instead."""
        hardware_imei.return_value = ("490154203237518", "test", "modem")
        upsert.side_effect = lambda value, **kwargs: value
        draft = {**self.draft, "imsi": "460110123456789", "mcc": "460", "mnc": "11"}
        card = {**self.card, "imsi": draft["imsi"], "mcc": "460", "mnc": "11"}

        result = main._auto_promote_card_draft(draft, card, [card])

        self.assertEqual(result["provisioning_state"], "ready")
        self.assertFalse(result["enabled"])

    @patch.object(main.egress, "publish")
    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_a_carrier_that_publishes_no_epdg_is_provisioned_with_vowifi_off(
            self, hardware_imei, upsert, _publish):
        hardware_imei.return_value = ("490154203237518", "test", "modem")
        upsert.side_effect = lambda value, **kwargs: value
        with patch.object(main.vowifi_support, "lookup", return_value="nxdomain"):
            result = main._auto_promote_card_draft(self.draft, self.card, [self.card])
        self.assertFalse(result["enabled"])

    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_incomplete_draft_stays_stopped(self, hardware_imei, upsert):
        hardware_imei.return_value = ("", "reader-test", "reader")

        result = main._auto_promote_card_draft(self.draft, self.card, [self.card])

        self.assertEqual(result["provisioning_state"], "draft")
        self.assertFalse(result["enabled"])
        self.assertIn("IMEI", result["auto_provision_missing"])
        upsert.assert_not_called()

    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_pin_locked_draft_waits_for_saved_pin(self, hardware_imei, upsert):
        hardware_imei.return_value = ("490154203237518", "test", "modem")
        self.card["pin_enabled"] = True

        result = main._auto_promote_card_draft(self.draft, self.card, [self.card])

        self.assertIn("SIM PIN", result["auto_provision_missing"])
        upsert.assert_not_called()

    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_ready_disabled_line_is_never_promoted(self, hardware_imei, upsert):
        ready = {**self.draft, "provisioning_state": "ready", "enabled": False}

        result = main._auto_promote_card_draft(ready, self.card, [self.card])

        self.assertIs(result, ready)
        hardware_imei.assert_not_called()
        upsert.assert_not_called()

    def test_giffgaff_profile_rebuilds_required_sip_identity(self):
        first = config.carrier_sip_defaults("234", "10", "test-card")
        again = config.carrier_sip_defaults("234", "010", "test-card")

        self.assertEqual(first, again)
        self.assertEqual(first["access_type"], "wlan1")
        self.assertTrue(first["user_eq_phone"])
        self.assertIn("country=GB", first["pani"])
        self.assertNotIn("ffffffffffff", first["pani"])

    def test_unknown_carrier_does_not_invent_sip_identity(self):
        self.assertEqual(config.carrier_sip_defaults("001", "01", "test-card"), {})


    def test_engine_render_uses_carrier_profile_but_keeps_explicit_overrides(self):
        base = {
            "id": "3", "index": 0, "imsi": "234100000000000",
            "mcc": "234", "mnc": "10", "iccid": "test-card",
            "imei": "490154203237518", "ami_secret": "test-secret",
            "epdg": "198.51.100.10",
            "sip": {"webrtc": {"enable": True, "password": "test-password"},
                    "access_type": "custom-access"},
        }

        rendered = config.render_instance_json(base, {})

        self.assertEqual(rendered["sip"]["access_type"], "custom-access")
        self.assertTrue(rendered["sip"]["user_eq_phone"])
        self.assertIn("country=GB", rendered["sip"]["pani"])
        self.assertEqual(rendered["epdg"], "198.51.100.10")

    def test_a_native_reader_gives_every_engine_role_the_line_s_own_slot(self):
        """A USB PC/SC reader has one slot. The rendered PIN/IMS readers used to be fixed at
        "0" and "2", so on a one-reader host ami_usim addressed a slot that does not exist and
        the SIM read as USIM = NO_CARD while IMS-AKA never ran (issue #8)."""
        base = {
            "id": "3", "index": 0, "imsi": "234100000000000",
            "mcc": "234", "mnc": "10", "iccid": "test-card", "reader_index": 0,
            "imei": "490154203237518", "ami_secret": "test-secret",
            "sip": {"webrtc": {"enable": True, "password": "test-password"}},
        }

        rendered = config.render_instance_json(base, {})

        self.assertEqual(rendered["pin_reader"], "0")
        self.assertEqual(rendered["ami_reader"], "0")

    def test_a_native_reader_on_a_higher_index_is_followed_by_every_role(self):
        base = {
            "id": "3", "index": 0, "imsi": "234100000000000",
            "mcc": "234", "mnc": "10", "iccid": "test-card", "reader_index": 3,
            "imei": "490154203237518", "ami_secret": "test-secret",
            "sip": {"webrtc": {"enable": True, "password": "test-password"}},
        }

        rendered = config.render_instance_json(base, {})

        self.assertEqual(rendered["pin_reader"], "3")
        self.assertEqual(rendered["ami_reader"], "3")


    def test_blank_sip_identity_fields_restore_carrier_defaults(self):
        merged = config.merge_carrier_sip_defaults("234", "10", "test-card", {
            "pani": "", "access_type": "", "user_eq_phone": False,
        })

        self.assertIn("country=GB", merged["pani"])
        self.assertEqual(merged["access_type"], "wlan1")
        self.assertFalse(merged["user_eq_phone"])

    @patch.object(main.egress, "publish")
    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_giffgaff_draft_promotion_applies_carrier_profile(
            self, hardware_imei, upsert, _publish):
        draft = {**self.draft, "mcc": "234", "mnc": "10", "sip": {
            "listen_addr": "0.0.0.0", "transport": "udp"}}
        card = {**self.card, "mcc": "234", "mnc": "10"}
        hardware_imei.return_value = ("490154203237518", "test", "modem")
        upsert.side_effect = lambda value, **kwargs: value

        result = main._auto_promote_card_draft(draft, card, [card])

        self.assertEqual(result["sip"]["access_type"], "wlan1")
        self.assertTrue(result["sip"]["user_eq_phone"])
        self.assertIn("country=GB", result["sip"]["pani"])


class HotplugDraftPromotionTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_vowifi_still_promotes_complete_draft_without_starting_engine(self):
        draft = {
            "id": "2", "iccid": "test-card", "provisioning_state": "draft",
            "enabled": False,
        }
        card = {"present": True, "iccid": "test-card", "hardware_kind": "reader"}
        # Completed, but VoWiFi left off (e.g. a carrier without VoWiFi support).
        promoted = {**draft, "provisioning_state": "ready", "enabled": False}

        with patch.object(main.asyncio, "sleep", new=AsyncMock()), \
                patch.object(main.cfg, "get_instance", return_value=draft), \
                patch.object(main.engine, "is_running", return_value=False), \
                patch.object(main.hub, "cards_list", return_value=[card]), \
                patch.object(main, "_auto_promote_card_draft",
                             return_value=promoted) as promote, \
                patch.object(main, "_start_engine_checked") as start:
            await main._auto_start_hotplugged_line("2")

        promote.assert_called_once_with(draft, card, [card])
        start.assert_not_called()
        self.assertNotIn("2", main.hub.hotplug_starts)


class ReaderImeiCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_saving_last_missing_reader_imei_promotes_and_starts_draft(self):
        device = {"id": "reader-1", "device_type": "reader", "instance_id": "2",
                  "name": "USB reader", "stable_path": "1-2"}
        draft = {"id": "2", "iccid": "test-card", "provisioning_state": "draft",
                 "enabled": False}
        card = {"present": True, "iccid": "test-card", "hardware_id": "reader-1"}
        ready = {**draft, "provisioning_state": "ready", "enabled": True,
                 "imei": "490154203237518"}

        with patch.object(main, "_unified_devices", new=AsyncMock(return_value=[device])), \
                patch.object(main.device_state, "set_hardware", return_value={
                    "imei": "490154203237518"}), \
                patch.object(main.cfg, "get_instance", return_value=draft), \
                patch.object(main.hub, "cards_list", return_value=[card]), \
                patch.object(main, "_auto_promote_card_draft", return_value=ready) as promote, \
                patch.object(main.engine, "is_running", return_value=False), \
                patch.object(main, "_line_auto_start_allowed", return_value=(True, "")), \
                patch.object(main, "_start_engine_checked") as start, \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.api_device_hardware(
                "reader-1", {"imei": "490154203237518"})

        promote.assert_called_once_with(draft, card, [card])
        start.assert_called_once()
        self.assertTrue(result["applied"])
        self.assertTrue(result["started"])


class DraftSetupGuidanceTests(unittest.IsolatedAsyncioTestCase):
    """A draft whose IMEI is saved must say exactly what still blocks it, so the operator is not
    left guessing which tab matters. This build sends no SMS, so a SIM that does not expose its
    SMS centre is no longer held back for it."""

    draft = {"id": "2", "iccid": "test-card", "provisioning_state": "draft", "enabled": False,
             "imsi": "234330000000000", "mcc": "234"}
    card = {"present": True, "iccid": "test-card", "hardware_id": "reader-1",
            "imsi": "234330000000000", "mcc": "234", "mnc": "", "smsc": ""}

    def test_missing_lists_exactly_what_blocks_promotion(self):
        with patch.object(main, "_hardware_imei_for_card",
                          return_value=("490154203237518", "reader-1", "reader")):
            self.assertEqual(main._draft_missing(self.draft, self.card, [self.card]),
                             ["MCC/MNC"])
            promoted = main._auto_promote_card_draft(self.draft, self.card, [self.card])
        self.assertEqual(promoted["provisioning_state"], "draft")
        self.assertEqual(promoted["auto_provision_missing"], ["MCC/MNC"])

    def test_a_sim_without_an_sms_centre_is_not_held_as_a_draft(self):
        card = {**self.card, "mnc": "33", "smsc": ""}
        with patch.object(main, "_hardware_imei_for_card",
                          return_value=("490154203237518", "reader-1", "reader")):
            self.assertEqual(main._draft_missing({**self.draft, "mnc": "33"}, card, [card]), [])

    def test_a_locked_sim_without_a_saved_pin_is_reported(self):
        card = {**self.card, "mnc": "33", "pin_enabled": True}
        with patch.object(main, "_hardware_imei_for_card",
                          return_value=("490154203237518", "reader-1", "reader")):
            self.assertEqual(main._draft_missing(self.draft, card, [card]), ["SIM PIN"])

    async def test_saving_the_imei_says_what_is_still_missing(self):
        device = {"id": "reader-1", "device_type": "reader", "instance_id": "2",
                  "name": "Alcor Link AK9563", "stable_path": "1-2"}
        with patch.object(main, "_unified_devices", new=AsyncMock(return_value=[device])), \
                patch.object(main.device_state, "set_hardware", return_value={
                    "imei": "490154203237518"}), \
                patch.object(main.cfg, "get_instance", return_value=self.draft), \
                patch.object(main.hub, "cards_list", return_value=[self.card]), \
                patch.object(main, "_hardware_imei_for_card",
                             return_value=("490154203237518", "reader-1", "reader")), \
                patch.object(main.engine, "is_running", return_value=False), \
                patch.object(main, "_start_engine_checked") as start, \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.api_device_hardware("reader-1", {"imei": "490154203237518"})

        start.assert_not_called()
        self.assertFalse(result["started"])
        self.assertEqual(result["missing"], ["MCC/MNC"])


class LateCardDataTests(unittest.IsolatedAsyncioTestCase):
    """An Alcor AK9563 can return an incomplete read at insertion that a later read completes.
    Nothing read the card again, and the SIM tab's "Read SIM card" only filled the form, so
    the line stayed a draft until the operator also pressed Save."""

    def card(self, **overrides):
        values = {"iccid": "8944000000000000001", "imsi": "234330000000000", "mcc": "234",
                  "mnc": "33", "mnc_len": 2, "smsc": "+447700900000", "pin_enabled": False,
                  "pin_tries": 3}
        values.update(overrides)
        return SimpleNamespace(**values, dict=lambda: dict(values))

    def entry(self, **overrides):
        values = {"name": "Alcor Link AK9563 00 00", "index": 4, "present": True,
                  "iccid": "8944000000000000001", "imsi": "234330000000000", "mcc": "234",
                  "mnc": None, "smsc": None, "matched": "2", "carrier_identity": {}}
        values.update(overrides)
        return values

    def test_a_later_read_only_fills_gaps(self):
        entry = self.entry(imsi="234330000000000")
        self.assertTrue(main._merge_card_read(entry, self.card(imsi="999990000000000")))
        self.assertEqual(entry["mnc"], "33")
        self.assertEqual(entry["imsi"], "234330000000000")   # never replaced

    def test_a_read_of_another_card_is_ignored(self):
        entry = self.entry()
        self.assertFalse(main._merge_card_read(entry, self.card(iccid="8944000000000000999")))
        self.assertIsNone(entry["mnc"])

    async def test_the_background_retry_completes_the_draft(self):
        entry = self.entry()
        draft = {"id": "2", "provisioning_state": "draft", "iccid": entry["iccid"]}
        main.hub.cards[entry["name"]] = entry
        try:
            with patch.object(main, "_CARD_REREAD_DELAYS", (0,)), \
                    patch.object(main.cfg, "get_instance", return_value=draft), \
                    patch.object(main, "_hardware_imei_for_card",
                                 return_value=("490154203237518", "reader-1", "reader")), \
                    patch.object(main.sim, "read_card", return_value=self.card()), \
                    patch.object(main, "_auto_start_hotplugged_line",
                                 new=AsyncMock()) as start:
                await main._complete_draft_from_card(entry["name"])
                await asyncio.sleep(0)
            start.assert_awaited_once_with("2")
            self.assertEqual(main.hub.cards[entry["name"]]["mnc"], "33")
        finally:
            main.hub.cards.pop(entry["name"], None)

    async def test_read_sim_card_on_the_sim_tab_now_finishes_the_draft(self):
        entry = self.entry()
        draft = {"id": "2", "provisioning_state": "draft", "iccid": entry["iccid"]}
        main.hub.cards[entry["name"]] = entry
        try:
            with patch.object(main.sim, "list_readers",
                              return_value=["r0", "r1", "r2", "r3", entry["name"]]), \
                    patch.object(main.sim, "read_card", return_value=self.card()), \
                    patch.object(main.cfg, "get_instance", return_value=draft), \
                    patch.object(main, "_hardware_imei_for_card",
                                 return_value=("490154203237518", "reader-1", "reader")), \
                    patch.object(main, "_auto_start_hotplugged_line",
                                 new=AsyncMock()) as start:
                result = await main.api_sim_detect(4)
                await asyncio.sleep(0)
            self.assertEqual(result["smsc"], "+447700900000")
            start.assert_awaited_once_with("2")
        finally:
            main.hub.cards.pop(entry["name"], None)

    async def test_the_device_button_reports_what_the_card_still_lacks(self):
        entry = self.entry()
        draft = {"id": "2", "provisioning_state": "draft", "iccid": entry["iccid"]}
        main.hub.cards[entry["name"]] = entry
        try:
            with patch.object(main, "_device_for_card", return_value=("reader-1", "reader")), \
                    patch.object(main.sim, "read_card", return_value=self.card(mnc="")), \
                    patch.object(main.cfg, "get_instance", return_value=draft), \
                    patch.object(main, "_hardware_imei_for_card",
                                 return_value=("490154203237518", "reader-1", "reader")), \
                    patch.object(main.hub, "broadcast", new=AsyncMock()), \
                    patch.object(main, "_auto_start_hotplugged_line", new=AsyncMock()) as start:
                result = await main.api_device_sim_reread("reader-1")
            self.assertEqual(result["missing"], ["MCC/MNC"])
            self.assertFalse(result["completing"])
            start.assert_not_awaited()
        finally:
            main.hub.cards.pop(entry["name"], None)


class ImsIdentityLearningTests(unittest.IsolatedAsyncioTestCase):
    def test_modemmanager_number_requires_ims_confirmation(self):
        self.assertTrue(main._needs_ims_msisdn_learning({
            "msisdn": "447000000000", "msisdn_source": "modemmanager"}))
        self.assertTrue(main._needs_ims_msisdn_learning({"msisdn": ""}))
        self.assertFalse(main._needs_ims_msisdn_learning({
            "msisdn": "+447000000001", "msisdn_source": "ims"}))
        self.assertFalse(main._needs_ims_msisdn_learning({
            "msisdn": "+447000000001", "msisdn_source": "manual"}))

    async def test_ims_correction_is_persisted_and_applied_to_running_engine(self):
        current = {"id": "2", "msisdn": "447000000000",
                   "msisdn_source": "modemmanager"}
        corrected = {**current, "msisdn": "+447000000001", "msisdn_source": "ims"}
        with patch.object(main.asyncio, "sleep", new=AsyncMock()), \
                patch.object(main.engine, "exec_cli", return_value=""), \
                patch.object(main, "extract_msisdn", return_value=corrected["msisdn"]), \
                patch.object(main.cfg, "get_instance", return_value=current), \
                patch.object(main.cfg, "upsert_instance", return_value=corrected) as upsert, \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main.engine, "is_running", return_value=True), \
                patch.object(main, "_start_engine_checked") as restart, \
                patch.object(main.hub, "drop_ami", new=AsyncMock()) as drop_ami, \
                patch.object(main.hub, "broadcast", new=AsyncMock()), \
                patch.object(main.hub, "reset_health"):
            await main.learn_msisdn("2")

        upsert.assert_called_once_with({"id": "2", "msisdn": "+447000000001",
                                        "msisdn_source": "ims"})
        drop_ami.assert_awaited_once_with("2")
        restart.assert_called_once_with(corrected, {}, False)


class IdenticalNativeReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_swapped_reader_names_are_attributed_by_live_card_identity(self):
        """A reboot may swap pcscd names while the physical USB ports stay fixed.

        A running engine's pin_status still naming the newly enumerated reader must not lend
        that line's saved ICCID to the other physical card. Both cards are probed and matched
        to their own lines before the live reader indices are refreshed.
        """
        cards = {
            0: main.sim.CardInfo(
                reader="AK9563 00 00", reader_index=0, present=True,
                iccid="iccid-b", imsi="imsi-b", mcc="002", mnc="02",
                pin_enabled=False, pin_tries=3, smsc="+200"),
            1: main.sim.CardInfo(
                reader="AK9563 01 00", reader_index=1, present=True,
                iccid="iccid-a", imsi="imsi-a", mcc="001", mnc="01",
                pin_enabled=False, pin_tries=3, smsc="+100"),
        }
        instances = {
            "1": {"id": "1", "iccid": "iccid-a", "imsi": "imsi-a",
                  "reader_index": 0, "reader_port": "2-1"},
            "2": {"id": "2", "iccid": "iccid-b", "imsi": "imsi-b",
                  "reader_index": 1, "reader_port": "2-3"},
        }
        pin_readers = {"1": "AK9563 00 00", "2": "AK9563 01 00"}

        def upsert(update):
            iid = str(update["id"])
            instances[iid].update(update)
            return dict(instances[iid])

        main.hub.cards.clear()
        self.addCleanup(main.hub.cards.clear)
        with patch.object(main.usbreader, "port_for_index",
                          side_effect=lambda idx: {0: "2-3", 1: "2-1"}[idx]), \
                patch.object(main.cfg, "list_instances",
                             side_effect=lambda: [dict(value) for value in instances.values()]), \
                patch.object(main.engine, "is_running", return_value=True), \
                patch.object(main.engine, "read_run_json",
                             side_effect=lambda iid, _name: {"reader": pin_readers[str(iid)]}), \
                patch.object(main.sim, "read_card", side_effect=lambda idx: cards[idx]) as read, \
                patch.object(main.cfg, "upsert_instance", side_effect=upsert), \
                patch.object(main, "_auto_start_hotplugged_line", new=AsyncMock()):
            await main._on_card_insert("AK9563 00 00", 0)
            await main._on_card_insert("AK9563 01 00", 1)
            await asyncio.sleep(0)

        self.assertEqual(read.call_count, 2)
        self.assertEqual(main.hub.cards["AK9563 00 00"]["matched"], "2")
        self.assertEqual(main.hub.cards["AK9563 01 00"]["matched"], "1")
        self.assertEqual(instances["1"]["reader_port"], "2-1")
        self.assertEqual(instances["1"]["reader_index"], 1)
        self.assertEqual(instances["2"]["reader_port"], "2-3")
        self.assertEqual(instances["2"]["reader_index"], 0)


class EsimProfileRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_active_profile_creates_line_and_schedules_auto_start(self):
        card = SimpleNamespace(
            iccid="89000000000000000067", imsi="234100000000001",
            mcc="234", mnc="10", pin_enabled=False, pin_tries=3,
            smsc="+447785016005",
        )
        draft = {"id": "5", "iccid": card.iccid, "provisioning_state": "draft"}
        scheduled = []

        def capture(coro):
            scheduled.append(coro)
            coro.close()

        with patch.dict(main.hub.cards, {"Reader": {
                "index": 4, "name": "Reader", "present": True,
                "reader_port": "1-1.2", "matched": "4",
                "iccid": "89441000400130000000",
        }}, clear=True), \
                patch.object(main.sim, "read_card", return_value=card), \
                patch.object(main, "_match_instance_by_iccid", return_value=None), \
                patch.object(main.cfg, "card_auto_create_suppressed", return_value=False), \
                patch.object(main, "_ensure_card_draft", return_value=draft) as ensure, \
                patch.object(main.hub, "broadcast", new=AsyncMock()) as broadcast, \
                patch.object(main.asyncio, "create_task", side_effect=capture):
            result = await main._esim_refresh_card("Reader", 4)

        self.assertEqual(result["iccid"], card.iccid)
        self.assertEqual(result["matched"], "5")
        ensure.assert_called_once()
        broadcast.assert_awaited_once()
        self.assertEqual(len(scheduled), 1)


if __name__ == "__main__":
    unittest.main()
