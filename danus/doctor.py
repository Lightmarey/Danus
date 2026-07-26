from __future__ import annotations

import importlib.util
import os
import shutil
from typing import Iterable, Tuple

from danus import codex, runtime
from danus.execution import layout as L


def _command_exists(command: str) -> bool:
    if not command:
        return False
    if shutil.which(command):
        return True
    path = command.strip('"')
    if not os.path.isabs(path):
        return False
    if not os.path.isfile(path):
        return False
    if runtime.is_windows():
        return os.path.splitext(path)[1].lower() in {".exe", ".cmd", ".bat", ".com", ".ps1"}
    return os.access(path, os.X_OK)


def checks() -> Iterable[Tuple[str, bool, str]]:
    py = runtime.current_python()
    yield ("python", bool(py), py or "no Python executable resolved")
    codex_bin = codex.resolve_bin()
    codex_ok = _command_exists(codex_bin)
    yield ("codex", codex_ok, codex_bin)
    try:
        contract = L.worker_md()
        yield ("worker_contract", contract.exists(), str(contract))
    except FileNotFoundError as exc:
        yield ("worker_contract", False, str(exc))
    try:
        skills = L.worker_skills_dir()
        yield ("worker_skills", skills.exists(), str(skills))
    except FileNotFoundError as exc:
        yield ("worker_skills", False, str(exc))
    for module in ("pytest", "mcp", "fastapi", "uvicorn", "pydantic"):
        yield (f"import:{module}", importlib.util.find_spec(module) is not None, module)


def main() -> int:
    runtime.configure_environment()
    failed = False
    for name, ok, detail in checks():
        status = "ok" if ok else "missing"
        print(f"{status:7} {name:<16} {detail}")
        failed = failed or not ok
    return 1 if failed else 0
