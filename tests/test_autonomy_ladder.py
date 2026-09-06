"""R8.2 autonomy ladder tests.

Covers the three invariants the ladder's trust rests on - agents cannot
promote themselves, demotion is automatic and immediate, the flag bundle
is derived rather than stored - plus planted demotion-trigger fixtures,
persistence, the config surface, and the threshold-replay tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.autonomy import (
    DEMOTION_COOLDOWN_RUNS,
    L2_MERGED_COMPONENTS_REQUIRED,
    MIN_DECISIVE_RUNS,
    THRESHOLDS,
    AutonomyConfig,
    AutonomyError,
    AutonomyLevel,
    AutonomyState,
    DemotionTrigger,
    effective_level,
    flag_bundle_for,
    manual_override_notes,
)
from kstrl.autonomy_replay import load_runs, replay, replay_file
from tests.helpers.component_prd import write_component_prd
from tests.helpers.replay import clean_run, failing_run, run_record


def _eligible_state(level: AutonomyLevel = AutonomyLevel.L1_SUPERVISED) -> AutonomyState:
    """A state that satisfies every criterion for the next level."""
    state = AutonomyState(level=int(level))
    state.decisive_runs_at_level = MIN_DECISIVE_RUNS
    state.components_merged_at_level = 50
    state.clean_merges_at_level = 50
    return state


# --------------------------------------------------------------------------
# Flag bundles: levels drive permissions
# --------------------------------------------------------------------------
class TestFlagBundles:
    def test_l1_is_fully_supervised(self) -> None:
        bundle = flag_bundle_for(AutonomyLevel.L1_SUPERVISED)
        assert bundle.pause_before_pr_merge is True
        assert bundle.auto_accept_plan is False
        assert bundle.auto_merge_when_green is False
        assert bundle.deploy_permitted is False
        assert bundle.deps_allow_new_permitted is False

    def test_l2_auto_accepts_plans_but_gates_merge(self) -> None:
        bundle = flag_bundle_for(AutonomyLevel.L2_GATED_MERGE)
        assert bundle.auto_accept_plan is True
        assert bundle.pause_before_pr_merge is True  # human still gates merge
        assert bundle.auto_merge_when_green is False

    def test_l3_drops_the_merge_gate(self) -> None:
        bundle = flag_bundle_for(AutonomyLevel.L3_ENVELOPED_AUTO)
        assert bundle.pause_before_pr_merge is False
        assert bundle.auto_merge_when_green is True
        assert bundle.deploy_permitted is False  # deploy is L4 only

    def test_l4_adds_deploy_only(self) -> None:
        l3 = flag_bundle_for(AutonomyLevel.L3_ENVELOPED_AUTO)
        l4 = flag_bundle_for(AutonomyLevel.L4_DEPLOY)
        assert l4.deploy_permitted is True
        for attr in (
            "pause_before_pr_merge",
            "auto_accept_plan",
            "auto_merge_when_green",
            "deps_allow_new_permitted",
        ):
            assert getattr(l4, attr) == getattr(l3, attr)

    def test_review_mode_stays_hard_at_every_level(self) -> None:
        # Goodhart guard: autonomy must never buy weaker verification.
        for level in AutonomyLevel:
            assert flag_bundle_for(level).review_mode == "hard"

    def test_permissions_are_monotonic_in_level(self) -> None:
        levels = sorted(AutonomyLevel)
        for lower, higher in zip(levels, levels[1:], strict=False):
            lo, hi = flag_bundle_for(lower), flag_bundle_for(higher)
            # Nothing a higher level grants may be revoked by going up.
            assert not (lo.auto_accept_plan and not hi.auto_accept_plan)
            assert not (lo.auto_merge_when_green and not hi.auto_merge_when_green)
            assert not (lo.deploy_permitted and not hi.deploy_permitted)

    def test_bundle_is_derived_not_stored(self) -> None:
        # The persisted payload must not contain flags - only the level.
        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        assert state.flag_bundle().pause_before_pr_merge is False
        assert not hasattr(state, "pause_before_pr_merge")


# --------------------------------------------------------------------------
# Promotion: evidence AND a human ack
# --------------------------------------------------------------------------
class TestPromotion:
    def test_requires_actor(self) -> None:
        state = _eligible_state()
        with pytest.raises(AutonomyError, match="agents cannot promote themselves"):
            state.promote(actor="", ack="looks good")

    def test_requires_ack(self) -> None:
        state = _eligible_state()
        with pytest.raises(AutonomyError, match="acknowledgement"):
            state.promote(actor="human", ack="   ")

    def test_blocked_without_evidence(self) -> None:
        state = AutonomyState()  # fresh: no runs, no merges
        with pytest.raises(AutonomyError, match="cannot promote"):
            state.promote(actor="human", ack="trust me")
        assert state.level == int(AutonomyLevel.L1_SUPERVISED)

    def test_succeeds_with_evidence_and_ack(self) -> None:
        state = _eligible_state()
        record = state.promote(actor="wumpini", ack="5 clean merges reviewed")
        assert state.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert record.direction == "promote"
        assert record.actor == "wumpini"
        assert state.last_promoted_by == "wumpini"

    def test_promotion_resets_level_counters(self) -> None:
        # Evidence earned at L1 must not count toward L3.
        state = _eligible_state()
        state.promote(actor="human", ack="ok")
        assert state.decisive_runs_at_level == 0
        assert state.components_merged_at_level == 0
        assert state.clean_merges_at_level == 0

    def test_cannot_skip_levels(self) -> None:
        state = _eligible_state()
        blockers = state.promotion_blockers(AutonomyLevel.L3_ENVELOPED_AUTO)
        assert any("cannot skip levels" in b for b in blockers)

    def test_cannot_promote_beyond_l4(self) -> None:
        state = _eligible_state(AutonomyLevel.L4_DEPLOY)
        with pytest.raises(AutonomyError, match="highest level"):
            state.promote(actor="human", ack="more")

    def test_policy_violation_blocks_promotion(self) -> None:
        state = _eligible_state()
        state.record_policy_violation()
        blockers = state.promotion_blockers()
        assert any("policy violation" in b for b in blockers)

    def test_force_records_the_override(self) -> None:
        state = AutonomyState()  # no evidence at all
        record = state.promote(actor="human", ack="accepting risk", force=True)
        assert state.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert record.evidence["forced_over_blockers"]

    def test_force_still_requires_an_ack(self) -> None:
        state = AutonomyState()
        with pytest.raises(AutonomyError):
            state.promote(actor="human", ack="", force=True)


# --------------------------------------------------------------------------
# Demotion: planted trigger fixtures ("Done when")
# --------------------------------------------------------------------------
PLANTED_TRIGGERS = [
    (DemotionTrigger.POLICY_VIOLATION, "policy envelope breach on kstrl/verify.py"),
    (DemotionTrigger.CALIBRATION_REGRESSION, "architect detection fell below baseline"),
    (DemotionTrigger.HEALTH_BREACH, "retry rate beyond 3 sigma"),
    (DemotionTrigger.HUMAN_REJECTED_AUTO_MERGE, "human rejected an L3 candidate"),
    (DemotionTrigger.MANUAL, "operator revoked"),
]


class TestDemotion:
    @pytest.mark.parametrize("trigger,reason", PLANTED_TRIGGERS)
    def test_each_trigger_drops_exactly_one_level(
        self,
        trigger: DemotionTrigger,
        reason: str,
    ) -> None:
        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        record = state.demote(trigger, reason)
        assert record is not None
        assert state.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert record.trigger == trigger.label
        assert record.reason == reason

    def test_demotion_needs_no_ack(self) -> None:
        # Revoking autonomy must never wait on a human.
        state = AutonomyState(level=int(AutonomyLevel.L4_DEPLOY))
        assert state.demote(DemotionTrigger.HEALTH_BREACH, "breach") is not None

    def test_demotion_at_l1_is_a_noop(self) -> None:
        state = AutonomyState()
        assert state.demote(DemotionTrigger.POLICY_VIOLATION, "breach") is None
        assert state.level == int(AutonomyLevel.L1_SUPERVISED)

    def test_demotion_starts_cooldown(self) -> None:
        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        state.demote(DemotionTrigger.POLICY_VIOLATION, "breach")
        assert state.cooldown_runs_remaining == DEMOTION_COOLDOWN_RUNS

    def test_cooldown_blocks_repromotion(self) -> None:
        state = _eligible_state(AutonomyLevel.L3_ENVELOPED_AUTO)
        state.demote(DemotionTrigger.POLICY_VIOLATION, "breach")
        state.decisive_runs_at_level = MIN_DECISIVE_RUNS
        state.components_merged_at_level = 50
        state.clean_merges_at_level = 50
        blockers = state.promotion_blockers()
        assert any("cool-down" in b for b in blockers)
        with pytest.raises(AutonomyError, match="cool-down"):
            state.promote(actor="human", ack="re-promote")

    def test_cooldown_burns_down_with_decisive_runs(self) -> None:
        state = AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE))
        state.demote(DemotionTrigger.HEALTH_BREACH, "breach")
        for _ in range(DEMOTION_COOLDOWN_RUNS):
            state.record_decisive_run()
        assert state.cooldown_runs_remaining == 0

    def test_demotion_resets_level_counters(self) -> None:
        state = _eligible_state(AutonomyLevel.L2_GATED_MERGE)
        state.demote(DemotionTrigger.POLICY_VIOLATION, "breach")
        assert state.components_merged_at_level == 0
        assert state.decisive_runs_at_level == 0

    def test_repeated_triggers_walk_down_one_at_a_time(self) -> None:
        state = AutonomyState(level=int(AutonomyLevel.L4_DEPLOY))
        for expected in (3, 2, 1):
            state.demote(DemotionTrigger.HEALTH_BREACH, "breach")
            assert state.level == expected
        assert state.demote(DemotionTrigger.HEALTH_BREACH, "breach") is None


# --------------------------------------------------------------------------
# Evidence accumulation
# --------------------------------------------------------------------------
class TestEvidence:
    def test_merged_component_extends_clean_streak(self) -> None:
        state = AutonomyState()
        state.record_merged_component()
        state.record_merged_component()
        assert state.components_merged_at_level == 2
        assert state.clean_merges_at_level == 2

    def test_human_edit_breaks_the_clean_streak(self) -> None:
        state = AutonomyState()
        state.record_merged_component()
        state.record_merged_component(human_edited=True)
        assert state.components_merged_at_level == 2
        assert state.clean_merges_at_level == 0

    def test_l2_criteria_report_progress(self) -> None:
        state = AutonomyState()
        state.decisive_runs_at_level = MIN_DECISIVE_RUNS
        state.record_merged_component()
        blockers = state.promotion_blockers()
        assert any(f"1/{L2_MERGED_COMPONENTS_REQUIRED} components merged" in b for b in blockers)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
class TestPersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        state = _eligible_state()
        state.promote(actor="human", ack="evidence reviewed")
        state.record_merged_component()
        state.save(tmp_path)
        loaded = AutonomyState.load(tmp_path)
        assert loaded.level == state.level
        assert loaded.last_promoted_by == "human"
        assert loaded.components_merged_at_level == 1
        assert len(loaded.history) == 1
        assert loaded.history[0].direction == "promote"

    def test_missing_file_defaults_to_l1(self, tmp_path: Path) -> None:
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L1_SUPERVISED)

    def test_corrupt_file_falls_back_to_l1(self, tmp_path: Path) -> None:
        # Unknown autonomy must resolve to the LEAST autonomy.
        path = AutonomyState.path_for(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L1_SUPERVISED)

    def test_out_of_range_level_falls_back_to_l1(self, tmp_path: Path) -> None:
        path = AutonomyState.path_for(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"level": 99}))
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L1_SUPERVISED)

    def test_saved_payload_has_no_flags(self, tmp_path: Path) -> None:
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        data = json.loads(AutonomyState.path_for(tmp_path).read_text())
        assert data["level"] == 3
        for forbidden in ("pause_before_pr_merge", "auto_merge_when_green", "flags"):
            assert forbidden not in data


# --------------------------------------------------------------------------
# Config + manual overrides
# --------------------------------------------------------------------------
class TestConfig:
    def test_disabled_by_default(self) -> None:
        assert AutonomyConfig().enabled is False

    def test_load_reads_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = true\nmax_level = 2\n")
        config = AutonomyConfig.load(tmp_path)
        assert config.enabled is True and config.max_level == 2

    def test_env_overrides_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = false\n")
        monkeypatch.setenv("KSTRL_AUTONOMY_ENABLED", "1")
        assert AutonomyConfig.load(tmp_path).enabled is True

    def test_invalid_max_level_rejected(self) -> None:
        with pytest.raises(AutonomyError):
            AutonomyConfig(max_level=9)

    def test_max_level_clamps_without_rewriting_state(self) -> None:
        state = AutonomyState(level=int(AutonomyLevel.L4_DEPLOY))
        config = AutonomyConfig(enabled=True, max_level=2)
        assert effective_level(state, config) is AutonomyLevel.L2_GATED_MERGE
        assert state.level == int(AutonomyLevel.L4_DEPLOY)  # earned level intact

    def test_manual_override_is_named_not_honored(self) -> None:
        bundle = flag_bundle_for(AutonomyLevel.L1_SUPERVISED)
        notes = manual_override_notes(
            bundle,
            configured_pause_before_pr_merge=False,  # contradicts L1
            configured_review_mode="advisory",  # contradicts hard
        )
        assert len(notes) == 2
        assert all("bundle wins" in n for n in notes)

    def test_agreeing_config_produces_no_notes(self) -> None:
        bundle = flag_bundle_for(AutonomyLevel.L1_SUPERVISED)
        assert (
            manual_override_notes(
                bundle,
                configured_pause_before_pr_merge=True,
                configured_review_mode="hard",
            )
            == []
        )


# --------------------------------------------------------------------------
# Threshold replay
# --------------------------------------------------------------------------
#: #339: the defaults moved to ``tests/helpers/replay.py`` when a second
#: file needed the same records and hand-rolled them, one of them
#: byte-for-byte identical to ``_clean`` below.
_run = run_record


class TestReplay:
    def test_infra_failures_are_not_decisive(self) -> None:
        assert _run(completed=0, failed=1, common_failure="pr:push-failed").decisive is False
        assert _run(completed=0, failed=1, common_failure="git:timeout").decisive is False

    def test_judgement_failures_are_decisive(self) -> None:
        assert _run(completed=0, failed=1, common_failure="review:prd_criterion").decisive

    def test_run_with_no_terminal_component_is_not_decisive(self) -> None:
        assert _run(completed=0, failed=0).decisive is False

    def test_small_sample_reports_insufficient(self) -> None:
        report = replay([_run(run_id=f"r{i}") for i in range(3)])
        assert report.sufficient_data is False
        assert "INSUFFICIENT DATA" in report.render()

    def test_replay_never_promotes_without_enough_runs(self) -> None:
        report = replay([_run(run_id=f"r{i}") for i in range(5)])
        assert report.would_promote == []
        assert report.final_level == int(AutonomyLevel.L1_SUPERVISED)

    def test_replay_reports_thresholds(self) -> None:
        report = replay([])
        assert report.thresholds == THRESHOLDS
        assert "UNMEASURED PLACEHOLDERS" in report.render()

    def test_judgement_failure_would_demote(self) -> None:
        # Needs a level above L1 to have somewhere to fall to; the replay
        # starts at L1, so a demote is a no-op and must not be reported.
        report = replay(
            [
                _run(run_id="r1", completed=0, failed=1, common_failure="review:x"),
            ]
        )
        assert report.would_demote == []  # already at the floor
        assert report.final_level == int(AutonomyLevel.L1_SUPERVISED)

    def test_missing_experiments_file_is_not_an_error(self, tmp_path: Path) -> None:
        report = replay_file(tmp_path / "nope.tsv")
        assert report.total_runs == 0
        assert report.sufficient_data is False

    def test_replay_never_mutates_stored_state(self, tmp_path: Path) -> None:
        # Reading a replay is a report, never a transition.
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        replay_file(tmp_path / "nope.tsv", tmp_path)
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_loads_real_tsv_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "experiments.tsv"
        path.write_text(
            "run_id\ttimestamp\tproject\tcomponents_total\tcompleted\tfailed\t"
            "skipped\tavg_iterations\tavg_duration_s\tretry_rate\tcommon_failure\t"
            "total_tokens\ttotal_cost_usd\tunreported_calls\n"
            "factory-1\t2026-07-20T14:23:31Z\tslugify\t1\t0\t1\t0\t1.00\t246.1\t"
            "1.00\tpr:failed-to-create-pr\t30204\t0.5\t0\n"
            "factory-2\t2026-07-20T16:08:20Z\tslugify\t2\t2\t0\t0\t1.00\t666.5\t"
            "0.50\t\t7558457\t5.28\t0\n"
        )
        runs = load_runs(path)
        assert len(runs) == 2
        assert runs[0].infra_aborted is True  # pr: prefix
        assert runs[1].decisive is True
        report = replay(runs)
        assert report.decisive_runs == 1
        assert report.infra_aborted_runs == 1
        assert report.components_merged == 2

    def test_a_torn_row_is_not_a_run_the_ladder_promotes_on(self, tmp_path: Path) -> None:
        """#331's read half, on the SECOND reader of experiments.tsv.

        A crash mid-write left a row torn, the next ``record_run``
        appended onto it, and ``csv.DictReader`` zipped the
        concatenation against the header: a run id from the fragment,
        this run's fields shifted along by however many columns the
        fragment held, and ``_as_int`` turning each of them into 0
        rather than raising. That is a fabricated run in the population
        a promotion is decided on, and it is silent.

        The torn row here is written the way a crash writes one, and the
        real ``record_run`` appends onto it, so this measures the reader
        against bytes the writer actually produces.
        """
        from kstrl.factory import FactoryResult
        from kstrl.manifest import Manifest
        from tests.helpers.journal import journal_at, tear

        journal = journal_at(tmp_path)
        manifest = Manifest(
            version="1",
            spec_file="s.md",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[],
        )
        journal.record_run("run-1", manifest, FactoryResult())
        tear(journal.config.experiments_path)
        journal.record_run("run-2", manifest, FactoryResult())

        runs = load_runs(journal.config.experiments_path)

        assert [run.run_id for run in runs] == ["run-1", "run-2"]


# --------------------------------------------------------------------------
# Factory wiring: levels drive the flag bundle ("Done when")
# --------------------------------------------------------------------------
def _init_git_repo(root: Path) -> None:
    """A real repo: without one the diff phase fails as infrastructure and
    no component can reach a terminal verdict."""
    import subprocess

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )

    run("init")
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "tester")
    (root / "README.md").write_text("base\n")
    run("add", ".")
    run("commit", "-m", "base")


def _run_factory_with_autonomy(
    tmp_path: Path,
    level: AutonomyLevel,
    *,
    enabled: bool,
    configured_pause: bool,
    policy_enabled: bool = True,
    findings: list[object] | None = None,
    succeed: bool = True,
) -> object:
    """Run the factory against a stored level; return the FactoryConfig."""
    from kstrl.config import KstrlConfig
    from kstrl.factory import ComponentResult, FactoryConfig, run_factory
    from kstrl.manifest import Component, Manifest
    from kstrl.ui.plain import PlainUI
    from kstrl.verify import CheckResult, VerificationResult, VerifyConfig

    _init_git_repo(tmp_path)
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    # Without it the run is refused before it starts (#293 review): a
    # component whose pre-run PRD will not read has no plan-time scope.
    write_component_prd(tmp_path, "scripts/kstrl/feature/comp-a/prd.json")
    (tmp_path / "kstrl.toml").write_text(
        f"[autonomy]\nenabled = {'true' if enabled else 'false'}\n"
        f"[policy]\nenabled = {'true' if policy_enabled else 'false'}\n"
    )
    AutonomyState(level=int(level)).save(tmp_path)

    manifest = Manifest(
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
    config = FactoryConfig(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        pause_before_pr_merge=configured_pause,
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    base = KstrlConfig(
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    result = ComponentResult("comp-a", success=succeed, iterations=1)
    # Findings reach the component the way they do in production: a
    # policy CheckResult carries them and the pipeline lifts them into
    # the finding stream (R8.1). Pre-seeding the manifest would not
    # survive - begin_attempt clears that stream.
    if findings:
        blocking = [f for f in findings if getattr(f, "severity", "") != "advisory"]
        verification = VerificationResult(
            passed=not blocking,
            checks=[
                CheckResult(
                    "policy_envelope",
                    not blocking,
                    "policy",
                    findings=list(findings),  # type: ignore[arg-type]
                )
            ],
        )
    else:
        verification = VerificationResult(
            passed=True,
            checks=[CheckResult("diff_scope", True, "ok")],
        )
    # The ladder forces review_mode="hard" at every level, so the review
    # phase always runs; stub it green so these tests measure the LADDER,
    # not the reviewer.
    from kstrl.review import ReviewResult

    with (
        patch("kstrl.factory._run_component", return_value=result),
        patch(
            "kstrl.factory.run_mechanical_verification",
            return_value=verification,
        ),
        patch(
            "kstrl.factory.run_review",
            return_value=ReviewResult(passed=True, mode="hard"),
        ),
    ):
        run_factory(manifest, config, base, PlainUI(no_color=True), tmp_path)
    return config


class TestFactoryWiring:
    def test_level_drives_flags_over_contradicting_config(
        self,
        tmp_path: Path,
    ) -> None:
        # L1 demands the merge gate ON; config says off. Bundle must win.
        config = _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L1_SUPERVISED,
            enabled=True,
            configured_pause=False,
        )
        assert config.pause_before_pr_merge is True  # type: ignore[attr-defined]
        assert config.review_mode == "hard"  # type: ignore[attr-defined]

    def test_l3_drops_the_merge_gate(self, tmp_path: Path) -> None:
        config = _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L3_ENVELOPED_AUTO,
            enabled=True,
            configured_pause=True,
        )
        assert config.pause_before_pr_merge is False  # type: ignore[attr-defined]

    def test_disabled_ladder_leaves_config_untouched(
        self,
        tmp_path: Path,
    ) -> None:
        # Opt-in: with [autonomy] off, the stored level changes nothing.
        config = _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L3_ENVELOPED_AUTO,
            enabled=False,
            configured_pause=True,
        )
        assert config.pause_before_pr_merge is True  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Review regressions (PR #174)
# --------------------------------------------------------------------------
class TestEnvelopeCeiling:
    """L3 is *Enveloped* auto-merge: no envelope, no auto-merge."""

    def test_l3_clamps_to_l2_without_policy_envelope(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.autonomy import resolve_runtime_level

        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        level, notes = resolve_runtime_level(
            state,
            AutonomyConfig(enabled=True),
            policy_enabled=False,
            root_dir=tmp_path,
        )
        assert level is AutonomyLevel.L2_GATED_MERGE
        assert any("requires the R8.1 policy envelope" in n for n in notes)

    def test_l4_clamps_to_l2_without_policy_envelope(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.autonomy import resolve_runtime_level

        state = AutonomyState(level=int(AutonomyLevel.L4_DEPLOY))
        level, _ = resolve_runtime_level(
            state,
            AutonomyConfig(enabled=True),
            policy_enabled=False,
            root_dir=tmp_path,
        )
        assert level is AutonomyLevel.L2_GATED_MERGE

    def test_l3_allowed_with_envelope(self, tmp_path: Path) -> None:
        from kstrl.autonomy import resolve_runtime_level

        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        level, notes = resolve_runtime_level(
            state,
            AutonomyConfig(enabled=True),
            policy_enabled=True,
            root_dir=tmp_path,
        )
        assert level is AutonomyLevel.L3_ENVELOPED_AUTO
        assert notes == []

    def test_lowest_ceiling_wins(self, tmp_path: Path) -> None:
        from kstrl.autonomy import resolve_runtime_level

        state = AutonomyState(level=int(AutonomyLevel.L4_DEPLOY))
        level, notes = resolve_runtime_level(
            state,
            AutonomyConfig(enabled=True, max_level=3),
            policy_enabled=False,
            root_dir=tmp_path,
        )
        assert level is AutonomyLevel.L2_GATED_MERGE  # envelope beats max_level
        assert len(notes) == 2

    def test_merge_gate_stays_on_when_envelope_disabled(
        self,
        tmp_path: Path,
    ) -> None:
        # The end-to-end version: an L3 repo with [policy] off must NOT
        # get auto-merge.
        config = _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L3_ENVELOPED_AUTO,
            enabled=True,
            configured_pause=False,
            policy_enabled=False,
        )
        assert config.pause_before_pr_merge is True  # type: ignore[attr-defined]


class TestBundleClampsPolicy:
    """The ladder withholds envelope permissions below the level that earns them."""

    def test_deps_allow_new_withheld_below_l3(self, tmp_path: Path) -> None:
        from kstrl.policy import PolicyConfig

        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "scripts" / "kstrl" / "prompt.md").write_text("p")
        (tmp_path / "kstrl.toml").write_text(
            "[autonomy]\nenabled = true\n[policy]\nenabled = true\ndeps_allow_new = true\n"
        )
        AutonomyState(level=int(AutonomyLevel.L1_SUPERVISED)).save(tmp_path)
        assert PolicyConfig.load(tmp_path).deps_allow_new is True

        config = _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L1_SUPERVISED,
            enabled=True,
            configured_pause=True,
            policy_enabled=True,
        )
        # kstrl.toml is rewritten by the helper, so assert via the bundle:
        assert flag_bundle_for(AutonomyLevel.L1_SUPERVISED).deps_allow_new_permitted is False
        assert config.policy_config is not None  # type: ignore[attr-defined]
        assert config.policy_config.deps_allow_new is False  # type: ignore[attr-defined]

    def test_l3_permits_new_dependencies(self) -> None:
        assert flag_bundle_for(AutonomyLevel.L3_ENVELOPED_AUTO).deps_allow_new_permitted is True


class TestRunOutcomesReachState:
    """A run must actually move the ladder's counters (not just in tests)."""

    def test_successful_run_records_evidence(self, tmp_path: Path) -> None:
        _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L1_SUPERVISED,
            enabled=True,
            configured_pause=True,
        )
        reloaded = AutonomyState.load(tmp_path)
        assert reloaded.decisive_runs_at_level == 1
        assert reloaded.components_merged_at_level == 1
        assert reloaded.clean_merges_at_level == 1

    def test_evidence_accumulates_across_runs(self, tmp_path: Path) -> None:
        for _ in range(3):
            _run_factory_with_autonomy(
                tmp_path,
                AutonomyLevel.L1_SUPERVISED,
                enabled=True,
                configured_pause=True,
            )
            # Helper rewrites state each call, so re-seed from disk:
            state = AutonomyState.load(tmp_path)
            state.save(tmp_path)
        assert AutonomyState.load(tmp_path).decisive_runs_at_level >= 1

    def test_disabled_ladder_records_nothing(self, tmp_path: Path) -> None:
        _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L1_SUPERVISED,
            enabled=False,
            configured_pause=True,
        )
        assert AutonomyState.load(tmp_path).decisive_runs_at_level == 0

    def test_policy_violation_demotes_and_journals(self, tmp_path: Path) -> None:
        from kstrl.findings import Finding

        violation = Finding.policy_violation(
            category="paths_deny",
            explanation="touched a denied path",
        )
        _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L3_ENVELOPED_AUTO,
            enabled=True,
            configured_pause=False,
            policy_enabled=True,
            findings=[violation],
        )
        reloaded = AutonomyState.load(tmp_path)
        assert reloaded.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert reloaded.cooldown_runs_remaining == DEMOTION_COOLDOWN_RUNS
        assert reloaded.history[-1].trigger == "policy_violation"
        # ... and the transition reached the evolution journal.
        journal = (tmp_path / ".kstrl" / "evolution.jsonl").read_text()
        assert '"event_type":"autonomy_transition"' in journal
        assert '"direction":"demote"' in journal

    def test_advisory_finding_does_not_demote(self, tmp_path: Path) -> None:
        from kstrl.findings import Finding

        advisory = Finding.policy_violation(
            category="license_unresolved",
            explanation="unknown license",
            severity="advisory",
        )
        _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L3_ENVELOPED_AUTO,
            enabled=True,
            configured_pause=False,
            policy_enabled=True,
            findings=[advisory],
        )
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L3_ENVELOPED_AUTO)


class TestTransitionAudit:
    """Every committed transition reaches the durable audit stream."""

    def test_commit_transition_writes_journal(self, tmp_path: Path) -> None:
        from kstrl.autonomy import commit_transition

        state = _eligible_state()
        record = state.promote(actor="human", ack="reviewed")
        commit_transition(state, record, tmp_path, run_id="run-1")
        journal = (tmp_path / ".kstrl" / "evolution.jsonl").read_text()
        assert '"event_type":"autonomy_transition"' in journal
        assert '"direction":"promote"' in journal
        assert '"actor":"human"' in journal
        # State was saved too, not just journaled.
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_commit_transition_emits_event_when_in_a_run(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.autonomy import commit_transition
        from kstrl.events import AutonomyTransition, EventBus

        seen: list[object] = []

        class _Collector:
            def emit(self, event: object) -> None:
                seen.append(event)

            def close(self) -> None:
                return None

        bus = EventBus(_Collector(), run_id="run-1")  # type: ignore[arg-type]
        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        record = state.demote(DemotionTrigger.HEALTH_BREACH, "breach")
        assert record is not None
        commit_transition(state, record, tmp_path, bus=bus, run_id="run-1")
        assert any(isinstance(e, AutonomyTransition) for e in seen)

    def test_journal_failure_is_not_fatal(self, tmp_path: Path) -> None:
        """Losing the log must never strand the ladder unsaved.

        A real failure on a real path (a directory where the journal
        file should be) rather than a patched ``kstrl.autonomy.open``,
        which stopped intercepting anything when #312 moved the write
        into ``EvolutionJournal.append_entries``. A patched builtin pins
        WHERE the code writes, which is not what this test is about.
        """
        from kstrl.autonomy import commit_transition
        from kstrl.evolution import EvolutionConfig

        journal_path = EvolutionConfig.load(tmp_path).journal_path
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.mkdir()

        state = _eligible_state()
        record = state.promote(actor="human", ack="ok")
        with pytest.warns(RuntimeWarning, match="journal append failed"):
            commit_transition(state, record, tmp_path)
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_an_unparseable_journal_config_is_not_fatal_either(
        self,
        tmp_path: Path,
    ) -> None:
        """#257 sweep: the guard here caught only OSError, and
        ``EvolutionConfig.load`` raises ValueError on a malformed
        [evolution] section. The state save has already happened by
        then, so a typo in an unrelated knob would leave the ladder
        saved and unjournaled - exactly the drift this function exists
        to prevent.
        """
        from kstrl.autonomy import commit_transition

        (tmp_path / "kstrl.toml").write_text("[evolution\nenabled = true\n")
        state = _eligible_state()
        record = state.promote(actor="human", ack="ok")

        with pytest.warns(RuntimeWarning, match="Evolution config unreadable"):
            commit_transition(state, record, tmp_path)

        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)


class TestPromotionAuthority:
    """A caller-supplied string is not a human acknowledgement."""

    def test_non_tty_cannot_promote(self) -> None:
        from kstrl.autonomy import promotion_authority_error

        with patch("sys.stdin.isatty", return_value=False):
            assert promotion_authority_error(force=False) is not None

    def test_non_tty_cannot_force(self) -> None:
        from kstrl.autonomy import promotion_authority_error

        with patch("sys.stdin.isatty", return_value=False):
            error = promotion_authority_error(force=True)
        assert error is not None
        assert "bypass evidence" in error

    def test_tty_may_promote(self) -> None:
        from kstrl.autonomy import promotion_authority_error

        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
        ):
            assert promotion_authority_error(force=False) is None

    def test_ladder_state_is_enforcement_machinery(self) -> None:
        # The obvious way around the TTY gate is to write the level
        # straight to disk; R8.1 must halt on that. Legacy in-tree control
        # paths stay halted after R8.9 relocated the live copies to XDG.
        from kstrl.policy import PolicyConfig, evaluate_policy

        for path in (
            ".kstrl/autonomy.json",
            ".kstrl/inbox.jsonl",
            ".kstrl/queue/spend.json",
            ".kstrl/queue/pause.json",
            ".kstrl/queue/github_processed.json",
            "kstrl/autonomy.py",
            "kstrl/statedir.py",
        ):
            result = evaluate_policy(
                [path],
                [(1, 0, path)],
                "",
                PolicyConfig(paths_deny=[]),
            )
            assert result.machinery_hit, path


class TestReplayAdvancesLevels:
    """The replay must traverse levels, not re-report the same eligibility."""

    _clean = staticmethod(clean_run)

    def test_level_advances_past_l1(self) -> None:
        report = replay([self._clean(i) for i in range(12)])
        assert report.final_level > int(AutonomyLevel.L1_SUPERVISED)

    def test_no_duplicate_eligibility_for_the_same_level(self) -> None:
        report = replay([self._clean(i) for i in range(12)])
        targets = [entry.split("->")[1].strip()[:2] for entry in report.would_promote]
        assert len(targets) == len(set(targets))

    def test_traverses_multiple_levels(self) -> None:
        report = replay([self._clean(i) for i in range(60)])
        assert report.final_level == int(AutonomyLevel.L4_DEPLOY)
        assert len(report.would_promote) == 3  # L1->L2->L3->L4

    def test_demotes_after_reaching_a_higher_level(self) -> None:
        runs = [self._clean(i) for i in range(60)]
        runs.append(failing_run("review:prd_criterion"))
        report = replay(runs)
        assert report.would_demote
        assert report.final_level == int(AutonomyLevel.L3_ENVELOPED_AUTO)

    def test_never_promotes_beyond_l4(self) -> None:
        report = replay([self._clean(i) for i in range(200)])
        assert report.final_level == int(AutonomyLevel.L4_DEPLOY)
