"""R10.11 (#232): the one applier every automatic demotion goes through.

``DemotionTrigger`` has declared five causes since R8.2 and exactly one
of them fired. ``autonomy.apply_demotion`` is the extraction that lets
the other four fire without the four writes a demotion performs (ladder
state, evolution journal, run event stream, inbox notice) drifting apart
per trigger. That the extraction changed nothing is proven by two
PRE-EXISTING tests passing unmodified -
``test_autonomy_ladder.py::TestFactoryIntegration::
test_policy_violation_demotes_and_journals`` and ``test_inbox.py::
TestFactoryEmission::test_demotion_emits_notice_with_evidence`` - with
one exception, the at-floor warning, whose text no test covered and
which is pinned here now.

The two emitters that call it are in ``test_calibration_ladder.py`` (the
compare command) and ``test_health_seam.py`` (the R8.4 seam); the shared
fixtures are in ``tests/helpers/demotion.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl.autonomy import (
    DEMOTION_COOLDOWN_RUNS,
    AutonomyConfig,
    AutonomyLevel,
    AutonomyState,
    DemotionTrigger,
    apply_demotion,
)
from kstrl.config import ConfigError
from kstrl.config_preflight import config_problem_lines
from kstrl.inbox import ItemKind
from kstrl.statedir import CONTROL_AUTONOMY, control_file
from tests.helpers.demotion import inbox_items, make_ui, write_config

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
        ui, _ = make_ui()

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

        notices = inbox_items(tmp_path, ItemKind.DEMOTION_NOTICE)
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
        ui, buffer = make_ui()

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
        assert inbox_items(tmp_path) == []
        assert "nothing to revoke" in buffer.getvalue()

    def test_floor_message_names_the_trigger_and_the_reason(self, tmp_path: Path) -> None:
        """The at-floor line, pinned because the extraction changed it.

        The inline factory block said "policy violation recorded (2
        component(s))". The applier serves five triggers, so it names the
        trigger and carries the reason, which lists the components rather
        than counting them. Nothing covered the old string, so the change
        was invisible to the suite; this is what makes the next one
        visible.
        """
        state = AutonomyState(level=int(AutonomyLevel.L1_SUPERVISED))
        ui, buffer = make_ui()

        apply_demotion(
            tmp_path,
            DemotionTrigger.POLICY_VIOLATION,
            "policy violation in comp-a, comp-b",
            evidence={},
            run_id="r1",
            ui=ui,
            state=state,
        )

        assert (
            "Autonomy: policy violation recorded (policy violation in comp-a, comp-b); "
            "already at L1, nothing to revoke" in buffer.getvalue()
        )

    def test_at_floor_does_not_overwrite_a_degraded_state(self, tmp_path: Path) -> None:
        """Damaged bytes are the only thing an operator could repair.

        ``load`` fails closed to a fresh L1 with a ``degraded_reason``.
        Saving that fresh state replaces the damaged file with an empty
        ladder, and the counters the save would carry were lost with the
        file they came from. The factory has always done this; the
        compare command is a human-invoked measurement and must not.
        """
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        path = control_file(tmp_path, CONTROL_AUTONOMY)
        path.write_text("{ not json", encoding="utf-8")
        state = AutonomyState.load(tmp_path)
        assert state.degraded_reason is not None
        ui, buffer = make_ui()

        record = apply_demotion(
            tmp_path,
            DemotionTrigger.CALIBRATION_REGRESSION,
            "security below floor",
            evidence={},
            run_id="r1",
            ui=ui,
            state=state,
        )

        assert record is None
        assert path.read_text(encoding="utf-8") == "{ not json"
        assert "ladder state is degraded" in buffer.getvalue()

    def test_notice_honours_inbox_disabled(self, tmp_path: Path) -> None:
        """An inbox an operator switched off is not re-opened by a demotion.

        The property is stated in ``apply_demotion``'s own comment and
        was not covered: deleting the ``if inbox_config.enabled`` guard
        left the suite green.
        """
        write_config(tmp_path, inbox=False)
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        ui, _ = make_ui()

        record = apply_demotion(
            tmp_path,
            DemotionTrigger.POLICY_VIOLATION,
            "policy violation in comp-a",
            evidence={},
            run_id="r1",
            ui=ui,
        )

        assert record is not None
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)
        assert inbox_items(tmp_path) == []


# ---------------------------------------------------------------------------
# 14: config surface
# ---------------------------------------------------------------------------


#: The toml key and the dataclass attribute are the same name by design,
#: so the table carries it once: a second column could only ever disagree.
@pytest.mark.parametrize(
    "key,env_var",
    [
        ("demote_on_calibration_regression", "KSTRL_AUTONOMY_DEMOTE_ON_CALIBRATION"),
        ("demote_on_health_breach", "KSTRL_AUTONOMY_DEMOTE_ON_HEALTH"),
    ],
)
class TestConfigFlags:
    def test_defaults_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, env_var: str
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        assert getattr(AutonomyConfig.load(tmp_path), key) is False
        assert getattr(AutonomyConfig(), key) is False

    def test_toml_alone_enables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, env_var: str
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        (tmp_path / "kstrl.toml").write_text(f"[autonomy]\n{key} = true\n", encoding="utf-8")
        assert getattr(AutonomyConfig.load(tmp_path), key) is True

    def test_env_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, env_var: str
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(f"[autonomy]\n{key} = true\n", encoding="utf-8")
        monkeypatch.setenv(env_var, "0")
        assert getattr(AutonomyConfig.load(tmp_path), key) is False
        monkeypatch.setenv(env_var, "1")
        (tmp_path / "kstrl.toml").write_text(f"[autonomy]\n{key} = false\n", encoding="utf-8")
        assert getattr(AutonomyConfig.load(tmp_path), key) is True

    def test_from_env_reads_the_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, env_var: str
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        assert getattr(AutonomyConfig.from_env(), key) is False
        monkeypatch.setenv(env_var, "1")
        assert getattr(AutonomyConfig.from_env(), key) is True

    @pytest.mark.parametrize("literal", ['"false"', '"off"', '"0"', "0"])
    def test_quoted_boolean_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        key: str,
        env_var: str,
        literal: str,
    ) -> None:
        """A typo that ARMS a revocation switch is worse than one that does not.

        ``bool("false")`` is True, so the reading the rest of the package
        uses turns all three quoted spellings into an armed switch. The
        unquoted ``0`` is a TOML integer and is refused for the same
        reason: it is not a boolean, and guessing which way it leans is
        how the quoted ones got through.
        """
        monkeypatch.delenv(env_var, raising=False)
        (tmp_path / "kstrl.toml").write_text(f"[autonomy]\n{key} = {literal}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=f"{key} must be a boolean"):
            AutonomyConfig.load(tmp_path)

    def test_refusal_reaches_the_preflight_as_a_problem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, env_var: str
    ) -> None:
        """The nth-order effect: a config that used to load now does not.

        ``[autonomy]`` is a fatal preflight section, and 16 CLI commands
        run the preflight, so refusing a value that previously coerced
        has to surface as a stated configuration problem rather than as
        a traceback out of ``ks status``. Also pins that the message
        carries no ``[autonomy]`` prefix of its own: the preflight adds
        the section label, and a self-labelled message reads
        "[autonomy] [autonomy] ..." there.
        """
        monkeypatch.delenv(env_var, raising=False)
        (tmp_path / "kstrl.toml").write_text(f'[autonomy]\n{key} = "false"\n', encoding="utf-8")

        problems = config_problem_lines(tmp_path, warn=lambda _line: None)

        assert len(problems) == 1, problems
        assert problems[0].startswith(f"[autonomy] {key} must be a boolean"), problems[0]
        # Not "[autonomy] [autonomy] ...". The trailing provenance the
        # preflight appends names the section again on purpose, so the
        # check is on the prefix rather than on a count.
        assert not problems[0].startswith("[autonomy] [autonomy]")
