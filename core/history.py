from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class HistoryEntry:
    """
    Snapshot of editor state used for Undo/Redo.

    Notes:
        - Store Project as a plain dict (Project.to_dict()) to keep this module
          independent from the UI layer and avoid accidental mutation.
        - Selection is stored as raw ids; the UI should validate existence after restore.
    """

    label: str
    project: Dict[str, Any]
    selected_track: Optional[str] = None  # "v" | "a" | None
    selected_clip_id: Optional[str] = None


class HistoryManager:
    def __init__(self, limit: int = 50) -> None:
        self.limit = max(1, int(limit))
        self._undo: List[HistoryEntry] = []
        self._redo: List[HistoryEntry] = []

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def peek_undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ""

    def peek_redo_label(self) -> str:
        return self._redo[-1].label if self._redo else ""

    def record(self, entry: HistoryEntry) -> None:
        """
        Record a new undo step.

        Call this *before* applying a mutation. Recording clears the redo stack.
        """

        self._undo.append(entry)
        if len(self._undo) > self.limit:
            self._undo = self._undo[-self.limit :]
        self._redo.clear()

    def undo(self, current: HistoryEntry) -> Optional[HistoryEntry]:
        if not self._undo:
            return None
        entry = self._undo.pop()
        # Keep the same label so redo describes the same operation.
        self._redo.append(HistoryEntry(label=entry.label, project=current.project, selected_track=current.selected_track, selected_clip_id=current.selected_clip_id))
        return entry

    def redo(self, current: HistoryEntry) -> Optional[HistoryEntry]:
        if not self._redo:
            return None
        entry = self._redo.pop()
        self._undo.append(HistoryEntry(label=entry.label, project=current.project, selected_track=current.selected_track, selected_clip_id=current.selected_clip_id))
        if len(self._undo) > self.limit:
            self._undo = self._undo[-self.limit :]
        return entry

