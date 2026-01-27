"""Protocol-level MCP transport tests."""

from __future__ import annotations

import socket
from pathlib import Path
import anyio
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from ralph_py.mcp.transport import http as http_transport

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_ralph_files(root: Path) -> None:
    ralph_dir = root / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("prompt\n")
    (ralph_dir / "understand_prompt.md").write_text("understand\n")
    (ralph_dir / "prd_prompt.txt").write_text("prd prompt\n")
    (ralph_dir / "progress.txt").write_text("# Progress\n")
    (ralph_dir / "codebase_map.md").write_text("# Codebase Map\n")
    (ralph_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}\n')


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_port(host: str, port: int) -> None:
    for _ in range(50):
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            await anyio.sleep(0.05)
    raise RuntimeError(f"Server did not start on {host}:{port}")


def test_stdio_protocol_validate(tmp_path: Path) -> None:
    _write_ralph_files(tmp_path)
    log_dir = tmp_path / ".ralph" / "logs"

    async def _run() -> None:
        params = StdioServerParameters(
            command="uv",
            args=[
                "run",
                "ralph",
                "mcp",
                "--transport",
                "stdio",
                "--root",
                str(tmp_path),
                "--log-dir",
                str(log_dir),
            ],
            env={
                "UV_CACHE_DIR": "/tmp/uv-cache",
                "UV_NO_CACHE": "1",
            },
            cwd=_PROJECT_ROOT,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool("ralph.validate", {"root": str(tmp_path)})

        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["exit_code"] == 0
        log_path = Path(result.structuredContent["log_path"])
        assert log_path.exists()

    anyio.run(_run)


def test_http_protocol_validate(tmp_path: Path) -> None:
    _write_ralph_files(tmp_path)
    log_dir = tmp_path / ".ralph" / "logs"
    host = "127.0.0.1"
    port = _find_free_port()

    async def _run() -> None:
        app = http_transport._build_app(tmp_path, log_dir)
        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)

        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve)
            try:
                await _wait_for_port(host, port)
                url = f"http://{host}:{port}/mcp"
                async with streamable_http_client(url) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool("ralph.validate", {"root": str(tmp_path)})

                assert result.isError is False
                assert result.structuredContent is not None
                assert result.structuredContent["exit_code"] == 0
                log_path = Path(result.structuredContent["log_path"])
                assert log_path.exists()
            finally:
                server.should_exit = True
                await anyio.sleep(0.05)
                tg.cancel_scope.cancel()

    anyio.run(_run)
