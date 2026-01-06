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
│       ├── prompt.md       # Instructions for the AI agent (required)
│       ├── prd.json        # User stories & acceptance criteria
│       ├── progress.txt    # Running log of patterns & progress
│       └── ralph.sh        # The main loop script
└── ...
```

## Quick Start

### 1. Setup

Copy the Ralph files into your project:

```bash
mkdir -p scripts/ralph
cp prompt.md prd.json progress.txt ralph.sh scripts/ralph/
```

### 2. Initialize

Run the initialization script to validate your setup:

```bash
./init.sh /path/to/your-project
```

This will:
- Validate that required files exist
- Create default `prd.json` and `progress.txt` if missing
- Validate the PRD schema
- Report current status

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
| `MODEL` | *(empty)* | Model override for codex (e.g., `gpt-5-codex`) |
| `SLEEP_SECONDS` | `2` | Seconds to wait between iterations |
| `INTERACTIVE` | *(empty)* | Set to `1` for human-in-the-loop mode (pause after each iteration) |

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
| `prompt.md` | Yes | AI agent instructions |
| `prd.json` | Yes | User stories and acceptance criteria |
| `progress.txt` | No | Running log of patterns and progress (auto-created) |
| `init.sh` | No | Initialization/validation script |
| `prd_prompt.txt` | No | Fillable template for generating PRDs with an LLM |

## Tips

1. **Start small**: Begin with 1-2 user stories to validate your prompt works
2. **Be specific**: Acceptance criteria should be testable and unambiguous
3. **Use progress.txt**: Encourage the agent to log patterns it discovers
4. **Set realistic limits**: Complex features may need 20-50 iterations
5. **Monitor output**: The loop streams agent output so you can watch progress
6. **Generate PRDs**: Use `prd_prompt.txt` with your favorite LLM to create well-structured PRDs

## License

MIT
