"""HTTP transport adapter for the MCP server."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from ralph_py.mcp.server import build_server
from ralph_py.mcp.transport.locks import acquire_root_lock


def start(*, root: Path | None, host: str | None, port: int | None, log_dir: Path) -> None:
    """Start the MCP server in HTTP mode."""
    root_path = (root or Path.cwd()).expanduser().resolve()
    host_value = host or "127.0.0.1"
    port_value = port or 8765
    app = _build_app(root_path, log_dir)
    with acquire_root_lock(root_path):
        uvicorn.run(app, host=host_value, port=port_value, log_level="info")


def _build_app(root: Path, log_dir: Path) -> Starlette:
    server = build_server(root=root, log_dir=log_dir)
    session_manager = StreamableHTTPSessionManager(server)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    async def mcp_app(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    async def not_found(_: Request) -> PlainTextResponse:
        return PlainTextResponse("Not Found", status_code=404)

    routes = [
        Mount("/mcp", app=mcp_app),
        Route("/", endpoint=not_found, methods=["GET", "POST", "DELETE"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
