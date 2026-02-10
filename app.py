from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import flet as ft
import flet_audio as fta
import flet_video as ftv

from core.config import ConfigStore
from core.ffmpeg import FFmpegNotFound, export_project, probe_media, resolve_ffmpeg_bins
from core.history import HistoryEntry, HistoryManager
from core.model import Project
from core.project_io import load_project, save_project
from core.timeline import (
    add_clip_end,
    insert_clip_before,
    move_clip_before,
    split_clip,
    total_duration,
    trim_clip,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("minicut")


def _fmt_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m:02d}:{s:05.2f}"


@dataclass
class MediaItem:
    path: str
    duration: float
    has_video: bool
    has_audio: bool


class AppState:
    def __init__(self) -> None:
        self.media: List[MediaItem] = []
        self.project: Project = Project(v_clips=[], a_clips=[], fps=30)
        self.project_path: Optional[str] = None
        self.dirty: bool = False
        self.selected_track: Optional[str] = None  # "v" | "a"
        self.selected_clip_id: Optional[str] = None
        self.px_per_sec: float = 60.0  # timeline zoom
        self.export_audio_mode: str = "mix"  # "mix" | "a1_only" | "v1_only"


def main(page: ft.Page) -> None:
    page.title = "MiniCut (MVP)"
    page.window.width = 1100
    page.window.height = 720
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 10

    root = Path(__file__).resolve().parent
    state = AppState()
    history = HistoryManager(limit=50)
    cfg = ConfigStore.default()

    # Default project path keeps existing behavior (single project.json in repo root).
    state.project_path = str(root / "project.json")

    # ---------- helpers ----------
    def snack(msg: str) -> None:
        # SnackBar is a DialogControl in newer Flet versions.
        page.show_dialog(ft.SnackBar(ft.Text(msg)))

    def get_bins() -> Optional[tuple[str, str]]:
        try:
            return resolve_ffmpeg_bins(root)
        except FFmpegNotFound as e:
            snack(str(e))
            return None

    def _update_title() -> None:
        name = Path(state.project_path).name if state.project_path else "Untitled"
        dirty = " *" if state.dirty else ""
        page.title = f"MiniCut (MVP) - {name}{dirty}"

    def _mark_dirty() -> None:
        state.dirty = True
        _update_title()

    def _mark_saved() -> None:
        state.dirty = False
        _update_title()

    def _parse_time_input(raw: str) -> Optional[float]:
        """
        Accept seconds (e.g. "3.5") or timestamps ("mm:ss", "hh:mm:ss").
        """
        s = str(raw or "").strip()
        if not s:
            return None

        try:
            if ":" not in s:
                return float(s)

            parts = s.split(":")
            if len(parts) == 2:
                mm, ss = parts
                return int(mm) * 60 + float(ss)
            if len(parts) == 3:
                hh, mm, ss = parts
                return int(hh) * 3600 + int(mm) * 60 + float(ss)
        except Exception:
            return None
        return None

    _update_title()

    def _track_clips(track: str):
        return state.project.v_clips if track == "v" else state.project.a_clips

    def _set_track_clips(track: str, clips):
        if track == "v":
            state.project.v_clips = clips
        else:
            state.project.a_clips = clips

    def _find_clip(track: str, clip_id: str):
        return next((c for c in _track_clips(track) if c.id == clip_id), None)

    def _selected_clip():
        if not state.selected_track or not state.selected_clip_id:
            return None
        return _find_clip(state.selected_track, state.selected_clip_id)

    # ---------- Undo / Redo ----------
    def _history_current(label: str = "(current)") -> HistoryEntry:
        return HistoryEntry(
            label=label,
            project=state.project.to_dict(),
            selected_track=state.selected_track,
            selected_clip_id=state.selected_clip_id,
        )

    def _history_record(label: str) -> None:
        history.record(
            HistoryEntry(
                label=label,
                project=state.project.to_dict(),
                selected_track=state.selected_track,
                selected_clip_id=state.selected_clip_id,
            )
        )
        _refresh_history_controls()

    def _history_apply(entry: HistoryEntry) -> None:
        state.project = Project.from_dict(entry.project)
        state.selected_track = entry.selected_track
        state.selected_clip_id = entry.selected_clip_id
        _refresh_history_controls()
        update_inspector()
        refresh_timeline()

    def undo_click(_e=None) -> None:
        entry = history.undo(_history_current())
        if not entry:
            return
        _history_apply(entry)
        _mark_dirty()
        snack(f"Undo: {entry.label}")

    def redo_click(_e=None) -> None:
        entry = history.redo(_history_current())
        if not entry:
            return
        _history_apply(entry)
        _mark_dirty()
        snack(f"Redo: {entry.label}")

    undo_btn = ft.IconButton(ft.Icons.UNDO, tooltip="Undo (Ctrl+Z)", on_click=undo_click, disabled=True)
    redo_btn = ft.IconButton(ft.Icons.REDO, tooltip="Redo (Ctrl+Y)", on_click=redo_click, disabled=True)

    def _refresh_history_controls() -> None:
        undo_btn.disabled = not history.can_undo()
        redo_btn.disabled = not history.can_redo()

        if history.can_undo():
            undo_btn.tooltip = f"Undo: {history.peek_undo_label()} (Ctrl+Z)"
        else:
            undo_btn.tooltip = "Undo (Ctrl+Z)"

        if history.can_redo():
            redo_btn.tooltip = f"Redo: {history.peek_redo_label()} (Ctrl+Y)"
        else:
            redo_btn.tooltip = "Redo (Ctrl+Y)"

    def _select_neighbor(delta: int) -> None:
        track = state.selected_track
        if track not in ("v", "a"):
            track = "v" if state.project.v_clips else ("a" if state.project.a_clips else None)
        if track is None:
            return

        clips = _track_clips(track)
        if not clips:
            return

        idx = 0
        if state.selected_track == track and state.selected_clip_id:
            for i, c in enumerate(clips):
                if c.id == state.selected_clip_id:
                    idx = i
                    break

        new_idx = max(0, min(len(clips) - 1, idx + int(delta)))
        if new_idx == idx and state.selected_clip_id == clips[idx].id:
            return

        state.selected_track = track
        state.selected_clip_id = clips[new_idx].id
        update_inspector()
        refresh_timeline()

    def on_keyboard(e: ft.KeyboardEvent) -> None:
        key = (getattr(e, "key", "") or "").lower()
        ctrl = bool(getattr(e, "ctrl", False))
        shift = bool(getattr(e, "shift", False))
        alt = bool(getattr(e, "alt", False))
        meta = bool(getattr(e, "meta", False))

        if ctrl and not shift and key == "z":
            undo_click()
        elif ctrl and (key == "y" or (shift and key == "z")):
            redo_click()
        elif (key == "delete" or key == "backspace") and not (ctrl or alt or meta):
            delete_click(None)
        elif ctrl and key == "s":
            save_click(None)
        elif not ctrl and key == "s":
            split_click(None)
        elif ctrl and key == "e":
            export_click(None)
        elif ctrl and key == "i":
            import_click(None)
        elif key in ("+", "=", "add"):
            try:
                timeline_zoom.value = min(180, float(timeline_zoom.value) + 10)
                on_zoom(None)
            except Exception:
                pass
        elif key in ("-", "_", "subtract"):
            try:
                timeline_zoom.value = max(20, float(timeline_zoom.value) - 10)
                on_zoom(None)
            except Exception:
                pass
        elif key in ("arrow left", "left", "arrowleft"):
            _select_neighbor(-1)
        elif key in ("arrow right", "right", "arrowright"):
            _select_neighbor(1)

    page.on_keyboard_event = on_keyboard
    _refresh_history_controls()

    # ---------- Media Bin ----------
    media_list = ft.ListView(expand=True, spacing=4, auto_scroll=False)

    def refresh_media() -> None:
        media_list.controls.clear()
        for it in state.media:
            icon = ft.Icons.MOVIE if it.has_video else ft.Icons.AUDIOTRACK
            media_list.controls.append(
                ft.Draggable(
                    group="tl",
                    data={"kind": "media", "path": it.path},
                    axis=ft.Axis.VERTICAL,
                    content=ft.Container(
                        padding=8,
                        border_radius=8,
                        bgcolor=ft.Colors.BLUE_GREY_800,
                        content=ft.Row(
                            [
                                ft.Icon(icon),
                                ft.Text(Path(it.path).name, expand=True, no_wrap=True),
                                ft.Text(_fmt_time(it.duration)),
                            ],
                            tight=True,
                        ),
                    ),
                )
            )
        page.update()

    file_picker = ft.FilePicker()

    recent_menu = ft.PopupMenuButton(icon=ft.Icons.HISTORY, tooltip="Recent projects", items=[])

    def _open_project(path: str) -> None:
        try:
            state.project = load_project(path)
            state.project_path = path
            state.selected_clip_id = None
            state.selected_track = None

            history.clear()
            _refresh_history_controls()

            cfg.add_recent_project(path)
            _mark_saved()
            _refresh_recent_menu()

            snack(f"Opened: {Path(path).name}")
            update_inspector()
            refresh_timeline()
        except Exception as ex:
            snack(f"Open failed: {ex}")

    def _clear_recent(_e=None) -> None:
        cfg.clear_recent_projects()
        _refresh_recent_menu()
        page.update()

    def _refresh_recent_menu() -> None:
        items: List[ft.PopupMenuItem] = []
        recents = cfg.recent_projects(limit=10)
        if not recents:
            items.append(ft.PopupMenuItem("No recent projects", disabled=True))
        else:
            for rp in recents:
                p = rp.path
                label = rp.name or Path(p).name
                items.append(ft.PopupMenuItem(label, on_click=lambda _e, path=p: _open_project(path)))
            items.append(ft.PopupMenuItem("Clear recent", on_click=_clear_recent))
        recent_menu.items = items

    recent_menu.on_open = lambda _e: _refresh_recent_menu()
    _refresh_recent_menu()

    # NOTE: Some desktop builds show a confusing "Unknown control: Audio" error
    # if the client doesn't have flet-audio enabled. Keep preview disabled by
    # default and allow opt-in via env var.
    audio_preview_enabled = os.environ.get("MINICUT_AUDIO_PREVIEW", "0") == "1"

    audio = None
    if audio_preview_enabled:
        # Non-visual audio player for A1 preview.
        audio = fta.Audio(volume=1.0)
        page.overlay.append(audio)

    # ---------- Preview / Inspector ----------
    selected_title = ft.Text("No clip selected", weight=ft.FontWeight.BOLD)
    selected_range = ft.Text("")
    split_label = ft.Text("Split: -")
    split_slider = ft.Slider(min=0, max=1, value=0.5, divisions=200)

    trim_in = ft.TextField(label="In", width=110, dense=True)
    trim_out = ft.TextField(label="Out", width=110, dense=True)

    def trim_click(_e=None) -> None:
        if not state.selected_track or not state.selected_clip_id:
            snack("เลือกคลิปก่อน")
            return
        clip = _selected_clip()
        if not clip:
            snack("ไม่พบคลิป")
            return

        new_in = _parse_time_input(trim_in.value)
        new_out = _parse_time_input(trim_out.value)
        if new_in is None or new_out is None:
            snack("Trim: รูปแบบเวลาไม่ถูกต้อง (ใส่วินาทีหรือ mm:ss)")
            return

        # Best-effort duration validation if we know it (imported media).
        mi = next((m for m in state.media if m.path == clip.src), None)
        if mi and new_out > mi.duration + 1e-6:
            snack(f"Trim: out เกินความยาวไฟล์ ({_fmt_time(mi.duration)})")
            return

        before = _track_clips(state.selected_track)
        clips, msg = trim_clip(before, clip.id, new_in, new_out)
        if clips != before:
            _history_record(f"Trim {clip.name}")
            _set_track_clips(state.selected_track, clips)
            _mark_dirty()
        snack(msg)
        update_inspector()
        refresh_timeline()

    trim_apply = ft.FilledButton("Apply Trim", icon=ft.Icons.CUT, on_click=trim_click)
    trim_row = ft.Row([trim_in, trim_out, trim_apply], visible=False, spacing=6)

    preview_host = ft.Container(
        height=260,
        border_radius=10,
        bgcolor=ft.Colors.BLACK,
        # Some Flet versions don't expose `ft.alignment.*`; Alignment(x, y) is stable.
        alignment=ft.Alignment(0, 0),
        content=ft.Text("Preview (optional)", color=ft.Colors.WHITE70),
    )

    audio_pos = ft.Text("", size=12, color=ft.Colors.WHITE70, visible=False)

    def _stop_audio_to_clip_start(_e=None) -> None:
        if not audio_preview_enabled or audio is None:
            return
        clip = _selected_clip()

        # flet_audio Audio methods are async; schedule them on the page task loop.
        target_ms = None
        if clip and state.selected_track == "a":
            if audio.src != clip.src:
                audio.src = clip.src
                audio.update()
            target_ms = int(clip.in_sec * 1000)

        async def _do() -> None:
            try:
                await audio.pause()
            except Exception:
                return
            if target_ms is not None:
                try:
                    await audio.seek(target_ms)
                except Exception:
                    pass

        page.run_task(_do)

    def _pause_audio(_e=None) -> None:
        if not audio_preview_enabled or audio is None:
            snack("ปิดพรีวิวเสียงอยู่ (ตั้งค่า MINICUT_AUDIO_PREVIEW=1 เพื่อเปิดใช้งาน)")
            return

        async def _do() -> None:
            try:
                await audio.pause()
            except Exception:
                pass

        page.run_task(_do)

    def _play_audio(from_split: bool) -> None:
        if not audio_preview_enabled or audio is None:
            snack("ปิดพรีวิวเสียงอยู่ (ตั้งค่า MINICUT_AUDIO_PREVIEW=1 เพื่อเปิดใช้งาน)")
            return
        if state.selected_track != "a":
            snack("เลือกคลิปเสียง (A1) ก่อน")
            return
        clip = _selected_clip()
        if not clip:
            snack("เลือกคลิปเสียง (A1) ก่อน")
            return

        # Ensure backend knows the latest src before invoking methods.
        if audio.src != clip.src:
            audio.src = clip.src
            audio.update()

        offset = float(split_slider.value) if from_split else 0.0
        start = clip.in_sec + offset
        start = min(max(clip.in_sec, start), max(clip.in_sec, clip.out_sec - 0.01))
        start_ms = int(start * 1000)

        async def _do() -> None:
            try:
                await audio.seek(start_ms)
            except Exception:
                pass
            try:
                await audio.play()
            except Exception:
                pass

        page.run_task(_do)

    audio_controls = ft.Row(
        [
            ft.IconButton(
                ft.Icons.PLAY_ARROW,
                tooltip="Play (from clip start)",
                on_click=lambda _e: _play_audio(from_split=False),
            ),
            ft.IconButton(
                ft.Icons.FAST_FORWARD,
                tooltip="Play (from split point)",
                on_click=lambda _e: _play_audio(from_split=True),
            ),
            ft.IconButton(ft.Icons.PAUSE, tooltip="Pause", on_click=_pause_audio),
            ft.IconButton(ft.Icons.STOP, tooltip="Stop", on_click=_stop_audio_to_clip_start),
        ],
        visible=False,
        spacing=4,
    )

    def on_audio_position(e: fta.AudioPositionChangeEvent) -> None:
        if not audio_preview_enabled:
            return
        if state.selected_track != "a":
            return
        clip = _selected_clip()
        if not clip:
            return
        pos_sec = float(e.position) / 1000.0
        rel_sec = max(0.0, pos_sec - clip.in_sec)
        audio_pos.value = f"{_fmt_time(rel_sec)} / {_fmt_time(clip.dur)}"
        audio_pos.update()

        # Auto-stop at end of the selected audio clip.
        if e.position >= int(clip.out_sec * 1000) - 30:
            _stop_audio_to_clip_start()

    if audio_preview_enabled and audio is not None:
        audio.on_position_change = on_audio_position

    def update_inspector() -> None:
        clip = _selected_clip()
        if not clip:
            state.selected_track = None
            state.selected_clip_id = None
            state.selected_track = None
            selected_title.value = "No clip selected"
            selected_range.value = ""
            split_label.value = "Split: -"
            split_slider.min = 0
            split_slider.max = 1
            split_slider.value = 0.5
            trim_in.value = ""
            trim_out.value = ""
            trim_row.visible = False
            audio_controls.visible = False
            audio_pos.visible = False
            _stop_audio_to_clip_start()
            preview_host.content = ft.Text("Preview (optional)", color=ft.Colors.WHITE70)
            page.update()
            return

        prefix = "[V1]" if state.selected_track == "v" else "[A1]"
        selected_title.value = f"{prefix} {clip.name}"
        selected_range.value = f"in={_fmt_time(clip.in_sec)}  out={_fmt_time(clip.out_sec)}  dur={_fmt_time(clip.dur)}"
        trim_in.value = _fmt_time(clip.in_sec)
        trim_out.value = _fmt_time(clip.out_sec)
        trim_row.visible = True

        split_slider.min = 0
        split_slider.max = max(0.01, clip.dur)
        split_slider.value = min(clip.dur / 2, max(0.01, clip.dur - 0.01))
        split_label.value = f"Split: {_fmt_time(split_slider.value)}"

        if state.selected_track == "v":
            audio_controls.visible = False
            audio_pos.visible = False
            _stop_audio_to_clip_start()
            preview_host.content = ftv.Video(
                expand=True,
                playlist=[ftv.VideoMedia(clip.src)],
                autoplay=False,
                muted=True,
                show_controls=True,
            )
        else:
            audio_controls.visible = audio_preview_enabled
            audio_pos.visible = audio_preview_enabled
            _stop_audio_to_clip_start()
            audio_pos.value = f"{_fmt_time(0.0)} / {_fmt_time(clip.dur)}"
            if audio_preview_enabled:
                preview_host.content = ft.Text(
                    "Audio selected: use controls below to listen",
                    color=ft.Colors.WHITE70,
                )
            else:
                preview_host.content = ft.Text(
                    "ปิดพรีวิวเสียงอยู่ (ตั้งค่า MINICUT_AUDIO_PREVIEW=1 เพื่อเปิดใช้งาน)",
                    color=ft.Colors.WHITE70,
                )
        page.update()

    def on_split_slider(e: ft.ControlEvent) -> None:
        split_label.value = f"Split: {_fmt_time(float(split_slider.value))}"
        split_label.update()

    split_slider.on_change = on_split_slider

    # ---------- Timeline ----------
    timeline_zoom = ft.Slider(min=20, max=180, value=state.px_per_sec, divisions=160)
    timeline_info = ft.Text("Timeline: 0 clips", size=12, color=ft.Colors.WHITE70)

    v_row = ft.Row(spacing=6, scroll=ft.ScrollMode.AUTO)
    a_row = ft.Row(spacing=6, scroll=ft.ScrollMode.AUTO)

    def on_zoom(e: ft.ControlEvent) -> None:
        state.px_per_sec = float(timeline_zoom.value)
        refresh_timeline()

    timeline_zoom.on_change = on_zoom

    def clip_block(track: str, clip_id: str) -> ft.Control:
        clip = _find_clip(track, clip_id)
        assert clip is not None

        is_audio = track == "a"
        dur_px = max(70, int(clip.dur * state.px_per_sec))
        color = ft.Colors.GREEN_600 if is_audio else ft.Colors.BLUE_600
        selected = state.selected_track == track and state.selected_clip_id == clip.id
        label = f"A: {clip.name}" if is_audio else clip.name

        cont = ft.Container(
            width=dur_px,
            height=28 if is_audio else 36,
            padding=6,
            border_radius=8,
            bgcolor=ft.Colors.AMBER_600 if selected else color,
            content=ft.Text(label, size=12, no_wrap=True),
        )

        def on_click(_e):
            state.selected_track = track
            state.selected_clip_id = clip.id
            update_inspector()
            refresh_timeline()

        cont.on_click = on_click

        return ft.Draggable(
            group="tl",
            data={"kind": "clip", "track": track, "id": clip.id},
            axis=ft.Axis.HORIZONTAL,
            content=cont,
            content_feedback=ft.Container(width=80, height=22, bgcolor=ft.Colors.WHITE24, border_radius=8),
        )

    def refresh_timeline() -> None:
        v_row.controls.clear()
        a_row.controls.clear()

        def handle_drop(track: str, target_clip_id: Optional[str], payload: dict) -> None:
            kind = payload.get("kind")
            if kind == "media":
                path = payload.get("path")
                mi = next((m for m in state.media if m.path == path), None)
                if not mi:
                    snack("ไม่พบ media")
                    return
                if track == "v" and not mi.has_video:
                    snack("No video stream (drop on A1 instead)")
                    return
                if track == "a" and not mi.has_audio:
                    snack("No audio stream")
                    return

                before = _track_clips(track)
                if target_clip_id:
                    clips = insert_clip_before(before, target_clip_id, path, mi.duration)
                else:
                    clips = add_clip_end(before, path, mi.duration)

                if clips != before:
                    tr = "V1" if track == "v" else "A1"
                    _history_record(f"Add {Path(path).name} to {tr}")
                    _set_track_clips(track, clips)
                    _mark_dirty()
            elif kind == "clip":
                moving_id = payload.get("id")
                moving_track = payload.get("track")
                if not moving_id:
                    return
                if moving_track != track:
                    return

                before = _track_clips(track)
                if target_clip_id:
                    clips = move_clip_before(before, moving_id, target_clip_id)
                else:
                    moving = None
                    rest = []
                    for c in before:
                        if c.id == moving_id:
                            moving = c
                        else:
                            rest.append(c)
                    if moving is None:
                        return
                    clips = [*rest, moving]

                if clips != before:
                    m = _find_clip(track, moving_id)
                    name = m.name if m else moving_id
                    _history_record(f"Move {name}")
                    _set_track_clips(track, clips)
                    _mark_dirty()
            refresh_timeline()

        def _payload(e: ft.DragTargetEvent):
            return getattr(getattr(e, "src", None), "data", None)

        def _end_drop(track: str, height: int) -> ft.DragTarget:
            def on_drop_end(e: ft.DragTargetEvent) -> None:
                payload = _payload(e)
                if isinstance(payload, dict):
                    handle_drop(track, None, payload)

            return ft.DragTarget(
                group="tl",
                on_accept=on_drop_end,
                content=ft.Container(
                    width=160,
                    height=height,
                    alignment=ft.Alignment(0, 0),
                    border_radius=10,
                    bgcolor=ft.Colors.BLUE_GREY_900,
                    border=ft.Border.all(1, ft.Colors.WHITE24),
                    content=ft.Text("Drop to append", size=12),
                ),
            )

        # V1 clips
        for c in state.project.v_clips:
            def _make_on_accept_v(target_id: str):
                def _on_accept(e: ft.DragTargetEvent) -> None:
                    payload = _payload(e)
                    if isinstance(payload, dict):
                        handle_drop("v", target_id, payload)
                return _on_accept

            block = clip_block("v", c.id)
            v_row.controls.append(
                ft.Stack(
                    [
                        block,
                        ft.Positioned(
                            left=0,
                            top=0,
                            right=0,
                            bottom=0,
                            content=ft.DragTarget(
                                group="tl",
                                on_accept=_make_on_accept_v(c.id),
                                content=ft.Container(bgcolor=ft.Colors.TRANSPARENT),
                            ),
                        ),
                    ],
                    width=max(70, int(c.dur * state.px_per_sec)),
                    height=40,
                )
            )

        # A1 clips
        for c in state.project.a_clips:
            def _make_on_accept_a(target_id: str):
                def _on_accept(e: ft.DragTargetEvent) -> None:
                    payload = _payload(e)
                    if isinstance(payload, dict):
                        handle_drop("a", target_id, payload)
                return _on_accept

            block = clip_block("a", c.id)
            a_row.controls.append(
                ft.Stack(
                    [
                        block,
                        ft.Positioned(
                            left=0,
                            top=0,
                            right=0,
                            bottom=0,
                            content=ft.DragTarget(
                                group="tl",
                                on_accept=_make_on_accept_a(c.id),
                                content=ft.Container(bgcolor=ft.Colors.TRANSPARENT),
                            ),
                        ),
                    ],
                    width=max(70, int(c.dur * state.px_per_sec)),
                    height=34,
                )
            )

        v_row.controls.append(_end_drop("v", height=36))
        a_row.controls.append(_end_drop("a", height=28))

        v_total = _fmt_time(total_duration(state.project.v_clips))
        a_total = _fmt_time(total_duration(state.project.a_clips))
        timeline_info.value = f"V1: {len(state.project.v_clips)} clips | {v_total}   A1: {len(state.project.a_clips)} clips | {a_total}"
        page.update()

    # ---------- Actions ----------
    def import_click(_e):
        async def _pick() -> None:
            picked = await file_picker.pick_files(
                allow_multiple=True,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["mp4", "mov", "mkv", "mp3", "wav", "m4a", "aac", "flac", "ogg"],
            )
            if not picked:
                return

            bins = get_bins()
            if not bins:
                return
            _, ffprobe = bins

            for f in picked:
                if not f.path:
                    continue
                try:
                    info = probe_media(ffprobe, f.path)
                    if info.duration <= 0.01:
                        continue
                    state.media.append(
                        MediaItem(
                            path=f.path,
                            duration=info.duration,
                            has_video=info.has_video,
                            has_audio=info.has_audio,
                        )
                    )
                except Exception as ex:
                    log.exception("probe failed: %s", ex)
                    snack(f"อ่านไฟล์ไม่สำเร็จ: {Path(f.path).name}")

            refresh_media()

        page.run_task(_pick)

    def split_click(_e):
        if not state.selected_track or not state.selected_clip_id:
            snack("เลือกคลิปก่อน")
            return
        before = _track_clips(state.selected_track)
        selected = _find_clip(state.selected_track, state.selected_clip_id)
        clips, new_selected, msg = split_clip(
            before,
            state.selected_clip_id,
            float(split_slider.value),
        )
        if clips != before:
            _history_record(f"Split {selected.name if selected else 'clip'}")
        state.selected_clip_id = new_selected
        _set_track_clips(state.selected_track, clips)
        if clips != before:
            _mark_dirty()
        snack(msg)
        update_inspector()
        refresh_timeline()

    def delete_click(_e):
        if not state.selected_track or not state.selected_clip_id:
            snack("เลือกคลิปก่อน")
            return
        before = _track_clips(state.selected_track)
        selected = _find_clip(state.selected_track, state.selected_clip_id)
        clips = [c for c in before if c.id != state.selected_clip_id]
        if clips != before:
            _history_record(f"Delete {selected.name if selected else 'clip'}")
        _set_track_clips(state.selected_track, clips)
        if clips != before:
            _mark_dirty()
        state.selected_clip_id = None
        state.selected_track = None
        update_inspector()
        refresh_timeline()

    def save_click(_e):
        try:
            path = state.project_path or str(root / "project.json")
            save_project(state.project, path)
            state.project_path = path
            cfg.add_recent_project(path)
            _mark_saved()
            _refresh_recent_menu()
            snack(f"Saved: {Path(path).name}")
            page.update()
        except Exception as ex:
            snack(f"Save failed: {ex}")

    def save_as_click(_e):
        async def _save_as() -> None:
            default_name = Path(state.project_path).name if state.project_path else "project.json"
            out_path = await file_picker.save_file(
                file_name=default_name,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["json"],
            )
            if not out_path:
                return
            try:
                save_project(state.project, out_path)
                state.project_path = out_path
                cfg.add_recent_project(out_path)
                _mark_saved()
                _refresh_recent_menu()
                snack(f"Saved: {Path(out_path).name}")
                page.update()
            except Exception as ex:
                snack(f"Save failed: {ex}")

        page.run_task(_save_as)

    def load_click(_e):
        async def _pick() -> None:
            picked = await file_picker.pick_files(
                allow_multiple=False,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["json"],
            )
            if not picked or not picked[0].path:
                return
            _open_project(picked[0].path)

        page.run_task(_pick)

    def export_click(_e):
        if not state.project.v_clips:
            snack("Timeline ว่าง")
            return

        async def _save_and_export() -> None:
            out_path = await file_picker.save_file(
                file_name="output.mp4",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["mp4"],
            )
            if not out_path:
                return

            bins = get_bins()
            if not bins:
                return
            ffmpeg, ffprobe = bins

            # Snapshot to keep export deterministic if the user keeps editing.
            v_clips = list(state.project.v_clips)
            a_clips = list(state.project.a_clips)
            audio_mode = state.export_audio_mode

            def _do_export() -> None:
                try:
                    export_project(
                        ffmpeg,
                        ffprobe,
                        v_clips,
                        a_clips,
                        out_path,
                        audio_mode=audio_mode,
                    )
                    msg = "Export เสร็จ"
                except Exception as ex:
                    log.exception("export failed: %s", ex)
                    msg = f"Export ล้มเหลว: {ex}"

                async def _notify() -> None:
                    snack(msg)

                page.run_task(_notify)

            page.run_thread(_do_export)

        page.run_task(_save_and_export)

    def on_audio_mode_change(e: ft.ControlEvent) -> None:
        state.export_audio_mode = str(e.control.value)

    export_audio_mode = ft.Dropdown(
        width=220,
        dense=True,
        label="Export audio",
        value=state.export_audio_mode,
        options=[
            ft.dropdown.Option(key="mix", text="Mix (V1 + A1)"),
            ft.dropdown.Option(key="a1_only", text="A1 only (mute V1)"),
            ft.dropdown.Option(key="v1_only", text="V1 only (ignore A1)"),
        ],
        on_select=on_audio_mode_change,
    )

    # ---------- Layout ----------
    toolbar = ft.Row(
        [
            ft.ElevatedButton("Import", icon=ft.Icons.UPLOAD_FILE, on_click=import_click),
            undo_btn,
            redo_btn,
            ft.ElevatedButton("Split", icon=ft.Icons.CONTENT_CUT, on_click=split_click),
            ft.OutlinedButton("Delete", icon=ft.Icons.DELETE, on_click=delete_click),
            ft.OutlinedButton("Save", icon=ft.Icons.SAVE, on_click=save_click),
            ft.OutlinedButton("Save As", icon=ft.Icons.SAVE_AS, on_click=save_as_click),
            ft.OutlinedButton("Load", icon=ft.Icons.FOLDER_OPEN, on_click=load_click),
            recent_menu,
            ft.Container(expand=True),
            export_audio_mode,
            ft.FilledButton("Export", icon=ft.Icons.OUTPUT, on_click=export_click),
        ],
        alignment=ft.MainAxisAlignment.START,
    )

    left_panel = ft.Container(
        width=330,
        padding=10,
        border_radius=12,
        bgcolor=ft.Colors.BLUE_GREY_900,
        content=ft.Column(
            [
                ft.Text("Media Bin", weight=ft.FontWeight.BOLD),
                ft.Text("ลากไฟล์ลง Timeline ได้", size=12, color=ft.Colors.WHITE70),
                ft.Divider(height=8),
                media_list,
            ],
            expand=True,
        ),
    )

    inspector = ft.Container(
        padding=10,
        border_radius=12,
        bgcolor=ft.Colors.BLUE_GREY_900,
        content=ft.Column(
            [
                ft.Text("Inspector", weight=ft.FontWeight.BOLD),
                selected_title,
                selected_range,
                ft.Divider(height=8),
                split_label,
                split_slider,
                trim_row,
                audio_controls,
                audio_pos,
                ft.Text("Tip: เลือกคลิปแล้วกด Split", size=12, color=ft.Colors.WHITE70),
            ],
            tight=True,
        ),
    )

    right_panel = ft.Column(
        [
            ft.Text("Preview", weight=ft.FontWeight.BOLD),
            preview_host,
            ft.Divider(height=8),
            inspector,
        ],
        expand=True,
        spacing=8,
    )

    main_row = ft.Row([left_panel, ft.VerticalDivider(width=8), right_panel], expand=True)

    timeline = ft.Container(
        padding=10,
        border_radius=12,
        bgcolor=ft.Colors.BLUE_GREY_900,
        content=ft.Column(
            [
                ft.Row([timeline_info, ft.Container(expand=True), ft.Text("Zoom"), timeline_zoom]),
                ft.Divider(height=6),
                ft.Row([ft.Text("V1", width=30), v_row], expand=True),
                ft.Row([ft.Text("A1", width=30), a_row], expand=True),
            ],
            expand=True,
        ),
    )

    page.add(ft.Column([toolbar, main_row, timeline], expand=True, spacing=10))

    # initial
    refresh_media()
    refresh_timeline()
    update_inspector()

    async def _auto_save_loop() -> None:
        while True:
            await asyncio.sleep(cfg.auto_save_interval_sec())
            if not state.dirty:
                continue
            path = state.project_path
            if not path:
                continue
            try:
                save_project(state.project, path)
                cfg.add_recent_project(path)
                _mark_saved()
                _refresh_recent_menu()
                page.update()
            except Exception as ex:
                log.exception("auto-save failed: %s", ex)
                snack(f"Auto-save failed: {ex}")

    page.run_task(_auto_save_loop)


if __name__ == "__main__":
    ft.app(target=main)
