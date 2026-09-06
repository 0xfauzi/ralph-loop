"""A ``RecursionError``'s provenance is settled by WHERE it is caught.

#323 asked for a predicate that separates "the operator nested a TOML
array 600 deep" from "kstrl recursed forever", on the grounds that the
exception object carries nothing that tells them apart. It carries
nothing, and no predicate is needed, because the two sites are in
series rather than in competition:

- ``config.load_toml_document`` catches ``Exception`` around the parse
  and re-raises ``ConfigError`` naming the file, so the document's
  ``RecursionError`` never travels further than that call.
- Every tomllib parse in ``kstrl/`` ends on a bare ``except
  Exception``, so no document's ``RecursionError`` escapes the parse
  that raised it: ``load_toml_document`` converts it to
  ``ConfigError``, and the pyproject.toml and ruff.toml readers in
  ``verify`` and ``feedforward`` swallow it. Not every guarded block
  goes through ``load_toml_document`` - ``init_wizard._detected_text``
  reaches ``verify._default_typecheck_command`` - which is why the
  closure is over the PARSES and not over the call graph.

These tests pin both directions in-process through
``preflight_config``, then direction (b) again at
:func:`config_problem_lines` and at the CLI seam, where an operator
sees the difference between a reported line and a traceback. Direction
(a) uses real bytes, and direction (b) a real self-calling function
rather than a constructed ``RecursionError``, because a stub would
pass in every broken state.

THE RESIDUAL, WHICH IS STATED RATHER THAN TESTED
------------------------------------------------
``load_toml_document``'s catch-all converts any ``Exception`` out of
the parse, so kstrl's OWN runaway that happens to bottom out inside a
parse is reported to the operator as their broken file. Measured on
CPython 3.12.8 at the default limit with a valid two-line kstrl.toml:
a caller holding 991 or 992 of the 1000 frames gets that relabelling,
and at 993 the ``RecursionError`` is raised before the guard and
escapes it. A caller that deep is itself the runaway, so the residual
is one runaway reported as a file rather than as a traceback, in a
two-frame band. No assertion here pins it: a test would have to key on
the interpreter's recursion limit and on this one function's frame
cost, and would move whenever either did.

WHAT IS ALREADY PINNED, AND WHAT IS NEW
---------------------------------------
``test_config_guard_survey.py`` is the closest prior art at the shared
traversal. Its
``test_the_entry_check_does_not_list_our_own_defect_as_a_config_problem``
monkeypatches ``EvolutionConfig.load`` to raise ``NotImplementedError``
- the other ``builtins`` ``RuntimeError`` subclass
``raise_if_defect``'s docstring names beside ``RecursionError`` - and
asserts it propagates through BOTH ``collect_config_problems`` and
:func:`config_problem_lines`.
Direction (a) lives at the loader in ``test_config_toml.py``
(``test_a_recursion_error_is_reported_not_raised``), one call below
:func:`preflight_config`, and at the seam as the ``deep_nest`` row of
``TOML_PARSE_FAULTS`` crossed with ``["status"]`` in
``test_config_preflight.py`` - which is why the seam test below covers
only direction (b) rather than restating that cell. Direction (b)
lives at :func:`load_or_report` in ``test_tui_config_guard.py``
(``test_every_runtimeerror_kstrl_did_not_define_is_re_raised``) and
raises a CONSTRUCTED ``RecursionError``, so no stack is ever
exhausted.

Three things here are not in any of those: a REAL stack exhaustion, so
the frames are the runaway's and the re-raise has something to keep;
``warned == []``, which is the only assertion anywhere that the one
DEGRADING section must not turn a defect of ours into a warning line
(``test_config_preflight.py:test_a_clean_config_raises_nothing`` makes
the same assertion about a CLEAN config, which is a different claim);
and the CLI seam, where the operator gets the traceback rather than a
reported line.
"""

from __future__ import annotations

import tomllib
import traceback
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.config import ConfigError
from kstrl.config_preflight import config_problem_lines, preflight_config
from kstrl.evolution import EvolutionConfig
from tests.helpers.bad_toml import BROAD_FRAGMENT, DEEP_NEST_TOML

VALID_TOML = "[agent]\ntype = 'claude-code'\n"

#: Where the standard library's TOML parser lives, resolved rather than
#: spelled: ``_parser.py`` is CPython-private, so a rename or a split
#: would make a filename check go quiet instead of red.
_TOMLLIB_DIR = Path(tomllib.__file__).parent


def _runaway(root_dir: Path | None = None) -> None:
    """A loader that recurses forever. kstrl's defect, not the file's."""
    return _runaway(root_dir)


def _frame_names(exc: BaseException) -> list[str]:
    return [f.name for f in traceback.extract_tb(exc.__traceback__)]


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
        monkeypatch.setattr(EvolutionConfig, "load", _runaway)
        warned: list[str] = []
        with pytest.raises(RecursionError) as caught:
            preflight_config(tmp_path, warn=warned.append)
        frames = traceback.extract_tb(caught.value.__traceback__)
        assert "_runaway" in [f.name for f in frames]
        # Re-RAISED by raise_if_defect, not merely never caught by
        # anything. Narrowing REJECTIONS to (ValueError, TypeError)
        # makes raise_if_defect unreachable for this exception; the
        # RecursionError still comes out of preflight_config, so every
        # other assertion here holds. Measured before this line and the
        # two like it existed: that mutation left the file 3 of 3 green.
        assert "raise_if_defect" in [f.name for f in frames]
        # A DISCLOSURE, not a control. This fixture writes VALID_TOML,
        # so tomllib is never entered and no production mutation can
        # turn this line red. It records the direction the frames point
        # in; the kills are the two assertions above.
        assert not [f for f in frames if Path(f.filename).is_relative_to(_TOMLLIB_DIR)]
        # [evolution] is the one degrading section. Its degrade path must
        # NOT swallow this: a warning line here would hide the cycle.
        assert warned == []

    def test_the_third_reporting_surface_re_raises_ours_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``config_problem_lines`` has a ``raise_if_defect`` of its own.

        It catches SURFACE_REJECTIONS around the traversal and folds an
        unparseable document back in as one line. That catch sees this
        ``RecursionError`` a second time, so deleting only ITS
        ``raise_if_defect`` turns the runaway into a returned string
        while the test above stays green.
        """
        (tmp_path / "kstrl.toml").write_text(VALID_TOML, encoding="utf-8")
        monkeypatch.setattr(EvolutionConfig, "load", _runaway)
        with pytest.raises(RecursionError) as caught:
            config_problem_lines(tmp_path, warn=lambda m: None)
        assert "config_problem_lines" in _frame_names(caught.value)
        assert "raise_if_defect" in _frame_names(caught.value)

    def test_the_seam_re_raises_ours_instead_of_reporting_a_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "kstrl.toml").write_text(VALID_TOML, encoding="utf-8")
        monkeypatch.setattr(EvolutionConfig, "load", _runaway)
        result = CliRunner().invoke(cli, ["status"])
        assert isinstance(result.exception, RecursionError)
        assert "raise_if_defect" in _frame_names(result.exception)
        # Not reported as the operator's file, in either register: a
        # kstrl.toml that parses cannot be what an "error:" would name.
        assert "error:" not in result.output
        assert "warning:" not in result.output
