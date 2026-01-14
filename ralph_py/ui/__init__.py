"""UI module for Ralph terminal output."""

from ralph_py.ui.base import UI
from ralph_py.ui.rich_ui import RichUI
from ralph_py.ui.plain import PlainUI

__all__ = ["UI", "RichUI", "PlainUI", "get_ui"]


def get_ui(mode: str = "auto", no_color: bool = False, ascii_only: bool = False) -> UI:
    """Get appropriate UI implementation based on mode and environment."""
    import sys

    if mode == "plain":
        return PlainUI(no_color=no_color, ascii_only=ascii_only)

    # auto or rich mode
    try:
        # Check if we have a real terminal
        is_tty = sys.stderr.isatty()
        if mode == "auto" and not is_tty:
            return PlainUI(no_color=no_color, ascii_only=ascii_only)
        return RichUI(no_color=no_color, ascii_only=ascii_only)
    except Exception:
        return PlainUI(no_color=no_color, ascii_only=ascii_only)
