from __future__ import annotations

import json
from pathlib import Path

from danus.control import ControlStore
from danus.core import GlobalMemory
from danus.execution import layout as L
from danus.execution import loop


def _worker(tmp: Path, *, budget: dict | None = None, round_timeout_seconds: int = 5400) -> tuple[L.WorkerLayout, ControlStore]:
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
    store.assign("high", obligation_id="v0001-root-1", route_id="r1", task="prove T", round_timeout_seconds=round_timeout_seconds)
    return wl, store


def _report() -> dict:
    return {
        "route_status": "no_progress", "summary": "nothing new",
        "new_fact_ids": [], "new_evidence_refs": [],
        "new_or_changed_obligations": [], "unresolved_interfaces": ["bridge"],
        "failed_attempt_signatures": ["same"], "novelty_basis": [],
        "recommended_next_action": "audit",
    }


def test_v2_loop_runs_two_exploration_rounds_then_independent_audit(tmp_path: Path):
    wl, store = _worker(tmp_path)
    loop.write_status(
        wl, state="waiting_retry", failure_class="auth_or_config",
        infra_failure_count=1, retry_after_seconds=30,
        last_fact_id="0123456789abcdef",
    )
    prompts = []
    round_statuses = []
    original = loop.run_round
    original_refresh = loop.refresh_worker_assets
    refreshes = []

    def fake(_wl, _role, prompt, log_path, _timeout, *, report_path=None, output_schema=None, **_kwargs):
        prompts.append(prompt)
        round_statuses.append(json.loads(_wl.status.read_text(encoding="utf-8")))
        assert report_path is not None and output_schema == store.work_report_schema
        report_path.write_text(json.dumps(_report()), encoding="utf-8")
        log_path.write_text('{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":2}}\n', encoding="utf-8")
        return 0

    loop.run_round = fake
    loop.refresh_worker_assets = lambda worker_layout: refreshes.append(worker_layout.name)
    try:
        assert loop._run_loop(
            wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, store, beat=0,
        ) == 0
    finally:
        loop.run_round = original
        loop.refresh_worker_assets = original_refresh
    assert len(prompts) == 3
    assert refreshes == ["high", "high", "high"]
    assert all(status["failure_class"] is None for status in round_statuses)
    assert all(status["infra_failure_count"] == 0 for status in round_statuses)
    assert all(status["retry_after_seconds"] is None for status in round_statuses)
    assert "independent route audit" not in prompts[0]
    assert "independent route audit" in prompts[2]
    assert store.assignment("high")["status"] == "stalled"
    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert status["state"] == "paused" and status["control_reason"] == "stalled"
    assert status["failure_class"] is None
    assert status["infra_failure_count"] == 0
    assert status["retry_after_seconds"] is None
    assert status["last_fact_id"] == "0123456789abcdef"
    costs = store.events("cost")
    assert len(costs) == 3 and costs[0]["usage"]["input_tokens"] == 3


def test_v2_loop_rebuilds_context_after_concurrent_generation_change(tmp_path: Path):
    wl, store = _worker(tmp_path)
    original_manifest = loop.ResearchQuery.build_context_manifest
    original_round = loop.run_round
    manifest_calls = 0
    prompts = []

    def flaky_manifest(self, worker, **kwargs):
        nonlocal manifest_calls
        manifest_calls += 1
        if manifest_calls == 1:
            raise ValueError(
                "snapshot generation 10 is not current generation 11; "
                "use the persisted ContextManifest to reproduce an earlier model view"
            )
        return original_manifest(self, worker, **kwargs)

    def stop_after_round(_wl, _role, prompt, log_path, _timeout, *, report_path=None, **_kwargs):
        prompts.append(prompt)
        assert report_path is not None
        report_path.write_text(json.dumps(_report()), encoding="utf-8")
        log_path.write_text("{}\n", encoding="utf-8")
        _wl.stop.touch()
        return 0

    loop.ResearchQuery.build_context_manifest = flaky_manifest
    loop.run_round = stop_after_round
    try:
        assert loop._run_loop(
            wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, store, beat=0,
        ) == 0
    finally:
        loop.ResearchQuery.build_context_manifest = original_manifest
        loop.run_round = original_round

    assert manifest_calls == 2
    assert len(prompts) == 1


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


def test_budget_rejection_cannot_be_reframed_as_information_gain(tmp_path: Path):
    from danus.core import GlobalMemory

    _wl, store = _worker(tmp_path)
    assignment = store.assignment("high")
    evidence = GlobalMemory(store.project).append(
        "obstacle", claim="old fact", evidence="checked", author="high",
        verifiable=False,
    )
    store.append_event(
        "call_reservation_rejected", component="verification",
        worker="high", assignment_epoch=assignment["epoch"],
        reason_code="wall_budget", reason="cannot reserve",
    )
    report = _report() | {
        "route_status": "blocked", "new_evidence_refs": [evidence],
        "novelty_basis": ["old evidence restated"],
    }
    result = store.evaluate_work_report("high", report, wall_seconds=1)
    assert result["gain"] == "none"
    assert result["decision"] == "budget_exhausted"
    assert result["project_budget_blocked"] is True
    assert store.route_state("r1") == "active"


def test_timeout_without_report_uses_persisted_infra_budget_not_research_rounds(tmp_path: Path):
    wl, store = _worker(tmp_path, budget={"max_infra_attempts": 2, "infra_retry_seconds": [0]})
    original = loop.run_round

    def timeout(_wl, _role, _prompt, log_path, _timeout, **_kwargs):
        log_path.write_text("[worker_loop] round hard-timeout\n", encoding="utf-8")
        return 124

    loop.run_round = timeout
    try:
        assert loop._run_loop(wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, store, beat=0) == 1
    finally:
        loop.run_round = original
    assignment = store.assignment("high")
    assert assignment["status"] == "infra_blocked"
    assert assignment["infra_failure_count"] == 2
    assert assignment["rounds_used"] == 0
    assert assignment["consecutive_low"] == 0
    assert [event["component"] for event in store.events("cost")] == ["worker_infra", "worker_infra"]
    assert store.events("work_checkpoint") == []


def test_failed_old_round_exits_without_mutating_reassignment(tmp_path: Path):
    wl, store = _worker(tmp_path, budget={"max_infra_attempts": 2, "infra_retry_seconds": [0]})
    original = loop.run_round
    replacement = None

    def timeout_after_reassign(_wl, _role, _prompt, log_path, _timeout, **_kwargs):
        nonlocal replacement
        current = store.assignment("high")
        replacement = store.assign(
            "high", obligation_id=current["obligation_id"], route_id=current["route_id"],
            task="replacement task",
        )
        log_path.write_text("[worker_loop] round hard-timeout\n", encoding="utf-8")
        return 124

    loop.run_round = timeout_after_reassign
    try:
        assert loop._run_loop(
            wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, store, beat=0,
        ) == 0
    finally:
        loop.run_round = original

    assert replacement is not None
    current = store.assignment("high")
    assert current["epoch"] == replacement["epoch"]
    assert current["status"] == "assigned"
    assert current["infra_failure_count"] == 0
    assert store.events("round_infra_error")[-1]["assignment_changed"] is True


def test_timeout_after_verification_drain_counts_as_a_research_round(tmp_path: Path):
    wl, store = _worker(tmp_path)
    original = loop.run_round

    def drained(_wl, _role, _prompt, log_path, _timeout, **_kwargs):
        log_path.write_text(
            "[worker_loop] verification committed; ending timed-out round\n",
            encoding="utf-8",
        )
        (wl.project_dir / L.DEADLINE_FILE).write_text("1", encoding="utf-8")
        return 124

    loop.run_round = drained
    try:
        assert loop._run_loop(
            wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, store, beat=0,
        ) == 0
    finally:
        loop.run_round = original
    assignment = store.assignment("high")
    assert assignment["status"] != "infra_blocked"
    assert assignment["rounds_used"] == 1
    assert store.events("work_checkpoint")[-1]["report"]["route_status"] == "no_progress"
    assert store.events("cost")[-1]["component"] == "worker_round"


def test_quota_exhaustion_blocks_without_retry_or_round_charge(tmp_path: Path):
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
        assert loop._run_loop(wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, store, beat=0) == 1
    finally:
        loop.run_round = original
    assignment = store.assignment("high")
    assert calls == 1
    assert assignment["status"] == "infra_blocked"
    assert assignment["last_failure_class"] == "quota_exhausted"
    assert assignment["rounds_used"] == 0


def test_operator_stop_records_partial_usage_without_consuming_a_round(tmp_path: Path):
    wl, store = _worker(tmp_path, budget={"max_wall_seconds": 20}, round_timeout_seconds=10)
    gm = GlobalMemory(wl.project_dir)
    source_id = gm.append(
        "proof_attempt", claim="Interrupted candidate", evidence="partial proof",
        author="high",
    )
    gm.set_status(source_id, "verifying")
    original = loop.run_round

    def interrupted(_wl, _role, _prompt, log_path, _timeout, **_kwargs):
        log_path.write_text(
            '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}\n',
            encoding="utf-8",
        )
        wl.stop.touch()
        return 130

    loop.run_round = interrupted
    try:
        assert loop._run_loop(
            wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, store, beat=0,
        ) == 0
    finally:
        loop.run_round = original
    assignment = store.assignment("high")
    assert assignment["status"] == "assigned"
    assert assignment["rounds_used"] == 0
    assert store.events("work_checkpoint") == []
    assert store.events("round_interrupted")[-1]["usage_status"] == "partial"
    cost = store.events("cost")[-1]
    assert cost["attempt_status"] == "interrupted"
    assert cost["usage"]["input_tokens"] == 12
    assert store.active_call_reservations() == []
    assert [e for e in gm.read("proof_attempt") if e["id"] == source_id][0]["status"] == "unverified"


def test_a_call_reservation_at_the_budget_ceiling_does_not_reject_its_own_report(tmp_path: Path):
    _wl, store = _worker(tmp_path, budget={"max_wall_seconds": 10}, round_timeout_seconds=10)
    assignment = store.assignment("high")
    reservation = store.reserve_call(component="worker_round", max_wall_seconds=10, worker="high", assignment_epoch=assignment["epoch"])
    result = store.evaluate_work_report("high", _report(), wall_seconds=1, reservation_id=reservation["id"])
    assert result["decision"] == "continue"
    assert store.assignment("high")["rounds_used"] == 1
    assert len(store.events("work_checkpoint")) == 1
    assert store.budget_state()["reserved_wall_seconds"] == 0
