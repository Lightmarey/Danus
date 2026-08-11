# Danus — Configuration Reference

All host- and account-specific configuration lives in gitignored `config/*.env`
files; **no path or secret is hardcoded** elsewhere. Every installed Python/uv
entry point loads the chain without executing it:

```
config/codex.env  →  config/danus.env  →  runtime/runtime.env  →  built-in defaults
   (BYO backend)      (host/account)      (machine paths, auto)   (Python defaults)
```

Only `*.env.example` templates are committed; copy them to the real names and
edit. `uv run danus`, `uv run consult`, the doctor, workers, services, and all
three project MCP servers load them automatically. Explicit values already in
the process environment win. Values below are the defaults from the Python
runtime and `config/danus.env.example`.

## Codex backend (workers + verifier)

| variable | default | meaning |
|---|---|---|
| `CODEX_BACKEND` | `api` | `api` (BYO OpenAI-compatible key) or `chatgpt` (your ChatGPT login) |
| `CODEX_HOME` | `runtime/codex-home` | codex auth/config home (gitignored) |
| `CODEX_API_BASE_URL` | — | (api) your OpenAI-compatible Responses endpoint |
| `CODEX_API_MODEL` | `gpt-5.5` | (api) backend model |
| `DANUS_CODEX_API_KEY` | — | (api) key, **read at run time**, never stored in a file |

These live in `config/codex.env`. See `getting-started.md` §2 and
run `uv run danus codex api` to materialize the keyless model-provider file.
Use `uv run danus codex login` for ChatGPT authentication and
`uv run danus codex status` to inspect the selected backend.

## Strategy consult (the system's brain)

| variable | default | meaning |
|---|---|---|
| `DANUS_CONSULT_TRANSPORT` | `gpt_pro` | `gpt_pro` \| `claude_api` \| `claude_code` \| `off` |
| `DANUS_CONSULT_API_KEY` | — | (gpt_pro) key for the OpenAI-compatible Responses API |
| `DANUS_CONSULT_BASE_URL` | `https://api.openai.com/v1` | (gpt_pro) endpoint |
| `DANUS_CONSULT_MODEL` | `gpt-5.5-pro` | (gpt_pro) model |
| `DANUS_CONSULT_BACKGROUND` | `1` | (gpt_pro) send `background=true`; `0` for a gateway that rejects it (per-call: `--background off`) |
| `DANUS_CONSULT_STORE` | `0` | (gpt_pro) send `store=false`; `1` for a gateway that requires stored responses (per-call: `--store on`) |
| `DANUS_CONSULT_CLAUDE_CODE_MODEL` | `claude-fable-5` | (claude_code) model via the `claude` CLI |
| `DANUS_CONSULT_CLAUDE_CODE_BIN` | `claude` | (claude_code) path to the CLI |
| `DANUS_CONSULT_CLAUDE_CODE_MAX_WALL` | `1800` | (claude_code) hard wall-clock cap per consult (s) |
| `DANUS_CONSULT_CLAUDE_CODE_PRICE_IN` | `10.0` | (claude_code) ledger estimate, USD per 1M input tokens |
| `DANUS_CONSULT_CLAUDE_CODE_PRICE_OUT` | `50.0` | (claude_code) ledger estimate, USD per 1M output tokens |
| `DANUS_CONSULT_CLAUDE_API_KEY` | — (falls back to `ANTHROPIC_API_KEY`) | (claude_api) BYO Anthropic API key |
| `DANUS_CONSULT_CLAUDE_API_BASE_URL` | Anthropic default | (claude_api) only for a proxy |
| `DANUS_CONSULT_CLAUDE_API_MODEL` | `claude-fable-5` | (claude_api) any Claude model |
| `DANUS_CONSULT_CLAUDE_API_FALLBACK` | `claude-opus-4-8` | (claude_api) refusal-fallback model; `off` disables |
| `DANUS_CONSULT_CLAUDE_API_PRICE_IN` | `10.0` | (claude_api) USD per 1M input tokens (real usage) |
| `DANUS_CONSULT_CLAUDE_API_PRICE_OUT` | `50.0` | (claude_api) USD per 1M output tokens (real usage) |

- `gpt_pro` = a paid, per-token OpenAI-compatible model. `claude_api` = the
  Anthropic API via the native SDK (per-token, BYO key; cost from real usage).
  `claude_code` = your Claude subscription via the Claude Code CLI (`claude -p`).
  `off` = the main agent reasons on its own, no consult.
- The `claude_code` consult runs **isolated**: a throwaway cwd, no settings and no MCP
  servers loaded (`--setting-sources "" --strict-mcp-config` — needs a recent
  `claude` CLI), web-only tools, and the prompt on stdin (never argv, which is
  world-readable on a shared host). It sees the elaboration and the public web,
  nothing else.
- Consult effort is selected per call with `--effort`. Accepted values are
  `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`. All transports support
  through `max`; a `gpt_pro` `max` request is never silently retried without its
  requested reasoning effort.

## Models & reasoning effort

All three codex-exec sites (workers, verifier, paper/report renderers) resolve
binary + model + effort through the shared launcher, so names are unified. Neutral
defaults apply everywhere; per-service overrides win.

| variable | default | applies to |
|---|---|---|
| `DANUS_CODEX_BIN` | `<repo>/bin/codex`, else `codex` on PATH | all codex calls |
| `DANUS_CODEX_MODEL` | `gpt-5.5` | neutral default (all sites) |
| `DANUS_CODEX_EFFORT` | `xhigh` | neutral default effort (all sites) |
| `DANUS_VERIFY_MODEL` / `_EFFORT` | neutral | verifier — the correctness authority; keep effort at `xhigh` |
| `DANUS_WRITE_PAPER_MODEL` / `_EFFORT` | neutral | paper renderer |
| `DANUS_HUMAN_SUMMARY_MODEL` / `_EFFORT` | neutral | human-summary renderer |

## Ports (all loopback)

| variable | default | service |
|---|---|---|
| `VERIFY_PORT` | `8091` | verify service (`127.0.0.1`) |
| `DASHBOARD_PORT` | `8099` | read-only dashboard (`127.0.0.1`) |
| `DANUS_VERIFY_URL` | `http://127.0.0.1:8091/verify` | where `fact_submit` posts |
| `VERIFY_HOST` | `127.0.0.1` | verify bind host (keep loopback — see security doc) |

## Runtime data locations (gitignored, under `runtime/`)

| variable | default | holds |
|---|---|---|
| `DANUS_RUNTIME` | `<repo>/runtime` | the whole self-contained runtime |
| `DANUS_AGENTS_ROOT` | `runtime/projects` | where `danus new` puts projects |
| `VERIFIER_RESULTS_DIR` | `runtime/verify-runs` | per-verification run logs |
| `DANUS_PY` | current uv Python (legacy wrappers fall back to `python`) | the engine's Python |

## Worker loop pacing (optional; engine defaults are sane)

| variable | default | meaning |
|---|---|---|
| `DANUS_ROUND_HARD_TIMEOUT` | `14400` (4h) | per-round wall-clock cap |
| `DANUS_MAX_ROUNDS` | `0` (unlimited) | round backstop |
| `DANUS_MAX_CONSEC_FAILURES` | `5` | bail after N consecutive failed rounds |
| `DANUS_ROUND_BEAT` | `5` | seconds between rounds |

Those variables govern legacy loops. V2 slice timeout and route limits are stored
on each structured assignment (`--slice-timeout`, `--max-slices`). Optional
`TargetContract.budget` limits total wall time and/or USD cost. When the Codex
backend exposes token usage, `DANUS_CODEX_PRICE_IN` and
`DANUS_CODEX_PRICE_OUT` give per-million-token rates for cost attribution.

V2 resilience defaults also live in the target's `budget` object, so every
worker and restart observes the same values:

| key | default | meaning |
|---|---:|---|
| `max_infra_attempts` | `3` (`2` for timeouts) | consecutive transport attempts before blocking |
| `max_infra_wall_seconds` | `min(1800, 5% of max_wall_seconds)` | separate outage-loss ceiling |
| `infra_retry_seconds` | `[30, 120, 600]` | persisted retry cooldowns; a provider `Retry-After` can only increase them |
| `strict_cost_reservations` | `false` | reject calls whose maximum USD cost cannot be estimated |
| `max_call_cost_usd` | unset | conservative per-call USD reservation used by strict cost control |

Infrastructure attempts count toward real project wall/cost totals but never
consume route slices or low-gain checkpoints. Missing provider usage is stored
as unknown cost, not zero cost. A provider/account hard spending limit remains
necessary for a strict external USD ceiling because a disconnected request may
not return a billing receipt.

Before every V2 worker, verifier, consult, or authoring call, SQLite reserves its
full configured wall timeout in one transaction. Active reservations participate
in the 70/85/100% thresholds, so concurrent callers cannot all pass a stale
budget check. Completion atomically replaces the reservation with the real
`CostEvent`; a crashed process leaves a conservative reservation that expires
after the hard timeout plus a short cleanup grace period.
If a strict USD reservation was configured but the provider returns no usage or
cost receipt, settlement consumes the reserved per-call ceiling with
`cost_status=estimated_ceiling`; it is never released as an invented zero.

## Rendering & misc

| variable | default | meaning |
|---|---|---|
| `DANUS_CHROME_BIN` | (auto-detect) | headless Chrome/Chromium for human-summary PDF |
| `DANUS_CHROME_NO_SANDBOX` | off | opt in to Chrome's `--no-sandbox` only on an already isolated host |
| `TEX_ENGINE` | auto (`latexmk` preferred) | write-paper LaTeX engine (`latexmk`/`pdflatex`/`xelatex`/`lualatex`/`tectonic`) |
| `DANUS_LATEX_TIMEOUT_SECONDS` | `300` | wall-clock cap for each native LaTeX command |
| `DANUS_SUMMARY_COMMAND_TIMEOUT_SECONDS` | `120` | wall-clock cap for each Node/npm/Chrome summary command |
| `DANUS_PDFTOTEXT_TIMEOUT_SECONDS` | `60` | wall-clock cap for each PDF style-anchor extraction |
| `DANUS_AUTHORING_TIMEOUT_SECONDS` | driver default | shared Codex writer/summary call cap |
| `DANUS_WRITE_PAPER_TIMEOUT_SECONDS` | shared authoring cap | write-paper Codex call override |
| `DANUS_HUMAN_SUMMARY_TIMEOUT_SECONDS` | shared authoring cap | human-summary Codex call override |
| `DANUS_WRITE_PAPER_RUN_LOG` | on | per-call write-paper diagnostic logs (`0` disables) |
| `DANUS_HUMAN_SUMMARY_RUN_LOG` | on | per-call human-summary diagnostic logs (`0` disables) |
| `DANUS_PAPER_VERIFY_WHOLE_DOC_CAP` | `700000` | char budget for one whole-paper math-verify call; over it the tool reports `too_large` (the main agent decomposes — the tool never auto-splits) |

## LaTeX-git push (write-paper deliver, optional)

In `config/latex-git.env` (gitignored): `LATEX_GIT_URL`, `LATEX_GIT_TOKEN`, and
optional `LATEX_GIT_AUTHOR_NAME` / `_EMAIL`. Pushing outward is an operator-gated
action. `DANUS_GIT_TIMEOUT_SECONDS` defaults to `120`; Git credential prompts
are disabled so a failed unattended delivery exits instead of opening a dialog.

---

Ports and the verify HTTP contract are **pinned** cross-module interfaces
(`../ARCHITECTURE.md` §4) — do not renumber `8091`/`8099` without changing both
ends. See `operations.md` to run the services and `cli-and-tools.md` for the
commands that use these.
