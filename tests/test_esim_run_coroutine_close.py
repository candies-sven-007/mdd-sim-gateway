import unittest
from unittest.mock import AsyncMock, patch

from control.app import main
from fastapi import HTTPException
from control.app import state as app_state
from control.app.routers import esim as esim_routes


class EsimRunCoroutineCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_by_engine_gate_closes_the_coroutine(self):
        async def work():
            return 42  # pragma: no cover - never awaited on the rejection path

        coro = work()
        with patch.object(main.asyncio, "to_thread",
                          side_effect=HTTPException(409, "busy")):
            with self.assertRaises(HTTPException):
                await esim_routes._esim_run("Reader-1", 0, coro)
        # close() ran: awaiting the coroutine again raises RuntimeError instead of
        # executing the body (and CPython's "never awaited" warning stays silent).
        with self.assertRaises(RuntimeError):
            await coro

    async def test_successful_run_awaits_normally(self):
        async def work():
            return "done"

        with patch.object(main.asyncio, "to_thread", new=AsyncMock()), \
             patch.object(app_state.hub, "reader_lock"):
            result = await esim_routes._esim_run("Reader-1", 0, work())
        self.assertEqual(result, "done")


if __name__ == "__main__":
    unittest.main()
