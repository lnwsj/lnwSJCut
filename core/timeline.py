from __future__ import annotations

from dataclasses import replace
from typing import List, Optional, Tuple

from .model import Clip, new_id


def add_clip_end(clips: List[Clip], src: str, duration: float) -> List[Clip]:
    """Append a full-length clip to the end of the timeline."""
    c = Clip(id=new_id(), src=src, in_sec=0.0, out_sec=float(duration))
    return [*clips, c]


def insert_clip_before(
    clips: List[Clip],
    before_clip_id: str,
    src: str,
    duration: float,
) -> List[Clip]:
    """Insert a full-length clip before another clip by id."""
    new_clip = Clip(id=new_id(), src=src, in_sec=0.0, out_sec=float(duration))
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


def total_duration(clips: List[Clip]) -> float:
    return sum(c.dur for c in clips)
