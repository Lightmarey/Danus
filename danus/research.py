"""Shared, snapshot-aware research queries for Danus v2.

Markdown remains the mathematical source of truth.  This module reads the
transactional SQLite projection used by both agents and the local console.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Optional

from danus.core._util import utc_now
from danus.core.factgraph import FactGraph, parse_frontmatter, statement_of
from danus.core.global_memory import GlobalMemory


_CONTEXT_STATEMENT_CHARS = 1000


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: Optional[str], default: Any = None) -> Any:
    return json.loads(value) if value else default


def _frontmatter_value(raw: str, name: str) -> str:
    prefix = f"{name}:"
    for line in raw.splitlines()[1:]:
        if line.strip() == "---":
            break
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def _section(raw: str, name: str) -> str:
    lines: list[str] = []
    active = False
    wanted = f"## {name}".lower()
    for line in raw.splitlines():
        if line.strip().startswith("## "):
            if active:
                break
            active = line.strip().lower() == wanted
            continue
        if active:
            lines.append(line)
    return "\n".join(lines).strip()


def _fallback_title(statement: str) -> str:
    compact = " ".join(statement.split())
    return compact[:80] or "Untitled verified fact"


def index_fact_into(db: sqlite3.Connection, project: Path, fact_id: str) -> None:
    """Index one Markdown fact in an existing transaction."""
    path = Path(project) / "fact_graph" / "facts" / f"{fact_id}.md"
    raw = path.read_text(encoding="utf-8")
    front = parse_frontmatter(raw)
    statement = statement_of(raw)
    title = " ".join(str(front.get("title") or "").split()) or _fallback_title(statement)
    proof = _section(raw, "proof")
    intuition = _section(raw, "intuition")
    db.execute(
        """INSERT INTO facts(fact_id,title,statement,proof,intuition,author,problem_id,status,raw)
           VALUES (?,?,?,?,?,?,?,'active',?)
           ON CONFLICT(fact_id) DO UPDATE SET title=excluded.title,
             statement=excluded.statement,proof=excluded.proof,intuition=excluded.intuition,
             author=excluded.author,problem_id=excluded.problem_id,raw=excluded.raw""",
        (fact_id, title, statement, proof, intuition, _frontmatter_value(raw, "author"),
         _frontmatter_value(raw, "problem_id"), raw),
    )
    db.execute("DELETE FROM fact_edges WHERE fact_id=?", (fact_id,))
    db.executemany(
        "INSERT OR IGNORE INTO fact_edges(predecessor_id,fact_id) VALUES (?,?)",
        [(str(pred), fact_id) for pred in front.get("predecessors", [])],
    )
    db.execute("DELETE FROM facts_fts WHERE fact_id=?", (fact_id,))
    db.execute(
        "INSERT INTO facts_fts(fact_id,title,statement,proof) VALUES (?,?,?,?)",
        (fact_id, title, statement, proof),
    )


def rebuild_fact_index(project: Path, store: Any) -> dict[str, Any]:
    """Rebuild every derived table from Markdown plus immutable control events."""
    graph = FactGraph(project)
    with store._tx() as db:
        db.execute("DELETE FROM facts")
        db.execute("DELETE FROM fact_edges")
        db.execute("DELETE FROM fact_scopes")
        db.execute("DELETE FROM checkpoints")
        db.execute("DELETE FROM obstacles")
        db.execute("DELETE FROM facts_fts")
        for fact_id in graph.list():
            index_fact_into(db, project, fact_id)

        route_rows = db.execute("SELECT id,target_version,obligation_id,payload FROM routes").fetchall()
        for row in route_rows:
            route = _load(row["payload"], {})
            for fact_id in route.get("input_fact_ids") or []:
                db.execute(
                    "INSERT OR IGNORE INTO fact_scopes VALUES (?,?,?,?,?,'input','input',NULL)",
                    (fact_id, row["target_version"], row["obligation_id"], row["id"], ""),
                )

        obstacle_first: dict[tuple[str, str], int] = {}
        event_rows = db.execute("SELECT seq,event,payload FROM events ORDER BY seq").fetchall()
        for event_row in event_rows:
            seq, kind = int(event_row["seq"]), str(event_row["event"])
            payload = _load(event_row["payload"], {})
            if kind == "fact_linked" and payload.get("fact_id"):
                db.execute(
                    "INSERT OR IGNORE INTO fact_scopes VALUES (?,?,?,?,?,?,?,?)",
                    (payload["fact_id"], payload.get("target_version", ""), payload.get("obligation_id", ""),
                     payload.get("route_id", ""), payload.get("assignment_epoch", ""),
                     payload.get("claim_role", "unconditional"), "direct", seq),
                )
            elif kind == "obligation_state" and payload.get("fact_id") and payload.get("state") in {"closed", "refuted"}:
                route_id = str(payload.get("route_id") or "")
                if not route_id:
                    scope = db.execute(
                        "SELECT route_id FROM fact_scopes WHERE fact_id=? AND obligation_id=? ORDER BY event_seq DESC LIMIT 1",
                        (payload["fact_id"], payload.get("obligation_id", "")),
                    ).fetchone()
                    route_id = str(scope[0]) if scope else ""
                db.execute(
                    "INSERT OR IGNORE INTO fact_scopes VALUES (?,?,?,?,?,?,?,?)",
                    (payload["fact_id"], payload.get("target_version", ""), payload.get("obligation_id", ""),
                     route_id, payload.get("assignment_epoch", ""), payload.get("claim_role", "unconditional"),
                     "closing", seq),
                )
            elif kind == "work_checkpoint":
                report = payload.get("report") or {}
                db.execute(
                    "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?)",
                    (seq, payload.get("target_version"), payload.get("obligation_id"), payload.get("route_id"),
                     payload.get("worker"), payload.get("rounds_used"), payload.get("gain"),
                     payload.get("decision"), _dump(report)),
                )
                for signature in report.get("failed_attempt_signatures") or []:
                    key = (str(signature), str(payload.get("route_id") or ""))
                    first = obstacle_first.setdefault(key, seq)
                    db.execute(
                        """INSERT INTO obstacles VALUES (?,?,?,?,?,?,?,?)
                           ON CONFLICT(signature,route_id) DO UPDATE SET
                           occurrences=obstacles.occurrences+1,last_seen_seq=excluded.last_seen_seq""",
                        (key[0], key[1], payload.get("obligation_id", ""), key[0], "open", 1, first, seq),
                    )
            elif kind == "fact_tainted":
                for fact_id in payload.get("affected_fact_ids") or [payload.get("fact_id")]:
                    if fact_id:
                        db.execute("UPDATE facts SET status='tainted' WHERE fact_id=?", (fact_id,))
        generation = store._bump(db)
    with store._connect() as db:
        counts = {name: int(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in ("targets", "obligations", "routes", "assignments", "events")}
    return {"path": str(store.db_path), "database": str(store.db_path), "facts": len(graph.list()), "generation": generation, **counts}


class ResearchQuery:
    """The sole v2 read surface for MCP, authoring, and the human console."""

    ROLE_PRIORITY = {"closing": 0, "direct": 1, "input": 2, "support": 3, "unassigned": 4}

    def __init__(self, project: Path) -> None:
        from danus.control import ControlStore, require_v2_project

        self.project = require_v2_project(Path(project))
        self.store = ControlStore(self.project)
        self.store.scaffold()

    def _snapshot(self, snapshot: Optional[int]) -> int:
        current = self.store.generation()
        if snapshot is not None and int(snapshot) != current:
            raise ValueError(
                f"snapshot generation {snapshot} is not current generation {current}; "
                "use the persisted ContextManifest to reproduce an earlier model view"
            )
        return current

    def _fact_rows(self, ids: Iterable[str], *, include_proof: bool = False) -> list[dict[str, Any]]:
        wanted = list(dict.fromkeys(str(item) for item in ids if item))
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        columns = "fact_id,title,statement,status" + (",proof,intuition" if include_proof else "")
        with self.store._connect() as db:
            rows = db.execute(f"SELECT {columns} FROM facts WHERE fact_id IN ({marks})", wanted).fetchall()
        found = {str(row["fact_id"]): dict(row) for row in rows}
        return [found[item] for item in wanted if item in found]

    def descendants(self, fact_id: str, *, limit: int = 10000) -> list[str]:
        found: list[str] = []
        queue = deque([fact_id])
        seen = {fact_id}
        with self.store._connect() as db:
            while queue and len(found) < limit:
                current = queue.popleft()
                for row in db.execute("SELECT fact_id FROM fact_edges WHERE predecessor_id=?", (current,)):
                    child = str(row[0])
                    if child not in seen:
                        seen.add(child)
                        found.append(child)
                        queue.append(child)
        return found

    def fact_get(self, fact_id: str, *, include_proof: bool = False) -> dict[str, Any]:
        columns = "fact_id,title,statement,intuition,author,problem_id,status" + (",proof" if include_proof else "")
        with self.store._connect() as db:
            row = db.execute(f"SELECT {columns} FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
            if not row:
                raise KeyError(f"unknown fact: {fact_id}")
            predecessors = [item[0] for item in db.execute("SELECT predecessor_id FROM fact_edges WHERE fact_id=? ORDER BY predecessor_id", (fact_id,))]
            successors = [item[0] for item in db.execute("SELECT fact_id FROM fact_edges WHERE predecessor_id=? ORDER BY fact_id", (fact_id,))]
            scopes = [dict(item) for item in db.execute("SELECT target_version,obligation_id,route_id,claim_role,relation FROM fact_scopes WHERE fact_id=? ORDER BY relation,route_id,obligation_id", (fact_id,))]
        return {**dict(row), "predecessors": predecessors, "successors": successors, "scopes": scopes}

    def fact_neighborhood(self, fact_id: str, *, direction: str = "both", depth: int = 1, limit: int = 300) -> dict[str, Any]:
        if direction not in {"predecessors", "successors", "both"}:
            raise ValueError("direction must be predecessors, successors, or both")
        depth, limit = max(0, min(int(depth), 12)), max(1, min(int(limit), 300))
        levels = {fact_id: 0}
        edges: set[tuple[str, str]] = set()
        queue = deque([fact_id])
        with self.store._connect() as db:
            while queue and len(levels) < limit:
                current = queue.popleft()
                if levels[current] >= depth:
                    continue
                pairs: list[tuple[str, str]] = []
                if direction in {"predecessors", "both"}:
                    pairs += [(str(row[0]), current) for row in db.execute("SELECT predecessor_id FROM fact_edges WHERE fact_id=?", (current,))]
                if direction in {"successors", "both"}:
                    pairs += [(current, str(row[0])) for row in db.execute("SELECT fact_id FROM fact_edges WHERE predecessor_id=?", (current,))]
                for source, target in sorted(pairs):
                    edges.add((source, target))
                    other = source if target == current else target
                    if other not in levels and len(levels) < limit:
                        levels[other] = levels[current] + 1
                        queue.append(other)
        return {"center": fact_id, "nodes": self._fact_rows(levels), "edges": [{"source": a, "target": b} for a, b in sorted(edges)], "truncated": bool(queue)}

    def _support_closure(self, fact_ids: Iterable[str], depth: Optional[int] = 2, limit: int = 300) -> tuple[list[str], list[dict[str, str]], int]:
        roots = list(dict.fromkeys(str(item) for item in fact_ids if item))
        found: list[str] = []
        edges: set[tuple[str, str]] = set()
        queue = deque((item, 0) for item in roots)
        seen = set(roots)
        omitted: set[str] = set()
        with self.store._connect() as db:
            while queue:
                current, level = queue.popleft()
                if depth is not None and level >= depth:
                    omitted.update(
                        str(row[0])
                        for row in db.execute(
                            "SELECT predecessor_id FROM fact_edges WHERE fact_id=? ORDER BY predecessor_id",
                            (current,),
                        )
                        if str(row[0]) not in seen
                    )
                    continue
                for row in db.execute("SELECT predecessor_id FROM fact_edges WHERE fact_id=? ORDER BY predecessor_id", (current,)):
                    pred = str(row[0])
                    edges.add((pred, current))
                    if pred in seen:
                        omitted.discard(pred)
                        continue
                    if len(seen) >= limit:
                        omitted.add(pred)
                        continue
                    seen.add(pred)
                    omitted.discard(pred)
                    found.append(pred)
                    queue.append((pred, level + 1))
        omitted.difference_update(seen)
        return found, [{"source": a, "target": b} for a, b in sorted(edges) if a in seen and b in seen], len(omitted)

    def _scoped_facts(self, *, route_id: Optional[str] = None, obligation_id: Optional[str] = None, support_depth: int = 2, limit: int = 300) -> dict[str, Any]:
        where, arg = ("route_id=?", route_id) if route_id else ("obligation_id=?", obligation_id)
        with self.store._connect() as db:
            scope_rows = db.execute(f"SELECT fact_id,relation FROM fact_scopes WHERE {where} ORDER BY fact_id,relation", (arg,)).fetchall()
            shared = {str(row[0]) for row in db.execute("SELECT fact_id FROM fact_scopes GROUP BY fact_id HAVING COUNT(DISTINCT route_id || ':' || obligation_id)>1")}
        roles: dict[str, set[str]] = defaultdict(set)
        for row in scope_rows:
            roles[str(row["fact_id"])].add(str(row["relation"]))
        support, edges, omitted = self._support_closure(roles, support_depth, limit)
        for fact_id in support:
            roles[fact_id].add("support")
        facts = []
        for fact in self._fact_rows(roles):
            statement = str(fact.get("statement") or "")
            if len(statement) > _CONTEXT_STATEMENT_CHARS:
                fact["statement"] = statement[:_CONTEXT_STATEMENT_CHARS].rstrip() + "…"
                fact["statement_truncated"] = True
            fact_roles = sorted(roles[fact["fact_id"]], key=lambda item: self.ROLE_PRIORITY.get(item, 9))
            fact.update(role=fact_roles[0], roles=fact_roles, shared=fact["fact_id"] in shared)
            facts.append(fact)
        facts.sort(key=lambda item: (self.ROLE_PRIORITY.get(item["role"], 9), item["title"], item["fact_id"]))
        return {"facts": facts[:limit], "edges": edges, "unexpanded_count": omitted + max(0, len(facts) - limit)}

    def research_map(self, target_version: Optional[str] = None) -> dict[str, Any]:
        target_version = target_version or self.store.current_target_version()
        targets = []
        for version in self.store.target_versions():
            target = self.store.target(version)
            targets.append({**target, "state": self.store.target_state(version), "diff": self.store.target_diff(version) if self.store.target_state(version) == "draft" else ""})
        if not target_version:
            with self.store._connect() as db:
                unassigned_count = int(db.execute("SELECT COUNT(*) FROM facts f WHERE NOT EXISTS (SELECT 1 FROM fact_scopes s WHERE s.fact_id=f.fact_id)").fetchone()[0])
                unassigned = [dict(row) | {"role": "unassigned"} for row in db.execute("SELECT fact_id,title,statement,status FROM facts f WHERE NOT EXISTS (SELECT 1 FROM fact_scopes s WHERE s.fact_id=f.fact_id) ORDER BY title,fact_id LIMIT 100")]
            return {"generation": self.store.generation(), "active_target": None, "targets": targets, "methods": [], "obligations": [], "unassigned_count": unassigned_count, "unassigned_facts": unassigned, "budget": self.store.budget_state(), "backend_circuits": self.store.backend_circuits(), "active_call_reservations": self.store.active_call_reservations(), "outbox": self.store.list_outbox()}
        obligations = self.store.list_obligations(target_version)
        routes = self.store.list_routes(target_version)
        with self.store._connect() as db:
            checkpoints = [dict(row) | {"report": _load(row["report"], {})} for row in db.execute("SELECT * FROM checkpoints WHERE target_version=? ORDER BY event_seq", (target_version,))]
            obstacles = [dict(row) for row in db.execute("SELECT * FROM obstacles WHERE route_id IN (SELECT id FROM routes WHERE target_version=?) ORDER BY last_seen_seq DESC", (target_version,))]
            unassigned_count = int(db.execute("SELECT COUNT(*) FROM facts f WHERE NOT EXISTS (SELECT 1 FROM fact_scopes s WHERE s.fact_id=f.fact_id)").fetchone()[0])
            unassigned = [dict(row) | {"role": "unassigned"} for row in db.execute("SELECT fact_id,title,statement,status FROM facts f WHERE NOT EXISTS (SELECT 1 FROM fact_scopes s WHERE s.fact_id=f.fact_id) ORDER BY title,fact_id LIMIT 100")]
        by_method: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for route in routes:
            route_checkpoints = [row for row in checkpoints if row["route_id"] == route["id"]]
            route_obstacles = [row for row in obstacles if row["route_id"] == route["id"]]
            obligation = next((item for item in obligations if item["id"] == route["obligation_id"]), None)
            by_method[(route["method_key"], route["method_title"])].append({**route, "obligation": obligation, "gain_sequence": [item["gain"] for item in route_checkpoints], "checkpoints": len(route_checkpoints), "obstacles": route_obstacles})
        methods = [{"method_key": key, "method_title": title, "routes": values} for (key, title), values in sorted(by_method.items())]
        return {"generation": self.store.generation(), "active_target": self.store.target(target_version), "target_state": self.store.target_state(target_version), "targets": targets, "methods": methods, "obligations": obligations, "unassigned_count": unassigned_count, "unassigned_facts": unassigned, "budget": self.store.budget_state(), "backend_circuits": self.store.backend_circuits(), "active_call_reservations": self.store.active_call_reservations(), "outbox": self.store.list_outbox()}

    def route_context(self, route_id: str, *, snapshot: Optional[int] = None) -> dict[str, Any]:
        generation = self._snapshot(snapshot)
        route = self.store.route(route_id)
        obligation = self.store.obligation(route["obligation_id"])
        with self.store._connect() as db:
            checkpoints = [dict(row) | {"report": _load(row["report"], {})} for row in db.execute("SELECT * FROM checkpoints WHERE route_id=? ORDER BY event_seq DESC LIMIT 20", (route_id,))]
            obstacles = [dict(row) for row in db.execute("SELECT * FROM obstacles WHERE route_id=? ORDER BY last_seen_seq DESC", (route_id,))]
        return {"snapshot_generation": generation, "route": {**route, "state": self.store.route_state(route_id)}, "obligation": obligation, "fact_group": self._scoped_facts(route_id=route_id), "checkpoints": checkpoints, "obstacles": obstacles}

    def obligation_context(self, obligation_id: str, *, snapshot: Optional[int] = None) -> dict[str, Any]:
        generation = self._snapshot(snapshot)
        obligation = self.store.obligation(obligation_id)
        routes = [item for item in self.store.list_routes(obligation["target_version"]) if item["obligation_id"] == obligation_id]
        dependencies = [self.store.obligation(item) for item in obligation.get("dependencies") or []]
        return {"snapshot_generation": generation, "obligation": obligation, "dependencies": dependencies, "routes": routes, "fact_group": self._scoped_facts(obligation_id=obligation_id)}

    def _topological_closure(self, seeds: Iterable[str]) -> list[str]:
        support, _, _ = self._support_closure(seeds, depth=None, limit=100000)
        selected = set(str(item) for item in seeds) | set(support)
        if not selected:
            return []
        marks = ",".join("?" for _ in selected)
        with self.store._connect() as db:
            active = {str(row[0]) for row in db.execute(f"SELECT fact_id FROM facts WHERE status='active' AND fact_id IN ({marks})", sorted(selected))}
            indegree = {item: 0 for item in active}
            children: dict[str, list[str]] = defaultdict(list)
            for row in db.execute("SELECT predecessor_id,fact_id FROM fact_edges"):
                source, target = str(row[0]), str(row[1])
                if source in active and target in active:
                    children[source].append(target)
                    indegree[target] += 1
        queue = deque(sorted(item for item, degree in indegree.items() if degree == 0))
        ordered = []
        while queue:
            item = queue.popleft()
            ordered.append(item)
            for child in sorted(children[item]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(ordered) != len(active):
            ordered.extend(sorted(active - set(ordered)))
        return ordered

    def target_research_manifest(self, target_version: Optional[str] = None) -> dict[str, Any]:
        """All active facts explicitly used by one target plus their proof support."""
        target_version = target_version or self.store.current_target_version()
        if not target_version:
            return {"target_version": None, "snapshot_generation": self.store.generation(), "fact_ids": [], "facts": []}
        with self.store._connect() as db:
            seeds = [str(row[0]) for row in db.execute("SELECT DISTINCT s.fact_id FROM fact_scopes s JOIN facts f ON f.fact_id=s.fact_id WHERE s.target_version=? AND f.status='active' ORDER BY s.fact_id", (target_version,))]
        ordered = self._topological_closure(seeds)
        return {"target_version": target_version, "snapshot_generation": self.store.generation(), "fact_ids": ordered, "facts": self._fact_rows(ordered)}

    def target_proof_manifest(self, target_version: Optional[str] = None) -> dict[str, Any]:
        target_version = target_version or self.store.current_target_version()
        if not target_version:
            return {"target_version": None, "complete": False, "root_obligations": [], "closing_fact_ids": [], "fact_ids": []}
        roots = [item for item in self.store.list_obligations(target_version) if item.get("kind") == "root"]
        root_ids = [item["id"] for item in roots]
        with self.store._connect() as db:
            closing = [str(row[0]) for row in db.execute(f"SELECT DISTINCT s.fact_id FROM fact_scopes s JOIN facts f ON f.fact_id=s.fact_id WHERE s.relation='closing' AND f.status='active' AND s.obligation_id IN ({','.join('?' for _ in root_ids)}) ORDER BY s.fact_id", root_ids)] if root_ids else []
        ordered = self._topological_closure(closing)
        complete = bool(roots) and all(item.get("state") in {"closed", "refuted"} for item in roots) and all(any(scope["obligation_id"] == item["id"] for fid in closing for scope in self.fact_get(fid)["scopes"]) for item in roots)
        return {"target_version": target_version, "snapshot_generation": self.store.generation(), "complete": complete, "root_obligations": roots, "closing_fact_ids": closing, "fact_ids": ordered, "facts": self._fact_rows(ordered)}

    def fact_search(self, query: str, *, target_version: Optional[str] = None, route_id: Optional[str] = None, obligation_id: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
        if not tokens:
            return []
        match = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:20])
        candidate_limit = max(100, limit * 10)
        try:
            with self.store._connect() as db:
                candidates = [dict(row) for row in db.execute(
                    "SELECT f.fact_id,f.title,substr(f.statement,1,400) snippet,f.status,bm25(facts_fts) score FROM facts_fts JOIN facts f ON f.fact_id=facts_fts.fact_id WHERE facts_fts MATCH ? ORDER BY score LIMIT ?",
                    (match, candidate_limit),
                )]
        except sqlite3.OperationalError:
            like = f"%{query}%"
            with self.store._connect() as db:
                candidates = [dict(row) for row in db.execute("SELECT fact_id,title,substr(statement,1,400) snippet,status,0.0 score FROM facts WHERE title LIKE ? OR statement LIKE ? ORDER BY title LIMIT ?", (like, like, candidate_limit))]

        scope_filter = target_version or route_id or obligation_id
        distances: dict[str, int] = {}
        scope_roles: dict[str, set[str]] = defaultdict(set)
        if scope_filter:
            clauses, args = [], []
            if target_version:
                clauses.append("target_version=?")
                args.append(target_version)
            if route_id:
                clauses.append("route_id=?")
                args.append(route_id)
            if obligation_id:
                clauses.append("obligation_id=?")
                args.append(obligation_id)
            with self.store._connect() as db:
                rows = db.execute(f"SELECT fact_id,relation FROM fact_scopes WHERE {' AND '.join(clauses)}", args).fetchall()
                queue = deque()
                for row in rows:
                    fact_id = str(row["fact_id"])
                    scope_roles[fact_id].add(str(row["relation"]))
                    if fact_id not in distances:
                        distances[fact_id] = 0
                        queue.append(fact_id)
                while queue and len(distances) < 10000:
                    current = queue.popleft()
                    for row in db.execute("SELECT predecessor_id FROM fact_edges WHERE fact_id=?", (current,)):
                        pred = str(row[0])
                        if pred not in distances:
                            distances[pred] = distances[current] + 1
                            queue.append(pred)
            candidates = [item for item in candidates if item["fact_id"] in distances]
        relation_rank = {"closing": 0, "direct": 1, "input": 2}
        for item in candidates:
            roles = scope_roles.get(item["fact_id"], set())
            item["scope_role"] = min(roles, key=lambda role: relation_rank.get(role, 9)) if roles else ("support" if scope_filter else "global")
            item["graph_distance"] = distances.get(item["fact_id"])
        candidates.sort(key=lambda item: (relation_rank.get(item["scope_role"], 3), item["graph_distance"] if item["graph_distance"] is not None else 1000000, float(item["score"]), item["fact_id"]))
        return candidates[:limit]

    def build_context_manifest(self, worker: str, *, char_budget: int = 16000, max_enriched: int = 20, support_depth: int = 2, search_limit: int = 3) -> dict[str, Any]:
        assignment = self.store.validate_assignment(worker)
        pending_verification = []
        global_memory = GlobalMemory(self.project)
        for kind in ("conclusion", "example", "counterexample", "proof_attempt"):
            for entry in global_memory.read(kind):
                links = entry.get("links") or {}
                if (
                    entry.get("author") == worker
                    and entry.get("status") in {"unverified", "verifying"}
                    and links.get("target_version") == assignment["target_version"]
                    and links.get("obligation_id") == assignment["obligation_id"]
                    and links.get("route_id") == assignment["route_id"]
                    and links.get("assignment_epoch") == assignment["epoch"]
                    and isinstance(links.get("verification_goal"), str)
                    and bool(links["verification_goal"].strip())
                ):
                    pending_verification.append({
                        "source_id": entry["id"], "kind": kind,
                        "status": entry["status"],
                        "verification_goal": links.get("verification_goal"),
                        "title": " ".join(str(
                            links.get("display_title") or entry.get("claim") or ""
                        ).split())[:80],
                    })
        pending_verification.sort(key=lambda item: (str(item["verification_goal"]), item["source_id"]))
        verification_obstacles = []
        for entry in global_memory.read("obstacle"):
            links = entry.get("links") or {}
            if (
                links.get("source_id")
                and links.get("target_version") == assignment["target_version"]
                and links.get("obligation_id") == assignment["obligation_id"]
                and links.get("route_id") == assignment["route_id"]
            ):
                try:
                    payload = json.loads(str(entry.get("evidence") or "{}"))
                    repair = str(payload.get("repair_hints") or "")
                except (TypeError, ValueError):
                    repair = ""
                verification_obstacles.append({
                    "id": entry["id"], "source_id": links["source_id"],
                    "verification_goal": links.get("verification_goal"),
                    "assignment_epoch": links.get("assignment_epoch"),
                    "title": " ".join(str(entry.get("claim") or "").split())[:120],
                    "repair_hints": " ".join(repair.split())[:300],
                })
        verification_obstacles = verification_obstacles[-10:]
        snapshot = self.store.generation()
        with self.store._connect() as db:
            prior = db.execute(
                "SELECT payload FROM context_manifests WHERE worker=? AND assignment_epoch=? AND snapshot_generation=? ORDER BY created_at_utc DESC LIMIT 1",
                (worker, assignment["epoch"], snapshot),
            ).fetchone()
        if prior:
            cached = _load(prior[0], {})
            expected = {"char_budget": char_budget, "max_enriched_facts": max_enriched, "support_depth": support_depth, "search_limit": search_limit}
            if (
                all(cached.get("compression", {}).get(key) == value for key, value in expected.items())
                and [item["source_id"] for item in cached.get("pending_verification", [])]
                == [item["source_id"] for item in pending_verification]
                and [item["id"] for item in cached.get("verification_obstacles", [])]
                == [item["id"] for item in verification_obstacles]
            ):
                return cached
        target = self.store.target(assignment["target_version"])
        route_context = self.route_context(assignment["route_id"], snapshot=snapshot)
        obligation = route_context["obligation"]
        dependency_ids = obligation.get("dependencies") or []
        with self.store._connect() as db:
            fixed_rows = db.execute("SELECT fact_id,relation,assignment_epoch FROM fact_scopes WHERE route_id=? OR obligation_id=? ORDER BY CASE relation WHEN 'closing' THEN 0 WHEN 'direct' THEN 1 WHEN 'input' THEN 2 ELSE 3 END,fact_id", (assignment["route_id"], assignment["obligation_id"])).fetchall()
            if dependency_ids:
                fixed_rows += db.execute(f"SELECT fact_id,'dependency_closing' relation,assignment_epoch FROM fact_scopes WHERE relation='closing' AND obligation_id IN ({','.join('?' for _ in dependency_ids)}) ORDER BY fact_id", dependency_ids).fetchall()
            checkpoints = [dict(row) | {"report": _load(row["report"], {})} for row in db.execute("SELECT * FROM checkpoints WHERE route_id=? ORDER BY event_seq DESC LIMIT 3", (assignment["route_id"],))]
            obstacles = [dict(row) for row in db.execute("SELECT * FROM obstacles WHERE route_id=? AND status='open' ORDER BY last_seen_seq DESC", (assignment["route_id"],))]
        reasons: dict[str, list[str]] = defaultdict(list)
        for row in fixed_rows:
            reasons[str(row["fact_id"])].append(str(row["relation"]))
            if row["assignment_epoch"] == assignment["epoch"]:
                reasons[str(row["fact_id"])].append("assignment_new")
        support, _, omitted = self._support_closure(reasons, support_depth, 300)
        for fact_id in support:
            reasons[fact_id].append("predecessor_support")
        fixed_facts = self._fact_rows(reasons)
        obligation_text = " ".join(str(obligation.get("statement") or "").split())
        already_have_exact_goal = any(
            " ".join(str(fact.get("statement") or "").split()) == obligation_text
            for fact in fixed_facts
        )
        search_query = " ".join([str(route_context["route"].get("expected_result") or ""), str(obligation.get("statement") or ""), *[str(item.get("title") or "") for item in obstacles]])
        search_results = []
        if search_query.strip() and not already_have_exact_goal:
            search_results = self.fact_search(
                search_query, target_version=assignment["target_version"], limit=search_limit,
            )
        for fact in search_results:
            reasons[fact["fact_id"]].append("fts_candidate")
        facts = self._fact_rows(reasons)
        priority = {"closing": 0, "input": 1, "assignment_new": 2, "direct": 3, "dependency_closing": 4, "predecessor_support": 5, "fts_candidate": 6}
        facts.sort(key=lambda item: (min(priority.get(reason, 9) for reason in reasons[item["fact_id"]]), item["fact_id"]))
        rendered, used, enriched = [], 0, 0
        for fact in facts:
            reason = sorted(set(reasons[fact["fact_id"]]), key=lambda item: priority.get(item, 9))
            full = f"[{fact['fact_id']}] {fact['title']}\n{fact['statement']}"
            title_only = f"[{fact['fact_id']}] {fact['title']}"
            is_search_only = reason == ["fts_candidate"]
            if not is_search_only and enriched < max_enriched and used + len(full) <= char_budget:
                item, mode = {**fact, "reasons": reason}, "title_statement"
                used += len(full)
                enriched += 1
            else:
                item, mode = {"fact_id": fact["fact_id"], "title": fact["title"], "status": fact["status"], "reasons": reason}, "title_only"
                used += len(title_only)
            item["mode"] = mode
            rendered.append(item)
        manifest = {"id": uuid.uuid4().hex, "worker": worker, "assignment_epoch": assignment["epoch"], "snapshot_generation": snapshot, "target": target, "obligation": obligation, "route": route_context["route"], "facts": rendered, "pending_verification": pending_verification, "verification_obstacles": verification_obstacles, "checkpoints": checkpoints, "obstacles": obstacles, "compression": {"char_budget": char_budget, "used_chars": used, "max_enriched_facts": max_enriched, "support_depth": support_depth, "search_limit": search_limit, "unexpanded_count": omitted, "title_only_count": sum(item["mode"] == "title_only" for item in rendered)}, "created_at_utc": utc_now()}
        with self.store._tx() as db:
            db.execute("INSERT INTO context_manifests VALUES (?,?,?,?,?,?)", (manifest["id"], worker, assignment["epoch"], snapshot, _dump(manifest), manifest["created_at_utc"]))
            assignment["context_cursor"] = snapshot
            changed = db.execute(
                "UPDATE assignments SET payload=? WHERE worker=? AND epoch=?",
                (_dump(assignment), worker, assignment["epoch"]),
            ).rowcount
            if not changed:
                from danus.control import ControlError

                raise ControlError("assignment changed during context assembly")
        return manifest

    def context_manifest(self, manifest_id: str) -> dict[str, Any]:
        with self.store._connect() as db:
            row = db.execute("SELECT payload FROM context_manifests WHERE id=?", (manifest_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown context manifest: {manifest_id}")
        return _load(row[0], {})

    def list_context_manifests(self, *, worker: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        where = " WHERE worker=?" if worker else ""
        args: list[Any] = [worker] if worker else []
        args.append(max(1, min(int(limit), 100)))
        with self.store._connect() as db:
            rows = db.execute(
                f"SELECT payload FROM context_manifests{where} ORDER BY created_at_utc DESC LIMIT ?", args,
            ).fetchall()
        return [_load(row[0], {}) for row in rows]

    @staticmethod
    def format_context_manifest(manifest: dict[str, Any]) -> str:
        lines = [
            f"# Research context snapshot {manifest['snapshot_generation']}",
            f"Target {manifest['target']['version']}: {manifest['target']['statement']}",
            "Allowed assumptions (copy exact list entries into assumptions_used): "
            + json.dumps(manifest["target"].get("allowed_assumptions") or [], ensure_ascii=False),
            f"Obligation {manifest['obligation']['id']}: {manifest['obligation']['statement']}",
            f"Route {manifest['route']['id']} ({manifest['route']['method_title']}): {manifest['route'].get('expected_result', '')}",
            "", "## Included verified facts",
        ]
        for fact in manifest["facts"]:
            lines.append(f"- [{fact['fact_id']}] {fact['title']} ({', '.join(fact['reasons'])})")
            if fact["mode"] == "title_statement":
                lines.append(f"  {fact['statement']}")
        if manifest.get("pending_verification"):
            lines += ["", "## Durable pending verification",
                      "Resume or submit these staged sources before re-deriving them."]
            lines += [
                f"- [{item['source_id']}] {item['verification_goal']}: {item['title']} ({item['status']})"
                for item in manifest["pending_verification"]
            ]
        if manifest.get("verification_obstacles"):
            lines += ["", "## Verification obstacles"]
            lines += [
                f"- [{item['source_id']}] {item['title']}: {item['repair_hints']}"
                for item in manifest["verification_obstacles"]
            ]
        if manifest["obstacles"]:
            lines += ["", "## Open obstacles"] + [f"- {item['title']}" for item in manifest["obstacles"]]
        lines += ["", "Proof bodies are omitted. Call fact_get(fact_id, include_proof=true) only when needed."]
        return "\n".join(lines)
