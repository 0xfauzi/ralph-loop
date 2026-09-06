"""Schema-v2 typed event model for kstrl runs (TUI rewrite, stage 1).

The filesystem is the event bus: the orchestrator and its workers append
one JSON object per line to files under ``.kstrl/runs/<run_id>/`` and
every surface (plain line output, the Textual TUI, ``ks status``) is
a projection of that stream. This module owns the vocabulary: the
:class:`Event` dataclasses, the sinks that write them, the tolerant
reader that parses them back, and the run-directory layout.

Envelope (one JSON object per line)::

    {"schema": 2, "event": "<type>", "ts": <float epoch seconds>,
     "run_id": "...", "component": "...", "source": "orchestrator|worker",
     "seq": <int>, "data": {<payload fields>}}

Envelope fields are stamped by :meth:`EventBus.emit`, never by call
sites. Decoding is TOTAL: every payload field has a default, unknown
event names become :class:`UnknownEvent` (losslessly re-serializable),
mistyped payload values degrade to the field default, and a torn tail
line parses to ``None``. Sinks are observability, never control flow:
:class:`EventBus` isolates sink exceptions and counts drops.

Naming note: v1 compatibility (``.kstrl/progress.jsonl``) is provided by
:class:`V1CompatSink`, which delegates to a real
:class:`~kstrl.observability.ProgressLog` so its file format AND its
attached ``ProgressSink`` observers (e.g. the Linear sink, R7.4) keep
working unchanged.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, ClassVar, Final, Protocol

from kstrl.appendio import (
    JOURNAL_REPAIR_EVENT,
    REPAIR_DETAIL,
    append_terminated,
    open_for_append,
)
from kstrl.observability import ProgressLog

SCHEMA_VERSION: Final = 2

_ENVELOPE_FIELDS: Final = frozenset({"ts", "run_id", "component", "source", "seq"})

_REGISTRY: dict[str, type[Event]] = {}
_FIELD_DEFAULTS: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True, kw_only=True)
class Event:
    """Base class for schema-v2 events.

    Envelope fields (``ts``/``run_id``/``component``/``source``/``seq``)
    are stamped by :meth:`EventBus.emit`; payload fields are everything a
    subclass adds. Every payload field MUST have a default so decoding
    is total (forward compatibility contract).
    """

    type: ClassVar[str] = ""  # registry key; "" = abstract base

    ts: float = 0.0
    run_id: str = ""
    component: str = ""
    source: str = "orchestrator"
    seq: int = 0

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.type:
            _REGISTRY[cls.type] = cls

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            if f.name in _ENVELOPE_FIELDS:
                continue
            value = getattr(self, f.name)
            data[f.name] = list(value) if isinstance(value, tuple) else value
        return {
            "schema": SCHEMA_VERSION,
            "event": type(self).type,
            "ts": self.ts,
            "run_id": self.run_id,
            "component": self.component,
            "source": self.source,
            "seq": self.seq,
            "data": data,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)


@dataclass(frozen=True, kw_only=True)
class UnknownEvent(Event):
    """An event whose type or shape this build does not understand.

    Preserves the raw envelope so copies/tees are lossless and reducers
    can count what they skipped instead of crashing on it.
    """

    type: ClassVar[str] = "unknown"
    type_name: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if self.raw:
            return dict(self.raw)
        return super().to_dict()


# ---------------------------------------------------------------------------
# v1-named events (1:1 with ProgressLog's catalogue; V1CompatSink maps these)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RunStarted(Event):
    type: ClassVar[str] = "factory_started"
    project: str = ""
    components: int = 0


@dataclass(frozen=True, kw_only=True)
class ComponentStarted(Event):
    type: ClassVar[str] = "component_started"


@dataclass(frozen=True, kw_only=True)
class ComponentCompleted(Event):
    type: ClassVar[str] = "component_completed"
    duration_seconds: float = 0.0
    iterations: int = 0


@dataclass(frozen=True, kw_only=True)
class ComponentFailed(Event):
    type: ClassVar[str] = "component_failed"
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class ComponentSkipped(Event):
    """A planned component that ended intentionally without completing."""

    type: ClassVar[str] = "component_skipped"
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class CircuitBreakerTripped(Event):
    """R7.5: engineer loop halted on the no-progress breaker."""

    type: ClassVar[str] = "circuit_breaker_tripped"
    iterations: int = 0
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class ComponentRetrying(Event):
    type: ClassVar[str] = "component_retrying"
    attempt: int = 0
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class VerificationResultEvent(Event):
    """One run of ``verify.run_mechanical_verification``.

    ``phase`` and ``advisory`` are #288. The factory's Phase 1 leaves
    both at their defaults: it is the gate, and it emits once per
    component attempt inside its own ``phase="verify"`` bracket.
    `ks feature` emits several per run - one after the implement loop and
    one after each repair attempt - and gates on none of them, so it
    names the loop it measured and marks the verdict advisory.

    Without those two fields a consumer reading `events.jsonl` cannot
    tell which loop a verdict is about (filtering by type loses the
    ordering that would otherwise say), and reads ``passed=False``
    followed by ``phase_completed(passed=True)`` as a contradiction
    rather than as a report next to a gate. Both default, so old
    payloads decode unchanged; adding them later would not reach runs
    already on disk.
    """

    type: ClassVar[str] = "verification_result"
    passed: bool = False
    checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    duration_seconds: float = 0.0
    #: The loop whose output was measured ("implement", "repair-2").
    #: Empty for the factory, whose PhaseStarted/PhaseCompleted bracket
    #: already names it.
    phase: str = ""
    #: True when nothing gated on this verdict.
    advisory: bool = False
    #: ``"check:reason"`` per check that was asked for and measured
    #: nothing (#306), e.g. ``"mutation_testing:tool_missing"``.
    #: Deliberately NOT folded into ``checks``, which names what ran:
    #: a consumer counting green checks must not count these. Defaults
    #: empty, so payloads already on disk decode unchanged.
    not_measured: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ReviewResultEvent(Event):
    """Phase 2 review AND phase 2.5 security (mode startswith "security")."""

    type: ClassVar[str] = "review_result"
    passed: bool = False
    mode: str = ""
    fail_count: int = 0
    advisory_count: int = 0
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class ComponentUsage(Event):
    """R3.1 cost meter capture: mirror of ``UsageTotals.to_dict()`` plus
    the phase. Token/cost figures are CLI self-reports - lower bounds
    whenever ``unreported_calls`` > 0.

    R8: ``token_calls`` is the narrower coverage figure - calls that
    reported an actual token count, as opposed to cost alone -
    and ``cost_calls`` is its mirror for the cost axis. Payloads written
    before those fields landed omit them and decode to 0; the decoder
    reads only keys it knows, so old and new readers interoperate both
    ways."""

    type: ClassVar[str] = "component_usage"
    phase: str = ""
    calls: int = 0
    known_calls: int = 0
    token_calls: int = 0
    cost_calls: int = 0
    unreported_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class BudgetExceeded(Event):
    """A run-level ceiling stopped a component.

    R8: ``ceiling`` names WHICH one (``"max_total_tokens"`` /
    ``"max_cost_usd"``), because a consumer that assumed "token" would
    tell the operator to raise the wrong knob. Empty on payloads written
    before the cost ceiling landed.

    R8 review (#180): ``condition`` and ``ceilings`` carry the same halt
    structurally, because ``ceiling`` alone conflated two orthogonal
    facts. ``condition`` is ``"breached"`` (a total reached a ceiling) or
    ``"unenforceable"`` (a configured ceiling provably cannot fire) -
    only the former licenses a "N >= cap" sentence, and rendering one for
    the latter reported ``token budget exceeded: 0 >= 500`` on runs whose
    totals never moved. ``ceilings`` holds one or many identities; the
    unenforceable halt legitimately names both, which no single-value
    field could express. ``ceiling`` stays as the joined legacy string so
    payloads written before this change still decode.

    R8 (measured): ``coverage`` records, per named ceiling, how many of
    the run's metered calls that ceiling actually counted and which roles
    it did not - one entry per ceiling, mirroring
    ``CeilingCoverage.to_dict()``. Without it the halt record states a
    total without saying what the total covers, which is how a run whose
    reviewer reported tokens and no cost recorded a dollar ceiling that
    bounded the engineer alone. Empty on payloads written before this
    landed, and on halts whose ceilings covered every call."""

    type: ClassVar[str] = "budget_exceeded"
    total_tokens: int = 0
    max_total_tokens: int = 0
    cost_usd: float = 0.0
    max_cost_usd: float = 0.0
    ceiling: str = ""
    condition: str = ""
    ceilings: tuple[str, ...] = ()
    coverage: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, kw_only=True)
class BudgetCoverage(Event):
    """A configured ceiling stopped covering every metered call.

    Emitted ONCE per ceiling per run, at the first phase whose usage
    leaves that ceiling short - the earliest point the evidence exists,
    and long before the halt message the operator would otherwise be
    reading only after the money is spent.

    Run-scoped (no component): coverage is a property of the run's
    adapters, not of whichever component happened to expose it.

    ``uncovered_tokens`` is deliberately a TOKEN count. The uncovered
    calls reported no price and this codebase holds no price table;
    converting them to dollars would put an invented number in the audit
    trail, which is worse than a missing one."""

    type: ClassVar[str] = "budget_coverage"
    ceiling: str = ""
    axis: str = ""
    calls: int = 0
    covered_calls: int = 0
    uncovered_calls: int = 0
    uncovered_tokens: int = 0
    uncovered_roles: tuple[str, ...] = ()
    detail: str = ""


def budget_halt_kind(
    condition: str,
    ceilings: Sequence[str],
    ceiling: str = "",
) -> str:
    """Classify a :class:`BudgetExceeded` payload: the ONE vocabulary.

    Returns ``"unenforceable"``, ``"cost"`` or ``"token"``. Every surface
    that renders a budget halt classifies through here rather than
    re-testing ``ceiling == "max_cost_usd"`` locally: those local tests
    were what silently reclassified the multi-ceiling unenforceable halt
    as a token breach in both the reducer and the Linear sink, and a
    third copy of the rule is how a mislabel becomes a divergence.

    Legacy payloads carry only the joined ``ceiling`` string and no
    condition; for them the single-value test is still the only reading
    available, and it was correct for everything written back then.
    """
    if condition == "unenforceable":
        return "unenforceable"
    if ceilings:
        return "cost" if tuple(ceilings) == ("max_cost_usd",) else "token"
    return "cost" if ceiling == "max_cost_usd" else "token"


@dataclass(frozen=True, kw_only=True)
class ContractResult(Event):
    type: ClassVar[str] = "contract_result"
    tier: int = 0
    passed: bool = False
    breaker: str | None = None
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class RunCompleted(Event):
    type: ClassVar[str] = "factory_completed"
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class MergePendingV1(Event):
    """v1-parity twin of :class:`PrMergePending` (kept so the compat
    file's ``merge_pending`` line survives unchanged; the reducer
    prefers the v2 event)."""

    type: ClassVar[str] = "merge_pending"
    pr_url: str = ""
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class PhaseSkipped(Event):
    type: ClassVar[str] = "phase_skipped"
    phase: str = ""
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class DiffFetchFailed(Event):
    type: ClassVar[str] = "diff_fetch_failed"
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class ReviewDivergence(Event):
    """#265: the retry loop was growing the change without the review
    retiring a single one of its blocking findings. Parallel series, one
    entry per attempt in the window that tripped it."""

    type: ClassVar[str] = "review_divergence"
    attempts: tuple[int, ...] = ()
    #: Lines ADDED PLUS REMOVED against the base, per attempt.
    lines_changed: tuple[int, ...] = ()
    files_changed: tuple[int, ...] = ()
    #: ``ReviewResult.fail_count`` per attempt, so this joins against
    #: ``ReviewResultEvent.fail_count`` rather than disagreeing with it.
    blocking_findings: tuple[int, ...] = ()
    #: Whether this trip actually failed the component. False in
    #: advisory mode, where it was recorded and the component retried.
    #: Named apart from ``blocking_findings`` above, which counts the
    #: reviewer's findings and is a different sense of the word.
    blocked: bool = False


@dataclass(frozen=True, kw_only=True)
class AdversarialAgentSelected(Event):
    """agent_type/model stay optional (None, not ""): the v1 line wrote
    JSON null for unset values and byte parity is this chunk's contract."""

    type: ClassVar[str] = "adversarial_agent_selected"
    phase: str = ""
    agent_source: str = ""
    identity: str = ""
    agent_type: str | None = None
    model: str | None = None
    homogeneous: bool = False


# ---------------------------------------------------------------------------
# v2-only events (dropped by V1CompatSink; the TUI/reducer's real signal)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RunPlan(Event):
    """The component DAG plus budget caps, emitted right after
    ``factory_started`` so consumers need no manifest read to draw the
    board. ``components`` entries: {"id", "title", "deps": [...]}."""

    type: ClassVar[str] = "run_plan"
    components: tuple[Mapping[str, Any], ...] = ()
    max_total_tokens: int = 0
    max_adversarial_calls: int = 0
    max_cost_usd: float = 0.0


@dataclass(frozen=True, kw_only=True)
class ComponentScopeResolved(Event):
    """One component's write scope, as resolved before any engineer ran.

    #269: both guards read a single plan-time snapshot
    (``scope.ComponentScope``) instead of each re-reading a PRD, so the
    scope decision is made once and has to be recorded once - otherwise
    the only way to answer "why was this component allowed to write
    that?" after the run is to re-derive a value from files the run has
    since changed. ``scope_source`` says which authority supplied the
    list (the component PRD, the run-wide flag, or nothing) and
    ``origin`` names the file or flag it came from.

    ``scope_source``, not ``source``: that name is an ENVELOPE field on
    every event, so a payload field sharing it is overwritten by the bus
    at emit and dropped from the serialised record.
    """

    type: ClassVar[str] = "component_scope_resolved"
    scope_source: str = ""
    origin: str = ""
    allowed_paths: tuple[str, ...] = ()
    harness_paths: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class PhaseStarted(Event):
    type: ClassVar[str] = "phase_started"
    phase: str = ""
    attempt: int = 0


@dataclass(frozen=True, kw_only=True)
class PhaseCompleted(Event):
    type: ClassVar[str] = "phase_completed"
    phase: str = ""
    passed: bool = False
    detail: str = ""
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class IterationStarted(Event):
    type: ClassVar[str] = "iteration_started"
    iteration: int = 0
    max_iterations: int = 0


@dataclass(frozen=True, kw_only=True)
class IterationCompleted(Event):
    type: ClassVar[str] = "iteration_completed"
    iteration: int = 0
    duration_seconds: float = 0.0
    completed: bool = False
    timed_out: bool = False


@dataclass(frozen=True, kw_only=True)
class WorkerHeartbeat(Event):
    type: ClassVar[str] = "worker_heartbeat"
    pid: int = 0
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class CheckpointRequested(Event):
    type: ClassVar[str] = "checkpoint_requested"
    kind: str = ""
    question: str = ""


@dataclass(frozen=True, kw_only=True)
class CheckpointResolved(Event):
    """``decided_by``: "auto" (non-interactive default) or "operator"."""

    type: ClassVar[str] = "checkpoint_resolved"
    kind: str = ""
    decision: str = ""
    decided_by: str = ""


@dataclass(frozen=True, kw_only=True)
class PrCreated(Event):
    type: ClassVar[str] = "pr_created"
    pr_number: int = 0
    pr_url: str = ""


@dataclass(frozen=True, kw_only=True)
class PrMerged(Event):
    type: ClassVar[str] = "pr_merged"
    pr_number: int = 0
    pr_url: str = ""


@dataclass(frozen=True, kw_only=True)
class PrMergePending(Event):
    type: ClassVar[str] = "pr_merge_pending"
    pr_url: str = ""
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class DistillResult(Event):
    """Pre-PR knowledge distillation outcome: the knowledge layer's
    WRITE side, for one component.

    The read side - did the engineer use the facts it was given - is
    :class:`FactUtilizationMeasured`, deliberately a separate event.
    Carrying utilization here too would duplicate one measurement
    across two events and invite a consumer to double count, and this
    event is only emitted when distillation actually ran.
    """

    type: ClassVar[str] = "distill_result"
    facts_written: int = 0
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class FactUtilizationMeasured(Event):
    """Did the engineer reference the facts injected into its prompt?

    Emitted once per component per attempt, on EVERY path the pipeline
    can take once a diff is obtainable - including components that go
    on to fail review or security, and including the paths where
    distillation is skipped or raises. That is the point of it being
    its own event: `evolution.jsonl` and `events.jsonl` must carry the
    same population, and the earlier design emitted only from a
    successful distill.

    ``measured`` must be read FIRST. False means we could not measure -
    it does NOT mean the engineer referenced nothing, and ``reason``
    says which it was. A measured ``referenced=0`` IS evidence, that
    injected facts went unused.

    The counts are matched against ADDED diff lines and the progress
    log only, so deleting the code that expressed a fact does not score
    as referencing it. Per-tier counts can sum to less than the totals;
    read ``core_*`` for the ratio that is about the component actually
    being built, since the sibling tier inflates the denominator.
    """

    type: ClassVar[str] = "fact_utilization_measured"
    measured: bool = False
    injected: int = 0
    referenced: int = 0
    reason: str = ""
    core_injected: int = 0
    core_referenced: int = 0
    dependency_injected: int = 0
    dependency_referenced: int = 0
    sibling_injected: int = 0
    sibling_referenced: int = 0


@dataclass(frozen=True, kw_only=True)
class FindingRecorded(Event):
    """One typed adversarial finding, streamed as it is recorded."""

    type: ClassVar[str] = "finding_recorded"
    phase: str = ""
    category: str = ""
    severity: str = ""
    location: str = ""
    explanation: str = ""
    attempt: int = 0
    # R7.1 attribution: the reviewing model identity ("codex (gpt-5)"),
    # extracted from the finding's model: tag; "" when no reviewer ran.
    model: str = ""


@dataclass(frozen=True, kw_only=True)
class SpecIssueRecorded(Event):
    """One architect spec issue, streamed as decompose parses it.

    Mirrors decompose.SpecIssue; ``kind`` is the architect's issue
    taxonomy (ambiguity, contradiction, ...), not a run kind.
    """

    type: ClassVar[str] = "spec_issue_recorded"
    severity: str = ""  # blocker | major | minor
    kind: str = ""
    summary: str = ""
    location: str = ""
    suggestion: str = ""


@dataclass(frozen=True, kw_only=True)
class ArtifactWritten(Event):
    """A durable output landed on disk (prd, manifest, spec-issues,
    codebase map...). ``path`` is root-relative when possible."""

    type: ClassVar[str] = "artifact_written"
    label: str = ""
    path: str = ""


@dataclass(frozen=True, kw_only=True)
class AutonomyTransition(Event):
    """R8.2: the autonomy level changed (promotion or demotion).

    ``direction``: promote | demote. ``actor`` is the human who
    acknowledged a promotion, or "system" for an automatic demotion;
    ``trigger`` carries the DemotionTrigger label on demotions only.
    Emitted for every transition so the level in force at any past moment
    is reconstructable from the event stream alone.
    """

    type: ClassVar[str] = "autonomy_transition"
    direction: str = ""
    from_level: int = 0
    to_level: int = 0
    actor: str = ""
    trigger: str = ""
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class AutonomyLevelApplied(Event):
    """R8.2: the flag bundle a run started under.

    Recorded at run start so a run's permissions are auditable after the
    fact even if the stored level later changes. ``overrides`` names any
    config flag that contradicted the bundle (the bundle still won).
    """

    type: ClassVar[str] = "autonomy_level_applied"
    level: int = 0
    label: str = ""
    flags: tuple[str, ...] = ()
    overrides: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class JournalRepaired(Event):
    """#331: this file's tail was not newline-terminated when a sink
    opened it, so a crash interrupted a write, and a newline was written
    before this event to stop the unterminated tail swallowing it.

    A REGISTERED event rather than a raw appended line, which is the
    whole reason this class exists. A raw line decodes to
    ``UnknownEvent`` and is counted in ``RunState.unknown_events``, and
    "this build does not understand it" is false for a row this build
    wrote deliberately. As a registered type it is understood and
    inert: ``reducer.apply`` falls through every isinstance branch for
    it and advances only the clock, so no component, phase or count
    moves.

    The envelope is empty (``run_id`` "", ``source`` at its default).
    The sink sits BELOW the bus that stamps those fields, and it repairs
    at open time, before the run has told it anything.
    """

    type: ClassVar[str] = JOURNAL_REPAIR_EVENT
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class Log(Event):
    """The escape hatch for imperative narration (the old UI protocol).

    ``kind``: line | kv | section | subsection | title | hr | channel |
    stream | startup_art. ``key`` carries the kv key / channel name /
    stream tag; ``severity``: info | ok | warn | error.
    """

    type: ClassVar[str] = "log"
    severity: str = "info"
    kind: str = "line"
    key: str = ""
    text: str = ""


# ---------------------------------------------------------------------------
# Decoding (total: never raises)
# ---------------------------------------------------------------------------


def _field_defaults(cls: type[Event]) -> dict[str, Any]:
    cached = _FIELD_DEFAULTS.get(cls.type)
    if cached is not None:
        return cached
    defaults: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name in _ENVELOPE_FIELDS:
            continue
        if f.default is not dataclasses.MISSING:
            defaults[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:
            defaults[f.name] = f.default_factory()
    _FIELD_DEFAULTS[cls.type] = defaults
    return defaults


def _coerce(default: Any, value: Any) -> Any:
    """Return a value type-compatible with ``default``, or the default.

    bool is checked before int (bool subclasses int); ints are accepted
    for float fields; lists become tuples for tuple fields.
    """
    if default is None:
        return value
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(value)
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    if isinstance(default, tuple):
        return tuple(value) if isinstance(value, (list, tuple)) else default
    return value


def _envelope_kwargs(obj: Mapping[str, Any]) -> dict[str, Any]:
    ts = obj.get("ts")
    seq = obj.get("seq")
    return {
        "ts": float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0,
        "run_id": obj.get("run_id") if isinstance(obj.get("run_id"), str) else "",
        "component": obj.get("component") if isinstance(obj.get("component"), str) else "",
        "source": obj.get("source") if isinstance(obj.get("source"), str) else "orchestrator",
        "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
    }


def event_from_dict(obj: Mapping[str, Any]) -> Event:
    """Decode one envelope dict into a typed event. Never raises."""
    name = obj.get("event")
    cls = _REGISTRY.get(name) if isinstance(name, str) else None
    envelope = _envelope_kwargs(obj)
    if cls is None or cls is UnknownEvent:
        return UnknownEvent(
            type_name=name if isinstance(name, str) else "",
            raw=dict(obj),
            **envelope,
        )
    payload: dict[str, Any] = {}
    raw_data = obj.get("data")
    defaults = _field_defaults(cls)
    if isinstance(raw_data, Mapping):
        for fname, default in defaults.items():
            if fname in raw_data:
                payload[fname] = _coerce(default, raw_data[fname])
    try:
        return cls(**payload, **envelope)
    except Exception:  # noqa: BLE001 - decode is total by contract
        return UnknownEvent(
            type_name=name if isinstance(name, str) else "",
            raw=dict(obj),
            **envelope,
        )


def parse_event_line(line: str) -> Event | None:
    """One JSONL line -> Event, or None for torn/blank/non-dict lines."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    return event_from_dict(obj)


def read_events(path: Path) -> list[Event]:
    """Tolerant reader: skips torn/blank lines, returns [] for a missing
    file (mirrors ``observability.read_progress_events``)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    events: list[Event] = []
    for line in lines:
        event = parse_event_line(line)
        if event is not None:
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Sinks and the bus
# ---------------------------------------------------------------------------


class EventSink(Protocol):
    """Destination for stamped events. Sinks must never raise into the
    run - EventBus isolates them anyway, but a sink should be cheap."""

    def emit(self, event: Event) -> None: ...

    def close(self) -> None: ...


class NullSink:
    def emit(self, event: Event) -> None:  # noqa: ARG002 - protocol
        return

    def close(self) -> None:
        return


class CallbackSink:
    """Wraps a callable; used for same-thread renderers and inline tees."""

    def __init__(self, callback: Callable[[Event], None]) -> None:
        self._callback = callback

    def emit(self, event: Event) -> None:
        self._callback(event)

    def close(self) -> None:
        return


class JsonlSink:
    """Append-only JSONL writer; one line per event, flushed, guarded by
    a lock so heartbeat threads can share it with the main thread.

    #331: the handle is opened ``"a+b"`` and the tail is probed ONCE,
    at the first emit. Without that probe a crash mid-write cost the
    next event as well as the torn one, measured through ``read_events``
    and ``reducer.fold``: ``['factory_started']`` where two events were
    written, and no components at all in the folded state.

    Once per sink, not once per event, is the reason
    ``handle_ends_without_newline`` takes a HANDLE. This sink holds one
    open for a whole run; re-probing would pay a seek and a read on
    every event and would write a second repair row for a tear it had
    already repaired.

    No lock on the file. One process owns each of these files: the
    orchestrator owns ``events.jsonl`` and each worker owns its own
    ``engineer.jsonl``. The threading lock below is about threads
    sharing this object, which is a different question and predates
    this change.

    THE ``"a+b"`` WIDENING, and it is the QUIETEST of the three sites
    that took it, so it is stated here rather than left to the module
    that opens the handle. A file this process can write but not read
    can no longer be appended to: the open raises. This sink is reached
    through ``EventBus.emit``, which catches every exception per sink
    and increments ``dropped``, and ``dropped`` has no production
    reader. So on a mode-0200 ``events.jsonl`` the whole stream is lost
    with no message on any surface, where 568bca4 wrote it. The same is
    true of ``progress.jsonl``, which the factory reaches through a
    ``V1CompatSink`` on the same bus, so both file streams of a run go
    quiet together.

    That was decided rather than overlooked. Reaching it needs a
    deliberate ``chmod 0200`` or an ACL on a file kstrl created itself
    at the umask default; the alternative to the widening is the
    fail-OPEN shape #327 round 1 found, where an unreadable file was
    reported as "not torn" and appended to blind. A warning on the
    first drop was considered and left out: ``events`` has no logger,
    the bus has no UI, and the surface it would reach is
    ``orchestrator.log``, which is the surface #333 exists because
    nobody reads. Giving ``dropped`` a real reader is the fix, and it
    is a change to the run summary rather than to this sink.
    """

    def __init__(self, path: Path, *, mkdir: bool = True) -> None:
        self.path = path
        if mkdir:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh: IO[bytes] | None = None

    def emit(self, event: Event) -> None:
        line = event.to_json_line() + "\n"
        with self._lock:
            if self._fh is None:
                self._fh = self._probe_and_write(line)
            else:
                # Every later emit writes straight through. The probe is
                # not repeated: this handle has been at the end of the
                # file since the first emit, so nothing can have torn it
                # without this process being dead.
                self._fh.write(line.encode("utf-8"))
            self._fh.flush()

    def _probe_and_write(self, line: str) -> IO[bytes]:
        """Open, probe and write the first line, returning the bound handle.

        The handle is returned rather than assigned, and that is the
        whole point of the method: :meth:`emit` binds ``self._fh`` only
        after this returns, so a first write that RAISES leaves the sink
        unbound and the next emit probes again.

        Assigning first was a real hole. ``EventBus.emit`` catches every
        exception per sink and increments ``dropped``, which nothing in
        the product reads, so a first emit that hit ``ENOSPC`` on the
        write or ``EIO`` on the probe's read was silent; the sink then
        took the no-probe branch for the rest of the run and wrote
        straight onto the unterminated tail. Measured on a torn
        ``events.jsonl`` with the first write made to raise:
        ``read_events`` returned NOTHING, because the concatenated line
        does not parse at all.

        The handle is closed on the way out. Leaking it would hold a
        descriptor for the life of the run for a file this sink is
        about to reopen on the next emit.
        """
        handle = open_for_append(self.path)
        try:
            append_terminated(
                handle,
                line,
                repair=JournalRepaired(detail=REPAIR_DETAIL).to_json_line() + "\n",
            )
        except BaseException:
            handle.close()
            raise
        return handle

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None


class V1CompatSink:
    """Projects v1-named events onto a real :class:`ProgressLog`.

    Delegating (rather than re-serializing) keeps two contracts intact
    by construction: the progress.jsonl line format, and the R7.4
    ``ProgressSink`` observers attached to the log (e.g. Linear).
    v2-only events are dropped silently - that is the point.

    KNOWN LIMITATION, v1 has no room for it (#288 review round 2).
    ``VerificationResultEvent`` gained ``phase`` and ``advisory``; the
    v1 ``ProgressLog.verification_result`` signature has neither, and
    widening it would change the progress.jsonl line format this sink
    exists to hold still. So a v1 reader sees an ADVISORY report and a
    GATE verdict as the same row: ``summarize_events`` cannot tell them
    apart, and ``_phase_for_event`` reports the component in phase
    "verify", a phase `ks feature` does not have.

    ``not_measured`` (#306) is dropped here for the same reason, and a
    v1 reader is not told which enabled check measured nothing.

    Nothing is wrong today, because `ks feature` is the only command
    that emits advisory reports and it attaches no ``V1CompatSink``. The
    moment a command does both, forward both fields, which means a v2
    progress-log format rather than an edit here.
    """

    def __init__(self, progress_log: ProgressLog) -> None:
        self._log = progress_log

    @property
    def path(self) -> Path:
        return self._log.path

    def emit(self, event: Event) -> None:  # noqa: C901 - flat dispatch table
        log = self._log
        comp = event.component
        if isinstance(event, RunStarted):
            log.factory_started(event.project, event.components)
        elif isinstance(event, ComponentStarted):
            log.component_started(comp)
        elif isinstance(event, ComponentCompleted):
            log.component_completed(comp, event.duration_seconds, event.iterations)
        elif isinstance(event, CircuitBreakerTripped):
            log.circuit_breaker_tripped(comp, event.iterations, event.error)
        elif isinstance(event, ComponentFailed):
            log.component_failed(comp, event.error)
        elif isinstance(event, ComponentRetrying):
            log.component_retrying(comp, event.attempt, event.reason)
        elif isinstance(event, VerificationResultEvent):
            log.verification_result(
                comp,
                event.passed,
                list(event.checks),
                list(event.failures),
                event.duration_seconds,
            )
        elif isinstance(event, ReviewResultEvent):
            log.review_result(
                comp,
                event.passed,
                event.mode,
                event.fail_count,
                event.advisory_count,
                event.duration_seconds,
            )
        elif isinstance(event, ComponentUsage):
            log.component_usage(
                comp,
                event.phase,
                {
                    "calls": event.calls,
                    "known_calls": event.known_calls,
                    "token_calls": event.token_calls,
                    "cost_calls": event.cost_calls,
                    "unreported_calls": event.unreported_calls,
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                    "cache_read_tokens": event.cache_read_tokens,
                    "cache_creation_tokens": event.cache_creation_tokens,
                    "total_tokens": event.total_tokens,
                    "cost_usd": event.cost_usd,
                    "duration_seconds": event.duration_seconds,
                },
            )
        elif isinstance(event, BudgetExceeded):
            log.budget_exceeded(
                comp,
                event.total_tokens,
                event.max_total_tokens,
                cost_usd=event.cost_usd,
                max_cost_usd=event.max_cost_usd,
                ceiling=event.ceiling,
                condition=event.condition,
                ceilings=event.ceilings,
                coverage=event.coverage,
            )
        elif isinstance(event, BudgetCoverage):
            # Mirrored rather than left v2-only: a ceiling that counts
            # only part of the run is a BUDGET fact, and every other
            # budget fact reaches progress.jsonl (and through it the
            # v1 `ks status` arm and the Linear ProgressSink).
            log.budget_coverage(
                ceiling=event.ceiling,
                axis=event.axis,
                calls=event.calls,
                covered_calls=event.covered_calls,
                uncovered_calls=event.uncovered_calls,
                uncovered_tokens=event.uncovered_tokens,
                uncovered_roles=event.uncovered_roles,
                detail=event.detail,
            )
        elif isinstance(event, ContractResult):
            log.contract_result(
                event.tier,
                event.passed,
                event.breaker,
                event.duration_seconds,
            )
        elif isinstance(event, RunCompleted):
            log.factory_completed(
                event.completed,
                event.failed,
                event.skipped,
                event.duration_seconds,
            )
        elif isinstance(event, MergePendingV1):
            log.emit(
                "merge_pending",
                comp,
                {
                    "pr_url": event.pr_url,
                    "error": event.error,
                },
            )
        elif isinstance(event, PhaseSkipped):
            log.emit(
                "phase_skipped",
                comp,
                {
                    "phase": event.phase,
                    "reason": event.reason,
                },
            )
        elif isinstance(event, DiffFetchFailed):
            log.emit("diff_fetch_failed", comp, {"error": event.error})
        elif isinstance(event, AdversarialAgentSelected):
            log.emit(
                "adversarial_agent_selected",
                data={
                    "phase": event.phase,
                    "source": event.agent_source,
                    "identity": event.identity,
                    "agent_type": event.agent_type,
                    "model": event.model,
                    "homogeneous": event.homogeneous,
                },
            )
        # v2-only events: dropped by design.

    def close(self) -> None:
        return


class EventBus:
    """Stamps the envelope and fans out to sinks, isolating failures.

    ``run_id``/``component``/``source`` defaults fill empty envelope
    fields; ``seq`` is per-bus monotonic; ``ts`` is wall-clock at emit
    (the TUI's last-event-age depends on this being wall time).
    """

    def __init__(
        self,
        *sinks: EventSink,
        run_id: str = "",
        source: str = "orchestrator",
        component: str = "",
    ) -> None:
        self._sinks: list[EventSink] = list(sinks)
        self.run_id = run_id
        self.source = source
        self.component = component
        self.dropped = 0
        self._seq = 0
        self._lock = threading.Lock()

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink: EventSink) -> None:
        """Detach one sink (does not close it). Lets a long-lived
        console bus shed a run's file sinks at run end without
        disturbing its renderer."""
        try:
            self._sinks.remove(sink)
        except ValueError:
            pass

    def emit(self, event: Event) -> Event:
        with self._lock:
            self._seq += 1
            seq = self._seq
        stamped = dataclasses.replace(
            event,
            ts=time.time(),
            run_id=event.run_id or self.run_id,
            component=event.component or self.component,
            source=self.source,
            seq=seq,
        )
        for sink in self._sinks:
            try:
                sink.emit(stamped)
            except Exception:  # noqa: BLE001 - observability never breaks the run
                self.dropped += 1
        return stamped

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:  # noqa: BLE001 - close is best-effort
                self.dropped += 1


# ---------------------------------------------------------------------------
# Run directory layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunPaths:
    """Canonical layout of one run's on-disk stream."""

    root: Path  # <project>/.kstrl/runs/<run_id>

    @classmethod
    def for_run(cls, project_root: Path, run_id: str) -> RunPaths:
        return cls(root=project_root / ".kstrl" / "runs" / run_id)

    @property
    def events_file(self) -> Path:
        return self.root / "events.jsonl"

    def component_dir(self, component_id: str) -> Path:
        return self.root / "components" / component_id

    def engineer_events(self, component_id: str) -> Path:
        return self.component_dir(component_id) / "engineer.jsonl"

    def engineer_log(self, component_id: str) -> Path:
        return self.component_dir(component_id) / "engineer.log"

    def engineer_usage(self, component_id: str) -> Path:
        """Latest engineer-loop usage snapshot (R8).

        Deliberately NOT an event: the worker rewrites this file at every
        iteration boundary, and the reducer sums ``component_usage``
        events, so streaming cumulative snapshots would double count in
        every rollup. It exists so a worker killed by a shutdown does
        not take its spend with it - the parent reads it only for
        futures that never delivered a result.
        """
        return self.component_dir(component_id) / "engineer_usage.json"

    def phase_log(self, component_id: str, phase: str) -> Path:
        return self.component_dir(component_id) / f"{phase}.log"
