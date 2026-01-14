"""Rich-based terminal UI implementation."""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Iterator

from rich import box
from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from ralph_py.ui import animated_art

if TYPE_CHECKING:
    from io import TextIO

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
        self._block_left = "|" if ascii_only else "\u2502"
        self._block_tl = "+" if ascii_only else "\u250c"
        self._block_tr = "+" if ascii_only else "\u2510"
        self._block_bl = "+" if ascii_only else "\u2514"
        self._block_br = "+" if ascii_only else "\u2518"
        self._tag_width = max(len(tag) for tag in CHANNEL_COLORS)
        self._block_active = False
        self._stream_live: Live | None = None

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

    def _build_stream_indicator(self) -> Table:
        frames = animated_art.small_loop_frames()
        indicator = animated_art.LoopIndicator(frames)
        width = max((len(frame) for frame in frames), default=1) + 2
        table = Table.grid(expand=True)
        table.add_column(ratio=1)
        table.add_column(width=width, justify="right")
        table.add_row("", indicator)
        return table

    def _start_stream_indicator(self) -> None:
        if self._stream_live is not None or not self._animations_enabled():
            return
        table = self._build_stream_indicator()
        self._stream_live = Live(
            table,
            console=self.console,
            refresh_per_second=12,
            transient=True,
        )
        self._stream_live.start()

    def _stop_stream_indicator(self) -> None:
        if self._stream_live is None:
            return
        self._stream_live.stop()
        self._stream_live = None

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

    def _block_footer_line(self) -> Text:
        """Build a block footer line."""
        width = self.console.size.width
        if width <= 2:
            return Text(self._block_bl + self._block_br, style="dim")
        line = Text()
        line.append(self._block_bl, style="dim")
        line.append(self._hr_char * (width - 2), style="dim")
        line.append(self._block_br, style="dim")
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

    def box(self, content: str) -> None:
        """Display content in a box."""
        if self.ascii_only:
            for line in content.splitlines():
                self.console.print(f"  {line}")
        else:
            self.console.print(
                Panel(
                    content,
                    expand=False,
                    box=box.MINIMAL,
                    border_style="dim",
                    padding=(0, 1),
                )
            )

    def panel(self, tag: str, title: str, content: str) -> None:
        """Display a titled panel block."""
        label = f"{tag} \u00b7 {title}" if title else tag
        title_text = Text(label, style=self._badge_style(tag))
        self.console.print(
            Panel(
                Text(content),
                title=title_text,
                border_style="dim",
                box=box.SQUARE,
                expand=True,
                padding=(0, 1),
            )
        )

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
            self._block_active = True
            self.console.print(self._block_header_line(full_title, self._badge_style(channel)))
            return

        self._block_active = True
        self.console.print(self._block_header_line(full_title, self._badge_style(channel)))
        if channel == "AI" and title == "Agent output":
            self._start_stream_indicator()

    def channel_footer(self, channel: str, title: str = "") -> None:
        """Display channel footer."""
        _ = channel
        _ = title
        self.console.print(self._block_footer_line())
        self._block_active = False
        if self._stream_live is not None:
            self._stop_stream_indicator()

    def stream_line(self, tag: str, line: str) -> None:
        """Display a single prefixed line."""
        sep = "|" if self.ascii_only else "\u2502"
        tag_label = self._format_tag(tag)
        prefix = Text()
        if self._block_active:
            prefix.append(f"{self._block_left} ", style="dim")
        prefix.append(f"{tag_label} ", style=self._badge_style(tag))
        prefix.append(f"{sep} ", style="dim")
        self.console.print(prefix, end="")
        line_style = LINE_STYLES.get(tag)
        if line_style:
            self.console.print(Text(line, style=line_style))
        else:
            self.console.print(line, markup=False)

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

        try:
            from prompt_toolkit.shortcuts import radiolist_dialog
        except Exception:
            radiolist_dialog = None

        if radiolist_dialog is not None:
            values = [(idx, opt) for idx, opt in enumerate(options)]
            result = radiolist_dialog(title="Ralph", text=header, values=values).run()
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

    def confirm(self, prompt: str, default: bool = False) -> bool:
        """Interactive yes/no confirmation."""
        if not self.can_prompt():
            return default
        try:
            from prompt_toolkit.shortcuts import yes_no_dialog
        except Exception:
            yes_no_dialog = None

        if yes_no_dialog is not None:
            result = yes_no_dialog(title="Ralph", text=prompt).run()
            if result is None:
                return default
            return bool(result)
        return Confirm.ask(prompt, default=default, console=self.console)

    def can_prompt(self) -> bool:
        """Check if interactive prompts are available."""
        return sys.stdin.isatty()
