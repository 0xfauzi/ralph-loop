"""Base agent protocol for Ralph."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Protocol


class Agent(Protocol):
    """Protocol for Ralph agent implementations."""

    @property
    def name(self) -> str:
        """Human-readable agent name for display."""
        ...

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        """Run agent with prompt, yielding output lines.

        Args:
            prompt: The prompt text to send to the agent
            cwd: Working directory for the agent process

        Yields:
            Output lines from the agent (without trailing newlines)
        """
        ...

    @property
    def final_message(self) -> str | None:
        """Return final message if available (for codex --output-last-message)."""
        ...
