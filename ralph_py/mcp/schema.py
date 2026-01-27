"""Tool input schemas and validation helpers for the MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

UiMode = Literal["auto", "rich", "plain"]
AiToolMode = Literal["summary", "full", "none"]
AiSysMode = Literal["summary", "full"]


class SharedInputs(BaseModel):
    """Inputs shared across tool calls."""

    model_config = ConfigDict(extra="forbid")

    root: str = Field(..., description="Absolute path to the project root.")
    prompt_file: str | None = Field(default=None, description="Prompt file path.")
    prd_file: str | None = Field(default=None, description="PRD file path.")
    max_iterations: int | None = Field(default=None, description="Max iterations.")
    sleep_seconds: float | None = Field(default=None, description="Sleep seconds.")
    interactive: bool | None = Field(
        default=None, description="Enable interactive prompts."
    )
    allowed_paths: list[str] | None = Field(
        default=None, description="Repo-relative paths allowed to change."
    )
    branch: str | None = Field(default=None, description="Branch name override.")
    agent_cmd: str | None = Field(default=None, description="Custom agent command.")
    model: str | None = Field(default=None, description="Model identifier.")
    model_reasoning_effort: str | None = Field(
        default=None, description="Model reasoning effort."
    )
    ui_mode: UiMode | None = Field(default=None, description="UI mode.")
    no_color: bool | None = Field(default=None, description="Disable color output.")
    ascii_only: bool | None = Field(default=None, description="Force ASCII-only output.")
    ai_raw: bool | None = Field(default=None, description="Stream raw AI output.")
    ai_show_prompt: bool | None = Field(default=None, description="Show prompt echo.")
    ai_show_final: bool | None = Field(default=None, description="Show final message.")
    ai_prompt_progress_every: int | None = Field(
        default=None, description="Prompt progress marker interval."
    )
    ai_tool_mode: AiToolMode | None = Field(default=None, description="Tool output mode.")
    ai_sys_mode: AiSysMode | None = Field(default=None, description="System output mode.")


class RunInputs(SharedInputs):
    """Inputs for ralph.run."""

    allow_edits: bool = Field(default=False, description="Allow edits during run.")


class UnderstandInputs(SharedInputs):
    """Inputs for ralph.understand."""

    allow_edits: Literal[False] = Field(
        default=False, description="Understand mode is always read-only."
    )


class InitInputs(BaseModel):
    """Inputs for ralph.init."""

    model_config = ConfigDict(extra="forbid")

    root: str = Field(..., description="Absolute path to the project root.")
    force: bool = Field(default=False, description="Allow overwriting scaffolding.")


class ValidateInputs(BaseModel):
    """Inputs for ralph.validate."""

    model_config = ConfigDict(extra="forbid")

    root: str = Field(..., description="Absolute path to the project root.")
    prompt_file: str | None = Field(default=None, description="Prompt file path.")
    prd_file: str | None = Field(default=None, description="PRD file path.")


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation issue."""

    field: str
    message: str

    def to_detail(self) -> dict[str, str]:
        """Return a JSON-serializable detail object."""
        return {"field": self.field, "message": self.message}


class InvalidArgumentError(ValueError):
    """Structured validation error for MCP tool inputs."""

    def __init__(self, message: str, issues: Sequence[ValidationIssue]) -> None:
        super().__init__(message)
        self.message = message
        self.issues = list(issues)

    @classmethod
    def from_pydantic(cls, exc: ValidationError) -> InvalidArgumentError:
        issues = [_issue_from_pydantic(error) for error in exc.errors()]
        return cls("Invalid tool input", issues)

    def to_payload(self) -> dict[str, Any]:
        """Return MCP-friendly invalid_argument payload."""
        return {
            "code": "invalid_argument",
            "message": self.message,
            "details": [issue.to_detail() for issue in self.issues],
        }


ModelT = TypeVar("ModelT", bound=BaseModel)


def parse_input(model: type[ModelT], data: Mapping[str, Any]) -> ModelT:
    """Parse tool input data into a model or raise InvalidArgumentError."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise InvalidArgumentError.from_pydantic(exc) from exc


def validate_shared_inputs(inputs: SharedInputs) -> None:
    """Validate shared inputs and raise InvalidArgumentError on failure."""
    issues: list[ValidationIssue] = []

    root_path = _validate_root(inputs.root, issues)
    _validate_non_negative("max_iterations", inputs.max_iterations, issues)
    _validate_non_negative("sleep_seconds", inputs.sleep_seconds, issues)
    _validate_non_negative(
        "ai_prompt_progress_every", inputs.ai_prompt_progress_every, issues
    )
    _validate_allowed_paths(inputs.allowed_paths, issues)

    if root_path is not None:
        _validate_path_within_root("prompt_file", inputs.prompt_file, root_path, issues)
        _validate_path_within_root("prd_file", inputs.prd_file, root_path, issues)

    _raise_if_issues(issues)


def validate_run_inputs(inputs: RunInputs) -> None:
    """Validate inputs for ralph.run."""
    validate_shared_inputs(inputs)


def validate_understand_inputs(inputs: UnderstandInputs) -> None:
    """Validate inputs for ralph.understand."""
    issues: list[ValidationIssue] = []
    if inputs.allow_edits is not False:
        issues.append(
            ValidationIssue("allow_edits", "allow_edits must be false for understand.")
        )
    _raise_if_issues(issues)
    validate_shared_inputs(inputs)


def validate_init_inputs(inputs: InitInputs) -> None:
    """Validate inputs for ralph.init."""
    issues: list[ValidationIssue] = []
    _validate_root(inputs.root, issues)
    _raise_if_issues(issues)


def validate_validate_inputs(inputs: ValidateInputs) -> None:
    """Validate inputs for ralph.validate."""
    issues: list[ValidationIssue] = []
    root_path = _validate_root(inputs.root, issues)
    if root_path is not None:
        _validate_path_within_root("prompt_file", inputs.prompt_file, root_path, issues)
        _validate_path_within_root("prd_file", inputs.prd_file, root_path, issues)
    _raise_if_issues(issues)


def _raise_if_issues(issues: Sequence[ValidationIssue]) -> None:
    if issues:
        raise InvalidArgumentError("Invalid tool input", issues)


def _validate_root(raw_root: str, issues: list[ValidationIssue]) -> Path | None:
    root_path = Path(raw_root).expanduser()
    if not root_path.is_absolute():
        issues.append(ValidationIssue("root", "root must be an absolute path."))
        return None
    resolved = root_path.resolve()
    if not resolved.exists():
        issues.append(ValidationIssue("root", "root must exist."))
        return None
    if not resolved.is_dir():
        issues.append(ValidationIssue("root", "root must be a directory."))
        return None
    return resolved


def _validate_path_within_root(
    field: str,
    raw_path: str | None,
    root_path: Path,
    issues: list[ValidationIssue],
) -> None:
    if raw_path is None:
        return
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root_path / candidate
    resolved = candidate.resolve()
    if not _is_within_root(resolved, root_path):
        issues.append(
            ValidationIssue(field, f"{field} must resolve within root.")
        )


def _validate_allowed_paths(
    allowed_paths: Sequence[str] | None, issues: list[ValidationIssue]
) -> None:
    if not allowed_paths:
        return
    for entry in allowed_paths:
        if Path(entry).expanduser().is_absolute():
            issues.append(
                ValidationIssue(
                    "allowed_paths", f"allowed_paths must be relative: {entry}"
                )
            )


def _validate_non_negative(
    field: str, value: int | float | None, issues: list[ValidationIssue]
) -> None:
    if value is None:
        return
    if value < 0:
        issues.append(ValidationIssue(field, f"{field} must be non-negative."))


def _is_within_root(path: Path, root_path: Path) -> bool:
    try:
        path.relative_to(root_path)
    except ValueError:
        return False
    return True


def _issue_from_pydantic(error: Mapping[str, Any]) -> ValidationIssue:
    loc = error.get("loc", ())
    field = _format_loc(loc)
    message = str(error.get("msg", "Invalid value"))
    return ValidationIssue(field, message)


def _format_loc(loc: object) -> str:
    if isinstance(loc, (tuple, list)):
        if not loc:
            return "input"
        return ".".join(str(part) for part in loc)
    if loc:
        return str(loc)
    return "input"
