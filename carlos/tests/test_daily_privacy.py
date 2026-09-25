import tempfile, unittest
from pathlib import Path
from ev.daily import DailyStore


class DailyPrivacyTests(unittest.TestCase):
    def test_private_workspace_and_scene_changes_never_reach_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "daily.db"
            store = DailyStore(path)
            store.save("carlos_scene", "existing", {"commands": []})
            store.set_private(True)
            store.save("workspace_layout", "private_canary", {"windows": []})
            self.assertNotIn("private_canary", DailyStore(path).records("workspace_layout"))
            store.set_private(False)
            self.assertNotIn("private_canary", store.records("workspace_layout"))
            self.assertIn("existing", store.records("carlos_scene"))
            store.set_private(True, guest=True)
            self.assertEqual(store.records("carlos_scene"), {})
            store.set_private(False)

    def test_close_discards_private_state_and_releases_connection(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as folder:
            store = DailyStore(Path(folder) / "daily.db")
            store.set_private(True)
            connection = store._private_db
            store.save("workspace_layout", "private", {"windows": []})
            store.close()
            store.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            self.assertNotIn("private", store.records("workspace_layout"))
