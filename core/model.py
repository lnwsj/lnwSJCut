from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List
import uuid


def new_id() -> str:
    """Generate a stable unique id for UI/timeline operations."""
    return uuid.uuid4().hex


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
        return Clip(
            id=str(d["id"]),
            src=str(d["src"]),
            in_sec=float(d["in_sec"]),
            out_sec=float(d["out_sec"]),
            volume=float(d.get("volume", 1.0) or 1.0),
            muted=bool(d.get("muted", False)),
        )


@dataclass
class Project:
    """Minimal project model: separate linear timelines for video (V1) and audio (A1)."""

    v_clips: List[Clip]
    a_clips: List[Clip]
    fps: int = 30

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fps": self.fps,
            "v_clips": [c.to_dict() for c in self.v_clips],
            "a_clips": [c.to_dict() for c in self.a_clips],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Project":
        fps = int(d.get("fps", 30))

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
