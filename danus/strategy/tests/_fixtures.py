"""Shared V2 project setup for offline strategy tests."""

from __future__ import annotations

import json
from pathlib import Path

from danus.control import ControlStore


def prepare_v2_project(path: str | Path) -> Path:
    project = Path(path)
    (project / "project.json").write_text(
        json.dumps({"name": project.name, "control_version": 2}), encoding="utf-8",
    )
    ControlStore(project).scaffold()
    return project
