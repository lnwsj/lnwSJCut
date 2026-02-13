import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.thumbnails import generate_thumbnail, generate_waveform


class TestThumbnails(unittest.TestCase):
    def test_generate_thumbnail_uses_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src.mp4"
            src.write_bytes(b"demo-video")
            cache_dir = root / "thumb_cache"
            calls = []

            def _fake_run(cmd, **_kwargs):
                calls.append(cmd)
                out = Path(cmd[-1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"png")
                return None

            with patch("core.thumbnails.subprocess.run", side_effect=_fake_run):
                p1 = generate_thumbnail("ffmpeg", str(src), 0.5, cache_dir, width=320)
                p2 = generate_thumbnail("ffmpeg", str(src), 0.5, cache_dir, width=320)

            self.assertIsNotNone(p1)
            self.assertEqual(p1, p2)
            self.assertEqual(len(calls), 1)

    def test_generate_waveform_builds_expected_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src.wav"
            src.write_bytes(b"demo-audio")
            cache_dir = root / "wave_cache"
            cmds = []

            def _fake_run(cmd, **_kwargs):
                cmds.append(cmd)
                out = Path(cmd[-1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"png")
                return None

            with patch("core.thumbnails.subprocess.run", side_effect=_fake_run):
                out = generate_waveform("ffmpeg", str(src), 1.2, 3.4, cache_dir, width=300, height=40)

            self.assertIsNotNone(out)
            self.assertEqual(len(cmds), 1)
            joined = " ".join(cmds[0])
            self.assertIn("showwavespic=s=300x40", joined)

    def test_generate_waveform_returns_none_on_ffmpeg_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src.wav"
            src.write_bytes(b"demo-audio")
            cache_dir = root / "wave_cache"

            with patch(
                "core.thumbnails.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, ["ffmpeg"]),
            ):
                out = generate_waveform("ffmpeg", str(src), 0.0, 2.0, cache_dir)

            self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()

