"""Shared fixtures for the danus.human_summary offline tests.

The read-only source fixture is the shipped example project
``.agents/skills/human-summary/examples/odd-sum/`` (it has ``fact_graph/facts/``
+ ``PROBLEM.md``). Anything that WRITES copies it to a tempdir first.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from pathlib import Path

from danus.control import ControlStore
from danus.core import FactGraph

_REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = _REPO_ROOT / ".agents" / "skills" / "human-summary"
MAIN_SKILL_DIR = SKILL_DIR
EXAMPLE_PROJECT = MAIN_SKILL_DIR / "examples" / "odd-sum"


@contextlib.contextmanager
def temp_project():
    """Copy the example project to a tempdir; yield its path."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="human-summary-test-") as d:
        dst = Path(d) / "project"
        shutil.copytree(EXAMPLE_PROJECT, dst)
        prepare_v2_project(dst)
        yield dst


def prepare_v2_project(dst: Path) -> None:
    """Index the copied example under the sole supported control model."""
    dst = Path(dst)
    (dst / "project.json").write_text(
        json.dumps({"name": "odd-sum", "control_version": 2}),
        encoding="utf-8",
    )
    graph = FactGraph(dst)
    fact_ids = graph.list()
    closing = "fact_odd_sum_main"
    store = ControlStore(dst)
    store.scaffold()
    target = store.propose_target({
        "statement": "Prove the odd-sum identity.",
        "allowed_assumptions": [],
        "forbidden_assumptions": [],
        "required_conclusions": ["The odd-sum identity holds."],
        "fallback_candidates": [],
    })
    store.approve_target(target["version"])
    obligation = "v0001-root-1"
    store.add_route({
        "id": "summary-route", "obligation_id": obligation,
        "method_family": "induction", "expected_result": "odd-sum identity",
        "input_fact_ids": [],
    })
    for fid in fact_ids:
        store.prepare_fact(fid, {"reused": True, "scope": {
            "worker": "fixture", "assignment_epoch": "fixture-epoch",
            "target_version": "v0001", "obligation_id": obligation,
            "route_id": "summary-route", "claim_role": "unconditional",
            "assumptions_used": [],
        }})
        store.finalize_fact(fid)
    store.set_obligation_state(
        obligation, "closed", actor="fixture", fact_id=closing,
        assignment_epoch="fixture-epoch",
    )


@contextlib.contextmanager
def env(**kv):
    """Temporarily set env vars (None deletes), restore after."""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
