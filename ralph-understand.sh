#!/usr/bin/env bash
# =============================================================================
# ralph-understand.sh - Ralph Codebase Understanding Loop (Brownfield)
# =============================================================================
# This is a convenience wrapper around `scripts/ralph/ralph.sh` for running a
# read-only “codebase understanding” loop.
#
# It selects the understanding prompt and writes findings into:
#   scripts/ralph/codebase_map.md
#
# Usage:
#   ./scripts/ralph/ralph-understand.sh [max_iterations]
#
# Notes:
# - The underlying loop is the same as ralph.sh.
# - This wrapper sets sensible defaults for understanding-mode:
#   - PROMPT_FILE -> understand_prompt.md
#   - ALLOWED_PATHS -> scripts/ralph/codebase_map.md (optional safety guard)
#
# You can still set:
#   AGENT_CMD / MODEL / SLEEP_SECONDS / INTERACTIVE
# =============================================================================

set -euo pipefail

MAX_ITERATIONS="${1:-10}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Use the understanding prompt by default (can be overridden)
export PROMPT_FILE="${PROMPT_FILE:-$SCRIPT_DIR/understand_prompt.md}"

# Path (relative to repo root) that is allowed to change in understanding mode.
# If you don't want enforcement, set ALLOWED_PATHS="".
export ALLOWED_PATHS="${ALLOWED_PATHS:-scripts/ralph/codebase_map.md}"

# Ensure the map file exists (create if missing)
MAP_FILE="$ROOT_DIR/scripts/ralph/codebase_map.md"
if [[ ! -f "$MAP_FILE" ]]; then
  cat >"$MAP_FILE" <<'EOF'
# Codebase Map (Brownfield Notes)

## Next Topics

- [ ] How to run locally
- [ ] Build / test / lint / CI gates
- [ ] Repo topology & module boundaries
- [ ] Entrypoints (server, worker, cron, CLI)
- [ ] Configuration, env vars, secrets, feature flags
- [ ] Authn/Authz
- [ ] Data model & persistence
- [ ] Core domain flow #1 (trace end-to-end)
- [ ] External integrations
- [ ] Observability
- [ ] Deployment / release process

---

## Iteration Notes

EOF
fi

exec "$SCRIPT_DIR/ralph.sh" "$MAX_ITERATIONS"

