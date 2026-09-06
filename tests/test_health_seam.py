"""R10.11 (#232): the R8.4 health seam, inert until #151 lands.

``kstrl/health.py`` does not exist yet, so what ships here is the
meeting point: the import guard, a ``health_breach`` inbox kind and
``[autonomy] demote_on_health_breach``. The seam is shown to fire
against a monkeypatched module rather than assumed to, and the guard's
narrowing - which is what keeps a broken ``kstrl.health`` loud instead
of reading as "#151 has not landed" - is driven through the real import
machinery. The applier it calls is pinned in
``test_demotion_triggers.py``; the shared fixtures are in
``tests/helpers/demotion.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from kstrl.autonomy import AutonomyLevel, AutonomyState
from kstrl.inbox import Inbox, ItemKind, Priority
from kstrl.statedir import CONTROL_AUTONOMY, ControlUnavailableError, control_file
from tests.helpers.demotion import (
    BrokenHealthFinder,
    fake_health,
    inbox_items,
    make_breach,
    run_outcome,
    write_config,
)

# ---------------------------------------------------------------------------
# 8-11, 13: the R8.4 health seam
# ---------------------------------------------------------------------------


class TestHealthSeam:
    def test_health_seam_inert_without_module(self, tmp_path: Path) -> None:
        """Until #151 lands there is no kstrl.health, and nothing fires."""
        assert "kstrl.health" not in sys.modules
        assert importlib.util.find_spec("kstrl.health") is None
        write_config(tmp_path, demote_on_health=True)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        run_outcome(tmp_path)

        assert inbox_items(tmp_path) == []
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_health_module_present_but_missing_function_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A renamed contract must fail loud, not disarm the seam.

        The guard swallows exactly one thing: kstrl.health not existing.
        A module that exists and does not export ``health_breaches``
        raises AttributeError, which the factory's caller reports as
        "Autonomy state update failed". A bare ``except ImportError``
        would have read the rename as "#151 has not landed yet".
        """
        monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(with_function=False))
        write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        with pytest.raises(AttributeError):
            run_outcome(tmp_path)

    def test_health_module_broken_by_its_own_dependency_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The OTHER ``.name``, which is what the narrowing is for.

        #151 will land a ``kstrl/health.py``; the operator who has not
        installed what it imports gets a ``ModuleNotFoundError`` naming
        THAT dependency. The guard swallows only ``kstrl.health`` itself
        being absent, so this one reaches the caller's "Autonomy state
        update failed" warning instead of reading as "#151 has not landed
        yet" and disarming the seam for good.

        This is the case the guard exists for and the one nothing pinned:
        with the module merely renamed, the guard passes under any except
        clause on the import, including ``except ImportError`` and a
        clause with no ``.name`` check at all.
        """
        assert "kstrl.health" not in sys.modules
        monkeypatch.setattr(sys, "meta_path", [BrokenHealthFinder(), *sys.meta_path])
        write_config(tmp_path, demote_on_health=True)
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)

        with pytest.raises(ModuleNotFoundError) as excinfo:
            run_outcome(tmp_path)

        assert excinfo.value.name == "scipy"
        assert inbox_items(tmp_path) == []
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L3_ENVELOPED_AUTO)

    def test_health_breach_opens_inbox_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(make_breach()))
        write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        run_outcome(tmp_path)

        breaches = inbox_items(tmp_path, ItemKind.HEALTH_BREACH)
        assert len(breaches) == 1
        assert breaches[0].title == "Health breach: retry_rate WE1: 1 point beyond 3 sigma"
        assert breaches[0].dedupe_key == "health:retry_rate:WE1: 1 point beyond 3 sigma"
        assert breaches[0].priority is Priority.NORMAL
        assert breaches[0].evidence["metric"] == "retry_rate"
        assert breaches[0].evidence["window_runs"] == 8
        # Advisory by default: a breach alone revokes nothing.
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)
        assert inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE) == []

    def test_health_breach_demotes_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exactly ONE level, from L3 so the floor cannot hide a second.

        Starting at L2 and asserting L1 passes whether the trigger
        revokes one level or every level it can reach.
        """
        monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(make_breach()))
        write_config(tmp_path, demote_on_health=True)
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        run_outcome(tmp_path)

        state = AutonomyState.load(tmp_path)
        assert state.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert len([t for t in state.history if t.direction == "demote"]) == 1
        assert state.history[-1].trigger == "health_breach"
        # N9: the durable record carries every field the inbox item does.
        assert state.history[-1].evidence["breaches"][0]["window_runs"] == 8
        assert inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE)

    def test_health_breach_suppressed_during_cooldown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cooling-down ladder records the breach and holds the level.

        A breach is a windowed trend, so it persists across runs. Without
        this gate one breach costs a level per run all the way to L1
        before the operator has read the first notice.
        """
        monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(make_breach()))
        write_config(tmp_path, demote_on_health=True)
        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        state.cooldown_runs_remaining = 3
        state.save(tmp_path)
        warned = run_outcome(tmp_path)

        assert inbox_items(tmp_path, ItemKind.HEALTH_BREACH)
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L3_ENVELOPED_AUTO)
        assert inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE) == []
        # Announced, not silent: a windowed trend can hold for the whole
        # cool-down, and a level that does not move for ten runs with no
        # line explaining why is the failure this ladder exists to show.
        assert "not demoting during cool-down" in warned
        # Two, not the three seeded: record_decisive_run() burns the
        # cool-down down before the health check in the same function, so
        # a decisive run that takes it from 1 to 0 CAN demote on a breach
        # in that same run. The cool-down has ended by then, which is the
        # intended edge and worth pinning rather than discovering.
        assert "2 decisive run(s) remaining" in warned

    def test_cooldown_of_one_ends_in_this_run_and_the_breach_demotes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lower edge of the gate, which the comment claimed and nothing pinned.

        ``record_decisive_run()`` burns the cool-down down before the
        health check runs in the same function, so a decisive run that
        takes it from 1 to 0 CAN demote on a breach in that same run.
        Seeded at 3 and at 0, the two existing cases straddle this
        without touching it.
        """
        monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(make_breach()))
        write_config(tmp_path, demote_on_health=True)
        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        state.cooldown_runs_remaining = 1
        state.save(tmp_path)
        run_outcome(tmp_path)

        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)
        assert inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE)

    def test_cooldown_of_two_suppresses_with_one_run_remaining(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The upper edge, and the case that discriminates.

        Seeded at 2 the cool-down burns to 1, which is still running, so
        the breach must not demote. This is the assertion an off-by-one
        in the gate (``> 0`` written ``> 1``) fails: at the seeds the
        other cases use, 3 and 0, both spellings agree.
        """
        monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(make_breach()))
        write_config(tmp_path, demote_on_health=True)
        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        state.cooldown_runs_remaining = 2
        state.save(tmp_path)
        warned = run_outcome(tmp_path)

        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L3_ENVELOPED_AUTO)
        assert inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE) == []
        assert "1 decisive run(s) remaining" in warned

    def test_clean_run_does_not_overwrite_a_degraded_state(self, tmp_path: Path) -> None:
        """The path an ordinary run takes, which is the common one.

        A run with no policy violation saves the ladder state to record
        its evidence counters. Doing that over a file ``load`` already
        failed closed on replaces the only bytes an operator could have
        repaired with a fresh L1, and the next load then finds a clean
        file, so nothing reports the damage again.
        """
        write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        path = control_file(tmp_path, CONTROL_AUTONOMY)
        damaged = '{"level": 3, "history": [ truncated'
        path.write_text(damaged, encoding="utf-8")

        warned = run_outcome(tmp_path)

        assert path.read_text(encoding="utf-8") == damaged
        assert "ladder state is degraded" in warned
        assert "refusing to overwrite" in warned

    def test_health_items_survive_an_unreadable_inbox_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``InboxConfig.load`` casts per key, so a TOML date raises TypeError.

        ``int(datetime.date)`` is a ``TypeError``, not a ``ValueError``,
        so the guard that was widened for the ladder's own config still
        let this one out - as a traceback from the seam, on a path whose
        whole contract is that bookkeeping cannot fail a run.
        """
        monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(make_breach()))
        (tmp_path / "kstrl.toml").write_text(
            "[autonomy]\nenabled = true\n\n[inbox]\nopen_item_cap = 1979-05-27\n",
            encoding="utf-8",
        )
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)

        warned = run_outcome(tmp_path)

        assert "Inbox write failed (non-fatal)" in warned
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_health_items_honour_inbox_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third emitter, and the third one nothing covered."""
        monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(make_breach()))
        write_config(tmp_path, inbox=False)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        run_outcome(tmp_path)

        assert inbox_items(tmp_path) == []

    def test_repeat_breach_refreshes_the_recorded_numbers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trend's dedupe key holds still while its numbers move.

        ``Inbox.add`` refreshed ``detail`` and left ``evidence``, so the
        prose reported the latest observation and the structured half -
        the one ``ks inbox`` and the TUI render - reported the first.
        """
        monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(make_breach()))
        write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        run_outcome(tmp_path)

        monkeypatch.setitem(
            sys.modules, "kstrl.health", fake_health(make_breach(value=0.9, window_runs=20))
        )
        run_outcome(tmp_path, run_id="run-2")

        breaches = inbox_items(tmp_path, ItemKind.HEALTH_BREACH)
        assert len(breaches) == 1
        assert breaches[0].occurrences == 2
        assert breaches[0].evidence["value"] == 0.9
        assert breaches[0].evidence["window_runs"] == 20
        assert "value 0.9" in breaches[0].detail

    def test_one_failed_write_does_not_drop_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One guard per breach, not one around the loop.

        A failure on the second of three used to take the third with it,
        under a single warning that named none of them.
        """
        breaches = [make_breach(metric=f"m{i}") for i in range(3)]
        monkeypatch.setitem(sys.modules, "kstrl.health", fake_health(*breaches))
        real_add = Inbox.add

        def flaky(self: Inbox, kind: ItemKind, title: str, **kwargs: Any) -> Any:
            if "m1" in title:
                raise ControlUnavailableError("control state directory unavailable (/x): boom")
            return real_add(self, kind, title, **kwargs)

        monkeypatch.setattr(Inbox, "add", flaky)
        write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        warned = run_outcome(tmp_path)

        metrics = {
            item.evidence["metric"] for item in inbox_items(tmp_path, ItemKind.HEALTH_BREACH)
        }
        assert metrics == {"m0", "m2"}
        assert "Inbox write failed for m1" in warned
