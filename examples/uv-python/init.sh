#!/usr/bin/env bash
# =============================================================================
# init.sh - Ralph Harness Initialization Script
# =============================================================================
# This script initializes and validates the Ralph agentic loop harness in a
# target project directory. It ensures all required files exist, creates
# default configuration files if missing, and validates the PRD schema.
#
# Usage:
#   ./init.sh [directory]
#
# Arguments:
#   directory - Target project root (defaults to current directory)
#
# Exit Codes:
#   0 - Success
#   1 - Missing required files or schema validation failed
#   2 - Target directory not found
# =============================================================================

set -euo pipefail  # Exit on error, undefined vars, and pipe failures

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------

# Accept target directory as first argument, default to current directory
ROOT_DIR="${1:-.}"

# Validate the target directory exists
if [[ ! -d "$ROOT_DIR" ]]; then
  echo "init.sh: directory not found: $ROOT_DIR" >&2
  exit 2
fi

# Convert to absolute path and change into it
ROOT_DIR="$(cd "$ROOT_DIR" && pwd)"
cd "$ROOT_DIR"

# Define paths to Ralph files (all relative to scripts/ralph/)
RALPH_DIR="scripts/ralph"
PROMPT_FILE="$RALPH_DIR/prompt.md"      # AI agent instructions (required)
PRD_FILE="$RALPH_DIR/prd.json"          # User stories & acceptance criteria
PROGRESS_FILE="$RALPH_DIR/progress.txt" # Running progress log
LOOP_FILE="$RALPH_DIR/ralph.sh"         # Main loop script (required)
CODEBASE_MAP_FILE="$RALPH_DIR/codebase_map.md"          # Brownfield notes (optional)
UNDERSTAND_PROMPT_FILE="$RALPH_DIR/understand_prompt.md" # Read-only understanding prompt (optional)
UNDERSTAND_LOOP_FILE="$RALPH_DIR/ralph-understand.sh"    # Understanding loop wrapper (optional)

# -----------------------------------------------------------------------------
# UI (gum optional)
# -----------------------------------------------------------------------------

UI_FILE="$ROOT_DIR/$RALPH_DIR/ui.sh"
if [[ -f "$UI_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$UI_FILE"
else
  # Minimal UI fallback (keeps init usable if ui.sh wasn't copied).
  ui_title() { echo ""; echo "$1"; echo ""; }
  ui_section() { echo ""; echo "== $1 =="; }
  ui_box_fd() { local fd="$1"; local line; while IFS= read -r line || [[ -n "$line" ]]; do printf '  %s\n' "$line" >&"$fd"; done; }
  ui_box() { ui_box_fd 1; }
  ui_box_err() { ui_box_fd 2; }
  ui_kv_fd() { local fd="$1"; printf '%-14s %s\n' "${2}:" "$3" >&"$fd"; }
  ui_ok() { echo "OK: $1"; }
  ui_warn() { echo "WARN: $1"; }
  ui_err() { echo "ERROR: $1" >&2; }
fi

ui_title "Ralph init"
ui_section "Target"
{
  ui_kv_fd 1 "Root" "$ROOT_DIR"
  ui_kv_fd 1 "Ralph dir" "$RALPH_DIR"
} | ui_box

# -----------------------------------------------------------------------------
# VALIDATE REQUIRED FILES
# -----------------------------------------------------------------------------

ui_section "Validate required files"

# The ralph directory must exist
if [[ ! -d "$RALPH_DIR" ]]; then
  ui_err "Missing $RALPH_DIR"
  exit 1
fi
ui_ok "Found $RALPH_DIR/"

# The prompt file is required - it tells the agent what to do
if [[ ! -f "$PROMPT_FILE" ]]; then
  ui_err "Missing $PROMPT_FILE"
  exit 1
fi
ui_ok "Found $PROMPT_FILE"

# The loop script is required - it's the main execution engine
if [[ ! -f "$LOOP_FILE" ]]; then
  ui_err "Missing $LOOP_FILE"
  exit 1
fi
ui_ok "Found $LOOP_FILE"

# -----------------------------------------------------------------------------
# CREATE DEFAULT FILES (if missing)
# -----------------------------------------------------------------------------

ui_section "Create defaults (if missing)"

# Create default prd.json if it doesn't exist
# This is a minimal skeleton with an empty user stories array
if [[ ! -f "$PRD_FILE" ]]; then
  cat >"$PRD_FILE" <<'EOF'
{
  "branchName": "ralph/feature",
  "userStories": []
}
EOF
  ui_ok "Created $PRD_FILE"
fi

# Create default progress.txt if it doesn't exist
# This provides a template for tracking patterns and key files
if [[ ! -f "$PROGRESS_FILE" ]]; then
  cat >"$PROGRESS_FILE" <<'EOF'
# Ralph Progress Log
Started: 2026-01-06

## Codebase Patterns
- (add reusable patterns here)

## Key Files
- scripts/ralph/prd.json
- scripts/ralph/prompt.md
- scripts/ralph/progress.txt
- scripts/ralph/codebase_map.md
- scripts/ralph/understand_prompt.md

---
EOF
  ui_ok "Created $PROGRESS_FILE"
fi

# Create default codebase_map.md if it doesn't exist
# This file is used by the brownfield "codebase understanding" loop.
if [[ ! -f "$CODEBASE_MAP_FILE" ]]; then
  cat >"$CODEBASE_MAP_FILE" <<'EOF'
# Codebase Map (Brownfield Notes)

This file is meant to be built over time using the Ralph **codebase understanding** loop.

## Next Topics (checklist)

- [ ] How to run locally (setup, env vars, start commands)
- [ ] Build / test / lint / CI gates (what runs in CI and how)
- [ ] Repo topology & module boundaries (where code lives, layering rules)
- [ ] Entrypoints (server, worker, cron, CLI)
- [ ] Configuration, env vars, secrets, feature flags
- [ ] Authn/Authz (where permissions are enforced)
- [ ] Data model & persistence (migrations, ORM patterns, transactions)
- [ ] Core domain flow #1 (trace end-to-end)
- [ ] External integrations (third-party APIs, webhooks, queues)
- [ ] Observability (logging, metrics, tracing, error reporting)
- [ ] Deployment / release process

---

## Iteration Notes

(New notes append below; keep older notes for history.)
EOF
  ui_ok "Created $CODEBASE_MAP_FILE"
fi

# Create default understand_prompt.md if it doesn't exist
# This is a read-only prompt for mapping an existing codebase without making changes.
if [[ ! -f "$UNDERSTAND_PROMPT_FILE" ]]; then
  cat >"$UNDERSTAND_PROMPT_FILE" <<'EOF'
# Ralph Codebase Understanding Instructions (Read-Only)

## Goal (one iteration)

You are running a **codebase understanding** loop. Your job is to explore the existing codebase and write an evidence-based “map” for humans.

**Hard rule:** do NOT modify application code, tests, configs, dependencies, or CI.

**The only file you may edit is:**
- `scripts/ralph/codebase_map.md`

If you think code changes are needed, write that as a note in the map under **Open questions / Follow-ups**. Do not implement changes in this mode.

## What to do

1. Read `scripts/ralph/codebase_map.md`.
2. Choose ONE topic to investigate this iteration:
   - If `codebase_map.md` has a **Next Topics** checklist, pick the first unchecked item.
   - Otherwise follow this default order:
     1) How to run locally
     2) Build / test / lint / CI gates
     3) Repo topology & module boundaries
     4) Entrypoints (server/worker/cron/CLI)
     5) Configuration, env vars, secrets, feature flags
     6) Authn/Authz
     7) Data model & persistence (migrations, ORM patterns, transactions)
     8) Core domain flows (trace one end-to-end)
     9) External integrations
     10) Observability (logging/metrics/tracing)
     11) Deployment / release process
3. Investigate by reading docs, configs, and code. Prefer fast, high-signal entrypoints.
4. Update **ONLY** `scripts/ralph/codebase_map.md`:
   - Append a new **Iteration Notes** section for this topic (format below)
   - If you used a Next Topics checklist, mark the topic as done (`[x]`)
   - Keep notes concise, factual, and verifiable

## Evidence rules

- Every “fact” should include evidence: file paths, what to look for, and ideally line ranges.
- If uncertain, label it as a hypothesis and add an open question.

## Iteration Notes format

Append this to the END of `scripts/ralph/codebase_map.md`:

## [YYYY-MM-DD] - [Topic]

- **Summary**: 1-3 bullets on what you learned
- **Evidence**:
  - `path/to/file.ext` — what to look for (and line range if available)
- **Conventions / invariants**:
  - “Do X, don’t do Y” rules implied by the codebase
- **Risks / hotspots**:
  - Areas likely to break or require extra care
- **Open questions / follow-ups**:
  - What’s unclear, what needs human confirmation

---

## Stop condition

If there are no remaining unchecked topics in the Next Topics checklist, reply with exactly:

<promise>COMPLETE</promise>
EOF
  ui_ok "Created $UNDERSTAND_PROMPT_FILE"
fi

# Create default ralph-understand.sh wrapper if it doesn't exist
if [[ ! -f "$UNDERSTAND_LOOP_FILE" ]]; then
  cat >"$UNDERSTAND_LOOP_FILE" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

MAX_ITERATIONS="${1:-10}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Use the understanding prompt by default (can be overridden)
export PROMPT_FILE="${PROMPT_FILE:-$SCRIPT_DIR/understand_prompt.md}"

# Restrict changes to the map file (git repos only). Disable by setting ALLOWED_PATHS="".
# NOTE: Use "-" (unset only) rather than ":-" (unset OR empty), so an explicit
# ALLOWED_PATHS="" disables enforcement as documented.
export ALLOWED_PATHS="${ALLOWED_PATHS-scripts/ralph/codebase_map.md}"

# Branch to use for understanding-mode work (git repos only).
# If you don't want automatic checkout/creation, set RALPH_BRANCH="".
export RALPH_BRANCH="${RALPH_BRANCH-ralph/understanding}"

exec "$SCRIPT_DIR/ralph.sh" "$MAX_ITERATIONS"
EOF
  ui_ok "Created $UNDERSTAND_LOOP_FILE"
fi

# -----------------------------------------------------------------------------
# VALIDATE PRD JSON SYNTAX
# -----------------------------------------------------------------------------

ui_section "Validate PRD"

# First pass: check if the file is valid JSON at all
if ! python3 -m json.tool "$PRD_FILE" >/dev/null 2>&1; then
  ui_err "Invalid JSON: $PRD_FILE"
  exit 1
fi
ui_ok "JSON is valid: $PRD_FILE"

# -----------------------------------------------------------------------------
# VALIDATE PRD SCHEMA
# -----------------------------------------------------------------------------
# The PRD must conform to a strict schema. This Python script validates:
# - Top-level object has exactly "branchName" and "userStories" keys
# - branchName is a non-empty string
# - userStories is an array of story objects
# - Each story has: id, title, acceptanceCriteria, priority, passes, notes
# - All fields have correct types

python3 - <<'PY' "$PRD_FILE"
import json
import sys

path = sys.argv[1]
data = json.load(open(path, "r", encoding="utf-8"))

errors = []

# Validate top-level structure
if not isinstance(data, dict):
  errors.append("top-level must be an object")
else:
  # Check for exactly the required keys (no extras allowed)
  if set(data.keys()) != {"branchName", "userStories"}:
    errors.append('top-level keys must be exactly: "branchName", "userStories"')
  
  # Validate branchName
  if not isinstance(data.get("branchName"), str) or not data.get("branchName"):
    errors.append('"branchName" must be a non-empty string')
  
  # Validate userStories array
  stories = data.get("userStories")
  if not isinstance(stories, list):
    errors.append('"userStories" must be an array')
  else:
    # Validate each story object
    for idx, story in enumerate(stories):
      if not isinstance(story, dict):
        errors.append(f"userStories[{idx}] must be an object")
        continue
      
      # Each story must have exactly these keys
      expected = {"id", "title", "acceptanceCriteria", "priority", "passes", "notes"}
      if set(story.keys()) != expected:
        errors.append(f"userStories[{idx}] keys must be exactly: {sorted(expected)}")
      
      # Validate individual fields
      if not isinstance(story.get("id"), str) or not story.get("id"):
        errors.append(f"userStories[{idx}].id must be a non-empty string")
      if not isinstance(story.get("title"), str) or not story.get("title"):
        errors.append(f"userStories[{idx}].title must be a non-empty string")
      if not isinstance(story.get("acceptanceCriteria"), list) or not all(isinstance(x, str) and x for x in story.get("acceptanceCriteria", [])):
        errors.append(f"userStories[{idx}].acceptanceCriteria must be an array of non-empty strings")
      if not isinstance(story.get("priority"), int):
        errors.append(f"userStories[{idx}].priority must be an integer")
      if not isinstance(story.get("passes"), bool):
        errors.append(f"userStories[{idx}].passes must be a boolean")
      if not isinstance(story.get("notes"), str):
        errors.append(f"userStories[{idx}].notes must be a string")

# Report any validation errors
if errors:
  print("init.sh: prd.json schema errors:", file=sys.stderr)
  for e in errors:
    print(f"- {e}", file=sys.stderr)
  raise SystemExit(1)
PY

# -----------------------------------------------------------------------------
# PRINT STATUS SUMMARY
# -----------------------------------------------------------------------------
# Parse the PRD and display a summary of stories and their status

ui_section "PRD summary"

python3 - <<'PY' "$PRD_FILE" | ui_box
import json
import sys

path = sys.argv[1]
data = json.load(open(path, "r", encoding="utf-8"))
stories = data.get("userStories", [])

# Count how many stories are failing (passes == false)
failing = sum(1 for s in stories if isinstance(s, dict) and s.get("passes") is False)

print(f"Branch: {data.get('branchName')}")
print(f"Stories: total={len(stories)} failing={failing}")
PY

# -----------------------------------------------------------------------------
# FINALIZE SETUP
# -----------------------------------------------------------------------------

# Make the loop scripts executable (ignore errors if chmod fails)
chmod +x "$LOOP_FILE" >/dev/null 2>&1 || true
chmod +x "ralph.sh" >/dev/null 2>&1 || true
chmod +x "$UNDERSTAND_LOOP_FILE" >/dev/null 2>&1 || true
chmod +x "ralph-understand.sh" >/dev/null 2>&1 || true

# Check if we're in a git repository (helpful for the user to know)
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  ui_ok "Git repo: yes"
else
  ui_warn "Git repo: no (git history will not be available)"
fi

# Success! Print instructions for running Ralph
ui_section "Next steps"
{
  echo "Run:        ./scripts/ralph/ralph.sh 25"
  echo "Understand: ./scripts/ralph/ralph-understand.sh 10"
} | ui_box

ui_ok "Ralph harness ready"