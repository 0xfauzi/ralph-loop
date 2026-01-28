"""Tests for CLI module."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ralph_py.cli import cli


class TestCliHelp:
    """Tests for CLI help commands."""

    def test_main_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Ralph" in result.output
        assert "run" in result.output
        assert "init" in result.output
        assert "understand" in result.output
        assert "feature" in result.output

    def test_run_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "MAX_ITERATIONS" in result.output
        assert "--agent-cmd" in result.output
        assert "--model" in result.output

    def test_init_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--help"])
        assert result.exit_code == 0
        assert "DIRECTORY" in result.output

    def test_understand_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["understand", "--help"])
        assert result.exit_code == 0
        assert "read-only" in result.output.lower()

    def test_feature_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["feature", "--help"])
        assert result.exit_code == 0
        assert "implementation" in result.output.lower()

    def test_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output


class TestCliValidation:
    """Tests for CLI argument validation."""

    def test_run_invalid_max_iterations(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "invalid"])
        assert result.exit_code == 2
        assert "not a valid integer" in result.output

    def test_run_missing_prompt_file(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["run", "1", "--agent-cmd", "echo test", "--branch", ""],
            )
            # Should fail because prompt file doesn't exist
            assert result.exit_code != 0

    def test_run_uses_prompt_env_for_root(self, tmp_path: Path, monkeypatch) -> None:
        project = tmp_path / "project"
        ralph_dir = project / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("test prompt")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            [
                "run",
                "1",
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
            ],
            env={
                "PROMPT_FILE": str(ralph_dir / "prompt.md"),
                "PRD_FILE": str(ralph_dir / "prd.json"),
            },
        )
        assert result.exit_code == 0

    def test_understand_uses_root_option(self, tmp_path: Path, monkeypatch) -> None:
        project = tmp_path / "project"
        ralph_dir = project / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "understand_prompt.md").write_text("test prompt")
        (ralph_dir / "codebase_map.md").write_text("# Map\n")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            [
                "understand",
                "1",
                "--root",
                str(project),
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
            ],
        )
        assert result.exit_code == 0

    def test_feature_uses_root_option(self, tmp_path: Path, monkeypatch) -> None:
        project = tmp_path / "project"
        ralph_dir = project / "scripts" / "ralph"
        feature_dir = ralph_dir / "feature" / "demo"
        feature_dir.mkdir(parents=True)
        (ralph_dir / "feature_understand_prompt.md").write_text("test prompt")
        (ralph_dir / "codebase_map.md").write_text("# Map\n")
        (feature_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            [
                "feature",
                "--root",
                str(project),
                "--prd",
                str(feature_dir / "prd.json"),
                "--understand-iterations",
                "1",
                "--implementation-auto-run",
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
            ],
        )
        assert result.exit_code == 0
        assert (feature_dir / "understand.md").exists()
