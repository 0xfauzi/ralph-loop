"""Tests for loop module."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ralph_py.config import RalphConfig
from ralph_py.loop import COMPLETION_MARKER, run_loop
from ralph_py.ui.plain import PlainUI


class MockAgent:
    """Mock agent for testing."""

    def __init__(self, output: list[str]):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "mock"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        yield from self._output
        if self._output:
            self._final_message = self._output[-1]

    @property
    def final_message(self) -> str | None:
        return self._final_message


class TestRunLoop:
    """Tests for run_loop."""

    def test_completes_on_marker(self, tmp_path: Path) -> None:
        """Loop exits with code 0 when completion marker found."""
        # Setup
        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("test prompt")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        config = RalphConfig(
            max_iterations=5,
            prompt_file=ralph_dir / "prompt.md",
            prd_file=ralph_dir / "prd.json",
            sleep_seconds=0,
            ralph_branch="",
            ralph_branch_explicit=True,
        )
        ui = PlainUI(no_color=True)
        agent = MockAgent(["working...", COMPLETION_MARKER])

        # Execute
        result = run_loop(config, ui, agent, tmp_path)

        # Verify
        assert result.completed is True
        assert result.exit_code == 0
        assert result.iterations == 1

    def test_max_iterations_without_completion(self, tmp_path: Path) -> None:
        """Loop exits with code 1 when max iterations reached."""
        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("test prompt")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        config = RalphConfig(
            max_iterations=3,
            prompt_file=ralph_dir / "prompt.md",
            prd_file=ralph_dir / "prd.json",
            sleep_seconds=0,
            ralph_branch="",
            ralph_branch_explicit=True,
        )
        ui = PlainUI(no_color=True)
        agent = MockAgent(["still working"])

        result = run_loop(config, ui, agent, tmp_path)

        assert result.completed is False
        assert result.exit_code == 1
        assert result.iterations == 3

    def test_missing_prompt_file(self, tmp_path: Path) -> None:
        """Loop exits with code 1 when prompt file missing."""
        config = RalphConfig(
            max_iterations=5,
            prompt_file=tmp_path / "nonexistent.md",
            ralph_branch="",
            ralph_branch_explicit=True,
        )
        ui = PlainUI(no_color=True)
        agent = MockAgent([])

        result = run_loop(config, ui, agent, tmp_path)

        assert result.completed is False
        assert result.exit_code == 1
        assert result.iterations == 0

    def test_completion_marker_in_middle_of_output(self, tmp_path: Path) -> None:
        """Completion marker found even when not at end of output."""
        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("test")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        config = RalphConfig(
            max_iterations=5,
            prompt_file=ralph_dir / "prompt.md",
            prd_file=ralph_dir / "prd.json",
            sleep_seconds=0,
            ralph_branch="",
            ralph_branch_explicit=True,
        )
        ui = PlainUI(no_color=True)
        agent = MockAgent(["start", f"found {COMPLETION_MARKER} here", "more output"])

        result = run_loop(config, ui, agent, tmp_path)

        assert result.completed is True
        assert result.exit_code == 0
