"""Engine runtime cache, refreshed from systemd (control/app/runtime.py)."""
import unittest
from unittest.mock import AsyncMock, Mock

from control.app import engine
from control.app.runtime import RuntimeRegistry

RUNNING = {"running": True, "ip": "10.88.1.2", "generation": "one", "restart_count": 0}


class RuntimeRegistryTests(unittest.IsolatedAsyncioTestCase):
    def registry(self, inspector=None, batch=None, lines=("1",)):
        return RuntimeRegistry(inspector or Mock(return_value=dict(RUNNING)),
                               batch or Mock(return_value={}), lambda: list(lines))

    async def test_repeated_get_uses_cached_inspect(self):
        inspector = Mock(return_value=dict(RUNNING))
        registry = self.registry(inspector)
        first = await registry.get("1")
        second = await registry.get("1")
        self.assertEqual(first["generation"], "one")
        self.assertEqual(second["ip"], "10.88.1.2")
        inspector.assert_called_once_with("1")

    async def test_force_inspects_again(self):
        inspector = Mock(return_value=dict(RUNNING))
        registry = self.registry(inspector)
        await registry.get("1")
        await registry.get("1", force=True)
        self.assertEqual(inspector.call_count, 2)

    async def test_poll_fills_the_cache_without_reporting_a_first_sighting(self):
        batch = Mock(return_value={"1": dict(RUNNING)})
        inspector = Mock()
        changed = AsyncMock()
        registry = self.registry(inspector, batch)
        registry._on_change = changed
        await registry.poll_once()
        self.assertEqual((await registry.get("1"))["generation"], "one")
        inspector.assert_not_called()
        changed.assert_not_awaited()

    async def test_a_stop_is_reported(self):
        batch = Mock(side_effect=[{"1": dict(RUNNING)},
                                  {"1": {**RUNNING, "running": False, "ip": None}}])
        changed = AsyncMock()
        registry = self.registry(batch=batch)
        registry._on_change = changed
        await registry.poll_once()
        await registry.poll_once()
        iid, runtime, action = changed.await_args.args
        self.assertEqual((iid, action), ("1", "die"))
        self.assertFalse(runtime["running"])

    async def test_a_restart_by_systemd_is_a_new_generation(self):
        batch = Mock(side_effect=[{"1": dict(RUNNING)},
                                  {"1": {**RUNNING, "generation": "two", "restart_count": 1}}])
        changed = AsyncMock()
        registry = self.registry(batch=batch)
        registry._on_change = changed
        await registry.poll_once()
        await registry.poll_once()
        iid, runtime, action = changed.await_args.args
        self.assertEqual((iid, action, runtime["generation"]), ("1", "start", "two"))

    async def test_unchanged_state_is_not_reported(self):
        batch = Mock(return_value={"1": dict(RUNNING)})
        changed = AsyncMock()
        registry = self.registry(batch=batch)
        registry._on_change = changed
        await registry.poll_once()
        await registry.poll_once()
        changed.assert_not_awaited()

    async def test_no_lines_no_systemctl(self):
        batch = Mock()
        await self.registry(batch=batch, lines=()).poll_once()
        batch.assert_not_called()


class SystemdShowTests(unittest.TestCase):
    def test_parse_show_splits_units(self):
        text = ("ActiveState=active\nSubState=running\nInvocationID=a\n\n"
                "ActiveState=inactive\nSubState=dead\nInvocationID=\n")
        self.assertEqual(engine.parse_show(text),
                         [{"ActiveState": "active", "SubState": "running", "InvocationID": "a"},
                          {"ActiveState": "inactive", "SubState": "dead", "InvocationID": ""}])

    def test_only_a_running_unit_has_an_address(self):
        active = engine.runtime_from_show("2", {"ActiveState": "active", "SubState": "running",
                                                "InvocationID": "x", "NRestarts": "3"})
        self.assertEqual((active["running"], active["ip"], active["restart_count"]),
                         (True, "10.88.2.2", 3))
        starting = engine.runtime_from_show("2", {"ActiveState": "activating",
                                                  "SubState": "start"})
        self.assertFalse(starting["running"])
        self.assertIsNone(starting["ip"])
        self.assertIsNone(starting["generation"])


if __name__ == "__main__":
    unittest.main()
