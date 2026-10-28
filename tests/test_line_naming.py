import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import config
from control.app import state as app_state
from control.app.routers import lines as lines_routes

try:
    from control.app import main
except ImportError:      # the Docker SDK is a manager runtime dependency naming does not need
    main = None

ICCID_A = "8944000000000004409"
ICCID_B = "8944000000000001234"


class TempConfig:
    """Run one test against a throwaway config.yaml."""

    def __init__(self):
        self._temp = tempfile.TemporaryDirectory()

    def __enter__(self):
        root = self._temp.name
        self._patch = patch.multiple(config, DATA_DIR=root,
                                     CONFIG_PATH=str(Path(root) / "config.yaml"))
        self._patch.start()
        # Naming is about telling lines apart, so these tests need room for two.
        config.update_settings({"max_lines": config.PRODUCT_MAX_LINES})
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._temp.cleanup()
        return False

    def add(self, iid, name, **fields):
        return config.upsert_instance({"id": iid, "name": name, **fields})


class GeneratedNameTests(unittest.TestCase):
    def test_the_generated_name_carries_the_carrier_and_the_sim_tail(self):
        self.assertEqual(config.default_instance_name("234", "10", ICCID_A), "234-10-4409")
        self.assertEqual(config.default_instance_name("310", "240", ICCID_B), "310-240-1234")

    def test_a_sim_read_before_its_carrier_still_gets_a_distinguishing_name(self):
        # MCC/MNC can be missing at creation; the ICCID never is, so it carries the name.
        self.assertEqual(config.default_instance_name("", "", ICCID_A), "SIM-4409")
        self.assertEqual(config.default_instance_name("234", "", ICCID_A), "SIM-4409")
        self.assertEqual(config.default_instance_name("", "", ""), "New SIM")

    def test_two_sims_of_one_carrier_no_longer_share_a_name(self):
        first = config.default_instance_name("234", "10", ICCID_A)
        second = config.default_instance_name("234", "10", ICCID_B)
        self.assertNotEqual(first, second)


class UniquenessTests(unittest.TestCase):
    def test_a_generated_name_that_collides_is_suffixed(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "234-10-4409")
            # Same carrier AND same last four digits — rare, but only four digits exist.
            second = config.upsert_instance({"id": "2", "name": "234-10-4409"},
                                            unique_name=True)
        self.assertEqual(second["name"], "234-10-4409 (2)")
        # A third line no longer fits under the line ceiling; the suffix keeps counting.
        data = {"instances": {"1": {"id": "1", "name": "234-10-4409"},
                              "2": {"id": "2", "name": "234-10-4409 (2)"}}}
        self.assertEqual(config._free_instance_name(data, "234-10-4409", "3"),
                         "234-10-4409 (3)")

    def test_suffixing_ignores_case_the_way_the_bot_matches_names(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            clash = config.upsert_instance({"id": "2", "name": "giff"}, unique_name=True)
        self.assertEqual(clash["name"], "giff (2)")

    def test_an_explicit_name_is_never_silently_altered(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            same = cfg_temp.add("2", "Giff")           # no unique_name -> stored verbatim
        self.assertEqual(same["name"], "Giff")

    def test_updating_a_line_keeps_its_own_name(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            again = config.upsert_instance({"id": "1", "name": "Giff", "msisdn": "+4477"},
                                           unique_name=True)
        self.assertEqual(again["name"], "Giff")

    def test_name_taken_excludes_the_line_itself_and_ignores_blanks(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            cfg_temp.add("2", "")
            self.assertTrue(config.instance_name_taken("giff"))
            self.assertTrue(config.instance_name_taken("Giff", exclude_iid="9"))
            self.assertFalse(config.instance_name_taken("Giff", exclude_iid="1"))
            self.assertFalse(config.instance_name_taken(""))
            self.assertFalse(config.instance_name_taken("   "))

    def test_runtime_only_api_fields_are_never_persisted(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff", proxy_country_effective="gb",
                         status={"state": "connected"}, has_pin=True)
            saved = config.get_instance("1")
        self.assertNotIn("proxy_country_effective", saved)
        self.assertNotIn("status", saved)
        self.assertNotIn("has_pin", saved)


@unittest.skipIf(main is None, "manager runtime dependencies are unavailable")
class RenameApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_running_line_rename_does_not_restart_its_engine(self):
        with TempConfig() as cfg_temp:
            before = cfg_temp.add("1", "Old name", mcc="234", mnc="010")
            with patch.object(main.engine, "is_running", return_value=True), \
                    patch.object(app_state, "_start_engine_checked") as restart, \
                    patch.object(main.egress, "publish") as publish:
                # The WebUI submits the complete form, not a special rename request. The server
                # must identify the effective persisted diff rather than trusting payload shape.
                renamed = await lines_routes.api_instance_upsert({**before, "name": "New name"})

        self.assertEqual(renamed["name"], "New name")
        self.assertFalse(renamed["applied"])
        restart.assert_not_called()
        publish.assert_called_once_with()
        self.assertTrue(lines_routes._only_instance_name_changed(
            before, {**before, "name": "New name"}))

    async def test_a_webui_rename_cleans_old_runtime_pollution_without_restart(self):
        with TempConfig() as cfg_temp:
            before = cfg_temp.add("1", "Old name", mcc="234", mnc="010")
            # Reproduce a config written by the affected release. The real WebUI then reads
            # this API-only value and sends the complete instance form back with the rename.
            polluted = {**before, "proxy_country_effective": "gb"}
            raw = config.load()
            raw["instances"]["1"] = polluted
            config.save(raw)
            with patch.object(main.engine, "is_running", return_value=True), \
                    patch.object(app_state, "_start_engine_checked") as restart, \
                    patch.object(main.egress, "publish") as publish:
                renamed = await lines_routes.api_instance_upsert(
                    {**polluted, "name": "New name", "status": {"state": "connected"},
                     "has_pin": False})

            persisted = config.get_instance("1")

        self.assertEqual(renamed["name"], "New name")
        self.assertFalse(renamed["applied"])
        self.assertNotIn("proxy_country_effective", persisted)
        restart.assert_not_called()
        publish.assert_called_once_with()

    async def test_a_webui_rename_with_a_padded_mnc_and_live_reader_does_not_restart(self):
        """Seen on the NAS with rc9: renaming line 4 also saved mnc "15" -> "015" (the form pads
        it) and reader_index 5 -> 1 (the list shows the live index), and the line restarted."""
        with TempConfig() as cfg_temp:
            before = cfg_temp.add("4", "234-15-6315", mcc="234", mnc="15", reader_index=5,
                                  reader_port="1-3.4.1")
            with patch.object(main.engine, "is_running", return_value=True), \
                    patch.object(lines_routes, "_reader_index_for_instance", return_value=1), \
                    patch.object(lines_routes, "_reader_port_for_instance", return_value="1-3.4.1"), \
                    patch.object(app_state, "_start_engine_checked") as restart, \
                    patch.object(main.egress, "publish"):
                renamed = await lines_routes.api_instance_upsert(
                    {**before, "name": "voxi-THB", "mnc": "015", "reader_index": 1})

        self.assertFalse(renamed["applied"])
        restart.assert_not_called()

    def test_a_reader_index_other_than_the_live_one_is_still_an_edit(self):
        before = {"id": "4", "name": "a", "reader_index": 5}
        self.assertFalse(lines_routes._only_instance_name_changed(
            before, {**before, "name": "b", "reader_index": 2}, {"reader_index": 1}))
        self.assertFalse(lines_routes._only_instance_name_changed(
            before, {**before, "name": "b", "reader_index": 1}, {}))

    def test_a_different_mnc_is_still_an_edit(self):
        before = {"id": "4", "name": "a", "mcc": "234", "mnc": "15"}
        self.assertTrue(lines_routes._only_instance_name_changed(
            before, {**before, "name": "b", "mnc": "015"}))
        self.assertFalse(lines_routes._only_instance_name_changed(
            before, {**before, "name": "b", "mnc": "10"}))

    def test_an_operational_edit_is_not_treated_as_a_rename(self):
        before = {"id": "1", "name": "Old name", "proxy_country": "gb"}
        self.assertFalse(lines_routes._only_instance_name_changed(
            before, {**before, "name": "New name", "proxy_country": "us"}))

    async def test_renaming_onto_another_lines_name_is_refused(self):
        from fastapi import HTTPException
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            cfg_temp.add("2", "310-240-1234")
            with patch.object(main.asyncio, "to_thread", return_value=False):
                with self.assertRaises(HTTPException) as refused:
                    await lines_routes.api_instance_upsert({"id": "2", "name": "giff"})
                self.assertEqual(refused.exception.status_code, 409)

                # Saving a line under its own name, and a free name, both still work.
                kept = await lines_routes.api_instance_upsert({"id": "2", "name": "310-240-1234"})
                self.assertEqual(kept["name"], "310-240-1234")
                renamed = await lines_routes.api_instance_upsert({"id": "2", "name": "Home"})
                self.assertEqual(renamed["name"], "Home")

    async def test_an_edit_that_carries_no_name_is_unaffected(self):
        with TempConfig() as cfg_temp:
            cfg_temp.add("1", "Giff")
            with patch.object(main.asyncio, "to_thread", return_value=False):
                saved = await lines_routes.api_instance_upsert({"id": "1", "proxy_country": "gb"})
        self.assertEqual(saved["name"], "Giff")


if __name__ == "__main__":
    unittest.main()
