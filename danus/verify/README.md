# danus.verify — the verify service (sole write-gate)

An **informal-LLM proof verifier** behind a tiny HTTP gateway. It is the sole
authority on mathematical correctness: a worker's fact submission (in `danus.gateway`)
calls it, and the candidate fact is written to the fact graph **iff** this service
returns `verdict: "correct"`.

It is **not** a formal / Lean checker — a gpt-5.5 codex agent reads the
natural-language markdown proof (logic, theorem application, external-citation
checking) and returns a verdict. There is **no human in the loop by default** —
research-level target theorems still need expert review before being trusted.

## Black-box contract

```
POST /verify
  request : {"statement": <str, >=1 char>, "proof": <str, >=1 char>,
             "timeout_seconds": <optional positive int>,
             "cancel_path": <optional worker stop-file path>,
             "request_id": <optional 64-char content hash>}            # application/json
  200     : {"verification_report": {"summary": str,
                                      "critical_errors": [{"location": str, "issue": str}, ...],
                                      "gaps":            [{"location": str, "issue": str}, ...]},
             "verdict": "correct" | "wrong",
             "repair_hints": str,
             "usage": {"input_tokens": int, "cached_input_tokens": int,
                       "fresh_input_tokens": int, "output_tokens": int,
                       "reasoning_tokens": int}}
                                                   # usage is parsed from Codex JSONL
  400     : vacuous input, or a P1/P3/P5 pre-check match (see prechecks.py)
  422     : request-model validation (empty statement/proof)
  499     : operator stop cancelled the verifier
  500     : codex failed / wrote no output / output is not valid JSON / non-dict
  504     : codex exec timed out (only if CODEX_TIMEOUT_SECONDS is set)

POST /verify-batch
  request : {"verification_goal": str,
             "candidates": [1-6 × {"candidate_id", "statement", "proof"}],
             "timeout_seconds"?: int, "cancel_path"?: str,
             "request_id"?: <64-char content hash>}
  200     : {"verifications": [ordered per-candidate reports], "usage": {...}}
  per-item: deterministic pre-check failures become `wrong`; eligible siblings
            still run in one codex process (all-precheck-rejected starts none)
  422/5xx : request/output contract or launcher failure

GET /health -> {"status": "ok", "pid": <int>}    # async; never queues behind /verify
                                                 # pid self-identifies the instance so
                                                 # doctor/services can tell OUR verify
                                                 # from a foreign one on a shared port
```

The gateway always supplies `request_id`. Concurrent retries wait on the same
run lock, and a retry after a lost HTTP response replays the completed artifact
and its measured usage instead of starting another Codex verifier.

**Invariant (enforced by both prompt and service):** `verdict == "correct"` ⟺
`critical_errors == []` **and** `gaps == []`. Malformed or inconsistent model
output is a 500 and never reaches the fact graph.

## Modules
- `prechecks.py` — pure, offline-testable: vacuousness + P1/P3/P5 hard prohibitions
  (all env-toggleable, all purely additive — they can only *reject* more).
- `launcher.py` — cold-start codex launcher (via the shared `danus.codex`): `codex
  exec --model gpt-5.5 --config model_reasoning_effort="xhigh" -C <AGENT_HOME>
  -c <danus MCP, role=verifier> --dangerously-bypass-approvals-and-sandbox <prompt>`;
  atomic run-id; reads back `verification.json`. Injects the gateway as **`python
  -m danus.gateway`**.
- `service.py` — FastAPI app (`/verify`, `/verify-batch`, `/health`) and output validator.

## Run

```bash
python -m danus.verify          # 127.0.0.1:8091, default CODEX_TIMEOUT_SECONDS=900
```

Binds **loopback by default** (set `VERIFY_HOST=0.0.0.0` if the
gateway runs on another host). Needs a codex CLI: set **`DANUS_CODEX_BIN`** (or
`codex` on PATH / the repo's `bin/codex` wrapper) and
an account via `CODEX_HOME` — **there is no built-in fallback path** (BYO). The
verifier agent runs `python -m danus.gateway`, so `danus` must be installed in that
environment.

## Configuration (env vars)

| var | default | meaning |
| --- | --- | --- |
| `VERIFY_HOST` / `VERIFY_PORT` (or `PORT`) | `127.0.0.1` / `8091` | bind addr (`python -m danus.verify`) |
| `VERIFY_AGENT_HOME` | `<this dir>/agent` | the codex `-C` working dir (AGENTS.md + skills) |
| `VERIFIER_RESULTS_DIR` | `<this dir>/runs` | per-verification run dirs (`log.md` + `verification.json`) |
| `DANUS_CODEX_BIN` | `<repo>/bin/codex` → `which codex` → bare `"codex"` | the codex binary; resolved via the shared `danus.codex` launcher |
| `DANUS_VERIFY_MODEL` / `DANUS_VERIFY_EFFORT` (fall back to neutral `DANUS_CODEX_MODEL` / `DANUS_CODEX_EFFORT`) | `gpt-5.5` / `xhigh` | codex knobs |
| `CODEX_TIMEOUT_SECONDS` | `0` lib / **`900`** via `python -m danus.verify` | per-verification codex timeout |
| `VERIFY_MIN_STATEMENT_CHARS` / `VERIFY_MIN_PROOF_CHARS` / `VERIFY_MIN_PROOF_WORDS` | 10 / 30 / 5 | vacuousness thresholds |
| `VERIFY_REJECT_PROBLEM_MD_CITATIONS` / `VERIFY_REJECT_UNPROVEN_CONDITIONALS` / `VERIFY_REJECT_VAGUE_GESTURES` | `1` | toggle P1 / P3 / P5 (`0` disables) |

## How fact submission reaches it
`danus.gateway` POSTs the claim(s), verifier deadline, and
worker stop path to `DANUS_VERIFY_URL`
(e.g. `http://127.0.0.1:8091/verify`; batch uses the adjacent `/verify-batch`), writes each fact **iff** its `verdict ==
"correct"`, and always records the outcome to global memory (kind `verification`).
Until this service is up and `DANUS_VERIFY_URL` is set, submission returns a
clear "verify service not wired" error.

## Trust assumptions (security)

- The verifier runs `codex exec --dangerously-bypass-approvals-and-sandbox` inside
  `VERIFY_AGENT_HOME` — that agent home (its `AGENTS.md` + skills) is **trusted
  input**; do not point it at untrusted content.
- It is an **LLM judge, not a formal (Lean) checker**, with **no human in the loop
  by default**; a `correct` verdict writes a permanent fact. Research-level target
  theorems need expert human review before being trusted.
- Binds **loopback** by default; `DANUS_VERIFY_TIMEOUT` (900 via `python -m
  danus.verify`) bounds each codex call.
