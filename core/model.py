from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid


def new_id() -> str:
    """Generate a stable unique id for UI/timeline operations."""
    return uuid.uuid4().hex


@dataclass
class Transition:
    kind: str = "fade"  # fade | crossfade | dissolve
    duration: float = 0.5

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> Optional["Transition"]:
        if not isinstance(d, dict):
            return None
        kind = str(d.get("kind", d.get("type", "fade")) or "fade").strip().lower()
        if kind in ("", "none", "off"):
            return None
        if kind not in ("fade", "crossfade", "dissolve"):
            kind = "fade"
        try:
            dur = float(d.get("duration", 0.5) or 0.5)
        except Exception:
            dur = 0.5
        dur = max(0.0, dur)
        if dur <= 0.0:
            return None
        return Transition(kind=kind, duration=dur)


@dataclass
class ExportSettings:
    """
    Output encoding settings used by FFmpeg export.

    Notes:
    - width/height = 0 means keep timeline/source resolution (no scale filter)
    - format controls output container extension preference (mp4/mov/webm)
    """

    width: int = 0
    height: int = 0
    video_codec: str = "libx264"
    crf: int = 23
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    format: str = "mp4"
    preset: str = "medium"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ExportSettings":
        if not isinstance(d, dict):
            return ExportSettings()
        out = ExportSettings()
        try:
            out.width = max(0, int(d.get("width", out.width) or 0))
        except Exception:
            out.width = 0
        try:
            out.height = max(0, int(d.get("height", out.height) or 0))
        except Exception:
            out.height = 0
        out.video_codec = str(d.get("video_codec", out.video_codec) or out.video_codec)
        try:
            out.crf = int(d.get("crf", out.crf))
        except Exception:
            out.crf = 23
        out.audio_codec = str(d.get("audio_codec", out.audio_codec) or out.audio_codec)
        out.audio_bitrate = str(d.get("audio_bitrate", out.audio_bitrate) or out.audio_bitrate)
        out.format = str(d.get("format", out.format) or out.format)
        out.preset = str(d.get("preset", out.preset) or out.preset)
        return out


@dataclass
class Clip:
    """
    Non-destructive clip segment on a timeline.

    Attributes:
        src: path to media file
        in_sec/out_sec: segment range within the source file (seconds)
    """

    id: str
    src: str
    in_sec: float
    out_sec: float
    volume: float = 1.0
    muted: bool = False
    has_audio: bool = True
    transition_in: Optional[Transition] = None

    @property
    def dur(self) -> float:
        return max(0.0, self.out_sec - self.in_sec)

    @property
    def name(self) -> str:
        return Path(self.src).name

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Clip":
        raw_transition = d.get("transition_in", None)
        transition_in: Optional[Transition]
        if isinstance(raw_transition, dict):
            transition_in = Transition.from_dict(raw_transition)
        elif isinstance(raw_transition, str):
            kind = str(raw_transition).strip().lower()
            transition_in = None if kind in ("", "none", "off") else Transition(kind=kind, duration=0.5)
        else:
            transition_in = None
        return Clip(
            id=str(d["id"]),
            src=str(d["src"]),
            in_sec=float(d["in_sec"]),
            out_sec=float(d["out_sec"]),
            volume=float(d.get("volume", 1.0) or 1.0),
            muted=bool(d.get("muted", False)),
            has_audio=bool(d.get("has_audio", True)),
            transition_in=transition_in,
        )


@dataclass
class Track:
    id: str
    name: str
    kind: str  # "video" | "audio"
    clips: List[Clip]
    muted: bool = False
    visible: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "clips": [c.to_dict() for c in self.clips],
            "muted": bool(self.muted),
            "visible": bool(self.visible),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Track":
        tid = str(d.get("id") or new_id())
        name = str(d.get("name") or "").strip() or tid
        kind = str(d.get("kind") or "").strip().lower()
        if kind not in ("video", "audio"):
            # Best-effort fallback for malformed legacy values.
            if name.upper().startswith("A") or tid.lower().startswith("a"):
                kind = "audio"
            else:
                kind = "video"
        raw = d.get("clips", [])
        clips = [Clip.from_dict(x) for x in raw] if isinstance(raw, list) else []
        return Track(
            id=tid,
            name=name,
            kind=kind,
            clips=clips,
            muted=bool(d.get("muted", False)),
            visible=bool(d.get("visible", True)),
        )


def transition_overlap_sec(prev_clip: Clip, clip: Clip, min_remaining_sec: float = 0.01) -> float:
    """
    Effective overlap seconds for `clip.transition_in` against `prev_clip`.
    """
    t = getattr(clip, "transition_in", None)
    if t is None:
        return 0.0

    kind = str(getattr(t, "kind", "") or "").strip().lower()
    if kind in ("", "none", "off"):
        return 0.0

    try:
        req = float(getattr(t, "duration", 0.0) or 0.0)
    except Exception:
        req = 0.0
    if req <= 0.0:
        return 0.0

    max_prev = max(0.0, float(prev_clip.dur) - float(min_remaining_sec))
    max_curr = max(0.0, float(clip.dur) - float(min_remaining_sec))
    return max(0.0, min(req, max_prev, max_curr))


class Project:
    """
    Project model with multi-track support.

    Backward compatibility:
    - accepts legacy constructor args: Project(v_clips=[...], a_clips=[...], fps=30)
    - still exposes .v_clips / .a_clips properties mapped to primary V/A tracks
    """

    def __init__(
        self,
        v_clips: Optional[List[Clip]] = None,
        a_clips: Optional[List[Clip]] = None,
        fps: int = 30,
        tracks: Optional[List[Track]] = None,
    ) -> None:
        self.fps = int(fps)
        self.tracks: List[Track] = []

        if tracks is not None:
            for t in tracks:
                if isinstance(t, Track):
                    self.tracks.append(
                        Track(
                            id=str(t.id),
                            name=str(t.name),
                            kind=str(t.kind).lower(),
                            clips=list(t.clips),
                            muted=bool(t.muted),
                            visible=bool(t.visible),
                        )
                    )
                elif isinstance(t, dict):
                    self.tracks.append(Track.from_dict(t))

        self._ensure_minimum_tracks()
        if tracks is None:
            self.v_clips = list(v_clips or [])
            self.a_clips = list(a_clips or [])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fps": self.fps,
            "tracks": [t.to_dict() for t in self.tracks],
            # Keep legacy keys for compatibility with older app builds/tools.
            "v_clips": [c.to_dict() for c in self.v_clips],
            "a_clips": [c.to_dict() for c in self.a_clips],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Project":
        fps = int(d.get("fps", 30))

        # New multi-track format.
        if isinstance(d.get("tracks"), list):
            return Project(
                fps=fps,
                tracks=[Track.from_dict(x) for x in d.get("tracks", []) if isinstance(x, dict)],
            )

        # New format: separate V1/A1 lists.
        if "v_clips" in d or "a_clips" in d:
            return Project(
                fps=fps,
                v_clips=[Clip.from_dict(x) for x in d.get("v_clips", [])],
                a_clips=[Clip.from_dict(x) for x in d.get("a_clips", [])],
            )

        # Backward compat: older projects had a single "clips" list.
        return Project(
            fps=fps,
            v_clips=[Clip.from_dict(x) for x in d.get("clips", [])],
            a_clips=[],
        )

    @property
    def video_tracks(self) -> List[Track]:
        return [t for t in self.tracks if t.kind == "video"]

    @property
    def audio_tracks(self) -> List[Track]:
        return [t for t in self.tracks if t.kind == "audio"]

    @property
    def v_clips(self) -> List[Clip]:
        return self.primary_video_track().clips

    @v_clips.setter
    def v_clips(self, clips: List[Clip]) -> None:
        self.primary_video_track().clips = list(clips or [])

    @property
    def a_clips(self) -> List[Clip]:
        return self.primary_audio_track().clips

    @a_clips.setter
    def a_clips(self, clips: List[Clip]) -> None:
        self.primary_audio_track().clips = list(clips or [])

    def get_track(self, track_id: str) -> Optional[Track]:
        tid = str(track_id or "")
        for t in self.tracks:
            if t.id == tid:
                return t
        return None

    def primary_video_track(self) -> Track:
        self._ensure_minimum_tracks()
        return self.video_tracks[0]

    def primary_audio_track(self) -> Track:
        self._ensure_minimum_tracks()
        return self.audio_tracks[0]

    def add_track(self, kind: str, name: Optional[str] = None) -> Track:
        k = str(kind or "").strip().lower()
        if k not in ("video", "audio"):
            k = "video"
        prefix = "V" if k == "video" else "A"
        idx = 1
        existing = {str(t.name).upper() for t in self.tracks}
        while f"{prefix}{idx}" in existing:
            idx += 1
        t = Track(
            id=f"{prefix.lower()}{idx}_{new_id()[:8]}",
            name=str(name or f"{prefix}{idx}"),
            kind=k,
            clips=[],
            muted=False,
            visible=True,
        )
        self.tracks.append(t)
        return t

    def remove_track(self, track_id: str) -> bool:
        t = self.get_track(track_id)
        if t is None:
            return False
        same_kind = self.video_tracks if t.kind == "video" else self.audio_tracks
        if len(same_kind) <= 1:
            # Keep at least one track of each kind for backward compatibility and UI stability.
            return False
        self.tracks = [x for x in self.tracks if x.id != t.id]
        return True

    def _ensure_minimum_tracks(self) -> None:
        if not self.video_tracks:
            self.tracks.append(
                Track(
                    id=f"v1_{new_id()[:8]}",
                    name="V1",
                    kind="video",
                    clips=[],
                )
            )
        if not self.audio_tracks:
            self.tracks.append(
                Track(
                    id=f"a1_{new_id()[:8]}",
                    name="A1",
                    kind="audio",
                    clips=[],
                )
            )
