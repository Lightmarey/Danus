from __future__ import annotations

import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

from danus.human_summary import assemble as human_summary
from danus.write_paper import assemble as write_paper


ROOT = Path(__file__).resolve().parents[2]


def test_main_assets_have_one_canonical_codex_source(monkeypatch):
    monkeypatch.delenv("DANUS_WRITE_PAPER_SKILL_DIR", raising=False)
    monkeypatch.delenv("DANUS_HUMAN_SUMMARY_SKILL_DIR", raising=False)
    skills = ROOT / ".agents" / "skills"
    assert {path.name for path in skills.iterdir() if path.is_dir()} == {
        "consult", "elaboration", "human-summary", "initialize", "write-paper"
    }
    assert write_paper.skill_dir() == skills / "write-paper"
    assert human_summary.skill_dir() == skills / "human-summary"
    assert not (ROOT / ".claude").exists()
    assert not (ROOT / "agents" / "skills" / "write-paper").exists()
    assert not (ROOT / "agents" / "skills" / "human-summary").exists()


def test_main_codex_mcp_config_is_portable_uv_stdio():
    config = tomllib.loads((ROOT / ".codex" / "config.toml").read_text(encoding="utf-8"))
    servers = config["mcp_servers"]
    assert set(servers) == {"danus", "write-paper", "human-summary"}
    assert {tuple(server["args"]) for server in servers.values()} == {
        ("run", "danus-mcp"),
        ("run", "write-paper-mcp"),
        ("run", "human-summary-mcp"),
    }
    for server in servers.values():
        assert server["command"] == "uv"
        assert server["cwd"] == "."
        assert server["env"] == {"DANUS_ROLE": "main", "DANUS_AUTHOR": "main_agent"}
        assert str(ROOT) not in str(server)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["consult"] == "danus.strategy.cli:main"


def test_required_codex_runtime_assets_have_no_legacy_paths():
    active = [
        ROOT / "AGENTS.md",
        ROOT / "OPERATOR.md",
        ROOT / ".codex" / "config.toml",
        ROOT / "danus" / "write_paper" / "assemble.py",
        ROOT / "danus" / "human_summary" / "assemble.py",
        ROOT / "danus" / "verify" / "launcher.py",
        ROOT / "danus" / "authoring" / "driver.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in active)
    for legacy in (".claude", ".mcp.json", "CLAUDE_SKILL_DIR", "python3"):
        assert legacy not in text


def test_all_native_entry_points_load_project_environment():
    entry_points = (
        ROOT / "danus" / "orchestration" / "cli.py",
        ROOT / "danus" / "strategy" / "cli.py",
        ROOT / "danus" / "doctor.py",
        ROOT / "danus" / "gateway" / "__main__.py",
        ROOT / "danus" / "write_paper" / "__main__.py",
        ROOT / "danus" / "human_summary" / "__main__.py",
        ROOT / "danus" / "verify" / "__main__.py",
        ROOT / "danus" / "observability" / "__main__.py",
        ROOT / "danus" / "execution" / "__main__.py",
    )
    for entry_point in entry_points:
        assert "configure_environment(" in entry_point.read_text(encoding="utf-8")


def test_initialize_is_native_codex_only():
    text = (ROOT / ".agents" / "skills" / "initialize" / "SKILL.md").read_text(
        encoding="utf-8"
    ).lower()
    prohibited = (
        "askuserquestion", "```bash", " bash ", "cp ", "mkdir", " date ",
        "setsid", "doctor.sh", "setup-codex.sh", "check-codex.sh",
    )
    assert not {token for token in prohibited if token in text}
    for required in (
        "uv sync", "uv run danus-doctor", "codex login status", "codex login",
        "uv run danus services", "ordinary codex conversation",
    ):
        assert required in text


def test_main_skill_control_instructions_are_codex_native():
    prohibited = (
        ".claude", ".mcp.json", "claude_skill_dir", "python3", "bin/consult",
        "scripts/services.sh", "scripts/doctor.sh", "setup-codex.sh",
        "check-codex.sh", "askuserquestion",
    )
    for skill in (ROOT / ".agents" / "skills").glob("*/SKILL.md"):
        text = skill.read_text(encoding="utf-8").lower()
        if skill.parent.name in ("human-summary", "write-paper"):
            text = "\n".join(
                line for line in text.splitlines()
                if not any(marker in line for marker in (
                    "driver/", "render_pdf.sh", "doctor.sh", "install-tex.sh"
                ))
            )
        assert not {
            token for token in prohibited if token in text
        }, f"legacy control instruction in {skill}"
    consult = (ROOT / ".agents" / "skills" / "consult" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "uv run consult" in consult
    assert "uv run danus assign" in consult


def test_wheel_install_layout_contains_authoring_assets(tmp_path):
    wheel_dir = tmp_path / "wheel"
    build_env = os.environ.copy()
    build_env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=ROOT,
        env=build_env,
        check=True,
        capture_output=True,
        text=True,
    )
    install = tmp_path / "install"
    with zipfile.ZipFile(next(wheel_dir.glob("*.whl"))) as wheel:
        shipped = {
            name.removeprefix("danus/_authoring_assets/")
            for name in wheel.namelist()
            if name.startswith("danus/_authoring_assets/") and not name.endswith("/")
        }
        assert shipped
        assert all(
            name == "human-summary/REPORT_WRITER_PROMPT.md"
            or name.startswith("write-paper/boilerplate/")
            or name.startswith("write-paper/roles/")
            or name.startswith("write-paper/style/")
            for name in shipped
        )
        assert not any(
            part in name
            for name in shipped
            for part in ("driver/", "examples/", "__pycache__", "SKILL.md")
        )
        wheel.extractall(install)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(install)
    code = (
        "from danus.human_summary.assemble import skill_dir as h;"
        "from danus.write_paper.assemble import skill_dir as w;"
        "assert (h() / 'REPORT_WRITER_PROMPT.md').is_file();"
        "assert (w() / 'roles' / 'AGENTS.md').is_file();"
        "assert '.agents' not in str(h()); assert '.agents' not in str(w())"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
