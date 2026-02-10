import json
import tempfile
import unittest
from pathlib import Path

from core.config import ConfigStore


class TestConfigStore(unittest.TestCase):
    def test_load_default_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(Path(td) / "cfg")
            cfg = store.load()
            self.assertIn("recent", cfg)
            self.assertIn("auto_save_interval_sec", cfg)

    def test_add_recent_dedup_and_limit(self):
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(Path(td))

            # Add same path twice; should de-dupe and keep most recent.
            store.add_recent_project(str(Path(td) / "a.json"))
            store.add_recent_project(str(Path(td) / "a.json"))
            recent = store.recent_projects(limit=10)
            self.assertEqual(len(recent), 1)

            # Add more than 10 items; should clamp.
            for i in range(30):
                store.add_recent_project(str(Path(td) / f"p{i}.json"))
            recent = store.recent_projects(limit=50)
            self.assertLessEqual(len(recent), 10)

    def test_auto_save_interval_clamps(self):
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(Path(td))
            store.save({"recent": [], "auto_save_interval_sec": 1})
            self.assertEqual(store.auto_save_interval_sec(), 10)

            store.save({"recent": [], "auto_save_interval_sec": 99999})
            self.assertEqual(store.auto_save_interval_sec(), 3600)

            # Garbage should fall back to default.
            store.path.write_text("not json", encoding="utf-8")
            self.assertEqual(store.auto_save_interval_sec(), 60)


if __name__ == "__main__":
    unittest.main()

