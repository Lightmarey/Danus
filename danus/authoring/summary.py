"""Native human-summary HTML/PDF rendering."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from danus.authoring.assets import skill_dir
from danus import runtime


def find_chrome(environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    explicit = env.get("DANUS_CHROME_BIN")
    if explicit:
        resolved = shutil.which(explicit) or explicit
        return str(Path(resolved)) if Path(resolved).is_file() else None
    candidates = [
        Path(env.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google/Chrome/Application/chrome.exe",
        Path(env.get("PROGRAMFILES", r"C:\Program Files"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    for name in ("chrome", "msedge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def node_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("DANUS_HUMAN_SUMMARY_NODE_DIR"):
        return Path(env["DANUS_HUMAN_SUMMARY_NODE_DIR"]).resolve()
    if os.name == "nt":
        base = Path(env.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        base = Path(env.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "danus" / "human-summary-node"


def dependencies_ready(directory: str | Path | None = None) -> bool:
    root = Path(directory) if directory else node_dir()
    return all((root / "node_modules" / name).is_dir() for name in ("markdown-it", "katex"))


def install_dependencies(directory: str | Path | None = None) -> Path:
    """Explicit opt-in install. Rendering itself never contacts the network."""
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found; install Node.js")
    root = Path(directory) if directory else node_dir()
    root.mkdir(parents=True, exist_ok=True)
    assets = skill_dir("human-summary")
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(assets / name, root / name)
    subprocess.run(
        [npm, "ci", "--no-fund", "--no-audit"],
        cwd=root,
        check=True,
    )
    if not dependencies_ready(root):
        raise RuntimeError(f"npm completed but dependencies are missing under {root}")
    return root


def doctor(environ: dict[str, str] | None = None) -> dict[str, object]:
    env = os.environ if environ is None else environ
    return {
        "node": shutil.which("node"),
        "chrome": find_chrome(env),
        "node_dir": str(node_dir(env)),
        "dependencies": dependencies_ready(node_dir(env)),
    }


def _run_with_timeout(command: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess:
    timeout = int(env.get("DANUS_SUMMARY_COMMAND_TIMEOUT_SECONDS", "120"))
    process = runtime.spawn_process(
        command,
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        new_process_group=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        runtime.stop_process(process, force=True)
        raise RuntimeError(f"command timed out after {timeout}s: {command[0]}")
    return subprocess.CompletedProcess(
        command, process.returncode,
        stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout,
        stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr,
    )


def render_pdf(
    source: str | Path,
    output: str | Path,
    title: str = "",
    *,
    environ: dict[str, str] | None = None,
    run: callable = subprocess.run,
) -> Path:
    source, destination = Path(source).resolve(), Path(output).resolve()
    if not source.is_file():
        raise ValueError(f"no such Markdown file: {source}")
    if destination.suffix.lower() != ".pdf" or destination == source:
        raise ValueError(f"summary output must be a distinct .pdf path: {destination}")
    env = dict(os.environ if environ is None else environ)
    node, chrome = shutil.which("node"), find_chrome(env)
    if not node:
        raise RuntimeError("node not found; install Node.js")
    if not chrome:
        raise RuntimeError("Chrome/Chromium not found; set DANUS_CHROME_BIN")
    deps = node_dir(env)
    if not dependencies_ready(deps):
        raise RuntimeError(
            f"markdown-it/KaTeX are missing under {deps}; run "
            "`uv run danus artifacts summary install-deps` explicitly"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    child_env = {**env, "NODE_PATH": str(deps / "node_modules")}
    with tempfile.TemporaryDirectory(prefix="danus-summary-") as temp:
        html = Path(temp) / "report.html"
        node_command = [
            node, str(skill_dir("human-summary") / "md2html.js"),
            str(source), str(html), title,
        ]
        convert = (
            _run_with_timeout(node_command, env=child_env)
            if run is subprocess.run else
            run(
                node_command, capture_output=True, text=True,
                errors="replace", check=False, env=child_env,
            )
        )
        if convert.returncode or not html.is_file() or not html.stat().st_size:
            raise RuntimeError(
                f"markdown render failed ({convert.returncode}): "
                f"{(convert.stdout or '')}{(convert.stderr or '')}"
            )
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{destination.stem}-", suffix=".pdf",
            dir=destination.parent, delete=False,
        )
        staged = Path(handle.name)
        handle.close()
        staged.unlink()
        profile = Path(temp) / "chrome-profile"
        try:
            browser_command = [
                    chrome, "--headless", "--disable-gpu",
                    "--no-first-run", "--disable-background-networking",
                    "--disable-component-update", "--disable-extensions",
                    f"--user-data-dir={profile}",
                    f"--print-to-pdf={staged}",
                    "--virtual-time-budget=25000",
                    "--run-all-compositor-stages-before-draw",
                    html.as_uri(),
                ]
            if env.get("DANUS_CHROME_NO_SANDBOX", "").lower() in ("1", "true", "yes"):
                browser_command.insert(3, "--no-sandbox")
            browser = (
                _run_with_timeout(browser_command, env=child_env)
                if run is subprocess.run else
                run(
                    browser_command, capture_output=True, text=True,
                    errors="replace", check=False,
                )
            )
            if browser.returncode or not staged.is_file() or not staged.stat().st_size:
                raise RuntimeError(
                    f"Chrome PDF render failed ({browser.returncode}): "
                    f"{(browser.stdout or '')}{(browser.stderr or '')}"
                )
            os.replace(staged, destination)
        finally:
            staged.unlink(missing_ok=True)
    return destination
