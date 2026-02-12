import unittest
from unittest.mock import patch

from core.ffmpeg import MediaInfo, build_export_command, build_export_command_project
from core.model import Clip, ExportSettings, Track, Transition


class TestFFmpegNoAudio(unittest.TestCase):
    def test_build_export_command_applies_speed_filters(self):
        clip = Clip(
            id="v1",
            src="hasaudio.mp4",
            in_sec=1.0,
            out_sec=5.0,
            speed=2.0,
            muted=False,
            has_audio=True,
        )
        cmd = build_export_command("ffmpeg", [clip], "out.mp4")
        joined = " ".join(cmd)
        self.assertIn("setpts=(PTS-STARTPTS)/2.000000", joined)
        self.assertIn("atempo=2.000000", joined)

    def test_build_export_command_uses_silence_when_clip_has_no_audio(self):
        clip = Clip(
            id="v1",
            src="noaudio.mp4",
            in_sec=0.0,
            out_sec=2.0,
            muted=False,
            has_audio=False,
        )
        cmd = build_export_command("ffmpeg", [clip], "out.mp4")
        joined = " ".join(cmd)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", joined)
        self.assertNotIn("[0:a]atrim", joined)

    def test_build_export_command_with_transition_adds_xfade_and_acrossfade(self):
        clips = [
            Clip(id="v1", src="a.mp4", in_sec=0.0, out_sec=2.0),
            Clip(
                id="v2",
                src="b.mp4",
                in_sec=0.0,
                out_sec=3.0,
                transition_in=Transition(kind="dissolve", duration=0.5),
            ),
        ]
        cmd = build_export_command("ffmpeg", clips, "out.mp4")
        joined = " ".join(cmd)
        self.assertIn("xfade=transition=dissolve:duration=0.500000:offset=1.500000", joined)
        self.assertIn("acrossfade=d=0.500000", joined)

    @patch("core.ffmpeg.probe_media")
    def test_build_export_command_project_a1_no_audio_falls_back_to_silence(self, probe_media):
        def _fake_probe(_ffprobe_path: str, src: str) -> MediaInfo:
            if src == "v.mp4":
                return MediaInfo(duration=5.0, has_video=True, has_audio=True)
            if src == "a_noaudio.mp4":
                return MediaInfo(duration=5.0, has_video=True, has_audio=False)
            return MediaInfo(duration=5.0, has_video=True, has_audio=True)

        probe_media.side_effect = _fake_probe

        v_clips = [
            Clip(id="v1", src="v.mp4", in_sec=0.0, out_sec=2.0, has_audio=True),
        ]
        a_clips = [
            Clip(id="a1", src="a_noaudio.mp4", in_sec=0.0, out_sec=2.0, has_audio=False),
        ]

        cmd = build_export_command_project(
            "ffmpeg",
            "ffprobe",
            v_clips,
            a_clips,
            "out.mp4",
            audio_mode="mix",
        )
        joined = " ".join(cmd)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", joined)
        self.assertIn("concat=n=1:v=0:a=1[a_a1]", joined)

    @patch("core.ffmpeg.probe_media")
    def test_build_export_command_project_transition_uses_overlap_duration(self, probe_media):
        probe_media.return_value = MediaInfo(duration=10.0, has_video=True, has_audio=True)
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
        cmd = build_export_command_project(
            "ffmpeg",
            "ffprobe",
            v_clips,
            [],
            "out.mp4",
            audio_mode="v1_only",
        )
        joined = " ".join(cmd)
        self.assertIn("xfade=transition=fade:duration=0.500000:offset=1.500000", joined)
        self.assertIn("[a_vid]atrim=start=0:end=4.5[a]", joined)

    @patch("core.ffmpeg.probe_media")
    def test_build_export_command_project_applies_resolution_and_quality_settings(self, probe_media):
        probe_media.return_value = MediaInfo(duration=10.0, has_video=True, has_audio=True)
        settings = ExportSettings(
            width=1280,
            height=720,
            video_codec="libx265",
            crf=18,
            preset="slow",
            audio_codec="aac",
            audio_bitrate="160k",
            format="mp4",
        )
        cmd = build_export_command_project(
            "ffmpeg",
            "ffprobe",
            [Clip(id="v1", src="v.mp4", in_sec=0.0, out_sec=2.0)],
            [],
            "out.mp4",
            export_settings=settings,
        )
        joined = " ".join(cmd)
        self.assertIn("scale=w=1280:h=720:force_original_aspect_ratio=decrease", joined)
        self.assertIn("pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v]", joined)
        self.assertIn("-c:v libx265", joined)
        self.assertIn("-crf 18", joined)
        self.assertIn("-preset slow", joined)
        self.assertIn("-b:a 160k", joined)
        self.assertIn("-f mp4", joined)
        self.assertIn("-movflags +faststart", joined)

    @patch("core.ffmpeg.probe_media")
    def test_build_export_command_project_webm_forces_vp9_opus(self, probe_media):
        probe_media.return_value = MediaInfo(duration=10.0, has_video=True, has_audio=True)
        settings = ExportSettings(
            format="webm",
            video_codec="libx265",
            audio_codec="aac",
            audio_bitrate="192k",
        )
        cmd = build_export_command_project(
            "ffmpeg",
            "ffprobe",
            [Clip(id="v1", src="v.mp4", in_sec=0.0, out_sec=2.0)],
            [],
            "out.webm",
            export_settings=settings,
        )
        joined = " ".join(cmd)
        self.assertIn("-c:v libvpx-vp9", joined)
        self.assertIn("-b:v 0", joined)
        self.assertIn("-c:a libopus", joined)
        self.assertIn("-f webm", joined)
        self.assertNotIn("-movflags +faststart", joined)

    @patch("core.ffmpeg.probe_media")
    def test_build_export_command_project_tracks_mix_uses_overlay_and_amix(self, probe_media):
        def _fake_probe(_ffprobe_path: str, src: str) -> MediaInfo:
            if src.endswith(".mp3"):
                return MediaInfo(duration=10.0, has_video=False, has_audio=True)
            return MediaInfo(duration=10.0, has_video=True, has_audio=True)

        probe_media.side_effect = _fake_probe

        tracks = [
            Track(
                id="v1",
                name="V1",
                kind="video",
                clips=[Clip(id="v1c", src="base.mp4", in_sec=0.0, out_sec=2.0)],
            ),
            Track(
                id="v2",
                name="V2",
                kind="video",
                clips=[Clip(id="v2c", src="overlay.mp4", in_sec=0.0, out_sec=2.0)],
            ),
            Track(
                id="a2",
                name="A2",
                kind="audio",
                clips=[Clip(id="a2c", src="music.mp3", in_sec=0.0, out_sec=2.0)],
            ),
        ]

        cmd = build_export_command_project(
            "ffmpeg",
            "ffprobe",
            [],
            [],
            "out.mp4",
            audio_mode="mix",
            tracks=tracks,
        )
        joined = " ".join(cmd)
        self.assertIn("overlay=eof_action=pass", joined)
        self.assertIn("amix=inputs=", joined)


if __name__ == "__main__":
    unittest.main()
