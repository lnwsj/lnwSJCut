import subprocess
import unittest
from unittest.mock import patch

from core.ffmpeg import export_project_with_progress, parse_ffmpeg_progress_seconds
from core.model import Clip, Track, Transition


class _FakeProc:
    def __init__(self, stderr_lines, retcode: int = 0):
        self.stderr = iter(stderr_lines)
        self._retcode = int(retcode)

    def wait(self) -> int:
        return self._retcode


class TestFFmpegExportProgress(unittest.TestCase):
    def test_parse_ffmpeg_progress_seconds(self):
        self.assertAlmostEqual(parse_ffmpeg_progress_seconds("out_time_ms=1500000"), 1.5, places=3)
        self.assertAlmostEqual(parse_ffmpeg_progress_seconds("out_time_us=250000"), 0.25, places=3)
        self.assertAlmostEqual(
            parse_ffmpeg_progress_seconds("frame=120 fps=24.0 q=23.0 time=00:00:02.34 bitrate=1200kbits/s"),
            2.34,
            places=2,
        )
        self.assertIsNone(parse_ffmpeg_progress_seconds("progress=continue"))
        self.assertIsNone(parse_ffmpeg_progress_seconds(""))

    @patch("core.ffmpeg.subprocess.Popen")
    @patch("core.ffmpeg.build_export_command_project")
    def test_export_project_with_progress_emits_updates(self, build_cmd, popen):
        build_cmd.return_value = ["ffmpeg", "-i", "v.mp4", "out.mp4"]
        popen.return_value = _FakeProc(
            [
                "out_time_ms=500000\n",
                "out_time_ms=1500000\n",
                "out_time_ms=5000000\n",  # larger than total, should clamp
                "progress=end\n",
            ],
            retcode=0,
        )

        v_clips = [Clip(id="v1", src="v.mp4", in_sec=0.0, out_sec=3.0)]
        events = []
        export_project_with_progress(
            "ffmpeg",
            "ffprobe",
            v_clips,
            [],
            "out.mp4",
            on_progress=lambda current, total: events.append((round(current, 3), round(total, 3))),
        )

        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0], (0.0, 3.0))
        self.assertEqual(events[-1], (3.0, 3.0))

        called_cmd = popen.call_args.args[0]
        self.assertIn("-progress", called_cmd)
        self.assertIn("pipe:2", called_cmd)
        self.assertIn("-nostats", called_cmd)

    @patch("core.ffmpeg.subprocess.Popen")
    @patch("core.ffmpeg.build_export_command_project")
    def test_export_project_with_progress_raises_when_ffmpeg_fails(self, build_cmd, popen):
        build_cmd.return_value = ["ffmpeg", "-i", "v.mp4", "out.mp4"]
        popen.return_value = _FakeProc(["out_time_ms=1000000\n"], retcode=1)

        v_clips = [Clip(id="v1", src="v.mp4", in_sec=0.0, out_sec=2.0)]
        with self.assertRaises(subprocess.CalledProcessError):
            export_project_with_progress("ffmpeg", "ffprobe", v_clips, [], "out.mp4")

    @patch("core.ffmpeg.subprocess.Popen")
    @patch("core.ffmpeg.build_export_command_project")
    def test_export_project_with_progress_uses_transition_total_duration(self, build_cmd, popen):
        build_cmd.return_value = ["ffmpeg", "-i", "v1.mp4", "-i", "v2.mp4", "out.mp4"]
        popen.return_value = _FakeProc(
            [
                "out_time_ms=1000000\n",
                "out_time_ms=4600000\n",
            ],
            retcode=0,
        )

        v_clips = [
            Clip(id="v1", src="v1.mp4", in_sec=0.0, out_sec=2.0),
            Clip(
                id="v2",
                src="v2.mp4",
                in_sec=0.0,
                out_sec=3.0,
                transition_in=Transition(kind="fade", duration=0.5),
            ),
        ]
        events = []
        export_project_with_progress(
            "ffmpeg",
            "ffprobe",
            v_clips,
            [],
            "out.mp4",
            on_progress=lambda current, total: events.append((round(current, 3), round(total, 3))),
        )

        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0], (0.0, 4.5))
        self.assertEqual(events[-1], (4.5, 4.5))

    @patch("core.ffmpeg.subprocess.Popen")
    @patch("core.ffmpeg.build_export_command_project")
    def test_export_project_with_progress_uses_tracks_duration_when_provided(self, build_cmd, popen):
        build_cmd.return_value = ["ffmpeg", "-i", "v.mp4", "out.mp4"]
        popen.return_value = _FakeProc(["out_time_ms=2500000\n"], retcode=0)

        tracks = [
            Track(
                id="v1",
                name="V1",
                kind="video",
                clips=[Clip(id="c1", src="v.mp4", in_sec=0.0, out_sec=3.0)],
            ),
        ]
        events = []
        export_project_with_progress(
            "ffmpeg",
            "ffprobe",
            [],
            [],
            "out.mp4",
            tracks=tracks,
            on_progress=lambda current, total: events.append((round(current, 3), round(total, 3))),
        )

        self.assertEqual(events[0], (0.0, 3.0))
        self.assertEqual(events[-1], (3.0, 3.0))


if __name__ == "__main__":
    unittest.main()
