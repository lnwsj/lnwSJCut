import unittest

from core.model import Clip, new_id
from core.timeline import move_clip_before, split_clip, total_duration


class TestTimeline(unittest.TestCase):
    def test_split_clip(self):
        c = Clip(id=new_id(), src="a.mp4", in_sec=0.0, out_sec=10.0)
        clips, sel, msg = split_clip([c], c.id, 3.0)
        self.assertEqual(len(clips), 2)
        self.assertAlmostEqual(clips[0].dur, 3.0)
        self.assertAlmostEqual(clips[1].dur, 7.0)
        self.assertIsNotNone(sel)
        self.assertEqual(msg, "Split แล้ว")

    def test_move_before(self):
        a = Clip(id="a", src="a.mp4", in_sec=0, out_sec=1)
        b = Clip(id="b", src="b.mp4", in_sec=0, out_sec=1)
        c = Clip(id="c", src="c.mp4", in_sec=0, out_sec=1)
        out = move_clip_before([a, b, c], "c", "b")
        self.assertEqual([x.id for x in out], ["a", "c", "b"])

    def test_total_duration(self):
        a = Clip(id="a", src="a.mp4", in_sec=0, out_sec=1.5)
        b = Clip(id="b", src="b.mp4", in_sec=2, out_sec=5)
        self.assertAlmostEqual(total_duration([a, b]), 4.5)


if __name__ == "__main__":
    unittest.main()
