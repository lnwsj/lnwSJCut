from __future__ import annotations

from dataclasses import replace
from typing import List, Optional, Tuple

from .model import Clip, new_id, transition_overlap_sec


def add_clip_end(clips: List[Clip], src: str, duration: float, has_audio: bool = True) -> List[Clip]:
    """Append a full-length clip to the end of the timeline."""
    c = Clip(id=new_id(), src=src, in_sec=0.0, out_sec=float(duration), has_audio=bool(has_audio))
    return [*clips, c]


def insert_clip_before(
    clips: List[Clip],
    before_clip_id: str,
    src: str,
    duration: float,
    has_audio: bool = True,
) -> List[Clip]:
    """Insert a full-length clip before another clip by id."""
    new_clip = Clip(id=new_id(), src=src, in_sec=0.0, out_sec=float(duration), has_audio=bool(has_audio))
    out: List[Clip] = []
    inserted = False
    for c in clips:
        if c.id == before_clip_id and not inserted:
            out.append(new_clip)
            inserted = True
        out.append(c)
    if not inserted:
        out.append(new_clip)
    return out


def find_clip(clips: List[Clip], clip_id: str) -> Optional[Clip]:
    for c in clips:
        if c.id == clip_id:
            return c
    return None


def split_clip(
    clips: List[Clip],
    clip_id: str,
    split_at_sec_from_clip_start: float,
    min_piece_sec: float = 0.08,
) -> Tuple[List[Clip], Optional[str], str]:
    """
    Split a clip into two clips at a relative time.

    Args:
        clips: current timeline clips
        clip_id: id of clip to split
        split_at_sec_from_clip_start: split time relative to clip start
        min_piece_sec: guard to avoid ultra-short pieces

    Returns:
        (new_clips, new_selected_clip_id, message)
    """
    out: List[Clip] = []
    msg = ""
    new_selected: Optional[str] = None

    for c in clips:
        if c.id != clip_id:
            out.append(c)
            continue

        t = float(split_at_sec_from_clip_start)
        if t <= min_piece_sec or t >= c.dur - min_piece_sec:
            out.append(c)
            msg = "จุด Split ใกล้ขอบเกินไป"
            new_selected = c.id
            continue

        mid = c.in_sec + t
        c1 = replace(c, id=new_id(), in_sec=c.in_sec, out_sec=mid)
        c2 = replace(c, id=new_id(), in_sec=mid, out_sec=c.out_sec)
        out.extend([c1, c2])
        new_selected = c1.id
        msg = "Split แล้ว"

    if not msg:
        msg = "ไม่พบคลิปที่จะ Split"
    return out, new_selected, msg


def split_clip_at_timeline_sec(
    clips: List[Clip],
    timeline_sec: float,
    min_piece_sec: float = 0.08,
) -> Tuple[List[Clip], Optional[str], str]:
    """
    Split the clip that intersects a global linear timeline time.

    The timeline is treated as a simple sum of clip durations (no gaps).
    """
    if not clips:
        return clips, None, "Track is empty"

    t = float(timeline_sec)
    if t < 0.0:
        return clips, None, "Split position is out of range"

    acc = 0.0
    for c in clips:
        start = acc
        end = acc + float(c.dur)
        if t <= end + 1e-9:
            rel = t - start
            return split_clip(clips, c.id, rel, min_piece_sec=min_piece_sec)
        acc = end
    return clips, None, "Split position is out of range"


def move_clip_before(clips: List[Clip], moving_id: str, target_id: str) -> List[Clip]:
    """Move clip `moving_id` to be placed before `target_id`."""
    if moving_id == target_id:
        return clips

    moving = None
    rest: List[Clip] = []
    for c in clips:
        if c.id == moving_id:
            moving = c
        else:
            rest.append(c)
    if moving is None:
        return clips

    out: List[Clip] = []
    inserted = False
    for c in rest:
        if c.id == target_id and not inserted:
            out.append(moving)
            inserted = True
        out.append(c)
    if not inserted:
        out.append(moving)
    return out


def duplicate_clip(clips: List[Clip], clip_id: str) -> Tuple[List[Clip], Optional[str], str]:
    """Duplicate a clip and insert it right after the original."""
    out: List[Clip] = []
    new_id_val: Optional[str] = None
    for c in clips:
        out.append(c)
        if c.id == clip_id:
            dup = replace(c, id=new_id())
            out.append(dup)
            new_id_val = dup.id

    if new_id_val is None:
        return clips, None, "ไม่พบคลิปที่จะ Duplicate"
    return out, new_id_val, "Duplicate แล้ว"


def total_duration(clips: List[Clip]) -> float:
    if not clips:
        return 0.0
    total = 0.0
    prev: Optional[Clip] = None
    for c in clips:
        total += c.dur
        if prev is not None:
            total -= transition_overlap_sec(prev, c)
        prev = c
    return max(0.0, total)


def trim_clip(
    clips: List[Clip],
    clip_id: str,
    new_in_sec: float,
    new_out_sec: float,
    min_piece_sec: float = 0.08,
) -> Tuple[List[Clip], str]:
    """
    Adjust in/out range of a clip (non-destructive).

    Args:
        clips: current timeline clips
        clip_id: clip to modify
        new_in_sec/new_out_sec: new absolute range within the source file (seconds)
        min_piece_sec: guard to avoid ultra-short clips

    Returns:
        (new_clips, message)
    """
    new_in_sec = float(new_in_sec)
    new_out_sec = float(new_out_sec)
    if new_in_sec < 0:
        return clips, "Trim failed: in < 0"
    if new_out_sec <= new_in_sec + float(min_piece_sec):
        return clips, "Trim failed: out must be > in"

    out: List[Clip] = []
    found = False
    changed = False
    for c in clips:
        if c.id != clip_id:
            out.append(c)
            continue
        found = True
        if abs(c.in_sec - new_in_sec) < 1e-9 and abs(c.out_sec - new_out_sec) < 1e-9:
            out.append(c)
        else:
            out.append(replace(c, in_sec=new_in_sec, out_sec=new_out_sec))
            changed = True

    if not found:
        return clips, "Trim failed: clip not found"
    if not changed:
        return clips, "Trim: no changes"
    return out, "Trimmed"
