"""Tests for MCP resource resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ralph_py.mcp import resources, schema


class TestResourceResolution:
    """Tests for ralph:// resource resolution."""

    def test_read_resource_returns_contents(self, tmp_path: Path) -> None:
        root = tmp_path
        ralph_dir = root / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("hello\n")

        contents = resources.read_resource(root, "ralph://prompt")

        assert contents
        assert contents[0].content == "hello\n"

    def test_unknown_uri_rejected(self) -> None:
        with pytest.raises(schema.InvalidArgumentError):
            resources.get_resource_spec("ralph://unknown")

    def test_relative_root_rejected(self) -> None:
        with pytest.raises(schema.InvalidArgumentError):
            resources.resolve_resource_path(Path("relative"), "ralph://prompt")

    def test_symlink_traversal_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        ralph_dir = root / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)

        outside = tmp_path / "outside.txt"
        outside.write_text("outside")
        link_path = ralph_dir / "prompt.md"
        os.symlink(outside, link_path)

        with pytest.raises(schema.InvalidArgumentError):
            resources.resolve_resource_path(root, "ralph://prompt")
