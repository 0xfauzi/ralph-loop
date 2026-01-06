#!/usr/bin/env bash
# =============================================================================
# ralph.sh - Ralph Agentic Loop Runner
# =============================================================================
# This script runs an AI agent in a loop, feeding it a prompt file on each
# iteration. The agent works through user stories defined in prd.json until
# all acceptance criteria pass (signaled by <promise>COMPLETE</promise>) or
# the maximum iteration count is reached.
#
# Usage:
#   ./scripts/ralph/ralph.sh [max_iterations]
#
# Arguments:
#   max_iterations - Maximum loop iterations (default: 10)
#
# Environment Variables:
#   AGENT_CMD     - Custom command to run (prompt piped to stdin, takes precedence)
#   MODEL         - Model override for codex (e.g., "o3", "gpt-4")
#   SLEEP_SECONDS - Seconds between iterations (default: 2)
#   INTERACTIVE   - Set to "1" for human-in-the-loop mode (pause after each iteration)
#   PROMPT_FILE   - Override prompt file path (default: scripts/ralph/prompt.md)
#   ALLOWED_PATHS - Comma-separated list of repo-root-relative paths allowed to change (git repos only)
#
# Exit Codes:
#   0 - Agent signaled completion (<promise>COMPLETE</promise>)
#   1 - Max iterations reached without completion
#   2 - Configuration error (invalid arguments)
#
# Examples:
#   ./scripts/ralph/ralph.sh 25                              # Use codex (default)
#   MODEL=o3 ./scripts/ralph/ralph.sh 25                     # Use codex with specific model
#   AGENT_CMD="claude --print" ./scripts/ralph/ralph.sh 25   # Use custom command
#   AGENT_CMD="cat > /dev/null" ./scripts/ralph/ralph.sh 1   # Dry run (no-op agent)
# =============================================================================

set -euo pipefail  # Exit on error, undefined vars, and pipe failures

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------

# Maximum number of iterations before giving up (first argument, default 10)
MAX_ITERATIONS="${1:-10}"

# Determine script location and project root
# Script lives in scripts/ralph/, so root is two directories up
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Path to the prompt file that will be fed to the agent each iteration.
# Can be overridden via environment variable PROMPT_FILE.
PROMPT_FILE="${PROMPT_FILE:-$SCRIPT_DIR/prompt.md}"

# Path to PRD file (used for branch selection). Optional if your prompt
# does not rely on prd.json (e.g., understanding mode).
PRD_FILE="${PRD_FILE:-$SCRIPT_DIR/prd.json}"

# -----------------------------------------------------------------------------
# VALIDATION
# -----------------------------------------------------------------------------

# Ensure max_iterations is a valid integer
if [[ ! "$MAX_ITERATIONS" =~ ^[0-9]+$ ]]; then
  echo "scripts/ralph/ralph.sh: MAX_ITERATIONS must be an integer (got: $MAX_ITERATIONS)" >&2
  exit 2
fi

# Ensure the prompt file exists
if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "scripts/ralph/ralph.sh: missing prompt file: $PROMPT_FILE" >&2
  exit 1
fi

# Change to project root for consistent working directory
cd "$ROOT_DIR"

# -----------------------------------------------------------------------------
# ENVIRONMENT VARIABLES
# -----------------------------------------------------------------------------

# Seconds to sleep between iterations (prevents API hammering)
SLEEP_SECONDS="${SLEEP_SECONDS:-2}"

# Custom agent command (if set, takes precedence over codex)
# The prompt is piped to this command on stdin
AGENT_CMD="${AGENT_CMD:-}"

# Optional model override for codex (e.g., "o3", "gpt-4")
MODEL="${MODEL:-}"

# Interactive mode - pause after each iteration for human review
# Set to "1", "true", or "yes" to enable
INTERACTIVE="${INTERACTIVE:-}"

# Optional safety guard (git repos only): restrict which paths may change.
# Provide comma-separated paths relative to repo root, for example:
#   ALLOWED_PATHS="scripts/ralph/codebase_map.md"
ALLOWED_PATHS="${ALLOWED_PATHS:-}"

# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------

is_git_repo() {
  git rev-parse --is-inside-work-tree >/dev/null 2>&1
}

trim_ws() {
  # Trim leading/trailing whitespace
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

path_is_allowed() {
  # Check if a repo-root-relative path is allowed by ALLOWED_PATHS.
  # Allowed entries are either:
  # - exact file paths (e.g., scripts/ralph/codebase_map.md)
  # - directory prefixes ending with / (e.g., docs/)
  local path="$1"
  local allowed raw

  IFS=',' read -r -a allowed <<< "$ALLOWED_PATHS"
  for raw in "${allowed[@]}"; do
    raw="$(trim_ws "$raw")"
    [[ -z "$raw" ]] && continue

    # Directory prefix rule
    if [[ "$raw" == */ ]]; then
      if [[ "$path" == "$raw"* ]]; then
        return 0
      fi
      continue
    fi

    # Exact match rule
    if [[ "$path" == "$raw" ]]; then
      return 0
    fi
  done

  return 1
}

enforce_allowed_paths_if_configured() {
  # If ALLOWED_PATHS is set and we're in a git repo, fail (or prompt in interactive mode)
  # when the iteration created changes outside the allowed paths.
  [[ -z "$ALLOWED_PATHS" ]] && return 0
  is_git_repo || return 0

  local changed_files=()
  local f

  # Unstaged changes
  while IFS= read -r f; do
    [[ -n "$f" ]] && changed_files+=("$f")
  done < <(git diff --name-only)

  # Staged changes
  while IFS= read -r f; do
    [[ -n "$f" ]] && changed_files+=("$f")
  done < <(git diff --name-only --cached)

  # Untracked files
  while IFS= read -r f; do
    [[ -n "$f" ]] && changed_files+=("$f")
  done < <(git ls-files --others --exclude-standard)

  # Deduplicate
  declare -A seen=()
  local unique=()
  for f in "${changed_files[@]}"; do
    if [[ -z "${seen["$f"]+x}" ]]; then
      seen["$f"]=1
      unique+=("$f")
    fi
  done

  # Compute disallowed
  local disallowed=()
  for f in "${unique[@]}"; do
    if ! path_is_allowed "$f"; then
      disallowed+=("$f")
    fi
  done

  if (( ${#disallowed[@]} == 0 )); then
    return 0
  fi

  echo ""
  echo "scripts/ralph/ralph.sh: disallowed changes detected (ALLOWED_PATHS=$ALLOWED_PATHS):" >&2
  for f in "${disallowed[@]}"; do
    echo "  - $f" >&2
  done

  if [[ "$INTERACTIVE" =~ ^(1|true|yes)$ ]]; then
    echo "" >&2
    echo "Choose an action:" >&2
    echo "  [r] revert disallowed changes and continue" >&2
    echo "  [q] quit now" >&2
    echo "  [c] continue anyway (leave changes as-is)" >&2
    echo "" >&2
    read -r -p "What next? " _choice

    case "$_choice" in
      r|R|revert)
        echo "Reverting disallowed changes..." >&2
        for f in "${disallowed[@]}"; do
          if git ls-files --error-unmatch -- "$f" >/dev/null 2>&1; then
            git restore --staged --worktree -- "$f" >/dev/null 2>&1 || true
          else
            rm -rf -- "$ROOT_DIR/$f" >/dev/null 2>&1 || true
          fi
        done
        ;;
      q|Q|quit|exit)
        echo "Stopped by user" >&2
        exit 1
        ;;
      c|C|continue)
        ;;
      *)
        # Default: quit (safer)
        echo "Unknown choice; quitting for safety." >&2
        exit 1
        ;;
    esac
  else
    echo "Set INTERACTIVE=1 to review/revert, or clear ALLOWED_PATHS to disable enforcement." >&2
    exit 1
  fi
}

auto_checkout_branch_from_prd() {
  # Automatically ensure we are on the branch specified in prd.json.
  # No-op if git repo is absent, PRD file is missing, or branchName is empty.
  is_git_repo || return 0
  [[ -f "$PRD_FILE" ]] || { echo "Branch: $PRD_FILE not found, skipping branch checkout"; return 0; }

  local branch
  branch="$(python3 - <<'PY' "$PRD_FILE"
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path, "r", encoding="utf-8"))
    b = data.get("branchName")
    if isinstance(b, str) and b.strip():
        print(b.strip())
except Exception:
    pass
PY
)"

  if [[ -z "$branch" ]]; then
    echo "Branch: branchName missing in $PRD_FILE; skipping branch checkout"
    return 0
  fi

  local current
  current="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
  if [[ "$current" == "$branch" ]]; then
    echo "Branch: already on $branch"
    return 0
  fi

  if git show-ref --verify --quiet "refs/heads/$branch"; then
    echo "Branch: switching to existing $branch"
    git checkout "$branch"
  else
    echo "Branch: creating branch $branch"
    git checkout -b "$branch"
  fi
}

# -----------------------------------------------------------------------------
# STARTUP MESSAGE
# -----------------------------------------------------------------------------

echo "Starting Ralph"
echo "Root: $ROOT_DIR"
echo "Prompt: $PROMPT_FILE"
if [[ -n "$AGENT_CMD" ]]; then
  echo "Agent: custom ($AGENT_CMD)"
else
  echo "Agent: codex${MODEL:+ (model: $MODEL)}"
fi
echo "Max iterations: $MAX_ITERATIONS"
if [[ "$INTERACTIVE" =~ ^(1|true|yes)$ ]]; then
  echo "Mode: interactive (will pause after each iteration)"
fi
if [[ -n "$ALLOWED_PATHS" ]]; then
  echo "Allowed changes: $ALLOWED_PATHS"
fi

# Automatically align to branchName in PRD (best effort)
auto_checkout_branch_from_prd

# If ALLOWED_PATHS is set, validate current repo state before starting.
enforce_allowed_paths_if_configured

# -----------------------------------------------------------------------------
# MAIN LOOP
# -----------------------------------------------------------------------------
# Run the agent repeatedly until it signals completion or we hit max iterations

for i in $(seq 1 "$MAX_ITERATIONS"); do
  echo "=== Iteration $i ==="

  # -------------------------------------------------------------------------
  # CUSTOM AGENT COMMAND (takes precedence if set)
  # -------------------------------------------------------------------------
  # If AGENT_CMD is set, use it directly. The prompt is piped to stdin.
  # This allows any CLI tool that accepts input on stdin.
  
  if [[ -n "$AGENT_CMD" ]]; then
    # Run custom agent command with prompt on stdin
    # - bash -lc: run in login shell for full environment
    # - tee /dev/stderr: show output in real-time
    # - || true: don't abort loop on agent failure
    OUTPUT="$(cat "$PROMPT_FILE" | bash -lc "$AGENT_CMD" 2>&1 | tee /dev/stderr)" || true

    # Check if agent signaled completion
    # The magic marker is: <promise>COMPLETE</promise>
    if echo "$OUTPUT" | grep -q "<promise>COMPLETE</promise>"; then
      echo "Done"
      exit 0  # Success! All stories complete
    fi

  # -------------------------------------------------------------------------
  # CODEX AGENT (default)
  # -------------------------------------------------------------------------
  # The default agent is OpenAI's Codex CLI. We pipe the prompt to it and
  # capture the last message to check for the completion signal.
  
  else
    # Verify codex is installed
    if ! command -v codex >/dev/null 2>&1; then
      echo "scripts/ralph/ralph.sh: codex not found in PATH" >&2
      echo "  Install codex or set AGENT_CMD to use a different agent" >&2
      exit 1
    fi

    # Create temp file to capture the agent's last message
    LAST_MSG_FILE="$(mktemp)"

    # Build codex command arguments
    # -C: working directory
    # --output-last-message: save final message to file for completion detection
    CODEX_ARGS=(exec -C "$ROOT_DIR" --output-last-message "$LAST_MSG_FILE")
    
    # Add model flag if specified
    if [[ -n "$MODEL" ]]; then
      CODEX_ARGS+=(-m "$MODEL")
    fi

    # Run codex with the prompt piped to stdin
    # - tee /dev/stderr: show output in real-time
    # - || true: don't abort loop on agent failure (max iterations is the backstop)
    cat "$PROMPT_FILE" | codex "${CODEX_ARGS[@]}" - 2>&1 | tee /dev/stderr || true

    # Check if agent signaled completion
    if grep -q "<promise>COMPLETE</promise>" "$LAST_MSG_FILE"; then
      echo "Done"
      rm -f "$LAST_MSG_FILE"
      exit 0  # Success! All stories complete
    fi

    # Clean up temp file
    rm -f "$LAST_MSG_FILE"
  fi

  # Optional guardrail: enforce allowed paths if configured (git repos only)
  enforce_allowed_paths_if_configured

  # -------------------------------------------------------------------------
  # INTERACTIVE MODE - Human-in-the-loop
  # -------------------------------------------------------------------------
  # If INTERACTIVE is enabled, pause and ask the human what to do next.
  # This allows reviewing changes before the next iteration.
  
  if [[ "$INTERACTIVE" =~ ^(1|true|yes)$ ]]; then
    echo ""
    echo "─────────────────────────────────────────────────────────────────────"
    echo "Iteration $i complete. Review the changes above."
    echo "─────────────────────────────────────────────────────────────────────"
    echo ""
    echo "  [Enter]  Continue to next iteration"
    echo "  [s]      Skip to completion (disable interactive mode)"
    echo "  [q]      Quit now"
    echo ""
    read -r -p "What next? " choice
    
    case "$choice" in
      s|S|skip)
        echo "Skipping to autonomous mode..."
        INTERACTIVE=""
        ;;
      q|Q|quit|exit)
        echo "Stopped by user"
        exit 0
        ;;
      *)
        # Continue to next iteration
        ;;
    esac
  fi

  # -------------------------------------------------------------------------
  # ITERATION DELAY
  # -------------------------------------------------------------------------
  # Sleep between iterations to avoid hammering APIs and allow the agent's
  # changes to settle (e.g., file writes, git operations)
  sleep "$SLEEP_SECONDS"
done

# -----------------------------------------------------------------------------
# MAX ITERATIONS REACHED
# -----------------------------------------------------------------------------
# If we get here, the agent never signaled completion within the allowed
# iterations. This is a safety backstop to prevent infinite loops.

echo "Max iterations reached"
exit 1
