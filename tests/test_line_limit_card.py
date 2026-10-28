"""A SIM kept out by the line limit, and the same SIM unplugged and plugged back in.

Seen on the device (1.14.0-slim.5, limit 1): after a reader was unplugged and plugged in again
the log said "SIM line limit reached; ignoring newly detected SIM 6697" and the devices page
only said to insert a readable SIM.

Matching is by ICCID (state._match_instance_by_iccid), never by reader slot, and nothing is
reserved or released on removal. These tests pin that: the same card comes back to its own line
whatever happens to the reader in between, and only a card whose ICCID matches no saved line
can reach the limit — which the page then names, and which clears once there is room.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from control.app import config as cfg
from control.app import device_state, egress, sim, usbreader
from control.app import state as app_state
from control.app import tasks as app_tasks
from control.app.routers import lines as lines_routes

READER = "Alcor Link AK9563 00 00"
ICCID_LINE = "8944000000000006697"       # synthetic; the tail matches the device log
ICCID_OTHER = "8944000000000001234"


def _card(iccid):
    return SimpleNamespace(iccid=iccid, imsi="234100000000001", mcc="234", mnc="10",
                           mnc_len=2, pin_enabled=False, pin_tries=3, smsc="+447000000000")


class _Fixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for patcher in (
                patch.multiple(cfg, DATA_DIR=str(root), CONFIG_PATH=str(root / "config.yaml")),
                patch.multiple(device_state, ROOT=str(root / "devices")),
                patch.object(usbreader, "port_for_index", return_value="1-1"),
                patch.object(app_state, "_find_running_by_reader", return_value=None),
                patch.object(app_state, "_auto_start_hotplugged_line", new=AsyncMock()),
                patch.object(app_state, "_draft_needs_card_read", return_value=None),
                patch.object(app_state, "_hardware_imei_for_card", return_value=("", "", "")),
                patch.object(app_state.hub, "broadcast", new=AsyncMock()),
                patch.object(egress, "publish"),
                patch.object(egress, "status", return_value={}),
                patch.object(app_state.engine, "is_running", return_value=False),
                patch.object(app_state.engine, "stop", return_value=False),
                patch.dict(app_state.hub.cards, {}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        (root / "devices").mkdir()
        cfg.update_settings({"max_lines": 1})

    async def insert(self, iccid):
        with patch.object(sim, "read_card", return_value=_card(iccid)):
            await app_tasks._on_card_insert(READER, 0)
        return app_state.hub.cards[READER]

    async def unplug(self):
        entry = app_state.hub.cards.pop(READER)
        await app_tasks._on_card_remove(entry, reader_unplugged=True)

    async def device(self):
        devices = await lines_routes._unified_devices()
        self.assertEqual(len(devices), 1)
        return devices[0]


class SameCardTests(_Fixture):
    async def test_unplug_and_replug_the_same_card_returns_to_its_line(self):
        first = await self.insert(ICCID_LINE)
        self.assertEqual(first["matched"], "1")
        self.assertEqual([i["id"] for i in cfg.list_instances()], ["1"])

        with patch.object(app_state, "_ensure_card_draft",
                          wraps=app_state._ensure_card_draft) as draft, \
                self.assertNoLogs("vowifi.main", level="WARNING"):
            await self.unplug()
            again = await self.insert(ICCID_LINE)

        self.assertEqual(again["matched"], "1")
        self.assertNotIn("not_enabled", again)
        draft.assert_not_called()                 # matched by ICCID; no new line attempted
        self.assertEqual([i["id"] for i in cfg.list_instances()], ["1"])

    async def test_card_pulled_and_pushed_back_without_unplugging_the_reader(self):
        await self.insert(ICCID_LINE)
        await app_tasks._on_card_remove(dict(app_state.hub.cards[READER]))
        self.assertIsNone(app_state.hub.cards[READER]["matched"])
        again = await self.insert(ICCID_LINE)
        self.assertEqual(again["matched"], "1")
        self.assertEqual(len(cfg.list_instances()), 1)

    async def test_a_failed_read_on_reinsertion_never_counts_against_the_limit(self):
        await self.insert(ICCID_LINE)
        await self.unplug()
        with patch.object(sim, "read_card", side_effect=RuntimeError("card busy")):
            await app_tasks._on_card_insert(READER, 0)
        entry = app_state.hub.cards[READER]
        self.assertIsNone(entry["matched"])
        self.assertNotIn("not_enabled", entry)
        self.assertEqual(len(cfg.list_instances()), 1)


class LineLimitTests(_Fixture):
    async def test_a_new_sim_at_the_limit_is_named_on_the_devices_page(self):
        await self.insert(ICCID_OTHER)                      # takes the only slot
        await self.unplug()
        with self.assertLogs("vowifi.main", level="WARNING") as logged:
            entry = await self.insert(ICCID_LINE)
        self.assertIn("line limit reached (1 line(s)); ignoring newly detected SIM 6697",
                      "\n".join(logged.output))
        self.assertIsNone(entry["matched"])
        self.assertEqual(entry["not_enabled"],
                         {"reason": "line_limit", "max_lines": 1, "iccid_tail": "6697"})

        device = await self.device()
        self.assertEqual(device["provisioning"]["state"], "line_limit")
        self.assertEqual(device["provisioning"]["line_limit"],
                         {"max_lines": 1, "iccid_tail": "6697"})
        self.assertEqual(device["capabilities"]["vowifi"]["reason"],
                         "This SIM has no line: the SIM line limit is reached")
        # /api/cards carries it too.
        self.assertEqual(app_state._client_cards()[0]["not_enabled"]["reason"], "line_limit")

    async def test_deleting_the_old_line_lets_the_waiting_sim_in(self):
        await self.insert(ICCID_OTHER)
        await self.unplug()
        await self.insert(ICCID_LINE)
        waiting = app_state.line_limited_cards()
        self.assertEqual(len(waiting), 1)

        self.assertEqual(app_state.retry_line_limited_cards(waiting), [])   # still full
        cfg.delete_instance("1")
        created = app_state.retry_line_limited_cards(waiting)

        self.assertEqual(created, ["1"])
        entry = app_state.hub.cards[READER]
        self.assertEqual(entry["matched"], "1")
        self.assertNotIn("not_enabled", entry)
        self.assertEqual(cfg.get_instance("1")["iccid"], ICCID_LINE)
        self.assertEqual(app_state.line_limited_cards(), [])
        self.assertEqual((await self.device())["provisioning"]["state"], "draft")

    async def test_raising_the_limit_lets_the_waiting_sim_in(self):
        await self.insert(ICCID_OTHER)
        await self.unplug()
        await self.insert(ICCID_LINE)
        cfg.update_settings({"max_lines": cfg.PRODUCT_MAX_LINES})
        self.assertEqual(app_state.retry_line_limited_cards(app_state.line_limited_cards()),
                         ["2"])
        self.assertEqual(sorted(i["iccid"] for i in cfg.list_instances()),
                         sorted([ICCID_OTHER, ICCID_LINE]))

    async def test_the_card_monitor_retries_on_its_own(self):
        await self.insert(ICCID_OTHER)
        await self.unplug()
        await self.insert(ICCID_LINE)
        cfg.delete_instance("1")
        present = [{"name": READER, "index": 0, "present": True}]
        with patch.object(app_tasks.card, "reader_states", return_value=present), \
                patch.object(app_tasks.card, "wait_for_change",
                             side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await app_tasks.card_monitor()
        self.assertEqual(app_state.hub.cards[READER]["matched"], "1")
        app_state._auto_start_hotplugged_line.assert_any_await("1")


if __name__ == "__main__":
    unittest.main()
