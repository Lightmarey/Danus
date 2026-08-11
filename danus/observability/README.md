# danus/observability — research console

A single **FastAPI** app that shows one project's research map, bounded fact DAG,
global-memory channels, and cost. All fact/control reads use `ResearchQuery`
over SQLite. The only mutations are secured target approval and withdrawal.

```
danus/observability/
  app.py        FastAPI queries, target commands, and static mount
  __main__.py   `python -m danus.observability --project <dir> [--port 8099]`
  static/       index.html + app.js (echarts / KaTeX / markdown-it, CDN, no build step)
  tests/{test_observability.py, test_observability_main.py}
```

## Endpoints

- `GET /api/overview` — counts, per-channel totals, verdict split, consult cost
- `GET /api/research/map`, `/routes/{id}`, `/obligations/{id}` — layered research state
- `GET /api/research/facts/{id}` and `/neighborhood` — opt-in detail and ≤300-node graph
- `GET /api/research/context-manifests` — exact persisted LLM snapshots
- `GET /api/channels` — per-kind counts
- `GET /api/channel/{kind}` — entries newest-first (unknown kind → 404)
- `GET /` → `static/index.html`; `/static/*` mounted
- `POST /api/control/targets/{version}/approve|withdraw` — capability, Origin,
  request-id, and expected-generation protected

## Binding & safety

Binds **`127.0.0.1:8099`** by default (loopback — expose via SSH port-forward, never a
public interface). Project dir resolved at call time from `--project` /
`DANUS_DASHBOARD_PROJECT` / `DANUS_PROJECT_DIR`; fails fast at launch if absent.
Startup prints a URL whose fragment contains a temporary control capability. The
page removes the fragment and holds the token only in memory. Pin/filter state is
also memory-only and never generates an HTTP write or control event.

## Gotcha

`CHANNELS` is a hand-maintained display ordering for the 11 memory kinds. If
`GLOBAL_KINDS` changes, re-sync it.

## Tests

`python -m pytest danus/observability/` (offline; TestClient over the routes; the
CDN browser assets are not exercised — do a one-time manual browser check once
deployed).
