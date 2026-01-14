"""CLI entry point for Ralph."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ralph_py import __version__
from ralph_py.agents import CodexAgent, get_agent
from ralph_py.config import RalphConfig
from ralph_py.init_cmd import run_init
from ralph_py.loop import run_loop
from ralph_py.ui import get_ui


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Ralph - Agentic loop harness for AI-driven development."""
    pass


@cli.command()
@click.argument("max_iterations", type=int, default=10)
@click.option(
    "--prompt", "-p",
    type=click.Path(exists=True, path_type=Path),
    help="Prompt file path",
)
@click.option(
    "--prd",
    type=click.Path(exists=True, path_type=Path),
    help="PRD file path",
)
@click.option(
    "--agent-cmd",
    envvar="AGENT_CMD",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model", "-m",
    envvar="MODEL",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    envvar="MODEL_REASONING_EFFORT",
    help="Reasoning effort for codex (low, medium, high)",
)
@click.option(
    "--sleep", "-s",
    type=float,
    default=2.0,
    envvar="SLEEP_SECONDS",
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive", "-i",
    is_flag=True,
    envvar="INTERACTIVE",
    help="Enable human-in-the-loop mode",
)
@click.option(
    "--branch",
    envvar="RALPH_BRANCH",
    help="Git branch to use (empty string to skip checkout)",
)
@click.option(
    "--allowed-paths",
    envvar="ALLOWED_PATHS",
    help="Comma-separated allowed paths for guardrails",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain"]),
    default="auto",
    envvar="RALPH_UI",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    envvar="NO_COLOR",
    help="Disable colors",
)
@click.option(
    "--ascii",
    is_flag=True,
    envvar="RALPH_ASCII",
    help="Use ASCII characters only",
)
def run(
    max_iterations: int,
    prompt: Path | None,
    prd: Path | None,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    sleep: float,
    interactive: bool,
    branch: str | None,
    allowed_paths: str | None,
    ui: str,
    no_color: bool,
    ascii: bool,
) -> None:
    """Run the agentic loop.

    MAX_ITERATIONS is the maximum number of iterations (default: 10).
    """
    cwd = Path.cwd()

    # Determine if branch was explicitly set
    branch_explicit = "RALPH_BRANCH" in click.get_current_context().params or branch is not None

    # Build config
    config = RalphConfig(
        max_iterations=max_iterations,
        prompt_file=prompt or (cwd / "scripts/ralph/prompt.md"),
        prd_file=prd or (cwd / "scripts/ralph/prd.json"),
        sleep_seconds=sleep,
        interactive=interactive,
        allowed_paths=[p.strip() for p in (allowed_paths or "").split(",") if p.strip()],
        ralph_branch=branch,
        ralph_branch_explicit=branch_explicit,
        agent_cmd=agent_cmd,
        model=model,
        model_reasoning_effort=reasoning,
        ui_mode=ui,
        no_color=no_color,
        ascii_only=ascii,
    )

    # Check codex availability if not using custom agent
    if not agent_cmd and not CodexAgent.is_available():
        ui_impl = get_ui(config.ui_mode, config.no_color, config.ascii_only)
        ui_impl.err("codex not found in PATH")
        ui_impl.info("Install codex or use --agent-cmd to specify a custom agent")
        sys.exit(1)

    # Get UI and agent
    ui_impl = get_ui(config.ui_mode, config.no_color, config.ascii_only)
    agent = get_agent(config.agent_cmd, config.model, config.model_reasoning_effort)

    # Run loop
    result = run_loop(config, ui_impl, agent, cwd)
    sys.exit(result.exit_code)


@cli.command()
@click.argument("directory", type=click.Path(path_type=Path), default=".")
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain"]),
    default="auto",
    envvar="RALPH_UI",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    envvar="NO_COLOR",
    help="Disable colors",
)
def init(directory: Path, ui: str, no_color: bool) -> None:
    """Initialize Ralph in a project directory.

    DIRECTORY is the target project directory (default: current directory).
    """
    ui_impl = get_ui(ui, no_color)
    exit_code = run_init(directory, ui_impl)
    sys.exit(exit_code)


@cli.command()
@click.argument("max_iterations", type=int, default=10)
@click.option(
    "--agent-cmd",
    envvar="AGENT_CMD",
    help="Custom agent command",
)
@click.option(
    "--model", "-m",
    envvar="MODEL",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    envvar="MODEL_REASONING_EFFORT",
    help="Reasoning effort for codex",
)
@click.option(
    "--sleep", "-s",
    type=float,
    default=2.0,
    envvar="SLEEP_SECONDS",
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive", "-i",
    is_flag=True,
    envvar="INTERACTIVE",
    help="Enable human-in-the-loop mode",
)
@click.option(
    "--branch",
    envvar="RALPH_BRANCH",
    help="Git branch (default: ralph/understanding)",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain"]),
    default="auto",
    envvar="RALPH_UI",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    envvar="NO_COLOR",
    help="Disable colors",
)
@click.option(
    "--ascii",
    is_flag=True,
    envvar="RALPH_ASCII",
    help="Use ASCII characters only",
)
def understand(
    max_iterations: int,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    sleep: float,
    interactive: bool,
    branch: str | None,
    ui: str,
    no_color: bool,
    ascii: bool,
) -> None:
    """Run codebase understanding loop (read-only mode).

    MAX_ITERATIONS is the maximum number of iterations (default: 10).

    This mode:
    - Uses understand_prompt.md instead of prompt.md
    - Only allows edits to codebase_map.md
    - Works on ralph/understanding branch by default
    """
    cwd = Path.cwd()
    ralph_dir = cwd / "scripts" / "ralph"

    # Create codebase_map.md if missing
    codebase_map = ralph_dir / "codebase_map.md"
    if not codebase_map.exists():
        from ralph_py.init_cmd import DEFAULT_CODEBASE_MAP
        codebase_map.write_text(DEFAULT_CODEBASE_MAP)

    # Set understanding mode defaults
    prompt_file = ralph_dir / "understand_prompt.md"
    allowed_paths = "scripts/ralph/codebase_map.md"
    default_branch = "ralph/understanding"

    # Use provided branch or default
    actual_branch = branch if branch is not None else default_branch
    branch_explicit = branch is not None or "RALPH_BRANCH" not in click.get_current_context().params

    config = RalphConfig(
        max_iterations=max_iterations,
        prompt_file=prompt_file,
        prd_file=cwd / "scripts/ralph/prd.json",
        sleep_seconds=sleep,
        interactive=interactive,
        allowed_paths=[allowed_paths],
        ralph_branch=actual_branch,
        ralph_branch_explicit=branch_explicit,
        agent_cmd=agent_cmd,
        model=model,
        model_reasoning_effort=reasoning,
        ui_mode=ui,
        no_color=no_color,
        ascii_only=ascii,
    )

    # Check codex availability
    if not agent_cmd and not CodexAgent.is_available():
        ui_impl = get_ui(config.ui_mode, config.no_color, config.ascii_only)
        ui_impl.err("codex not found in PATH")
        ui_impl.info("Install codex or use --agent-cmd to specify a custom agent")
        sys.exit(1)

    ui_impl = get_ui(config.ui_mode, config.no_color, config.ascii_only)
    agent = get_agent(config.agent_cmd, config.model, config.model_reasoning_effort)

    result = run_loop(config, ui_impl, agent, cwd)
    sys.exit(result.exit_code)


def main() -> None:
    """Main entry point."""
    cli(auto_envvar_prefix="RALPH")


if __name__ == "__main__":
    main()
