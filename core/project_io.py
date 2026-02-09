from __future__ import annotations

import json
from pathlib import Path

from .model import Project


def save_project(project: Project, path: str) -> None:
    p = Path(path)
    p.write_text(json.dumps(project.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_project(path: str) -> Project:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return Project.from_dict(data)
