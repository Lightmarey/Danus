"""Danus verify service — the sole write-gate's HTTP front.

    POST /verify       {statement, proof} -> one verdict + usage
    POST /verify-batch {verification_goal, candidates} -> ordered verdicts + usage
    GET  /health                           -> {status: "ok", pid: <int>}

/verify runs the deterministic pre-checks (``prechecks.run_prechecks``) and, if
they pass, cold-starts a fresh codex verifier (``launcher.run_codex_verification``)
whose verdict the gateway's ``fact_submit`` uses to decide whether a claim becomes
a fact. The verifier is an LLM, NOT a formal proof assistant, with no human in the
loop by default — see the verifier contract (``agents/contracts/verifier.md``).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from danus import runtime
from danus.runtime import process_identity

from .launcher import (
    VERIFICATION_FILENAMES,
    _allocate_run_id,
    _results_dir,
    load_verification_result,
    run_codex_batch_verification,
    run_codex_verification,
)
from .prechecks import run_prechecks


class VerifyRequest(BaseModel):
    statement: str = Field(..., min_length=1)
    proof: str = Field(..., min_length=1)
    timeout_seconds: int | None = Field(default=None, gt=0)
    cancel_path: str | None = None
    request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class VerifyBatchCandidate(BaseModel):
    candidate_id: str = Field(..., min_length=1, max_length=64)
    statement: str = Field(..., min_length=1)
    proof: str = Field(..., min_length=1)


class VerifyBatchRequest(BaseModel):
    verification_goal: str = Field(..., min_length=4, max_length=500)
    candidates: List[VerifyBatchCandidate] = Field(..., min_length=1, max_length=6)
    timeout_seconds: int | None = Field(default=None, gt=0)
    cancel_path: str | None = None
    request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


app = FastAPI(title="Danus verify service", version="0.1.0")


def validate_verification_output(payload: Any) -> Dict[str, Any]:
    """Enforce the strict verifier JSON contract before a verdict reaches storage."""
    if not isinstance(payload, dict):
        raise ValueError("verification result must be an object")
    report = payload.get("verification_report")
    if not isinstance(report, dict) or not isinstance(report.get("summary"), str):
        raise ValueError("verification_report with a string summary is required")
    for key in ("critical_errors", "gaps"):
        findings = report.get(key)
        if not isinstance(findings, list):
            raise ValueError(f"verification_report.{key} must be a list")
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("location"), str) or not item["location"]
            or not isinstance(item.get("issue"), str) or not item["issue"]
            for item in findings
        ):
            raise ValueError(f"every verification_report.{key} item needs location and issue")
    verdict = payload.get("verdict")
    repair_hints = payload.get("repair_hints")
    clean = not report["critical_errors"] and not report["gaps"]
    if verdict not in {"correct", "wrong"} or (verdict == "correct") != clean:
        raise ValueError("verdict must be correct iff the report has no findings")
    if (
        not isinstance(repair_hints, str)
        or (verdict == "correct" and repair_hints != "")
        or (verdict == "wrong" and not repair_hints.strip())
    ):
        raise ValueError("repair_hints must be empty iff verdict is correct")
    return payload


def _validated(payload: Any) -> Dict[str, Any]:
    try:
        return validate_verification_output(payload)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"invalid verifier output: {exc}") from exc


def _idempotent_run(
    request_id: str, invoke: Callable[[str], Dict[str, Any]], *,
    timeout_seconds: int | None, cancel_path: str | None,
) -> Dict[str, Any]:
    """Run or replay one content-addressed verifier request.

    A disconnected client can retry the same request without cold-starting a
    second verifier.  The file lock also deduplicates concurrent retries.
    """
    run_id = f"request_{request_id}"
    run_dir = _results_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds + 15 if timeout_seconds else None
    while True:
        with runtime.file_lock(run_dir / ".request.lock") as lock:
            if lock is not None:
                try:
                    cached = load_verification_result(run_id)
                except HTTPException:
                    for filename in VERIFICATION_FILENAMES:
                        (run_dir / filename).unlink(missing_ok=True)
                    cached = None
                return cached if cached is not None else invoke(run_id)
        if cancel_path and Path(cancel_path).exists():
            raise HTTPException(status_code=499, detail="verification retry cancelled by operator stop")
        if deadline is not None and time.monotonic() >= deadline:
            raise HTTPException(status_code=504, detail="timed out waiting for the original verifier request")
        time.sleep(.1)


@app.get("/health")
async def health() -> Dict[str, Any]:
    # async on purpose: /health must not queue behind sync /verify threadpool
    # calls, so it responds in ~microseconds regardless of in-flight verifications.
    # `pid` self-identifies this instance: a health probe alone cannot tell OUR
    # verify from another deployment's verify holding the same port on a shared
    # host — callers match this pid against runtime/run/verify.pid to be sure.
    pid = os.getpid()
    return {"status": "ok", "pid": pid, "identity": process_identity(pid)}


@app.post("/verify")
def verify(request: VerifyRequest) -> Dict[str, Any]:
    rejected = run_prechecks(request.statement, request.proof)
    if rejected is not None:
        status_code, detail = rejected
        raise HTTPException(status_code=status_code, detail=detail)
    kwargs = {
        key: value for key, value in {
            "timeout_seconds": request.timeout_seconds,
            "cancel_path": request.cancel_path,
        }.items() if value is not None
    }
    invoke = lambda run_id: run_codex_verification(
        run_id=run_id, statement=request.statement, proof=request.proof, **kwargs,
    )
    result = (
        _idempotent_run(
            request.request_id, invoke, timeout_seconds=request.timeout_seconds,
            cancel_path=request.cancel_path,
        )
        if request.request_id else invoke(_allocate_run_id(request.statement))
    )
    return _validated(result)


@app.post("/verify-batch")
def verify_batch(request: VerifyBatchRequest) -> Dict[str, Any]:
    ids = [candidate.candidate_id for candidate in request.candidates]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=422, detail="candidate_id values must be unique")
    prechecked: Dict[str, Dict[str, Any]] = {}
    eligible = []
    for candidate in request.candidates:
        rejected = run_prechecks(candidate.statement, candidate.proof)
        if rejected is not None:
            _, detail = rejected
            prechecked[candidate.candidate_id] = {
                "candidate_id": candidate.candidate_id,
                "verification_report": {
                    "summary": "Rejected by deterministic verifier precheck.",
                    "critical_errors": [{"location": "precheck", "issue": detail}],
                    "gaps": [],
                },
                "verdict": "wrong", "repair_hints": detail,
            }
        else:
            eligible.append(candidate.model_dump())
    kwargs = {
        key: value for key, value in {
            "timeout_seconds": request.timeout_seconds,
            "cancel_path": request.cancel_path,
        }.items() if value is not None
    }
    if eligible:
        invoke = lambda run_id: run_codex_batch_verification(
            run_id, request.verification_goal, eligible, **kwargs,
        )
        result = (
            _idempotent_run(
                request.request_id, invoke, timeout_seconds=request.timeout_seconds,
                cancel_path=request.cancel_path,
            )
            if request.request_id else invoke(_allocate_run_id(
                "\n".join(candidate.statement for candidate in request.candidates)
            ))
        )
        verifications = result.get("verifications")
        if not isinstance(verifications, list) or any(not isinstance(item, dict) for item in verifications):
            raise HTTPException(status_code=500, detail="batch verification output must contain a verifications array")
        eligible_ids = [item["candidate_id"] for item in eligible]
        if [item.get("candidate_id") for item in verifications] != eligible_ids:
            raise HTTPException(status_code=500, detail="batch verification candidate_ids must match eligible input order exactly")
        for item in verifications:
            _validated(item)
        checked = {item["candidate_id"]: item for item in verifications}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    else:
        checked, usage = {}, {}
    return {
        "verifications": [prechecked.get(candidate_id) or checked[candidate_id] for candidate_id in ids],
        "usage": usage,
    }
