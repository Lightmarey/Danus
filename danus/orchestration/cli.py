"""``danus`` — the main agent's control surface over codex workers.

    danus list   [--json]
    danus new    <project> [--roles high:3,xhigh:4] [--model M]
    danus assign <project>/<worker> (--task "…" | --file P | --stdin)
    danus finalize <project> [--paper <paper_id>] [<fact_id> ...]
    danus start  <project>[/<worker>]
    danus status <project>[/<worker>] [--json]
    danus stop   <project>[/<worker>] [--force]

This module is the verbs/UX only. The worker outer loop, the on-disk layout, and
the scaffolding they drive live in ``danus.execution`` (imported here as a
library). Reads/writes only files under the project dir — the loop is autonomous;
this CLI just assigns / starts / monitors / stops it.

Notes:
  - the layout + scaffolding + config template are imported from ``danus.execution``
    (no duplicated layout / config template);
  - the verbs are mode-agnostic and identical across deployments.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from danus.execution import layout as L
from danus.execution.scaffold import atomic_write, do_new, spawn_loop
from danus import runtime
from danus.control import ControlError, ControlStore, is_v2_project

__all__ = [
    "do_new", "do_assign", "do_start", "do_status", "worker_status",
    "do_list", "do_stop", "do_finalize", "do_target", "do_obligation",
    "do_route", "do_control_rebuild", "do_control_taint", "build_parser", "main",
]


# --------------------------------------------------------------------------- #
# read helpers                                                                 #
# --------------------------------------------------------------------------- #

def _read_pid(wl: L.WorkerLayout) -> Optional[int]:
    pf = wl.pid
    if not pf.exists():
        return None
    try:
        return int(pf.read_text().strip())
    except (ValueError, OSError):
        return None


def _alive(pid: Optional[int]) -> bool:
    return runtime.pid_alive(pid)


def _read_status(wl: L.WorkerLayout) -> Dict:
    sp = wl.status
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# --------------------------------------------------------------------------- #
# assign                                                                       #
# --------------------------------------------------------------------------- #

def do_assign(target: str, task: str, *, obligation: Optional[str] = None,
              route: Optional[str] = None, max_slices: int = 12,
              slice_timeout: int = 5400) -> Dict:
    """Overwrite (replace, NOT append) a worker's TASK.md, ensuring a trailing
    newline. Rejects a bare project, a nonexistent worker, and an empty task."""
    project, worker = L.resolve_target(target)
    if not worker:
        raise SystemExit("assign needs a specific worker: <project>/<worker>")
    wl = L.WorkerLayout(L.worker_dir(project, worker))
    if not wl.dir.is_dir():
        raise SystemExit(f"no such worker: {project}/{worker}")
    if not task.strip():
        raise SystemExit("refusing to assign an empty task")
    control = ControlStore(wl.project_dir)
    assignment = None
    if control.enabled:
        if not obligation or not route:
            raise SystemExit("Danus v2 assign requires --obligation and --route")
        try:
            assignment = control.assign(
                worker, obligation_id=obligation, route_id=route, task=task,
                max_slices=max_slices, slice_timeout=slice_timeout,
            )
        except ControlError as exc:
            raise SystemExit(f"cannot assign: {exc}") from exc
    atomic_write(wl.task, task if task.endswith("\n") else task + "\n")
    return {"worker": f"{project}/{worker}", "task_file": str(wl.task),
            "assignment": assignment}


# --------------------------------------------------------------------------- #
# finalize                                                                     #
# --------------------------------------------------------------------------- #

def do_finalize(project: str, fact_ids: List[str],
                paper_id: Optional[str] = None) -> Dict:
    """Record the finalized target theorem(s) for a PAPER of a project in that
    paper's TARGET.md — the durable slot write-paper reads (never a guess). The
    default paper writes the LEGACY ``<project>/TARGET.md``; a non-default
    ``paper_id`` writes ``<project>/papers/<paper_id>/TARGET.md`` (its own
    workspace). One fact graph per project; per-paper targets.

    Resolves the project dir, VALIDATES every ``fact_id`` against that project's
    fact graph (refuses an id the graph does not have — you cannot record a
    phantom target), then writes the ids to the paper's TARGET.md.

    With NO ``fact_ids`` (suggestion mode): prints the candidate terminal facts
    (facts that are no other fact's predecessor — the ``assemble._terminal_facts``
    helper) as SUGGESTIONS and writes NOTHING (returns ``{"suggested": [...]}``).

    Rejections raise ``SystemExit`` (nonzero exit) with a clear message."""
    from danus.core import FactGraph
    from danus.write_paper import assemble

    pdir = L.project_dir(project)
    if not pdir.is_dir():
        raise SystemExit(f"no such project: {project}")
    fg = FactGraph(pdir)

    if not fact_ids:
        # suggestion mode: never auto-pick — just list candidate terminal facts.
        return {"project": project, "paper_id": paper_id,
                "suggested": assemble._terminal_facts(fg)}

    unknown = [fid for fid in fact_ids if not fg.exists(fid)]
    if unknown:
        raise SystemExit(
            f"cannot finalize: unknown fact id(s) in {project}: {', '.join(unknown)} "
            f"(a target must be a verified fact in the project's graph)"
        )
    # validate a non-default paper_id as a single safe path segment before writing.
    try:
        if not assemble._is_default_paper(paper_id):
            assemble._validate_paper_id(paper_id)  # type: ignore[arg-type]
    except ValueError as e:
        raise SystemExit(f"cannot finalize: {e}")
    # de-dup while preserving order
    seen: set = set()
    ids: List[str] = []
    for fid in fact_ids:
        if fid not in seen:
            seen.add(fid)
            ids.append(fid)
    path = assemble.write_target_fact_ids(pdir, ids, paper_id)
    return {"project": project, "paper_id": paper_id,
            "target_file": str(path), "target_fact_ids": ids}


# --------------------------------------------------------------------------- #
# start                                                                        #
# --------------------------------------------------------------------------- #

def _start_one(wl: L.WorkerLayout) -> str:
    """Returns 'started' / 'already-running' / 'locked'. Idempotent via an flock
    on .pid.lock; clears a stale .stop before spawning."""
    wl.dir.mkdir(parents=True, exist_ok=True)
    wl.logs.mkdir(exist_ok=True)
    with runtime.file_lock(wl.lock) as lock:
        if lock is None:
            return "locked"
        if _alive(_read_pid(wl)):
            return "already-running"
        wl.stop.unlink(missing_ok=True)  # clear a stale stop flag
        pid = spawn_loop(wl.dir)
        atomic_write(wl.pid, str(pid))
        return "started"


def do_start(target: str, stagger: float = 0.2) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    out = []
    for i, wdir in enumerate(dirs):
        if i and stagger:
            time.sleep(stagger)
        wl = L.WorkerLayout(wdir)
        control = ControlStore(wl.project_dir)
        if control.enabled:
            try:
                control.validate_assignment(wl.name)
            except ControlError as exc:
                out.append({"worker": wdir.name, "result": "waiting", "reason": str(exc)})
                continue
        out.append({"worker": wdir.name, "result": _start_one(wl)})
    return out


# --------------------------------------------------------------------------- #
# status                                                                       #
# --------------------------------------------------------------------------- #

def worker_status(wl: L.WorkerLayout) -> Dict:
    pid = _read_pid(wl)
    alive = _alive(pid)
    st = _read_status(wl)
    state = st.get("state", "-")
    now = time.time()
    last = st.get("last_round_at") or st.get("round_started_at") or st.get("updated_at")
    age = (now - last) if isinstance(last, (int, float)) else None

    if alive:
        # a round legitimately runs for hours; only flag truly stale running rounds
        rs = st.get("round_started_at")
        hard = int(os.environ.get("DANUS_ROUND_HARD_TIMEOUT", "14400"))
        if state == "running" and isinstance(rs, (int, float)) and (now - rs) > hard * 1.5:
            label = "stuck?"
        else:
            label = "working"
    else:
        label = state if state in ("stopped", "deadline", "max_rounds", "error",
                                   "terminated", "created") else "dead"
    out = {
        "worker": wl.name, "pid": pid, "alive": alive, "state": state,
        "round": st.get("round", 0), "age_s": round(age, 1) if age is not None else None,
        "last_fact_id": st.get("last_fact_id"), "label": label,
    }
    control = ControlStore(wl.project_dir)
    if control.enabled:
        assignment = control.assignment(wl.name)
        out["control"] = ({
            "status": assignment.get("status"),
            "target_version": assignment.get("target_version"),
            "obligation_id": assignment.get("obligation_id"),
            "route_id": assignment.get("route_id"),
            "slice_count": assignment.get("slice_count"),
            "max_slices": assignment.get("max_slices"),
            "consecutive_low": assignment.get("consecutive_low"),
            "audit_required": assignment.get("audit_required"),
        } if assignment else {"status": "unassigned"})
    return out


def do_status(target: str) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    return [worker_status(L.WorkerLayout(d)) for d in dirs]


# --------------------------------------------------------------------------- #
# list                                                                         #
# --------------------------------------------------------------------------- #

def do_list() -> List[Dict]:
    """One row per project: roster + how many workers are live + model."""
    out: List[Dict] = []
    for project in L.list_projects():
        meta = {}
        mp = L.project_dir(project) / "project.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        workers = L.list_workers(project)
        live = sum(1 for w in workers
                   if _alive(_read_pid(L.WorkerLayout(L.worker_dir(project, w)))))
        out.append({"project": project, "workers": len(workers), "live": live,
                    "model": meta.get("model", "-")})
    return out


def _fmt_list(rows: List[Dict]) -> str:
    head = f"{'PROJECT':<24}{'WORKERS':>8}{'LIVE':>6}  {'MODEL':<12}"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(f"{r['project']:<24}{r['workers']:>8}{r['live']:>6}  {str(r['model']):<12}")
    return "\n".join(lines) if rows else "(no projects under the agents root)"


def _fmt_status(rows: List[Dict]) -> str:
    head = f"{'WORKER':<14}{'LABEL':<12}{'STATE':<13}{'ROUND':>6}  {'AGE':>7}  {'LAST_FACT':<16}"
    lines = [head, "-" * len(head)]
    for r in rows:
        age = f"{r['age_s']:.0f}s" if r["age_s"] is not None else "-"
        lines.append(f"{r['worker']:<14}{r['label']:<12}{r['state']:<13}"
                     f"{r['round']:>6}  {age:>7}  {str(r['last_fact_id'] or '-'):<16}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# stop                                                                         #
# --------------------------------------------------------------------------- #

def _stop_one(wl: L.WorkerLayout, force: bool) -> str:
    pid = _read_pid(wl)
    if not force:
        if not _alive(pid):
            return "not-running"
        wl.stop.touch()      # graceful: loop exits at round boundary
        return "stopping (graceful)"
    if not _alive(pid):
        wl.pid.unlink(missing_ok=True)
        return "not-running"
    runtime.terminate_process_tree(pid, force=False)
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    if _alive(pid):
        runtime.terminate_process_tree(pid, force=True)
    wl.pid.unlink(missing_ok=True)
    return "killed"


def do_stop(target: str, force: bool = False) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    return [{"worker": d.name, "result": _stop_one(L.WorkerLayout(d), force)} for d in dirs]


# --------------------------------------------------------------------------- #
# Danus v2 research control                                                   #
# --------------------------------------------------------------------------- #

def _control(project: str) -> ControlStore:
    pdir = L.project_dir(project)
    if not pdir.is_dir():
        raise SystemExit(f"no such project: {project}")
    store = ControlStore(pdir)
    if not store.enabled:
        raise SystemExit(f"project {project} is legacy; v2 control commands are unavailable")
    return store


def _json_object(path: str) -> Dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"JSON input must be an object: {path}")
    return value


def do_target(project: str, action: str, *, file: Optional[str] = None,
              version: Optional[str] = None, against: Optional[str] = None) -> Dict:
    store = _control(project)
    try:
        if action == "propose":
            return {"target": store.propose_target(_json_object(file or ""))}
        if action == "approve":
            result = store.approve_target(version or "")
            stopped = []
            for worker in result["stale_workers"]:
                stopped.append({"worker": worker, "result": _stop_one(
                    L.WorkerLayout(L.worker_dir(project, worker)), force=True,
                )})
            return {**result, "stopped": stopped}
        if action == "diff":
            return {"version": version, "against": against, "diff": store.target_diff(version or "", against)}
        if action == "fallback":
            result = store.propose_fallback()
            stopped = []
            for worker in result["stale_workers"]:
                stopped.append({"worker": worker, "result": _stop_one(
                    L.WorkerLayout(L.worker_dir(project, worker)), force=True,
                )})
            return {**result, "approved": False, "stopped": stopped}
        if action == "status":
            versions = [{"version": item, "state": store.target_state(item)} for item in store.target_versions()]
            return {"current": store.current_target_version(), "versions": versions}
    except ControlError as exc:
        raise SystemExit(f"target {action} failed: {exc}") from exc
    raise SystemExit(f"unknown target action: {action}")


def do_obligation(project: str, action: str, *, file: Optional[str] = None) -> Dict:
    store = _control(project)
    try:
        if action == "add":
            return {"obligation": store.add_obligation(_json_object(file or ""))}
        rows = []
        for path in sorted(store.obligations.glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            rows.append({**item, "state": store.obligation_state(item["id"])})
        return {"obligations": rows}
    except ControlError as exc:
        raise SystemExit(f"obligation {action} failed: {exc}") from exc


def do_route(project: str, action: str, *, file: Optional[str] = None) -> Dict:
    store = _control(project)
    try:
        if action == "add":
            return {"route": store.add_route(_json_object(file or ""))}
        rows = []
        for path in sorted(store.routes.glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            rows.append({**item, "state": store.route_state(item["id"])})
        return {"routes": rows}
    except ControlError as exc:
        raise SystemExit(f"route {action} failed: {exc}") from exc


def do_control_rebuild(project: str) -> Dict:
    try:
        return _control(project).rebuild_read_model()
    except ControlError as exc:
        raise SystemExit(f"control rebuild failed: {exc}") from exc


def do_control_taint(project: str, fact_id: str, reason: str) -> Dict:
    try:
        result = _control(project).taint_fact(fact_id, reason)
    except ControlError as exc:
        raise SystemExit(f"control taint failed: {exc}") from exc
    result["stopped"] = [{"worker": worker, "result": _stop_one(
        L.WorkerLayout(L.worker_dir(project, worker)), force=True,
    )} for worker in result["stale_workers"]]
    return result


# --------------------------------------------------------------------------- #
# argparse                                                                      #
# --------------------------------------------------------------------------- #

def _task_from_args(args) -> str:
    import sys
    if args.task is not None:
        return args.task
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    raise SystemExit("assign needs one of --task, --file, or --stdin")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="danus", description="Control codex workers.")
    sub = p.add_subparsers(dest="cmd", required=True)

    li = sub.add_parser("list", help="list all projects + live worker counts")
    li.add_argument("--json", action="store_true")

    n = sub.add_parser("new", help="scaffold a project + worker dirs")
    n.add_argument("project")
    n.add_argument("--roles", default="high:3,xhigh:4", help="e.g. high:3,xhigh:4 (default)")
    n.add_argument("--model", default=None)
    n.add_argument("--problem", default=None, help="optional PROBLEM.md source for the v2 project")
    n.add_argument("--legacy", action="store_true", help="explicitly create a legacy v1 project")

    a = sub.add_parser("assign", help="write a worker's per-round TASK.md")
    a.add_argument("target", help="<project>/<worker>")
    a.add_argument("--task", default=None)
    a.add_argument("--file", default=None)
    a.add_argument("--stdin", action="store_true")
    a.add_argument("--obligation", default=None, help="required v2 obligation id")
    a.add_argument("--route", default=None, help="required v2 route id")
    a.add_argument("--max-slices", type=int, default=12)
    a.add_argument("--slice-timeout", type=int, default=5400, help="seconds; default 90 minutes")

    target = sub.add_parser("target", help="versioned v2 target lifecycle")
    target_actions = target.add_subparsers(dest="target_action", required=True)
    for name in ("propose", "approve", "diff", "status", "fallback"):
        tp = target_actions.add_parser(name)
        tp.add_argument("project")
        if name == "propose":
            tp.add_argument("--file", required=True)
        if name in {"approve", "diff"}:
            tp.add_argument("version")
        if name == "diff":
            tp.add_argument("--against", default=None)

    obligation = sub.add_parser("obligation", help="v2 proof obligations")
    obligation_actions = obligation.add_subparsers(dest="obligation_action", required=True)
    for name in ("add", "status"):
        op = obligation_actions.add_parser(name)
        op.add_argument("project")
        if name == "add":
            op.add_argument("--file", required=True)

    route = sub.add_parser("route", help="v2 research routes")
    route_actions = route.add_subparsers(dest="route_action", required=True)
    for name in ("add", "status"):
        rp = route_actions.add_parser(name)
        rp.add_argument("project")
        if name == "add":
            rp.add_argument("--file", required=True)

    control = sub.add_parser("control", help="v2 derived control state")
    control_actions = control.add_subparsers(dest="control_action", required=True)
    rebuild = control_actions.add_parser("rebuild")
    rebuild.add_argument("project")
    taint = control_actions.add_parser("taint", help="mark a suspect fact and pause dependent routes")
    taint.add_argument("project")
    taint.add_argument("fact_id")
    taint.add_argument("--reason", required=True)

    f = sub.add_parser("finalize", help="record the finalized target fact_id(s) in "
                                        "a paper's TARGET.md (write-paper reads this)")
    f.add_argument("project")
    f.add_argument("--paper", default=None,
                   help="the paper_id (multiple papers per project). Default / 'main' "
                        "-> legacy <project>/TARGET.md; else "
                        "<project>/papers/<paper_id>/TARGET.md")
    f.add_argument("fact_ids", nargs="*",
                   help="the target fact id(s); omit to print candidate terminal facts")

    s = sub.add_parser("start", help="launch worker loop(s)")
    s.add_argument("target", help="<project> or <project>/<worker>")

    st = sub.add_parser("status", help="liveness + progress")
    st.add_argument("target", help="<project> or <project>/<worker>")
    st.add_argument("--json", action="store_true")

    sp = sub.add_parser("stop", help="stop worker loop(s)")
    sp.add_argument("target", help="<project> or <project>/<worker>")
    sp.add_argument("--force", action="store_true", help="kill now (else finish current round)")
    from danus import services
    services.configure_parser(sub)
    from danus import codex_backend
    codex_backend.configure_parser(sub)
    from danus.authoring import cli as authoring_cli
    authoring_cli.configure_parser(sub)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    runtime.configure_environment()
    args = build_parser().parse_args(argv)
    if args.cmd == "list":
        rows = do_list()
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else _fmt_list(rows))
    elif args.cmd == "new":
        if args.legacy and args.problem:
            raise SystemExit("new accepts either --problem (v2) or --legacy, not both")
        r = do_new(
            args.project, roles=args.roles, model=args.model,
            problem=Path(args.problem) if args.problem else None,
            control_version=1 if args.legacy else 2,
        )
        print(f"created {args.project} with {len(r['workers'])} workers: "
              f"{', '.join(r['workers'])}\n  {r['project_dir']}")
    elif args.cmd == "assign":
        r = do_assign(
            args.target, _task_from_args(args), obligation=args.obligation,
            route=args.route, max_slices=args.max_slices, slice_timeout=args.slice_timeout,
        )
        print(f"assigned {r['worker']} -> {r['task_file']}")
    elif args.cmd == "target":
        print(json.dumps(do_target(
            args.project, args.target_action, file=getattr(args, "file", None),
            version=getattr(args, "version", None), against=getattr(args, "against", None),
        ), ensure_ascii=False, indent=2))
    elif args.cmd == "obligation":
        print(json.dumps(do_obligation(
            args.project, args.obligation_action, file=getattr(args, "file", None),
        ), ensure_ascii=False, indent=2))
    elif args.cmd == "route":
        print(json.dumps(do_route(
            args.project, args.route_action, file=getattr(args, "file", None),
        ), ensure_ascii=False, indent=2))
    elif args.cmd == "control":
        result = (do_control_rebuild(args.project) if args.control_action == "rebuild"
                  else do_control_taint(args.project, args.fact_id, args.reason))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "finalize":
        r = do_finalize(args.project, args.fact_ids, paper_id=args.paper)
        paper_note = f" (paper {args.paper})" if args.paper else ""
        paper_flag = f" --paper {args.paper}" if args.paper else ""
        if "suggested" in r:
            sug = r["suggested"]
            if sug:
                print(f"no fact_id given - candidate target facts for {r['project']}{paper_note} "
                      f"(terminal facts; nothing depends on them):")
                for fid in sug:
                    print(f"  {fid}")
                print(f"\nrun: danus finalize {r['project']}{paper_flag} <fact_id> [<fact_id> ...] to record")
            else:
                print(f"no candidate terminal facts in {r['project']} "
                      f"(is the fact graph empty?); nothing recorded")
        else:
            print(f"finalized target for {r['project']}{paper_note}: {', '.join(r['target_fact_ids'])}\n"
                  f"  wrote {r['target_file']}")
    elif args.cmd == "start":
        for r in do_start(args.target):
            print(f"{r['worker']}: {r['result']}")
    elif args.cmd == "status":
        rows = do_status(args.target)
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else _fmt_status(rows))
    elif args.cmd == "stop":
        for r in do_stop(args.target, force=args.force):
            print(f"{r['worker']}: {r['result']}")
    elif args.cmd == "services":
        from danus import services
        return services.dispatch(args)
    elif args.cmd == "codex":
        from danus import codex_backend
        return codex_backend.dispatch(args)
    elif args.cmd == "artifacts":
        from danus.authoring import cli as authoring_cli
        return authoring_cli.dispatch(args)
    return 0
