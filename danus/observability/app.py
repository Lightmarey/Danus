"""Danus observability — a local research console for one project.

A single self-contained FastAPI app serving a one-page client (echarts + KaTeX +
markdown-it via CDN — no build step). v2 research reads use the shared indexed
ResearchQuery. The only writes are capability-protected target governance:

  <project>/fact_graph/facts/*.md         the verified-fact DAG
  <project>/global_memory/<kind>.jsonl    categorized findings (the 11 kinds)
  <project>/spend/consult.jsonl       pro-consult cost ledger (optional)

Decoupled by design: it imports no danus.core runtime module. The channel set is
a plain data constant here (mirrors core.schema.GLOBAL_KINDS); if core changes it,
re-sync ``CHANNELS`` below.

Config is read at CALL time from args / env (never at import): project dir from
``--project`` / ``DANUS_DASHBOARD_PROJECT`` / ``DANUS_PROJECT_DIR``; bind
127.0.0.1:8099 (loopback only) by default.

Run:
    python -m danus.observability --project /path/to/project [--port 8099]
    DANUS_PROJECT_DIR=/path/to/project python -m danus.observability
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from danus.control import ControlError, ControlStore, require_v2_project
from danus.control_service import ControlService
from danus.research import ResearchQuery

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
CONTROL_TOKEN = os.environ.get("DANUS_CONTROL_TOKEN") or secrets.token_urlsafe(32)

# ------------------------------------------------------------------------- #
# channels — global-memory kinds in display order, each with a semantic role  #
# tag the client uses to color/group them. Mirrors danus.core GLOBAL_KINDS    #
# (kept as data on purpose — the dashboard imports no core runtime module).   #
# ------------------------------------------------------------------------- #
CHANNELS = [
    ("conclusion", "result"), ("example", "result"), ("counterexample", "result"),
    ("proof_attempt", "result"), ("plan", "judgment"), ("direction", "judgment"),
    ("obstacle", "deadend"), ("dead_end", "deadend"), ("verification", "verify"),
    ("elaboration", "strategy"), ("master_guidance", "strategy"),
]
_CHANNEL_KINDS = {k for k, _ in CHANNELS}


# ------------------------------------------------------------------------- #
# config — resolved at CALL time (never at import)                           #
# ------------------------------------------------------------------------- #

def _project_dir() -> Path:
    p = os.environ.get("DANUS_DASHBOARD_PROJECT") or os.environ.get("DANUS_PROJECT_DIR")
    if not p:
        raise RuntimeError("no project dir — set --project / DANUS_PROJECT_DIR")
    project = Path(p)
    return require_v2_project(project) if project.is_dir() else project


def _channel_file(project: Path, kind: str) -> Path:
    return project / "global_memory" / f"{kind}.jsonl"


def _spend_file(project: Path) -> Path:
    return project / "spend" / "consult.jsonl"


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read a .jsonl line-by-line, skipping blank/malformed lines. Missing file
    -> empty. Never raises on bad data (stores are appended while we read)."""
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _load_channel(project: Path, kind: str) -> List[Dict[str, Any]]:
    return _load_jsonl(_channel_file(project, kind))


def _load_spend(project: Path) -> List[Dict[str, Any]]:
    return _load_jsonl(_spend_file(project))


# ------------------------------------------------------------------------- #
# typed response models (the four /api/* payloads)                           #
# ------------------------------------------------------------------------- #

class Overview(BaseModel):
    project: str
    facts: int
    facts_with_predecessors: int
    facts_by_author: Dict[str, int]
    channel_counts: Dict[str, int]
    verdicts: Dict[str, int]
    consult_count: int
    consult_cost_usd: float
    updated_at: float


class ChannelInfo(BaseModel):
    kind: str
    role: str
    count: int


class ChannelsResp(BaseModel):
    channels: List[ChannelInfo]


class ChannelResp(BaseModel):
    kind: str
    count: int
    entries: List[Dict[str, Any]]


# ------------------------------------------------------------------------- #
# route implementations (pure functions — testable offline without a client) #
# ------------------------------------------------------------------------- #

def build_overview(project: Optional[Path] = None) -> Dict[str, Any]:
    project = require_v2_project(project or _project_dir())
    control = ControlStore(project)
    control.scaffold()
    with control._connect() as db:
        facts = [dict(row) for row in db.execute("SELECT author FROM facts")]
        linked = int(db.execute("SELECT COUNT(DISTINCT fact_id) FROM fact_edges").fetchone()[0])
    counts = {k: len(_load_channel(project, k)) for k, _ in CHANNELS}
    verdicts: Dict[str, int] = {}
    for e in _load_channel(project, "verification"):
        v = e.get("verdict", "?")
        verdicts[v] = verdicts.get(v, 0) + 1
    spend = _load_spend(project)
    total_cost = round(sum(float(s.get("cost_usd", 0.0) or 0.0) for s in spend), 2)
    by_author: Dict[str, int] = {}
    for f in facts:
        by_author[f["author"]] = by_author.get(f["author"], 0) + 1
    return {
        "project": project.name,
        "facts": len(facts),
        "facts_with_predecessors": linked,
        "facts_by_author": by_author,
        "channel_counts": counts,
        "verdicts": verdicts,
        "consult_count": len(spend),
        "consult_cost_usd": total_cost,
        "updated_at": time.time(),
    }


def build_channels(project: Optional[Path] = None) -> Dict[str, Any]:
    project = require_v2_project(project or _project_dir())
    return {"channels": [{"kind": k, "role": r, "count": len(_load_channel(project, k))}
                         for k, r in CHANNELS]}


def build_channel(kind: str, project: Optional[Path] = None) -> Dict[str, Any]:
    if kind not in _CHANNEL_KINDS:
        raise KeyError(kind)
    project = require_v2_project(project or _project_dir())
    entries = _load_channel(project, kind)
    entries.sort(key=lambda e: e.get("timestamp_utc", ""), reverse=True)
    return {"kind": kind, "count": len(entries), "entries": entries}


def build_control(project: Optional[Path] = None) -> Dict[str, Any]:
    """Control summary backed by the same ResearchQuery as agents."""
    project = require_v2_project(project or _project_dir())
    query = ResearchQuery(project)
    research = query.research_map()
    store = query.store
    assignments = [item for worker in [row["worker"] for row in _assignment_rows(store)] if (item := store.assignment(worker))]
    costs = store.events("cost")
    routes = [route for method in research["methods"] for route in method["routes"]]
    return {"generation": research["generation"],
            "current_target": (research["active_target"] or {}).get("version"),
            "targets": research["targets"], "obligations": research.get("obligations", []),
            "routes": routes, "methods": research["methods"], "assignments": assignments,
            "budget": research["budget"], "outbox": research["outbox"],
            "cost": {"events": len(costs), "wall_seconds": sum(float(item.get("wall_seconds") or 0) for item in costs),
                     "cost_usd": sum(float(item.get("cost_usd") or 0) for item in costs if item.get("cost_usd") is not None)}}


def _assignment_rows(store: ControlStore) -> List[Dict[str, Any]]:
    with store._connect() as db:
        return [dict(row) for row in db.execute("SELECT worker FROM assignments ORDER BY worker")]


class TargetCommand(BaseModel):
    request_id: str
    expected_generation: int
    reason: str = ""


def _authorize_control(request: Request) -> None:
    if request.headers.get("X-Danus-Control-Token") != CONTROL_TOKEN:
        raise HTTPException(401, "invalid control capability")
    origin = request.headers.get("Origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if not origin or origin.rstrip("/") != expected:
        raise HTTPException(403, "invalid Origin")


def _control_service(project: Path) -> ControlService:
    from danus.execution import layout as layout
    from danus.orchestration.cli import _stop_one
    return ControlService(project, lambda kind, payload: (
        _stop_one(layout.WorkerLayout(project / "workers" / payload["worker"]), force=True)
        if kind == "stop_worker" else None
    ))


# ------------------------------------------------------------------------- #
# app                                                                        #
# ------------------------------------------------------------------------- #

app = FastAPI(title="danus-observability", version="0.1.0")


@app.middleware("http")
async def no_store_dashboard_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/overview", response_model=Overview)
def overview() -> JSONResponse:
    return JSONResponse(build_overview())


@app.get("/api/channels", response_model=ChannelsResp)
def channels() -> JSONResponse:
    return JSONResponse(build_channels())


@app.get("/api/control")
def control() -> JSONResponse:
    return JSONResponse(build_control())


@app.get("/api/research/map")
def research_map(target_version: Optional[str] = None) -> JSONResponse:
    return JSONResponse(ResearchQuery(_project_dir()).research_map(target_version))


@app.get("/api/research/archive")
def research_archive() -> JSONResponse:
    return JSONResponse(ResearchQuery(_project_dir()).archive_fact_graph())


@app.get("/api/research/routes/{route_id}")
def research_route(route_id: str, snapshot: Optional[int] = None) -> JSONResponse:
    try:
        return JSONResponse(ResearchQuery(_project_dir()).route_context(route_id, snapshot=snapshot))
    except (ControlError, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/research/obligations/{obligation_id}")
def research_obligation(obligation_id: str, snapshot: Optional[int] = None) -> JSONResponse:
    try:
        return JSONResponse(ResearchQuery(_project_dir()).obligation_context(obligation_id, snapshot=snapshot))
    except (ControlError, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/research/facts/{fact_id}")
def research_fact(fact_id: str, include_proof: bool = False) -> JSONResponse:
    try:
        return JSONResponse(ResearchQuery(_project_dir()).fact_get(fact_id, include_proof=include_proof))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/research/facts/{fact_id}/neighborhood")
def research_neighborhood(fact_id: str, direction: str = "both", depth: int = 1, limit: int = 300) -> JSONResponse:
    try:
        return JSONResponse(ResearchQuery(_project_dir()).fact_neighborhood(fact_id, direction=direction, depth=depth, limit=limit))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/research/context-manifests")
def context_manifests(worker: Optional[str] = None, limit: int = 20) -> JSONResponse:
    return JSONResponse({"manifests": ResearchQuery(_project_dir()).list_context_manifests(worker=worker, limit=limit)})


@app.post("/api/control/targets/{version}/approve")
def approve_target(version: str, command: TargetCommand, request: Request) -> JSONResponse:
    _authorize_control(request)
    try:
        result = _control_service(_project_dir()).approve_target(version, request_id=command.request_id, expected_generation=command.expected_generation)
        return JSONResponse(result)
    except ControlError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/control/targets/{version}/withdraw")
def withdraw_target(version: str, command: TargetCommand, request: Request) -> JSONResponse:
    _authorize_control(request)
    try:
        result = _control_service(_project_dir()).withdraw_target(version, reason=command.reason, request_id=command.request_id, expected_generation=command.expected_generation)
        return JSONResponse(result)
    except ControlError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/channel/{kind}", response_model=ChannelResp)
def channel(kind: str) -> JSONResponse:
    try:
        return JSONResponse(build_channel(kind))
    except KeyError:
        raise HTTPException(404, f"unknown channel {kind}")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/health")
def health() -> Dict[str, Any]:
    from danus.runtime import process_identity

    pid = os.getpid()
    return {
        "status": "ok",
        "pid": pid,
        "identity": process_identity(pid),
        "project": str(_project_dir().resolve()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Danus read-only fact-graph + global-memory dashboard.")
    ap.add_argument("--project", help="project dir (or set DANUS_PROJECT_DIR)")
    ap.add_argument("--host", default="127.0.0.1")  # loopback only; expose via SSH port-forward
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    if args.project:
        os.environ["DANUS_DASHBOARD_PROJECT"] = args.project
    project = _project_dir()  # fail fast if unset
    if not project.is_dir():
        raise SystemExit(f"project dir not found: {project}")
    import uvicorn
    # The detached service manager redirects stdout to a log.  Flush the
    # capability URL so the platform launchers can read it immediately and
    # open the dashboard without persisting the token anywhere else.
    print(
        f"danus dashboard: http://{args.host}:{args.port}/#control-token={CONTROL_TOKEN}  (project: {project})",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
