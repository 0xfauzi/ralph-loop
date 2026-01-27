"""Tests for MCP transport root locking."""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_py.mcp.transport import locks


class TestRootLocks:
    """Tests for per-root locking behavior."""

    def test_lock_blocks_same_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()

        with locks.acquire_root_lock(root):
            with pytest.raises(RuntimeError):
                with locks.acquire_root_lock(root):
                    pass

        with locks.acquire_root_lock(root):
            pass

    def test_lock_allows_distinct_roots(self, tmp_path: Path) -> None:
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root_a.mkdir()
        root_b.mkdir()

        with locks.acquire_root_lock(root_a):
            with locks.acquire_root_lock(root_b):
                pass
