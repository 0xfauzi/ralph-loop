"""Init command for Ralph - initialize harness in a project."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py import git
from ralph_py.prd import PRD

if TYPE_CHECKING:
    from ralph_py.ui.base import UI

# Default file contents
DEFAULT_PRD = {
    "branchName": "ralph/feature",
    "userStories": [],
}

DEFAULT_PROMPT = """# Ralph Agent Instructions

## Your Task (one iteration)

1. Read `scripts/ralph/prd.json`
2. Read `scripts/ralph/progress.txt` (check `## Codebase Patterns` first)
3. If `scripts/ralph/codebase_map.md` exists, scan its headers and read only sections
   relevant to your current story
   (skip unrelated sections to save context)
4. Branch is pre-checked out to `branchName` from `scripts/ralph/prd.json`
   (verify only; do not switch)
5. Pick the highest priority story where `passes` is `false` (lowest `priority` wins)
6. Implement that ONE story (keep the change small and focused)
7. Run feedback loops (Python + uv):
   - Find the project's fastest typecheck and tests
   - Use `uv run ...` to run them
   - If the project has no typecheck/tests configured, add them (prefer `ruff` + `mypy`
     or `pyright` + `pytest`)
     and ensure they run fast and deterministically
   - Do NOT mark the story as done unless typecheck AND tests pass. If they fail, fix and rerun;
     only proceed when both are green.
8. Update `AGENTS.md` files with reusable learnings
   (only if you discovered something worth preserving):
   - Only update `AGENTS.md` in directories you edited
   - Add patterns/gotchas/conventions, not story-specific notes
9. Commit with message: `feat: [ID] - [Title]`
10. Update `scripts/ralph/prd.json`: set that story's `passes` to `true`
    (only after tests/typecheck pass)
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

Add reusable patterns to the TOP section in `scripts/ralph/progress.txt`
under `## Codebase Patterns`.

## Stop Condition

If ALL stories pass, reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""

DEFAULT_PROGRESS = """# Ralph Progress Log

## Codebase Patterns
- (add reusable patterns here)

## Iteration Notes
- (append entries below using the format in prompt.md)

---
"""

DEFAULT_CODEBASE_MAP = """# Codebase Map (Brownfield Notes)

This file is meant to be built over time using the Ralph **codebase understanding** loop.

## How to use this map

- **Evidence-first**: prefer citations to specific files/entrypoints over broad claims.
- **Read-only mode**: in understanding mode, the agent should ONLY edit this file.
- **Small increments**: one topic per iteration keeps notes high-signal.

## Next Topics (checklist)

Edit this list to match your repo. During the understanding loop, mark items as done.

- [ ] How to run locally (setup, env vars, start commands)
- [ ] Build / test / lint / CI gates (what runs in CI and how)
- [ ] Repo topology & module boundaries (where code lives, layering rules)
- [ ] Entrypoints (server, worker, cron, CLI)
- [ ] Configuration, env vars, secrets, feature flags
- [ ] Authn/Authz (where permissions are enforced)
- [ ] Data model & persistence (migrations, ORM patterns, transactions)
- [ ] Core domain flow #1 (trace end-to-end)
- [ ] Core domain flow #2 (trace end-to-end)
- [ ] External integrations (third-party APIs, webhooks, queues)
- [ ] Observability (logging, metrics, tracing, error reporting)
- [ ] Deployment / release process

## Quick Facts (keep updated)

- **Language / framework**:
- **How to run**:
- **How to test**:
- **How to typecheck/lint**:
- **Primary entrypoints**:
- **Data store**:

## Known "Do Not Touch" Areas (optional)

- (add directories/files that are fragile or off-limits)

---

## Iteration Notes

(New notes append below; keep older notes for history.)
"""

DEFAULT_UNDERSTAND_PROMPT = """# Ralph Codebase Understanding Instructions (Read-Only)

## Goal (one iteration)

You are running a **codebase understanding** loop. Your job is to explore the existing codebase
and write an evidence-based "map" for humans.

**Hard rule:** do NOT modify application code, tests, configs, dependencies, or CI.

**The only file you may edit is:**
- `scripts/ralph/codebase_map.md`

If you think code changes are needed, write that as a note in the map under
**Open questions / Follow-ups**. Do not implement changes in this mode.

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
     7) Data model & persistence (migrations, ORM patterns)
     8) Core domain flows (trace one end-to-end)
     9) External integrations
     10) Observability (logging/metrics/tracing)
     11) Deployment / release process
3. Investigate by reading docs, configs, and code. Prefer fast, high-signal entrypoints:
   - README / docs
   - package/lock files
   - build/test scripts
   - app entrypoints (server/main)
   - routes/controllers
   - data layer (models, migrations)
4. Update **ONLY** `scripts/ralph/codebase_map.md`:
   - Append a new **Iteration Notes** section for this topic (template below)
   - If you used a Next Topics checklist, mark the topic as done (`[x]`)
   - Keep notes concise, factual, and verifiable

## Evidence rules (important)

- Every "fact" should include **evidence**:
  - File paths
  - What to look for (function/class name)
  - Preferably line ranges (if your tooling can provide them)
- If you are uncertain, label it clearly as a hypothesis and add an **Open question**.

## Iteration Notes format

Append this to the END of `scripts/ralph/codebase_map.md`:

## [YYYY-MM-DD] - [Topic]

- **Summary**: 1-3 bullets on what you learned
- **Evidence**:
  - `path/to/file.ext` - what to look for (and line range if available)
- **Conventions / invariants**:
  - "Do X, don't do Y" rules implied by the codebase
- **Risks / hotspots**:
  - Areas likely to break or require extra care
- **Open questions / follow-ups**:
  - What's unclear, what needs human confirmation

---

## Stop condition

If there are **no remaining unchecked topics** in the Next Topics checklist
(or you have covered the default list above), reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""

DEFAULT_PRD_PROMPT = """You are a product manager and QA lead. Produce a PRD as a single JSON object
meant to be saved as `scripts/ralph/prd.json`.

Output requirements:
- Output ONLY valid JSON (no Markdown, no code fences, no comments).
- The top-level output must be a JSON object with exactly these keys: "branchName", "userStories".
- Do not add any other top-level keys.
- "userStories" must be an array of story objects.
- Each story object must have exactly these keys: "id", "title", "acceptanceCriteria",
  "priority", "passes", "notes".
- Set "passes" to false for every story.
- Set "notes" to "" for every story.

Schema (example shape only, do not copy the text):
{
  "branchName": "ralph/feature-name",
  "userStories": [
    {
      "id": "US-001",
      "title": "Short description of the story",
      "acceptanceCriteria": [
        "First testable requirement",
        "Second testable requirement",
        "Typecheck passes: <typecheck command>",
        "Tests pass: <test command>"
      ],
      "priority": 1,
      "passes": false,
      "notes": ""
    }
  ]
}

Content rules:
- Stories must be small and atomic: each story should be implementable and verifiable in a single
  Ralph iteration.
- "priority": lower number = higher priority. Priorities must be unique and start at 1.
- "acceptanceCriteria": explicit, testable, ordered checks. Include verification commands with
  expected outcomes.
- Always include typecheck and test criteria using the commands specified in the context below.
- Do not invent UI elements, endpoints, or files that are not described in the context.
- Cover core flows first, then important edge cases implied by the context.
- If the context mentions existing patterns or conventions, criteria should verify the new code
  follows them.

================================================================================
CONTEXT - Fill in the sections below, then feed this entire file to an LLM
================================================================================

## Feature Overview
<!-- What are you building? Describe the feature in 2-3 sentences. -->



## Branch Name
<!-- What should the git branch be called? e.g., ralph/add-user-auth -->



## Target Users
<!-- Who will use this feature? What problem does it solve for them? -->



## Requirements
<!-- List the specific things this feature must do. Be concrete. -->
1. 
2. 
3. 

## Out of Scope
<!-- What are you NOT building? Helps the LLM avoid inventing extra features. -->
- 

## Tech Stack & Conventions
<!-- What technologies is the project using? What patterns should be followed? -->
- Language: 
- Framework: 
- Testing: 
- Other: 

## Existing Code Context
<!-- Describe relevant existing files, APIs, or patterns the new code should integrate with. -->



## Verification Commands
<!-- How should the agent verify the code works? Provide exact commands. -->
- Typecheck: 
- Tests: 
- Lint: 
- Other: 

## Constraints
<!-- Any limitations, performance requirements, or non-functional requirements? -->



## Additional Notes
<!-- Anything else the LLM should know? Edge cases, error handling, etc. -->
"""


def run_init(directory: Path, ui: UI) -> int:
    """Initialize Ralph harness in a project directory.

    Args:
        directory: Target project directory
        ui: UI for output

    Returns:
        Exit code (0=success, 1=validation failure, 2=directory not found)
    """
    ui.title("Ralph Init")

    # Validate directory
    ui.section("Target")
    if not directory.exists():
        ui.err(f"Directory not found: {directory}")
        return 2

    root = directory.resolve()
    ui.kv("Directory", str(root))

    # Check for git repo
    is_repo = git.is_git_repo(root)
    if is_repo:
        ui.ok("Git repository detected")
    else:
        ui.warn("Not a git repository")

    ui.section("Scaffold")
    ralph_dir = root / "scripts" / "ralph"
    if not ralph_dir.exists():
        ralph_dir.mkdir(parents=True, exist_ok=True)
        ui.ok("Created scripts/ralph/")
    else:
        ui.ok("scripts/ralph/ exists")

    ui.section("Create defaults")
    _create_if_missing(ralph_dir / "prompt.md", DEFAULT_PROMPT, ui)
    _create_if_missing(ralph_dir / "prd_prompt.txt", DEFAULT_PRD_PROMPT, ui)
    _create_if_missing(ralph_dir / "prd.json", json.dumps(DEFAULT_PRD, indent=2) + "\n", ui)
    _create_if_missing(ralph_dir / "progress.txt", DEFAULT_PROGRESS, ui)
    _create_if_missing(ralph_dir / "codebase_map.md", DEFAULT_CODEBASE_MAP, ui)
    _create_if_missing(ralph_dir / "understand_prompt.md", DEFAULT_UNDERSTAND_PROMPT, ui)

    # Validate PRD
    ui.section("Validate PRD")
    prd_file = ralph_dir / "prd.json"

    try:
        with open(prd_file) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        ui.err(f"Invalid JSON in prd.json: {e}")
        return 1

    errors = PRD.validate_schema(data)
    if errors:
        ui.err("PRD schema validation failed:")
        for error in errors:
            ui.info(f"  - {error}")
        return 1

    ui.ok("PRD schema valid")

    # PRD summary
    ui.section("PRD summary")
    prd = PRD.load(prd_file)
    ui.kv("Branch", prd.branch_name)
    ui.kv("Stories", str(len(prd.user_stories)))

    passing = sum(1 for s in prd.user_stories if s.passes)
    failing = len(prd.user_stories) - passing
    if prd.user_stories:
        ui.kv("Passing", str(passing))
        ui.kv("Failing", str(failing))

    # Next steps
    ui.section("Next steps")
    ui.info("1. Edit scripts/ralph/prompt.md")
    ui.info("2. Add user stories to scripts/ralph/prd.json")
    ui.info("3. Run: python -m ralph_py run [iterations]")
    ui.info("")
    ui.info("For codebase understanding mode:")
    ui.info("  python -m ralph_py understand [iterations]")

    return 0


def _create_if_missing(path: Path, content: str, ui: UI) -> None:
    """Create file if it doesn't exist."""
    if path.exists():
        ui.info(f"  {path.name} already exists")
    else:
        path.write_text(content)
        ui.ok(f"  Created {path.name}")
