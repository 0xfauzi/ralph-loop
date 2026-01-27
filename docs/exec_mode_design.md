# Ralph Goal-driven `exec` Mode: Design and Implementation Spec

## Summary

Add a goal-driven workflow to Ralph that:

1. Starts from a user-stated goal.
2. Runs an interactive planner to produce a strict-schema `scripts/ralph/prd.json`.
3. Optionally runs `ralph understand` based on clear triggers, with explicit user consent.
4. Asks for explicit user consent before starting `ralph run`.

This is a composition layer on top of existing Ralph commands. It must not change the PRD schema and it must not auto-start code changes.

## User flow diagrams

### `ralph plan "<goal>"`

```text
User
  |
  |  ralph plan "<goal>"
  v
Preflight
  - Ensure scripts/ralph/ exists (init if missing)
  - Load or create plan_state.json and plan_transcript.md
  |
  v
Collect context (loop)
  - Ask targeted questions (free-text answers)
  - Persist Q/A to plan_state.json and plan_transcript.md
  |
  v
Draft PRD (loop)
  - Agent returns envelope JSON (may include prdDraft)
  - Validate prdDraft with PRD.validate_schema
  |
  +--> invalid PRD: show errors -> ask agent to correct -> back to Draft PRD
  |
  v
Review gate (loop)
  - Show PRD summary (branchName, stories, priorities, verify commands in AC)
  - Prompt: "Write this PRD to scripts/ralph/prd.json?" (default: no)
  |
  +--> No: ask what to change -> back to Collect context
  |
  v
Write PRD + exit
  - Write scripts/ralph/prd.json
  - Record approval in plan_state.json and plan_transcript.md
```

### `ralph exec "<goal>"`

```text
User
  |
  |  ralph exec "<goal>" [--understand auto|always|never]
  v
Run planner
  - Executes the full `ralph plan` flow
  |
  +--> If PRD not approved: stop (no understand, no run)
  |
  v
Understand decision (optional)
  - Compute triggers (missing map, stale map, planner uncertainty)
  - Determine max understand iterations:
      - auto: unchecked Next Topics count (fallback: default topics in understand_prompt.md)
  - Prompt: "Run ralph understand now?" (default: no), include why and iteration cap
  |
  +--> No: skip understand
  |
  +--> Yes: run ralph understand (read-only except codebase_map.md)
  |
  v
Run decision (optional)
  - Determine max run iterations:
      - auto: number of failing PRD stories (passes=false)
  - Prompt: "Start ralph run now?" (default: no), include iteration cap
  |
  +--> No: exit 0 and print next commands
  |
  +--> Yes: run ralph run (normal behavior)
```

## Background (current Ralph behavior)

Ralph today supports:

- `ralph init`: scaffolds `scripts/ralph/` (prompt, PRD, progress, understand prompt, codebase map).
- `ralph run`: executes the agentic loop using `scripts/ralph/prompt.md` and `scripts/ralph/prd.json`.
- `ralph understand`: executes a read-only understanding loop that may only edit `scripts/ralph/codebase_map.md`.

The `run` and `understand` commands are "iteration based" and only prompt the user at the end of an iteration when `--interactive` is set.

## Goals

- Provide `ralph plan "<goal>"` to help the user produce a strict-schema `scripts/ralph/prd.json` via an interactive question and answer loop.
- Provide `ralph exec "<goal>"` to run `plan`, then optionally `understand`, then optionally `run`, with explicit user approvals at each stage.
- Keep `scripts/ralph/prd.json` strict and unchanged.
- Never start `ralph run` without explicit user approval.
- In `--understand auto`, prompt the user when understand is recommended, and include the reason(s).
- Before the PRD is approved, do not edit non-Ralph project files.

## Non-goals

- PRD schema changes or a "PRD v2".
- Replacing `ralph run` or `ralph understand`. `exec` is orchestration only.
- Autonomous "hands off" implementation runs without explicit user opt-in.
- Building a full MCP server as part of this work (separate design exists in `docs/mcp_server_design.md`).

## Terminology

- Goal: the user provided natural language statement, for example: "implement a new API with authz".
- Planner mode: interactive workflow that gathers context and produces a strict-schema PRD.
- Understand mode: existing `ralph understand` loop, read-only except `scripts/ralph/codebase_map.md`.
- Exec mode: orchestration workflow that runs plan, then optionally understand, then optionally run.
- Stale codebase map: `scripts/ralph/codebase_map.md` whose last dated entry is older than today.

## Command surface

### `ralph plan "<goal>"`

Purpose: interactively produce or update `scripts/ralph/prd.json` without starting implementation.

Key properties:

- Creates `scripts/ralph/` scaffolding if missing (same behavior as `ralph init`).
- Writes planner artifacts only under `scripts/ralph/` until the PRD is explicitly approved.
- Outputs a schema-valid PRD to `scripts/ralph/prd.json` only after explicit confirmation.

Proposed options (minimal, align with existing CLI):

- `--root PATH`: repo root (defaults to current directory, same resolution rules as `run`).
- `--agent-cmd TEXT`, `--model TEXT`, `--reasoning TEXT`: same as `run`.
- `--ui auto|rich|plain`, `--no-color`, `--ascii`: same as `run`.
- `--non-interactive`: do not prompt, error out if required information is missing.
- `--resume`: resume from `scripts/ralph/plan_state.json` if present.

### `ralph exec "<goal>"`

Purpose: run the full workflow: plan, optional understand, optional run.

Key properties:

- Runs `plan` first and hard-stops if the PRD is not approved.
- In `--understand auto`, it determines if understand is recommended, asks the user explicitly, and includes why.
- Hard-stops before implementation unless the user explicitly approves starting `ralph run`.

Proposed options:

- All `plan` options.
- `--understand auto|always|never`:
  - `auto` (default): recommend understand based on triggers (missing map, stale map, planner uncertainty).
  - `always`: always prompt to run understand (still requires explicit user consent).
  - `never`: never prompt to run understand.
- `--max-understand-iterations auto|INT`: forwarded to `ralph understand`.
  - Default: `auto`, computed from the number of unchecked topics in `scripts/ralph/codebase_map.md`.
  - Fallback: if no checklist can be found, compute from the default topic list in `scripts/ralph/understand_prompt.md`.
- `--max-run-iterations auto|INT`: forwarded to `ralph run`.
  - Default: `auto`, computed from the number of failing user stories in `scripts/ralph/prd.json` (`passes=false`).

## File layout and write rules

### Strict PRD (unchanged)

`scripts/ralph/prd.json` remains the single source of truth for feature execution. It must validate against the existing strict schema:

- Top-level keys: exactly `branchName`, `userStories`.
- Each story keys: exactly `id`, `title`, `acceptanceCriteria`, `priority`, `passes`, `notes`.

### Planner artifacts (new)

These files exist to keep PRD strict while enabling interactive planning:

- `scripts/ralph/plan_state.json`: machine-readable state for resuming the planner.
- `scripts/ralph/plan_transcript.md`: human-readable transcript of planner interactions and decisions.

Optional (only if needed):

- `scripts/ralph/plan_context.md`: extra context the user provided that must not live in PRD. This is not required if `plan_state.json` and `plan_transcript.md` are sufficient.

### Write safety invariants

Hard rules:

- Before PRD approval: `ralph plan` and `ralph exec` may only write under `scripts/ralph/`.
- Understand mode remains read-only except `scripts/ralph/codebase_map.md`.
- Exec mode must not start `ralph run` without explicit user consent.

## Planner mode: state machine

Planner mode is implemented as a resumable state machine, not as a nested Ralph loop driven by a separate "meta PRD".

### States

1. `preflight`
2. `collect_context`
3. `draft_prd`
4. `review_prd`
5. `done`

Transitions:

- `preflight` -> `collect_context`
- `collect_context` -> `draft_prd` when the agent indicates it has enough information to draft.
- `draft_prd` -> `review_prd` when a PRD draft validates locally.
- `review_prd` -> `collect_context` if the user requests changes or answers follow-ups.
- `review_prd` -> `done` only when the user explicitly confirms writing `scripts/ralph/prd.json`.

### Preflight details

- Ensure `scripts/ralph/` exists. If missing, create it using the same contents as `ralph init`.
- Load any existing `scripts/ralph/prd.json` and show a summary. Ask whether to overwrite or update.
- Initialize or load `scripts/ralph/plan_state.json`.

### Context collection loop

Goals:

- Gather missing information necessary to write atomic, testable user stories.
- Gather exact verification commands (typecheck, tests, lint if relevant) so acceptance criteria can be executable.

Inputs:

- The goal string.
- Existing `scripts/ralph/codebase_map.md` if present (read-only in planner mode).
- Repo configuration files to infer test commands when possible (planner should still ask the user to confirm).

Planner questions should focus on:

- Scope and non-goals.
- Interfaces: API shape, endpoints, CLI, or UI integration points.
- Authn vs authz boundaries (if relevant).
- Data model changes and migrations (if relevant).
- Error handling expectations.
- Verification commands: exact commands and expected outcomes.

All Q and A must be persisted in `plan_state.json` and appended to `plan_transcript.md`.

### PRD drafting loop

The planner asks the agent for a strict-schema PRD draft and locally validates it.

Hard requirements:

- Agent must output only JSON for the PRD draft (no Markdown, no code fences).
- The CLI must validate the draft using the existing `PRD.validate_schema`.
- If invalid, the CLI must feed back the specific validation errors and request a corrected draft.

### PRD review and approval gate

The CLI must show a concise PRD summary:

- Branch name
- Story count
- Story titles and priorities
- For each story: acceptance criteria count and whether it includes verification commands

Then it must prompt:

- "Write this PRD to `scripts/ralph/prd.json`?" (default: no)

If no:

- Ask the user what to change and return to `collect_context`.

If yes:

- Write `scripts/ralph/prd.json`.
- Record the approval event in `plan_state.json` and `plan_transcript.md`.
- Planner ends successfully.

## Planner agent output protocol

To avoid brittle parsing, the planner agent should return a single JSON object per turn in a stable envelope. The CLI should parse this envelope and decide what to do next.

### Envelope schema (proposed)

The agent response is a JSON object with exactly these keys:

- `questions`: array of strings. Empty if no questions.
- `uncertainties`: array of objects, each with:
  - `topic`: string
  - `reason`: string
  - `evidenceMissing`: string
- `prdDraft`: either:
  - `null` when not ready to draft, or
  - a PRD object matching Ralph's strict schema (top-level only `branchName` and `userStories`).
- `recommendUnderstand`: object with:
  - `shouldRun`: boolean
  - `reasons`: array of strings

### CLI behavior for the envelope

- If `questions` is non-empty: ask each question to the user and persist answers.
- Persist `uncertainties` into `plan_state.json` and include them in the understand recommendation reasoning.
- If `prdDraft` is present: validate it.
  - If valid: proceed to PRD review.
  - If invalid: show errors and ask the agent to regenerate a corrected draft.

## Understand gating (exec mode)

Exec mode only considers understand after PRD approval is complete.

### Triggers

Understand recommendation triggers are:

1. Missing map: `scripts/ralph/codebase_map.md` does not exist.
2. Stale map: last dated entry in `scripts/ralph/codebase_map.md` is older than today.
3. Planner uncertainty: agent returned any `uncertainties` or set `recommendUnderstand.shouldRun=true`.

In `--understand auto`:

- If any trigger is true, the CLI must prompt the user explicitly, including a short explanation of why understand is recommended.
- If no triggers are true, the CLI must not prompt and must continue to the run gate.

In `--understand always`:

- The CLI must always prompt to run understand, even if triggers are false.

In `--understand never`:

- The CLI must never prompt to run understand.

### Staleness parsing

Definition: A codebase map is stale when the last dated entry header is older than today.

Parsing rules:

- Look for lines that match the format: `## YYYY-MM-DD - `.
- Extract `YYYY-MM-DD` and parse as a local date.
- Use the maximum (most recent) date found in the file as the last entry date.
- If no dated entries exist, treat the map as stale.
- Stale condition: `last_entry_date < today`.

### Understand prompt details

When prompting, the CLI must say:

- Whether the map is missing or stale (include last entry date if present).
- Whether the planner is uncertain (include the topics).
- A short recommendation summary from `recommendUnderstand.reasons` when available.
- How many iterations it will run:
  - When `--max-understand-iterations auto`: show the computed iteration count and how it was computed.
  - When set explicitly: show the configured value.

Prompt:

- "Run `ralph understand` now?" (default: no)

If yes:

- Run `ralph understand` using the configured root and agent options.
- Forward `--max-understand-iterations`.
- Preserve understand mode safety: only allow edits to `scripts/ralph/codebase_map.md`.

If no:

- Continue to the run gate.

## Run gate (exec mode)

After planning and optional understand, exec mode must always ask:

- "Start `ralph run` now?" (default: no)
  - Include how many iterations it will run:
    - When `--max-run-iterations auto`: show the computed iteration count and how it was computed.
    - When set explicitly: show the configured value.

If no:

- Exit 0 and print the next commands the user can run manually.

If yes:

- Run `ralph run` with the configured root and agent options.
- Forward `--max-run-iterations`.

## Auto iteration cap computation

The auto iteration caps are meant to match Ralph's "one unit per iteration" prompts:

- In `run` mode, the agent is instructed to implement exactly one failing story per iteration.
- In `understand` mode, the agent is instructed to investigate exactly one checklist topic per iteration.

Auto caps must be overrideable and must be shown to the user before execution.

### Run cap (auto)

Compute:

1. Load `scripts/ralph/prd.json`.
2. Count the number of user stories where `passes` is `false`.
3. Use that count as the `ralph run` max iterations.

If the count is 0:

- Skip running `ralph run` in `exec` mode and report that all stories are already passing.

### Understand cap (auto)

Compute:

1. Ensure `scripts/ralph/codebase_map.md` exists (create default if missing, same as `ralph init`).
2. Parse `codebase_map.md` and count unchecked checklist items under the first `## Next Topics` section:
   - Unchecked item format: `- [ ] `
3. Use that count as the `ralph understand` max iterations.

Fallback:

- If `## Next Topics` is missing, or contains no checklist items, use the number of topics in the default list in `scripts/ralph/understand_prompt.md`.

If the computed count is 0:

- Skip running `ralph understand` and report that there are no remaining topics.

## Error handling and UX requirements

### Interactive requirements

Planner mode requires collecting free-text answers, not only yes or no.

If no TTY is available and `--non-interactive` is not set:

- Fail with a clear error explaining that planner requires an interactive terminal, or that `--non-interactive` must be used with pre-supplied inputs.

### Agent output failures

If the planner agent outputs invalid JSON for the envelope:

- Show a concise error message.
- Retry the agent with an instruction to output valid JSON only.
- Persist the raw invalid output to `plan_transcript.md` for debugging.

If the PRD draft fails strict validation:

- Show the validation errors verbatim.
- Retry agent drafting with the validation errors included.

### Safety failures

If planner attempts to write outside `scripts/ralph/`:

- Treat as an internal error and abort.

If understand writes outside `scripts/ralph/codebase_map.md`:

- This should already be blocked by `ALLOWED_PATHS` enforcement.
- Abort and report the diff and offending paths.

## Implementation guidance (for the agent implementing this)

### Reuse existing code paths

- Use `run_init` logic to create scaffolding when missing.
- Do not reimplement `run_loop`. Call it with an appropriate `RalphConfig`.
- Do not change PRD schema. Use `PRD.validate_schema`.

### Suggested module layout

- `ralph_py/plan.py`
  - `PlanState` dataclass and JSON serialization
  - `plan(goal: str, config: RalphConfig, ui: UI, agent: Agent, root: Path) -> PlanResult`
  - PRD summary formatting
  - Planner agent prompting and envelope parsing
  - Staleness parsing helper
- `ralph_py/cli.py`
  - Add `plan` and `exec` commands
  - Compose `plan`, optional `understand`, optional `run`

### UI additions (required)

Planner needs a free-text prompt. The current UI protocol does not expose one.

Add to `ralph_py/ui/base.py`:

- `ask(self, prompt: str, default: str | None = None) -> str`

Implement in:

- `ralph_py/ui/plain.py`: use `input()` (handle EOF and return default when provided).
- `ralph_py/ui/rich_ui.py`: use `rich.prompt.Prompt.ask` (or prompt_toolkit if already available).

### Plan state file schema (proposed)

`scripts/ralph/plan_state.json` should include:

- `schemaVersion`: integer, start at 1
- `root`: string (absolute path)
- `goal`: string
- `createdAt`: ISO timestamp
- `updatedAt`: ISO timestamp
- `qa`: array of objects:
  - `id`: string
  - `question`: string
  - `answer`: string
  - `askedAt`: ISO timestamp
- `uncertainties`: array of `{ topic, reason, evidenceMissing }`
- `recommendUnderstand`: last seen `{ shouldRun, reasons }`
- `lastPrdDraft`: last PRD object produced by the agent (optional)
- `approvedPrdAt`: ISO timestamp or null

### Planner transcript requirements

Append-only `scripts/ralph/plan_transcript.md` should capture:

- The goal
- Each question and answer
- Each agent envelope (raw JSON) in a code block
- Each PRD summary shown to the user
- The user's approval decision
- Understand recommendation and decision
- Run gate decision

## Testing strategy

Unit tests:

- Staleness parser for `codebase_map.md`.
- Understand trigger logic given combinations of missing, stale, uncertainty.
- PRD strict validation retry behavior when draft is invalid.
- Plan state serialization and resumability.

Integration tests (temp repo):

- `ralph plan` creates only `scripts/ralph/*` artifacts prior to PRD approval.
- `ralph exec --understand auto` prompts when map is missing or stale.
- `ralph exec` never invokes `run` unless the user approves (mock UI).

## Example user flow

1. User runs: `ralph exec "implement a new API on this repo that does authz"`
2. Planner asks targeted questions, writes `scripts/ralph/plan_state.json` and `scripts/ralph/plan_transcript.md`.
3. Planner produces a strict-schema PRD draft, validates it, shows summary.
4. User explicitly approves writing `scripts/ralph/prd.json`.
5. Exec detects `codebase_map.md` is missing or stale or planner is uncertain. Exec explains why and asks to run understand.
6. User decides whether to run understand.
7. Exec asks whether to start `ralph run`. Default is no.
8. If user approves, Ralph executes `ralph run` normally.
