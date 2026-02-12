from __future__ import annotations

from typing import List, Optional, Tuple


# Shortcut action ids used by app.py dispatcher.
ACTION_DELETE = "delete"
ACTION_SPLIT = "split"
ACTION_RAZOR = "razor"
ACTION_TRIM_IN = "trim_in"
ACTION_TRIM_OUT = "trim_out"
ACTION_SAVE = "save"
ACTION_UNDO = "undo"
ACTION_REDO = "redo"
ACTION_EXPORT = "export"
ACTION_IMPORT = "import"
ACTION_DUPLICATE = "duplicate"
ACTION_ZOOM_IN = "zoom_in"
ACTION_ZOOM_OUT = "zoom_out"
ACTION_SELECT_PREV = "select_prev"
ACTION_SELECT_NEXT = "select_next"
ACTION_TOGGLE_PLAY_PAUSE = "toggle_play_pause"
ACTION_SHOW_SHORTCUTS = "show_shortcuts"


def _normalize_key(key: str) -> str:
    raw = str(key or "")
    if raw == " ":
        return "space"
    k = raw.strip().lower().replace(" ", "")
    aliases = {
        "arrowleft": "left",
        "arrowright": "right",
        "arrowup": "up",
        "arrowdown": "down",
        "spacebar": "space",
        "add": "+",
        "subtract": "-",
    }
    return aliases.get(k, k)


def resolve_shortcut_action(
    *,
    key: str,
    ctrl: bool = False,
    shift: bool = False,
    alt: bool = False,
    meta: bool = False,
    typing_focus: bool = False,
) -> Optional[str]:
    """
    Resolve a keyboard event into an app action.

    `typing_focus=True` blocks plain editing/navigation shortcuts so users can
    type in TextFields without accidental timeline operations.
    """
    k = _normalize_key(key)
    if not k:
        return None

    # Always-available help shortcut.
    if k == "f1" or k == "?" or (k == "/" and bool(shift)):
        return ACTION_SHOW_SHORTCUTS

    if bool(alt):
        return None

    primary_mod = bool(ctrl or meta)
    if primary_mod and (not shift) and k == "z":
        return ACTION_UNDO
    if primary_mod and (k == "y" or (shift and k == "z")):
        return ACTION_REDO
    if primary_mod and k == "s":
        return ACTION_SAVE
    if primary_mod and k == "e":
        return ACTION_EXPORT
    if primary_mod and k == "i":
        return ACTION_IMPORT
    if primary_mod and k == "d":
        return ACTION_DUPLICATE

    if typing_focus:
        return None

    if k in ("delete", "backspace"):
        return ACTION_DELETE
    if k == "s":
        return ACTION_SPLIT
    if k == "r":
        return ACTION_RAZOR
    if k == "i":
        return ACTION_TRIM_IN
    if k == "o":
        return ACTION_TRIM_OUT
    if k in ("+", "="):
        return ACTION_ZOOM_IN
    if k in ("-", "_"):
        return ACTION_ZOOM_OUT
    if k == "left":
        return ACTION_SELECT_PREV
    if k == "right":
        return ACTION_SELECT_NEXT
    if k == "space":
        return ACTION_TOGGLE_PLAY_PAUSE
    return None


def shortcut_legend() -> List[Tuple[str, str]]:
    """Human-readable shortcuts list for the in-app help dialog."""
    return [
        ("Delete / Backspace", "Delete selected clip"),
        ("S", "Split selected clip"),
        ("R", "Razor cut all tracks at playhead"),
        ("I / O", "Set trim in/out at playhead"),
        ("Left / Right", "Select previous/next clip"),
        ("+ / -", "Zoom timeline in/out"),
        ("Space", "Play/Pause selected clip"),
        ("Ctrl/Cmd + S", "Save project"),
        ("Ctrl/Cmd + Z", "Undo"),
        ("Ctrl/Cmd + Y", "Redo"),
        ("Ctrl/Cmd + Shift + Z", "Redo"),
        ("Ctrl/Cmd + E", "Export"),
        ("Ctrl/Cmd + I", "Import files"),
        ("Ctrl/Cmd + D", "Duplicate selected clip"),
        ("F1 or ?", "Show shortcuts help"),
    ]

