"""Stdio transport adapter for the MCP server."""

from __future__ import annotations

from contextlib import redirect_stdout
from pathlib import Path
import sys

import anyio
from mcp.server import stdio

from ralph_py.mcp.server import build_server
from ralph_py.mcp.transport.locks import acquire_root_lock


def start(*, root: Path | None, log_dir: Path) -> None:
    """Start the MCP server in stdio mode."""
    root_path = (root or Path.cwd()).expanduser().resolve()
    with acquire_root_lock(root_path):
        anyio.run(_run_stdio, root_path, log_dir)


async def _run_stdio(root: Path, log_dir: Path) -> None:
    server = build_server(root=root, log_dir=log_dir)
    async with stdio.stdio_server() as (read_stream, write_stream):
        with redirect_stdout(sys.stderr):
            await server.run(read_stream, write_stream, server.create_initialization_options())
