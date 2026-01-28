"""CLI entry point for Ralph."""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import click
from click.core import ParameterSource

from ralph_py import __version__
from ralph_py.agents import CodexAgent, get_agent
from ralph_py.config import RalphConfig, _parse_paths
from ralph_py.init_cmd import DEFAULT_CODEBASE_MAP, DEFAULT_FEATURE_UNDERSTAND, run_init
from ralph_py.loop import run_loop
from ralph_py.prd import PRD
from ralph_py.ui import get_ui


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
    if normalized not in {"auto", "rich", "plain"}:
        return "auto"
    return normalized


def _derive_feature_name(prd_path: Path, root: Path) -> str:
    try:
        rel = prd_path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = None

    if rel is not None and len(rel.parts) >= 4:
        if rel.parts[0] == "scripts" and rel.parts[1] == "ralph" and rel.parts[2] == "feature":
            return rel.parts[3]

    return prd_path.stem


class LoggingAgent:
    """Agent wrapper that appends streamed output to a log file."""

    def __init__(self, agent: object, log_path: Path) -> None:
        self._agent = agent
        self._log_path = log_path

    @property
    def name(self) -> str:
        return self._agent.name

    def run(self, prompt: str, cwd: Path | None = None):
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a") as handle:
            for line in self._agent.run(prompt, cwd):
                handle.write(f"{line}\n")
                handle.flush()
                yield line

    @property
    def final_message(self) -> str | None:
        return self._agent.final_message


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


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
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

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
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)
    exit_code = run_init(directory, ui_impl)
    sys.exit(exit_code)


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
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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

    # Apply understanding defaults when not overridden by env or CLI.
    if not _use_cli_value(ctx, "prompt") and "PROMPT_FILE" not in os.environ:
        config.prompt_file = ralph_dir / "understand_prompt.md"
    if not _use_cli_value(ctx, "allowed_paths") and "ALLOWED_PATHS" not in os.environ:
        config.allowed_paths = ["scripts/ralph/codebase_map.md"]
    if not _use_cli_value(ctx, "branch") and "RALPH_BRANCH" not in os.environ:
        config.ralph_branch = "ralph/understanding"
        config.ralph_branch_explicit = False

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

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


@cli.command()
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prd",
    type=str,
    help="Feature PRD file path",
)
@click.option(
    "--understand-iterations",
    type=int,
    help="Iterations for the feature understanding phase",
)
@click.option(
    "--understand-prompt", "-p",
    type=str,
    help="Prompt file path for feature understanding",
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
    "--implementation-allowed-paths",
    help="Comma-separated allowed paths for implementation/repairs",
)
@click.option(
    "--implementation-auto-run",
    is_flag=True,
    help="Skip review gate and start implementation automatically",
)
@click.option(
    "--repair-max-runs",
    type=int,
    default=5,
    help="Maximum auto repair runs after a failed implementation",
)
@click.option(
    "--repair-iterations",
    type=int,
    default=5,
    help="Iterations per repair run",
)
@click.option(
    "--repair-agent-cmd",
    help="Custom agent command for repair runs (prompt piped to stdin)",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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
def feature(
    root: Path | None,
    prd: str | None,
    understand_iterations: int | None,
    understand_prompt: str | None,
    agent_cmd: str | None,
    repair_agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    sleep: float,
    interactive: bool,
    branch: str | None,
    implementation_allowed_paths: str | None,
    implementation_auto_run: bool,
    repair_max_runs: int,
    repair_iterations: int,
    ui: str,
    no_color: bool,
    ascii: bool,
) -> None:
    """Run feature understanding, then implementation.

    This mode:
    - Uses feature_understand_prompt.md for understanding by default
    - Only allows edits to the feature understand file during understanding
    - Uses the PRD branch by default
    - Starts implementation after review
    """
    ctx = click.get_current_context()
    env_prompt = os.environ.get("PROMPT_FILE")
    env_prd = os.environ.get("PRD_FILE")

    prompt_for_root = (
        Path(understand_prompt)
        if _use_cli_value(ctx, "understand_prompt") and understand_prompt is not None
        else None
    )
    if prompt_for_root is None and env_prompt is not None:
        prompt_for_root = Path(env_prompt)

    prd_for_root = Path(prd) if _use_cli_value(ctx, "prd") and prd is not None else None
    if prd_for_root is None and env_prd is not None:
        prd_for_root = Path(env_prd)

    root_value = root if _use_cli_value(ctx, "root") else None
    root_dir = _resolve_root(root_value, prompt_for_root, prd_for_root)
    ralph_dir = root_dir / "scripts" / "ralph"

    base_config = RalphConfig.from_env(root_dir)

    # Apply CLI overrides that should affect both phases.
    if _use_cli_value(ctx, "sleep"):
        base_config.sleep_seconds = sleep
    if _use_cli_value(ctx, "interactive"):
        base_config.interactive = interactive
    if _use_cli_value(ctx, "agent_cmd"):
        base_config.agent_cmd = agent_cmd
    if _use_cli_value(ctx, "model"):
        base_config.model = model
    if _use_cli_value(ctx, "reasoning"):
        base_config.model_reasoning_effort = reasoning
    if _use_cli_value(ctx, "ui"):
        base_config.ui_mode = _normalize_ui_mode(ui)
    if _use_cli_value(ctx, "no_color"):
        base_config.no_color = no_color
    if _use_cli_value(ctx, "ascii"):
        base_config.ascii_only = ascii

    base_config.ui_mode = _normalize_ui_mode(base_config.ui_mode)

    # Check codex availability
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(
        base_config.ui_mode,
        base_config.no_color,
        base_config.ascii_only,
        force_rich=force_rich,
    )

    codebase_map = ralph_dir / "codebase_map.md"
    if not codebase_map.exists():
        ui_impl.err(f"codebase_map.md not found: {codebase_map}")
        ui_impl.info("Run `ralph init` or `ralph understand` first.")
        sys.exit(1)

    if _use_cli_value(ctx, "understand_iterations"):
        if understand_iterations is None or understand_iterations < 0:
            ui_impl.err(
                "UNDERSTAND_ITERATIONS must be non-negative "
                f"(got: {understand_iterations})"
            )
            sys.exit(2)
        understand_iterations_value = understand_iterations
    else:
        if base_config.max_iterations < 0:
            ui_impl.err(
                "UNDERSTAND_ITERATIONS must be non-negative "
                f"(got: {base_config.max_iterations})"
            )
            sys.exit(2)
        understand_iterations_value = base_config.max_iterations

    if repair_max_runs < 0:
        ui_impl.err(
            f"REPAIR_MAX_RUNS must be non-negative (got: {repair_max_runs})"
        )
        sys.exit(2)

    if repair_iterations < 0:
        ui_impl.err(
            f"REPAIR_ITERATIONS must be non-negative (got: {repair_iterations})"
        )
        sys.exit(2)

    if _use_cli_value(ctx, "prd"):
        prd_path = _resolve_path(root_dir, prd, ralph_dir / "prd.json")
    elif env_prd is not None:
        prd_path = _resolve_path(root_dir, env_prd, ralph_dir / "prd.json")
    else:
        prd_path = None
    if prd_path is None:
        ui_impl.err("Feature PRD is required. Use --prd or PRD_FILE.")
        sys.exit(2)

    if not prd_path.exists():
        ui_impl.err(f"Feature PRD not found: {prd_path}")
        sys.exit(1)

    try:
        prd_doc = PRD.load(prd_path)
    except Exception as exc:
        ui_impl.err(f"Invalid PRD: {exc}")
        sys.exit(1)

    feature_name = _derive_feature_name(prd_path, root_dir)
    if not feature_name:
        ui_impl.err("Unable to determine feature name from PRD path.")
        sys.exit(2)

    feature_dir = ralph_dir / "feature" / feature_name
    feature_dir.mkdir(parents=True, exist_ok=True)
    feature_understand = feature_dir / "understand.md"
    if not feature_understand.exists():
        feature_understand.write_text(DEFAULT_FEATURE_UNDERSTAND)

    log_dir = root_dir / ".ralph" / "logs" / f"feature_{feature_name}"

    def log_path(label: str, attempt: int | None = None) -> Path:
        stamp = _timestamp()
        if attempt is None:
            name = f"{label}_{stamp}.log"
        else:
            name = f"{label}_{attempt:02d}_{stamp}.log"
        return log_dir / name

    def build_repair_prd(log_file: Path, attempt: int) -> Path:
        repair_dir = feature_dir / "repairs"
        repair_dir.mkdir(parents=True, exist_ok=True)
        repair_path = repair_dir / f"repair_{_timestamp()}.json"
        latest_path = repair_dir / "latest.json"

        verification: list[str] = []
        seen: set[str] = set()
        for story in prd_doc.user_stories:
            for item in story.acceptance_criteria:
                lower = item.lower()
                if ("typecheck" in lower or "tests" in lower or "lint" in lower) and "pass" in lower:
                    if item not in seen:
                        seen.add(item)
                        verification.append(item)

        try:
            rel_log = log_file.relative_to(root_dir)
            log_ref = rel_log.as_posix()
        except ValueError:
            log_ref = str(log_file)

        criteria = [f"Repair failures reported in {log_ref}"]
        criteria.extend(verification)

        repair_story = {
            "id": f"REPAIR-{attempt:02d}",
            "title": "Repair failures from last run",
            "acceptanceCriteria": criteria,
            "priority": 1,
            "passes": False,
            "notes": f"Original PRD: {prd_path}",
        }
        repair_doc = {
            "branchName": prd_doc.branch_name,
            "userStories": [repair_story],
        }
        with open(repair_path, "w") as handle:
            json.dump(repair_doc, handle, indent=2)
            handle.write("\n")
        with open(latest_path, "w") as handle:
            json.dump(repair_doc, handle, indent=2)
            handle.write("\n")

        return repair_path

    if not base_config.agent_cmd and not CodexAgent.is_available():
        ui_impl.err("codex not found in PATH")
        ui_impl.info("Install codex or use --agent-cmd to specify a custom agent")
        sys.exit(1)

    agent = get_agent(
        base_config.agent_cmd,
        base_config.model,
        base_config.model_reasoning_effort,
    )

    # Feature understanding phase
    understand_config = copy.deepcopy(base_config)
    understand_config.max_iterations = understand_iterations_value
    if _use_cli_value(ctx, "understand_prompt"):
        understand_config.prompt_file = _resolve_path(
            root_dir, understand_prompt, ralph_dir / "feature_understand_prompt.md"
        )
    elif "PROMPT_FILE" not in os.environ:
        understand_config.prompt_file = ralph_dir / "feature_understand_prompt.md"
    understand_config.prd_file = prd_path
    rel_feature_understand = feature_understand.relative_to(root_dir).as_posix()
    understand_config.allowed_paths = [rel_feature_understand]
    if _use_cli_value(ctx, "branch"):
        understand_config.ralph_branch = branch
        understand_config.ralph_branch_explicit = True

    understand_log = log_path("understand")
    understand_agent = LoggingAgent(agent, understand_log)
    understand_result = run_loop(understand_config, ui_impl, understand_agent, root_dir)
    if understand_result.exit_code != 0:
        sys.exit(understand_result.exit_code)

    # Review gate
    ui_impl.section("Feature understand review")
    ui_impl.kv("Understand file", str(feature_understand))
    if implementation_auto_run:
        ui_impl.info("IMPLEMENTATION_AUTO_RUN enabled: skipping review gate")
    else:
        if not ui_impl.can_prompt():
            ui_impl.err(
                "Interactive review required. Re-run with --implementation-auto-run."
            )
            sys.exit(2)

        choice = ui_impl.choose(
            "Review the understand file and confirm implementation start:",
            ["Start implementation", "Quit to amend"],
            default=0,
        )
        if choice != 0:
            ui_impl.info("Amend the understand file and re-run `ralph feature`.")
            sys.exit(0)

    # Implementation phase
    run_config = copy.deepcopy(base_config)
    run_config.prd_file = prd_path
    run_config.max_iterations = len(prd_doc.user_stories)
    if run_config.max_iterations == 0:
        ui_impl.warn("PRD has no user stories. Skipping implementation.")
        sys.exit(0)
    run_config.prompt_file = root_dir / "scripts/ralph/prompt.md"
    if _use_cli_value(ctx, "implementation_allowed_paths"):
        run_config.allowed_paths = _parse_paths(implementation_allowed_paths)
    if _use_cli_value(ctx, "branch"):
        run_config.ralph_branch = branch
        run_config.ralph_branch_explicit = True

    run_log = log_path("run")
    run_agent = LoggingAgent(agent, run_log)
    result = run_loop(run_config, ui_impl, run_agent, root_dir)
    if result.exit_code == 0 or repair_max_runs == 0 or result.iterations == 0:
        sys.exit(result.exit_code)

    last_log = run_log
    repair_result = result
    for attempt in range(1, repair_max_runs + 1):
        repair_prd = build_repair_prd(last_log, attempt)
        repair_config = copy.deepcopy(base_config)
        repair_config.prd_file = repair_prd
        repair_config.prompt_file = root_dir / "scripts/ralph/prompt.md"
        repair_config.max_iterations = repair_iterations
        if _use_cli_value(ctx, "implementation_allowed_paths"):
            repair_config.allowed_paths = _parse_paths(implementation_allowed_paths)
        repair_config.ralph_branch = ""
        repair_config.ralph_branch_explicit = True

        repair_log = log_path("repair", attempt)
        repair_agent_base = get_agent(
            repair_agent_cmd or base_config.agent_cmd,
            base_config.model,
            base_config.model_reasoning_effort,
        )
        repair_agent = LoggingAgent(repair_agent_base, repair_log)
        repair_result = run_loop(repair_config, ui_impl, repair_agent, root_dir)
        if repair_result.exit_code == 0:
            sys.exit(0)
        last_log = repair_log

    sys.exit(repair_result.exit_code)


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
