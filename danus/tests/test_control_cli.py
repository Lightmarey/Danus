from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from danus.control import ControlStore
from danus.core import FactGraph
from danus.orchestration import cli


@contextmanager
def _env(tmp: Path):
    contract = tmp / "worker.md"
    contract.write_text("# worker\n", encoding="utf-8")
    skills = tmp / "skills"
    skills.mkdir()
    values = {
        "DANUS_AGENTS_ROOT": str(tmp / "projects"),
        "DANUS_WORKER_CONTRACT": str(contract),
        "DANUS_WORKER_SKILLS": str(skills),
    }
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write(path: Path, value: dict) -> str:
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def test_v2_cli_lifecycle_requires_approved_bound_work(tmp_path: Path):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove T.\n", encoding="utf-8")
    with _env(tmp_path):
        result = cli.do_new("P", roles="high:1", problem=problem)
        assert result["control_version"] == 2
        project = Path(result["project_dir"])
        assert (project / "PROBLEM.md").read_text() == "Prove T.\n"
        assert cli.do_start("P/high")[0]["result"] == "waiting"

        target_file = _write(tmp_path / "target.json", {
            "statement": "T", "allowed_assumptions": [], "forbidden_assumptions": [],
            "required_conclusions": ["T"], "fallback_candidates": [],
        })
        draft = cli.do_target("P", "propose", file=target_file)["target"]
        cli.do_target("P", "approve", version=draft["version"])
        oid = "v0001-root-1"
        route_file = _write(tmp_path / "route.json", {
            "id": "r1", "obligation_id": oid, "method_family": "direct",
            "expected_result": "prove T",
        })
        cli.do_route("P", "add", file=route_file)
        assigned = cli.do_assign("P/high", "prove T", obligation=oid, route="r1")
        assert assigned["assignment"]["target_version"] == "v0001"
        assert cli.do_status("P/high")[0]["control"]["route_id"] == "r1"


def test_public_new_command_is_v2_only(tmp_path: Path):
    with _env(tmp_path):
        assert cli.main(["new", "P", "--roles", "high:1"]) == 0
        meta = json.loads((tmp_path / "projects" / "P" / "project.json").read_text())
        assert meta["control_version"] == 2
        assert cli.do_start("P/high")[0]["result"] == "waiting"
        try:
            cli.build_parser().parse_args(["new", "legacy", "--legacy"])
            raise AssertionError("--legacy must not be accepted")
        except SystemExit:
            pass


def test_v1_project_requires_explicit_lossless_migration(tmp_path: Path):
    with _env(tmp_path):
        project = tmp_path / "projects" / "old"
        worker = project / "workers" / "high"
        worker.mkdir(parents=True)
        (worker / ".status.json").write_text("{}", encoding="utf-8")
        (project / "project.json").write_text(json.dumps({
            "name": "old", "model": "m", "roles": "high:1", "workers": ["high"],
        }), encoding="utf-8")
        fact_id = FactGraph(project).add(
            problem_id="old", author="high", statement="Legacy theorem",
            proof="Legacy proof", display_title="Legacy theorem",
        )

        try:
            cli.do_start("old/high")
            raise AssertionError("unmigrated projects must not start")
        except SystemExit as exc:
            assert "danus migrate old" in str(exc)

        result = cli.do_migrate("old")
        assert result["migrated"] is True and result["facts"] == 1
        meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
        assert meta["control_version"] == 2
        store = ControlStore(project)
        assert store.target_versions() == []
        assert store.events("project_migrated_from_v1")[0]["source_project_meta"]["name"] == "old"
        with store._connect() as db:
            assert db.execute("SELECT fact_id FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
        assert cli.do_start("old/high")[0]["result"] == "waiting"
        assert cli.do_migrate("old")["migrated"] is False


def test_target_change_stales_assignment_and_fallback_remains_draft(tmp_path: Path):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove T.\n", encoding="utf-8")
    with _env(tmp_path):
        result = cli.do_new("P", roles="high:1", problem=problem)
        store = ControlStore(Path(result["project_dir"]))
        first = store.propose_target({
            "statement": "T", "allowed_assumptions": [], "forbidden_assumptions": [],
            "required_conclusions": ["T"], "fallback_candidates": ["T weak"],
        })
        store.approve_target(first["version"])
        store.add_route({
            "id": "r1", "obligation_id": "v0001-root-1",
            "method_family": "direct", "expected_result": "T",
        })
        store.assign("high", obligation_id="v0001-root-1", route_id="r1", task="T")
        fallback_result = cli.do_target("P", "fallback")
        fallback = fallback_result["target"]
        assert store.target_state(fallback["version"]) == "draft"
        assert store.assignment("high")["status"] == "stale"
        approved = cli.do_target("P", "approve", version=fallback["version"])
        assert approved["stale_workers"] == []
        assert store.assignment("high")["status"] == "stale"


def test_start_recovers_crashed_round_before_spawning(tmp_path: Path, monkeypatch):
    with _env(tmp_path):
        result = cli.do_new("P", roles="high:1")
        project = Path(result["project_dir"])
        store = ControlStore(project)
        target = store.propose_target({
            "statement": "T", "allowed_assumptions": [], "forbidden_assumptions": [],
            "required_conclusions": ["T"], "fallback_candidates": [],
        })
        store.approve_target(target["version"])
        store.add_route({
            "id": "r1", "obligation_id": "v0001-root-1",
            "method_family": "direct", "expected_result": "T",
        })
        assignment = store.assign(
            "high", obligation_id="v0001-root-1", route_id="r1", task="T",
        )
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
        store.reserve_call(
            component="verification", max_wall_seconds=30,
            parent_reservation_id=parent["id"], **scope,
        )
        worker = project / "workers" / "high"
        (worker / ".pid").write_text("99999999", encoding="utf-8")
        (worker / ".status.json").write_text(json.dumps({
            "state": "running", "round_started_at": time.time() - 5,
        }), encoding="utf-8")
        monkeypatch.setattr(cli, "spawn_loop", lambda _worker_dir: 4321)

        assert cli.do_start("P/high")[0]["result"] == "started"
        assert store.assignment("high")["status"] == "assigned"
        assert store.assignment("high")["rounds_used"] == 0
        assert store.active_call_reservations() == []
        assert store.events("round_interrupted")[-1]["reason"] == "restart_after_dead_worker"


def test_dead_idle_worker_resets_control_without_fake_interruption(tmp_path: Path):
    with _env(tmp_path):
        result = cli.do_new("P", roles="high:1")
        project = Path(result["project_dir"])
        store = ControlStore(project)
        target = store.propose_target({
            "statement": "T", "allowed_assumptions": [], "forbidden_assumptions": [],
            "required_conclusions": ["T"], "fallback_candidates": [],
        })
        store.approve_target(target["version"])
        store.add_route({
            "id": "r1", "obligation_id": "v0001-root-1",
            "method_family": "direct", "expected_result": "T",
        })
        assignment = store.assign(
            "high", obligation_id="v0001-root-1", route_id="r1", task="T",
        )
        assignment["status"] = "running"
        store.save_assignment(assignment)
        worker = project / "workers" / "high"
        (worker / ".status.json").write_text(json.dumps({
            "state": "running", "round_started_at": 10, "last_round_at": 20,
        }), encoding="utf-8")

        recovered = cli._recover_dead_worker(
            cli.L.WorkerLayout(worker), reason="test_idle_stop",
        )

        assert recovered["reset_idle_assignment"] is True
        assert store.assignment("high")["status"] == "assigned"
        assert store.events("round_interrupted") == []
