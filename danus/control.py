"""Versioned research control for Danus v2 projects.

The verified FactGraph remains the truth store.  This module adds the small,
append-only control plane that binds work to an approved target, obligation,
route, and finite exploration lease.  Projects without ``control_version=2``
remain legacy projects and never enter this code path implicitly.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from danus.core._util import append_jsonl, read_jsonl, utc_now


CONTROL_VERSION = 2
INITIAL_SLICES = 3
RENEWAL_SLICES = 2
MAX_ROUTE_SLICES = 12
SLICE_HARD_TIMEOUT = 90 * 60
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ControlError(ValueError):
    """A v2 control contract or state transition is invalid."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ControlError(f"{label} must be a list of non-empty strings")
    return [item.strip() for item in value]


def is_v2_project(project_dir: Path) -> bool:
    meta = Path(project_dir) / "project.json"
    if not meta.is_file():
        return False
    try:
        return json.loads(meta.read_text(encoding="utf-8")).get("control_version") == CONTROL_VERSION
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


class ControlStore:
    """Filesystem-backed v2 control state rooted at one project directory."""

    def __init__(self, project_dir: Path) -> None:
        self.project = Path(project_dir)
        self.dir = self.project / "control"
        self.targets = self.dir / "targets"
        self.obligations = self.dir / "obligations"
        self.routes = self.dir / "routes"
        self.assignments = self.dir / "assignments"
        self.events_file = self.dir / "events.jsonl"
        self.work_report_schema = self.dir / "work_report.schema.json"

    @property
    def enabled(self) -> bool:
        return is_v2_project(self.project)

    def scaffold(self) -> None:
        for directory in (self.targets, self.obligations, self.routes, self.assignments):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.work_report_schema.exists():
            _atomic_json(self.work_report_schema, work_report_schema())

    def append_event(self, event: str, **payload: Any) -> Dict[str, Any]:
        record = {"event_id": uuid.uuid4().hex, "timestamp_utc": utc_now(), "event": event, **payload}
        append_jsonl(self.events_file, record)
        return record

    def events(self, event: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = read_jsonl(self.events_file)
        return rows if event is None else [row for row in rows if row.get("event") == event]

    # ---------------------------------------------------------------- targets
    def _target_path(self, version: str) -> Path:
        return self.targets / f"{_id(version, 'target version')}.json"

    def target_versions(self) -> List[str]:
        return sorted(path.stem for path in self.targets.glob("v*.json"))

    def target(self, version: str) -> Dict[str, Any]:
        path = self._target_path(version)
        if not path.is_file():
            raise ControlError(f"unknown target version: {version}")
        return _read_json(path)

    def target_state(self, version: str) -> str:
        state = "draft"
        for row in self.events():
            if row.get("target_version") != version:
                continue
            if row.get("event") == "target_approved":
                state = "approved"
            elif row.get("event") == "target_superseded":
                state = "superseded"
            elif row.get("event") == "target_rejected":
                state = "rejected"
        return state

    def current_target_version(self) -> Optional[str]:
        current = None
        for row in self.events():
            if row.get("event") == "target_approved":
                current = row.get("target_version")
            elif row.get("event") == "target_superseded" and row.get("target_version") == current:
                current = None
        return current

    def current_target(self) -> Optional[Dict[str, Any]]:
        version = self.current_target_version()
        return self.target(version) if version else None

    def propose_target(self, contract: Dict[str, Any], *, proposed_by: str = "operator") -> Dict[str, Any]:
        self.scaffold()
        versions = self.target_versions()
        version = f"v{(int(versions[-1][1:]) + 1 if versions else 1):04d}"
        statement = str(contract.get("statement") or "").strip()
        if not statement:
            raise ControlError("TargetContract.statement is required")
        allowed = _strings(contract.get("allowed_assumptions"), "allowed_assumptions")
        forbidden = _strings(contract.get("forbidden_assumptions"), "forbidden_assumptions")
        overlap = sorted(set(allowed) & set(forbidden))
        if overlap:
            raise ControlError(f"assumptions cannot be both allowed and forbidden: {overlap}")
        conclusions = contract.get("required_conclusions")
        if not isinstance(conclusions, list) or not conclusions:
            raise ControlError("required_conclusions must be a non-empty list")
        normalized_conclusions = []
        for index, item in enumerate(conclusions, 1):
            if isinstance(item, str):
                item = {"id": f"root-{index}", "statement": item}
            if not isinstance(item, dict) or not str(item.get("statement") or "").strip():
                raise ControlError("each required conclusion needs a statement")
            normalized_conclusions.append({
                "id": _id(str(item.get("id") or f"root-{index}"), "conclusion id"),
                "statement": str(item["statement"]).strip(),
            })
        problem = self.project / "PROBLEM.md"
        payload = {
            "version": version,
            "statement": statement,
            "allowed_assumptions": allowed,
            "forbidden_assumptions": forbidden,
            "required_conclusions": normalized_conclusions,
            "acceptance": contract.get("acceptance") or "all required conclusions are closed",
            "out_of_scope": _strings(contract.get("out_of_scope"), "out_of_scope"),
            "fallback_candidates": contract.get("fallback_candidates") or [],
            "budget": contract.get("budget") or {},
            "problem_sha256": hashlib.sha256(problem.read_bytes()).hexdigest() if problem.is_file() else None,
            "created_at_utc": utc_now(),
            "proposed_by": proposed_by,
        }
        path = self._target_path(version)
        if path.exists():
            raise ControlError(f"target already exists: {version}")
        _atomic_json(path, payload)
        self.append_event("target_proposed", target_version=version, actor=proposed_by)
        return payload

    def approve_target(self, version: str, *, approved_by: str = "operator") -> Dict[str, Any]:
        target = self.target(version)
        if self.target_state(version) != "draft":
            raise ControlError(f"target {version} is not a draft")
        old = self.current_target_version()
        if old:
            self.append_event("target_superseded", target_version=old, replacement=version, actor=approved_by)
        stale_workers = self.invalidate_assignments(reason=f"target changed to {version}")
        self.append_event("target_approved", target_version=version, actor=approved_by)
        for conclusion in target["required_conclusions"]:
            oid = f"{version}-{conclusion['id']}"
            if not (self.obligations / f"{oid}.json").exists():
                self.add_obligation({
                    "id": oid,
                    "target_version": version,
                    "statement": conclusion["statement"],
                    "kind": "root",
                    "dependencies": [],
                    "closure": "verified unconditional fact matching the statement",
                }, actor=approved_by)
        return {"target": target, "stale_workers": stale_workers}

    def target_diff(self, version: str, against: Optional[str] = None) -> str:
        current = against or self.current_target_version()
        before = self.target(current) if current else {}
        after = self.target(version)
        a = json.dumps(before, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
        b = json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
        return "\n".join(difflib.unified_diff(a, b, fromfile=current or "none", tofile=version, lineterm=""))

    def propose_fallback(self, *, proposed_by: str = "system") -> Dict[str, Any]:
        current = self.current_target()
        if not current:
            raise ControlError("no approved target")
        candidates = current.get("fallback_candidates") or []
        if not candidates:
            raise ControlError("the approved target has no explicit fallback_candidates")
        candidate = candidates[0]
        if isinstance(candidate, str):
            candidate = {"statement": candidate, "required_conclusions": [candidate]}
        if not isinstance(candidate, dict):
            raise ControlError("fallback candidate must be a string or object")
        draft = {**current, **candidate}
        for key in ("version", "created_at_utc", "problem_sha256", "proposed_by"):
            draft.pop(key, None)
        proposed = self.propose_target(draft, proposed_by=proposed_by)
        self.append_event(
            "target_fallback_drafted",
            target_version=proposed["version"],
            from_target=current["version"],
            requires_human_approval=True,
        )
        stale_workers = self.invalidate_assignments(reason=f"target fallback draft {proposed['version']} requires approval")
        return {"target": proposed, "stale_workers": stale_workers}

    # ------------------------------------------------------------- obligations
    def add_obligation(self, value: Dict[str, Any], *, actor: str = "main") -> Dict[str, Any]:
        self.scaffold()
        oid = _id(str(value.get("id") or ""), "obligation id")
        target_version = _id(str(value.get("target_version") or self.current_target_version() or ""), "target version")
        self.target(target_version)
        statement = str(value.get("statement") or "").strip()
        if not statement:
            raise ControlError("obligation statement is required")
        dependencies = [_id(item, "obligation dependency") for item in _strings(value.get("dependencies"), "dependencies")]
        payload = {
            "id": oid,
            "target_version": target_version,
            "statement": statement,
            "kind": str(value.get("kind") or "subgoal"),
            "dependencies": dependencies,
            "closure": str(value.get("closure") or "verified fact matching the statement"),
            "created_at_utc": utc_now(),
        }
        path = self.obligations / f"{oid}.json"
        if path.exists():
            existing = _read_json(path)
            if existing != payload:
                raise ControlError(f"obligation already exists: {oid}")
            return existing
        for dependency in dependencies:
            if not (self.obligations / f"{dependency}.json").is_file():
                raise ControlError(f"unknown obligation dependency: {dependency}")
        _atomic_json(path, payload)
        self.append_event("obligation_added", obligation_id=oid, target_version=target_version, actor=actor)
        self.set_obligation_state(oid, "open", actor=actor)
        return payload

    def obligation(self, oid: str) -> Dict[str, Any]:
        path = self.obligations / f"{_id(oid, 'obligation id')}.json"
        if not path.is_file():
            raise ControlError(f"unknown obligation: {oid}")
        return _read_json(path)

    def obligation_state(self, oid: str) -> str:
        self.obligation(oid)
        state = "open"
        for row in self.events("obligation_state"):
            if row.get("obligation_id") == oid:
                state = str(row.get("state") or state)
        return state

    def set_obligation_state(self, oid: str, state: str, *, actor: str,
                             fact_id: Optional[str] = None,
                             assignment_epoch: Optional[str] = None) -> None:
        if state not in {"open", "active", "closed", "blocked", "refuted", "superseded"}:
            raise ControlError(f"invalid obligation state: {state}")
        obligation = self.obligation(oid)
        self.append_event(
            "obligation_state", obligation_id=oid, target_version=obligation["target_version"],
            state=state, actor=actor, fact_id=fact_id, assignment_epoch=assignment_epoch,
        )

    def dependencies_closed(self, oid: str) -> bool:
        return all(self.obligation_state(dep) == "closed" for dep in self.obligation(oid)["dependencies"])

    # ------------------------------------------------------------------ routes
    @staticmethod
    def route_signature(value: Dict[str, Any]) -> str:
        basis = {
            "target_version": value.get("target_version"),
            "obligation_id": value.get("obligation_id"),
            "method_family": value.get("method_family"),
            "assumptions": sorted(value.get("assumptions") or []),
            "input_fact_ids": sorted(value.get("input_fact_ids") or []),
            "expected_result": value.get("expected_result"),
        }
        return hashlib.sha256(_json_bytes(basis)).hexdigest()[:24]

    def add_route(self, value: Dict[str, Any], *, actor: str = "main") -> Dict[str, Any]:
        self.scaffold()
        rid = _id(str(value.get("id") or ""), "route id")
        oid = _id(str(value.get("obligation_id") or ""), "obligation id")
        obligation = self.obligation(oid)
        target_version = _id(str(value.get("target_version") or obligation["target_version"]), "target version")
        if target_version != obligation["target_version"]:
            raise ControlError("route and obligation target versions differ")
        method = str(value.get("method_family") or "").strip()
        expected = str(value.get("expected_result") or "").strip()
        if not method or not expected:
            raise ControlError("route method_family and expected_result are required")
        payload = {
            "id": rid,
            "target_version": target_version,
            "obligation_id": oid,
            "method_family": method,
            "assumptions": _strings(value.get("assumptions"), "route assumptions"),
            "input_fact_ids": _strings(value.get("input_fact_ids"), "input_fact_ids"),
            "expected_result": expected,
            "novelty_basis": _strings(value.get("novelty_basis"), "novelty_basis"),
            "fallback_route_ids": _strings(value.get("fallback_route_ids"), "fallback_route_ids"),
            "created_at_utc": utc_now(),
        }
        payload["signature"] = self.route_signature(payload)
        for path in self.routes.glob("*.json"):
            existing = _read_json(path)
            if existing.get("signature") == payload["signature"] and not payload["novelty_basis"]:
                raise ControlError(f"duplicate route without novelty_basis: {existing['id']}")
        path = self.routes / f"{rid}.json"
        if path.exists():
            raise ControlError(f"route already exists: {rid}")
        _atomic_json(path, payload)
        self.append_event(
            "route_added", route_id=rid, target_version=target_version,
            obligation_id=oid, signature=payload["signature"], actor=actor,
        )
        self.set_route_state(rid, "proposed", actor=actor)
        return payload

    def route(self, rid: str) -> Dict[str, Any]:
        path = self.routes / f"{_id(rid, 'route id')}.json"
        if not path.is_file():
            raise ControlError(f"unknown route: {rid}")
        return _read_json(path)

    def route_state(self, rid: str) -> str:
        self.route(rid)
        state = "proposed"
        for row in self.events("route_state"):
            if row.get("route_id") == rid:
                state = str(row.get("state") or state)
        return state

    def set_route_state(self, rid: str, state: str, *, actor: str, reason: str = "") -> None:
        if state not in {"proposed", "active", "stalled", "failed", "succeeded", "superseded"}:
            raise ControlError(f"invalid route state: {state}")
        route = self.route(rid)
        self.append_event(
            "route_state", route_id=rid, obligation_id=route["obligation_id"],
            target_version=route["target_version"], state=state, reason=reason, actor=actor,
        )

    # --------------------------------------------------------------- assignment
    def assignment_path(self, worker: str) -> Path:
        return self.assignments / f"{_id(worker, 'worker')}.json"

    def assignment(self, worker: str) -> Optional[Dict[str, Any]]:
        path = self.assignment_path(worker)
        return _read_json(path) if path.is_file() else None

    def assign(
        self, worker: str, *, obligation_id: str, route_id: str, task: str,
        max_slices: int = MAX_ROUTE_SLICES, slice_timeout: int = SLICE_HARD_TIMEOUT,
    ) -> Dict[str, Any]:
        target_version = self.current_target_version()
        if not target_version:
            raise ControlError("no approved target")
        obligation = self.obligation(obligation_id)
        route = self.route(route_id)
        if obligation["target_version"] != target_version or route["target_version"] != target_version:
            raise ControlError("obligation or route belongs to a stale target")
        if route["obligation_id"] != obligation_id:
            raise ControlError("route is not bound to the assigned obligation")
        if self.obligation_state(obligation_id) in {"closed", "refuted", "superseded"}:
            raise ControlError(f"obligation is not assignable: {self.obligation_state(obligation_id)}")
        if self.route_state(route_id) in {"stalled", "failed", "succeeded", "superseded"} and not route["novelty_basis"]:
            raise ControlError("route is terminal and has no novelty_basis")
        if not task.strip():
            raise ControlError("assignment task is required")
        payload = {
            "worker": _id(worker, "worker"),
            "epoch": uuid.uuid4().hex,
            "target_version": target_version,
            "obligation_id": obligation_id,
            "route_id": route_id,
            "task": task.strip(),
            "status": "assigned",
            "slice_count": 0,
            "lease_remaining": INITIAL_SLICES,
            "max_slices": max(3, int(max_slices)),
            "slice_timeout": max(1, int(slice_timeout)),
            "consecutive_low": 0,
            "audit_required": False,
            "wall_seconds": 0.0,
            "last_unresolved_interfaces": None,
            "credited_evidence_refs": [],
            "event_cursor": len(self.events()),
            "assigned_at_utc": utc_now(),
        }
        _atomic_json(self.assignment_path(worker), payload)
        self.set_route_state(route_id, "active", actor="assignment")
        self.set_obligation_state(obligation_id, "active", actor="assignment")
        self.append_event("assignment_created", **payload)
        return payload

    def save_assignment(self, assignment: Dict[str, Any]) -> None:
        _atomic_json(self.assignment_path(assignment["worker"]), assignment)

    def invalidate_assignments(self, *, reason: str) -> List[str]:
        workers = []
        for path in self.assignments.glob("*.json"):
            assignment = _read_json(path)
            if assignment.get("status") in {"assigned", "running", "auditing"}:
                assignment["status"] = "stale"
                assignment["stale_reason"] = reason
                self.save_assignment(assignment)
                workers.append(assignment["worker"])
                self.append_event("assignment_stale", worker=assignment["worker"], epoch=assignment["epoch"], reason=reason)
        return workers

    def validate_assignment(self, worker: str) -> Dict[str, Any]:
        assignment = self.assignment(worker)
        if not assignment:
            raise ControlError(f"worker {worker} has no v2 assignment")
        if assignment.get("status") not in {"assigned", "running", "auditing"}:
            raise ControlError(f"assignment is not runnable: {assignment.get('status')}")
        if assignment["target_version"] != self.current_target_version():
            raise ControlError("assignment target is stale")
        self.obligation(assignment["obligation_id"])
        self.route(assignment["route_id"])
        if int(assignment["slice_count"]) >= int(assignment["max_slices"]):
            raise ControlError("route slice budget exhausted")
        budget = self.budget_state()
        if budget["stage"] == "exhausted":
            assignment["status"] = "budget_exhausted"
            self.save_assignment(assignment)
            raise ControlError("project budget exhausted")
        if budget["stage"] == "audit" and not assignment.get("audit_required"):
            assignment["audit_required"] = True
            assignment["status"] = "auditing"
            self.save_assignment(assignment)
        return assignment

    def validate_submission(
        self, worker: str, *, target_version: str, obligation_id: str,
        route_id: str, assignment_epoch: str, assumptions_used: Iterable[str],
    ) -> Dict[str, Any]:
        assignment = self.validate_assignment(worker)
        expected = (
            assignment["target_version"], assignment["obligation_id"],
            assignment["route_id"], assignment["epoch"],
        )
        actual = (target_version, obligation_id, route_id, assignment_epoch)
        if actual != expected:
            raise ControlError("fact submission is not bound to the current assignment")
        target = self.target(target_version)
        used = set(_strings(list(assumptions_used), "assumptions_used"))
        forbidden = used & set(target["forbidden_assumptions"])
        outside = used - set(target["allowed_assumptions"])
        if forbidden:
            raise ControlError(f"submission uses forbidden assumptions: {sorted(forbidden)}")
        if outside:
            raise ControlError(f"submission uses assumptions outside the target: {sorted(outside)}")
        return assignment

    # ----------------------------------------------------------- gain / leases
    def evidence_exists(self, reference: str) -> bool:
        reference = str(reference)
        if (self.project / "fact_graph" / "facts" / f"{reference}.md").is_file():
            return True
        memory = self.project / "global_memory"
        for path in memory.glob("*.jsonl"):
            if any(row.get("id") == reference for row in read_jsonl(path)):
                return True
        return False

    def reusable_fact(self, statement: str, assumptions_used: Iterable[str]) -> Optional[str]:
        """Return an already-verified v2 fact for the exact same claim contract."""
        from danus.core import FactGraph
        from danus.core.factgraph import statement_of

        normalized = " ".join(statement.split())
        assumptions = sorted(str(item) for item in assumptions_used)
        graph = FactGraph(self.project)
        for row in reversed(self.events("fact_linked")):
            fact_id = row.get("fact_id")
            if not fact_id or not graph.exists(fact_id):
                continue
            if self.fact_tainted(str(fact_id)):
                continue
            if sorted(row.get("assumptions_used") or []) != assumptions:
                continue
            if " ".join(statement_of(graph.get_raw(fact_id) or "").split()) == normalized:
                return str(fact_id)
        return None

    def fact_tainted(self, fact_id: str) -> bool:
        return any(row.get("fact_id") == fact_id for row in self.events("fact_tainted"))

    def taint_fact(self, fact_id: str, reason: str, *, actor: str = "main") -> Dict[str, Any]:
        from danus.core import FactGraph

        graph = FactGraph(self.project)
        if not graph.exists(fact_id):
            raise ControlError(f"unknown fact: {fact_id}")
        affected = {fact_id, *graph.descendants(fact_id)}
        event = self.append_event(
            "fact_tainted", fact_id=fact_id, reason=str(reason).strip(), actor=actor,
            affected_fact_ids=sorted(affected), review_required=True,
        )
        stale_workers = []
        for path in self.assignments.glob("*.json"):
            assignment = _read_json(path)
            route = self.route(assignment["route_id"])
            if affected.intersection(route.get("input_fact_ids") or []):
                assignment["status"] = "tainted"
                assignment["stale_reason"] = f"route depends on tainted fact {fact_id}"
                self.save_assignment(assignment)
                stale_workers.append(assignment["worker"])
                self.set_route_state(route["id"], "stalled", actor="controller", reason=assignment["stale_reason"])
        return {"event": event, "stale_workers": stale_workers}

    def evaluate_work_report(
        self, worker: str, report: Dict[str, Any], *, wall_seconds: float,
        usage: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        assignment = self.validate_assignment(worker)
        assignment["status"] = "running"
        assignment["slice_count"] += 1
        assignment["lease_remaining"] = max(0, int(assignment["lease_remaining"]) - 1)
        assignment["wall_seconds"] = float(assignment.get("wall_seconds", 0.0)) + max(0.0, wall_seconds)
        epoch = assignment["epoch"]
        all_events = self.events()
        recent_events = all_events[int(assignment.get("event_cursor", 0)):]
        linked = [row for row in recent_events if row.get("event") == "fact_linked" and row.get("assignment_epoch") == epoch]
        closed = [
            row for row in recent_events
            if row.get("event") == "obligation_state"
            if row.get("obligation_id") == assignment["obligation_id"]
            and row.get("state") in {"closed", "refuted"}
            and row.get("assignment_epoch") == epoch
        ]
        assignment["event_cursor"] = len(all_events)
        gain = "high" if linked or closed else "low"
        refs = report.get("new_evidence_refs") or []
        credited = set(assignment.get("credited_evidence_refs") or [])
        valid_refs = [
            ref for ref in refs
            if isinstance(ref, str) and ref not in credited and self.evidence_exists(ref)
        ]
        interfaces = report.get("unresolved_interfaces") or []
        previous_interfaces = assignment.get("last_unresolved_interfaces")
        reduced_interfaces = isinstance(previous_interfaces, int) and len(interfaces) < previous_interfaces
        state_changing = bool(report.get("new_or_changed_obligations")) or report.get("route_status") in {
            "blocked", "refuted", "new_route", "applicability_changed",
        }
        if gain == "low" and valid_refs and (state_changing or reduced_interfaces or report.get("novelty_basis")):
            gain = "medium"
            assignment["credited_evidence_refs"] = sorted(credited | set(valid_refs))
        assignment["last_unresolved_interfaces"] = len(interfaces)
        audit_was_required = bool(assignment.get("audit_required"))
        if gain in {"high", "medium"}:
            assignment["consecutive_low"] = 0
            assignment["audit_required"] = False
            assignment["lease_remaining"] = min(
                int(assignment["max_slices"]) - int(assignment["slice_count"]),
                int(assignment["lease_remaining"]) + RENEWAL_SLICES,
            )
            decision = "continue"
        else:
            assignment["consecutive_low"] += 1
            if assignment["consecutive_low"] == 1:
                decision = "continue"
            elif assignment["consecutive_low"] == 2:
                assignment["audit_required"] = True
                assignment["status"] = "auditing"
                decision = "audit"
            elif audit_was_required:
                assignment["status"] = "stalled"
                decision = "stalled"
                self.set_route_state(assignment["route_id"], "stalled", actor="controller", reason="three low-gain checkpoints including route audit")
            else:
                assignment["audit_required"] = True
                decision = "audit"
        if assignment["slice_count"] >= assignment["max_slices"]:
            assignment["status"] = "budget_exhausted"
            decision = "budget_exhausted"
            self.set_route_state(assignment["route_id"], "stalled", actor="controller", reason="route hard slice budget exhausted")
        self.save_assignment(assignment)
        self.append_event(
            "work_checkpoint", worker=worker, assignment_epoch=epoch,
            target_version=assignment["target_version"], obligation_id=assignment["obligation_id"],
            route_id=assignment["route_id"], slice_count=assignment["slice_count"],
            gain=gain, decision=decision, valid_evidence_refs=valid_refs,
            report=report,
        )
        self.record_cost(
            component="worker_slice", worker=worker, target_version=assignment["target_version"],
            obligation_id=assignment["obligation_id"], route_id=assignment["route_id"],
            assignment_epoch=epoch, wall_seconds=wall_seconds, usage=usage,
        )
        return {"gain": gain, "decision": decision, "assignment": assignment}

    def activate_fallback(self, worker: str) -> Optional[Dict[str, Any]]:
        assignment = self.assignment(worker)
        if not assignment:
            return None
        route = self.route(assignment["route_id"])
        for rid in route["fallback_route_ids"]:
            if self.route_state(rid) == "proposed":
                fallback = self.route(rid)
                if fallback["obligation_id"] != assignment["obligation_id"]:
                    continue
                self.set_route_state(route["id"], "superseded", actor="controller", reason=f"fallback to {rid}")
                return self.assign(
                    worker, obligation_id=assignment["obligation_id"], route_id=rid,
                    task=f"Fallback route {rid}: {fallback['expected_result']}",
                    max_slices=assignment["max_slices"], slice_timeout=assignment["slice_timeout"],
                )
        return None

    # -------------------------------------------------------------------- cost
    def record_cost(
        self, *, component: str, wall_seconds: float, usage: Optional[Dict[str, Any]] = None,
        cost_usd: Optional[float] = None, **scope: Any,
    ) -> Dict[str, Any]:
        usage = usage or {}
        if cost_usd is None:
            try:
                in_rate = float(os.environ.get("DANUS_CODEX_PRICE_IN", ""))
                out_rate = float(os.environ.get("DANUS_CODEX_PRICE_OUT", ""))
                cost_usd = (
                    float(usage.get("input_tokens", 0) or 0) * in_rate
                    + float(usage.get("output_tokens", 0) or 0) * out_rate
                ) / 1_000_000
            except ValueError:
                cost_usd = None
        event = self.append_event(
            "cost", component=component, wall_seconds=round(max(0.0, wall_seconds), 3),
            usage=usage, cost_usd=cost_usd, **scope,
        )
        self._record_budget_threshold()
        return event

    def budget_state(self) -> Dict[str, Any]:
        target = self.current_target() or {}
        budget = target.get("budget") or {}
        costs = self.events("cost")
        spent_wall = sum(float(row.get("wall_seconds") or 0) for row in costs)
        spent_cost = sum(float(row.get("cost_usd") or 0) for row in costs if row.get("cost_usd") is not None)
        ratios = []
        for spent, key in ((spent_wall, "max_wall_seconds"), (spent_cost, "max_cost_usd")):
            try:
                limit = float(budget.get(key))
                if limit > 0:
                    ratios.append(spent / limit)
            except (TypeError, ValueError):
                pass
        ratio = max(ratios, default=0.0)
        stage = "exhausted" if ratio >= 1 else "audit" if ratio >= .85 else "warn" if ratio >= .70 else "normal"
        return {"stage": stage, "ratio": ratio, "wall_seconds": spent_wall,
                "cost_usd": spent_cost, "budget": budget}

    def _record_budget_threshold(self) -> None:
        state = self.budget_state()
        prior = [row.get("stage") for row in self.events("budget_threshold")]
        if state["stage"] != "normal" and (not prior or prior[-1] != state["stage"]):
            self.append_event(
                "budget_threshold", target_version=self.current_target_version(), **state,
            )

    # -------------------------------------------------------------- read model
    def rebuild_read_model(self) -> Dict[str, Any]:
        self.scaffold()
        path = self.dir / "read_model.sqlite3"
        tmp = self.dir / "read_model.sqlite3.tmp"
        tmp.unlink(missing_ok=True)
        db = sqlite3.connect(tmp)
        try:
            db.executescript(
                """
                CREATE TABLE targets(version TEXT PRIMARY KEY, state TEXT, statement TEXT, payload TEXT);
                CREATE TABLE obligations(id TEXT PRIMARY KEY, target_version TEXT, state TEXT, statement TEXT, payload TEXT);
                CREATE TABLE routes(id TEXT PRIMARY KEY, target_version TEXT, obligation_id TEXT, state TEXT, method_family TEXT, signature TEXT, payload TEXT);
                CREATE TABLE assignments(worker TEXT PRIMARY KEY, status TEXT, target_version TEXT, obligation_id TEXT, route_id TEXT, payload TEXT);
                CREATE TABLE events(event_id TEXT PRIMARY KEY, timestamp_utc TEXT, event TEXT, target_version TEXT, obligation_id TEXT, route_id TEXT, worker TEXT, payload TEXT);
                CREATE TABLE facts(fact_id TEXT PRIMARY KEY, statement TEXT, proof TEXT, payload TEXT);
                CREATE VIEW cost_events AS SELECT * FROM events WHERE event='cost';
                """
            )
            for version in self.target_versions():
                target = self.target(version)
                db.execute("INSERT INTO targets VALUES (?,?,?,?)", (version, self.target_state(version), target["statement"], json.dumps(target, ensure_ascii=False)))
            for path_obj in self.obligations.glob("*.json"):
                item = _read_json(path_obj)
                db.execute("INSERT INTO obligations VALUES (?,?,?,?,?)", (item["id"], item["target_version"], self.obligation_state(item["id"]), item["statement"], json.dumps(item, ensure_ascii=False)))
            for path_obj in self.routes.glob("*.json"):
                item = _read_json(path_obj)
                db.execute("INSERT INTO routes VALUES (?,?,?,?,?,?,?)", (item["id"], item["target_version"], item["obligation_id"], self.route_state(item["id"]), item["method_family"], item["signature"], json.dumps(item, ensure_ascii=False)))
            for path_obj in self.assignments.glob("*.json"):
                item = _read_json(path_obj)
                db.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?)", (item["worker"], item["status"], item["target_version"], item["obligation_id"], item["route_id"], json.dumps(item, ensure_ascii=False)))
            for item in self.events():
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (
                    item.get("event_id"), item.get("timestamp_utc"), item.get("event"),
                    item.get("target_version"), item.get("obligation_id"), item.get("route_id"),
                    item.get("worker"), json.dumps(item, ensure_ascii=False),
                ))
            from danus.core import FactGraph
            from danus.core.factgraph import statement_of
            graph = FactGraph(self.project)
            for fact_id in graph.list():
                raw = graph.get_raw(fact_id) or ""
                statement = statement_of(raw)
                db.execute("INSERT INTO facts VALUES (?,?,?,?)", (fact_id, statement, raw, json.dumps({"fact_id": fact_id, "raw": raw}, ensure_ascii=False)))
            try:
                db.execute("CREATE VIRTUAL TABLE research_fts USING fts5(kind, object_id, text)")
                db.execute("INSERT INTO research_fts SELECT 'target', version, statement FROM targets")
                db.execute("INSERT INTO research_fts SELECT 'obligation', id, statement FROM obligations")
                db.execute("INSERT INTO research_fts SELECT 'route', id, method_family || ' ' || payload FROM routes")
                db.execute("INSERT INTO research_fts SELECT 'fact', fact_id, statement || ' ' || proof FROM facts")
            except sqlite3.OperationalError:
                # ponytail: FTS5 may be absent in a minimal Python build; the canonical
                # files remain usable and a plain table keeps rebuild deterministic.
                db.execute("CREATE TABLE research_fts(kind TEXT, object_id TEXT, text TEXT)")
                db.execute("INSERT INTO research_fts SELECT 'target', version, statement FROM targets")
                db.execute("INSERT INTO research_fts SELECT 'obligation', id, statement FROM obligations")
                db.execute("INSERT INTO research_fts SELECT 'route', id, method_family || ' ' || payload FROM routes")
                db.execute("INSERT INTO research_fts SELECT 'fact', fact_id, statement || ' ' || proof FROM facts")
            db.commit()
        finally:
            db.close()
        os.replace(tmp, path)
        return {
            "path": str(path), "targets": len(self.target_versions()),
            "obligations": len(list(self.obligations.glob("*.json"))),
            "routes": len(list(self.routes.glob("*.json"))),
            "events": len(self.events()),
        }


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
            "failed_attempt_signatures", "novelty_basis", "recommended_next_action",
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
    if not path.is_file():
        return {
            "route_status": "no_progress", "summary": "worker produced no structured report",
            "new_fact_ids": [], "new_evidence_refs": [], "new_or_changed_obligations": [],
            "unresolved_interfaces": [], "failed_attempt_signatures": [],
            "novelty_basis": [], "recommended_next_action": "route audit",
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("report is not an object")
        return value
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "route_status": "no_progress", "summary": f"invalid structured report: {exc}",
            "new_fact_ids": [], "new_evidence_refs": [], "new_or_changed_obligations": [],
            "unresolved_interfaces": [], "failed_attempt_signatures": [],
            "novelty_basis": [], "recommended_next_action": "route audit",
        }


def work_report_valid(report: Any) -> bool:
    """Small runtime check for reports recovered after an abnormal process exit."""
    schema = work_report_schema()
    if not isinstance(report, dict) or set(report) != set(schema["required"]):
        return False
    if report.get("route_status") not in schema["properties"]["route_status"]["enum"]:
        return False
    if not isinstance(report.get("summary"), str) or not isinstance(report.get("recommended_next_action"), str):
        return False
    array_fields = set(schema["required"]) - {"route_status", "summary", "recommended_next_action"}
    return all(isinstance(report.get(key), list) and all(isinstance(item, str) for item in report[key]) for key in array_fields)


def parse_codex_usage(log_path: Path) -> Dict[str, int]:
    """Best-effort extraction from ``codex exec --json`` without assuming one CLI version."""
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
                if any(key in item for key in aliases):
                    for source, target in aliases.items():
                        try:
                            best[target] = max(best.get(target, 0), int(item.get(source, 0) or 0))
                        except (TypeError, ValueError):
                            pass
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    if "input_tokens" in best:
        best["fresh_input_tokens"] = max(
            0, best["input_tokens"] - best.get("cached_input_tokens", 0)
        )
    return best


# Keep the old implementation solely as the one-time importer for pre-SQLite v2
# projects. Runtime callers receive the transactional store.
FileControlStore = ControlStore


class ControlStore:
    """Lazy public constructor, avoiding a control ↔ control_db import cycle."""

    def __new__(cls, project_dir: Path):
        from danus.control_db import SQLiteControlStore

        return SQLiteControlStore(project_dir)
