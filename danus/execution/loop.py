"""The per-worker bounded exploration-round driver.

Launched detached by ``danus start`` (``python -m danus.execution <worker_dir>``).
Self-contained. Each round runs one ``codex exec`` session bound to an approved
target, obligation, route, assignment epoch, and finite round budget. The controller
scores its structured WorkReport and decides whether to renew, audit, fall back,
pause, or complete.

Config:
  - codex binary resolved via the shared ``danus.codex`` launcher
    (``DANUS_CODEX_BIN`` / ``CODEX_BIN`` alias / PATH);
  - all config read at CALL time from env (matches core/gateway/verify).

Env (all optional; tests inject these):
  DANUS_CODEX_BIN            codex binary (default "codex")
  DANUS_ROUND_BEAT           seconds to sleep between rounds (default 5)
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import layout as L
from . import scaffold
from danus import codex, runtime
from danus.control import (
    ControlError, ControlStore, parse_codex_usage, parse_work_report,
    require_v2_project, work_report_valid,
)
from danus.research import ResearchQuery

_FACT_ID_RE = re.compile(r'"?fact_id"?\s*[:=]\s*"?([0-9a-f]{16})"?')


# --- the round prompt ------------------------------------------------------ #

def kickoff(project: str, worker: str, assignment: dict, *, audit: bool,
            context: str = "") -> str:
    mode = (
        "This round is an independent route audit. Do not repeat the route. Compare its "
        "failed signatures and evidence with the obligation, identify a genuinely new route "
        "or report no progress honestly."
        if audit else
        "Explore the assigned route deeply for this bounded round. Preserve useful partial "
        "progress, but do not change the approved target or silently adopt extra assumptions."
    )
    scope = {
        "target_version": assignment["target_version"],
        "obligation_id": assignment["obligation_id"],
        "route_id": assignment["route_id"],
        "assignment_epoch": assignment["epoch"],
        "round_number": int(assignment.get("rounds_used", 0)) + 1,
    }
    return (
        f"You are worker '{worker}' on Danus v2 project '{project}'.\n"
        f"{mode}\n"
        f"Authoritative scope:\n{json.dumps(scope, ensure_ascii=False, indent=2)}\n"
        f"Assigned task:\n{assignment['task']}\n"
        f"\n{context}\n"
        "AGENTS.md, the task, and this research snapshot are already loaded. Do not reopen them, "
        "list the workspace, or perform routine memory/fact searches. Use included facts first and "
        "expand only what the route needs. Work only in the scope above. Finish this round with the "
        "required WorkReport JSON; after completion or a control/budget/provider block, stop."
    )


# --- config (read at call time) -------------------------------------------- #

# codex binary + model/effort defaults are resolved via the shared danus.codex
# launcher (DANUS_CODEX_BIN / DANUS_CODEX_MODEL / DANUS_CODEX_EFFORT).


# --- small helpers --------------------------------------------------------- #

def _read_role(wl: L.WorkerLayout) -> dict:
    out = {"MODEL": codex.model(),
           "REASONING_EFFORT": "high", "ROLE": "high", "DANUS_AUTHOR": wl.name}
    rp = wl.role
    if rp.exists():
        for line in rp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_status(wl: L.WorkerLayout, **fields) -> None:
    """Atomic status write (so `danus status` never reads a half-written file)."""
    path = wl.status
    cur = {}
    if path.exists():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cur = {}
    cur.update(fields)
    cur["worker"] = wl.name
    cur["pid"] = os.getpid()
    cur["updated_at"] = time.time()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _write_own_pid(wl: L.WorkerLayout) -> None:
    """Claim the worker .pid with the actual loop pid.

    On Windows the spawned pid observed by the parent can differ from the long-lived
    interpreter pid that writes status and later performs cleanup. Overwrite the file
    from inside the loop so liveness and cleanup always refer to the real owner.
    """
    tmp = wl.pid.with_suffix(wl.pid.suffix + ".tmp")
    tmp.write_text(str(os.getpid()), encoding="utf-8")
    os.replace(tmp, wl.pid)


def _deadline_passed(project_dir: Path) -> bool:
    f = project_dir / L.DEADLINE_FILE
    if not f.exists():
        return False
    try:
        return time.time() >= float(f.read_text().strip())
    except (ValueError, OSError):
        return False


def _parse_last_fact_id(log_path: Path) -> Optional[str]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    saw_json_event = False
    saw_structured_submission = False
    accepted: Optional[str] = None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_json_event = saw_json_event or isinstance(event, dict)
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict) or item.get("type") != "mcp_tool_call" or item.get("tool") != "fact_submit":
            continue
        saw_structured_submission = True
        result = item.get("result")
        payload = result.get("structured_content", {}).get("result") if isinstance(result, dict) else None
        if isinstance(payload, dict) and payload.get("accepted") is True:
            fact_id = payload.get("fact_id")
            if isinstance(fact_id, str) and re.fullmatch(r"[0-9a-f]{16}", fact_id):
                accepted = fact_id
    if saw_structured_submission or saw_json_event:
        return accepted
    ids = _FACT_ID_RE.findall(text)
    return ids[-1] if ids else None


# --- one round ------------------------------------------------------------- #

class _Child:
    """Holds the running codex subprocess so the SIGTERM handler can kill it."""
    proc: "subprocess.Popen | None" = None


def run_round(wl: L.WorkerLayout, role: dict, prompt: str, log_path: Path,
              hard_timeout: int, *, report_path: Path,
              output_schema: Path,
              reservation_id: Optional[str] = None) -> int:
    """Exec one structured round; return rc, timeout 124, or missing-bin 127."""
    wdir = wl.dir
    codex_bin = codex.resolve_bin()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.unlink(missing_ok=True)
    structured = [
        "--config", scaffold.worker_gateway_config_arg(wl),
        "--output-schema", str(output_schema),
        "--output-last-message", str(report_path),
    ]
    cmd = codex.exec_cmd(
        codex_bin, role["MODEL"], role["REASONING_EFFORT"],
        "-C", str(wdir),
        # on an install without .git (tarball download), codex's
        # trusted-directory check refuses to run the worker round
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        *structured,
        prompt,
    )
    timed_out = False
    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            child_env = codex.subprocess_env(codex_bin)
            if reservation_id:
                child_env["DANUS_CALL_RESERVATION_ID"] = reservation_id
            _Child.proc = runtime.spawn_process(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=str(wdir),
                env=child_env,
                new_process_group=True,
            )
        except FileNotFoundError:
            logf.write(f"[worker_loop] codex binary not found: {cmd[0]}\n")
            return 127
        try:
            deadline = time.monotonic() + hard_timeout if hard_timeout > 0 else None
            while True:
                wait_for = .5 if deadline is None else min(.5, max(.01, deadline - time.monotonic()))
                try:
                    return _Child.proc.wait(timeout=wait_for)
                except subprocess.TimeoutExpired:
                    if wl.stop.exists():
                        runtime.stop_process(_Child.proc, wait_seconds=1, force=True)
                        logf.write("\n[worker_loop] round interrupted by stop request\n")
                        return 130
                    if deadline is not None and time.monotonic() >= deadline:
                        raise
        except subprocess.TimeoutExpired:
            runtime.stop_process(_Child.proc, wait_seconds=10, force=True)
            logf.write(f"\n[worker_loop] round hard-timeout after {hard_timeout}s\n")
            timed_out = True
        finally:
            _Child.proc = None
    if timed_out:
        if not runtime.wait_until_path_releasable(log_path, timeout_seconds=10):
            raise RuntimeError(f"log path still locked after timeout cleanup: {log_path}")
        return 124


# --- the loop -------------------------------------------------------------- #

def _cleanup_pid(wl: L.WorkerLayout) -> None:
    """Remove our own .pid if it still points at us (clean exit only)."""
    pf = wl.pid
    try:
        if pf.exists() and pf.read_text().strip() == str(os.getpid()):
            pf.unlink(missing_ok=True)
    except OSError:
        pass


def refresh_worker_assets(wl: L.WorkerLayout) -> None:
    runtime.sync_symlink_or_copy(L.worker_md(), wl.dir / "AGENTS.md")
    runtime.sync_symlink_or_copy(
        L.worker_skills_dir(), wl.dir / ".agents" / "skills",
    )


def _run_loop(wl: L.WorkerLayout, role: dict, control: ControlStore, beat: float) -> int:
    """Run finite, controller-scored exploration rounds for one assignment."""
    worker = wl.name
    while True:
        if wl.stop.exists():
            wl.stop.unlink(missing_ok=True)
            write_status(wl, state="stopped")
            return 0
        if _deadline_passed(wl.project_dir):
            write_status(wl, state="deadline")
            return 0
        probe_claimed = False
        raw_assignment = control.assignment(worker)
        if raw_assignment and raw_assignment.get("status") == "waiting_retry":
            gate = control.claim_backend_call("codex")
            if not gate["allowed"]:
                write_status(wl, state="waiting_retry", failure_class=gate.get("failure_class"), retry_after_seconds=gate.get("wait_seconds"))
                if gate.get("wait_seconds") is None:
                    return 0
                time.sleep(min(float(gate["wait_seconds"]), max(1.0, beat)))
                continue
            control.resume_worker_retry(worker)
            probe_claimed = True
        try:
            assignment = control.validate_assignment(worker)
        except ControlError as exc:
            write_status(wl, state="waiting", control_reason=str(exc))
            return 0
        if not probe_claimed:
            gate = control.claim_backend_call("codex")
            if not gate["allowed"]:
                write_status(wl, state="waiting_retry", failure_class=gate.get("failure_class"), retry_after_seconds=gate.get("wait_seconds"))
                if gate.get("wait_seconds") is None:
                    return 0
                time.sleep(min(float(gate["wait_seconds"]), max(1.0, beat)))
                continue

        audit = bool(assignment.get("audit_required"))
        round_no = int(assignment["rounds_used"]) + 1
        manifest = ResearchQuery(wl.project_dir).build_context_manifest(worker)
        prompt = kickoff(
            wl.project, worker, assignment, audit=audit,
            context=ResearchQuery.format_context_manifest(manifest),
        )
        log_path = wl.logs / f"round_{round_no}.jsonl"
        report_path = wl.logs / f"round_{round_no}_report.json"
        write_status(
            wl, state="auditing" if audit else "running", round=round_no,
            round_started_at=time.time(), target_version=assignment["target_version"],
            obligation_id=assignment["obligation_id"], route_id=assignment["route_id"],
            context_manifest_id=manifest["id"], context_snapshot=manifest["snapshot_generation"],
            last_rc=None, usage_status=None, control_reason=None, error=None,
        )
        try:
            reservation = control.reserve_call(
                component="worker_round", max_wall_seconds=float(assignment["round_timeout_seconds"]),
                worker=worker, assignment_epoch=assignment["epoch"],
                target_version=assignment["target_version"], obligation_id=assignment["obligation_id"],
                route_id=assignment["route_id"],
            )
        except ControlError as exc:
            if probe_claimed:
                control.cancel_backend_probe("codex")
            write_status(wl, state="budget_exhausted", control_reason=str(exc))
            return 0
        started = time.monotonic()
        rc = run_round(
            wl, role, prompt, log_path, int(assignment["round_timeout_seconds"]),
            report_path=report_path, output_schema=control.work_report_schema,
            reservation_id=reservation["id"],
        )
        wall = time.monotonic() - started
        report = parse_work_report(report_path)
        complete_report = report_path.is_file() and work_report_valid(report)
        if rc == 130 and wl.stop.exists():
            usage = parse_codex_usage(log_path)
            control.record_worker_interruption(
                worker, wall_seconds=wall, usage=usage,
                reservation_id=reservation["id"],
            )
            wl.stop.unlink(missing_ok=True)
            write_status(
                wl, state="stopped", last_rc=rc,
                usage_status="partial" if usage else "unavailable",
                control_reason="operator stop interrupted the active round",
            )
            return 0
        if rc != 0 and not (rc == 124 and complete_report):
            outcome = codex.classify_failure(rc, log_path)
            failure = control.record_worker_infra_failure(
                worker, outcome, wall_seconds=wall, usage=parse_codex_usage(log_path),
                reservation_id=reservation["id"],
            )
            write_status(
                wl, state=failure["assignment"]["status"], last_rc=rc,
                failure_class=outcome["failure_class"],
                infra_failure_count=failure["assignment"]["infra_failure_count"],
                retry_after_seconds=failure["wait_seconds"],
            )
            if failure["blocked"]:
                return 127 if rc == 127 else 1
            continue
        control.record_worker_call_success(worker)
        result = control.evaluate_work_report(
            worker, report, wall_seconds=wall,
            usage=parse_codex_usage(log_path),
            reservation_id=reservation["id"],
        )
        current = result["assignment"]
        write_status(
            wl, state=current["status"], round=current["rounds_used"],
            last_round_at=time.time(), last_rc=rc, gain=result["gain"],
            decision=result["decision"], last_fact_id=_parse_last_fact_id(log_path),
        )
        if result["decision"] in {"stalled", "budget_exhausted"}:
            fallback = None if result.get("project_budget_blocked") else control.activate_fallback(worker)
            if fallback:
                write_status(wl, state="fallback", route_id=fallback["route_id"])
                continue
            write_status(wl, state="paused", control_reason=result["decision"])
            return 0
        if result["decision"] == "completed":
            write_status(wl, state="waiting", control_reason="assignment completed")
            return 0
        if result["decision"] == "invalidated":
            write_status(wl, state="waiting", control_reason="assignment invalidated while the round was running")
            return 0
        if beat > 0:
            time.sleep(beat)


def main(worker_dir: str) -> int:
    wdir = Path(worker_dir).resolve()
    if not wdir.is_dir():
        print(f"worker dir not found: {wdir}", file=sys.stderr)
        return 2
    wl = L.WorkerLayout(wdir)
    project_dir = wl.project_dir
    try:
        require_v2_project(project_dir)
    except ControlError as exc:
        write_status(wl, state="error", control_reason=str(exc), error=str(exc))
        return 2
    role = _read_role(wl)
    control = ControlStore(project_dir)
    refresh_worker_assets(wl)

    # Refresh the worker gateway command from the shared runtime resolver on every
    # start, so a moved/rebuilt or explicitly configured interpreter is picked up.
    scaffold.write_codex_config(wl)

    beat = float(os.environ.get("DANUS_ROUND_BEAT", "5"))
    wl.logs.mkdir(parents=True, exist_ok=True)

    def _on_term(signum, _frame):
        if _Child.proc is not None:
            runtime.stop_process(_Child.proc, wait_seconds=1, force=True)
        write_status(wl, state="terminated")
        _cleanup_pid(wl)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)

    _write_own_pid(wl)
    write_status(
        wl, state="running", round=0, started_at=time.time(),
        last_rc=None, last_fact_id=None, usage_status=None, control_reason=None,
        error=None,
    )
    try:
        return _run_loop(wl, role, control, beat)
    finally:
        _cleanup_pid(wl)
