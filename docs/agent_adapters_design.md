# Ralph Multi-Agent Support: Agent Runners and Transcript Adapters

## Summary

Ralph currently has a Codex-specific transcript parser and UI enhancements that do not generalize to other coding agents. This document specifies a pluggable agent and transcript adapter architecture that supports:

- Codex (current default)
- Claude Code (first non-Codex target)
- Arbitrary custom agents (via a command string or adapter plugin)

The design separates:

1. Agent runner: how Ralph invokes an agent (process, API client, etc).
2. Transcript adapter: how Ralph turns the agent output into structured events for UI, tool summaries, and usage tracking.

## Goals

- Keep existing `ralph run` and `ralph understand` flows working with minimal CLI changes.
- Support multiple agent backends without hard-coding Codex-specific parsing in the loop.
- Provide a stable transcript event model that enables:
  - Consistent channel tagging (`AI`, `THINK`, `SYS`, `TOOL`, `PROMPT`)
  - Tool activity summaries (when the agent can provide tool metadata)
  - Usage metrics (tokens) when the agent can provide them
- Allow custom agents to opt into richer UI by emitting a standard transcript format.

## Non-goals

- Perfect parity across all agents. If an agent cannot provide tool or usage events, Ralph must degrade gracefully.
- Converting tokens to dollars without an explicit, user-provided pricing table.
- Implementing agent-side tool execution. Ralph only renders what the agent reports.
- Replacing the completion marker contract (`<promise>COMPLETE</promise>`).

## Current state (problem)

In `ralph_py/loop.py`, Codex is treated specially:

- Codex output is parsed with `CodexTranscriptParser` (`ralph_py/ui/codex_transcript.py`).
- Tool summary and token parsing rely on Codex transcript conventions and regexes.
- Custom agents (`--agent-cmd`) are treated as raw text streams with no structured parsing.

This makes UI behavior inconsistent across agents and prevents clean support for Claude Code or other agent CLIs.

## Proposed architecture

### Concepts

#### Agent runner

An agent runner is responsible for:

- Accepting a prompt string and working directory.
- Producing a stream of output lines (raw) and optionally a final message.

Runners can be:

- Process runners (Codex, Claude CLI, arbitrary shell commands)
- API runners (future)

#### Transcript adapter

A transcript adapter is responsible for:

- Turning raw output lines into structured transcript events that Ralph can render and aggregate.

Adapters are pluggable and selected per run. Codex keeps a Codex adapter. Claude Code gets its own adapter or uses a standard format adapter.

### Transcript event model (canonical)

Define a canonical in-process representation used by Ralph internally:

`TranscriptEvent` (dataclass)

- `type`: one of:
  - `text`
  - `tool_start`
  - `tool_output`
  - `tool_end`
  - `usage`
  - `meta`
- `tag`: for `text` events only, one of:
  - `AI`, `THINK`, `SYS`, `TOOL`, `PROMPT`, `USER`
- `text`: for `text` and `tool_output` events
- `tool`: for tool events:
  - `id`: stable id for the tool call within a run (string)
  - `name`: tool name (string), for example `shell`
  - `input`: dict (optional)
  - `status`: `ok|fail|unknown` (for `tool_end`)
  - `duration_ms`: int (optional)
- `usage`: for `usage` events:
  - `prompt_tokens`: int (optional)
  - `completion_tokens`: int (optional)
  - `total_tokens`: int (optional)
  - `model`: string (optional)
- `meta`: for `meta` events:
  - implementation-defined dict (optional)

Rules:

- Adapters may emit only `text` if they cannot infer tool or usage information.
- `text` events are rendered with `ui.stream_line(tag, text)`.
- Ralph completion detection remains string-based by scanning raw output lines for `<promise>COMPLETE</promise>`.

### TranscriptAdapter interface

`TranscriptAdapter` is a small, line-oriented protocol:

- `feed(line: str) -> list[TranscriptEvent]`
- `flush() -> list[TranscriptEvent]` (optional)

Responsibilities:

- Maintain any necessary state to map the raw line stream into events.
- Be resilient to partial or malformed output: emit best-effort `text` events and record `meta` errors if needed.

### Adapter selection

Add a new config setting and CLI option:

- CLI: `--transcript auto|codex|rtf1|plain|claude`
- Env: `RALPH_TRANSCRIPT`

Selection rules (recommended):

1. If `--transcript` is explicitly set, use it.
2. Else if the agent runner is Codex, use `codex`.
3. Else if the agent runner is Claude, use `claude` (or `rtf1` if the Claude runner emits RTF1).
4. Else use `plain`.

`auto` behavior should be deterministic and documented.

### Capabilities

Ralph features should be capability-based. Example capabilities exposed by the adapter:

- `supports_roles`: can emit `THINK`, `SYS`, etc.
- `supports_tool_events`
- `supports_usage_events`
- `supports_prompt_hiding` (Codex only initially)

Ralph should only render callouts (PLAN, ACTIONS, REPORT) when it has the required events.

## Standard transcript format for custom agents (RTF1)

Custom agents need a way to opt into richer UI without Ralph having to implement a bespoke parser for every agent. Define a standard format: Ralph Transcript Format v1 (RTF1).

### RTF1 transport

- The agent prints normal text as usual.
- Any line that begins with the sentinel prefix `@@RALPH@@ ` is treated as a single JSON object describing a transcript event.
- The JSON object must fit on one line (no newlines).

Example:

```text
@@RALPH@@ {"type":"text","tag":"THINK","text":"Plan: inspect auth layer"}
@@RALPH@@ {"type":"tool_start","tool":{"id":"t1","name":"shell","input":{"cmd":"rg -n authz -S ."}}}
@@RALPH@@ {"type":"tool_end","tool":{"id":"t1","status":"ok","duration_ms":218}}
@@RALPH@@ {"type":"usage","usage":{"prompt_tokens":1234,"completion_tokens":567,"total_tokens":1801}}
```

### RTF1 schema

Each `@@RALPH@@` JSON object must have:

- `type`: string, one of `text|tool_start|tool_output|tool_end|usage|meta`

For `text`:

- `tag`: `AI|THINK|SYS|TOOL|PROMPT|USER`
- `text`: string

For `tool_start`:

- `tool.id`: string
- `tool.name`: string
- `tool.input`: object (optional)

For `tool_output`:

- `tool.id`: string
- `text`: string

For `tool_end`:

- `tool.id`: string
- `tool.status`: `ok|fail|unknown`
- `tool.duration_ms`: int (optional)

For `usage`:

- `usage.prompt_tokens`: int (optional)
- `usage.completion_tokens`: int (optional)
- `usage.total_tokens`: int (optional)
- `usage.model`: string (optional)

For `meta`:

- `meta`: object

### RTF1 parsing rules

- If a `@@RALPH@@` line is invalid JSON or violates the schema, Ralph must:
  - Emit a `meta` event describing the parse error.
  - Also render the raw line as `SYS` (or `AI`) so the user can see what happened.
- Non-RTF1 lines are emitted as `text` events tagged as `AI` by default.

## Codex adapter

Codex remains a first-class integration:

- The existing `CodexTranscriptParser` is wrapped by a `CodexTranscriptAdapter` that emits `text` events with tags `AI`, `THINK`, `SYS`, `TOOL`, `PROMPT`.
- Codex-specific prompt hiding is retained and remains behind Codex-only capability.
- Tool activity summary can continue using the existing Codex tool header pattern, but it should move behind a tool event generator:
  - If Codex emits `TOOL` lines matching the current header regex, translate those into `tool_start` and `tool_end` events.

## Claude Code support

### Approach

Claude Code support must be implemented without assuming a specific transcript format unless it is verified with real output. The adapter strategy is:

1. Prefer RTF1 if Claude Code can be run in a mode where it emits tagged events (directly or via a wrapper command).
2. Otherwise, treat Claude output as plain text (`AI`) and skip tool and usage features.

### Claude runner options (implementation choices)

Option 1 (minimal):

- Continue using `CustomAgent` with `--agent-cmd` pointing to the Claude Code CLI.
- User selects `--transcript rtf1` if the command emits RTF1.
- Otherwise use `--transcript plain`.

Option 2 (first-class runner):

- Add `ClaudeAgent` (process runner) to `ralph_py/agents/`.
- The runner is responsible only for invocation and streaming.
- Transcript parsing is handled by `--transcript` selection, not hard-coded in the agent.

### Evidence requirement

To implement a `claude` specific adapter that attempts to parse role markers, this must be based on captured transcripts.

Implementation must include:

- A small set of fixture transcripts checked into `tests/fixtures/claude/`.
- Unit tests that validate adapter behavior on these fixtures.

If transcripts differ across versions, prefer RTF1 and document that as the recommended stable integration path.

## Loop and UI integration changes

### Refactor `run_loop` to be adapter-driven

Replace:

- `isinstance(agent, CodexAgent)` checks

With:

- Select adapter based on config (`--transcript`) and agent kind.
- Parse raw lines through the adapter and render resulting `TranscriptEvent`s.

### Preserve existing UI behavior where possible

- Continue using `ui.stream_line(tag, line)` for `text` events.
- Keep PLAN callout extraction based on `THINK` text events.
- Replace Codex-only token parsing with usage events:
  - If usage events are present, render the REPORT callout from them.
  - If not present, do not guess.

### Tool summaries

Tool summaries should be produced from tool events when present:

- `tool_start` begins an active tool call
- `tool_output` increments output line count (optional)
- `tool_end` finalizes the tool summary (status, duration)

If no tool events exist:

- Do not attempt to infer tool activity unless the adapter explicitly supports it (Codex-only fallback).

## CLI and configuration changes

### New CLI options

Add:

- `--transcript auto|codex|rtf1|plain|claude`

Optional quality-of-life additions:

- `--agent codex|claude|custom` (explicit runner selection)
  - Keep `--agent-cmd` for `custom`

### Backward compatibility

- If a user does nothing, Codex remains the default and behavior stays the same.
- Existing `--agent-cmd` continues to work and defaults to `plain` transcript parsing.

## Logging and cost tracking hooks (future)

This design enables repo-wide usage tracking by emitting `usage` events. USD cost tracking requires:

- A pricing configuration file (explicit, user-owned)
- A stable mapping of `model` to pricing

Ralph must not assume pricing. If pricing is configured, Ralph can compute USD.

## Suggested file and module layout

Recommended new modules:

- `ralph_py/transcripts/events.py`: `TranscriptEvent` and related dataclasses
- `ralph_py/transcripts/adapters/base.py`: `TranscriptAdapter` protocol
- `ralph_py/transcripts/adapters/plain.py`: pass-through adapter
- `ralph_py/transcripts/adapters/rtf1.py`: RTF1 adapter
- `ralph_py/transcripts/adapters/codex.py`: Codex adapter
- `ralph_py/transcripts/adapters/claude.py`: optional Claude adapter (only after collecting fixtures)

## Testing strategy

Unit tests:

- RTF1 parsing: valid events, invalid JSON, invalid schema.
- Tool summary aggregation from tool events.
- Adapter selection logic for `--transcript` and `auto`.
- Codex adapter parity: existing Codex transcript fixtures still render tags as before.

Integration tests:

- Run loop with a fake agent that emits RTF1 events and verify:
  - UI renders tags
  - PLAN and ACTIONS callouts appear
  - REPORT callout appears when usage events are present

## Open questions

- Claude Code invocation: what is the exact CLI command and what output formats are available?
  - This must be measured by capturing real transcripts in the environments we care about.
- Do we want to support a plugin registry for third-party adapters, or keep adapters in-tree only?
- Should RTF1 accept a compact tag syntax as an alternative to JSON for easier human authoring?

