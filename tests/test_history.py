import unittest

from core.history import HistoryEntry, HistoryManager
from core.model import Clip, Project


class TestHistoryManager(unittest.TestCase):
    def test_undo_redo_roundtrip(self):
        mgr = HistoryManager(limit=10)

        p0 = Project(v_clips=[], a_clips=[], fps=30)
        p1 = Project(v_clips=[Clip(id="c1", src="a.mp4", in_sec=0.0, out_sec=1.0)], a_clips=[], fps=30)

        mgr.record(HistoryEntry(label="Add clip", project=p0.to_dict(), selected_track=None, selected_clip_id=None))

        undo_entry = mgr.undo(current=HistoryEntry(label="(current)", project=p1.to_dict()))
        self.assertIsNotNone(undo_entry)
        self.assertEqual(undo_entry.label, "Add clip")
        self.assertEqual(undo_entry.project, p0.to_dict())
        self.assertFalse(mgr.can_undo())
        self.assertTrue(mgr.can_redo())

        redo_entry = mgr.redo(current=HistoryEntry(label="(current)", project=p0.to_dict()))
        self.assertIsNotNone(redo_entry)
        self.assertEqual(redo_entry.label, "Add clip")
        self.assertEqual(redo_entry.project, p1.to_dict())
        self.assertTrue(mgr.can_undo())
        self.assertFalse(mgr.can_redo())

    def test_record_clears_redo(self):
        mgr = HistoryManager(limit=10)

        p0 = Project(v_clips=[], a_clips=[], fps=30)
        p1 = Project(v_clips=[Clip(id="c1", src="a.mp4", in_sec=0.0, out_sec=1.0)], a_clips=[], fps=30)
        p2 = Project(v_clips=[Clip(id="c2", src="b.mp4", in_sec=0.0, out_sec=1.0)], a_clips=[], fps=30)

        mgr.record(HistoryEntry(label="A", project=p0.to_dict()))
        _ = mgr.undo(current=HistoryEntry(label="(current)", project=p1.to_dict()))
        self.assertTrue(mgr.can_redo())

        mgr.record(HistoryEntry(label="B", project=p1.to_dict()))
        self.assertFalse(mgr.can_redo())

        undo_entry = mgr.undo(current=HistoryEntry(label="(current)", project=p2.to_dict()))
        self.assertEqual(undo_entry.label, "B")

    def test_limit_drops_oldest(self):
        mgr = HistoryManager(limit=3)
        p = Project(v_clips=[], a_clips=[], fps=30)

        mgr.record(HistoryEntry(label="1", project={"n": 1}))
        mgr.record(HistoryEntry(label="2", project={"n": 2}))
        mgr.record(HistoryEntry(label="3", project={"n": 3}))
        mgr.record(HistoryEntry(label="4", project={"n": 4}))

        e4 = mgr.undo(current=HistoryEntry(label="(current)", project=p.to_dict()))
        e3 = mgr.undo(current=HistoryEntry(label="(current)", project=p.to_dict()))
        e2 = mgr.undo(current=HistoryEntry(label="(current)", project=p.to_dict()))
        e1 = mgr.undo(current=HistoryEntry(label="(current)", project=p.to_dict()))

        self.assertEqual(e4.label, "4")
        self.assertEqual(e3.label, "3")
        self.assertEqual(e2.label, "2")
        self.assertIsNone(e1)


if __name__ == "__main__":
    unittest.main()

