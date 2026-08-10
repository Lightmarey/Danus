#!/usr/bin/env python
"""Danus gateway — the role-gated MCP server.

A thin MCP wrapper over ``danus.core`` (the truth stores) + one external
integration (``danus.integrations`` arXiv search). It exposes only the verbs an
LLM can't do reliably itself (content-addressed writes, cascade integrity, the
verifier-gated fact write, BM25) — reads / local memory / novelty judgment are
the agent's own file operations, deliberately NOT tools.

The permission model (which tools each role sees) lives in ``roles.py``. The
``fact_submit`` tool is the ONLY fact-write path: it runs the glossary-coverage
check, calls the verify service, writes the node IFF the verdict is ``correct``,
and ALWAYS traces the verdict to global memory (kind ``verification``) — accept,
reject, or accept-but-write-failed — so a verdict is never stored by nobody (the
verify service is stateless).

Config is read from the environment at CALL time (not import time) so the server
is testable and reconfigurable:
  DANUS_PROJECT_DIR   the project dir a worker is pinned to (fallback for main)
  DANUS_AGENTS_ROOT   root holding all projects (<root>/<project>); lets main
                      address any project by name via the ``project`` arg
  DANUS_AUTHOR        this agent's id, for attribution
  DANUS_ROLE          worker | main | verifier | all  (selects exposed tools;
                      unset falls back to the read-only verifier set — fail-closed)
  DANUS_VERIFY_URL    verify-service endpoint for fact_submit
  DANUS_PROBLEM_ID    problem id stamped on written facts (default: project name)
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus._mcp import FastMCP
from danus.core import FactGraph, GlobalMemory
from danus.core.schema import compute_fact_id
from danus.control import ControlError, ControlStore
from danus.integrations import search as _arxiv_search
from danus.research import ResearchQuery

from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------- #
# config resolution (env read at call time — testable / reconfigurable)       #
# --------------------------------------------------------------------------- #

def _author() -> str:
    return os.environ.get("DANUS_AUTHOR", "unknown")


def _role() -> str:
    # Fail-closed: an UNSET role gets the most-restrictive read-only set, same as
    # a mis-typed one (roles.tools_for). Dev use of the full set is explicit:
    # DANUS_ROLE=all.
    return os.environ.get("DANUS_ROLE", "verifier")


def _project(project: Optional[str] = None) -> Path:
    """Resolve the project dir to operate on.

    ``project`` (the main agent's per-call selector) wins: it names a project
    under ``DANUS_AGENTS_ROOT`` (``<root>/<project>``), so one session can touch
    several projects. With no ``project`` we fall back to ``DANUS_PROJECT_DIR``
    (a worker is always pinned this way). The name is validated to a single path
    segment — no ``/`` or ``..`` — so it can never escape the agents root."""
    agents_root = os.environ.get("DANUS_AGENTS_ROOT") or str(
        Path.cwd() / "runtime" / "projects"
    )
    project_dir = os.environ.get("DANUS_PROJECT_DIR", "")
    if project:
        if not _PROJECT_NAME_RE.match(project):
            raise RuntimeError(f"invalid project name: {project!r}")
        pdir = Path(agents_root) / project
        if not pdir.is_dir():
            raise RuntimeError(f"no such project: {project!r} (under {agents_root})")
        return pdir
    if not project_dir:
        raise RuntimeError("DANUS_PROJECT_DIR is not set and no project was given")
    return Path(project_dir)


def _gm(project: Optional[str] = None) -> GlobalMemory:
    return GlobalMemory(_project(project))


def _fg(project: Optional[str] = None) -> FactGraph:
    return FactGraph(_project(project))


def _verify(statement: str, proof: str) -> Dict[str, Any]:
    """POST {statement, proof} to the verify service; return its JSON."""
    verify_url = os.environ.get("DANUS_VERIFY_URL", "")
    if not verify_url:
        raise RuntimeError("DANUS_VERIFY_URL is not set (verify service not wired yet)")
    try:
        timeout = int(os.environ.get("DANUS_VERIFY_TIMEOUT", "3600"))
    except ValueError:
        timeout = 3600
    data = json.dumps({"statement": statement, "proof": proof}).encode("utf-8")
    req = urllib.request.Request(
        verify_url, data=data, headers={"Content-Type": "application/json"}
    )
    # The verifier is a loopback service.  Bypass host proxy variables so a
    # machine-wide HTTP_PROXY cannot redirect local proof material elsewhere.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:  # noqa: S310 (trusted local URL)
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# global memory                                                               #
# --------------------------------------------------------------------------- #

def gm_add(
    kind: str,
    claim: str,
    evidence: str = "",
    verifiable: Optional[bool] = None,
    glossary: Optional[Dict[str, str]] = None,
    links: Optional[Dict[str, Any]] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish a finding to shared global memory (claim + evidence). Verifiable
    kinds (conclusion/example/counterexample/proof_attempt) require explicit
    evidence; judgments (plan/direction/obstacle/master_guidance/elaboration) do
    not. Define your symbols in ``glossary`` and reuse project terminology.

    Main agent: pass ``project`` to target one of several projects by name;
    workers omit it (pinned to their own project)."""
    project_dir = _project(project)
    control = ControlStore(project_dir)
    if control.enabled and kind == "master_guidance":
        links = links or {}
        missing = [key for key in ("target_version", "route_id", "obligation_id") if not links.get(key)]
        if missing:
            raise ControlError(f"v2 master_guidance missing scope links: {', '.join(missing)}")
        route = control.route(str(links["route_id"]))
        if (links["target_version"], links["obligation_id"]) != (route["target_version"], route["obligation_id"]):
            raise ControlError("master_guidance scope does not match its route")
    entry_id = GlobalMemory(project_dir).append(
        kind, claim=claim, evidence=evidence, author=_author(),
        verifiable=verifiable, glossary=glossary, links=links,
    )
    return {"id": entry_id, "kind": kind}


def gm_search(query: str, kinds: Optional[List[str]] = None, limit_per_kind: int = 10,
              project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 over shared global-memory findings. Use to reuse others' results,
    avoid duplicate work, and learn which paths already died. Main agent: pass
    ``project`` to search a specific project; workers omit it."""
    return _gm(project).search(query, kinds=kinds, limit_per_kind=limit_per_kind)


# --------------------------------------------------------------------------- #
# fact graph                                                                  #
# --------------------------------------------------------------------------- #

def _close_v2_obligation(
    control: ControlStore, *, statement: str, fact_id: str, obligation_id: str,
    assignment_epoch: str, claim_role: str, undefined: List[str], requested: bool,
) -> Dict[str, Any]:
    if not requested:
        return {"requested": False, "closed": False}
    obligation = control.obligation(obligation_id)
    reasons = []
    if claim_role != "unconditional":
        reasons.append("only an unconditional claim can close an obligation")
    if " ".join(statement.split()) != " ".join(obligation["statement"].split()):
        reasons.append("fact statement does not exactly match the obligation")
    if undefined:
        reasons.append(f"unbound symbols remain: {', '.join(undefined)}")
    if control.fact_tainted(fact_id):
        reasons.append("fact is tainted pending review")
    if not control.dependencies_closed(obligation_id):
        reasons.append("predecessor obligations are not closed")
    if reasons:
        control.append_event(
            "obligation_closure_rejected", obligation_id=obligation_id,
            target_version=obligation["target_version"], fact_id=fact_id,
            assignment_epoch=assignment_epoch, reasons=reasons,
        )
        return {"requested": True, "closed": False, "reasons": reasons}
    control.set_obligation_state(
        obligation_id, "closed", actor="fact_submit", fact_id=fact_id,
        assignment_epoch=assignment_epoch,
    )
    return {"requested": True, "closed": True, "fact_id": fact_id}

def fact_submit(
    statement: str,
    proof: str,
    display_title: str = "",
    predecessors: Optional[List[str]] = None,
    glossary_introduces: Optional[Dict[str, str]] = None,
    intuition: str = "",
    source_id: Optional[str] = None,
    external_refs: Optional[List[Dict[str, Any]]] = None,
    target_version: Optional[str] = None,
    obligation_id: Optional[str] = None,
    route_id: Optional[str] = None,
    assignment_epoch: Optional[str] = None,
    claim_role: Optional[str] = None,
    assumptions_used: Optional[List[str]] = None,
    closes_obligation: bool = False,
) -> Dict[str, Any]:
    """The only way to write a fact. Runs the glossary-coverage check, calls the
    verifier, and writes the node IFF accepted. On reject, returns repair hints
    and writes nothing. Cite the returned ``fact_id`` in downstream proofs.

    Once a verdict exists, the verification outcome is **always** recorded to
    global memory (kind ``verification``) — accept, reject, or accept-but-write-
    failed — so a verdict is never stored by nobody (the verifier is stateless;
    this worker tool persists it). ``source_id`` optionally links to the
    global-memory finding being promoted.

    When your proof cites an external (published) result, pass it in
    ``external_refs`` as a structured entry — e.g.
    ``{"key": "HL26", "authors": ["Han", "Liu"], "title": "...",
    "arxiv": "2603.03817", "year": 2026, "cited_for": "Theorem 1.2"}`` (ground it
    with ``search_arxiv_theorems``). This is captured on the fact so the paper
    pipeline can cite it without re-deriving; it is mutable metadata and does not
    affect the ``fact_id``."""
    started = time.monotonic()
    project_dir = _project()
    fg = _fg()
    gm = _gm()
    problem_id = os.environ.get("DANUS_PROBLEM_ID", Path(_project()).name)
    control = ControlStore(project_dir)
    if control.enabled:
        required = {
            "target_version": target_version, "obligation_id": obligation_id,
            "route_id": route_id, "assignment_epoch": assignment_epoch,
            "claim_role": claim_role,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            return {"accepted": False, "verdict": "control_error",
                    "error": f"Danus v2 fact_submit missing: {', '.join(missing)}"}
        if claim_role not in {"unconditional", "conditional", "counterexample", "literature_import"}:
            return {"accepted": False, "verdict": "control_error",
                    "error": f"invalid claim_role: {claim_role}"}
        try:
            control.validate_submission(
                _author(), target_version=target_version or "",
                obligation_id=obligation_id or "", route_id=route_id or "",
                assignment_epoch=assignment_epoch or "",
                assumptions_used=assumptions_used or [],
            )
        except ControlError as exc:
            return {"accepted": False, "verdict": "control_error", "error": str(exc)}
        clean_title = " ".join(display_title.split())
        if "\n" in display_title or "\r" in display_title or not 4 <= len(clean_title) <= 80:
            return {"accepted": False, "verdict": "control_error",
                    "error": "Danus v2 display_title must be one line and 4-80 characters"}
        tainted_predecessors = [fid for fid in (predecessors or []) if control.fact_tainted(fid)]
        if tainted_predecessors:
            return {"accepted": False, "verdict": "control_error",
                    "error": f"submission depends on tainted facts: {', '.join(tainted_predecessors)}"}

    # glossary coverage is advisory — never let a heuristic bug block submission
    try:
        undefined = fg.undefined_symbols(
            statement=statement, proof=proof, intuition=intuition,
            predecessors=predecessors, glossary_introduces=glossary_introduces,
        )
    except Exception:
        undefined = []

    reused_fact_id = control.reusable_fact(statement, assumptions_used or []) if control.enabled else None
    if reused_fact_id:
        control.prepare_fact(reused_fact_id, {"reused": True, "scope": {
            "worker": _author(), "assignment_epoch": assignment_epoch,
            "target_version": target_version, "obligation_id": obligation_id,
            "route_id": route_id, "claim_role": claim_role,
            "assumptions_used": assumptions_used or [],
        }})
        control.finalize_fact(reused_fact_id)
        closure = _close_v2_obligation(
            control, statement=statement, fact_id=reused_fact_id,
            obligation_id=obligation_id or "", assignment_epoch=assignment_epoch or "",
            claim_role=claim_role or "", undefined=undefined,
            requested=closes_obligation,
        )
        control.record_cost(
            component="verification", wall_seconds=time.monotonic() - started,
            worker=_author(), target_version=target_version, obligation_id=obligation_id,
            route_id=route_id, assignment_epoch=assignment_epoch,
            usage={}, cost_usd=0.0,
        )
        return {"accepted": True, "fact_id": reused_fact_id, "reused": True,
                "closure": closure, "undefined_symbols": undefined}

    # 1) Verify. If the verify service errors, no verdict exists yet: return a
    #    clean error so the worker retries. Nothing is lost.
    try:
        result = _verify(statement, proof)
    except Exception as e:
        if control.enabled:
            control.record_cost(
                component="verification", wall_seconds=time.monotonic() - started,
                worker=_author(), target_version=target_version, obligation_id=obligation_id,
                route_id=route_id, assignment_epoch=assignment_epoch,
            )
        return {"accepted": False, "verdict": "error", "error": str(e),
                "undefined_symbols": undefined}
    # A successful call that returned a non-dict body (e.g. a bare list) would make
    # the .get() below throw uncaught; treat it as a verify error (clean retry
    # envelope, no verdict to store) rather than leaking a stack trace to the worker.
    if not isinstance(result, dict):
        if control.enabled:
            control.record_cost(
                component="verification", wall_seconds=time.monotonic() - started,
                worker=_author(), target_version=target_version, obligation_id=obligation_id,
                route_id=route_id, assignment_epoch=assignment_epoch,
            )
        return {"accepted": False, "verdict": "error",
                "error": f"verify service returned a non-dict body ({type(result).__name__})",
                "undefined_symbols": undefined}
    verdict = result.get("verdict")
    accepted = verdict == "correct"

    # 2) Write the fact iff accepted. Catch write failures (e.g. a revoked
    #    predecessor) so they do NOT skip the trace below.
    fact_id = None
    write_error = None
    if accepted:
        try:
            if control.enabled:
                pending_id = compute_fact_id(
                    problem_id=problem_id, predecessors=predecessors or [],
                    glossary_introduces=glossary_introduces or {},
                    statement=statement, proof=proof,
                )
                control.prepare_fact(pending_id, {"reused": False, "scope": {
                    "worker": _author(), "assignment_epoch": assignment_epoch,
                    "target_version": target_version, "obligation_id": obligation_id,
                    "route_id": route_id, "claim_role": claim_role,
                    "assumptions_used": assumptions_used or [],
                }})
            fact_id = fg.add(
                problem_id=problem_id, author=_author(), statement=statement, proof=proof,
                display_title=display_title,
                predecessors=predecessors, glossary_introduces=glossary_introduces,
                intuition=intuition, external_refs=external_refs,
            )
            if control.enabled:
                control.finalize_fact(fact_id)
        except Exception as e:
            write_error = str(e)

    # 3) ALWAYS record the verification outcome to global memory once a verdict exists.
    gm.append(
        "verification",
        claim=statement,
        evidence="verdict: correct" if accepted else (result.get("repair_hints") or "verdict: wrong"),
        author=_author(),
        verifiable=False,
        links={"source_id": source_id, "predecessors": predecessors or []},
        verdict=verdict,
        fact_id=fact_id,
        write_error=write_error,
        verification_report=result.get("verification_report"),
    )

    closure = None
    if control.enabled:
        control.record_cost(
            component="verification", wall_seconds=time.monotonic() - started,
            worker=_author(), target_version=target_version, obligation_id=obligation_id,
            route_id=route_id, assignment_epoch=assignment_epoch,
            usage=(result.get("usage") if isinstance(result.get("usage"), dict) else {}),
            cost_usd=result.get("cost_usd"),
        )
        if accepted and fact_id:
            closure = _close_v2_obligation(
                control, statement=statement, fact_id=fact_id,
                obligation_id=obligation_id or "", assignment_epoch=assignment_epoch or "",
                claim_role=claim_role or "", undefined=undefined,
                requested=closes_obligation,
            )

    # 4) Return.
    if not accepted:
        return {
            "accepted": False,
            "verdict": verdict,
            "repair_hints": result.get("repair_hints"),
            "verification_report": result.get("verification_report"),
            "undefined_symbols": undefined,
        }
    if write_error:
        return {"accepted": True, "fact_id": None, "write_error": write_error,
                "undefined_symbols": undefined}
    return {"accepted": True, "fact_id": fact_id, "closure": closure,
            "undefined_symbols": undefined}


def fact_search(query: str, limit: int = 10, project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 search over the verified fact graph (statement + proof + glossary),
    the derived fact index rebuilt on demand from the fact files — the fact graph
    stays the single source of truth. Use it **before proving** to check whether a
    fact like yours already exists, and to find the verified facts that bear on
    your subgoal so you can cite their ``fact_id``. Returns ranked ``{fact_id,
    score, statement}``. Main agent: pass ``project`` to search a specific
    project's graph; workers omit it."""
    project_dir = _project(project)
    results = ResearchQuery(project_dir).fact_search(query, limit=limit) if ControlStore(project_dir).enabled else _fg(project).search(query, limit=limit)
    return {"query": query, "results": results}


def research_map(target_version: Optional[str] = None, project: Optional[str] = None) -> Dict[str, Any]:
    """Return the shared target/method/route/obligation research map."""
    return ResearchQuery(_project(project)).research_map(target_version)


def route_context(route_id: str, snapshot: Optional[int] = None, project: Optional[str] = None) -> Dict[str, Any]:
    """Return one route's deterministic fact group, progress, and obstacles."""
    return ResearchQuery(_project(project)).route_context(route_id, snapshot=snapshot)


def obligation_context(obligation_id: str, snapshot: Optional[int] = None, project: Optional[str] = None) -> Dict[str, Any]:
    """Return an obligation, dependencies, routes, and proof-support group."""
    return ResearchQuery(_project(project)).obligation_context(obligation_id, snapshot=snapshot)


def fact_get(fact_id: str, include_proof: bool = False, project: Optional[str] = None) -> Dict[str, Any]:
    """Read an indexed fact; proof text is opt-in."""
    return ResearchQuery(_project(project)).fact_get(fact_id, include_proof=include_proof)


def fact_neighborhood(fact_id: str, direction: str = "both", depth: int = 1, limit: int = 300, project: Optional[str] = None) -> Dict[str, Any]:
    """Read a bounded local fact DAG (never more than 300 nodes)."""
    return ResearchQuery(_project(project)).fact_neighborhood(fact_id, direction=direction, depth=depth, limit=limit)


def target_proof_manifest(target_version: Optional[str] = None, project: Optional[str] = None) -> Dict[str, Any]:
    """Return the root closing facts and their full topological predecessor closure."""
    return ResearchQuery(_project(project)).target_proof_manifest(target_version)


def fact_revoke(fact_id: str, reason: str, project: Optional[str] = None) -> Dict[str, Any]:
    """Cascade-revoke a wrong fact and everything that depends on it. Destructive;
    operator / main-agent only. Main agent: pass ``project`` to target the project
    that owns the fact."""
    project_dir = _project(project)
    control = ControlStore(project_dir)
    if control.enabled:
        return {"tainted_pending_review": control.taint_fact(fact_id, reason), "revoked": []}
    revoked = _fg(project).revoke(fact_id, reason=reason)
    return {"revoked": revoked}


# --------------------------------------------------------------------------- #
# arXiv theorem search (external integration)                                 #
# --------------------------------------------------------------------------- #

def search_arxiv_theorems(query: str, num_results: int = 10) -> Dict[str, Any]:
    """Semantic search over arXiv theorem statements (Matlas). Returns
    **verbatim, as-published** theorem / lemma / definition statements — statement
    fidelity matters for math reasoning and citation checking. Phrase the query as
    a *complete mathematical statement* when possible. Returns ranked results,
    each with ``title``, the full ``theorem`` text, ``arxiv_id``, and the in-paper
    ``theorem_id``. External HTTP, no auth; on outage returns an ``error`` and
    empty ``results`` (retry / fall back to built-in web search)."""
    return _arxiv_search(query, num_results=num_results)


# --------------------------------------------------------------------------- #
# role-based registration                                                     #
# --------------------------------------------------------------------------- #

_TOOLS = {
    "gm_add": gm_add,
    "gm_search": gm_search,
    "fact_submit": fact_submit,
    "fact_search": fact_search,
    "fact_revoke": fact_revoke,
    "research_map": research_map,
    "route_context": route_context,
    "obligation_context": obligation_context,
    "fact_get": fact_get,
    "fact_neighborhood": fact_neighborhood,
    "target_proof_manifest": target_proof_manifest,
    "search_arxiv_theorems": search_arxiv_theorems,
}


def build_app(role: Optional[str] = None) -> FastMCP:
    """Build the stdio MCP app exposing exactly the tools ``role`` may use.
    ``role`` defaults to ``DANUS_ROLE`` (env); unset falls back to the read-only
    verifier set (fail-closed)."""
    app = FastMCP("danus-core")
    for name in tools_for(role if role is not None else _role()):
        app.tool(name=name)(_TOOLS[name])
    return app
