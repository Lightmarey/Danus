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

Proposing writes an immutable draft. Only `target approve` activates it. Creating
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

A route records `method_family`, `expected_result`, assumptions, input fact IDs,
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
`assumptions_used`, and `closes_obligation`. Stale assignments, forbidden or
undeclared assumptions, and tainted predecessors are rejected before verification.

Use `danus control taint <project> <fact_id> --reason "..."` to append a
non-destructive review marker and stop routes that explicitly depend on the fact.
Only the existing operator-approved `fact_revoke` path removes the fact and its
descendants from the active graph.

## Read model and budget

```console
danus control rebuild my-project
danus services up dashboard my-project
```

The rebuild creates `control/read_model.sqlite3` with FTS5 when the local SQLite
build supports it. It is derived entirely from immutable definitions, the event
log, and fact files and can be deleted/rebuilt. The dashboard's **Research
Control** tab shows target versions, obligations, routes, assignments, and costs.

Target budgets warn at 70%, force an audit at 85%, and block new slices at 100%.
Worker, verifier, consult, paper, and human-summary calls write scoped `CostEvent`
records. If a backend does not expose metered usage, Danus records wall time and
leaves monetary cost unknown rather than inventing a zero price.
