"""Pytest fixtures for ralph_py tests."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture
def temp_project(tmp_path: Path) -> Generator[Path, None, None]:
    """Create a temporary project directory with Ralph structure."""
    ralph_dir = tmp_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)

    # Create minimal prompt.md
    (ralph_dir / "prompt.md").write_text("Test prompt\n")

    # Create minimal prd.json
    (ralph_dir / "prd.json").write_text(
        '{"branchName": "test-branch", "userStories": []}\n'
    )

    # Save current directory
    original_dir = os.getcwd()
    os.chdir(tmp_path)

    yield tmp_path

    # Restore directory
    os.chdir(original_dir)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear Ralph-related environment variables."""
    env_vars = [
        "MAX_ITERATIONS",
        "AGENT_CMD",
        "MODEL",
        "MODEL_REASONING_EFFORT",
        "SLEEP_SECONDS",
        "INTERACTIVE",
        "PROMPT_FILE",
        "ALLOWED_PATHS",
        "RALPH_BRANCH",
        "PRD_FILE",
        "RALPH_UI",
        "GUM_FORCE",
        "NO_COLOR",
        "RALPH_ASCII",
    ]
    for var in env_vars:
        monkeypatch.delenv(var, raising=False)
