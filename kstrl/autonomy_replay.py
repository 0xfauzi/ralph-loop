"""R8.2 threshold replay: what WOULD the ladder have done, historically?

The R8 cycle carries one standing rule: **no assumed thresholds**. Every
number in :mod:`kstrl.autonomy` is a placeholder taken from the roadmap
table, and none may gate or demote anything until it has been replayed
against real run data and the would-have-fired count recorded.

This module is that replay. It reads the recorded history
(``.kstrl/experiments.tsv``, written per run) and answers two questions:

1. Is there even enough data to calibrate? A threshold tuned on a handful
   of runs is a guess wearing a number's clothes, so the report leads with
   the sample size and says plainly when it is too small.
2. Replaying the ladder over that history, which transitions would have
   fired, and when?

It deliberately reports rather than decides: nothing here mutates ladder
state. Reading a replay is a human act.

Decisive-run definition (matters, and is the main judgement call here): a
run counts as decisive when it produced a verdict about the factory's
JUDGEMENT. A run that died on a git push or a PR-creation failure is an
infrastructure casualty - it says nothing about whether the factory's
reviews are trustworthy - so it is excluded. Counting those would let a
string of broken runs unlock a promotion, which is exactly backwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kstrl.autonomy import (
    MIN_DECISIVE_RUNS,
    THRESHOLDS,
    AutonomyLevel,
    AutonomyState,
    DemotionTrigger,
)
from kstrl.evolution import INFRASTRUCTURE_CHECKS, experiment_rows
from kstrl.verify import SCOPE_UNREADABLE_CHECK

DEFAULT_EXPERIMENTS_PATH = Path(".kstrl/experiments.tsv")

#: The prefixes this module adds to the ones the journal's own
#: taxonomy supplies. It asks a WIDER question than that taxonomy does:
#: the journal asks which part of the factory produced a failure, this
#: asks whether the run yielded a verdict about the factory's JUDGEMENT
#: at all, and a failure can be a gate's honest verdict and still answer
#: that with no.
#:
#: ONE OF THE FOUR IS LIVE. `scope_unreadable:` is emitted by kstrl and
#: is the reason this constant exists; `git:`, `infra:` and `timeout:`
#: match nothing this code can produce, and the paragraph below says how
#: that was measured. Stated here as well as there because a reader who
#: takes the list at face value inherits the wrong prior about how much
#: of the ladder's evidence these four remove.
#:
#: `scope_unreadable:` (#294) is the harness failing to establish its own
#: input: the component's scope could not be read from the pre-run tree,
#: so nothing was ever measured about the change. The same reasoning that
#: attaches `Finding.infrastructure_error` to that check applies to the
#: signature stream, and it was missed there first: a run dominated by it
#: was counted DECISIVE and fed autonomy promotion and demotion as
#: evidence about the factory's judgement. #294 makes that more likely
#: rather than less, because refusing the component before its engineer
#: runs makes this the modal signature of the run instead of one of
#: three retries. The journal keeps it under `verification` all the same:
#: it IS a Phase 1 gate result, and #294 gave it its own gate, its own
#: table row and its own proposal arm on that basis. That divergence is
#: the point of splitting this constant in two, and
#: `tests/test_infrastructure_category_consumers.py` pins both halves of
#: it, through the property rather than against this tuple.
#:
#: `git:`, `infra:` and `timeout:` name families no version of kstrl has
#: ever emitted: measured with `git log -S`, all three strings entered
#: the tree in 606cbb1, the commit that created this tuple, and no call
#: site produces them. #339 re-measured it four ways and got the same
#: answer: none of the three appears in `kstrl/` as a signature prefix,
#: none is in `_CATEGORY_BY_CHECK`, none is in `_classify_check`'s range,
#: and none is producible by any of the four check-name producers
#: `tests/test_check_name_enrolment.py` enumerates.
#:
#: Kept, and #339 review is why that needs saying carefully: an earlier
#: draft of this comment defended them as protecting historical rows,
#: which cannot be true alongside the sentence above it. If no version
#: ever emitted the strings then no experiments.tsv row contains them,
#: and deleting them would change nothing that exists. They stay because
#: a prefix that matches nothing costs nothing to carry and because
#: removing them is a decision about what this constant is FOR - whether
#: it guards against families kstrl has never written - which is worth
#: making on its own rather than as a tidy-up inside a categorisation
#: fix. They are NOT enrolled in `_CATEGORY_BY_CHECK`: that table
#: describes names kstrl emits, and the enrolment guard would make any
#: real `git:` gate enrol itself on the day it lands.
_REPLAY_ONLY_PREFIXES: tuple[str, ...] = (
    f"{SCOPE_UNREADABLE_CHECK}:",
    "git:",
    "infra:",
    "timeout:",
)

#: `common_failure` prefixes that mark an infrastructure casualty rather
#: than a judgement. Kept as a prefix tuple so a new plumbing failure
#: family is one entry, not a new code path.
#:
#: #315: every check the journal files as `infrastructure` is in here by
#: construction, rather than being retyped. The two consumers disagreed
#: about `pr:merge-conflict` - plumbing to this module, an engineer-loop
#: failure to the journal - and a taxonomy that answers a question twice
#: will eventually answer it two ways. Deriving means enrolling a check
#: as infrastructure in `_CATEGORY_BY_CHECK` reaches both in one edit,
#: and the enrolment guard forces every emitted name through that table.
#: `_REPLAY_ONLY_PREFIXES` above is the deliberate remainder. The sorted
#: tuple interleaves the two halves, so read the two sources, not the
#: order.
#:
#: `tests/test_infrastructure_category_consumers.py` pins the contents
#: through `RunRecord.infra_aborted` rather than against this tuple, so a
#: property that stopped consulting it would be caught.
INFRA_FAILURE_PREFIXES: tuple[str, ...] = tuple(
    sorted({f"{check}:" for check in INFRASTRUCTURE_CHECKS} | set(_REPLAY_ONLY_PREFIXES))
)


@dataclass
class RunRecord:
    """One row of experiments.tsv, typed and normalized."""

    run_id: str
    timestamp: str
    project: str
    components_total: int
    completed: int
    failed: int
    skipped: int
    retry_rate: float
    common_failure: str

    @property
    def infra_aborted(self) -> bool:
        """True when the run's dominant failure was plumbing, not judgement."""
        return self.common_failure.startswith(INFRA_FAILURE_PREFIXES)

    @property
    def decisive(self) -> bool:
        """A run that yielded a verdict about the factory's judgement.

        Requires at least one component to have reached a terminal state
        AND the run not to have been an infrastructure casualty.
        """
        if self.infra_aborted:
            return False
        return (self.completed + self.failed) > 0


def _as_int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def load_runs(path: Path) -> list[RunRecord]:
    """Parse experiments.tsv. Missing file -> no runs (not an error).

    Through ``evolution.experiment_rows`` rather than a bare
    ``csv.DictReader``, which is #331's read half applied to the second
    reader of this file. A crash mid-write used to leave a row torn, the
    next ``record_run`` appended onto it, and ``DictReader`` zipped the
    concatenation against the header: the ladder then saw a run whose
    numeric columns had shifted, and ``_as_int`` turned every one of
    them into 0 without raising. That is a fabricated run in the
    population ``ks autonomy replay`` reports on. (Not in a live
    promotion: this function has one caller, :func:`replay_file`, and
    that command never mutates ladder state.) The shared parser drops a
    row whose width does not fit the file's header.

    MISSING is no runs; UNREADABLE is a refusal. ``OSError`` and
    ``ValueError`` go to the caller, where 568bca4 let the ``OSError``
    out too. Swallowing them turned a permission error or a decode
    error into an empty population, and an empty population reads as
    "insufficient data to calibrate anything", which is a sentence about
    the project's history rather than about the file. CLAUDE.md, on
    #260: make unreadable a REFUSAL, not an empty read, because an empty
    read is a silent removal of the mechanism. ``ks autonomy replay``
    catches both and names the cause.
    """
    if not path.exists():
        return []
    runs: list[RunRecord] = []
    text = path.read_text(encoding="utf-8")
    for row in experiment_rows(text):
        if row.get("run_id"):
            runs.append(
                RunRecord(
                    run_id=row.get("run_id", ""),
                    timestamp=row.get("timestamp", ""),
                    project=row.get("project", ""),
                    components_total=_as_int(row.get("components_total", "0")),
                    completed=_as_int(row.get("completed", "0")),
                    failed=_as_int(row.get("failed", "0")),
                    skipped=_as_int(row.get("skipped", "0")),
                    retry_rate=_as_float(row.get("retry_rate", "0")),
                    common_failure=(row.get("common_failure") or "").strip(),
                )
            )
    return runs


@dataclass
class ReplayReport:
    """Outcome of replaying the ladder over recorded history."""

    total_runs: int
    decisive_runs: int
    infra_aborted_runs: int
    projects: list[str]
    #: Merges in DECISIVE runs only, which is the population the ladder
    #: promotes on. #339: this used to sum every row, so a run Ctrl-C'd
    #: after merging 24 components printed "Components merged: 24" two
    #: lines under "infra-aborted: 1 (excluded)" while contributing
    #: nothing to a promotion. Two populations, one label.
    components_merged: int
    #: The merges the line above deliberately leaves out, so the total is
    #: still recoverable and the exclusion is visible rather than a
    #: number that quietly got smaller. Rendered only when non-zero,
    #: unlike the infra-aborted count above it, because a report with
    #: nothing excluded has nothing to disclose.
    #:
    #: No default: a hand-built report would otherwise claim silently
    #: that nothing was excluded, which is the exact under-report this
    #: field exists to stop.
    merged_in_excluded_runs: int
    would_promote: list[str] = field(default_factory=list)
    would_demote: list[str] = field(default_factory=list)
    final_level: int = int(AutonomyLevel.L1_SUPERVISED)
    thresholds: dict[str, int] = field(default_factory=lambda: dict(THRESHOLDS))

    @property
    def sufficient_data(self) -> bool:
        """Whether the sample can calibrate anything at all."""
        return self.decisive_runs >= MIN_DECISIVE_RUNS

    def render(self) -> str:
        lines = [
            "Autonomy threshold replay (R8.2)",
            "=" * 34,
            "",
            f"Runs recorded:        {self.total_runs}",
            f"  decisive:           {self.decisive_runs}",
            f"  infra-aborted:      {self.infra_aborted_runs} (excluded)",
            f"Components merged:    {self.components_merged} (in decisive runs)",
            *(
                [f"  in excluded runs:   {self.merged_in_excluded_runs} (not counted)"]
                if self.merged_in_excluded_runs
                else []
            ),
            f"Projects:             {', '.join(self.projects) or '(none)'}",
            "",
            "Thresholds replayed (ALL UNMEASURED PLACEHOLDERS):",
        ]
        lines.extend(f"  {name:<34} {value}" for name, value in sorted(self.thresholds.items()))
        lines.extend(
            [
                "",
                f"Would-have-promoted:  {len(self.would_promote)}",
                *(f"  + {entry}" for entry in self.would_promote),
                f"Would-have-demoted:   {len(self.would_demote)}",
                *(f"  - {entry}" for entry in self.would_demote),
                "",
                f"Final level after replay: L{self.final_level}",
                "",
            ]
        )
        if not self.sufficient_data:
            lines.extend(
                [
                    "VERDICT: INSUFFICIENT DATA.",
                    f"  {self.decisive_runs} decisive run(s) against a "
                    f"MIN_DECISIVE_RUNS floor of {MIN_DECISIVE_RUNS}.",
                    "  No threshold above can be calibrated from this sample, and",
                    "  none should gate a real transition yet. The ladder is safe",
                    "  to run - L1 grants nothing - but promotion beyond L1 rests",
                    "  on evidence that does not exist yet.",
                    "  Required: more real factory runs (see docs/dark-factory-roadmap.md,",
                    "  'User-run measurements required').",
                ]
            )
        else:
            lines.append(
                "VERDICT: sample meets the minimum; thresholds may be tuned "
                "against it and the result recorded in the roadmap."
            )
        return "\n".join(lines)


def replay(runs: list[RunRecord]) -> ReplayReport:
    """Replay the ladder over recorded runs, reporting hypothetical moves.

    Promotion is replayed as "criteria would have been MET" - it never
    fires unattended in reality, since a human ack is required, so a
    would-promote entry means "the factory would have been eligible to
    ask", not "it would have promoted itself".
    """
    state = AutonomyState()
    report = ReplayReport(
        total_runs=len(runs),
        decisive_runs=sum(1 for r in runs if r.decisive),
        infra_aborted_runs=sum(1 for r in runs if r.infra_aborted),
        projects=sorted({r.project for r in runs if r.project}),
        components_merged=sum(r.completed for r in runs if r.decisive),
        merged_in_excluded_runs=sum(r.completed for r in runs if not r.decisive),
    )
    for run in runs:
        if not run.decisive:
            continue
        state.record_decisive_run()
        for _ in range(run.completed):
            state.record_merged_component()
        # A run whose components failed review is the closest proxy the
        # recorded history has for a judgement-quality regression. Real
        # demotion triggers (policy violation, calibration regression,
        # health breach) are not in experiments.tsv, so this replay can
        # only bound the demote side from below - stated, not hidden.
        if run.failed and not run.infra_aborted:
            record = state.demote(
                DemotionTrigger.CALIBRATION_REGRESSION,
                f"replay: {run.failed} component(s) failed in {run.run_id}",
            )
            if record is not None:
                report.would_demote.append(
                    f"{run.timestamp} {run.run_id}: "
                    f"L{record.from_level} -> L{record.to_level} "
                    f"({record.trigger})"
                )
        if state.autonomy_level is not AutonomyLevel.L4_DEPLOY and not state.promotion_blockers():
            # Advance the SIMULATED level, not just the report. Recording
            # eligibility without moving would pin the replay at L1
            # forever: it would re-report the same L2 opportunity on every
            # subsequent run and never exercise the L3/L4 thresholds, the
            # cool-down, or any post-promotion demotion. The ack is a
            # real-world requirement, not a replay one - here we ask "what
            # would the criteria have allowed?", so the simulation grants
            # it and says so.
            record = state.promote(
                actor="replay",
                ack=f"simulated: criteria met at {run.run_id}",
            )
            report.would_promote.append(
                f"{run.timestamp} {run.run_id}: L{record.from_level} -> "
                f"L{record.to_level} (eligible; human ack still required "
                "in reality)"
            )
    report.final_level = state.level
    return report


def replay_file(path: Path | None = None, root_dir: Path | None = None) -> ReplayReport:
    """Replay from an experiments.tsv path (default ``.kstrl/`` under root)."""
    if path is None:
        base = root_dir or Path.cwd()
        path = base / DEFAULT_EXPERIMENTS_PATH
    return replay(load_runs(path))
