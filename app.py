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
from core.ffmpeg import (
    FFmpegNotFound,
    ExportCancelled,
    export_project,
    export_project_with_progress,
    probe_media,
    resolve_ffmpeg_bins,
)
from core.history import HistoryEntry, HistoryManager
from core.model import MAX_CLIP_SPEED, MIN_CLIP_SPEED, ExportSettings, Project, Transition, normalize_speed
from core.project_io import load_project, save_project
from core.shortcuts import (
    ACTION_DELETE,
    ACTION_DUPLICATE,
    ACTION_EXPORT,
    ACTION_IMPORT,
    ACTION_RAZOR,
    ACTION_REDO,
    ACTION_SAVE,
    ACTION_SELECT_NEXT,
    ACTION_SELECT_PREV,
    ACTION_SHOW_SHORTCUTS,
    ACTION_SPLIT,
    ACTION_TOGGLE_PLAY_PAUSE,
    ACTION_TRIM_IN,
    ACTION_TRIM_OUT,
    ACTION_UNDO,
    ACTION_ZOOM_IN,
    ACTION_ZOOM_OUT,
    resolve_shortcut_action,
    shortcut_legend,
)
from core.thumbnails import generate_thumbnail, generate_waveform
from core.timeline import (
    add_clip_end,
    duplicate_clip,
    insert_clip_before,
    move_clip_before,
    split_clip,
    split_clip_at_timeline_sec,
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
        # Selected timeline track id (e.g. "v1_xxx", "a2_xxx").
        self.selected_track: Optional[str] = None
        self.selected_clip_id: Optional[str] = None
        self.px_per_sec: float = 60.0  # timeline zoom
        self.export_audio_mode: str = "mix"  # "mix" | "a1_only" | "v1_only"
        self.export_settings: ExportSettings = ExportSettings()
        # Split marker time (seconds) for the currently selected clip.
        self.split_pos_sec: float = 0.0
        self.split_pos_clip_id: Optional[str] = None
        # Playback / playhead
        self.playhead_sec: float = 0.0  # global timeline seconds on the active video timeline track
        self.playhead_clip_id: Optional[str] = None
        self.is_playing: bool = False
        # Timeline snap/grid
        self.snap_enabled: bool = True
        self.snap_to_grid: bool = True
        self.snap_to_edges: bool = True
        self.snap_grid_sec: float = 0.5
        self.snap_threshold_px: float = 12.0


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
    timeline_visual_web_cache: dict[str, str] = {}
    timeline_cache_root = root / ".cache" / "timeline_visuals"
    timeline_thumb_dir = timeline_cache_root / "thumbs"
    timeline_wave_dir = timeline_cache_root / "waves"
    timeline_ffmpeg_path: Optional[str] = None
    timeline_visual_disabled: bool = False
    history = HistoryManager(limit=50)
    cfg = ConfigStore.default()
    typing_shortcuts_blocked = False
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
    timeline_header_h = 116.0
    playhead_line = ft.Container(left=0, top=timeline_header_h, content=playhead_handle)
    preview_video: Optional[ftv.Video] = None
    preview_video_src: Optional[str] = None
    playback_loop_id: int = 0
    v_start_sec_map: dict[str, float] = {}
    v_start_px_map: dict[str, float] = {}
    v_clip_width_px_map: dict[str, float] = {}
    playhead_drag_start_left: float = 0.0
    playhead_drag_start_pointer_x: float = 0.0
    timeline_pan_active: bool = False
    timeline_pan_start_pointer_x: float = 0.0
    timeline_pan_start_playhead_x: float = 0.0
    timeline_lane_label_w = 78.0
    timeline_lane_gap_w = 8.0
    timeline_v1_left_offset = timeline_lane_label_w + timeline_lane_gap_w
    timeline_total_sec: float = 0.0
    export_in_progress: bool = False

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

    def _existing_dir_or_none(path_like: Optional[str]) -> Optional[str]:
        p = str(path_like or "").strip()
        if not p:
            return None
        try:
            d = Path(p)
            if d.suffix:
                d = d.parent
            d = d.resolve()
            if d.exists() and d.is_dir():
                return str(d)
        except Exception:
            return None
        return None

    def _initial_project_dir() -> Optional[str]:
        return (
            _existing_dir_or_none(cfg.last_project_dir())
            or _existing_dir_or_none(state.project_path)
            or _existing_dir_or_none(str(root))
        )

    def _initial_export_dir() -> Optional[str]:
        return (
            _existing_dir_or_none(cfg.last_export_dir())
            or _existing_dir_or_none(state.project_path)
            or _existing_dir_or_none(cfg.last_project_dir())
            or _existing_dir_or_none(str(root))
        )

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

    def _clip_speed(clip) -> float:
        return normalize_speed(getattr(clip, "speed", 1.0), default=1.0)

    def _timeline_rel_to_source_sec(clip, rel_sec: float) -> float:
        speed = _clip_speed(clip)
        rel = max(0.0, min(float(clip.dur), float(rel_sec)))
        return float(clip.in_sec) + (rel * speed)

    def _source_abs_to_timeline_rel(clip, source_sec: float) -> float:
        speed = _clip_speed(clip)
        rel = (float(source_sec) - float(clip.in_sec)) / speed
        return max(0.0, min(float(clip.dur), rel))

    def _source_rel_to_timeline_rel(clip, source_rel_sec: float) -> float:
        speed = _clip_speed(clip)
        rel = float(source_rel_sec) / speed
        return max(0.0, min(float(clip.dur), rel))

    _update_title()

    def _track_obj(track_id: Optional[str]):
        if not track_id:
            return None
        return state.project.get_track(str(track_id))

    def _track_kind(track_id: Optional[str]) -> Optional[str]:
        t = _track_obj(track_id)
        return str(t.kind) if t is not None else None

    def _track_name(track_id: Optional[str]) -> str:
        t = _track_obj(track_id)
        return str(t.name) if t is not None else "-"

    def _primary_video_track_id() -> str:
        return state.project.primary_video_track().id

    def _primary_audio_track_id() -> str:
        return state.project.primary_audio_track().id

    def _timeline_video_track():
        for t in state.project.video_tracks:
            if t.visible and t.clips:
                return t
        for t in state.project.video_tracks:
            if t.clips:
                return t
        return state.project.primary_video_track()

    def _timeline_video_track_id() -> str:
        return _timeline_video_track().id

    def _timeline_video_clips():
        return list(_timeline_video_track().clips)

    def _timeline_video_total_sec() -> float:
        return max(0.0, total_duration(_timeline_video_clips()))

    def _timeline_sec_to_position(sec: float) -> tuple[Optional[str], float, float]:
        clips = _timeline_video_clips()
        if not clips:
            return None, 0.0, 0.0
        s = max(0.0, min(float(sec), _timeline_video_total_sec()))
        acc = 0.0
        for c in clips:
            end = acc + float(c.dur)
            if s <= end + 1e-9:
                rel = max(0.0, min(float(c.dur), s - acc))
                return c.id, s, rel
            acc = end
        last = clips[-1]
        return last.id, _timeline_video_total_sec(), float(last.dur)

    def _timeline_edge_points_sec() -> List[float]:
        clips = _timeline_video_clips()
        points: List[float] = [0.0]
        acc = 0.0
        for c in clips:
            points.append(acc)
            acc += float(c.dur)
            points.append(acc)
        if acc > 0.0:
            points.append(acc)
        return points

    def _snap_sec(sec: float) -> tuple[float, Optional[str]]:
        if not bool(state.snap_enabled):
            return float(sec), None

        total = _timeline_video_total_sec()
        s = max(0.0, min(float(sec), total))
        threshold_sec = max(0.0, float(state.snap_threshold_px)) / max(1.0, float(state.px_per_sec))

        best = s
        best_dist = float("inf")
        best_kind: Optional[str] = None

        if bool(state.snap_to_edges):
            for p in _timeline_edge_points_sec():
                d = abs(float(p) - s)
                if d < best_dist:
                    best = float(p)
                    best_dist = d
                    best_kind = "edge"

        if bool(state.snap_to_grid):
            try:
                step = float(state.snap_grid_sec)
            except Exception:
                step = 0.5
            if step > 0:
                grid = round(s / step) * step
                grid = max(0.0, min(grid, total))
                d = abs(grid - s)
                if d < best_dist:
                    best = float(grid)
                    best_dist = d
                    best_kind = "grid"

        if best_dist <= threshold_sec:
            return best, best_kind
        return s, None

    def _is_selected_video() -> bool:
        return _track_kind(state.selected_track) == "video"

    def _is_selected_audio() -> bool:
        return _track_kind(state.selected_track) == "audio"

    def _track_clips(track_id: str):
        t = _track_obj(track_id)
        return list(t.clips) if t is not None else []

    def _set_track_clips(track_id: str, clips):
        t = _track_obj(track_id)
        if t is not None:
            t.clips = list(clips)

    def _find_clip(track_id: str, clip_id: str):
        return next((c for c in _track_clips(track_id) if c.id == clip_id), None)

    def _selected_clip():
        if not state.selected_track or not state.selected_clip_id:
            return None
        if _track_obj(state.selected_track) is None:
            return None
        return _find_clip(state.selected_track, state.selected_clip_id)

    def _get_timeline_ffmpeg() -> Optional[str]:
        nonlocal timeline_ffmpeg_path, timeline_visual_disabled
        if timeline_visual_disabled:
            return None
        if timeline_ffmpeg_path:
            return timeline_ffmpeg_path
        try:
            ffmpeg, _ffprobe = resolve_ffmpeg_bins(root)
            timeline_ffmpeg_path = ffmpeg
            return timeline_ffmpeg_path
        except Exception:
            timeline_visual_disabled = True
            return None

    def _prepare_web_asset_src(src: str, bucket: str = "_timeline_cache") -> Optional[str]:
        if not is_web:
            return src
        try:
            src_path = Path(src)
            if not src_path.exists():
                return None

            st = src_path.stat()
            key = f"{bucket}|{src_path.resolve()}|{st.st_mtime_ns}|{st.st_size}"
            cached = timeline_visual_web_cache.get(key)
            if cached:
                return cached

            ext = (src_path.suffix or ".png").lower()
            digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:16]
            rel = Path(bucket) / f"{digest}{ext}"
            dst = root / "assets" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)

            if not dst.exists() or dst.stat().st_size != st.st_size:
                shutil.copy2(src_path, dst)

            rel_web = str(rel).replace("\\", "/")
            timeline_visual_web_cache[key] = rel_web
            return rel_web
        except Exception:
            return None

    def _timeline_clip_visual_src(track_id: str, clip) -> Optional[str]:
        ffmpeg = _get_timeline_ffmpeg()
        if not ffmpeg:
            return None
        try:
            kind = _track_kind(track_id)
            if kind == "video":
                img = generate_thumbnail(
                    ffmpeg_path=ffmpeg,
                    src=clip.src,
                    in_sec=clip.in_sec,
                    cache_dir=timeline_thumb_dir,
                    width=320,
                )
                if not img:
                    return None
                return _prepare_web_asset_src(img, "_timeline_cache_v")

            if kind != "audio":
                return None

            img = generate_waveform(
                ffmpeg_path=ffmpeg,
                src=clip.src,
                in_sec=clip.in_sec,
                duration=max(0.0, float(clip.out_sec) - float(clip.in_sec)),
                cache_dir=timeline_wave_dir,
                width=420,
                height=42,
            )
            if not img:
                return None
            return _prepare_web_asset_src(img, "_timeline_cache_a")
        except Exception:
            return None

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

    undo_btn = ft.IconButton(ft.Icons.UNDO, tooltip="Undo (Ctrl/Cmd+Z)", on_click=undo_click, disabled=True)
    redo_btn = ft.IconButton(ft.Icons.REDO, tooltip="Redo (Ctrl/Cmd+Y)", on_click=redo_click, disabled=True)
    shortcuts_btn = ft.IconButton(
        ft.Icons.KEYBOARD,
        tooltip="Keyboard shortcuts (F1 / ?)",
        on_click=lambda _e: _show_shortcuts_dialog(),
    )

    def _refresh_history_controls() -> None:
        undo_btn.disabled = not history.can_undo()
        redo_btn.disabled = not history.can_redo()

        if history.can_undo():
            undo_btn.tooltip = f"Undo: {history.peek_undo_label()} (Ctrl/Cmd+Z)"
        else:
            undo_btn.tooltip = "Undo (Ctrl/Cmd+Z)"

        if history.can_redo():
            redo_btn.tooltip = f"Redo: {history.peek_redo_label()} (Ctrl/Cmd+Y)"
        else:
            redo_btn.tooltip = "Redo (Ctrl/Cmd+Y)"

    def _select_neighbor(delta: int) -> None:
        selected_track = _track_obj(state.selected_track)
        if selected_track is None:
            candidates = [t for t in state.project.tracks if t.clips]
            selected_track = candidates[0] if candidates else state.project.primary_video_track()
        if selected_track is None:
            return

        track_id = selected_track.id
        clips = _track_clips(track_id)
        if not clips:
            return

        idx = 0
        if state.selected_track == track_id and state.selected_clip_id:
            for i, c in enumerate(clips):
                if c.id == state.selected_clip_id:
                    idx = i
                    break

        new_idx = max(0, min(len(clips) - 1, idx + int(delta)))
        if new_idx == idx and state.selected_clip_id == clips[idx].id:
            return

        state.selected_track = track_id
        state.selected_clip_id = clips[new_idx].id
        update_inspector()
        refresh_timeline()

    def _show_shortcuts_dialog(_e=None) -> None:
        rows = []
        for keys, desc in shortcut_legend():
            rows.append(
                ft.Row(
                    [
                        ft.Container(width=210, content=ft.Text(keys, weight=ft.FontWeight.BOLD, size=12)),
                        ft.Text(desc, size=12, color=ft.Colors.WHITE70),
                    ],
                    alignment=ft.MainAxisAlignment.START,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                )
            )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Keyboard Shortcuts"),
            content=ft.Container(
                width=560,
                height=360,
                content=ft.ListView(rows, spacing=6),
            ),
            actions=[ft.TextButton("Close", on_click=lambda _e: page.pop_dialog())],
        )
        page.show_dialog(dlg)

    def on_keyboard(e: ft.KeyboardEvent) -> None:
        ev_type = str(getattr(e, "type", "") or "").strip().lower().replace("_", "")
        if ev_type and ev_type != "keydown":
            return

        action = resolve_shortcut_action(
            key=str(getattr(e, "key", "") or ""),
            ctrl=bool(getattr(e, "ctrl", False)),
            shift=bool(getattr(e, "shift", False)),
            alt=bool(getattr(e, "alt", False)),
            meta=bool(getattr(e, "meta", False)),
            typing_focus=bool(typing_shortcuts_blocked),
        )
        if not action:
            return

        if action == ACTION_UNDO:
            undo_click()
        elif action == ACTION_REDO:
            redo_click()
        elif action == ACTION_DELETE:
            delete_click(None)
        elif action == ACTION_SAVE:
            save_click(None)
        elif action == ACTION_SPLIT:
            split_click(None)
        elif action == ACTION_RAZOR:
            razor_multi_cut_click(None)
        elif action == ACTION_TRIM_IN:
            trim_set_in_click(None)
        elif action == ACTION_TRIM_OUT:
            trim_set_out_click(None)
        elif action == ACTION_EXPORT:
            export_click(None)
        elif action == ACTION_IMPORT:
            import_click(None)
        elif action == ACTION_DUPLICATE:
            duplicate_click(None)
        elif action == ACTION_ZOOM_IN:
            try:
                timeline_zoom.value = min(180, float(timeline_zoom.value) + 10)
                on_zoom(None)
            except Exception:
                pass
        elif action == ACTION_ZOOM_OUT:
            try:
                timeline_zoom.value = max(20, float(timeline_zoom.value) - 10)
                on_zoom(None)
            except Exception:
                pass
        elif action == ACTION_SELECT_PREV:
            _select_neighbor(-1)
        elif action == ACTION_SELECT_NEXT:
            _select_neighbor(1)
        elif action == ACTION_TOGGLE_PLAY_PAUSE:
            if state.is_playing:
                pause_click(None)
            elif _is_selected_audio():
                _play_audio(from_split=False)
            else:
                play_click(None)
        elif action == ACTION_SHOW_SHORTCUTS:
            _show_shortcuts_dialog()

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
        raw_path = str(path or "").strip()
        if not raw_path:
            return
        try:
            opened_path = str(Path(raw_path).resolve())
        except Exception:
            opened_path = raw_path
        if not Path(opened_path).exists():
            cfg.remove_recent_project(opened_path)
            _refresh_recent_menu()
            snack(f"Project not found: {Path(opened_path).name}")
            return
        try:
            state.project = load_project(opened_path)
            state.project_path = opened_path
            state.selected_clip_id = None
            state.selected_track = None

            history.clear()
            _refresh_history_controls()

            cfg.add_recent_project(opened_path)
            cfg.set_last_project_dir(opened_path)
            _mark_saved()
            _refresh_recent_menu()

            snack(f"Opened: {Path(opened_path).name}")
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
        missing_paths: List[str] = []
        for rp in recents:
            try:
                if not Path(rp.path).exists():
                    missing_paths.append(rp.path)
            except Exception:
                missing_paths.append(rp.path)
        if missing_paths:
            for p in missing_paths:
                cfg.remove_recent_project(p)
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
        # Non-visual audio player for selected audio-track preview.
        audio = fta.Audio(volume=1.0)
        page.overlay.append(audio)

    # ---------- Preview / Inspector ----------
    selected_title = ft.Text("No clip selected", weight=ft.FontWeight.BOLD)
    selected_range = ft.Text("")
    split_label = ft.Text("Split: -")
    split_slider = ft.Slider(min=0, max=1, value=0.5, divisions=200)

    trim_min_piece_sec = 0.08
    trim_in = ft.TextField(label="In", width=110, dense=True, hint_text="mm:ss")
    trim_out = ft.TextField(label="Out", width=110, dense=True, hint_text="mm:ss")
    trim_hint = ft.Text("", size=11, color=ft.Colors.WHITE70)
    trim_range = ft.RangeSlider(
        start_value=0.0,
        end_value=1.0,
        min=0.0,
        max=1.0,
        divisions=100,
        visible=False,
    )
    trim_range_hint = ft.Text("", size=11, color=ft.Colors.WHITE70)
    trim_slider_programmatic = False

    def _text_input_focus_on(_e: ft.ControlEvent) -> None:
        nonlocal typing_shortcuts_blocked
        typing_shortcuts_blocked = True

    def _text_input_focus_off(_e: ft.ControlEvent) -> None:
        nonlocal typing_shortcuts_blocked
        typing_shortcuts_blocked = False

    trim_in.on_focus = _text_input_focus_on
    trim_in.on_blur = _text_input_focus_off
    trim_out.on_focus = _text_input_focus_on
    trim_out.on_blur = _text_input_focus_off

    def _selected_source_duration(clip) -> Optional[float]:
        mi = next((m for m in state.media if m.path == clip.src), None)
        if mi:
            try:
                return float(mi.duration)
            except Exception:
                return None
        return None

    def _set_trim_slider_state(
        source_duration: Optional[float],
        in_sec: float,
        out_sec: float,
        *,
        note: str = "",
    ) -> None:
        nonlocal trim_slider_programmatic

        if source_duration is None:
            trim_range.visible = False
            trim_range_hint.value = "Range slider unavailable (source duration unknown)."
            return

        max_sec = float(source_duration)
        if max_sec <= trim_min_piece_sec + 1e-6:
            trim_range.visible = False
            trim_range_hint.value = "Range slider unavailable (source file is too short)."
            return

        start_sec = max(0.0, min(max_sec, float(in_sec)))
        end_sec = max(start_sec + trim_min_piece_sec, min(max_sec, float(out_sec)))
        if end_sec > max_sec:
            end_sec = max_sec
            start_sec = max(0.0, end_sec - trim_min_piece_sec)

        divisions = int(round(max_sec / 0.05))
        divisions = max(1, min(2000, divisions))

        trim_slider_programmatic = True
        try:
            trim_range.min = 0.0
            trim_range.max = max_sec
            trim_range.divisions = divisions
            trim_range.start_value = start_sec
            trim_range.end_value = end_sec
            trim_range.visible = True
        finally:
            trim_slider_programmatic = False

        default_note = (
            f"Range slider: {_fmt_time(start_sec)} -> {_fmt_time(end_sec)} "
            "(drag handles and release to apply)."
        )
        trim_range_hint.value = note or default_note

    def on_trim_range_change(_e: ft.ControlEvent) -> None:
        if trim_slider_programmatic:
            return
        try:
            start_sec = float(trim_range.start_value)
            end_sec = float(trim_range.end_value)
        except Exception:
            return

        trim_in.value = _fmt_time(start_sec)
        trim_out.value = _fmt_time(end_sec)
        if end_sec <= start_sec + trim_min_piece_sec:
            trim_range_hint.value = f"Invalid range: length must be > {_fmt_time(trim_min_piece_sec)}."
        else:
            trim_range_hint.value = (
                f"Preview: {_fmt_time(start_sec)} -> {_fmt_time(end_sec)} (release to apply)."
            )
        try:
            trim_in.update()
            trim_out.update()
            trim_range_hint.update()
        except Exception:
            pass

    def _trim_anchor_source_sec(clip) -> float:
        # Anchor trim helpers to the current split/playhead position inside the selected clip.
        rel = 0.0
        try:
            if state.split_pos_clip_id == clip.id:
                rel = float(state.split_pos_sec)
            else:
                rel = float(split_slider.value)
        except Exception:
            rel = 0.0
        rel = max(0.0, min(clip.dur, rel))
        return _timeline_rel_to_source_sec(clip, rel)

    def _apply_trim_values(new_in: float, new_out: float, action_label: str = "Trim") -> bool:
        if not state.selected_track or not state.selected_clip_id:
            snack("เลือกคลิปก่อน")
            return False
        clip = _selected_clip()
        if not clip:
            snack("ไม่พบคลิป")
            return False

        try:
            new_in = float(new_in)
            new_out = float(new_out)
        except Exception:
            snack(f"{action_label}: รูปแบบเวลาไม่ถูกต้อง")
            return False

        src_dur = _selected_source_duration(clip)
        if new_in < 0.0:
            snack(f"{action_label}: in ต้องไม่ติดลบ")
            return False
        if src_dur is not None and new_in > src_dur - trim_min_piece_sec:
            snack(f"{action_label}: in เกินช่วงไฟล์ ({_fmt_time(src_dur)})")
            return False
        if src_dur is not None and new_out > src_dur + 1e-6:
            snack(f"{action_label}: out เกินความยาวไฟล์ ({_fmt_time(src_dur)})")
            return False
        if new_out <= new_in + trim_min_piece_sec:
            snack(
                f"{action_label}: ความยาวคลิปต้องมากกว่า {_fmt_time(trim_min_piece_sec)} "
                f"(in={_fmt_time(new_in)} out={_fmt_time(new_out)})"
            )
            return False

        # Keep split/playhead anchored to the same source time after trim.
        anchor_source_sec = _trim_anchor_source_sec(clip)

        before = _track_clips(state.selected_track)
        clips, msg = trim_clip(
            before,
            clip.id,
            new_in,
            new_out,
            min_piece_sec=trim_min_piece_sec,
        )
        if clips != before:
            _history_record(f"Trim {clip.name}")
            _set_track_clips(state.selected_track, clips)
            _mark_dirty()
            speed = _clip_speed(clip)
            new_rel = max(0.0, min((new_out - new_in) / speed, (anchor_source_sec - new_in) / speed))
            state.split_pos_clip_id = clip.id
            state.split_pos_sec = new_rel

        snack(msg)
        update_inspector()
        refresh_timeline()
        return clips != before

    def trim_click(_e=None) -> None:
        new_in = _parse_time_input(trim_in.value)
        new_out = _parse_time_input(trim_out.value)
        if new_in is None or new_out is None:
            snack("Trim: รูปแบบเวลาไม่ถูกต้อง (ใส่วินาทีหรือ mm:ss)")
            return
        _apply_trim_values(new_in, new_out, action_label="Trim")

    def trim_range_commit(_e=None) -> None:
        if trim_slider_programmatic:
            return
        try:
            new_in = float(trim_range.start_value)
            new_out = float(trim_range.end_value)
        except Exception:
            return
        if new_out <= new_in + trim_min_piece_sec:
            snack(f"Trim slider: ความยาวคลิปต้องมากกว่า {_fmt_time(trim_min_piece_sec)}")
            update_inspector()
            return
        _apply_trim_values(new_in, new_out, action_label="Trim Slider")

    def trim_set_in_click(_e=None) -> None:
        clip = _selected_clip()
        if not clip:
            snack("เลือกคลิปก่อน")
            return
        anchor = _trim_anchor_source_sec(clip)
        new_in = min(anchor, clip.out_sec - trim_min_piece_sec)
        _apply_trim_values(new_in, clip.out_sec, action_label="Set In")

    def trim_set_out_click(_e=None) -> None:
        clip = _selected_clip()
        if not clip:
            snack("เลือกคลิปก่อน")
            return
        anchor = _trim_anchor_source_sec(clip)
        new_out = max(anchor, clip.in_sec + trim_min_piece_sec)
        src_dur = _selected_source_duration(clip)
        if src_dur is not None:
            new_out = min(new_out, src_dur)
        _apply_trim_values(clip.in_sec, new_out, action_label="Set Out")

    def trim_reset_click(_e=None) -> None:
        clip = _selected_clip()
        if not clip:
            snack("เลือกคลิปก่อน")
            return
        src_dur = _selected_source_duration(clip)
        if src_dur is None:
            snack("Reset Trim ใช้ได้เมื่อไฟล์อยู่ใน Media Bin")
            return
        _apply_trim_values(0.0, src_dur, action_label="Reset Trim")

    trim_apply = ft.FilledButton("Apply Trim", icon=ft.Icons.CUT, on_click=trim_click)
    trim_set_in = ft.OutlinedButton("Set In @ Split", icon=ft.Icons.FIRST_PAGE, on_click=trim_set_in_click)
    trim_set_out = ft.OutlinedButton("Set Out @ Split", icon=ft.Icons.LAST_PAGE, on_click=trim_set_out_click)
    trim_reset = ft.TextButton("Reset", icon=ft.Icons.RESTART_ALT, on_click=trim_reset_click)
    trim_in.on_submit = trim_click
    trim_out.on_submit = trim_click
    trim_range.on_change = on_trim_range_change
    trim_range.on_change_end = trim_range_commit
    trim_row = ft.Column(
        [
            ft.Row([trim_in, trim_out, trim_apply], spacing=6),
            trim_range,
            trim_range_hint,
            ft.Row([trim_set_in, trim_set_out, trim_reset], spacing=6),
            trim_hint,
        ],
        visible=False,
        spacing=4,
        tight=True,
    )

    # ---------- Transition controls (video tracks) ----------
    transition_kind = ft.Dropdown(
        width=170,
        dense=True,
        label="Transition In",
        value="none",
        options=[
            ft.dropdown.Option(key="none", text="None"),
            ft.dropdown.Option(key="fade", text="Fade"),
            ft.dropdown.Option(key="crossfade", text="Crossfade"),
            ft.dropdown.Option(key="dissolve", text="Dissolve"),
        ],
    )
    transition_dur_value = ft.Text("0.50s", size=12, color=ft.Colors.WHITE70)
    transition_dur = ft.Slider(min=0.05, max=2.0, value=0.5, divisions=39)
    transition_hint = ft.Text("", size=11, color=ft.Colors.WHITE70)

    def _selected_v_clip_index() -> int:
        if not _is_selected_video() or not state.selected_track or not state.selected_clip_id:
            return -1
        for i, c in enumerate(_track_clips(state.selected_track)):
            if c.id == state.selected_clip_id:
                return i
        return -1

    def _set_selected_clip_transition(kind: str, duration: float) -> bool:
        track_id = state.selected_track
        track = _track_obj(track_id)
        idx = _selected_v_clip_index()
        if idx < 0 or not track_id or track is None or track.kind != "video":
            snack("เลือกคลิปวิดีโอก่อน")
            return False
        if idx == 0:
            snack("คลิปแรกไม่สามารถมี Transition In ได้")
            return False
        clips_on_track = _track_clips(track_id)
        clip = clips_on_track[idx]
        prev = clips_on_track[idx - 1]

        k = str(kind or "none").strip().lower()
        if k in ("", "none", "off"):
            new_transition = None
        else:
            if k not in ("fade", "crossfade", "dissolve"):
                k = "fade"
            max_allowed = max(0.0, min(prev.dur, clip.dur) - 0.01)
            if max_allowed < 0.05:
                snack("คลิปสั้นเกินไปสำหรับ transition")
                return False
            d = min(max_allowed, max(0.05, float(duration)))
            new_transition = Transition(kind=k, duration=d)

        before = clips_on_track
        out = []
        changed = False
        for c in before:
            if c.id != clip.id:
                out.append(c)
                continue
            nc = replace(c, transition_in=new_transition)
            out.append(nc)
            changed = changed or (nc != c)

        if not changed:
            return False
        _history_record(f"Transition {track.name}:{clip.name}")
        _set_track_clips(track_id, out)
        _mark_dirty()
        update_inspector()
        refresh_timeline()
        return True

    def on_transition_dur_change(e: ft.ControlEvent) -> None:
        try:
            transition_dur_value.value = f"{float(e.control.value):.2f}s"
        except Exception:
            transition_dur_value.value = "-"
        transition_dur_value.update()

    def on_transition_kind_change(_e: ft.ControlEvent) -> None:
        k = str(transition_kind.value or "none").strip().lower()
        is_none = k in ("", "none", "off")
        transition_dur.disabled = is_none
        if is_none:
            transition_hint.value = "Transition is off. Choose a type then Apply."
        else:
            try:
                transition_hint.value = f"Selected {k} ({float(transition_dur.value):.2f}s). Click Apply."
            except Exception:
                transition_hint.value = f"Selected {k}. Click Apply."
        transition_panel.update()

    def transition_apply_click(_e=None) -> None:
        kind = str(transition_kind.value or "none")
        try:
            dur = float(transition_dur.value)
        except Exception:
            dur = 0.5
        changed = _set_selected_clip_transition(kind, dur)
        if changed:
            snack("Transition updated")

    transition_dur.on_change = on_transition_dur_change
    transition_kind.on_change = on_transition_kind_change
    transition_apply = ft.OutlinedButton(
        "Apply Transition",
        icon=ft.Icons.AUTO_FIX_HIGH,
        on_click=transition_apply_click,
    )
    transition_panel = ft.Column(
        [
            ft.Text("Transition", weight=ft.FontWeight.BOLD, size=12),
            ft.Row([transition_kind, ft.Container(expand=True), transition_dur_value], tight=True),
            transition_dur,
            ft.Row([transition_apply], tight=True),
            transition_hint,
        ],
        visible=False,
        spacing=4,
        tight=True,
    )

    # ---------- Clip speed + audio controls (export-time) ----------
    speed_title = ft.Text("Speed", weight=ft.FontWeight.BOLD, size=12)
    speed_value = ft.Text("1.00x", size=12, color=ft.Colors.WHITE70)
    speed_divisions = max(1, int(round((float(MAX_CLIP_SPEED) - float(MIN_CLIP_SPEED)) * 100)))
    speed_slider = ft.Slider(
        min=float(MIN_CLIP_SPEED),
        max=float(MAX_CLIP_SPEED),
        value=1.0,
        divisions=speed_divisions,
        round=2,
    )
    speed_apply = ft.OutlinedButton("Apply Speed")
    speed_reset = ft.TextButton("Reset 1.00x")
    speed_btn_05 = ft.TextButton("0.5x")
    speed_btn_125 = ft.TextButton("1.25x")
    speed_btn_15 = ft.TextButton("1.5x")
    speed_btn_20 = ft.TextButton("2x")
    speed_panel = ft.Column(
        [
            speed_title,
            ft.Row([ft.Text("Rate", size=12), ft.Container(expand=True), speed_value], tight=True),
            speed_slider,
            ft.Row(
                [
                    speed_apply,
                    speed_reset,
                    speed_btn_05,
                    speed_btn_125,
                    speed_btn_15,
                    speed_btn_20,
                ],
                wrap=True,
                spacing=4,
            ),
        ],
        visible=False,
        spacing=4,
        tight=True,
    )

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

    preview_hint = ft.Text("Preview (optional)", color=ft.Colors.WHITE70)
    preview_hint_layer = ft.Container(
        expand=True,
        alignment=ft.Alignment(0, 0),
        content=preview_hint,
        visible=True,
    )
    preview_video_slot = ft.Container(expand=True)

    # Keep the preview reasonably small so the timeline stays visible in typical browser heights.
    preview_host = ft.Container(
        height=200,
        border_radius=10,
        bgcolor=ft.Colors.BLACK,
        # Some Flet versions don't expose `ft.alignment.*`; Alignment(x, y) is stable.
        alignment=ft.Alignment(0, 0),
        content=ft.Stack([preview_video_slot, preview_hint_layer], expand=True),
    )

    audio_pos = ft.Text("", size=12, color=ft.Colors.WHITE70, visible=False)

    def _stop_audio_to_clip_start(_e=None) -> None:
        if not audio_preview_enabled or audio is None:
            return
        clip = _selected_clip()

        # flet_audio Audio methods are async; schedule them on the page task loop.
        target_ms = None
        if clip and _is_selected_audio():
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
        if not _is_selected_audio():
            snack("เลือกคลิปเสียงก่อน")
            return
        clip = _selected_clip()
        if not clip:
            snack("เลือกคลิปเสียงก่อน")
            return

        # Ensure backend knows the latest src before invoking methods.
        if audio.src != clip.src:
            audio.src = clip.src
            audio.update()

        offset = float(split_slider.value) if from_split else 0.0
        start = _timeline_rel_to_source_sec(clip, offset)
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
        if not _is_selected_audio():
            return
        clip = _selected_clip()
        if not clip:
            return
        pos_sec = float(e.position) / 1000.0
        rel_sec = _source_abs_to_timeline_rel(clip, pos_sec)
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
        if not clip or not track or _track_obj(track) is None:
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

    def _set_selected_clip_speed(speed: float, label: str = "Speed") -> None:
        clip = _selected_clip()
        track = state.selected_track
        if not clip or not track or _track_obj(track) is None:
            return

        target_speed = normalize_speed(speed, default=_clip_speed(clip))
        if abs(_clip_speed(clip) - target_speed) < 1e-6:
            return

        before = _track_clips(track)
        out = []
        changed = False
        for c in before:
            if c.id != clip.id:
                out.append(c)
                continue
            nc = replace(c, speed=target_speed)
            out.append(nc)
            changed = changed or (nc != c)

        if not changed:
            return
        if state.is_playing:
            stop_playback()
        _history_record(f"{label} {clip.name}")
        _set_track_clips(track, out)
        _mark_dirty()
        update_inspector()
        refresh_timeline()

    def on_speed_change(e: ft.ControlEvent) -> None:
        try:
            val = normalize_speed(float(e.control.value), default=1.0)
            speed_value.value = f"{val:.2f}x"
        except Exception:
            speed_value.value = "-"
        speed_value.update()

    def _apply_speed_value(raw_value: object, label: str = "Speed") -> None:
        clip = _selected_clip()
        if not clip:
            return
        try:
            val = normalize_speed(float(raw_value), default=_clip_speed(clip))
        except Exception:
            return
        speed_slider.value = val
        speed_value.value = f"{val:.2f}x"
        _set_selected_clip_speed(val, label=label)

    def on_speed_change_end(e: ft.ControlEvent) -> None:
        _apply_speed_value(e.control.value, label="Speed")

    def on_speed_apply_click(_e=None) -> None:
        _apply_speed_value(speed_slider.value, label="Speed")

    def _make_speed_preset_handler(rate: float):
        def _handler(_e=None) -> None:
            _apply_speed_value(rate, label="Speed")

        return _handler

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

    speed_slider.on_change = on_speed_change
    speed_slider.on_change_end = on_speed_change_end
    speed_apply.on_click = on_speed_apply_click
    speed_reset.on_click = _make_speed_preset_handler(1.0)
    speed_btn_05.on_click = _make_speed_preset_handler(0.5)
    speed_btn_125.on_click = _make_speed_preset_handler(1.25)
    speed_btn_15.on_click = _make_speed_preset_handler(1.5)
    speed_btn_20.on_click = _make_speed_preset_handler(2.0)

    volume_slider.on_change = on_volume_change
    volume_slider.on_change_end = on_volume_change_end
    mute_checkbox.on_change = on_mute_change

    def _preview_ready() -> bool:
        return bool(preview_video and preview_video_src and _is_selected_video())

    def play_click(_e=None):
        nonlocal playback_loop_id
        clip = _selected_clip()
        if not clip or not _preview_ready():
            return
        if state.is_playing:
            stop_playback()
        clip_start = v_start_sec_map.get(clip.id, 0.0)
        clip_end = clip_start + clip.dur
        if state.playhead_sec < clip_start or state.playhead_sec > clip_end:
            state.playhead_sec = clip_start
        state.is_playing = True
        state.playhead_clip_id = clip.id
        playback_loop_id += 1
        loop_id = playback_loop_id
        _run_sync_video_to_playhead(resume=True, expected_playback_id=loop_id)

        async def _runner() -> None:
            await _playhead_loop(loop_id)

        page.run_task(_runner)

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

    async def _sync_video_to_playhead(resume: bool = False, expected_playback_id: Optional[int] = None) -> None:
        nonlocal preview_video, playback_loop_id
        if expected_playback_id is not None and expected_playback_id != playback_loop_id:
            return
        if not _preview_ready() or not preview_video:
            return
        clip = _selected_clip()
        if not clip:
            return
        seek_sec = max(0.0, float(clip.in_sec))
        if state.playhead_clip_id == clip.id:
            clip_start = v_start_sec_map.get(clip.id, 0.0)
            rel = max(0.0, min(clip.dur, state.playhead_sec - clip_start))
            seek_sec = _timeline_rel_to_source_sec(clip, rel)
        try:
            await preview_video.pause()
        except Exception:
            pass
        if expected_playback_id is not None and expected_playback_id != playback_loop_id:
            return
        try:
            await preview_video.seek(int(seek_sec * 1000))
        except Exception:
            pass
        try:
            preview_video.playback_rate = _clip_speed(clip)
            preview_video.update()
        except Exception:
            pass
        if expected_playback_id is not None and expected_playback_id != playback_loop_id:
            return
        if resume:
            try:
                await preview_video.play()
            except Exception:
                pass

    def _run_sync_video_to_playhead(
        resume: bool = False,
        expected_playback_id: Optional[int] = None,
    ) -> None:
        async def _do() -> None:
            await _sync_video_to_playhead(resume=resume, expected_playback_id=expected_playback_id)

        page.run_task(_do)

    def stop_playback() -> None:
        nonlocal preview_video, playback_loop_id
        state.is_playing = False
        playback_loop_id += 1
        if preview_video and preview_video_src:
            pv = preview_video

            async def _pause_video() -> None:
                try:
                    await pv.pause()
                except Exception:
                    pass

            page.run_task(_pause_video)

    async def _playhead_loop(loop_id: int) -> None:
        nonlocal preview_video, playback_loop_id
        last_tick = time.perf_counter()
        stale_ticks = 0
        anchor_clip_id: Optional[str] = None
        anchor_base_rel = 0.0
        clock_origin_wall = last_tick
        clock_origin_rel = 0.0
        startup_mode = True
        startup_deadline = last_tick + 0.35
        while state.is_playing and loop_id == playback_loop_id and preview_video and preview_video_src:
            clip = _selected_clip()
            if not clip or not _is_selected_video():
                stop_playback()
                break
            clip_start = v_start_sec_map.get(clip.id, 0.0)
            prev_rel_sec = max(0.0, min(clip.dur, state.playhead_sec - clip_start))
            now = time.perf_counter()
            last_tick = now

            # Re-anchor the wall-clock model when playback clip changes.
            if anchor_clip_id != clip.id:
                anchor_clip_id = clip.id
                anchor_base_rel = prev_rel_sec
                clock_origin_wall = now
                clock_origin_rel = prev_rel_sec
                startup_mode = True
                startup_deadline = now + 0.35
                stale_ticks = 0

            expected_rel = max(
                0.0,
                min(clip.dur, clock_origin_rel + max(0.0, now - clock_origin_wall)),
            )

            try:
                pos_raw = await preview_video.get_current_position()
            except Exception:
                pos_raw = None

            if loop_id != playback_loop_id or not state.is_playing:
                break

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

            rel_backend: Optional[float] = None
            if pos_sec is not None:
                # Backends may report timeline-relative, source-relative, or absolute source time.
                candidates = [
                    max(0.0, min(clip.dur, pos_sec)),
                    _source_rel_to_timeline_rel(clip, pos_sec),
                    _source_abs_to_timeline_rel(clip, pos_sec),
                ]
                rel_backend = min(candidates, key=lambda v: abs(v - expected_rel))

            # Avoid racing ahead during startup while the media backend is still ramping up.
            if startup_mode:
                if rel_backend is not None and rel_backend > anchor_base_rel + 0.015:
                    startup_mode = False
                    clock_origin_wall = now
                    clock_origin_rel = rel_backend
                    expected_rel = rel_backend
                elif now >= startup_deadline:
                    startup_mode = False

            if rel_backend is None:
                # Keep UI moving by the wall-clock model when backend position is unavailable.
                rel_sec = expected_rel
                stale_ticks = 0
            else:
                # If backend time is stale/regressing, blend toward wall-clock progression.
                if rel_backend <= prev_rel_sec + 0.001:
                    stale_ticks += 1
                else:
                    stale_ticks = 0

                # Clamp backend jitter so playhead pace stays close to 1:1 wall-clock timing.
                max_trail_sec = 0.05
                max_lead_sec = 0.12
                lo = max(0.0, expected_rel - max_trail_sec)
                hi = min(clip.dur, expected_rel + max_lead_sec)
                rel_fused = min(hi, max(lo, rel_backend))

                if stale_ticks >= 2:
                    rel_sec = expected_rel
                else:
                    rel_sec = rel_fused

            # Playback should be monotonic while actively running.
            if (not startup_mode) and rel_sec < prev_rel_sec and prev_rel_sec < clip.dur - 0.02:
                rel_sec = prev_rel_sec

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
        (clip_id, global_sec_on_timeline_video, sec_from_clip_start).
        """
        timeline_clips = _timeline_video_clips()
        if not timeline_clips:
            return None, 0.0, 0.0

        x = float(x_px)
        first = timeline_clips[0]
        first_start_px = v_start_px_map.get(first.id, 0.0)
        if x <= first_start_px:
            return first.id, v_start_sec_map.get(first.id, 0.0), 0.0

        for c in timeline_clips:
            start_px = v_start_px_map.get(c.id, 0.0)
            width_px = max(1.0, float(v_clip_width_px_map.get(c.id, max(1.0, c.dur * state.px_per_sec))))
            end_px = start_px + width_px
            start_sec = v_start_sec_map.get(c.id, 0.0)
            if x <= end_px:
                ratio = max(0.0, min(1.0, (x - start_px) / width_px))
                rel_sec = c.dur * ratio
                return c.id, start_sec + rel_sec, rel_sec

        last = timeline_clips[-1]
        last_start_sec = v_start_sec_map.get(last.id, 0.0)
        return last.id, last_start_sec + last.dur, last.dur

    def _set_playhead_from_timeline_x(x_px: float, from_drag: bool = False) -> bool:
        clip_id, global_sec, rel_sec = _timeline_x_to_v1_position(x_px)
        if not clip_id:
            return False

        snapped_sec, _snap_kind = _snap_sec(global_sec)
        if abs(snapped_sec - global_sec) > 1e-9:
            clip_id2, global_sec2, rel_sec2 = _timeline_sec_to_position(snapped_sec)
            if clip_id2 is not None:
                clip_id = clip_id2
                global_sec = global_sec2
                rel_sec = rel_sec2

        if state.is_playing:
            stop_playback()

        state.playhead_clip_id = clip_id
        state.playhead_sec = global_sec
        state.split_pos_clip_id = clip_id
        state.split_pos_sec = rel_sec

        v_track_id = _timeline_video_track_id()
        selection_changed = state.selected_track != v_track_id or state.selected_clip_id != clip_id
        state.selected_track = v_track_id
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
        timeline_clips = _timeline_video_clips()
        for c in timeline_clips:
            start_sec = v_start_sec_map.get(c.id, 0.0)
            end_sec = start_sec + c.dur
            start_px = v_start_px_map.get(c.id, 0.0)
            width_px = max(1.0, float(v_clip_width_px_map.get(c.id, max(1.0, c.dur * state.px_per_sec))))
            if sec >= start_sec and sec <= end_sec:
                rel = 0.0 if c.dur <= 0 else (sec - start_sec) / c.dur
                px = start_px + max(0.0, min(1.0, rel)) * width_px
                break
        if px is None:
            if timeline_clips:
                first = timeline_clips[0]
                last = timeline_clips[-1]
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
            timeline_tracks_col.update()
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
        if y < timeline_header_h:
            return
        _set_playhead_from_timeline_x(x - timeline_v1_left_offset, from_drag=False)

    def on_timeline_pan_down(e: ft.DragDownEvent) -> None:
        x, y = _event_local_xy(e)
        if y < timeline_header_h:
            return
        _set_playhead_from_timeline_x(x - timeline_v1_left_offset, from_drag=False)

    def on_timeline_pan_start(e: ft.DragStartEvent) -> None:
        nonlocal timeline_pan_active, timeline_pan_start_pointer_x, timeline_pan_start_playhead_x
        _x, y = _event_local_xy(e)
        timeline_pan_active = y >= timeline_header_h
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
        timeline_pan_active = y >= timeline_header_h
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
        nonlocal preview_video, preview_video_src, trim_slider_programmatic

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
            trim_hint.value = ""
            trim_slider_programmatic = True
            try:
                trim_range.min = 0.0
                trim_range.max = 1.0
                trim_range.divisions = 100
                trim_range.start_value = 0.0
                trim_range.end_value = 1.0
                trim_range.visible = False
            finally:
                trim_slider_programmatic = False
            trim_range_hint.value = ""
            trim_row.visible = False
            transition_panel.visible = False
            transition_kind.value = "none"
            transition_kind.disabled = True
            transition_dur.min = 0.05
            transition_dur.max = 2.0
            transition_dur.value = 0.5
            transition_dur.divisions = 39
            transition_dur.disabled = True
            transition_dur_value.value = "0.50s"
            transition_apply.disabled = True
            transition_hint.value = ""
            speed_panel.visible = False
            speed_slider.value = 1.0
            speed_value.value = "1.00x"
            audio_edit_panel.visible = False
            audio_controls.visible = False
            audio_pos.visible = False
            _stop_audio_to_clip_start()
            stop_playback()
            preview_video_src = None
            if preview_video:
                preview_video.visible = False
            preview_hint.value = "Preview (optional)"
            preview_hint_layer.visible = True
            update_playhead_ui()
            page.update()
            return

        prefix = f"[{_track_name(state.selected_track)}]"
        clip_speed = _clip_speed(clip)
        source_span = max(0.0, float(clip.out_sec) - float(clip.in_sec))
        selected_title.value = f"{prefix} {clip.name}"
        selected_range.value = (
            f"in={_fmt_time(clip.in_sec)}  out={_fmt_time(clip.out_sec)}  "
            f"src={_fmt_time(source_span)}  dur={_fmt_time(clip.dur)}  speed={clip_speed:.2f}x"
        )
        trim_in.value = _fmt_time(clip.in_sec)
        trim_out.value = _fmt_time(clip.out_sec)
        src_dur = _selected_source_duration(clip)
        _set_trim_slider_state(src_dur, clip.in_sec, clip.out_sec)
        if src_dur is not None:
            trim_hint.value = (
                f"Source: {_fmt_time(src_dur)} | Clip: {_fmt_time(clip.dur)} @ {clip_speed:.2f}x "
                f"| Min trim length: {_fmt_time(trim_min_piece_sec)}"
            )
        else:
            trim_hint.value = (
                f"Clip: {_fmt_time(clip.dur)} @ {clip_speed:.2f}x | Min trim length: {_fmt_time(trim_min_piece_sec)} "
                "(source duration unknown)"
            )
        trim_row.visible = True
        speed_panel.visible = True
        speed_slider.value = clip_speed
        speed_value.value = f"{clip_speed:.2f}x"
        audio_edit_panel.visible = True

        if _is_selected_video():
            transition_panel.visible = True
            transition_apply.disabled = False
            transition_kind.disabled = False

            v_idx = _selected_v_clip_index()
            trans = getattr(clip, "transition_in", None)
            trans_kind = "none"
            trans_dur = 0.5
            if trans is not None:
                trans_kind = str(getattr(trans, "kind", "fade") or "fade").strip().lower()
                if trans_kind not in ("fade", "crossfade", "dissolve"):
                    trans_kind = "fade"
                try:
                    trans_dur = float(getattr(trans, "duration", 0.5) or 0.5)
                except Exception:
                    trans_dur = 0.5

            if v_idx <= 0:
                transition_kind.value = "none"
                transition_kind.disabled = True
                transition_dur.min = 0.05
                transition_dur.max = 2.0
                transition_dur.divisions = 39
                transition_dur.value = 0.5
                transition_dur.disabled = True
                transition_dur_value.value = "0.50s"
                transition_apply.disabled = True
                transition_hint.value = "First clip cannot have Transition In."
            else:
                selected_track_id = state.selected_track
                prev = _track_clips(selected_track_id)[v_idx - 1] if selected_track_id else None
                if prev is None:
                    transition_kind.value = "none"
                    transition_kind.disabled = True
                    transition_dur.disabled = True
                    transition_apply.disabled = True
                    transition_hint.value = "Transition unavailable"
                else:
                    max_allowed = max(0.0, min(prev.dur, clip.dur) - 0.01)
                    if max_allowed < 0.05:
                        transition_kind.value = "none"
                        transition_kind.disabled = True
                        transition_dur.min = 0.05
                        transition_dur.max = 0.05
                        transition_dur.divisions = 1
                        transition_dur.value = 0.05
                        transition_dur.disabled = True
                        transition_dur_value.value = "0.05s"
                        transition_apply.disabled = True
                        transition_hint.value = "Clips are too short for transition (need >= 0.05s overlap)."
                    else:
                        transition_kind.value = trans_kind
                        transition_dur.min = 0.05
                        transition_dur.max = max_allowed
                        steps = int(round((max_allowed - 0.05) / 0.01))
                        transition_dur.divisions = max(1, min(200, steps))
                        clamped_dur = max(0.05, min(max_allowed, trans_dur))
                        transition_dur.value = clamped_dur
                        transition_dur.disabled = trans_kind == "none"
                        transition_dur_value.value = f"{clamped_dur:.2f}s"
                        if trans_kind == "none":
                            transition_hint.value = f"Max overlap: {_fmt_time(max_allowed)}"
                        else:
                            transition_hint.value = (
                                f"{trans_kind} {_fmt_time(clamped_dur)} (max {_fmt_time(max_allowed)})"
                            )
        else:
            transition_panel.visible = False
            transition_kind.value = "none"
            transition_kind.disabled = True
            transition_dur.disabled = True
            transition_apply.disabled = True
            transition_hint.value = ""

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

        if _is_selected_video():
            audio_controls.visible = False
            audio_pos.visible = False
            _stop_audio_to_clip_start()
            preview_src = _prepare_web_preview_src(clip.src)
            if preview_src:
                if preview_video is None:
                    preview_video = ftv.Video(
                        expand=True,
                        playlist=[ftv.VideoMedia(preview_src)],
                        autoplay=False,
                        muted=True,
                        show_controls=True,
                        visible=True,
                    )
                    preview_video_slot.content = preview_video
                    preview_video_src = preview_src
                elif preview_video_src != preview_src:
                    stop_playback()
                    preview_video.playlist = [ftv.VideoMedia(preview_src)]
                    preview_video_src = preview_src
                preview_video.visible = True
                preview_hint_layer.visible = False
            else:
                stop_playback()
                preview_video_src = None
                if preview_video:
                    preview_video.visible = False
                preview_hint.value = "Preview load failed"
                preview_hint_layer.visible = True
        else:
            audio_controls.visible = audio_preview_enabled
            audio_pos.visible = audio_preview_enabled
            _stop_audio_to_clip_start()
            audio_pos.value = f"{_fmt_time(0.0)} / {_fmt_time(clip.dur)}"
            stop_playback()
            preview_video_src = None
            if preview_video:
                preview_video.visible = False
            if audio_preview_enabled:
                preview_hint.value = "Audio selected: use controls below to listen"
            else:
                preview_hint.value = "ปิดพรีวิวเสียงอยู่ (ตั้งค่า MINICUT_AUDIO_PREVIEW=1 เพื่อเปิดใช้งาน)"
            preview_hint_layer.visible = True
        update_playhead_ui()
        if _is_selected_video() and preview_video_src:
            _run_sync_video_to_playhead(resume=False)
        page.update()

    def on_split_slider(e: ft.ControlEvent) -> None:
        try:
            val = float(split_slider.value)
        except Exception:
            return
        state.split_pos_sec = val
        state.split_pos_clip_id = state.selected_clip_id
        track_id = state.selected_track
        if track_id == _timeline_video_track_id() and state.selected_clip_id:
            start = v_start_sec_map.get(state.selected_clip_id, 0.0)
            snap_sec, _snap_kind = _snap_sec(start + val)
            clip_id2, snapped_global_sec, rel2 = _timeline_sec_to_position(snap_sec)
            if clip_id2 is not None:
                state.selected_track = _timeline_video_track_id()
                state.selected_clip_id = clip_id2
                state.playhead_clip_id = clip_id2
                state.split_pos_clip_id = clip_id2
                state.split_pos_sec = rel2
                split_slider.value = rel2
                state.playhead_sec = snapped_global_sec
                val = rel2
        split_label.value = f"Split: {_fmt_time(val)}"
        split_label.update()
        # Move playhead to match split slider position.
        clip = _selected_clip()
        if clip and state.selected_clip_id:
            start = (
                v_start_sec_map.get(state.selected_clip_id, clip.in_sec)
                if state.selected_track == _timeline_video_track_id()
                else 0.0
            )
            state.playhead_clip_id = state.selected_clip_id
            state.playhead_sec = start + val
            update_playhead_ui()
            if state.selected_track == _timeline_video_track_id():
                _run_sync_video_to_playhead(resume=False)
        refresh_timeline()

    split_slider.on_change = on_split_slider

    # ---------- Timeline ----------
    timeline_zoom = ft.Slider(min=20, max=180, value=state.px_per_sec, divisions=160)
    timeline_info = ft.Text("Timeline: 0 clips", size=12, color=ft.Colors.WHITE70)
    timeline_ruler_row = ft.Row(spacing=0, scroll=ft.ScrollMode.AUTO)
    snap_threshold_label = ft.Text(f"{int(state.snap_threshold_px)}px", size=11, color=ft.Colors.WHITE70)
    snap_enable_sw = ft.Switch(label="Snap", value=state.snap_enabled)
    snap_edges_cb = ft.Checkbox(label="Edges", value=state.snap_to_edges)
    snap_grid_cb = ft.Checkbox(label="Grid", value=state.snap_to_grid)
    snap_grid_dd = ft.Dropdown(
        label="Step",
        width=94,
        dense=True,
        value=f"{state.snap_grid_sec:g}",
        options=[
            ft.dropdown.Option(key="0.25", text="0.25s"),
            ft.dropdown.Option(key="0.5", text="0.5s"),
            ft.dropdown.Option(key="1", text="1s"),
            ft.dropdown.Option(key="2", text="2s"),
        ],
    )
    snap_threshold_slider = ft.Slider(
        min=4,
        max=30,
        value=float(state.snap_threshold_px),
        divisions=26,
        width=120,
    )

    timeline_tracks_col = ft.Column(spacing=6, expand=True)

    def on_zoom(e: ft.ControlEvent) -> None:
        state.px_per_sec = float(timeline_zoom.value)
        refresh_timeline()

    timeline_zoom.on_change = on_zoom

    def _on_snap_toggle(_e: ft.ControlEvent) -> None:
        state.snap_enabled = bool(snap_enable_sw.value)
        refresh_timeline()

    def _on_snap_edges(_e: ft.ControlEvent) -> None:
        state.snap_to_edges = bool(snap_edges_cb.value)
        refresh_timeline()

    def _on_snap_grid(_e: ft.ControlEvent) -> None:
        state.snap_to_grid = bool(snap_grid_cb.value)
        refresh_timeline()

    def _on_snap_grid_step(_e: ft.ControlEvent) -> None:
        try:
            state.snap_grid_sec = max(0.05, float(snap_grid_dd.value))
        except Exception:
            state.snap_grid_sec = 0.5
        refresh_timeline()

    def _on_snap_threshold(_e: ft.ControlEvent) -> None:
        try:
            state.snap_threshold_px = float(snap_threshold_slider.value)
        except Exception:
            state.snap_threshold_px = 12.0
        snap_threshold_label.value = f"{int(round(state.snap_threshold_px))}px"
        try:
            snap_threshold_label.update()
        except Exception:
            pass
        refresh_timeline()

    snap_enable_sw.on_change = _on_snap_toggle
    snap_edges_cb.on_change = _on_snap_edges
    snap_grid_cb.on_change = _on_snap_grid
    snap_grid_dd.on_change = _on_snap_grid_step
    snap_threshold_slider.on_change = _on_snap_threshold

    def clip_block(track_id: str, clip_id: str) -> ft.Control:
        track = _track_obj(track_id)
        assert track is not None
        clip = _find_clip(track_id, clip_id)
        assert clip is not None

        is_audio = track.kind == "audio"
        dur_px = max(70, int(clip.dur * state.px_per_sec))
        color = ft.Colors.GREEN_600 if is_audio else ft.Colors.BLUE_600
        selected = state.selected_track == track_id and state.selected_clip_id == clip.id
        label = f"A: {clip.name}" if is_audio else clip.name

        def _select_at(position_px: float | None = None) -> None:
            state.selected_track = track_id
            state.selected_clip_id = clip.id
            state.playhead_clip_id = clip.id
            # If user clicked within the clip, set split position to that proportion.
            if position_px is not None and dur_px > 0 and clip.dur > 0:
                ratio = max(0.0, min(1.0, position_px / dur_px))
                state.split_pos_clip_id = clip.id
                state.split_pos_sec = max(0.0, min(clip.dur, clip.dur * ratio))
                if track_id == _timeline_video_track_id():
                    raw_global_sec = v_start_sec_map.get(clip.id, 0.0) + state.split_pos_sec
                    snapped_sec, _snap_kind = _snap_sec(raw_global_sec)
                    clip_id2, snapped_global_sec, rel2 = _timeline_sec_to_position(snapped_sec)
                    if clip_id2 is not None:
                        state.selected_track = _timeline_video_track_id()
                        state.selected_clip_id = clip_id2
                        state.playhead_clip_id = clip_id2
                        state.split_pos_clip_id = clip_id2
                        state.split_pos_sec = rel2
                        state.playhead_sec = snapped_global_sec
                update_playhead_ui()
                _run_sync_video_to_playhead(resume=False)
            update_inspector()
            refresh_timeline()

        block_height = 28 if is_audio else 36
        visual_src = _timeline_clip_visual_src(track_id, clip)
        if visual_src:
            overlay_tint = ft.Colors.AMBER_900 if selected else ft.Colors.BLACK54
            label_bg = ft.Colors.AMBER_800 if selected else ft.Colors.BLACK54
            cont = ft.Container(
                width=dur_px,
                height=block_height,
                border_radius=8,
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                border=ft.Border.all(1, ft.Colors.AMBER_300 if selected else ft.Colors.WHITE24),
                content=ft.Stack(
                    controls=[
                        ft.Image(src=visual_src, width=dur_px, height=block_height, fit=ft.ImageFit.COVER),
                        ft.Container(width=dur_px, height=block_height, bgcolor=overlay_tint, opacity=0.30),
                        ft.Container(
                            width=dur_px,
                            height=block_height,
                            padding=6,
                            alignment=ft.Alignment(-1, 0),
                            bgcolor=label_bg,
                            opacity=0.85,
                            content=ft.Text(label, size=12, no_wrap=True),
                        ),
                    ],
                    width=dur_px,
                    height=block_height,
                ),
            )
        else:
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
            data={"kind": "clip", "track": track_id, "id": clip.id},
            axis=ft.Axis.HORIZONTAL,
            on_drag_start=lambda _e: _select_at(),
            content=clip_surface,
            content_feedback=ft.Container(width=80, height=22, bgcolor=ft.Colors.WHITE24, border_radius=8),
        )
        return draggable

    def refresh_timeline() -> None:
        timeline_tracks_col.controls.clear()
        timeline_ruler_row.controls.clear()
        v_start_sec_map.clear()
        v_start_px_map.clear()
        v_clip_width_px_map.clear()

        snap_edges_cb.disabled = not bool(state.snap_enabled)
        snap_grid_cb.disabled = not bool(state.snap_enabled)
        snap_grid_dd.disabled = (not bool(state.snap_enabled)) or (not bool(state.snap_to_grid))
        snap_threshold_slider.disabled = not bool(state.snap_enabled)

        def _insert_existing_clip(before, target_id: str, moving_clip):
            out = []
            inserted = False
            for c in before:
                if c.id == target_id and not inserted:
                    out.append(moving_clip)
                    inserted = True
                out.append(c)
            if not inserted:
                out.append(moving_clip)
            return out

        def handle_drop(track_id: str, target_clip_id: Optional[str], payload: dict) -> None:
            track = _track_obj(track_id)
            if track is None:
                return
            kind = payload.get("kind")
            if kind == "media":
                path = payload.get("path")
                mi = next((m for m in state.media if m.path == path), None)
                if not mi:
                    snack("ไม่พบ media")
                    return
                if track.kind == "video" and not mi.has_video:
                    snack(f"No video stream (drop on {state.project.primary_audio_track().name} instead)")
                    return
                if track.kind == "audio" and not mi.has_audio:
                    snack("No audio stream")
                    return

                before = _track_clips(track_id)
                if target_clip_id:
                    clips = insert_clip_before(before, target_clip_id, path, mi.duration, has_audio=mi.has_audio)
                else:
                    clips = add_clip_end(before, path, mi.duration, has_audio=mi.has_audio)

                if clips != before:
                    _history_record(f"Add {Path(path).name} to {track.name}")
                    _set_track_clips(track_id, clips)
                    _mark_dirty()
            elif kind == "clip":
                moving_id = payload.get("id")
                moving_track = payload.get("track")
                if not moving_id:
                    return
                src_track = _track_obj(str(moving_track or ""))
                if src_track is None:
                    return
                if src_track.kind != track.kind:
                    snack("Cannot move clip between video and audio tracks")
                    return

                src_before = _track_clips(src_track.id)
                moving_clip = next((c for c in src_before if c.id == moving_id), None)
                if moving_clip is None:
                    return

                if src_track.id == track_id:
                    if target_clip_id:
                        clips = move_clip_before(src_before, moving_id, target_clip_id)
                    else:
                        moving = None
                        rest = []
                        for c in src_before:
                            if c.id == moving_id:
                                moving = c
                            else:
                                rest.append(c)
                        if moving is None:
                            return
                        clips = [*rest, moving]

                    if clips != src_before:
                        m = _find_clip(track_id, moving_id)
                        name = m.name if m else moving_id
                        _history_record(f"Move {name}")
                        _set_track_clips(track_id, clips)
                        _mark_dirty()
                else:
                    dst_before = _track_clips(track_id)
                    src_after = [c for c in src_before if c.id != moving_id]
                    dst_after = (
                        _insert_existing_clip(dst_before, target_clip_id, moving_clip)
                        if target_clip_id
                        else [*dst_before, moving_clip]
                    )
                    if src_after != src_before or dst_after != dst_before:
                        _history_record(f"Move {moving_clip.name} {src_track.name}->{track.name}")
                        _set_track_clips(src_track.id, src_after)
                        _set_track_clips(track_id, dst_after)
                        state.selected_track = track_id
                        state.selected_clip_id = moving_clip.id
                        _mark_dirty()
            refresh_timeline()

        def _payload(e: ft.DragTargetEvent):
            return getattr(getattr(e, "src", None), "data", None)

        def _end_drop(track_id: str, height: int) -> ft.DragTarget:
            def on_drop_end(e: ft.DragTargetEvent) -> None:
                payload = _payload(e)
                if isinstance(payload, dict):
                    handle_drop(track_id, None, payload)

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

        primary_video_track_id = _timeline_video_track_id()
        v_time = 0.0
        v_px = 0.0
        total_clip_count = 0

        for track in state.project.tracks:
            row = ft.Row(spacing=6, scroll=ft.ScrollMode.AUTO)
            total_clip_count += len(track.clips)

            for c in track.clips:
                def _make_on_accept(current_track_id: str, target_id: str):
                    def _on_accept(e: ft.DragTargetEvent) -> None:
                        payload = _payload(e)
                        if isinstance(payload, dict):
                            handle_drop(current_track_id, target_id, payload)
                    return _on_accept

                width = max(70, int(c.dur * state.px_per_sec))
                block = clip_block(track.id, c.id)
                drop_zone = ft.DragTarget(
                    group="tl",
                    on_accept=_make_on_accept(track.id, c.id),
                    content=ft.Container(
                        width=16,
                        height=40 if track.kind == "video" else 34,
                        bgcolor=ft.Colors.TRANSPARENT,
                        border=ft.Border(left=ft.BorderSide(1, ft.Colors.WHITE12)),
                    ),
                )
                row.controls.append(ft.Row(spacing=0, controls=[drop_zone, ft.Container(width=width, content=block)]))

                if track.id == primary_video_track_id:
                    v_start_sec_map[c.id] = v_time
                    v_start_px_map[c.id] = v_px + 16  # clip starts after drop zone
                    v_clip_width_px_map[c.id] = width
                    v_time += c.dur
                    v_px += 16 + width

            row.controls.append(_end_drop(track.id, height=36 if track.kind == "video" else 28))

            badge = "V" if track.kind == "video" else "A"
            label = ft.Container(
                width=timeline_lane_label_w,
                padding=ft.padding.only(top=4, right=4),
                content=ft.Column(
                    [
                        ft.Text(
                            track.name,
                            size=11,
                            no_wrap=True,
                            weight=ft.FontWeight.BOLD if state.selected_track == track.id else ft.FontWeight.W_500,
                        ),
                        ft.Text(
                            f"{badge}{' M' if track.muted else ''}{' H' if not track.visible else ''}",
                            size=10,
                            color=ft.Colors.WHITE70,
                        ),
                    ],
                    spacing=1,
                    tight=True,
                ),
            )
            timeline_tracks_col.controls.append(
                ft.Row(
                    [
                        label,
                        ft.Container(width=timeline_lane_gap_w),
                        row,
                    ],
                    expand=True,
                    spacing=0,
                )
            )

        timeline_video_track = _timeline_video_track()
        v_total = _fmt_time(total_duration(timeline_video_track.clips))
        a_total = _fmt_time(total_duration(state.project.primary_audio_track().clips))
        timeline_info.value = (
            f"Tracks V:{len(state.project.video_tracks)} A:{len(state.project.audio_tracks)} "
            f"| Clips:{total_clip_count} | {timeline_video_track.name}:{v_total} "
            f"| {state.project.primary_audio_track().name}:{a_total}"
        )

        total_sec = _timeline_video_total_sec()
        step = max(0.1, float(state.snap_grid_sec or 0.5))
        if total_sec <= 0.0:
            timeline_ruler_row.controls.append(
                ft.Container(
                    width=220,
                    height=18,
                    border=ft.Border(left=ft.BorderSide(1, ft.Colors.WHITE24)),
                    content=ft.Text("00:00.00", size=10, color=ft.Colors.WHITE38),
                )
            )
        else:
            t = 0.0
            max_marks = 500
            marks = 0
            while t <= total_sec + 1e-9 and marks < max_marks:
                w = max(8, int(step * state.px_per_sec))
                timeline_ruler_row.controls.append(
                    ft.Container(
                        width=w,
                        height=18,
                        border=ft.Border(left=ft.BorderSide(1, ft.Colors.WHITE24)),
                        alignment=ft.Alignment(-1, 0),
                        padding=ft.padding.only(left=2),
                        content=ft.Text(_fmt_time(t), size=9, color=ft.Colors.WHITE38),
                    )
                )
                t += step
                marks += 1
            # Keep ruler aligned to at least end duration width.
            remaining = max(0, int(total_sec * state.px_per_sec) - int(marks * step * state.px_per_sec))
            if remaining > 0:
                timeline_ruler_row.controls.append(
                    ft.Container(
                        width=remaining,
                        height=18,
                        border=ft.Border(left=ft.BorderSide(1, ft.Colors.WHITE24)),
                    )
                )

        nonlocal timeline_total_sec
        timeline_total_sec = _timeline_video_total_sec()
        update_playhead_ui()
        page.update()

    def add_video_track_click(_e=None) -> None:
        _history_record("Add video track")
        t = state.project.add_track("video")
        state.selected_track = t.id
        state.selected_clip_id = None
        _mark_dirty()
        update_inspector()
        refresh_timeline()

    def add_audio_track_click(_e=None) -> None:
        _history_record("Add audio track")
        t = state.project.add_track("audio")
        state.selected_track = t.id
        state.selected_clip_id = None
        _mark_dirty()
        update_inspector()
        refresh_timeline()

    def remove_selected_track_click(_e=None) -> None:
        track = _track_obj(state.selected_track)
        if track is None:
            snack("Select a track first")
            return
        if track.clips:
            snack("Delete or move clips out first before removing this track")
            return
        _history_record(f"Remove track {track.name}")
        ok = state.project.remove_track(track.id)
        if not ok:
            snack("Cannot remove the last track of this type")
            return
        state.selected_track = None
        state.selected_clip_id = None
        _mark_dirty()
        update_inspector()
        refresh_timeline()

    def toggle_selected_track_mute_click(_e=None) -> None:
        track = _track_obj(state.selected_track)
        if track is None:
            snack("Select a track first")
            return
        _history_record(f"{'Unmute' if track.muted else 'Mute'} track {track.name}")
        track.muted = not bool(track.muted)
        _mark_dirty()
        refresh_timeline()

    def toggle_selected_track_visible_click(_e=None) -> None:
        track = _track_obj(state.selected_track)
        if track is None:
            snack("Select a track first")
            return
        _history_record(f"{'Show' if not track.visible else 'Hide'} track {track.name}")
        track.visible = not bool(track.visible)
        _mark_dirty()
        refresh_timeline()

    # ---------- Actions ----------
    def import_click(_e):
        async def _pick() -> None:
            picked = await file_picker.pick_files(
                allow_multiple=True,
                initial_directory=_initial_project_dir(),
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

    def razor_multi_cut_click(_e=None):
        tracks = list(state.project.tracks)
        if not tracks:
            snack("Timeline is empty")
            return

        playhead_sec = max(0.0, float(state.playhead_sec))
        changes: List[tuple[str, List, Optional[str], Optional[str]]] = []
        for t in tracks:
            before = _track_clips(t.id)
            if not before:
                continue
            after, left_id, _msg = split_clip_at_timeline_sec(before, playhead_sec)
            if after == before:
                continue

            right_id: Optional[str] = None
            if left_id:
                try:
                    left_idx = next(i for i, c in enumerate(after) if c.id == left_id)
                except StopIteration:
                    left_idx = -1
                if 0 <= left_idx < len(after) - 1:
                    right_id = after[left_idx + 1].id
            changes.append((t.id, after, left_id, right_id))

        if not changes:
            snack("Razor: no clip crosses the playhead")
            return

        _history_record(f"Razor cut {len(changes)} track(s)")
        for track_id, after, _left_id, _right_id in changes:
            _set_track_clips(track_id, after)

        sel_track_id = state.selected_track
        sel_change = next((x for x in changes if x[0] == sel_track_id), None)
        if sel_change and sel_change[3]:
            new_id = sel_change[3]
            state.selected_clip_id = new_id
            state.playhead_clip_id = new_id
            state.split_pos_clip_id = new_id
            state.split_pos_sec = 0.0
        elif not state.selected_track:
            base_track_id = _timeline_video_track_id()
            base_change = next((x for x in changes if x[0] == base_track_id), None)
            if base_change and base_change[3]:
                new_id = base_change[3]
                state.selected_track = base_track_id
                state.selected_clip_id = new_id
                state.playhead_clip_id = new_id
                state.split_pos_clip_id = new_id
                state.split_pos_sec = 0.0

        _mark_dirty()
        snack(f"Razor cut {len(changes)} track(s)")
        update_inspector()
        refresh_timeline()

    def split_click(_e):
        if not state.selected_track or not state.selected_clip_id:
            snack("เลือกคลิปก่อน")
            return
        before = _track_clips(state.selected_track)
        selected = _find_clip(state.selected_track, state.selected_clip_id)
        split_at = float(split_slider.value)
        split_global_sec = None
        if selected and state.selected_track == _timeline_video_track_id():
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
            cfg.set_last_project_dir(path)
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
                initial_directory=_initial_project_dir(),
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["json"],
            )
            if not out_path:
                return
            try:
                save_project(state.project, out_path)
                state.project_path = out_path
                cfg.add_recent_project(out_path)
                cfg.set_last_project_dir(out_path)
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
                initial_directory=_initial_project_dir(),
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["json"],
            )
            if not picked or not picked[0].path:
                return
            _open_project(picked[0].path)

        page.run_task(_pick)
    def _open_export_settings_dialog(on_confirm) -> None:
        presets = {
            "social": ExportSettings(
                width=1080,
                height=1920,
                video_codec="libx264",
                crf=20,
                audio_codec="aac",
                audio_bitrate="192k",
                format="mp4",
                preset="medium",
            ),
            "yt1080": ExportSettings(
                width=1920,
                height=1080,
                video_codec="libx264",
                crf=20,
                audio_codec="aac",
                audio_bitrate="192k",
                format="mp4",
                preset="medium",
            ),
            "yt720": ExportSettings(
                width=1280,
                height=720,
                video_codec="libx264",
                crf=23,
                audio_codec="aac",
                audio_bitrate="160k",
                format="mp4",
                preset="medium",
            ),
            "draft": ExportSettings(
                width=854,
                height=480,
                video_codec="libx264",
                crf=28,
                audio_codec="aac",
                audio_bitrate="128k",
                format="mp4",
                preset="veryfast",
            ),
        }
        working = ExportSettings.from_dict(state.export_settings.to_dict())

        def _settings_fingerprint(s: ExportSettings) -> tuple:
            return (
                int(s.width or 0),
                int(s.height or 0),
                str(s.video_codec or "libx264").strip().lower(),
                int(s.crf if s.crf is not None else 23),
                str(s.audio_codec or "aac").strip().lower(),
                str(s.audio_bitrate or "192k").strip().lower(),
                str(s.format or "mp4").strip().lower(),
                str(s.preset or "medium").strip().lower(),
            )

        def _detect_preset_key(s: ExportSettings) -> str:
            fp = _settings_fingerprint(s)
            for key, val in presets.items():
                if _settings_fingerprint(val) == fp:
                    return key
            return "custom"

        preset_sync_lock = False

        preset_dd = ft.Dropdown(
            label="Preset",
            width=200,
            dense=True,
            value=_detect_preset_key(working),
            options=[
                ft.dropdown.Option(key="custom", text="Custom"),
                ft.dropdown.Option(key="social", text="Social (1080x1920)"),
                ft.dropdown.Option(key="yt1080", text="YouTube 1080p"),
                ft.dropdown.Option(key="yt720", text="YouTube 720p"),
                ft.dropdown.Option(key="draft", text="Draft (fast)"),
            ],
        )
        format_dd = ft.Dropdown(
            label="Format",
            width=130,
            dense=True,
            value=str(working.format or "mp4").lower(),
            options=[
                ft.dropdown.Option(key="mp4", text="MP4"),
                ft.dropdown.Option(key="mov", text="MOV"),
                ft.dropdown.Option(key="webm", text="WEBM"),
            ],
        )
        width_tf = ft.TextField(
            label="Width",
            width=100,
            dense=True,
            value=str(int(working.width or 0)),
            hint_text="0=keep",
        )
        height_tf = ft.TextField(
            label="Height",
            width=100,
            dense=True,
            value=str(int(working.height or 0)),
            hint_text="0=keep",
        )
        video_codec_dd = ft.Dropdown(
            label="Video codec",
            width=170,
            dense=True,
            value=str(working.video_codec or "libx264").lower(),
            options=[
                ft.dropdown.Option(key="libx264", text="H.264 (libx264)"),
                ft.dropdown.Option(key="libx265", text="HEVC (libx265)"),
                ft.dropdown.Option(key="libvpx-vp9", text="VP9 (WEBM)"),
            ],
        )
        audio_codec_dd = ft.Dropdown(
            label="Audio codec",
            width=140,
            dense=True,
            value=str(working.audio_codec or "aac").lower(),
            options=[
                ft.dropdown.Option(key="aac", text="AAC"),
                ft.dropdown.Option(key="libopus", text="Opus"),
            ],
        )
        bitrate_tf = ft.TextField(
            label="Audio bitrate",
            width=120,
            dense=True,
            value=str(working.audio_bitrate or "192k").lower(),
            hint_text="e.g. 192k",
        )
        crf_value = ft.Text("", size=12, color=ft.Colors.WHITE70)
        crf_slider = ft.Slider(
            min=0,
            max=51,
            divisions=51,
            value=float(max(0, min(51, int(working.crf if working.crf is not None else 23)))),
        )
        encode_preset_dd = ft.Dropdown(
            label="Encode preset",
            width=150,
            dense=True,
            value=str(working.preset or "medium").lower(),
            options=[
                ft.dropdown.Option(key="ultrafast", text="ultrafast"),
                ft.dropdown.Option(key="superfast", text="superfast"),
                ft.dropdown.Option(key="veryfast", text="veryfast"),
                ft.dropdown.Option(key="faster", text="faster"),
                ft.dropdown.Option(key="fast", text="fast"),
                ft.dropdown.Option(key="medium", text="medium"),
                ft.dropdown.Option(key="slow", text="slow"),
                ft.dropdown.Option(key="slower", text="slower"),
                ft.dropdown.Option(key="veryslow", text="veryslow"),
            ],
        )
        settings_hint = ft.Text("0x0 keeps original resolution", size=11, color=ft.Colors.WHITE70)
        settings_preview = ft.Text("", size=11, color=ft.Colors.WHITE70)

        def _set_preset_custom() -> None:
            nonlocal preset_sync_lock
            if preset_sync_lock:
                return
            preset_sync_lock = True
            try:
                preset_dd.value = "custom"
            finally:
                preset_sync_lock = False

        def _update_crf_text() -> None:
            try:
                crf_value.value = f"CRF {int(round(float(crf_slider.value)))} (lower = better quality)"
            except Exception:
                crf_value.value = "CRF -"
            crf_value.update()

        def _update_settings_preview() -> None:
            fmt = str(format_dd.value or "mp4").strip().lower()
            try:
                w = int(str(width_tf.value or "0").strip() or "0")
                h = int(str(height_tf.value or "0").strip() or "0")
            except Exception:
                w = 0
                h = 0
            if w > 0 and h > 0:
                res = f"{w}x{h}"
            else:
                res = "source"
            vcodec = str(video_codec_dd.value or "libx264").strip().lower()
            acodec = str(audio_codec_dd.value or "aac").strip().lower()
            ab = str(bitrate_tf.value or "192k").strip().lower() or "192k"
            try:
                crf_now = int(round(float(crf_slider.value)))
            except Exception:
                crf_now = 23
            ep = str(encode_preset_dd.value or "medium").strip().lower()
            settings_preview.value = (
                f"Output: {fmt.upper()} | Res: {res} | V: {vcodec} CRF {crf_now} {ep} | A: {acodec} {ab}"
            )
            try:
                settings_preview.update()
            except Exception:
                pass

        def _sync_codec_controls() -> None:
            fmt = str(format_dd.value or "mp4").strip().lower()
            if fmt == "webm":
                video_codec_dd.value = "libvpx-vp9"
                audio_codec_dd.value = "libopus"
                video_codec_dd.disabled = True
                audio_codec_dd.disabled = True
                if str(bitrate_tf.value or "").strip() == "":
                    bitrate_tf.value = "160k"
                settings_hint.value = "WEBM uses VP9 + Opus automatically"
            else:
                if str(video_codec_dd.value or "") == "libvpx-vp9":
                    video_codec_dd.value = "libx264"
                if str(audio_codec_dd.value or "") == "libopus":
                    audio_codec_dd.value = "aac"
                video_codec_dd.disabled = False
                audio_codec_dd.disabled = False
                settings_hint.value = "0x0 keeps original resolution"
            _update_settings_preview()
            try:
                dialog.update()
            except Exception:
                pass

        def _apply_settings_to_controls(s: ExportSettings) -> None:
            nonlocal preset_sync_lock
            preset_sync_lock = True
            try:
                width_tf.value = str(int(s.width or 0))
                height_tf.value = str(int(s.height or 0))
                format_dd.value = str(s.format or "mp4").lower()
                video_codec_dd.value = str(s.video_codec or "libx264").lower()
                audio_codec_dd.value = str(s.audio_codec or "aac").lower()
                bitrate_tf.value = str(s.audio_bitrate or "192k").lower()
                crf_slider.value = float(max(0, min(51, int(s.crf if s.crf is not None else 23))))
                encode_preset_dd.value = str(s.preset or "medium").lower()
                preset_dd.value = _detect_preset_key(s)
                _update_crf_text()
                _update_settings_preview()
                _sync_codec_controls()
            finally:
                preset_sync_lock = False
            try:
                dialog.update()
            except Exception:
                pass

        def _parse_non_negative_int(raw: str, field: str) -> Optional[int]:
            txt = str(raw or "").strip()
            if txt == "":
                return 0
            try:
                val = int(txt)
            except Exception:
                snack(f"{field} must be an integer")
                return None
            if val < 0:
                snack(f"{field} must be >= 0")
                return None
            return val

        def _collect_export_settings() -> Optional[ExportSettings]:
            width = _parse_non_negative_int(width_tf.value, "Width")
            height = _parse_non_negative_int(height_tf.value, "Height")
            if width is None or height is None:
                return None
            if (width == 0) != (height == 0):
                snack("Width and Height must both be 0, or both > 0")
                return None
            if width > 0 and (width < 16 or height < 16):
                snack("Width/Height must be >= 16 when scaling is enabled")
                return None
            if width > 0 and ((width % 2) != 0 or (height % 2) != 0):
                snack("Width/Height must be even numbers (e.g. 1920x1080)")
                return None

            bitrate = str(bitrate_tf.value or "").strip().lower()
            if not bitrate:
                bitrate = "192k"
            if len(bitrate) < 2 or bitrate[-1] not in ("k", "m") or (not bitrate[:-1].isdigit()):
                snack("Audio bitrate must look like 192k or 1m")
                return None

            try:
                crf = int(round(float(crf_slider.value)))
            except Exception:
                crf = 23

            return ExportSettings(
                width=width,
                height=height,
                video_codec=str(video_codec_dd.value or "libx264").strip().lower(),
                crf=max(0, min(51, crf)),
                audio_codec=str(audio_codec_dd.value or "aac").strip().lower(),
                audio_bitrate=bitrate,
                format=str(format_dd.value or "mp4").strip().lower(),
                preset=str(encode_preset_dd.value or "medium").strip().lower(),
            )

        def _on_preset_change(_e: ft.ControlEvent) -> None:
            if preset_sync_lock:
                return
            key = str(preset_dd.value or "custom")
            if key == "custom":
                return
            p = presets.get(key)
            if p:
                _apply_settings_to_controls(p)

        def _on_format_change(_e: ft.ControlEvent) -> None:
            _set_preset_custom()
            _sync_codec_controls()

        def _on_crf_change(_e: ft.ControlEvent) -> None:
            _set_preset_custom()
            _update_crf_text()
            _update_settings_preview()

        def _on_manual_control_change(_e: ft.ControlEvent) -> None:
            _set_preset_custom()
            _update_settings_preview()

        def _on_apply(_e: ft.ControlEvent) -> None:
            settings = _collect_export_settings()
            if settings is None:
                return
            state.export_settings = settings
            try:
                page.pop_dialog()
            except Exception:
                pass
            on_confirm(settings)

        def _on_cancel(_e: ft.ControlEvent) -> None:
            try:
                page.pop_dialog()
            except Exception:
                pass

        preset_dd.on_change = _on_preset_change
        format_dd.on_change = _on_format_change
        crf_slider.on_change = _on_crf_change
        width_tf.on_change = _on_manual_control_change
        height_tf.on_change = _on_manual_control_change
        video_codec_dd.on_change = _on_manual_control_change
        audio_codec_dd.on_change = _on_manual_control_change
        bitrate_tf.on_change = _on_manual_control_change
        encode_preset_dd.on_change = _on_manual_control_change

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Export Settings"),
            content=ft.Column(
                [
                    ft.Row([preset_dd, ft.Container(expand=True), format_dd], wrap=True),
                    ft.Row([width_tf, height_tf], spacing=8),
                    ft.Row([video_codec_dd, audio_codec_dd, bitrate_tf], spacing=8, wrap=True),
                    ft.Row([encode_preset_dd, ft.Container(expand=True), crf_value], wrap=True),
                    crf_slider,
                    settings_hint,
                    settings_preview,
                ],
                spacing=8,
                tight=True,
                width=560,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=_on_cancel),
                ft.FilledButton("Continue Export", icon=ft.Icons.OUTPUT, on_click=_on_apply),
            ],
        )
        page.show_dialog(dialog)
        _update_crf_text()
        _sync_codec_controls()
        _update_settings_preview()

    def export_click(_e):
        nonlocal export_in_progress
        if export_in_progress:
            snack("Export is already running")
            return
        if not any(t.clips for t in state.project.video_tracks):
            snack("Timeline is empty")
            return

        def _run_export_with_settings(settings: ExportSettings) -> None:
            async def _save_and_export() -> None:
                nonlocal export_in_progress
                fmt = str(settings.format or "mp4").strip().lower()
                if fmt not in ("mp4", "mov", "webm"):
                    fmt = "mp4"

                out_path = await file_picker.save_file(
                    file_name=f"output.{fmt}",
                    initial_directory=_initial_export_dir(),
                    file_type=ft.FilePickerFileType.CUSTOM,
                    allowed_extensions=[fmt],
                )
                if not out_path:
                    return
                out_path = str(Path(out_path).with_suffix(f".{fmt}"))
                cfg.set_last_export_dir(out_path)

                bins = get_bins()
                if not bins:
                    return
                ffmpeg, ffprobe = bins

                # Snapshot to keep export deterministic if the user keeps editing.
                project_snapshot = Project.from_dict(state.project.to_dict())
                v_clips = list(project_snapshot.v_clips)
                a_clips = list(project_snapshot.a_clips)
                tracks = list(project_snapshot.tracks)
                audio_mode = state.export_audio_mode
                export_settings = ExportSettings.from_dict(settings.to_dict())
                video_tracks_with_clips = [t for t in project_snapshot.video_tracks if t.clips]
                visible_video_tracks = [t for t in video_tracks_with_clips if t.visible]
                progress_track = (
                    visible_video_tracks[0]
                    if visible_video_tracks
                    else (video_tracks_with_clips[0] if video_tracks_with_clips else None)
                )
                total_sec = max(0.0, total_duration(progress_track.clips if progress_track else []))

                progress_label = ft.Text("Preparing export...", size=12)
                progress_hint = ft.Text(
                    f"{_fmt_time(0.0)} / {_fmt_time(total_sec)}",
                    size=11,
                    color=ft.Colors.WHITE70,
                )
                progress_bar = ft.ProgressBar(value=0.0, width=420)
                cancel_requested = False
                cancel_btn = ft.TextButton("Cancel Export")

                def _request_cancel_export(_e=None) -> None:
                    nonlocal cancel_requested
                    if cancel_requested:
                        return
                    cancel_requested = True
                    cancel_btn.disabled = True
                    progress_label.value = "Cancelling export..."
                    progress_hint.value = "Waiting for ffmpeg to stop..."
                    try:
                        page.update()
                    except Exception:
                        pass

                cancel_btn.on_click = _request_cancel_export
                export_dialog = ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Exporting"),
                    content=ft.Column(
                        [
                            progress_label,
                            progress_bar,
                            progress_hint,
                        ],
                        tight=True,
                        spacing=8,
                        width=460,
                    ),
                    actions=[cancel_btn],
                )
                page.show_dialog(export_dialog)
                page.update()
                export_in_progress = True

                last_ui_emit = 0.0
                last_ui_ratio = -1.0
                progress_active = True

                def _schedule_progress_update(current_sec: float, total_sec_cb: float, force: bool = False) -> None:
                    nonlocal last_ui_emit, last_ui_ratio, progress_active
                    if not progress_active:
                        return
                    total_for_ui = max(total_sec, float(total_sec_cb or 0.0), 0.001)
                    current_for_ui = max(0.0, min(total_for_ui, float(current_sec or 0.0)))
                    ratio = max(0.0, min(1.0, current_for_ui / total_for_ui))

                    now = time.perf_counter()
                    if (not force) and ratio < 1.0:
                        if now - last_ui_emit < 0.18 and (ratio - last_ui_ratio) < 0.01:
                            return

                    last_ui_emit = now
                    last_ui_ratio = ratio
                    pct = int(round(ratio * 100.0))

                    async def _apply() -> None:
                        if not progress_active:
                            return
                        progress_bar.value = ratio
                        progress_label.value = f"Encoding... {pct}%"
                        progress_hint.value = f"{_fmt_time(current_for_ui)} / {_fmt_time(total_for_ui)}"
                        try:
                            page.update()
                        except Exception:
                            pass

                    page.run_task(_apply)

                def _do_export() -> None:
                    try:
                        export_project_with_progress(
                            ffmpeg,
                            ffprobe,
                            v_clips,
                            a_clips,
                            out_path,
                            audio_mode=audio_mode,
                            export_settings=export_settings,
                            on_progress=lambda current, total: _schedule_progress_update(current, total),
                            should_cancel=lambda: bool(cancel_requested),
                            tracks=tracks,
                        )
                        ok = True
                        cancelled = False
                        err = ""
                    except ExportCancelled:
                        ok = False
                        cancelled = True
                        err = ""
                    except Exception as ex:
                        log.exception("export failed: %s", ex)
                        ok = False
                        cancelled = False
                        err = str(ex)

                    async def _notify() -> None:
                        nonlocal export_in_progress, progress_active
                        export_in_progress = False
                        progress_active = False
                        try:
                            page.pop_dialog()
                        except Exception:
                            pass
                        if ok:
                            snack(f"Export done: {Path(out_path).name}")
                        elif cancelled:
                            snack("Export cancelled")
                        else:
                            snack(f"Export failed: {err}")

                    page.run_task(_notify)

                page.run_thread(_do_export)

            page.run_task(_save_and_export)

        _open_export_settings_dialog(_run_export_with_settings)

    def on_audio_mode_change(e: ft.ControlEvent) -> None:
        state.export_audio_mode = str(e.control.value)

    export_audio_mode = ft.Dropdown(
        width=220,
        dense=True,
        label="Export audio",
        value=state.export_audio_mode,
        options=[
            ft.dropdown.Option(key="mix", text="Mix all tracks"),
            ft.dropdown.Option(key="a1_only", text="Primary A only"),
            ft.dropdown.Option(key="v1_only", text="Primary V audio only"),
        ],
        on_select=on_audio_mode_change,
    )

    # ---------- Layout ----------
    toolbar = ft.Row(
        [
            ft.ElevatedButton("Import", icon=ft.Icons.UPLOAD_FILE, on_click=import_click),
            undo_btn,
            redo_btn,
            shortcuts_btn,
            ft.ElevatedButton("Split", icon=ft.Icons.CONTENT_CUT, on_click=split_click),
            ft.OutlinedButton(
                "Razor",
                icon=ft.Icons.CONTENT_CUT,
                tooltip="Cut all tracks at playhead (R)",
                on_click=razor_multi_cut_click,
            ),
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
                speed_panel,
                transition_panel,
                audio_edit_panel,
                audio_controls,
                audio_pos,
                ft.Text("Tip: Split (S) | Razor all tracks (R) | Shortcuts (F1)", size=12, color=ft.Colors.WHITE70),
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

    timeline_track_controls = ft.Row(
        [
            ft.OutlinedButton("Add V", icon=ft.Icons.VIDEO_COLLECTION, on_click=add_video_track_click),
            ft.OutlinedButton("Add A", icon=ft.Icons.AUDIOTRACK, on_click=add_audio_track_click),
            ft.OutlinedButton("Mute/Unmute", icon=ft.Icons.VOLUME_OFF, on_click=toggle_selected_track_mute_click),
            ft.OutlinedButton("Show/Hide", icon=ft.Icons.VISIBILITY_OFF, on_click=toggle_selected_track_visible_click),
            ft.TextButton("Remove Selected Track", icon=ft.Icons.DELETE_FOREVER, on_click=remove_selected_track_click),
        ],
        spacing=6,
        wrap=False,
    )

    timeline_content = ft.Column(
        [
            ft.Row([timeline_info, ft.Container(expand=True), ft.Text("Zoom"), timeline_zoom]),
            ft.Row(
                [
                    snap_enable_sw,
                    snap_edges_cb,
                    snap_grid_cb,
                    snap_grid_dd,
                    ft.Text("Threshold", size=11),
                    snap_threshold_slider,
                    snap_threshold_label,
                ],
                spacing=6,
                wrap=True,
            ),
            timeline_track_controls,
            ft.Divider(height=6),
            ft.Row(
                [
                    ft.Container(width=timeline_lane_label_w),
                    ft.Container(width=timeline_lane_gap_w),
                    timeline_ruler_row,
                ],
                expand=True,
                spacing=0,
            ),
            timeline_tracks_col,
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
        height=300,
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
            timeline_clips = _timeline_video_clips()
            if timeline_clips and not state.selected_clip_id:
                state.selected_track = _timeline_video_track_id()
                state.selected_clip_id = timeline_clips[0].id
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
            timeline_track_id = _timeline_video_track_id()
            for src in demo_files:
                mi = next((m for m in state.media if m.path == str(src)), None)
                if not mi:
                    continue
                _set_track_clips(
                    timeline_track_id,
                    add_clip_end(
                        _track_clips(timeline_track_id),
                        mi.path,
                        mi.duration,
                        has_audio=mi.has_audio,
                    ),
                )
            _mark_dirty()
            refresh_timeline()

            # Select first clip for inspector visibility.
            timeline_clips = _timeline_video_clips()
            if timeline_clips:
                state.selected_track = _timeline_video_track_id()
                state.selected_clip_id = timeline_clips[0].id
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
                clip = next((c for c in _timeline_video_clips() if c.src == str(src)), None)
                if not clip:
                    continue

                state.selected_track = _timeline_video_track_id()
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
                clips = _timeline_video_clips()
                idx = next((k for k, c in enumerate(clips) if c.id == state.selected_clip_id), None)
                if idx is not None and idx + 1 < len(clips):
                    state.selected_track = _timeline_video_track_id()
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
            cfg.set_last_export_dir(out_path)

            exporting = ft.AlertDialog(
                modal=False,
                title=ft.Text("System test export"),
                content=ft.Text("Exporting..."),
            )
            page.show_dialog(exporting)
            await asyncio.sleep(0.6)
            await _shot("07_exporting")

            project_snapshot = Project.from_dict(state.project.to_dict())
            v_clips = list(project_snapshot.v_clips)
            a_clips = list(project_snapshot.a_clips)
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
                    export_settings=state.export_settings,
                    tracks=list(project_snapshot.tracks),
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
                cfg.set_last_project_dir(path)
                _mark_saved()
                _refresh_recent_menu()
                page.update()
            except Exception as ex:
                log.exception("auto-save failed: %s", ex)
                snack(f"Auto-save failed: {ex}")

    page.run_task(_auto_save_loop)


if __name__ == "__main__":
    ft.app(target=main)

