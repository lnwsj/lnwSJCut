import unittest

from core.model import Clip, Transition, new_id
from core.timeline import (
    add_clip_end,
    duplicate_clip,
    insert_clip_before,
    move_clip_before,
    split_clip,
    split_clip_at_timeline_sec,
    total_duration,
    trim_clip,
)


class TestTimeline(unittest.TestCase):
    def test_split_clip(self):
        c = Clip(id=new_id(), src="a.mp4", in_sec=0.0, out_sec=10.0)
        clips, sel, msg = split_clip([c], c.id, 3.0)
        self.assertEqual(len(clips), 2)
        self.assertAlmostEqual(clips[0].dur, 3.0)
        self.assertAlmostEqual(clips[1].dur, 7.0)
        self.assertIsNotNone(sel)
        self.assertEqual(msg, "Split แล้ว")

    def test_split_clip_at_timeline_sec_middle(self):
        a = Clip(id="a", src="a.mp4", in_sec=0.0, out_sec=3.0)
        b = Clip(id="b", src="b.mp4", in_sec=0.0, out_sec=2.0)
        out, sel, msg = split_clip_at_timeline_sec([a, b], 1.5)
        self.assertIn("Split", msg)
        self.assertEqual(len(out), 3)
        self.assertAlmostEqual(out[0].dur, 1.5)
        self.assertAlmostEqual(out[1].dur, 1.5)
        self.assertIsNotNone(sel)

    def test_split_clip_at_timeline_sec_boundary_no_change(self):
        a = Clip(id="a", src="a.mp4", in_sec=0.0, out_sec=3.0)
        b = Clip(id="b", src="b.mp4", in_sec=0.0, out_sec=2.0)
        out, _sel, _msg = split_clip_at_timeline_sec([a, b], 3.0)
        self.assertEqual([c.id for c in out], ["a", "b"])

    def test_split_clip_at_timeline_sec_out_of_range(self):
        a = Clip(id="a", src="a.mp4", in_sec=0.0, out_sec=3.0)
        out, sel, msg = split_clip_at_timeline_sec([a], 9.0)
        self.assertEqual(out[0].id, "a")
        self.assertIsNone(sel)
        self.assertIn("out of range", msg)

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

    def test_total_duration_with_transition_overlap(self):
        a = Clip(id="a", src="a.mp4", in_sec=0.0, out_sec=3.0)
        b = Clip(
            id="b",
            src="b.mp4",
            in_sec=0.0,
            out_sec=4.0,
            transition_in=Transition(kind="fade", duration=1.0),
        )
        self.assertAlmostEqual(total_duration([a, b]), 6.0)

    def test_total_duration_transition_clamped_by_clip_length(self):
        a = Clip(id="a", src="a.mp4", in_sec=0.0, out_sec=1.2)
        b = Clip(
            id="b",
            src="b.mp4",
            in_sec=0.0,
            out_sec=2.0,
            transition_in=Transition(kind="fade", duration=5.0),
        )
        self.assertAlmostEqual(total_duration([a, b]), 2.01, places=2)

    def test_trim_clip(self):
        c = Clip(id="c", src="a.mp4", in_sec=0.0, out_sec=10.0)
        out, msg = trim_clip([c], "c", 2.0, 7.0)
        self.assertEqual(msg, "Trimmed")
        self.assertAlmostEqual(out[0].in_sec, 2.0)
        self.assertAlmostEqual(out[0].out_sec, 7.0)

    def test_trim_clip_invalid(self):
        c = Clip(id="c", src="a.mp4", in_sec=0.0, out_sec=10.0)
        out, msg = trim_clip([c], "c", 5.0, 5.01)
        self.assertEqual(out[0].in_sec, 0.0)
        self.assertIn("Trim failed", msg)

    def test_trim_clip_not_found(self):
        c = Clip(id="c", src="a.mp4", in_sec=0.0, out_sec=10.0)
        out, msg = trim_clip([c], "missing", 1.0, 3.0)
        self.assertEqual(out[0].in_sec, 0.0)
        self.assertIn("clip not found", msg)

    def test_trim_clip_no_changes(self):
        c = Clip(id="c", src="a.mp4", in_sec=1.0, out_sec=4.0)
        out, msg = trim_clip([c], "c", 1.0, 4.0)
        self.assertEqual(out[0].in_sec, 1.0)
        self.assertEqual(out[0].out_sec, 4.0)
        self.assertEqual(msg, "Trim: no changes")

    def test_duplicate_clip(self):
        a = Clip(id="a", src="a.mp4", in_sec=0, out_sec=1)
        b = Clip(id="b", src="b.mp4", in_sec=0, out_sec=1)
        out, new_id_val, msg = duplicate_clip([a, b], "a")
        self.assertIn("Duplicate", msg)
        self.assertIsNotNone(new_id_val)
        self.assertEqual([x.id for x in out][0], "a")
        self.assertEqual([x.id for x in out][2], "b")
        self.assertEqual(out[1].src, "a.mp4")
        self.assertNotEqual(out[1].id, "a")
        self.assertEqual(out[1].id, new_id_val)

    def test_add_clip_end_with_has_audio(self):
        out = add_clip_end([], "silent.mp4", 2.0, has_audio=False)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].has_audio)

    def test_insert_clip_before_with_has_audio(self):
        a = Clip(id="a", src="a.mp4", in_sec=0, out_sec=1)
        out = insert_clip_before([a], "a", "silent.mp4", 2.0, has_audio=False)
        self.assertEqual(len(out), 2)
        self.assertFalse(out[0].has_audio)


if __name__ == "__main__":
    unittest.main()
