"""Custom command agent for Ralph."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator


class CustomAgent:
    """Agent that runs a custom shell command."""

    def __init__(self, command: str):
        """Initialize with command string.

        Args:
            command: Shell command to run. Prompt is piped to stdin.
        """
        self._command = command
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        """Human-readable agent name."""
        return f"custom ({self._command})"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        """Run command with prompt piped to stdin.

        Yields output lines as they arrive.
        """
        self._final_message = None

        proc = subprocess.Popen(
            self._command,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
        )

        # Write prompt to stdin
        if proc.stdin:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except BrokenPipeError:
                pass

        # Stream output
        output_lines: list[str] = []
        if proc.stdout:
            for line in proc.stdout:
                line = line.rstrip("\n")
                output_lines.append(line)
                yield line

        proc.wait()

        # Store last output as "final message" for consistency
        if output_lines:
            self._final_message = output_lines[-1]

    @property
    def final_message(self) -> str | None:
        """Return last output line."""
        return self._final_message
