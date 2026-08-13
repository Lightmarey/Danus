# danus/execution — the worker swarm (round loop + scaffolding + layout)

Where `codex` workers actually prove. This module owns the on-disk
**layout**, project/worker **scaffolding**, and the per-worker **round loop**. The
`danus` CLI (`danus/orchestration`) is a thin UX layer over this; the real lifecycle
lives here.

```
danus/execution/
  layout.py     paths + names; WorkerLayout; parse_roles("high:3,xhigh:4")
  scaffold.py   do_new (project + worker dirs, .codex config, symlinks), spawn_loop
  loop.py       bounded round loop: context, WorkReport, stop conditions, status
  __main__.py   `python -m danus.execution <worker_dir>` → loop.main
  tests/{test_execution.py, test_loop.py}
```

## On-disk layout (`layout.py`)

`<agents_root>/<project>/` holds the shared `global_memory/` + `fact_graph/` +
`project.json`; each `workers/<worker>/` is a codex cwd with `AGENTS.md` →
`agents/contracts/worker.md`, `.agents/skills` → `agents/skills/worker`, a
`.codex/config.toml` (MCP = `python -m danus.gateway`, `DANUS_ROLE=worker`,
`DANUS_VERIFY_URL`, `tool_timeout_sec=3600`), `TASK.md`, `local_memory/`, and the
control files (`.status.json` `.pid` `.stop` `logs/`). `agents_root` =
`DANUS_AGENTS_ROOT` (default `runtime/projects`).

## The round loop (`loop.py`)

A **round = one structured `codex exec` session** bound to an approved target,
obligation, route, assignment epoch, and finite round budget. It is launched detached in
its **own process group**
(`start_new_session`), so it survives your shell and `stop --force` can `killpg` the
loop + its codex child. The assignment supplies the round timeout and route cap;
`.stop` and `.run_deadline` remain hard stops. The controller scores each
WorkReport and decides whether to renew, audit, fall back, pause, or complete.
`.status.json` is written atomically. Resumability comes from SQLite control state
and the verified fact graph, not process state.

The round timeout stops exploration. If the worker is blocked in `fact_submit` or
`fact_submit_batch`,
the loop drains that one verifier through fact commit and cost settlement, then
ends the timed-out round. An explicit stop still interrupts immediately.

## Connects to

Projects require an approved target and a structured assignment before they can
start. Unmigrated projects are rejected with `danus migrate <project>`. Workers
write facts only through the gateway's submission tools (gateway → verifier); the loop itself never
writes mathematical truth.

## Tests

`python -m pytest danus/execution/` (offline; a fake codex stub drives the loop /
stop / scaffolding).
