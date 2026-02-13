import unittest

from core.model import Clip, ExportSettings, Project, Track, Transition


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
        self.assertAlmostEqual(p.v_clips[0].volume, 1.0)
        self.assertFalse(p.v_clips[0].muted)
        self.assertTrue(p.v_clips[0].has_audio)

    def test_roundtrip_new_format(self):
        v = Clip(id="v1", src="a.mp4", in_sec=0.0, out_sec=2.0, speed=1.5, volume=0.5, muted=False, has_audio=False)
        a = Clip(id="a1", src="b.mp3", in_sec=1.0, out_sec=3.0, speed=0.5, volume=1.0, muted=True, has_audio=True)
        p = Project(v_clips=[v], a_clips=[a], fps=24)

        d = p.to_dict()
        self.assertIn("v_clips", d)
        self.assertIn("a_clips", d)

        p2 = Project.from_dict(d)
        self.assertEqual(p2.fps, 24)
        self.assertEqual(len(p2.v_clips), 1)
        self.assertEqual(len(p2.a_clips), 1)
        self.assertEqual(p2.a_clips[0].src, "b.mp3")
        self.assertAlmostEqual(p2.v_clips[0].speed, 1.5)
        self.assertAlmostEqual(p2.v_clips[0].volume, 0.5)
        self.assertFalse(p2.v_clips[0].muted)
        self.assertFalse(p2.v_clips[0].has_audio)
        self.assertAlmostEqual(p2.a_clips[0].speed, 0.5)
        self.assertAlmostEqual(p2.a_clips[0].volume, 1.0)
        self.assertTrue(p2.a_clips[0].muted)
        self.assertTrue(p2.a_clips[0].has_audio)

    def test_clip_speed_from_dict_defaults_and_clamps(self):
        c = Clip.from_dict({"id": "x", "src": "a.mp4", "in_sec": 0.0, "out_sec": 4.0, "speed": "bad"})
        self.assertAlmostEqual(c.speed, 1.0)
        self.assertAlmostEqual(c.dur, 4.0)

        c2 = Clip.from_dict({"id": "y", "src": "a.mp4", "in_sec": 0.0, "out_sec": 4.0, "speed": 99})
        self.assertAlmostEqual(c2.speed, 4.0)
        self.assertAlmostEqual(c2.dur, 1.0)

    def test_clip_transition_roundtrip(self):
        v = Clip(
            id="v1",
            src="a.mp4",
            in_sec=0.0,
            out_sec=2.0,
            transition_in=Transition(kind="dissolve", duration=0.75),
        )
        p = Project(v_clips=[v], a_clips=[], fps=30)
        p2 = Project.from_dict(p.to_dict())
        self.assertEqual(p2.v_clips[0].transition_in.kind, "dissolve")
        self.assertAlmostEqual(p2.v_clips[0].transition_in.duration, 0.75)

    def test_clip_transition_backward_compat_string(self):
        p = Project.from_dict(
            {
                "fps": 30,
                "v_clips": [
                    {"id": "1", "src": "a.mp4", "in_sec": 0.0, "out_sec": 1.0, "transition_in": "fade"},
                ],
                "a_clips": [],
            }
        )
        self.assertIsNotNone(p.v_clips[0].transition_in)
        self.assertEqual(p.v_clips[0].transition_in.kind, "fade")
        self.assertAlmostEqual(p.v_clips[0].transition_in.duration, 0.5)

    def test_export_settings_roundtrip(self):
        s = ExportSettings(
            width=1920,
            height=1080,
            video_codec="libx264",
            crf=20,
            audio_codec="aac",
            audio_bitrate="192k",
            format="mp4",
            preset="slow",
        )
        s2 = ExportSettings.from_dict(s.to_dict())
        self.assertEqual(s2.width, 1920)
        self.assertEqual(s2.height, 1080)
        self.assertEqual(s2.video_codec, "libx264")
        self.assertEqual(s2.crf, 20)
        self.assertEqual(s2.audio_codec, "aac")
        self.assertEqual(s2.audio_bitrate, "192k")
        self.assertEqual(s2.format, "mp4")
        self.assertEqual(s2.preset, "slow")

    def test_export_settings_from_dict_invalid_values(self):
        s = ExportSettings.from_dict({"width": "x", "height": -10, "crf": "bad"})
        self.assertEqual(s.width, 0)
        self.assertEqual(s.height, 0)
        self.assertEqual(s.crf, 23)

    def test_project_from_dict_tracks_format(self):
        d = {
            "fps": 30,
            "tracks": [
                {
                    "id": "v1",
                    "name": "V1",
                    "kind": "video",
                    "clips": [{"id": "c1", "src": "a.mp4", "in_sec": 0.0, "out_sec": 1.0}],
                },
                {
                    "id": "v2",
                    "name": "V2",
                    "kind": "video",
                    "clips": [{"id": "c2", "src": "b.mp4", "in_sec": 0.0, "out_sec": 1.5}],
                    "visible": False,
                },
                {
                    "id": "a1",
                    "name": "A1",
                    "kind": "audio",
                    "clips": [{"id": "a1c", "src": "m.mp3", "in_sec": 0.0, "out_sec": 1.0}],
                    "muted": True,
                },
            ],
        }
        p = Project.from_dict(d)
        self.assertEqual(len(p.video_tracks), 2)
        self.assertEqual(len(p.audio_tracks), 1)
        self.assertEqual(p.video_tracks[1].name, "V2")
        self.assertFalse(p.video_tracks[1].visible)
        self.assertTrue(p.audio_tracks[0].muted)
        self.assertEqual(p.v_clips[0].src, "a.mp4")
        self.assertEqual(p.a_clips[0].src, "m.mp3")

    def test_project_track_add_remove_keeps_minimum_kind(self):
        p = Project(v_clips=[], a_clips=[])
        self.assertEqual(len(p.video_tracks), 1)
        self.assertEqual(len(p.audio_tracks), 1)

        v2 = p.add_track("video")
        a2 = p.add_track("audio")
        self.assertEqual(len(p.video_tracks), 2)
        self.assertEqual(len(p.audio_tracks), 2)
        self.assertEqual([t.kind for t in p.tracks], ["video", "video", "audio", "audio"])

        self.assertTrue(p.remove_track(v2.id))
        self.assertTrue(p.remove_track(a2.id))
        self.assertEqual(len(p.video_tracks), 1)
        self.assertEqual(len(p.audio_tracks), 1)

        self.assertFalse(p.remove_track(p.video_tracks[0].id))
        self.assertFalse(p.remove_track(p.audio_tracks[0].id))

    def test_project_move_track_reorders_within_kind(self):
        p = Project(v_clips=[], a_clips=[])
        v2 = p.add_track("video")
        v3 = p.add_track("video")

        self.assertEqual([t.name for t in p.video_tracks], ["V1", v2.name, v3.name])

        # Move V3 above V2.
        self.assertTrue(p.move_track(v3.id, -1))
        self.assertEqual([t.name for t in p.video_tracks], ["V1", v3.name, v2.name])

        # Boundary: cannot move further up within its kind.
        self.assertFalse(p.move_track(p.video_tracks[0].id, -1))

    def test_project_constructor_accepts_track_objects(self):
        t = Track(id="v2", name="V2", kind="video", clips=[Clip(id="x", src="a.mp4", in_sec=0.0, out_sec=2.0)])
        p = Project(tracks=[t], fps=24)
        self.assertEqual(p.fps, 24)
        self.assertEqual(len(p.video_tracks), 1)
        self.assertEqual(len(p.audio_tracks), 1)  # auto-created minimum A track
        self.assertEqual(p.video_tracks[0].id, "v2")


if __name__ == "__main__":
    unittest.main()
