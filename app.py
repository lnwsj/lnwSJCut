from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
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
    duplicate_clip,
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


def _fmt_bytes(n: int) -> str:
    try:
        n = int(n)
    except Exception:
        return "-"
    if n <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    u = 0
    while v >= 1024.0 and u < len(units) - 1:
        v /= 1024.0
        u += 1
    return f"{v:.1f} {units[u]}"


def _fmt_bps(bps: int) -> str:
    try:
        bps = int(bps)
    except Exception:
        return "-"
    if bps <= 0:
        return "-"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    return f"{bps / 1000:.0f} Kbps"


def _channels_label(ch: int) -> str:
    try:
        ch = int(ch)
    except Exception:
        return ""
    if ch == 1:
        return "mono"
    if ch == 2:
        return "stereo"
    if ch > 2:
        return f"{ch} ch"
    return ""


@dataclass
class MediaItem:
    path: str
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
        # Split marker time (seconds) for the currently selected clip.
        self.split_pos_sec: float = 0.0
        self.split_pos_clip_id: Optional[str] = None
        # Playback / playhead
        self.playhead_sec: float = 0.0  # global timeline seconds on V1
        self.playhead_clip_id: Optional[str] = None
        self.is_playing: bool = False


def main(page: ft.Page) -> None:
    page.title = "MiniCut (MVP)"
    # Window sizing is desktop-only. In web mode it can lead to awkward clipping where the
    # timeline panel isn't visible in the browser viewport.
    _platform = str(getattr(page, "platform", "") or "").lower()
    is_web = bool(getattr(page, "web", False)) or ("web" in _platform)
    if not is_web:
        page.window.width = 1100
        page.window.height = 720
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 10

    root = Path(__file__).resolve().parent
    state = AppState()
    web_preview_cache: dict[str, str] = {}
    history = HistoryManager(limit=50)
    cfg = ConfigStore.default()
    playhead_handle_w = 14.0
    playhead_bar = ft.Column(
        [
            ft.Container(width=10, height=10, border_radius=5, bgcolor=ft.Colors.RED_300, opacity=0.95),
            ft.Container(width=2, height=84, bgcolor=ft.Colors.RED_400, opacity=0.95),
        ],
        spacing=0,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )
    playhead_handle = ft.GestureDetector(
        mouse_cursor=ft.MouseCursor.GRAB,
        drag_interval=0,
        content=ft.Container(
            width=int(playhead_handle_w),
            height=94,
            alignment=ft.Alignment(0, 0),
            bgcolor=ft.Colors.TRANSPARENT,
            content=playhead_bar,
        ),
    )
    playhead_line = ft.Container(left=0, top=28, content=playhead_handle)
    preview_video: Optional[ftv.Video] = None
    v_start_sec_map: dict[str, float] = {}
    v_start_px_map: dict[str, float] = {}
    v_clip_width_px_map: dict[str, float] = {}
    playhead_drag_start_left: float = 0.0
    playhead_drag_start_pointer_x: float = 0.0
    timeline_pan_active: bool = False
    timeline_pan_start_pointer_x: float = 0.0
    timeline_pan_start_playhead_x: float = 0.0
    timeline_lane_label_w = 30.0
    timeline_lane_gap_w = 8.0
    timeline_v1_left_offset = timeline_lane_label_w + timeline_lane_gap_w
    timeline_total_sec: float = 0.0

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
        elif ctrl and key == "d":
            duplicate_click(None)
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

    def _media_info_text(it: MediaItem) -> str:
        name = Path(it.path).name
        lines: List[str] = [name, f"Duration: {_fmt_time(it.duration)}", f"File size: {_fmt_bytes(it.file_size_bytes)}"]

        if it.has_video:
            res = f"{it.width}x{it.height}" if it.width and it.height else "-"
            fps = f"{it.fps:.2f} fps" if it.fps else "-"
            vc = it.video_codec or "-"
            pf = it.pixel_format or "-"
            vb = _fmt_bps(it.video_bitrate)
            lines.append(f"Video: {vc} ({pf})")
            lines.append(f"Resolution: {res} | FPS: {fps} | Bitrate: {vb}")
        else:
            lines.append("Video: -")

        if it.has_audio:
            ac = it.audio_codec or "-"
            sr = f"{it.sample_rate} Hz" if it.sample_rate else "-"
            ch = _channels_label(it.channels) or "-"
            ab = _fmt_bps(it.audio_bitrate)
            lines.append(f"Audio: {ac} | {sr} | {ch} | Bitrate: {ab}")
        else:
            lines.append("Audio: -")

        lines.append(f"Path: {it.path}")
        return "\n".join(lines)

    def _show_media_info(it: MediaItem) -> None:
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Media Info"),
            content=ft.Container(
                width=560,
                padding=10,
                content=ft.Text(_media_info_text(it), selectable=True),
            ),
            actions=[ft.TextButton("Close", on_click=lambda _e: page.pop_dialog())],
        )
        page.show_dialog(dlg)

    def refresh_media() -> None:
        media_list.controls.clear()
        for it in state.media:
            icon = ft.Icons.MOVIE if it.has_video else ft.Icons.AUDIOTRACK
            tooltip = f"{Path(it.path).name}\n{_fmt_time(it.duration)}"
            if it.has_video and it.width and it.height:
                tooltip += f"\n{it.width}x{it.height}"
            media_list.controls.append(
                ft.Draggable(
                    group="tl",
                    data={"kind": "media", "path": it.path},
                    axis=ft.Axis.VERTICAL,
                    content=ft.Container(
                        padding=8,
                        border_radius=8,
                        bgcolor=ft.Colors.BLUE_GREY_800,
                        tooltip=tooltip,
                        content=ft.Row(
                            [
                                ft.Icon(icon),
                                ft.Text(Path(it.path).name, expand=True, no_wrap=True),
                                ft.Text(_fmt_time(it.duration)),
                                ft.IconButton(
                                    ft.Icons.INFO_OUTLINE,
                                    tooltip="Media info",
                                    on_click=lambda _e, it=it: _show_media_info(it),
                                ),
                            ],
                            tight=True,
                        ),
                    ),
                )
            )
        page.update()

    file_picker = ft.FilePicker()

    recent_menu = ft.PopupMenuButton(icon=ft.Icons.HISTORY, tooltip="Recent projects", items=[])

    # ---------- File Drop Import (OS -> Media Bin) ----------
    allowed_import_ext = {
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
        ".flv",
        ".wmv",
        ".m4v",
        ".mp3",
        ".wav",
        ".flac",
        ".aac",
        ".ogg",
        ".m4a",
    }

    def on_file_drop(e) -> None:
        files = getattr(e, "files", None) or []
        if not files:
            return

        bins = get_bins()
        if not bins:
            return
        _, ffprobe = bins

        added = 0
        for f in files:
            path = getattr(f, "path", None)
            if not path:
                continue
            ext = Path(path).suffix.lower()
            if ext not in allowed_import_ext:
                continue
            if any(m.path == path for m in state.media):
                continue
            try:
                info = probe_media(ffprobe, path)
                if info.duration <= 0.01:
                    continue
                state.media.append(
                    MediaItem(
                        path=path,
                        duration=info.duration,
                        has_video=info.has_video,
                        has_audio=info.has_audio,
                        width=info.width,
                        height=info.height,
                        fps=info.fps,
                        video_codec=info.video_codec,
                        audio_codec=info.audio_codec,
                        video_bitrate=info.video_bitrate,
                        audio_bitrate=info.audio_bitrate,
                        file_size_bytes=info.file_size_bytes,
                        pixel_format=info.pixel_format,
                        sample_rate=info.sample_rate,
                        channels=info.channels,
                    )
                )
                added += 1
            except Exception as ex:
                log.exception("probe failed: %s", ex)

        if added:
            snack(f"Imported {added} file(s)")
            refresh_media()

    # Best-effort: Flet desktop supports dropping files from OS.
    page.on_drop = on_file_drop

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

    # ---------- Clip audio controls (export-time) ----------
    volume_title = ft.Text("Audio", weight=ft.FontWeight.BOLD, size=12)
    volume_value = ft.Text("1.00x", size=12, color=ft.Colors.WHITE70)
    volume_slider = ft.Slider(min=0.0, max=3.0, value=1.0, divisions=300, round=2)
    mute_checkbox = ft.Checkbox(label="Mute", value=False)
    audio_edit_panel = ft.Column(
        [
            volume_title,
            ft.Row([ft.Text("Volume", size=12), ft.Container(expand=True), volume_value], tight=True),
            volume_slider,
            mute_checkbox,
        ],
        visible=False,
        spacing=4,
        tight=True,
    )

    # Keep the preview reasonably small so the timeline stays visible in typical browser heights.
    preview_host = ft.Container(
        height=200,
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

    def _set_selected_clip_audio(volume: Optional[float] = None, muted: Optional[bool] = None, label: str = "Audio") -> None:
        clip = _selected_clip()
        track = state.selected_track
        if not clip or track not in ("v", "a"):
            return

        before = _track_clips(track)
        out = []
        changed = False
        for c in before:
            if c.id != clip.id:
                out.append(c)
                continue
            updates = {}
            if volume is not None:
                updates["volume"] = float(volume)
            if muted is not None:
                updates["muted"] = bool(muted)
            nc = replace(c, **updates) if updates else c
            out.append(nc)
            changed = changed or (nc != c)

        if not changed:
            return
        _history_record(f"{label} {clip.name}")
        _set_track_clips(track, out)
        _mark_dirty()
        update_inspector()
        refresh_timeline()

    def on_volume_change(e: ft.ControlEvent) -> None:
        try:
            volume_value.value = f"{float(e.control.value):.2f}x"
        except Exception:
            volume_value.value = "-"
        volume_value.update()

    def on_volume_change_end(e: ft.ControlEvent) -> None:
        clip = _selected_clip()
        if not clip:
            return
        try:
            v = float(e.control.value)
        except Exception:
            return
        if abs(float(getattr(clip, "volume", 1.0)) - v) < 1e-6:
            return
        _set_selected_clip_audio(volume=v, label="Volume")

    def on_mute_change(e: ft.ControlEvent) -> None:
        clip = _selected_clip()
        if not clip:
            return
        m = bool(e.control.value)
        if bool(getattr(clip, "muted", False)) == m:
            return
        _set_selected_clip_audio(muted=m, label="Mute")

    volume_slider.on_change = on_volume_change
    volume_slider.on_change_end = on_volume_change_end
    mute_checkbox.on_change = on_mute_change

    def play_click(_e=None):
        clip = _selected_clip()
        if not clip or not preview_video:
            return
        clip_start = v_start_sec_map.get(clip.id, 0.0)
        clip_end = clip_start + clip.dur
        if state.playhead_sec < clip_start or state.playhead_sec > clip_end:
            state.playhead_sec = clip_start
        state.is_playing = True
        state.playhead_clip_id = clip.id
        _run_sync_video_to_playhead(resume=True)
        page.run_task(_playhead_loop)

    def pause_click(_e=None):
        stop_playback()

    def stop_click(_e=None):
        stop_playback()
        clip = _selected_clip()
        if clip:
            state.playhead_clip_id = clip.id
            state.playhead_sec = v_start_sec_map.get(clip.id, 0.0)
            _run_sync_video_to_playhead(resume=False)
            update_playhead_ui()

    async def _sync_video_to_playhead(resume: bool = False) -> None:
        nonlocal preview_video
        if not preview_video:
            return
        seek_sec = max(0.0, state.playhead_sec)
        clip = _selected_clip()
        if clip and state.playhead_clip_id == clip.id:
            clip_start = v_start_sec_map.get(clip.id, 0.0)
            rel = max(0.0, min(clip.dur, state.playhead_sec - clip_start))
            seek_sec = clip.in_sec + rel
        try:
            await preview_video.pause()
        except Exception:
            pass
        try:
            await preview_video.seek(int(seek_sec * 1000))
        except Exception:
            pass
        if resume:
            try:
                await preview_video.play()
            except Exception:
                pass

    def _run_sync_video_to_playhead(resume: bool = False) -> None:
        async def _do() -> None:
            await _sync_video_to_playhead(resume=resume)

        page.run_task(_do)

    def stop_playback() -> None:
        nonlocal preview_video
        state.is_playing = False
        if preview_video:
            pv = preview_video

            async def _pause_video() -> None:
                try:
                    await pv.pause()
                except Exception:
                    pass

            page.run_task(_pause_video)

    async def _playhead_loop() -> None:
        nonlocal preview_video
        last_tick = time.perf_counter()
        stale_ticks = 0
        while state.is_playing and preview_video:
            clip = _selected_clip()
            if not clip:
                stop_playback()
                break
            clip_start = v_start_sec_map.get(clip.id, 0.0)
            prev_rel_sec = max(0.0, min(clip.dur, state.playhead_sec - clip_start))
            now = time.perf_counter()
            dt = max(0.0, now - last_tick)
            last_tick = now

            try:
                pos_raw = await preview_video.get_current_position()
            except Exception:
                pos_raw = None

            pos_sec: Optional[float] = None
            if pos_raw is not None:
                try:
                    if hasattr(pos_raw, "in_milliseconds"):
                        pos_sec = max(0.0, float(pos_raw.in_milliseconds) / 1000.0)
                    elif isinstance(pos_raw, (int, float)):
                        pos_sec = max(0.0, float(pos_raw) / 1000.0)
                    elif isinstance(pos_raw, str):
                        pos_sec = _parse_time_input(pos_raw)
                except Exception:
                    pos_sec = None

            wall_rel_sec = prev_rel_sec + dt
            if pos_sec is None:
                # Web backend can occasionally return no position; keep playhead moving by wall-clock.
                rel_sec = wall_rel_sec
                stale_ticks = 0
            else:
                # Backends may report clip-relative time or absolute source time.
                rel_from_relative = max(0.0, min(clip.dur, pos_sec))
                rel_from_absolute = max(0.0, min(clip.dur, pos_sec - clip.in_sec))
                expected_rel = max(0.0, min(clip.dur, wall_rel_sec))

                # Pick the interpretation closest to the expected forward progression.
                if abs(rel_from_absolute - expected_rel) + 1e-6 < abs(rel_from_relative - expected_rel):
                    rel_backend = rel_from_absolute
                else:
                    rel_backend = rel_from_relative

                # If backend time is stale/regressing, blend toward wall-clock progression.
                if rel_backend <= prev_rel_sec + 0.001:
                    stale_ticks += 1
                else:
                    stale_ticks = 0

                if stale_ticks >= 2:
                    rel_sec = max(rel_backend, wall_rel_sec)
                else:
                    rel_sec = rel_backend

            rel_sec = max(0.0, min(clip.dur, rel_sec))
            state.playhead_clip_id = clip.id
            state.playhead_sec = clip_start + rel_sec
            update_playhead_ui()

            if state.split_pos_clip_id == clip.id:
                state.split_pos_sec = rel_sec
                split_slider.value = rel_sec
                split_label.value = f"Split: {_fmt_time(rel_sec)}"
                # Avoid frequent per-control updates while playing in web mode.
                # The control tree is rebuilt often and async method results can race
                # with disposal, causing "Control ... is not registered" errors.

            if rel_sec >= clip.dur - 0.02:
                stop_playback()
                break
            await asyncio.sleep(0.08)

    def _timeline_x_to_v1_position(x_px: float) -> tuple[Optional[str], float, float]:
        """
        Convert timeline X (same coordinate space as playhead_line center) to:
        (clip_id, global_sec_on_v1, sec_from_clip_start).
        """
        if not state.project.v_clips:
            return None, 0.0, 0.0

        x = float(x_px)
        first = state.project.v_clips[0]
        first_start_px = v_start_px_map.get(first.id, 0.0)
        if x <= first_start_px:
            return first.id, v_start_sec_map.get(first.id, 0.0), 0.0

        for c in state.project.v_clips:
            start_px = v_start_px_map.get(c.id, 0.0)
            width_px = max(1.0, float(v_clip_width_px_map.get(c.id, max(1.0, c.dur * state.px_per_sec))))
            end_px = start_px + width_px
            start_sec = v_start_sec_map.get(c.id, 0.0)
            if x <= end_px:
                ratio = max(0.0, min(1.0, (x - start_px) / width_px))
                rel_sec = c.dur * ratio
                return c.id, start_sec + rel_sec, rel_sec

        last = state.project.v_clips[-1]
        last_start_sec = v_start_sec_map.get(last.id, 0.0)
        return last.id, last_start_sec + last.dur, last.dur

    def _set_playhead_from_timeline_x(x_px: float, from_drag: bool = False) -> bool:
        clip_id, global_sec, rel_sec = _timeline_x_to_v1_position(x_px)
        if not clip_id:
            return False

        if state.is_playing:
            stop_playback()

        state.playhead_clip_id = clip_id
        state.playhead_sec = global_sec
        state.split_pos_clip_id = clip_id
        state.split_pos_sec = rel_sec

        selection_changed = state.selected_track != "v" or state.selected_clip_id != clip_id
        state.selected_track = "v"
        state.selected_clip_id = clip_id

        if selection_changed:
            update_inspector()
            return True

        split_slider.value = rel_sec
        split_label.value = f"Split: {_fmt_time(rel_sec)}"
        try:
            split_slider.update()
            split_label.update()
        except Exception:
            pass

        update_playhead_ui()
        if not from_drag:
            _run_sync_video_to_playhead(resume=False)
        return False

    def update_playhead_ui() -> None:
        if not v_start_sec_map:
            playhead_line.visible = False
            return
        playhead_line.visible = True
        sec = max(0.0, state.playhead_sec)
        px = None
        for c in state.project.v_clips:
            start_sec = v_start_sec_map.get(c.id, 0.0)
            end_sec = start_sec + c.dur
            start_px = v_start_px_map.get(c.id, 0.0)
            width_px = max(1.0, float(v_clip_width_px_map.get(c.id, max(1.0, c.dur * state.px_per_sec))))
            if sec >= start_sec and sec <= end_sec:
                rel = 0.0 if c.dur <= 0 else (sec - start_sec) / c.dur
                px = start_px + max(0.0, min(1.0, rel)) * width_px
                break
        if px is None:
            if state.project.v_clips:
                first = state.project.v_clips[0]
                last = state.project.v_clips[-1]
                first_px = v_start_px_map.get(first.id, 0.0)
                last_px = v_start_px_map.get(last.id, 0.0) + max(
                    0.0,
                    float(v_clip_width_px_map.get(last.id, last.dur * state.px_per_sec)),
                )
                if sec <= 0:
                    px = first_px
                else:
                    px = last_px
            else:
                px = 0.0
        playhead_line.left = max(0.0, timeline_v1_left_offset + px - (playhead_handle_w / 2))
        try:
            playhead_line.update()
        except Exception:
            pass
        try:
            v_row.update()
        except Exception:
            pass
        try:
            page.update()
        except Exception:
            pass

    def _playhead_timeline_x() -> float:
        return max(0.0, float(playhead_line.left or 0.0) + (playhead_handle_w / 2) - timeline_v1_left_offset)

    def on_playhead_pan_start(_e: ft.DragStartEvent) -> None:
        nonlocal playhead_drag_start_left, playhead_drag_start_pointer_x
        playhead_drag_start_left = float(playhead_line.left or 0.0)
        try:
            playhead_drag_start_pointer_x = float(_e.global_position.x)
        except Exception:
            playhead_drag_start_pointer_x = 0.0
        if state.is_playing:
            stop_playback()

    def on_playhead_pan_update(e: ft.DragUpdateEvent) -> None:
        nonlocal playhead_drag_start_pointer_x
        dx = None
        try:
            dx = float(e.global_position.x) - float(playhead_drag_start_pointer_x)
        except Exception:
            dx = None
        if dx is None:
            try:
                if e.local_delta:
                    dx = float(e.local_delta.x or 0.0)
            except Exception:
                dx = None
        if dx is None:
            try:
                dx = float(e.primary_delta or 0.0)
            except Exception:
                dx = 0.0
        target_left = max(0.0, playhead_drag_start_left + dx)
        timeline_x = target_left + (playhead_handle_w / 2) - timeline_v1_left_offset
        selection_changed = _set_playhead_from_timeline_x(timeline_x, from_drag=True)
        if not selection_changed:
            _run_sync_video_to_playhead(resume=False)

    def on_playhead_tap_down(_e: ft.TapEvent) -> None:
        _set_playhead_from_timeline_x(_playhead_timeline_x(), from_drag=False)

    def _event_local_xy(e) -> tuple[float, float]:
        try:
            return float(e.local_position.x), float(e.local_position.y)
        except Exception:
            pass
        try:
            return float(getattr(e, "local_x", 0.0) or 0.0), float(getattr(e, "local_y", 0.0) or 0.0)
        except Exception:
            return 0.0, 0.0

    def _event_global_x(e) -> Optional[float]:
        try:
            return float(e.global_position.x)
        except Exception:
            pass
        try:
            return float(getattr(e, "global_x", None))
        except Exception:
            return None
        return None

    def on_timeline_tap_down(e: ft.TapEvent) -> None:
        x, y = _event_local_xy(e)
        # Ignore top info/zoom row taps.
        if y < 36:
            return
        _set_playhead_from_timeline_x(x - timeline_v1_left_offset, from_drag=False)

    def on_timeline_pan_down(e: ft.DragDownEvent) -> None:
        x, y = _event_local_xy(e)
        if y < 36:
            return
        _set_playhead_from_timeline_x(x - timeline_v1_left_offset, from_drag=False)

    def on_timeline_pan_start(e: ft.DragStartEvent) -> None:
        nonlocal timeline_pan_active, timeline_pan_start_pointer_x, timeline_pan_start_playhead_x
        _x, y = _event_local_xy(e)
        timeline_pan_active = y >= 36
        gx = _event_global_x(e)
        timeline_pan_start_pointer_x = float(gx) if gx is not None else 0.0
        timeline_pan_start_playhead_x = _playhead_timeline_x()
        if timeline_pan_active and state.is_playing:
            stop_playback()

    def on_timeline_pan_update(e: ft.DragUpdateEvent) -> None:
        nonlocal timeline_pan_active, timeline_pan_start_pointer_x, timeline_pan_start_playhead_x
        if not timeline_pan_active:
            return

        dx = None
        gx = _event_global_x(e)
        if gx is not None:
            dx = float(gx) - float(timeline_pan_start_pointer_x)
        if dx is None:
            try:
                if e.local_delta:
                    dx = float(e.local_delta.x or 0.0)
            except Exception:
                dx = None
        if dx is None:
            try:
                dx = float(e.primary_delta or 0.0)
            except Exception:
                dx = 0.0

        target_x = max(0.0, timeline_pan_start_playhead_x + dx)
        selection_changed = _set_playhead_from_timeline_x(target_x, from_drag=True)
        if not selection_changed:
            _run_sync_video_to_playhead(resume=False)

    def on_timeline_pan_end(_e: ft.DragEndEvent) -> None:
        nonlocal timeline_pan_active
        timeline_pan_active = False

    def on_timeline_right_pan_start(e: ft.PointerEvent) -> None:
        nonlocal timeline_pan_active, timeline_pan_start_pointer_x, timeline_pan_start_playhead_x
        x, y = _event_local_xy(e)
        timeline_pan_active = y >= 36
        timeline_pan_start_pointer_x = x
        timeline_pan_start_playhead_x = _playhead_timeline_x()
        if timeline_pan_active and state.is_playing:
            stop_playback()

    def on_timeline_right_pan_update(e: ft.PointerEvent) -> None:
        nonlocal timeline_pan_active, timeline_pan_start_pointer_x, timeline_pan_start_playhead_x
        if not timeline_pan_active:
            return
        x, _y = _event_local_xy(e)
        dx = x - timeline_pan_start_pointer_x
        target_x = max(0.0, timeline_pan_start_playhead_x + dx)
        selection_changed = _set_playhead_from_timeline_x(target_x, from_drag=True)
        if not selection_changed:
            _run_sync_video_to_playhead(resume=False)

    def on_timeline_right_pan_end(_e: ft.PointerEvent) -> None:
        nonlocal timeline_pan_active
        timeline_pan_active = False

    playhead_handle.on_pan_start = on_playhead_pan_start
    playhead_handle.on_pan_update = on_playhead_pan_update
    playhead_handle.on_horizontal_drag_start = on_playhead_pan_start
    playhead_handle.on_horizontal_drag_update = on_playhead_pan_update
    playhead_handle.on_tap_down = on_playhead_tap_down

    def update_inspector() -> None:
        def _prepare_web_preview_src(src: str) -> Optional[str]:
            if not is_web:
                return src
            try:
                src_path = Path(src)
                if not src_path.exists():
                    return None

                st = src_path.stat()
                key = f"{src_path.resolve()}|{st.st_mtime_ns}|{st.st_size}"
                cached = web_preview_cache.get(key)
                if cached:
                    return cached

                ext = (src_path.suffix or ".mp4").lower()
                digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:16]
                rel = Path("_preview_cache") / f"{digest}{ext}"
                dst = root / "assets" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)

                if not dst.exists() or dst.stat().st_size != st.st_size:
                    shutil.copy2(src_path, dst)

                rel_web = str(rel).replace("\\", "/")
                web_preview_cache[key] = rel_web
                return rel_web
            except Exception as ex:
                log.exception("prepare web preview failed: %s", ex)
                return None

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
            state.split_pos_clip_id = None
            state.split_pos_sec = 0.0
            state.playhead_clip_id = None
            state.playhead_sec = 0.0
            trim_in.value = ""
            trim_out.value = ""
            trim_row.visible = False
            audio_edit_panel.visible = False
            audio_controls.visible = False
            audio_pos.visible = False
            _stop_audio_to_clip_start()
            preview_host.content = ft.Text("Preview (optional)", color=ft.Colors.WHITE70)
            nonlocal preview_video
            preview_video = None
            stop_playback()
            update_playhead_ui()
            page.update()
            return

        prefix = "[V1]" if state.selected_track == "v" else "[A1]"
        selected_title.value = f"{prefix} {clip.name}"
        selected_range.value = f"in={_fmt_time(clip.in_sec)}  out={_fmt_time(clip.out_sec)}  dur={_fmt_time(clip.dur)}"
        trim_in.value = _fmt_time(clip.in_sec)
        trim_out.value = _fmt_time(clip.out_sec)
        trim_row.visible = True
        audio_edit_panel.visible = True

        try:
            volume_slider.value = float(getattr(clip, "volume", 1.0) or 1.0)
        except Exception:
            volume_slider.value = 1.0
        mute_checkbox.value = bool(getattr(clip, "muted", False))
        volume_value.value = f"{float(volume_slider.value):.2f}x"

        split_slider.min = 0
        split_slider.max = max(0.01, clip.dur)
        if state.split_pos_clip_id != clip.id:
            # Default split position when selecting a new clip.
            state.split_pos_clip_id = clip.id
            state.split_pos_sec = min(clip.dur / 2, max(0.01, clip.dur - 0.01))
        state.split_pos_sec = max(0.0, min(clip.dur, float(state.split_pos_sec)))
        split_slider.value = state.split_pos_sec
        split_label.value = f"Split: {_fmt_time(state.split_pos_sec)}"
        state.playhead_clip_id = clip.id
        clip_start_sec = v_start_sec_map.get(clip.id, 0.0)
        clip_end_sec = clip_start_sec + clip.dur
        if state.playhead_sec < clip_start_sec or state.playhead_sec > clip_end_sec:
            if state.split_pos_clip_id == clip.id:
                state.playhead_sec = clip_start_sec + state.split_pos_sec
            else:
                state.playhead_sec = clip_start_sec

        if state.selected_track == "v":
            audio_controls.visible = False
            audio_pos.visible = False
            _stop_audio_to_clip_start()
            preview_src = _prepare_web_preview_src(clip.src)
            if preview_src:
                pv = ftv.Video(
                    expand=True,
                    playlist=[ftv.VideoMedia(preview_src)],
                    autoplay=False,
                    muted=True,
                    show_controls=True,
                )
                preview_host.content = pv
                preview_video = pv
            else:
                preview_host.content = ft.Text("Preview load failed", color=ft.Colors.WHITE70)
                preview_video = None
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
            preview_video = None
        update_playhead_ui()
        _run_sync_video_to_playhead(resume=False)
        page.update()

    def on_split_slider(e: ft.ControlEvent) -> None:
        try:
            val = float(split_slider.value)
        except Exception:
            return
        state.split_pos_sec = val
        state.split_pos_clip_id = state.selected_clip_id
        split_label.value = f"Split: {_fmt_time(val)}"
        split_label.update()
        # Move playhead to match split slider position.
        clip = _selected_clip()
        if clip and state.selected_clip_id:
            start = v_start_sec_map.get(state.selected_clip_id, clip.in_sec)
            state.playhead_clip_id = state.selected_clip_id
            state.playhead_sec = start + val
            update_playhead_ui()
            _run_sync_video_to_playhead(resume=False)
        refresh_timeline()

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

        def _select_at(position_px: float | None = None) -> None:
            state.selected_track = track
            state.selected_clip_id = clip.id
            state.playhead_clip_id = clip.id
            # If user clicked within the clip, set split position to that proportion.
            if position_px is not None and dur_px > 0 and clip.dur > 0:
                ratio = max(0.0, min(1.0, position_px / dur_px))
                state.split_pos_clip_id = clip.id
                state.split_pos_sec = max(0.0, min(clip.dur, clip.dur * ratio))
                state.playhead_sec = v_start_sec_map.get(clip.id, 0.0) + state.split_pos_sec
                update_playhead_ui()
                _run_sync_video_to_playhead(resume=False)
            update_inspector()
            refresh_timeline()

        block_height = 28 if is_audio else 36
        cont = ft.Container(
            width=dur_px,
            height=block_height,
            padding=6,
            border_radius=8,
            bgcolor=ft.Colors.AMBER_600 if selected else color,
            content=ft.Text(label, size=12, no_wrap=True),
        )

        stack_children = [cont]
        if selected and clip.dur > 0:
            try:
                play_px = max(0.0, min(dur_px, (float(split_slider.value) / clip.dur) * dur_px))
            except Exception:
                play_px = None
            if play_px is not None:
                stack_children.append(
                    ft.Container(
                        left=play_px,
                        top=0,
                        bottom=0,
                        width=2,
                        bgcolor=ft.Colors.AMBER_200,
                        opacity=0.85,
                    )
                )

        clip_surface = ft.Stack(
            controls=stack_children,
            width=dur_px,
            height=block_height,
        )

        draggable = ft.Draggable(
            group="tl",
            data={"kind": "clip", "track": track, "id": clip.id},
            axis=ft.Axis.HORIZONTAL,
            on_drag_start=lambda _e: _select_at(),
            content=clip_surface,
            content_feedback=ft.Container(width=80, height=22, bgcolor=ft.Colors.WHITE24, border_radius=8),
        )
        return draggable

    def refresh_timeline() -> None:
        v_row.controls.clear()
        a_row.controls.clear()
        v_start_sec_map.clear()
        v_start_px_map.clear()
        v_clip_width_px_map.clear()

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
                    width=190,
                    height=height,
                    alignment=ft.Alignment(0, 0),
                    border_radius=10,
                    bgcolor=ft.Colors.BLUE_GREY_900,
                    border=ft.Border.all(1, ft.Colors.WHITE24),
                    content=ft.Text("Drop to append", size=12),
                ),
            )

        # V1 clips
        v_time = 0.0
        v_px = 0.0
        for c in state.project.v_clips:
            def _make_on_accept_v(target_id: str):
                def _on_accept(e: ft.DragTargetEvent) -> None:
                    payload = _payload(e)
                    if isinstance(payload, dict):
                        handle_drop("v", target_id, payload)
                return _on_accept

            width = max(70, int(c.dur * state.px_per_sec))
            block = clip_block("v", c.id)
            drop_zone = ft.DragTarget(
                group="tl",
                on_accept=_make_on_accept_v(c.id),
                content=ft.Container(
                    width=16,
                    height=40,
                    bgcolor=ft.Colors.TRANSPARENT,
                    border=ft.Border(left=ft.BorderSide(1, ft.Colors.WHITE12)),
                ),
            )
            v_row.controls.append(ft.Row(spacing=0, controls=[drop_zone, ft.Container(width=width, content=block)]))
            v_start_sec_map[c.id] = v_time
            v_start_px_map[c.id] = v_px + 16  # clip starts after drop zone
            v_clip_width_px_map[c.id] = width
            v_time += c.dur
            v_px += 16 + width

        # A1 clips
        for c in state.project.a_clips:
            def _make_on_accept_a(target_id: str):
                def _on_accept(e: ft.DragTargetEvent) -> None:
                    payload = _payload(e)
                    if isinstance(payload, dict):
                        handle_drop("a", target_id, payload)
                return _on_accept

            width = max(70, int(c.dur * state.px_per_sec))
            block = clip_block("a", c.id)
            drop_zone = ft.DragTarget(
                group="tl",
                on_accept=_make_on_accept_a(c.id),
                content=ft.Container(
                    width=16,
                    height=34,
                    bgcolor=ft.Colors.TRANSPARENT,
                    border=ft.Border(left=ft.BorderSide(1, ft.Colors.WHITE12)),
                ),
            )
            a_row.controls.append(ft.Row(spacing=0, controls=[drop_zone, ft.Container(width=width, content=block)]))

        v_row.controls.append(_end_drop("v", height=36))
        a_row.controls.append(_end_drop("a", height=28))

        v_total = _fmt_time(total_duration(state.project.v_clips))
        a_total = _fmt_time(total_duration(state.project.a_clips))
        timeline_info.value = f"V1: {len(state.project.v_clips)} clips | {v_total}   A1: {len(state.project.a_clips)} clips | {a_total}"
        nonlocal timeline_total_sec
        timeline_total_sec = total_duration(state.project.v_clips)
        update_playhead_ui()
        page.update()

    # ---------- Actions ----------
    def import_click(_e):
        async def _pick() -> None:
            picked = await file_picker.pick_files(
                allow_multiple=True,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=[
                    "mp4",
                    "mov",
                    "mkv",
                    "avi",
                    "webm",
                    "flv",
                    "wmv",
                    "m4v",
                    "mp3",
                    "wav",
                    "flac",
                    "aac",
                    "ogg",
                    "m4a",
                ],
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
                if any(m.path == f.path for m in state.media):
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
                            width=info.width,
                            height=info.height,
                            fps=info.fps,
                            video_codec=info.video_codec,
                            audio_codec=info.audio_codec,
                            video_bitrate=info.video_bitrate,
                            audio_bitrate=info.audio_bitrate,
                            file_size_bytes=info.file_size_bytes,
                            pixel_format=info.pixel_format,
                            sample_rate=info.sample_rate,
                            channels=info.channels,
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
        split_at = float(split_slider.value)
        split_global_sec = None
        if selected and state.selected_track == "v":
            split_global_sec = v_start_sec_map.get(selected.id, 0.0) + split_at
        clips, new_selected, msg = split_clip(
            before,
            state.selected_clip_id,
            split_at,
        )
        if clips != before:
            _history_record(f"Split {selected.name if selected else 'clip'}")

        chosen_id = new_selected
        if clips != before and new_selected:
            try:
                left_idx = next(i for i, c in enumerate(clips) if c.id == new_selected)
            except StopIteration:
                left_idx = -1
            if 0 <= left_idx < len(clips) - 1:
                right = clips[left_idx + 1]
                if selected and right.src == selected.src:
                    chosen_id = right.id
                    state.split_pos_clip_id = chosen_id
                    state.split_pos_sec = 0.0

        state.selected_clip_id = chosen_id
        _set_track_clips(state.selected_track, clips)
        if clips != before:
            _mark_dirty()
            if split_global_sec is not None:
                state.playhead_sec = split_global_sec
                state.playhead_clip_id = state.selected_clip_id
        snack(msg)
        update_inspector()
        refresh_timeline()

    def duplicate_click(_e):
        if not state.selected_track or not state.selected_clip_id:
            snack("เลือกคลิปก่อน")
            return

        before = _track_clips(state.selected_track)
        selected = _find_clip(state.selected_track, state.selected_clip_id)
        clips, new_id_val, msg = duplicate_clip(before, state.selected_clip_id)
        if clips != before:
            _history_record(f"Duplicate {selected.name if selected else 'clip'}")
            _set_track_clips(state.selected_track, clips)
            if new_id_val:
                state.selected_clip_id = new_id_val
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
            ft.OutlinedButton("Duplicate", icon=ft.Icons.CONTENT_COPY, on_click=duplicate_click),
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
        expand=True,
        content=ft.ListView(
            [
                ft.Text("Inspector", weight=ft.FontWeight.BOLD),
                selected_title,
                selected_range,
                ft.Row(
                    [
                        ft.ElevatedButton("Play", icon=ft.Icons.PLAY_ARROW, on_click=play_click),
                        ft.OutlinedButton("Pause", icon=ft.Icons.PAUSE, on_click=pause_click),
                        ft.OutlinedButton("Stop", icon=ft.Icons.STOP, on_click=stop_click),
                    ],
                    spacing=6,
                    wrap=True,
                ),
                ft.Divider(height=8),
                split_label,
                split_slider,
                trim_row,
                audio_edit_panel,
                audio_controls,
                audio_pos,
                ft.Text("Tip: เลือกคลิปแล้วกด Split", size=12, color=ft.Colors.WHITE70),
            ],
            spacing=6,
            expand=True,
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

    # On web, we observed cases where the main row content could overflow and visually cover the
    # fixed-height timeline panel. Clip the main area so the timeline is always visible.
    main_row = ft.Container(
        # Web viewport heights vary a lot; using a fixed height keeps the timeline visible
        # without relying on page scroll behavior.
        expand=not is_web,
        height=440 if is_web else None,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
        content=ft.Row([left_panel, ft.VerticalDivider(width=8), right_panel], expand=True),
    )

    timeline_content = ft.Column(
        [
            ft.Row([timeline_info, ft.Container(expand=True), ft.Text("Zoom"), timeline_zoom]),
            ft.Divider(height=6),
            ft.Row(
                [
                    ft.Text("V1", width=timeline_lane_label_w),
                    ft.Container(width=timeline_lane_gap_w),
                    v_row,
                ],
                expand=True,
                spacing=0,
            ),
            ft.Row(
                [
                    ft.Text("A1", width=timeline_lane_label_w),
                    ft.Container(width=timeline_lane_gap_w),
                    a_row,
                ],
                expand=True,
                spacing=0,
            ),
        ],
        expand=True,
    )

    timeline_surface = ft.GestureDetector(
        mouse_cursor=ft.MouseCursor.MOVE,
        on_tap_down=on_timeline_tap_down,
        on_pan_down=on_timeline_pan_down,
        on_pan_start=on_timeline_pan_start,
        on_pan_update=on_timeline_pan_update,
        on_pan_end=on_timeline_pan_end,
        on_horizontal_drag_start=on_timeline_pan_start,
        on_horizontal_drag_update=on_timeline_pan_update,
        on_horizontal_drag_end=on_timeline_pan_end,
        on_right_pan_start=on_timeline_right_pan_start,
        on_right_pan_update=on_timeline_right_pan_update,
        on_right_pan_end=on_timeline_right_pan_end,
        content=ft.Stack([timeline_content, playhead_line]),
    )

    timeline = ft.Container(
        padding=10,
        height=180,
        border_radius=12,
        bgcolor=ft.Colors.BLUE_GREY_900,
        content=timeline_surface,
    )

    page.add(ft.Column([toolbar, main_row, timeline], expand=True, spacing=10))

    system_test_enabled = os.environ.get("MINICUT_SYSTEM_TEST", "0") == "1"

    # initial
    refresh_media()
    refresh_timeline()
    update_inspector()

    # Convenience: auto-open the default project file on startup.
    # Skip in system-test mode to keep tests deterministic.
    if not system_test_enabled and state.project_path:
        try:
            default_project = Path(state.project_path)
        except Exception:
            default_project = None
        if default_project and default_project.exists():
            _open_project(str(default_project))
            # Make split UX obvious: select the first clip if nothing is selected.
            if state.project.v_clips and not state.selected_clip_id:
                state.selected_track = "v"
                state.selected_clip_id = state.project.v_clips[0].id
                update_inspector()
                refresh_timeline()


    def _choose_demo_files(demo_dir: Path) -> List[Path]:
        files = sorted([p for p in demo_dir.glob("*.mp4") if p.is_file()])
        if len(files) < 2:
            return files[:2]
        # Prefer numbered clips if present.
        p1 = next((p for p in files if "_1_" in p.name), None)
        p2 = next((p for p in files if "_2_" in p.name), None)
        if p1 and p2:
            return [p1, p2]
        return files[:2]

    def _take_active_window_screenshot(tag: str) -> Optional[str]:
        """
        Capture the current active window to the default screenshot location and
        rename it to include `tag`. Best-effort: requires screenshot skill helper.
        """
        script = Path.home() / ".codex" / "skills" / "screenshot" / "scripts" / "take_screenshot.ps1"
        if not script.exists():
            return None
        try:
            p = subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script), "-ActiveWindow"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
        except Exception:
            log.exception("screenshot failed")
            return None

        out = (p.stdout or "").strip().splitlines()
        if not out:
            return None
        src = Path(out[-1].strip())
        if not src.exists():
            return None

        safe_tag = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in str(tag))
        dest = src.with_name(f"minicut_{safe_tag}_{src.name}")
        try:
            if dest.exists():
                dest = src.with_name(f"minicut_{safe_tag}_{src.stem}_{os.getpid()}{src.suffix}")
            src.rename(dest)
            return str(dest)
        except Exception:
            return str(src)

    async def _run_system_test() -> None:
        # Give the window time to appear.
        await asyncio.sleep(1.0)

        shots: List[str] = []
        async def _try_window(coro, timeout_sec: float = 1.2) -> None:
            try:
                await asyncio.wait_for(coro, timeout=timeout_sec)
            except Exception:
                return

        async def _shot(tag: str) -> None:
            try:
                page.window.focused = True
            except Exception:
                pass
            page.update()
            await _try_window(page.window.to_front())
            await asyncio.sleep(0.25)
            p = _take_active_window_screenshot(tag)
            if p:
                shots.append(p)

        try:
            try:
                page.window.always_on_top = True
            except Exception:
                pass

            try:
                page.window.focused = True
            except Exception:
                pass
            page.update()
            await _try_window(page.window.center())
            await _try_window(page.window.to_front())
            await asyncio.sleep(0.8)

            await _shot("01_start")

            demo_dir = Path(os.environ.get("MINICUT_SYSTEM_TEST_DIR", r"C:\Users\sj88s\Videos\000001"))
            demo_files = _choose_demo_files(demo_dir)
            if len(demo_files) < 2:
                snack(f"System test: not enough mp4 files in {demo_dir}")
                return

            bins = get_bins()
            if not bins:
                return
            ffmpeg, ffprobe = bins

            # Import media into Media Bin.
            for src in demo_files:
                if any(m.path == str(src) for m in state.media):
                    continue
                info = probe_media(ffprobe, str(src))
                state.media.append(
                    MediaItem(
                        path=str(src),
                        duration=info.duration,
                        has_video=info.has_video,
                        has_audio=info.has_audio,
                        width=info.width,
                        height=info.height,
                        fps=info.fps,
                        video_codec=info.video_codec,
                        audio_codec=info.audio_codec,
                        video_bitrate=info.video_bitrate,
                        audio_bitrate=info.audio_bitrate,
                        file_size_bytes=info.file_size_bytes,
                        pixel_format=info.pixel_format,
                        sample_rate=info.sample_rate,
                        channels=info.channels,
                    )
                )
            refresh_media()
            await asyncio.sleep(0.6)
            await _shot("02_imported")

            # Add both files to V1 timeline.
            for src in demo_files:
                mi = next((m for m in state.media if m.path == str(src)), None)
                if not mi:
                    continue
                state.project.v_clips = add_clip_end(state.project.v_clips, mi.path, mi.duration)
            _mark_dirty()
            refresh_timeline()

            # Select first clip for inspector visibility.
            if state.project.v_clips:
                state.selected_track = "v"
                state.selected_clip_id = state.project.v_clips[0].id
            update_inspector()
            await asyncio.sleep(0.6)
            await _shot("03_on_timeline")

            # Playback smoke test: play briefly and ensure playhead can advance.
            play_click(None)
            await asyncio.sleep(1.1)
            await _shot("03_playing")
            pause_click(None)
            await asyncio.sleep(0.3)
            await _shot("03_paused")

            # Split each clip in half and keep the first half.
            for i, src in enumerate(demo_files, start=1):
                clip = next((c for c in state.project.v_clips if c.src == str(src)), None)
                if not clip:
                    continue

                state.selected_track = "v"
                state.selected_clip_id = clip.id
                update_inspector()
                refresh_timeline()
                await asyncio.sleep(0.5)
                await _shot(f"04_clip{i}_selected")

                # Split at half: update_inspector() already sets the slider to half.
                split_click(None)
                await asyncio.sleep(0.5)
                await _shot(f"05_clip{i}_split")

                # Delete the second half (the clip right after the selected first piece).
                clips = state.project.v_clips
                idx = next((k for k, c in enumerate(clips) if c.id == state.selected_clip_id), None)
                if idx is not None and idx + 1 < len(clips):
                    state.selected_track = "v"
                    state.selected_clip_id = clips[idx + 1].id
                    update_inspector()
                    delete_click(None)
                    await asyncio.sleep(0.5)
                await _shot(f"06_clip{i}_kept_first")

            # Export result (no file picker).
            out_path = os.environ.get(
                "MINICUT_SYSTEM_TEST_OUT",
                str(demo_dir / "minicut_export_test.mp4"),
            )
            out_file = Path(out_path)
            try:
                out_file.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            exporting = ft.AlertDialog(
                modal=False,
                title=ft.Text("System test export"),
                content=ft.Text("Exporting..."),
            )
            page.show_dialog(exporting)
            await asyncio.sleep(0.6)
            await _shot("07_exporting")

            v_clips = list(state.project.v_clips)
            a_clips = list(state.project.a_clips)
            audio_mode = state.export_audio_mode
            try:
                await asyncio.to_thread(
                    export_project,
                    ffmpeg,
                    ffprobe,
                    v_clips,
                    a_clips,
                    out_path,
                    audio_mode=audio_mode,
                )
                ok = True
                err = ""
            except Exception as ex:
                ok = False
                err = str(ex)

            size_bytes = 0
            if ok:
                try:
                    size_bytes = out_file.stat().st_size
                except Exception:
                    size_bytes = 0

            exporting.content = ft.Text(
                "Export done\n"
                f"Path: {out_path}\n"
                f"Size: {_fmt_bytes(size_bytes)}\n\n"
                f"Screenshots:\n" + "\n".join(shots)
                if ok
                else f"Export failed: {err}"
            )
            exporting.actions = [ft.TextButton("Close", on_click=lambda _e: page.pop_dialog())]
            page.update()
            await asyncio.sleep(0.8)
            await _shot("08_export_done")

            # Close the app after a short delay to keep screenshots visible.
            await asyncio.sleep(1.5)
            try:
                await page.window.close()
            except Exception:
                pass
        except Exception:
            log.exception("system test failed")
        finally:
            # Ensure the process terminates even if the window close doesn't.
            await asyncio.sleep(0.8)
            try:
                await _try_window(page.window.close(), timeout_sec=1.0)
            except Exception:
                pass
            try:
                os._exit(0)
            except Exception:
                pass

    if system_test_enabled:
        page.run_task(_run_system_test)

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
