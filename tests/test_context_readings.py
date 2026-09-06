"""#247: the record of which phases produced a reading, at the layer the
rule lives in.

``tests/test_context.py`` holds the one case the issue names. This file
holds the cases that decide whether the record is SAFE: that a stale
record retires nothing, that a crashed sensor still beats it, that it
survives the process boundary it is built to cross, and that a context
written before it existed reads back as the old rule rather than as
nonsense. It is a separate file because ``tests/test_context.py`` is at
687 lines against an 800-line ratchet.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from itertools import product

import pytest

from kstrl.context import (
    PHASE_RANK,
    SKIPPABLE_PHASES,
    FailureEntry,
    IterationContext,
    PhaseReading,
)
from tests.test_context import NOT_REMEASURED, RESOLVED, section


class TestReadingsRetireOnlyWhatTheyMeasured:
    def test_a_reading_from_an_older_attempt_retires_nothing(self) -> None:
        """The record is attempt-scoped, which is what makes carrying it
        forward safe.

        ``_with_phase_readings`` in the pipeline merges every reading it
        holds into the context at whichever gate fails, and the context
        then travels to the next attempt with all of them. A reading
        from attempt 1 must not retire anything when attempt 2 is the
        latest evidence: the reviewer's attempt-1 verdict is exactly the
        finding under discussion, not a re-measurement of it.
        """
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_review_finding("sql injection", attempt=2, phase="security")
        ctx.add_phase_reading("review", attempt=1)

        text = ctx.format_for_prompt()
        assert "criterion X" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_a_reading_never_beats_the_crashed_sensor_rule(self) -> None:
        """Branch ORDER in ``_buckets``, pinned.

        Attempt 2's review entry is an infrastructure failure, so the
        ``rank == q`` branch refuses to retire attempt 1's finding. That
        branch sits above the skippable branch, so it decides first even
        with a reading recorded for review at attempt 2. Reordering the
        two would let a crashed reviewer retire a live finding by way of
        a record that should never have been written for it in the first
        place, and this is the assertion that fails if someone does.
        """
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_review_finding(
            "reviewer crashed",
            attempt=2,
            phase="review",
            infrastructure=True,
        )
        ctx.add_phase_reading("review", attempt=2)

        text = ctx.format_for_prompt()
        assert "criterion X" in section(text, NOT_REMEASURED)
        assert section(text, RESOLVED) == ""

    def test_a_reading_for_an_unknown_phase_is_rejected(self) -> None:
        """Same vocabulary and the same refusal as ``_add``: the only
        strings the record holds are phase names from ``PHASE_RANK``."""
        ctx = IterationContext()
        with pytest.raises(ValueError) as exc:
            ctx.add_phase_reading("distill", attempt=1)
        assert "unknown phase 'distill'" in str(exc.value)

    def test_recording_the_same_reading_twice_is_one_reading(self) -> None:
        """The merge runs once per failing gate and the record is
        carried forward, so a repeated pair arrives by design."""
        ctx = IterationContext()
        ctx.add_phase_reading("review", attempt=2)
        ctx.add_phase_reading("review", attempt=2)
        assert ctx.readings == [PhaseReading(attempt=2, phase="review")]


class TestReadingsCrossTheProcessBoundary:
    def test_readings_survive_the_json_round_trip(self) -> None:
        """The real transport. ``component_contexts`` holds a STRING
        that crosses the ProcessPoolExecutor boundary and is re-parsed in
        the worker, and ``_with_phase_readings`` itself goes through
        ``from_json``/``to_json``. A record that does not serialise is a
        record that does not exist.
        """
        ctx = IterationContext()
        ctx.add_review_finding("criterion X", attempt=1, phase="review")
        ctx.add_review_finding("sql injection", attempt=2, phase="security")
        ctx.add_phase_reading("review", attempt=2)

        back = IterationContext.from_json(ctx.to_json())
        assert back.readings == [PhaseReading(attempt=2, phase="review")]
        assert back.format_for_prompt() == ctx.format_for_prompt()
        assert "criterion X" not in back.format_for_prompt()

    def test_a_context_written_before_readings_existed_reads_back_clean(
        self,
    ) -> None:
        """An absent key is no readings, which is the pre-#247 rule.

        Both older shapes are covered: the current entries shape written
        by a parent process that predates this field, and the pre-R10.2
        three-list shape. Degrading to showing the finding is the safe
        direction; degrading to dropping it is the failure this default
        is chosen to avoid.
        """
        current_shape = json.dumps(
            {
                "records": [],
                "entries": [
                    {
                        "attempt": 1,
                        "phase": "review",
                        "text": "criterion X",
                        "infrastructure": False,
                    },
                    {
                        "attempt": 2,
                        "phase": "security",
                        "text": "sql injection",
                        "infrastructure": False,
                    },
                ],
            }
        )
        ctx = IterationContext.from_json(current_shape)
        assert ctx.readings == []
        assert "criterion X" in section(ctx.format_for_prompt(), NOT_REMEASURED)

        legacy_shape = json.dumps(
            {
                "records": [],
                "review_findings": ["criterion X"],
                "verification_failures": [],
                "contract_failures": [],
            }
        )
        legacy = IterationContext.from_json(legacy_shape)
        assert legacy.readings == []
        assert "criterion X" in section(legacy.format_for_prompt(), NOT_REMEASURED)


class TestBucketRuleSweepWithReadings:
    """The same 5600-case product as ``TestBucketRuleSweep``, run with a
    reading recorded for every skippable phase at attempt ``n``.

    ``TestBucketRuleSweep`` is the control: it records nothing, so it
    proves the rule is byte-identical when the record is empty. This is
    the other half, asserting the amended rule over the whole space
    rather than at the one case the issue names.
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
        for phase in sorted(SKIPPABLE_PHASES):
            ctx.add_phase_reading(phase, attempt=len(sequence))
        return ctx

    def sequences(self) -> Iterator[tuple[str, ...]]:
        for length in range(1, 5):
            yield from product(PHASE_RANK, repeat=length)

    def check_one_partition(self, sequence: tuple[str, ...], legacy: bool) -> None:
        ctx = self.build(sequence, legacy)
        b = ctx._buckets()
        total = len(b.current) + len(b.not_remeasured) + len(b.resolved)
        assert total == len(ctx.entries)

        n = len(sequence)
        q = PHASE_RANK[sequence[-1]]
        assert [e.attempt for e in b.current] == [n]
        for e in b.not_remeasured:
            # Every skippable phase has a reading at n, so the only
            # dated survivors are the ones ranked ABOVE q, which no
            # phase ran past. The crashed-sensor rule holds nothing
            # back here: this sweep records no infrastructure entries.
            assert e.attempt == 0 or (e.attempt < n and PHASE_RANK[e.phase] > q)
        for e in b.resolved:
            assert 0 < e.attempt < n
            assert PHASE_RANK[e.phase] <= q

    def test_every_entry_lands_in_exactly_one_bucket(self) -> None:
        cases = 0
        for sequence in self.sequences():
            for legacy in (False, True):
                self.check_one_partition(sequence, legacy)
                cases += 1
        assert cases == 5600

    def check_one_difference(self, sequence: tuple[str, ...]) -> int:
        """How many entries the record moved into ``resolved`` for one
        sequence, having checked that each is one it may move."""
        with_readings = self.build(sequence, legacy=False)
        without = IterationContext(
            records=list(with_readings.records),
            entries=list(with_readings.entries),
        )
        q = PHASE_RANK[sequence[-1]]
        before = {e.text for e in without._buckets().resolved}
        after = {e.text for e in with_readings._buckets().resolved}
        assert before <= after
        for text in after - before:
            entry = next(e for e in with_readings.entries if e.text == text)
            assert entry.phase in SKIPPABLE_PHASES
            assert PHASE_RANK[entry.phase] < q
            assert entry.attempt < len(sequence)
        return len(after - before)

    def test_a_reading_moves_exactly_the_entries_it_should(self) -> None:
        """Diff the two sweeps rather than restating either.

        The entries that change bucket between ``TestBucketRuleSweep``'s
        contexts and these are exactly the skippable-phase entries
        ranked strictly below ``q``. Asserting the difference is what
        makes this a statement about the record's effect rather than a
        second copy of the rule.
        """
        moved = sum(self.check_one_difference(seq) for seq in self.sequences())
        assert moved > 0
