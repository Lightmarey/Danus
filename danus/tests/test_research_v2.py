from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from danus.control import ControlError, ControlStore
from danus.core import FactGraph
from danus.research import ResearchQuery

observability_app = importlib.import_module("danus.observability.app")


def _project(tmp_path: Path) -> tuple[Path, ControlStore, dict]:
    project = tmp_path / "P"
    project.mkdir()
    (project / "project.json").write_text('{"name":"P","control_version":2}', encoding="utf-8")
    (project / "PROBLEM.md").write_text("Prove T", encoding="utf-8")
    store = ControlStore(project)
    store.scaffold()
    target = store.propose_target({
        "statement": "T holds", "allowed_assumptions": [], "forbidden_assumptions": [],
        "required_conclusions": [{"id": "T", "statement": "T holds"}],
        "fallback_candidates": [], "budget": {"max_wall_seconds": 100},
    })
    return project, store, target


def test_target_commands_are_transactional_idempotent_and_withdraw_to_no_target(tmp_path: Path):
    _, store, target = _project(tmp_path)
    generation = store.generation()
    first = store.approve_target(target["version"], request_id="approve-1", expected_generation=generation)
    repeated = store.approve_target(target["version"], request_id="approve-1", expected_generation=generation)
    assert repeated == first
    assert store.current_target_version() == "v0001"
    assert store.obligation_state("v0001-T") == "open"
    try:
        store.approve_target(target["version"], request_id="different", expected_generation=generation)
        assert False, "a stale generation must conflict"
    except ControlError as exc:
        assert "stale generation" in str(exc) or "not a draft" in str(exc)
    store.withdraw_target("v0001", reason="bad goal", request_id="withdraw-1", expected_generation=store.generation())
    assert store.current_target_version() is None
    assert store.target_state("v0001") == "withdrawn"


def test_approval_failure_rolls_back_every_control_change(tmp_path: Path, monkeypatch):
    _, store, first = _project(tmp_path)
    store.approve_target(first["version"])
    store.add_route({"id": "r1", "obligation_id": "v0001-T", "method_title": "Direct", "expected_result": "T"})
    assignment = store.assign("high", obligation_id="v0001-T", route_id="r1", task="T")
    second = store.propose_target({
        "statement": "U holds", "allowed_assumptions": [], "forbidden_assumptions": [],
        "required_conclusions": [{"id": "U", "statement": "U holds"}], "fallback_candidates": [],
    })
    original = store._event

    def fail_mid_approval(db, event, **payload):
        if event == "target_approved":
            raise RuntimeError("injected transaction interruption")
        return original(db, event, **payload)

    monkeypatch.setattr(store, "_event", fail_mid_approval)
    try:
        store.approve_target(second["version"])
        assert False, "injected failure must escape"
    except RuntimeError:
        pass
    assert store.current_target_version() == "v0001"
    assert store.target_state("v0002") == "draft"
    assert store.assignment("high")["epoch"] == assignment["epoch"]
    assert store.assignment("high")["status"] == "assigned"
    try:
        store.obligation("v0002-U")
        assert False, "rolled-back root obligation must not exist"
    except ControlError:
        pass


def test_file_backed_v2_state_is_imported_once_and_preserved(tmp_path: Path):
    project = tmp_path / "legacy-v2"
    control = project / "control"
    for name in ("targets", "obligations", "routes", "assignments"):
        (control / name).mkdir(parents=True, exist_ok=True)
    (project / "project.json").write_text('{"name":"P","control_version":2}', encoding="utf-8")
    target = {"version": "v0001", "statement": "T", "allowed_assumptions": [], "forbidden_assumptions": [], "required_conclusions": [{"id": "T", "statement": "T"}], "fallback_candidates": [], "budget": {"max_cost_usd": 10}}
    obligation = {"id": "v0001-T", "target_version": "v0001", "statement": "T", "kind": "root", "dependencies": [], "closure": "verified"}
    route = {"id": "r1", "target_version": "v0001", "obligation_id": "v0001-T", "method_family": "direct", "signature": "sig", "expected_result": "T", "input_fact_ids": []}
    assignment = {"worker": "high", "epoch": "epoch-1", "status": "running", "target_version": "v0001", "obligation_id": "v0001-T", "route_id": "r1", "slice_count": 2, "max_slices": 12}
    for folder, name, value in (("targets", "v0001", target), ("obligations", "v0001-T", obligation), ("routes", "r1", route), ("assignments", "high", assignment)):
        (control / folder / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    events = [
        {"event_id": "e1", "timestamp_utc": "2026-01-01T00:00:00Z", "event": "target_approved", "target_version": "v0001"},
        {"event_id": "e2", "timestamp_utc": "2026-01-01T00:00:01Z", "event": "obligation_state", "target_version": "v0001", "obligation_id": "v0001-T", "state": "active"},
        {"event_id": "e3", "timestamp_utc": "2026-01-01T00:00:02Z", "event": "route_state", "target_version": "v0001", "obligation_id": "v0001-T", "route_id": "r1", "state": "active"},
        {"event_id": "e4", "timestamp_utc": "2026-01-01T00:00:03Z", "event": "cost", "target_version": "v0001", "cost_usd": 2, "wall_seconds": 4},
    ]
    (control / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
    store = ControlStore(project)
    store.scaffold()
    assert store.current_target_version() == "v0001"
    assert store.obligation_state("v0001-T") == "active"
    assert store.route("r1")["method_key"] == "direct"
    assert store.assignment("high")["epoch"] == "epoch-1"
    assert store.budget_state()["cost_usd"] == 2
    assert (control / "MIGRATED_TO_SQLITE").is_file()
    assert (control / "targets" / "v0001.json").is_file()
    before = len(store.events())
    ControlStore(project).scaffold()
    assert len(store.events()) == before


def test_fact_title_pending_recovery_scopes_and_manifest_are_deterministic(tmp_path: Path):
    project, store, target = _project(tmp_path)
    store.approve_target(target["version"])
    store.add_route({"id": "r1", "obligation_id": "v0001-T", "method_title": "Direct proof", "expected_result": "T holds"})
    assignment = store.assign("high", obligation_id="v0001-T", route_id="r1", task="prove T")
    graph = FactGraph(project)
    fact_id = graph.add(problem_id="P", author="high", display_title="Readable target theorem", statement="T holds", proof="proof")
    # Presentation metadata is deliberately outside content addressing and is first-write-wins.
    assert graph.add(problem_id="P", author="high", display_title="Replacement title", statement="T holds", proof="proof") == fact_id
    assert "title: Readable target theorem" in (graph.get_raw(fact_id) or "")
    scope = {"worker": "high", "target_version": "v0001", "obligation_id": "v0001-T", "route_id": "r1", "assignment_epoch": assignment["epoch"], "claim_role": "unconditional", "assumptions_used": []}
    store.prepare_fact(fact_id, {"reused": False, "scope": scope})
    # A fresh store recovers the Markdown-written / DB-not-finalized interruption.
    recovered = ControlStore(project)
    recovered.scaffold()
    assert recovered.events("fact_linked")[-1]["fact_id"] == fact_id
    recovered.set_obligation_state("v0001-T", "closed", actor="test", fact_id=fact_id, assignment_epoch=assignment["epoch"])
    query = ResearchQuery(project)
    first = query.build_context_manifest("high")
    second = query.build_context_manifest("high")
    assert first == second
    assert first["facts"][0]["title"] == "Readable target theorem"
    assert "assignment_new" in first["facts"][0]["reasons"]
    manifest = query.target_proof_manifest("v0001")
    assert manifest["complete"] is True and manifest["closing_fact_ids"] == [fact_id]
    research = query.target_research_manifest("v0001")
    assert research["fact_ids"] == [fact_id]
    from danus.human_summary import assemble as summary_assemble
    assert "T holds" in summary_assemble.fact_bundle(project)
    recovered.taint_fact(fact_id, "test invalidation")
    assert query.target_research_manifest("v0001")["fact_ids"] == []
    assert query.target_proof_manifest("v0001")["complete"] is False


def test_same_fact_can_be_shared_by_routes_and_indexed_reads_do_not_open_markdown(tmp_path: Path, monkeypatch):
    project, store, target = _project(tmp_path)
    store.approve_target(target["version"])
    store.add_route({"id": "r1", "obligation_id": "v0001-T", "method_title": "Method A", "expected_result": "T"})
    store.add_route({"id": "r2", "obligation_id": "v0001-T", "method_title": "Method B", "expected_result": "T", "novelty_basis": ["different method"]})
    fact_id = FactGraph(project).add(problem_id="P", author="high", display_title="Shared supporting lemma", statement="L", proof="proof")
    for route in ("r1", "r2"):
        store.prepare_fact(fact_id, {"reused": route == "r2", "scope": {"worker": "high", "target_version": "v0001", "obligation_id": "v0001-T", "route_id": route, "assignment_epoch": route, "claim_role": "unconditional", "assumptions_used": []}})
        store.finalize_fact(fact_id)
    query = ResearchQuery(project)
    assert query.route_context("r1")["fact_group"]["facts"][0]["shared"] is True
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("Markdown read")) if self.suffix == ".md" else original(self, *a, **k))
    assert query.fact_get(fact_id)["title"] == "Shared supporting lemma"
    assert query.fact_search("supporting lemma")[0]["fact_id"] == fact_id
    from danus.human_summary import assemble as summary_assemble
    assert "Shared supporting lemma" not in summary_assemble.fact_bundle(project)
    assert "## statement\n\nL" in summary_assemble.fact_bundle(project)


def test_control_http_requires_capability_origin_and_generation(tmp_path: Path, monkeypatch):
    project, store, target = _project(tmp_path)
    monkeypatch.setenv("DANUS_DASHBOARD_PROJECT", str(project))
    client = TestClient(observability_app.app)
    body = {"request_id": "http-approve", "expected_generation": store.generation()}
    path = f"/api/control/targets/{target['version']}/approve"
    assert client.post(path, json=body).status_code == 401
    headers = {"X-Danus-Control-Token": observability_app.CONTROL_TOKEN, "Origin": "http://evil.invalid"}
    assert client.post(path, json=body, headers=headers).status_code == 403
    headers["Origin"] = "http://testserver"
    assert client.post(path, json=body, headers=headers).status_code == 200
    stale = {"request_id": "stale-approve", "expected_generation": body["expected_generation"]}
    assert client.post(path, json=stale, headers=headers).status_code == 409


def test_browser_view_state_has_no_persistent_or_write_path():
    script = (Path(observability_app.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "const viewPins = new Set()" in script
    assert "localStorage" not in script
    assert "e.connectionFailure = true" in script
    assert "e.status === 410" in script
    assert "function renderFactResearchMap(d)" in script
    assert "selectFactGraphRoute" in script
    assert "function renderResearchMap(d)" in script
    assert "Show all routes" in (Path(observability_app.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_10k_fact_30k_edge_indexed_payload_and_local_graph_bound(tmp_path: Path):
    project, store, target = _project(tmp_path)
    store.approve_target(target["version"])
    store.add_route({"id": "r1", "obligation_id": "v0001-T", "method_title": "Scale route", "expected_result": "T"})
    facts = [(f"f{i:05d}", f"Fact {i}", f"Statement {i}", "active", "") for i in range(10_000)]
    edges = []
    for i in range(1, 10_000):
        for gap in (1, 2, 3):
            if i >= gap:
                edges.append((f"f{i-gap:05d}", f"f{i:05d}"))
    with store._tx() as db:
        db.executemany("INSERT INTO facts(fact_id,title,statement,proof,intuition,author,problem_id,status,raw) VALUES (?,?,?,'','','','P',?,?)", facts)
        db.executemany("INSERT INTO facts_fts(fact_id,title,statement,proof) VALUES (?,?,?,'')", [(row[0], row[1], row[2]) for row in facts])
        db.executemany("INSERT INTO fact_edges VALUES (?,?)", edges)
        db.execute("INSERT INTO fact_scopes VALUES ('f09999','v0001','v0001-T','r1','','unconditional','direct',NULL)")
        store._bump(db)
    query = ResearchQuery(project)
    started = time.monotonic()
    research_map = query.research_map("v0001")
    route = query.route_context("r1")
    elapsed = time.monotonic() - started
    assert len(json.dumps(research_map).encode()) < 500_000
    assert len(route["fact_group"]["facts"]) <= 300
    assert route["fact_group"]["unexpanded_count"] > 0
    assert len(edges) >= 29_000 and elapsed < 5
