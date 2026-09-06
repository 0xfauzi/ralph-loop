"""Tests for context module."""

from __future__ import annotations

import json
from itertools import product

from kstrl.context import (
    PHASE_RANK,
    SKIPPABLE_PHASES,
    FailureEntry,
    IterationContext,
    IterationRecord,
)

CURRENT = "## Current failures"
NOT_REMEASURED = "## Not re-measured"
RESOLVED = "## Resolved or superseded"
HISTORY = "## Attempt history"


def section(text: str, heading: str) -> str:
    """The body under ``heading``, or "" when the section is absent.

    A section runs from its heading to the next heading, the closing
    instruction, or the end marker. Test failure texts are single-line,
    so no body line here is blank.
    """
    terminators = ("## ", "=== END", "Fix the current failures.")
    body: list[str] = []
    capturing = False
    for line in text.splitlines():
        if line.startswith(heading):
            capturing = True
            continue
        if capturing:
            if line.startswith(terminators):
                break
            body.append(line)
    if not capturing:
        return ""
    return "\n".join(body).strip()


class TestIterationContext:
    def test_empty_context(self) -> None:
        ctx = IterationContext()
        assert ctx.records == []
        assert ctx.review_findings == []

    def test_add_iteration(self) -> None:
        ctx = IterationContext()
        ctx.add_iteration(IterationRecord(iteration=1, success=False, error="tests failed"))
        assert len(ctx.records) == 1
        assert ctx.records[0].error == "tests failed"

    def test_add_review_finding(self) -> None:
        ctx = IterationContext()
        ctx.add_review_finding(
            "US-001: missing index",
            attempt=1,
            phase="review",
        )
        assert ctx.review_findings == ["US-001: missing index"]

    def test_add_empty_string_ignored(self) -> None:
        ctx = IterationContext()
        ctx.add_review_finding("", attempt=1, phase="review")
        ctx.add_verification_failure("", attempt=1)
        ctx.add_contract_failure("", attempt=1)
        assert ctx.review_findings == []
        assert ctx.verification_failures == []
        assert ctx.contract_failures == []
        assert ctx.entries == []

    def test_unknown_phase_rejected(self) -> None:
        ctx = IterationContext()
        try:
            ctx.add_review_finding("x", attempt=1, phase="distill")
        except ValueError as exc:
            assert "distill" in str(exc)
        else:  # pragma: no cover - the raise above is the contract
            raise AssertionError("an unranked phase must not be accepted")

    def test_format_for_prompt_empty(self) -> None:
        ctx = IterationContext()
        text = ctx.format_for_prompt()
        assert "PREVIOUS ATTEMPT CONTEXT" in text
        assert "Attempt 1" in text

    def test_format_for_prompt_with_failures(self) -> None:
        ctx = IterationContext()
        ctx.add_iteration(IterationRecord(1, False, "tests failed", attempt=1))
        ctx.add_verification_failure(
            "- check_test_suite: FAIL - 2 errors",
            attempt=1,
        )
        ctx.add_review_finding(
            "- US-001: FAIL - missing index",
            attempt=1,
            phase="review",
        )
        ctx.add_contract_failure(
            "- Integration test failed after merging api component",
            attempt=1,
        )

        text = ctx.format_for_prompt()
        # One attempt, so every failure is current and nothing is stale.
        assert HISTORY in text
        assert "check_test_suite: FAIL" in section(text, CURRENT)
        assert "US-001: FAIL" in section(text, CURRENT)
        assert "Integration test failed" in section(text, CURRENT)
        assert section(text, NOT_REMEASURED) == ""
        assert section(text, RESOLVED) == ""
        # Attempt 1 produced dated entries, so the history names it
        # without repeating the failure text through the record's error.
        assert section(text, HISTORY) == "- Attempt 1: FAILED"
        assert "tests failed" not in text
        # The gate named in the heading is the highest-ranked one that
        # produced an entry this attempt.
        assert "measured in attempt 1, contract" in text

    def test_history_keeps_the_error_when_the_attempt_had_no_entry(self) -> None:
        """A plain engineer-loop failure records no dated entry, so the
        record's error is the only account of that attempt and stays."""
        ctx = IterationContext()
        ctx.add_iteration(
            IterationRecord(
                3,
                False,
                "agent stalled: no file changed",
                attempt=1,
            )
        )
        ctx.add_verification_failure("linter: E501", attempt=2)

        text = ctx.format_for_prompt()
        assert section(text, HISTORY) == ("- Attempt 1: FAILED - agent stalled: no file changed")
        assert "E501" in section(text, CURRENT)

    def test_format_attempt_number_increments(self) -> None:
        ctx = IterationContext()
        ctx.add_iteration(IterationRecord(1, False, "fail"))
        ctx.add_iteration(IterationRecord(2, False, "fail again"))
        text = ctx.format_for_prompt()
        assert "Attempt 3" in text  # next attempt after 2 records

    def test_json_roundtrip(self) -> None:
        ctx = IterationContext()
        ctx.add_iteration(IterationRecord(1, False, "err", "summary", attempt=1))
        ctx.add_review_finding("finding-1", attempt=1, phase="review")
        ctx.add_verification_failure("check failed", attempt=1)
        ctx.add_contract_failure("integration broke", attempt=1)

        json_str = ctx.to_json()
        restored = IterationContext.from_json(json_str)

        assert len(restored.records) == 1
        assert restored.records[0].iteration == 1
        assert restored.records[0].error == "err"
        assert restored.records[0].summary == "summary"
        assert restored.records[0].attempt == 1
        assert restored.review_findings == ["finding-1"]
        assert restored.verification_failures == ["check failed"]
        assert restored.contract_failures == ["integration broke"]

    def test_from_json_empty(self) -> None:
        ctx = IterationContext.from_json("")
        assert ctx.records == []

    def test_from_json_empty_object(self) -> None:
        ctx = IterationContext.from_json("{}")
        assert ctx.records == []

    # R10.2 (#223): the retry context is level-triggered.

    def test_resolved_when_later_phase_fails(self) -> None:
        """Attempt 2 got past verification, so attempt 1's linter failure
        is not shown again."""
        ctx = IterationContext()
        ctx.add_verification_failure("linter: E501", attempt=1)
        ctx.add_review_finding("criterion X", attempt=2, phase="review")

        text = ctx.format_for_prompt()
        assert "criterion X" in section(text, CURRENT)
        assert "E501" not in text
        assert section(text, NOT_REMEASURED) == ""
        assert "1 earlier finding(s) from verification" in section(text, RESOLVED)

    def test_not_remeasured_when_earlier_phase_fails(self) -> None:
        """Attempt 2 stopped at verification, so the reviewer never ran
        and attempt 1's finding is of unknown status, not resolved."""
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_verification_failure("typecheck: arg-type", attempt=2)

        text = ctx.format_for_prompt()
        assert "arg-type" in section(text, CURRENT)
        assert "criterion X" not in section(text, CURRENT)
        assert "## Not re-measured since attempt 1" in text
        assert "criterion X" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_same_phase_supersedes(self) -> None:
        ctx = IterationContext()
        ctx.add_verification_failure("A", attempt=1)
        ctx.add_verification_failure("B", attempt=2)

        text = ctx.format_for_prompt()
        assert section(text, CURRENT) == "B"
        assert section(text, NOT_REMEASURED) == ""
        assert "1 earlier finding(s) from verification" in section(text, RESOLVED)

    def test_three_attempts_only_latest_is_current(self) -> None:
        ctx = IterationContext()
        for attempt, text in enumerate(("alpha", "beta", "gamma"), start=1):
            ctx.add_verification_failure(text, attempt=attempt)

        rendered = ctx.format_for_prompt()
        assert section(rendered, CURRENT) == "gamma"
        assert "alpha" not in rendered
        assert "beta" not in rendered
        assert "2 earlier finding(s) from verification" in section(rendered, RESOLVED)

        # The current section does not grow with the attempt count: it is
        # identical to what attempt 3's failure alone would render.
        alone = IterationContext()
        alone.add_verification_failure("gamma", attempt=3)
        assert section(rendered, CURRENT) == section(
            alone.format_for_prompt(),
            CURRENT,
        )

    def test_json_round_trip_preserves_attempt_and_phase(self) -> None:
        ctx = IterationContext()
        ctx.add_verification_failure("E501", attempt=1)
        ctx.add_review_finding("criterion X", attempt=2, phase="review")
        ctx.add_review_finding("sql injection", attempt=2, phase="security")
        ctx.add_contract_failure("tier 0 broke", attempt=3)

        restored = IterationContext.from_json(ctx.to_json())
        assert restored.entries == ctx.entries
        assert [(e.attempt, e.phase) for e in restored.entries] == [
            (1, "verification"),
            (2, "review"),
            (2, "security"),
            (3, "contract"),
        ]
        # The derived views still split the entries the pre-R10.2 way.
        assert restored.review_findings == ["criterion X", "sql injection"]

    def test_legacy_json_loads_as_not_remeasured(self) -> None:
        """A context serialised before entries existed still loads, and its
        findings render as un-re-measured whatever the latest failure is.

        The rank rule alone would file the legacy review finding under
        Resolved for the review, security and contract sub-cases, because
        it infers "that phase ran and passed" from the entry being older.
        An entry of unknown age supports no such inference, so
        ``_buckets`` special-cases attempt 0 ahead of the rank comparison.
        """
        legacy = json.dumps(
            {
                "records": [],
                "review_findings": ["old"],
                "verification_failures": [],
                "contract_failures": [],
            }
        )

        bare = IterationContext.from_json(legacy)
        assert [(e.attempt, e.phase, e.text) for e in bare.entries] == [
            (0, "review", "old"),
        ]
        assert "old" in section(bare.format_for_prompt(), NOT_REMEASURED)

        for phase in PHASE_RANK:
            ctx = IterationContext.from_json(legacy)
            ctx.add_review_finding("fresh", attempt=1, phase=phase)
            text = ctx.format_for_prompt()
            assert "old" in section(text, NOT_REMEASURED), phase
            assert "old" not in section(text, RESOLVED), phase
            assert "fresh" in section(text, CURRENT), phase
            assert "(attempt unknown, review) old" in text, phase

    def test_legacy_record_history_falls_back_to_position(self) -> None:
        legacy = json.dumps(
            {
                "records": [
                    {"iteration": 7, "success": False, "error": "boom"},
                ],
                "review_findings": [],
                "verification_failures": [],
                "contract_failures": [],
            }
        )
        ctx = IterationContext.from_json(legacy)
        assert ctx.records[0].attempt == 0
        # Position stands in for the missing attempt number, and the
        # engineer-loop iteration count (7) is not passed off as one.
        assert "- Attempt 1: FAILED - boom" in section(
            ctx.format_for_prompt(),
            HISTORY,
        )

    def test_skippable_phase_is_not_retired_by_a_later_gate(self) -> None:
        """A review finding survives a later contract failure.

        Ranking review below contract would normally mean "review ran in
        attempt 2 and passed". It does not: an ADVISORY `_phase_review`
        downgrades to SKIP when the adversarial LLM budget runs out and
        lets the component proceed to contract testing, so the reviewer
        may never have seen attempt 2 at all. Hard mode halts instead
        since #226, which removes one cause of this and leaves the
        advisory downgrade and an explicit `review_mode = "skip"`.
        """
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_contract_failure("tier 0 broke", attempt=2)

        text = ctx.format_for_prompt()
        assert "tier 0 broke" in section(text, CURRENT)
        assert "criterion X" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_skippable_phase_is_still_superseded_by_itself(self) -> None:
        """Retiring a review finding because review ran AGAIN is observed,
        not inferred, so the skippable rule does not block it."""
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_review_finding("criterion Y", attempt=2, phase="review")

        text = ctx.format_for_prompt()
        assert section(text, CURRENT) == "criterion Y"
        assert "criterion X" not in text
        assert "1 earlier finding(s) from review" in section(text, RESOLVED)

    def test_security_is_skippable_too(self) -> None:
        ctx = IterationContext()
        ctx.add_review_finding("sql injection", attempt=1, phase="security")
        ctx.add_contract_failure("tier 0 broke", attempt=2)
        assert "sql injection" in section(
            ctx.format_for_prompt(),
            NOT_REMEASURED,
        )

    def test_engineer_only_attempt_retires_nothing(self) -> None:
        """An attempt that never got past the engineer loop records an
        IterationRecord and no entry. No sensor ran, so nothing earlier
        may be retired, and nothing earlier may be shown as current."""
        ctx = IterationContext()
        ctx.add_verification_failure("linter: E501", attempt=1)
        ctx.add_iteration(
            IterationRecord(
                4,
                False,
                "agent stalled: no file changed",
                attempt=2,
            )
        )

        text = ctx.format_for_prompt()
        assert "Attempt 3" in text
        assert section(text, CURRENT) == ""
        assert "E501" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""
        assert "agent stalled" in section(text, HISTORY)

    def test_diff_failure_is_not_superseded_by_verification(self) -> None:
        """The diff fetch is its own sensor. Verification failing in
        attempt 2 means the diff was never fetched again, so the old diff
        failure is un-re-measured rather than superseded."""
        ctx = IterationContext()
        ctx.add_verification_failure(
            "git diff against main failed: boom",
            attempt=1,
            phase="diff",
        )
        ctx.add_verification_failure("linter: E501", attempt=2)

        text = ctx.format_for_prompt()
        assert "E501" in section(text, CURRENT)
        assert "git diff against main failed" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""
        # It still reads back through the pre-R10.2 view it used to fill.
        assert ctx.verification_failures == [
            "git diff against main failed: boom",
            "linter: E501",
        ]

    def test_diff_failure_is_retired_once_review_runs(self) -> None:
        """Review failing in attempt 2 does prove the diff was fetched:
        review cannot run without it, and diff is not skippable."""
        ctx = IterationContext()
        ctx.add_verification_failure(
            "git diff failed: boom",
            attempt=1,
            phase="diff",
        )
        ctx.add_review_finding("criterion X", attempt=2, phase="review")

        text = ctx.format_for_prompt()
        assert "boom" not in text
        assert "1 earlier finding(s) from diff" in section(text, RESOLVED)

    def test_in_loop_guard_does_not_retire_a_phase_one_failure(self) -> None:
        """The in-loop diff-scope guard fires inside the engineer loop,
        before Phase 1 runs. Its text matches Phase 1's diff_scope token
        on purpose, but it must not rank as verification: Phase 1
        produced no reading in that attempt, so an earlier attempt's test
        failure is un-re-measured, not superseded."""
        ctx = IterationContext()
        ctx.add_verification_failure("- test_suite: FAIL - 2 errors", attempt=1)
        ctx.add_engineer_failure("diff_scope: FAIL - evil.txt", attempt=2)

        text = ctx.format_for_prompt()
        assert "diff_scope: FAIL" in section(text, CURRENT)
        assert "test_suite: FAIL" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_phase_one_does_retire_an_earlier_guard_trip(self) -> None:
        """The reverse holds: Phase 1 running at all proves the engineer
        loop finished, so the guard cannot have fired."""
        ctx = IterationContext()
        ctx.add_engineer_failure("diff_scope: FAIL - evil.txt", attempt=1)
        ctx.add_verification_failure("- linter: FAIL - E501", attempt=2)

        text = ctx.format_for_prompt()
        assert "evil.txt" not in text
        assert "1 earlier finding(s) from engineer" in section(text, RESOLVED)

    def test_unsplittable_diff_does_not_retire_a_review_finding(self) -> None:
        """Failing to split an oversized diff happens while preparing the
        diff FOR the reviewer, so the reviewer never ran."""
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_verification_failure(
            "The diff is too large to review",
            attempt=2,
            phase="diff",
        )

        text = ctx.format_for_prompt()
        assert "too large to review" in section(text, CURRENT)
        assert "criterion X" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_engineer_failure_helper_ranks_as_engineer(self) -> None:
        ctx = IterationContext()
        ctx.add_engineer_failure("guard tripped", attempt=1)
        assert ctx.entries[0].phase == "engineer"
        assert ctx.review_findings == ["guard tripped"]

    def test_cleared_skippable_finding_is_shown_not_dropped(self) -> None:
        """The accepted cost of the skippable rule, pinned so it cannot
        drift in silence.

        Attempt 2 passes review and fails security. A passing review
        records no entry, so attempt 1's review finding still renders as
        un-re-measured even though the reviewer cleared it. That is a
        bounded over-show, flagged to the agent as needing a re-check;
        the alternative is dropping a live finding when the budget
        skipped review instead. Issue #247 removes it by recording which
        phases ran.
        """
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_review_finding("sql injection", attempt=2, phase="security")

        text = ctx.format_for_prompt()
        assert "sql injection" in section(text, CURRENT)
        assert "criterion X" in section(text, NOT_REMEASURED)
        assert "do not assume they still apply" in text

    def test_derived_views_group_by_sensor_phase(self) -> None:
        """The two texts that moved view when they were re-ranked."""
        ctx = IterationContext()
        ctx.add_engineer_failure("diff_scope: FAIL - evil.txt", attempt=1)
        ctx.add_verification_failure(
            "The diff is too large to review",
            attempt=1,
            phase="diff",
        )
        # Pre-R10.2 these were the other way round: the guard called
        # add_verification_failure and the unsplittable diff called
        # add_review_finding.
        assert ctx.review_findings == ["diff_scope: FAIL - evil.txt"]
        assert ctx.verification_failures == ["The diff is too large to review"]

    def test_crashed_sensor_does_not_supersede_a_real_finding(self) -> None:
        """A security review that CRASHED in attempt 2 is not a reading of
        attempt 2, so attempt 1's high finding survives.

        Without this, a crash would retire the finding, and if the
        adversarial budget were then exhausted the security phase would
        skip and attempt 3 would never see the vulnerability at all.
        """
        ctx = IterationContext()
        ctx.add_review_finding("sql injection in /users", attempt=1, phase="security")
        ctx.add_review_finding(
            "Security review infrastructure error: model timed out",
            attempt=2,
            phase="security",
            infrastructure=True,
        )

        text = ctx.format_for_prompt()
        assert "infrastructure error" in section(text, CURRENT)
        assert "sql injection in /users" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_crashed_sensor_still_retires_lower_ranked_phases(self) -> None:
        """Reaching the security phase at all proves verification ran and
        passed, crash or no crash, so a verification finding is retired."""
        ctx = IterationContext()
        ctx.add_verification_failure("linter: E501", attempt=1)
        ctx.add_review_finding(
            "Security review crashed",
            attempt=2,
            phase="security",
            infrastructure=True,
        )

        text = ctx.format_for_prompt()
        assert "E501" not in text
        assert "1 earlier finding(s) from verification" in section(text, RESOLVED)

    def test_a_real_reading_still_supersedes_after_a_crash(self) -> None:
        """The flag is per entry, not per phase: attempt 3 measuring for
        real retires both the attempt-1 finding and the attempt-2 crash."""
        ctx = IterationContext()
        ctx.add_review_finding("sql injection", attempt=1, phase="security")
        ctx.add_review_finding(
            "crashed",
            attempt=2,
            phase="security",
            infrastructure=True,
        )
        ctx.add_review_finding("xss in /search", attempt=3, phase="security")

        text = ctx.format_for_prompt()
        assert section(text, CURRENT) == "xss in /search"
        assert "sql injection" not in text
        assert "2 earlier finding(s) from security" in section(text, RESOLVED)

    def test_infrastructure_flag_survives_the_json_round_trip(self) -> None:
        ctx = IterationContext()
        ctx.add_verification_failure(
            "git diff failed",
            attempt=1,
            phase="diff",
            infrastructure=True,
        )
        ctx.add_verification_failure("linter: E501", attempt=1)
        restored = IterationContext.from_json(ctx.to_json())
        assert [e.infrastructure for e in restored.entries] == [True, False]
        # Entries written before the flag existed default to measured.
        legacy = IterationContext.from_json(
            '{"records": [], "entries": [{"attempt": 1, "phase": "review", "text": "x"}]}'
        )
        assert legacy.entries[0].infrastructure is False

    def test_engineer_crash_does_not_retire_a_checkpoint_request(self) -> None:
        """A human asked for changes at the checkpoint in attempt 1, and
        attempt 2's engineer loop died before reaching any gate. The
        operator's direction is still outstanding.

        At the engineer rank both entries would sit at rank 0 and the
        second would supersede the first, dropping the human's request.
        """
        ctx = IterationContext()
        ctx.add_checkpoint_request(
            "Human reviewer requested changes at PR checkpoint",
            attempt=1,
        )
        ctx.add_engineer_failure("agent exited non-zero", attempt=2)

        text = ctx.format_for_prompt()
        assert "agent exited non-zero" in section(text, CURRENT)
        assert "Human reviewer requested changes" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_checkpoint_request_retires_what_it_got_past(self) -> None:
        """Reaching the checkpoint proves every gate passed."""
        ctx = IterationContext()
        ctx.add_verification_failure("linter: E501", attempt=1)
        ctx.add_checkpoint_request("please rename the endpoint", attempt=2)

        text = ctx.format_for_prompt()
        assert "please rename the endpoint" in section(text, CURRENT)
        assert "E501" not in text
        assert "1 earlier finding(s) from verification" in section(text, RESOLVED)
        assert ctx.review_findings == ["please rename the endpoint"]

    def test_closing_instruction_changed(self) -> None:
        ctx = IterationContext()
        ctx.add_verification_failure("E501", attempt=1)
        text = ctx.format_for_prompt()
        assert "Fix ALL issues listed above" not in text
        assert text.endswith(
            "Fix the current failures. Re-check the not-re-measured items "
            "yourself; do not assume they still apply.\n"
            "=== END PREVIOUS CONTEXT ==="
        )

    def test_derived_views_still_work(self) -> None:
        ctx = IterationContext()
        ctx.add_review_finding("guard tripped", attempt=1, phase="engineer")
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_review_finding("sql injection", attempt=1, phase="security")
        ctx.add_verification_failure("E501", attempt=1)
        ctx.add_contract_failure("tier 0 broke", attempt=1)

        # The pre-R10.2 lists: engineer, review and security all landed in
        # review_findings.
        assert ctx.review_findings == [
            "guard tripped",
            "criterion X",
            "sql injection",
        ]
        assert ctx.verification_failures == ["E501"]
        assert ctx.contract_failures == ["tier 0 broke"]


class TestBucketRuleSweep:
    """Every failure sequence of one to four attempts over the seven
    phases, with and without a legacy entry: 5600 cases.

    The invariants are the ones the widget behind issue #223
    (``docs/lessons/verify/pr-221/retry_context_rank.py``) established
    before the rule was written into the codebase; this re-establishes
    them against the shipped implementation rather than the model.
    """

    def build(self, sequence: tuple[str, ...], legacy: bool) -> IterationContext:
        ctx = IterationContext()
        if legacy:
            ctx.entries.append(FailureEntry(0, "review", "legacy"))
        for attempt, phase in enumerate(sequence, start=1):
            ctx.add_review_finding(
                f"{phase}-{attempt}",
                attempt=attempt,
                phase=phase,
            )
        return ctx

    def test_every_entry_lands_in_exactly_one_bucket(self) -> None:
        cases = 0
        for length in range(1, 5):
            for sequence in product(PHASE_RANK, repeat=length):
                for legacy in (False, True):
                    ctx = self.build(sequence, legacy)
                    b = ctx._buckets()
                    total = len(b.current) + len(b.not_remeasured) + len(b.resolved)
                    assert total == len(ctx.entries)

                    n = length
                    q = PHASE_RANK[sequence[-1]]
                    assert [e.attempt for e in b.current] == [n]
                    for e in b.not_remeasured:
                        rank = PHASE_RANK[e.phase]
                        assert e.attempt == 0 or (
                            e.attempt < n
                            and (rank > q or (rank < q and e.phase in SKIPPABLE_PHASES))
                        )
                    for e in b.resolved:
                        rank = PHASE_RANK[e.phase]
                        assert 0 < e.attempt < n
                        # Retired only when observed (same phase re-ran)
                        # or safely inferred (a phase that always runs).
                        assert rank == q or (rank < q and e.phase not in SKIPPABLE_PHASES)
                    cases += 1
        assert cases == 5600

    def test_current_section_does_not_grow_with_repeated_failures(self) -> None:
        sizes = []
        for k in range(1, 5):
            ctx = self.build(("verification",) * k, legacy=False)
            sizes.append(len(ctx._buckets().current))
        assert sizes == [1, 1, 1, 1]

    def test_legacy_entry_never_resolves(self) -> None:
        for length in range(1, 5):
            for sequence in product(PHASE_RANK, repeat=length):
                ctx = self.build(sequence, legacy=True)
                assert all(e.attempt != 0 for e in ctx._buckets().resolved)
