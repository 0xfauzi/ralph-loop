"""In-memory locking for MCP server transports."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

_LOCK = Lock()
_ACTIVE_ROOTS: set[Path] = set()


@contextmanager
def acquire_root_lock(root: Path) -> Iterator[None]:
    """Ensure only one MCP server runs per absolute root."""
    normalized = root.resolve()
    with _LOCK:
        if normalized in _ACTIVE_ROOTS:
            raise RuntimeError(f"MCP server already running for root: {normalized}")
        _ACTIVE_ROOTS.add(normalized)
    try:
        yield
    finally:
        with _LOCK:
            _ACTIVE_ROOTS.discard(normalized)
