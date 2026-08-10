from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from danus.control import ControlStore
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
        result = cli.do_new(
            "P", roles="high:1", problem=problem, control_version=2,
        )
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


def test_public_new_command_requires_problem_or_explicit_legacy(tmp_path: Path):
    with _env(tmp_path):
        try:
            cli.main(["new", "P", "--roles", "high:1"])
            assert False, "new without a problem should fail"
        except SystemExit as exc:
            assert "require --problem" in str(exc)
        assert cli.main(["new", "legacy", "--roles", "high:1", "--legacy"]) == 0
        meta = json.loads((tmp_path / "projects" / "legacy" / "project.json").read_text())
        assert "control_version" not in meta


def test_target_change_stales_assignment_and_fallback_remains_draft(tmp_path: Path):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove T.\n", encoding="utf-8")
    with _env(tmp_path):
        result = cli.do_new("P", roles="high:1", problem=problem, control_version=2)
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
        fallback = cli.do_target("P", "fallback")["target"]
        assert store.target_state(fallback["version"]) == "draft"
        assert store.assignment("high")["status"] == "assigned"
        approved = cli.do_target("P", "approve", version=fallback["version"])
        assert approved["stale_workers"] == ["high"]
        assert store.assignment("high")["status"] == "stale"
