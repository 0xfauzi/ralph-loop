"""Runner adapter for Ralph MCP tools."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ralph_py import git
from ralph_py.agents import CodexAgent, get_agent
from ralph_py.config import RalphConfig
from ralph_py.loop import LoopResult, run_loop
from ralph_py.mcp.schema import (
    InvalidArgumentError,
    RunInputs,
    SharedInputs,
    UnderstandInputs,
    ValidationIssue,
    validate_run_inputs,
    validate_understand_inputs,
)
from ralph_py.ui import get_ui


@dataclass(frozen=True)
class RunnerResult:
    """Output from running a Ralph MCP tool."""

    loop_result: LoopResult
    changed_files: list[str]


class ExecutionFailedError(RuntimeError):
    """Structured runtime error for MCP tool failures."""

    def __init__(self, message: str, details: Sequence[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = list(details or [])

    def to_payload(self) -> dict[str, Any]:
        """Return MCP-friendly execution_failed payload."""
        return {
            "code": "execution_failed",
            "message": self.message,
            "details": self.details,
        }


def run(inputs: RunInputs) -> RunnerResult:
    """Run the Ralph loop with MCP inputs."""
    validate_run_inputs(inputs)
    root = _resolve_root(inputs.root)
    config = _build_config(
        root,
        inputs,
        default_prompt=root / "scripts" / "ralph" / "prompt.md",
        default_prd=root / "scripts" / "ralph" / "prd.json",
    )
    return _run_loop(root, config, allow_edits=inputs.allow_edits)


def understand(inputs: UnderstandInputs) -> RunnerResult:
    """Run the Ralph understanding loop with MCP inputs."""
    validate_understand_inputs(inputs)
    root = _resolve_root(inputs.root)
    config = _build_config(
        root,
        inputs,
        default_prompt=root / "scripts" / "ralph" / "understand_prompt.md",
        default_prd=root / "scripts" / "ralph" / "prd.json",
    )
    _apply_understand_defaults(config, inputs, root)
    return _run_loop(root, config, allow_edits=False)


def _run_loop(root: Path, config: RalphConfig, *, allow_edits: bool) -> RunnerResult:
    config.ui_mode = _normalize_ui_mode(config.ui_mode)
    ui = get_ui(config.ui_mode, config.no_color, config.ascii_only)

    if not config.agent_cmd and not CodexAgent.is_available():
        raise ExecutionFailedError("codex not found in PATH")

    agent = get_agent(config.agent_cmd, config.model, config.model_reasoning_effort)

    iteration_callback = None
    if not allow_edits:
        iteration_callback = _build_read_only_guard(root)

    loop_result = run_loop(
        config,
        ui,
        agent,
        root,
        iteration_callback=iteration_callback,
    )

    changed_files = _get_changed_files(root) if allow_edits else []
    return RunnerResult(loop_result=loop_result, changed_files=changed_files)


def _build_config(
    root: Path,
    inputs: SharedInputs,
    *,
    default_prompt: Path,
    default_prd: Path,
) -> RalphConfig:
    config = RalphConfig.from_env(root)

    if _field_is_set(inputs, "prompt_file"):
        config.prompt_file = _resolve_input_path(
            root, inputs.prompt_file, default_prompt, "prompt_file"
        )
    if _field_is_set(inputs, "prd_file"):
        config.prd_file = _resolve_input_path(
            root, inputs.prd_file, default_prd, "prd_file"
        )
    if _field_is_set(inputs, "max_iterations") and inputs.max_iterations is not None:
        config.max_iterations = inputs.max_iterations
    if _field_is_set(inputs, "sleep_seconds") and inputs.sleep_seconds is not None:
        config.sleep_seconds = inputs.sleep_seconds
    if _field_is_set(inputs, "interactive") and inputs.interactive is not None:
        config.interactive = inputs.interactive
    if _field_is_set(inputs, "allowed_paths"):
        config.allowed_paths = list(inputs.allowed_paths or [])
    if _field_is_set(inputs, "branch") and inputs.branch is not None:
        config.ralph_branch = inputs.branch
        config.ralph_branch_explicit = True
    if _field_is_set(inputs, "agent_cmd"):
        config.agent_cmd = inputs.agent_cmd
    if _field_is_set(inputs, "model"):
        config.model = inputs.model
    if _field_is_set(inputs, "model_reasoning_effort"):
        config.model_reasoning_effort = inputs.model_reasoning_effort
    if _field_is_set(inputs, "ui_mode") and inputs.ui_mode is not None:
        config.ui_mode = inputs.ui_mode
    if _field_is_set(inputs, "no_color") and inputs.no_color is not None:
        config.no_color = inputs.no_color
    if _field_is_set(inputs, "ascii_only") and inputs.ascii_only is not None:
        config.ascii_only = inputs.ascii_only
    if _field_is_set(inputs, "ai_raw") and inputs.ai_raw is not None:
        config.ai_raw = inputs.ai_raw
    if _field_is_set(inputs, "ai_show_prompt") and inputs.ai_show_prompt is not None:
        config.ai_show_prompt = inputs.ai_show_prompt
    if _field_is_set(inputs, "ai_show_final") and inputs.ai_show_final is not None:
        config.ai_show_final = inputs.ai_show_final
    if (
        _field_is_set(inputs, "ai_prompt_progress_every")
        and inputs.ai_prompt_progress_every is not None
    ):
        config.ai_prompt_progress_every = inputs.ai_prompt_progress_every
    if _field_is_set(inputs, "ai_tool_mode") and inputs.ai_tool_mode is not None:
        config.ai_tool_mode = inputs.ai_tool_mode
    if _field_is_set(inputs, "ai_sys_mode") and inputs.ai_sys_mode is not None:
        config.ai_sys_mode = inputs.ai_sys_mode

    return config


def _apply_understand_defaults(
    config: RalphConfig, inputs: SharedInputs, root: Path
) -> None:
    if not _field_is_set(inputs, "prompt_file") and "PROMPT_FILE" not in os.environ:
        config.prompt_file = root / "scripts" / "ralph" / "understand_prompt.md"
    if not _field_is_set(inputs, "allowed_paths") and "ALLOWED_PATHS" not in os.environ:
        config.allowed_paths = ["scripts/ralph/codebase_map.md"]
    if not _field_is_set(inputs, "branch") and "RALPH_BRANCH" not in os.environ:
        config.ralph_branch = "ralph/understanding"
        config.ralph_branch_explicit = False


def _build_read_only_guard(root: Path) -> Callable[[int, Path], None]:
    def _guard(iteration: int, cwd: Path) -> None:
        changed_files = _get_changed_files(cwd)
        if changed_files:
            details = [{"file": file} for file in changed_files]
            raise ExecutionFailedError(
                f"File changes detected after iteration {iteration} while allow_edits is false.",
                details=details,
            )

    return _guard


def _get_changed_files(root: Path) -> list[str]:
    return sorted(git.get_changed_files(root))


def _resolve_root(raw_root: str) -> Path:
    root_path = Path(raw_root).expanduser()
    if not root_path.is_absolute():
        raise InvalidArgumentError(
            "Invalid tool input",
            [ValidationIssue("root", "root must be an absolute path.")],
        )
    resolved = root_path.resolve()
    if not resolved.exists():
        raise InvalidArgumentError(
            "Invalid tool input",
            [ValidationIssue("root", "root must exist.")],
        )
    if not resolved.is_dir():
        raise InvalidArgumentError(
            "Invalid tool input",
            [ValidationIssue("root", "root must be a directory.")],
        )
    return resolved


def _resolve_input_path(
    root: Path,
    raw_value: str | None,
    default: Path,
    field: str,
) -> Path:
    if raw_value is None or raw_value == "":
        candidate = default
    else:
        candidate = Path(raw_value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
    resolved = candidate.resolve()
    if not _is_within_root(resolved, root):
        raise InvalidArgumentError(
            "Invalid tool input",
            [ValidationIssue(field, f"{field} must resolve within root.")],
        )
    return resolved


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _normalize_ui_mode(value: str) -> str:
    normalized = (value or "auto").strip().lower()
    if normalized == "gum":
        return "rich"
    if normalized == "textual":
        return "plain"
    if normalized in {"plain", "off", "no", "0"}:
        return "plain"
    if normalized not in {"auto", "rich", "plain"}:
        return "auto"
    return normalized


def _field_is_set(inputs: BaseModel, name: str) -> bool:
    return name in inputs.model_fields_set
