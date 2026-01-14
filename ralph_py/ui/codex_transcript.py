"""Codex transcript parsing for UI output."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TranscriptLine:
    """A parsed transcript line with a display tag."""

    tag: str
    text: str


class CodexTranscriptParser:
    """Parse codex transcript lines into tagged output lines."""

    def __init__(
        self,
        prompt_file: Path | None,
        show_prompt: bool,
        prompt_progress_every: int = 50,
    ) -> None:
        self._role = "SYS"
        self._show_prompt = show_prompt
        self._prompt_progress_every = max(prompt_progress_every, 0)

        self._prompt_lines: list[str] = []
        self._prompt_src = "prompt"
        if prompt_file and prompt_file.exists():
            self._prompt_src = str(prompt_file)
            self._prompt_lines = prompt_file.read_text().splitlines()

        self._prompt_hide_active = bool(self._prompt_lines) and not self._show_prompt
        self._prompt_i = 0
        self._hidden_prompt_lines = 0
        self._prompt_header_printed = False
        self._prompt_summary_printed = False

    def feed(self, line: str) -> list[TranscriptLine]:
        """Consume a raw codex line and return display-ready lines."""
        outputs: list[TranscriptLine] = []
        clean_line = line.rstrip("\r")

        marker = clean_line.strip().rstrip(":").lower()
        if marker:
            if marker == "user":
                self._role = "PROMPT"
                self._reset_prompt_state()
                return outputs
            if marker in {"assistant", "codex", "final"}:
                self._finish_prompt_hiding(outputs)
                self._role = "AI"
                return outputs
            if marker in {"thinking", "analysis"}:
                self._finish_prompt_hiding(outputs)
                self._role = "THINK"
                return outputs
            if marker in {"tool", "exec"}:
                self._role = "TOOL"
                return outputs
            if marker == "system":
                self._role = "SYS"
                return outputs

        if self._role == "PROMPT" and self._prompt_hide_active:
            if self._prompt_i >= len(self._prompt_lines):
                self._finish_prompt_hiding(outputs)
                self._role = "SYS"
            elif clean_line == self._prompt_lines[self._prompt_i]:
                self._prompt_i += 1
                self._hidden_prompt_lines += 1
                if not self._prompt_header_printed:
                    outputs.append(
                        TranscriptLine("PROMPT", f"[prompt hidden: {self._prompt_src}]")
                    )
                    self._prompt_header_printed = True
                if (
                    self._prompt_progress_every > 0
                    and self._hidden_prompt_lines % self._prompt_progress_every == 0
                ):
                    outputs.append(
                        TranscriptLine(
                            "PROMPT",
                            (
                                f"[prompt hidden: {self._prompt_src} - "
                                f"{self._hidden_prompt_lines} lines suppressed]"
                            ),
                        )
                    )
                return outputs
            else:
                self._finish_prompt_hiding(outputs)
                outputs.append(
                    TranscriptLine(
                        "SYS",
                        f"[prompt hiding disabled: output diverged from {self._prompt_src}]",
                    )
                )
                self._role = "SYS"

        outputs.append(TranscriptLine(self._role, clean_line))
        return outputs

    def _reset_prompt_state(self) -> None:
        self._prompt_hide_active = bool(self._prompt_lines) and not self._show_prompt
        self._prompt_i = 0
        self._hidden_prompt_lines = 0
        self._prompt_header_printed = False
        self._prompt_summary_printed = False

    def _finish_prompt_hiding(self, outputs: list[TranscriptLine]) -> None:
        if (
            self._prompt_hide_active
            and self._hidden_prompt_lines > 0
            and not self._prompt_summary_printed
        ):
            outputs.append(
                TranscriptLine(
                    "PROMPT",
                    (
                        f"[prompt hidden: {self._prompt_src} - "
                        f"{self._hidden_prompt_lines} lines suppressed]"
                    ),
                )
            )
            self._prompt_summary_printed = True
        self._prompt_hide_active = False
