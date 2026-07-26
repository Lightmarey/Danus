"""Shared fixtures for the danus.write_paper offline tests.

The read-only source fixture is the shipped example project
``.agents/skills/write-paper/examples/paper/project/``. Anything that WRITES
copies it to a tempdir first. The reference ledger is seeded from the facts'
``external_refs`` (via ``seed_ledger``) so the writer/auditor prompts have a real
ledger to embed.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

from danus.write_paper import seed_ledger

_REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = _REPO_ROOT / ".agents" / "skills" / "write-paper"
EXAMPLE_PROJECT = SKILL_DIR / "examples" / "paper" / "project"

_MINIMAL_TEX = (
    "\\documentclass{amsart}\n"
    "\\begin{document}\n"
    "\\title{The Sum of the First $n$ Odd Numbers}\n"
    "\\author{A. Author}\n"
    "\\maketitle\n"
    "\\begin{thm}\\label{thm:main} $S(n)=n^2$. \\end{thm}\n"
    "\\begin{proof} By induction, see \\cite{Exm20}. \\end{proof}\n"
    "\\begin{thebibliography}{99}\n"
    "\\bibitem[AC24]{AC24} A. Author and B. Coauthor, A note on telescoping sums.\n"
    "\\bibitem[Exm20]{Exm20} C. Example, Elementary induction, revisited.\n"
    "\\end{thebibliography}\n"
    "\\end{document}\n"
)


def seed_ledger_text(project_dir: Path) -> str:
    return seed_ledger.render(seed_ledger.collect(Path(project_dir)))


def write_ledger(project_dir: Path) -> Path:
    ledger = Path(project_dir) / "paper" / "REFERENCE_LEDGER.md"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(seed_ledger_text(project_dir), encoding="utf-8")
    return ledger


def write_main_tex(project_dir: Path, tex: str = _MINIMAL_TEX) -> Path:
    p = Path(project_dir) / "paper" / "main.tex"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tex, encoding="utf-8")
    return p


@contextlib.contextmanager
def temp_project(with_ledger: bool = True, with_tex: bool = False):
    """Copy the example project to a tempdir; yield its path. Seeds the ledger by
    default; optionally writes a minimal main.tex."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="write-paper-test-") as d:
        dst = Path(d) / "project"
        shutil.copytree(EXAMPLE_PROJECT, dst)
        if with_ledger:
            write_ledger(dst)
        if with_tex:
            write_main_tex(dst)
        yield dst


@contextlib.contextmanager
def env(**kv):
    """Temporarily set env vars (None deletes), restore after."""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
