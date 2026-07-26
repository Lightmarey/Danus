# Danus — Getting Started

Danus runs natively on Windows, macOS, and Linux. The required path is `uv`,
Python 3.10+, an installed `codex` executable, and Git. LaTeX and Chrome are
optional until you render papers or human-summary PDFs.

## 1. Install and check

From the repository root:

```powershell
uv sync
uv run danus-doctor
```

`uv sync` creates the environment from the checked-in lockfile.
`danus-doctor` checks the static Python, Codex executable, and packaged agent
assets. Authentication and live service health are checked explicitly below.

## 2. Configure

Copy the templates, then put local keys and choices only in the gitignored
files:

```powershell
Copy-Item config/danus.env.example config/danus.env
Copy-Item config/codex.env.example config/codex.env
```

Codex is the only required agent runtime. Configure either its existing ChatGPT
login or the API settings in `config/codex.env`. Strategy consultation can use
`gpt_pro` or `off`; `claude_api` and `claude_code` are optional external
providers and are never required for the core Codex flow.

After setting `CODEX_BACKEND=chatgpt`, use the existing Codex login:

```powershell
uv run danus codex login
```

For `CODEX_BACKEND=api`, fill `CODEX_API_BASE_URL`, `CODEX_API_MODEL`, and
`DANUS_CODEX_API_KEY`, then generate the keyless provider configuration:

```powershell
uv run danus codex api
```

Inspect the active route and run the doctor:

```powershell
uv run danus codex status
uv run danus-doctor
```

## 3. Start and verify services

The verifier must be healthy before workers can submit facts:

```powershell
uv run danus services up verify
uv run danus services test
uv run danus services status
```

Without the verifier, `fact_submit` fails and no fact enters the fact graph.

## 4. Start Codex at the repository root

```powershell
codex mcp list
codex
```

Root `AGENTS.md` supplies the main-agent contract, `.agents/skills/` supplies the
canonical main skills, and `.codex/config.toml` starts the `danus`,
`write-paper`, and `human-summary` MCP servers portably through `uv`.

On the first session, use `initialize`; it records the operator profile and
local settings, ensures the verifier is running, and writes the gitignored
initialization marker. Then follow `operating-guide.md` to create a project.

## Troubleshooting

```powershell
uv run danus-doctor
uv run danus services logs verify
uv run danus services down verify
uv run danus services up verify
```

See `operations.md` for service lifecycle and `configuration.md` for all
environment variables.
