# Ralph

**Ralph** is a lightweight agentic loop harness for autonomous AI-driven development. It iteratively runs an AI agent against a set of user stories until all acceptance criteria pass - or a maximum iteration count is reached.

## Overview

Ralph operates on a simple loop:

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│   prompt.md ──▶ AI Agent ──▶ Code Changes           │
│        ▲                          │                 │
│        │                          ▼                 │
│        └────── prd.json ◀── Tests/Validation        │
│                                                     │
│   Repeat until: <promise>COMPLETE</promise>         │
│                 or max iterations reached           │
│                                                     │
└─────────────────────────────────────────────────────┘
```

## Directory Structure

```
your-project/
├── scripts/
│   └── ralph/
│       ├── ralph.sh              # Main loop runner (required)
│       ├── ui.sh                 # UI toolkit (gum-enhanced output; plain fallback)
│       ├── prompt.md             # Feature-mode agent instructions (required for feature mode)
│       ├── prd.json              # Feature-mode user stories (created by init.sh if missing)
│       ├── progress.txt          # Optional running log (created by init.sh if missing)
│       ├── prd_prompt.txt        # Optional template for generating prd.json
│       ├── ralph-understand.sh   # Understanding-mode wrapper (created by init.sh if missing)
│       ├── understand_prompt.md  # Understanding-mode prompt (created by init.sh if missing)
│       └── codebase_map.md       # Understanding-mode output (created by init.sh if missing)
└── ...
```

## Implementations

Ralph has two implementations that can be used interchangeably:

| Version | Location | Requirements |
|---------|----------|--------------|
| **Shell** | `scripts/ralph/ralph.sh` | bash 3.2+, python3, git (optional) |
| **Python** | `python -m ralph_py` | Python 3.11+, uv |

Both implementations preserve identical behavior, exit codes, and environment variables.

---

## Python Version

### Installation

```bash
# Clone and install
git clone https://github.com/0xfauzi/ralph-loop.git
cd ralph-loop
uv sync
```

### Commands

```bash
# Run the agentic loop
python -m ralph_py run [MAX_ITERATIONS] [OPTIONS]

# Initialize Ralph in a project
python -m ralph_py init [DIRECTORY]

# Codebase understanding mode (read-only)
python -m ralph_py understand [MAX_ITERATIONS]
```

### Python CLI Options

```bash
python -m ralph_py run 25 \
  --agent-cmd "claude --print" \    # Custom agent command
  --model gpt-4o \                  # Model for codex
  --reasoning low \                 # Reasoning effort
  --sleep 1 \                       # Sleep between iterations
  --interactive \                   # Human-in-the-loop mode
  --branch "my-feature" \           # Git branch (empty to skip)
  --allowed-paths "src/,tests/" \   # Restrict file changes
  --ui plain                        # UI mode: auto, rich, plain
```

### Python Architecture

```
ralph_py/
  cli.py          # Click-based CLI
  config.py       # Configuration dataclass
  loop.py         # Main agentic loop
  prd.py          # PRD loading and validation
  git.py          # Git operations
  guards.py       # ALLOWED_PATHS enforcement
  agents/         # Agent implementations (codex, custom)
  ui/             # Terminal UI (rich, plain)
```

### Development

```bash
uv run pytest              # Run 60 unit tests
uv run pytest --cov        # With coverage
uv run mypy ralph_py       # Type checking
uv run ruff check .        # Linting
```

---

## Shell Version

### Requirements

- bash (macOS default Bash 3.2 supported)
- python3 (used for PRD parsing/validation and branch selection)
- git (optional but recommended; enables branch checkout + `ALLOWED_PATHS` enforcement)
- gum (optional; makes output prettier): `brew install gum`
- An agent CLI:
  - Default: OpenAI Codex CLI (`codex`)
  - Or set `AGENT_CMD` to any command that reads a prompt from stdin and prints a response

## Quick Start

### 1. Setup

Copy the Ralph files into your project:

```bash
mkdir -p scripts/ralph
cp prompt.md prd.json progress.txt ralph.sh ui.sh prd_prompt.txt understand_prompt.md codebase_map.md ralph-understand.sh scripts/ralph/
```

### 2. Initialize

Run the initialization script to validate your setup:

```bash
./init.sh /path/to/your-project
```

Alternatively, copy `init.sh` into your project root and run it in-place:

```bash
cp init.sh /path/to/your-project/init.sh
cd /path/to/your-project
./init.sh
```

This will:
- Validate that required files exist
- Create defaults if missing: `prd.json`, `progress.txt`, `codebase_map.md`, `understand_prompt.md`, `ralph-understand.sh`
- Validate the PRD schema (`scripts/ralph/prd.json`)
- Make loop scripts executable
- Print a quick status summary

### 3. Configure Your PRD

You can write `prd.json` manually or generate it with an LLM using the included prompt template.

#### Option A: Generate with an LLM

Use `prd_prompt.txt` as a template. It contains structured sections for you to fill in:

1. **Copy the template** to your project:
   ```bash
   cp prd_prompt.txt scripts/ralph/prd_prompt.txt
   ```

2. **Fill in the context sections** at the bottom:
   - Feature Overview - what you're building
   - Branch Name - git branch for this work
   - Requirements - specific things the feature must do
   - Tech Stack - languages, frameworks, testing tools
   - Verification Commands - exact commands to run typecheck/tests
   - Constraints - limitations or non-functional requirements

3. **Feed it to an LLM**:
   ```bash
   # Generate PRD with Claude
   cat scripts/ralph/prd_prompt.txt | claude --print > scripts/ralph/prd.json

   # Or any LLM CLI that accepts stdin
   cat scripts/ralph/prd_prompt.txt | your-llm-cli > scripts/ralph/prd.json
   ```

The template instructs the LLM to output valid JSON with properly scoped, testable user stories.

#### Option B: Write manually

Create `scripts/ralph/prd.json` with your user stories:

```json
{
  "branchName": "ralph/my-feature",
  "userStories": [
    {
      "id": "US-001",
      "title": "User can log in with email",
      "acceptanceCriteria": [
        "Login form accepts email and password",
        "Submitting an invalid email shows a validation error",
        "Typecheck passes (run the project's typecheck)",
        "Tests pass (run the project's tests)"
      ],
      "priority": 1,
      "passes": false,
      "notes": ""
    }
  ]
}
```

### 4. Write Your Prompt

Edit `scripts/ralph/prompt.md` with instructions for the AI agent. This typically includes:
- Reference to `prd.json` for the current user stories
- Instructions on how to validate acceptance criteria
- Guidelines for updating `progress.txt`
- The completion signal: `<promise>COMPLETE</promise>`

### 5. Run Ralph

```bash
./scripts/ralph/ralph.sh 25
```

By default (git repos only), Ralph will checkout/create the branch specified in `scripts/ralph/prd.json` (`branchName`) before starting iterations.

Override it (git repos only):

```bash
RALPH_BRANCH="ralph/my-feature" ./scripts/ralph/ralph.sh 25
```

Skip branch management entirely:

```bash
RALPH_BRANCH="" ./scripts/ralph/ralph.sh 25
```

### Brownfield: Codebase Understanding Mode (Read-only)

Before implementing changes in an existing repo, you can run a **read-only mapping loop** that builds a codebase map over multiple iterations.

It writes findings to:
- `scripts/ralph/codebase_map.md`

Understanding mode does not require `prd.json`.

Run it with the convenience wrapper:

```bash
# Uses scripts/ralph/understand_prompt.md and writes to scripts/ralph/codebase_map.md
./scripts/ralph/ralph-understand.sh 10
```

By default, understanding mode will checkout/create the branch:
- `ralph/understanding`

Override it (git repos only):

```bash
RALPH_BRANCH="ralph/codebase-map" ./scripts/ralph/ralph-understand.sh 10
```

Stay on the current branch (skip checkout/creation):

```bash
RALPH_BRANCH="" ./scripts/ralph/ralph-understand.sh 10
```

With Claude + human review after each iteration:

```bash
INTERACTIVE=1 AGENT_CMD="claude --print" ./scripts/ralph/ralph-understand.sh 10
```

Under the hood, this uses:
- `PROMPT_FILE=scripts/ralph/understand_prompt.md`
- `ALLOWED_PATHS=scripts/ralph/codebase_map.md` (git repos only; blocks other file edits)
- `RALPH_BRANCH=ralph/understanding` (git repos only; wrapper default)

Note: `ALLOWED_PATHS` also counts **untracked files** as changes. If you just copied `scripts/ralph/` into a repo and haven't committed it yet, either commit once or loosen/disable the guard:

```bash
# Allow changes anywhere under scripts/ralph/
ALLOWED_PATHS="scripts/ralph/" ./scripts/ralph/ralph-understand.sh 10

# Or disable enforcement entirely
ALLOWED_PATHS="" ./scripts/ralph/ralph-understand.sh 10
```

## Example projects

This repo includes a self-contained example project you can use to try Ralph end-to-end:

- `examples/uv-python/` — a minimal **uv-managed** Python project (`pyproject.toml`) that vendors Ralph under `scripts/ralph/`.

Quick run:

```bash
cd examples/uv-python
uv sync
uv run pytest

# In this mono-repo, disable branch checkout while experimenting:
AGENT_CMD="printf 'hello\n<promise>COMPLETE</promise>\n'" RALPH_BRANCH="" ./scripts/ralph/ralph.sh 1
```

## Usage Examples

### Basic Usage

```bash
# Run with default settings (codex agent, 10 iterations)
./scripts/ralph/ralph.sh

# Specify max iterations
./scripts/ralph/ralph.sh 25

# Run from project root (script finds its own location)
cd /path/to/your-project
./scripts/ralph/ralph.sh 50
```

### Using Different Agents

```bash
# Use OpenAI Codex (default)
./scripts/ralph/ralph.sh 25

# Use Codex with a specific model
MODEL=gpt-5-codex ./scripts/ralph/ralph.sh 25

# Use Claude CLI
AGENT_CMD="claude --print" ./scripts/ralph/ralph.sh 25

# Use Anthropic API directly via curl
AGENT_CMD="curl -s https://api.anthropic.com/v1/messages -H 'x-api-key: $ANTHROPIC_API_KEY' ..." ./scripts/ralph/ralph.sh 25

# Use any CLI that reads from stdin
AGENT_CMD="my-custom-agent --input-stdin" ./scripts/ralph/ralph.sh 25
```

### Tuning Iteration Speed

```bash
# Faster iterations (1 second between each)
SLEEP_SECONDS=1 ./scripts/ralph/ralph.sh 25

# Slower iterations (10 seconds, for rate-limited APIs)
SLEEP_SECONDS=10 ./scripts/ralph/ralph.sh 25

# No delay (not recommended for most APIs)
SLEEP_SECONDS=0 ./scripts/ralph/ralph.sh 25
```

### Testing & Debugging

```bash
# Dry run - see what prompt would be sent (no actual agent call)
AGENT_CMD="cat" ./scripts/ralph/ralph.sh 1

# Dry run - discard output silently
AGENT_CMD="cat > /dev/null" ./scripts/ralph/ralph.sh 1

# Test completion detection
AGENT_CMD="echo '<promise>COMPLETE</promise>'" ./scripts/ralph/ralph.sh 1

# Log all output to a file while running
./scripts/ralph/ralph.sh 25 2>&1 | tee ralph-output.log
```

### Human-in-the-Loop (Interactive Mode)

```bash
# Pause after each iteration for human review
INTERACTIVE=1 ./scripts/ralph/ralph.sh 25

# Interactive mode with Claude
INTERACTIVE=1 AGENT_CMD="claude --print" ./scripts/ralph/ralph.sh 25
```

When interactive mode is enabled, Ralph pauses after each iteration and prompts:
- **Enter** - Continue to next iteration
- **s** - Skip to autonomous mode (disable pausing)
- **q** - Quit immediately

This lets you review the agent's changes, edit files manually, or adjust the PRD between iterations.

### Combining Options

```bash
# Claude with fast iterations and high limit
AGENT_CMD="claude --print" SLEEP_SECONDS=1 ./scripts/ralph/ralph.sh 50

# Codex with custom delay
MODEL=gpt-5-codex SLEEP_SECONDS=5 ./scripts/ralph/ralph.sh 30

# Interactive Claude with slow iterations
INTERACTIVE=1 AGENT_CMD="claude --print" SLEEP_SECONDS=5 ./scripts/ralph/ralph.sh 25
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_CMD` | *(empty)* | Custom agent command (prompt piped to stdin). Takes precedence over codex. |
| `MODEL` | *(empty)* | Model override for codex (passed as `codex -m ...`). If unset, codex uses its own defaults from `~/.codex/config.toml`. |
| `MODEL_REASONING_EFFORT` | *(empty)* | Codex reasoning effort override (passed as `codex -c model_reasoning_effort="..."`). Common values are `low`, `medium`, `high`, `xhigh` (model-dependent). |
| `SLEEP_SECONDS` | `2` | Seconds to wait between iterations |
| `INTERACTIVE` | *(empty)* | Set to `1` for human-in-the-loop mode (pause after each iteration) |
| `PROMPT_FILE` | `scripts/ralph/prompt.md` | Override prompt file path |
| `ALLOWED_PATHS` | *(empty)* | Comma-separated repo-root-relative paths allowed to change (git repos only). Entries can be exact files or directory prefixes ending with `/`. Set to empty (`ALLOWED_PATHS=""`) to disable enforcement. |
| `PRD_FILE` | `scripts/ralph/prd.json` | PRD file path used for branch selection (git repos only) |
| `RALPH_BRANCH` | *(unset)* | Branch to checkout/create before running (git repos only). If set to empty (`RALPH_BRANCH=""`), branch checkout is skipped. Takes precedence over PRD `branchName` when non-empty. |
| `RALPH_UI` | `auto` | UI mode: `auto` (use gum if available), `gum` (force), `plain` (disable gum) |
| `GUM_FORCE` | *(empty)* | If set to `1`, force gum even when not a TTY (not recommended for CI logs) |
| `NO_COLOR` | *(empty)* | Disable ANSI colors |
| `RALPH_ASCII` | *(empty)* | If set to `1`, use ASCII separators instead of box-drawing chars |
| `RALPH_AI_SHOW_PROMPT` | *(empty)* | Show the prompt echoed by Codex in logs (by default it is collapsed/hidden) |
| `RALPH_AI_RAW` | *(empty)* | Stream raw Codex output (disables the Codex transcript pretty-printer) |
| `RALPH_AI_PROMPT_PROGRESS_EVERY` | `50` | When the Codex echoed prompt is hidden, print a progress line every N suppressed lines (set to `0` to disable) |

### Examples

```bash
# Use codex with a specific model
MODEL=gpt-5-codex ./scripts/ralph/ralph.sh 25

# Use a custom agent command (prompt is piped to stdin)
AGENT_CMD="claude --print" ./scripts/ralph/ralph.sh 25

# Human-in-the-loop mode (pause after each iteration)
INTERACTIVE=1 ./scripts/ralph/ralph.sh 25

# Use any CLI that accepts stdin
AGENT_CMD="my-agent --stdin --json" ./scripts/ralph/ralph.sh 25

# Dry run with a no-op agent (useful for testing)
AGENT_CMD="cat > /dev/null" ./scripts/ralph/ralph.sh 1

# Faster iteration (1 second delay)
SLEEP_SECONDS=1 ./scripts/ralph/ralph.sh 25
```

## PRD Schema

The `prd.json` file must conform to this schema:

| Field | Type | Description |
|-------|------|-------------|
| `branchName` | `string` | Git branch name for this feature |
| `userStories` | `array` | Array of user story objects |

Each user story object:

| Field | Type | Description |
|-------|------|-------------|
| `id` | `string` | Unique identifier (e.g., `"US-001"`) |
| `title` | `string` | Brief description of the story |
| `acceptanceCriteria` | `string[]` | Explicit, testable, ordered checks |
| `priority` | `integer` | Lower = higher priority (unique, starting at 1) |
| `passes` | `boolean` | Whether all criteria are met (start as `false`) |
| `notes` | `string` | Agent notes/observations |

### PRD Best Practices

From `prd_prompt.txt`:

- **Stories must be small and atomic**: each story should be implementable and verifiable in a single iteration
- **Acceptance criteria must be testable**: explicit checks with expected outcomes
- **Priorities must be unique**: lower number = higher priority, starting at 1
- **Don't invent UI or endpoints**: only reference what exists in your codebase
- **Cover core flows first**: then important edge cases

## Completion Signal

The agent signals completion by outputting:

```
<promise>COMPLETE</promise>
```

When Ralph detects this in the agent's output, it exits successfully (code 0).

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | All stories complete (agent signaled `<promise>COMPLETE</promise>`) |
| `1` | Max iterations reached without completion, or missing required files |
| `2` | Configuration error (invalid arguments) |

## Files

| File | Required | Purpose |
|------|----------|---------|
| `ralph.sh` | Yes | Main loop script (lives in `scripts/ralph/`) |
| `prompt.md` | Yes (feature mode) | AI agent instructions for feature work |
| `prd.json` | Yes (feature mode) | User stories and acceptance criteria (auto-created by `init.sh` if missing) |
| `progress.txt` | No | Running log of patterns and progress (auto-created by `init.sh` if missing) |
| `init.sh` | No | Initialization/validation script (creates optional files + validates PRD) |
| `prd_prompt.txt` | No | Fillable template for generating PRDs with an LLM |
| `understand_prompt.md` | Yes (understanding mode) | Read-only prompt for codebase understanding (auto-created by `init.sh` if missing) |
| `codebase_map.md` | No | Output file for the codebase map (auto-created by `init.sh` if missing) |
| `ralph-understand.sh` | No | Convenience wrapper for understanding mode (auto-created by `init.sh` if missing) |
| `ui.sh` | No | Output/UI helpers (uses `gum` if installed; falls back to plain output) |

## Tips

1. **Start small**: Begin with 1-2 user stories to validate your prompt works
2. **Be specific**: Acceptance criteria should be testable and unambiguous
3. **Use progress.txt**: Encourage the agent to log patterns it discovers
4. **Set realistic limits**: Complex features may need 20-50 iterations
5. **Monitor output**: The loop streams agent output so you can watch progress
6. **Generate PRDs**: Use `prd_prompt.txt` with your favorite LLM to create well-structured PRDs

## License

MIT
