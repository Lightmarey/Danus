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

import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, get_args

from danus._mcp import FastMCP
from danus.control import ControlError, ControlStore, require_v2_project
from danus.core import FactGraph, GlobalMemory
from danus.core.schema import compute_fact_id
from danus.integrations import search as _arxiv_search
from danus.research import ResearchQuery

from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ClaimRole = Literal["unconditional", "conditional", "counterexample", "literature_import"]
CLAIM_ROLES = get_args(ClaimRole)


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
        return require_v2_project(pdir)
    if not project_dir:
        raise RuntimeError("DANUS_PROJECT_DIR is not set and no project was given")
    return require_v2_project(Path(project_dir))


def _gm(project: Optional[str] = None) -> GlobalMemory:
    return GlobalMemory(_project(project))


def _fg(project: Optional[str] = None) -> FactGraph:
    return FactGraph(_project(project))


def _verify_timeout() -> int:
    try:
        timeout = max(1, int(os.environ.get("DANUS_VERIFY_TIMEOUT", "900")))
    except ValueError:
        timeout = 900
    parent_id = os.environ.get("DANUS_CALL_RESERVATION_ID")
    if parent_id:
        timeout = ControlStore(_project()).nested_call_timeout(parent_id, timeout)
    return timeout


def _verification_request_id(kind: str, content: Dict[str, Any]) -> str:
    project = os.environ.get("DANUS_PROJECT_DIR", "")
    material = {
        "kind": kind, "project": str(Path(project).resolve()) if project else "",
        "author": _author(), "content": content,
    }
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify(statement: str, proof: str) -> Dict[str, Any]:
    """POST {statement, proof} to the verify service; return its JSON."""
    verify_url = os.environ.get("DANUS_VERIFY_URL", "")
    if not verify_url:
        raise RuntimeError("DANUS_VERIFY_URL is not set (verify service not wired yet)")
    timeout = _verify_timeout()
    payload = {
        "statement": statement, "proof": proof, "timeout_seconds": timeout,
        "request_id": _verification_request_id(
            "single", {"statement": statement, "proof": proof},
        ),
    }
    if os.environ.get("DANUS_PROJECT_DIR"):
        payload["cancel_path"] = str(
            Path(os.environ["DANUS_PROJECT_DIR"]) / "workers" / _author() / ".stop"
        )
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        verify_url, data=data, headers={"Content-Type": "application/json"}
    )
    # The verifier is a loopback service.  Bypass host proxy variables so a
    # machine-wide HTTP_PROXY cannot redirect local proof material elsewhere.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout + 15) as resp:  # noqa: S310 (trusted local URL)
        return json.loads(resp.read().decode("utf-8"))


def _verify_batch(verification_goal: str, candidates: List[Dict[str, str]]) -> Dict[str, Any]:
    """Verify a bounded semantic theorem group in one cold verifier process."""
    verify_url = os.environ.get("DANUS_VERIFY_BATCH_URL", "")
    if not verify_url:
        single_url = os.environ.get("DANUS_VERIFY_URL", "")
        if not single_url:
            raise RuntimeError("DANUS_VERIFY_URL is not set (verify service not wired yet)")
        verify_url = single_url.removesuffix("/verify") + "/verify-batch"
    timeout = _verify_timeout()
    payload: Dict[str, Any] = {
        "verification_goal": verification_goal,
        "candidates": candidates,
        "timeout_seconds": timeout,
        "request_id": _verification_request_id(
            "batch", {"verification_goal": verification_goal, "candidates": candidates},
        ),
    }
    if os.environ.get("DANUS_PROJECT_DIR"):
        payload["cancel_path"] = str(
            Path(os.environ["DANUS_PROJECT_DIR"]) / "workers" / _author() / ".stop"
        )
    req = urllib.request.Request(
        verify_url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout + 15) as resp:  # noqa: S310 (trusted local URL)
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
    links = dict(links or {})
    if _role() == "worker" and links.get("verification_goal"):
        goal = " ".join(str(links["verification_goal"]).split())
        if not 4 <= len(goal) <= 500:
            raise ControlError("verification_goal must be one line and 4-500 characters")
        assignment = ControlStore(project_dir).validate_assignment(_author())
        scope = {
            "target_version": assignment["target_version"],
            "obligation_id": assignment["obligation_id"],
            "route_id": assignment["route_id"],
            "assignment_epoch": assignment["epoch"],
        }
        mismatched = [key for key, value in scope.items() if links.get(key) not in {None, value}]
        if mismatched:
            raise ControlError(f"staged verification scope mismatch: {', '.join(mismatched)}")
        links.update(scope, verification_goal=goal)
    control = ControlStore(project_dir)
    if kind == "master_guidance":
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
              project: Optional[str] = None, include_evidence: bool = False) -> Dict[str, Any]:
    """BM25 over shared global-memory findings. Use to reuse others' results,
    avoid duplicate work, and learn which paths already died. Main agent: pass
    ``project`` to search a specific project; workers omit it."""
    project_dir = _project(project)
    result = GlobalMemory(project_dir).search(query, kinds=kinds, limit_per_kind=limit_per_kind)
    # Workers use fact_get for deliberate expansion. Keep recall compact so a
    # broad memory search cannot silently consume another large model turn.
    compact: Dict[str, Any] = {"query": query, "results_by_kind": {}, "truncated": False}
    used = len(query)
    for kind, group in result["results_by_kind"].items():
        rows = []
        for hit in group["results"]:
            entry = hit["entry"]
            row = {
                "score": hit["score"],
                "entry": {
                    "id": entry.get("id"),
                    "kind": entry.get("kind"),
                    "claim": str(entry.get("claim") or "")[:600],
                    "status": entry.get("status"),
                    "fact_id": entry.get("fact_id"),
                    "links": entry.get("links") or {},
                },
            }
            if include_evidence:
                row["entry"]["evidence"] = str(entry.get("evidence") or "")[:1200]
            size = len(json.dumps(row, ensure_ascii=False))
            if used + size > 12000:
                compact["truncated"] = True
                break
            rows.append(row)
            used += size
        compact["results_by_kind"][kind] = {"count": len(rows), "results": rows}
        if compact["truncated"]:
            break
    return compact


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


def _persist_verification(
    *, result: Dict[str, Any], statement: str, proof: str, display_title: str,
    predecessors: Optional[List[str]], glossary_introduces: Optional[Dict[str, str]],
    intuition: str, source_id: Optional[str], external_refs: Optional[List[Dict[str, Any]]],
    target_version: str, obligation_id: str, route_id: str, assignment_epoch: str,
    claim_role: str, assumptions_used: Optional[List[str]], closes_obligation: bool,
    undefined: List[str], problem_id: str, control: ControlStore, fg: FactGraph,
    gm: GlobalMemory,
) -> Dict[str, Any]:
    """Persist one already-issued verifier verdict without starting a verifier."""
    verdict = result.get("verdict")
    accepted = verdict == "correct"
    binding_error = None
    if accepted:
        try:
            control.validate_submission(
                _author(), target_version=target_version,
                obligation_id=obligation_id, route_id=route_id,
                assignment_epoch=assignment_epoch,
                assumptions_used=assumptions_used or [],
                reservation_id=os.environ.get("DANUS_CALL_RESERVATION_ID"),
            )
        except ControlError as exc:
            binding_error = f"control state changed during verification: {exc}"

    fact_id = None
    write_error = None
    if accepted and not binding_error:
        try:
            pending_id = compute_fact_id(
                problem_id=problem_id, predecessors=predecessors or [],
                glossary_introduces=glossary_introduces or {},
                statement=statement, proof=proof,
            )
            submission_id = control.prepare_fact(pending_id, {"reused": False, "scope": {
                "worker": _author(), "assignment_epoch": assignment_epoch,
                "target_version": target_version, "obligation_id": obligation_id,
                "route_id": route_id, "claim_role": claim_role,
                "assumptions_used": assumptions_used or [],
            }})
            fact_id = fg.add(
                problem_id=problem_id, author=_author(), statement=statement, proof=proof,
                display_title=display_title, predecessors=predecessors,
                glossary_introduces=glossary_introduces, intuition=intuition,
                external_refs=external_refs,
            )
            control.finalize_fact(fact_id, submission_id)
        except Exception as exc:
            write_error = str(exc)

    gm.append(
        "verification", claim=statement,
        evidence="verdict: correct" if verdict == "correct" else (result.get("repair_hints") or "verdict: wrong"),
        author=_author(), verifiable=False,
        links={"source_id": source_id, "predecessors": predecessors or []},
        verdict=verdict, fact_id=fact_id, write_error=binding_error or write_error,
        verification_report=result.get("verification_report"),
    )
    if verdict == "wrong":
        verification_goal = ""
        if source_id:
            for kind in ("conclusion", "example", "counterexample", "proof_attempt"):
                source = next((entry for entry in gm.read(kind) if entry.get("id") == source_id), None)
                if source:
                    verification_goal = str((source.get("links") or {}).get("verification_goal") or "")
                    break
        gm.append(
            "obstacle",
            claim=f"Verifier rejected: {' '.join(display_title.split()) or statement[:80]}",
            evidence=json.dumps({
                "repair_hints": result.get("repair_hints") or "",
                "verification_report": result.get("verification_report") or {},
            }, ensure_ascii=False, sort_keys=True),
            author=_author(), verifiable=False,
            links={
                "source_id": source_id, "verification_goal": verification_goal,
                "target_version": target_version, "obligation_id": obligation_id,
                "route_id": route_id, "assignment_epoch": assignment_epoch,
            },
        )
    if source_id:
        gm.set_status(
            source_id,
            "verified" if fact_id else "refuted" if verdict == "wrong" else "unverified",
            fact_id=fact_id,
        )

    closure = None
    if accepted and fact_id:
        closure = _close_v2_obligation(
            control, statement=statement, fact_id=fact_id,
            obligation_id=obligation_id, assignment_epoch=assignment_epoch,
            claim_role=claim_role, undefined=undefined, requested=closes_obligation,
        )
    if binding_error:
        return {"accepted": False, "verifier_accepted": True, "verdict": verdict,
                "write_error": binding_error, "undefined_symbols": undefined}
    if not accepted:
        return {
            "accepted": False, "verdict": verdict,
            "repair_hints": result.get("repair_hints"),
            "verification_report": result.get("verification_report"),
            "undefined_symbols": undefined,
        }
    if write_error:
        return {"accepted": True, "fact_id": None, "write_error": write_error,
                "undefined_symbols": undefined}
    return {"accepted": True, "fact_id": fact_id, "closure": closure,
            "undefined_symbols": undefined}

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
    claim_role: Optional[ClaimRole] = None,
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
    required = {
        "target_version": target_version, "obligation_id": obligation_id,
        "route_id": route_id, "assignment_epoch": assignment_epoch,
        "claim_role": claim_role,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        return {"accepted": False, "verdict": "control_error",
                "error": f"fact_submit missing: {', '.join(missing)}"}
    if claim_role not in CLAIM_ROLES:
        return {"accepted": False, "verdict": "control_error",
                "error": f"invalid claim_role: {claim_role}"}
    try:
        control.validate_submission(
            _author(), target_version=target_version or "",
            obligation_id=obligation_id or "", route_id=route_id or "",
            assignment_epoch=assignment_epoch or "",
            assumptions_used=assumptions_used or [],
            reservation_id=os.environ.get("DANUS_CALL_RESERVATION_ID"),
        )
    except ControlError as exc:
        return {"accepted": False, "verdict": "control_error", "error": str(exc)}
    clean_title = " ".join(display_title.split())
    if "\n" in display_title or "\r" in display_title or not 4 <= len(clean_title) <= 80:
        return {"accepted": False, "verdict": "control_error",
                "error": "display_title must be one line and 4-80 characters"}
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

    reused_fact_id = control.reusable_fact(statement, assumptions_used or [])
    if reused_fact_id:
        submission_id = control.prepare_fact(reused_fact_id, {"reused": True, "scope": {
            "worker": _author(), "assignment_epoch": assignment_epoch,
            "target_version": target_version, "obligation_id": obligation_id,
            "route_id": route_id, "claim_role": claim_role,
            "assumptions_used": assumptions_used or [],
        }})
        control.finalize_fact(reused_fact_id, submission_id)
        if source_id:
            gm.set_status(source_id, "verified", fact_id=reused_fact_id)
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
    reservation = None
    try:
        parent_reservation_id = os.environ.get("DANUS_CALL_RESERVATION_ID")
        reservation = control.reserve_call(
            component="verification", max_wall_seconds=_verify_timeout(),
            parent_reservation_id=parent_reservation_id,
            worker=_author(), target_version=target_version,
            obligation_id=obligation_id, route_id=route_id,
            assignment_epoch=assignment_epoch,
        )
        gate = {"allowed": True} if parent_reservation_id else control.claim_backend_call("codex")
        if not gate["allowed"]:
            control.cancel_call_reservation(reservation["id"], reason="provider circuit is open")
            return {"accepted": False, "verdict": "error", "error": f"Codex provider circuit is {gate['state']}", "undefined_symbols": undefined}
    except ControlError as exc:
        message = str(exc)
        reason_code = (
            "wall_budget" if "wall budget" in message
            else "cost_budget" if "cost budget" in message
            else "control"
        )
        control.append_event(
            "call_reservation_rejected", component="verification",
            worker=_author(), target_version=target_version,
            obligation_id=obligation_id, route_id=route_id,
            assignment_epoch=assignment_epoch, reason_code=reason_code,
            reason=message,
        )
        return {"accepted": False, "verdict": "control_error", "error": str(exc), "undefined_symbols": undefined}
    try:
        if source_id:
            gm.set_status(source_id, "verifying")
        result = _verify(statement, proof)
    except Exception as e:
        if source_id:
            gm.set_status(source_id, "unverified")
        control.record_cost(
            component="verification", wall_seconds=time.monotonic() - started,
            worker=_author(), target_version=target_version, obligation_id=obligation_id,
            route_id=route_id, assignment_epoch=assignment_epoch,
            reservation_id=reservation["id"] if reservation else None,
            attempt_status="failed",
        )
        from danus import codex
        rc = 124 if "timed out" in str(e).lower() or "504" in str(e) else 1
        control.record_backend_failure(codex.classify_failure(rc, text=str(e)), provider_key="codex", actor="verification", wall_seconds=time.monotonic() - started)
        return {"accepted": False, "verdict": "error", "error": str(e),
                "undefined_symbols": undefined}
    # A successful call that returned a non-dict body (e.g. a bare list) would make
    # the .get() below throw uncaught; treat it as a verify error (clean retry
    # envelope, no verdict to store) rather than leaking a stack trace to the worker.
    if not isinstance(result, dict):
        if source_id:
            gm.set_status(source_id, "unverified")
        control.record_cost(
            component="verification", wall_seconds=time.monotonic() - started,
            worker=_author(), target_version=target_version, obligation_id=obligation_id,
            route_id=route_id, assignment_epoch=assignment_epoch,
            reservation_id=reservation["id"] if reservation else None,
            attempt_status="invalid_response",
        )
        control.record_backend_failure({"failure_class": "invalid_response", "retryable": False, "retry_after_seconds": 0, "error_signature": "invalid-response"}, provider_key="codex", actor="verification", wall_seconds=time.monotonic() - started)
        return {"accepted": False, "verdict": "error",
                "error": f"verify service returned a non-dict body ({type(result).__name__})",
                "undefined_symbols": undefined}
    response = _persist_verification(
        result=result, statement=statement, proof=proof, display_title=display_title,
        predecessors=predecessors, glossary_introduces=glossary_introduces,
        intuition=intuition, source_id=source_id, external_refs=external_refs,
        target_version=target_version or "", obligation_id=obligation_id or "",
        route_id=route_id or "", assignment_epoch=assignment_epoch or "",
        claim_role=claim_role or "", assumptions_used=assumptions_used,
        closes_obligation=closes_obligation, undefined=undefined,
        problem_id=problem_id, control=control, fg=fg, gm=gm,
    )
    control.record_cost(
        component="verification", wall_seconds=time.monotonic() - started,
        worker=_author(), target_version=target_version, obligation_id=obligation_id,
        route_id=route_id, assignment_epoch=assignment_epoch,
        usage=(result.get("usage") if isinstance(result.get("usage"), dict) else {}),
        cost_usd=result.get("cost_usd"),
        reservation_id=reservation["id"] if reservation else None,
        attempt_status="completed",
    )
    control.record_backend_success(provider_key="codex", actor="verification")
    return response


def fact_submit_batch(
    candidates: List[Dict[str, Any]], verification_goal: str,
) -> Dict[str, Any]:
    """Verify a durable semantic group of 1-6 independent candidate facts.

    Each candidate is ``{"source_id": ...}`` pointing to a verifiable global-memory
    entry staged with the same ``verification_goal`` and assignment scope. The
    source claim/evidence are authoritative, so an interrupted worker can resume
    without reconstructing its proof. The 1-6 bound is a safety cap, not a wait
    target. Candidates may cite existing signed facts, but not one another.
    """
    started = time.monotonic()
    verification_goal = " ".join(verification_goal.split())
    if not 4 <= len(verification_goal) <= 500:
        return {"accepted": False, "verdict": "control_error",
                "error": "verification_goal must be one line and 4-500 characters"}
    if not 1 <= len(candidates) <= 6:
        return {"accepted": False, "verdict": "control_error",
                "error": "fact_submit_batch requires 1-6 staged candidates"}

    project_dir = _project()
    control = ControlStore(project_dir)
    try:
        assignment = control.validate_assignment(_author())
    except ControlError as exc:
        return {"accepted": False, "verdict": "control_error", "error": str(exc)}
    target_version = assignment["target_version"]
    obligation_id = assignment["obligation_id"]
    route_id = assignment["route_id"]
    assignment_epoch = assignment["epoch"]
    fg, gm = _fg(), _gm()
    problem_id = os.environ.get("DANUS_PROBLEM_ID", project_dir.name)
    prepared: List[Dict[str, Any]] = []
    normalized: List[Dict[str, Any]] = []
    results: List[Optional[Dict[str, Any]]] = [None] * len(candidates)
    entries = {
        str(entry.get("id")): entry
        for kind in ("conclusion", "example", "counterexample", "proof_attempt")
        for entry in gm.read(kind)
        if entry.get("id")
    }
    recoverable_verdicts = {
        str((entry.get("links") or {}).get("source_id")): {
            "verdict": "correct", "repair_hints": "",
            "verification_report": entry.get("verification_report"),
        }
        for entry in gm.read("verification")
        if entry.get("verdict") == "correct"
        and entry.get("write_error")
        and (entry.get("links") or {}).get("source_id")
    }
    source_ids = [raw.get("source_id") if isinstance(raw, dict) else None for raw in candidates]
    if any(not isinstance(source_id, str) or not source_id for source_id in source_ids):
        return {"accepted": False, "verdict": "control_error",
                "error": "every batch candidate requires a staged source_id"}
    if len(set(source_ids)) != len(source_ids):
        return {"accepted": False, "verdict": "control_error",
                "error": "batch source_id values must be unique"}
    normalized_claims: Dict[str, str] = {}
    for source_id in source_ids:
        entry = entries.get(str(source_id)) or {}
        claim = " ".join(str(entry.get("claim") or "").split())
        prior = normalized_claims.get(claim) if claim else None
        if prior:
            return {
                "accepted": False, "verdict": "control_error",
                "error": (
                    "batch candidates must have distinct statements; "
                    f"source_ids {prior} and {source_id} are duplicates"
                ),
            }
        if claim:
            normalized_claims[claim] = str(source_id)
    if sum(bool((entries.get(str(source_id)) or {}).get("links", {}).get("closes_obligation")) for source_id in source_ids) > 1:
        return {"accepted": False, "verdict": "control_error",
                "error": "at most one batch candidate may close the obligation"}

    for index, raw in enumerate(candidates):
        source_id = str(raw["source_id"])
        entry = entries.get(source_id)
        if not entry:
            return {"accepted": False, "verdict": "control_error",
                    "error": f"candidate {index + 1} source_id is not a verifiable global-memory entry"}
        links = entry.get("links") or {}
        expected_scope = {
            "target_version": target_version, "obligation_id": obligation_id,
            "route_id": route_id, "assignment_epoch": assignment_epoch,
            "verification_goal": verification_goal,
        }
        if entry.get("author") != _author() or entry.get("status") not in {"unverified", "verifying"}:
            return {"accepted": False, "verdict": "control_error",
                    "error": f"candidate {index + 1} is not a pending draft owned by this worker"}
        mismatched = [key for key, value in expected_scope.items() if links.get(key) != value]
        if mismatched:
            return {"accepted": False, "verdict": "control_error",
                    "error": f"candidate {index + 1} staged scope mismatch: {', '.join(mismatched)}"}
        candidate = {
            "statement": entry.get("claim"), "proof": entry.get("evidence"),
            "display_title": links.get("display_title", ""),
            "predecessors": links.get("predecessors") or [],
            "glossary_introduces": entry.get("glossary") or {},
            "intuition": links.get("intuition", ""), "source_id": source_id,
            "external_refs": links.get("external_refs"),
            "claim_role": links.get("claim_role"),
            "assumptions_used": links.get("assumptions_used") or [],
            "closes_obligation": bool(links.get("closes_obligation", False)),
        }
        if not isinstance(candidate["statement"], str) or not candidate["statement"].strip():
            return {"accepted": False, "verdict": "control_error",
                    "error": f"candidate {index + 1} has no statement"}
        if not isinstance(candidate["proof"], str) or not candidate["proof"].strip():
            return {"accepted": False, "verdict": "control_error",
                    "error": f"candidate {index + 1} has no proof"}
        title = " ".join(str(candidate["display_title"]).split())
        if "\n" in str(candidate["display_title"]) or "\r" in str(candidate["display_title"]) or not 4 <= len(title) <= 80:
            return {"accepted": False, "verdict": "control_error",
                    "error": f"candidate {index + 1} display_title must be one line and 4-80 characters"}
        if candidate["claim_role"] not in CLAIM_ROLES:
            return {"accepted": False, "verdict": "control_error",
                    "error": f"candidate {index + 1} has invalid claim_role: {candidate['claim_role']}"}
        if not isinstance(candidate["predecessors"], list) or any(
            not isinstance(fid, str) or not fg.exists(fid) for fid in candidate["predecessors"]
        ):
            return {"accepted": False, "verdict": "control_error",
                    "error": f"candidate {index + 1} predecessors must be existing signed fact_ids"}
        tainted = [fid for fid in candidate["predecessors"] if control.fact_tainted(fid)]
        if tainted:
            return {"accepted": False, "verdict": "control_error",
                    "error": f"candidate {index + 1} depends on tainted facts: {', '.join(tainted)}"}
        try:
            control.validate_submission(
                _author(), target_version=target_version, obligation_id=obligation_id,
                route_id=route_id, assignment_epoch=assignment_epoch,
                assumptions_used=candidate["assumptions_used"],
                reservation_id=os.environ.get("DANUS_CALL_RESERVATION_ID"),
            )
        except ControlError as exc:
            return {"accepted": False, "verdict": "control_error",
                    "error": f"candidate {index + 1}: {exc}"}
        try:
            undefined = fg.undefined_symbols(
                statement=candidate["statement"], proof=candidate["proof"],
                intuition=candidate["intuition"], predecessors=candidate["predecessors"],
                glossary_introduces=candidate["glossary_introduces"],
            )
        except Exception:
            undefined = []
        candidate.update(index=index, undefined=undefined)
        normalized.append(candidate)

        reused = control.reusable_fact(candidate["statement"], candidate["assumptions_used"])
        if reused:
            submission_id = control.prepare_fact(reused, {"reused": True, "scope": {
                "worker": _author(), "assignment_epoch": assignment_epoch,
                "target_version": target_version, "obligation_id": obligation_id,
                "route_id": route_id, "claim_role": candidate["claim_role"],
                "assumptions_used": candidate["assumptions_used"],
            }})
            control.finalize_fact(reused, submission_id)
            gm.set_status(candidate["source_id"], "verified", fact_id=reused)
            results[index] = {"accepted": True, "fact_id": reused, "reused": True,
                              "closure": None, "undefined_symbols": undefined}
        else:
            recovered_verdict = recoverable_verdicts.get(candidate["source_id"])
            if recovered_verdict:
                results[index] = _persist_verification(
                    result=recovered_verdict, statement=candidate["statement"],
                    proof=candidate["proof"], display_title=candidate["display_title"],
                    predecessors=candidate["predecessors"],
                    glossary_introduces=candidate["glossary_introduces"],
                    intuition=candidate["intuition"], source_id=candidate["source_id"],
                    external_refs=candidate["external_refs"], target_version=target_version,
                    obligation_id=obligation_id, route_id=route_id,
                    assignment_epoch=assignment_epoch, claim_role=candidate["claim_role"],
                    assumptions_used=candidate["assumptions_used"], closes_obligation=False,
                    undefined=undefined, problem_id=problem_id, control=control, fg=fg, gm=gm,
                )
            else:
                prepared.append(candidate)

    def close_after_writes() -> None:
        for candidate in normalized:
            if not candidate["closes_obligation"]:
                continue
            item = results[candidate["index"]]
            if item and item.get("accepted") and item.get("fact_id"):
                item["closure"] = _close_v2_obligation(
                    control, statement=candidate["statement"], fact_id=item["fact_id"],
                    obligation_id=obligation_id, assignment_epoch=assignment_epoch,
                    claim_role=candidate["claim_role"], undefined=candidate["undefined"],
                    requested=True,
                )

    if not prepared:
        close_after_writes()
        control.record_cost(
            component="verification", wall_seconds=time.monotonic() - started,
            worker=_author(), target_version=target_version, obligation_id=obligation_id,
            route_id=route_id, assignment_epoch=assignment_epoch, batch_size=len(candidates),
            usage={}, cost_usd=0.0,
        )
        return {"accepted": all(bool(item and item.get("fact_id")) for item in results),
                "results": results, "verified_count": 0,
                "reused_count": sum(bool(item and item.get("reused")) for item in results),
                "recovered_write_count": sum(bool(item and not item.get("reused")) for item in results)}

    reservation = None
    try:
        parent_reservation_id = os.environ.get("DANUS_CALL_RESERVATION_ID")
        reservation = control.reserve_call(
            component="verification", max_wall_seconds=_verify_timeout(),
            parent_reservation_id=parent_reservation_id,
            worker=_author(), target_version=target_version, obligation_id=obligation_id,
            route_id=route_id, assignment_epoch=assignment_epoch, batch_size=len(prepared),
        )
        gate = {"allowed": True} if parent_reservation_id else control.claim_backend_call("codex")
        if not gate["allowed"]:
            control.cancel_call_reservation(reservation["id"], reason="provider circuit is open")
            return {"accepted": False, "verdict": "error",
                    "error": f"Codex provider circuit is {gate['state']}"}
    except ControlError as exc:
        return {"accepted": False, "verdict": "control_error", "error": str(exc)}

    try:
        for candidate in prepared:
            gm.set_status(candidate["source_id"], "verifying")
        payloads = [
            {"candidate_id": str(candidate["index"] + 1),
             "statement": candidate["statement"], "proof": candidate["proof"]}
            for candidate in prepared
        ]
        if len(payloads) == 1:
            raw_result = _verify(payloads[0]["statement"], payloads[0]["proof"])
            verdicts = [raw_result]
        else:
            raw_result = _verify_batch(verification_goal, payloads)
            verdicts = raw_result.get("verifications") if isinstance(raw_result, dict) else None
            if not isinstance(verdicts, list) or [item.get("candidate_id") for item in verdicts if isinstance(item, dict)] != [item["candidate_id"] for item in payloads]:
                raise RuntimeError("verify service returned an invalid batch result")
        if not isinstance(raw_result, dict) or any(not isinstance(item, dict) for item in verdicts):
            raise RuntimeError("verify service returned an invalid response")
    except Exception as exc:
        for candidate in prepared:
            gm.set_status(candidate["source_id"], "unverified")
        control.record_cost(
            component="verification", wall_seconds=time.monotonic() - started,
            worker=_author(), target_version=target_version, obligation_id=obligation_id,
            route_id=route_id, assignment_epoch=assignment_epoch, batch_size=len(prepared),
            reservation_id=reservation["id"] if reservation else None,
            attempt_status="failed",
        )
        from danus import codex
        rc = 124 if "timed out" in str(exc).lower() or "504" in str(exc) else 1
        control.record_backend_failure(
            codex.classify_failure(rc, text=str(exc)), provider_key="codex",
            actor="verification", wall_seconds=time.monotonic() - started,
        )
        return {"accepted": False, "verdict": "error", "error": str(exc)}

    for candidate, verdict in zip(prepared, verdicts):
        results[candidate["index"]] = _persist_verification(
            result=verdict, statement=candidate["statement"], proof=candidate["proof"],
            display_title=candidate["display_title"], predecessors=candidate["predecessors"],
            glossary_introduces=candidate["glossary_introduces"], intuition=candidate["intuition"],
            source_id=candidate["source_id"], external_refs=candidate["external_refs"],
            target_version=target_version, obligation_id=obligation_id, route_id=route_id,
            assignment_epoch=assignment_epoch, claim_role=candidate["claim_role"],
            assumptions_used=candidate["assumptions_used"],
            closes_obligation=False, undefined=candidate["undefined"],
            problem_id=problem_id, control=control, fg=fg, gm=gm,
        )
    close_after_writes()
    usage = raw_result.get("usage") if isinstance(raw_result.get("usage"), dict) else {}
    control.record_cost(
        component="verification", wall_seconds=time.monotonic() - started,
        worker=_author(), target_version=target_version, obligation_id=obligation_id,
        route_id=route_id, assignment_epoch=assignment_epoch, batch_size=len(prepared),
        usage=usage, cost_usd=raw_result.get("cost_usd"),
        reservation_id=reservation["id"] if reservation else None,
        attempt_status="completed",
    )
    control.record_backend_success(provider_key="codex", actor="verification")
    verifier_accepted_count = sum(bool(item and item.get("accepted")) for item in results)
    persisted_count = sum(bool(item and item.get("fact_id")) for item in results)
    return {"accepted": persisted_count == len(results), "results": results,
            "accepted_count": persisted_count,
            "verifier_accepted_count": verifier_accepted_count,
            "verified_count": len(prepared),
            "reused_count": sum(bool(item and item.get("reused")) for item in results),
            "recovered_write_count": len(candidates) - len(prepared)
            - sum(bool(item and item.get("reused")) for item in results),
            "usage": usage}


def fact_search(query: str, limit: int = 10, project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 search over the verified fact graph (statement + proof + glossary),
    the derived fact index rebuilt on demand from the fact files — the fact graph
    stays the single source of truth. Use it **before proving** to check whether a
    fact like yours already exists, and to find the verified facts that bear on
    your subgoal so you can cite their ``fact_id``. Main agent: pass ``project`` to search a specific
    project's graph; workers omit it. V2 returns a bounded snippet; use
    ``fact_get`` to expand a selected fact."""
    project_dir = _project(project)
    results = ResearchQuery(project_dir).fact_search(query, limit=limit)
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
    return {"tainted_pending_review": control.taint_fact(fact_id, reason), "revoked": []}


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
    "fact_submit_batch": fact_submit_batch,
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
