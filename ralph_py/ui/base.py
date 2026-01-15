"""Base UI protocol for Ralph output."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from typing import TextIO


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

    def box(self, content: str) -> None:
        """Display content in a box."""
        ...

    def panel(self, tag: str, title: str, content: str) -> None:
        """Display a titled panel block."""
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

    def channel_footer(self, channel: str, title: str = "") -> None:
        """Display channel footer."""
        ...

    def stream_line(self, tag: str, line: str) -> None:
        """Display a single prefixed line."""
        ...

    def stream_lines(self, tag: str, stream: TextIO) -> Iterator[str]:
        """Stream lines with prefix, yielding raw lines."""
        ...

    def choose(self, header: str, options: list[str], default: int = 0) -> int:
        """Interactive choice, returns selected index."""
        ...

    def confirm(self, prompt: str, default: bool = False) -> bool:
        """Interactive yes/no confirmation."""
        ...

    def can_prompt(self) -> bool:
        """Check if interactive prompts are available."""
        ...
