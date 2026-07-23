"""Native persistent-service manager used by ``danus services``."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from danus import runtime

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _identifier(value: str, label: str = "identifier") -> str:
    if value in (".", "..") or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must match {_IDENTIFIER.pattern}")
    return value


def _arg_identifier(value: str) -> str:
    try:
        return _identifier(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _service_name(value: str) -> str:
    if value == "verify":
        return value
    if value.startswith("dashboard-"):
        _identifier(value.removeprefix("dashboard-"), "dashboard project")
        return value
    raise ValueError("service must be verify or dashboard-PROJECT")


def _arg_service_name(value: str) -> str:
    try:
        return _service_name(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def service_env(root: Optional[Path] = None) -> Dict[str, str]:
    root = (root or repo_root()).resolve()
    env = dict(os.environ)
    env.update(runtime.load_env_files(root, env))
    runtime_dir = Path(env.get("DANUS_RUNTIME", root / "runtime")).resolve()
    env.setdefault("DANUS_ROOT", str(root))
    env.setdefault("DANUS_RUNTIME", str(runtime_dir))
    env.setdefault("DANUS_AGENTS_ROOT", str(runtime_dir / "projects"))
    env.setdefault("VERIFIER_RESULTS_DIR", str(runtime_dir / "verify-runs"))
    env.setdefault("VERIFY_HOST", "127.0.0.1")
    env.setdefault("VERIFY_PORT", "8091")
    env.setdefault("DASHBOARD_PORT", "8099")
    env.setdefault("DANUS_VERIFY_URL", f"http://127.0.0.1:{env['VERIFY_PORT']}/verify")
    return env


def _dirs(env: Dict[str, str]) -> tuple[Path, Path]:
    base = Path(env["DANUS_RUNTIME"])
    run, logs = base / "run", base / "logs"
    run.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return run.resolve(), logs.resolve()


def _safe_path(base: Path, filename: str) -> Path:
    path = (base / filename).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes {base}: {filename}") from exc
    return path


def _pid_file(run: Path, name: str) -> Path:
    return _safe_path(run, f"{_service_name(name)}.pid")


def _identity_file(run: Path, name: str) -> Path:
    return _safe_path(run, f"{_service_name(name)}.identity")


def _log_file(logs: Path, name: str) -> Path:
    return _safe_path(logs, f"{_service_name(name)}.log")


def _read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _read_identity(path: Path) -> Optional[str]:
    try:
        value = path.read_text(encoding="ascii").strip()
        return value or None
    except OSError:
        return None


def _atomic_write(path: Path, value: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def _valid_manifest_entry(entry: str) -> str:
    if entry == "verify":
        return entry
    if entry.startswith("dashboard "):
        _identifier(entry.removeprefix("dashboard "), "dashboard project")
        return entry
    raise ValueError(f"invalid autostart entry: {entry!r}")


def _manifest(run: Path) -> List[str]:
    path = _safe_path(run, "autostart")
    try:
        return [_valid_manifest_entry(line.strip()) for line in path.read_text(
            encoding="utf-8"
        ).splitlines() if line.strip()]
    except OSError:
        return []


def _write_manifest(run: Path, lines: List[str]) -> None:
    path = _safe_path(run, "autostart")
    if not lines:
        path.unlink(missing_ok=True)
        return
    _atomic_write(path, "".join(f"{_valid_manifest_entry(line)}\n" for line in lines))


def _set_manifest(run: Path, entry: str, present: bool) -> None:
    entry = _valid_manifest_entry(entry)
    lines = _manifest(run)
    if present and entry not in lines:
        lines.append(entry)
    if not present:
        lines = [line for line in lines if line != entry]
    _write_manifest(run, lines)


def _http_health(port: int, timeout: float = 0.4) -> Optional[Dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            value = json.load(response)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _health_matches(health: Optional[Dict], pid: Optional[int], project: Optional[Path] = None) -> bool:
    identity = runtime.process_identity(pid)
    if not health or not identity:
        return False
    if health.get("status") != "ok" or health.get("pid") != pid or health.get("identity") != identity:
        return False
    return project is None or health.get("project") == str(project.resolve())


def _state(name: str, port: int, env: Dict[str, str], run: Path,
           project: Optional[Path] = None) -> Dict:
    pid_file = _pid_file(run, name)
    pid = _read_pid(pid_file)
    alive = runtime.pid_alive(pid)
    current = runtime.process_identity(pid)
    saved = _read_identity(_identity_file(run, name))
    health = _http_health(port)
    health_ok = _health_matches(health, pid, project)
    identity_ok = bool(current and (saved == current or (saved is None and health_ok)))
    if alive and identity_ok and health_ok:
        state = "up"
    elif health is not None:
        state = "foreign"
    elif pid_file.exists() and not alive:
        state = "stale"
    elif alive and saved != current:
        state = "unsafe"
    else:
        state = "starting" if alive else "down"
    return {
        "service": name, "state": state, "pid": pid,
        "health_pid": health.get("pid") if health else None,
        "project": str(project.resolve()) if project else None,
    }


def verify_state(env: Dict[str, str], run: Path) -> Dict:
    return _state("verify", int(env["VERIFY_PORT"]), env, run)


def dashboard_state(name: str, env: Dict[str, str], run: Path) -> Dict:
    project = Path(env["DANUS_AGENTS_ROOT"]) / name.removeprefix("dashboard-")
    return _state(name, int(env["DASHBOARD_PORT"]), env, run, project.resolve())


def _dashboard_name(project: str) -> str:
    return f"dashboard-{_identifier(project, 'dashboard project')}"


def _python(env: Dict[str, str]) -> Optional[str]:
    return env.get("DANUS_PYTHON_BIN") or env.get("DANUS_PY")


def up(service: str, project: Optional[str] = None, *, root: Optional[Path] = None) -> Dict:
    if service not in ("verify", "dashboard"):
        raise SystemExit("service must be verify or dashboard")
    env = service_env(root)
    run, logs = _dirs(env)
    _manifest(run)  # validate before starting a process or changing evidence
    if service == "verify":
        name, entry, project_dir = "verify", "verify", None
        port = int(env["VERIFY_PORT"])
        cmd = runtime.module_cmd("danus.verify", python=_python(env))
    else:
        if not project:
            raise SystemExit("usage: danus services up dashboard PROJECT")
        name, entry = _dashboard_name(project), f"dashboard {project}"
        project_dir = (Path(env["DANUS_AGENTS_ROOT"]) / project).resolve()
        agents_root = Path(env["DANUS_AGENTS_ROOT"]).resolve()
        try:
            project_dir.relative_to(agents_root)
        except ValueError as exc:
            raise SystemExit("dashboard project escapes the projects directory") from exc
        if not project_dir.is_dir():
            raise SystemExit(f"project not found: {project_dir}")
        port = int(env["DASHBOARD_PORT"])
        cmd = runtime.module_cmd(
            "danus.observability", "--project", str(project_dir),
            "--host", "127.0.0.1", "--port", str(port), python=_python(env),
        )
    state = _state(name, port, env, run, project_dir)
    if state["state"] == "up":
        _set_manifest(run, entry, True)
        return state
    if state["state"] == "foreign":
        raise SystemExit(f"port {port} is served by a foreign process")
    pid_file, identity_file = _pid_file(run, name), _identity_file(run, name)
    if state["state"] in ("unsafe", "starting"):
        raise SystemExit(f"{name} has a live unverified PID file; refusing to overwrite it")
    if _port_open(port):
        raise SystemExit(f"port {port} is already in use; refusing to start {name}")
    pid_file.unlink(missing_ok=True)
    identity_file.unlink(missing_ok=True)
    log = _log_file(logs, name)
    with open(log, "ab", buffering=0) as output:
        proc = runtime.spawn_detached(cmd, cwd=root or repo_root(), env=env, stdout=output)
    _atomic_write(pid_file, str(proc.pid))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        health = _http_health(port)
        health_pid = health.get("pid") if health else None
        if (
            isinstance(health_pid, int)
            and health_pid in runtime.process_tree_pids(proc.pid)
            and _health_matches(health, health_pid, project_dir)
        ):
            identity = runtime.process_identity(health_pid)
            if identity:
                _atomic_write(pid_file, str(health_pid))
                _atomic_write(identity_file, identity)
                state = _state(name, port, env, run, project_dir)
                if state["state"] == "up":
                    _set_manifest(run, entry, True)
                    return state
        if not runtime.pid_alive(proc.pid):
            break
        time.sleep(0.05)
    with contextlib.suppress(Exception):
        runtime.terminate_process_tree(proc.pid, force=True)
    raise SystemExit(f"{name} failed to start; evidence retained in {pid_file} and {log}")


def status(*, root: Optional[Path] = None) -> List[Dict]:
    env = service_env(root)
    run, _ = _dirs(env)
    _manifest(run)  # surface an injected manifest without interpreting it
    rows = [verify_state(env, run)]
    for path in sorted(run.glob("dashboard-*.pid")):
        name = _service_name(path.stem)
        rows.append(dashboard_state(name, env, run))
    return rows


def _safe_to_stop(name: str, pid: int, env: Dict[str, str], run: Path) -> bool:
    current = runtime.process_identity(pid)
    if not current:
        return False
    saved = _read_identity(_identity_file(run, name))
    project = None
    port = int(env["VERIFY_PORT"])
    if name.startswith("dashboard-"):
        project = (Path(env["DANUS_AGENTS_ROOT"]) / name.removeprefix("dashboard-")).resolve()
        port = int(env["DASHBOARD_PORT"])
    health = _http_health(port)
    if health is not None and not _health_matches(health, pid, project):
        return False
    return saved == current or (saved is None and _health_matches(health, pid, project))


def down(target: str, *, root: Optional[Path] = None) -> List[Dict]:
    if target not in ("verify", "dashboard", "all"):
        raise SystemExit("service must be verify, dashboard, or all")
    env = service_env(root)
    run, _ = _dirs(env)
    _manifest(run)  # reject injected manifests before stopping or deleting evidence
    if target == "all":
        names = ["verify", *[_service_name(p.stem) for p in sorted(run.glob("dashboard-*.pid"))]]
    elif target == "dashboard":
        names = [_service_name(p.stem) for p in sorted(run.glob("dashboard-*.pid"))]
    else:
        names = ["verify"]
    rows = []
    for name in dict.fromkeys(names):
        pid_file, identity_file = _pid_file(run, name), _identity_file(run, name)
        pid = _read_pid(pid_file)
        if not pid_file.exists():
            rows.append({"service": name, "state": "not-running", "pid": None})
            continue
        if not runtime.pid_alive(pid):
            pid_file.unlink()
            identity_file.unlink(missing_ok=True)
            entry = "verify" if name == "verify" else f"dashboard {name.removeprefix('dashboard-')}"
            _set_manifest(run, entry, False)
            rows.append({"service": name, "state": "cleared-stale", "pid": pid})
            continue
        if not _safe_to_stop(name, pid, env, run):
            rows.append({"service": name, "state": "refused-unsafe", "pid": pid})
            continue
        runtime.terminate_process_tree(pid, force=False)
        if runtime.pid_alive(pid):
            runtime.terminate_process_tree(pid, force=True)
        if runtime.pid_alive(pid):
            rows.append({"service": name, "state": "stop-failed", "pid": pid})
            continue
        pid_file.unlink()
        identity_file.unlink(missing_ok=True)
        entry = "verify" if name == "verify" else f"dashboard {name.removeprefix('dashboard-')}"
        _set_manifest(run, entry, False)
        rows.append({"service": name, "state": "stopped", "pid": pid})
    return rows


def logs(service: str, *, root: Optional[Path] = None, lines: int = 50) -> str:
    service = _service_name(service)
    env = service_env(root)
    _, log_dir = _dirs(env)
    path = _log_file(log_dir, service)
    if not path.is_file():
        raise SystemExit(f"no log: {path}")
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def test(*, root: Optional[Path] = None) -> List[Dict]:
    return status(root=root)


def configure_parser(sub) -> None:
    services = sub.add_parser("services", help="manage verify and dashboard services")
    commands = services.add_subparsers(dest="services_cmd", required=True)
    up_p = commands.add_parser("up")
    up_p.add_argument("service", choices=("verify", "dashboard"))
    up_p.add_argument("project", nargs="?", type=_arg_identifier)
    commands.add_parser("status").add_argument("--json", action="store_true")
    commands.add_parser("test").add_argument("--json", action="store_true")
    log_p = commands.add_parser("logs")
    log_p.add_argument("service", type=_arg_service_name)
    down_p = commands.add_parser("down")
    down_p.add_argument("service", choices=("verify", "dashboard", "all"))


def dispatch(args) -> int:
    if args.services_cmd == "up":
        rows = [up(args.service, args.project)]
    elif args.services_cmd in ("status", "test"):
        rows = status() if args.services_cmd == "status" else test()
    elif args.services_cmd == "logs":
        print(logs(args.service))
        return 0
    else:
        rows = down(args.service)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            pid = f" pid {row['pid']}" if row.get("pid") else ""
            print(f"{row['service']}: {row['state']}{pid}")
    return 1 if args.services_cmd == "test" and rows[0]["state"] != "up" else 0
