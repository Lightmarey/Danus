from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from danus.authoring import paper, summary


def test_tex_engine_prefers_latexmk(monkeypatch):
    monkeypatch.delenv("TEX_ENGINE", raising=False)
    monkeypatch.setattr(paper.shutil, "which", lambda name: f"/bin/{name}")
    assert paper.find_tex_engine() == ("latexmk", "/bin/latexmk")


def test_compile_rejects_undefined_references_and_does_not_publish(tmp_path, monkeypatch):
    source = tmp_path / "main.tex"
    source.write_text(r"\documentclass{article}\begin{document}x\end{document}")
    monkeypatch.setattr(paper, "find_tex_engine", lambda requested=None: ("latexmk", "latexmk"))

    def fake_run(command, **kwargs):
        build = Path(next(x.split("=", 1)[1] for x in command if x.startswith("-outdir=")))
        (build / "main.pdf").write_bytes(b"%PDF fake")
        (build / "main.log").write_text("LaTeX Warning: Reference `x' undefined")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = paper.compile_tex(source, run=fake_run)
    assert not result.ok
    assert result.engine_available
    assert not source.with_suffix(".pdf").exists()


def test_compile_atomically_publishes_nonempty_pdf(tmp_path, monkeypatch):
    source = tmp_path / "main.tex"
    source.write_text(r"\documentclass{article}\begin{document}x\end{document}")
    monkeypatch.setattr(paper, "find_tex_engine", lambda requested=None: ("latexmk", "latexmk"))

    def fake_run(command, **kwargs):
        build = Path(next(x.split("=", 1)[1] for x in command if x.startswith("-outdir=")))
        (build / "main.pdf").write_bytes(b"%PDF fake")
        (build / "main.log").write_text("clean")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = paper.compile_tex(source, run=fake_run)
    assert result.ok
    assert source.with_suffix(".pdf").read_bytes() == b"%PDF fake"


def test_artifact_outputs_cannot_overwrite_their_sources(tmp_path):
    tex = tmp_path / "main.tex"
    tex.write_text("source", encoding="utf-8")
    with pytest.raises(ValueError, match="distinct .pdf"):
        paper.compile_tex(tex, output=tex)
    report = tmp_path / "report.md"
    report.write_text("source", encoding="utf-8")
    with pytest.raises(ValueError, match="distinct .pdf"):
        summary.render_pdf(report, report)
    assert tex.read_text(encoding="utf-8") == "source"
    assert report.read_text(encoding="utf-8") == "source"


def test_latexmk_ignores_early_pass_warnings_when_final_log_is_clean(tmp_path, monkeypatch):
    source = tmp_path / "main.tex"
    source.write_text(r"\documentclass{article}\begin{document}x\end{document}")
    monkeypatch.setattr(paper, "find_tex_engine", lambda requested=None: ("latexmk", "latexmk"))

    def fake_run(command, **kwargs):
        build = Path(next(x.split("=", 1)[1] for x in command if x.startswith("-outdir=")))
        (build / "main.pdf").write_bytes(b"%PDF fake")
        (build / "main.log").write_text("Output written on main.pdf")
        return subprocess.CompletedProcess(
            command, 0, "LaTeX Warning: Reference `x' undefined on input line 1.", ""
        )

    assert paper.compile_tex(source, run=fake_run).ok


def test_latex_timeout_kills_the_process_tree(monkeypatch, tmp_path):
    stopped = []

    class Process:
        returncode = None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("latexmk", timeout)

    process = Process()
    monkeypatch.setenv("DANUS_LATEX_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(paper.runtime, "spawn_process", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        paper.runtime, "stop_process",
        lambda proc, force, **kwargs: stopped.append((proc, force)),
    )
    result = paper._run_with_timeout(["latexmk"], cwd=tmp_path)
    assert result.returncode == 124
    assert "timed out" in result.stderr
    assert stopped == [(process, True)]


def test_anchors_stale_ignores_shipped_readme(tmp_path):
    anchors = tmp_path / "style" / "anchors"
    anchors.mkdir(parents=True)
    (anchors / "README.md").write_text("docs")
    assert not paper.anchors_stale(tmp_path)
    (anchors / "mine.tex").write_text("paper")
    assert paper.anchors_stale(tmp_path)
    marker = tmp_path / "style" / ".distilled_at"
    marker.touch()
    assert not paper.anchors_stale(tmp_path)


def test_find_chrome_explicit_and_windows_standard_path(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"x")
    assert summary.find_chrome({"DANUS_CHROME_BIN": str(chrome)}) == str(chrome)
    program_files = tmp_path / "Program Files"
    standard = program_files / "Google/Chrome/Application/chrome.exe"
    standard.parent.mkdir(parents=True)
    standard.write_bytes(b"x")
    monkeypatch.setattr(summary.shutil, "which", lambda name: None)
    assert summary.find_chrome({"PROGRAMFILES": str(program_files)}) == str(standard)


def test_summary_render_never_installs_and_rejects_browser_failure(tmp_path, monkeypatch):
    source = tmp_path / "report.md"
    source.write_text("# report")
    deps = tmp_path / "deps"
    for name in ("markdown-it", "katex"):
        (deps / "node_modules" / name).mkdir(parents=True)
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"x")
    monkeypatch.setattr(summary, "node_dir", lambda environ=None: deps)
    monkeypatch.setattr(summary, "find_chrome", lambda environ=None: str(chrome))
    monkeypatch.setattr(summary.shutil, "which", lambda name: "node" if name == "node" else None)

    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "node":
            Path(command[3]).write_text("<html></html>")
            return subprocess.CompletedProcess(command, 0, "ok", "")
        return subprocess.CompletedProcess(command, 1, "", "browser failed")

    with pytest.raises(RuntimeError, match="Chrome PDF render failed"):
        summary.render_pdf(source, tmp_path / "out.pdf", run=fake_run)
    assert not (tmp_path / "out.pdf").exists()
    assert not any("npm" in command[0] for command in calls)


def test_summary_missing_dependencies_has_explicit_install_command(tmp_path, monkeypatch):
    source = tmp_path / "report.md"
    source.write_text("# report")
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"x")
    monkeypatch.setattr(summary, "node_dir", lambda environ=None: tmp_path / "missing")
    monkeypatch.setattr(summary, "find_chrome", lambda environ=None: str(chrome))
    monkeypatch.setattr(summary.shutil, "which", lambda name: "node" if name == "node" else None)
    with pytest.raises(RuntimeError, match="install-deps"):
        summary.render_pdf(source, tmp_path / "out.pdf")


def test_summary_dependency_install_uses_bounded_process_tree_runner(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("package.json", "package-lock.json"):
        (assets / name).write_text("{}", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(summary, "skill_dir", lambda name: assets)
    monkeypatch.setattr(summary.shutil, "which", lambda name: "npm" if name == "npm" else None)
    monkeypatch.setattr(
        summary, "_run_with_timeout",
        lambda command, *, env, cwd=None: seen.update(command=command, cwd=cwd) or
        subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(summary, "dependencies_ready", lambda root: True)
    assert summary.install_dependencies(tmp_path / "node") == tmp_path / "node"
    assert seen["command"][1:3] == ["ci", "--no-fund"]
    assert seen["cwd"] == tmp_path / "node"


def test_markdown_renderer_escapes_an_untrusted_title(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    modules = tmp_path / "node_modules"
    markdown = modules / "markdown-it"
    katex = modules / "katex"
    markdown.mkdir(parents=True)
    katex.mkdir(parents=True)
    (markdown / "index.js").write_text(
        "module.exports=class { render(x){ return x; } };",
        encoding="utf-8",
    )
    (katex / "index.js").write_text(
        "exports.renderToString=()=>{ throw new Error('bad math'); };",
        encoding="utf-8",
    )
    source = tmp_path / "report.md"
    output = tmp_path / "report.html"
    source.write_text("body $<img src=x onerror=bad()>$", encoding="utf-8")
    title = "</title><script>bad()</script>"
    env = {**os.environ, "NODE_PATH": str(modules)}
    subprocess.run(
        [
            node,
            str(summary.skill_dir("human-summary") / "md2html.js"),
            str(source), str(output), title,
        ],
        env=env, check=True, capture_output=True, text=True,
    )
    html = output.read_text(encoding="utf-8")
    assert title not in html
    assert "&lt;/title&gt;&lt;script&gt;bad()&lt;/script&gt;" in html
    assert "<img src=x onerror=bad()>" not in html
    assert "&lt;img src=x onerror=bad()&gt;" in html
