---
name: initialize
description: First-run Codex setup for a native Danus deployment. Use when runtime/.danus-initialized is absent, OPERATOR.md is blank, or the operator asks to initialize or reconfigure.
---

# Initialize Danus

Set up the repository from its root and leave the required verifier healthy.
Codex is the only required agent runtime.

## 1. Inspect before asking

Run:

```powershell
uv sync
uv run danus-doctor
git branch --show-current
codex login status
```

Read `OPERATOR.md` and check whether `config/danus.env`,
`config/codex.env`, and `runtime/.danus-initialized` already exist. Do not ask
again for values that are already recorded.

## 2. Ask for operator choices

Use a structured question tool when one is available. Otherwise ask concise
questions in ordinary Codex conversation and wait for the answers.

Collect:

- How to address the operator, their reply language, and timezone.
- Codex authentication: an existing ChatGPT login or an API configuration kept
  in `config/codex.env`.
- Strategy consultation: `gpt_pro` or `off`.
- A warning ceiling in USD when `gpt_pro` is selected.
- The deployment branch name if the current branch is `main`.

`claude_api` and `claude_code` are optional external strategy providers. Mention
them only when the operator asks for them; never require either for setup.

## 3. Persist without overwriting

- If currently on `main`, create the operator-approved branch.
- If a local environment file is absent, copy its matching `.example` file with
  a filesystem operation that refuses to replace an existing destination.
- Store secrets only in `config/*.env`.
- Update `OPERATOR.md` in place with the confirmed profile, consultation choice,
  spending ceiling, and default worker roster. Avoid duplicate entries.
- Set the selected values in the local environment files without replacing
  unrelated existing settings.

For ChatGPT authentication, run `codex login status`. If no valid login exists,
run `codex login` and let the operator complete the displayed authorization. For
an API backend, record the operator-supplied endpoint, model, and key only in
`config/codex.env`, then run:

```powershell
uv run danus codex api
```

This writes the keyless Codex `model_provider` configuration under the
gitignored Danus runtime. The API key remains only in `config/codex.env`. Use
`uv run danus codex status` to inspect either backend; use
`uv run danus codex login` to switch to ChatGPT authentication.

## 4. Verify the required runtime

Run:

```powershell
uv run danus-doctor
uv run danus services up verify
uv run danus services test
uv run danus services status
```

If consultation is `gpt_pro`, make one short bounded check only after the
operator has approved its cost:

```powershell
uv run consult --file <short-prompt.md> --project <project-dir> --out <reply.md>
```

Accept the check only when the returned envelope is completed and the reply is
non-empty. With consultation set to `off`, skip this check.

Report failures exactly and leave initialization incomplete until required
checks pass. Optional paper and report rendering dependencies may remain
warnings.

## 5. Mark initialized and hand off

After the verifier test succeeds, create `runtime` if necessary and write the
current UTC ISO-8601 timestamp to `runtime/.danus-initialized` using Python or a
Codex filesystem operation. Do not replace an existing marker during routine
initialization.

Summarize the selected Codex authentication and consultation mode, confirm the
verifier state, and ask for the mathematics problem. Do not commit or push unless
the operator explicitly requests it. Never commit `config/*.env` or `runtime/`.
