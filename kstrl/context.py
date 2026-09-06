"""Context accumulation for retry prompts.

R10.2 (issue #223) makes this level-triggered. An edge-triggered system
acts on transitions ("this failure happened"); a level-triggered one acts
on current state ("this failure is happening now"). Before R10.2 the
object was an integrator with no discharge: three append-only lists, no
clear and no expiry, re-rendered in full on every retry under the line
"Fix ALL issues listed above before completing." An agent on attempt 3
was therefore handed attempt 1's failures - which it may well have fixed
on attempt 2 - unmarked as fixed, and told to fix them.

Each failure now carries the attempt it was measured in and the phase
that measured it, and the renderer sorts them into three buckets. See
``_buckets`` for the rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# The phases run in this fixed order inside one attempt. The rank is
# what lets the renderer tell "this sensor ran again in the latest
# attempt, so the older reading is stale" from "this sensor never ran in
# the latest attempt, so its older reading still stands".
#
# "engineer" covers what the engineer loop raises before Phase 1
# measures anything: guard violations that abort the loop, breaker trips,
# and the loop's own failures. "pr" is the far end, after every sensor
# has passed: a human asking for changes at the E6 checkpoint. It ranks
# above contract because reaching the checkpoint proves every gate
# passed, and because an operator's direction must not be retired by an
# engineer-loop failure in the next attempt (which the engineer rank
# would have allowed: two entries at the same rank supersede).
PHASE_RANK: dict[str, int] = {
    "engineer": 0,
    "verification": 1,
    "diff": 2,
    "review": 3,
    "security": 4,
    "contract": 5,
    "pr": 6,
}

#: Phases whose having run in an attempt cannot be inferred from a
#: higher-ranked failure in that attempt.
#:
#: The rank rule reads "an entry ranked below Q means that phase ran in
#: attempt N and passed, or Q would be lower". That holds only for a
#: phase that always runs once its predecessor passes. Review and
#: security do not: in ADVISORY mode `_phase_review` and
#: `_phase_security` downgrade to SKIP when the adversarial LLM budget
#: runs out and let the component carry on, and `review_mode = "skip"`
#: turns the reviewer off outright, so a later contract failure does not
#: prove the reviewer ran. Entries from these phases are only ever
#: retired by a fresh reading from the same phase, which is observed
#: rather than inferred.
#:
#: An entry from one of these phases is therefore retired only by an
#: OBSERVED reading, and there are exactly two: a fresh entry at the
#: same rank in attempt N, or a ``PhaseReading`` recorded because the
#: phase ran in attempt N and returned a verdict. Neither is an
#: inference from a higher-ranked failure.
#:
#: #247 supplied the second route. Before it there was only the first,
#: and a passing phase records no entry, so a reviewer that ran in
#: attempt N and cleared an earlier finding still left that finding
#: under "Not re-measured" and the agent was told to re-check something
#: it had already fixed. The reason that cost was accepted rather than
#: fixed cheaply is that the opposite error - dropping a live finding
#: when the budget had skipped the reviewer - is silent, and the
#: halt-over-heroics doctrine rules it out. So the record has to
#: distinguish "ran and passed" from "did not run", which is why
#: ``PhaseReading`` keys on the phase producing a verdict rather than on
#: the phase being invoked.
#: #226 removed the HARD-mode case: hard mode now halts instead of
#: skipping, so the remaining causes are an advisory budget downgrade
#: and an explicit skip. Both are live, so this set stays.
SKIPPABLE_PHASES: frozenset[str] = frozenset({"review", "security"})

#: Attempt number carried by entries recovered from a context serialised
#: before entries existed. Their real age is unknown.
LEGACY_ATTEMPT = 0

#: Which phases feed each backward-compatible string view.
#:
#: The views group by sensor phase, which is close to but not identical
#: to the list each text landed in before R10.2. Two texts moved,
#: because ranking them by which sensor ran mattered more than the
#: grouping of a shim: the in-loop diff-scope guard (verification ->
#: engineer, so review_findings) and the unsplittable diff (review ->
#: diff, so verification_failures). Nothing in kstrl reads these; the
#: E6 checkpoint screen reads CheckpointContext.review_findings, a
#: different type. They exist so the pre-R10.2 shape still reads back.
_VIEW_PHASES: dict[str, tuple[str, ...]] = {
    "review_findings": ("engineer", "review", "security", "pr"),
    "verification_failures": ("verification", "diff"),
    "contract_failures": ("contract",),
}

#: Phase assigned to each legacy list when reading pre-R10.2 JSON. The
#: three lists cannot distinguish engineer/review/security, so
#: review_findings collapses to "review"; the attempt is unknown either
#: way, which is what actually decides where those entries render.
_LEGACY_LIST_PHASE: dict[str, str] = {
    "review_findings": "review",
    "verification_failures": "verification",
    "contract_failures": "contract",
}


def _require_known_phase(phase: str) -> None:
    """One definition of a phase name this object will accept.

    Entries and readings are joined by this string: an entry is retired
    when a reading names the same phase. Two copies of the check are two
    definitions of the vocabulary, and the weaker one is the one that
    decides.
    """
    if phase not in PHASE_RANK:
        raise ValueError(f"unknown phase {phase!r}; expected one of {sorted(PHASE_RANK)}")


@dataclass
class IterationRecord:
    """Record of a single iteration attempt.

    ``iteration`` is the engineer loop's own iteration counter, which is
    not the attempt number: one attempt runs many iterations. ``attempt``
    carries the attempt number so the history renders honestly; it is
    ``LEGACY_ATTEMPT`` on records deserialised from pre-R10.2 JSON, and
    the renderer then falls back to the record's position.
    """

    iteration: int
    success: bool
    error: str | None = None
    summary: str = ""
    attempt: int = LEGACY_ATTEMPT


@dataclass(frozen=True)
class FailureEntry:
    """One sensor reading: a failure, the attempt it was measured in, and
    the phase that measured it.

    ``infrastructure`` marks an entry that records the sensor FAILING TO
    RUN rather than a measurement: a crashed reviewer, a git diff that
    could not be fetched, a diff that could not be split for review. The
    codebase already draws this line with ``Finding.infrastructure_error``
    (E9), and it matters here for the same reason: a crash is not a fresh
    reading, so it must not supersede an earlier real finding from the
    same phase.
    """

    attempt: int
    phase: str
    text: str
    infrastructure: bool = False


@dataclass(frozen=True)
class PhaseReading:
    """Positive evidence that a phase RAN in an attempt and returned a
    verdict about the change, whether that verdict was pass or fail.

    The counterpart to ``FailureEntry``, which records only what went
    wrong: a phase that passes writes no entry, so without this there is
    no difference in the context between a reviewer that looked again
    and cleared a finding and a reviewer that never looked. That is
    issue #247.

    What it is NOT. ``FailureEntry.infrastructure`` draws the same line
    from the failure side:

    - a phase that was SKIPPED produces no reading, so an entry it
      cleared in an earlier attempt is still shown. That is the whole
      reason ``SKIPPABLE_PHASES`` exists and it must not regress.
    - a phase whose sensor CRASHED produces no reading either. An
      advisory-mode reviewer that raises is reported as passing, and
      treating that as evidence would retire a live finding on the
      strength of an exception.

    Only ``SKIPPABLE_PHASES`` phases are ever recorded. For every other
    phase, having run is already inferable from a higher-ranked failure
    in the same attempt, so a record would be redundant and would give
    the rank rule a second, overlapping source of truth.
    """

    attempt: int
    phase: str


@dataclass(frozen=True)
class _Buckets:
    """The three groups ``format_for_prompt`` renders, plus the latest
    attempt any of them was measured in."""

    current: list[FailureEntry]
    not_remeasured: list[FailureEntry]
    resolved: list[FailureEntry]
    measured_attempt: int


@dataclass
class IterationContext:
    """Accumulated context across retries for a component.

    Serializable to JSON for transport across process boundaries.
    """

    records: list[IterationRecord] = field(default_factory=list)
    entries: list[FailureEntry] = field(default_factory=list)
    readings: set[PhaseReading] = field(default_factory=set)

    # Backward-compatible read-only views. Nothing in kstrl/ reads these
    # any more, but they keep the shape the pre-R10.2 object exposed.
    @property
    def review_findings(self) -> list[str]:
        return self._texts("review_findings")

    @property
    def verification_failures(self) -> list[str]:
        return self._texts("verification_failures")

    @property
    def contract_failures(self) -> list[str]:
        return self._texts("contract_failures")

    def _texts(self, view: str) -> list[str]:
        phases = _VIEW_PHASES[view]
        return [e.text for e in self.entries if e.phase in phases]

    def add_iteration(self, record: IterationRecord) -> None:
        self.records.append(record)

    def add_review_finding(
        self,
        finding: str,
        *,
        attempt: int,
        phase: str,
        infrastructure: bool = False,
    ) -> None:
        """``phase`` is explicit: the review and security call sites both
        route their text through here. Pass ``infrastructure=True`` when
        the reviewer crashed instead of reporting."""
        self._add(finding, attempt, phase, infrastructure)

    def add_engineer_failure(self, failure: str, *, attempt: int) -> None:
        self._add(failure, attempt, "engineer")

    def add_checkpoint_request(self, request: str, *, attempt: int) -> None:
        """A human asking for changes at the E6 checkpoint. Not a sensor
        reading: it is retired only by another checkpoint decision."""
        self._add(request, attempt, "pr")

    def add_verification_failure(
        self,
        failure: str,
        *,
        attempt: int,
        phase: str = "verification",
        infrastructure: bool = False,
    ) -> None:
        """``phase`` is "diff" at the one site where the diff fetch
        fails: that failure used to land in ``verification_failures``,
        and the derived view keeps it there, but it must rank as its own
        sensor or a later verification failure would retire it as though
        the diff had been fetched again. Pass ``infrastructure=True``
        when the phase could not run at all."""
        self._add(failure, attempt, phase, infrastructure)

    def add_contract_failure(self, failure: str, *, attempt: int) -> None:
        self._add(failure, attempt, "contract")

    def add_phase_reading(self, phase: str, *, attempt: int) -> None:
        """Record that ``phase`` ran in ``attempt`` and returned a
        verdict. See ``PhaseReading`` for what does and does not count.

        The merge that writes these runs once per failing gate and the
        record is carried forward across attempts, so the same reading
        arrives more than once by design; ``readings`` is a set, so that
        is a no-op rather than a rule to remember.
        """
        _require_known_phase(phase)
        self.readings.add(PhaseReading(attempt=attempt, phase=phase))

    def _add(
        self,
        text: str,
        attempt: int,
        phase: str,
        infrastructure: bool = False,
    ) -> None:
        if not text:
            return
        _require_known_phase(phase)
        self.entries.append(
            FailureEntry(
                attempt=attempt,
                phase=phase,
                text=text,
                infrastructure=infrastructure,
            )
        )

    def _latest_attempt(self) -> int:
        """The latest attempt any evidence came from.

        Failure entries and records both carry the attempt. Records
        deserialised from pre-R10.2 JSON do not, so their count is the
        floor: at most one record is appended per attempt.
        """
        return max(
            max((e.attempt for e in self.entries), default=0),
            max((r.attempt for r in self.records), default=0),
            len(self.records),
        )

    def _phases_read_in(self, attempt: int) -> set[str]:
        """Phases that ran in ``attempt`` and returned a verdict."""
        return {r.phase for r in self.readings if r.attempt == attempt}

    def _buckets(self) -> _Buckets:
        """Sort the entries into current, not re-measured, and resolved.

        Let ``N`` be the latest attempt any evidence came from and ``Q``
        the rank of the highest-ranked phase with an entry from ``N`` (an
        attempt stops at its first failing gate, so in practice that is
        the gate that fired; the max is the safe general form).

        - attempt ``N``: current, rendered in full.
        - rank above ``Q``: that sensor never ran in attempt ``N``, so
          its reading is un-re-measured, not stale. Rendered in full.
        - rank equal to ``Q``: the same sensor produced a fresh reading
          that supersedes the old one. Observed, so it holds even for a
          skippable phase - but only when attempt ``N``'s entry at that
          rank is a measurement. A sensor that crashed produced no
          reading, so it retires nothing.
        - rank below ``Q``: the phase ran in attempt ``N`` and passed, or
          ``Q`` would be lower. That is an inference, and it is only
          sound for a phase that always runs once its predecessor
          passes, so ``SKIPPABLE_PHASES`` is excluded from it. The
          inference stays barred for those phases; what #247 adds is the
          other route to the same conclusion, which is OBSERVATION: the
          phase recorded a ``PhaseReading`` for attempt ``N`` and no
          entry, so it ran and it passed.

        When attempt ``N`` produced no entry at all, ``Q`` sits below
        every rank and nothing is retired: a plain engineer-loop failure
        and a merge-conflict restart record an ``IterationRecord`` and no
        entry, and no sensor ran in such an attempt.
        """
        current: list[FailureEntry] = []
        not_remeasured: list[FailureEntry] = []
        resolved: list[FailureEntry] = []

        n = self._latest_attempt()
        latest = [e for e in self.entries if e.attempt == n]
        ranks = [PHASE_RANK[e.phase] for e in latest]
        q = max(ranks) if ranks else -1
        # A crashed sensor proves the phases BEFORE it ran (the attempt
        # got that far), but it is not a reading of its own phase, so it
        # cannot supersede an earlier real finding there.
        measured_ranks = {PHASE_RANK[e.phase] for e in latest if not e.infrastructure}
        # The skippable phases whose older entries the rank rule still
        # may not retire. Computed as a set here, and through a helper
        # rather than inline, so the branch below stays a single
        # membership test and this function's cognitive complexity does
        # not rise: it is already at 18 against a gate of 15, and the
        # pre-commit ratchet fails a rise in a function that is over.
        unread_skippable = SKIPPABLE_PHASES - self._phases_read_in(n)

        for entry in self.entries:
            if entry.attempt == LEGACY_ATTEMPT:
                # Special-cased AHEAD of the rank comparison. The rank
                # rule infers "this phase ran in attempt N and passed"
                # from an entry being older than N; an entry of unknown
                # age supports no such inference. Without this branch the
                # rank rule files a legacy review entry under Resolved
                # whenever the latest failure ranks at or above review,
                # silently dropping a finding whose age nobody knows.
                not_remeasured.append(entry)
            elif entry.attempt == n:
                current.append(entry)
            elif PHASE_RANK[entry.phase] > q:
                not_remeasured.append(entry)
            elif PHASE_RANK[entry.phase] == q:
                if q in measured_ranks:
                    resolved.append(entry)
                else:
                    not_remeasured.append(entry)
            elif entry.phase in unread_skippable:
                not_remeasured.append(entry)
            else:
                resolved.append(entry)
        return _Buckets(current, not_remeasured, resolved, n)

    def format_for_prompt(self) -> str:
        """Format accumulated context as text to prepend to the agent prompt."""
        sections: list[str] = []
        latest = self._latest_attempt()
        sections.append(f"=== PREVIOUS ATTEMPT CONTEXT (Attempt {latest + 1}) ===")

        buckets = self._buckets()
        measured = buckets.measured_attempt

        if buckets.current:
            gate = max(
                buckets.current,
                key=lambda e: PHASE_RANK[e.phase],
            ).phase
            sections.append("")
            sections.append(f"## Current failures (measured in attempt {measured}, {gate})")
            sections.extend(e.text for e in buckets.current)

        if buckets.not_remeasured:
            dated = [e.attempt for e in buckets.not_remeasured if e.attempt > LEGACY_ATTEMPT]
            heading = "## Not re-measured"
            if dated:
                heading += f" since attempt {min(dated)}"
            sections.append("")
            sections.append(heading)
            for entry in buckets.not_remeasured:
                label = (
                    "attempt unknown"
                    if entry.attempt == LEGACY_ATTEMPT
                    else f"attempt {entry.attempt}"
                )
                sections.append(f"({label}, {entry.phase}) {entry.text}")

        if buckets.resolved:
            names = ", ".join(
                sorted(
                    {e.phase for e in buckets.resolved},
                    key=lambda p: PHASE_RANK[p],
                )
            )
            sections.append("")
            sections.append("## Resolved or superseded")
            sections.append(
                f"{len(buckets.resolved)} earlier finding(s) from {names} "
                f"passed or were re-measured in attempt {measured} and are "
                f"omitted."
            )

        if self.records:
            measured_attempts = {e.attempt for e in self.entries if e.attempt > LEGACY_ATTEMPT}
            sections.append("")
            sections.append("## Attempt history")
            for position, rec in enumerate(self.records, start=1):
                attempt = rec.attempt if rec.attempt > LEGACY_ATTEMPT else position
                status = "completed" if rec.success else "FAILED"
                line = f"- Attempt {attempt}: {status}"
                # The record's error is the failure text itself on the
                # paths that raise one (the in-loop guard sets it to the
                # scope violation, which is also a dated entry). Printing
                # it here would smuggle a superseded failure back into
                # the prompt through the history, undoing the bucketing.
                # It is the only account of the attempt where no entry
                # was recorded - a plain engineer-loop failure, a
                # merge-conflict restart - so it renders there.
                if rec.error and attempt not in measured_attempts:
                    line += f" - {rec.error}"
                if rec.summary:
                    line += f" ({rec.summary})"
                sections.append(line)

        sections.append("")
        sections.append(
            "Fix the current failures. Re-check the not-re-measured items "
            "yourself; do not assume they still apply."
        )
        sections.append("=== END PREVIOUS CONTEXT ===")

        return "\n".join(sections)

    def to_json(self) -> str:
        """Serialize to JSON string for ProcessPoolExecutor transport."""
        data: dict[str, Any] = {
            "records": [
                {
                    "iteration": r.iteration,
                    "success": r.success,
                    "error": r.error,
                    "summary": r.summary,
                    "attempt": r.attempt,
                }
                for r in self.records
            ],
            "entries": [
                {
                    "attempt": e.attempt,
                    "phase": e.phase,
                    "text": e.text,
                    "infrastructure": e.infrastructure,
                }
                for e in self.entries
            ],
            # Sorted because the source is a set and this string crosses
            # a process boundary: unsorted, the same context would
            # serialise differently under a different PYTHONHASHSEED.
            "readings": [
                {"attempt": attempt, "phase": phase}
                for attempt, phase in sorted((r.attempt, r.phase) for r in self.readings)
            ],
        }
        return json.dumps(data)

    @classmethod
    def from_json(cls, data: str) -> IterationContext:
        """Deserialize from JSON string, in either the current shape or the
        pre-R10.2 shape (three undated string lists)."""
        if not data or data == "{}":
            return cls()
        parsed = json.loads(data)
        ctx = cls()
        for rec_data in parsed.get("records", []):
            ctx.records.append(
                IterationRecord(
                    iteration=rec_data["iteration"],
                    success=rec_data["success"],
                    error=rec_data.get("error"),
                    summary=rec_data.get("summary", ""),
                    attempt=rec_data.get("attempt", LEGACY_ATTEMPT),
                )
            )
        # Placed ahead of the two shape branches so one loop serves both.
        # An absent key means no readings, which is exactly the rule
        # that held before #247: a context written by an older parent
        # process degrades to showing the finding rather than to
        # dropping it.
        for reading_data in parsed.get("readings", []):
            ctx.readings.add(
                PhaseReading(
                    attempt=reading_data["attempt"],
                    phase=reading_data["phase"],
                )
            )
        if "entries" in parsed:
            for entry_data in parsed["entries"]:
                ctx.entries.append(
                    FailureEntry(
                        attempt=entry_data["attempt"],
                        phase=entry_data["phase"],
                        text=entry_data["text"],
                        infrastructure=entry_data.get("infrastructure", False),
                    )
                )
            return ctx
        for list_name, phase in _LEGACY_LIST_PHASE.items():
            for text in parsed.get(list_name, []):
                ctx.entries.append(
                    FailureEntry(
                        attempt=LEGACY_ATTEMPT,
                        phase=phase,
                        text=text,
                    )
                )
        return ctx
