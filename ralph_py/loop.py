"""Main agentic loop for Ralph."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py import git, guards
from ralph_py.prd import PRD

if TYPE_CHECKING:
    from ralph_py.agents.base import Agent
    from ralph_py.config import RalphConfig
    from ralph_py.ui.base import UI

COMPLETION_MARKER = "<promise>COMPLETE</promise>"


@dataclass
class LoopResult:
    """Result of running the agentic loop."""

    completed: bool
    iterations: int
    exit_code: int


def run_loop(config: RalphConfig, ui: UI, agent: Agent, cwd: Path | None = None) -> LoopResult:
    """Run the main agentic loop.

    Args:
        config: Ralph configuration
        ui: UI implementation for output
        agent: Agent to run
        cwd: Working directory (defaults to current)

    Returns:
        LoopResult with completion status and exit code
    """
    if cwd is None:
        cwd = Path.cwd()

    # Display title
    ui.title("Ralph")

    # Display startup info
    ui.section("Startup")
    ui.kv("Root", str(cwd))
    ui.kv("Prompt", str(config.prompt_file))
    ui.kv("PRD", str(config.prd_file))
    ui.kv("Agent", agent.name)
    ui.kv("Max iterations", str(config.max_iterations))
    ui.kv("Sleep", f"{config.sleep_seconds}s")
    ui.kv("Interactive", "yes" if config.interactive else "no")
    ui.kv("Allowed paths", ", ".join(config.allowed_paths) if config.allowed_paths else "<disabled>")
    ui.kv("Reasoning", config.model_reasoning_effort or "<default>")
    ui.kv("UI", config.ui_mode)

    # Validate prompt file
    if not config.prompt_file.exists():
        ui.err(f"Prompt file not found: {config.prompt_file}")
        return LoopResult(completed=False, iterations=0, exit_code=1)

    # Read prompt
    prompt = config.prompt_file.read_text()

    # Preflight
    ui.section("Preflight")

    # Git/Branch handling
    ui.subsection("Git / Branch")
    is_repo = git.is_git_repo(cwd)

    if not is_repo:
        ui.warn("Not a git repository")
    else:
        branch, source = _determine_branch(config)
        if branch:
            if not git.checkout_branch(branch, ui, cwd, source):
                ui.err(f"Failed to checkout branch: {branch}")
                return LoopResult(completed=False, iterations=0, exit_code=1)
        elif branch == "":
            ui.info("Branch: RALPH_BRANCH is set but empty; skipping branch checkout")
        else:
            ui.info("Branch: no branch configured")

    # Guardrails info
    ui.subsection("Guardrails")
    if config.allowed_paths and is_repo:
        ui.info(f"Enforcing ALLOWED_PATHS={','.join(config.allowed_paths)}")
    else:
        ui.info("ALLOWED_PATHS is empty; enforcement disabled")

    # Main loop
    for iteration in range(1, config.max_iterations + 1):
        ui.section(f"Iteration {iteration} / {config.max_iterations}")

        # Run agent
        ui.channel_header("AI", "Agent output")
        output_lines: list[str] = []
        for line in agent.run(prompt, cwd):
            ui.stream_line("AI", line)
            output_lines.append(line)
        ui.channel_footer("AI", "Agent output")

        # Check for completion
        output = "\n".join(output_lines)
        if COMPLETION_MARKER in output:
            ui.ok("Done")
            return LoopResult(completed=True, iterations=iteration, exit_code=0)

        # Enforce ALLOWED_PATHS
        if config.allowed_paths and is_repo:
            ok, _ = guards.enforce_allowed_paths(config, ui, cwd)
            if not ok:
                return LoopResult(completed=False, iterations=iteration, exit_code=1)

        # Interactive pause
        if config.interactive and ui.can_prompt():
            choice = ui.choose(
                "Iteration complete. What next?",
                ["Continue", "Skip interactive", "Quit"],
                default=0,
            )
            if choice == 1:
                # Disable interactive for remaining iterations
                config.interactive = False
            elif choice == 2:
                return LoopResult(completed=False, iterations=iteration, exit_code=0)

        # Sleep before next iteration (except on last)
        if iteration < config.max_iterations:
            time.sleep(config.sleep_seconds)

    # Max iterations reached
    ui.warn(f"Max iterations reached (no {COMPLETION_MARKER} seen)")
    return LoopResult(completed=False, iterations=config.max_iterations, exit_code=1)


def _determine_branch(config: RalphConfig) -> tuple[str | None, str | None]:
    """Determine which branch to use.

    Returns:
        Tuple of (branch_name, source) where:
        - branch_name: Branch to checkout, "" to skip, None if not configured
        - source: Source description (e.g. "from RALPH_BRANCH", "from PRD")
    """
    # RALPH_BRANCH takes precedence if explicitly set
    if config.ralph_branch_explicit:
        return config.ralph_branch, "from RALPH_BRANCH"

    # Try to get from PRD
    if config.prd_file.exists():
        try:
            prd = PRD.load(config.prd_file)
            if prd.branch_name:
                return prd.branch_name, "from PRD"
        except Exception:
            pass

    return None, None
