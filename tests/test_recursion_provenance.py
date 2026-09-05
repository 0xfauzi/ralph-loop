"""A ``RecursionError``'s provenance is settled by WHERE it is caught.

#323 asked for a predicate that separates "the operator nested a TOML
array 600 deep" from "kstrl recursed forever", on the grounds that the
exception object carries nothing that tells them apart. It carries
nothing, and no predicate is needed, because the two sites are in
series rather than in competition:

- ``config.load_toml_document`` catches ``Exception`` around the parse
  and re-raises ``ConfigError`` naming the file, so the document's
  ``RecursionError`` never travels further than that call.
- Every block whose exception reaches ``config_preflight.raise_if_defect``
  reaches tomllib only through that function. So a ``RecursionError``
  that arrives there is kstrl's own by construction, and re-raising it
  with its traceback intact is right.

These tests pin both directions at two levels: in-process through
``preflight_config``, and at the CLI seam where an operator actually
sees the difference between a reported line and a traceback. Direction
(a) uses real bytes, and direction (b) a real self-calling function
rather than a constructed ``RecursionError``, because a stub would pass
in every broken state.

What was already pinned, and why it is not enough. Direction (a) lives
at the loader in ``test_config_toml.py``
(``test_a_recursion_error_is_reported_not_raised``), one call below
:func:`preflight_config`. Direction (b) lives at
:func:`load_or_report` in ``test_tui_config_guard.py``
(``test_every_runtimeerror_kstrl_did_not_define_is_re_raised``) and
raises a CONSTRUCTED ``RecursionError``, so no stack is ever exhausted.
Neither goes through ``collect_config_problems``, which is the
traversal all three reporting surfaces share, and neither is a pair:
the two halves are what make this a provenance claim rather than two
unrelated handler checks.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.config import ConfigError
from kstrl.config_preflight import preflight_config
from kstrl.evolution import EvolutionConfig
from tests.helpers.bad_toml import BROAD_FRAGMENT, DEEP_NEST_TOML

VALID_TOML = "[agent]\nname = 'x'\n"


def _runaway(root_dir: Path | None = None) -> None:
    """A loader that recurses forever. kstrl's defect, not the file's."""
    return _runaway(root_dir)


def _tb_names(exc: BaseException) -> tuple[list[str], list[str]]:
    frames = traceback.extract_tb(exc.__traceback__)
    return [f.name for f in frames], [Path(f.filename).name for f in frames]


class TestRecursionErrorProvenance:
    def test_a_parse_recursion_is_the_files_fault_and_names_the_file(self, tmp_path: Path) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_bytes(DEEP_NEST_TOML)
        with pytest.raises(ConfigError) as caught:
            preflight_config(tmp_path, warn=lambda m: None)
        assert str(toml) in str(caught.value)
        assert BROAD_FRAGMENT in str(caught.value)
        # The cause is kept, so the boundary is visible in the report
        # rather than inferred from the message alone.
        assert isinstance(caught.value.__cause__, RecursionError)

    def test_a_kstrl_recursion_is_ours_and_keeps_its_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(VALID_TOML, encoding="utf-8")
        monkeypatch.setattr(EvolutionConfig, "load", staticmethod(_runaway))
        warned: list[str] = []
        with pytest.raises(RecursionError) as caught:
            preflight_config(tmp_path, warn=warned.append)
        funcs, files = _tb_names(caught.value)
        assert "_runaway" in funcs
        assert "_parser.py" not in files
        # [evolution] is the one degrading section. Its degrade path must
        # NOT swallow this: a warning line here would hide the cycle.
        assert warned == []

    def test_the_seam_reports_the_file_and_re_raises_ours(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        toml = tmp_path / "kstrl.toml"
        toml.write_bytes(DEEP_NEST_TOML)
        result = CliRunner().invoke(cli, ["status"], env={"KSTRL_NO_TUI": "1"})
        assert not isinstance(result.exception, RecursionError)
        assert result.exit_code == 1
        assert "error:" in result.output and BROAD_FRAGMENT in result.output

        toml.write_text(VALID_TOML, encoding="utf-8")
        monkeypatch.setattr(EvolutionConfig, "load", staticmethod(_runaway))
        result = CliRunner().invoke(cli, ["status"], env={"KSTRL_NO_TUI": "1"})
        assert isinstance(result.exception, RecursionError)
        assert "error:" not in result.output
        assert "warning:" not in result.output
