"""Public contracts and helpers for transactional Danus research control."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List


CONTROL_VERSION = 2
INITIAL_SLICES = 3
RENEWAL_SLICES = 2
MAX_ROUTE_SLICES = 12
SLICE_HARD_TIMEOUT = 90 * 60
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ControlError(ValueError):
    """A research-control contract or state transition is invalid."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"invalid control JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlError(f"control JSON must be an object: {path}")
    return value


def _id(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not _SAFE_ID.fullmatch(value):
        raise ControlError(f"invalid {label}: {value!r}")
    return value


def _strings(value: Any, label: str) -> List[str]:
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ControlError(f"{label} must be a list of non-empty strings")
    return [item.strip() for item in value]


def is_v2_project(project_dir: Path) -> bool:
    meta = Path(project_dir) / "project.json"
    if not meta.is_file():
        return False
    try:
        value = json.loads(meta.read_text(encoding="utf-8"))
        return isinstance(value, dict) and value.get("control_version") == CONTROL_VERSION
    except (OSError, json.JSONDecodeError):
        return False


def require_v2_project(project_dir: Path) -> Path:
    """Reject an unmigrated project instead of silently running V1 semantics."""
    project = Path(project_dir)
    if not is_v2_project(project):
        raise ControlError(
            f"project {project.name!r} requires migration; run "
            f"`danus migrate {project.name}` before using it"
        )
    return project


def work_report_schema() -> Dict[str, Any]:
    """JSON Schema passed directly to ``codex exec --output-schema``."""
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "route_status", "summary", "new_fact_ids", "new_evidence_refs",
            "new_or_changed_obligations", "unresolved_interfaces",
            "failed_attempt_signatures", "novelty_basis",
            "recommended_next_action",
        ],
        "properties": {
            "route_status": {"type": "string", "enum": [
                "progress", "blocked", "refuted", "new_route",
                "applicability_changed", "no_progress", "completed",
            ]},
            "summary": {"type": "string"},
            "new_fact_ids": string_array,
            "new_evidence_refs": string_array,
            "new_or_changed_obligations": string_array,
            "unresolved_interfaces": string_array,
            "failed_attempt_signatures": string_array,
            "novelty_basis": string_array,
            "recommended_next_action": {"type": "string"},
        },
    }


def parse_work_report(path: Path) -> Dict[str, Any]:
    fallback = {
        "route_status": "no_progress",
        "summary": "worker produced no structured report",
        "new_fact_ids": [],
        "new_evidence_refs": [],
        "new_or_changed_obligations": [],
        "unresolved_interfaces": [],
        "failed_attempt_signatures": [],
        "novelty_basis": [],
        "recommended_next_action": "route audit",
    }
    if not path.is_file():
        return fallback
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("report is not an object")
        return value
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return fallback | {"summary": f"invalid structured report: {exc}"}


def work_report_valid(report: Any) -> bool:
    """Small runtime check for reports recovered after an abnormal process exit."""
    schema = work_report_schema()
    if not isinstance(report, dict) or set(report) != set(schema["required"]):
        return False
    if report.get("route_status") not in schema["properties"]["route_status"]["enum"]:
        return False
    if (
        not isinstance(report.get("summary"), str)
        or not isinstance(report.get("recommended_next_action"), str)
    ):
        return False
    array_fields = set(schema["required"]) - {
        "route_status", "summary", "recommended_next_action",
    }
    return all(
        isinstance(report.get(key), list)
        and all(isinstance(item, str) for item in report[key])
        for key in array_fields
    )


def parse_codex_usage(log_path: Path) -> Dict[str, int]:
    """Best-effort extraction from Codex JSON events across CLI versions."""
    best: Dict[str, int] = {}
    aliases = {
        "input_tokens": "input_tokens",
        "cached_input_tokens": "cached_input_tokens",
        "output_tokens": "output_tokens",
        "reasoning_tokens": "reasoning_tokens",
        "reasoning_output_tokens": "reasoning_tokens",
    }
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return best
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for source, target in aliases.items():
                    if source not in item:
                        continue
                    try:
                        best[target] = max(
                            best.get(target, 0), int(item.get(source, 0) or 0),
                        )
                    except (TypeError, ValueError):
                        pass
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    if "input_tokens" in best:
        best["fresh_input_tokens"] = max(
            0, best["input_tokens"] - best.get("cached_input_tokens", 0),
        )
    return best


class ControlStore:
    """Lazy public constructor, avoiding a control/control_db import cycle."""

    def __new__(cls, project_dir: Path):
        from danus.control_db import SQLiteControlStore

        return SQLiteControlStore(project_dir)
