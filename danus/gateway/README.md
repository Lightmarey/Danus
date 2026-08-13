# danus/gateway — role-gated MCP server (the permission gate)

The **only sanctioned door to the truth stores.** A stdio MCP server (`danus-core`)
whose exposed tools depend on the caller's role — permission is enforced by *which
tools a role can even see*, not by prompt convention.

```
danus/gateway/
  server.py            MCP tools + verifier-gated fact writes; build_app(role)
  roles.py             ROLE_TOOLS — the role→tools table (the security surface)
  __main__.py          `python -m danus.gateway` → build_app().run() (role from DANUS_ROLE)
  tests/test_gateway.py
```

## The role table (`roles.py`)

| role | tools |
|---|---|
| worker | shared reads/writes plus `fact_submit_batch` |
| main | orchestration reads/writes and `fact_revoke` (**no fact submission**) |
| verifier | `search_arxiv_theorems` only (read-only) |

Ungated tools are **physically absent** from the surface. Unknown, mis-typed, or
*unset* role → **fail-closed** to the verifier set; the full dev set requires the
explicit `DANUS_ROLE=all`.

## The write-gate (`fact_submit` / `fact_submit_batch`, in `server.py`)

The single path a fact enters truth: (1) call the verify service
(`DANUS_VERIFY_URL`); (2) **write the fact iff `verdict == "correct"`**; (3) **always**
trace the verdict to global memory. Service unreachable / invalid body → clean error,
nothing written. The verify service enforces the strict verdict/report contract.

`fact_submit_batch` loads 1-6 durable verifiable global-memory `source_id`s that
share an exact assignment scope and `verification_goal`, then uses one cold
verifier process (or the single endpoint for a singleton). The bound is a safety
cap, not a wait threshold. It preserves a separate verdict, source status, trace,
and content-addressed `fact_id` for every candidate, records Codex usage once,
and permits partial acceptance. Candidates may cite existing facts but not one
another in the same batch.

## Launched by

`.codex/config.toml` (role=main, via `uv run danus-mcp`); each worker's
`.codex/config.toml` (role=worker); the verify launcher injects it (role=verifier) so
the judge can call `search_arxiv_theorems`. Config (`DANUS_PROJECT_DIR`,
`DANUS_AGENTS_ROOT`, `DANUS_VERIFY_URL`, role, author) is read at **call time**.

## Pinned interfaces (ARCHITECTURE §4 — change both ends together)

The role table; `python -m danus.gateway` launch; the verify HTTP seam.

## Tests

`python -m pytest danus/gateway/` (offline; the verify call is stubbed).
