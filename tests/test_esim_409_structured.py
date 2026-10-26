import unittest
from unittest.mock import patch

from fastapi import HTTPException
from control.app import state as app_state
from control.app.routers import esim as esim_routes


class EsimEngineRunningConflictTests(unittest.TestCase):
    def test_guard_409_is_structured_with_instance_id(self):
        inst = {"id": 7, "reader": "Reader-1"}
        with patch.object(app_state, "_find_running_by_reader", return_value=inst):
            with self.assertRaises(HTTPException) as ctx:
                esim_routes._esim_guard_engine("Reader-1")
        exc = ctx.exception
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["code"], "engine_running")
        self.assertEqual(exc.detail["instance_id"], "7")
        self.assertIn("7", exc.detail["message"])

    def test_guard_passes_when_no_line_holds_the_reader(self):
        with patch.object(app_state, "_find_running_by_reader", return_value=None):
            esim_routes._esim_guard_engine("Reader-1")  # must not raise


if __name__ == "__main__":
    unittest.main()
