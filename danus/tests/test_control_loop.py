from __future__ import annotations

import json
from pathlib import Path

from danus.control import ControlStore
from danus.execution import layout as L
from danus.execution import loop


def _worker(tmp: Path, *, budget: dict | None = None) -> tuple[L.WorkerLayout, ControlStore]:
    project = tmp / "P"
    wl = L.WorkerLayout(project / "workers" / "high")
    wl.logs.mkdir(parents=True)
    (project / "project.json").write_text(
        json.dumps({"name": "P", "control_version": 2}), encoding="utf-8"
    )
    (project / "PROBLEM.md").write_text("Prove T", encoding="utf-8")
    store = ControlStore(project)
    store.scaffold()
    target = store.propose_target({
        "statement": "T", "allowed_assumptions": [], "forbidden_assumptions": [],
        "required_conclusions": ["T"], "fallback_candidates": [],
        "budget": budget or {},
    })
    store.approve_target(target["version"])
    store.add_route({
        "id": "r1", "obligation_id": "v0001-root-1",
        "method_family": "direct", "expected_result": "T",
    })
    store.assign("high", obligation_id="v0001-root-1", route_id="r1", task="prove T")
    return wl, store


def _report() -> dict:
    return {
        "route_status": "no_progress", "summary": "nothing new",
        "new_fact_ids": [], "new_evidence_refs": [],
        "new_or_changed_obligations": [], "unresolved_interfaces": ["bridge"],
        "failed_attempt_signatures": ["same"], "novelty_basis": [],
        "recommended_next_action": "audit",
    }


def test_v2_loop_runs_two_exploration_slices_then_independent_audit(tmp_path: Path):
    wl, store = _worker(tmp_path)
    prompts = []
    original = loop.run_round

    def fake(_wl, _role, prompt, log_path, _timeout, *, report_path=None, output_schema=None):
        prompts.append(prompt)
        assert report_path is not None and output_schema == store.work_report_schema
        report_path.write_text(json.dumps(_report()), encoding="utf-8")
        log_path.write_text('{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":2}}\n', encoding="utf-8")
        return 0

    loop.run_round = fake
    try:
        assert loop._run_v2_loop(
            wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, store, beat=0,
        ) == 0
    finally:
        loop.run_round = original
    assert len(prompts) == 3
    assert "independent route audit" not in prompts[0]
    assert "independent route audit" in prompts[2]
    assert store.assignment("high")["status"] == "stalled"
    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert status["state"] == "paused" and status["control_reason"] == "stalled"
    costs = store.events("cost")
    assert len(costs) == 3 and costs[0]["usage"]["input_tokens"] == 3


def test_reusing_the_same_evidence_does_not_keep_renewing_a_route(tmp_path: Path):
    from danus.core import GlobalMemory

    wl, store = _worker(tmp_path)
    evidence = GlobalMemory(store.project).append(
        "obstacle", claim="bridge missing", evidence="checked", author="high", verifiable=False,
    )
    report = _report() | {"route_status": "blocked", "new_evidence_refs": [evidence]}
    first = store.evaluate_work_report("high", report, wall_seconds=1)
    second = store.evaluate_work_report("high", report, wall_seconds=1)
    assert first["gain"] == "medium"
    assert second["gain"] == "low"


def test_timeout_without_report_uses_persisted_infra_budget_not_research_slices(tmp_path: Path):
    wl, store = _worker(tmp_path, budget={"max_infra_attempts": 2, "infra_retry_seconds": [0]})
    original = loop.run_round

    def timeout(_wl, _role, _prompt, log_path, _timeout, **_kwargs):
        log_path.write_text("[worker_loop] round hard-timeout\n", encoding="utf-8")
        return 124

    loop.run_round = timeout
    try:
        assert loop._run_v2_loop(wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, store, beat=0) == 1
    finally:
        loop.run_round = original
    assignment = store.assignment("high")
    assert assignment["status"] == "infra_blocked"
    assert assignment["infra_failure_count"] == 2
    assert assignment["slice_count"] == 0
    assert assignment["consecutive_low"] == 0
    assert [event["component"] for event in store.events("cost")] == ["worker_infra", "worker_infra"]
    assert store.events("work_checkpoint") == []


def test_quota_exhaustion_blocks_without_retry_or_slice_charge(tmp_path: Path):
    wl, store = _worker(tmp_path, budget={"infra_retry_seconds": [0]})
    calls = 0
    original = loop.run_round

    def quota(_wl, _role, _prompt, log_path, _timeout, **_kwargs):
        nonlocal calls
        calls += 1
        log_path.write_text('{"error":"insufficient_quota: credit balance exhausted"}\n', encoding="utf-8")
        return 1

    loop.run_round = quota
    try:
        assert loop._run_v2_loop(wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, store, beat=0) == 1
    finally:
        loop.run_round = original
    assignment = store.assignment("high")
    assert calls == 1
    assert assignment["status"] == "infra_blocked"
    assert assignment["last_failure_class"] == "quota_exhausted"
    assert assignment["slice_count"] == 0
