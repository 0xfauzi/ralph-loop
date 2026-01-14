"""Configuration handling for Ralph."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _parse_bool(value: str | None) -> bool:
    """Parse boolean from environment variable."""
    if value is None:
        return False
    return bool(re.match(r"^(1|true|yes)$", value.lower()))


def _parse_paths(value: str | None) -> list[str]:
    """Parse comma-separated paths, trimming whitespace."""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _parse_mode(value: str | None, default: str, allowed: set[str]) -> str:
    """Parse a mode value with allowed options."""
    if value is None:
        return default
    lowered = value.lower().strip()
    if lowered in allowed:
        return lowered
    return default


@dataclass
class RalphConfig:
    """Configuration for Ralph agentic loop."""

    max_iterations: int = 10
    prompt_file: Path = field(default_factory=lambda: Path("scripts/ralph/prompt.md"))
    prd_file: Path = field(default_factory=lambda: Path("scripts/ralph/prd.json"))
    sleep_seconds: float = 2.0
    interactive: bool = False
    allowed_paths: list[str] = field(default_factory=list)

    # Branch config - None means use PRD, "" means skip
    ralph_branch: str | None = None
    ralph_branch_explicit: bool = False  # Was RALPH_BRANCH env var set?

    # Agent config
    agent_cmd: str | None = None
    model: str | None = None
    model_reasoning_effort: str | None = None

    # UI config
    ui_mode: str = "auto"  # auto|rich|plain
    no_color: bool = False
    ascii_only: bool = False

    # Codex-specific UI settings
    ai_raw: bool = False
    ai_show_final: bool = True
    ai_show_prompt: bool = False
    ai_prompt_progress_every: int = 50
    ai_tool_mode: str = "summary"  # summary|full|none
    ai_sys_mode: str = "summary"  # summary|full

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> RalphConfig:
        """Load configuration from environment variables."""
        if root_dir is None:
            root_dir = Path.cwd()

        # Check if RALPH_BRANCH is explicitly set (even if empty)
        ralph_branch_explicit = "RALPH_BRANCH" in os.environ
        ralph_branch: str | None = os.environ.get("RALPH_BRANCH")
        if not ralph_branch_explicit:
            ralph_branch = None

        return cls(
            max_iterations=int(os.environ.get("MAX_ITERATIONS", "10")),
            prompt_file=root_dir / os.environ.get("PROMPT_FILE", "scripts/ralph/prompt.md"),
            prd_file=root_dir / os.environ.get("PRD_FILE", "scripts/ralph/prd.json"),
            sleep_seconds=float(os.environ.get("SLEEP_SECONDS", "2")),
            interactive=_parse_bool(os.environ.get("INTERACTIVE")),
            allowed_paths=_parse_paths(os.environ.get("ALLOWED_PATHS")),
            ralph_branch=ralph_branch,
            ralph_branch_explicit=ralph_branch_explicit,
            agent_cmd=os.environ.get("AGENT_CMD"),
            model=os.environ.get("MODEL"),
            model_reasoning_effort=os.environ.get("MODEL_REASONING_EFFORT"),
            ui_mode=os.environ.get("RALPH_UI", "auto"),
            no_color="NO_COLOR" in os.environ,
            ascii_only=_parse_bool(os.environ.get("RALPH_ASCII")),
            ai_raw=_parse_bool(os.environ.get("RALPH_AI_RAW")),
            ai_show_final=os.environ.get("RALPH_AI_SHOW_FINAL", "1") != "0",
            ai_show_prompt=_parse_bool(os.environ.get("RALPH_AI_SHOW_PROMPT")),
            ai_prompt_progress_every=int(
                os.environ.get("RALPH_AI_PROMPT_PROGRESS_EVERY", "50")
            ),
            ai_tool_mode=_parse_mode(
                os.environ.get("RALPH_AI_TOOL_MODE"),
                "summary",
                {"summary", "full", "none"},
            ),
            ai_sys_mode=_parse_mode(
                os.environ.get("RALPH_AI_SYS_MODE"),
                "summary",
                {"summary", "full"},
            ),
        )

    def validate(self) -> list[str]:
        """Validate configuration, returning list of errors."""
        errors: list[str] = []

        if self.max_iterations < 0:
            errors.append(f"MAX_ITERATIONS must be non-negative (got: {self.max_iterations})")

        if not self.prompt_file.exists():
            errors.append(f"Prompt file not found: {self.prompt_file}")

        return errors
