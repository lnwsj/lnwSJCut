import tempfile
import unittest
from pathlib import Path

from core.model import Clip, Project
from core.project_io import load_project, save_project


class TestProjectIO(unittest.TestCase):
    def test_save_project_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "nested" / "deep" / "project.json"
            p = Project(v_clips=[Clip(id="v1", src="a.mp4", in_sec=0.0, out_sec=1.0)], a_clips=[], fps=30)
            save_project(p, str(out))
            self.assertTrue(out.exists())

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "project.json"
            p = Project(
                v_clips=[Clip(id="v1", src="a.mp4", in_sec=1.0, out_sec=2.5, volume=0.8)],
                a_clips=[Clip(id="a1", src="b.mp3", in_sec=0.0, out_sec=2.0, muted=True)],
                fps=24,
            )
            save_project(p, str(out))
            p2 = load_project(str(out))
            self.assertEqual(p2.fps, 24)
            self.assertEqual(len(p2.v_clips), 1)
            self.assertEqual(len(p2.a_clips), 1)
            self.assertAlmostEqual(p2.v_clips[0].in_sec, 1.0)
            self.assertAlmostEqual(p2.v_clips[0].out_sec, 2.5)
            self.assertTrue(p2.a_clips[0].muted)


if __name__ == "__main__":
    unittest.main()

