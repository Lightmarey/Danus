"""Offline tests for danus.execution.loop + __main__ (no real codex, no network).

Covers the slice driver end-to-end without ever launching a real codex:

  - ``run_round`` against a FIXED fake-codex stub script: a chosen exit code, a
    hard-timeout (terminate → 124), and a missing binary (→ 127). These drive the
    real ``subprocess.Popen`` path in loop.py.
  - the ``main`` outer loop: V2 gate, stop flag, deadline, SIGTERM handling, and
    atomic status writes.
  - the SIGTERM handler (_on_term): terminates the in-flight child, writes
    ``terminated`` status, and exits 0.
  - __main__: ``runpy.run_module("danus.execution", run_name="__main__")`` with the
    loop entry patched, covering the argv guard + dispatch without spawning.
  - the remaining small error/edge branches in loop / layout / scaffold helpers.

Runs standalone (``python -m danus.execution.tests.test_loop``) and pytest.
"""

from __future__ import annotations

import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from danus import runtime
from danus.control import ControlStore
from danus.execution import layout as L
from danus.execution import loop, scaffold
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
def _restore_sigterm():
    """main() installs a SIGTERM handler; save/restore so tests don't leak it."""
    old = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def _mk_worker(tmp: Path, name: str = "high") -> L.WorkerLayout:
    """A minimal worker home under tmp: <tmp>/proj/workers/<name>."""
    wl = L.WorkerLayout(tmp / "proj" / "workers" / name)
    wl.dir.mkdir(parents=True)
    return wl


def _enable_v2(wl: L.WorkerLayout) -> ControlStore:
    (wl.project_dir / "project.json").write_text(
        json.dumps({"name": wl.project, "control_version": 2}), encoding="utf-8",
    )
    store = ControlStore(wl.project_dir)
    store.scaffold()
    return store


def _write_fake_codex(tmp: Path, body: str) -> Path:
    script = "from __future__ import annotations\n" + body
    return write_python_launcher(tmp, "fake_codex", script)


def _slice_files(wl: L.WorkerLayout) -> dict:
    schema = wl.dir / "report.schema.json"
    schema.write_text("{}", encoding="utf-8")
    return {"report_path": wl.dir / "report.json", "output_schema": schema}


# --- run_round: chosen exit code ------------------------------------------- #

def test_run_round_returns_codex_rc(tmp: Path):
    wl = _mk_worker(tmp)
    fake = _write_fake_codex(tmp, "import sys\nsys.stdout.write('hello from codex\\n')\nsys.exit(3)\n")
    log = wl.dir / "round.log"
    with _env(DANUS_CODEX_BIN=str(fake)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=30, **_slice_files(wl))
    assert rc == 3
    assert "hello from codex" in log.read_text()
    assert loop._Child.proc is None            # cleared in finally


def test_run_round_success_rc0(tmp: Path):
    wl = _mk_worker(tmp)
    fake = _write_fake_codex(tmp, "import sys\nsys.exit(0)\n")
    log = wl.dir / "round.log"
    with _env(DANUS_CODEX_BIN=str(fake)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=0,
                            **_slice_files(wl))   # 0 => no timeout (wait forever)
    assert rc == 0


def test_run_round_injects_the_worker_gateway_config(tmp: Path):
    wl = _mk_worker(tmp)
    argv = wl.dir / "argv.json"
    reservation = wl.dir / "reservation.txt"
    fake = _write_fake_codex(
        tmp,
        f"import json, os, sys\nfrom pathlib import Path\nPath({str(argv)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\nPath({str(reservation)!r}).write_text(os.environ.get('DANUS_CALL_RESERVATION_ID', ''), encoding='utf-8')\nsys.exit(0)\n",
    )
    schema = wl.dir / "report.schema.json"
    schema.write_text("{}", encoding="utf-8")
    with _env(DANUS_CODEX_BIN=str(fake)):
        rc = loop.run_round(
            wl,
            {"MODEL": "m", "REASONING_EFFORT": "high"},
            "prompt",
            wl.dir / "slice.jsonl",
            hard_timeout=30,
            report_path=wl.dir / "report.json",
            output_schema=schema,
            reservation_id="reservation-1",
        )
    assert rc == 0
    args = json.loads(argv.read_text(encoding="utf-8"))
    assert "--ignore-user-config" not in args
    inline = next(arg for arg in args if "mcp_servers.danus=" in arg)
    assert "mcp_servers.danus=" in inline
    assert "DANUS_PROJECT_DIR=" in inline and 'DANUS_ROLE="worker"' in inline
    assert reservation.read_text(encoding="utf-8") == "reservation-1"


def test_run_round_interrupts_an_active_child_on_stop_request(tmp: Path):
    wl = _mk_worker(tmp)
    fake = _write_fake_codex(tmp, "import time\ntime.sleep(30)\n")
    schema = wl.dir / "report.schema.json"
    schema.write_text("{}", encoding="utf-8")
    wl.stop.touch()
    with _env(DANUS_CODEX_BIN=str(fake)):
        started = time.monotonic()
        rc = loop.run_round(
            wl, {"MODEL": "m", "REASONING_EFFORT": "high"}, "prompt",
            wl.dir / "slice.jsonl", hard_timeout=30,
            report_path=wl.dir / "report.json", output_schema=schema,
        )
    assert rc == 130
    assert time.monotonic() - started < 5
    assert "interrupted by stop request" in (wl.dir / "slice.jsonl").read_text()


# --- run_round: hard timeout → terminate → 124 ----------------------------- #

def test_run_round_hard_timeout_terminates(tmp: Path):
    wl = _mk_worker(tmp)
    pid_file = wl.dir / "child.pid"
    fake = _write_fake_codex(
        tmp,
        f"import os, time\nfrom pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n",
    )
    log = wl.dir / "round.log"
    with _env(DANUS_CODEX_BIN=str(fake)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=1, **_slice_files(wl))
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    assert rc == 124
    assert "hard-timeout after 1s" in log.read_text()
    deadline = time.time() + 5
    while time.time() < deadline and runtime.pid_alive(child_pid):
        time.sleep(0.05)
    assert not runtime.pid_alive(child_pid)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("reopened after timeout\n")
    log.unlink()
    assert not log.exists()
    assert loop._Child.proc is None


# --- run_round: missing binary → 127 --------------------------------------- #

def test_run_round_missing_binary_returns_127(tmp: Path):
    wl = _mk_worker(tmp)
    missing = tmp / "does_not_exist_codex"
    log = wl.dir / "round.log"
    with _env(DANUS_CODEX_BIN=str(missing)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=30, **_slice_files(wl))
    assert rc == 127
    assert "codex binary not found" in log.read_text()


# --- run_round: unresponsive child → terminate times out → kill → 124 ------ #

def test_run_round_timeout_then_kill(tmp: Path):
    """A child that ignores terminate() (wait(10) times out) is force-killed. We
    fake Popen so the 10s terminate-grace does not slow the test."""
    wl = _mk_worker(tmp)
    log = wl.dir / "round.log"

    class _StubProc:
        def __init__(self):
            self.returncode = None
            self._waits = 0
            self.stop_calls = []

        def wait(self, timeout=None):
            self._waits += 1
            if self.returncode is None:
                raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)
            return self.returncode

        def poll(self):
            return self.returncode

    stub = _StubProc()
    orig_spawn = runtime.spawn_process
    orig_stop = runtime.stop_process
    orig_wait_path = runtime.wait_until_path_releasable
    wait_calls = []
    runtime.spawn_process = lambda *a, **k: stub
    def _fake_stop_process(proc, *, wait_seconds=5.0, force=False):
        proc.stop_calls.append((wait_seconds, force))
        proc.returncode = -9
        return proc.returncode
    runtime.stop_process = _fake_stop_process
    runtime.wait_until_path_releasable = lambda path, *, timeout_seconds: (wait_calls.append((path, timeout_seconds)) or True)  # type: ignore[assignment]
    try:
        with _env(DANUS_CODEX_BIN=str(tmp / "anything")):
            rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                                "prompt", log, hard_timeout=1, **_slice_files(wl))
    finally:
        runtime.spawn_process = orig_spawn
        runtime.stop_process = orig_stop
        runtime.wait_until_path_releasable = orig_wait_path
    assert rc == 124
    assert stub.stop_calls == [(10, True)]
    assert wait_calls == [(log, 10)]
    assert loop._Child.proc is None


# --- main loop: stop flag → graceful stop ---------------------------------- #

def test_main_stops_on_stop_flag(tmp: Path):
    wl = _mk_worker(tmp)
    _enable_v2(wl)
    wl.codex_config.parent.mkdir()
    wl.codex_config.write_text("stale", encoding="utf-8")
    wl.stop.touch()          # stop before the first round
    configured = str(tmp / "custom-python")
    with _restore_sigterm(), _env(
        DANUS_ROUND_BEAT="0", DANUS_PYTHON_BIN=configured,
    ):
        rc = loop.main(str(wl.dir))
    assert rc == 0
    assert not wl.stop.exists()                       # consumed
    assert json.loads(wl.status.read_text())["state"] == "stopped"
    escaped = configured.replace("\\", "\\\\")
    assert f'command = "{escaped}"' in wl.codex_config.read_text(encoding="utf-8")


# --- main loop: deadline → stop -------------------------------------------- #

def test_main_stops_on_deadline(tmp: Path):
    wl = _mk_worker(tmp)
    _enable_v2(wl)
    (wl.project_dir / L.DEADLINE_FILE).write_text("1")   # epoch 1 = long past
    with _restore_sigterm(), _env(DANUS_ROUND_BEAT="0"):
        rc = loop.main(str(wl.dir))
    assert rc == 0
    assert json.loads(wl.status.read_text())["state"] == "deadline"


# --- main: bad worker dir → rc 2 ------------------------------------------- #

def test_main_missing_worker_dir(tmp: Path):
    rc = loop.main(str(tmp / "nope"))
    assert rc == 2


# --- SIGTERM handler: terminate child, write terminated, exit 0 ------------ #

def test_main_sigterm_handler(tmp: Path):
    wl = _mk_worker(tmp)
    _enable_v2(wl)

    class _FakeProc:
        def __init__(self):
            self.stopped = False

    fake_proc = _FakeProc()
    orig_stop = runtime.stop_process

    # Install a live child then deliver SIGTERM to ourselves so the loop's own
    # handler fires (covers _on_term end to end).
    def _controlled_loop(*_args, **_kwargs):
        loop._Child.proc = fake_proc
        signal.raise_signal(signal.SIGTERM)
        return 0

    original_loop = loop._run_loop
    with _restore_sigterm(), _env(DANUS_ROUND_BEAT="0"):
        runtime.stop_process = lambda proc, *, wait_seconds=5.0, force=False: setattr(proc, "stopped", True) or 0
        loop._run_loop = _controlled_loop
        try:
            try:
                loop.main(str(wl.dir))
                assert False, "handler should sys.exit(0)"
            except SystemExit as e:
                assert e.code == 0
        finally:
            loop._run_loop = original_loop
            loop._Child.proc = None
            runtime.stop_process = orig_stop
    assert fake_proc.stopped
    assert json.loads(wl.status.read_text())["state"] == "terminated"


# --- write_status: recovers from a corrupt existing status ----------------- #

def test_write_status_corrupt_existing_recovers(tmp: Path):
    wl = _mk_worker(tmp)
    wl.status.write_text("{not json")            # corrupt → JSONDecodeError branch
    loop.write_status(wl, state="running")
    st = json.loads(wl.status.read_text())
    assert st["state"] == "running" and st["worker"] == "high"


# --- _parse_last_fact_id: unreadable path → None --------------------------- #

def test_parse_last_fact_id_missing_file(tmp: Path):
    assert loop._parse_last_fact_id(tmp / "no_such.log") is None   # OSError branch


# --- _cleanup_pid: only removes a .pid that points at us ------------------- #

def test_cleanup_pid_removes_own(tmp: Path):
    wl = _mk_worker(tmp)
    wl.pid.write_text(str(os.getpid()))
    loop._cleanup_pid(wl)
    assert not wl.pid.exists()


def test_cleanup_pid_keeps_foreign(tmp: Path):
    wl = _mk_worker(tmp)
    wl.pid.write_text("999999999")            # some other pid
    loop._cleanup_pid(wl)
    assert wl.pid.exists()                     # left intact


def test_cleanup_pid_swallows_oserror(tmp: Path):
    """A .pid that cannot be read (here: it is a directory) → OSError swallowed."""
    wl = _mk_worker(tmp)
    wl.pid.mkdir()                             # read_text on a dir raises OSError
    loop._cleanup_pid(wl)                      # must not raise
    assert wl.pid.exists()


# --- kickoff prompt -------------------------------------------------------- #

def test_kickoff_is_scoped_without_full_assignment_dump():
    assignment = {
        "target_version": "v0001",
        "obligation_id": "O",
        "route_id": "R",
        "epoch": "E",
        "slice_count": 2,
        "task": "Prove the assigned lemma.",
        "credited_evidence_refs": ["should-not-be-embedded"],
    }
    prompt = loop.kickoff("P", "high", assignment, audit=False, context="CTX")
    assert all(value in prompt for value in ("v0001", "O", "R", "E", "CTX"))
    assert "Prove the assigned lemma." in prompt
    assert "should-not-be-embedded" not in prompt
    assert "Do not reopen" in prompt


# --- __main__ entry -------------------------------------------------------- #

def test_dunder_main_dispatches(tmp: Path):
    """runpy the package as __main__ with the loop entry patched: the guard runs
    and dispatches to main() without spawning anything."""
    seen = {}

    def _fake_main(arg):
        seen["arg"] = arg
        return 0

    orig = loop.main
    loop.main = _fake_main
    argv = sys.argv
    sys.argv = ["prog", "/some/worker/dir"]
    try:
        try:
            runpy.run_module("danus.execution", run_name="__main__")
            assert False, "should sys.exit"
        except SystemExit as e:
            assert e.code == 0
    finally:
        loop.main = orig
        sys.argv = argv
    assert seen["arg"] == "/some/worker/dir"


def test_dunder_main_usage_guard():
    """Wrong argc → usage message + exit 2 (no dispatch)."""
    argv = sys.argv
    sys.argv = ["prog"]                        # missing worker_dir
    try:
        try:
            runpy.run_module("danus.execution", run_name="__main__")
            assert False, "should sys.exit(2)"
        except SystemExit as e:
            assert e.code == 2
    finally:
        sys.argv = argv


# --- layout defaults (no env overrides) ------------------------------------ #

def test_layout_defaults_and_empties(tmp: Path):
    with _env(DANUS_WORKER_CONTRACT=None, DANUS_WORKER_SKILLS=None,
              DANUS_AGENTS_ROOT=None):
        # worker contract / skills default to the source checkout assets
        rr = Path(__file__).resolve().parents[3]
        assert L.worker_md() == rr / "agents" / "contracts" / "worker.md"
        assert L.worker_skills_dir() == rr / "agents" / "skills" / "worker"
        # agents_root default = <cwd>/runtime/projects
        assert L.agents_root() == (Path.cwd() / "runtime" / "projects").resolve()
    # list_workers / list_projects on a nonexistent root → []
    with _env(DANUS_AGENTS_ROOT=str(tmp / "no_such_root")):
        assert L.list_workers("ghost") == []
        assert L.list_projects() == []


# --- scaffold.symlink branches --------------------------------------------- #

def test_symlink_skips_existing(tmp: Path):
    target = tmp / "target"
    target.write_text("x")
    link = tmp / "link"
    link.write_text("already here")            # link path exists → early return
    scaffold.symlink(target, link)
    assert link.read_text() == "already here"  # untouched


def test_symlink_swallows_oserror(tmp: Path):
    target = tmp / "target"
    target.write_text("x")
    # a link path whose parent does not exist → fallback copy raises OSError, swallowed
    link = tmp / "no_parent_dir" / "link"
    real = runtime.shutil.copy2
    runtime.shutil.copy2 = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))  # type: ignore[assignment]
    try:
        scaffold.symlink(target, link)         # must not raise
    finally:
        runtime.shutil.copy2 = real  # type: ignore[assignment]
    assert not link.exists()


# --- runner ---------------------------------------------------------------- #

_NO_TMP = {test_kickoff_is_scoped_without_full_assignment_dump,
           test_dunder_main_usage_guard}


def main() -> None:
    tests = [
        test_run_round_returns_codex_rc,
        test_run_round_success_rc0,
        test_run_round_injects_the_worker_gateway_config,
        test_run_round_interrupts_an_active_child_on_stop_request,
        test_run_round_hard_timeout_terminates,
        test_run_round_missing_binary_returns_127,
        test_run_round_timeout_then_kill,
        test_main_stops_on_stop_flag,
        test_main_stops_on_deadline,
        test_main_missing_worker_dir,
        test_main_sigterm_handler,
        test_write_status_corrupt_existing_recovers,
        test_parse_last_fact_id_missing_file,
        test_cleanup_pid_removes_own,
        test_cleanup_pid_keeps_foreign,
        test_cleanup_pid_swallows_oserror,
        test_kickoff_is_scoped_without_full_assignment_dump,
        test_dunder_main_dispatches,
        test_dunder_main_usage_guard,
        test_layout_defaults_and_empties,
        test_symlink_skips_existing,
        test_symlink_swallows_oserror,
    ]
    for t in tests:
        if t in _NO_TMP:
            t()
        else:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL LOOP TESTS PASSED")


if __name__ == "__main__":
    main()
