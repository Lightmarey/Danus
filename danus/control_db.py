"""Transactional Danus v2 control store.

Verified mathematics remains in ``fact_graph/facts/*.md``.  This database is
authoritative for research control and contains rebuildable fact/query indexes.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from danus.core._util import read_jsonl, utc_now

from .control import (
    DEFAULT_ROUND_TIMEOUT_SECONDS,
    INITIAL_ROUNDS,
    MAX_ROUTE_ROUNDS,
    RENEWAL_ROUNDS,
    ControlError,
    _id,
    _json_bytes,
    _read_json,
    _strings,
    work_report_schema,
)

_METHOD_KEY = re.compile(r"[^a-z0-9]+")
_RESERVATION_GRACE_SECONDS = 60.0
_NESTED_CALL_CLEANUP_SECONDS = 15.0
_FRESH_INPUT_ANOMALY_THRESHOLD = 100_000
_SQLITE_BUSY_TIMEOUT_MS = 30_000


class _Connection(sqlite3.Connection):
    """Make ``with store._connect()`` release Windows file handles promptly."""

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


class SQLiteControlStore:
    """SQLite-backed control state with the same public surface as v2's store."""

    def __init__(self, project_dir: Path) -> None:
        self.project = Path(project_dir)
        self.dir = self.project / "control"
        self.db_path = self.dir / "control.sqlite3"
        self.work_report_schema = self.dir / "work_report.schema.json"
        # Legacy import locations; callers must not use them as live state.
        self.targets = self.dir / "targets"
        self.obligations = self.dir / "obligations"
        self.routes = self.dir / "routes"
        self.assignments = self.dir / "assignments"
        self.events_file = self.dir / "events.jsonl"
        self._initialized = False
        self._initializing = False

    def _connect(self) -> sqlite3.Connection:
        self.dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(
            self.db_path,
            timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000,
            factory=_Connection,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        return db

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def scaffold(self) -> None:
        if self._initialized or self._initializing:
            return
        self._initializing = True
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            if not self.work_report_schema.exists():
                tmp = self.work_report_schema.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(work_report_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, self.work_report_schema)
            db = self._connect()
            try:
                db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS targets(
                    version TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS obligations(
                    id TEXT PRIMARY KEY, target_version TEXT NOT NULL REFERENCES targets(version),
                    state TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS obligation_edges(
                    dependency_id TEXT NOT NULL, obligation_id TEXT NOT NULL,
                    PRIMARY KEY(dependency_id, obligation_id)
                );
                CREATE TABLE IF NOT EXISTS routes(
                    id TEXT PRIMARY KEY, target_version TEXT NOT NULL, obligation_id TEXT NOT NULL,
                    state TEXT NOT NULL, method_key TEXT NOT NULL, method_title TEXT NOT NULL,
                    signature TEXT NOT NULL, payload TEXT NOT NULL,
                    FOREIGN KEY(target_version) REFERENCES targets(version),
                    FOREIGN KEY(obligation_id) REFERENCES obligations(id)
                );
                CREATE INDEX IF NOT EXISTS routes_target ON routes(target_version, method_key);
                CREATE TABLE IF NOT EXISTS route_inputs(
                    route_id TEXT NOT NULL, fact_id TEXT NOT NULL, PRIMARY KEY(route_id, fact_id)
                );
                CREATE TABLE IF NOT EXISTS assignments(
                    worker TEXT PRIMARY KEY, epoch TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                    target_version TEXT NOT NULL, obligation_id TEXT NOT NULL, route_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(target_version) REFERENCES targets(version),
                    FOREIGN KEY(obligation_id) REFERENCES obligations(id),
                    FOREIGN KEY(route_id) REFERENCES routes(id)
                );
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    timestamp_utc TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_kind ON events(event, seq);
                CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, created_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests(
                    request_id TEXT PRIMARY KEY, action TEXT NOT NULL, result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_facts(
                    submission_id TEXT PRIMARY KEY, fact_id TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pending_facts_fact ON pending_facts(fact_id, status);
                CREATE TABLE IF NOT EXISTS facts(
                    fact_id TEXT PRIMARY KEY, title TEXT NOT NULL, statement TEXT NOT NULL,
                    proof TEXT NOT NULL, intuition TEXT NOT NULL, author TEXT NOT NULL,
                    problem_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', raw TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fact_edges(
                    predecessor_id TEXT NOT NULL, fact_id TEXT NOT NULL,
                    PRIMARY KEY(predecessor_id, fact_id)
                );
                CREATE INDEX IF NOT EXISTS fact_edges_fact ON fact_edges(fact_id);
                CREATE TABLE IF NOT EXISTS fact_scopes(
                    fact_id TEXT NOT NULL, target_version TEXT NOT NULL, obligation_id TEXT NOT NULL,
                    route_id TEXT NOT NULL, assignment_epoch TEXT NOT NULL, claim_role TEXT NOT NULL,
                    relation TEXT NOT NULL, event_seq INTEGER,
                    PRIMARY KEY(fact_id, target_version, obligation_id, route_id, assignment_epoch, relation)
                );
                CREATE INDEX IF NOT EXISTS fact_scopes_route ON fact_scopes(route_id, relation);
                CREATE INDEX IF NOT EXISTS fact_scopes_obligation ON fact_scopes(obligation_id, relation);
                CREATE TABLE IF NOT EXISTS checkpoints(
                    event_seq INTEGER PRIMARY KEY, target_version TEXT, obligation_id TEXT, route_id TEXT,
                    worker TEXT, rounds_used INTEGER, gain TEXT, decision TEXT, report TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS checkpoints_route ON checkpoints(route_id, event_seq DESC);
                CREATE TABLE IF NOT EXISTS obstacles(
                    signature TEXT NOT NULL, route_id TEXT NOT NULL, obligation_id TEXT NOT NULL,
                    title TEXT NOT NULL, status TEXT NOT NULL, occurrences INTEGER NOT NULL,
                    first_seen_seq INTEGER NOT NULL, last_seen_seq INTEGER NOT NULL,
                    PRIMARY KEY(signature, route_id)
                );
                CREATE TABLE IF NOT EXISTS context_manifests(
                    id TEXT PRIMARY KEY, worker TEXT NOT NULL, assignment_epoch TEXT NOT NULL,
                    snapshot_generation INTEGER NOT NULL, payload TEXT NOT NULL, created_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backend_circuits(
                    provider_key TEXT PRIMARY KEY, state TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL, opened_until REAL,
                    failure_class TEXT, infra_wall_seconds REAL NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS call_reservations(
                    id TEXT PRIMARY KEY, component TEXT NOT NULL, provider_key TEXT NOT NULL,
                    reserved_wall_seconds REAL NOT NULL, reserved_cost_usd REAL,
                    status TEXT NOT NULL, scope TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL, expires_at_epoch REAL NOT NULL,
                    settled_at_utc TEXT
                );
                CREATE INDEX IF NOT EXISTS call_reservations_active ON call_reservations(status, expires_at_epoch);
                """
                )
                if "infra_wall_seconds" not in {row[1] for row in db.execute("PRAGMA table_info(backend_circuits)")}:
                    db.execute("ALTER TABLE backend_circuits ADD COLUMN infra_wall_seconds REAL NOT NULL DEFAULT 0")
                # Route signatures are a deterministic duplicate warning, not a
                # database uniqueness rule: a justified novelty basis may reuse one.
                db.execute("DROP INDEX IF EXISTS routes_signature")
                db.execute("CREATE INDEX IF NOT EXISTS routes_signature_lookup ON routes(signature)")
                try:
                    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(fact_id UNINDEXED, title, statement, proof)")
                except sqlite3.OperationalError:
                    db.execute("CREATE TABLE IF NOT EXISTS facts_fts(fact_id TEXT PRIMARY KEY, title TEXT, statement TEXT, proof TEXT)")
                self._migrate_round_vocabulary(db)
                self._migrate_fact_submission_schema(db)
                db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','4')")
                db.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
                db.execute("INSERT OR IGNORE INTO meta VALUES ('generation','0')")
                db.commit()
            finally:
                db.close()
            self._migrate_files_once()
            self._initialized = True
            self.recover_pending_facts()
            self.recover_call_reservations()
        finally:
            self._initializing = False

    @staticmethod
    def _round_payload(value: Any) -> Any:
        """Translate pre-v3 control payloads without retaining runtime aliases."""
        keys = {
            "slice_count": "rounds_used",
            "lease_remaining": "rounds_remaining",
            "max_slices": "max_rounds",
            "slice_timeout": "round_timeout_seconds",
            "slice_number": "round_number",
        }
        if isinstance(value, dict):
            return {
                keys.get(key, key): SQLiteControlStore._round_payload(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [SQLiteControlStore._round_payload(item) for item in value]
        return "worker_round" if value == "worker_slice" else value

    def _migrate_round_vocabulary(self, db: sqlite3.Connection) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(checkpoints)")}
        if "slice_count" in columns and "rounds_used" not in columns:
            db.execute("ALTER TABLE checkpoints RENAME COLUMN slice_count TO rounds_used")
        for table, key in (("assignments", "worker"), ("events", "seq"), ("context_manifests", "id")):
            for row in db.execute(f"SELECT {key},payload FROM {table}").fetchall():
                payload = self._round_payload(_load(row["payload"], {}))
                db.execute(f"UPDATE {table} SET payload=? WHERE {key}=?", (_dump(payload), row[key]))
        event_names = {
            "slice_infra_error": "round_infra_error",
            "slice_interrupted": "round_interrupted",
            "slice_discarded": "round_discarded",
        }
        for old, new in event_names.items():
            db.execute("UPDATE events SET event=? WHERE event=?", (new, old))
        db.execute("UPDATE call_reservations SET component='worker_round' WHERE component='worker_slice'")

    def _migrate_fact_submission_schema(self, db: sqlite3.Connection) -> None:
        if "submission_id" not in {row[1] for row in db.execute("PRAGMA table_info(pending_facts)")}:
            db.executescript("""
                ALTER TABLE pending_facts RENAME TO pending_facts_v3;
                CREATE TABLE pending_facts(
                    submission_id TEXT PRIMARY KEY, fact_id TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL);
                INSERT INTO pending_facts SELECT fact_id,fact_id,payload,status FROM pending_facts_v3;
                DROP TABLE pending_facts_v3;
                CREATE INDEX pending_facts_fact ON pending_facts(fact_id, status);
            """)
        if not any(row[1] == "assignment_epoch" and row[5] for row in db.execute("PRAGMA table_info(fact_scopes)")):
            db.executescript("""
                ALTER TABLE fact_scopes RENAME TO fact_scopes_v3;
                CREATE TABLE fact_scopes(
                    fact_id TEXT NOT NULL, target_version TEXT NOT NULL, obligation_id TEXT NOT NULL,
                    route_id TEXT NOT NULL, assignment_epoch TEXT NOT NULL, claim_role TEXT NOT NULL,
                    relation TEXT NOT NULL, event_seq INTEGER,
                    PRIMARY KEY(fact_id,target_version,obligation_id,route_id,assignment_epoch,relation));
                INSERT INTO fact_scopes SELECT * FROM fact_scopes_v3;
                DROP TABLE fact_scopes_v3;
                CREATE INDEX fact_scopes_route ON fact_scopes(route_id, relation);
                CREATE INDEX fact_scopes_obligation ON fact_scopes(obligation_id, relation);
            """)

    def _migrate_files_once(self) -> None:
        db = self._connect()
        try:
            if db.execute("SELECT value FROM meta WHERE key='files_imported'").fetchone():
                return
        finally:
            db.close()
        events = read_jsonl(self.events_file)
        target_state: dict[str, str] = {}
        obligation_state: dict[str, str] = {}
        route_state: dict[str, str] = {}
        for row in events:
            version = row.get("target_version")
            kind = row.get("event")
            if kind == "target_approved" and version:
                target_state[str(version)] = "approved"
            elif kind == "target_superseded" and version:
                target_state[str(version)] = "superseded"
            elif kind == "target_rejected" and version:
                target_state[str(version)] = "rejected"
            elif kind == "obligation_state" and row.get("obligation_id"):
                obligation_state[str(row["obligation_id"])] = str(row.get("state") or "open")
            elif kind == "route_state" and row.get("route_id"):
                route_state[str(row["route_id"])] = str(row.get("state") or "proposed")
        with self._tx() as db:
            for path in sorted(self.targets.glob("v*.json")):
                value = _read_json(path)
                db.execute("INSERT OR IGNORE INTO targets VALUES (?,?,?,1)", (value["version"], target_state.get(value["version"], "draft"), _dump(value)))
            for path in sorted(self.obligations.glob("*.json")):
                value = _read_json(path)
                db.execute("INSERT OR IGNORE INTO obligations VALUES (?,?,?,?)", (value["id"], value["target_version"], obligation_state.get(value["id"], "open"), _dump(value)))
                for dependency in value.get("dependencies") or []:
                    db.execute("INSERT OR IGNORE INTO obligation_edges VALUES (?,?)", (dependency, value["id"]))
            for path in sorted(self.routes.glob("*.json")):
                value = _read_json(path)
                method_title = str(value.get("method_title") or value.get("method_family") or "method")
                method_key = str(value.get("method_key") or self._method_key(method_title))
                value.update(method_key=method_key, method_title=method_title)
                db.execute("INSERT OR IGNORE INTO routes VALUES (?,?,?,?,?,?,?,?)", (value["id"], value["target_version"], value["obligation_id"], route_state.get(value["id"], "proposed"), method_key, method_title, value["signature"], _dump(value)))
                for fact_id in value.get("input_fact_ids") or []:
                    db.execute("INSERT OR IGNORE INTO route_inputs VALUES (?,?)", (value["id"], fact_id))
            for path in sorted(self.assignments.glob("*.json")):
                value = self._round_payload(_read_json(path))
                db.execute("INSERT OR REPLACE INTO assignments VALUES (?,?,?,?,?,?,?)", (value["worker"], value["epoch"], value["status"], value["target_version"], value["obligation_id"], value["route_id"], _dump(value)))
            for row in events:
                record = dict(row)
                event_id = str(record.pop("event_id", uuid.uuid4().hex))
                timestamp = str(record.pop("timestamp_utc", utc_now()))
                kind = str(record.pop("event", "legacy_event"))
                if kind.startswith("slice_"):
                    kind = f"round_{kind.removeprefix('slice_')}"
                record = self._round_payload(record)
                db.execute("INSERT OR IGNORE INTO events(event_id,timestamp_utc,event,payload) VALUES (?,?,?,?)", (event_id, timestamp, kind, _dump(record)))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('files_imported',?)", (utc_now(),))
            self._bump(db)
        if any((self.targets.exists(), self.events_file.exists())):
            marker = self.dir / "MIGRATED_TO_SQLITE"
            marker.write_text("control.sqlite3 is authoritative; legacy files are import-only\n", encoding="utf-8")
        self.rebuild_read_model()

    @staticmethod
    def _method_key(title: str) -> str:
        key = _METHOD_KEY.sub("-", title.lower()).strip("-")
        return key[:64] or f"method-{hashlib.sha256(title.encode('utf-8')).hexdigest()[:10]}"

    def _bump(self, db: sqlite3.Connection) -> int:
        current = int(db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]) + 1
        db.execute("UPDATE meta SET value=? WHERE key='generation'", (str(current),))
        return current

    def generation(self) -> int:
        self.scaffold()
        with self._connect() as db:
            return int(db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0])

    def _event(self, db: sqlite3.Connection, event: str, **payload: Any) -> dict[str, Any]:
        record = {"event_id": uuid.uuid4().hex, "timestamp_utc": utc_now(), "event": event, **payload}
        body = {k: v for k, v in record.items() if k not in {"event_id", "timestamp_utc", "event"}}
        cur = db.execute("INSERT INTO events(event_id,timestamp_utc,event,payload) VALUES (?,?,?,?)", (record["event_id"], record["timestamp_utc"], event, _dump(body)))
        record["seq"] = cur.lastrowid
        return record

    def append_event(self, event: str, **payload: Any) -> dict[str, Any]:
        self.scaffold()
        with self._tx() as db:
            record = self._event(db, event, **payload)
            self._bump(db)
            return record

    def events(self, event: Optional[str] = None) -> list[dict[str, Any]]:
        self.scaffold()
        sql = "SELECT * FROM events" + (" WHERE event=?" if event else "") + " ORDER BY seq"
        args = (event,) if event else ()
        with self._connect() as db:
            rows = db.execute(sql, args).fetchall()
        return [{"seq": row["seq"], "event_id": row["event_id"], "timestamp_utc": row["timestamp_utc"], "event": row["event"], **_load(row["payload"], {})} for row in rows]

    # --------------------------------------------------------------- targets
    def target_versions(self) -> list[str]:
        self.scaffold()
        with self._connect() as db:
            return [row[0] for row in db.execute("SELECT version FROM targets ORDER BY version")]

    def target(self, version: str) -> dict[str, Any]:
        self.scaffold()
        with self._connect() as db:
            row = db.execute("SELECT payload FROM targets WHERE version=?", (_id(version, "target version"),)).fetchone()
        if not row:
            raise ControlError(f"unknown target version: {version}")
        return _load(row[0], {})

    def target_state(self, version: str) -> str:
        self.scaffold()
        with self._connect() as db:
            row = db.execute("SELECT state FROM targets WHERE version=?", (_id(version, "target version"),)).fetchone()
        if not row:
            raise ControlError(f"unknown target version: {version}")
        return str(row[0])

    def current_target_version(self) -> Optional[str]:
        self.scaffold()
        with self._connect() as db:
            row = db.execute("SELECT version FROM targets WHERE state='approved' ORDER BY version DESC LIMIT 1").fetchone()
        return str(row[0]) if row else None

    def current_target(self) -> Optional[dict[str, Any]]:
        version = self.current_target_version()
        return self.target(version) if version else None

    def propose_target(self, contract: dict[str, Any], *, proposed_by: str = "operator") -> dict[str, Any]:
        self.scaffold()
        versions = self.target_versions()
        version = f"v{(int(versions[-1][1:]) + 1 if versions else 1):04d}"
        statement = str(contract.get("statement") or "").strip()
        if not statement:
            raise ControlError("TargetContract.statement is required")
        allowed = _strings(contract.get("allowed_assumptions"), "allowed_assumptions")
        forbidden = _strings(contract.get("forbidden_assumptions"), "forbidden_assumptions")
        if set(allowed) & set(forbidden):
            raise ControlError("assumptions cannot be both allowed and forbidden")
        conclusions = contract.get("required_conclusions")
        if not isinstance(conclusions, list) or not conclusions:
            raise ControlError("required_conclusions must be a non-empty list")
        normalized = []
        for index, item in enumerate(conclusions, 1):
            item = {"id": f"root-{index}", "statement": item} if isinstance(item, str) else item
            if not isinstance(item, dict) or not str(item.get("statement") or "").strip():
                raise ControlError("each required conclusion needs a statement")
            normalized.append({"id": _id(str(item.get("id") or f"root-{index}"), "conclusion id"), "statement": str(item["statement"]).strip()})
        problem = self.project / "PROBLEM.md"
        payload = {
            "version": version, "statement": statement, "allowed_assumptions": allowed,
            "forbidden_assumptions": forbidden, "required_conclusions": normalized,
            "acceptance": contract.get("acceptance") or "all required conclusions are closed",
            "out_of_scope": _strings(contract.get("out_of_scope"), "out_of_scope"),
            "fallback_candidates": contract.get("fallback_candidates") or [],
            "budget": contract.get("budget") or {},
            "problem_sha256": hashlib.sha256(problem.read_bytes()).hexdigest() if problem.is_file() else None,
            "created_at_utc": utc_now(), "proposed_by": proposed_by,
        }
        with self._tx() as db:
            db.execute("INSERT INTO targets VALUES (?,?,?,1)", (version, "draft", _dump(payload)))
            self._event(db, "target_proposed", target_version=version, actor=proposed_by)
            self._bump(db)
        return payload

    def _invalidate_assignments(self, db: sqlite3.Connection, reason: str) -> list[str]:
        workers = []
        for row in db.execute("SELECT worker,status,payload FROM assignments WHERE status!='stale'"):
            value = _load(row["payload"], {})
            value.update(status="stale", stale_reason=reason)
            db.execute("UPDATE assignments SET status='stale',payload=? WHERE worker=?", (_dump(value), row["worker"]))
            self._event(db, "assignment_stale", worker=row["worker"], epoch=value["epoch"], reason=reason)
            if row["status"] in {"assigned", "running", "auditing"}:
                workers.append(str(row["worker"]))
                db.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?)", (uuid.uuid4().hex, "stop_worker", _dump({"worker": row["worker"], "force": True}), "pending", 0, None, utc_now()))
        return workers

    def _approve(self, db: sqlite3.Connection, version: str, actor: str) -> dict[str, Any]:
        row = db.execute("SELECT state,payload FROM targets WHERE version=?", (version,)).fetchone()
        if not row:
            raise ControlError(f"unknown target version: {version}")
        if row["state"] != "draft":
            raise ControlError(f"target {version} is not a draft")
        old = db.execute("SELECT version FROM targets WHERE state='approved' LIMIT 1").fetchone()
        if old:
            db.execute("UPDATE targets SET state='superseded',revision=revision+1 WHERE version=?", (old[0],))
            self._event(db, "target_superseded", target_version=old[0], replacement=version, actor=actor)
        stale = self._invalidate_assignments(db, f"target changed to {version}")
        db.execute("UPDATE targets SET state='approved',revision=revision+1 WHERE version=?", (version,))
        self._event(db, "target_approved", target_version=version, actor=actor)
        target = _load(row["payload"], {})
        for conclusion in target["required_conclusions"]:
            oid = f"{version}-{conclusion['id']}"
            payload = {"id": oid, "target_version": version, "statement": conclusion["statement"], "kind": "root", "dependencies": [], "closure": "verified unconditional fact matching the statement", "created_at_utc": utc_now()}
            db.execute("INSERT OR IGNORE INTO obligations VALUES (?,?,?,?)", (oid, version, "open", _dump(payload)))
            self._event(db, "obligation_added", obligation_id=oid, target_version=version, actor=actor)
            self._event(db, "obligation_state", obligation_id=oid, target_version=version, state="open", actor=actor)
        return {"target": target, "stale_workers": stale}

    def approve_target(self, version: str, *, approved_by: str = "operator", request_id: Optional[str] = None, expected_generation: Optional[int] = None) -> dict[str, Any]:
        self.scaffold()
        version = _id(version, "target version")
        with self._tx() as db:
            if request_id:
                prior = db.execute("SELECT action,result FROM requests WHERE request_id=?", (request_id,)).fetchone()
                if prior:
                    if prior["action"] != f"target_approve:{version}":
                        raise ControlError("request_id was already used for a different action")
                    return _load(prior["result"], {})
            generation = int(db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0])
            if expected_generation is not None and expected_generation != generation:
                raise ControlError(f"stale generation: expected {expected_generation}, current {generation}")
            result = self._approve(db, version, approved_by)
            result["generation"] = self._bump(db)
            if request_id:
                db.execute("INSERT INTO requests VALUES (?,?,?)", (request_id, f"target_approve:{version}", _dump(result)))
            return result

    def withdraw_target(self, version: str, *, reason: str, actor: str = "operator", request_id: Optional[str] = None, expected_generation: Optional[int] = None) -> dict[str, Any]:
        self.scaffold()
        if not reason.strip():
            raise ControlError("withdraw reason is required")
        with self._tx() as db:
            if request_id:
                prior = db.execute("SELECT action,result FROM requests WHERE request_id=?", (request_id,)).fetchone()
                if prior:
                    if prior["action"] != f"target_withdraw:{version}":
                        raise ControlError("request_id was already used for a different action")
                    return _load(prior["result"], {})
            generation = int(db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0])
            if expected_generation is not None and generation != expected_generation:
                raise ControlError(f"stale generation: expected {expected_generation}, current {generation}")
            row = db.execute("SELECT state,payload FROM targets WHERE version=?", (version,)).fetchone()
            if not row or row["state"] != "approved":
                raise ControlError(f"target {version} is not approved")
            stale = self._invalidate_assignments(db, f"target {version} withdrawn: {reason.strip()}")
            db.execute("UPDATE targets SET state='withdrawn',revision=revision+1 WHERE version=?", (version,))
            self._event(db, "target_withdrawn", target_version=version, actor=actor, reason=reason.strip())
            result = {"target": _load(row["payload"], {}), "stale_workers": stale, "generation": self._bump(db)}
            if request_id:
                db.execute("INSERT INTO requests VALUES (?,?,?)", (request_id, f"target_withdraw:{version}", _dump(result)))
            return result

    def target_diff(self, version: str, against: Optional[str] = None) -> str:
        current = against or self.current_target_version()
        before = self.target(current) if current else {}
        after = self.target(version)
        a = json.dumps(before, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
        b = json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
        return "\n".join(difflib.unified_diff(a, b, fromfile=current or "none", tofile=version, lineterm=""))

    def propose_fallback(self, *, proposed_by: str = "system") -> dict[str, Any]:
        current = self.current_target()
        if not current or not current.get("fallback_candidates"):
            raise ControlError("the approved target has no explicit fallback_candidates")
        candidate = current["fallback_candidates"][0]
        candidate = {"statement": candidate, "required_conclusions": [candidate]} if isinstance(candidate, str) else candidate
        draft = {**current, **candidate}
        for key in ("version", "created_at_utc", "problem_sha256", "proposed_by"):
            draft.pop(key, None)
        proposed = self.propose_target(draft, proposed_by=proposed_by)
        self.append_event("target_fallback_drafted", target_version=proposed["version"], from_target=current["version"], requires_human_approval=True)
        stale = self.invalidate_assignments(reason=f"target fallback draft {proposed['version']} requires approval")
        return {"target": proposed, "stale_workers": stale}

    # ------------------------------------------------------------ obligations
    def add_obligation(self, value: dict[str, Any], *, actor: str = "main") -> dict[str, Any]:
        self.scaffold()
        oid = _id(str(value.get("id") or ""), "obligation id")
        target_version = _id(str(value.get("target_version") or self.current_target_version() or ""), "target version")
        self.target(target_version)
        statement = str(value.get("statement") or "").strip()
        if not statement:
            raise ControlError("obligation statement is required")
        dependencies = [_id(item, "obligation dependency") for item in _strings(value.get("dependencies"), "dependencies")]
        payload = {"id": oid, "target_version": target_version, "statement": statement, "kind": str(value.get("kind") or "subgoal"), "dependencies": dependencies, "closure": str(value.get("closure") or "verified fact matching the statement"), "created_at_utc": utc_now()}
        with self._tx() as db:
            for dependency in dependencies:
                if not db.execute("SELECT 1 FROM obligations WHERE id=?", (dependency,)).fetchone():
                    raise ControlError(f"unknown obligation dependency: {dependency}")
            existing = db.execute("SELECT payload FROM obligations WHERE id=?", (oid,)).fetchone()
            if existing:
                return _load(existing[0], {})
            db.execute("INSERT INTO obligations VALUES (?,?,?,?)", (oid, target_version, "open", _dump(payload)))
            for dependency in dependencies:
                db.execute("INSERT INTO obligation_edges VALUES (?,?)", (dependency, oid))
            self._event(db, "obligation_added", obligation_id=oid, target_version=target_version, actor=actor)
            self._event(db, "obligation_state", obligation_id=oid, target_version=target_version, state="open", actor=actor)
            self._bump(db)
        return payload

    def obligation(self, oid: str) -> dict[str, Any]:
        self.scaffold()
        with self._connect() as db:
            row = db.execute("SELECT payload FROM obligations WHERE id=?", (_id(oid, "obligation id"),)).fetchone()
        if not row:
            raise ControlError(f"unknown obligation: {oid}")
        return _load(row[0], {})

    def list_obligations(self, target_version: Optional[str] = None) -> list[dict[str, Any]]:
        self.scaffold()
        sql = "SELECT state,payload FROM obligations" + (" WHERE target_version=?" if target_version else "") + " ORDER BY id"
        with self._connect() as db:
            rows = db.execute(sql, (target_version,) if target_version else ()).fetchall()
        return [{**_load(row["payload"], {}), "state": row["state"]} for row in rows]

    def obligation_state(self, oid: str) -> str:
        self.scaffold()
        with self._connect() as db:
            row = db.execute("SELECT state FROM obligations WHERE id=?", (_id(oid, "obligation id"),)).fetchone()
        if not row:
            raise ControlError(f"unknown obligation: {oid}")
        return str(row[0])

    def _set_obligation_state(self, db: sqlite3.Connection, oid: str, state: str, *, actor: str, fact_id: Optional[str] = None, assignment_epoch: Optional[str] = None) -> None:
        if state not in {"open", "active", "closed", "blocked", "refuted", "superseded"}:
            raise ControlError(f"invalid obligation state: {state}")
        row = db.execute("SELECT target_version FROM obligations WHERE id=?", (oid,)).fetchone()
        if not row:
            raise ControlError(f"unknown obligation: {oid}")
        db.execute("UPDATE obligations SET state=? WHERE id=?", (state, oid))
        route_id = ""
        claim_role = "unconditional"
        if fact_id:
            scope = db.execute(
                "SELECT route_id,claim_role FROM fact_scopes WHERE fact_id=? AND obligation_id=? ORDER BY event_seq DESC LIMIT 1",
                (fact_id, oid),
            ).fetchone()
            if scope:
                route_id, claim_role = str(scope[0]), str(scope[1])
        event = self._event(db, "obligation_state", obligation_id=oid, target_version=row[0], state=state, actor=actor, fact_id=fact_id, assignment_epoch=assignment_epoch, route_id=route_id, claim_role=claim_role)
        if fact_id and state in {"closed", "refuted"}:
            db.execute(
                "INSERT OR IGNORE INTO fact_scopes VALUES (?,?,?,?,?,?,?,?)",
                (fact_id, row[0], oid, route_id, assignment_epoch or "", claim_role, "closing", event["seq"]),
            )

    def set_obligation_state(self, oid: str, state: str, *, actor: str, fact_id: Optional[str] = None, assignment_epoch: Optional[str] = None) -> None:
        self.scaffold()
        with self._tx() as db:
            self._set_obligation_state(db, oid, state, actor=actor, fact_id=fact_id, assignment_epoch=assignment_epoch)
            self._bump(db)

    def dependencies_closed(self, oid: str) -> bool:
        self.scaffold()
        with self._connect() as db:
            rows = db.execute("SELECT o.state FROM obligation_edges e JOIN obligations o ON o.id=e.dependency_id WHERE e.obligation_id=?", (oid,)).fetchall()
        return all(row[0] == "closed" for row in rows)

    # ---------------------------------------------------------------- routes
    @staticmethod
    def route_signature(value: dict[str, Any]) -> str:
        basis = {"target_version": value.get("target_version"), "obligation_id": value.get("obligation_id"), "method_key": value.get("method_key") or value.get("method_family"), "assumptions": sorted(value.get("assumptions") or []), "input_fact_ids": sorted(value.get("input_fact_ids") or []), "expected_result": value.get("expected_result")}
        return hashlib.sha256(_json_bytes(basis)).hexdigest()[:24]

    def add_route(self, value: dict[str, Any], *, actor: str = "main") -> dict[str, Any]:
        self.scaffold()
        rid = _id(str(value.get("id") or ""), "route id")
        oid = _id(str(value.get("obligation_id") or ""), "obligation id")
        obligation = self.obligation(oid)
        target_version = _id(str(value.get("target_version") or obligation["target_version"]), "target version")
        method_title = str(value.get("method_title") or value.get("method_family") or "").strip()
        method_key = _id(str(value.get("method_key") or self._method_key(method_title)), "method key")
        expected = str(value.get("expected_result") or "").strip()
        if not method_title or not expected:
            raise ControlError("route method_title/method_family and expected_result are required")
        payload = {"id": rid, "target_version": target_version, "obligation_id": oid, "method_key": method_key, "method_title": method_title, "method_family": method_title, "assumptions": _strings(value.get("assumptions"), "route assumptions"), "input_fact_ids": _strings(value.get("input_fact_ids"), "input_fact_ids"), "expected_result": expected, "novelty_basis": _strings(value.get("novelty_basis"), "novelty_basis"), "fallback_route_ids": _strings(value.get("fallback_route_ids"), "fallback_route_ids"), "created_at_utc": utc_now()}
        payload["signature"] = self.route_signature(payload)
        with self._tx() as db:
            duplicate = db.execute("SELECT id FROM routes WHERE signature=?", (payload["signature"],)).fetchone()
            if duplicate and not payload["novelty_basis"]:
                raise ControlError(f"duplicate route without novelty_basis: {duplicate[0]}")
            db.execute("INSERT INTO routes VALUES (?,?,?,?,?,?,?,?)", (rid, target_version, oid, "proposed", method_key, method_title, payload["signature"], _dump(payload)))
            for fact_id in payload["input_fact_ids"]:
                db.execute("INSERT INTO route_inputs VALUES (?,?)", (rid, fact_id))
                db.execute(
                    "INSERT OR IGNORE INTO fact_scopes VALUES (?,?,?,?,?,'input','input',NULL)",
                    (fact_id, target_version, oid, rid, ""),
                )
            self._event(db, "route_added", route_id=rid, target_version=target_version, obligation_id=oid, signature=payload["signature"], actor=actor)
            self._event(db, "route_state", route_id=rid, obligation_id=oid, target_version=target_version, state="proposed", reason="", actor=actor)
            self._bump(db)
        return payload

    def route(self, rid: str) -> dict[str, Any]:
        self.scaffold()
        with self._connect() as db:
            row = db.execute("SELECT payload FROM routes WHERE id=?", (_id(rid, "route id"),)).fetchone()
        if not row:
            raise ControlError(f"unknown route: {rid}")
        return _load(row[0], {})

    def list_routes(self, target_version: Optional[str] = None) -> list[dict[str, Any]]:
        self.scaffold()
        sql = "SELECT state,payload FROM routes" + (" WHERE target_version=?" if target_version else "") + " ORDER BY id"
        with self._connect() as db:
            rows = db.execute(sql, (target_version,) if target_version else ()).fetchall()
        return [{**_load(row["payload"], {}), "state": row["state"]} for row in rows]

    def route_state(self, rid: str) -> str:
        self.scaffold()
        with self._connect() as db:
            row = db.execute("SELECT state FROM routes WHERE id=?", (_id(rid, "route id"),)).fetchone()
        if not row:
            raise ControlError(f"unknown route: {rid}")
        return str(row[0])

    def _set_route_state(self, db: sqlite3.Connection, rid: str, state: str, *, actor: str, reason: str = "") -> None:
        if state not in {"proposed", "active", "stalled", "failed", "refuted", "succeeded", "superseded"}:
            raise ControlError(f"invalid route state: {state}")
        row = db.execute("SELECT target_version,obligation_id FROM routes WHERE id=?", (rid,)).fetchone()
        if not row:
            raise ControlError(f"unknown route: {rid}")
        db.execute("UPDATE routes SET state=? WHERE id=?", (state, rid))
        self._event(db, "route_state", route_id=rid, obligation_id=row["obligation_id"], target_version=row["target_version"], state=state, reason=reason, actor=actor)

    def set_route_state(self, rid: str, state: str, *, actor: str, reason: str = "") -> None:
        self.scaffold()
        with self._tx() as db:
            self._set_route_state(db, rid, state, actor=actor, reason=reason)
            self._bump(db)

    # ------------------------------------------------------------ assignments
    def assignment(self, worker: str) -> Optional[dict[str, Any]]:
        self.scaffold()
        with self._connect() as db:
            row = db.execute("SELECT payload FROM assignments WHERE worker=?", (_id(worker, "worker"),)).fetchone()
        return _load(row[0], {}) if row else None

    def assign(
        self, worker: str, *, obligation_id: str, route_id: str, task: str,
        max_rounds: int = MAX_ROUTE_ROUNDS,
        round_timeout_seconds: int = DEFAULT_ROUND_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        self.scaffold()
        target_version = self.current_target_version()
        if not target_version:
            raise ControlError("no approved target")
        obligation = self.obligation(obligation_id)
        route = self.route(route_id)
        if obligation["target_version"] != target_version or route["target_version"] != target_version or route["obligation_id"] != obligation_id:
            raise ControlError("obligation or route is not bound to the current target")
        if not task.strip():
            raise ControlError("assignment task is required")
        payload = {"worker": _id(worker, "worker"), "epoch": uuid.uuid4().hex, "target_version": target_version, "obligation_id": obligation_id, "route_id": route_id, "task": task.strip(), "status": "assigned", "rounds_used": 0, "rounds_remaining": INITIAL_ROUNDS, "max_rounds": max(3, int(max_rounds)), "round_timeout_seconds": max(1, int(round_timeout_seconds)), "consecutive_low": 0, "audit_required": False, "wall_seconds": 0.0, "infra_failure_count": 0, "infra_wall_seconds": 0.0, "infra_outage_wall_seconds": 0.0, "next_retry_at_epoch": None, "last_failure_class": None, "last_error_signature": None, "last_unresolved_interfaces": None, "credited_evidence_refs": [], "event_cursor": len(self.events()), "context_cursor": self.generation(), "assigned_at_utc": utc_now()}
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO assignments VALUES (?,?,?,?,?,?,?)", (payload["worker"], payload["epoch"], payload["status"], target_version, obligation_id, route_id, _dump(payload)))
            self._set_route_state(db, route_id, "active", actor="assignment")
            self._set_obligation_state(db, obligation_id, "active", actor="assignment")
            self._event(db, "assignment_created", **payload)
            self._bump(db)
        return payload

    def save_assignment(self, assignment: dict[str, Any]) -> None:
        self.scaffold()
        with self._tx() as db:
            changed = db.execute(
                "UPDATE assignments SET status=?,payload=? WHERE worker=? AND epoch=?",
                (assignment["status"], _dump(assignment), assignment["worker"], assignment["epoch"]),
            ).rowcount
            if not changed:
                raise ControlError("assignment changed during update")
            self._bump(db)

    def invalidate_assignments(self, *, reason: str) -> list[str]:
        self.scaffold()
        with self._tx() as db:
            workers = self._invalidate_assignments(db, reason)
            self._bump(db)
            return workers

    def validate_assignment(self, worker: str, *, reservation_id: Optional[str] = None) -> dict[str, Any]:
        assignment = self.assignment(worker)
        if not assignment:
            raise ControlError(f"worker {worker} has no v2 assignment")
        if assignment.get("status") not in {"assigned", "running", "auditing"}:
            raise ControlError(f"assignment is not runnable: {assignment.get('status')}")
        if assignment["target_version"] != self.current_target_version():
            raise ControlError("assignment target is stale")
        if int(assignment["rounds_used"]) >= int(assignment["max_rounds"]):
            raise ControlError("route round budget exhausted")
        if reservation_id:
            with self._connect() as db:
                row = db.execute(
                    "SELECT scope FROM call_reservations WHERE id=? AND component='worker_round' AND status='active' AND expires_at_epoch>?",
                    (reservation_id, time.time()),
                ).fetchone()
            reservation_scope = _load(row[0], {}) if row else {}
            if (
                reservation_scope.get("worker") != worker
                or reservation_scope.get("assignment_epoch") != assignment["epoch"]
            ):
                raise ControlError("worker-round reservation does not match the assignment")
        budget = self.budget_state(exclude_reservation_id=reservation_id)
        if budget["stage"] == "exhausted":
            assignment["status"] = "budget_exhausted"
            self.save_assignment(assignment)
            raise ControlError("project budget exhausted")
        if budget["stage"] == "audit" and not assignment.get("audit_required"):
            assignment.update(audit_required=True, status="auditing")
            self.save_assignment(assignment)
        return assignment

    def claim_backend_call(self, provider_key: str = "codex") -> dict[str, Any]:
        """Atomically admit normal calls or one half-open probe after a cooldown."""
        self.scaffold()
        now = time.time()
        with self._tx() as db:
            row = db.execute("SELECT * FROM backend_circuits WHERE provider_key=?", (provider_key,)).fetchone()
            if not row or row["state"] == "closed":
                return {"allowed": True, "state": "closed", "wait_seconds": 0}
            if row["state"] == "blocked":
                return {"allowed": False, "state": "blocked", "wait_seconds": None, "failure_class": row["failure_class"]}
            opened_until = float(row["opened_until"] or 0)
            if row["state"] == "open" and opened_until <= now:
                db.execute("UPDATE backend_circuits SET state='half_open',updated_at_utc=? WHERE provider_key=?", (utc_now(), provider_key))
                self._bump(db)
                return {"allowed": True, "state": "half_open", "wait_seconds": 0}
            wait = max(1, int(opened_until - now)) if opened_until else 1
            return {"allowed": False, "state": str(row["state"]), "wait_seconds": wait, "failure_class": row["failure_class"]}

    def backend_circuits(self) -> list[dict[str, Any]]:
        self.scaffold()
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM backend_circuits ORDER BY provider_key")]

    def cancel_backend_probe(self, provider_key: str = "codex") -> None:
        """Release a half-open claim when local preflight prevents the actual call."""
        self.scaffold()
        with self._tx() as db:
            changed = db.execute("UPDATE backend_circuits SET state='open',opened_until=?,updated_at_utc=? WHERE provider_key=? AND state='half_open'", (time.time(), utc_now(), provider_key)).rowcount
            if changed:
                self._bump(db)

    def active_call_reservations(self) -> list[dict[str, Any]]:
        self.scaffold()
        with self._connect() as db:
            return [dict(row) | {"scope": _load(row["scope"], {})} for row in db.execute("SELECT * FROM call_reservations WHERE status='active' AND expires_at_epoch>? ORDER BY created_at_utc", (time.time(),))]

    def _infra_policy(self, failure_class: str) -> tuple[int, float, list[int]]:
        budget = (self.current_target() or {}).get("budget") or {}
        attempts = max(1, int(budget.get("max_infra_attempts", 3)))
        if failure_class == "timeout":
            attempts = min(attempts, 2)
        if budget.get("max_infra_wall_seconds") is not None:
            wall_limit = max(1.0, float(budget["max_infra_wall_seconds"]))
        else:
            try:
                wall_limit = min(1800.0, max(1.0, float(budget.get("max_wall_seconds")) * .05))
            except (TypeError, ValueError):
                wall_limit = 1800.0
        raw = budget.get("infra_retry_seconds") or [30, 120, 600]
        retry = [max(0, int(value)) for value in raw] if isinstance(raw, list) and raw else [30, 120, 600]
        return attempts, wall_limit, retry

    def record_worker_infra_failure(self, worker: str, outcome: dict[str, Any], *, wall_seconds: float, usage: Optional[dict[str, Any]] = None, provider_key: str = "codex", reservation_id: Optional[str] = None) -> dict[str, Any]:
        """Persist retry/circuit state and actual cost without consuming a research round."""
        self.scaffold()
        with self._tx() as db:
            row = db.execute("SELECT payload FROM assignments WHERE worker=?", (_id(worker, "worker"),)).fetchone()
            if not row:
                raise ControlError(f"worker {worker} has no v2 assignment")
            assignment = _load(row[0], {})
            reservation_scope: dict[str, Any] = {}
            if reservation_id:
                reservation = db.execute(
                    "SELECT scope FROM call_reservations WHERE id=? AND status='active'",
                    (reservation_id,),
                ).fetchone()
                if reservation:
                    reservation_scope = _load(reservation["scope"], {})
            assignment_changed = bool(
                reservation_scope.get("assignment_epoch")
                and reservation_scope["assignment_epoch"] != assignment["epoch"]
            )
            failure_class = str(outcome.get("failure_class") or "unknown_infra")
            attempts_limit, wall_limit, retry_schedule = self._infra_policy(failure_class)
            circuit = db.execute("SELECT consecutive_failures,infra_wall_seconds FROM backend_circuits WHERE provider_key=?", (provider_key,)).fetchone()
            attempts = (
                int(circuit["consecutive_failures"] if circuit else 0) + 1
                if assignment_changed
                else int(assignment.get("infra_failure_count") or 0) + 1
            )
            infra_wall = float(assignment.get("infra_wall_seconds") or 0) + max(0.0, wall_seconds)
            outage_wall = float(assignment.get("infra_outage_wall_seconds") or 0) + max(0.0, wall_seconds)
            provider_outage_wall = float(circuit["infra_wall_seconds"] if circuit else 0) + max(0.0, wall_seconds)
            retryable = bool(outcome.get("retryable"))
            blocked = not retryable or attempts >= attempts_limit or (not assignment_changed and outage_wall >= wall_limit) or provider_outage_wall >= wall_limit
            requested_wait = max(0, int(outcome.get("retry_after_seconds") or 0))
            wait = max(requested_wait, retry_schedule[min(attempts - 1, len(retry_schedule) - 1)])
            next_retry = None if blocked else time.time() + wait
            if not assignment_changed:
                assignment.update(
                    status="infra_blocked" if blocked else "waiting_retry",
                    infra_failure_count=attempts,
                    infra_wall_seconds=infra_wall,
                    infra_outage_wall_seconds=outage_wall,
                    next_retry_at_epoch=next_retry,
                    last_failure_class=failure_class,
                    last_error_signature=outcome.get("error_signature"),
                )
                db.execute(
                    "UPDATE assignments SET status=?,payload=? WHERE worker=? AND epoch=?",
                    (assignment["status"], _dump(assignment), worker, assignment["epoch"]),
                )
            circuit_state = "blocked" if blocked else "open"
            db.execute(
                "INSERT INTO backend_circuits(provider_key,state,consecutive_failures,opened_until,failure_class,infra_wall_seconds,updated_at_utc) VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider_key) DO UPDATE SET state=excluded.state,consecutive_failures=backend_circuits.consecutive_failures+1,opened_until=excluded.opened_until,failure_class=excluded.failure_class,infra_wall_seconds=backend_circuits.infra_wall_seconds+excluded.infra_wall_seconds,updated_at_utc=excluded.updated_at_utc",
                (provider_key, circuit_state, 1, next_retry, failure_class, max(0.0, wall_seconds), utc_now()),
            )
            event_scope = {
                "assignment_epoch": reservation_scope.get("assignment_epoch") or assignment["epoch"],
                "target_version": reservation_scope.get("target_version") or assignment["target_version"],
                "obligation_id": reservation_scope.get("obligation_id") or assignment["obligation_id"],
                "route_id": reservation_scope.get("route_id") or assignment["route_id"],
            }
            event = self._event(db, "round_infra_error", worker=worker, failure_class=failure_class, retryable=retryable, retry_at_epoch=next_retry, error_signature=outcome.get("error_signature"), return_code=outcome.get("return_code"), blocked=blocked, assignment_changed=assignment_changed, **event_scope)
            self._record_cost(db, component="worker_infra", wall_seconds=wall_seconds, usage=usage, reservation_id=reservation_id, worker=worker, failure_class=failure_class, attempt_status="failed", **event_scope)
            self._bump(db)
        self._record_budget_threshold()
        return {"assignment": assignment, "event": event, "blocked": blocked, "wait_seconds": None if blocked else wait, "assignment_changed": assignment_changed}

    def resume_worker_retry(self, worker: str) -> dict[str, Any]:
        assignment = self.assignment(worker)
        if not assignment or assignment.get("status") != "waiting_retry":
            raise ControlError(f"worker {worker} is not waiting for retry")
        assignment["status"] = "running"
        self.save_assignment(assignment)
        return assignment

    def record_worker_call_success(self, worker: str, provider_key: str = "codex", *, assignment_epoch: Optional[str] = None) -> None:
        self.scaffold()
        with self._tx() as db:
            sql = "SELECT payload FROM assignments WHERE worker=?"
            params: tuple[Any, ...] = (_id(worker, "worker"),)
            if assignment_epoch:
                sql += " AND epoch=?"
                params += (assignment_epoch,)
            row = db.execute(sql, params).fetchone()
            circuit = db.execute("SELECT state FROM backend_circuits WHERE provider_key=?", (provider_key,)).fetchone()
            if row:
                current = _load(row[0], {})
                if not current.get("infra_failure_count") and (not circuit or circuit["state"] == "closed"):
                    return
            if row:
                assignment = current
                assignment.update(infra_failure_count=0, infra_outage_wall_seconds=0.0, next_retry_at_epoch=None, last_failure_class=None, last_error_signature=None)
                db.execute(
                    "UPDATE assignments SET payload=? WHERE worker=? AND epoch=?",
                    (_dump(assignment), worker, assignment["epoch"]),
                )
            db.execute("INSERT INTO backend_circuits(provider_key,state,consecutive_failures,opened_until,failure_class,infra_wall_seconds,updated_at_utc) VALUES (?, 'closed', 0, NULL, NULL, 0, ?) ON CONFLICT(provider_key) DO UPDATE SET state='closed',consecutive_failures=0,opened_until=NULL,failure_class=NULL,infra_wall_seconds=0,updated_at_utc=excluded.updated_at_utc", (provider_key, utc_now()))
            if circuit and circuit["state"] != "closed":
                self._event(db, "backend_recovered", provider_key=provider_key, worker=worker)
            self._bump(db)

    def record_worker_interruption(self, worker: str, *, wall_seconds: float,
                                   usage: Optional[dict[str, Any]] = None,
                                   reservation_id: Optional[str] = None,
                                   reason: str = "operator_stop") -> dict[str, Any]:
        """Settle a cancelled round without charging a research checkpoint."""
        self.scaffold()
        usage = usage or {}
        with self._tx() as db:
            row = db.execute(
                "SELECT payload FROM assignments WHERE worker=?", (_id(worker, "worker"),),
            ).fetchone()
            if not row:
                raise ControlError(f"worker {worker} has no v2 assignment")
            current = _load(row["payload"], {})
            reservation_scope: dict[str, Any] = {}
            if reservation_id:
                reservation = db.execute(
                    "SELECT scope FROM call_reservations WHERE id=? AND status='active'",
                    (reservation_id,),
                ).fetchone()
                if reservation:
                    reservation_scope = _load(reservation["scope"], {})
            assignment_changed = bool(
                reservation_scope.get("assignment_epoch")
                and reservation_scope["assignment_epoch"] != current["epoch"]
            )
            if not assignment_changed and current.get("status") in {"assigned", "running", "auditing"}:
                current["status"] = "assigned"
                db.execute(
                    "UPDATE assignments SET status=?,payload=? WHERE worker=? AND epoch=?",
                    (current["status"], _dump(current), worker, current["epoch"]),
                )
            event_scope = {
                "assignment_epoch": reservation_scope.get("assignment_epoch") or current["epoch"],
                "target_version": reservation_scope.get("target_version") or current["target_version"],
                "obligation_id": reservation_scope.get("obligation_id") or current["obligation_id"],
                "route_id": reservation_scope.get("route_id") or current["route_id"],
            }
            event = self._event(
                db, "round_interrupted", worker=worker,
                reason=reason, assignment_changed=assignment_changed,
                usage_status="partial" if usage else "unavailable",
                **event_scope,
            )
            self._record_cost(
                db, component="worker_round", wall_seconds=wall_seconds,
                usage=usage, reservation_id=reservation_id, worker=worker,
                attempt_status="interrupted", **event_scope,
                usage_status="partial" if usage else "unavailable",
            )
            self._bump(db)
        self._record_budget_threshold()
        return {"assignment": current, "event": event}

    def recover_worker_interruption(self, worker: str, *, wall_seconds: float = 0.0,
                                    reason: str = "dead_worker",
                                    round_was_active: bool = False) -> dict[str, Any]:
        """Idempotently reconcile a round whose worker process disappeared.

        The worker-round reservation is the durable indication that a call was
        in flight.  Nested verifier reservations are cancelled first, then the
        parent is settled without consuming a research round.  If reservation
        expiry already ran, a ``running`` assignment is still made runnable.
        """
        self.scaffold()
        with self._tx() as db:
            row = db.execute(
                "SELECT payload FROM assignments WHERE worker=?", (_id(worker, "worker"),)
            ).fetchone()
            if not row:
                return {"recovered": False, "reason": "unassigned"}
            assignment = _load(row[0], {})
            reservations = db.execute(
                "SELECT * FROM call_reservations WHERE status='active' ORDER BY created_at_utc"
            ).fetchall()
            parents = []
            for reservation in reservations:
                scope = _load(reservation["scope"], {})
                if (
                    reservation["component"] == "worker_round"
                    and scope.get("worker") == worker
                    and scope.get("assignment_epoch") == assignment.get("epoch")
                ):
                    parents.append(reservation)
            parent_ids = {str(item["id"]) for item in parents}
            children = [
                item for item in reservations
                if _load(item["scope"], {}).get("parent_reservation_id") in parent_ids
            ]
            was_active = bool(
                round_was_active
                and assignment.get("status") in {"assigned", "running", "auditing"}
            )
            if not parents and not children and not was_active:
                return {"recovered": False, "assignment": assignment}

            for child in children:
                db.execute(
                    "UPDATE call_reservations SET status='cancelled',settled_at_utc=? "
                    "WHERE id=? AND status='active'",
                    (utc_now(), child["id"]),
                )
                self._event(
                    db, "call_reservation_cancelled", reservation_id=child["id"],
                    reason=f"{reason}: parent worker disappeared",
                )

            for index, parent in enumerate(parents):
                self._record_cost(
                    db, component="worker_round",
                    wall_seconds=max(0.0, wall_seconds) if index == 0 else 0.0,
                    usage=None, reservation_id=str(parent["id"]), worker=worker,
                    assignment_epoch=assignment["epoch"],
                    target_version=assignment["target_version"],
                    obligation_id=assignment["obligation_id"],
                    route_id=assignment["route_id"], attempt_status="interrupted",
                    usage_status="unavailable",
                )
            if not parents and was_active:
                self._record_cost(
                    db, component="worker_round", wall_seconds=max(0.0, wall_seconds),
                    usage=None, reservation_id=None, worker=worker,
                    assignment_epoch=assignment["epoch"],
                    target_version=assignment["target_version"],
                    obligation_id=assignment["obligation_id"],
                    route_id=assignment["route_id"], attempt_status="interrupted",
                    usage_status="unavailable", reservation_status="expired_or_missing",
                )

            if assignment.get("status") in {"assigned", "running", "auditing"}:
                assignment["status"] = "assigned"
                db.execute(
                    "UPDATE assignments SET status=?,payload=? WHERE worker=?",
                    (assignment["status"], _dump(assignment), worker),
                )
            event = self._event(
                db, "round_interrupted", worker=worker,
                assignment_epoch=assignment["epoch"],
                target_version=assignment["target_version"],
                obligation_id=assignment["obligation_id"],
                route_id=assignment["route_id"], reason=reason,
                usage_status="unavailable", recovered=True,
                cancelled_nested_reservations=[str(item["id"]) for item in children],
                settled_round_reservations=[str(item["id"]) for item in parents],
            )
            self._bump(db)
        self._record_budget_threshold()
        return {"recovered": True, "assignment": assignment, "event": event}

    def record_backend_failure(self, outcome: dict[str, Any], *, provider_key: str, actor: str, wall_seconds: float = 0.0) -> dict[str, Any]:
        """Open the shared circuit for non-worker Codex/provider calls."""
        self.scaffold()
        failure_class = str(outcome.get("failure_class") or "unknown_infra")
        attempts_limit, wall_limit, retry_schedule = self._infra_policy(failure_class)
        with self._tx() as db:
            row = db.execute("SELECT consecutive_failures,infra_wall_seconds FROM backend_circuits WHERE provider_key=?", (provider_key,)).fetchone()
            attempts = int(row[0] if row else 0) + 1
            outage_wall = float(row[1] if row else 0) + max(0.0, wall_seconds)
            retryable = bool(outcome.get("retryable"))
            blocked = not retryable or attempts >= attempts_limit or outage_wall >= wall_limit
            requested_wait = max(0, int(outcome.get("retry_after_seconds") or 0))
            wait = max(requested_wait, retry_schedule[min(attempts - 1, len(retry_schedule) - 1)])
            opened_until = None if blocked else time.time() + wait
            state = "blocked" if blocked else "open"
            db.execute("INSERT INTO backend_circuits(provider_key,state,consecutive_failures,opened_until,failure_class,infra_wall_seconds,updated_at_utc) VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider_key) DO UPDATE SET state=excluded.state,consecutive_failures=excluded.consecutive_failures,opened_until=excluded.opened_until,failure_class=excluded.failure_class,infra_wall_seconds=excluded.infra_wall_seconds,updated_at_utc=excluded.updated_at_utc", (provider_key, state, attempts, opened_until, failure_class, outage_wall, utc_now()))
            event = self._event(db, "backend_failure", provider_key=provider_key, actor=actor, failure_class=failure_class, retryable=retryable, retry_at_epoch=opened_until, error_signature=outcome.get("error_signature"), blocked=blocked)
            self._bump(db)
        return {"event": event, "state": state, "wait_seconds": None if blocked else wait}

    def record_backend_success(self, *, provider_key: str, actor: str) -> None:
        self.scaffold()
        with self._tx() as db:
            row = db.execute("SELECT state FROM backend_circuits WHERE provider_key=?", (provider_key,)).fetchone()
            if not row or row["state"] == "closed":
                return
            db.execute("UPDATE backend_circuits SET state='closed',consecutive_failures=0,opened_until=NULL,failure_class=NULL,infra_wall_seconds=0,updated_at_utc=? WHERE provider_key=?", (utc_now(), provider_key))
            self._event(db, "backend_recovered", provider_key=provider_key, actor=actor)
            self._bump(db)

    def retry_backend(self, provider_key: str, *, reason: str) -> dict[str, Any]:
        """Permit one half-open probe after an operator fixes external provider state."""
        self.scaffold()
        reason = reason.strip()
        if not reason:
            raise ControlError("backend retry reason is required")
        resumed: list[str] = []
        now = time.time()
        with self._tx() as db:
            db.execute("INSERT INTO backend_circuits(provider_key,state,consecutive_failures,opened_until,failure_class,infra_wall_seconds,updated_at_utc) VALUES (?,'open',0,?,NULL,0,?) ON CONFLICT(provider_key) DO UPDATE SET state='open',consecutive_failures=0,opened_until=excluded.opened_until,failure_class=NULL,infra_wall_seconds=0,updated_at_utc=excluded.updated_at_utc", (provider_key, now, utc_now()))
            if provider_key == "codex":
                for row in db.execute("SELECT worker,payload FROM assignments WHERE status='infra_blocked'"):
                    assignment = _load(row["payload"], {})
                    assignment.update(status="waiting_retry", infra_failure_count=0, infra_outage_wall_seconds=0.0, next_retry_at_epoch=now, last_failure_class=None, last_error_signature=None)
                    db.execute("UPDATE assignments SET status='waiting_retry',payload=? WHERE worker=?", (_dump(assignment), row["worker"]))
                    resumed.append(str(row["worker"]))
            event = self._event(db, "backend_retry_requested", provider_key=provider_key, reason=reason, resumed_workers=resumed)
            self._bump(db)
        return {"event": event, "provider_key": provider_key, "resumed_workers": resumed}

    def validate_submission(self, worker: str, *, target_version: str, obligation_id: str, route_id: str, assignment_epoch: str, assumptions_used: Iterable[str], reservation_id: Optional[str] = None) -> dict[str, Any]:
        assignment = self.validate_assignment(worker, reservation_id=reservation_id)
        if (target_version, obligation_id, route_id, assignment_epoch) != (assignment["target_version"], assignment["obligation_id"], assignment["route_id"], assignment["epoch"]):
            raise ControlError("fact submission is not bound to the current assignment")
        target = self.target(target_version)
        used = set(_strings(list(assumptions_used), "assumptions_used"))
        if used & set(target["forbidden_assumptions"]):
            raise ControlError("submission uses forbidden assumptions")
        outside = used - set(target["allowed_assumptions"])
        if outside:
            raise ControlError(
                f"submission uses assumptions outside the target: {sorted(outside)}; "
                f"use exact allowed_assumptions entries: {target['allowed_assumptions']}"
            )
        return assignment

    # --------------------------------------------------------------- facts
    def prepare_fact(self, fact_id: str, payload: dict[str, Any]) -> str:
        self.scaffold()
        submission_id = uuid.uuid4().hex
        with self._tx() as db:
            db.execute(
                "INSERT INTO pending_facts(submission_id,fact_id,payload,status) VALUES (?,?,?,'prepared')",
                (submission_id, fact_id, _dump(payload)),
            )
            self._bump(db)
        return submission_id

    def finalize_fact(self, fact_id: str, submission_id: str) -> dict[str, Any]:
        self.scaffold()
        from danus.research import index_fact_into
        with self._tx() as db:
            row = db.execute(
                "SELECT fact_id,payload,status FROM pending_facts WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            if not row:
                raise ControlError(f"unknown pending fact: {fact_id}")
            if str(row["fact_id"]) != fact_id:
                raise ControlError(f"pending submission does not match fact: {fact_id}")
            if row["status"] == "complete":
                return {"fact_id": fact_id, "already_complete": True}
            if row["status"] != "prepared":
                raise ControlError(f"pending fact is not prepared: {fact_id}")
            payload = _load(row["payload"], {})
            if not (self.project / "fact_graph" / "facts" / f"{fact_id}.md").is_file():
                raise ControlError(f"pending fact file is missing: {fact_id}")
            index_fact_into(db, self.project, fact_id)
            event = self._event(db, "fact_linked", fact_id=fact_id, reused=payload.get("reused", False), **payload["scope"])
            db.execute("INSERT OR IGNORE INTO fact_scopes VALUES (?,?,?,?,?,?,?,?)", (fact_id, payload["scope"]["target_version"], payload["scope"]["obligation_id"], payload["scope"]["route_id"], payload["scope"]["assignment_epoch"], payload["scope"].get("claim_role") or "unconditional", "direct", event["seq"]))
            db.execute("UPDATE pending_facts SET status='complete' WHERE submission_id=?", (submission_id,))
            self._bump(db)
            return event

    def recover_pending_facts(self) -> None:
        if not self.db_path.exists():
            return
        with self._connect() as db:
            rows = db.execute("SELECT submission_id,fact_id FROM pending_facts WHERE status='prepared'").fetchall()
        for row in rows:
            if (self.project / "fact_graph" / "facts" / f"{row['fact_id']}.md").is_file():
                self.finalize_fact(str(row["fact_id"]), str(row["submission_id"]))

    def reusable_fact(self, statement: str, assumptions_used: Iterable[str]) -> Optional[str]:
        normalized = " ".join(statement.split())
        assumptions = sorted(str(item) for item in assumptions_used)
        if not assumptions:
            with self._connect() as db:
                row = db.execute(
                    "SELECT f.fact_id FROM facts f WHERE f.statement=? AND f.status='active' "
                    "AND EXISTS (SELECT 1 FROM fact_scopes s WHERE s.fact_id=f.fact_id) "
                    "ORDER BY f.fact_id LIMIT 1",
                    (statement,),
                ).fetchone()
            if row and not self.fact_tainted(str(row[0])):
                return str(row[0])
        for row in reversed(self.events("fact_linked")):
            fact_id = row.get("fact_id")
            if not fact_id or self.fact_tainted(str(fact_id)) or sorted(row.get("assumptions_used") or []) != assumptions:
                continue
            with self._connect() as db:
                fact = db.execute("SELECT statement FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
            if fact and " ".join(str(fact[0]).split()) == normalized:
                return str(fact_id)
        return None

    def fact_tainted(self, fact_id: str) -> bool:
        return any(row.get("fact_id") == fact_id for row in self.events("fact_tainted"))

    def taint_fact(self, fact_id: str, reason: str, *, actor: str = "main") -> dict[str, Any]:
        from danus.research import ResearchQuery
        affected = {fact_id, *ResearchQuery(self.project).descendants(fact_id)}
        producing_routes = {
            str(row["route_id"])
            for row in self.events("fact_linked")
            if row.get("fact_id") in affected and row.get("route_id")
        }
        event = self.append_event("fact_tainted", fact_id=fact_id, reason=reason.strip(), actor=actor, affected_fact_ids=sorted(affected), review_required=True)
        stale = []
        with self._tx() as db:
            db.executemany("UPDATE facts SET status='tainted' WHERE fact_id=?", [(item,) for item in affected])
            for row in db.execute("SELECT a.worker,a.payload,r.payload route FROM assignments a JOIN routes r ON r.id=a.route_id"):
                assignment, route = _load(row["payload"], {}), _load(row["route"], {})
                if affected.intersection(route.get("input_fact_ids") or []) or assignment["route_id"] in producing_routes:
                    assignment.update(status="tainted", stale_reason=f"route depends on tainted fact {fact_id}")
                    db.execute("UPDATE assignments SET status='tainted',payload=? WHERE worker=?", (_dump(assignment), row["worker"]))
                    self._set_route_state(db, assignment["route_id"], "stalled", actor="controller", reason=assignment["stale_reason"])
                    stale.append(str(row["worker"]))
            self._bump(db)
        return {"event": event, "stale_workers": stale}

    # -------------------------------------------------------- gain and cost
    def evidence_exists(self, reference: str) -> bool:
        if (self.project / "fact_graph" / "facts" / f"{reference}.md").is_file():
            return True
        return any(any(row.get("id") == reference for row in read_jsonl(path)) for path in (self.project / "global_memory").glob("*.jsonl"))

    def evaluate_work_report(self, worker: str, report: dict[str, Any], *, wall_seconds: float, usage: Optional[dict[str, Any]] = None, reservation_id: Optional[str] = None) -> dict[str, Any]:
        assignment = self.assignment(worker)
        invalid_reason = None
        if not assignment:
            raise ControlError(f"worker {worker} has no v2 assignment")
        reservation_scope: dict[str, Any] = {}
        if reservation_id:
            with self._connect() as db:
                row = db.execute(
                    "SELECT component,status,scope FROM call_reservations WHERE id=?",
                    (reservation_id,),
                ).fetchone()
            if row:
                reservation_scope = _load(row["scope"], {})
            if (
                not row
                or row["component"] != "worker_round"
                or row["status"] != "active"
                or reservation_scope.get("worker") != worker
                or reservation_scope.get("assignment_epoch") != assignment.get("epoch")
            ):
                invalid_reason = "worker-round reservation does not match the assignment"
        if not invalid_reason and assignment.get("status") not in {"assigned", "running", "auditing"}:
            invalid_reason = f"assignment is not runnable: {assignment.get('status')}"
        elif not invalid_reason and assignment["target_version"] != self.current_target_version():
            invalid_reason = "assignment target is stale"
        elif not invalid_reason and int(assignment["rounds_used"]) >= int(assignment["max_rounds"]):
            invalid_reason = "route round budget exhausted"
        if invalid_reason:
            cost_scope = {
                "target_version": reservation_scope.get("target_version") or assignment.get("target_version"),
                "obligation_id": reservation_scope.get("obligation_id") or assignment.get("obligation_id"),
                "route_id": reservation_scope.get("route_id") or assignment.get("route_id"),
                "assignment_epoch": reservation_scope.get("assignment_epoch") or assignment.get("epoch"),
            }
            with self._tx() as db:
                self._record_cost(db, component="worker_round", wall_seconds=wall_seconds, usage=usage, reservation_id=reservation_id, worker=worker, attempt_status="discarded", **cost_scope)
                self._event(db, "round_discarded", worker=worker, assignment_epoch=cost_scope["assignment_epoch"], reason=invalid_reason)
                self._bump(db)
            self._record_budget_threshold()
            return {"gain": "none", "decision": "invalidated", "assignment": assignment}
        assignment.update(status="running", rounds_used=int(assignment["rounds_used"]) + 1, rounds_remaining=max(0, int(assignment["rounds_remaining"]) - 1), wall_seconds=float(assignment.get("wall_seconds", 0)) + max(0.0, wall_seconds))
        recent = self.events()[int(assignment.get("event_cursor", 0)):]
        linked = [row for row in recent if row.get("event") == "fact_linked" and row.get("assignment_epoch") == assignment["epoch"]]
        closed = [row for row in recent if row.get("event") == "obligation_state" and row.get("assignment_epoch") == assignment["epoch"] and row.get("state") in {"closed", "refuted"}]
        budget_blocks = [
            row for row in recent
            if row.get("event") == "call_reservation_rejected"
            and row.get("assignment_epoch") == assignment["epoch"]
            and row.get("reason_code") in {"wall_budget", "cost_budget"}
        ]
        assignment["event_cursor"] = len(self.events())
        gain = "none" if budget_blocks else "high" if linked or closed else "low"
        refs = report.get("new_evidence_refs") or []
        credited = set(assignment.get("credited_evidence_refs") or [])
        valid_refs = [ref for ref in refs if isinstance(ref, str) and ref not in credited and self.evidence_exists(ref)]
        interfaces = report.get("unresolved_interfaces") or []
        reduced = isinstance(assignment.get("last_unresolved_interfaces"), int) and len(interfaces) < assignment["last_unresolved_interfaces"]
        state_change = bool(report.get("new_or_changed_obligations")) or report.get("route_status") in {"blocked", "refuted", "new_route", "applicability_changed"}
        if not budget_blocks and gain == "low" and valid_refs and (state_change or reduced or report.get("novelty_basis")):
            gain = "medium"
            assignment["credited_evidence_refs"] = sorted(credited | set(valid_refs))
        assignment["last_unresolved_interfaces"] = len(interfaces)
        audit_was_required = bool(assignment.get("audit_required"))
        route_status = report.get("route_status")
        route_completed = route_status in {"completed", "refuted"} and gain == "high"
        if budget_blocks:
            assignment.update(status="budget_exhausted", audit_required=False)
            decision = "budget_exhausted"
        elif route_completed:
            assignment.update(status="completed", audit_required=False)
            decision = "completed"
        elif gain in {"high", "medium"}:
            assignment.update(consecutive_low=0, audit_required=False)
            assignment["rounds_remaining"] = min(int(assignment["max_rounds"]) - int(assignment["rounds_used"]), int(assignment["rounds_remaining"]) + RENEWAL_ROUNDS)
            decision = "continue"
        else:
            assignment["consecutive_low"] += 1
            if assignment["consecutive_low"] == 1:
                decision = "continue"
            elif assignment["consecutive_low"] == 2 or not audit_was_required:
                assignment.update(audit_required=True, status="auditing")
                decision = "audit"
            else:
                assignment["status"] = "stalled"
                decision = "stalled"
        obligation_closed = self.obligation_state(assignment["obligation_id"]) in {"closed", "refuted"}
        if obligation_closed:
            assignment.update(status="completed", audit_required=False)
            decision = "completed"
        if assignment["rounds_used"] >= assignment["max_rounds"]:
            if decision != "completed":
                assignment["status"], decision = "budget_exhausted", "budget_exhausted"
        superseded_assignment = None
        with self._tx() as db:
            current = db.execute(
                "SELECT epoch,payload FROM assignments WHERE worker=?", (worker,),
            ).fetchone()
            if not current or current["epoch"] != assignment["epoch"]:
                superseded_assignment = _load(current["payload"], {}) if current else assignment
                self._record_cost(db, component="worker_round", wall_seconds=wall_seconds, usage=usage, reservation_id=reservation_id, worker=worker, target_version=assignment["target_version"], obligation_id=assignment["obligation_id"], route_id=assignment["route_id"], assignment_epoch=assignment["epoch"], attempt_status="discarded")
                self._event(db, "round_discarded", worker=worker, assignment_epoch=assignment["epoch"], reason="assignment changed during report evaluation")
                self._bump(db)
            else:
                db.execute("UPDATE assignments SET status=?,payload=? WHERE worker=? AND epoch=?", (assignment["status"], _dump(assignment), worker, assignment["epoch"]))
                if decision == "stalled" or (decision == "budget_exhausted" and not budget_blocks):
                    self._set_route_state(db, assignment["route_id"], "stalled", actor="controller", reason=decision)
                elif decision == "completed":
                    route_state = "refuted" if route_status == "refuted" else "succeeded"
                    reason = "obligation closed" if obligation_closed else f"verified route {route_status}"
                    self._set_route_state(db, assignment["route_id"], route_state, actor="controller", reason=reason)
                event = self._event(db, "work_checkpoint", worker=worker, assignment_epoch=assignment["epoch"], target_version=assignment["target_version"], obligation_id=assignment["obligation_id"], route_id=assignment["route_id"], rounds_used=assignment["rounds_used"], gain=gain, decision=decision, valid_evidence_refs=valid_refs, budget_block_event_ids=[row["event_id"] for row in budget_blocks], report=report)
                db.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?)", (event["seq"], assignment["target_version"], assignment["obligation_id"], assignment["route_id"], worker, assignment["rounds_used"], gain, decision, _dump(report)))
                for signature in report.get("failed_attempt_signatures") or []:
                    db.execute("INSERT INTO obstacles VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(signature,route_id) DO UPDATE SET occurrences=occurrences+1,last_seen_seq=excluded.last_seen_seq,title=excluded.title,status='open'", (signature, assignment["route_id"], assignment["obligation_id"], signature, "open", 1, event["seq"], event["seq"]))
                self._record_cost(db, component="worker_round", wall_seconds=wall_seconds, usage=usage, reservation_id=reservation_id, worker=worker, target_version=assignment["target_version"], obligation_id=assignment["obligation_id"], route_id=assignment["route_id"], assignment_epoch=assignment["epoch"])
                self._bump(db)
        self._record_budget_threshold()
        if superseded_assignment is not None:
            return {"gain": "none", "decision": "invalidated", "assignment": superseded_assignment}
        return {
            "gain": gain, "decision": decision, "assignment": assignment,
            "project_budget_blocked": bool(budget_blocks),
            "last_fact_id": str(linked[-1]["fact_id"]) if linked else None,
        }

    def activate_fallback(self, worker: str) -> Optional[dict[str, Any]]:
        assignment = self.assignment(worker)
        if not assignment:
            return None
        route = self.route(assignment["route_id"])
        for rid in route.get("fallback_route_ids") or []:
            if self.route_state(rid) == "proposed" and self.route(rid)["obligation_id"] == assignment["obligation_id"]:
                self.set_route_state(route["id"], "superseded", actor="controller", reason=f"fallback to {rid}")
                return self.assign(worker, obligation_id=assignment["obligation_id"], route_id=rid, task=f"Fallback route {rid}: {self.route(rid)['expected_result']}", max_rounds=assignment["max_rounds"], round_timeout_seconds=assignment["round_timeout_seconds"])
        return None

    def reserve_call(self, *, component: str, max_wall_seconds: float, provider_key: str = "codex", estimated_cost_usd: Optional[float] = None, parent_reservation_id: Optional[str] = None, require_current_assignment: bool = False, **scope: Any) -> dict[str, Any]:
        """Atomically reserve the worst-case local budget before spawning a paid call."""
        self.scaffold()
        wall = float(max_wall_seconds)
        if wall <= 0:
            raise ControlError("V2 paid calls require a finite positive timeout")
        estimated = None if estimated_cost_usd is None else max(0.0, float(estimated_cost_usd))
        now = time.time()
        with self._tx() as db:
            if component == "worker_round" and require_current_assignment:
                row = db.execute(
                    "SELECT epoch,status FROM assignments WHERE worker=?",
                    (scope.get("worker"),),
                ).fetchone()
                if (
                    not row
                    or row["epoch"] != scope.get("assignment_epoch")
                    or row["status"] not in {"assigned", "running", "auditing"}
                ):
                    raise ControlError("worker-round reservation does not match the assignment")
            reserved_wall = wall
            if parent_reservation_id:
                parent = db.execute(
                    "SELECT component,scope,expires_at_epoch FROM call_reservations WHERE id=? AND status='active' AND expires_at_epoch>?",
                    (parent_reservation_id, now),
                ).fetchone()
                if not parent or parent["component"] != "worker_round":
                    raise ControlError("nested call requires an active worker-round reservation")
                parent_scope = _load(parent["scope"], {})
                for key in ("worker", "assignment_epoch", "target_version", "obligation_id", "route_id"):
                    if scope.get(key) and parent_scope.get(key) != scope[key]:
                        raise ControlError(f"nested call scope does not match parent reservation: {key}")
                reserved_wall = 0.0
                scope["parent_reservation_id"] = parent_reservation_id
            target = db.execute("SELECT payload FROM targets WHERE state='approved' ORDER BY version DESC LIMIT 1").fetchone()
            budget = (_load(target[0], {}).get("budget") if target else {}) or {}
            if estimated is None and budget.get("max_call_cost_usd") is not None:
                estimated = max(0.0, float(budget["max_call_cost_usd"]))
            spent = self._budget_state_in(db)
            try:
                wall_limit = float(budget.get("max_wall_seconds"))
            except (TypeError, ValueError):
                wall_limit = 0.0
            if wall_limit > 0 and spent["wall_seconds"] + spent["reserved_wall_seconds"] + reserved_wall > wall_limit:
                raise ControlError("project wall budget cannot reserve this call")
            try:
                cost_limit = float(budget.get("max_cost_usd"))
            except (TypeError, ValueError):
                cost_limit = 0.0
            if cost_limit > 0 and estimated is not None and spent["cost_usd"] + spent["reserved_cost_usd"] + estimated > cost_limit:
                raise ControlError("project cost budget cannot reserve this call")
            if cost_limit > 0 and estimated is None and budget.get("strict_cost_reservations"):
                raise ControlError("strict project cost budget requires an estimated call cost")
            reservation_id = uuid.uuid4().hex
            expires = now + wall + _RESERVATION_GRACE_SECONDS
            if parent_reservation_id:
                db.execute(
                    "UPDATE call_reservations SET expires_at_epoch=max(expires_at_epoch,?) WHERE id=?",
                    (expires + _NESTED_CALL_CLEANUP_SECONDS, parent_reservation_id),
                )
            db.execute("INSERT INTO call_reservations VALUES (?,?,?,?,?,'active',?,?,?,NULL)", (reservation_id, component, provider_key, reserved_wall, estimated, _dump(scope), utc_now(), expires))
            self._event(db, "call_reserved", reservation_id=reservation_id, component=component, provider_key=provider_key, reserved_wall_seconds=reserved_wall, requested_wall_seconds=wall, reserved_cost_usd=estimated, **scope)
            # Operational reservations do not change the mathematical/control
            # snapshot seen by a running worker.
        return {"id": reservation_id, "component": component, "provider_key": provider_key, "reserved_wall_seconds": reserved_wall, "requested_wall_seconds": wall, "reserved_cost_usd": estimated, "expires_at_epoch": expires, "scope": scope}

    def nested_call_timeout(self, parent_reservation_id: str, requested_seconds: float) -> int:
        """Validate a verifier child without shortening its own correctness deadline."""
        self.scaffold()
        now = time.time()
        with self._connect() as db:
            parent = db.execute(
                "SELECT component,expires_at_epoch FROM call_reservations "
                "WHERE id=? AND status='active' AND expires_at_epoch>?",
                (parent_reservation_id, now),
            ).fetchone()
        if not parent or parent["component"] != "worker_round":
            raise ControlError("nested call requires an active worker-round reservation")
        return max(1, int(float(requested_seconds)))

    def active_nested_call(self, parent_reservation_id: str) -> Optional[dict[str, Any]]:
        """Return the live child call that a timed-out worker must drain, if any."""
        for row in self.active_call_reservations():
            if row["scope"].get("parent_reservation_id") == parent_reservation_id:
                return row
        return None

    def _settle_reservation(self, db: sqlite3.Connection, reservation_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not reservation_id:
            return None
        row = db.execute("SELECT reserved_cost_usd,scope FROM call_reservations WHERE id=? AND status='active'", (reservation_id,)).fetchone()
        changed = db.execute("UPDATE call_reservations SET status='settled',settled_at_utc=? WHERE id=? AND status='active'", (utc_now(), reservation_id)).rowcount
        if not changed:
            raise ControlError(f"call reservation is not active: {reservation_id}")
        return {
            "reserved_cost_usd": None if row[0] is None else float(row[0]),
            "scope": _load(row[1], {}),
        }

    def cancel_call_reservation(self, reservation_id: str, *, reason: str) -> None:
        self.scaffold()
        with self._tx() as db:
            changed = db.execute("UPDATE call_reservations SET status='cancelled',settled_at_utc=? WHERE id=? AND status='active'", (utc_now(), reservation_id)).rowcount
            if not changed:
                raise ControlError(f"call reservation is not active: {reservation_id}")
            self._event(db, "call_reservation_cancelled", reservation_id=reservation_id, reason=reason)
            self._bump(db)

    def recover_call_reservations(self) -> list[str]:
        if not self.db_path.exists():
            return []
        now = time.time()
        with self._tx() as db:
            rows = db.execute("SELECT id,component,provider_key FROM call_reservations WHERE status='active' AND expires_at_epoch<=?", (now,)).fetchall()
            for row in rows:
                db.execute("UPDATE call_reservations SET status='expired',settled_at_utc=? WHERE id=?", (utc_now(), row["id"]))
                self._event(db, "call_reservation_expired", reservation_id=row["id"], component=row["component"], provider_key=row["provider_key"])
            if rows:
                self._bump(db)
        return [str(row["id"]) for row in rows]

    def _record_cost(self, db: sqlite3.Connection, *, component: str, wall_seconds: float, usage: Optional[dict[str, Any]] = None, cost_usd: Optional[float] = None, reservation_id: Optional[str] = None, **scope: Any) -> dict[str, Any]:
        settlement = self._settle_reservation(db, reservation_id)
        reserved_cost = settlement.get("reserved_cost_usd") if settlement else None
        reservation_scope = settlement.get("scope", {}) if settlement else {}
        if reservation_scope.get("parent_reservation_id"):
            scope = {
                **scope,
                "parent_reservation_id": reservation_scope["parent_reservation_id"],
                "nested_wall_seconds": round(max(0.0, wall_seconds), 3),
            }
            wall_seconds = 0.0
        usage = usage or {}
        if "input_tokens" in usage and "fresh_input_tokens" not in usage:
            usage = {**usage, "fresh_input_tokens": max(
                0, int(usage.get("input_tokens", 0) or 0)
                - int(usage.get("cached_input_tokens", 0) or 0),
            )}
        fresh_input = int(usage.get("fresh_input_tokens", 0) or 0)
        usage_anomaly = fresh_input >= _FRESH_INPUT_ANOMALY_THRESHOLD
        if cost_usd is None and any(key in usage for key in ("input_tokens", "output_tokens")):
            try:
                cost_usd = (float(usage.get("input_tokens", 0) or 0) * float(os.environ.get("DANUS_CODEX_PRICE_IN", "")) + float(usage.get("output_tokens", 0) or 0) * float(os.environ.get("DANUS_CODEX_PRICE_OUT", ""))) / 1_000_000
            except ValueError:
                cost_usd = None
        cost_status = "known" if cost_usd is not None else "unknown"
        if cost_usd is None and reserved_cost is not None:
            cost_usd, cost_status = reserved_cost, "estimated_ceiling"
        event = self._event(db, "cost", component=component, wall_seconds=round(max(0.0, wall_seconds), 3), usage=usage, usage_anomaly=usage_anomaly, cost_usd=cost_usd, cost_status=cost_status, reservation_id=reservation_id, **scope)
        if usage_anomaly:
            self._event(
                db, "usage_anomaly", component=component,
                fresh_input_tokens=fresh_input,
                threshold=_FRESH_INPUT_ANOMALY_THRESHOLD, **scope,
            )
        return event

    def record_cost(self, *, component: str, wall_seconds: float, usage: Optional[dict[str, Any]] = None, cost_usd: Optional[float] = None, reservation_id: Optional[str] = None, **scope: Any) -> dict[str, Any]:
        self.scaffold()
        with self._tx() as db:
            event = self._record_cost(db, component=component, wall_seconds=wall_seconds, usage=usage, cost_usd=cost_usd, reservation_id=reservation_id, **scope)
            self._bump(db)
        self._record_budget_threshold()
        return event

    def _budget_state_in(self, db: sqlite3.Connection, *, exclude_reservation_id: Optional[str] = None) -> dict[str, Any]:
        target_row = db.execute("SELECT payload FROM targets WHERE state='approved' ORDER BY version DESC LIMIT 1").fetchone()
        target = _load(target_row[0], {}) if target_row else {}
        costs = [_load(row[0], {}) for row in db.execute("SELECT payload FROM events WHERE event='cost'")]
        wall = sum(float(row.get("wall_seconds") or 0) for row in costs)
        cost = sum(float(row.get("cost_usd") or 0) for row in costs if row.get("cost_usd") is not None)
        active = db.execute(
            "SELECT reserved_wall_seconds,reserved_cost_usd FROM call_reservations WHERE status='active' AND expires_at_epoch>? AND (? IS NULL OR id<>?)",
            (time.time(), exclude_reservation_id, exclude_reservation_id),
        ).fetchall()
        reserved_wall = sum(float(row["reserved_wall_seconds"] or 0) for row in active)
        reserved_cost = sum(float(row["reserved_cost_usd"] or 0) for row in active)
        ratios = []
        budget = target.get("budget") or {}
        for spent, key in ((wall + reserved_wall, "max_wall_seconds"), (cost + reserved_cost, "max_cost_usd")):
            try:
                limit = float(budget.get(key))
                if limit > 0:
                    ratios.append(spent / limit)
            except (TypeError, ValueError):
                pass
        ratio = max(ratios, default=0.0)
        stage = "exhausted" if ratio >= 1 else "audit" if ratio >= .85 else "warn" if ratio >= .70 else "normal"
        infra_statuses = {"failed", "invalid_response", "error", "timeout", "infra_blocked"}
        infra_wall = sum(float(row.get("wall_seconds") or 0) for row in costs if row.get("component") == "worker_infra" or row.get("attempt_status") in infra_statuses)
        return {"stage": stage, "ratio": ratio, "wall_seconds": wall, "cost_usd": cost, "reserved_wall_seconds": reserved_wall, "reserved_cost_usd": reserved_cost, "unknown_cost_events": sum(row.get("cost_usd") is None for row in costs), "infra_wall_seconds": infra_wall, "budget": budget}

    def budget_state(self, *, exclude_reservation_id: Optional[str] = None) -> dict[str, Any]:
        self.scaffold()
        with self._connect() as db:
            return self._budget_state_in(db, exclude_reservation_id=exclude_reservation_id)

    def _record_budget_threshold(self) -> None:
        state = self.budget_state()
        if state["stage"] == "normal":
            return
        prior = [row.get("stage") for row in self.events("budget_threshold")]
        if state["stage"] not in prior:
            self.append_event("budget_threshold", target_version=self.current_target_version(), **state)

    # ------------------------------------------------------- derived indexes
    def rebuild_read_model(self) -> dict[str, Any]:
        self.scaffold()
        from danus.research import rebuild_fact_index
        return rebuild_fact_index(self.project, self)

    def process_outbox(self, handler: Any) -> list[dict[str, Any]]:
        self.scaffold()
        results = []
        with self._connect() as db:
            rows = db.execute("SELECT * FROM outbox WHERE status IN ('pending','failed') ORDER BY created_at_utc").fetchall()
        for row in rows:
            try:
                result = handler(str(row["kind"]), _load(row["payload"], {}))
                status, error = "completed", None
            except Exception as exc:  # controller records and retries external failures
                result, status, error = None, "failed", str(exc)
            with self._tx() as db:
                db.execute("UPDATE outbox SET status=?,attempts=attempts+1,last_error=? WHERE id=?", (status, error, row["id"]))
                self._bump(db)
            results.append({"id": row["id"], "status": status, "result": result, "error": error})
        return results

    def list_outbox(self) -> list[dict[str, Any]]:
        self.scaffold()
        with self._connect() as db:
            rows = db.execute("SELECT * FROM outbox ORDER BY created_at_utc DESC").fetchall()
        return [{**dict(row), "payload": _load(row["payload"], {})} for row in rows]
