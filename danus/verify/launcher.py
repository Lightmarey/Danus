"""Cold-start codex launcher for the verify service.

Each /verify spawns a fresh ``codex exec`` session (the verify agent), driven by
AGENT_HOME/AGENTS.md + the verify skills, which writes ``verification.json`` to
the run dir. Stateless. The injected MCP server is ``python -m danus.gateway``
(installed package, role=verifier); the codex binary + model/effort are resolved
via the shared ``danus.codex`` launcher (config read at CALL time, so the service
is testable/reconfigurable).

Config (env):
  DANUS_CODEX_BIN,
  DANUS_VERIFY_MODEL (default gpt-5.5),
  DANUS_VERIFY_EFFORT (default xhigh),
  CODEX_TIMEOUT_SECONDS (0 = no timeout),
  VERIFY_AGENT_HOME (the codex `-C` dir: AGENTS.md + .agents/skills + .codex),
  VERIFIER_RESULTS_DIR (run dirs; gitignored).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from danus import codex
from danus import runtime
from danus import agent_assets

_HERE = Path(__file__).resolve().parent  # danus/verify/
VERIFICATION_FILENAMES = ("verification.json", "verificationt.json")


# --------------------------------------------------------------------------- #
# config resolution (env read at call time)                                   #
# --------------------------------------------------------------------------- #

def _agent_home() -> Path:
    return Path(os.getenv("VERIFY_AGENT_HOME", str(_HERE / "agent"))).resolve()


def ensure_agent_home() -> Path:
    """Provision the verifier's codex ``-C`` home if absent, then return it.

    Unlike a worker home (assembled per project by ``danus new``), the verify
    agent home is a singleton with no scaffolder — so a fresh checkout has none and
    the codex ``-C`` dir would not exist. Assets resolve from a source checkout
    first and from package data when installed from a wheel."""
    home = _agent_home()
    contract = agent_assets.contract("verifier")
    skills = agent_assets.skills("verifier")
    agents_md = home / "AGENTS.md"
    skills_link = home / ".agents" / "skills"
    (home / ".agents").mkdir(parents=True, exist_ok=True)
    runtime.sync_symlink_or_copy(contract, agents_md)
    runtime.sync_symlink_or_copy(skills, skills_link)
    return home



def _results_root() -> Path:
    return Path(os.getenv("VERIFIER_RESULTS_DIR", str(_HERE / "runs"))).resolve()


def _model() -> str:
    return codex.model("DANUS_VERIFY_MODEL")


def _effort() -> str:
    return codex.effort("DANUS_VERIFY_EFFORT")


def _timeout() -> Optional[int]:
    return int(os.getenv("CODEX_TIMEOUT_SECONDS", "0")) or None


def _mcp_config_arg() -> str:
    """Inject the danus gateway (role=verifier) into the codex agent via `-c`,
    independent of CODEX_HOME. Runs the current interpreter with ``-m
    danus.gateway``); the verifier role exposes only search_arxiv_theorems."""
    py = json.dumps(runtime.current_python())
    return f'mcp_servers.danus={{command={py},args=["-m","danus.gateway"],env={{DANUS_ROLE="verifier"}}}}'


# --------------------------------------------------------------------------- #
# run-dir allocation                                                          #
# --------------------------------------------------------------------------- #

def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def generate_run_id(statement: str) -> str:
    return f"{_utc_timestamp()}_{hashlib.sha256(statement.encode('utf-8')).hexdigest()[:12]}"


def _allocate_run_id(statement: str) -> str:
    """Claim a unique run dir atomically (mkdir exist_ok=False, retry with a
    numeric suffix) so concurrent verifiers sharing RESULTS_ROOT never clobber."""
    root = _results_root()
    root.mkdir(parents=True, exist_ok=True)
    base = generate_run_id(statement)
    run_id, suffix = base, 1
    for _ in range(10000):
        try:
            (root / run_id).mkdir(parents=False, exist_ok=False)
            return run_id
        except FileExistsError:
            suffix += 1
            run_id = f"{base}_{suffix}"
    raise RuntimeError(f"could not allocate a unique run_id under {root} for base={base}")


def _results_dir(run_id: str) -> Path:
    return _results_root() / run_id


def _verification_path(run_id: str) -> Optional[Path]:
    for filename in VERIFICATION_FILENAMES:
        path = _results_dir(run_id) / filename
        if path.exists():
            return path
    return None


def build_prompt(run_id: str, statement: str, proof: str) -> str:
    output_path = _results_dir(run_id) / VERIFICATION_FILENAMES[0]
    return (
        f"Run_id: {run_id}. "
        f"Statement: {statement}. "
        f"Proof:\n{proof}\n\n"
        "Use AGENTS.md to verify the above proof for the statement. "
        f"Write the verification JSON to this exact path: {output_path}."
    )


def build_codex_command(run_id: str, statement: str, proof: str) -> List[str]:
    return codex.exec_cmd(
        codex.resolve_bin(), _model(), _effort(),
        "-C", str(_agent_home()),
        # on an install without .git (tarball download), codex's
        # trusted-directory check refuses to run (exit 1 → /verify HTTP 500)
        "--skip-git-repo-check",
        "-c", _mcp_config_arg(),
        "--dangerously-bypass-approvals-and-sandbox",
        build_prompt(run_id=run_id, statement=statement, proof=proof),
    )


def run_codex_verification(run_id: str, statement: str, proof: str) -> Dict[str, Any]:
    """Spawn the cold-start codex verifier; read back + return the verification
    JSON. Raises HTTPException 504 (timeout) / 500 (nonzero exit, no output, or
    bad/non-dict JSON) — the callers translate these into the fact_submit
    verify-error path."""
    results_dir = _results_dir(run_id)
    results_dir.mkdir(parents=True, exist_ok=True)
    log_path = results_dir / "log.md"
    ensure_agent_home()  # provision the codex -C home on a fresh checkout (idempotent)
    cmd = build_codex_command(run_id=run_id, statement=statement, proof=proof)
    env = codex.subprocess_env(cmd[0])

    started_at = datetime.now(timezone.utc).isoformat()
    proc = None
    try:
        with log_path.open("w", encoding="utf-8") as log_handle:
            log_handle.write(f"started_at_utc: {started_at}\n")
            log_handle.write(f"command: {shlex.join(cmd)}\n\n")
            log_handle.flush()
            proc = runtime.spawn_process(
                cmd, cwd=_agent_home(), env=env,
                stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
                new_process_group=True,
            )
            completed_rc = proc.wait(timeout=_timeout())
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            runtime.stop_process(proc, wait_seconds=10.0, force=True)
        if not runtime.wait_until_path_releasable(log_path, timeout_seconds=10.0):
            raise HTTPException(
                status_code=500,
                detail=f"verifier log remained locked after timeout cleanup at {log_path}",
            ) from exc
        raise HTTPException(status_code=504,
                            detail=f"codex exec timed out after {exc.timeout}s. See log at {log_path}") from exc

    if completed_rc != 0:
        raise HTTPException(status_code=500,
                            detail=f"codex exec failed with exit code {completed_rc}. See log at {log_path}")

    verification_path = _verification_path(run_id)
    if verification_path is None:
        expected = results_dir / VERIFICATION_FILENAMES[0]
        raise HTTPException(status_code=500,
                            detail=f"verification output was not found at {expected}. See log at {log_path}")
    try:
        payload = json.loads(verification_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500,
                            detail=f"verification output at {verification_path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500,
                            detail=f"verification output at {verification_path} must be a JSON object")
    return payload
