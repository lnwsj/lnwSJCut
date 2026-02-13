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

    def test_remove_recent_project(self):
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(Path(td))
            p1 = str(Path(td) / "a.json")
            p2 = str(Path(td) / "b.json")
            store.add_recent_project(p1)
            store.add_recent_project(p2)
            self.assertEqual(len(store.recent_projects(limit=10)), 2)

            store.remove_recent_project(p1)
            rec = store.recent_projects(limit=10)
            self.assertEqual(len(rec), 1)
            self.assertTrue(rec[0].path.endswith("b.json"))

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

    def test_last_dir_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ConfigStore(root)
            proj_dir = root / "projects"
            proj_dir.mkdir(parents=True, exist_ok=True)
            exp_dir = root / "exports"
            exp_dir.mkdir(parents=True, exist_ok=True)

            project_path = proj_dir / "demo.json"
            project_path.write_text("{}", encoding="utf-8")
            store.set_last_project_dir(str(project_path))
            self.assertEqual(Path(store.last_project_dir()), proj_dir.resolve())

            export_path = exp_dir / "out.mp4"
            store.set_last_export_dir(str(export_path))
            self.assertEqual(Path(store.last_export_dir()), exp_dir.resolve())


if __name__ == "__main__":
    unittest.main()
