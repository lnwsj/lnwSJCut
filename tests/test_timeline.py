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

    def test_split_clip_respects_speed(self):
        c = Clip(id=new_id(), src="a.mp4", in_sec=10.0, out_sec=14.0, speed=2.0)
        clips, _sel, msg = split_clip([c], c.id, 1.0)
        self.assertIn("Split", msg)
        self.assertEqual(len(clips), 2)
        self.assertAlmostEqual(clips[0].in_sec, 10.0)
        self.assertAlmostEqual(clips[0].out_sec, 12.0)
        self.assertAlmostEqual(clips[1].in_sec, 12.0)
        self.assertAlmostEqual(clips[1].out_sec, 14.0)
        self.assertAlmostEqual(clips[0].dur, 1.0)
        self.assertAlmostEqual(clips[1].dur, 1.0)

    def test_split_clip_transition_stays_on_left_piece(self):
        a = Clip(id="a", src="a.mp4", in_sec=0.0, out_sec=4.0)
        b = Clip(
            id="b",
            src="b.mp4",
            in_sec=0.0,
            out_sec=4.0,
            transition_in=Transition(kind="dissolve", duration=0.5),
        )
        out, _sel, msg = split_clip([a, b], "b", 1.5)
        self.assertIn("Split", msg)
        self.assertEqual(len(out), 3)
        self.assertIsNotNone(out[1].transition_in)
        self.assertEqual(out[1].transition_in.kind, "dissolve")
        self.assertAlmostEqual(out[1].transition_in.duration, 0.5)
        self.assertIsNone(out[2].transition_in)

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

    def test_move_before_clears_transition_on_first_clip(self):
        a = Clip(id="a", src="a.mp4", in_sec=0.0, out_sec=2.0)
        b = Clip(
            id="b",
            src="b.mp4",
            in_sec=0.0,
            out_sec=2.0,
            transition_in=Transition(kind="fade", duration=0.4),
        )
        out = move_clip_before([a, b], "b", "a")
        self.assertEqual([x.id for x in out], ["b", "a"])
        self.assertIsNone(out[0].transition_in)

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

    def test_trim_clip_allows_custom_min_piece(self):
        c = Clip(id="c", src="a.mp4", in_sec=0.0, out_sec=10.0)
        out, msg = trim_clip([c], "c", 5.0, 5.03, min_piece_sec=0.02)
        self.assertEqual(msg, "Trimmed")
        self.assertAlmostEqual(out[0].in_sec, 5.0)
        self.assertAlmostEqual(out[0].out_sec, 5.03)

    def test_trim_clip_preserves_other_fields(self):
        p = Clip(id="p", src="p.mp4", in_sec=0.0, out_sec=2.0)
        c = Clip(
            id="c",
            src="a.mp4",
            in_sec=0.0,
            out_sec=10.0,
            speed=1.75,
            volume=0.35,
            muted=True,
            has_audio=False,
            transition_in=Transition(kind="fade", duration=0.4),
        )
        out, msg = trim_clip([p, c], "c", 2.0, 6.0)
        self.assertEqual(msg, "Trimmed")
        self.assertAlmostEqual(out[1].in_sec, 2.0)
        self.assertAlmostEqual(out[1].out_sec, 6.0)
        self.assertAlmostEqual(out[1].speed, 1.75)
        self.assertAlmostEqual(out[1].volume, 0.35)
        self.assertTrue(out[1].muted)
        self.assertFalse(out[1].has_audio)
        self.assertIsNotNone(out[1].transition_in)
        self.assertEqual(out[1].transition_in.kind, "fade")
        self.assertAlmostEqual(out[1].transition_in.duration, 0.4)

    def test_trim_clip_clamps_transition_duration(self):
        a = Clip(id="a", src="a.mp4", in_sec=0.0, out_sec=1.0)
        b = Clip(
            id="b",
            src="b.mp4",
            in_sec=0.0,
            out_sec=2.0,
            transition_in=Transition(kind="fade", duration=0.8),
        )
        out, msg = trim_clip([a, b], "b", 0.0, 0.2, min_piece_sec=0.01)
        self.assertEqual(msg, "Trimmed")
        self.assertIsNotNone(out[1].transition_in)
        self.assertAlmostEqual(out[1].transition_in.duration, 0.19, places=2)

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

    def test_duplicate_clip_new_copy_has_no_transition(self):
        p = Clip(id="p", src="p.mp4", in_sec=0.0, out_sec=2.0)
        a = Clip(
            id="a",
            src="a.mp4",
            in_sec=0.0,
            out_sec=2.0,
            transition_in=Transition(kind="fade", duration=0.4),
        )
        out, new_id_val, _msg = duplicate_clip([p, a], "a")
        self.assertIsNotNone(new_id_val)
        self.assertIsNotNone(out[1].transition_in)
        self.assertIsNone(out[2].transition_in)

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
