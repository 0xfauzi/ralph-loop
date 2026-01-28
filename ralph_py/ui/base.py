"""Base UI protocol for Ralph output."""

from __future__ import annotations

from typing import Protocol


class UI(Protocol):
    """Protocol for Ralph UI implementations."""

    def title(self, text: str) -> None:
        """Display a large title."""
        ...

    def section(self, text: str) -> None:
        """Display a section header."""
        ...

    def subsection(self, text: str) -> None:
        """Display a subsection header."""
        ...

    def hr(self) -> None:
        """Display a horizontal rule."""
        ...

    def kv(self, key: str, value: str) -> None:
        """Display a key-value pair."""
        ...

    def startup_art(self) -> None:
        """Display startup animation if supported."""
        ...

    def info(self, text: str) -> None:
        """Display info message (dim)."""
        ...

    def ok(self, text: str) -> None:
        """Display success message (green)."""
        ...

    def warn(self, text: str) -> None:
        """Display warning message (yellow)."""
        ...

    def err(self, text: str) -> None:
        """Display error message (red)."""
        ...

    def channel_header(self, channel: str, title: str = "") -> None:
        """Display channel header with optional title."""
        ...

    def stream_line(self, tag: str, line: str) -> None:
        """Display a single prefixed line."""
        ...

    def choose(self, header: str, options: list[str], default: int = 0) -> int:
        """Interactive choice, returns selected index."""
        ...

    def can_prompt(self) -> bool:
        """Check if interactive prompts are available."""
        ...
