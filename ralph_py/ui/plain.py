"""Plain text UI implementation (no Rich dependency)."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import TextIO

# ANSI color codes
COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "bright_black": "\033[90m",
    "bright_blue": "\033[94m",
}

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


class PlainUI:
    """Plain text UI with optional ANSI colors."""

    def __init__(
        self,
        no_color: bool = False,
        ascii_only: bool = False,
        file: TextIO | None = None,
    ):
        self.no_color = no_color
        self.ascii_only = ascii_only
        self._file = file or sys.stderr
        self._hr_char = "-" if ascii_only else "\u2500"
        self._sep_char = "|" if ascii_only else "\u2502"
        self._block_left = "|" if ascii_only else "\u2502"
        self._block_tl = "+" if ascii_only else "\u250c"
        self._block_tr = "+" if ascii_only else "\u2510"
        self._block_bl = "+" if ascii_only else "\u2514"
        self._block_br = "+" if ascii_only else "\u2518"
        self._tag_width = max(len(tag) for tag in CHANNEL_COLORS)
        self._width = 80
        self._block_active = False

        # Try to get terminal width
        try:
            import shutil
            self._width = shutil.get_terminal_size().columns
        except Exception:
            pass

    def _color(self, text: str, *styles: str) -> str:
        """Apply color codes if colors are enabled."""
        if self.no_color:
            return text
        prefix = "".join(COLORS.get(s, "") for s in styles)
        return f"{prefix}{text}{COLORS['reset']}" if prefix else text

    def _print(self, text: str = "") -> None:
        """Print to output file."""
        print(text, file=self._file)

    def _format_tag(self, tag: str) -> str:
        """Pad tag to a stable width for alignment."""
        if len(tag) > self._tag_width:
            self._tag_width = len(tag)
        return tag.ljust(self._tag_width)

    def _block_header_line(self, label: str) -> str:
        """Build a block header line with label."""
        label_text = f" {label} "
        width = max(self._width, len(label_text) + 2)
        fill_len = width - len(label_text) - 2
        if fill_len < 0:
            return label_text
        return f"{self._block_tl}{label_text}{self._hr_char * fill_len}{self._block_tr}"

    def _block_footer_line(self) -> str:
        """Build a block footer line."""
        width = max(self._width, 2)
        return f"{self._block_bl}{self._hr_char * (width - 2)}{self._block_br}"

    def title(self, text: str) -> None:
        """Display a large title."""
        self.hr()
        padding = (self._width - len(text)) // 2
        self._print(self._color(" " * padding + text, "bold"))
        self.hr()
        self._print()

    def startup_art(self) -> None:
        """Display startup animation (plain UI does not animate)."""
        return

    def section(self, text: str) -> None:
        """Display a section header."""
        self._print()
        self._print(self._color(f"== {text} ==", "bold"))

    def subsection(self, text: str) -> None:
        """Display a subsection header."""
        self._print(self._color(f"-- {text} --", "dim"))

    def hr(self) -> None:
        """Display a horizontal rule."""
        self._print(self._color(self._hr_char * self._width, "dim"))

    def kv(self, key: str, value: str) -> None:
        """Display a key-value pair."""
        padded_key = f"  {key}:".ljust(16)
        self._print(f"{padded_key}{value}")

    def box(self, content: str) -> None:
        """Display content in a box (just indented)."""
        for line in content.splitlines():
            self._print(f"  {line}")

    def panel(self, tag: str, title: str, content: str) -> None:
        """Display a titled panel block."""
        label = f"{tag} \u00b7 {title}" if title else tag
        self._print(self._block_header_line(label))
        for line in content.splitlines():
            self._print(f"{self._block_left} {line}")
        self._print(self._block_footer_line())

    def info(self, text: str) -> None:
        """Display info message (dim)."""
        self._print(self._color(text, "dim"))

    def ok(self, text: str) -> None:
        """Display success message (green)."""
        self._print(self._color(f"OK: {text}", "green"))

    def warn(self, text: str) -> None:
        """Display warning message (yellow)."""
        self._print(self._color(f"WARN: {text}", "yellow"))

    def err(self, text: str) -> None:
        """Display error message (red)."""
        self._print(self._color(f"ERROR: {text}", "red", "bold"))

    def channel_header(self, channel: str, title: str = "") -> None:
        """Display channel header with optional title."""
        full_title = f"{channel} \u00b7 {title}" if title else channel
        self._block_active = True
        self._print(self._block_header_line(full_title))

    def channel_footer(self, channel: str, title: str = "") -> None:
        """Display channel footer."""
        _ = channel
        _ = title
        self._block_active = False
        self._print(self._block_footer_line())

    def stream_line(self, tag: str, line: str) -> None:
        """Display a single prefixed line."""
        color = CHANNEL_COLORS.get(tag, "white")
        tag_label = self._format_tag(tag)
        block = f"{self._block_left} " if self._block_active else ""
        prefix = self._color(f"{block}{tag_label} {self._sep_char} ", color)
        line_style = "dim" if tag in {"SYS", "PROMPT"} else ""
        if line_style:
            self._print(f"{prefix}{self._color(line, line_style)}")
        else:
            self._print(f"{prefix}{line}")

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

        prompt_radiolist_dialog: Callable[..., Any] | None
        try:
            from prompt_toolkit.shortcuts import (  # type: ignore[import-not-found]
                radiolist_dialog as _radiolist_dialog,
            )
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

        self._print(f"\n{header}")
        for i, opt in enumerate(options):
            marker = "*" if i == default else " "
            self._print(f"  {marker} {i + 1}. {opt}")

        while True:
            try:
                choice = input(f"Select option [{default + 1}]: ").strip()
                if not choice:
                    return default

                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(options):
                        return idx
                except ValueError:
                    # Try matching by first word
                    choice_lower = choice.lower().split()[0]
                    for i, opt in enumerate(options):
                        if opt.lower().startswith(choice_lower):
                            return i

                self._print(self._color(f"Invalid choice. Enter 1-{len(options)}", "red"))
            except (EOFError, KeyboardInterrupt):
                return default

    def confirm(self, prompt: str, default: bool = False) -> bool:
        """Interactive yes/no confirmation."""
        if not self.can_prompt():
            return default

        prompt_yes_no_dialog: Callable[..., Any] | None
        try:
            from prompt_toolkit.shortcuts import yes_no_dialog as _yes_no_dialog
        except Exception:
            prompt_yes_no_dialog = None
        else:
            prompt_yes_no_dialog = _yes_no_dialog

        if prompt_yes_no_dialog is not None:
            result = prompt_yes_no_dialog(title="Ralph", text=prompt).run()
            if result is None:
                return default
            return bool(result)

        suffix = "[Y/n]" if default else "[y/N]"
        try:
            response = input(f"{prompt} {suffix}: ").strip().lower()
            if not response:
                return default
            return response in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return default

    def can_prompt(self) -> bool:
        """Check if interactive prompts are available."""
        return sys.stdin.isatty()
