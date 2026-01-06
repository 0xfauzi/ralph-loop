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

# Path to the prompt file that will be fed to the agent each iteration
PROMPT_FILE="$SCRIPT_DIR/prompt.md"

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

# -----------------------------------------------------------------------------
# STARTUP MESSAGE
# -----------------------------------------------------------------------------

echo "Starting Ralph"
echo "Root: $ROOT_DIR"
if [[ -n "$AGENT_CMD" ]]; then
  echo "Agent: custom ($AGENT_CMD)"
else
  echo "Agent: codex${MODEL:+ (model: $MODEL)}"
fi
echo "Max iterations: $MAX_ITERATIONS"
if [[ "$INTERACTIVE" =~ ^(1|true|yes)$ ]]; then
  echo "Mode: interactive (will pause after each iteration)"
fi

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

  # -------------------------------------------------------------------------
  # ITERATION DELAY
  # -------------------------------------------------------------------------
  # Sleep between iterations to avoid hammering APIs and allow the agent's
  # changes to settle (e.g., file writes, git operations)
  
  sleep "$SLEEP_SECONDS"

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
done

# -----------------------------------------------------------------------------
# MAX ITERATIONS REACHED
# -----------------------------------------------------------------------------
# If we get here, the agent never signaled completion within the allowed
# iterations. This is a safety backstop to prevent infinite loops.

echo "Max iterations reached"
exit 1
