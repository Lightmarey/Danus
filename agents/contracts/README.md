# contracts/ — agent root contracts

The standing system prompt each agent **tier** reads at the top of every session —
the binding operating protocol, distinct from the on-demand skills under
`agents/skills/`. These are data (markdown), not code.

| File | Tier | Reads / writes |
| --- | --- | --- |
| `main_agent.md` | Codex main agent | reads global memory + fact graph; writes `master_guidance` / `elaboration` (`gm_add`); `fact_revoke`; high-autonomy orchestration. NO `fact_submit`. |
| `worker.md` | controlled worker | compact assignment-bound contract; uses the supplied ContextManifest and expands facts only on demand. |
| `verifier.md` | codex verifier (verify service) | judges `{statement, proof}` → strict verdict; called by `fact_submit`; bounded read-only fact/glossary/literature tools; writes its verdict JSON directly to results/{run_id}/verification.json. |

Codex auto-loads the condensed repo guidance from root `AGENTS.md`;
`main_agent.md` is the full contract and single source
of truth (the two must not contradict).

## The shared spine

Consistent across all three tiers:

- **The fact graph is the one source of truth** — a content-addressed DAG of
  verifier-accepted facts.
- **A fact enters only through `fact_submit`** (verifier-gated).
- **The verifier is the sole authority on correctness** — `correct` iff zero
  `critical_errors` AND zero `gaps`; no peer/LLM opinion substitutes.
- **Global memory** (incl. `master_guidance`) is shared awareness/strategy, never
  a correctness source — a proof builds only on `fact_id`s.
- **The shared stores change only through the sanctioned MCP tools**, never by hand.

## Who binds to these files

- `danus/gateway` — the exact MCP tool set + role gating (`main` has no
  `fact_submit`; `worker` adds it; `verifier` has bounded read-only
  `fact_get`, `glossary_get`, and `search_arxiv_theorems`).
- `danus/core` — the three-memory data model, the global-memory `kind`s, `fact_id`,
  the global glossary. The contracts are the human-readable statement of that model.
- `danus/verify` — `verifier.md` **is** the verify service's system prompt; its
  P1/P3/P5/P6 prohibitions pair with the server's single-line prechecks (and are
  the sole enforcement wherever those prechecks are off).
- `danus/execution` — links every worker home's `AGENTS.md` to the single
  assignment-bound `worker.md` contract.
- `agents/skills/worker` & `agents/skills/verify` — the contracts reference `$…`
  skills by name; inherited skills defer to this data model.
