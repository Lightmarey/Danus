"""Deterministic offline coverage for the native service manager."""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

import pytest

from danus import runtime, services
from danus.orchestration import cli


@contextmanager
def env(**values):
    old = {key: os.environ.get(key) for key in values}
    os.environ.update({key: str(value) for key, value in values.items()})
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def root(tmp: Path) -> Path:
    (tmp / "config").mkdir()
    (tmp / "runtime").mkdir()
    return tmp


def test_env_loader_order_quotes_comments_and_process_precedence():
    with tempfile.TemporaryDirectory() as td:
        r = root(Path(td))
        (r / "config" / "codex.env").write_text(
            "A=codex\nROOT=/base\nexport QUOTED=\"hello world\"\n"
            "COMMENT=value # note\nONE=$ROOT/one\nTWO=${ROOT}/two\n", encoding="utf-8"
        )
        (r / "config" / "danus.env").write_text("A=danus\nSINGLE='x y'\n", encoding="utf-8")
        (r / "runtime" / "runtime.env").write_text("A=runtime\n", encoding="utf-8")
        assert runtime.load_env_files(r, {"A": "process"}) == {
            "ROOT": "/base", "QUOTED": "hello world", "COMMENT": "value",
            "ONE": "/base/one", "TWO": "/base/two", "SINGLE": "x y",
        }
        loaded = runtime.load_env_files(r, {})
        assert loaded["A"] == "runtime"


def test_health_identity_stale_and_foreign(monkeypatch, tmp_path):
    r = root(tmp_path)
    e = services.service_env(r)
    run, _ = services._dirs(e)
    (run / "verify.pid").write_text("123")
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "token" if pid == 123 else None)
    monkeypatch.setattr(
        services, "_http_health",
        lambda port: {"status": "ok", "pid": 123, "identity": "token"},
    )
    assert services.verify_state(e, run)["state"] == "up"
    monkeypatch.setattr(
        services, "_http_health",
        lambda port: {"status": "ok", "pid": 999, "identity": "other"},
    )
    assert services.verify_state(e, run)["state"] == "foreign"
    monkeypatch.setattr(services, "_http_health", lambda port: None)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)
    assert services.verify_state(e, run)["state"] == "stale"


def test_dashboard_resolution_autostart_logs_and_down(monkeypatch, tmp_path):
    r = root(tmp_path)
    projects = r / "runtime" / "projects"
    (projects / "alpha").mkdir(parents=True)
    seen = {}

    class Proc:
        pid = 321

    monkeypatch.setattr(runtime, "spawn_detached", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or Proc())
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 321)
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "created" if pid == 321 else None)
    monkeypatch.setattr(runtime, "process_tree_pids", lambda pid: [321])
    monkeypatch.setattr(services, "_port_open", lambda port: False)
    monkeypatch.setattr(
        services, "_http_health",
        lambda port: ({
            "status": "ok", "pid": 321, "identity": "created",
            "project": str((projects / "alpha").resolve()),
        } if seen else None),
    )
    row = services.up("dashboard", "alpha", root=r)
    assert row["state"] == "up" and row["pid"] == 321
    assert str(projects / "alpha") in seen["cmd"]
    run = r / "runtime" / "run"
    assert (run / "dashboard-alpha.pid").read_text() == "321"
    assert (run / "dashboard-alpha.identity").read_text() == "created"
    assert (run / "autostart").read_text() == "dashboard alpha\n"
    log = r / "runtime" / "logs" / "dashboard-alpha.log"
    log.write_text("\n".join(str(i) for i in range(60)), encoding="utf-8")
    assert services.logs("dashboard-alpha", root=r).splitlines() == [str(i) for i in range(10, 60)]
    monkeypatch.setattr(
        runtime, "terminate_process_tree",
        lambda pid, force: seen.update(stopped=pid, dead=True),
    )
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 321 and not seen.get("dead"))
    assert services.down("dashboard", root=r)[0]["state"] == "stopped"
    assert seen["stopped"] == 321
    assert not (run / "autostart").exists()


def test_verify_start_refuses_foreign_and_port_collision(monkeypatch, tmp_path):
    r = root(tmp_path)
    monkeypatch.setattr(
        services, "_http_health",
        lambda port: {"status": "ok", "pid": 777, "identity": "foreign"},
    )
    with pytest.raises(SystemExit, match="foreign"):
        services.up("verify", root=r)
    monkeypatch.setattr(services, "_http_health", lambda port: None)
    monkeypatch.setattr(services, "_port_open", lambda port: True)
    with pytest.raises(SystemExit, match="already in use"):
        services.up("verify", root=r)


def test_cli_services_status_json(monkeypatch):
    monkeypatch.setattr(services, "status", lambda: [{"service": "verify", "state": "down", "pid": None}])
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["services", "status", "--json"]) == 0
    assert json.loads(out.getvalue())[0]["service"] == "verify"


@pytest.mark.parametrize("bad", ["../x", "..", "x/y", "x\\y", "x\nY", "x\x00y"])
def test_rejects_path_and_control_character_targets(bad, tmp_path):
    r = root(tmp_path)
    with pytest.raises((ValueError, SystemExit)):
        services.logs(bad, root=r)
    with pytest.raises((ValueError, SystemExit)):
        services.up("dashboard", bad, root=r)
    with pytest.raises(SystemExit):
        services.down(bad, root=r)


def test_unsafe_reused_and_foreign_pids_are_not_killed_or_deleted(monkeypatch, tmp_path):
    r = root(tmp_path)
    e = services.service_env(r)
    run, _ = services._dirs(e)
    pid_file = run / "verify.pid"
    identity_file = run / "verify.identity"
    pid_file.write_text("123")
    identity_file.write_text("old-token")
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "new-token")
    killed = []
    monkeypatch.setattr(runtime, "terminate_process_tree", lambda *args, **kwargs: killed.append(args))
    monkeypatch.setattr(services, "_http_health", lambda port: None)
    assert services.down("verify", root=r)[0]["state"] == "refused-unsafe"
    assert not killed and pid_file.exists() and identity_file.exists()

    identity_file.write_text("new-token")
    monkeypatch.setattr(
        services, "_http_health",
        lambda port: {"status": "ok", "pid": 999, "identity": "foreign"},
    )
    assert services.down("verify", root=r)[0]["state"] == "refused-unsafe"
    assert not killed and pid_file.exists()


def test_dashboard_project_mismatch_is_foreign_and_unsafe_to_stop(monkeypatch, tmp_path):
    r = root(tmp_path)
    projects = r / "runtime" / "projects"
    (projects / "alpha").mkdir(parents=True)
    e = services.service_env(r)
    run, _ = services._dirs(e)
    (run / "dashboard-alpha.pid").write_text("123")
    (run / "dashboard-alpha.identity").write_text("token")
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "token")
    monkeypatch.setattr(services, "_http_health", lambda port: {
        "status": "ok", "pid": 123, "identity": "token",
        "project": str((projects / "other").resolve()),
    })
    assert services.dashboard_state("dashboard-alpha", e, run)["state"] == "foreign"
    assert services.down("dashboard", root=r)[0]["state"] == "refused-unsafe"
    assert (run / "dashboard-alpha.pid").exists()


def test_dashboard_start_rejects_racing_foreign_health(monkeypatch, tmp_path):
    r = root(tmp_path)
    project = r / "runtime" / "projects" / "alpha"
    project.mkdir(parents=True)
    spawned = {"value": False, "killed": False}

    class Proc:
        pid = 321

    monkeypatch.setattr(
        runtime, "spawn_detached",
        lambda *args, **kwargs: spawned.update(value=True) or Proc(),
    )
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: bool(pid) and not spawned["killed"])
    monkeypatch.setattr(runtime, "process_tree_pids", lambda pid: [321])
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "ours")
    monkeypatch.setattr(services, "_port_open", lambda port: False)
    monkeypatch.setattr(runtime, "terminate_process_tree", lambda *args, **kwargs: spawned.update(killed=True))
    monkeypatch.setattr(services, "_http_health", lambda port: ({
        "status": "ok", "pid": 999, "identity": "foreign",
        "project": str(project.resolve()),
    } if spawned["value"] else None))
    monkeypatch.setattr(services.time, "sleep", lambda seconds: spawned.update(killed=True))
    with pytest.raises(SystemExit, match="failed to start"):
        services.up("dashboard", "alpha", root=r)
    assert not (r / "runtime" / "run" / "dashboard-alpha.identity").exists()
    assert not (r / "runtime" / "run" / "autostart").exists()


def test_legacy_stop_requires_matching_identity_health(monkeypatch, tmp_path):
    r = root(tmp_path)
    e = services.service_env(r)
    run, _ = services._dirs(e)
    (run / "verify.pid").write_text("123")
    alive = {"value": True}
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: alive["value"])
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "token")
    monkeypatch.setattr(
        services, "_http_health",
        lambda port: {"status": "ok", "pid": 123, "identity": "token"},
    )
    monkeypatch.setattr(
        runtime, "terminate_process_tree",
        lambda pid, force: alive.update(value=False),
    )
    assert services.down("verify", root=r)[0]["state"] == "stopped"


def test_services_test_requires_verify_up(monkeypatch):
    monkeypatch.setattr(
        services, "test",
        lambda: [{"service": "verify", "state": "down", "pid": None}],
    )
    assert cli.main(["services", "test"]) == 1


def test_loaded_python_override_is_used(monkeypatch, tmp_path):
    r = root(tmp_path)
    (r / "config" / "danus.env").write_text("DANUS_PY=C:/Python/custom.exe\n")
    seen = {}
    monkeypatch.setattr(runtime, "module_cmd", lambda module, *args, python=None: seen.update(
        python=python
    ) or ["fake"])
    monkeypatch.setattr(services, "_http_health", lambda port: {
        "status": "ok", "pid": 777, "identity": "foreign"
    })
    with pytest.raises(SystemExit):
        services.up("verify", root=r)
    assert seen["python"] == "C:/Python/custom.exe"


def test_safe_paths_and_manifest_reject_escape_or_injection(tmp_path):
    r = root(tmp_path)
    e = services.service_env(r)
    run, logs = services._dirs(e)
    with pytest.raises(ValueError, match="escapes"):
        services._safe_path(logs, "../outside.log")
    (run / "autostart").write_text("verify\ndashboard ok\nbad\ninjected")
    with pytest.raises(ValueError, match="invalid autostart"):
        services._manifest(run)


def test_real_verify_lifecycle_offline(tmp_path):
    """Exercise detached launch, PID health identity, and process-tree stop."""
    r = root(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with env(VERIFY_PORT=port, DANUS_RUNTIME=r / "runtime"):
        try:
            started = services.up("verify", root=r)
            assert started["state"] == "up"
            assert started["pid"] == started["health_pid"]
            assert services.status(root=r)[0]["state"] == "up"
            assert services.test(root=r)[0]["state"] == "up"
        finally:
            services.down("verify", root=r)
        assert not runtime.pid_alive(started["pid"])
        assert not (r / "runtime" / "run" / "verify.pid").exists()
        assert not (r / "runtime" / "run" / "verify.identity").exists()


def test_real_dashboard_lifecycle_offline(tmp_path):
    r = root(tmp_path)
    project = r / "runtime" / "projects" / "alpha"
    project.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with env(DASHBOARD_PORT=port, DANUS_RUNTIME=r / "runtime"):
        try:
            started = services.up("dashboard", "alpha", root=r)
            assert started["state"] == "up"
            assert started["project"] == str(project.resolve())
            assert services.status(root=r)[1]["state"] == "up"
        finally:
            services.down("dashboard", root=r)
        assert not runtime.pid_alive(started["pid"])
