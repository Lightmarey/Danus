# Danus v2 research control

Danus v2 keeps the verified fact graph as the only mathematical truth and adds a
separate control plane for approved targets, proof obligations, research routes,
bounded assignments, checkpoints, and cost attribution. Existing projects that
do not declare `"control_version": 2` continue to use the legacy loop.

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

Each Codex session is one structured exploration slice. A route starts with three
slices, gains two on validated high/medium information gain, enters an independent
audit after two consecutive low-gain reports, and stalls only after a third
low-gain audit. The default per-slice timeout is 90 minutes and the route hard
ceiling is 12 slices. Reusing the same evidence does not renew the lease twice.

## Fact binding and recovery

For v2 projects a worker's `fact_submit` must include the current
`target_version`, `obligation_id`, `route_id`, `assignment_epoch`, `claim_role`,
`assumptions_used`, `closes_obligation`, and a one-line 4-80 character
`display_title`. The title is stored in Markdown but is not part of `fact_id`;
the first accepted title wins for duplicate mathematical content. Stale assignments, forbidden or
undeclared assumptions, and tainted predecessors are rejected before verification.

Use `danus control taint <project> <fact_id> --reason "..."` to append a
non-destructive review marker and stop routes that explicitly depend on the fact.
For v2, the gateway's legacy-named `fact_revoke` action also taints rather than
deleting. Formal removal remains a separate operator review action.

## Read model and budget

```console
danus control rebuild my-project
danus services up dashboard my-project
```

`control/control.sqlite3` is the transactional authority for targets,
obligations, routes, assignments, events, and the outbox. Verified Markdown in
`fact_graph/facts/` remains the mathematical authority. Fact indexes, scopes,
checkpoints, obstacles, and FTS5 live in the same database but are rebuildable.
Existing file-backed v2 control state is imported once and retained only as a
migration source; v1 projects are not migrated.

Gateway tools, worker kickoff, write-paper, and the dashboard all use
`ResearchQuery`. A worker slice receives a persisted `ContextManifest` containing
stable route facts, dependency closures, open obstacles, recent checkpoints, and
a bounded set of search candidates. Proof bodies are opt-in through `fact_get`.
The dashboard reproduces the same snapshot and groups facts as Target → Method →
Route → Obligation → Fact with closing/direct/input/support/shared roles.

Dashboard Pin, filter, expansion, and comparison state exists only in the
browser's in-memory `Set` and disappears on refresh. The only mutations are
target Approve/Withdraw, protected by an ephemeral launch-URL capability,
Origin checking, request-id idempotency, and generation compare-and-swap.

```console
danus target withdraw my-project v0001 --reason "incorrect target contract"
```

Withdrawal invalidates assignments and leaves no active target; it never revives
an older target automatically.

Target budgets warn at 70%, force an audit at 85%, and block new slices at 100%.
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

Transport and provider failures do not consume route slices or low-gain counts.
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
