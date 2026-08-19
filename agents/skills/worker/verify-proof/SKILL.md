---
name: verify-proof
description: Durably stage related results, verify them with fact_submit_batch, and write each accepted result as a fact. Use for the full target theorem and every sharply-delimited intermediate result intended for downstream use.
---

# Verify Proof

The verifier is the **canonical and sole authority on mathematical correctness**.
Mathematics requires 100% accuracy; even though this verifier is not a formal
proof assistant, it is the strongest correctness check in the system. No LLM
consultation, panel, or self-critique substitutes for it.

**You verify and write facts through durable staging plus `fact_submit_batch`.**
First persist each self-contained candidate with `gm_add`; then submit one to six
staged `source_id`s sharing a semantic `verification_goal`. The size cap is not a
target: flush when that theorem group is complete or the round is ending, even
for a singleton. Each candidate is written to the fact graph **iff its own
verdict accepts it**.

## When to submit

- **The full target theorem** — when you have assembled a complete proof of the
  whole problem (as a self-contained statement + proof, citing its predecessors by
  `fact_id`).
- **Every intermediate result you intend to USE downstream** — a lemma, a
  candidate construction, an arithmetic/closed-form claim, a saturation or
  local-to-global claim, any sharply-delimited step. **Adopting an unverified
  partial result as a building block is the single biggest correctness risk.** When
  in doubt, submit.

Do **not** build on an unverified finding from global memory. A `conclusion` /
`example` / `counterexample` there is awareness, not a brick — re-derive it as a
self-contained statement+proof and submit it before relying on it.

## Before you submit — write an "ugly-proof" fact

A fact in the fact graph is written in **"ugly-but-rigorous"** form (the operator
may call it an **"ugly-proof"**). The one goal of
this form is that the fact is **mechanically checkable for correctness** by a
reader with no memory and no math intuition (an agent with no recall, a human, the
verifier). It is allowed — encouraged — to be **ugly**: redundant, machine-flavored,
verbose. It is **not allowed** to be ambiguous, vague, or context-dependent.
"Ugly" is the deliberate contrast with the polished arXiv paper (a separate
pipeline); here, only mechanical correctness matters.

Concretely, before you submit:

- **Self-contained.** A reader using only this fact + its declared predecessors +
  the project glossary can decide whether the math is correct. No appeal to chart
  positions, parse status, project history, or "as we know".
- **Define every symbol.** Each symbol used in the statement/proof is defined: in
  this fact's `glossary_introduces`, in a cited predecessor's glossary, in the
  project glossary, or in the **global glossary** of universal notation (Z, Q, R,
  C, floor/ceil, gcd/lcm, intervals, Greek parameter names). Don't redefine
  universal notation — `glossary_introduces` is for project-specific symbols only.
  Reuse the project's existing symbol for the same object. Batch results return
  `undefined_symbols` for candidates that missed one.
- **Cite every dependency by `fact_id`** — never "by the result above", never the
  problem statement as a math source.
- **Only load-bearing material.** Put strategy surveys, failed alternatives,
  novelty discussion, and unused related facts in the `WorkReport`, not in the
  candidate proof. Every cited or declared predecessor must be used by a proof
  step needed for the submitted statement.
- **Every quantifier explicit; every introduced parameter (epsilon, k, …) carries
  an explicit range.**
- **No handwave** ("obviously", "easy to see", "routine", "analogously",
  "by some classical argument") and **no chart-position references** ("as above").
- **Avoid duplicates.** `gm_search` the fact graph / global memory (or read
  `fact_graph/facts/`) for an existing fact with the same statement; if one
  exists, cite its `fact_id` instead of re-proving it.

## Stage, submit, and repair

Call `gm_add(kind="proof_attempt"|"conclusion", claim=<statement>,
evidence=<proof>, verifiable=true)` and put `verification_goal`, `display_title`,
`display_summary`, `display_method`, `display_tags`, `predecessors`,
`glossary_introduces`, `intuition`, `external_refs`, `claim_role`,
`assumptions_used`, and `closes_obligation` in `links`. The three additional
display fields are optional and may be empty. Copy every `assumptions_used`
entry exactly from the research-context list; the gateway
stamps the assignment scope. Then call
`fact_submit_batch(verification_goal, candidates=[{"source_id": ...}])`.
When `closes_obligation=true`, keep `claim` self-contained. If the obligation
text is only a short research directive, copy it verbatim into
`links.closure_statement` and put the detailed theorem in `claim`; the gateway
verifies that theorem together with its exact obligation binding. Omit
`closure_statement` only when the self-contained `claim` already equals the
obligation text after whitespace normalization.
`claim_role` must be exactly one of:

- `unconditional` — the default for an ordinary positive lemma or theorem proved
  under the target contract;
- `conditional` — the statement retains an additional explicit condition;
- `counterexample` — the fact refutes a claim by construction;
- `literature_import` — the fact imports a published result with
  `external_refs` and an applicability check.

Do not submit positive theorems with invented roles such as `theorem`, `lemma`,
`positive`, or `result`.

Read each candidate result:

- `accepted: true, fact_id` — the fact is written. **Cite `fact_id`** downstream.
- `verdict: "control_error"` — correct the metadata from the exact research
  context or returned constraint and retry once; this is not a mathematical
  rejection and not a reason to end the round.
- `accepted: false, repair_hints` (+ `undefined_symbols`) — revise only that
  candidate and candidates that truly depend on its invalid step: resolve
  critical errors first, then all remaining gaps; do not assume the fix is local —
  change strategy or backtrack if needed; then resubmit. Treat any `wrong` verdict,
  any critical error, or any gap as failure.
- `verdict: "error"` — the verify service was unavailable; follow the provider
  retry policy, without reclassifying it as a mathematical obstruction.
- `accepted: true, write_error` (e.g. a predecessor was revoked) — the fact was not
  written; re-prove or avoid that predecessor.

Every outcome is auto-logged to global memory, and rejected candidates create a
durable obstacle. After interruption, resume the pending `source_id`s shown in
the next research context instead of re-deriving them.

## The verifier is the only correctness authority

If your own reasoning, the main agent's `master_guidance`, or any other LLM calls
a result correct but the verifier rejects it, the verifier wins. Always. Note the
disagreement (a `dead_end` finding) and treat the "looks correct" opinion as the
unreliable signal it was. A non-verifier opinion (including `master_guidance`) is
for ideas and directions, never for correctness.

## Tools

- `gm_add` (durably stage a self-contained candidate)
- `fact_submit_batch` (the only worker path to verify staged results and write accepted facts)
- `gm_search` (check for an existing fact before submitting; read others' verification outcomes)
- the fact graph is read directly (`fact_graph/facts/`, `glossary.json`)
