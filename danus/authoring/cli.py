"""CLI for native paper and human-summary artifact operations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from danus import runtime
from danus.authoring.assets import skill_dir
from danus.authoring import paper, summary


def configure_parser(subparsers) -> None:
    root = subparsers.add_parser("artifacts", help="build paper and report artifacts")
    kinds = root.add_subparsers(dest="artifact_kind", required=True)
    latex = kinds.add_parser("paper")
    latex_ops = latex.add_subparsers(dest="artifact_action", required=True)
    compile_p = latex_ops.add_parser("compile")
    compile_p.add_argument("tex")
    compile_p.add_argument("--output")
    compile_p.add_argument("--engine")
    stale = latex_ops.add_parser("anchors-stale")
    stale.add_argument("--skill-dir")
    reviewed = latex_ops.add_parser("anchors-reviewed")
    reviewed.add_argument("--skill-dir")
    seed = latex_ops.add_parser("seed-ledger")
    seed.add_argument("project_dir")
    seed.add_argument("--out")
    seed.add_argument("--headline", nargs="*")
    seed.add_argument("--all-facts", action="store_true")
    seed.add_argument("--paper")
    push = latex_ops.add_parser("push")
    push.add_argument("tex")
    push.add_argument("--message", default="Update paper")
    push.add_argument("--env-file")

    human = kinds.add_parser("summary")
    human_ops = human.add_subparsers(dest="artifact_action", required=True)
    human_ops.add_parser("doctor")
    human_ops.add_parser("install-deps")
    render = human_ops.add_parser("render")
    render.add_argument("markdown")
    render.add_argument("output")
    render.add_argument("--title", default="")


def dispatch(args) -> int:
    try:
        if args.artifact_kind == "paper" and args.artifact_action == "compile":
            result = paper.compile_tex(args.tex, output=args.output, engine=args.engine)
            print(result.log if not result.ok else (
                f"COMPILE OK: {result.output} ({result.output.stat().st_size} bytes) "
                f"[{result.engine}]"
            ))
            return 0 if result.ok else (3 if not result.engine_available else 1)
        if args.artifact_kind == "paper" and args.artifact_action == "anchors-stale":
            stale = paper.anchors_stale(args.skill_dir or skill_dir("write-paper"))
            print("STALE" if stale else "CURRENT")
            return 0 if stale else 1
        if args.artifact_kind == "paper" and args.artifact_action == "anchors-reviewed":
            marker = Path(args.skill_dir or skill_dir("write-paper")) / "style" / ".distilled_at"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            print(f"marked reviewed: {marker}")
            return 0
        if args.artifact_kind == "paper" and args.artifact_action == "seed-ledger":
            from danus.write_paper.seed_ledger import seed

            path = seed(
                args.project_dir,
                output=args.out,
                headline=args.headline,
                all_facts=args.all_facts,
                paper_id=args.paper,
            )
            print(f"wrote {path}")
            return 0
        if args.artifact_kind == "paper" and args.artifact_action == "push":
            root = Path(os.environ.get("DANUS_ROOT", Path.cwd()))
            env_file = args.env_file or os.environ.get(
                "LATEX_GIT_ENV_FILE", root / "config" / "latex-git.env"
            )
            print(paper.latex_git_push(args.tex, args.message, env_file=env_file))
            return 0
        if args.artifact_kind == "summary" and args.artifact_action == "doctor":
            result = summary.doctor()
            print(json.dumps(result, indent=2))
            return 0 if all((result["node"], result["chrome"], result["dependencies"])) else 1
        if args.artifact_kind == "summary" and args.artifact_action == "install-deps":
            print(f"installed human-summary dependencies under {summary.install_dependencies()}")
            return 0
        if args.artifact_kind == "summary" and args.artifact_action == "render":
            out = summary.render_pdf(args.markdown, args.output, args.title)
            print(f"PDF -> {out} ({out.stat().st_size} bytes)")
            return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"artifact error: {exc}", file=__import__("sys").stderr)
        return 2
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="danus-artifacts")
    sub = parser.add_subparsers(dest="cmd", required=True)
    configure_parser(sub)
    return parser


def main(argv=None) -> int:
    runtime.configure_environment()
    args = build_parser().parse_args(["artifacts", *(argv or __import__("sys").argv[1:])])
    return dispatch(args)
