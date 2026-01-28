"""Rich-based terminal UI implementation."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich import box
from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from ralph_py.ui import animated_art

if TYPE_CHECKING:
    from typing import TextIO

# Channel colors matching shell script
CHANNEL_COLORS = {
    "AI": "cyan",
    "USER": "green",
    "PROMPT": "blue",
    "THINK": "magenta",
    "SYS": "bright_black",
    "TOOL": "yellow",
    "GIT": "bright_blue",
    "GUARD": "red",
    "PLAN": "green",
    "REPORT": "white",
    "ACTIONS": "yellow",
}

BADGE_STYLES = {
    "AI": "bold cyan",
    "USER": "bold green",
    "PROMPT": "bold blue",
    "THINK": "bold magenta",
    "SYS": "dim",
    "TOOL": "bold yellow",
    "GIT": "bold bright_blue",
    "GUARD": "bold red",
    "PLAN": "bold green",
    "REPORT": "bold white",
    "ACTIONS": "bold yellow",
}

LINE_STYLES = {
    "SYS": "dim",
    "PROMPT": "dim",
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
        self._block_tl = "+" if ascii_only else "\u250c"
        self._block_tr = "+" if ascii_only else "\u2510"
        self._tag_width = max(len(tag) for tag in CHANNEL_COLORS)

    def _format_tag(self, tag: str) -> str:
        """Pad tag to a stable width for alignment."""
        if len(tag) > self._tag_width:
            self._tag_width = len(tag)
        return tag.ljust(self._tag_width)

    def _badge_style(self, tag: str) -> str:
        """Return a style string for tag badges."""
        return BADGE_STYLES.get(tag, f"bold {CHANNEL_COLORS.get(tag, 'white')}")

    def _animations_enabled(self) -> bool:
        """Return True when terminal animations are safe to render."""
        if self.ascii_only or not self.console.is_terminal:
            return False
        isatty = getattr(self._file, "isatty", None)
        return bool(isatty and isatty())

    def _block_header_line(self, label: str, style: str) -> Text:
        """Build a block header line with label."""
        label_text = f" {label} "
        width = self.console.size.width
        if width <= len(label_text) + 2:
            return Text(label_text, style=style)
        fill_len = width - len(label_text) - 2
        line = Text()
        line.append(self._block_tl, style="dim")
        line.append(label_text, style=style)
        line.append(self._hr_char * fill_len, style="dim")
        line.append(self._block_tr, style="dim")
        return line

    def title(self, text: str) -> None:
        """Display a large title."""
        self.hr()
        self.console.print(text, justify="center", style="bold")
        self.hr()
        self.console.print()

    def startup_art(self) -> None:
        """Display startup animation."""
        if not self._animations_enabled():
            return
        frames = animated_art.large_loop_frames()
        if not frames:
            return
        delay = 1 / 24
        panel = Panel(
            "",
            title=Text("Ralph", style="bold"),
            subtitle=Text("agentic loop", style="dim"),
            border_style="dim",
            box=box.SQUARE,
            padding=(1, 2),
            expand=False,
        )
        try:
            with Live(panel, console=self.console, refresh_per_second=24, transient=True) as live:
                for art in frames:
                    panel = Panel(
                        Align.center(art),
                        title=Text("Ralph", style="bold"),
                        subtitle=Text("agentic loop", style="dim"),
                        border_style="dim",
                        box=box.SQUARE,
                        padding=(1, 2),
                        expand=False,
                    )
                    live.update(panel, refresh=True)
                    time.sleep(delay)
        except Exception:
            self.console.print(panel)

    def section(self, text: str) -> None:
        """Display a section header."""
        self.console.print()
        if self.ascii_only:
            self.console.print(f"== {text} ==", style="bold")
        else:
            self.console.rule(Text(text, style="bold"), style="dim")

    def subsection(self, text: str) -> None:
        """Display a subsection header."""
        self.console.print(Text(text, style="bold dim"))

    def hr(self) -> None:
        """Display a horizontal rule."""
        width = self.console.size.width
        self.console.print(self._hr_char * width, style="dim")

    def kv(self, key: str, value: str) -> None:
        """Display a key-value pair."""
        # Fixed-width key column (14 chars like shell script)
        padded_key = f"  {key}:".ljust(16)
        self.console.print(Text(padded_key, style="dim") + Text(value))

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
        full_title = f"{channel} \u00b7 {title}" if title else channel
        if self.ascii_only:
            self.console.print(self._block_header_line(full_title, self._badge_style(channel)))
            return

        self.console.print(self._block_header_line(full_title, self._badge_style(channel)))

    def stream_line(self, tag: str, line: str) -> None:
        """Display a single prefixed line."""
        sep = "|" if self.ascii_only else "\u2502"
        tag_label = self._format_tag(tag)
        prefix = Text()
        prefix.append(f"{tag_label} ", style=self._badge_style(tag))
        prefix.append(f"{sep} ", style="dim")
        self.console.print(prefix, end="")
        line_style = LINE_STYLES.get(tag)
        if line_style:
            self.console.print(Text(line, style=line_style))
        else:
            self.console.print(line, markup=False)

    def choose(self, header: str, options: list[str], default: int = 0) -> int:
        """Interactive choice, returns selected index."""
        if not self.can_prompt():
            return default

        prompt_radiolist_dialog: Callable[..., Any] | None
        try:
            from prompt_toolkit.shortcuts import radiolist_dialog as _radiolist_dialog
        except Exception:
            prompt_radiolist_dialog = None
        else:
            prompt_radiolist_dialog = _radiolist_dialog

        if prompt_radiolist_dialog is not None:
            values = [(idx, opt) for idx, opt in enumerate(options)]
            result = prompt_radiolist_dialog(
                title="Ralph", text=header, values=values
            ).run()
            if result is None:
                return default
            return int(result)

        self.console.print(f"\n{header}")
        for i, opt in enumerate(options):
            marker = "*" if i == default else " "
            self.console.print(f"  {marker} {i + 1}. {opt}")

        while True:
            choice = Prompt.ask(
                "Select option",
                default=str(default + 1),
                console=self.console,
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

    def can_prompt(self) -> bool:
        """Check if interactive prompts are available."""
        return sys.stdin.isatty()
