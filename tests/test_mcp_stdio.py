"""Tests for MCP stdio transport safety."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import anyio
import pytest

from ralph_py.mcp.transport import stdio as stdio_transport


class _FakeServer:
    def create_initialization_options(self) -> dict[str, str]:
        return {}

    async def run(self, *_args: object) -> None:
        print("stdout noise")
        sys.stdout.write("more stdout noise\n")
        sys.stderr.write("stderr noise\n")


@asynccontextmanager
async def _fake_stdio_server() -> AsyncIterator[tuple[object, object]]:
    read_sender, read_receiver = anyio.create_memory_object_stream(0)
    write_sender, write_receiver = anyio.create_memory_object_stream(0)
    try:
        yield read_receiver, write_sender
    finally:
        await read_sender.aclose()
        await read_receiver.aclose()
        await write_sender.aclose()
        await write_receiver.aclose()


def test_stdio_transport_redirects_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_server = _FakeServer()
    monkeypatch.setattr(stdio_transport, "build_server", lambda **_: fake_server)
    monkeypatch.setattr(stdio_transport.stdio, "stdio_server", _fake_stdio_server)

    anyio.run(stdio_transport._run_stdio, tmp_path, tmp_path / ".ralph/logs")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stdout noise" in captured.err
    assert "more stdout noise" in captured.err
    assert "stderr noise" in captured.err
