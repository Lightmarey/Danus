# Danus v2 research control

Danus v2 keeps the verified fact graph as the only mathematical truth and adds a
separate control plane for approved targets, proof obligations, research routes,
bounded assignments, checkpoints, and cost attribution. This is the only
runtime model. Older projects must be stopped and converted once with
`danus migrate <project>` before any worker, Gateway, authoring, or dashboard
entry point will use them. Migration indexes existing facts and memories but
does not invent a target, obligation, route, or assignment.

## Create and approve a target

```console
danus new my-project --problem ./PROBLEM.md --roles high:3,xhigh:4
danus target propose my-project --file target.json
danus target diff my-project v0001
danus target approve my-project v0001
```

`target.json` is an object such as:

```json
{
  "statement": "Exact theorem to establish",
  "allowed_assumptions": ["A1"],
  "forbidden_assumptions": ["compactness not present in the problem"],
  "required_conclusions": [
    {"id": "main", "statement": "Exact theorem to establish"}
  ],
  "acceptance": "all required conclusions are closed",
  "out_of_scope": [],
  "fallback_candidates": ["Explicit weaker theorem"],
  "budget": {"max_wall_seconds": 86400, "max_cost_usd": 25}
}
```

Proposing writes an immutable draft into `control/control.sqlite3`. Only `target approve` activates it. Creating
a fallback pauses stale assignments and creates another draft; it never approves
the weaker target.

## Add routes and assign workers

Root obligations are created when the target is approved. Add sub-obligations or
routes from JSON, then bind a worker:

```console
danus obligation add my-project --file obligation.json
danus route add my-project --file route.json
danus assign my-project/high --obligation v0001-main --route moving-spheres \
  --task "Establish the monotonicity bridge"
danus start my-project/high
```

A route records a stable `method_key`, readable `method_title`, `expected_result`, assumptions, input fact IDs,
and optional `novelty_basis` / `fallback_route_ids`. An exact duplicate route is
rejected unless it cites concrete novelty.

Each Codex session is one structured exploration round. A route starts with a
three-round budget, gains two rounds on validated high/medium information gain, enters an independent
audit after two consecutive low-gain reports, and stalls only after a third
low-gain audit. The default per-round timeout is 90 minutes and the route hard
ceiling is 12 rounds. Reusing the same evidence does not extend the round budget twice.
`rounds_used`, `rounds_remaining`, `max_rounds`, and
`round_timeout_seconds` are the canonical runtime and storage names; there is no
separate slice or lease abstraction.

## Fact binding and recovery

A worker stages `claim_role`, `assumptions_used`, `closes_obligation`, and a
one-line 4-80 character `display_title`, then calls `fact_submit_batch`. The
gateway stamps and checks the current assignment scope. The title is stored in
Markdown but is not part of `fact_id`;
the first accepted title wins for duplicate mathematical content. Stale assignments, forbidden or
undeclared assumptions, and tainted predecessors are rejected before verification.

Before adaptive batching, each candidate is a verifiable global-memory entry; the
gateway stamps its current assignment scope alongside a semantic
`verification_goal`. This is durable non-truth,
not a fact. `fact_submit_batch` loads 1-6 such `source_id`s. The bound is only a
safety cap: a semantic group flushes when complete or at round end, including a
singleton. Verdicts, source statuses, fact IDs, traces, and partial rejection
remain per candidate; in-batch predecessor references are forbidden. After an
interruption, pending IDs reappear in `ContextManifest.pending_verification`.

`claim_role` is an MCP-schema enum: `unconditional`, `conditional`,
`counterexample`, or `literature_import`. Ordinary positive lemmas and theorems
use `unconditional` when they rely only on target-contract assumptions and
verified predecessors. `conditional` preserves an additional condition and
cannot close an unconditional obligation; `literature_import` requires
structured external references and an applicability audit.

Use `danus control taint <project> <fact_id> --reason "..."` to append a
non-destructive review marker and stop routes that explicitly depend on the fact.
The gateway's `fact_revoke` action also taints rather than deleting. Formal
removal remains a separate operator review action.

## Read model and budget

```console
danus control rebuild my-project
danus services up dashboard my-project
```

For a local browser launch that preserves the ephemeral governance capability,
use `scripts/open-dashboard.ps1 -Project my-project` on Windows or
`scripts/open-dashboard.sh my-project` on POSIX. Add `-NoOpen` or `--no-open`
to print the launch URL without opening a browser. Opening only
`http://127.0.0.1:8099/` intentionally gives no Approve/Withdraw capability.
Omit the project argument to list configured projects and choose one by number.

`control/control.sqlite3` is the transactional authority for targets,
obligations, routes, assignments, events, and the outbox. Verified Markdown in
`fact_graph/facts/` remains the mathematical authority. Fact indexes, scopes,
checkpoints, obstacles, and FTS5 live in the same database but are rebuildable.
Existing pre-SQLite control files are imported once and retained only as a
migration source. Older projects use the explicit `danus migrate` data
conversion; no old worker runtime remains.

Gateway tools, worker kickoff, write-paper, and the dashboard all use
`ResearchQuery`. A worker round receives a persisted `ContextManifest` containing
stable route facts, dependency closures, open obstacles, recent checkpoints, and
a bounded set of search candidates. The default manifest budget is 16,000
characters, at most 20 facts include their statement, and at most three FTS
candidates are included title-only. If an already-scoped fact exactly matches the
obligation, the redundant FTS expansion is skipped. `fact_search` returns a
bounded snippet and v2 `gm_search` omits evidence unless explicitly requested;
proof bodies are opt-in through `fact_get`.
The dashboard reproduces the same snapshot and groups facts as Target → Method →
Route → Theorem Group → Fact. A theorem group is a read-only DAG projection rooted
at one direct predecessor of the route's closing fact; clicking it expands only
that root's bounded predecessor neighborhood. This keeps the control model
unchanged while avoiding an unreadable route-wide fact dump. The underlying fact
roles remain closing/direct/input/support/shared.

Fact Markdown supports KaTeX delimiters (`$...$`, `$$...$$`, `\(...\)`, and
`\[...\]`). The renderer never guesses LaTeX from plain-text mathematics.
Migrated facts therefore keep their authoritative statement/proof unchanged and
should receive readable presentation titles (and, in a later derived display
index, optional `display_markdown`) without changing `fact_id`.

Dashboard Pin, filter, expansion, and comparison state exists only in the
browser's in-memory `Set` and disappears on refresh. The only mutations are
target Approve/Withdraw, protected by an ephemeral launch-URL capability,
Origin checking, request-id idempotency, and generation compare-and-swap.

```console
danus target withdraw my-project v0001 --reason "incorrect target contract"
```

Withdrawal invalidates assignments and leaves no active target; it never revives
an older target automatically.

Target budgets warn at 70%, force an audit at 85%, and block new rounds at 100%.
Worker, verifier, consult, paper, and human-summary calls write scoped `CostEvent`
records. If a backend does not expose metered usage, Danus records wall time and
leaves monetary cost unknown rather than inventing a zero price.

Before an expensive v2 call starts, the controller atomically reserves its full
configured timeout and, when configured, a conservative USD ceiling. Active
reservations participate in the budget thresholds, so concurrent calls cannot
independently spend the same remaining budget. Successful or failed calls settle
their reservation into one `CostEvent`; reservations left by a crashed process
expire during recovery. With `strict_cost_reservations` enabled, a call whose
maximum USD cost cannot be estimated is rejected. If actual usage remains
unknown, the reserved ceiling is recorded as `estimated_ceiling`, never as zero.

A verifier call made inside a worker round is a child reservation: its wall time
is recorded as `nested_wall_seconds` and is not counted a second time against the
project wall budget, while its token and monetary cost remain separate. A round
deadline stops new exploration but drains an already-running child through the
verdict, fact commit, and settlement; the child's own timeout remains the bound.
A real
wall/cost reservation rejection is a typed control event, forces `gain=none`, and
cannot be reframed as medium progress using old evidence. An operator stop
interrupts an active v2 child promptly, settles the reservation, records partial
usage when the provider emitted it (otherwise `usage_status=unavailable`), and
does not consume a research round.

Forced stop and dead-worker restart also run an idempotent reconciliation: live
nested verifier reservations are cancelled before the parent round reservation
is settled, the assignment becomes runnable again, and no research round is
charged. If reservation expiry already ran, persisted active-round status still
records unavailable usage and elapsed wall time. Completed-round idle time is
distinguished by `last_round_at`, so it is not mischarged as an interruption.

Verifier HTTP requests are content-addressed. A dropped connection may put the
assignment into bounded transport retry, but the staged source remains durable;
the same retry request waits for or replays the existing verifier artifact and
token usage rather than launching a duplicate verifier.

Transport and provider failures do not consume route rounds or low-gain counts.
They do consume their real wall time and known or reserved cost. Bounded retries
move an assignment through `waiting_retry`; quota, authentication, configuration,
or exhausted outage limits move it to `infra_blocked`. A provider-wide circuit
prevents retry storms and admits at most one half-open probe after cooldown.
Caller-correctable HTTP 400 validation rejects are the exception: they settle at
known zero monetary cost and do not open the outage circuit, so the next call can
change `background`, `store`, `max_output_tokens`, effort, tools, or model.
Once an external outage is repaired, an operator can explicitly reopen that path:

```console
danus control retry-backend my-project --provider codex --reason "quota renewed"
```

This command only resets infrastructure retry state and records the reason. It
does not change the target, obligations, routes, facts, or mathematical progress.
See `configuration.md` for limits and `operations.md` for recovery procedures.
