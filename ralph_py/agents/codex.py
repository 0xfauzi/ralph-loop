"""Codex CLI agent for Ralph."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator


class CodexAgent:
    """Agent that uses the Codex CLI."""

    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ):
        """Initialize Codex agent.

        Args:
            model: Model name to pass to codex -m
            reasoning_effort: Reasoning effort level for codex -c
        """
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        """Human-readable agent name."""
        if self._model:
            return f"codex ({self._model})"
        return "codex"

    @classmethod
    def is_available(cls) -> bool:
        """Check if codex CLI is available."""
        return shutil.which("codex") is not None

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        """Run codex with prompt piped to stdin.

        Yields output lines as they arrive.
        """
        self._final_message = None

        # Build command
        cmd = ["codex"]
        if self._model:
            cmd.extend(["-m", self._model])
        if self._reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{self._reasoning_effort}"'])

        # Use temp file for --output-last-message
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            last_msg_file = Path(f.name)

        cmd.extend(["--output-last-message", str(last_msg_file)])

        try:
            proc = subprocess.Popen(
                cmd,
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
            if proc.stdout:
                for line in proc.stdout:
                    yield line.rstrip("\n")

            proc.wait()

            # Read final message
            if last_msg_file.exists():
                content = last_msg_file.read_text().strip()
                if content:
                    self._final_message = content

        finally:
            # Cleanup temp file
            try:
                last_msg_file.unlink()
            except Exception:
                pass

    @property
    def final_message(self) -> str | None:
        """Return final message from --output-last-message."""
        return self._final_message
