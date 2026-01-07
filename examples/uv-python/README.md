# uv + Ralph example project

This directory is a **minimal uv-managed Python project** (via `pyproject.toml`) that also includes the **Ralph harness** under `scripts/ralph/`.

## Requirements

- `uv` installed (see `https://docs.astral.sh/uv/`)
- Python 3.11+ (uv will manage it if configured)
- Optional: `gum` for prettier CLI output (`brew install gum`)

## Setup

From this directory:

```bash
uv sync
```

Run tests:

```bash
uv run pytest
```

Run the example CLI:

```bash
uv run ralph-uv-example Alice
```

## Ralph (agent loop) in this example

Ralph lives at:
- `scripts/ralph/ralph.sh`
- UI helpers: `scripts/ralph/ui.sh` (uses `gum` if installed; falls back to plain)

Try a **dry run** (just echo the prompt through):

```bash
AGENT_CMD="cat" RALPH_BRANCH="" ./scripts/ralph/ralph.sh 1
```

Try a **fake agent** that immediately completes:

```bash
AGENT_CMD="printf 'hello\\n<promise>COMPLETE</promise>\\n'" RALPH_BRANCH="" ./scripts/ralph/ralph.sh 3
```

Notes:
- This example directory is inside the top-level git repo, so **branch checkout would affect the main repo**. Use `RALPH_BRANCH=""` to disable branch checkout while you experiment.
- The agent instructions live in `scripts/ralph/prompt.md`.
- The PRD lives in `scripts/ralph/prd.json`.

## UI knobs

```bash
# Force plain output (no gum)
RALPH_UI=plain ./scripts/ralph/ralph.sh 1

# ASCII separators (no box-drawing chars)
RALPH_ASCII=1 ./scripts/ralph/ralph.sh 1

# Disable ANSI colors
NO_COLOR=1 ./scripts/ralph/ralph.sh 1
```

## Agent output knobs (Codex)

By default, Ralph prettifies the Codex transcript and **hides the echoed user prompt**
(since it’s just your `scripts/ralph/prompt.md` content repeated in the logs).

```bash
# Lower reasoning effort (faster/cheaper; model-dependent)
MODEL_REASONING_EFFORT=low ./scripts/ralph/ralph.sh 1

# Show the echoed prompt in logs
RALPH_AI_SHOW_PROMPT=1 ./scripts/ralph/ralph.sh 1

# Stream raw Codex output (no transcript prettifier)
RALPH_AI_RAW=1 ./scripts/ralph/ralph.sh 1

# While hiding the echoed prompt, print progress every N suppressed lines
RALPH_AI_PROMPT_PROGRESS_EVERY=25 ./scripts/ralph/ralph.sh 1
```
