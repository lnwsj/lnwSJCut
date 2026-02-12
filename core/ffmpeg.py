from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .model import Clip, ExportSettings, Track, transition_overlap_sec
from .timeline import total_duration


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    video_bitrate: int = 0
    audio_bitrate: int = 0
    file_size_bytes: int = 0
    pixel_format: str = ""
    sample_rate: int = 0
    channels: int = 0


class FFmpegNotFound(RuntimeError):
    """Raised when ffmpeg/ffprobe cannot be located."""
    pass


_PROGRESS_OUT_TIME_RE = re.compile(r"out_time_(?:ms|us)=(\d+)")
_PROGRESS_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_BITRATE_RE = re.compile(r"^\d+(?:k|m)$", re.IGNORECASE)
_X26X_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
}


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
    # ffprobe outputs JSON as UTF-8. On Thai Windows consoles the default
    # codepage can cause decode errors, so force UTF-8 here.
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    data = json.loads(p.stdout)

    fmt = data.get("format", {}) or {}
    dur = float(fmt.get("duration", 0.0) or 0.0)

    file_size_bytes = 0
    try:
        file_size_bytes = int(fmt.get("size", 0) or 0)
    except Exception:
        file_size_bytes = 0
    if not file_size_bytes:
        try:
            file_size_bytes = Path(src).stat().st_size
        except Exception:
            file_size_bytes = 0

    streams = data.get("streams", []) or []
    has_v = any(s.get("codec_type") == "video" for s in streams)
    has_a = any(s.get("codec_type") == "audio" for s in streams)

    width = 0
    height = 0
    fps = 0.0
    video_codec = ""
    audio_codec = ""
    video_bitrate = 0
    audio_bitrate = 0
    pixel_format = ""
    sample_rate = 0
    channels = 0

    def _ratio_to_fps(v: str) -> float:
        try:
            num, den = str(v or "0/1").split("/")
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        except Exception:
            return 0.0

    for s in streams:
        if s.get("codec_type") == "video" and not video_codec:
            try:
                width = int(s.get("width", 0) or 0)
            except Exception:
                width = 0
            try:
                height = int(s.get("height", 0) or 0)
            except Exception:
                height = 0

            fps = _ratio_to_fps(s.get("r_frame_rate") or s.get("avg_frame_rate") or "0/1")
            video_codec = str(s.get("codec_name", "") or "")
            pixel_format = str(s.get("pix_fmt", "") or "")
            try:
                video_bitrate = int(s.get("bit_rate", 0) or 0)
            except Exception:
                video_bitrate = 0
        elif s.get("codec_type") == "audio" and not audio_codec:
            audio_codec = str(s.get("codec_name", "") or "")
            try:
                sample_rate = int(s.get("sample_rate", 0) or 0)
            except Exception:
                sample_rate = 0
            try:
                channels = int(s.get("channels", 0) or 0)
            except Exception:
                channels = 0
            try:
                audio_bitrate = int(s.get("bit_rate", 0) or 0)
            except Exception:
                audio_bitrate = 0

    return MediaInfo(
        duration=dur,
        has_video=has_v,
        has_audio=has_a,
        width=width,
        height=height,
        fps=fps,
        video_codec=video_codec,
        audio_codec=audio_codec,
        video_bitrate=video_bitrate,
        audio_bitrate=audio_bitrate,
        file_size_bytes=file_size_bytes,
        pixel_format=pixel_format,
        sample_rate=sample_rate,
        channels=channels,
    )


def parse_ffmpeg_progress_seconds(line: str) -> Optional[float]:
    """
    Parse FFmpeg progress seconds from a stderr/progress line.

    Supports:
    - -progress pipe lines: out_time_ms=..., out_time_us=...
    - regular stats lines: time=HH:MM:SS.xx
    """
    text = str(line or "").strip()
    if not text:
        return None

    m_out = _PROGRESS_OUT_TIME_RE.search(text)
    if m_out:
        try:
            # FFmpeg's out_time_ms/out_time_us are microseconds in practice.
            return max(0.0, float(m_out.group(1)) / 1_000_000.0)
        except Exception:
            return None

    m_time = _PROGRESS_TIME_RE.search(text)
    if not m_time:
        return None
    try:
        hh = float(m_time.group(1))
        mm = float(m_time.group(2))
        ss = float(m_time.group(3))
        return max(0.0, hh * 3600.0 + mm * 60.0 + ss)
    except Exception:
        return None


def _normalize_export_settings(export_settings: Optional[ExportSettings]) -> ExportSettings:
    if export_settings is None:
        raw = ExportSettings()
    else:
        raw = ExportSettings.from_dict(export_settings.to_dict())

    fmt = str(raw.format or "mp4").strip().lower()
    if fmt not in ("mp4", "mov", "webm"):
        fmt = "mp4"

    try:
        width = max(0, int(raw.width or 0))
    except Exception:
        width = 0
    try:
        height = max(0, int(raw.height or 0))
    except Exception:
        height = 0
    # Scale is only valid when both dimensions are provided.
    if width <= 0 or height <= 0:
        width = 0
        height = 0

    try:
        crf = max(0, min(51, int(raw.crf if raw.crf is not None else 23)))
    except Exception:
        crf = 23
    preset = str(raw.preset or "medium").strip().lower()
    if preset not in _X26X_PRESETS:
        preset = "medium"

    audio_bitrate = str(raw.audio_bitrate or "192k").strip().lower()
    if not _AUDIO_BITRATE_RE.match(audio_bitrate):
        audio_bitrate = "192k"

    if fmt == "webm":
        video_codec = "libvpx-vp9"
        audio_codec = "libopus"
    else:
        video_codec = str(raw.video_codec or "libx264").strip().lower()
        if video_codec not in ("libx264", "libx265"):
            video_codec = "libx264"
        audio_codec = "aac"

    return ExportSettings(
        width=width,
        height=height,
        video_codec=video_codec,
        crf=crf,
        audio_codec=audio_codec,
        audio_bitrate=audio_bitrate,
        format=fmt,
        preset=preset,
    )


def _append_final_video_filter(parts: List[str], source_video_label: str, settings: ExportSettings) -> None:
    if settings.width > 0 and settings.height > 0:
        w = int(settings.width)
        h = int(settings.height)
        parts.append(
            f"[{source_video_label}]setpts=PTS-STARTPTS,"
            f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v]"
        )
    else:
        parts.append(f"[{source_video_label}]setpts=PTS-STARTPTS[v]")


def _build_output_encode_args(settings: ExportSettings) -> List[str]:
    args: List[str] = [
        "-c:v",
        settings.video_codec,
    ]
    if settings.video_codec in ("libx264", "libx265"):
        args += [
            "-crf",
            str(settings.crf),
            "-preset",
            settings.preset,
        ]
    elif settings.video_codec == "libvpx-vp9":
        # For VP9 quality mode, keep bitrate unconstrained and rely on CRF.
        args += [
            "-crf",
            str(settings.crf),
            "-b:v",
            "0",
        ]

    args += [
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        settings.audio_codec,
        "-b:a",
        settings.audio_bitrate,
    ]
    if settings.format in ("mp4", "mov"):
        args += [
            "-movflags",
            "+faststart",
        ]
    args += [
        "-f",
        settings.format,
    ]
    return args


def _xfade_name(kind: str) -> str:
    k = str(kind or "").strip().lower()
    if k == "dissolve":
        return "dissolve"
    if k == "crossfade":
        return "fade"
    return "fade"


def _build_transition_chain(
    parts: List[str],
    clips: List[Clip],
    video_labels: List[str],
    audio_labels: Optional[List[str]],
) -> Tuple[str, Optional[str], float]:
    """
    Build a mixed hard-cut/transition chain and return final labels plus duration.
    """
    if not clips:
        raise ValueError("Timeline ว่าง")
    if len(video_labels) != len(clips):
        raise ValueError("video labels mismatch")
    if audio_labels is not None and len(audio_labels) != len(clips):
        raise ValueError("audio labels mismatch")

    curr_v = video_labels[0]
    curr_a = audio_labels[0] if audio_labels else None
    curr_total = float(clips[0].dur)

    for i in range(1, len(clips)):
        next_v = video_labels[i]
        next_a = audio_labels[i] if audio_labels else None
        overlap = transition_overlap_sec(clips[i - 1], clips[i])

        if overlap > 0.0:
            trans = getattr(clips[i], "transition_in", None)
            xfade = _xfade_name(getattr(trans, "kind", "fade"))
            out_v = f"vx{i}"
            offset = max(0.0, curr_total - overlap)
            parts.append(
                f"[{curr_v}][{next_v}]xfade=transition={xfade}:duration={overlap:.6f}:offset={offset:.6f}[{out_v}]"
            )
            curr_v = out_v

            if curr_a is not None and next_a is not None:
                out_a = f"ax{i}"
                parts.append(f"[{curr_a}][{next_a}]acrossfade=d={overlap:.6f}[{out_a}]")
                curr_a = out_a

            curr_total = curr_total + float(clips[i].dur) - overlap
        else:
            out_v = f"vc{i}"
            parts.append(f"[{curr_v}][{next_v}]concat=n=2:v=1:a=0[{out_v}]")
            curr_v = out_v

            if curr_a is not None and next_a is not None:
                out_a = f"ac{i}"
                parts.append(f"[{curr_a}][{next_a}]concat=n=2:v=0:a=1[{out_a}]")
                curr_a = out_a

            curr_total = curr_total + float(clips[i].dur)

    return curr_v, curr_a, max(0.0, curr_total)


def build_export_command(
    ffmpeg_path: str,
    clips: List[Clip],
    out_path: str,
    export_settings: Optional[ExportSettings] = None,
) -> List[str]:
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

    settings = _normalize_export_settings(export_settings)
    parts: List[str] = []
    v_labels: List[str] = []
    a_labels: List[str] = []
    for i, c in enumerate(clips):
        idx = src_to_idx[c.src]
        v = f"v{i}"
        a = f"a{i}"
        v_labels.append(v)
        a_labels.append(a)
        parts.append(
            f"[{idx}:v]trim=start={c.in_sec}:end={c.out_sec},setpts=PTS-STARTPTS[{v}]"
        )
        vol = max(0.0, float(getattr(c, "volume", 1.0) or 1.0))
        muted = bool(getattr(c, "muted", False))
        has_audio = bool(getattr(c, "has_audio", True))
        if muted or not has_audio:
            parts.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=start=0:end={c.dur},asetpts=PTS-STARTPTS[{a}]"
            )
        else:
            parts.append(
                f"[{idx}:a]atrim=start={c.in_sec}:end={c.out_sec},asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates=48000:channel_layouts=stereo,volume={vol:.2f}[{a}]"
            )
    final_v, final_a, _total = _build_transition_chain(parts, clips, v_labels, a_labels)
    _append_final_video_filter(parts, final_v, settings)
    if final_a is None:
        raise ValueError("Missing audio chain")
    parts.append(f"[{final_a}]asetpts=PTS-STARTPTS[a]")
    filter_complex = ";".join(parts)

    args += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "[a]",
    ]
    args += _build_output_encode_args(settings)
    args += [out_path]
    return args


def export_timeline(
    ffmpeg_path: str,
    clips: List[Clip],
    out_path: str,
    export_settings: Optional[ExportSettings] = None,
) -> None:
    cmd = build_export_command(ffmpeg_path, clips, out_path, export_settings=export_settings)
    subprocess.run(cmd, check=True)


def _build_export_command_tracks(
    ffmpeg_path: str,
    ffprobe_path: str,
    tracks: List[Track],
    out_path: str,
    audio_mode: str = "mix",
    export_settings: Optional[ExportSettings] = None,
) -> List[str]:
    """
    Build command for project tracks (multiple video/audio tracks).
    """
    settings = _normalize_export_settings(export_settings)
    all_tracks = [t for t in tracks if isinstance(t, Track)]
    if not all_tracks:
        raise ValueError("No tracks")

    video_tracks = [t for t in all_tracks if t.kind == "video"]
    audio_tracks = [t for t in all_tracks if t.kind == "audio"]
    if not video_tracks:
        raise ValueError("No video tracks")

    base_candidates = [t for t in video_tracks if t.visible and t.clips]
    if not base_candidates:
        base_candidates = [t for t in video_tracks if t.clips]
    if not base_candidates:
        raise ValueError("V1 is empty")
    base_track = base_candidates[0]

    srcs: List[str] = []
    src_to_idx: Dict[str, int] = {}
    for t in all_tracks:
        for c in t.clips:
            if c.src not in src_to_idx:
                src_to_idx[c.src] = len(srcs)
                srcs.append(c.src)

    infos: Dict[str, MediaInfo] = {}
    for s in srcs:
        infos[s] = probe_media(ffprobe_path, s)

    for t in video_tracks:
        for c in t.clips:
            if not infos[c.src].has_video:
                raise ValueError(f"{t.name} requires video stream: {Path(c.src).name}")

    args: List[str] = [ffmpeg_path, "-y"]
    for s in srcs:
        args += ["-i", s]

    parts: List[str] = []
    video_outputs: List[Tuple[Track, str, Optional[str], float]] = []
    audio_outputs: List[Tuple[Track, str, float]] = []

    for ti, t in enumerate(video_tracks):
        if not t.clips:
            continue
        v_labels: List[str] = []
        a_labels: List[str] = []
        for i, c in enumerate(t.clips):
            idx = src_to_idx[c.src]
            v = f"tv{ti}_{i}"
            a = f"ta{ti}_{i}"
            v_labels.append(v)
            a_labels.append(a)
            parts.append(f"[{idx}:v]trim=start={c.in_sec}:end={c.out_sec},setpts=PTS-STARTPTS[{v}]")

            vol = max(0.0, float(getattr(c, "volume", 1.0) or 1.0))
            muted = bool(getattr(c, "muted", False)) or bool(t.muted) or (not t.visible)
            has_audio = bool(getattr(c, "has_audio", True)) and infos[c.src].has_audio
            if muted or not has_audio:
                parts.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=start=0:end={c.dur},asetpts=PTS-STARTPTS[{a}]"
                )
            else:
                parts.append(
                    f"[{idx}:a]atrim=start={c.in_sec}:end={c.out_sec},asetpts=PTS-STARTPTS,"
                    f"aformat=sample_rates=48000:channel_layouts=stereo,volume={vol:.2f}[{a}]"
                )

        out_v, out_a, t_dur = _build_transition_chain(parts, t.clips, v_labels, a_labels)
        video_outputs.append((t, out_v, out_a, t_dur))

    if not video_outputs:
        raise ValueError("V1 is empty")

    base_entry = next((x for x in video_outputs if x[0].id == base_track.id), video_outputs[0])
    final_v = base_entry[1]
    v_total = float(base_entry[3])
    for oi, (t, ov, _oa, _dur) in enumerate(video_outputs):
        if t.id == base_track.id or not t.visible:
            continue
        out = f"vov{oi}"
        parts.append(f"[{final_v}][{ov}]overlay=eof_action=pass[{out}]")
        final_v = out

    _append_final_video_filter(parts, final_v, settings)

    for ai, t in enumerate(audio_tracks):
        if not t.clips:
            continue
        segs: List[str] = []
        t_total = 0.0
        for i, c in enumerate(t.clips):
            idx = src_to_idx[c.src]
            a = f"au{ai}_{i}"
            vol = max(0.0, float(getattr(c, "volume", 1.0) or 1.0))
            muted = bool(getattr(c, "muted", False)) or bool(t.muted) or (not t.visible)
            has_audio = bool(getattr(c, "has_audio", True)) and infos[c.src].has_audio
            if muted or not has_audio:
                parts.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=start=0:end={c.dur},asetpts=PTS-STARTPTS[{a}]"
                )
            else:
                parts.append(
                    f"[{idx}:a]atrim=start={c.in_sec}:end={c.out_sec},asetpts=PTS-STARTPTS,"
                    f"aformat=sample_rates=48000:channel_layouts=stereo,volume={vol:.2f}[{a}]"
                )
            segs.append(f"[{a}]")
            t_total += float(c.dur)

        if len(segs) == 1:
            out_a = segs[0].strip("[]")
        else:
            out_a = f"aud{ai}"
            parts.append(f"{''.join(segs)}concat=n={len(segs)}:v=0:a=1[{out_a}]")
        audio_outputs.append((t, out_a, t_total))

    selected_audio: List[str] = []
    if audio_mode == "v1_only":
        if base_entry[2] is not None:
            selected_audio = [base_entry[2]]
    elif audio_mode == "a1_only":
        if audio_outputs:
            selected_audio = [audio_outputs[0][1]]
    elif audio_mode == "mix":
        for t, _v, va, _d in video_outputs:
            if va is not None and t.visible and not t.muted:
                selected_audio.append(va)
        for t, aa, _d in audio_outputs:
            if t.visible and not t.muted:
                selected_audio.append(aa)
    else:
        raise ValueError(f"Unknown audio_mode: {audio_mode}")

    if not selected_audio:
        parts.append(
            f"anullsrc=channel_layout=stereo:sample_rate=48000,"
            f"atrim=start=0:end={max(0.0, v_total):.6f},asetpts=PTS-STARTPTS[a]"
        )
    else:
        mix_inputs: List[str] = []
        for i, lbl in enumerate(selected_audio):
            out = f"am{i}"
            parts.append(f"[{lbl}]apad,atrim=start=0:end={max(0.0, v_total):.6f}[{out}]")
            mix_inputs.append(f"[{out}]")
        if len(mix_inputs) == 1:
            parts.append(f"{mix_inputs[0]}asetpts=PTS-STARTPTS[a]")
        else:
            parts.append(f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=2[a]")

    filter_complex = ";".join(parts)
    args += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "[a]",
    ]
    args += _build_output_encode_args(settings)
    args += [out_path]
    return args


def build_export_command_project(
    ffmpeg_path: str,
    ffprobe_path: str,
    v_clips: List[Clip],
    a_clips: List[Clip],
    out_path: str,
    audio_mode: str = "mix",  # "mix" | "a1_only" | "v1_only"
    export_settings: Optional[ExportSettings] = None,
    tracks: Optional[List[Track]] = None,
) -> List[str]:
    """
    Build an ffmpeg command to export a project with separate V1/A1 tracks.

    Assumptions (MVP):
    - V1 is a linear concat of trimmed segments (no gaps).
    - A1 is a linear concat of trimmed audio segments (no gaps).
    - Output duration follows V1 (video timeline).
    """
    if tracks is not None:
        return _build_export_command_tracks(
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            tracks=list(tracks),
            out_path=out_path,
            audio_mode=audio_mode,
            export_settings=export_settings,
        )

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

    settings = _normalize_export_settings(export_settings)
    args: List[str] = [ffmpeg_path, "-y"]
    for s in srcs:
        args += ["-i", s]

    parts: List[str] = []

    # ----- V1 chain (hard cut + optional transitions) -----
    need_v1_audio = audio_mode in ("mix", "v1_only")
    v_video_labels: List[str] = []
    v_audio_labels: List[str] = []
    for i, c in enumerate(v_clips):
        idx = src_to_idx[c.src]
        v = f"v{i}"
        v_video_labels.append(v)
        parts.append(f"[{idx}:v]trim=start={c.in_sec}:end={c.out_sec},setpts=PTS-STARTPTS[{v}]")

        if need_v1_audio:
            a = f"va{i}"
            v_audio_labels.append(a)
            vol = max(0.0, float(getattr(c, "volume", 1.0) or 1.0))
            muted = bool(getattr(c, "muted", False))
            has_audio = bool(getattr(c, "has_audio", True)) and infos[c.src].has_audio
            if has_audio and not muted:
                parts.append(
                    f"[{idx}:a]atrim=start={c.in_sec}:end={c.out_sec},asetpts=PTS-STARTPTS,"
                    f"aformat=sample_rates=48000:channel_layouts=stereo,volume={vol:.2f}[{a}]"
                )
            else:
                # Silence segment matching the clip duration.
                parts.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=start=0:end={c.dur},asetpts=PTS-STARTPTS[{a}]"
                )
    final_v, final_a, v_total = _build_transition_chain(
        parts,
        v_clips,
        v_video_labels,
        v_audio_labels if need_v1_audio else None,
    )
    _append_final_video_filter(parts, final_v, settings)
    if need_v1_audio:
        if final_a is None:
            raise ValueError("Missing V1 audio chain")
        parts.append(f"[{final_a}]asetpts=PTS-STARTPTS[a_vid]")

    # ----- A1 concat (audio only) -----
    have_a1 = bool(a_clips)
    if have_a1:
        a_seg_labels: List[str] = []
        for j, c in enumerate(a_clips):
            idx = src_to_idx[c.src]
            a = f"a{j}"
            vol = max(0.0, float(getattr(c, "volume", 1.0) or 1.0))
            muted = bool(getattr(c, "muted", False))
            has_audio = bool(getattr(c, "has_audio", True)) and infos[c.src].has_audio
            if muted or not has_audio:
                parts.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=start=0:end={c.dur},asetpts=PTS-STARTPTS[{a}]"
                )
            else:
                parts.append(
                    f"[{idx}:a]atrim=start={c.in_sec}:end={c.out_sec},asetpts=PTS-STARTPTS,"
                    f"aformat=sample_rates=48000:channel_layouts=stereo,volume={vol:.2f}[{a}]"
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
    ]
    args += _build_output_encode_args(settings)
    args += [out_path]
    return args


def export_project(
    ffmpeg_path: str,
    ffprobe_path: str,
    v_clips: List[Clip],
    a_clips: List[Clip],
    out_path: str,
    audio_mode: str = "mix",
    export_settings: Optional[ExportSettings] = None,
    tracks: Optional[List[Track]] = None,
) -> None:
    cmd = build_export_command_project(
        ffmpeg_path,
        ffprobe_path,
        v_clips,
        a_clips,
        out_path,
        audio_mode=audio_mode,
        export_settings=export_settings,
        tracks=tracks,
    )
    subprocess.run(cmd, check=True)


def _export_total_duration(v_clips: List[Clip], tracks: Optional[List[Track]]) -> float:
    """
    Best-effort export duration used by progress reporting.

    For multi-track projects, follow the primary visible video track; if no visible
    video track has clips, fallback to the first non-empty video track.
    """
    if tracks:
        video_tracks = [t for t in tracks if isinstance(t, Track) and t.kind == "video" and t.clips]
        if video_tracks:
            visible = [t for t in video_tracks if t.visible]
            target = visible[0] if visible else video_tracks[0]
            return max(0.0, total_duration(target.clips))
    return max(0.0, total_duration(v_clips))


def export_project_with_progress(
    ffmpeg_path: str,
    ffprobe_path: str,
    v_clips: List[Clip],
    a_clips: List[Clip],
    out_path: str,
    audio_mode: str = "mix",
    export_settings: Optional[ExportSettings] = None,
    on_progress: Optional[Callable[[float, float], None]] = None,
    tracks: Optional[List[Track]] = None,
) -> None:
    """
    Export project and report progress as (current_sec, total_sec).

    `on_progress` is best-effort and called on the caller thread. It should be
    lightweight and non-blocking.
    """
    cmd = build_export_command_project(
        ffmpeg_path,
        ffprobe_path,
        v_clips,
        a_clips,
        out_path,
        audio_mode=audio_mode,
        export_settings=export_settings,
        tracks=tracks,
    )

    total_sec = _export_total_duration(v_clips, tracks)
    if on_progress:
        try:
            on_progress(0.0, total_sec)
        except Exception:
            pass

    # Ask FFmpeg to emit machine-readable progress lines.
    run_cmd = [*cmd[:-1], "-progress", "pipe:2", "-nostats", cmd[-1]]
    proc = subprocess.Popen(
        run_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    last_reported = 0.0
    if proc.stderr is not None:
        for line in proc.stderr:
            sec = parse_ffmpeg_progress_seconds(line)
            if sec is None:
                continue

            current_sec = max(0.0, sec)
            if total_sec > 0:
                current_sec = min(total_sec, current_sec)

            # Keep progress monotonic.
            if current_sec + 1e-6 < last_reported:
                continue

            should_emit = current_sec >= last_reported + 0.05
            if total_sec > 0 and current_sec >= total_sec - 1e-6:
                should_emit = True

            if should_emit and on_progress:
                last_reported = current_sec
                try:
                    on_progress(current_sec, total_sec)
                except Exception:
                    pass

    ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, run_cmd)

    if on_progress:
        try:
            on_progress(total_sec, total_sec)
        except Exception:
            pass
