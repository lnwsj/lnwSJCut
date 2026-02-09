import unittest

from core.model import Clip, Project


class TestProjectModel(unittest.TestCase):
    def test_from_dict_backward_compat_clips(self):
        d = {
            "fps": 30,
            "clips": [
                {"id": "1", "src": "a.mp4", "in_sec": 0.0, "out_sec": 1.0},
            ],
        }
        p = Project.from_dict(d)
        self.assertEqual(p.fps, 30)
        self.assertEqual(len(p.v_clips), 1)
        self.assertEqual(len(p.a_clips), 0)
        self.assertEqual(p.v_clips[0].src, "a.mp4")

    def test_roundtrip_new_format(self):
        v = Clip(id="v1", src="a.mp4", in_sec=0.0, out_sec=2.0)
        a = Clip(id="a1", src="b.mp3", in_sec=1.0, out_sec=3.0)
        p = Project(v_clips=[v], a_clips=[a], fps=24)

        d = p.to_dict()
        self.assertIn("v_clips", d)
        self.assertIn("a_clips", d)

        p2 = Project.from_dict(d)
        self.assertEqual(p2.fps, 24)
        self.assertEqual(len(p2.v_clips), 1)
        self.assertEqual(len(p2.a_clips), 1)
        self.assertEqual(p2.a_clips[0].src, "b.mp3")


if __name__ == "__main__":
    unittest.main()

