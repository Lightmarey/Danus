"""danus.write_paper — the write-paper MCP service.

Wraps the three paper roles (writer / reviser / auditor) behind role-gated MCP
tools so the main agent invokes them with structured args: the large bytes
(style guide + fact-graph content) are assembled inside the tool and never enter
the main agent's context, and each role's codex runs isolated by construction.

See ``assemble.build_prompt`` (the deterministic per-role assembler),
``danus.authoring.driver.run_codex`` (the shared one-shot codex driver), and
``server.build_app`` (the FastMCP app: paper_write / reference_audit / paper_revise).
Run as ``python -m danus.write_paper``.
"""

from __future__ import annotations

from danus.authoring.driver import run_codex

from .assemble import build_prompt


def build_app(*args, **kwargs):
    """Import the optional MCP dependency only when the server is requested."""
    from .server import build_app as _build_app

    return _build_app(*args, **kwargs)

__all__ = ["build_prompt", "run_codex", "build_app"]
