import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from control.app import device_state, main
from control.app import state as app_state
from control.app.routers import lines as lines_routes
from fastapi import HTTPException


class DeviceStateTests(unittest.TestCase):
    def test_native_reader_is_stable_and_never_collides_with_modem_vpcd_slots(self):
        cards = [
            {"name": "USB Smart Card Reader 00 00", "reader_port": "3-2",
             "hardware_kind": "reader", "present": True},
            {"name": "VPCD modem slot", "hardware_kind": "modem", "present": True},
        ]
        first = device_state.native_reader_devices(cards)
        second = device_state.native_reader_devices(list(reversed(cards)))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertTrue(next(iter(first)).startswith("reader-"))

    def test_native_reader_vowifi_state_is_independent_of_cellular(self):
        self.assertEqual(device_state.native_vowifi_capability(False, False, None)["actual"], "off")
        self.assertEqual(device_state.native_vowifi_capability(True, True, {"state": "OK"})["actual"], "on")
        self.assertEqual(device_state.native_vowifi_capability(True, False, None)["actual"], "degraded")


    def test_the_packaged_virtual_pcd_endpoint_is_never_a_device(self):
        """The vsmartcard package's own reader definition rendered as two phantom devices
        that exist whether or not any hardware does. The installer disables the file, but a
        package reinstall can restore it — the device list must not trust that."""
        cards = [
            {"name": "Virtual PCD 00 00", "hardware_kind": "reader", "present": True},
            {"name": "Virtual PCD 00 01", "hardware_kind": "reader", "present": True},
            {"name": "SCR Prime CCID Reader (000000000001) 00 00",
             "hardware_kind": "reader", "reader_port": "1-1.5"},
        ]
        readers = device_state.native_reader_devices(cards)
        self.assertEqual(len(readers), 1)
        self.assertEqual(next(iter(readers.values()))["reader_port"], "1-1.5")


class ReaderRecordMigrationTests(unittest.TestCase):
    """A reader's id comes from its USB port, so moving it to a hub mints a new one and
    strands the old record — rendering a connected reader twice, once permanently offline."""

    NAME = "SCR Prime CCID Reader (000000000001) 00 00"

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            device_state, "HARDWARE",
            str(Path(self._temp.name) / "devices-hardware.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._temp.cleanup()

    def test_the_record_follows_the_reader_to_its_new_port(self):
        device_state.set_hardware("reader-old", {
            "device_type": "reader", "name": self.NAME,
            "imei": "350000000000018", "stable_path": "1-1.5"})
        moved = device_state.migrate_reader_records(
            {"reader-new": {"name": self.NAME, "reader_port": "1-1.4.1"}})

        self.assertEqual(moved, [("reader-old", "reader-new")])
        records = device_state.hardware()
        self.assertEqual(set(records), {"reader-new"})
        # The IMEI is what the line presents to the carrier and is refreshed from here on
        # every start; losing it would silently change the device identity.
        self.assertEqual(records["reader-new"]["imei"], "350000000000018")
        self.assertEqual(records["reader-new"]["stable_path"], "1-1.4.1")

    def test_an_unplugged_reader_keeps_its_record(self):
        device_state.set_hardware("reader-old", {
            "device_type": "reader", "name": self.NAME, "imei": "350000000000018"})
        self.assertEqual(device_state.migrate_reader_records({}), [])
        self.assertIn("reader-old", device_state.hardware())

    def test_an_ambiguous_set_is_left_for_a_person(self):
        # Two identical readers replugged at once cannot be told apart by name.
        device_state.set_hardware("reader-a", {"device_type": "reader", "name": self.NAME})
        device_state.set_hardware("reader-b", {"device_type": "reader", "name": self.NAME})
        self.assertEqual(device_state.migrate_reader_records(
            {"reader-c": {"name": self.NAME}}), [])
        self.assertEqual(set(device_state.hardware()), {"reader-a", "reader-b"})

    def test_a_reader_that_did_not_move_is_untouched(self):
        device_state.set_hardware("reader-a", {"device_type": "reader", "name": self.NAME})
        self.assertEqual(device_state.migrate_reader_records(
            {"reader-a": {"name": self.NAME}}), [])
        self.assertIn("reader-a", device_state.hardware())

    def test_a_modem_record_is_never_claimed_by_a_reader(self):
        device_state.set_hardware("2c7c-0125-1-1.4",
                                  {"device_type": "modem", "name": self.NAME})
        self.assertEqual(device_state.migrate_reader_records(
            {"reader-new": {"name": self.NAME}}), [])
        self.assertIn("2c7c-0125-1-1.4", device_state.hardware())


class DeviceDisplayNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_nickname_then_imei_keeps_raw_name_for_port_migration(self):
        raw_name = "Alcor Link AK9563 01 00"
        device = {"id": "reader-old", "device_type": "reader", "instance_id": None,
                  "name": "Desk reader", "reader": raw_name, "stable_path": "1-2"}
        with tempfile.TemporaryDirectory() as temp:
            with patch.multiple(device_state, ROOT=temp,
                                HARDWARE=str(Path(temp) / "devices-hardware.json")), \
                    patch.object(lines_routes, "_unified_devices", new=AsyncMock(return_value=[device])), \
                    patch.object(app_state.hub, "broadcast", new=AsyncMock()):
                await lines_routes.api_device_name("reader-old", {"display_name": " Desk reader "})
                await lines_routes.api_device_hardware("reader-old", {"imei": "490154203237518"})
                record = device_state.hardware()["reader-old"]
                self.assertEqual(record["display_name"], "Desk reader")
                self.assertEqual(record["name"], raw_name)
                self.assertEqual(record["imei"], "490154203237518")

                moved = device_state.migrate_reader_records({
                    "reader-new": {"name": raw_name, "reader_port": "1-3"}})
                self.assertEqual(moved, [("reader-old", "reader-new")])
                self.assertEqual(device_state.hardware()["reader-new"]["display_name"],
                                 "Desk reader")

    async def test_name_validation_and_missing_device(self):
        with patch.object(lines_routes, "_unified_devices", new=AsyncMock(return_value=[])):
            with self.assertRaises(HTTPException) as invalid:
                await lines_routes.api_device_name("reader-a", {"display_name": "bad\nname"})
            self.assertEqual(invalid.exception.status_code, 422)
            with self.assertRaises(HTTPException) as missing:
                await lines_routes.api_device_name("reader-a", {"display_name": "Desk reader"})
            self.assertEqual(missing.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
