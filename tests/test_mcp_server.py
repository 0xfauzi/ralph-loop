"""Integration tests for MCP server handlers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
from mcp import types

from ralph_py.loop import COMPLETION_MARKER
from ralph_py.mcp import runner
from ralph_py.mcp import server as mcp_server


class FakeAgent:
    """Fake agent that emits a completion marker."""

    def __init__(self, output: list[str]):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "fake"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        yield from self._output
        if self._output:
            self._final_message = self._output[-1]

    @property
    def final_message(self) -> str | None:
        return self._final_message


def _write_ralph_files(root: Path) -> None:
    ralph_dir = root / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("prompt\n")
    (ralph_dir / "understand_prompt.md").write_text("understand\n")
    (ralph_dir / "prd_prompt.txt").write_text("prd prompt\n")
    (ralph_dir / "progress.txt").write_text("# Progress\n")
    (ralph_dir / "codebase_map.md").write_text("# Codebase Map\n")
    (ralph_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}\n')


def _read_summary_payload(log_path: Path) -> dict[str, object]:
    summary_path = log_path.with_name(f"{log_path.stem}.summary.json")
    return json.loads(summary_path.read_text())


class TestMcpHandlers:
    """Tests for MCP tool handlers."""

    def test_run_handler_writes_logs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path
        _write_ralph_files(root)

        fake_agent = FakeAgent([COMPLETION_MARKER])
        monkeypatch.setattr(runner, "get_agent", lambda *args, **kwargs: fake_agent)
        monkeypatch.setattr(
            runner.CodexAgent,
            "is_available",
            classmethod(lambda cls: True),
        )

        context = mcp_server.ToolContext(log_dir=Path(".ralph/logs"))
        payload = mcp_server._handle_run(
            context,
            {
                "root": str(root),
                "allow_edits": False,
                "max_iterations": 1,
                "sleep_seconds": 0,
                "ui_mode": "plain",
            },
        )

        assert isinstance(payload, dict)
        assert payload["exit_code"] == 0
        log_path = Path(payload["log_path"])
        assert log_path.exists()

        summary_payload = _read_summary_payload(log_path)
        assert summary_payload["tool"] == "ralph.run"
        assert summary_payload["exit_code"] == 0
        assert summary_payload["log_path"] == str(log_path)

    def test_understand_handler_writes_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path
        _write_ralph_files(root)

        fake_agent = FakeAgent([COMPLETION_MARKER])
        monkeypatch.setattr(runner, "get_agent", lambda *args, **kwargs: fake_agent)
        monkeypatch.setattr(
            runner.CodexAgent,
            "is_available",
            classmethod(lambda cls: True),
        )

        context = mcp_server.ToolContext(log_dir=Path(".ralph/logs"))
        payload = mcp_server._handle_understand(
            context,
            {
                "root": str(root),
                "allow_edits": False,
                "max_iterations": 1,
                "sleep_seconds": 0,
                "ui_mode": "plain",
            },
        )

        assert isinstance(payload, dict)
        assert payload["exit_code"] == 0
        log_path = Path(payload["log_path"])
        assert log_path.exists()

        summary_payload = _read_summary_payload(log_path)
        assert summary_payload["tool"] == "ralph.understand"
        assert summary_payload["exit_code"] == 0
        assert summary_payload["log_path"] == str(log_path)

    def test_validate_handler_writes_logs(self, tmp_path: Path) -> None:
        root = tmp_path
        _write_ralph_files(root)

        context = mcp_server.ToolContext(log_dir=Path(".ralph/logs"))
        payload = mcp_server._handle_validate(context, {"root": str(root)})

        assert isinstance(payload, dict)
        assert payload["exit_code"] == 0
        log_path = Path(payload["log_path"])
        assert log_path.exists()

        summary_payload = _read_summary_payload(log_path)
        assert summary_payload["tool"] == "ralph.validate"
        assert summary_payload["exit_code"] == 0
        assert summary_payload["log_path"] == str(log_path)

    def test_validate_handler_missing_file(self, tmp_path: Path) -> None:
        root = tmp_path
        _write_ralph_files(root)
        (root / "scripts" / "ralph" / "progress.txt").unlink()

        context = mcp_server.ToolContext(log_dir=Path(".ralph/logs"))
        payload = mcp_server._handle_validate(context, {"root": str(root)})

        assert isinstance(payload, types.CallToolResult)
        assert payload.isError is True
        assert payload.structuredContent is not None
        structured = payload.structuredContent
        assert structured["code"] == "invalid_argument"
        assert structured["message"] == "Validation failed"
        assert any(
            detail.get("field") == "progress_file" for detail in structured["details"]
        )
