from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .model import Clip


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_video: bool
    has_audio: bool


class FFmpegNotFound(RuntimeError):
    """Raised when ffmpeg/ffprobe cannot be located."""
    pass


def _which(name: str, local_bin: Path) -> Optional[str]:
    local = local_bin / name
    if local.exists():
        return str(local)
    return shutil.which(name)


def resolve_ffmpeg_bins(project_root: Path) -> Tuple[str, str]:
    """Return (ffmpeg_path, ffprobe_path). Prefer ./bin, fallback to PATH."""
    
    # 1. Check custom path first
    # hardcoded path as requested
    custom_root = Path(r"G:\ไดรฟ์ของฉัน\work\คอร์สติ๊กต๊อก2026\คอร์สออนไลน์\Tiktok2026Online")
    if os.name == "nt":
        ffmpeg_cust = _which("ffmpeg.exe", custom_root) or _which("ffmpeg", custom_root)
        ffprobe_cust = _which("ffprobe.exe", custom_root) or _which("ffprobe", custom_root)
        if ffmpeg_cust and ffprobe_cust:
            return ffmpeg_cust, ffprobe_cust

    # 2. Check local ./bin
    local_bin = project_root / "bin"

    if os.name == "nt":
        ffmpeg = _which("ffmpeg.exe", local_bin) or _which("ffmpeg", local_bin)
        ffprobe = _which("ffprobe.exe", local_bin) or _which("ffprobe", local_bin)
    else:
        ffmpeg = _which("ffmpeg", local_bin)
        ffprobe = _which("ffprobe", local_bin)

    if not ffmpeg or not ffprobe:
        # 3. Fallback to system PATH (os.environ["PATH"])
        # _which checking shutil.which covers this if local_bin failed, wait _which implementation:
        # def _which(name, local_bin): ... return shutil.which(name)
        # Yes, existing _which does check PATH if not found in local_bin.
        # But wait, existing _which logic:
        # if local.exists(): return str(local)
        # return shutil.which(name)
        # So it checks local, then PATH.
        # We need to ensure we don't error out if local bin doesn't have it, but do error if custom path didn't have it (which we did by creating step 1).
        pass

    if not ffmpeg or not ffprobe:
        raise FFmpegNotFound(f"ไม่พบ ffmpeg/ffprobe\nCustom path: {custom_root}\nLocal bin: {local_bin}\nและไม่อยู่ใน PATH")
    return ffmpeg, ffprobe


def probe_media(ffprobe_path: str, src: str) -> MediaInfo:
    """Use ffprobe to get duration and whether streams exist."""
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        src,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(p.stdout)

    fmt = data.get("format", {}) or {}
    dur = float(fmt.get("duration", 0.0) or 0.0)

    streams = data.get("streams", []) or []
    has_v = any(s.get("codec_type") == "video" for s in streams)
    has_a = any(s.get("codec_type") == "audio" for s in streams)

    return MediaInfo(duration=dur, has_video=has_v, has_audio=has_a)


def build_export_command(ffmpeg_path: str, clips: List[Clip], out_path: str) -> List[str]:
    """
    Build an ffmpeg command to concatenate trimmed segments.

    MVP assumptions:
    - Each segment has both video+audio streams.
    """
    if not clips:
        raise ValueError("Timeline ว่าง")

    # One input per unique source
    srcs: List[str] = []
    src_to_idx: Dict[str, int] = {}
    for c in clips:
        if c.src not in src_to_idx:
            src_to_idx[c.src] = len(srcs)
            srcs.append(c.src)

    args: List[str] = [ffmpeg_path, "-y"]
    for s in srcs:
        args += ["-i", s]

    parts: List[str] = []
    seg_labels: List[str] = []
    for i, c in enumerate(clips):
        idx = src_to_idx[c.src]
        v = f"v{i}"
        a = f"a{i}"
        parts.append(
            f"[{idx}:v]trim=start={c.in_sec}:end={c.out_sec},setpts=PTS-STARTPTS[{v}]"
        )
        parts.append(
            f"[{idx}:a]atrim=start={c.in_sec}:end={c.out_sec},asetpts=PTS-STARTPTS[{a}]"
        )
        seg_labels.append(f"[{v}][{a}]")

    parts.append(f"{''.join(seg_labels)}concat=n={len(clips)}:v=1:a=1[v][a]")
    filter_complex = ";".join(parts)

    args += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        out_path,
    ]
    return args


def export_timeline(ffmpeg_path: str, clips: List[Clip], out_path: str) -> None:
    cmd = build_export_command(ffmpeg_path, clips, out_path)
    subprocess.run(cmd, check=True)


def build_export_command_project(
    ffmpeg_path: str,
    ffprobe_path: str,
    v_clips: List[Clip],
    a_clips: List[Clip],
    out_path: str,
    audio_mode: str = "mix",  # "mix" | "a1_only" | "v1_only"
) -> List[str]:
    """
    Build an ffmpeg command to export a project with separate V1/A1 tracks.

    Assumptions (MVP):
    - V1 is a linear concat of trimmed segments (no gaps).
    - A1 is a linear concat of trimmed audio segments (no gaps).
    - Output duration follows V1 (video timeline).
    """
    if not v_clips:
        raise ValueError("V1 ว่าง")

    # One input per unique source across both tracks
    srcs: List[str] = []
    src_to_idx: Dict[str, int] = {}
    for c in [*v_clips, *a_clips]:
        if c.src not in src_to_idx:
            src_to_idx[c.src] = len(srcs)
            srcs.append(c.src)

    # Probe stream presence for each unique source
    infos: Dict[str, MediaInfo] = {}
    for s in srcs:
        infos[s] = probe_media(ffprobe_path, s)

    for c in v_clips:
        if not infos[c.src].has_video:
            raise ValueError(f"V1 ต้องเป็นไฟล์ที่มี video stream: {Path(c.src).name}")

    for c in a_clips:
        if not infos[c.src].has_audio:
            raise ValueError(f"A1 ต้องเป็นไฟล์ที่มี audio stream: {Path(c.src).name}")

    args: List[str] = [ffmpeg_path, "-y"]
    for s in srcs:
        args += ["-i", s]

    parts: List[str] = []

    # ----- V1 concat -----
    need_v1_audio = audio_mode in ("mix", "v1_only")
    v_video_labels: List[str] = []
    v_av_labels: List[str] = []
    for i, c in enumerate(v_clips):
        idx = src_to_idx[c.src]
        v = f"v{i}"
        parts.append(f"[{idx}:v]trim=start={c.in_sec}:end={c.out_sec},setpts=PTS-STARTPTS[{v}]")
        v_video_labels.append(f"[{v}]")

        if need_v1_audio:
            a = f"va{i}"
            if infos[c.src].has_audio:
                parts.append(
                    f"[{idx}:a]atrim=start={c.in_sec}:end={c.out_sec},asetpts=PTS-STARTPTS,"
                    f"aformat=sample_rates=48000:channel_layouts=stereo[{a}]"
                )
            else:
                # Silence segment matching the clip duration.
                parts.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=start=0:end={c.dur},asetpts=PTS-STARTPTS[{a}]"
                )
            v_av_labels.append(f"[{v}][{a}]")

    if need_v1_audio:
        parts.append(f"{''.join(v_av_labels)}concat=n={len(v_clips)}:v=1:a=1[v][a_vid]")
    else:
        parts.append(f"{''.join(v_video_labels)}concat=n={len(v_clips)}:v=1:a=0[v]")

    # ----- A1 concat (audio only) -----
    v_total = sum(c.dur for c in v_clips)
    have_a1 = bool(a_clips)
    if have_a1:
        a_seg_labels: List[str] = []
        for j, c in enumerate(a_clips):
            idx = src_to_idx[c.src]
            a = f"a{j}"
            parts.append(
                f"[{idx}:a]atrim=start={c.in_sec}:end={c.out_sec},asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates=48000:channel_layouts=stereo[{a}]"
            )
            a_seg_labels.append(f"[{a}]")
        parts.append(f"{''.join(a_seg_labels)}concat=n={len(a_clips)}:v=0:a=1[a_a1]")

    if audio_mode == "mix":
        if have_a1:
            parts.append(f"[a_vid]atrim=start=0:end={v_total}[a_vid_t]")
            parts.append(f"[a_a1]apad,atrim=start=0:end={v_total}[a_a1_t]")
            parts.append("[a_vid_t][a_a1_t]amix=inputs=2:duration=first:dropout_transition=2[a]")
        else:
            parts.append(f"[a_vid]atrim=start=0:end={v_total}[a]")
    elif audio_mode == "a1_only":
        if have_a1:
            parts.append(f"[a_a1]apad,atrim=start=0:end={v_total}[a]")
        else:
            parts.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=start=0:end={v_total},asetpts=PTS-STARTPTS[a]"
            )
    elif audio_mode == "v1_only":
        parts.append(f"[a_vid]atrim=start=0:end={v_total}[a]")
    else:
        raise ValueError(f"Unknown audio_mode: {audio_mode}")

    filter_complex = ";".join(parts)

    args += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        out_path,
    ]
    return args


def export_project(
    ffmpeg_path: str,
    ffprobe_path: str,
    v_clips: List[Clip],
    a_clips: List[Clip],
    out_path: str,
    audio_mode: str = "mix",
) -> None:
    cmd = build_export_command_project(ffmpeg_path, ffprobe_path, v_clips, a_clips, out_path, audio_mode=audio_mode)
    subprocess.run(cmd, check=True)
