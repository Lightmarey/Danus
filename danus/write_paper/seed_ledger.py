"""Seed a paper reference ledger from verified fact metadata."""

from __future__ import annotations

import argparse
from pathlib import Path

from danus.core import FactGraph
from danus.write_paper import assemble


def closure_fact_ids(project_dir: Path, headline=None, paper_id=None):
    graph = FactGraph(project_dir)
    resolved, source = assemble.resolve_headline(project_dir, headline, paper_id)
    if source == "unset":
        raise assemble.TargetUnsetError(
            "no paper target is set; pass --headline, record TARGET.md, "
            "or use --all-facts"
        )
    return assemble._toposort_with_predecessors(graph, resolved)


def collect(project_dir: Path, headline=None, all_facts=False, paper_id=None) -> dict:
    graph = FactGraph(project_dir)
    fact_ids = graph.list() if all_facts else closure_fact_ids(
        project_dir, headline, paper_id
    )
    rows: dict = {}
    for fact_id in fact_ids:
        for ref in graph.external_refs(fact_id):
            if not isinstance(ref, dict):
                continue
            key = str(
                ref.get("key") or ref.get("arxiv") or ref.get("title") or "UNKEYED"
            ).strip()
            row = rows.setdefault(key, {"key": key, "cited_by": []})
            for field, value in ref.items():
                if value and not row.get(field):
                    row[field] = value
            if fact_id not in row["cited_by"]:
                row["cited_by"].append(fact_id)
    return rows


def render(rows: dict) -> str:
    lines = [
        "# REFERENCE_LEDGER", "",
        "Seeded from the project's verified facts' `external_refs`. Each row is a",
        "published result some proof cited. `verified-by: unverified` rows still",
        "need an independent check by the reference auditor (authors / title /",
        "venue / year / arXiv id). Do not fabricate; flag what cannot be verified.",
        "",
    ]
    if not rows:
        return "\n".join([*lines, "_(no external references captured on any fact yet)_", ""])
    for key in sorted(rows):
        row = rows[key]
        lines.append(f"## {key}")
        authors = row.get("authors")
        if authors:
            lines.append(
                "- authors: " + (
                    ", ".join(str(value) for value in authors)
                    if isinstance(authors, list) else str(authors)
                )
            )
        for field in ("title", "arxiv", "year", "venue", "doi"):
            if row.get(field):
                lines.append(f"- {field}: {row[field]}")
        lines.extend((
            f"- cited_by_facts: {', '.join(row['cited_by']) or '—'}",
            "- verified-by: unverified",
            "",
        ))
    return "\n".join(lines)


def seed(
    project_dir: str | Path,
    *,
    output: str | Path | None = None,
    headline=None,
    all_facts=False,
    paper_id=None,
) -> Path:
    project = Path(project_dir)
    if not (project / "fact_graph").is_dir():
        raise ValueError(f"no fact_graph/ under {project}")
    text = render(collect(project, headline, all_facts, paper_id))
    destination = (
        Path(output) if output
        else assemble.paper_workspace(project, paper_id) / "REFERENCE_LEDGER.md"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--out")
    parser.add_argument("--headline", nargs="*")
    parser.add_argument("--all-facts", action="store_true")
    parser.add_argument("--paper")
    args = parser.parse_args(argv)
    try:
        path = seed(
            args.project_dir,
            output=args.out,
            headline=args.headline,
            all_facts=args.all_facts,
            paper_id=args.paper,
        )
    except assemble.TargetUnsetError as exc:
        print(f"seed_ledger: {exc}", file=__import__("sys").stderr)
        return 3
    except ValueError as exc:
        print(f"seed_ledger: {exc}", file=__import__("sys").stderr)
        return 2
    print(f"wrote {path}")
    return 0
