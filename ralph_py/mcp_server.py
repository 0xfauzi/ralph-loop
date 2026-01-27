"""Entry point for the Ralph MCP server."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ralph_py.mcp.transport import http as http_transport
from ralph_py.mcp.transport import stdio as stdio_transport

_TRANSPORT_CHOICES: tuple[str, ...] = ("stdio", "http")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the MCP server.")
    parser.add_argument(
        "--transport",
        choices=_TRANSPORT_CHOICES,
        default="stdio",
        help="Transport mode",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Project root path",
    )
    parser.add_argument(
        "--host",
        help="HTTP host",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="HTTP port",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(".ralph/logs"),
        help="Log directory",
    )
    return parser


def _start_transport(
    transport: str,
    root: Path | None,
    host: str | None,
    port: int | None,
    log_dir: Path,
) -> None:
    if transport == "stdio":
        stdio_transport.start(root=root, log_dir=log_dir)
        return
    if transport == "http":
        http_transport.start(root=root, host=host, port=port, log_dir=log_dir)
        return
    raise ValueError(f"Unknown transport: {transport}")


def main(argv: Sequence[str] | None = None) -> None:
    """Run the MCP server from the CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _start_transport(args.transport, args.root, args.host, args.port, args.log_dir)


if __name__ == "__main__":
    main()
