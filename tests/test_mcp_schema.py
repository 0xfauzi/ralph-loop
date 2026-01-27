"""Tests for MCP schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_py.mcp import schema


class TestSchemaValidation:
    """Tests for MCP schema validation helpers."""

    def test_rejects_relative_root(self) -> None:
        inputs = schema.RunInputs(root="relative/path")
        with pytest.raises(schema.InvalidArgumentError) as exc:
            schema.validate_run_inputs(inputs)

        payload = exc.value.to_payload()
        assert payload["code"] == "invalid_argument"
        assert any(detail["field"] == "root" for detail in payload["details"])

    def test_rejects_missing_root(self, tmp_path: Path) -> None:
        missing_root = tmp_path / "missing"
        inputs = schema.RunInputs(root=str(missing_root))

        with pytest.raises(schema.InvalidArgumentError) as exc:
            schema.validate_run_inputs(inputs)

        payload = exc.value.to_payload()
        assert any(detail["field"] == "root" for detail in payload["details"])

    def test_rejects_root_file(self, tmp_path: Path) -> None:
        root_file = tmp_path / "file.txt"
        root_file.write_text("not a dir")
        inputs = schema.RunInputs(root=str(root_file))

        with pytest.raises(schema.InvalidArgumentError) as exc:
            schema.validate_run_inputs(inputs)

        payload = exc.value.to_payload()
        assert any(detail["field"] == "root" for detail in payload["details"])

    def test_defaults_allow_edits_false(self, tmp_path: Path) -> None:
        inputs = schema.RunInputs(root=str(tmp_path))
        assert inputs.allow_edits is False

    def test_rejects_negative_values(self, tmp_path: Path) -> None:
        inputs = schema.RunInputs(
            root=str(tmp_path),
            max_iterations=-1,
            sleep_seconds=-0.5,
            ai_prompt_progress_every=-10,
        )
        with pytest.raises(schema.InvalidArgumentError) as exc:
            schema.validate_run_inputs(inputs)

        payload = exc.value.to_payload()
        fields = {detail["field"] for detail in payload["details"]}
        assert "max_iterations" in fields
        assert "sleep_seconds" in fields
        assert "ai_prompt_progress_every" in fields

    def test_rejects_absolute_allowed_paths(self, tmp_path: Path) -> None:
        absolute_path = str(tmp_path / "abs.txt")
        inputs = schema.RunInputs(
            root=str(tmp_path),
            allowed_paths=[absolute_path],
        )

        with pytest.raises(schema.InvalidArgumentError) as exc:
            schema.validate_run_inputs(inputs)

        payload = exc.value.to_payload()
        assert any(detail["field"] == "allowed_paths" for detail in payload["details"])

    def test_rejects_prompt_outside_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        prompt_path = tmp_path / "outside" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("prompt")

        inputs = schema.RunInputs(
            root=str(root),
            prompt_file=str(prompt_path),
        )

        with pytest.raises(schema.InvalidArgumentError) as exc:
            schema.validate_run_inputs(inputs)

        payload = exc.value.to_payload()
        assert any(detail["field"] == "prompt_file" for detail in payload["details"])

    def test_rejects_prd_outside_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        prd_path = tmp_path / "outside" / "prd.json"
        prd_path.parent.mkdir()
        prd_path.write_text("{}")

        inputs = schema.RunInputs(
            root=str(root),
            prd_file=str(prd_path),
        )

        with pytest.raises(schema.InvalidArgumentError) as exc:
            schema.validate_run_inputs(inputs)

        payload = exc.value.to_payload()
        assert any(detail["field"] == "prd_file" for detail in payload["details"])
