from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from danus.control import ControlStore
from danus.core import FactGraph, GlobalMemory
from danus.gateway import server


@contextmanager
def _env(**values):
    old = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _project(tmp: Path) -> tuple[ControlStore, dict]:
    (tmp / "project.json").write_text(
        json.dumps({"name": "P", "control_version": 2}), encoding="utf-8"
    )
    (tmp / "PROBLEM.md").write_text("Prove the target conclusion.", encoding="utf-8")
    store = ControlStore(tmp)
    store.scaffold()
    target = store.propose_target({
        "statement": "The target conclusion holds.",
        "allowed_assumptions": [], "forbidden_assumptions": ["choice"],
        "required_conclusions": ["The target conclusion holds."],
        "fallback_candidates": [],
    })
    store.approve_target(target["version"])
    store.add_route({
        "id": "r1", "obligation_id": "v0001-root-1", "method_family": "direct",
        "expected_result": "The target conclusion holds.",
    })
    assignment = store.assign(
        "high", obligation_id="v0001-root-1", route_id="r1", task="prove it",
    )
    return store, assignment


def _args(assignment: dict, **extra):
    return {
        "target_version": assignment["target_version"],
        "obligation_id": assignment["obligation_id"],
        "route_id": assignment["route_id"],
        "assignment_epoch": assignment["epoch"],
        "claim_role": "unconditional",
        "assumptions_used": [],
        **extra,
    }


def test_v2_global_memory_search_is_bounded_and_evidence_is_opt_in(tmp_path: Path):
    _project(tmp_path)
    GlobalMemory(tmp_path).append(
        "plan", claim="compact recall target", evidence="e" * 20_000, author="test",
    )
    with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="high"):
        compact = server.gm_search("compact recall")
        expanded = server.gm_search("compact recall", include_evidence=True)
    hit = compact["results_by_kind"]["plan"]["results"][0]["entry"]
    assert "evidence" not in hit
    assert len(json.dumps(compact)) < 12_500
    expanded_hit = expanded["results_by_kind"]["plan"]["results"][0]["entry"]
    assert len(expanded_hit["evidence"]) == 1200


def test_fact_submit_claim_role_schema_matches_worker_contract():
    tool = next(item for item in server.build_app("all")._tool_manager.list_tools() if item.name == "fact_submit")
    schemas = tool.parameters["properties"]["claim_role"]["anyOf"]
    enum = next(item["enum"] for item in schemas if "enum" in item)
    assert tuple(enum) == server.CLAIM_ROLES
    contract = (Path(__file__).parents[3] / "agents" / "contracts" / "worker.md").read_text(encoding="utf-8")
    assert all(f"`{role}`" in contract for role in server.CLAIM_ROLES)


def test_all_documented_claim_roles_reach_verifier(tmp_path: Path):
    _, assignment = _project(tmp_path)
    calls = []
    original = server._verify
    server._verify = lambda statement, _proof: (calls.append(statement) or {
        "verdict": "correct", "verification_report": {"summary": "ok"},
    })
    try:
        with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="high", DANUS_VERIFY_URL="mock"):
            results = [
                server.fact_submit(
                    statement=f"Verified claim {index}.", proof="Proof.", display_title=f"Verified claim {index}",
                    **(_args(assignment) | {"claim_role": role}),
                )
                for index, role in enumerate(server.CLAIM_ROLES)
            ]
            invalid = server.fact_submit(
                statement="Invalid role claim.", proof="Proof.", display_title="Invalid role claim",
                **(_args(assignment) | {"claim_role": "theorem"}),
            )
    finally:
        server._verify = original
    assert all(result["accepted"] is True for result in results)
    assert len(calls) == len(server.CLAIM_ROLES)
    assert invalid["verdict"] == "control_error" and "invalid claim_role" in invalid["error"]


def test_v2_submission_requires_current_assignment_binding(tmp_path: Path):
    _, assignment = _project(tmp_path)
    with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="high", DANUS_VERIFY_URL="mock"):
        missing = server.fact_submit(statement="The target conclusion holds.", proof="Proof.")
        assert missing["verdict"] == "control_error" and "target_version" in missing["error"]
        stale = server.fact_submit(
            statement="The target conclusion holds.", proof="Proof.",
            **(_args(assignment) | {"assignment_epoch": "stale"}),
        )
        assert stale["verdict"] == "control_error" and "current assignment" in stale["error"]
        assert FactGraph(tmp_path).list() == []


def test_v2_fact_links_closes_and_records_verifier_cost(tmp_path: Path):
    store, assignment = _project(tmp_path)
    original = server._verify
    server._verify = lambda _s, _p: {
        "verdict": "correct", "verification_report": {"summary": "ok"},
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    try:
        with _env(
            DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="high", DANUS_VERIFY_URL="mock",
            DANUS_CODEX_PRICE_IN="1", DANUS_CODEX_PRICE_OUT="2",
        ):
            result = server.fact_submit(
                statement="The target conclusion holds.", proof="A complete proof.", display_title="Target conclusion",
                closes_obligation=True, **_args(assignment),
            )
    finally:
        server._verify = original
    assert result["accepted"] is True and result["closure"]["closed"] is True
    assert store.obligation_state("v0001-root-1") == "closed"
    linked = store.events("fact_linked")
    assert linked[-1]["assignment_epoch"] == assignment["epoch"]
    costs = store.events("cost")
    assert costs[-1]["component"] == "verification"
    assert costs[-1]["usage"]["input_tokens"] == 10
    assert costs[-1]["cost_usd"] == 0.00002


def test_verifier_inside_worker_round_does_not_need_a_second_wall_budget(tmp_path: Path):
    store, assignment = _project(tmp_path)
    target = store.current_target()
    target["budget"] = {"max_wall_seconds": 30}
    with store._tx() as db:
        db.execute(
            "UPDATE targets SET payload=? WHERE version=?",
            (json.dumps(target), target["version"]),
        )
    parent = store.reserve_call(
        component="worker_round", max_wall_seconds=30, worker="high",
        assignment_epoch=assignment["epoch"], target_version=assignment["target_version"],
        obligation_id=assignment["obligation_id"], route_id=assignment["route_id"],
    )
    store.retry_backend("codex", reason="test nested half-open probe")
    assert store.claim_backend_call("codex")["state"] == "half_open"
    original = server._verify
    server._verify = lambda _s, _p: {"verdict": "correct"}
    try:
        with _env(
            DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="high", DANUS_VERIFY_URL="mock",
            DANUS_CALL_RESERVATION_ID=parent["id"],
        ):
            result = server.fact_submit(
                statement="A nested verified lemma.", proof="Proof.",
                display_title="A nested verified lemma", **_args(assignment),
            )
    finally:
        server._verify = original
        store.cancel_call_reservation(parent["id"], reason="test complete")
    assert result["accepted"] is True, result
    verification_cost = store.events("cost")[-1]
    assert verification_cost["wall_seconds"] == 0
    assert verification_cost["nested_wall_seconds"] >= 0


def test_exact_v2_claim_reuses_verified_fact_without_second_verifier_call(tmp_path: Path):
    store, assignment = _project(tmp_path)
    calls = []
    original = server._verify
    server._verify = lambda _s, _p: (calls.append(1) or {
        "verdict": "correct", "verification_report": {"summary": "ok"},
    })
    try:
        with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="high", DANUS_VERIFY_URL="mock"):
            first = server.fact_submit(
                statement="The target conclusion holds.", proof="First proof.", display_title="Target conclusion", **_args(assignment),
            )
            second = server.fact_submit(
                statement="The target conclusion holds.", proof="Different proof.", display_title="Target conclusion", **_args(assignment),
            )
    finally:
        server._verify = original
    assert len(calls) == 1
    assert second["reused"] is True and second["fact_id"] == first["fact_id"]
    assert len(FactGraph(tmp_path).list()) == 1
    assert store.events("fact_linked")[-1]["reused"] is True


def test_conditional_fact_cannot_close_unconditional_obligation(tmp_path: Path):
    store, assignment = _project(tmp_path)
    original = server._verify
    server._verify = lambda _s, _p: {
        "verdict": "correct", "verification_report": {"summary": "ok"},
    }
    try:
        with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="high", DANUS_VERIFY_URL="mock"):
            result = server.fact_submit(
                statement="The target conclusion holds.", proof="Conditional proof.", display_title="Conditional target claim",
                closes_obligation=True, **(_args(assignment) | {"claim_role": "conditional"}),
            )
    finally:
        server._verify = original
    assert result["accepted"] is True and result["closure"]["closed"] is False
    assert store.obligation_state("v0001-root-1") == "active"


def test_verifier_quota_failure_opens_shared_circuit_and_prevents_retry_storm(tmp_path: Path):
    store, assignment = _project(tmp_path)
    calls = 0
    original = server._verify

    def quota(_statement: str, _proof: str):
        nonlocal calls
        calls += 1
        raise RuntimeError("insufficient_quota: credit balance exhausted")

    server._verify = quota
    try:
        with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="high", DANUS_VERIFY_URL="mock"):
            first = server.fact_submit(statement="A new lemma.", proof="Proof.", display_title="A new lemma", **_args(assignment))
            second = server.fact_submit(statement="Another lemma.", proof="Proof.", display_title="Another lemma", **_args(assignment))
    finally:
        server._verify = original
    assert first["verdict"] == second["verdict"] == "error"
    assert calls == 1
    assert store.assignment("high")["rounds_used"] == 0
    assert store.events("backend_failure")[-1]["failure_class"] == "quota_exhausted"
    assert store.budget_state()["reserved_wall_seconds"] == 0


def test_target_change_during_verification_cannot_publish_under_the_stale_epoch(tmp_path: Path):
    store, assignment = _project(tmp_path)
    original = server._verify

    def change_target(_statement: str, _proof: str):
        replacement = store.propose_target({
            "statement": "Replacement target.",
            "allowed_assumptions": [], "forbidden_assumptions": [],
            "required_conclusions": ["Replacement target."], "fallback_candidates": [],
        })
        store.approve_target(replacement["version"])
        return {"verdict": "correct", "verification_report": {"summary": "correct"}}

    server._verify = change_target
    try:
        with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="high", DANUS_VERIFY_URL="mock"):
            result = server.fact_submit(statement="Late lemma.", proof="Proof.", display_title="Late verified lemma", **_args(assignment))
    finally:
        server._verify = original
    assert result["accepted"] is False and result["verifier_accepted"] is True
    assert "control state changed" in result["write_error"]
    assert FactGraph(tmp_path).list() == []
    assert store.budget_state()["reserved_wall_seconds"] == 0
