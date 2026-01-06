# Ralph Agent Instructions

## Your Task (one iteration)

1. Read `scripts/ralph/prd.json`
2. Read `scripts/ralph/progress.txt` (check `## Codebase Patterns` first)
3. Branch is pre-checked out to `branchName` from `scripts/ralph/prd.json` (verify only; do not switch)
4. Pick the highest priority story where `passes` is `false` (lowest `priority` wins)
5. Implement that ONE story (keep the change small and focused)
6. Run feedback loops (project’s typecheck/tests):
   - Find the project's fastest typecheck and tests
   - Run them (e.g., `uv run ...` or the project's commands)
   - If the project has no typecheck/tests configured, add them (prefer `ruff` + `mypy` or `pyright` + `pytest`) and ensure they run fast and deterministic
7. Do NOT mark the story as done unless typecheck AND tests pass. If they fail, fix and rerun; only proceed when both are green.
8. Update `AGENTS.md` files with reusable learnings (only if you discovered something worth preserving):
   - Only update `AGENTS.md` in directories you edited
   - Add patterns/gotchas/conventions, not story-specific notes
9. Commit with message: `feat: [ID] - [Title]`
10. Update `scripts/ralph/prd.json`: set that story's `passes` to `true` (only after tests/typecheck pass)
11. Append learnings to `scripts/ralph/progress.txt`

## Progress Format

Append this to the END of `scripts/ralph/progress.txt`:

## [YYYY-MM-DD] - [Story ID]
- What was implemented
- Files changed
- Verification run (exact commands)
- **Learnings:**
  - Patterns discovered
  - Gotchas encountered
---

## Codebase Patterns

Add reusable patterns to the TOP section in `scripts/ralph/progress.txt` under `## Codebase Patterns`.

## Stop Condition

If ALL stories pass, reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.

