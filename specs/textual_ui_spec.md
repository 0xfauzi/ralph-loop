# Textual UI Spec

Status: Draft

## Summary
Introduce a full screen Textual UI that runs in parallel with the existing Rich and Plain modes. The Textual UI is always used when selected, runs in full screen mode, supports in place updates, removes the startup animation for that mode, and emphasizes native Textual widgets for layout and interaction.

## Decisions (from user)
- Textual UI is a parallel implementation, not a replacement.
- It should run even when not a TTY if selected.
- It should be a full screen TUI.
- No startup animation in the Textual UI.
- `textual` is a required dependency.
- The UI may update sections in place.

## Goals
- Provide a full screen UI with a structured timeline and an inspector rail for summaries.
- Keep behavior parity with the current UI protocol where practical.
- Allow in place updates for status, summaries, and progress tracking.
- Preserve existing CLI and config flow with a new `textual` mode.

## Non-goals
- Implement the agent adapter system described in docs/agent_adapters_design.md.
- Change run loop semantics or completion detection rules.
- Rewrite the core logic to be async or event driven beyond what Textual needs.

## User experience

### Layout
- Header bar: app title, current mode, iteration progress, agent name, streaming status.
- Main body: split view.
  - Left: timeline of events and stream blocks with stable tag gutter.
  - Right: inspector rail with clickable summary cards and a detail pane.
    - Session details (config and system summary)
    - Iteration summaries with progress and spinner
- Footer bar: key hints for scrolling and interactive prompts.

### Stream behavior
- Stream blocks are grouped per channel header and rendered as timeline cards.
- Stream lines use a fixed tag column so wrapped content never disturbs the gutter.
- Scroll follows the tail by default with a toggle for manual scroll.

### Summary behavior
- The summary rail is a list of cards, each card has a progress bar and a spinner.
- Progress steps are driven by explicit PLAN, ACTIONS, REPORT, FINAL panels.
- The spinner reflects streaming or pending summaries for that iteration.
- Clicking a card updates the detail pane without reordering timeline content.

## Implementation plan

### New module
- Add `ralph_py/ui/textual_ui.py` containing `TextualApp` and `TextualUI` implementations.
- `TextualApp` subclasses `textual.app.App` and constructs a header, timeline, summary rail, and detail inspector.
- `TextualUI` implements the existing UI protocol and routes calls to the app.
- Add `ralph_py/ui/textual.tcss` for the Textual theme and widget styling.

### Execution model
- For `RALPH_UI=textual`, the CLI should run the Textual app as the top level process.
- The run loop should execute in a worker thread started in `on_mount`.
- UI methods should post updates to the Textual app via `call_from_thread` or `post_message`.
- When the run loop completes, the app should exit cleanly with the same exit code as today.
- This needs to be measured against the Textual API for exit codes and thread safety.

### Protocol mapping
- `title`, `section`, `subsection`, `hr`, `kv`, `box` create timeline events and update session details.
- `panel` updates summary cards for PLAN, ACTIONS, REPORT, FINAL, SYS and creates timeline cards for other tags.
- `channel_header` and `channel_footer` start and stop stream blocks in the timeline.
- `stream_line` appends to the active stream block with a fixed tag gutter.
- `choose` and `confirm` use modal dialogs and block the worker thread until a response is received.
- `can_prompt` returns True only when the app is running with an active input device.
- `startup_art` is a no-op for Textual.

### Dependencies and configuration
- Add `textual` to `pyproject.toml` dependencies.
- Add `textual` to `--ui` choices in `ralph_py/cli.py` and `RALPH_UI` parsing in `ralph_py/ui/__init__.py`.
- Add `ralph_py/ui/textual.tcss` for the Textual theme.
- Update README to document the new mode and behavior.

## Data flow and threading
- The run loop calls UI methods synchronously.
- Textual UI methods enqueue update messages to the UI thread.
- For prompts, the UI thread opens a modal and returns the selection to the worker via a thread safe queue or event.

## Error handling
- If Textual cannot start, exit with a clear error message.
- If the app crashes, dump a short error to stderr and exit non zero.

## Acceptance criteria
- `RALPH_UI=textual` launches a full screen TUI and uses Textual widgets.
- Streaming output is readable with a stable tag gutter and correct wrapping.
- Summary rail cards show progress and spinner, and detail panes update on selection.
- Interactive prompts work and block the loop until answered.
- Startup animation is absent in Textual mode.
- The CLI exits with the same code as the run loop result.

## Validation plan
- Run `examples/uv-python` with a stub agent to validate stream updates and summaries.
- Run an interactive mode test to confirm `choose` and `confirm` behaviors.
- Validate behavior in non TTY contexts and confirm the expected error or behavior.

## Open questions
- Should non TTY runs be blocked outright for Textual, or allowed with a warning.
- Do we want a persistent status panel for tool activity instead of the summary panel.
- Should the stream log be bounded to avoid memory growth.
