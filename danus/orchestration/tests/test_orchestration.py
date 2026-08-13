"""Offline tests for danus.orchestration — the ``danus`` CLI verbs.

Filesystem verbs (new/assign/status/list) are deterministic. The loop tests are
integration: they spawn the real ``python -m danus.execution`` loop subprocess but
stub codex with a fake shell binary (``DANUS_CODEX_BIN``) so nothing real is
invoked and no API is spent. All processes are force-cleaned in ``finally``.

Runs standalone (``python -m danus.orchestration.tests.test_orchestration``) and
under pytest.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
import contextlib
from pathlib import Path

from danus.control import ControlStore
from danus.execution import layout as L
from danus.orchestration import cli
from danus import runtime
from danus.tests.portable import write_python_launcher


@contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _project_env(tmp: Path, **extra):
    """Agents root + stub worker contract/skills so tests never touch the repo's
    agents/ tree; merge any extra env (codex stub, round vars)."""
    contract = tmp / "worker.md"
    contract.write_text("# worker contract (stub)\n", encoding="utf-8")
    skills = tmp / "skills"
    skills.mkdir(exist_ok=True)
    env = {"DANUS_AGENTS_ROOT": str(tmp / "agents"),
           "DANUS_WORKER_CONTRACT": str(contract),
           "DANUS_WORKER_SKILLS": str(skills)}
    env.update(extra)
    with _env(**env):
        yield


def _fake_codex(d: Path) -> Path:
    """A stub codex that emits one schema-valid, low-gain WorkReport."""
    report = {
        "route_status": "no_progress", "summary": "no new evidence",
        "new_fact_ids": [], "new_evidence_refs": [],
        "new_or_changed_obligations": [], "unresolved_interfaces": [],
        "failed_attempt_signatures": ["fixture-repeat"], "novelty_basis": [],
        "recommended_next_action": "audit route",
    }
    return write_python_launcher(
        d,
        "fake_codex",
        'import json, os, pathlib, sys, time\n'
        f'report = {report!r}\n'
        'out = sys.argv[sys.argv.index("--output-last-message") + 1]\n'
        'pathlib.Path(out).write_text(json.dumps(report), encoding="utf-8")\n'
        'sys.stdout.write("{\\"type\\":\\"turn.completed\\",\\"usage\\":{}}\\n")\n'
        'sys.stdout.flush()\n'
        'time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP", "0")))\n',
    )


def _prepare_route(project: str, workers: tuple[str, ...]) -> tuple[str, str]:
    store = ControlStore(L.project_dir(project))
    target = store.propose_target({
        "statement": "Prove T.", "allowed_assumptions": [],
        "forbidden_assumptions": [], "required_conclusions": ["T"],
        "fallback_candidates": [],
    })
    store.approve_target(target["version"])
    obligation, route = "v0001-root-1", "test-route"
    store.add_route({
        "id": route, "obligation_id": obligation,
        "method_family": "direct", "expected_result": "T",
        "input_fact_ids": [],
    })
    for worker in workers:
        store.assign(worker, obligation_id=obligation, route_id=route, task="Prove T")
    return obligation, route


def _wait_until(pred, timeout=15.0, interval=0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _st(project: str, worker: str) -> dict:
    return cli.worker_status(L.WorkerLayout(L.worker_dir(project, worker)))


def _status_pid(wl: L.WorkerLayout):
    try:
        data = json.loads(wl.status.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = data.get("pid")
    return pid if isinstance(pid, int) else None


def _kill_project(project: str):
    pids = []
    wait_opts = getattr(os, "WNOHANG", 0)
    logs = []
    for d in L.target_worker_dirs(project):
        wl = L.WorkerLayout(d)
        pid = cli._read_pid(wl) or _status_pid(wl)
        if pid:
            pids.append(pid)
        logs.append(wl.logs / "loop.log")
    try:
        cli.do_stop(project, force=True)
    except SystemExit:
        pass
    _wait_until(lambda: all(not runtime.pid_alive(pid) for pid in pids), timeout=8.0)
    for pid in pids:
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, wait_opts)
    for log_path in logs:
        assert runtime.wait_until_path_releasable(log_path, timeout_seconds=8.0), (
            f"loop log still locked after process exit: {log_path}"
        )


# --- filesystem verb tests ------------------------------------------------- #

def test_assign_replace_and_rejects(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        obligation, route = _prepare_route("P", ())
        cli.do_assign(
            "P/high", "explore direction 3: the symplectic-rank route",
            obligation=obligation, route=route,
        )
        assert L.WorkerLayout(L.worker_dir("P", "high")).task.read_text() == \
            "explore direction 3: the symplectic-rank route\n"
        cli.do_assign(
            "P/high", "switch to direction 5", obligation=obligation, route=route,
        )   # replace, not append
        assert L.WorkerLayout(L.worker_dir("P", "high")).task.read_text() == "switch to direction 5\n"
        for bad in ["P", "P/nope"]:
            try:
                cli.do_assign(bad, "x")
                assert False, f"should reject {bad!r}"
            except SystemExit:
                pass
        try:
            cli.do_assign("P/high", "   ")
            assert False, "should reject empty task"
        except SystemExit:
            pass


def test_status_before_start(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        s = _st("P", "high")
        assert s["alive"] is False and s["state"] == "created" and s["label"] == "created"


def test_list(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:2", model="gpt-5.5")
        cli.do_new("Q", roles="xhigh:1", model="gpt-x")
        rows = {r["project"]: r for r in cli.do_list()}
        assert rows["P"]["workers"] == 2 and rows["P"]["live"] == 0 and rows["P"]["model"] == "gpt-5.5"
        assert rows["Q"]["workers"] == 1 and rows["Q"]["model"] == "gpt-x"


# --- loop integration tests (stubbed codex) -------------------------------- #

def test_loop_stalls_only_after_audited_low_gain(tmp: Path):
    fc = _fake_codex(tmp)
    with _project_env(tmp, DANUS_CODEX_BIN=str(fc), DANUS_ROUND_BEAT="0",
                      FAKE_CODEX_SLEEP="0"):
        cli.do_new("P", roles="high:1")
        _prepare_route("P", ("high",))
        try:
            res = cli.do_start("P/high")
            assert res[0]["result"] == "started"
            assert _wait_until(lambda: not _st("P", "high")["alive"]), "loop should pause after audit"
            s = _st("P", "high")
            assert s["state"] == "paused" and s["round"] == 3
            wl = L.WorkerLayout(L.worker_dir("P", "high"))
            assert (wl.logs / "round_1.jsonl").exists()
            assert (wl.logs / "round_3.jsonl").exists()
        finally:
            _kill_project("P")


def test_graceful_stop(tmp: Path):
    fc = _fake_codex(tmp)
    with _project_env(tmp, DANUS_CODEX_BIN=str(fc), DANUS_ROUND_BEAT="0.1",
                      FAKE_CODEX_SLEEP="0.1"):
        cli.do_new("P", roles="high:1")
        _prepare_route("P", ("high",))
        try:
            wl = L.WorkerLayout(L.worker_dir("P", "high"))
            cli.do_start("P/high")
            assert _wait_until(lambda: _st("P", "high")["round"] >= 1), "should start a round"
            assert _st("P", "high")["alive"] is True
            assert _wait_until(
                lambda: wl.pid.exists()
                and wl.status.exists()
                and cli._read_pid(wl) == json.loads(wl.status.read_text(encoding="utf-8"))["pid"]
            ), "loop should claim .pid with its own running pid"
            loop_pid = cli._read_pid(wl)
            assert loop_pid is not None
            r = cli.do_stop("P/high")            # graceful
            assert "graceful" in r[0]["result"]
            assert _wait_until(
                lambda: cli._read_pid(wl) is None and not runtime.pid_alive(loop_pid)
            ), "loop should exit after .stop and fully close its handles"
            assert cli._read_pid(wl) is None  # pid cleaned
        finally:
            _kill_project("P")


def test_force_stop(tmp: Path):
    fc = _fake_codex(tmp)
    with _project_env(tmp, DANUS_CODEX_BIN=str(fc), DANUS_ROUND_BEAT="0",
                      FAKE_CODEX_SLEEP="30"):
        cli.do_new("P", roles="high:1")
        _prepare_route("P", ("high",))
        try:
            cli.do_start("P/high")
            assert _wait_until(lambda: _st("P", "high")["state"] == "running"), "round should run"
            r = cli.do_stop("P/high", force=True)
            assert r[0]["result"] == "killed"
            assert _wait_until(lambda: not _st("P", "high")["alive"], timeout=8), "force kills fast"
        finally:
            _kill_project("P")


def test_idempotent_start(tmp: Path):
    fc = _fake_codex(tmp)
    with _project_env(tmp, DANUS_CODEX_BIN=str(fc), DANUS_ROUND_BEAT="0",
                      FAKE_CODEX_SLEEP="30"):
        cli.do_new("P", roles="high:1")
        _prepare_route("P", ("high",))
        try:
            assert cli.do_start("P/high")[0]["result"] == "started"
            assert _wait_until(lambda: _st("P", "high")["alive"])
            assert cli.do_start("P/high")[0]["result"] == "already-running"
        finally:
            _kill_project("P")


def test_start_after_audited_backend_retry(tmp: Path):
    fc = _fake_codex(tmp)
    with _project_env(tmp, DANUS_CODEX_BIN=str(fc), DANUS_ROUND_BEAT="0",
                      FAKE_CODEX_SLEEP="0"):
        cli.do_new("P", roles="high:1")
        _prepare_route("P", ("high",))
        store = ControlStore(L.project_dir("P"))
        quota = {
            "failure_class": "quota_exhausted", "retryable": False,
            "retry_after_seconds": 0, "error_signature": "quota", "return_code": 1,
        }
        store.record_worker_infra_failure("high", quota, wall_seconds=1)
        store.retry_backend("codex", reason="provider quota renewed")
        assert store.assignment("high")["status"] == "waiting_retry"
        try:
            assert cli.do_start("P/high")[0]["result"] == "started"
            assert _wait_until(
                lambda: store.assignment("high")["status"] != "waiting_retry"
            )
            assert _wait_until(lambda: not _st("P", "high")["alive"])
        finally:
            _kill_project("P")


def test_project_wide_targets(tmp: Path):
    fc = _fake_codex(tmp)
    with _project_env(tmp, DANUS_CODEX_BIN=str(fc), DANUS_ROUND_BEAT="0",
                      FAKE_CODEX_SLEEP="0"):
        cli.do_new("P", roles="high:2")
        _prepare_route("P", ("high", "high2"))
        try:
            res = cli.do_start("P")              # whole project
            assert {r["worker"] for r in res} == {"high", "high2"}
            assert _wait_until(lambda: all(not _st("P", w)["alive"] for w in ("high", "high2")))
            assert len(cli.do_status("P")) == 2
        finally:
            _kill_project("P")


def test_missing_codex_returns_error_state(tmp: Path):
    with _project_env(tmp, DANUS_CODEX_BIN="/nonexistent/codex-bin",
                      DANUS_ROUND_BEAT="0"):
        cli.do_new("P", roles="high:1")
        _prepare_route("P", ("high",))
        try:
            cli.do_start("P/high")
            # A missing provider binary is infrastructure failure, not research
            # progress; bounded retry opens the shared circuit and pauses work.
            assert _wait_until(lambda: not _st("P", "high")["alive"]), "loop should exit on missing codex"
            s = _st("P", "high")
            assert s["state"] == "infra_blocked"
        finally:
            _kill_project("P")


# --- runner ---------------------------------------------------------------- #

def main() -> None:
    fs_tests = [test_assign_replace_and_rejects, test_status_before_start, test_list]
    loop_tests = [test_loop_stalls_only_after_audited_low_gain, test_graceful_stop, test_force_stop,
                  test_idempotent_start, test_start_after_audited_backend_retry,
                  test_project_wide_targets,
                  test_missing_codex_returns_error_state]
    for t in fs_tests + loop_tests:
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL ORCHESTRATION TESTS PASSED")


if __name__ == "__main__":
    main()
