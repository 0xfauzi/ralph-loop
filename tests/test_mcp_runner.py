"""Tests for MCP runner configuration defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_py.mcp import runner, schema


def _write_ralph_files(root: Path) -> None:
    ralph_dir = root / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("prompt\n")
    (ralph_dir / "understand_prompt.md").write_text("understand\n")
    (ralph_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}\n')
    (ralph_dir / "codebase_map.md").write_text("# Codebase Map\n")


class TestMcpRunnerDefaults:
    """Tests for MCP runner defaults."""

    def test_understand_defaults_match_cli(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_ralph_files(tmp_path)
        for env_key in ("PROMPT_FILE", "ALLOWED_PATHS", "RALPH_BRANCH"):
            monkeypatch.delenv(env_key, raising=False)

        inputs = schema.UnderstandInputs(root=str(tmp_path))
        config = runner._build_config(
            tmp_path,
            inputs,
            default_prompt=tmp_path / "scripts" / "ralph" / "understand_prompt.md",
            default_prd=tmp_path / "scripts" / "ralph" / "prd.json",
        )

        runner._apply_understand_defaults(config, inputs, tmp_path)

        assert config.prompt_file == tmp_path / "scripts" / "ralph" / "understand_prompt.md"
        assert config.allowed_paths == ["scripts/ralph/codebase_map.md"]
        assert config.ralph_branch == "ralph/understanding"
        assert config.ralph_branch_explicit is False

    def test_interactive_env_is_preserved_when_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_ralph_files(tmp_path)
        monkeypatch.setenv("INTERACTIVE", "1")

        inputs = schema.RunInputs(root=str(tmp_path))
        config = runner._build_config(
            tmp_path,
            inputs,
            default_prompt=tmp_path / "scripts" / "ralph" / "prompt.md",
            default_prd=tmp_path / "scripts" / "ralph" / "prd.json",
        )

        assert config.interactive is True

        inputs_override = schema.RunInputs(root=str(tmp_path), interactive=False)
        config_override = runner._build_config(
            tmp_path,
            inputs_override,
            default_prompt=tmp_path / "scripts" / "ralph" / "prompt.md",
            default_prd=tmp_path / "scripts" / "ralph" / "prd.json",
        )

        assert config_override.interactive is False
