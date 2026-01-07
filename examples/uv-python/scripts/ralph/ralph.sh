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
#   MODEL_REASONING_EFFORT - Codex reasoning effort override (e.g., "low", "medium", "high", "xhigh")
#   SLEEP_SECONDS - Seconds between iterations (default: 2)
#   INTERACTIVE   - Set to "1" for human-in-the-loop mode (pause after each iteration)
#   PROMPT_FILE   - Override prompt file path (default: scripts/ralph/prompt.md)
#   ALLOWED_PATHS - Comma-separated list of repo-root-relative paths allowed to change (git repos only)
#   RALPH_BRANCH  - (git repos only) Branch to checkout/create before running.
#                  Takes precedence over PRD_FILE's branchName when set.
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
# UI (gum optional)
# -----------------------------------------------------------------------------

UI_FILE="$SCRIPT_DIR/ui.sh"
if [[ -f "$UI_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$UI_FILE"
else
  # Minimal UI fallback (keeps script usable if ui.sh wasn't copied).
  ui_title() { echo ""; echo "$1"; echo ""; }
  ui_section() { echo ""; echo "== $1 =="; }
  ui_subsection() { echo "-- $1 --"; }

  ui_box_fd() { local fd="$1"; local line; while IFS= read -r line || [[ -n "$line" ]]; do printf '  %s\n' "$line" >&"$fd"; done; }
  ui_box() { ui_box_fd 1; }
  ui_box_err() { ui_box_fd 2; }

  ui_kv_fd() { local fd="$1"; printf '%-14s %s\n' "${2}:" "$3" >&"$fd"; }
  ui_kv() { ui_kv_fd 1 "$1" "$2"; }

  ui_info() { echo "$1"; }
  ui_info_err() { echo "$1" >&2; }
  ui_ok() { echo "OK: $1"; }
  ui_ok_err() { echo "OK: $1" >&2; }
  ui_warn() { echo "WARN: $1"; }
  ui_warn_err() { echo "WARN: $1" >&2; }
  ui_err() { echo "ERROR: $1" >&2; }
  ui_err_err() { echo "ERROR: $1" >&2; }

  ui_channel_header_err() { echo "" >&2; echo "---- $1${2:+ · $2} ----" >&2; }
  ui_channel_footer_err() { echo "end: $1${2:+ · $2}" >&2; }

  ui_can_prompt() { [[ -t 0 ]]; }
  ui_choose_fd() { local _fd="$1"; local _header="$2"; local _default="$3"; shift 3; printf '%s\n' "$_default"; }

  ui_print_prefixed_fd() { local fd="$1"; local tag="$2"; local line="${3-}"; local sep="|"; [[ -z "$line" ]] && { printf '\n' >&"$fd"; return 0; }; printf '%s %s %s\n' "$tag" "$sep" "$line" >&"$fd"; }

  ui_stream_prefix_fd() { local fd="$1"; local tag="$2"; local sep="|"; local line; while IFS= read -r line || [[ -n "$line" ]]; do printf '%s %s %s\n' "$tag" "$sep" "$line" >&"$fd"; done; }
  ui_tee_prefix_err() { local tag="$1"; local sep="|"; local line; while IFS= read -r line || [[ -n "$line" ]]; do printf '%s\n' "$line"; printf '%s %s %s\n' "$tag" "$sep" "$line" >&2; done; }

  ui_tee_ai_pretty_err() { ui_tee_prefix_err AI; }
  ui_codex_pretty_stream_fd() { local fd="$1"; shift; ui_stream_prefix_fd "$fd" "AI"; }

  ui_mode() { printf '%s' 'plain'; }
fi

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

# Optional reasoning effort override for codex models (if supported by your model).
# Values are model-dependent (commonly: minimal|low|medium|high|xhigh).
MODEL_REASONING_EFFORT="${MODEL_REASONING_EFFORT:-}"

# Optional branch override for git repos (takes precedence over PRD branchName)
# Note: do not assign RALPH_BRANCH here; use ${RALPH_BRANCH-} / ${RALPH_BRANCH+x}
# expansions to remain `set -u` safe while still detecting whether the variable
# was explicitly set (even if empty).

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
  # Bash 3.2 + `set -u`: expanding an empty array like "${allowed[@]}" errors.
  for raw in "${allowed[@]+"${allowed[@]}"}"; do
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
  local unique=()
  # Bash 3.2 + `set -u`: expanding an empty array like "${changed_files[@]}" errors.
  for f in "${changed_files[@]+"${changed_files[@]}"}"; do
    # Bash 3.2 (macOS default) does not support associative arrays, so do a
    # simple linear dedupe here. The list is typically small, so O(n^2) is fine.
    local already_seen=""
    local u
    for u in "${unique[@]+"${unique[@]}"}"; do
      if [[ "$u" == "$f" ]]; then
        already_seen="1"
        break
      fi
    done
    [[ -z "$already_seen" ]] && unique+=("$f")
  done

  # Compute disallowed
  local disallowed=()
  for f in "${unique[@]+"${unique[@]}"}"; do
    if ! path_is_allowed "$f"; then
      disallowed+=("$f")
    fi
  done

  if (( ${#disallowed[@]} == 0 )); then
    return 0
  fi

  ui_channel_header_err "GUARD" "Disallowed changes"
  {
    ui_kv_fd 1 "ALLOWED_PATHS" "$ALLOWED_PATHS"
    echo ""
    echo "Disallowed files:"
    for f in "${disallowed[@]}"; do
      echo "  - $f"
    done
  } | ui_box_err

  if [[ "$INTERACTIVE" =~ ^(1|true|yes)$ ]]; then
    local action=""
    action="$(ui_choose_fd 2 "Disallowed changes detected — choose an action" "Quit" \
      "Quit" "Revert and continue" "Continue anyway")"

    case "$action" in
      "Revert and continue")
        ui_info_err "Reverting disallowed changes..."
        for f in "${disallowed[@]}"; do
          if git ls-files --error-unmatch -- "$f" >/dev/null 2>&1; then
            git restore --staged --worktree -- "$f" >/dev/null 2>&1 || true
          else
            rm -rf -- "$ROOT_DIR/$f" >/dev/null 2>&1 || true
          fi
        done
        ;;
      "Continue anyway")
        ui_warn_err "Continuing anyway (leaving disallowed changes as-is)."
        ;;
      *)
        ui_err_err "Stopped (no selection / no TTY / quit)."
        exit 1
        ;;
    esac
  else
    ui_err_err "Disallowed changes detected. Set INTERACTIVE=1 to review/revert, or clear ALLOWED_PATHS to disable enforcement."
    exit 1
  fi
}

auto_checkout_branch_from_prd() {
  # Automatically ensure we are on a desired branch:
  # - If RALPH_BRANCH is set, use it (highest priority).
  # - Else, use branchName from PRD_FILE if present.
  # No-op if git repo is absent, PRD file is missing, or no branch is specified.
  is_git_repo || return 0

  local branch=""
  local source=""

  if [[ "${RALPH_BRANCH+x}" == "x" ]]; then
    # RALPH_BRANCH was explicitly set (even if empty).
    if [[ -z "${RALPH_BRANCH-}" ]]; then
      ui_info "Branch: RALPH_BRANCH is set but empty; skipping branch checkout"
      return 0
    fi
    branch="${RALPH_BRANCH-}"
    source="RALPH_BRANCH"
  else
    [[ -f "$PRD_FILE" ]] || { ui_info "Branch: $PRD_FILE not found, skipping branch checkout"; return 0; }
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
    source="$PRD_FILE"
  fi

  if [[ -z "$branch" ]]; then
    ui_info "Branch: branchName missing in $PRD_FILE; skipping branch checkout"
    return 0
  fi

  local current
  current="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
  if [[ "$current" == "$branch" ]]; then
    ui_info "Branch: already on $branch"
    return 0
  fi

  if git show-ref --verify --quiet "refs/heads/$branch"; then
    ui_info "Branch: switching to existing $branch${source:+ (from $source)}"
    git checkout "$branch" 2>&1 | ui_stream_prefix_fd 1 "GIT"
  else
    ui_info "Branch: creating branch $branch${source:+ (from $source)}"
    git checkout -b "$branch" 2>&1 | ui_stream_prefix_fd 1 "GIT"
  fi
}

# -----------------------------------------------------------------------------
# STARTUP MESSAGE
# -----------------------------------------------------------------------------

ui_title "Ralph"

ui_section "Startup"
{
  ui_kv_fd 1 "Root" "$ROOT_DIR"
  ui_kv_fd 1 "Prompt" "$PROMPT_FILE"
  ui_kv_fd 1 "PRD" "$PRD_FILE"
  if [[ -n "$AGENT_CMD" ]]; then
    ui_kv_fd 1 "Agent" "custom ($AGENT_CMD)"
  else
    ui_kv_fd 1 "Agent" "codex${MODEL:+ (model: $MODEL)}"
  fi
  ui_kv_fd 1 "Max iterations" "$MAX_ITERATIONS"
  ui_kv_fd 1 "Sleep" "${SLEEP_SECONDS}s"
  ui_kv_fd 1 "Interactive" "$(if [[ "$INTERACTIVE" =~ ^(1|true|yes)$ ]]; then echo yes; else echo no; fi)"
  ui_kv_fd 1 "Allowed paths" "$(if [[ -n "$ALLOWED_PATHS" ]]; then echo "$ALLOWED_PATHS"; else echo "<disabled>"; fi)"
  ui_kv_fd 1 "Reasoning" "$(if [[ -n "$MODEL_REASONING_EFFORT" ]]; then echo "$MODEL_REASONING_EFFORT"; else echo "<default>"; fi)"
  ui_kv_fd 1 "UI" "$(ui_mode)"
} | ui_box

if [[ "$INTERACTIVE" =~ ^(1|true|yes)$ ]] && ! ui_can_prompt; then
  ui_warn "Interactive mode is enabled but stdin is not a TTY; prompts will auto-select defaults."
fi

ui_section "Preflight"
ui_subsection "Git / Branch"
if is_git_repo; then
  # Automatically align to desired branch (best effort)
  auto_checkout_branch_from_prd
else
  ui_info "Git repo: no (branch management disabled)"
fi

ui_subsection "Guardrails"
if [[ -z "$ALLOWED_PATHS" ]]; then
  ui_info "ALLOWED_PATHS is empty; enforcement disabled"
elif ! is_git_repo; then
  ui_info "Git repo: no; ALLOWED_PATHS enforcement disabled"
else
  ui_info "Enforcing ALLOWED_PATHS=$ALLOWED_PATHS"
  enforce_allowed_paths_if_configured
fi

# -----------------------------------------------------------------------------
# MAIN LOOP
# -----------------------------------------------------------------------------
# Run the agent repeatedly until it signals completion or we hit max iterations

for i in $(seq 1 "$MAX_ITERATIONS"); do
  ui_section "Iteration $i / $MAX_ITERATIONS"
  ITER_START_SECONDS="$SECONDS"

  # -------------------------------------------------------------------------
  # CUSTOM AGENT COMMAND (takes precedence if set)
  # -------------------------------------------------------------------------
  # If AGENT_CMD is set, use it directly. The prompt is piped to stdin.
  # This allows any CLI tool that accepts input on stdin.
  
  if [[ -n "$AGENT_CMD" ]]; then
    # Run custom agent command with prompt on stdin
    # - bash -lc: run in login shell for full environment
    # - ui_tee_prefix_err: show output in real-time (prefixed) while capturing raw
    # - || true: don't abort loop on agent failure
    ui_channel_header_err "AI" "Agent output"
    OUTPUT="$(cat "$PROMPT_FILE" | bash -lc "$AGENT_CMD" 2>&1 | ui_tee_ai_pretty_err)" || true
    ui_channel_footer_err "AI" "Agent output"

    # Check if agent signaled completion
    # The magic marker is: <promise>COMPLETE</promise>
    if echo "$OUTPUT" | grep -q "<promise>COMPLETE</promise>"; then
      ui_ok "Done"
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
      ui_err_err "codex not found in PATH"
      ui_info_err "Install codex or set AGENT_CMD to use a different agent"
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
    
    # Add reasoning effort override if specified (Codex config key)
    if [[ -n "$MODEL_REASONING_EFFORT" ]]; then
      CODEX_ARGS+=(-c "model_reasoning_effort=\"$MODEL_REASONING_EFFORT\"")
    fi

    # Run codex with the prompt piped to stdin
    # - ui_stream_prefix_fd: show output in real-time with AI demarcation
    # - || true: don't abort loop on agent failure (max iterations is the backstop)
    ui_channel_header_err "AI" "Codex output"
    if [[ "${RALPH_AI_RAW-}" == "1" ]]; then
      cat "$PROMPT_FILE" | codex "${CODEX_ARGS[@]}" - 2>&1 | ui_stream_prefix_fd 2 "AI" || true
    else
      cat "$PROMPT_FILE" | codex "${CODEX_ARGS[@]}" - 2>&1 | ui_codex_pretty_stream_fd 2 "$PROMPT_FILE" || true
    fi
    ui_channel_footer_err "AI" "Codex output"

    # Always show the final assistant message as a reliable fallback. This avoids
    # cases where the streaming transcript format changes and we miss AI lines.
    if [[ "${RALPH_AI_SHOW_FINAL-1}" != "0" ]]; then
      if [[ -s "$LAST_MSG_FILE" ]]; then
        ui_channel_header_err "AI" "Final message"
        cat "$LAST_MSG_FILE" | ui_ai_pretty_stream_fd 2 "AI"
        ui_channel_footer_err "AI" "Final message"
      else
        ui_warn_err "No final message captured (LAST_MSG_FILE is empty)"
      fi
    fi

    # Check if agent signaled completion
    if grep -q "<promise>COMPLETE</promise>" "$LAST_MSG_FILE"; then
      ui_ok "Done"
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
    ui_channel_header_err "USER" "Iteration review"
    ITER_ELAPSED_SECONDS=$((SECONDS - ITER_START_SECONDS))
    choice="$(ui_choose_fd 2 "Iteration $i complete (${ITER_ELAPSED_SECONDS}s) — what next?" "Continue" \
      "Continue" "Skip interactive (autonomous)" "Quit")"
    ui_info_err "Selected: $choice"
    ui_channel_footer_err "USER" "Iteration review"
    
    case "$choice" in
      "Skip interactive (autonomous)")
        ui_info "Skipping to autonomous mode..."
        INTERACTIVE=""
        ;;
      "Quit")
        ui_info "Stopped by user"
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

ui_warn "Max iterations reached (no <promise>COMPLETE</promise> seen)"
exit 1
