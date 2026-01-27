"""CLI entry point for Ralph."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from click.core import ParameterSource

from ralph_py import __version__
from ralph_py.agents import CodexAgent, get_agent
from ralph_py.config import RalphConfig, _parse_paths
from ralph_py.init_cmd import run_init
from ralph_py.loop import run_loop
from ralph_py.ui import get_ui
from ralph_py.ui.base import UI


def _use_cli_value(ctx: click.Context, name: str) -> bool:
    return ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE


def _resolve_root(root: Path | None, prompt: Path | None, prd: Path | None) -> Path:
    if root is not None:
        return root.resolve()

    for candidate in (prompt, prd):
        if candidate is None:
            continue
        resolved = candidate.resolve()
        parent = resolved.parent
        if parent.name == "ralph" and parent.parent.name == "scripts":
            return parent.parent.parent

    return Path.cwd()


def _resolve_path(root: Path, value: str | None, default: Path) -> Path:
    if value is None or value == "":
        return default
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def _normalize_ui_mode(value: str) -> str:
    normalized = (value or "auto").strip().lower()
    if normalized == "gum":
        return "rich"
    if normalized in {"plain", "off", "no", "0"}:
        return "plain"
    if normalized not in {"auto", "rich", "plain", "textual"}:
        return "auto"
    return normalized


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Ralph - Agentic loop harness for AI-driven development."""
    pass


@cli.command()
@click.argument("max_iterations", type=int, default=10)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prompt", "-p",
    type=str,
    help="Prompt file path",
)
@click.option(
    "--prd",
    type=str,
    help="PRD file path",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model", "-m",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort for codex (low, medium, high)",
)
@click.option(
    "--sleep", "-s",
    type=float,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive", "-i",
    is_flag=True,
    help="Enable human-in-the-loop mode",
)
@click.option(
    "--branch",
    help="Git branch to use (empty string to skip checkout)",
)
@click.option(
    "--allowed-paths",
    help="Comma-separated allowed paths for guardrails",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum", "textual"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
@click.option(
    "--ascii",
    is_flag=True,
    help="Use ASCII characters only",
)
@click.option(
    "--ai-raw/--ai-no-raw",
    default=None,
    help="Stream raw Codex output without transcript parsing",
)
@click.option(
    "--ai-show-prompt/--ai-hide-prompt",
    default=None,
    help="Show the echoed prompt in Codex output",
)
@click.option(
    "--ai-show-final/--ai-hide-final",
    default=None,
    help="Show the final assistant message",
)
@click.option(
    "--ai-prompt-progress-every",
    type=int,
    default=None,
    help="Emit a prompt suppression marker every N lines (0 disables)",
)
@click.option(
    "--ai-tool-mode",
    type=click.Choice(["summary", "full", "none"]),
    default=None,
    help="Tool output display mode",
)
@click.option(
    "--ai-sys-mode",
    type=click.Choice(["summary", "full"]),
    default=None,
    help="System output display mode",
)
def run(
    max_iterations: int,
    root: Path | None,
    prompt: str | None,
    prd: str | None,
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
    ai_raw: bool | None,
    ai_show_prompt: bool | None,
    ai_show_final: bool | None,
    ai_prompt_progress_every: int | None,
    ai_tool_mode: str | None,
    ai_sys_mode: str | None,
) -> None:
    """Run the agentic loop.

    MAX_ITERATIONS is the maximum number of iterations (default: 10).
    """
    ctx = click.get_current_context()
    env_prompt = os.environ.get("PROMPT_FILE")
    env_prd = os.environ.get("PRD_FILE")

    prompt_for_root = (
        Path(prompt) if _use_cli_value(ctx, "prompt") and prompt is not None else None
    )
    if prompt_for_root is None and env_prompt is not None:
        prompt_for_root = Path(env_prompt)

    prd_for_root = Path(prd) if _use_cli_value(ctx, "prd") and prd is not None else None
    if prd_for_root is None and env_prd is not None:
        prd_for_root = Path(env_prd)

    root_value = root if _use_cli_value(ctx, "root") else None
    root_dir = _resolve_root(root_value, prompt_for_root, prd_for_root)

    # Build config from environment defaults first.
    config = RalphConfig.from_env(root_dir)

    # Apply CLI overrides when explicitly provided.
    if _use_cli_value(ctx, "max_iterations"):
        config.max_iterations = max_iterations
    if _use_cli_value(ctx, "prompt"):
        config.prompt_file = _resolve_path(
            root_dir, prompt, root_dir / "scripts/ralph/prompt.md"
        )
    if _use_cli_value(ctx, "prd"):
        config.prd_file = _resolve_path(
            root_dir, prd, root_dir / "scripts/ralph/prd.json"
        )
    if _use_cli_value(ctx, "sleep"):
        config.sleep_seconds = sleep
    if _use_cli_value(ctx, "interactive"):
        config.interactive = interactive
    if _use_cli_value(ctx, "allowed_paths"):
        config.allowed_paths = _parse_paths(allowed_paths)
    if _use_cli_value(ctx, "branch"):
        config.ralph_branch = branch
        config.ralph_branch_explicit = True
    if _use_cli_value(ctx, "agent_cmd"):
        config.agent_cmd = agent_cmd
    if _use_cli_value(ctx, "model"):
        config.model = model
    if _use_cli_value(ctx, "reasoning"):
        config.model_reasoning_effort = reasoning
    if _use_cli_value(ctx, "ui"):
        config.ui_mode = _normalize_ui_mode(ui)
    if _use_cli_value(ctx, "no_color"):
        config.no_color = no_color
    if _use_cli_value(ctx, "ascii"):
        config.ascii_only = ascii
    if _use_cli_value(ctx, "ai_raw"):
        config.ai_raw = bool(ai_raw)
    if _use_cli_value(ctx, "ai_show_prompt"):
        config.ai_show_prompt = bool(ai_show_prompt)
    if _use_cli_value(ctx, "ai_show_final"):
        config.ai_show_final = bool(ai_show_final)
    if _use_cli_value(ctx, "ai_prompt_progress_every") and ai_prompt_progress_every is not None:
        config.ai_prompt_progress_every = ai_prompt_progress_every
    if _use_cli_value(ctx, "ai_tool_mode") and ai_tool_mode is not None:
        config.ai_tool_mode = ai_tool_mode
    if _use_cli_value(ctx, "ai_sys_mode") and ai_sys_mode is not None:
        config.ai_sys_mode = ai_sys_mode

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

    if config.ui_mode == "textual":
        from ralph_py.ui.textual_ui import run_textual_app

        def runner(ui: UI) -> int:
            if config.max_iterations < 0:
                ui.err(
                    f"MAX_ITERATIONS must be non-negative (got: {config.max_iterations})"
                )
                return 2
            if not config.agent_cmd and not CodexAgent.is_available():
                ui.err("codex not found in PATH")
                ui.info("Install codex or use --agent-cmd to specify a custom agent")
                return 1
            agent = get_agent(config.agent_cmd, config.model, config.model_reasoning_effort)
            result = run_loop(config, ui, agent, root_dir)
            return result.exit_code

        exit_code = run_textual_app(runner, mode_label="run")
        sys.exit(exit_code)

    # Check codex availability if not using custom agent
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(
        config.ui_mode,
        config.no_color,
        config.ascii_only,
        force_rich=force_rich,
    )

    if config.max_iterations < 0:
        ui_impl.err(
            f"MAX_ITERATIONS must be non-negative (got: {config.max_iterations})"
        )
        sys.exit(2)

    if not config.agent_cmd and not CodexAgent.is_available():
        ui_impl.err("codex not found in PATH")
        ui_impl.info("Install codex or use --agent-cmd to specify a custom agent")
        sys.exit(1)

    # Get UI and agent
    agent = get_agent(config.agent_cmd, config.model, config.model_reasoning_effort)

    # Run loop
    result = run_loop(config, ui_impl, agent, root_dir)
    sys.exit(result.exit_code)


@cli.command()
@click.argument("directory", type=click.Path(path_type=Path), default=".")
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum", "textual"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
def init(directory: Path, ui: str, no_color: bool) -> None:
    """Initialize Ralph in a project directory.

    DIRECTORY is the target project directory (default: current directory).
    """
    mode = _normalize_ui_mode(ui)
    if mode == "textual":
        from ralph_py.ui.textual_ui import run_textual_app

        def runner(ui_impl: UI) -> int:
            return run_init(directory, ui_impl)

        exit_code = run_textual_app(runner, mode_label="init")
        sys.exit(exit_code)

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(mode, no_color, force_rich=force_rich)
    exit_code = run_init(directory, ui_impl)
    sys.exit(exit_code)


@cli.command()
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    help="Transport mode",
)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path",
)
@click.option(
    "--host",
    help="HTTP host",
)
@click.option(
    "--port",
    type=int,
    help="HTTP port",
)
@click.option(
    "--log-dir",
    type=click.Path(path_type=Path),
    default=Path(".ralph/logs"),
    help="Log directory",
)
def mcp(
    transport: str,
    root: Path | None,
    host: str | None,
    port: int | None,
    log_dir: Path,
) -> None:
    """Run the MCP server."""
    from ralph_py import mcp_server

    args = ["--transport", transport, "--log-dir", str(log_dir)]
    if root is not None:
        args.extend(["--root", str(root)])
    if host is not None:
        args.extend(["--host", host])
    if port is not None:
        args.extend(["--port", str(port)])

    mcp_server.main(args)


@cli.command()
@click.argument("max_iterations", type=int, default=10)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prompt", "-p",
    type=str,
    help="Prompt file path",
)
@click.option(
    "--prd",
    type=str,
    help="PRD file path",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command",
)
@click.option(
    "--model", "-m",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort for codex",
)
@click.option(
    "--sleep", "-s",
    type=float,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive", "-i",
    is_flag=True,
    help="Enable human-in-the-loop mode",
)
@click.option(
    "--branch",
    help="Git branch (default: ralph/understanding)",
)
@click.option(
    "--allowed-paths",
    help="Comma-separated allowed paths for guardrails",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum", "textual"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
@click.option(
    "--ascii",
    is_flag=True,
    help="Use ASCII characters only",
)
@click.option(
    "--ai-raw/--ai-no-raw",
    default=None,
    help="Stream raw Codex output without transcript parsing",
)
@click.option(
    "--ai-show-prompt/--ai-hide-prompt",
    default=None,
    help="Show the echoed prompt in Codex output",
)
@click.option(
    "--ai-show-final/--ai-hide-final",
    default=None,
    help="Show the final assistant message",
)
@click.option(
    "--ai-prompt-progress-every",
    type=int,
    default=None,
    help="Emit a prompt suppression marker every N lines (0 disables)",
)
@click.option(
    "--ai-tool-mode",
    type=click.Choice(["summary", "full", "none"]),
    default=None,
    help="Tool output display mode",
)
@click.option(
    "--ai-sys-mode",
    type=click.Choice(["summary", "full"]),
    default=None,
    help="System output display mode",
)
def understand(
    max_iterations: int,
    root: Path | None,
    prompt: str | None,
    prd: str | None,
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
    ai_raw: bool | None,
    ai_show_prompt: bool | None,
    ai_show_final: bool | None,
    ai_prompt_progress_every: int | None,
    ai_tool_mode: str | None,
    ai_sys_mode: str | None,
) -> None:
    """Run codebase understanding loop (read-only mode).

    MAX_ITERATIONS is the maximum number of iterations (default: 10).

    This mode:
    - Uses understand_prompt.md instead of prompt.md
    - Only allows edits to codebase_map.md
    - Works on ralph/understanding branch by default
    """
    ctx = click.get_current_context()
    env_prompt = os.environ.get("PROMPT_FILE")
    env_prd = os.environ.get("PRD_FILE")

    prompt_for_root = (
        Path(prompt) if _use_cli_value(ctx, "prompt") and prompt is not None else None
    )
    if prompt_for_root is None and env_prompt is not None:
        prompt_for_root = Path(env_prompt)

    prd_for_root = Path(prd) if _use_cli_value(ctx, "prd") and prd is not None else None
    if prd_for_root is None and env_prd is not None:
        prd_for_root = Path(env_prd)

    root_value = root if _use_cli_value(ctx, "root") else None
    root_dir = _resolve_root(root_value, prompt_for_root, prd_for_root)
    ralph_dir = root_dir / "scripts" / "ralph"

    # Create codebase_map.md if missing
    codebase_map = ralph_dir / "codebase_map.md"
    if not codebase_map.exists():
        from ralph_py.init_cmd import DEFAULT_CODEBASE_MAP
        codebase_map.parent.mkdir(parents=True, exist_ok=True)
        codebase_map.write_text(DEFAULT_CODEBASE_MAP)

    config = RalphConfig.from_env(root_dir)

    # Apply CLI overrides when explicitly provided.
    if _use_cli_value(ctx, "max_iterations"):
        config.max_iterations = max_iterations
    if _use_cli_value(ctx, "prompt"):
        config.prompt_file = _resolve_path(
            root_dir, prompt, ralph_dir / "understand_prompt.md"
        )
    if _use_cli_value(ctx, "prd"):
        config.prd_file = _resolve_path(
            root_dir, prd, ralph_dir / "prd.json"
        )
    if _use_cli_value(ctx, "sleep"):
        config.sleep_seconds = sleep
    if _use_cli_value(ctx, "interactive"):
        config.interactive = interactive
    if _use_cli_value(ctx, "allowed_paths"):
        config.allowed_paths = _parse_paths(allowed_paths)
    if _use_cli_value(ctx, "branch"):
        config.ralph_branch = branch
        config.ralph_branch_explicit = True
    if _use_cli_value(ctx, "agent_cmd"):
        config.agent_cmd = agent_cmd
    if _use_cli_value(ctx, "model"):
        config.model = model
    if _use_cli_value(ctx, "reasoning"):
        config.model_reasoning_effort = reasoning
    if _use_cli_value(ctx, "ui"):
        config.ui_mode = _normalize_ui_mode(ui)
    if _use_cli_value(ctx, "no_color"):
        config.no_color = no_color
    if _use_cli_value(ctx, "ascii"):
        config.ascii_only = ascii
    if _use_cli_value(ctx, "ai_raw"):
        config.ai_raw = bool(ai_raw)
    if _use_cli_value(ctx, "ai_show_prompt"):
        config.ai_show_prompt = bool(ai_show_prompt)
    if _use_cli_value(ctx, "ai_show_final"):
        config.ai_show_final = bool(ai_show_final)
    if _use_cli_value(ctx, "ai_prompt_progress_every") and ai_prompt_progress_every is not None:
        config.ai_prompt_progress_every = ai_prompt_progress_every
    if _use_cli_value(ctx, "ai_tool_mode") and ai_tool_mode is not None:
        config.ai_tool_mode = ai_tool_mode
    if _use_cli_value(ctx, "ai_sys_mode") and ai_sys_mode is not None:
        config.ai_sys_mode = ai_sys_mode

    # Apply understanding defaults when not overridden by env or CLI.
    if not _use_cli_value(ctx, "prompt") and "PROMPT_FILE" not in os.environ:
        config.prompt_file = ralph_dir / "understand_prompt.md"
    if not _use_cli_value(ctx, "allowed_paths") and "ALLOWED_PATHS" not in os.environ:
        config.allowed_paths = ["scripts/ralph/codebase_map.md"]
    if not _use_cli_value(ctx, "branch") and "RALPH_BRANCH" not in os.environ:
        config.ralph_branch = "ralph/understanding"
        config.ralph_branch_explicit = False

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

    if config.ui_mode == "textual":
        from ralph_py.ui.textual_ui import run_textual_app

        def runner(ui: UI) -> int:
            if config.max_iterations < 0:
                ui.err(
                    f"MAX_ITERATIONS must be non-negative (got: {config.max_iterations})"
                )
                return 2
            if not config.agent_cmd and not CodexAgent.is_available():
                ui.err("codex not found in PATH")
                ui.info("Install codex or use --agent-cmd to specify a custom agent")
                return 1
            agent = get_agent(config.agent_cmd, config.model, config.model_reasoning_effort)
            result = run_loop(config, ui, agent, root_dir)
            return result.exit_code

        exit_code = run_textual_app(runner, mode_label="understand")
        sys.exit(exit_code)

    # Check codex availability
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(
        config.ui_mode,
        config.no_color,
        config.ascii_only,
        force_rich=force_rich,
    )

    if config.max_iterations < 0:
        ui_impl.err(
            f"MAX_ITERATIONS must be non-negative (got: {config.max_iterations})"
        )
        sys.exit(2)

    if not config.agent_cmd and not CodexAgent.is_available():
        ui_impl.err("codex not found in PATH")
        ui_impl.info("Install codex or use --agent-cmd to specify a custom agent")
        sys.exit(1)

    agent = get_agent(config.agent_cmd, config.model, config.model_reasoning_effort)

    result = run_loop(config, ui_impl, agent, root_dir)
    sys.exit(result.exit_code)


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
