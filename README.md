# Danus: Orchestrating Mathematical Reasoning Agents with Fact-Graph Memory

<p align="center">
  <a href="https://arxiv.org/abs/2607.06447"><img src="https://img.shields.io/badge/arXiv-2607.06447-b31b1b" alt="Danus paper on arXiv"></a>
  <a href="https://frenzymath.com/blog/danus/"><img src="https://img.shields.io/badge/Technical%20Report-frenzymath.com-1f6feb" alt="Technical report"></a>
  <a href="https://github.com/frenzymath/Rethlas"><img src="https://img.shields.io/badge/Rethlas-GitHub-181717?logo=github" alt="Rethlas on GitHub"></a>
  <a href="https://www.xiaohongshu.com/discovery/item/6a4da1ba00000000070201ef?source=webshare&xhsshare=pc_web&xsec_token=ABfiiMB7yyB-dW_hMzh3MW7ZRG2ddm5in_wBnBALXO6DE=&xsec_source=pc_share"><img src="https://img.shields.io/badge/rednote-%E5%B0%8F%E7%BA%A2%E4%B9%A6-FF2442?logo=xiaohongshu&logoColor=white" alt="rednote (Xiaohongshu) post"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-4c1" alt="Apache 2.0 license"></a>
</p>

Danus orchestrates mathematical reasoning agents with fact-graph memory. A
Codex main agent steers a swarm of autonomous Codex workers that prove; a
cold-start verifier is the sole authority on correctness: a result becomes real
only once it passes. Verified results accumulate in a content-addressed fact
graph — the system's only source of truth — and a strategy loop (a strong
reasoning model) decomposes the problem and steers the swarm. When you have the
answer, Danus renders it into a human report or a publishable LaTeX paper.

Danus builds on the worker–verifier core of our earlier system
[Rethlas](https://github.com/frenzymath/Rethlas)
([arXiv:2604.03789](https://arxiv.org/abs/2604.03789)). The
[paper](https://arxiv.org/abs/2607.06447) and the
[technical report](https://frenzymath.com/blog/danus/) tell the full story:
the system, six research-level case studies it resolved, and what we learned
along the way.

See `ARCHITECTURE.md` for the layered design and the map of every module. New v2
projects use versioned research control, bounded provider retries, and atomic call
budget reservations; see `docs/research-control-v2.md`, `docs/configuration.md`,
and `docs/operations.md` for the contract, defaults, and recovery workflow.

## How it works

<p align="center"><img src="docs/assets/architecture.png" width="820" alt="Danus architecture: a main agent orchestrates a worker swarm; a stateless verifier gates every fact; global memory and the fact graph are the shared storage"></p>

The design follows a strict separation of powers: the main agent performs the
global planning and coordination, the workers carry out the detailed proof
search, the verifier is the sole authority on correctness, and the fact graph
holds every verified result and is the system's only source of truth.

Each kind of agent carries out its role through its own skills and its own
role-gated set of tools, so the separation is enforced by construction, not by
prompts: the main agent has no `fact_submit` (the agent that steers the search
structurally cannot introduce unverified mathematics into the fact graph), and
the verifier writes nothing at all.

<p align="center"><img src="docs/assets/agent-tools.png" width="820" alt="The three kinds of agent, each with its own skills and its own role-gated set of tools: the main agent orchestrates and renders, the workers prove and submit, the verifier only reads"></p>

Every claim enters truth through one cycle:

<p align="center"><img src="docs/assets/verify-loop.png" width="820" alt="The submit–verify–repair cycle: a worker submits a statement and proof citing existing facts; a fresh verifier instance accepts it into the fact graph or rejects it with repair hints"></p>

A worker typically focuses on one claim at a time — a lemma, a counterexample, a
toy example — rather than an entire proof. Candidates are staged durably before
verification. The verifier can judge one candidate or a semantic theorem group of
up to six in one cold Codex process; every candidate still receives its own verdict
and fact identity, and accepted siblings do not make rejected ones true. A worker
then repairs only the rejected claims. Because workers receive a bounded database
snapshot rather than every earlier proof, the working context stays small even as
many workers' contributions accumulate into one shared structure.

The graph below is the fact graph of a real research run: **3,157 verified facts
and 8,616 dependency edges**, in dependency chains up to 54 facts deep (nodes
darken and grow with dependency depth). The search was far broader than the proof
it left behind: 664 facts form the supporting closure of the final theorem, and
the clusters are separate lines of attack — among them conditional scaffolding
that the final proof never cites, and an independent re-derivation of one of its
bounds.

<p align="center"><img src="docs/assets/fact-graph.png" width="440" alt="The fact graph of a real run: 3,157 verified facts and 8,616 dependency edges, nodes darkening and growing with dependency depth"></p>

## V2 engineering: stable long runs with bounded context

V2 treats agent processes as disposable and mathematical state as durable. The
fact graph remains the correctness source; SQLite is the transactional control and
query layer around it.

| Goal | Engineering implementation | Result |
|---|---|---|
| **Recover safely** | `control/control.sqlite3` stores versioned targets, obligations, routes, assignment epochs, checkpoints, obstacles, events, call reservations, and provider-circuit state. Pending candidates are staged in global memory before verification; verifier requests use content-derived IDs and replay completed results after a lost response. | A crash, forced stop, timeout, or disconnect does not turn process state into mathematical truth or require rediscovering queued work. Stale workers cannot write into a newer assignment. |
| **Make large runs visible** | Accepted Markdown facts are indexed into rebuildable SQLite fact, edge, scope, checkpoint, obstacle, and FTS5 tables. One snapshot-aware `ResearchQuery` serves MCP reads, the dashboard, authoring, and worker context. | Visualization and route inspection query indexed graph slices instead of reparsing thousands of Markdown files. Every view refers to one database generation, so facts, edges, routes, and failures stay consistent. |
| **Spend fewer input tokens** | A `ContextManifest` follows database edges from the current obligation and route, enriches only a bounded number of relevant facts with statements, leaves the rest title-only, and expands proofs only on request. The database supplies predecessor, scope, distance, and search relations rather than asking the LLM to rediscover them. | Long proofs grow in storage without being copied into every round. Workers see the local support closure and a few FTS candidates, not the whole fact graph. |
| **Amortize verification** | Candidates with the same durable `verification_goal` can be sent through `fact_submit_batch` as a 1–6 fact theorem group. One cold verifier reads the shared context once, then returns ordered per-fact reports; partial acceptance, per-fact persistence, and fail-closed schema validation remain mandatory. | Closely related lemmas share verifier input and startup cost without weakening the fact-level correctness boundary. The bound is a safety cap, not a fixed wait threshold. |
| **Control failure** | Provider errors use persistent bounded retry and a shared circuit breaker without consuming a mathematical round. Repeated low-information rounds trigger an independent audit; continued failure stalls or activates an already-approved fallback route. Rejected facts become durable obstacles with repair hints. Target fallback is drafted for human approval. | The system neither loops forever nor silently changes the theorem. It resumes useful work, avoids known dead ends, and reports the best verified state when progress is exhausted. |

Expensive calls reserve wall-time and optional cost before they start, then settle
against measured Codex usage. A round timeout lets an in-flight verifier finish its
fact commit and accounting; an explicit operator stop cancels immediately. This
keeps concurrency available while preventing orphaned verifier results or invisible
token spend.

Writing uses the same indexed graph rather than loading the entire proof corpus. The
database computes the target's predecessor closure and returns a compact skeleton;
the main agent selects the load-bearing facts, and the writer receives those facts
in full plus the direct predecessor statements it must cite. The assembled paper is
then verified again as one document.

## Layout

```
danus/                 the engine (installable Python package)
  core/                truth layer: content-addressed fact graph + typed memory + schema
  gateway/             role-gated MCP server — the only door to the truth stores
  verify/              cold-start proof-verifier HTTP service (the sole write-gate)
  execution/           worker swarm: the autonomous per-worker round loop + scaffolding
  orchestration/       the `danus` CLI verbs (list/new/assign/start/status/stop)
  strategy/            consult gateway (elaboration → strong model → master_guidance)
  integrations/        arXiv theorem search
  observability/       read-only dashboard
  authoring/           shared one-shot isolated-codex driver for the two renderers below
  write_paper/         write-paper MCP service (fact graph → publishable LaTeX paper)
  human_summary/       human-summary MCP service (fact graph → progress-report PDF)
agents/                isolated worker/verifier contracts and generated-home skills
.agents/skills/        canonical main-agent skills and writing/report assets
bin/ scripts/ config/  runtime layer (wrappers, bootstrap/services/doctor, env templates)
docs/                  human docs: getting started · concepts · operating guide · security & trust · …
examples/              unattended-ops examples + a toy project
```

## Quickstart

```powershell
# 1. create the Python environment (native Windows, macOS, or Linux)
uv sync

# 2. configure — copy the templates and fill in YOUR keys (never committed)
Copy-Item config/danus.env.example config/danus.env
Copy-Item config/codex.env.example config/codex.env

# 3. static prerequisites, Codex authentication, and required verifier health
uv run danus-doctor
uv run danus codex status
uv run danus services up verify
uv run danus services test

# 4. start Codex rooted at this repo; AGENTS.md and .codex/config.toml load here
codex
```

On POSIX hosts the existing `scripts/*.sh` wrappers remain available, but are
not required for service management.

Everything runs on your own keys (BYO). Main, workers, verifier, and writers use Codex.
The strategy consult defaults to
`gpt_pro` (paid) or `off`. Optional `claude_api` and `claude_code` transports
remain available but are not required by the core path.

**Notes**

- **Settle the stopping condition with the main agent before you start.** By
  default the main agent keeps the swarm running until every target is proved and
  stops it on its own once they are (a hard or slow problem is not a reason to
  stop). Talk through what "done" means for your problem at the outset, so the swarm
  does not keep spending tokens past the point you cared about.

- **Give the writing system a few exemplar papers.** Out of the box, `write-paper`
  produces a complete, compilable paper, but the prose can read like a stack of
  verified facts. In our experience the single highest-leverage fix is to provide
  a few papers of your own as exemplars when you ask for the write-up — the writer
  imitates them, and readability improves substantially.
- **Build artifacts natively on Windows or POSIX.** `uv run danus artifacts paper
  compile <main.tex>` uses `latexmk` when available and applies the strict compile
  gate. For reader reports, run `uv run danus artifacts summary doctor`, explicitly
  install the locked Node dependencies once with `summary install-deps`, then use
  `summary render <report.md> <report.pdf>`.

## Design invariants (see ARCHITECTURE.md §3)

- Three memory tiers, one correctness boundary: only the verifier-gated fact graph
  is truth; global memory is awareness.
- Permission is enforced by the MCP role table (main has no fact-submission tools;
  the verifier is read-only).
- Content-addressed, cascade-revocable facts; the verifier is the sole write-gate.
- The finished paper is itself re-verified as written (a dedicated paper-math
  verifier reads the whole document) before delivery, on top of the per-fact
  verification.
