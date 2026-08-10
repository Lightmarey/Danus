from __future__ import annotations

import json
from pathlib import Path

from danus.control import ControlError, ControlStore, is_v2_project, parse_codex_usage
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


def test_low_gain_gets_two_normal_slices_then_an_audit_before_stall(tmp_path: Path):
    store, _ = _assigned(tmp_path)
    first = store.evaluate_work_report("high", _low_report(), wall_seconds=1)
    second = store.evaluate_work_report("high", _low_report(), wall_seconds=1)
    third = store.evaluate_work_report("high", _low_report(), wall_seconds=1)
    assert first["decision"] == "continue"
    assert second["decision"] == "audit"
    assert third["decision"] == "stalled"
    assert store.route_state("route-1") == "stalled"


def test_valid_exploration_evidence_is_medium_gain_and_renews_lease(tmp_path: Path):
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
    assert result["assignment"]["lease_remaining"] == 4  # 3 - 1 + 2


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
    for used in (["choice"], ["unknown"]):
        try:
            store.validate_submission(
                "high", target_version="v0001", obligation_id="v0001-T",
                route_id="route-1", assignment_epoch=assignment["epoch"], assumptions_used=used,
            )
            assert False, "out-of-contract assumption should fail"
        except ControlError:
            pass
    result = store.rebuild_read_model()
    assert Path(result["path"]).is_file()
    assert result["targets"] == 1 and result["obligations"] == 1 and result["routes"] == 1


def test_parse_codex_usage_is_version_tolerant(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    log.write_text(
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":4}}\n'
        '{"nested":{"usage":{"input_tokens":12,"reasoning_tokens":3}}}\n',
        encoding="utf-8",
    )
    assert parse_codex_usage(log) == {"input_tokens": 12, "output_tokens": 4, "reasoning_tokens": 3}


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


def test_only_explicit_v2_metadata_enables_control(tmp_path: Path):
    project = tmp_path / "legacy"
    project.mkdir()
    (project / "project.json").write_text('{"name":"legacy"}', encoding="utf-8")
    assert is_v2_project(project) is False
