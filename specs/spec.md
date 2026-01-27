# Ralph TUI Improvements Spec

Status: Draft

## Summary
Improve the terminal UI for readability, visual consistency, and deterministic layout. Remove brittle keyword based plan detection and make summary panels render in a stable order across all modes, including understand mode. These changes use the current codebase only and do not rely on the unimplemented adapter design.

## Goals
- Provide a stable, aligned gutter for streamed output with correct wrapping.
- Render summary panels in a consistent order and location.
- Remove keyword based plan detection from agent output.
- Improve visual clarity without adding new dependencies.
- Keep Rich and Plain UIs behaviorally consistent.

## Non-goals
- Implement the transcript adapter architecture in docs/agent_adapters_design.md.
- Change agent execution behavior or completion detection semantics.
- Introduce a new UI framework.

## Observed issues
- Gutter alignment breaks on wrapped lines because `stream_line` prints a prefix and then raw text, so wrapped lines return to column 0.
- Summary panels can render out of order because the PLAN callout is emitted before streaming starts.
- In understand mode this ordering issue is more noticeable and reads as inconsistent formatting.
- The spinner is rendered on the same line as stream output in Rich mode, which can cause wrapping and jitter.
- In tool mode `full`, the ACTIONS panel can still claim no tool calls even though tool output streamed.
- Startup key value output wraps awkwardly for long paths and disrupts alignment.

## Proposed changes

### 1. Make the stream gutter real and stable
- Rich UI: render each stream line using a two column layout with a fixed tag column and a wrapped text column. Use a stable width based on the longest tag plus the block prefix. Ensure wrapped lines keep the same left offset.
- Plain UI: wrap the content to terminal width and indent continuation lines with the same prefix width used for the tag gutter.
- Apply the same logic to all tags, including SYS and PROMPT.

### 2. Remove keyword based plan detection
- Delete `PLAN_PREFIXES`, `_extract_heading`, `_extract_plan_lines`, and `_merge_plan_lines` from `ralph_py/loop.py`.
- Remove the early PLAN callout from `start_streaming` and the fallback PLAN callout at the end of the iteration.
- Result: there is no PLAN panel unless a future explicit structure is added. This is deliberate and aligns with the request to remove brittle heuristics.

### 3. Reorder summaries and keep them separate from the stream
- Always render summary panels after the streaming block and in a fixed order.
- Recommended order: SYS summary (if any), ACTIONS summary (only in summary mode), REPORT summary, Final message.
- Optionally group summaries under a single "Summary" section header to reinforce the ordering.

### 4. Relocate the Rich spinner
- Move the stream indicator to a dedicated status line or to the header line, not the stream content column.
- This keeps the stream width stable and prevents line wrapping caused by the spinner overlay.

### 5. Improve startup key value layout
- Rich UI: replace the current key value printing with a two column table so long paths wrap in the value column only.
- Plain UI: wrap long values and indent continuation lines to align with the value column.

### 6. Clarify tool summary behavior
- Only render the ACTIONS panel when `ai_tool_mode=summary`.
- In `full` mode, rely on streamed TOOL lines alone and omit the ACTIONS panel entirely.
- In `summary` mode, show the panel only if at least one tool call is summarized.

### 7. Prompt visibility handling
- In `ai_sys_mode=summary`, keep the prompt suppression summary as-is.
- In `ai_sys_mode=full`, avoid dumping the entire prompt into the stream. Prefer a capped prompt output with a truncation note or a dedicated panel. This keeps the stream readable.

### 8. Visual theming cleanup
- Centralize color and style choices so tags, panels, and headers are consistent.
- Lighten summary panel borders relative to the stream block so the stream remains the visual focus.
- Keep the number of accent colors small to improve scanability.

### 9. Optional quality of life controls
- Add a single verbosity knob that maps to `ai_sys_mode`, `ai_tool_mode`, and prompt visibility.
- Add a `wrap-width` override for plain mode to make wrapping predictable in non TTY logs.

## Implementation notes by file
- `ralph_py/ui/rich_ui.py`: implement a wrapped stream renderer with fixed tag column; move spinner out of the content column; replace `kv` with a table based layout; centralize palette.
- `ralph_py/ui/plain.py`: implement wrapped stream renderer; improve `kv` wrapping; add optional wrap width override.
- `ralph_py/loop.py`: remove plan heuristics; render summaries only after streaming in a fixed order; skip ACTIONS panel in full tool mode; optionally add a single "Summary" section header.
- `ralph_py/ui/codex_transcript.py`: keep prompt suppression logic, but adjust output in full sys mode to avoid massive prompt dumps.
- `ralph_py/config.py` and `ralph_py/cli.py`: add optional verbosity and wrap width configuration if approved.

## Acceptance criteria
- Wrapped stream lines remain aligned under their tag gutter in both Rich and Plain modes.
- Summary panels always appear after the stream block and in the same order every time.
- No keyword based plan detection remains in the codebase.
- The spinner no longer causes wrapping or shifts in the content column.
- In tool mode `full`, no ACTIONS panel claims no tool calls when tool output streamed.
- Startup key value output wraps without breaking the key column alignment.

## Validation plan
- Run the example project with a stub codex to exercise SYS, PROMPT, TOOL, THINK, and AI tags.
- Capture a TTY session via `script` to confirm the spinner placement and wrapping behavior.
- Run plain mode with ASCII and NO_COLOR to confirm wrapping and gutter alignment without Rich.
- Run understand mode and confirm panel ordering and formatting consistency.

## Risks and mitigations
- Rich table rendering per line could impact performance on very long outputs. Measure with a large stream before merging.
- Any prompt truncation must preserve the existence of the prompt in logs. Include a clear truncation note when applied.

## Open questions
- Should the PLAN panel be removed entirely or replaced by an explicit marker contract to be introduced later.
- Do you want a dedicated "Summary" section header or should summary panels follow immediately after the stream.
- Should prompt output in full sys mode be capped or moved to a dedicated panel by default.
