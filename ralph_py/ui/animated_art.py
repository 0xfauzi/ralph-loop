"""Animated ASCII art for Ralph UI."""
from __future__ import annotations

import math
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from rich.text import Text

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions
LARGE_ACCENT = "o"
LARGE_ACCENT_SECONDARY = "O"
LARGE_PRIMARY = "."
LARGE_SECONDARY = ":"


def _lissajous_points(
    center_x: int,
    center_y: int,
    amp_x: float,
    amp_y: float,
    steps: int,
    a: int = 1,
    b: int = 2,
    phase: float = 0.0,
) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for idx in range(steps):
        t = (2 * math.pi * idx) / steps
        x = int(round(center_x + math.sin(a * t + phase) * amp_x))
        y = int(round(center_y + math.sin(b * t) * amp_y))
        points.append((x, y))
    return points


def build_loop_frames(width: int = 31, height: int = 11, steps: int = 48) -> list[list[str]]:
    """Build abstract loop frames with orbiting highlights."""
    center_x = width // 2
    center_y = height // 2
    outer = _lissajous_points(center_x, center_y, width * 0.4, height * 0.32, steps)
    inner = _lissajous_points(
        center_x,
        center_y,
        width * 0.23,
        height * 0.18,
        steps,
        phase=math.pi / 2,
    )

    base_grid = [[" " for _ in range(width)] for _ in range(height)]
    for x, y in set(outer):
        if 0 <= y < height and 0 <= x < width:
            base_grid[y][x] = LARGE_PRIMARY
    for x, y in set(inner):
        if 0 <= y < height and 0 <= x < width:
            if base_grid[y][x] == " ":
                base_grid[y][x] = LARGE_SECONDARY

    frames: list[list[str]] = []
    for idx in range(steps):
        grid = [row[:] for row in base_grid]
        outer_point = outer[idx % len(outer)]
        inner_point = inner[(idx * 2) % len(inner)]
        outer_trail = outer[(idx - 2) % len(outer)]
        inner_trail = inner[(idx * 2 - 2) % len(inner)]
        grid[outer_trail[1]][outer_trail[0]] = LARGE_SECONDARY
        grid[inner_trail[1]][inner_trail[0]] = LARGE_SECONDARY
        grid[outer_point[1]][outer_point[0]] = LARGE_ACCENT
        grid[inner_point[1]][inner_point[0]] = LARGE_ACCENT_SECONDARY
        frames.append(["".join(row) for row in grid])
    return frames


def render_loop_frame(lines: Iterable[str]) -> Text:
    """Render a loop frame into styled Rich text."""
    text = Text()
    for line in lines:
        for ch in line:
            if ch == LARGE_ACCENT:
                text.append(ch, style="bold cyan")
            elif ch == LARGE_ACCENT_SECONDARY:
                text.append(ch, style="bold white")
            elif ch.isalpha():
                text.append(ch, style="bold white")
            elif ch == LARGE_PRIMARY:
                text.append(ch, style="dim")
            elif ch == LARGE_SECONDARY:
                text.append(ch, style="dim")
            elif ch.strip():
                text.append(ch, style="dim")
            else:
                text.append(ch)
        text.append("\n")
    if text:
        text.rstrip()
    return text


def large_loop_frames() -> list[Text]:
    """Return rendered large loop frames."""
    raw_frames = build_loop_frames()
    return [render_loop_frame(frame) for frame in raw_frames]


SMALL_LOOP_FRAMES = [
    "\u280b",
    "\u2819",
    "\u2839",
    "\u2838",
    "\u283c",
    "\u2834",
    "\u2826",
    "\u2827",
    "\u2807",
    "\u280f",
]


def small_loop_frames() -> list[str]:
    """Return small loop frames for streaming."""
    return SMALL_LOOP_FRAMES


def render_small_frame(frame: str) -> Text:
    """Render a small loop frame with subtle styling."""
    text = Text()
    text.append("[", style="dim")
    text.append(frame, style="cyan")
    text.append("]", style="dim")
    return text


class LoopIndicator:
    """Renderable that animates a small loop indicator."""

    def __init__(self, frames: list[str], interval: float = 0.08):
        self._frames = frames
        self._interval = interval
        self._start = time.monotonic()

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> Iterable[Text]:
        if not self._frames:
            yield Text("")
            return
        elapsed = time.monotonic() - self._start
        idx = int(elapsed / self._interval) % len(self._frames)
        yield render_small_frame(self._frames[idx])
