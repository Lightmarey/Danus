"""Offline tests for the V2-only local research console."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from danus.control import ControlStore
from danus.core import FactGraph
from danus.observability import app as obs_app
from danus.observability.app import (
    build_channel,
    build_channels,
    build_control,
    build_overview,
)


@contextmanager
def _env(**values):
    old = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _seed_project(root: Path, *, with_facts: bool = True) -> tuple[ControlStore, list[str]]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.json").write_text(
        json.dumps({"name": "P", "control_version": 2}), encoding="utf-8",
    )
    (root / "PROBLEM.md").write_text("Prove T", encoding="utf-8")
    facts: list[str] = []
    if with_facts:
        graph = FactGraph(root)
        base = graph.add(
            problem_id="P", author="worker_high", statement="Base lemma.",
            proof="By identity.", display_title="Base lemma",
        )
        middle = graph.add(
            problem_id="P", author="worker_high", statement="Middle result.",
            proof="Use the base lemma.", predecessors=[base],
            display_title="Middle result",
        )
        top = graph.add(
            problem_id="P", author="worker_xhigh", statement="Top result.",
            proof="Use the middle result.", predecessors=[middle],
            intuition="the deep one", display_title="Top result",
        )
        standalone = graph.add(
            problem_id="P", author="worker_xhigh", statement="Standalone axiom.",
            proof="Trivial.", display_title="Standalone axiom",
        )
        facts = [base, middle, top, standalone]

    memory = root / "global_memory"
    memory.mkdir(exist_ok=True)
    (memory / "plan.jsonl").write_text(
        json.dumps({"claim": "reduce", "timestamp_utc": "2026-07-01T10:00:00"})
        + "\n{malformed\n"
        + json.dumps({"claim": "try route", "timestamp_utc": "2026-07-02T09:00:00"})
        + "\n",
        encoding="utf-8",
    )
    (memory / "verification.jsonl").write_text(
        json.dumps({"verdict": "correct"}) + "\n"
        + json.dumps({"verdict": "wrong"}) + "\n",
        encoding="utf-8",
    )
    spend = root / "spend"
    spend.mkdir(exist_ok=True)
    (spend / "consult.jsonl").write_text(
        json.dumps({"cost_usd": 1.25}) + "\n"
        + json.dumps({"cost_usd": 0.75}) + "\n",
        encoding="utf-8",
    )

    store = ControlStore(root)
    store.scaffold()
    target = store.propose_target({
        "statement": "T", "allowed_assumptions": [],
        "forbidden_assumptions": [], "required_conclusions": ["T"],
        "fallback_candidates": [],
    })
    store.approve_target(target["version"])
    store.add_route({
        "id": "r1", "obligation_id": "v0001-root-1",
        "method_family": "direct", "expected_result": "T",
        "input_fact_ids": facts[:1],
    })
    store.assign("high", obligation_id="v0001-root-1", route_id="r1", task="T")
    store.record_cost(component="worker_slice", wall_seconds=5, cost_usd=1.25)
    return store, facts


def test_overview_uses_sqlite_fact_index(tmp_path: Path):
    _seed_project(tmp_path)
    overview = build_overview(tmp_path)
    assert overview["facts"] == 4
    assert overview["facts_with_predecessors"] == 2
    assert overview["facts_by_author"] == {"worker_high": 2, "worker_xhigh": 2}
    assert overview["channel_counts"]["plan"] == 2
    assert overview["verdicts"] == {"correct": 1, "wrong": 1}
    assert overview["consult_count"] == 2
    assert overview["consult_cost_usd"] == 2.0


def test_channels_remain_tolerant_read_only_views(tmp_path: Path):
    _seed_project(tmp_path)
    channels = {row["kind"]: row for row in build_channels(tmp_path)["channels"]}
    assert len(channels) == 11
    assert channels["plan"]["count"] == 2
    plan = build_channel("plan", tmp_path)
    assert plan["entries"][0]["claim"] == "try route"
    try:
        build_channel("unknown", tmp_path)
        raise AssertionError("unknown channel must fail")
    except KeyError:
        pass


def test_control_view_uses_the_shared_research_query(tmp_path: Path):
    _seed_project(tmp_path)
    view = build_control(tmp_path)
    assert "enabled" not in view
    assert view["current_target"] == "v0001"
    assert view["obligations"][0]["state"] == "active"
    assert view["routes"][0]["state"] == "active"
    assert view["assignments"][0]["worker"] == "high"
    assert view["cost"]["cost_usd"] == 1.25


def test_empty_v2_project_is_a_valid_console_source(tmp_path: Path):
    project = tmp_path / "empty"
    _seed_project(project, with_facts=False)
    assert build_overview(project)["facts"] == 0
    assert build_channel("obstacle", project)["entries"] == []


def test_unmigrated_project_is_rejected(tmp_path: Path):
    (tmp_path / "project.json").write_text('{"name":"old"}', encoding="utf-8")
    try:
        build_overview(tmp_path)
        raise AssertionError("unmigrated project must fail")
    except ValueError as exc:
        assert "danus migrate" in str(exc)


def test_http_routes_expose_only_indexed_research_graph(tmp_path: Path):
    from starlette.testclient import TestClient

    _seed_project(tmp_path)
    with _env(DANUS_DASHBOARD_PROJECT=str(tmp_path), DANUS_PROJECT_DIR=None):
        client = TestClient(obs_app)
        assert client.get("/api/overview").json()["facts"] == 4
        assert client.get("/api/factgraph").status_code == 404
        research = client.get("/api/research/map")
        assert research.status_code == 200 and research.json()["active_target"]["version"] == "v0001"
        assert client.get("/api/channels").status_code == 200
        assert "enabled" not in client.get("/api/control").json()
        assert client.get("/api/channel/plan").json()["count"] == 2
        assert client.get("/api/channel/unknown").status_code == 404
        index = client.get("/")
        assert index.status_code == 200 and "Danus" in index.text
        assert index.headers["cache-control"] == "no-store"
