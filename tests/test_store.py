import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import store


class StoreMigrationTests(unittest.TestCase):
    def test_store_connection_closes_after_commit_and_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "store.sqlite"
            with patch.multiple(store, DATA_DIR=temp, DB_PATH=str(path)):
                with store._conn() as connection:
                    connection.execute("CREATE TABLE marker (value TEXT)")
                    connection.execute("INSERT INTO marker VALUES ('kept')")
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
                with self.assertRaisesRegex(RuntimeError, "rollback"):
                    with store._conn() as connection:
                        connection.execute("INSERT INTO marker VALUES ('lost')")
                        raise RuntimeError("rollback")
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
                with store._conn() as connection:
                    self.assertEqual(connection.execute("SELECT value FROM marker").fetchall()[0][0], "kept")
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
