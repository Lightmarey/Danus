from __future__ import annotations

import json
from pathlib import Path

from danus.control import ControlStore
from danus.execution import layout as L
from danus.execution import loop


def _worker(tmp: Path) -> tuple[L.WorkerLayout, ControlStore]:
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
