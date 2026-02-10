from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    # Local time with offset is easier to inspect than UTC "Z".
    return datetime.now(timezone.utc).astimezone().isoformat()


@dataclass(frozen=True)
class RecentProject:
    path: str
    name: str
    last_opened: str

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> Optional["RecentProject"]:
        try:
            path = str(d.get("path", "")).strip()
            if not path:
                return None
            name = str(d.get("name") or Path(path).stem)
            last_opened = str(d.get("last_opened") or "")
            return RecentProject(path=path, name=name, last_opened=last_opened)
        except Exception:
            return None

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "name": self.name, "last_opened": self.last_opened}


class ConfigStore:
    """
    Simple JSON config store.

    Default location: ~/.minicut/config.json
    """

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir)
        self.path = self.root_dir / "config.json"

    @staticmethod
    def default() -> "ConfigStore":
        # Keep it Unix-y even on Windows; it's in the user's home anyway.
        return ConfigStore(Path.home() / ".minicut")

    def load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            return self.default_config()
        except Exception:
            # Corrupted file; don't crash the app.
            return self.default_config()
        return self.default_config()

    def save(self, data: Dict[str, Any]) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def default_config(self) -> Dict[str, Any]:
        return {
            "recent": [],
            "auto_save_interval_sec": 60,
            "last_export_dir": "",
        }

    def auto_save_interval_sec(self) -> int:
        cfg = self.load()
        raw = cfg.get("auto_save_interval_sec", 60)
        try:
            v = int(raw)
        except Exception:
            v = 60
        # Clamp: don't allow 0 or extremely small values.
        return max(10, min(3600, v))

    def recent_projects(self, limit: int = 10) -> List[RecentProject]:
        cfg = self.load()
        items = cfg.get("recent", [])
        out: List[RecentProject] = []
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                rp = RecentProject.from_dict(it)
                if rp:
                    out.append(rp)
        return out[: max(0, int(limit))]

    def clear_recent_projects(self) -> None:
        cfg = self.load()
        cfg["recent"] = []
        self.save(cfg)

    def add_recent_project(self, path: str, name: Optional[str] = None) -> None:
        p = str(path).strip()
        if not p:
            return

        # Normalize for de-dupe.
        try:
            p = str(Path(p).resolve())
        except Exception:
            pass

        # Case-insensitive compare on Windows.
        key = p.lower() if os.name == "nt" else p

        cfg = self.load()
        existing = self.recent_projects(limit=50)
        kept: List[RecentProject] = []
        for rp in existing:
            rp_key = rp.path.lower() if os.name == "nt" else rp.path
            if rp_key != key:
                kept.append(rp)

        rp = RecentProject(path=p, name=name or Path(p).stem, last_opened=_now_iso())
        recent = [rp, *kept][:10]
        cfg["recent"] = [x.to_dict() for x in recent]
        self.save(cfg)

