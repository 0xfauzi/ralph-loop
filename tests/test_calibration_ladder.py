"""R10.11 (#232): the calibration-regression emitter on ``compare``.

``python -m kstrl.calibration compare`` gains ``--root`` and folds a
regression into the autonomy ladder: advisory always (a
``calibration_drift`` inbox item), demoting only behind
``[autonomy] demote_on_calibration_regression``. The applier it calls is
pinned in ``test_demotion_triggers.py``; the shared fixtures are in
``tests/helpers/demotion.py``.

No LLM anywhere: the baselines are built from synthetic per-run records
through ``calibration.build_report``, the same path a real capture uses.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from kstrl.autonomy import AutonomyLevel, AutonomyState
from kstrl.calibration import compare_baselines, load_baseline
from kstrl.calibration_ladder import LADDER_DISABLED_LINE
from kstrl.inbox import Inbox, ItemKind
from kstrl.statedir import ControlUnavailableError
from tests.helpers.bad_toml import TOML_PARSE_FAULTS
from tests.helpers.demotion import (
    ARCHITECT_MISSED_IN_OLD,
    EXPECTED_FAILURES,
    MISSED_IN_NEW,
    NEW_TS,
    OLD_TS,
    OTHER_MISSED_IN_NEW,
    OTHER_OLD_TS,
    baseline,
    baseline_pair,
    inbox_items,
    run_compare,
    without_timestamp,
    write_config,
)

# ---------------------------------------------------------------------------
# 3-7, 12: the calibration-regression emitter
# ---------------------------------------------------------------------------


class TestCompareLadder:
    def test_compare_regression_opens_inbox_item(self, tmp_path: Path) -> None:
        write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        code = run_compare(tmp_path, old, new)

        assert code == 1
        drift = inbox_items(tmp_path, ItemKind.CALIBRATION_DRIFT)
        assert len(drift) == 1
        assert drift[0].title == f"Calibration regression: {EXPECTED_FAILURES} failing threshold(s)"
        assert drift[0].dedupe_key == f"calibration:{OLD_TS}:{NEW_TS}"
        assert len(drift[0].evidence["failures"]) == EXPECTED_FAILURES
        assert drift[0].evidence["comparison_id"] == f"{OLD_TS}:{NEW_TS}"
        assert drift[0].evidence["new_baseline"] == NEW_TS
        # Advisory tier: the level does not move without the switch.
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)
        assert inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE) == []

    def test_compare_regression_demotes_when_enabled(self, tmp_path: Path) -> None:
        write_config(tmp_path, demote_on_calibration=True)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        code = run_compare(tmp_path, old, new)

        assert code == 1
        state = AutonomyState.load(tmp_path)
        assert state.level == int(AutonomyLevel.L1_SUPERVISED)
        assert state.history[-1].trigger == "calibration_regression"
        assert state.history[-1].evidence["comparison_id"] == f"{OLD_TS}:{NEW_TS}"
        kinds = {item.kind for item in inbox_items(tmp_path)}
        assert kinds == {ItemKind.CALIBRATION_DRIFT, ItemKind.DEMOTION_NOTICE}

    def test_compare_regression_dedupes_inbox_item(self, tmp_path: Path) -> None:
        write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        for _ in range(2):
            assert run_compare(tmp_path, old, new) == 1

        drift = inbox_items(tmp_path, ItemKind.CALIBRATION_DRIFT)
        assert len(drift) == 1
        assert drift[0].occurrences == 2

    def test_compare_demotes_once_for_the_same_comparison(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Re-reading one measurement must not cost a second level.

        The compare command is human-invoked and gets re-run. Deduping on
        the pair of baselines makes the level a function of what was
        measured, not of how often it was looked at.
        """
        write_config(tmp_path, demote_on_calibration=True)
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        for _ in range(2):
            assert run_compare(tmp_path, old, new) == 1
        out = capsys.readouterr().out

        state = AutonomyState.load(tmp_path)
        assert state.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert len([t for t in state.history if t.direction == "demote"]) == 1
        assert len(inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE)) == 1
        assert f"comparison {OLD_TS}:{NEW_TS} already cost a level" in out

    def test_compare_no_regression_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_config(tmp_path, demote_on_calibration=True)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=frozenset())

        code = run_compare(tmp_path, old, new)

        assert code == 0
        assert capsys.readouterr().out.count("FAIL") == 0
        assert inbox_items(tmp_path) == []
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_compare_autonomy_disabled_writes_nothing_and_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        code = run_compare(tmp_path, old, new)

        assert code == 1
        out = capsys.readouterr().out
        # A mistyped --root lands on a directory with no kstrl.toml,
        # which loads as "disabled": the line has to say which root it
        # consulted or it reports the ladder off when it is on. Asserted
        # against THAT line, not against the whole of stdout: the
        # baseline paths printed above it are under tmp_path too, so
        # `str(tmp_path) in out` passes with the root dropped.
        disabled = [line for line in out.splitlines() if LADDER_DISABLED_LINE in line]
        assert len(disabled) == 1, out
        assert str(tmp_path) in disabled[0]
        assert inbox_items(tmp_path) == []
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    @pytest.mark.parametrize("body,fragment", TOML_PARSE_FAULTS)
    def test_compare_unloadable_config_exits_2(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        body: bytes,
        fragment: str,
    ) -> None:
        """A broken config is a refusal, never a silent "ladder is off".

        Parametrized on the shared #318 table rather than one hand-rolled
        syntax error: ``report_to_ladder`` is a new config-reading seam,
        and the three faults that each escaped a shipped handler (not
        utf-8, a 4301-digit integer, 496 nested arrays) are exactly the
        ones a single malformed-header fixture would never have reached.
        """
        (tmp_path / "kstrl.toml").write_bytes(body)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        code = run_compare(tmp_path, old, new)

        assert code == 2
        err = capsys.readouterr().err
        assert err.startswith("error:")
        assert fragment in err
        assert inbox_items(tmp_path) == []

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads a 000 file"
    )
    def test_compare_unreadable_config_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """UNREADABLE, not just unparseable.

        ``load_toml_section`` normalises a document fault to
        ``ConfigError`` and deliberately does NOT normalise ``OSError``,
        so a guard written as ``except ValueError`` handles a 4301-digit
        integer and lets a permissions mistake out as a traceback. The
        permissions mistake is the commoner one.
        """
        config = tmp_path / "kstrl.toml"
        config.write_text("[autonomy]\nenabled = true\n", encoding="utf-8")
        config.chmod(0o000)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)
        try:
            code = run_compare(tmp_path, old, new)
        finally:
            config.chmod(0o644)

        assert code == 2
        assert capsys.readouterr().err.startswith("error:")
        assert inbox_items(tmp_path) == []

    def test_compare_non_boolean_max_level_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A per-key cast raises TypeError, which is not a ValueError.

        ``max_level = 1979-05-27`` is a valid TOML date, so the document
        parses and ``int(section["max_level"])`` raises ``TypeError``.
        The shared fault table only covers DOCUMENT-level faults.
        """
        (tmp_path / "kstrl.toml").write_text(
            "[autonomy]\nenabled = true\nmax_level = 1979-05-27\n", encoding="utf-8"
        )
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        assert run_compare(tmp_path, old, new) == 2
        assert capsys.readouterr().err.startswith("error:")

    def test_compare_quoted_boolean_switch_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``= "false"`` must not ARM the switch that revokes a level."""
        (tmp_path / "kstrl.toml").write_text(
            '[autonomy]\nenabled = true\ndemote_on_calibration_regression = "false"\n',
            encoding="utf-8",
        )
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        assert run_compare(tmp_path, old, new) == 2
        assert "demote_on_calibration_regression must be a boolean" in capsys.readouterr().err
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L3_ENVELOPED_AUTO)

    def test_compare_pass_with_unloadable_config_exits_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A green comparison never consults the ladder, so never refuses.

        Exit 2 exists so a broken config cannot read as "the ladder is
        off". On a PASSING comparison the ladder is not consulted at all,
        so there is nothing to be mis-read, and refusing there would turn
        every green CI run in a repo with a typo into a failure.
        """
        (tmp_path / "kstrl.toml").write_bytes(b"[autonomy\nenabled = true\n")
        old, new = baseline_pair(tmp_path, missed=frozenset())

        assert run_compare(tmp_path, old, new) == 0
        assert capsys.readouterr().err == ""
        assert inbox_items(tmp_path) == []

    def test_compare_drift_item_honours_inbox_disabled(self, tmp_path: Path) -> None:
        """A second emitter must not re-enable an inbox that is off."""
        write_config(tmp_path, inbox=False)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        assert run_compare(tmp_path, old, new) == 1
        assert inbox_items(tmp_path) == []

    def test_two_comparisons_sharing_a_new_baseline_stay_distinct(self, tmp_path: Path) -> None:
        """A comparison is a PAIR, so the identity has to be the pair.

        Keyed on the new baseline alone, ``compare old_A new`` and
        ``compare old_B new`` fold onto one item, which then carries the
        title of the first and the detail of the second.
        """
        write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old_a = baseline(tmp_path, missed=frozenset(), timestamp=OLD_TS)
        old_b = baseline(tmp_path, missed=OTHER_MISSED_IN_NEW, timestamp=OTHER_OLD_TS)
        new = baseline(tmp_path, missed=MISSED_IN_NEW, timestamp=NEW_TS)

        assert run_compare(tmp_path, old_a, new) == 1
        assert run_compare(tmp_path, old_b, new) == 1

        drift = inbox_items(tmp_path, ItemKind.CALIBRATION_DRIFT)
        assert len(drift) == 2
        assert {item.dedupe_key for item in drift} == {
            f"calibration:{OLD_TS}:{NEW_TS}",
            f"calibration:{OTHER_OLD_TS}:{NEW_TS}",
        }
        assert [item.occurrences for item in drift] == [1, 1]

    def test_timestampless_baselines_do_not_share_an_identity(self, tmp_path: Path) -> None:
        """``UNKNOWN_TIMESTAMP`` is a fill-in, not an identity.

        ``load_baseline`` fills a missing ``timestamp`` key with one
        literal, so every such baseline had the same dedupe key: the
        first sentinel regression to demote was the last one that ever
        could, because the next deduped against it. The key falls back to
        a digest of the regression instead.
        """
        write_config(tmp_path, demote_on_calibration=True)
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        old = baseline(tmp_path, missed=frozenset(), timestamp=OLD_TS)
        new_a = without_timestamp(baseline(tmp_path, missed=MISSED_IN_NEW, timestamp=NEW_TS))
        new_b = without_timestamp(
            baseline(tmp_path, missed=OTHER_MISSED_IN_NEW, timestamp=OTHER_OLD_TS)
        )

        assert run_compare(tmp_path, old, new_a) == 1
        assert run_compare(tmp_path, old, new_b) == 1

        state = AutonomyState.load(tmp_path)
        assert state.level == int(AutonomyLevel.L1_SUPERVISED)
        assert len([t for t in state.history if t.direction == "demote"]) == 2
        drift = inbox_items(tmp_path, ItemKind.CALIBRATION_DRIFT)
        assert len(drift) == 2
        assert all(item.dedupe_key.startswith("calibration:sha256-") for item in drift)

    def test_timestampless_olds_with_different_rates_stay_distinct(self, tmp_path: Path) -> None:
        """The digest is the identity, so it has to carry what was measured.

        A FLOOR failure quotes only the new rate ("role 'security'
        detection rate 0.60 is below its floor 0.80"), so two different
        old baselines whose difference sits in a role that does not fail
        produce byte-identical failure lines. Digesting the failures
        alone collapsed those two comparisons onto one key: the second
        could not demote, and the item's evidence described comparison B
        while the durable transition record described comparison A.
        """
        write_config(tmp_path, demote_on_calibration=True)
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        old_a = without_timestamp(baseline(tmp_path, missed=frozenset(), timestamp=OLD_TS))
        old_b = without_timestamp(
            baseline(tmp_path, missed=ARCHITECT_MISSED_IN_OLD, timestamp=OTHER_OLD_TS)
        )
        new = baseline(tmp_path, missed=MISSED_IN_NEW, timestamp=NEW_TS)
        # The premise, measured rather than assumed: the two comparisons
        # fail identically and differ only in the rate table.
        loaded_new = load_baseline(new)
        failures_a = compare_baselines(load_baseline(old_a), loaded_new).failures
        failures_b = compare_baselines(load_baseline(old_b), loaded_new).failures
        assert failures_a == failures_b

        assert run_compare(tmp_path, old_a, new) == 1
        assert run_compare(tmp_path, old_b, new) == 1

        state = AutonomyState.load(tmp_path)
        assert len([t for t in state.history if t.direction == "demote"]) == 2
        assert state.level == int(AutonomyLevel.L1_SUPERVISED)
        drift = inbox_items(tmp_path, ItemKind.CALIBRATION_DRIFT)
        assert len(drift) == 2
        assert len({item.dedupe_key for item in drift}) == 2

    def test_drift_item_survives_an_unreadable_inbox_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``InboxConfig.load`` casts per key, so a TOML date raises TypeError.

        ``report_to_ladder`` catches ``TypeError`` twenty-five lines
        above for exactly this reason, on the ladder's own config. The
        inbox guard below it did not, so a valid document with one wrong
        VALUE came out of a measurement command as a raw traceback.
        """
        (tmp_path / "kstrl.toml").write_text(
            "[autonomy]\nenabled = true\n\n[inbox]\nsnooze_hours = 1979-05-27\n",
            encoding="utf-8",
        )
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        assert run_compare(tmp_path, old, new) == 1
        assert "inbox write failed" in capsys.readouterr().err
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_reversed_arguments_do_not_consult_the_ladder(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``compare <new> <old>`` reads a recovery as a regression.

        Every recovered fixture reads as newly missed, which was harmless
        while the command only printed. It costs a level now, so the one
        case where the order is demonstrably wrong is refused and said.
        """
        write_config(tmp_path, demote_on_calibration=True)
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        # A real recovery: the older capture missed two security
        # fixtures and the newer one catches them again.
        worse = baseline(tmp_path, missed=MISSED_IN_NEW, timestamp=OLD_TS)
        better = baseline(tmp_path, missed=frozenset(), timestamp=NEW_TS)
        assert run_compare(tmp_path, worse, better) == 0
        capsys.readouterr()

        assert run_compare(tmp_path, better, worse) == 1

        assert "arguments look reversed" in capsys.readouterr().out
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L3_ENVELOPED_AUTO)
        assert inbox_items(tmp_path) == []

    def test_inbox_control_failure_warns_and_keeps_the_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``ControlUnavailableError`` is a RuntimeError, not an OSError.

        ``Inbox._append`` takes the control lock on every write, so this
        is the likely inbox failure rather than an exotic one, and it
        escaped the ``(OSError, ValueError)`` pair all four inbox sites
        were written with.
        """

        def boom(*args: Any, **kwargs: Any) -> None:
            raise ControlUnavailableError("control state directory unavailable (/x): boom")

        monkeypatch.setattr(Inbox, "add", boom)
        write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)

        assert run_compare(tmp_path, old, new) == 1
        assert "inbox write failed" in capsys.readouterr().err

    def test_demotion_control_failure_warns_and_keeps_the_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The factory absorbs these in an outer except; compare has none."""

        def boom(*args: Any, **kwargs: Any) -> None:
            raise ControlUnavailableError("control state directory unavailable (/x): boom")

        write_config(tmp_path, demote_on_calibration=True)
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        old, new = baseline_pair(tmp_path, missed=MISSED_IN_NEW)
        monkeypatch.setattr(AutonomyState, "save", boom)

        assert run_compare(tmp_path, old, new) == 1
        assert "autonomy demotion failed" in capsys.readouterr().err
