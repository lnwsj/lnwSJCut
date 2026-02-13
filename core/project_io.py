from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .model import Project


def save_project(project: Project, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(project.to_dict(), ensure_ascii=False, indent=2)

    # Atomic write: auto-save shouldn't risk corrupting the project file.
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(p.parent),
            prefix=f".{p.name}.",
            suffix=".tmp",
        ) as fp:
            tmp_path = Path(fp.name)
            fp.write(payload)
            fp.flush()
            try:
                os.fsync(fp.fileno())
            except Exception:
                pass
        os.replace(str(tmp_path), str(p))
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def load_project(path: str) -> Project:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return Project.from_dict(data)
