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

# -----------------------------------------------------------------------------
# VALIDATE REQUIRED FILES
# -----------------------------------------------------------------------------

# The ralph directory must exist
if [[ ! -d "$RALPH_DIR" ]]; then
  echo "init.sh: missing $RALPH_DIR" >&2
  exit 1
fi

# The prompt file is required - it tells the agent what to do
if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "init.sh: missing $PROMPT_FILE" >&2
  exit 1
fi

# The loop script is required - it's the main execution engine
if [[ ! -f "$LOOP_FILE" ]]; then
  echo "init.sh: missing $LOOP_FILE" >&2
  exit 1
fi

# -----------------------------------------------------------------------------
# CREATE DEFAULT FILES (if missing)
# -----------------------------------------------------------------------------

# Create default prd.json if it doesn't exist
# This is a minimal skeleton with an empty user stories array
if [[ ! -f "$PRD_FILE" ]]; then
  cat >"$PRD_FILE" <<'EOF'
{
  "branchName": "ralph/feature",
  "userStories": []
}
EOF
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

---
EOF
fi

# -----------------------------------------------------------------------------
# VALIDATE PRD JSON SYNTAX
# -----------------------------------------------------------------------------

# First pass: check if the file is valid JSON at all
if ! python3 -m json.tool "$PRD_FILE" >/dev/null 2>&1; then
  echo "init.sh: invalid JSON: $PRD_FILE" >&2
  exit 1
fi

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

python3 - <<'PY' "$PRD_FILE"
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

# Check if we're in a git repository (helpful for the user to know)
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Git repo: yes"
else
  echo "Git repo: no (git history will not be available)"
fi

# Success! Print instructions for running Ralph
echo "Ralph harness ready"
echo "Run: ./scripts/ralph/ralph.sh 25"
