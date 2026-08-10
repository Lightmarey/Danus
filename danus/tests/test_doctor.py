from __future__ import annotations

import os
from pathlib import Path

from danus import doctor, runtime
from danus.tests.portable import write_python_launcher


def test_command_exists_finds_bare_name_on_path(tmp_path: Path):
    launcher = write_python_launcher(tmp_path, "codex-test", "pass\n")
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(tmp_path) + os.pathsep + old_path
    try:
        assert doctor._command_exists("codex-test") is True
    finally:
        os.environ["PATH"] = old_path


def test_command_exists_rejects_missing_absolute_path():
    missing = str(Path.cwd() / "definitely-missing-codex")
    assert doctor._command_exists(missing) is False


def test_command_exists_rejects_non_windows_script_path(tmp_path: Path):
    fake = tmp_path / "codex"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    if runtime.is_windows():
        assert doctor._command_exists(str(fake)) is False
    else:
        fake.chmod(0o755)
        assert doctor._command_exists(str(fake)) is True


def test_checks_report_missing_codex_for_nonexecutable_absolute_override(tmp_path: Path):
    fake = tmp_path / "codex"
    fake.write_text("not executable", encoding="utf-8")
    old = os.environ.get("DANUS_CODEX_BIN")
    os.environ["DANUS_CODEX_BIN"] = str(fake)
    try:
        rows = list(doctor.checks())
    finally:
        if old is None:
            os.environ.pop("DANUS_CODEX_BIN", None)
        else:
            os.environ["DANUS_CODEX_BIN"] = old
    codex_row = next(row for row in rows if row[0] == "codex")
    assert codex_row[1] is False


def test_missing_agent_assets_are_reported_without_traceback(monkeypatch):
    missing = FileNotFoundError("packaged worker asset missing")
    monkeypatch.setattr(doctor.L, "worker_md", lambda: (_ for _ in ()).throw(missing))
    monkeypatch.setattr(
        doctor.L, "worker_skills_dir", lambda: (_ for _ in ()).throw(missing),
    )
    rows = {name: (ok, detail) for name, ok, detail in doctor.checks()}
    assert rows["worker_contract"] == (False, "packaged worker asset missing")
    assert rows["worker_skills"] == (False, "packaged worker asset missing")


def test_incompatible_mcp_is_reported(monkeypatch):
    def fail_import(name: str):
        assert name == "danus._mcp"
        raise ImportError("FastMCP API missing")

    monkeypatch.setattr(doctor, "import_module", fail_import)
    rows = {name: (ok, detail) for name, ok, detail in doctor.checks()}
    assert rows["import:mcp"] == (False, "FastMCP API missing")
