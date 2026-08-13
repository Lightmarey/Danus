from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from danus.control import ControlError, ControlStore, is_v2_project, parse_codex_usage
from danus import codex
from danus.core import FactGraph, GlobalMemory


def _store(tmp: Path) -> ControlStore:
    project = tmp / "P"
    project.mkdir()
    (project / "project.json").write_text(
        json.dumps({"name": "P", "control_version": 2}), encoding="utf-8"
    )
    (project / "PROBLEM.md").write_text("Prove T.\n", encoding="utf-8")
    store = ControlStore(project)
    store.scaffold()
    return store


def _target(statement: str = "T holds") -> dict:
    return {
        "statement": statement,
        "allowed_assumptions": ["A"],
        "forbidden_assumptions": ["choice"],
        "required_conclusions": [{"id": "T", "statement": statement}],
        "out_of_scope": [],
        "fallback_candidates": ["T holds under A"],
    }


def _assigned(tmp: Path) -> tuple[ControlStore, dict]:
    store = _store(tmp)
    target = store.propose_target(_target())
    store.approve_target(target["version"])
    oid = f"{target['version']}-T"
    store.add_route({
        "id": "route-1", "obligation_id": oid, "method_family": "direct",
        "expected_result": "prove T", "assumptions": ["A"],
    })
    return store, store.assign("high", obligation_id=oid, route_id="route-1", task="prove T")


def _low_report() -> dict:
    return {
        "route_status": "no_progress", "summary": "same attempt",
        "new_fact_ids": [], "new_evidence_refs": [],
        "new_or_changed_obligations": [], "unresolved_interfaces": ["bridge"],
        "failed_attempt_signatures": ["same"], "novelty_basis": [],
        "recommended_next_action": "audit",
    }


def test_target_approval_creates_root_obligation_and_never_auto_approves_fallback(tmp_path: Path):
    store = _store(tmp_path)
    draft = store.propose_target(_target())
    assert store.target_state(draft["version"]) == "draft"
    assert store.current_target_version() is None
    store.approve_target(draft["version"])
    assert store.current_target_version() == "v0001"
    assert store.obligation_state("v0001-T") == "open"
    fallback = store.propose_fallback()["target"]
    assert fallback["version"] == "v0002"
    assert store.target_state("v0002") == "draft"
    assert store.current_target_version() == "v0001"


def test_duplicate_route_requires_novelty_basis(tmp_path: Path):
    store = _store(tmp_path)
    store.approve_target(store.propose_target(_target())["version"])
    base = {
        "obligation_id": "v0001-T", "method_family": "direct",
        "expected_result": "prove T", "assumptions": ["A"],
    }
    store.add_route({"id": "r1", **base})
    try:
        store.add_route({"id": "r2", **base})
        assert False, "duplicate route should fail"
    except ControlError as exc:
        assert "duplicate route" in str(exc)
    assert store.add_route({"id": "r3", **base, "novelty_basis": ["new lemma L"]})["id"] == "r3"


def test_low_gain_gets_two_normal_rounds_then_an_audit_before_stall(tmp_path: Path):
    store, _ = _assigned(tmp_path)
    first = store.evaluate_work_report("high", _low_report(), wall_seconds=1)
    second = store.evaluate_work_report("high", _low_report(), wall_seconds=1)
    third = store.evaluate_work_report("high", _low_report(), wall_seconds=1)
    assert first["decision"] == "continue"
    assert second["decision"] == "audit"
    assert third["decision"] == "stalled"
    assert store.route_state("route-1") == "stalled"


def test_valid_exploration_evidence_is_medium_gain_and_extends_round_budget(tmp_path: Path):
    store, _ = _assigned(tmp_path)
    evidence = GlobalMemory(store.project).append(
        "obstacle", claim="direct route needs compactness", evidence="missing uniform bound",
        author="high", verifiable=False,
    )
    report = _low_report() | {
        "route_status": "blocked", "new_evidence_refs": [evidence],
        "summary": "identified a grounded blocking interface",
    }
    result = store.evaluate_work_report("high", report, wall_seconds=2)
    assert result["gain"] == "medium"
    assert result["decision"] == "continue"
    assert result["assignment"]["rounds_remaining"] == 4  # 3 - 1 + 2


def test_memory_or_self_report_without_state_change_cannot_fake_progress(tmp_path: Path):
    store, _ = _assigned(tmp_path)
    GlobalMemory(store.project).append(
        "plan", claim="try again", evidence="", author="high", verifiable=False,
    )
    report = _low_report() | {"novelty_basis": ["I say this is new"]}
    result = store.evaluate_work_report("high", report, wall_seconds=1)
    assert result["gain"] == "low"


def test_fact_event_is_high_gain_but_stale_target_invalidates_assignment(tmp_path: Path):
    store, assignment = _assigned(tmp_path)
    fact_id = FactGraph(store.project).add(
        problem_id="P", author="high", statement="Lemma L", proof="Proof of L",
    )
    store.append_event(
        "fact_linked", fact_id=fact_id, worker="high",
        assignment_epoch=assignment["epoch"], target_version="v0001",
        obligation_id="v0001-T", route_id="route-1",
    )
    assert store.evaluate_work_report("high", _low_report(), wall_seconds=1)["gain"] == "high"
    second = store.propose_target(_target("T under A"))
    approved = store.approve_target(second["version"])
    assert approved["stale_workers"] == ["high"]
    try:
        store.validate_submission(
            "high", target_version="v0001", obligation_id="v0001-T",
            route_id="route-1", assignment_epoch=assignment["epoch"], assumptions_used=["A"],
        )
        assert False, "stale submission should fail"
    except ControlError as exc:
        assert "not runnable" in str(exc)


def test_assumption_boundary_and_read_model(tmp_path: Path):
    store, assignment = _assigned(tmp_path)
    store.validate_submission(
        "high", target_version="v0001", obligation_id="v0001-T",
        route_id="route-1", assignment_epoch=assignment["epoch"], assumptions_used=["A"],
    )
    for used, expected in (
        (["choice"], "submission uses forbidden assumptions"),
        (["unknown"], "use exact allowed_assumptions entries: ['A']"),
    ):
        try:
            store.validate_submission(
                "high", target_version="v0001", obligation_id="v0001-T",
                route_id="route-1", assignment_epoch=assignment["epoch"], assumptions_used=used,
            )
            assert False, "out-of-contract assumption should fail"
        except ControlError as exc:
            assert expected in str(exc)
    result = store.rebuild_read_model()
    assert Path(result["path"]).is_file()
    assert result["targets"] == 1 and result["obligations"] == 1 and result["routes"] == 1


def test_parse_codex_usage_is_version_tolerant(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    log.write_text(
        '{"type":"turn.completed","usage":{"input_tokens":12,"cached_input_tokens":7,"output_tokens":4,"reasoning_output_tokens":3}}\n'
        '{"nested":{"usage":{"input_tokens":10,"reasoning_tokens":2}}}\n',
        encoding="utf-8",
    )
    assert parse_codex_usage(log) == {
        "input_tokens": 12,
        "cached_input_tokens": 7,
        "fresh_input_tokens": 5,
        "output_tokens": 4,
        "reasoning_tokens": 3,
    }


def test_taint_is_append_only_and_pauses_routes_that_depend_on_the_fact(tmp_path: Path):
    store = _store(tmp_path)
    target = store.propose_target(_target())
    store.approve_target(target["version"])
    fact_id = FactGraph(store.project).add(
        problem_id="P", author="high", statement="Lemma", proof="Proof",
    )
    store.add_route({
        "id": "uses-lemma", "obligation_id": "v0001-T", "method_family": "reduction",
        "expected_result": "T", "input_fact_ids": [fact_id],
    })
    store.assign("high", obligation_id="v0001-T", route_id="uses-lemma", task="use lemma")
    result = store.taint_fact(fact_id, "later contradiction")
    assert result["stale_workers"] == ["high"]
    assert store.assignment("high")["status"] == "tainted"
    assert store.fact_tainted(fact_id) is True
    assert FactGraph(store.project).exists(fact_id) is True  # review marker, not destructive revoke


def test_route_input_fact_is_reusable_without_a_fact_link_event(tmp_path: Path):
    store = _store(tmp_path)
    target = store.propose_target(_target())
    store.approve_target(target["version"])
    fact_id = FactGraph(store.project).add(
        problem_id="P", author="legacy", statement="Imported route input",
        proof="Proof",
    )
    store.add_route({
        "id": "route-input", "obligation_id": "v0001-T",
        "method_family": "reuse", "expected_result": "T",
        "input_fact_ids": [fact_id],
    })
    store.rebuild_read_model()
    assert store.events("fact_linked") == []
    assert store.reusable_fact("Imported route input", []) == fact_id


def test_only_explicit_v2_metadata_enables_control(tmp_path: Path):
    project = tmp_path / "legacy"
    project.mkdir()
    (project / "project.json").write_text('{"name":"legacy"}', encoding="utf-8")
    assert is_v2_project(project) is False


def test_project_budget_warns_audits_then_blocks_expensive_work(tmp_path: Path):
    store = _store(tmp_path)
    contract = _target() | {"budget": {"max_wall_seconds": 100}}
    target = store.propose_target(contract)
    store.approve_target(target["version"])
    store.add_route({
        "id": "r1", "obligation_id": "v0001-T", "method_family": "direct",
        "expected_result": "T",
    })
    store.assign("high", obligation_id="v0001-T", route_id="r1", task="T")
    store.record_cost(component="worker_round", wall_seconds=70)
    assert store.budget_state()["stage"] == "warn"
    store.record_cost(component="verification", wall_seconds=15)
    assert store.validate_assignment("high")["audit_required"] is True
    store.record_cost(component="worker_round", wall_seconds=15)
    try:
        store.validate_assignment("high")
        assert False, "100% project budget must block another round"
    except ControlError as exc:
        assert "project budget exhausted" in str(exc)


def test_consult_ledger_also_attributes_v2_control_cost(tmp_path: Path):
    from danus.strategy.ledger import log_spend

    store = _store(tmp_path)
    target = store.propose_target(_target())
    store.approve_target(target["version"])
    total = log_spend(str(store.project), {
        "model": "m", "effort": "high", "status": "completed",
        "usage": {"input": 10, "output": 4, "reasoning": 2},
        "cost_usd": 0.25, "seconds": 3,
    })
    assert total == "0.2500"
    event = store.events("cost")[-1]
    assert event["component"] == "strategy_consult"
    assert event["usage"]["output_tokens"] == 4


def test_authoring_call_records_v2_wall_time_without_inventing_token_cost(tmp_path: Path):
    import subprocess
    from danus.human_summary import server as summary_server

    store = _store(tmp_path)
    original = summary_server.driver.run_codex
    summary_server.driver.run_codex = lambda *args, **kwargs: subprocess.CompletedProcess(
        ["codex"], 0, stdout="report", stderr="",
    )
    try:
        result = summary_server._drive_scoped("prompt", store.project)
    finally:
        summary_server.driver.run_codex = original
    assert result["status"] == "ok"
    event = store.events("cost")[-1]
    assert event["component"] == "human_summary"
    assert event["cost_usd"] is None


def test_failure_classification_and_unknown_billing_are_explicit(tmp_path: Path, monkeypatch):
    log = tmp_path / "codex.jsonl"
    log.write_text('{"error":"HTTP 429 rate limit","retry_after":17}\n', encoding="utf-8")
    outcome = codex.classify_failure(1, log)
    assert outcome["failure_class"] == "rate_limited"
    assert outcome["retryable"] is True
    assert outcome["retry_after_seconds"] == 17
    store, _ = _assigned(tmp_path)
    monkeypatch.setenv("DANUS_CODEX_PRICE_IN", "1")
    monkeypatch.setenv("DANUS_CODEX_PRICE_OUT", "1")
    event = store.record_cost(component="worker_infra", wall_seconds=2)
    assert event["cost_status"] == "unknown"
    assert store.budget_state()["unknown_cost_events"] == 1


def test_infra_failure_and_project_circuit_survive_restart(tmp_path: Path):
    store, _ = _assigned(tmp_path)
    target = store.current_target()
    target["budget"] = {"infra_retry_seconds": [0], "max_infra_attempts": 3}
    with store._tx() as db:
        db.execute("UPDATE targets SET payload=? WHERE version=?", (json.dumps(target), target["version"]))
    outcome = {"failure_class": "transient_network", "retryable": True, "retry_after_seconds": 0, "error_signature": "network", "return_code": 1}
    store.record_worker_infra_failure("high", outcome, wall_seconds=1)
    reopened = ControlStore(store.project)
    assignment = reopened.assignment("high")
    assert assignment["infra_failure_count"] == 1
    assert assignment["status"] == "waiting_retry"
    assert reopened.claim_backend_call()["allowed"] is True
    assert reopened.claim_backend_call()["allowed"] is False  # only one half-open probe


def test_call_reservations_atomically_prevent_concurrent_budget_overshoot(tmp_path: Path):
    store = _store(tmp_path)
    target = store.propose_target(_target() | {"budget": {"max_wall_seconds": 100}})
    store.approve_target(target["version"])
    barrier = threading.Barrier(2)
    results: list[str] = []

    def reserve() -> None:
        barrier.wait()
        try:
            store.reserve_call(component="worker_round", max_wall_seconds=60)
            results.append("reserved")
        except ControlError:
            results.append("blocked")

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["blocked", "reserved"]
    assert store.budget_state()["reserved_wall_seconds"] == 60


def test_reservation_settlement_and_crash_expiry_release_budget(tmp_path: Path):
    store = _store(tmp_path)
    target = store.propose_target(_target() | {"budget": {"max_wall_seconds": 100}})
    store.approve_target(target["version"])
    first = store.reserve_call(component="verification", max_wall_seconds=60)
    store.record_cost(component="verification", wall_seconds=10, reservation_id=first["id"])
    assert store.budget_state()["reserved_wall_seconds"] == 0
    expired = store.reserve_call(component="authoring", max_wall_seconds=80)
    with store._tx() as db:
        db.execute("UPDATE call_reservations SET expires_at_epoch=0 WHERE id=?", (expired["id"],))
    reopened = ControlStore(store.project)
    assert reopened.recover_call_reservations() == [expired["id"]]
    state = reopened.budget_state()
    assert state["reserved_wall_seconds"] == 0 and state["wall_seconds"] == 10


def test_nested_verification_uses_parent_wall_reservation_without_double_counting(tmp_path: Path):
    store = _store(tmp_path)
    target = store.propose_target(_target() | {"budget": {"max_wall_seconds": 100}})
    store.approve_target(target["version"])
    scope = {
        "worker": "high", "assignment_epoch": "epoch", "target_version": "v0001",
        "obligation_id": "o1", "route_id": "r1",
    }
    generation = store.generation()
    parent = store.reserve_call(
        component="worker_round", max_wall_seconds=100, **scope,
    )
    assert store.generation() == generation
    child = store.reserve_call(
        component="verification", max_wall_seconds=60,
        parent_reservation_id=parent["id"], **scope,
    )
    assert child["reserved_wall_seconds"] == 0
    assert store.budget_state()["reserved_wall_seconds"] == 100
    nested = store.record_cost(
        component="verification", wall_seconds=6, reservation_id=child["id"], **scope,
    )
    assert nested["wall_seconds"] == 0
    assert nested["nested_wall_seconds"] == 6
    store.record_cost(
        component="worker_round", wall_seconds=8, reservation_id=parent["id"], **scope,
    )
    assert store.budget_state()["wall_seconds"] == 8


def test_nested_verification_extends_parent_for_drain(tmp_path: Path):
    store = _store(tmp_path)
    target = store.propose_target(_target())
    store.approve_target(target["version"])
    parent = store.reserve_call(component="worker_round", max_wall_seconds=30)
    child = store.reserve_call(
        component="verification", max_wall_seconds=3600,
        parent_reservation_id=parent["id"],
    )
    assert child["requested_wall_seconds"] == 3600
    assert store.nested_call_timeout(parent["id"], 3600) == 3600
    assert store.active_nested_call(parent["id"])["id"] == child["id"]
    parent_row = next(row for row in store.active_call_reservations() if row["id"] == parent["id"])
    assert parent_row["expires_at_epoch"] > child["expires_at_epoch"]
    store.record_cost(component="verification", wall_seconds=1, reservation_id=child["id"])
    assert store.active_nested_call(parent["id"]) is None


def test_dead_worker_recovery_cancels_nested_call_and_releases_parent(tmp_path: Path):
    store, assignment = _assigned(tmp_path)
    assignment["status"] = "running"
    store.save_assignment(assignment)
    scope = {
        "worker": "high", "assignment_epoch": assignment["epoch"],
        "target_version": assignment["target_version"],
        "obligation_id": assignment["obligation_id"],
        "route_id": assignment["route_id"],
    }
    parent = store.reserve_call(
        component="worker_round", max_wall_seconds=30, **scope,
    )
    child = store.reserve_call(
        component="verification", max_wall_seconds=60,
        parent_reservation_id=parent["id"], **scope,
    )

    result = store.recover_worker_interruption(
        "high", wall_seconds=7, reason="test_crash",
    )

    assert result["recovered"] is True
    assert store.assignment("high")["status"] == "assigned"
    assert store.assignment("high")["rounds_used"] == 0
    assert store.active_call_reservations() == []
    assert store.events("call_reservation_cancelled")[-1]["reservation_id"] == child["id"]
    assert store.events("round_interrupted")[-1]["reason"] == "test_crash"
    cost = store.events("cost")[-1]
    assert cost["reservation_id"] == parent["id"]
    assert cost["wall_seconds"] == 7
    assert cost["usage_status"] == "unavailable"
    assert store.recover_worker_interruption("high")["recovered"] is False


def test_authoring_and_consult_preflight_stop_before_an_over_budget_call(tmp_path: Path):
    from danus.human_summary import server as summary_server
    from danus.strategy import cli as strategy_cli

    store = _store(tmp_path)
    target = store.propose_target(_target() | {"budget": {"max_wall_seconds": 100}})
    store.approve_target(target["version"])
    calls = []
    original = summary_server._drive
    summary_server._drive = lambda _prompt: (calls.append("summary") or {"status": "ok"})
    try:
        summary = summary_server._drive_scoped("prompt", store.project)
    finally:
        summary_server._drive = original
    consult = strategy_cli._consult_scoped(str(store.project), "consult:test", 101, lambda: calls.append("consult"))
    assert summary["status"] == consult["status"] == "budget_exhausted"
    assert calls == []
    assert store.budget_state()["reserved_wall_seconds"] == 0


def test_correctable_consult_rejection_does_not_open_circuit_or_spend_reservation(tmp_path: Path):
    from danus.strategy import cli as strategy_cli

    class FakeBadRequestError(Exception):
        status_code = 400

    store = _store(tmp_path)
    target = store.propose_target(_target() | {"budget": {
        "max_wall_seconds": 100, "max_cost_usd": 1,
        "strict_cost_reservations": True, "max_call_cost_usd": .6,
    }})
    store.approve_target(target["version"])

    def reject():
        raise FakeBadRequestError("background is not permitted")

    failed = strategy_cli._consult_scoped(
        str(store.project), "consult:test", 10, reject,
        transport="gpt_pro", model="m", effort="max",
    )
    assert failed["status"] == "failed"
    assert failed["failure_class"] == "request_rejected"
    assert store.backend_circuits() == []
    cost = store.events("cost")[-1]
    assert cost["cost_usd"] == 0 and cost["cost_status"] == "known"
    assert store.budget_state()["reserved_cost_usd"] == 0

    completed = strategy_cli._consult_scoped(
        str(store.project), "consult:test", 10,
        lambda: {"status": "completed", "reply": "ok", "usage": {},
                 "cost_usd": 0.1, "seconds": .1},
        transport="gpt_pro", model="m", effort="max",
    )
    assert completed["status"] == "completed"


def test_unknown_consult_failure_settles_conservative_cost_and_opens_circuit(tmp_path: Path):
    from danus.strategy import cli as strategy_cli

    store = _store(tmp_path)
    target = store.propose_target(_target() | {"budget": {
        "max_wall_seconds": 100, "max_cost_usd": 1,
        "strict_cost_reservations": True, "max_call_cost_usd": .6,
        "infra_retry_seconds": [30],
    }})
    store.approve_target(target["version"])

    def disconnect():
        raise ConnectionError("connection reset after request")

    failed = strategy_cli._consult_scoped(
        str(store.project), "consult:test", 10, disconnect,
        transport="gpt_pro", model="m", effort="high",
    )
    assert failed["status"] == "failed" and failed["cost_usd"] is None
    cost = store.events("cost")[-1]
    assert cost["cost_usd"] == .6 and cost["cost_status"] == "estimated_ceiling"
    assert store.backend_circuits()[0]["state"] == "open"


def test_strict_cost_budget_requires_and_reserves_a_per_call_ceiling(tmp_path: Path):
    store = _store(tmp_path)
    target = store.propose_target(_target() | {"budget": {"max_cost_usd": 1, "strict_cost_reservations": True, "max_call_cost_usd": .6}})
    store.approve_target(target["version"])
    first = store.reserve_call(component="verification", max_wall_seconds=10)
    assert first["reserved_cost_usd"] == .6
    event = store.record_cost(component="verification", wall_seconds=1, reservation_id=first["id"])
    assert event["cost_status"] == "estimated_ceiling" and event["cost_usd"] == .6
    try:
        store.reserve_call(component="authoring", max_wall_seconds=10)
        assert False, "concurrent reservations must not exceed the strict USD ceiling"
    except ControlError as exc:
        assert "cost budget" in str(exc)


def test_infrastructure_wall_limit_is_shared_across_concurrent_workers(tmp_path: Path):
    store, assignment = _assigned(tmp_path)
    target = store.current_target()
    target["budget"] = {"max_infra_wall_seconds": 10, "max_infra_attempts": 5, "infra_retry_seconds": [0]}
    with store._tx() as db:
        db.execute("UPDATE targets SET payload=? WHERE version=?", (json.dumps(target), target["version"]))
    store.assign("medium", obligation_id=assignment["obligation_id"], route_id=assignment["route_id"], task="prove T independently")
    outcome = {"failure_class": "transient_network", "retryable": True, "retry_after_seconds": 0, "error_signature": "network", "return_code": 1}
    first = store.record_worker_infra_failure("high", outcome, wall_seconds=6)
    second = store.record_worker_infra_failure("medium", outcome, wall_seconds=5)
    assert first["blocked"] is False
    assert second["blocked"] is True
    assert store.backend_circuits()[0]["infra_wall_seconds"] == 11


def test_blocked_backend_requires_audited_retry_and_only_one_probe(tmp_path: Path):
    store, _ = _assigned(tmp_path)
    quota = {"failure_class": "quota_exhausted", "retryable": False, "retry_after_seconds": 0, "error_signature": "quota", "return_code": 1}
    assert store.record_worker_infra_failure("high", quota, wall_seconds=1)["blocked"] is True
    result = store.retry_backend("codex", reason="provider quota renewed")
    assert result["resumed_workers"] == ["high"]
    assert store.assignment("high")["status"] == "waiting_retry"
    assert store.claim_backend_call("codex")["allowed"] is True
    assert store.claim_backend_call("codex")["allowed"] is False


def test_existing_v2_database_adds_resilience_columns_in_place(tmp_path: Path):
    project = tmp_path / "P"
    (project / "control").mkdir(parents=True)
    (project / "project.json").write_text('{"name":"P","control_version":2}', encoding="utf-8")
    (project / "PROBLEM.md").write_text("Prove T", encoding="utf-8")
    with sqlite3.connect(project / "control" / "control.sqlite3") as db:
        db.execute("CREATE TABLE backend_circuits(provider_key TEXT PRIMARY KEY,state TEXT NOT NULL,consecutive_failures INTEGER NOT NULL,opened_until REAL,failure_class TEXT,updated_at_utc TEXT NOT NULL)")
    store = ControlStore(project)
    store.scaffold()
    with store._connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(backend_circuits)")}
    assert "infra_wall_seconds" in columns


def test_existing_v2_database_migrates_round_vocabulary_once(tmp_path: Path):
    store, assignment = _assigned(tmp_path)
    old = dict(assignment)
    old["slice_count"] = old.pop("rounds_used")
    old["lease_remaining"] = old.pop("rounds_remaining")
    old["max_slices"] = old.pop("max_rounds")
    old["slice_timeout"] = old.pop("round_timeout_seconds")
    with store._connect() as db:
        db.execute("ALTER TABLE checkpoints RENAME COLUMN rounds_used TO slice_count")
        db.execute(
            "UPDATE assignments SET payload=? WHERE worker='high'", (json.dumps(old),),
        )
        db.execute(
            "INSERT INTO events(event_id,timestamp_utc,event,payload) VALUES (?,?,?,?)",
            ("old-slice-event", "2026-01-01T00:00:00Z", "slice_interrupted",
             json.dumps({"slice_count": 1, "component": "worker_slice"})),
        )
        db.execute(
            "INSERT INTO call_reservations VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("old-reservation", "worker_slice", "codex", 10, None, "cancelled",
             "{}", "2026-01-01T00:00:00Z", 1, "2026-01-01T00:00:01Z"),
        )
        db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        db.commit()

    reopened = ControlStore(store.project)
    reopened.scaffold()
    migrated = reopened.assignment("high")
    assert migrated["rounds_used"] == 0 and migrated["rounds_remaining"] == 3
    assert migrated["max_rounds"] == 12 and migrated["round_timeout_seconds"] == 5400
    assert not {"slice_count", "lease_remaining", "max_slices", "slice_timeout"}.intersection(migrated)
    event = reopened.events("round_interrupted")[-1]
    assert event["rounds_used"] == 1 and event["component"] == "worker_round"
    with reopened._connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(checkpoints)")}
        component = db.execute(
            "SELECT component FROM call_reservations WHERE id='old-reservation'",
        ).fetchone()[0]
        version = db.execute(
            "SELECT value FROM meta WHERE key='schema_version'",
        ).fetchone()[0]
    assert "rounds_used" in columns and "slice_count" not in columns
    assert component == "worker_round" and version == "4"


def test_existing_v3_database_migrates_fact_submission_identity(tmp_path: Path):
    store, _ = _assigned(tmp_path)
    payload = json.dumps({"scope": {"assignment_epoch": "epoch-old"}})
    with store._connect() as db:
        db.execute("ALTER TABLE pending_facts RENAME TO pending_facts_v4")
        db.execute("CREATE TABLE pending_facts(fact_id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL)")
        db.execute("INSERT INTO pending_facts VALUES ('fact-old',?,'complete')", (payload,))
        db.execute("DROP TABLE pending_facts_v4")
        db.execute("ALTER TABLE fact_scopes RENAME TO fact_scopes_v4")
        db.execute(
            "CREATE TABLE fact_scopes("
            "fact_id TEXT NOT NULL,target_version TEXT NOT NULL,obligation_id TEXT NOT NULL,"
            "route_id TEXT NOT NULL,assignment_epoch TEXT NOT NULL,claim_role TEXT NOT NULL,"
            "relation TEXT NOT NULL,event_seq INTEGER,"
            "PRIMARY KEY(fact_id,target_version,obligation_id,route_id,relation))"
        )
        db.execute("INSERT INTO fact_scopes VALUES ('fact-old','v0001','v0001-T','r1','epoch-old','unconditional','direct',NULL)")
        db.execute("DROP TABLE fact_scopes_v4")
        db.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        db.commit()

    reopened = ControlStore(store.project)
    reopened.scaffold()
    with reopened._connect() as db:
        pending_columns = {row[1] for row in db.execute("PRAGMA table_info(pending_facts)")}
        scope_pk = {row[1]: row[5] for row in db.execute("PRAGMA table_info(fact_scopes)")}
        pending = db.execute("SELECT submission_id,fact_id,status FROM pending_facts").fetchone()
        scope = db.execute("SELECT assignment_epoch FROM fact_scopes").fetchone()
    assert pending_columns >= {"submission_id", "fact_id"}
    assert scope_pk["assignment_epoch"] > 0
    assert tuple(pending) == ("fact-old", "fact-old", "complete")
    assert scope[0] == "epoch-old"


def test_target_change_during_a_round_settles_cost_but_discards_research_state(tmp_path: Path):
    store, assignment = _assigned(tmp_path)
    reservation = store.reserve_call(component="worker_round", max_wall_seconds=10, worker="high", assignment_epoch=assignment["epoch"])
    replacement = store.propose_target(_target("replacement target"))
    store.approve_target(replacement["version"])
    result = store.evaluate_work_report("high", _low_report(), wall_seconds=3, reservation_id=reservation["id"])
    assert result["decision"] == "invalidated" and result["assignment"]["rounds_used"] == 0
    assert store.events("round_discarded")[-1]["reason"] == "assignment is not runnable: stale"
    assert store.budget_state()["wall_seconds"] == 3
    assert store.budget_state()["reserved_wall_seconds"] == 0
