# Danus — Operations Runbook

Day-to-day operation of a Danus deployment: the persistent services, health checks,
recovery after a restart, and unattended-operation helpers.

> In normal operation you do not run these commands yourself — you talk to the
> **Codex main agent**, and it runs them for you. This page documents what
> happens underneath, and doubles as your fallback for the moments the main agent
> is not there to act (a fresh host restart, a session that will not start,
> debugging the stack by hand).

## The persistent services

Use the native Python service manager. It detaches each service with the current
uv Python and works on Windows and POSIX.

```powershell
uv run danus services up verify               # REQUIRED
uv run danus services up dashboard <p>        # optional project dashboard
uv run danus services status [--json]
uv run danus services test [--json]
uv run danus services logs <svc>              # last 50 lines
uv run danus services down verify
uv run danus services down dashboard
uv run danus services down all
```

The POSIX `scripts/services.sh` wrapper remains available for existing
deployments; new cross-platform automation should use `danus services`.

- **verify** — `127.0.0.1:8091`. The correctness gate. Must be up before starting
  any workers.
- **dashboard** — `127.0.0.1:8099`, read-only. View it via an SSH port-forward
  (do not expose it to a network).

> **Shared-host caveat.** These ports are per-host, not per-deployment. If a second
> Danus deployment (another user/checkout) is already bound to `8091`, your
> `danus services up verify` will refuse to start. A bare
> health probe cannot tell your verify from the other one, so `/health` now
> **self-identifies with the serving process pid**: `danus services
> status`/`test` match that pid against your `runtime/run/verify.pid` and report the
> port as **`FAIL … answered by a FOREIGN process`** instead of a false `ok` when
> another deployment holds it. On a shared host, give each deployment its own
> `VERIFY_PORT` / `DASHBOARD_PORT` (`config/danus.env`).

`danus services` keeps exact PID files under `runtime/run/` and an `autostart` manifest
of `up` invocations, so a restart can replay them (see recovery).

## Health checks

```powershell
uv run danus-doctor             # static executable/import/agent-asset checks
uv run danus codex status       # configured backend + real Codex authentication
uv run danus services test      # required verify identity + health only
uv run danus artifacts summary doctor  # optional Node/Chrome report tooling
codex mcp list                  # confirm repo MCP configuration is visible
```

- `danus-doctor` is deliberately static and read-only. It exits nonzero when the
  Python/Codex executable, required imports, or packaged worker assets are absent.
- `danus codex status` is the authentication check. It exits nonzero when the
  selected ChatGPT or API route is not usable.
- `danus services test` exits nonzero unless this deployment's verifier is
  definitively healthy and its process identity matches the retained sidecar.
- LaTeX and report dependencies are optional until artifact generation. Check
  report tooling with `danus artifacts summary doctor`; a paper compile reports a
  missing TeX engine directly.

## Recovery after a host restart

```powershell
uv sync
uv run danus codex status
uv run danus services recover
uv run danus services test
```

The native recovery command validates and replays the
`runtime/run/autostart` manifest. It reuses the normal identity/health checks,
so dead PID evidence is replaced while a foreign or PID-reused live process is
never killed or overwritten. The command is idempotent. `uv sync` remains an
explicit first step so a moved Python installation can rebuild the environment.

> Note: after a restart, worker loops are **not** auto-resumed by recovery — it
> restores the services. Restart workers with `danus start <project>` (they resume
> from persisted memory).

## Worker lifecycle (operational view)

```bash
danus status <project>          # per-worker liveness + round + last activity
danus start  <project>          # (re)launch the worker loop(s); resumes from memory
danus stop   <project>          # graceful: finish the round, then exit
danus stop   <project> --force  # kill the process group now
```

- Workers run detached in their own process groups, so they outlive your session and
  a graceful stop lets an in-flight round finish (no lost verified work). `--force`
  kills a live codex child.
- `status` shows a `stuck?` soft signal when a running round exceeds ~1.5× the hard
  timeout — investigate (often a flaky backend); decide stop/restart.

## Unattended operation (examples, not core)

Under `examples/ops/` (parameterized; nothing in the engine depends on them):

- Start `codex` at the repository root for the main-agent session. Worker loops
  remain detached services managed by `uv run danus`.
- `strategy-loop.sh <project>` — fire a strategy consult on a cadence
  (`DANUS_STRATEGY_BEAT`, default ~2h) when an elaboration is present.
- `watchdog.sh <project>` — probe verify `/health` + parse `danus status`; alarm via
  a generic `DANUS_NOTIFY` hook on a `stuck?`/`dead`/`error` worker or a down verify.

## Common issues

| symptom | check |
|---|---|
| no facts appearing | is `verify` up? `services.sh status`; `doctor.sh` |
| workers erroring in rounds | `check-codex.sh`; `runtime/logs/codex-health.jsonl`; the worker's `logs/round_*.log` |
| a `paper_*` tool came back non-`ok` | read the returned `log_path` (`<project>/paper/.runs/<utc>-<tool>/log.md`) |
| dashboard blank | port-forward `:8099`; is the dashboard service up for that project? |
| after reboot, nothing runs | `recover.sh`, then `danus start <project>` |

See `configuration.md` for the variables that tune all of the above, and
`security-and-trust.md` for the trust assumptions behind the sandbox-bypassed codex
sessions.
