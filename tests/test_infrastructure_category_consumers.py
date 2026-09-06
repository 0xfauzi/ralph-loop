"""The journal's ``infrastructure`` category, and what it costs the ladder.

``evolution._CATEGORY_BY_CHECK`` decides what the journal calls a
failure. ``autonomy_replay.INFRA_FAILURE_PREFIXES`` decides which runs
count as evidence about the factory's judgement. Since #315 the second
is DERIVED from the first, so the table is no longer only a label on a
report: enrolling a check as ``infrastructure`` removes every run that
failure dominates from the autonomy ladder's evidence.

Split out of ``tests/test_check_name_enrolment.py`` on #339, and the
seam is the one ``tests/test_journal_one_writer.py`` documents about its
own split: that file is a static AST guard over ``kstrl/`` with no
journal and no ladder in it, and this one runs the two consumers on real
records and asserts what they do. Different subject, different failure
message, different reason to fail. The 800-line ratchet is what forced
the timing, but the seam was already there.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kstrl import autonomy_replay
from kstrl.autonomy import AutonomyLevel
from kstrl.autonomy_replay import INFRA_FAILURE_PREFIXES, RunRecord, replay
from kstrl.evolution import _CATEGORY_BY_CHECK, INFRASTRUCTURE_CHECKS, signature_for_error
from tests.helpers.replay import clean_run, failing_run, run_record

#: A recorded run whose modal failure was the given signature.
#:
#: Asserting through ``RunRecord`` rather than against
#: ``INFRA_FAILURE_PREFIXES`` on purpose: ``infra_aborted`` is what
#: decides a run's fate, and a test that reads the constant directly
#: would stay green if the property stopped consulting it.
#:
#: An alias rather than a second builder. #339 review found this file
#: and ``tests/helpers/replay.py`` shipping two names for the same
#: record in one change, which is how the five hand-rolled builders that
#: helper replaced got started.
_run_dominated_by = failing_run


def _prefixes_without(prefix: str) -> tuple[str, ...]:
    return tuple(sorted(set(INFRA_FAILURE_PREFIXES) - {prefix}))


def _prefixes_with(prefix: str) -> tuple[str, ...]:
    return tuple(sorted(set(INFRA_FAILURE_PREFIXES) | {prefix}))


class TestTheTwoInfrastructureConsumersAgree:
    """#315: the journal and the autonomy replay both decide what counts
    as infrastructure, and before this they disagreed about
    ``pr:merge-conflict`` - plumbing to the replay, an engineer-loop
    failure to the journal. The replay now derives its prefixes from the
    journal's table, so the shared part cannot drift. What is left is one
    deliberate difference, pinned here so that changing either side
    without the other is a red test rather than a quiet divergence.

    Every assertion goes through ``RunRecord.infra_aborted``, the
    property that actually decides whether a run counts as evidence
    about the factory's judgement. An earlier version of this class
    asserted ``startswith(INFRA_FAILURE_PREFIXES)`` instead, which
    cannot fail while the tuple is built from ``INFRASTRUCTURE_CHECKS``:
    it restated the constructor rather than testing anything.

    ``decisive`` is asserted ONCE, in the test below, and not beside
    every ``infra_aborted``. #339 review measured why: these records all
    have ``completed + failed > 0``, so ``decisive`` reduces to ``not
    infra_aborted`` and a second assertion beside the first is its own
    negation restated. One case establishes that ``decisive`` consults
    the property; the rest would only repeat the signature literal, and
    a typo in the repeat would silently change what it tests."""

    @pytest.mark.parametrize("check", sorted(INFRASTRUCTURE_CHECKS))
    def test_an_infrastructure_check_costs_the_run_its_verdict(self, check: str) -> None:
        run = _run_dominated_by(f"{check}:any-code")
        assert run.infra_aborted, (
            f"{check!r} is 'infrastructure' in the journal but a decisive "
            f"judgement failure to autonomy_replay."
        )
        # The one place `decisive` is asserted next to `infra_aborted`:
        # it is what couples the property to the ladder's evidence, and
        # asserting it again elsewhere would restate this negation.
        assert not run.decisive

    def test_the_replay_treats_exactly_these_signatures_as_plumbing(self) -> None:
        """The contents, not the derivation. The four rows beyond the
        journal's own are the replay asking a WIDER question: not 'which
        part of the factory failed' but 'did this run yield a verdict
        about the factory's judgement at all', which a gate's honest
        verdict can answer with no. Spelled out here so that adding a
        fifth is a visible edit in two files."""
        assert set(INFRA_FAILURE_PREFIXES) == {
            "aborted:",
            "adversarial_budget:",
            "diff:",
            "pr:",
            "provisioning:",
            "token_budget:",
            # Replay-only; see autonomy_replay._REPLAY_ONLY_PREFIXES.
            "scope_unreadable:",
            "git:",
            "infra:",
            "timeout:",
        }

    def test_the_scope_refusal_is_the_deliberate_divergence(self) -> None:
        """``scope_unreadable`` is a Phase 1 gate result, so the journal
        files it under verification (#294 gave it its own gate, table row
        and proposal arm on that basis). It is still an infrastructure
        casualty for the replay: nothing was measured about the change,
        so the run says nothing about judgement. Both halves asserted,
        because the divergence is only defensible while it is on
        purpose."""
        # Not also `"scope_unreadable" not in INFRASTRUCTURE_CHECKS`:
        # that set is comprehended from this table on the category, so
        # the line above already entails it.
        assert _CATEGORY_BY_CHECK["scope_unreadable"] == "verification"
        assert _run_dominated_by("scope_unreadable:no-trustworthy-scope").infra_aborted

    def test_a_judgement_failure_still_counts_as_evidence(self) -> None:
        """The other direction, or the class above would pass with every
        run called plumbing. ``diff:`` must not swallow ``diff_scope:``
        either: a scope violation is a verdict on the change, and the
        colon is the only thing separating the two names."""
        assert not _run_dominated_by("diff_scope:files-outside-allowed-scope").infra_aborted
        assert not _run_dominated_by("review:scope_creep").infra_aborted
        assert not _run_dominated_by("test_suite:assertion-error").infra_aborted

    def test_a_human_rejection_is_a_verdict_not_an_outage(self) -> None:
        """#339 review, P2-1. A person looking at the change and saying
        no is the most decisive evidence about the factory's judgement
        there is. It reached the journal as
        ``pr:rejected-at-hitl-checkpoint`` because the HITL checkpoint
        fails at ``phase="pr"`` and the phase becomes the check name, so
        the ``pr`` row - real push, create and merge plumbing - swallowed
        it and the replay threw the whole run away."""
        assert not _run_dominated_by("review:hitl-rejected").infra_aborted

    def test_asking_for_changes_is_a_verdict_too(self) -> None:
        """#339 A4, the sibling branch 46 lines below the one P2-1
        fixed. ``CheckpointDecision.RETRY`` is a human reading the change
        and asking for different work, which is a judgement about the
        change by the same argument that makes a rejection one."""
        assert not _run_dominated_by("review:hitl-changes-requested").infra_aborted

    def test_the_unanswered_gate_stays_an_outage(self) -> None:
        """The third HITL-adjacent branch, and the one that is NOT a
        verdict: ``PARKED`` means the merge gate could not be put to
        anybody, so nothing was decided about the change and the run says
        nothing about the factory's judgement. It keeps the ``pr:``
        fallback deliberately, which is why the branch above it carries a
        ``signatures=`` and this one carries a comment saying it must
        not.

        Derived through ``signature_for_error`` rather than pasted,
        because the slug is truncated at 63 characters and a pasted copy
        would pin the truncation rather than the check name."""
        parked = signature_for_error("pr", "Parked awaiting merge approval (no interactive UI)")
        assert parked.startswith("pr:")
        assert _CATEGORY_BY_CHECK["pr"] == "infrastructure"
        assert _run_dominated_by(parked).infra_aborted


class TestTheTableMovesTheLadder:
    """#339 review, P2-3. The derivation is a real coupling and it had no
    test: every assertion above stops at ``infra_aborted``, one run at a
    time, and none of them showed the thing that actually changes when
    the journal's table is edited, which is where the autonomy ladder
    ends up.

    Both directions are here because the coupling is NOT monotone, and
    that is the part a reader gets wrong. Enrolling a check as
    infrastructure removes runs from the evidence, and a removed run can
    be a run that would have DEMOTED, so more infrastructure can leave
    the ladder HIGHER. The PR that introduced this measured exactly that
    on one unchanged ``experiments.tsv``, in both directions on different
    data.

    ``monkeypatch.setattr`` on the module global is the technique, and it
    works because ``RunRecord.infra_aborted`` looks the tuple up at call
    time rather than closing over it. Measured before relying on it.
    """

    def test_enrolling_a_check_leaves_the_ladder_higher(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The non-monotone direction. Twelve clean runs promote L1 to
        L2, then one run dies on the token ceiling. Counted as a verdict
        that run is a failure and demotes; counted as plumbing it is
        skipped and the promotion stands."""
        runs = [clean_run(i) for i in range(12)] + [failing_run("token_budget:exhausted")]

        monkeypatch.setattr(
            autonomy_replay, "INFRA_FAILURE_PREFIXES", _prefixes_without("token_budget:")
        )
        before = replay(runs)
        assert before.final_level == int(AutonomyLevel.L1_SUPERVISED)
        assert before.decisive_runs == 13
        assert before.would_demote

        monkeypatch.undo()
        after = replay(runs)
        assert after.final_level == int(AutonomyLevel.L2_GATED_MERGE)
        assert after.decisive_runs == 12
        assert after.infra_aborted_runs == 1
        assert not after.would_demote

    def test_enrolling_a_check_leaves_the_ladder_lower(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The direction a reader expects, on data where the excluded
        runs were the evidence FOR a promotion rather than against one.
        ``sufficient_data`` flips with it, which is what changes
        ``ks autonomy replay``'s exit status from 0 to 2."""
        runs = [
            replace(clean_run(i), common_failure="diff:could-not-fetch-diff") for i in range(12)
        ]

        monkeypatch.setattr(autonomy_replay, "INFRA_FAILURE_PREFIXES", _prefixes_without("diff:"))
        before = replay(runs)
        assert before.final_level == int(AutonomyLevel.L2_GATED_MERGE)
        assert before.sufficient_data

        monkeypatch.undo()
        after = replay(runs)
        assert after.final_level == int(AutonomyLevel.L1_SUPERVISED)
        assert after.decisive_runs == 0
        assert not after.sufficient_data

    def test_one_new_row_in_the_journal_table_is_enough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The warning, as a mechanism. ``test_suite`` is a real gate and
        a real verdict; filing it as infrastructure would be a one-line
        edit to ``_CATEGORY_BY_CHECK`` with no code diff, and it would
        promote the factory a rung on data where the tests were failing.
        That is the direction this coupling is dangerous in, so it is
        asserted rather than described."""
        runs = [clean_run(i) for i in range(12)] + [failing_run("test_suite:assertion-error")]

        assert replay(runs).final_level == int(AutonomyLevel.L1_SUPERVISED)

        monkeypatch.setattr(
            autonomy_replay, "INFRA_FAILURE_PREFIXES", _prefixes_with("test_suite:")
        )
        assert replay(runs).final_level == int(AutonomyLevel.L2_GATED_MERGE)


class TestTheReportCountsOnePopulation:
    """#339 review, P2-2. A Ctrl-C during a run that had already merged
    24 components files `aborted:shutdown`, which #315 made
    infrastructure, so the replay skips the run entirely - and the
    report still printed "Components merged: 24" two lines under
    "infra-aborted: 1 (excluded)". Two populations under one label, and
    the bigger one was the one that counted for nothing."""

    def _ctrl_c_run(self) -> RunRecord:
        return run_record(
            run_id="interrupted",
            timestamp="2026-07-29T00:00:00Z",
            project="p",
            components_total=25,
            completed=24,
            failed=1,
            common_failure="aborted:shutdown",
        )

    def test_the_headline_counts_the_runs_the_ladder_counts(self) -> None:
        report = replay([clean_run(0), self._ctrl_c_run()])
        assert report.infra_aborted_runs == 1
        assert report.components_merged == 1
        assert report.merged_in_excluded_runs == 24

    def test_the_excluded_merges_are_shown_rather_than_dropped(self) -> None:
        """Not counted is not the same as not reported: the total has to
        stay recoverable, or the fix trades one wrong number for a
        missing one."""
        text = replay([clean_run(0), self._ctrl_c_run()]).render()
        assert "Components merged:    1 (in decisive runs)" in text
        assert "in excluded runs:   24 (not counted)" in text

    def test_a_clean_sample_says_nothing_about_exclusions(self) -> None:
        """The line only appears when there is something to disclose."""
        text = replay([clean_run(i) for i in range(3)]).render()
        assert "in excluded runs" not in text
