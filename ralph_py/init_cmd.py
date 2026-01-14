"""Init command for Ralph - initialize harness in a project."""

from __future__ import annotations

import json
import os
import stat
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

DEFAULT_PROGRESS = """# Progress Log

## Patterns & Learnings
- (Add patterns discovered during iterations)

## Iteration Notes
"""

DEFAULT_CODEBASE_MAP = """# Codebase Map

## Next Topics
- [ ] How to run locally
- [ ] Build/test/lint/CI gates
- [ ] Repo topology & module boundaries
- [ ] Entrypoints
- [ ] Configuration & env vars
- [ ] Authn/Authz
- [ ] Data model & persistence
- [ ] Core domain flows
- [ ] External integrations
- [ ] Observability
- [ ] Deployment / release

## Notes
"""

DEFAULT_UNDERSTAND_PROMPT = """# Understanding Mode

You are in **read-only understanding mode**. Your goal is to map and document this codebase.

## Rules
1. **DO NOT** modify any application code
2. **ONLY** edit `scripts/ralph/codebase_map.md`
3. Work through the "Next Topics" checklist systematically
4. For each topic, document:
   - File paths and line ranges
   - Conventions and patterns
   - Risks and open questions

## Output
Update `scripts/ralph/codebase_map.md` with your findings. Use evidence-based notes with specific file references.

When you have thoroughly documented the codebase, output:
<promise>COMPLETE</promise>
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

    # Validate required files
    ui.section("Validate required files")
    ralph_dir = root / "scripts" / "ralph"

    if not ralph_dir.exists():
        ui.err(f"Required directory not found: scripts/ralph/")
        ui.info("Create the directory and add prompt.md to get started")
        return 1

    prompt_file = ralph_dir / "prompt.md"
    if not prompt_file.exists():
        ui.err(f"Required file not found: scripts/ralph/prompt.md")
        return 1
    ui.ok("prompt.md exists")

    ralph_sh = ralph_dir / "ralph.sh"
    if not ralph_sh.exists():
        ui.warn("ralph.sh not found (optional for Python version)")
    else:
        ui.ok("ralph.sh exists")

    # Create default files
    ui.section("Create defaults")
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

    # Make scripts executable
    ui.section("Permissions")
    for script in ["ralph.sh", "ralph-understand.sh"]:
        script_path = ralph_dir / script
        if script_path.exists():
            try:
                current = script_path.stat().st_mode
                script_path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                ui.ok(f"Made {script} executable")
            except Exception:
                ui.warn(f"Could not make {script} executable")

    # Next steps
    ui.section("Next steps")
    ui.info("1. Add user stories to scripts/ralph/prd.json")
    ui.info("2. Run: python -m ralph_py run [iterations]")
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
