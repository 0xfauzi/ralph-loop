"""R10.11 (#232): the demotion triggers this PR wires, and the R8.4 seam.

``DemotionTrigger`` has declared five causes since R8.2 and exactly one
of them fired. Three things changed here, and each is pinned below:

- ``autonomy.apply_demotion``, one applier for every automatic demotion,
  so the four writes a demotion performs (state, evolution journal, run
  event stream, inbox notice) cannot drift apart per trigger. That the
  extraction changed nothing is proven by two PRE-EXISTING tests passing
  unmodified - ``test_autonomy_ladder.py::TestFactoryIntegration::
  test_policy_violation_demotes_and_journals`` and ``test_inbox.py::
  TestFactoryEmission::test_demotion_emits_notice_with_evidence``. The
  tests here pin the applier's own contract on top of that.
- the calibration-regression emitter on ``python -m kstrl.calibration
  compare``: advisory always, demoting only behind
  ``[autonomy] demote_on_calibration_regression``.
- the health seam, inert until ``kstrl/health.py`` exists (#151) and
  shown to fire against a monkeypatched module rather than assumed to.

No LLM anywhere: the baselines are built from synthetic per-run records
through ``calibration.build_report``, the same path a real capture uses.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from kstrl import calibration
from kstrl.autonomy import (
    DEMOTION_COOLDOWN_RUNS,
    AutonomyConfig,
    AutonomyLevel,
    AutonomyState,
    DemotionTrigger,
    apply_demotion,
)
from kstrl.calibration_ladder import LADDER_DISABLED_LINE
from kstrl.events import EventBus
from kstrl.factory import FactoryResult, _record_autonomy_outcome
from kstrl.inbox import Inbox, InboxConfig, ItemKind, Priority
from kstrl.manifest import Component, Manifest
from kstrl.ui.plain import PlainUI

OLD_TS = "20260901-000000"
NEW_TS = "20260905-000000"

#: (role, fixture id, category, cwe) for a full 12-fixture capture. The
#: shape matters, not the content: two security fixtures missed in the
#: new baseline put security at 0.60, under its 0.80 floor and a 0.40
#: drop, and take two categories from 1.00 to 0.00. Four failures.
_FIXTURES: tuple[tuple[str, str, str, str | None], ...] = (
    ("security", "sec-01-sqli", "injection", "CWE-89"),
    ("security", "sec-02-ssrf", "ssrf", "CWE-918"),
    ("security", "sec-03-auth", "auth", "CWE-287"),
    ("security", "sec-04-path", "path_traversal", "CWE-22"),
    ("security", "sec-05-secret", "secrets", "CWE-798"),
    ("reviewer", "rev-01-scope", "scope_creep", None),
    ("reviewer", "rev-02-tests", "test_quality", None),
    ("reviewer", "rev-03-error", "error_handling", None),
    ("architect", "spec-01-no-error-handling", "spec_issues", None),
    ("architect", "spec-02-ambiguous", "spec_issues", None),
    ("architect", "spec-03-contradiction", "spec_issues", None),
    ("architect_allowed_paths", "spec-04-paths", "allowed_paths", None),
)
_MISSED_IN_NEW = frozenset({"sec-04-path", "sec-05-secret"})
_EXPECTED_FAILURES = 4


def _records(missed: frozenset[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for role, fixture_id, category, cwe in _FIXTURES:
        for _ in range(3):
            out.append(
                {
                    "role": role,
                    "fixture_id": fixture_id,
                    "category": category,
                    "cwe": cwe,
                    "caught": fixture_id not in missed,
                    "error": False,
                    "detail": "synthetic",
                }
            )
    return out


def _baseline_pair(tmp_path: Path, *, missed: frozenset[str]) -> tuple[Path, Path]:
    """Write an old/new baseline pair; ``missed`` regresses the new one."""
    results = tmp_path / "baselines"
    old = calibration.save_report(
        calibration.build_report(
            _records(frozenset()), model="haiku", timestamp=OLD_TS, runs_per_fixture=3
        ),
        results,
    )
    new = calibration.save_report(
        calibration.build_report(
            _records(missed), model="haiku", timestamp=NEW_TS, runs_per_fixture=3
        ),
        results,
    )
    return old, new


def _write_config(
    tmp_path: Path,
    *,
    autonomy: bool = True,
    demote_on_calibration: bool | None = None,
    demote_on_health: bool | None = None,
) -> None:
    lines = ["[autonomy]", f"enabled = {'true' if autonomy else 'false'}"]
    if demote_on_calibration is not None:
        lines.append(
            f"demote_on_calibration_regression = {'true' if demote_on_calibration else 'false'}"
        )
    if demote_on_health is not None:
        lines.append(f"demote_on_health_breach = {'true' if demote_on_health else 'false'}")
    (tmp_path / "kstrl.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _items(tmp_path: Path, kind: ItemKind | None = None) -> list[Any]:
    items = Inbox(tmp_path, InboxConfig()).items()
    if kind is None:
        return list(items)
    return [item for item in items if item.kind is kind]


def _ui() -> tuple[PlainUI, io.StringIO]:
    buffer = io.StringIO()
    return PlainUI(no_color=True, file=buffer), buffer


def _manifest() -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                "comp-a",
                "Component A",
                "Desc",
                [],
                "scripts/kstrl/feature/comp-a/prd.json",
                "kstrl/factory/comp-a",
            )
        ],
    )


def _breach(
    metric: str = "retry_rate",
    rule: str = "WE1: 1 point beyond 3 sigma",
) -> SimpleNamespace:
    return SimpleNamespace(metric=metric, rule=rule, value=0.4, limit=0.3, window_runs=8)


def _fake_health(*breaches: object, with_function: bool = True) -> ModuleType:
    module = ModuleType("kstrl.health")
    if with_function:
        module.health_breaches = lambda root_dir: list(breaches)  # type: ignore[attr-defined]
    return module


def _run_outcome(tmp_path: Path, ui: PlainUI) -> None:
    _record_autonomy_outcome(
        root_dir=tmp_path,
        manifest=_manifest(),
        factory_result=FactoryResult(completed=["comp-a"]),
        bus=EventBus(),
        run_id="run-1",
        ui=ui,
    )


# ---------------------------------------------------------------------------
# 1-2: the extracted applier
# ---------------------------------------------------------------------------


class TestApplyDemotion:
    def test_apply_demotion_matches_previous_behaviour(self, tmp_path: Path) -> None:
        """The applier writes exactly what the inline factory block wrote.

        The other half of this proof is that the two pre-existing tests
        named in the module docstring still pass unmodified; this half
        pins the record, the journal and the notice at the seam itself.
        """
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        ui, _ = _ui()

        record = apply_demotion(
            tmp_path,
            DemotionTrigger.POLICY_VIOLATION,
            "policy violation in comp-a",
            evidence={"components": ["comp-a"], "run_id": "r1"},
            run_id="r1",
            ui=ui,
        )

        assert record is not None
        assert (record.from_level, record.to_level) == (3, 2)
        assert record.direction == "demote"
        assert record.actor == "system"
        assert record.trigger == "policy_violation"

        reloaded = AutonomyState.load(tmp_path)
        assert reloaded.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert reloaded.cooldown_runs_remaining == DEMOTION_COOLDOWN_RUNS

        journal = (tmp_path / ".kstrl" / "evolution.jsonl").read_text(encoding="utf-8")
        assert '"event_type":"autonomy_transition"' in journal
        assert '"direction":"demote"' in journal

        notices = _items(tmp_path, ItemKind.DEMOTION_NOTICE)
        assert len(notices) == 1
        assert notices[0].title == "Autonomy demoted L3 -> L2"
        assert notices[0].dedupe_key == "demotion:r1:2"
        assert notices[0].evidence["trigger"] == "policy_violation"
        assert notices[0].evidence["from_level"] == 3
        assert notices[0].evidence["to_level"] == 2
        assert notices[0].evidence["components"] == ["comp-a"]

    def test_apply_demotion_at_floor_returns_none_and_counts(self, tmp_path: Path) -> None:
        state = AutonomyState(level=int(AutonomyLevel.L1_SUPERVISED))
        state.record_policy_violation()
        ui, buffer = _ui()

        record = apply_demotion(
            tmp_path,
            DemotionTrigger.POLICY_VIOLATION,
            "policy violation in comp-a",
            evidence={"components": ["comp-a"]},
            run_id="r1",
            ui=ui,
            state=state,
        )

        assert record is None
        reloaded = AutonomyState.load(tmp_path)
        assert reloaded.level == int(AutonomyLevel.L1_SUPERVISED)
        # The passed-in state's pending count survived: that is why the
        # keyword exists, and what blocks the next promotion.
        assert reloaded.policy_violations_at_level == 1
        assert _items(tmp_path) == []
        assert "nothing to revoke" in buffer.getvalue()


# ---------------------------------------------------------------------------
# 3-7, 12: the calibration-regression emitter
# ---------------------------------------------------------------------------


class TestCompareLadder:
    def test_compare_regression_opens_inbox_item(self, tmp_path: Path, capsys) -> None:
        _write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = _baseline_pair(tmp_path, missed=_MISSED_IN_NEW)

        code = calibration.main(["compare", str(old), str(new), "--root", str(tmp_path)])

        assert code == 1
        capsys.readouterr()
        drift = _items(tmp_path, ItemKind.CALIBRATION_DRIFT)
        assert len(drift) == 1
        assert (
            drift[0].title == f"Calibration regression: {_EXPECTED_FAILURES} failing threshold(s)"
        )
        assert drift[0].dedupe_key == f"calibration:{NEW_TS}"
        assert len(drift[0].evidence["failures"]) == _EXPECTED_FAILURES
        assert drift[0].evidence["new_baseline"] == NEW_TS
        # Advisory tier: the level does not move without the switch.
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)
        assert _items(tmp_path, ItemKind.DEMOTION_NOTICE) == []

    def test_compare_regression_demotes_when_enabled(self, tmp_path: Path, capsys) -> None:
        _write_config(tmp_path, demote_on_calibration=True)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = _baseline_pair(tmp_path, missed=_MISSED_IN_NEW)

        code = calibration.main(["compare", str(old), str(new), "--root", str(tmp_path)])

        assert code == 1
        capsys.readouterr()
        state = AutonomyState.load(tmp_path)
        assert state.level == int(AutonomyLevel.L1_SUPERVISED)
        assert state.history[-1].trigger == "calibration_regression"
        assert state.history[-1].evidence["new_baseline"] == NEW_TS
        kinds = {item.kind for item in _items(tmp_path)}
        assert kinds == {ItemKind.CALIBRATION_DRIFT, ItemKind.DEMOTION_NOTICE}

    def test_compare_regression_dedupes_inbox_item(self, tmp_path: Path, capsys) -> None:
        _write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = _baseline_pair(tmp_path, missed=_MISSED_IN_NEW)

        for _ in range(2):
            assert calibration.main(["compare", str(old), str(new), "--root", str(tmp_path)]) == 1
        capsys.readouterr()

        drift = _items(tmp_path, ItemKind.CALIBRATION_DRIFT)
        assert len(drift) == 1
        assert drift[0].occurrences == 2

    def test_compare_demotes_once_for_the_same_baseline(self, tmp_path: Path, capsys) -> None:
        """Re-reading one measurement must not cost a second level.

        The compare command is human-invoked and gets re-run. Deduping on
        the new baseline's timestamp makes the level a function of what
        was measured, not of how often it was looked at.
        """
        _write_config(tmp_path, demote_on_calibration=True)
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        old, new = _baseline_pair(tmp_path, missed=_MISSED_IN_NEW)

        for _ in range(2):
            assert calibration.main(["compare", str(old), str(new), "--root", str(tmp_path)]) == 1
        out = capsys.readouterr().out

        state = AutonomyState.load(tmp_path)
        assert state.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert len([t for t in state.history if t.direction == "demote"]) == 1
        assert len(_items(tmp_path, ItemKind.DEMOTION_NOTICE)) == 1
        assert f"baseline {NEW_TS} already cost a level" in out

    def test_compare_no_regression_writes_nothing(self, tmp_path: Path, capsys) -> None:
        _write_config(tmp_path, demote_on_calibration=True)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = _baseline_pair(tmp_path, missed=frozenset())

        code = calibration.main(["compare", str(old), str(new), "--root", str(tmp_path)])

        assert code == 0
        assert LADDER_DISABLED_LINE not in capsys.readouterr().out
        assert _items(tmp_path) == []
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_compare_autonomy_disabled_writes_nothing_and_says_so(
        self, tmp_path: Path, capsys
    ) -> None:
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        old, new = _baseline_pair(tmp_path, missed=_MISSED_IN_NEW)

        code = calibration.main(["compare", str(old), str(new), "--root", str(tmp_path)])

        assert code == 1
        assert LADDER_DISABLED_LINE in capsys.readouterr().out
        assert _items(tmp_path) == []
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_compare_unloadable_config_exits_2(self, tmp_path: Path, capsys) -> None:
        """A broken config is a refusal, never a silent "ladder is off"."""
        (tmp_path / "kstrl.toml").write_text("[autonomy\nenabled = true\n", encoding="utf-8")
        old, new = _baseline_pair(tmp_path, missed=_MISSED_IN_NEW)

        code = calibration.main(["compare", str(old), str(new), "--root", str(tmp_path)])

        assert code == 2
        assert "error:" in capsys.readouterr().err
        assert _items(tmp_path) == []


# ---------------------------------------------------------------------------
# 8-11, 13: the R8.4 health seam
# ---------------------------------------------------------------------------


class TestHealthSeam:
    def test_health_seam_inert_without_module(self, tmp_path: Path) -> None:
        """Until #151 lands there is no kstrl.health, and nothing fires."""
        assert "kstrl.health" not in sys.modules
        assert importlib.util.find_spec("kstrl.health") is None
        _write_config(tmp_path, demote_on_health=True)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        ui, _ = _ui()

        _run_outcome(tmp_path, ui)

        assert _items(tmp_path) == []
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
        monkeypatch.setitem(sys.modules, "kstrl.health", _fake_health(with_function=False))
        _write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        ui, _ = _ui()

        with pytest.raises(AttributeError):
            _run_outcome(tmp_path, ui)

    def test_health_breach_opens_inbox_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "kstrl.health", _fake_health(_breach()))
        _write_config(tmp_path)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        ui, _ = _ui()

        _run_outcome(tmp_path, ui)

        breaches = _items(tmp_path, ItemKind.HEALTH_BREACH)
        assert len(breaches) == 1
        assert breaches[0].title == "Health breach: retry_rate WE1: 1 point beyond 3 sigma"
        assert breaches[0].dedupe_key == "health:retry_rate:WE1: 1 point beyond 3 sigma"
        assert breaches[0].priority is Priority.NORMAL
        assert breaches[0].evidence["metric"] == "retry_rate"
        assert breaches[0].evidence["window_runs"] == 8
        # Advisory by default: a breach alone revokes nothing.
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)
        assert _items(tmp_path, ItemKind.DEMOTION_NOTICE) == []

    def test_health_breach_demotes_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "kstrl.health", _fake_health(_breach()))
        _write_config(tmp_path, demote_on_health=True)
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        ui, _ = _ui()

        _run_outcome(tmp_path, ui)

        state = AutonomyState.load(tmp_path)
        assert state.level == int(AutonomyLevel.L1_SUPERVISED)
        assert state.history[-1].trigger == "health_breach"
        assert _items(tmp_path, ItemKind.DEMOTION_NOTICE)

    def test_health_breach_suppressed_during_cooldown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cooling-down ladder records the breach and holds the level.

        A breach is a windowed trend, so it persists across runs. Without
        this gate one breach costs a level per run all the way to L1
        before the operator has read the first notice.
        """
        monkeypatch.setitem(sys.modules, "kstrl.health", _fake_health(_breach()))
        _write_config(tmp_path, demote_on_health=True)
        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        state.cooldown_runs_remaining = 3
        state.save(tmp_path)
        ui, _ = _ui()

        _run_outcome(tmp_path, ui)

        assert _items(tmp_path, ItemKind.HEALTH_BREACH)
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L3_ENVELOPED_AUTO)
        assert _items(tmp_path, ItemKind.DEMOTION_NOTICE) == []


# ---------------------------------------------------------------------------
# 14: config surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,env_var,attr",
    [
        (
            "demote_on_calibration_regression",
            "KSTRL_AUTONOMY_DEMOTE_ON_CALIBRATION",
            "demote_on_calibration_regression",
        ),
        (
            "demote_on_health_breach",
            "KSTRL_AUTONOMY_DEMOTE_ON_HEALTH",
            "demote_on_health_breach",
        ),
    ],
)
class TestConfigFlags:
    def test_defaults_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, env_var: str, attr: str
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        assert getattr(AutonomyConfig.load(tmp_path), attr) is False
        assert getattr(AutonomyConfig(), attr) is False

    def test_toml_alone_enables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, env_var: str, attr: str
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        (tmp_path / "kstrl.toml").write_text(f"[autonomy]\n{key} = true\n", encoding="utf-8")
        assert getattr(AutonomyConfig.load(tmp_path), attr) is True

    def test_env_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, env_var: str, attr: str
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(f"[autonomy]\n{key} = true\n", encoding="utf-8")
        monkeypatch.setenv(env_var, "0")
        assert getattr(AutonomyConfig.load(tmp_path), attr) is False
        monkeypatch.setenv(env_var, "1")
        (tmp_path / "kstrl.toml").write_text(f"[autonomy]\n{key} = false\n", encoding="utf-8")
        assert getattr(AutonomyConfig.load(tmp_path), attr) is True

    def test_from_env_reads_the_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, env_var: str, attr: str
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        assert getattr(AutonomyConfig.from_env(), attr) is False
        monkeypatch.setenv(env_var, "1")
        assert getattr(AutonomyConfig.from_env(), attr) is True
