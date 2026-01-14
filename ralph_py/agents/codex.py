"""Codex CLI agent for Ralph."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator


class CodexAgent:
    """Agent that uses the Codex CLI."""

    _supports_output_last_message: bool | None = None

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
        last_non_empty_line: str | None = None

        # Build command (non-interactive)
        cmd = ["codex", "exec"]
        if cwd:
            cmd.extend(["-C", str(cwd)])
        if self._model:
            cmd.extend(["-m", self._model])
        if self._reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{self._reasoning_effort}"'])

        # Use --output-last-message when supported by the codex CLI.
        last_msg_file: Path | None = None
        if self._codex_supports_output_last_message():
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
                    line = line.rstrip("\n")
                    if line.strip():
                        last_non_empty_line = line
                    yield line

            proc.wait()

            # Read final message
            if last_msg_file and last_msg_file.exists():
                content = last_msg_file.read_text().strip()
                if content:
                    self._final_message = content
            if self._final_message is None and last_non_empty_line:
                self._final_message = last_non_empty_line

        finally:
            # Cleanup temp file
            if last_msg_file is not None:
                try:
                    last_msg_file.unlink()
                except Exception:
                    pass

    @property
    def final_message(self) -> str | None:
        """Return final message from --output-last-message."""
        return self._final_message

    @classmethod
    def _codex_supports_output_last_message(cls) -> bool:
        """Check whether codex supports --output-last-message."""
        if cls._supports_output_last_message is not None:
            return cls._supports_output_last_message

        try:
            result = subprocess.run(
                ["codex", "exec", "--help"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            cls._supports_output_last_message = "--output-last-message" in result.stdout
        except Exception:
            cls._supports_output_last_message = False

        return cls._supports_output_last_message
