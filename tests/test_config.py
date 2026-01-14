"""Tests for config module."""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_py.config import RalphConfig, _parse_bool, _parse_paths


class TestParseBool:
    """Tests for _parse_bool helper."""

    def test_none_returns_false(self) -> None:
        assert _parse_bool(None) is False

    def test_empty_string_returns_false(self) -> None:
        assert _parse_bool("") is False

    def test_one_returns_true(self) -> None:
        assert _parse_bool("1") is True

    def test_true_returns_true(self) -> None:
        assert _parse_bool("true") is True
        assert _parse_bool("TRUE") is True
        assert _parse_bool("True") is True

    def test_yes_returns_true(self) -> None:
        assert _parse_bool("yes") is True
        assert _parse_bool("YES") is True

    def test_other_values_return_false(self) -> None:
        assert _parse_bool("0") is False
        assert _parse_bool("false") is False
        assert _parse_bool("no") is False
        assert _parse_bool("random") is False


class TestParsePaths:
    """Tests for _parse_paths helper."""

    def test_none_returns_empty(self) -> None:
        assert _parse_paths(None) == []

    def test_empty_string_returns_empty(self) -> None:
        assert _parse_paths("") == []

    def test_single_path(self) -> None:
        assert _parse_paths("foo/bar.txt") == ["foo/bar.txt"]

    def test_multiple_paths(self) -> None:
        assert _parse_paths("foo/bar.txt,baz/qux.py") == ["foo/bar.txt", "baz/qux.py"]

    def test_trims_whitespace(self) -> None:
        assert _parse_paths("  foo/bar.txt , baz/qux.py  ") == ["foo/bar.txt", "baz/qux.py"]

    def test_skips_empty_entries(self) -> None:
        assert _parse_paths("foo,,bar") == ["foo", "bar"]


class TestRalphConfig:
    """Tests for RalphConfig."""

    def test_defaults(self) -> None:
        config = RalphConfig()
        assert config.max_iterations == 10
        assert config.sleep_seconds == 2.0
        assert config.interactive is False
        assert config.allowed_paths == []
        assert config.agent_cmd is None
        assert config.ui_mode == "auto"

    def test_from_env_basic(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MAX_ITERATIONS", "25")
        monkeypatch.setenv("SLEEP_SECONDS", "5")
        monkeypatch.setenv("INTERACTIVE", "1")

        config = RalphConfig.from_env(tmp_path)

        assert config.max_iterations == 25
        assert config.sleep_seconds == 5.0
        assert config.interactive is True

    def test_from_env_ralph_branch_explicit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # When RALPH_BRANCH is set (even if empty)
        monkeypatch.setenv("RALPH_BRANCH", "")
        config = RalphConfig.from_env(tmp_path)
        assert config.ralph_branch == ""
        assert config.ralph_branch_explicit is True

    def test_from_env_ralph_branch_not_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # When RALPH_BRANCH is not set at all
        monkeypatch.delenv("RALPH_BRANCH", raising=False)
        config = RalphConfig.from_env(tmp_path)
        assert config.ralph_branch is None
        assert config.ralph_branch_explicit is False

    def test_validate_negative_iterations(self, tmp_path: Path) -> None:
        config = RalphConfig(
            max_iterations=-1,
            prompt_file=tmp_path / "prompt.md",
        )
        errors = config.validate()
        assert any("non-negative" in e for e in errors)

    def test_validate_missing_prompt(self, tmp_path: Path) -> None:
        config = RalphConfig(
            prompt_file=tmp_path / "nonexistent.md",
        )
        errors = config.validate()
        assert any("not found" in e for e in errors)
