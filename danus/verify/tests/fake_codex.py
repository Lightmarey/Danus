#!/usr/bin/env python
"""A stand-in for the `codex` CLI, for PLUMBING tests of the verify service.

The real service cold-starts `codex exec ... <prompt>`; the codex agent reads
AGENTS.md, judges the proof, and writes verification.json to the path named in
the prompt. This stub does NOT judge any mathematics -- it only exercises the
service's subprocess + file-readback + verdict-propagation plumbing
deterministically, with no codex install and no API spend.

Verdict rule (deterministic, plumbing only):
  - prompt contains "[[FAKE:wrong]]"  -> verdict "wrong"
  - otherwise                         -> verdict "correct"

Point the service at it with DANUS_CODEX_BIN=/abs/path/to/fake_codex.py . It accepts
(and ignores) the real codex flags; the prompt is the final argv entry.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2 and not os.environ.get("FAKE_CODEX_PROMPT"):
        sys.stderr.write("fake_codex: no prompt argument\n")
        return 2
    prompt = os.environ.get("FAKE_CODEX_PROMPT") or " ".join(sys.argv[1:])

    m = re.search(r"this exact path:\s*(\S+)", prompt)
    if m:
        out_path = Path(m.group(1).rstrip("."))
    else:
        results_root = Path(os.environ["VERIFIER_RESULTS_DIR"])
        run_dirs = [p for p in results_root.iterdir() if p.is_dir()]
        if len(run_dirs) != 1:
            sys.stderr.write("fake_codex: could not infer unique run directory\n")
            return 3
        out_path = run_dirs[0] / "verification.json"

    def verdict(text: str) -> dict:
        if "[[FAKE:wrong]]" not in text:
            return {
                "verification_report": {
                    "summary": "FAKE stub verdict (plumbing test): no error marker; accepting.",
                    "critical_errors": [],
                    "gaps": [],
                },
                "verdict": "correct",
                "repair_hints": "",
            }
        return {
            "verification_report": {
                "summary": "FAKE stub verdict (plumbing test): marker [[FAKE:wrong]] present.",
                "critical_errors": [
                    {"location": "proof", "issue": "fake_codex injected critical error for the reject path"}
                ],
                "gaps": [],
            },
            "verdict": "wrong",
            "repair_hints": "This is a fake reject from fake_codex.py (plumbing only).",
        }

    marker = "Candidates (verify each independently):"
    if marker in prompt:
        candidates, _ = json.JSONDecoder().raw_decode(prompt.partition(marker)[2].lstrip())
        payload = {"verifications": [
            {"candidate_id": candidate["candidate_id"], **verdict(candidate["proof"])}
            for candidate in candidates
        ]}
    else:
        payload = verdict(prompt)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    sys.stdout.write(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 120, "cached_input_tokens": 80,
        "output_tokens": 20, "reasoning_output_tokens": 10,
    }}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
