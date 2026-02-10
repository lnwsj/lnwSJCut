import json
import unittest
from unittest.mock import Mock, patch

from core.ffmpeg import probe_media


class TestProbeMedia(unittest.TestCase):
    @patch("core.ffmpeg.subprocess.run")
    def test_probe_media_extended_fields(self, run: Mock):
        run.return_value = Mock(
            stdout=json.dumps(
                {
                    "format": {"duration": "12.34", "size": "1234567"},
                    "streams": [
                        {
                            "codec_type": "video",
                            "width": 1920,
                            "height": 1080,
                            "r_frame_rate": "30000/1001",
                            "codec_name": "h264",
                            "pix_fmt": "yuv420p",
                            "bit_rate": "8000000",
                        },
                        {
                            "codec_type": "audio",
                            "codec_name": "aac",
                            "sample_rate": "48000",
                            "channels": 2,
                            "bit_rate": "192000",
                        },
                    ],
                }
            )
        )

        info = probe_media("ffprobe", "x.mp4")
        self.assertAlmostEqual(info.duration, 12.34)
        self.assertTrue(info.has_video)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.width, 1920)
        self.assertEqual(info.height, 1080)
        self.assertAlmostEqual(info.fps, 30000 / 1001, places=2)
        self.assertEqual(info.video_codec, "h264")
        self.assertEqual(info.audio_codec, "aac")
        self.assertEqual(info.video_bitrate, 8000000)
        self.assertEqual(info.audio_bitrate, 192000)
        self.assertEqual(info.file_size_bytes, 1234567)
        self.assertEqual(info.pixel_format, "yuv420p")
        self.assertEqual(info.sample_rate, 48000)
        self.assertEqual(info.channels, 2)


if __name__ == "__main__":
    unittest.main()

