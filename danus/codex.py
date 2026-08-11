"""The single shared codex launcher — one uniform CALL + env contract.

Every place Danus execs codex (the proving workers in ``danus.execution.loop``,
the verify service in ``danus.verify.launcher``, and the one-shot artifact
renderers in ``danus.authoring.driver``) resolves the codex binary, the model,
the reasoning effort, the subprocess environment, and the ``exec`` command prefix
**through this module** — so the four are uniform across the three sites and there
is exactly one place to change any of them.

All config is read at CALL time (never import time), so services stay
testable/reconfigurable.

Env contract (neutral defaults + back-compat aliases):
  DANUS_CODEX_BIN     codex binary; back-compat alias: CODEX_BIN
  DANUS_CODEX_MODEL   neutral default model (default "gpt-5.5")
  DANUS_CODEX_EFFORT  neutral default reasoning effort (default "xhigh")

Each site layers its own per-service override env names on top of the neutral
defaults via ``model(*overrides)`` / ``effort(*overrides)`` (e.g. the verify
service passes ``DANUS_VERIFY_MODEL``; the renderers pass
``DANUS_WRITE_PAPER_MODEL`` / ``DANUS_HUMAN_SUMMARY_MODEL``).
"""

from __future__ import annotations

import os
import hashlib
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

# danus/codex.py → repo root is one parent up; the deployment's bin/codex wrapper
# lives at <repo>/bin/codex.
_REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MODEL = "gpt-5.5"
DEFAULT_EFFORT = "xhigh"

_FAILURE_PATTERNS = (
    ("quota_exhausted", False, ("insufficient_quota", "quota exceeded", "credit balance", "billing hard limit", "usage limit reached")),
    ("rate_limited", True, ("rate limit", "too many requests", "status 429", "http 429")),
    ("auth_or_config", False, ("unauthorized", "invalid api key", "authentication", "status 401", "http 401", "status 403", "http 403")),
    ("transient_network", True, ("connection reset", "connection refused", "connection error", "network is unreachable", "name resolution", "timed out connecting", "dns")),
    ("provider_5xx", True, ("status 500", "http 500", "status 502", "http 502", "status 503", "http 503", "status 504", "http 504")),
    ("invalid_request", False, ("invalid request", "bad request", "status 400", "http 400", "model not found")),
)


def classify_failure(return_code: int, log_path: Optional[Path] = None, *, text: str = "") -> Dict[str, Any]:
    """Classify a failed Codex subprocess without storing its potentially sensitive log."""
    if return_code == 0:
        return {"failure_class": "success", "retryable": False, "retry_after_seconds": 0, "error_signature": ""}
    if return_code == 124:
        failure_class, retryable = "timeout", True
    elif return_code == 127:
        failure_class, retryable, text = "auth_or_config", False, text or "codex binary not found"
    else:
        if not text:
            try:
                text = (log_path.read_text(encoding="utf-8", errors="replace")[-200_000:] if log_path else "")
            except OSError:
                text = ""
        lowered = text.lower()
        failure_class, retryable = "unknown_infra", True
        for category, can_retry, needles in _FAILURE_PATTERNS:
            if any(needle in lowered for needle in needles):
                failure_class, retryable = category, can_retry
                break
    retry_after = 0
    match = re.search(r"retry[-_ ]after[^0-9]{0,20}(\d+)", text.lower())
    if match:
        retry_after = int(match.group(1))
    signature_input = f"{failure_class}:{return_code}:" + " ".join(text.lower().split())[-2000:]
    return {
        "failure_class": failure_class,
        "retryable": retryable,
        "retry_after_seconds": retry_after,
        "error_signature": hashlib.sha256(signature_input.encode()).hexdigest()[:16],
        "return_code": return_code,
    }


def _repo_wrapper() -> Path:
    return _REPO_ROOT / "bin" / "codex"


def _usable_repo_wrapper(wrapper: Path) -> bool:
    if not wrapper.exists():
        return False
    if os.name == "nt":
        return wrapper.suffix.lower() in {".exe", ".cmd", ".bat", ".com", ".ps1"}
    return True


def resolve_bin() -> str:
    """Resolve the codex binary at CALL time. Precedence:
      1. ``DANUS_CODEX_BIN`` env,
      2. ``<repo>/bin/codex`` wrapper if it exists,
      3. ``shutil.which("codex")``,
      4. the bare string ``"codex"`` (so a missing binary raises a clear
         FileNotFoundError at exec time, not import time).
    """
    override = os.environ.get("DANUS_CODEX_BIN")
    if override:
        # An absolute override is used as-is; a bare/relative name is resolved to
        # its absolute path via PATH so subprocess_env can prepend its dir for the
        # codex ``#!/usr/bin/env node`` shebang. Fall back to the raw override if
        # it is not on PATH (exec then surfaces a clear FileNotFoundError).
        if os.path.isabs(override):
            return override
        return shutil.which(override) or override
    wrapper = _repo_wrapper()
    if _usable_repo_wrapper(wrapper):
        return str(wrapper)
    which = shutil.which("codex")
    if which:
        return which
    return "codex"


def model(*override_env_names: str, default: str = DEFAULT_MODEL) -> str:
    """The codex model: first non-empty among the given per-service override env
    vars (in order), then the neutral ``DANUS_CODEX_MODEL``, then ``default``."""
    for name in override_env_names:
        val = os.environ.get(name)
        if val:
            return val
    return os.environ.get("DANUS_CODEX_MODEL") or default


def effort(*override_env_names: str, default: str = DEFAULT_EFFORT) -> str:
    """The reasoning effort: first non-empty among the given per-service override
    env vars (in order), then the neutral ``DANUS_CODEX_EFFORT``, then
    ``default``."""
    for name in override_env_names:
        val = os.environ.get(name)
        if val:
            return val
    return os.environ.get("DANUS_CODEX_EFFORT") or default


def subprocess_env(codex_bin: str) -> Dict[str, str]:
    """A copy of ``os.environ`` with the codex binary's DIR prepended to ``PATH``
    so its ``#!/usr/bin/env node`` shebang resolves regardless of how the caller
    was launched.

    Only augments PATH when ``codex_bin`` has a directory component (a concrete
    path); the bare ``"codex"`` fallback must NOT inject the CWD into the
    subprocess PATH.
    """
    env = os.environ.copy()
    if os.path.dirname(codex_bin):
        codex_dir = os.path.dirname(os.path.abspath(codex_bin))
        if codex_dir and codex_dir != ".":
            existing = env.get("PATH", "")
            parts = existing.split(os.pathsep) if existing else []
            if codex_dir not in parts:
                env["PATH"] = codex_dir + (os.pathsep + existing if existing else "")
    return env


def exec_cmd(codex_bin: str, model: str, effort: str, *tail: str) -> List[str]:
    """The uniform ``codex exec`` command prefix + the caller's exact tail.

    Standardizes on the QUOTED reasoning-effort config form
    (``model_reasoning_effort="<effort>"``). The ``*tail`` is passed through
    verbatim (each site keeps its own exact tail: sandbox flags, ``-C`` home,
    MCP ``-c`` injection, output path, the ``-`` stdin sentinel, the prompt, …).
    """
    return [
        codex_bin, "exec",
        "--model", model,
        "--config", f'model_reasoning_effort="{effort}"',
        *tail,
    ]
