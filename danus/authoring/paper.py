"""Native LaTeX artifact gates used by Codex on Windows and POSIX."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from danus import runtime


_UNSUPPORTED = {"", "auto"}
_ENGINES = {"latexmk", "pdflatex", "xelatex", "lualatex", "tectonic"}
_BAD_LOG = re.compile(
    r"(?im)^(?:!|.*LaTeX Error:)|Undefined control sequence|"
    r"Citation .+ undefined|Reference .+ undefined|"
    r"There were undefined (?:references|citations)"
)


@dataclass(frozen=True)
class CompileResult:
    ok: bool
    engine_available: bool
    engine: str
    output: Path | None
    log: str


def find_tex_engine(requested: str | None = None) -> tuple[str, str | None]:
    """Resolve an explicit engine, otherwise prefer latexmk."""
    requested = requested or os.environ.get("TEX_ENGINE")
    if requested:
        name = requested.lower()
        if name not in _ENGINES:
            raise ValueError(
                f"unsupported TEX_ENGINE {requested!r}; use {', '.join(sorted(_ENGINES))}"
            )
        return name, shutil.which(name)
    for name in ("latexmk", "pdflatex", "xelatex", "lualatex", "tectonic"):
        executable = shutil.which(name)
        if executable:
            return name, executable
    return "latexmk", None


def _command(engine: str, executable: str, tex: Path, build: Path) -> list[str]:
    common = ["-interaction=nonstopmode", "-halt-on-error", "-file-line-error"]
    if engine == "latexmk":
        mode = {
            "xelatex": "-xelatex",
            "lualatex": "-lualatex",
        }.get(os.environ.get("DANUS_LATEXMK_ENGINE", "").lower(), "-pdf")
        return [executable, mode, *common, f"-outdir={build}", str(tex)]
    if engine == "tectonic":
        return [
            executable, "--keep-logs", "--chatter", "minimal",
            "--outdir", str(build), str(tex),
        ]
    return [executable, *common, f"-output-directory={build}", str(tex)]


def _run_with_timeout(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    timeout = int(os.environ.get("DANUS_LATEX_TIMEOUT_SECONDS", "300"))
    process = runtime.spawn_process(
        command,
        cwd=cwd,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        new_process_group=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        runtime.stop_process(process, force=True)
        stdout, stderr = process.communicate()
        message = f"LaTeX command timed out after {timeout}s"
        stderr = (stderr or b"") + ("\n" + message).encode()
        returncode = 124
    else:
        returncode = process.returncode
    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout,
        stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr,
    )


def compile_tex(
    tex: str | Path,
    *,
    output: str | Path | None = None,
    engine: str | None = None,
    run: callable = subprocess.run,
) -> CompileResult:
    """Compile in an isolated build directory and atomically publish a clean PDF."""
    source = Path(tex).resolve()
    if source.suffix.lower() != ".tex" or not source.is_file():
        raise ValueError(f"no such .tex file: {source}")
    destination = Path(output).resolve() if output else source.with_suffix(".pdf")
    if destination.suffix.lower() != ".pdf" or destination == source:
        raise ValueError(f"LaTeX output must be a distinct .pdf path: {destination}")
    engine_name, executable = find_tex_engine(engine)
    if not executable:
        return CompileResult(
            False, False, engine_name, None,
            f"{engine_name} not found; install MiKTeX/TeX Live or set TEX_ENGINE",
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="danus-latex-") as temp:
        build = Path(temp)
        command = _command(engine_name, executable, source, build)
        passes = 1 if engine_name in {"latexmk", "tectonic"} else 2
        chunks: list[str] = []
        returncode = 0
        for _ in range(passes):
            completed = (
                _run_with_timeout(command, cwd=source.parent)
                if run is subprocess.run else
                run(
                    command,
                    cwd=source.parent,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    check=False,
                )
            )
            returncode = completed.returncode
            chunks.extend((completed.stdout or "", completed.stderr or ""))
            if returncode:
                break
        log_path = build / f"{source.stem}.log"
        final_log = (
            log_path.read_text(encoding="utf-8", errors="replace")
            if log_path.is_file() else ""
        )
        if final_log:
            chunks.append(final_log)
        log = "\n".join(chunk for chunk in chunks if chunk)
        built = build / f"{source.stem}.pdf"
        strict_log = final_log or log
        if (
            returncode or not built.is_file() or not built.stat().st_size
            or _BAD_LOG.search(strict_log)
        ):
            return CompileResult(False, True, engine_name, None, log)

        handle = tempfile.NamedTemporaryFile(
            prefix=f".{destination.stem}-", suffix=".pdf",
            dir=destination.parent, delete=False,
        )
        staged = Path(handle.name)
        handle.close()
        try:
            shutil.copyfile(built, staged)
            os.replace(staged, destination)
        finally:
            staged.unlink(missing_ok=True)
        return CompileResult(True, True, engine_name, destination, log)


def compile_tex_text(
    tex: str,
    *,
    engine: str | None = None,
    resource_dir: str | Path | None = None,
) -> CompileResult:
    with tempfile.TemporaryDirectory(prefix="danus-latex-check-") as temp:
        root = Path(temp)
        if resource_dir:
            shutil.copytree(
                Path(resource_dir), root,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    "*.aux", "*.log", "*.out", "*.toc",
                    "*.fdb_latexmk", "*.fls", ".runs",
                ),
            )
        path = root / "main.tex"
        path.write_text(tex, encoding="utf-8")
        return compile_tex(path, engine=engine)


def anchors_stale(skill_dir: str | Path) -> bool:
    style = Path(skill_dir) / "style"
    anchors = style / "anchors"
    files = [
        path for path in anchors.rglob("*")
        if path.is_file() and path != anchors / "README.md"
    ] if anchors.is_dir() else []
    if not files:
        return False
    marker = style / ".distilled_at"
    return not marker.is_file() or max(p.stat().st_mtime_ns for p in files) > marker.stat().st_mtime_ns


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{path}:{number}: expected KEY=value")
        key, value = (part.strip() for part in line.split("=", 1))
        values[key] = value.strip("'\"")
    return values


def latex_git_push(
    tex: str | Path,
    message: str = "Update paper",
    *,
    env_file: str | Path,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Publish a paper after the caller has obtained operator approval."""
    source = Path(tex).resolve()
    if not source.is_file():
        raise ValueError(f"no such .tex file: {source}")
    secrets_path = Path(env_file).resolve()
    if not secrets_path.is_file():
        raise ValueError(f"missing credentials file: {secrets_path}")
    values = {**_read_env(secrets_path), **dict(environ or {})}
    url, token = values.get("LATEX_GIT_URL"), values.get("LATEX_GIT_TOKEN")
    if not url or not token:
        raise ValueError("LATEX_GIT_URL and LATEX_GIT_TOKEN are required")
    auth = base64.b64encode(f"git:{token}".encode()).decode()
    git_env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
    }
    git_timeout = int(values.get("DANUS_GIT_TIMEOUT_SECONDS", "120"))
    with tempfile.TemporaryDirectory(prefix="danus-latex-git-") as temp:
        repo = Path(temp) / "repo"
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", url, str(repo)],
            check=True, env=git_env, timeout=git_timeout,
        )
        shutil.copy2(source, repo / source.name)
        pdf = source.with_suffix(".pdf")
        if pdf.is_file():
            shutil.copy2(pdf, repo / pdf.name)
        name = values.get("LATEX_GIT_AUTHOR_NAME") or "paper"
        email = values.get("LATEX_GIT_AUTHOR_EMAIL") or "paper@local"
        subprocess.run(["git", "config", "user.name", name], cwd=repo, check=True, timeout=git_timeout)
        subprocess.run(["git", "config", "user.email", email], cwd=repo, check=True, timeout=git_timeout)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, timeout=git_timeout)
        changed = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=repo, timeout=git_timeout,
        ).returncode
        if not changed:
            return "no changes to push"
        subprocess.run(
            ["git", "commit", "--quiet", "-m", message],
            cwd=repo, check=True, timeout=git_timeout,
        )
        subprocess.run(
            ["git", "push", "--quiet", "origin", "HEAD"],
            cwd=repo, check=True, env=git_env, timeout=git_timeout,
        )
    return f"pushed {source.name} to {url}"
