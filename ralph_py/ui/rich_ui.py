"""Rich-based terminal UI implementation."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Iterator

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

if TYPE_CHECKING:
    from io import TextIO

# Channel colors matching shell script
CHANNEL_COLORS = {
    "AI": "cyan",
    "USER": "green",
    "PROMPT": "blue",
    "THINK": "magenta",
    "SYS": "yellow",
    "TOOL": "bright_black",
    "GIT": "bright_blue",
    "GUARD": "red",
}


class RichUI:
    """Rich-based terminal UI."""

    def __init__(
        self,
        no_color: bool = False,
        ascii_only: bool = False,
        file: TextIO | None = None,
    ):
        self.no_color = no_color
        self.ascii_only = ascii_only
        self._file = file or sys.stderr
        self.console = Console(
            file=self._file,
            no_color=no_color,
            force_terminal=True,
        )
        self._hr_char = "-" if ascii_only else "\u2500"

    def title(self, text: str) -> None:
        """Display a large title."""
        self.hr()
        self.console.print(text, justify="center", style="bold")
        self.hr()
        self.console.print()

    def section(self, text: str) -> None:
        """Display a section header."""
        self.console.print()
        self.console.print(f"== {text} ==", style="bold")

    def subsection(self, text: str) -> None:
        """Display a subsection header."""
        self.console.print(f"-- {text} --", style="dim")

    def hr(self) -> None:
        """Display a horizontal rule."""
        width = self.console.size.width
        self.console.print(self._hr_char * width, style="dim")

    def kv(self, key: str, value: str) -> None:
        """Display a key-value pair."""
        # Fixed-width key column (14 chars like shell script)
        padded_key = f"  {key}:".ljust(16)
        self.console.print(f"{padded_key}{value}")

    def box(self, content: str) -> None:
        """Display content in a box."""
        if self.ascii_only:
            for line in content.splitlines():
                self.console.print(f"  {line}")
        else:
            self.console.print(Panel(content, expand=False))

    def info(self, text: str) -> None:
        """Display info message (dim)."""
        self.console.print(text, style="dim")

    def ok(self, text: str) -> None:
        """Display success message (green)."""
        self.console.print(f"OK: {text}", style="green")

    def warn(self, text: str) -> None:
        """Display warning message (yellow)."""
        self.console.print(f"WARN: {text}", style="yellow")

    def err(self, text: str) -> None:
        """Display error message (red)."""
        self.console.print(f"ERROR: {text}", style="red bold")

    def channel_header(self, channel: str, title: str = "") -> None:
        """Display channel header with optional title."""
        color = CHANNEL_COLORS.get(channel, "white")
        full_title = f"{channel} \u00b7 {title}" if title else channel
        self.hr()
        self.console.print(full_title, style=f"bold {color}")
        self.hr()

    def channel_footer(self, channel: str, title: str = "") -> None:
        """Display channel footer."""
        full_title = f"{channel} \u00b7 {title}" if title else channel
        self.console.print(f"end: {full_title}", style="dim")

    def stream_line(self, tag: str, line: str) -> None:
        """Display a single prefixed line."""
        color = CHANNEL_COLORS.get(tag, "white")
        sep = "|" if self.ascii_only else "\u2502"
        prefix = Text(f"{tag} {sep} ", style=color)
        self.console.print(prefix, end="")
        self.console.print(line)

    def stream_lines(self, tag: str, stream: TextIO) -> Iterator[str]:
        """Stream lines with prefix, yielding raw lines."""
        for line in stream:
            line = line.rstrip("\n")
            self.stream_line(tag, line)
            yield line

    def choose(self, header: str, options: list[str], default: int = 0) -> int:
        """Interactive choice, returns selected index."""
        if not self.can_prompt():
            return default

        self.console.print(f"\n{header}")
        for i, opt in enumerate(options):
            marker = "*" if i == default else " "
            self.console.print(f"  {marker} {i + 1}. {opt}")

        while True:
            choice = Prompt.ask(
                "Select option",
                default=str(default + 1),
                console=Console(file=sys.stdin),
            )
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(options):
                    return idx
            except ValueError:
                # Try matching by first word
                choice_lower = choice.lower().split()[0] if choice else ""
                for i, opt in enumerate(options):
                    if opt.lower().startswith(choice_lower):
                        return i

            self.console.print(f"Invalid choice. Enter 1-{len(options)}", style="red")

    def confirm(self, prompt: str, default: bool = False) -> bool:
        """Interactive yes/no confirmation."""
        if not self.can_prompt():
            return default
        return Confirm.ask(prompt, default=default)

    def can_prompt(self) -> bool:
        """Check if interactive prompts are available."""
        return sys.stdin.isatty()
