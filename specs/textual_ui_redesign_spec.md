# Textual UI Redesign Spec

Status: Draft

## Summary
Redesign the Textual TUI from first principles to feel intentional, calm, and information rich. The redesign uses native Textual widgets, minimizes visual noise, and improves scanning for both live streaming and iteration summaries. This spec defines the research findings, design principles, layout concepts, and detailed component behavior. No code changes in this phase.

## Goals
- Make the stream readable and stable without tag noise or layout drift.
- Provide clear, reliable iteration status with progress and spinner indicators.
- Separate live output from summaries so the user can scan at different depths.
- Keep the UI truthful to the protocol. No keyword or heuristic parsing.
- Preserve full screen operation and interactive prompts.

## Non-goals
- Rewriting the run loop or adding new protocol methods.
- Adding animations beyond required spinners.
- Replacing the Rich or Plain UIs.

## Research Findings
### Observed problems
- The visual hierarchy is flat. Most elements share the same surface and border weight.
- The left pane feels like an empty slab before content arrives.
- The summary area is a markdown dump rather than a purposeful summary.
- Tag gutters repeat on every line, creating unnecessary visual noise.
- Borders stack without rhythm, which makes the interface feel heavy.

### User jobs
- Understand what the agent is doing now and if it is making progress.
- Read stream output without losing track of speakers or channels.
- Review iteration summaries quickly and drill into details when needed.
- Access session context when something seems off.

### Content inventory
- Live stream: `channel_header`, `stream_line`, `channel_footer`.
- Summary panels: `PLAN`, `ACTIONS`, `REPORT`, `FINAL`, `SYS`.
- Session config: `kv` pairs at startup.
- Status messages: `info`, `ok`, `warn`, `err`.
- Interactive prompts: choose and confirm dialogs.

## Design Principles
- Clarity over ornament. Use spacing and weight to establish hierarchy.
- One glance for status, one click for detail.
- Stream should be readable even at high volume.
- Summaries must be stable and ordered. No interleaving with stream.
- Use Textual primitives for layout and interaction.

## Layout Concepts
### Concept A: Stream-first
Best when live output is the primary focus.
- Left: wide stream area with grouped stream blocks.
- Right: summary rail and a detail panel stacked vertically.
- Header: compact status, mode, agent, iteration progress.

### Concept B: Editorial
Best when summaries and conclusions matter most.
- Upper left: live stream block with limited height.
- Lower left: timeline of system events and status.
- Right: summary rail with detail inspector.

### Concept C: Inspector
Best when users frequently drill into summaries.
- Left: stream area.
- Center: summary rail.
- Right: detail inspector.

### Recommendation
Start with Concept A. It aligns with the primary task of watching the agent in real time while keeping summaries easily accessible.

## Information Architecture
- Primary plane: Stream output grouped into blocks.
- Secondary plane: Iteration summary list with progress and spinner.
- Tertiary plane: Session and system context available on demand.
- Principle: No duplication unless it improves scanability.

## Visual Language
### Tone
Editorial control panel. Calm, restrained, and legible.

### Color roles
Define semantic roles, then tune values using contrast checks.
- Background, surface, panel
- Primary, secondary
- Accent, warning, error, success
- Foreground, muted foreground

This needs to be measured for contrast in a real terminal. Target minimum contrast ratio of 4.5:1 for body text and 3:1 for secondary labels.

### Typography
- Use the terminal default monospace.
- Heading emphasis via weight and spacing, not size.
- Labels use muted color and bold weight.

### Spacing
Use a small set of spacing tokens: xs, s, m, l.
Map tokens to actual cell counts after measuring typical terminal sizes.

## Component Specifications
### Header bar
Purpose: show global status at a glance.
- Fields: title, mode, agent, iteration label, progress bar, streaming indicator.
- Status color changes only on ok, warn, err.
- Progress is based on iteration count, not heuristics.

### Stream block
Purpose: group related streaming output.
- Title line shows channel and optional label.
- Content area is a vertical list of stream rows.
- No heavy borders. Use a subtle surface change and left edge marker.

### Stream row
Purpose: stable gutter and readable text.
- Fixed tag column. Tag only shown when it changes from previous line.
- Content wraps within its column, never into the tag gutter.
- Tag colors are semantic, not decorative.

### Timeline events
Purpose: record sections, subsections, status notes.
- Section entries are high weight with extra vertical spacing.
- Subsections are muted and indented.
- Status entries use semantic color only, no borders.

### Summary rail
Purpose: quick view of iteration status.
- Each card contains title, short status line, progress bar, spinner.
- Spinner active while streaming or while any summary step is pending.
- Progress steps are only PLAN, ACTIONS, REPORT, FINAL when present.

### Detail inspector
Purpose: reveal full context for the selected summary card.
- Detail panel updates on selection, not on scroll.
- Iteration detail has tabs for Plan, Actions, Report, Final.
- Session detail includes config and system summary.

### Prompts
Purpose: clear interruption with obvious focus.
- Modal overlay with strong contrast from background.
- Buttons stacked vertically for easy keyboard selection.
- Escape dismisses to default.

### Empty states
Purpose: reduce confusion and indicate what is expected.
- Stream area shows a short "waiting for agent output" placeholder.
- Summary detail shows "No data yet" until populated.

## Interaction and State
### Iteration lifecycle
- Iteration starts on section line with iteration pattern.
- Summary progress starts at zero and advances on explicit panels.
- Spinner stops only when streaming ends and all summaries are present.

### Scrolling
- Stream auto follows tail by default.
- Toggle key to lock scroll for reading.
- Summary rail scrolls independently.

### Focus
- Summary rail is focusable. Highlighted item updates details.
- Detail panel is read-only and does not steal focus.

## Measurement Plan
This needs to be measured before implementation.
- Collect typical terminal widths and heights used by the team.
- Validate summary rail width against the longest expected label.
- Validate stream area width for readable wrapping.
- Confirm contrast ratios in the actual terminal theme.

## Implementation Notes
Use Textual primitives only.
- Containers: `Horizontal`, `Vertical`, `VerticalScroll`, `ContentSwitcher`.
- Lists: `ListView`, `ListItem`.
- Detail: `TabbedContent`, `TabPane`, `Markdown`.
- Status: `ProgressBar`, `LoadingIndicator`, `Label`.
- Prompts: `ModalScreen`, `Button`.

No heuristic parsing. Summary content only updates from explicit `panel` calls.
