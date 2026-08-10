"""Shared target-governance application service for CLI and HTTP callers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from danus.control import ControlStore


class ControlService:
    def __init__(self, project: Path, outbox_handler: Callable[[str, dict[str, Any]], Any]) -> None:
        self.store = ControlStore(Path(project))
        self.outbox_handler = outbox_handler

    def approve_target(self, version: str, *, actor: str = "operator", request_id: Optional[str] = None, expected_generation: Optional[int] = None) -> dict[str, Any]:
        result = self.store.approve_target(
            version, approved_by=actor, request_id=request_id,
            expected_generation=expected_generation,
        )
        return {**result, "outbox": self.store.process_outbox(self.outbox_handler)}

    def withdraw_target(self, version: str, *, reason: str, actor: str = "operator", request_id: Optional[str] = None, expected_generation: Optional[int] = None) -> dict[str, Any]:
        result = self.store.withdraw_target(
            version, reason=reason, actor=actor, request_id=request_id,
            expected_generation=expected_generation,
        )
        return {**result, "outbox": self.store.process_outbox(self.outbox_handler)}
