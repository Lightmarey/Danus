from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10; pytest's dependency provides tomli.
    import tomli as tomllib

from danus import agent_assets
from danus.human_summary import assemble as human_summary
from danus.write_paper import assemble as write_paper


ROOT = Path(__file__).resolve().parents[2]


def test_agent_asset_resolver_prefers_source_checkout(monkeypatch, tmp_path):
    source = tmp_path / "source"
    packaged = tmp_path / "packaged"
    for root, text in ((source, "source"), (packaged, "packaged")):
        path = root / "contracts" / "worker.md"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(agent_assets, "_SOURCE_ROOT", source)
    monkeypatch.setattr(agent_assets, "_PACKAGED_ROOT", packaged)
    assert agent_assets.contract("worker").read_text(encoding="utf-8") == "source"


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
            name in {
                "human-summary/REPORT_WRITER_PROMPT.md",
                "human-summary/md2html.js",
                "human-summary/package.json",
                "human-summary/package-lock.json",
            }
            or name.startswith("write-paper/boilerplate/")
            or name.startswith("write-paper/roles/")
            or name.startswith("write-paper/style/")
            or name.startswith("write-paper/templates/")
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
    project = tmp_path / "project"
    shutil.copytree(
        ROOT / ".agents" / "skills" / "write-paper" / "examples" / "paper" / "project",
        project,
    )
    code = (
        "import sys;"
        "from danus.human_summary.assemble import skill_dir as h;"
        "from danus.write_paper.assemble import skill_dir as w;"
        "from danus.write_paper.seed_ledger import seed;"
        "assert (h() / 'REPORT_WRITER_PROMPT.md').is_file();"
        "assert (w() / 'roles' / 'AGENTS.md').is_file();"
        "assert '.agents' not in str(h()); assert '.agents' not in str(w());"
        f"assert seed({str(project)!r}).is_file();"
        "assert 'mcp' not in sys.modules"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_clean_wheel_provisions_worker_and_verifier_agent_assets(tmp_path):
    wheel_dir = tmp_path / "wheel"
    build_env = os.environ.copy()
    build_env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=ROOT, env=build_env, check=True, capture_output=True, text=True,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    install = tmp_path / "install"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        archive.extractall(install)
    required = {
        "danus/_agent_assets/contracts/worker.md",
        "danus/_agent_assets/contracts/verifier.md",
        "danus/_agent_assets/skills/worker/check_conformance.py",
        "danus/_agent_assets/skills/worker/direct-proving/SKILL.md",
        "danus/_agent_assets/skills/verify/test_verification_schema.py",
        "danus/_agent_assets/skills/verify/verify-sequential-statements/SKILL.md",
    }
    assert required <= names
    assert not any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names)
    shipped = {
        name.removeprefix("danus/_agent_assets/")
        for name in names if name.startswith("danus/_agent_assets/")
    }
    assert all(
        name in {
            "contracts/worker.md", "contracts/verifier.md",
            "skills/worker/check_conformance.py",
            "skills/verify/test_verification_schema.py",
        }
        or name.endswith("/SKILL.md")
        or name.endswith("/agents/openai.yaml")
        for name in shipped
    )

    env = os.environ.copy()
    for name in (
        "DANUS_ROOT", "DANUS_RUNTIME", "VERIFIER_RESULTS_DIR",
        "DANUS_WORKER_CONTRACT", "DANUS_WORKER_SKILLS", "DANUS_PROJECT_DIR",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(install)
    env["DANUS_AGENTS_ROOT"] = str(tmp_path / "projects")
    env["VERIFY_AGENT_HOME"] = str(tmp_path / "verifier-home")
    subprocess.run(
        [sys.executable, "-m", "danus.orchestration", "new", "wheel-project",
         "--roles", "high:1"],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True,
    )
    code = (
        "from pathlib import Path;"
        "from danus.verify.launcher import ensure_agent_home;"
        "from danus.services import service_env;"
        "assert Path(service_env()['DANUS_RUNTIME']) == (Path.cwd()/'runtime').resolve();"
        "worker=Path.cwd()/'projects'/'wheel-project'/'workers'/'high';"
        "assert (worker/'AGENTS.md').read_text(encoding='utf-8').strip();"
        "assert (worker/'.agents/skills/direct-proving/SKILL.md').read_text(encoding='utf-8').strip();"
        "verifier=ensure_agent_home();"
        "assert (verifier/'AGENTS.md').read_text(encoding='utf-8').strip();"
        "assert (verifier/'.agents/skills/verify-sequential-statements/SKILL.md').read_text(encoding='utf-8').strip()"
    )
    subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env, check=True,
        capture_output=True, text=True,
    )


def test_sdist_can_build_the_same_clean_authoring_wheel(tmp_path):
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    sdist_dir = tmp_path / "sdist"
    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(sdist_dir)],
        cwd=ROOT, env=env, check=True, capture_output=True, text=True,
    )
    sdist = next(sdist_dir.glob("*.tar.gz"))
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir), str(sdist)],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True,
    )
    with zipfile.ZipFile(next(wheel_dir.glob("*.whl"))) as wheel:
        names = set(wheel.namelist())
    for required in (
        "danus/_authoring_assets/human-summary/md2html.js",
        "danus/_authoring_assets/human-summary/package-lock.json",
        "danus/_authoring_assets/write-paper/templates/PROJECT_BRIEF.md.template",
        "danus/_authoring_assets/write-paper/templates/REVISION_LOG.md.template",
        "danus/_agent_assets/contracts/worker.md",
        "danus/_agent_assets/contracts/verifier.md",
        "danus/_agent_assets/skills/worker/direct-proving/SKILL.md",
        "danus/_agent_assets/skills/verify/verify-sequential-statements/SKILL.md",
    ):
        assert required in names
    assert not any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names)
