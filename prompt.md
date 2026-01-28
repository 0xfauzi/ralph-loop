# Ralph Agent Instructions

## Your Task (one iteration)

1. Read the PRD file for this run (default: `scripts/ralph/prd.json`)
2. Read `scripts/ralph/progress.txt` (check `## Codebase Patterns` first)
3. Derive a short list of keywords from the PRD intent, not just exact wording.
4. If `scripts/ralph/codebase_map.md` exists, query it for sections relevant to your story using those keywords.
   - Do not load the entire file.
   - Always check **Quick Facts** and any relevant **Iteration Notes**.
5. If a feature understand file exists for this PRD, query it using the same keywords.
   - Default path: `scripts/ralph/feature/<feature_name>/understand.md`
   - If the PRD is at `scripts/ralph/feature/<feature_name>/prd.json`, use that folder name.
   - Otherwise use the PRD filename stem as `<feature_name>`.
6. Branch is pre-checked out to `branchName` from the PRD (verify only; do not switch)
7. Pick the highest priority story where `passes` is `false` (lowest `priority` wins)
8. Implement that ONE story (keep the change small and focused)
9. Run feedback loops (Python + uv):
   - Find the project's fastest typecheck and tests
   - Use `uv run ...` to run them
   - If the project has no typecheck/tests configured, add them (prefer `ruff` + `mypy` or `pyright` + `pytest`) and ensure they run fast and deterministically
   - Do NOT mark the story as done unless typecheck AND tests pass. If they fail, fix and rerun; only proceed when both are green.
10. If you discover durable, reusable codebase facts, append a brief, evidence-based note to `scripts/ralph/codebase_map.md` under **Iteration Notes** or update **Quick Facts** (skip if nothing new).
11. Update `AGENTS.md` files with reusable learnings (only if you discovered something worth preserving):
   - Only update `AGENTS.md` in directories you edited
   - Add patterns/gotchas/conventions, not story-specific notes
12. Commit with message: `feat: [ID] - [Title]`
13. Update `scripts/ralph/prd.json`: set that story's `passes` to `true` (only after tests/typecheck pass)
14. Append learnings to `scripts/ralph/progress.txt`

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
