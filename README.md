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
│       ├── prompt.md             # Feature-mode agent instructions (required)
│       ├── prd.json              # Feature-mode user stories (created by ralph init if missing)
│       ├── progress.txt          # Optional running log (created by ralph init if missing)
│       ├── prd_prompt.txt        # Optional template for generating prd.json
│       ├── understand_prompt.md  # Understanding-mode prompt (created by ralph init if missing)
│       ├── feature_understand_prompt.md # Feature-understand prompt (created by ralph init if missing)
│       └── codebase_map.md       # Understanding-mode output (created by ralph init if missing)
│       └── feature/
│           └── <feature_name>/
│               ├── prd.json       # Feature scoped PRD
│               ├── understand.md  # Feature understanding notes
│               └── repairs/
│                   ├── repair_<timestamp>.json
│                   └── latest.json
├── .ralph/
│   └── logs/
│       └── feature_<feature_name>/  # Feature run logs
└── ...
```

## CLI

Install the CLI from the repo:

```bash
uv tool install .
```

This creates the `ralph` command. `python -m ralph_py` remains available for dev workflows.

| Command | Requirements |
|---------|--------------|
| `ralph` | Python 3.11+, uv |

---

## Python Version

### Installation

```bash
# Clone and install
git clone https://github.com/0xfauzi/ralph-loop.git
cd ralph-loop
uv sync
uv tool install .
```

### Commands

```bash
# Run the agentic loop
ralph run [MAX_ITERATIONS] [OPTIONS]

# Initialize Ralph in a project
ralph init [DIRECTORY]

# Codebase understanding mode (read-only)
ralph understand [MAX_ITERATIONS]

# Feature understanding + implementation
ralph feature --understand-iterations 5 --prd scripts/ralph/feature/<feature_name>/prd.json
```

Note: You can replace `python -m ralph_py` with `ralph` in the examples below after installing the CLI.

### Python CLI Options

```bash
ralph run 25 \
  --root /path/to/your-project \   # Optional when running outside the project root
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
uv run pytest              # Run unit tests
uv run pytest --cov        # With coverage
uv run mypy ralph_py       # Type checking
uv run ruff check .        # Linting
```

---

## Quick Start

### 1. Initialize

From your project root:

```bash
ralph init .
```

Or run it from anywhere:

```bash
ralph init /path/to/your-project
```

This will:
- Create `scripts/ralph/` with `prompt.md`, `prd.json`, `progress.txt`, `prd_prompt.txt`,
  `understand_prompt.md`, and `codebase_map.md`
- Validate the PRD schema (`scripts/ralph/prd.json`)
- Print a quick status summary

### 2. Configure Your PRD

You can write `prd.json` manually or generate it with an LLM using the included prompt template.

#### Option A: Generate with an LLM

Use `scripts/ralph/prd_prompt.txt` as a template. It contains structured sections for you to fill in:

1. **Fill in the context sections** at the bottom:
   - Feature Overview - what you're building
   - Branch Name - git branch for this work
   - Requirements - specific things the feature must do
   - Tech Stack - languages, frameworks, testing tools
   - Verification Commands - exact commands to run typecheck/tests
   - Constraints - limitations or non-functional requirements

2. **Feed it to an LLM**:
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

### 3. Write Your Prompt

Edit `scripts/ralph/prompt.md` with instructions for the AI agent. This typically includes:
- Reference to `prd.json` for the current user stories
- Instructions on how to validate acceptance criteria
- Guidelines for updating `progress.txt`
- The completion signal: `<promise>COMPLETE</promise>`

### 4. Run Ralph

```bash
ralph run 25
```

If you are outside the project root, pass `--root /path/to/your-project`.

By default (git repos only), Ralph will checkout/create the branch specified in `scripts/ralph/prd.json` (`branchName`) before starting iterations.

Override it (git repos only):

```bash
RALPH_BRANCH="ralph/my-feature" python -m ralph_py run 25
```

Skip branch management entirely:

```bash
RALPH_BRANCH="" python -m ralph_py run 25
```

### Brownfield: Codebase Understanding Mode (Read-only)

Before implementing changes in an existing repo, you can run a **read-only mapping loop** that builds a codebase map over multiple iterations.

It writes findings to:
- `scripts/ralph/codebase_map.md`

Understanding mode does not require `prd.json`.

Run understanding mode:

```bash
python -m ralph_py understand 10
```

By default, understanding mode will checkout/create the branch:
- `ralph/understanding`

Override it (git repos only):

```bash
RALPH_BRANCH="ralph/codebase-map" python -m ralph_py understand 10
```

Stay on the current branch (skip checkout/creation):

```bash
RALPH_BRANCH="" python -m ralph_py understand 10
```

With Claude + human review after each iteration:

```bash
INTERACTIVE=1 AGENT_CMD="claude --print" python -m ralph_py understand 10
```

Under the hood, this uses:
- `PROMPT_FILE=scripts/ralph/understand_prompt.md`
- `ALLOWED_PATHS=scripts/ralph/codebase_map.md` (git repos only; blocks other file edits)
- `RALPH_BRANCH=ralph/understanding` (git repos only; wrapper default)

Note: `ALLOWED_PATHS` also counts **untracked files** as changes. If `scripts/ralph/` is untracked in your repo, either commit once or loosen or disable the guard:

```bash
# Allow changes anywhere under scripts/ralph/
ALLOWED_PATHS="scripts/ralph/" python -m ralph_py understand 10

# Or disable enforcement entirely
ALLOWED_PATHS="" python -m ralph_py understand 10
```

### Feature Understanding + Implementation

Use this when you want a **feature scoped map** tied to a specific PRD, then run implementation.
It writes findings to:
- `scripts/ralph/feature/<feature_name>/understand.md`
Requires:
- `scripts/ralph/codebase_map.md` (run `ralph init` or `ralph understand` if missing)

Run feature understanding mode:

```bash
python -m ralph_py feature \
  --understand-iterations 5 \
  --prd scripts/ralph/feature/<feature_name>/prd.json
```

Example with auto-run and repairs tuned:

```bash
python -m ralph_py feature \
  --understand-iterations 3 \
  --prd scripts/ralph/feature/<feature_name>/prd.json \
  --implementation-auto-run \
  --repair-max-runs 3 \
  --repair-iterations 4
```

Under the hood, this uses:
- `PROMPT_FILE=scripts/ralph/feature_understand_prompt.md`
- `ALLOWED_PATHS=scripts/ralph/feature/<feature_name>/understand.md` (git repos only; blocks other file edits)
- Branch selection follows the PRD branch by default

Behavior:
- Runs the understanding loop for `--understand-iterations`
- Prompts you to review `understand.md` before implementation (unless `--implementation-auto-run`)
- Implementation iterations are auto-set to the number of user stories in the PRD
- Feature name resolution:
  - If the PRD is at `scripts/ralph/feature/<feature_name>/prd.json`, uses that folder name
  - Otherwise uses the PRD filename stem
- Use `--implementation-auto-run` to skip the review gate
- Auto-repair runs will trigger on failures (default max 5, 5 iterations each)
- Use `--repair-max-runs` and `--repair-iterations` to tune repair behavior
- Use `--repair-agent-cmd` to run repairs with a different agent
- Repair runs skip branch checkout and write repair PRDs to `scripts/ralph/feature/<feature_name>/repairs/`
- Logs are stored under `.ralph/logs/feature_<feature_name>/`

## Example projects

This repo includes a self-contained example project you can use to try Ralph end-to-end:

- `examples/uv-python/` - a minimal **uv-managed** Python project (`pyproject.toml`) that includes `scripts/ralph/` prompt and PRD files.

Quick run:

```bash
cd examples/uv-python
uv sync
uv run pytest

# In this mono-repo, disable branch checkout while experimenting:
AGENT_CMD="printf 'hello\n<promise>COMPLETE</promise>\n'" RALPH_BRANCH="" uv run python -m ralph_py run 1
```

## Usage Examples

### Basic Usage

```bash
# Run with default settings (codex agent, 10 iterations)
python -m ralph_py run

# Specify max iterations
python -m ralph_py run 25

# Run from outside the project root
python -m ralph_py run 50 --root /path/to/your-project
```

### Using Different Agents

```bash
# Use OpenAI Codex (default)
python -m ralph_py run 25

# Use Codex with a specific model
MODEL=gpt-5-codex python -m ralph_py run 25

# Use Claude CLI
AGENT_CMD="claude --print" python -m ralph_py run 25

# Use Anthropic API directly via curl
AGENT_CMD="curl -s https://api.anthropic.com/v1/messages -H 'x-api-key: $ANTHROPIC_API_KEY' ..." python -m ralph_py run 25

# Use any CLI that reads from stdin
AGENT_CMD="my-custom-agent --input-stdin" python -m ralph_py run 25
```

### Tuning Iteration Speed

```bash
# Faster iterations (1 second between each)
SLEEP_SECONDS=1 python -m ralph_py run 25

# Slower iterations (10 seconds, for rate-limited APIs)
SLEEP_SECONDS=10 python -m ralph_py run 25

# No delay (not recommended for most APIs)
SLEEP_SECONDS=0 python -m ralph_py run 25
```

### Testing & Debugging

```bash
# Dry run - see what prompt would be sent (no actual agent call)
AGENT_CMD="cat" python -m ralph_py run 1

# Dry run - discard output silently
AGENT_CMD="cat > /dev/null" python -m ralph_py run 1

# Test completion detection
AGENT_CMD="echo '<promise>COMPLETE</promise>'" python -m ralph_py run 1

# Log all output to a file while running
python -m ralph_py run 25 2>&1 | tee ralph-output.log
```

### Human-in-the-Loop (Interactive Mode)

```bash
# Pause after each iteration for human review
INTERACTIVE=1 python -m ralph_py run 25

# Interactive mode with Claude
INTERACTIVE=1 AGENT_CMD="claude --print" python -m ralph_py run 25
```

When interactive mode is enabled, Ralph pauses after each iteration and prompts:
- **Enter** - Continue to next iteration
- **s** - Skip to autonomous mode (disable pausing)
- **q** - Quit immediately

This lets you review the agent's changes, edit files manually, or adjust the PRD between iterations.

### Combining Options

```bash
# Claude with fast iterations and high limit
AGENT_CMD="claude --print" SLEEP_SECONDS=1 python -m ralph_py run 50

# Codex with custom delay
MODEL=gpt-5-codex SLEEP_SECONDS=5 python -m ralph_py run 30

# Interactive Claude with slow iterations
INTERACTIVE=1 AGENT_CMD="claude --print" SLEEP_SECONDS=5 python -m ralph_py run 25
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
| `RALPH_UI` | `auto` | UI mode: `auto`, `rich`, `plain` (accepts `gum` as an alias for `rich`) |
| `GUM_FORCE` | *(empty)* | If set to `1`, force rich UI even when not a TTY (not recommended for CI logs) |
| `NO_COLOR` | *(empty)* | Disable ANSI colors |
| `RALPH_ASCII` | *(empty)* | If set to `1`, use ASCII separators instead of box-drawing chars |

### Examples

```bash
# Use codex with a specific model
MODEL=gpt-5-codex python -m ralph_py run 25

# Use a custom agent command (prompt is piped to stdin)
AGENT_CMD="claude --print" python -m ralph_py run 25

# Human-in-the-loop mode (pause after each iteration)
INTERACTIVE=1 python -m ralph_py run 25

# Use any CLI that accepts stdin
AGENT_CMD="my-agent --stdin --json" python -m ralph_py run 25

# Dry run with a no-op agent (useful for testing)
AGENT_CMD="cat > /dev/null" python -m ralph_py run 1

# Faster iteration (1 second delay)
SLEEP_SECONDS=1 python -m ralph_py run 25
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

Ralph treats a line that exactly matches this marker as completion and exits successfully
(code 0).

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | All stories complete (agent signaled `<promise>COMPLETE</promise>`) |
| `1` | Max iterations reached without completion, or missing required files |
| `2` | Configuration error (invalid arguments) |

## Files

| File | Required | Purpose |
|------|----------|---------|
| `prompt.md` | Yes (feature mode) | AI agent instructions for feature work |
| `prd.json` | Yes (feature mode) | User stories and acceptance criteria (created by `python -m ralph_py init` if missing) |
| `progress.txt` | No | Running log of patterns and progress (created by `python -m ralph_py init` if missing) |
| `prd_prompt.txt` | No | Fillable template for generating PRDs with an LLM |
| `understand_prompt.md` | Yes (understanding mode) | Read-only prompt for codebase understanding (created by `python -m ralph_py init` if missing) |
| `feature_understand_prompt.md` | Yes (feature understanding mode) | Read-only prompt for feature understanding (created by `python -m ralph_py init` if missing) |
| `codebase_map.md` | No | Output file for the codebase map (created by `python -m ralph_py init` if missing) |
| `feature/<feature_name>/prd.json` | Yes (feature understanding mode) | Feature scoped PRD used by `ralph feature` |
| `feature/<feature_name>/understand.md` | No | Output file for feature understanding notes (created by `ralph feature` if missing) |
| `feature/<feature_name>/repairs/repair_<timestamp>.json` | No | Auto-generated repair PRDs |
| `feature/<feature_name>/repairs/latest.json` | No | Latest repair PRD (overwritten each repair run) |
| `.ralph/logs/feature_<feature_name>/` | No | Feature run logs (understand, run, repairs) |

## Tips

1. **Start small**: Begin with 1-2 user stories to validate your prompt works
2. **Be specific**: Acceptance criteria should be testable and unambiguous
3. **Use progress.txt**: Encourage the agent to log patterns it discovers
4. **Set realistic limits**: Complex features may need 20-50 iterations
5. **Monitor output**: The loop streams agent output so you can watch progress
6. **Generate PRDs**: Use `prd_prompt.txt` with your favorite LLM to create well-structured PRDs

## License

MIT
