"""Per-component pipeline: the factory's component state machine (R7.3).

Extracted from ``factory.run_factory``'s ``_handle_result`` closure so the
state machine is unit-testable in isolation. The pipeline owns one
component attempt's journey through the phase chain:

    engineer result -> verify -> diff -> review -> security
        -> knowledge distillation (PRE-PR, a named step: the distiller
           reads the component's true delta before the merge pulls main
           into the worktree)
        -> HITL checkpoint -> PR create+merge -> COMPLETED

and every transition out of it:

    RETRYING       retries remain; component back to PENDING with context
    FAILED         retries exhausted, budget wall, HITL reject, PR failure
                   (dependents cascade-skip)
    MERGE_PENDING  PR merge initiated but unconfirmed; re-polled next run
    COMPLETED      merge confirmed (or PR flow not configured)

Each phase returns an explicit typed result; ``process_result`` is the
single place that routes a phase failure into a transition. LLM- and
subprocess-heavy phase functions are injected via ``PipelineHooks`` (the
factory resolves them from its own module globals at run start, so the
historical ``patch("kstrl.factory.run_review")`` seam keeps working),
and ``kstrl.git`` functions are looked up on the module at call time
for the same reason.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import events as ev
from kstrl import git
from kstrl.adequacy import AdequacyConfig
from kstrl.agents.base import (
    ARCHITECT_COMPONENT,
    ARCHITECT_ROLE,
    CEILING_AXES,
    CeilingCoverage,
    UsageTotals,
    collect_usage,
    usage_coverage,
)
from kstrl.autonomy import AutonomyConfig, AutonomyState
from kstrl.context import IterationContext, IterationRecord
from kstrl.divergence import (
    AttemptReading,
    DivergenceConfig,
    detect_divergence,
    review_finding_keys,
)
from kstrl.findings import (
    ADEQUACY_CATEGORY_PREFIX,
    POLICY_CATEGORY_PREFIX,
    SETPOINT_DISAGREEMENT_CATEGORY,
    Finding,
    finding_model,
    tag_finding_with_attempt,
)
from kstrl.fixtures import FixturesConfig
from kstrl.inbox import Inbox, InboxConfig, InboxError, ItemKind, notifiable
from kstrl.interaction import (
    CheckpointContext,
    InteractionChannel,
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)
from kstrl.loop import UNENFORCEABLE_CALLS
from kstrl.manifest import (
    ADVERSARIAL_BUDGET_CHECK,
    Component,
    ComponentStatus,
    Manifest,
)
from kstrl.observability import NotifyHooks
from kstrl.policy import PolicyConfig, count_diff_size
from kstrl.prd import PRD
from kstrl.review import (
    ReviewMode,
    ReviewResult,
    revert_unconfirmed_stories,
    setpoint_blocks,
    setpoint_disagreements,
    setpoint_retry_context,
)
from kstrl.sandbox import SandboxConfig
from kstrl.scope import RunScope
from kstrl.security import SecurityConfig, SecurityMode, SecurityResult
from kstrl.verify import (
    SCOPE_UNREADABLE_CHECK,
    CheckResult,
    MechanicalVerification,
    VerificationResult,
    scope_unreadable_error,
)

if TYPE_CHECKING:
    from kstrl.config import KstrlConfig
    from kstrl.factory import (
        AdversarialAgentSelection,
        ComponentResult,
        FactoryConfig,
        FactoryResult,
    )
    from kstrl.knowledge import KnowledgeConfig
    from kstrl.ui.base import UI


# PR A: the E6 checkpoint shows a real diff excerpt, not just the
# review summary string. Bounded so a huge diff cannot flood the modal.
CHECKPOINT_DIFF_CHAR_LIMIT = 20_000


def _iso_now() -> str:
    """Current UTC time as ISO 8601, matching the manifest timestamps."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Transition(Enum):
    """Terminal disposition of one ``process_result`` pass."""

    RETRYING = "retrying"
    FAILED = "failed"
    MERGE_PENDING = "merge_pending"
    COMPLETED = "completed"


class FailureAction(Enum):
    """How a phase failure must be routed (the single transition point)."""

    # Normal gate failure: retry with context, or FAILED once exhausted.
    RETRY_OR_FAIL = "retry_or_fail"
    # A wall retrying can never fix (the adversarial budget only
    # shrinks): fail directly without burning engineer iterations.
    FAIL = "fail"
    # R3.1/R8 run-level ceiling (max_total_tokens or max_cost_usd): fail
    # loudly via the budget path (synthetic finding + budget_exceeded
    # event), never silently degrade. The member name is unchanged
    # vocabulary; the ceiling that tripped travels in the message.
    TOKEN_BUDGET = "token_budget"


@dataclass(frozen=True)
class PhaseFailure:
    """A phase's terminal signal: what fired and how to transition."""

    action: FailureAction
    error: str
    phase: str
    check: str = ""
    context_json: str | None = None
    signatures: list[str] | None = None


@dataclass(frozen=True)
class VerifyPhaseResult:
    """Phase 1 outcome. ``ran=False`` means --no-verify skipped it."""

    ran: bool
    verification: VerificationResult
    failure: PhaseFailure | None = None


@dataclass(frozen=True)
class DiffPhaseResult:
    """The component's diff, fetched once and shared by the phases that
    still take one as text: fact-utilization measurement, the knowledge
    distiller, the HITL checkpoint excerpt and the PR body.

    #266: the review and security phases are NOT among them any more.
    They run inside the worktree and read git themselves, so there is no
    prompt-sized diff to prepare for them, no chunking decision to make,
    and no way for a diff to be too large to review."""

    diff: str = ""
    failure: PhaseFailure | None = None


@dataclass(frozen=True)
class ReviewPhaseResult:
    """Phase 2 outcome. ``ran=False`` carries a skip reason when the
    phase was SKIPPED; the R10.5 budget refusal leaves it None, because
    nothing was skipped and the ``failure`` is the record."""

    ran: bool
    skip_reason: str | None = None
    result: ReviewResult | None = None
    failure: PhaseFailure | None = None


@dataclass(frozen=True)
class SecurityPhaseResult:
    """Phase 2.5 outcome. Same rule as ``ReviewPhaseResult``: a skip
    carries a reason, the R10.5 budget refusal carries a ``failure``."""

    ran: bool
    skip_reason: str | None = None
    result: SecurityResult | None = None
    failure: PhaseFailure | None = None


@dataclass(frozen=True)
class FactUtilization:
    """One component's knowledge fact-utilization measurement.

    ``measured=False`` means WE COULD NOT MEASURE. It does not mean the
    engineer referenced nothing. A recorded ``referenced=0`` alongside
    ``measured=True`` is real evidence that injected facts went unused;
    an unmeasured entry is no evidence at all. Collapsing the two is
    what let a broken recorder read as a legitimate zero and left the
    L2+ fact-utilization gate un-evidenceable (#191).

    ``injected`` is a lower bound: the metric is a 30-char
    case-insensitive substring match
    (``knowledge.measure_fact_utilization``).

    The totals are also biased upward in the denominator, which is what
    ``by_tier`` exists to expose: the sibling tier carries
    first-sentence summaries of every OTHER component's facts, and those
    are the claims this component is least likely to echo.
    ``core_referenced / core_injected`` is the sharper ratio - did the
    engineer use what was known about the component it was building?
    """

    measured: bool = False
    injected: int = 0
    referenced: int = 0
    # Per-tier split of the same measurement. These can sum to less than
    # the totals when the prefix carries a section the knowledge module
    # did not write; an unrecognized section is not folded into a real
    # tier.
    core_injected: int = 0
    core_referenced: int = 0
    dependency_injected: int = 0
    dependency_referenced: int = 0
    sibling_injected: int = 0
    sibling_referenced: int = 0
    # Why unmeasured; "" when measured. Journal-only: the event stream
    # carries the top-line numbers, the journal carries the diagnosis
    # and the tier breakdown.
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "measured": self.measured,
            "injected": self.injected,
            "referenced": self.referenced,
            "reason": self.reason,
            "by_tier": {
                "core": {
                    "injected": self.core_injected,
                    "referenced": self.core_referenced,
                },
                "dependency": {
                    "injected": self.dependency_injected,
                    "referenced": self.dependency_referenced,
                },
                "sibling": {
                    "injected": self.sibling_injected,
                    "referenced": self.sibling_referenced,
                },
            },
        }


@dataclass(frozen=True)
class DistillPhaseResult:
    """Pre-PR knowledge distillation outcome. Never fails the component."""

    ran: bool
    skip_reason: str | None = None
    # Deliberately independent of `ran`: fact utilization costs zero
    # tokens, so it is measured and recorded even when distillation was
    # budget-skipped or raised.
    utilization: FactUtilization | None = None


class CheckpointDecision(Enum):
    """E6 human-in-the-loop checkpoint outcome."""

    NOT_PROMPTED = "not_prompted"
    APPROVED = "approved"
    # R8.3: no interactive UI was available to answer the gate, so the
    # component is parked for the inbox rather than merged unreviewed.
    PARKED = "parked"
    REJECTED = "rejected"
    RETRY = "retry"


class PrDisposition(Enum):
    """Outcome of the per-component PR create+merge step."""

    SKIPPED = "skipped"  # create_prs off, or single_pr defers to end-of-run
    MERGED = "merged"
    MERGE_PENDING = "merge_pending"
    # R7.5: the PR conflicts with base; routed to the re-run doctrine
    # (re-run the component against the freshly merged base) instead of
    # a terminal failure.
    CONFLICT = "conflict"
    FAILED = "failed"
    NO_GH = "no_gh"  # completes without a PR; code stays on its branch


@dataclass(frozen=True)
class PrPhaseResult:
    """PR step outcome; pending/failed dispositions carry the error."""

    disposition: PrDisposition
    pr_url: str = ""
    error: str = ""


@dataclass(frozen=True)
class PipelineOutcome:
    """Everything one ``process_result`` pass decided, for callers/tests."""

    transition: Transition
    verify: VerifyPhaseResult | None = None
    diff: DiffPhaseResult | None = None
    review: ReviewPhaseResult | None = None
    security: SecurityPhaseResult | None = None
    distill: DistillPhaseResult | None = None
    checkpoint: CheckpointDecision | None = None
    pr: PrPhaseResult | None = None


@dataclass(frozen=True)
class PipelineHooks:
    """Injected phase functions (LLM / subprocess seams).

    The factory resolves these from its module globals when the run
    starts, so tests patching ``kstrl.factory.run_review`` (and
    friends) keep intercepting them; pipeline unit tests inject stubs
    directly.
    """

    # Typed by shape, not ``Callable[..., VerificationResult]`` (#316):
    # Phase 1 is the seam that carries a component's SCOPE, and ``...``
    # checks nothing about it. See ``verify.MechanicalVerification``.
    #
    # The three hooks below were NOT cleared, they were not done. Each
    # carries the same hazard, measured: `run_review` and
    # `run_security_review` take adjacent `prd_path` / `worktree_path`
    # Paths and are called positionally below; `distill_facts` takes
    # three Paths among ten positional arguments. Transposing any of
    # those type-checks clean today, exactly as `harness_paths` did.
    # Tightening them is not one character each - their call sites pass
    # positionally, so it is a signature plus a call-site change plus a
    # drift test per hook, in three more modules. Its own issue.
    run_mechanical_verification: MechanicalVerification
    run_review: Callable[..., ReviewResult]
    run_security_review: Callable[..., SecurityResult]
    distill_facts: Callable[..., tuple[int, str]]
    # No build_knowledge_context seam by design (#191). The distill
    # phase once rebuilt the knowledge prefix to measure utilization
    # against; that rebuild read the store AFTER distillation had
    # written this run's facts into it. The prefix is now captured by
    # the factory at submit time and handed over via
    # ComponentPipeline.record_injected_knowledge, so there is no way
    # to reintroduce the rebuild through this struct.
    measure_fact_utilization: Callable[..., dict[str, int]]
    cleanup_worktree: Callable[[str, Path, str], None]


def _verify_routing(failing: list[CheckResult]) -> tuple[FailureAction, str]:
    """How a failed Phase 1 transitions, and what the record says.

    #294: a scope that could not be READ is a wall, not a gate. Every
    other Phase 1 check measures the engineer's work, so a retry
    re-measures something that changed. This one measures the HARNESS's
    own input, resolved once at plan time from the pre-run checkout and
    frozen for the life of the run (``scope.RunScope``), so attempt two
    runs the identical prompt against the identical snapshot into the
    identical refusal. ``FailureAction.FAIL`` is written for exactly
    that: "a wall retrying can never fix... fail directly without
    burning engineer iterations".

    Reaching here at all means the component got past
    ``factory``'s two pre-engineer refusals, which is the cheap place to
    catch this and where the cost is actually saved. This is the
    backstop for a caller that has neither.

    Any other failing check alongside it still fails rather than
    retries: a readable scope is a precondition for judging the rest, so
    an unreadable one is decisive whatever else also failed. A check
    that is NOT it retries exactly as before.

    Its own function so ``_phase_verify`` spends no cognitive complexity
    on the choice: that method is already over the repo's gate and is
    judged against its own previous value, so ternaries inline there are
    a refusal at commit time.
    """
    for check in failing:
        if check.name == SCOPE_UNREADABLE_CHECK:
            cause = check.details[0] if check.details else check.message
            return FailureAction.FAIL, scope_unreadable_error(cause)
    return FailureAction.RETRY_OR_FAIL, "Mechanical verification failed"


class ComponentPipeline:
    """Drives one component result through the phase chain and owns every
    component state transition (R7.3).

    Shared mutable structures (``worktree_paths``, ``component_contexts``,
    ``fresh_base_retry_ids``, ``component_failure_signatures``,
    ``factory_result``) are passed in by the factory and shared with its
    scheduler; the pipeline is the only writer for transition-related
    fields, the scheduler for provisioning-related ones.
    """

    def __init__(
        self,
        *,
        manifest: Manifest,
        manifest_path: Path,
        factory_config: FactoryConfig,
        base_config: KstrlConfig,
        ui: UI,
        root_dir: Path,
        run_id: str,
        bus: ev.EventBus,
        journal_path: Path | None,
        run_paths: ev.RunPaths | None = None,
        usage_paths: ev.RunPaths | None = None,
        interaction: InteractionChannel | None = None,
        notify: NotifyHooks,
        review_selection: AdversarialAgentSelection,
        security_selection: AdversarialAgentSelection | None,
        knowledge_config: KnowledgeConfig,
        factory_result: FactoryResult,
        run_scope: RunScope,
        hooks: PipelineHooks,
        worktree_paths: dict[str, Path],
        component_contexts: dict[str, str],
        fresh_base_retry_ids: set[str],
        component_failure_signatures: dict[str, list[str]],
    ) -> None:
        self.manifest = manifest
        self.manifest_path = manifest_path
        self.factory_config = factory_config
        self.base_config = base_config
        self.ui = ui
        self.root_dir = root_dir
        # #266 review finding 3: the reviewer roles were built with
        # read_only=True and NO sandbox, so `[sandbox] enabled = true`
        # reached the engineer and never the reviewers - the one pair of
        # roles that now runs shell commands inside the tree under
        # review. read_only is a permission-layer posture on the claude
        # adapters; the operator's OS-level enforcement is a separate
        # payload and both are wanted. Loaded once here rather than per
        # phase: it is a run-level setting and the phases run per
        # component attempt.
        self.sandbox_config = SandboxConfig.load(root_dir)
        self.run_id = run_id
        self.bus = bus
        self.journal_path = journal_path
        self.run_paths = run_paths
        # R8: where engineer-loop usage snapshots live. Deliberately
        # SEPARATE from run_paths, which is None when progress logging
        # is off: accounting must survive the observability opt-out
        # (review finding P2-d). None only for callers that never run
        # the abort-salvage path (tests, embedded pipelines).
        self.usage_paths = usage_paths
        # PR A: the interaction seam. Defaults to today's terminal
        # behavior; embedded mode (PR F) injects a QueueInteractionChannel.
        self.interaction: InteractionChannel = (
            interaction if interaction is not None else UiInteractionChannel(ui)
        )
        self.notify = notify
        # R8.3: lazily built so a disabled inbox costs nothing and a
        # broken one can never fail a run (see _inbox_add).
        self._inbox: Inbox | None = None
        self._inbox_disabled = False
        self._inbox_typed: set[str] = set()
        self.review_selection = review_selection
        self.security_selection = security_selection
        self.knowledge_config = knowledge_config
        self.factory_result = factory_result
        # #269: the run's plan-time scope snapshot. The pipeline READS
        # it and never resolves one of its own, which is what stops
        # Phase 1 and the in-loop guard drifting apart.
        self.run_scope = run_scope
        self.hooks = hooks
        self.worktree_paths = worktree_paths
        self.component_contexts = component_contexts
        self.fresh_base_retry_ids = fresh_base_retry_ids
        self.component_failure_signatures = component_failure_signatures

        # R3.1 cost meter: per-component, per-phase usage rollup plus a
        # run-level total. Phases: "engineer" (loop iterations, reported
        # by the worker), "review", "security", "distill" (fresh agent
        # instance per phase, so an instance's accumulated usage_records
        # ARE that phase's spend). Retried attempts accumulate: every
        # attempt cost real tokens, so the meter never forgets a failed
        # attempt.
        self.usage_meter: dict[str, dict[str, UsageTotals]] = {}
        # #191: the knowledge prefix each component's engineer ACTUALLY
        # saw, captured by the factory at submit time. Keyed by
        # component id and overwritten every attempt. None means the
        # capture failed or knowledge was off, which is NOT the same as
        # "" ("knowledge on, store empty, nothing to inject").
        self.injected_knowledge: dict[str, str | None] = {}
        # #191: fact-utilization per component, read at run end by the
        # evolution journal. The journal - not the event stream - is the
        # durable L2+ gate evidence.
        self.fact_utilization: dict[str, FactUtilization] = {}
        # #265: one reading per attempt whose REVIEWER ran and failed the
        # component, keyed by component id. Named for the phase rather
        # than for the change: the predicate is phase-agnostic, so a
        # second phase wiring into it later must get its own store or it
        # would compare one phase's finding identities against another's.
        #
        # In-run only, deliberately: `ks retry` already resets `retries`
        # and clears the finding stream, so an operator's explicit retry
        # is meant to start from a clean slate and this history must not
        # outlive it. It also stays out of IterationContext, which is
        # rendered into the engineer's prompt - a cost governor has no
        # business in the agent's context.
        self.review_readings: dict[str, list[AttemptReading]] = {}
        # Components whose usage snapshot could not be retired
        # before this attempt launched; disk salvage is refused
        # for them (R8 review P2 on 22e99b4).
        self._usage_salvage_unsafe: set[str] = set()
        self.run_usage = UsageTotals()

        # R8 (measured): ceilings whose coverage gap has already been
        # announced this run. One warning per ceiling - the gap only
        # widens, and a line per phase would train the operator to
        # ignore it. The final numbers travel with the halt and the
        # rollup.
        self._coverage_announced: set[str] = set()

        # E4: adversarial-call counter shared across review / security /
        # knowledge phases. When max_adversarial_calls is 0 the budget is
        # unbounded. Otherwise, once it is exhausted: a hard-mode review
        # or security phase REFUSES and the component fails (R10.5,
        # #226), an advisory one is skipped with a warning and a
        # recorded phase_skipped, and the distiller is always skipped
        # because it gates nothing.
        self._adversarial_calls = 0

        # R6.4: monotonic start of each component's current attempt, so
        # the recorded duration covers the whole attempt (engineer loop +
        # verify + review + security + PR flow), not just the engineer
        # loop, and backstop-timeout failures stop recording 0.0.
        self._attempt_started_monotonic: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Budget + usage accounting
    # ------------------------------------------------------------------

    def adversarial_budget_ok(self) -> bool:
        cap = self.factory_config.max_adversarial_calls
        if cap <= 0:
            return True
        return self._adversarial_calls < cap

    def adversarial_budget_consume(self) -> None:
        self._adversarial_calls += 1

    def adversarial_budget_remaining(self) -> int | None:
        """Calls left in the budget, or None when unbounded."""
        cap = self.factory_config.max_adversarial_calls
        if cap <= 0:
            return None
        return max(0, cap - self._adversarial_calls)

    def _record_usage(
        self,
        comp_id: str,
        phase: str,
        totals: UsageTotals,
    ) -> None:
        if totals.calls == 0:
            return
        slot = self.usage_meter.setdefault(comp_id, {}).setdefault(
            phase,
            UsageTotals(),
        )
        slot.merge(totals)
        self.run_usage.merge(totals)
        self.bus.emit(
            ev.ComponentUsage(
                component=comp_id,
                phase=phase,
                **totals.to_dict(),
            )
        )
        self._announce_coverage_gaps()

    def _announce_coverage_gaps(self) -> None:
        """Report the FIRST time a configured ceiling stops covering
        every metered call (R8, measured).

        Evaluated on every usage capture rather than at the halt,
        because a coverage fact delivered with the halt arrives after
        the money is spent. The earliest honest moment is the phase that
        first reports nothing on that axis - typically the first
        cross-family review, a few minutes into a run.

        Adds no failure mode to ``_record_usage``: the work is integer
        arithmetic over the meter plus one ``bus.emit``, and
        ``EventBus.emit`` already isolates sink exceptions. The meter
        must never gate correctness (R3.1 requirement 4).
        """
        for ceiling in CEILING_AXES:
            if ceiling in self._coverage_announced:
                continue
            coverage = self.ceiling_coverage(ceiling)
            if coverage is None or coverage.calls == 0 or coverage.complete:
                continue
            self._coverage_announced.add(ceiling)
            detail = coverage.note()
            self.ui.warn(f"  BUDGET COVERAGE: {detail}")
            self.bus.emit(
                ev.BudgetCoverage(
                    ceiling=coverage.ceiling,
                    axis=coverage.axis,
                    calls=coverage.calls,
                    covered_calls=coverage.covered_calls,
                    uncovered_calls=coverage.uncovered_calls,
                    uncovered_tokens=coverage.uncovered_tokens,
                    uncovered_roles=coverage.uncovered_roles,
                    detail=detail,
                )
            )

    def record_engineer_usage(
        self,
        comp_id: str,
        totals: UsageTotals,
    ) -> None:
        """Record engineer spend that never came back through
        process_result (R8 abort path). The worker was killed, so its
        records reach the meter here or not at all; the caller
        guarantees the matching future produced no result, so this can
        never double count with the normal path."""
        self._record_usage(comp_id, "engineer", totals)

    def record_architect_usage(self, totals: UsageTotals | None) -> None:
        """Fold the architect's spend into this run before it starts (#257).

        Every other role is metered by a phase this pipeline drives. The
        architect is not: `ks factory` decomposes the spec in the command
        itself, before any run id or run directory exists, and only then
        builds this pipeline. Its spend therefore has to be handed in
        rather than captured, which is what ``architect_usage`` on
        ``run_factory`` carries.

        It goes through the ordinary ``_record_usage`` path, and that is
        the entire point of the seat. ``run_usage`` is what
        :meth:`cost_budget_exceeded` reads, so an operator's
        ``--max-cost-usd`` now bounds the architect too instead of
        bounding the four roles that follow it; the meter gains a fifth
        row; and ``_announce_coverage_gaps`` counts the architect as a
        metered call rather than leaving it invisible to the coverage
        accounting.

        The component id is ``ARCHITECT_COMPONENT``, the namespaced key
        `ks decompose` already writes and the one
        ``serve.read_run_spend`` reads; the phase is the bare
        ``ARCHITECT_ROLE``. Pairing them HERE is why the constants exist
        rather than a literal per surface.

        They stopped being the same string in #281. A bare component key
        shared a keyspace with LLM-emitted component ids, so a component
        genuinely named `architect` merged with this row - folding its
        spend into the architect's, and clearing the honesty flag on
        ``serve.RunSpend`` for a run whose architect never reported.

        ``None`` or zero calls records nothing: a run resumed from a
        manifest never ran an architect, and an agent that reported no
        usage must not become a phantom row claiming it cost nothing.
        """
        if totals is None:
            return
        self._record_usage(ARCHITECT_COMPONENT, ARCHITECT_ROLE, totals)

    def record_injected_knowledge(
        self,
        comp_id: str,
        prefix: str | None,
    ) -> None:
        """Record the knowledge prefix handed to ``comp_id``'s engineer.

        Called by the factory at submit time (#191), unconditionally -
        including with ``None`` when retrieval failed or knowledge is
        off. Unconditional is the point: ``_submit_args`` runs once per
        ATTEMPT, so a retry whose capture fails must not silently
        inherit the previous attempt's prefix and measure this
        attempt's diff against it.

        Freezing the prefix here is also what makes the measurement
        honest. The distill phase used to rebuild it, by which time
        ``distill_facts`` had written this run's facts into the store
        and the core tier read them straight back - counting facts the
        engineer never saw, and (since those facts are distilled FROM
        the diff) matching them against it. A parallel sibling writing
        mid-run corrupted the dependency and sibling tiers the same
        way. Neither is reachable from a submit-time snapshot.
        """
        self.injected_knowledge[comp_id] = prefix

    def record_fact_utilization(
        self,
        comp: Component,
        wt_path: Path,
        diff_text: str | None = None,
        *,
        unavailable: str = "",
    ) -> None:
        """Measure, store and emit this attempt's fact utilization (#191).

        Called from ``process_result`` as soon as a diff is obtainable,
        NOT from the distill phase. Distillation runs only after
        verification, review, and security all pass, so measuring there
        sampled successful components exclusively - a component that
        failed any gate had facts injected and may well have used them,
        and the gate never saw it.

        Pass ``diff_text`` when the diff phase already fetched it. Pass
        nothing to have the diff fetched here: a mechanical-verification
        failure means ``_phase_diff`` has not run YET, not that no diff
        exists - the engineer's change is committed in the worktree and
        is exactly what verification just inspected. Excluding those
        components would leave every test, typecheck, and lint failure
        out of the sample, which is most of the failure population. The
        extra ``git diff`` only runs on that path and is cheap next to
        the component run that preceded it.

        ``unavailable`` records a component as unmeasured with a stated
        reason rather than letting it go silently absent; it is used
        when the diff phase itself already failed, so we do not re-run a
        fetch that is known to fail.

        The measurement is free (a substring scan, no LLM call), so it
        sits above every budget guard. Overwrites per attempt, matching
        ``record_injected_knowledge``.
        """
        if not self.knowledge_config.enabled:
            return
        if self.manifest.single_pr:
            # That mode's shared diff carries sibling components'
            # changes, which would inflate `referenced` - the same
            # reason distillation skips it.
            return
        if unavailable:
            self._store_fact_utilization(
                comp,
                FactUtilization(
                    reason=unavailable,
                ),
            )
            return
        if diff_text is None:
            try:
                diff_text = git.get_diff_content(
                    self.manifest.base_branch,
                    wt_path,
                )
            except git.GitDiffError as exc:
                self._store_fact_utilization(
                    comp,
                    FactUtilization(
                        reason=f"diff unavailable: {exc}",
                    ),
                )
                return
        self._store_fact_utilization(
            comp,
            self._measure_utilization(comp, wt_path, diff_text),
        )

    def _store_fact_utilization(
        self,
        comp: Component,
        util: FactUtilization,
    ) -> None:
        """Persist one measurement and put it on the event stream.

        The event is emitted HERE, not from the distill phase, so that
        `events.jsonl` carries the same population `evolution.jsonl`
        does. Emitting it from the distill phase meant a component that
        failed review, or whose distill was budget-skipped or raised,
        landed in the journal with evidence the event stream never saw -
        and #191 requires the ratio in both.
        """
        self.fact_utilization[comp.id] = util
        self.bus.emit(
            ev.FactUtilizationMeasured(
                component=comp.id,
                measured=util.measured,
                injected=util.injected,
                referenced=util.referenced,
                reason=util.reason,
                core_injected=util.core_injected,
                core_referenced=util.core_referenced,
                dependency_injected=util.dependency_injected,
                dependency_referenced=util.dependency_referenced,
                sibling_injected=util.sibling_injected,
                sibling_referenced=util.sibling_referenced,
            )
        )
        if util.measured and util.injected > 0:
            self.ui.info(
                f"  Knowledge utilization: "
                f"{util.referenced}/{util.injected} "
                f"facts referenced in added lines or progress.txt"
                + (
                    f" (core {util.core_referenced}/{util.core_injected})"
                    if util.core_injected
                    else ""
                )
            )

    def mark_usage_salvage_safe(self, comp_id: str) -> None:
        """The attempt's usage snapshot slot is provably clean."""
        self._usage_salvage_unsafe.discard(comp_id)

    def mark_usage_salvage_unsafe(self, comp_id: str) -> None:
        """A stale snapshot may survive for this attempt, so disk
        salvage must not run for it (R8 review: deletion IS the
        attempt-scoping invariant; when it fails, what is on disk may
        already have been counted by ``process_result``)."""
        self._usage_salvage_unsafe.add(comp_id)

    def usage_salvage_is_safe(self, comp_id: str) -> bool:
        return comp_id not in self._usage_salvage_unsafe

    def engineer_usage_totals(self) -> UsageTotals:
        """Engineer-loop spend across every component and attempt.

        Feeds the loop-side budget's tokenless-call threshold (R8). That
        threshold asks "does the ENGINEER's adapter report tokens?", so
        it must not be answered with run-wide totals: a timed-out
        architect or reviewer call is tokenless too, and counting those
        let two unrelated timeouts condemn an engineer adapter that had
        been reporting perfectly well - while the halt message asserted
        the cap "can never trip on this adapter". Engineer-scoped, the
        counter still survives the case it exists for
        (``max_iterations = 1`` and retries, where a per-loop counter
        resets before it can conclude anything).

        The OVERRUN half stays run-wide: that one asks what the RUN has
        spent against the cap, which is every phase's business.
        """
        totals = UsageTotals()
        for phases in self.usage_meter.values():
            engineer = phases.get("engineer")
            if engineer is not None:
                totals.merge(engineer)
        return totals

    def usage_totals_for(self, comp_id: str) -> UsageTotals:
        """One component's spend across all phases (PR A: shown at the
        E6 checkpoint so the human sees what the attempt cost)."""
        totals = UsageTotals()
        for phase_totals in self.usage_meter.get(comp_id, {}).values():
            totals.merge(phase_totals)
        return totals

    def token_budget_exceeded(self) -> bool:
        cap = self.factory_config.max_total_tokens
        return cap > 0 and self.run_usage.total_tokens >= cap

    def cost_budget_exceeded(self) -> bool:
        """R8: the run's reported USD spend has reached ``max_cost_usd``.

        Separate from :meth:`token_budget_exceeded` because the two
        ceilings measure genuinely different things. Measured on a real
        run: 1,864,081 total tokens (95.6% of them cache reads, which
        ``total_tokens`` counts at par) cost $1.22, so a token ceiling is
        a poor proxy for spend.
        """
        cap = self.factory_config.max_cost_usd
        return cap > 0 and self.run_usage.cost_usd >= cap

    def breached_ceiling(self) -> str | None:
        """Which configured ceiling the run has reached, or None.

        Returns the config key name (``"max_total_tokens"`` /
        ``"max_cost_usd"``) so every audit surface can NAME the ceiling
        that tripped instead of asserting "token budget" for both. When
        both are over at the same evaluation the token one is named
        first - an arbitrary but fixed order; over time whichever is
        reached first halts, because the gates run continuously.
        """
        if self.token_budget_exceeded():
            return "max_total_tokens"
        if self.cost_budget_exceeded():
            return "max_cost_usd"
        return None

    def budget_exceeded(self) -> bool:
        """Any configured run-level ceiling has been reached."""
        return self.breached_ceiling() is not None

    def token_budget_unenforceable(self) -> str | None:
        """Why the TOKEN cap can no longer fire at all, or None.

        The parent-side twin of :meth:`LoopBudget.halt_reason`'s
        unenforceable branch, and it exists because the in-loop check has
        a blind spot the loop cannot cover itself: a loop that emits
        COMPLETE returns BEFORE evaluating its budget, so an adapter that
        finishes on its first tokenless call never reaches the halt.
        That is the ordinary success path for a custom ``agent_cmd``, not
        an artificial case - each component completes, the engineer's
        tokenless count climbs, and the cap never fires (review finding
        on 22e99b4; the previous docstring's "the halt lands on the next
        loop" was simply false when the next loop also completes).

        Per-ceiling by construction: a dead TOKEN cap is not on its own a
        reason to stop when a live COST cap is also configured. The
        scheduling gate consults :meth:`budget_unenforceable`, which
        halts only when EVERY configured ceiling is dead.

        Checked at the scheduling gate, so the run stops handing out NEW
        work under a dead cap. Deliberately does not retroactively fail
        components that already completed: their work is valid, and the
        cap's job is to stop spending, not to destroy what was bought.
        Bound: at most the component in flight when the determination
        lands.
        """
        cap = self.factory_config.max_total_tokens
        if cap <= 0:
            return None
        engineer = self.engineer_usage_totals()
        if engineer.token_calls > 0:
            return None
        if engineer.tokenless_calls < UNENFORCEABLE_CALLS:
            return None
        return (
            f"token budget unenforceable: the engineer has made "
            f"{engineer.tokenless_calls} agent call(s) this run and none "
            f"reported a token count, so max_total_tokens ({cap}) cannot "
            "advance; refusing to schedule further components rather than "
            "spending under a cap that cannot fire (R8)"
        )

    def cost_budget_unenforceable(self) -> str | None:
        """Why the COST cap can no longer fire at all, or None.

        The cost mirror of :meth:`token_budget_unenforceable`, reading
        ``cost_calls`` rather than ``token_calls``. The two answers are
        independent in both directions: the codex adapter reports a token
        total and no cost (token ceiling alive, cost ceiling dead), the
        claude adapter can report ``total_cost_usd`` with no ``usage``
        dict (cost ceiling alive, token ceiling dead).
        """
        cap = self.factory_config.max_cost_usd
        if cap <= 0:
            return None
        engineer = self.engineer_usage_totals()
        if engineer.cost_calls > 0:
            return None
        if engineer.costless_calls < UNENFORCEABLE_CALLS:
            return None
        return (
            f"cost budget unenforceable: the engineer has made "
            f"{engineer.costless_calls} agent call(s) this run and none "
            f"reported a cost, so max_cost_usd (${cap}) cannot advance; "
            "refusing to schedule further components rather than spending "
            "under a cap that cannot fire (R8)"
        )

    def ceiling_coverage(self, ceiling: str) -> CeilingCoverage | None:
        """What fraction of the run's metered calls ``ceiling`` counts.

        None when that ceiling is not configured (nothing to qualify) or
        the key is not a ceiling.

        The middle term the R8 ceilings were missing. ``*_budget_exceeded``
        answers "was it breached", ``*_budget_unenforceable`` answers "can
        it ever fire"; both were true-or-false over the WHOLE run, and a
        ceiling that counts some roles and not others is neither. Measured
        on a real run: the engineer reported cost on every call, the
        cross-family reviewer reported tokens and no cost on 5, and the
        run's cost total equalled the engineer's exactly - a $25 ceiling
        that bounded one role while every existing surface reported it as
        healthy.

        Deliberately does NOT change what the ceiling counts. Converting
        the uncovered calls' tokens to dollars would need a price table
        this repo does not have and must not invent (a fabricated cost in
        an audit trail is worse than a missing one). What changes is that
        the gap is now stated instead of implied.
        """
        axis = CEILING_AXES.get(ceiling)
        if axis is None:
            return None
        if ceiling == "max_cost_usd" and self.factory_config.max_cost_usd <= 0:
            return None
        if ceiling == "max_total_tokens" and self.factory_config.max_total_tokens <= 0:
            return None
        return usage_coverage(self.usage_meter, axis=axis, ceiling=ceiling)

    def coverage_notes(self, ceilings: Sequence[str]) -> list[str]:
        """Operator sentences for the named ceilings that fall short.

        Empty when every named ceiling counted every call, so a
        fully-covered halt reads exactly as it did before.
        """
        notes: list[str] = []
        for ceiling in ceilings:
            coverage = self.ceiling_coverage(ceiling)
            if coverage is None:
                continue
            note = coverage.note()
            if note:
                notes.append(note)
        return notes

    def unenforceable_ceilings(self) -> list[str]:
        """Configured ceilings that can no longer fire, by config key.

        The identity half of :meth:`budget_unenforceable`. An
        unenforceable halt crosses no numeric threshold, so
        :meth:`breached_ceiling` correctly returns None for it - and
        every audit surface downstream then rendered the empty string as
        "token budget", naming a knob that may not even be configured
        (review finding on #180: a cost-only run on a costless adapter
        reported "run token budget exceeded (200/0)"). The ceiling that
        FAILED still has a name even when nothing was breached.
        """
        dead: list[str] = []
        if (
            self.factory_config.max_total_tokens > 0
            and self.token_budget_unenforceable() is not None
        ):
            dead.append("max_total_tokens")
        if self.factory_config.max_cost_usd > 0 and self.cost_budget_unenforceable() is not None:
            dead.append("max_cost_usd")
        return dead

    def budget_halt_identity(self) -> tuple[str, tuple[str, ...]]:
        """``(condition, ceilings)`` for a halt derived from run totals.

        The precedence rule lives HERE, once, because both call sites got
        it wrong when they each spelled it out: a numeric breach and a
        dead ceiling can coexist (a run whose token cap trips while its
        cost cap never received a figure), and joining the dead list
        first named ``max_cost_usd`` for a halt the TOKEN cap caused -
        which then rendered as ``cost budget exceeded: $0 >= $100``, a
        sentence that is both false and arithmetically impossible
        (review finding on #180).

        A breach is the stronger fact: it is a threshold that was
        actually crossed, with numbers behind it. Dead ceilings are only
        consulted when nothing was breached.
        """
        breached = self.breached_ceiling()
        if breached is not None:
            return ("breached", (breached,))
        dead = self.unenforceable_ceilings()
        if dead:
            return ("unenforceable", tuple(dead))
        return ("", ())

    def budget_unenforceable(self) -> str | None:
        """Why NO configured ceiling can fire any more, or None.

        The scheduling gate's question. Halts only when EVERY configured
        ceiling is dead: an adapter that reports cost but not tokens can
        still enforce ``max_cost_usd``, and stopping the run because the
        token ceiling died would discard a ceiling that still works.
        With no ceiling configured there is nothing to enforce and this
        is always None.
        """
        reasons = [
            reason
            for reason in (
                self.token_budget_unenforceable(),
                self.cost_budget_unenforceable(),
            )
            if reason is not None
        ]
        configured = sum(
            (
                self.factory_config.max_total_tokens > 0,
                self.factory_config.max_cost_usd > 0,
            )
        )
        if not reasons or len(reasons) < configured:
            return None
        return "; ".join(reasons)

    # ------------------------------------------------------------------
    # Attempt lifecycle + evidence pointers (R3.3)
    # ------------------------------------------------------------------

    def _journal_offset(self) -> int:
        """Current byte size of the v1 progress log; used to bracket one
        attempt's slice of events (R3.3). -1 when no real progress log
        is configured for this run. Deliberately pegged to the v1 compat
        file, NOT events.jsonl - the manifest's journal_offset_start/end
        semantics must not silently repoint (plan: explicit future
        schema decision)."""
        if self.journal_path is None:
            return -1
        try:
            return self.journal_path.stat().st_size if self.journal_path.exists() else 0
        except OSError:
            return -1

    @contextmanager
    def _phase_transcript(
        self,
        comp_id: str,
        phase: str,
    ) -> Iterator[Callable[[str], None] | None]:
        """Line writer onto RunPaths.phase_log for one phase invocation.

        Yields None when no run dir is configured (progress logging
        disabled) or the file cannot be opened - transcripts are
        observability and must never gate a phase. Repeated
        invocations (retries) append.
        """
        if self.run_paths is None:
            yield None
            return
        path = self.run_paths.phase_log(comp_id, phase)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a", buffering=1, encoding="utf-8")
        except OSError:
            yield None
            return

        def _write_line(line: str) -> None:
            fh.write(line + "\n")

        try:
            yield _write_line
        finally:
            try:
                fh.close()
            except OSError:
                pass

    def _phase_started(self, comp: Component, phase: str) -> float:
        """Emit the authoritative phase bracket opener; returns the
        monotonic start for the matching _phase_completed."""
        self.bus.emit(
            ev.PhaseStarted(
                component=comp.id,
                phase=phase,
                attempt=comp.retries + 1,
            )
        )
        return time.monotonic()

    def _phase_completed(
        self,
        comp: Component,
        phase: str,
        started: float,
        passed: bool,
        detail: str = "",
    ) -> None:
        self.bus.emit(
            ev.PhaseCompleted(
                component=comp.id,
                phase=phase,
                passed=passed,
                detail=detail,
                duration_seconds=round(time.monotonic() - started, 2),
            )
        )

    def _debug_dir_for(self, comp_id: str) -> Path:
        """Forensic raw-output dir for this run's component (R1.2)."""
        return self.root_dir / ".kstrl" / "debug" / self.run_id / comp_id

    def _add_findings(
        self,
        comp: Component,
        new_findings: list[Finding],
    ) -> None:
        """Append findings tagged ``attempt:<n>`` for the attempt in
        flight (R3.3), so the journal can attribute every finding to the
        attempt that produced it."""
        attempt = comp.retries + 1
        comp.findings.extend(tag_finding_with_attempt(f, attempt) for f in new_findings)
        # Chunk 4: stream each finding as a typed event the moment it is
        # recorded (the manifest only carries them at transition time).
        for finding in new_findings:
            self.bus.emit(
                ev.FindingRecorded(
                    component=comp.id,
                    phase=finding.phase,
                    category=finding.category,
                    severity=finding.severity,
                    location=finding.location,
                    explanation=finding.explanation,
                    attempt=attempt,
                    model=finding_model(finding) or "",
                )
            )

    def begin_attempt(self, comp: Component) -> None:
        """PENDING -> RUNNING transition for one attempt (R3.3).

        The prior attempt's findings were journaled when its retry was
        scheduled (or by record_run when a previous run ended), so the
        manifest carries only the current attempt's stream; the failure
        and evidence pointers likewise describe only the attempt in
        flight."""
        comp.findings = []
        comp.review_findings = ""
        comp.failed_phase = ""
        comp.failed_check = ""
        comp.completed_at = ""
        comp.evidence_worktree = ""
        comp.evidence_debug_dir = ""
        comp.journal_offset_start = self._journal_offset()
        comp.journal_offset_end = -1
        comp.status = ComponentStatus.RUNNING.value
        comp.started_at = _iso_now()
        self.component_failure_signatures.pop(comp.id, None)
        self._attempt_started_monotonic[comp.id] = time.monotonic()

    def _end_attempt(self, comp: Component) -> None:
        """Stamp the attempt's evidence pointers when it stops running:
        the progress-log slice end, and the debug dir when any phase
        dumped raw output there (R3.3). Also stamp the attempt's full
        wall-clock duration (R6.4): every terminal transition (retry,
        fail, merge-pending, completed, scheduler backstop) routes
        through here, so duration_seconds covers engineer + verify +
        review + security + PR instead of the engineer loop only."""
        comp.journal_offset_end = self._journal_offset()
        started = self._attempt_started_monotonic.get(comp.id)
        if started is not None:
            comp.duration_seconds = time.monotonic() - started
        debug_dir = self._debug_dir_for(comp.id)
        if debug_dir.exists():
            comp.evidence_debug_dir = str(debug_dir)

    def journal_superseded_findings(self, comp: Component) -> None:
        """A scheduled retry supersedes the current attempt's findings.
        Record them in the evolution journal (attempt-tagged) before the
        next attempt clears the manifest stream, so superseded and
        shipped findings stay distinguishable (R3.3). The final
        attempt's findings reach the journal via record_run instead.
        Non-fatal on I/O errors, matching _record_contract_event."""
        if not comp.findings:
            return
        from kstrl.evolution import JOURNAL_SCHEMA_VERSION, EvolutionJournal

        journal = EvolutionJournal.open(self.root_dir, warn=self.ui.warn)
        if journal is None:
            return
        entry = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "timestamp": _iso_now(),
            "run_id": self.run_id,
            "project": self.manifest.project_name,
            "component_id": comp.id,
            "event_type": "findings_superseded",
            "attempt": comp.retries + 1,
            "failure_signatures": self.component_failure_signatures.get(
                comp.id,
                [],
            ),
            "findings": [f.to_dict() for f in comp.findings],
        }
        try:
            journal.append_entries([entry])
        except OSError as exc:
            # Evolution recording is non-fatal, but never silent (R6.1).
            self.ui.warn(f"  Evolution journal write failed (non-fatal): {exc}")

    # ------------------------------------------------------------------
    # Transitions (the single place component state moves)
    # ------------------------------------------------------------------

    def _record_failure_signatures(
        self,
        comp: Component,
        phase: str,
        error: str,
        signatures: list[str] | None,
    ) -> None:
        """R6.1: remember the structured signatures for this failure so
        record_run journals real "<check>:<code>" identifiers instead of
        re-deriving a degenerate slug from the flattened error string.
        Sites without parser-level codes fall back to a slug of the
        error text under the failing phase."""
        from kstrl.evolution import signature_for_error

        if signatures:
            self.component_failure_signatures[comp.id] = list(signatures)
        else:
            self.component_failure_signatures[comp.id] = [
                signature_for_error(phase or "unknown", error),
            ]

    def retry_or_fail(
        self,
        comp: Component,
        error: str,
        context_json: str | None,
        phase: str = "",
        check: str = "",
        signatures: list[str] | None = None,
        fresh_base: bool = False,
    ) -> Transition:
        """Retry a component or mark it as failed. ``phase``/``check``
        name the gate that fired (R3.3); on a retry they describe the
        superseded attempt until the next attempt clears them.
        ``signatures`` are the structured failure signatures (R6.1).
        ``fresh_base=True`` (R7.5 merge-conflict doctrine) forces the
        retry to recreate the worktree AND branch from the freshly
        merged base instead of resuming the attempt's commits."""
        self._record_failure_signatures(comp, phase, error, signatures)
        if comp.retries < self.factory_config.max_retries:
            if fresh_base and self.factory_config.use_worktrees:
                self.fresh_base_retry_ids.add(comp.id)
                error = (
                    error + " [conflict retry: component re-run against the "
                    "freshly merged base; agent output is not rebased]"
                )
            # A timeout failure means the agent was killed mid-flight: the
            # worktree/branch state cannot be trusted. Note the hygiene
            # behavior in the error string so the audit trail explains why
            # the retry does not resume from the killed attempt's commits.
            elif "timeout" in error.lower() and self.factory_config.use_worktrees:
                self.fresh_base_retry_ids.add(comp.id)
                error = (
                    error + " [timeout retry: worktree recreated from base; "
                    "stale index.lock removed]"
                )
            # R3.3: journal this attempt's findings as superseded BEFORE
            # the retry counter moves (the tag and the journal entry
            # must agree on the attempt number), then stamp the
            # attempt's evidence pointers.
            self.journal_superseded_findings(comp)
            self._end_attempt(comp)
            comp.failed_phase = phase
            comp.failed_check = check
            comp.retries += 1
            comp.status = ComponentStatus.PENDING.value
            comp.error = error
            if context_json:
                self.component_contexts[comp.id] = context_json
            self.bus.emit(
                ev.ComponentRetrying(
                    component=comp.id,
                    attempt=comp.retries,
                    reason=error,
                )
            )
            self.ui.info(
                f"  Retrying '{comp.id}' "
                f"(attempt {comp.retries}/{self.factory_config.max_retries}): "
                f"{error[:80]}"
            )
            time.sleep(self.factory_config.retry_delay)
            self.manifest.save(self.manifest_path)
            return Transition.RETRYING
        return self.fail(
            comp,
            error,
            phase=phase,
            check=check,
            signatures=signatures,
        )

    def fail_aborted(self, comp_id: str, reason: str) -> None:
        """PR B: a shutdown aborted this component's in-flight attempt.
        Recorded as a plain FAILED with phase="aborted" so a resume can
        retry it; distinct from every organic failure signature."""
        comp = self.manifest.get_component(comp_id)
        if comp is None:
            return
        self.fail(
            comp,
            f"aborted: {reason}",
            phase="aborted",
            check="shutdown",
            signatures=["aborted:shutdown"],
        )

    def fail(
        self,
        comp: Component,
        error: str,
        phase: str = "",
        check: str = "",
        signatures: list[str] | None = None,
    ) -> Transition:
        """Mark a component FAILED with no retry. Direct callers are
        conditions a retry can never fix (the adversarial budget only
        shrinks, so re-running the engineer would burn LLM calls to hit
        the same wall); retry_or_fail routes here once retries are
        exhausted."""
        self._record_failure_signatures(comp, phase, error, signatures)
        comp.status = ComponentStatus.FAILED.value
        comp.error = error
        comp.completed_at = _iso_now()
        comp.failed_phase = phase
        comp.failed_check = check
        self._end_attempt(comp)
        skipped = self.manifest.cascade_skip(comp.id)
        self.factory_result.failed.append(comp.id)
        self.factory_result.skipped.extend(skipped)
        self.bus.emit(ev.ComponentFailed(component=comp.id, error=error))
        self.notify.fire_first_failure(comp.id, error)
        if comp.id in self._inbox_typed:
            self._inbox_typed.discard(comp.id)
        else:
            self._inbox_add(
                ItemKind.HALTED_RUN,
                f"{comp.id} halted in {phase}",
                detail=error,
                component=comp.id,
                dedupe_key=f"halted:{comp.id}:{phase}:{check}",
                evidence={"phase": phase, "check": check, "error": error},
            )
        self.ui.err(f"  Failed: {comp.id}: {error[:80]}")
        self.manifest.save(self.manifest_path)
        return Transition.FAILED

    def fail_for_budget(
        self,
        comp: Component,
        phase: str,
        reason: str = "",
        condition: str = "",
        ceilings: tuple[str, ...] = (),
    ) -> Transition:
        """R3.1/R8: halt LOUDLY on a blown run-level ceiling. Mirrors the
        R1.2 synthetic-finding pattern: a typed Finding in the
        stream, a progress-log event, and a FAILED
        component - never a silent degrade. Retrying cannot un-spend what
        was spent, so this fails directly instead of burning retries.

        The message and the finding NAME the ceiling that tripped
        (``max_total_tokens`` or ``max_cost_usd``). With two ceilings a
        message that always said "token budget" would send the operator
        to raise the wrong knob.

        ``reason`` (R8) overrides the derived message when the breach
        was detected somewhere the parent's own totals do not describe -
        specifically the engineer loop's in-loop halt on unreportable
        usage, where ``run_usage`` shows no breach and stating one would
        put a false number in the audit trail. That string is produced by
        :meth:`LoopBudget.halt_reason`, which names its own ceiling.
        Every other side effect is identical wherever the breach is
        caught.

        R8 (measured): the message and the event also state what the
        named ceilings COVER when they do not cover everything. A run
        whose engineer reported cost and whose cross-family reviewer
        reported tokens and no cost halted on a total that equalled the
        engineer's exactly - 193,633 reviewer tokens contributed $0 -
        while the sentence said only "cost budget exceeded". The number
        was true and the sentence was misleading, so the coverage is now
        attached where the ceiling is evaluated."""
        # An identity supplied by the caller wins: the engineer loop
        # detects its own halt against priors the parent's totals do not
        # describe, so only the loop knows what actually fired there.
        # Everything else derives from run totals under the single
        # precedence rule in budget_halt_identity().
        if not ceilings:
            condition, ceilings = self.budget_halt_identity()
        # Only a BREACH licenses a "N >= cap" sentence. An unenforceable
        # halt crossed nothing, and the derived comparison read
        # "token budget exceeded: 0 >= 500" for runs where the totals
        # never moved (review finding on #180).
        if reason:
            error = reason
        elif condition == "unenforceable":
            error = (
                f"budget ceiling unenforceable ({', '.join(ceilings)}): no "
                "configured ceiling can still fire, so the run cannot be "
                "bounded; halting rather than spending under a cap that "
                "cannot trip (R8)"
            )
        elif ceilings == ("max_cost_usd",):
            error = (
                f"cost budget exceeded: ${self.run_usage.cost_usd:.6f} "
                f"recorded >= max_cost_usd "
                f"(${self.factory_config.max_cost_usd}); halting instead of "
                "spending further (R8)"
            )
        else:
            error = (
                f"token budget exceeded: {self.run_usage.total_tokens} total "
                f"tokens recorded >= max_total_tokens "
                f"({self.factory_config.max_total_tokens}); halting instead "
                "of spending further (R3.1)"
            )
        # Coverage rides along with the halt, in the prose AND
        # structurally. Appended rather than folded into each branch so
        # it also qualifies a loop-supplied ``reason``: the loop knows
        # what fired, only the parent knows what the ceiling counted.
        #
        # EVERY CONFIGURED named ceiling is recorded, including ones that
        # covered every call and ones that did not cause this halt. An
        # empty or partial ``coverage`` would otherwise mean several
        # things at once - "no gap", "not the cause", "written before
        # this landed" - and a reader could not tell verified from
        # unknown (the distinction E9 added ``infrastructure_error``
        # for).
        #
        # Iterating CEILING_AXES rather than ``ceilings`` is R8 review
        # finding 3: ``ceilings`` is the CAUSAL identity, so with both
        # caps enabled a token breach yields ``("max_total_tokens",)``
        # and a simultaneously PARTIAL ``max_cost_usd`` was dropped from
        # the halt event and the inbox evidence - the operator deciding
        # which knob to raise never saw that the other cap was counting
        # a subset. ``ceiling_coverage()`` returns None for a ceiling
        # that is not configured, so an absent entry keeps exactly one
        # meaning: that cap was off.
        #
        # The PROSE stays quiet on full coverage, because ``note()``
        # returns "" there: the operator-facing sentence is unchanged
        # wherever there is nothing to disclose.
        coverage = [
            cov for cov in (self.ceiling_coverage(c) for c in CEILING_AXES) if cov is not None
        ]
        notes = [note for note in (cov.note() for cov in coverage) if note]
        if notes:
            error = f"{error} [{'; '.join(notes)}]"
        ceiling = ", ".join(ceilings)
        label = ceiling or "budget"
        self.ui.err(f"  BUDGET EXCEEDED ({label}) for {comp.id}: {error}")
        self._add_findings(
            comp,
            [
                Finding.infrastructure_error(
                    phase=phase,
                    explanation=error,
                )
            ],
        )
        self.bus.emit(
            ev.BudgetExceeded(
                component=comp.id,
                total_tokens=self.run_usage.total_tokens,
                max_total_tokens=self.factory_config.max_total_tokens,
                cost_usd=round(self.run_usage.cost_usd, 6),
                max_cost_usd=self.factory_config.max_cost_usd,
                ceiling=ceiling,
                condition=condition,
                ceilings=ceilings,
                coverage=tuple(cov.to_dict() for cov in coverage),
            )
        )
        # Raised BEFORE delegating to fail(): a blown budget is its own
        # exception kind, and the generic halted_run item fail() adds
        # would bury why the run stopped.
        self._inbox_add(
            ItemKind.BUDGET_OVERRUN,
            f"{comp.id} exceeded the {label} budget",
            detail=error,
            component=comp.id,
            dedupe_key=f"budget:{comp.id}",
            evidence={
                "phase": phase,
                "ceiling": ceiling,
                "condition": condition,
                "total_tokens": self.run_usage.total_tokens,
                "max_total_tokens": self.factory_config.max_total_tokens,
                "cost_usd": round(self.run_usage.cost_usd, 6),
                "max_cost_usd": self.factory_config.max_cost_usd,
                # The operator triaging this item is deciding whether to
                # raise the ceiling; what it counted is part of that
                # decision, not a footnote.
                "coverage": [cov.to_dict() for cov in coverage],
            },
        )
        self._inbox_suppress_generic(comp.id)
        # The check name and failure signature stay "token_budget" for
        # both ceilings: they are the evolution journal's stable
        # vocabulary for "a run-level ceiling stopped this component",
        # and renaming them would orphan every historical record. The
        # ceiling that tripped is carried by the message, the finding,
        # the event and the inbox evidence instead.
        return self.fail(
            comp,
            error,
            phase=phase,
            check="token_budget",
            signatures=["token_budget:exceeded"],
        )

    def complete(
        self,
        comp: Component,
        duration_seconds: float,
        iterations: int,
    ) -> Transition:
        """VERIFYING -> COMPLETED: every gate passed (and the PR merge,
        when configured, was confirmed)."""
        comp.status = ComponentStatus.COMPLETED.value
        comp.error = ""
        self.component_failure_signatures.pop(comp.id, None)
        comp.completed_at = _iso_now()
        self._end_attempt(comp)
        self.factory_result.completed.append(comp.id)
        self.bus.emit(
            ev.ComponentCompleted(
                component=comp.id,
                duration_seconds=duration_seconds,
                iterations=iterations,
            )
        )
        self.ui.ok(f"  COMPLETED: {comp.id} ({iterations} iterations, {duration_seconds:.0f}s)")
        self.manifest.save(self.manifest_path)
        return Transition.COMPLETED

    def _inbox_resolve(self, dedupe_key: str, reason: str) -> None:
        """Close an open item whose question the world has answered."""
        try:
            if self._inbox is None:
                config = InboxConfig.load(self.root_dir)
                if not config.enabled:
                    return
                self._inbox = Inbox(self.root_dir, config)
            existing = self._inbox.find_by_dedupe_key(dedupe_key)
            if existing is not None and existing.is_open:
                self._inbox.resolve(existing.id, comment=reason)
        except (OSError, ValueError, InboxError) as exc:
            self.ui.warn(f"  Inbox resolve failed (non-fatal): {exc}")

    def _inbox_suppress_generic(self, comp_id: str) -> None:
        """Mark that a typed item already covers this component's halt.

        fail() emits a generic halted_run for every terminal failure; a
        budget halt (or a parked merge gate) has already raised a more
        specific item, and two items for one event burn two cap slots and
        bury the reason.
        """
        self._inbox_typed.add(comp_id)

    def _inbox_add(
        self,
        kind: ItemKind,
        title: str,
        *,
        detail: str = "",
        component: str = "",
        dedupe_key: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """Record an exception for a human (R8.3). Never fatal.

        Every terminal halt routes through here, so the inbox reflects
        what actually happened rather than what someone remembered to
        report. Bookkeeping must not be able to fail a run, so a broken
        inbox degrades to a warning - but it warns, because a silently
        empty inbox reads exactly like a clean run.
        """
        try:
            if self._inbox is None:
                config = InboxConfig.load(self.root_dir)
                if not config.enabled:
                    self._inbox_disabled = True
                    return
                self._inbox = Inbox(self.root_dir, config)
            if self._inbox_disabled:
                return
            item = self._inbox.add(
                kind,
                title,
                detail=detail,
                component=component,
                run_id=self.run_id,
                dedupe_key=dedupe_key,
                evidence=evidence or {},
            )
            # One-way push: only items that actually want a human reach
            # the hook, and only when the operator asked for it. Without
            # this the [inbox] notify knob was a documented no-op.
            if self._inbox.config.notify_action_required and notifiable([item]):
                self.notify.fire_inbox_item(
                    str(item.kind),
                    item.title,
                    component_id=component,
                )
        except (OSError, ValueError) as exc:
            self.ui.warn(f"  Inbox write failed (non-fatal): {exc}")

    def _park_merge_pending(
        self,
        comp: Component,
        error: str,
    ) -> Transition:
        """VERIFYING -> MERGE_PENDING: the PR merge was initiated but not
        confirmed (R0.2). Parked, not terminal: no completed_at, but the
        attempt's journal slice is closed (R3.3) - after the
        merge_pending event so the slice includes it."""
        comp.status = ComponentStatus.MERGE_PENDING.value
        comp.error = error
        # Richer v2 event first; the v1-parity twin keeps progress.jsonl
        # unchanged (the reducer prefers the v2 event, chunk 2).
        self.bus.emit(
            ev.PrMergePending(
                component=comp.id,
                pr_url=comp.pr_url,
                error=comp.error,
            )
        )
        self.bus.emit(
            ev.MergePendingV1(
                component=comp.id,
                pr_url=comp.pr_url,
                error=comp.error,
            )
        )
        self.notify.fire_merge_pending(comp.id, comp.error)
        self._inbox_add(
            ItemKind.MERGE_GATE,
            f"{comp.id} merge unconfirmed",
            detail=comp.error,
            component=comp.id,
            dedupe_key=f"merge:{comp.id}",
            evidence={"pr_url": comp.pr_url},
        )
        self._end_attempt(comp)
        self.ui.warn(
            f"  MERGE PENDING: {comp.id}: {comp.error}; "
            f"dependents stay blocked; a factory re-run "
            f"re-polls the PR"
        )
        self.manifest.save(self.manifest_path)
        return Transition.MERGE_PENDING

    def _retry_after_merge_conflict(
        self,
        comp: Component,
        comp_result: ComponentResult,
        pr: PrPhaseResult,
    ) -> Transition:
        """R7.5 merge-conflict doctrine: re-run, don't rebase.

        A conflicting PR means the base moved under this component
        (usually a sibling merged first). Rebasing agent output would
        hand the conflict back to a model with no context on the other
        side of it; re-running the component against the freshly merged
        base lets the engineer implement WITH the sibling's code in
        view. Mechanics: close the conflicting PR (audit comment) and
        delete its remote branch, clear the manifest's PR pointers so
        the retry creates a fresh PR instead of re-polling the closed
        one, then route through the fresh-base retry path (worktree AND
        branch recreated from origin/<base>).
        """
        from kstrl.pr import close_pr_for_rerun, pr_number_from_url

        error = pr.error or "PR conflicts with base"
        self.ui.warn(
            f"  MERGE CONFLICT: {comp.id}: {error[:120]}; re-running "
            f"the component against the freshly merged base"
        )
        pr_number = comp.pr_number or pr_number_from_url(
            comp.pr_url or pr.pr_url,
        )
        if pr_number:
            close_error = close_pr_for_rerun(
                pr_number,
                comp.branch_name,
                self.root_dir,
            )
            if close_error:
                # Non-fatal: the re-run's own push fails loudly if the
                # remote branch is still in the way.
                self.ui.warn(f"  Conflicting-PR cleanup incomplete (non-fatal): {close_error}")
        comp.pr_number = None
        comp.pr_url = ""
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        ctx.add_iteration(
            IterationRecord(
                iteration=comp_result.iterations,
                success=False,
                attempt=comp.retries + 1,
                error=(
                    "The previous attempt's PR hit a merge conflict with the "
                    "base branch; this attempt starts from the freshly merged "
                    "base, which already contains the sibling changes"
                ),
            )
        )
        return self.retry_or_fail(
            comp,
            error,
            ctx.to_json(),
            phase="pr",
            check="merge_conflict",
            signatures=["pr:merge-conflict"],
            fresh_base=True,
        )

    def _fail_pr_flow(self, comp: Component, error: str) -> Transition:
        """VERIFYING -> FAILED on a push/create/merge failure (R0.2:
        COMPLETED requires a CONFIRMED merge)."""
        comp.status = ComponentStatus.FAILED.value
        comp.error = error
        comp.completed_at = _iso_now()
        comp.failed_phase = "pr"
        comp.failed_check = "pr_flow"
        # Spelled out with keywords, and read that way. #339: this
        # is the third caller of the funnel and the only one that is
        # not `fail` / `retry_or_fail`, so no `signatures=` is ever
        # handed to it and the phase becomes the check name. It was
        # invisible to every layer of
        # `tests/test_check_name_enrolment.py`: not a fail call, no
        # keyword, no colon in "pr" for the spellings net to see.
        # Mutating "pr" to "bogus_flow" left 4737 tests green.
        self._record_failure_signatures(
            comp,
            phase="pr",
            error=comp.error,
            signatures=None,
        )
        self._end_attempt(comp)
        skipped = self.manifest.cascade_skip(comp.id)
        self.factory_result.failed.append(comp.id)
        self.factory_result.skipped.extend(skipped)
        self.bus.emit(ev.ComponentFailed(component=comp.id, error=comp.error))
        self.notify.fire_first_failure(comp.id, comp.error)
        self._inbox_add(
            ItemKind.HALTED_RUN,
            f"{comp.id} halted in the PR flow",
            detail=comp.error,
            component=comp.id,
            dedupe_key=f"halted:{comp.id}:pr",
            evidence={"phase": "pr", "pr_url": comp.pr_url},
        )
        self.ui.err(f"  Failed: {comp.id}: {comp.error[:120]}")
        self.manifest.save(self.manifest_path)
        return Transition.FAILED

    def fail_scheduler_backstop(
        self,
        comp_id: str,
        backstop_seconds: float,
    ) -> None:
        """RUNNING -> FAILED when the scheduler backstop deadline passes
        (R0.1): the worker hung outside the adapter and loop timeout
        layers. The worker may still be alive, so its worktree is kept
        and pointed at as evidence (R3.3)."""
        timed_out_comp = self.manifest.get_component(comp_id)
        if timed_out_comp is not None:
            timed_out_comp.status = ComponentStatus.FAILED.value
            timed_out_comp.error = "component timeout"
            timed_out_comp.completed_at = _iso_now()
            timed_out_comp.failed_phase = "engineer"
            timed_out_comp.failed_check = "scheduler_backstop"
            self.component_failure_signatures[comp_id] = [
                "engineer:component-timeout",
            ]
            self._end_attempt(timed_out_comp)
            # The worktree stays (leaked worker may own it);
            # point the evidence at it (R3.3).
            if comp_id in self.worktree_paths:
                timed_out_comp.evidence_worktree = str(self.worktree_paths[comp_id])
            skipped = self.manifest.cascade_skip(comp_id)
            self.factory_result.failed.append(comp_id)
            self.factory_result.skipped.extend(skipped)
            started = self._attempt_started_monotonic.get(comp_id)
            duration = time.monotonic() - started if started is not None else 0.0
            self.bus.emit(
                ev.PhaseCompleted(
                    component=comp_id,
                    phase="engineer",
                    passed=False,
                    detail="component timeout",
                    duration_seconds=round(duration, 2),
                )
            )
            self.bus.emit(
                ev.ComponentFailed(
                    component=comp_id,
                    error="component timeout",
                )
            )
            self.notify.fire_first_failure(comp_id, "component timeout")
        self.ui.err(
            f"  Failed: {comp_id}: component timeout "
            f"(scheduler backstop after {backstop_seconds:.0f}s)"
        )
        self.ui.warn(
            f"  A worker process for '{comp_id}' may be leaked; its worktree is left in place"
        )
        self._inbox_add(
            ItemKind.HALTED_RUN,
            f"{comp_id} abandoned by the scheduler backstop",
            detail=(
                f"no result after {backstop_seconds}s; the worker may still "
                "be alive, so its worktree was left in place"
            ),
            component=comp_id,
            dedupe_key=f"halted:{comp_id}:backstop",
            evidence={"backstop_seconds": backstop_seconds},
        )
        self.manifest.save(self.manifest_path)

    def repoll_merge_pending(self) -> None:
        """R0.2 crash recovery: MERGE_PENDING is re-pollable, not failed.

        A prior run initiated the merge but could not confirm it; check
        the PR state again before scheduling so confirmed merges unblock
        their dependents (MERGE_PENDING -> COMPLETED, or -> FAILED when
        the PR was closed without merging)."""
        merge_pending_comps = [
            c for c in self.manifest.components if c.status == ComponentStatus.MERGE_PENDING.value
        ]
        if not merge_pending_comps:
            return
        from kstrl.pr import (
            is_gh_available,
            pr_number_from_url,
            wait_for_merge,
        )

        if not self.factory_config.create_prs or not is_gh_available():
            self.ui.warn(
                f"  {len(merge_pending_comps)} component(s) are "
                f"merge-pending but PR polling is unavailable (create_prs "
                f"off or gh missing); their dependents stay blocked"
            )
        else:
            for comp in merge_pending_comps:
                pr_number = comp.pr_number or pr_number_from_url(comp.pr_url)
                if not pr_number:
                    self.ui.warn(f"  Cannot re-poll '{comp.id}': no PR number recorded")
                    continue
                self.ui.info(f"  Re-polling merge state for '{comp.id}' (PR #{pr_number})...")
                merge_state = wait_for_merge(
                    pr_number,
                    self.root_dir,
                    timeout=self.factory_config.merge_timeout,
                )
                if merge_state == "merged":
                    git.fetch_base_branch(
                        self.manifest.base_branch,
                        self.root_dir,
                    )
                    comp.status = ComponentStatus.COMPLETED.value
                    comp.error = ""
                    self.component_failure_signatures.pop(comp.id, None)
                    comp.completed_at = _iso_now()
                    self.factory_result.completed.append(comp.id)
                    self.bus.emit(
                        ev.ComponentCompleted(
                            component=comp.id,
                            duration_seconds=comp.duration_seconds,
                            iterations=comp.iteration_count,
                        )
                    )
                    self.ui.ok(f"  PR #{pr_number} merged; '{comp.id}' completed")
                    # The gate that parked this component is answered by
                    # reality; leaving it open would hold a cap slot
                    # forever and ask a human to decide something already
                    # decided.
                    self._inbox_resolve(
                        f"merge:{comp.id}",
                        f"PR #{pr_number} merged",
                    )
                elif merge_state == "closed":
                    comp.status = ComponentStatus.FAILED.value
                    comp.error = f"PR #{pr_number} closed without merge"
                    comp.completed_at = _iso_now()
                    # Not a merge decision any more - it is a halt.
                    self._inbox_resolve(
                        f"merge:{comp.id}",
                        f"PR #{pr_number} closed without merging",
                    )
                    self._inbox_add(
                        ItemKind.HALTED_RUN,
                        f"{comp.id}: PR closed without merging",
                        detail=comp.error,
                        component=comp.id,
                        dedupe_key=f"halted:{comp.id}:pr-closed",
                        evidence={"pr_number": pr_number},
                    )
                    comp.failed_phase = "pr"
                    comp.failed_check = "pr_closed"
                    self.component_failure_signatures[comp.id] = [
                        "pr:closed-without-merge",
                    ]
                    skipped = self.manifest.cascade_skip(comp.id)
                    self.factory_result.failed.append(comp.id)
                    self.factory_result.skipped.extend(skipped)
                    self.bus.emit(
                        ev.ComponentFailed(
                            component=comp.id,
                            error=comp.error,
                        )
                    )
                    self.notify.fire_first_failure(comp.id, comp.error)
                    self.ui.err(f"  Failed: {comp.id}: {comp.error}")
                else:
                    self.ui.warn(
                        f"  '{comp.id}' still awaiting merge of "
                        f"PR #{pr_number}; dependents stay blocked"
                    )
        self.manifest.save(self.manifest_path)

    # ------------------------------------------------------------------
    # Phase chain
    # ------------------------------------------------------------------

    def _record_phase_skip(
        self,
        comp: Component,
        phase: str,
        reason: str,
    ) -> None:
        """R1.2: a phase that never ran must leave a trace in both
        the findings stream and the journal, so "ran clean" and
        "never ran" are distinguishable downstream."""
        self._add_findings(comp, [Finding.phase_skipped(phase, reason)])
        self.bus.emit(
            ev.PhaseSkipped(
                component=comp.id,
                phase=phase,
                reason=reason,
            )
        )

    def process_result(
        self,
        comp_id: str,
        comp_result: ComponentResult,
    ) -> PipelineOutcome | None:
        """Process one component result through the phase chain.

        Returns the typed outcome (None when the component id is unknown).
        Every side effect - manifest saves, progress events, notify hooks,
        retries - happens here or in the transition methods this routes
        into; the scheduler only launches workers and hands results in.
        """
        comp = self.manifest.get_component(comp_id)
        if comp is None:
            return None

        # Record timing
        comp.duration_seconds = comp_result.duration_seconds
        comp.iteration_count = comp_result.iterations

        # Engineer bracket closer: PhaseStarted(engineer) was emitted by
        # the scheduler at submit time; the worker's exit lands here.
        self.bus.emit(
            ev.PhaseCompleted(
                component=comp_id,
                phase="engineer",
                passed=comp_result.success,
                detail=comp_result.error or "",
                duration_seconds=round(comp_result.duration_seconds, 2),
            )
        )

        # R3.1: engineer-loop spend counts BEFORE the success branch -
        # failed attempts cost real tokens too.
        if comp_result.usage is not None:
            self._record_usage(comp_id, "engineer", comp_result.usage)

        # R3.1 budget checkpoint: the engineer loop just reported the
        # dominant spend; halt before starting adversarial phases (or a
        # retry) when the run-level cap is blown.
        #
        # R8: the loop can also halt ITSELF between iterations, which is
        # the only enforcement that happens while the spend is being
        # incurred. Both routes land here, so the audit trail (typed
        # finding, BudgetExceeded event, single budget_overrun inbox
        # item, no duplicate halted_run) is identical either way. The
        # worker's reason is used only when the parent's own totals do
        # not show a breach - the unreportable-usage case, where the
        # derived "N >= cap" sentence would be false.
        if comp_result.budget_exceeded or self.budget_exceeded():
            reason = "" if self.budget_exceeded() else (comp_result.error or "")
            return PipelineOutcome(
                transition=self.fail_for_budget(
                    comp,
                    "engineer",
                    reason,
                    # The loop's own verdict when IT halted; empty when
                    # the parent's totals are what tripped, in which
                    # case fail_for_budget derives the identity under
                    # the single precedence rule.
                    condition=comp_result.budget_halt_condition,
                    ceilings=comp_result.budget_halt_ceilings,
                ),
            )

        if not comp_result.success:
            # R7.5: the no-progress circuit breaker is a direct FAILED
            # transition, never a retry - a fresh attempt would re-run
            # the same prompt against the same base state, which is the
            # exact spend the breaker exists to stop. Loud and distinct:
            # its own progress-log event plus a structured failure
            # signature for the evolution journal.
            if comp_result.no_progress:
                error = comp_result.error or "no-progress circuit breaker tripped"
                self.bus.emit(
                    ev.CircuitBreakerTripped(
                        component=comp_id,
                        iterations=comp_result.iterations,
                        error=error,
                    )
                )
                return PipelineOutcome(
                    transition=self.fail(
                        comp,
                        error,
                        phase="engineer",
                        check="no_progress_breaker",
                        signatures=["engineer:no-progress-stall"],
                    ),
                )
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_iteration(
                IterationRecord(
                    iteration=comp_result.iterations,
                    success=False,
                    attempt=comp.retries + 1,
                    error=comp_result.error,
                )
            )
            if comp_result.guard_violations:
                # The in-loop guard runs the SAME check as Phase 1's
                # diff_scope, only earlier, so it must produce the same
                # audit record - otherwise moving the catch forward
                # silently changes the shape of the journal, and a
                # scope failure caught in-loop would leave no
                # verification_result behind at all.
                #
                # Exactly one check is reported. Listing the others
                # would claim tests and typecheck ran when the loop
                # halted before they could.
                detail = comp_result.error or "files outside allowed scope"
                self.bus.emit(
                    ev.VerificationResultEvent(
                        component=comp.id,
                        passed=False,
                        checks=("diff_scope",),
                        failures=(detail,),
                        duration_seconds=0.0,
                    )
                )
                # Same "<check>: FAIL" token as
                # VerificationResult.as_context(), and the same retry
                # reason Phase 1 uses: consumers keying on either keep
                # working no matter which layer caught the violation. A
                # second wording for one failure is how a mislabel
                # becomes a divergence (R7.1 / #179).
                # R10.2: the engineer rank, not verification. The
                # failure text deliberately matches Phase 1's diff_scope
                # token (above), but the RANK answers a different
                # question: did Phase 1 produce a reading this attempt?
                # It did not, the guard fired inside the engineer loop
                # first. Ranking this as verification retired an earlier
                # attempt's real Phase 1 failure as re-measured.
                ctx.add_engineer_failure(
                    f"diff_scope: FAIL - {detail}",
                    attempt=comp.retries + 1,
                )
                return PipelineOutcome(
                    transition=self.retry_or_fail(
                        comp,
                        "Mechanical verification failed",
                        ctx.to_json(),
                        phase="verify",
                        check="diff_scope",
                    ),
                )
            # R10.2: without an entry this attempt would carry no
            # dated evidence, and the renderer would present an older
            # attempt's finding as the current one. The guard branch
            # above already records its own entry, so only the plain
            # loop failure needs this.
            error = comp_result.error or "Unknown error"
            ctx.add_engineer_failure(error, attempt=comp.retries + 1)
            return PipelineOutcome(
                transition=self.retry_or_fail(
                    comp,
                    error,
                    ctx.to_json(),
                    phase="engineer",
                    check="loop",
                ),
            )

        wt_path = self.worktree_paths.get(comp_id, self.root_dir)

        # PHASE 1: Mechanical verification
        comp.status = ComponentStatus.VERIFYING.value
        self.manifest.save(self.manifest_path)

        t0 = self._phase_started(comp, "verify")
        verify = self._phase_verify(comp, comp_result, wt_path)
        self._phase_completed(
            comp,
            "verify",
            t0,
            verify.failure is None,
            verify.failure.error if verify.failure else "",
        )
        if verify.failure is not None:
            # No diff_text: the diff phase has not run, but the change
            # IS committed in the worktree - it is what verification
            # just inspected - so the diff is fetched here rather than
            # writing off every test/lint/typecheck failure as
            # unmeasurable.
            self.record_fact_utilization(comp, wt_path)
            return PipelineOutcome(
                transition=self._route_failure(comp, verify.failure),
                verify=verify,
            )

        t0 = self._phase_started(comp, "diff")
        diff = self._phase_diff(comp, comp_result, wt_path)
        self._phase_completed(
            comp,
            "diff",
            t0,
            diff.failure is None,
            diff.failure.error if diff.failure else "",
        )
        if diff.failure is not None:
            self.record_fact_utilization(
                comp,
                wt_path,
                "",
                unavailable="diff unavailable",
            )
            return PipelineOutcome(
                transition=self._route_failure(comp, diff.failure),
                verify=verify,
                diff=diff,
            )

        # #191: measure fact utilization the moment a diff exists, so
        # the sample is every component that produced one - not only
        # those that survive the review and security gates below.
        self.record_fact_utilization(comp, wt_path, diff.diff)

        # PHASE 2: Second-opinion review
        t0 = self._phase_started(comp, "review")
        review = self._phase_review(
            comp,
            comp_result,
            wt_path,
            verify.verification,
        )
        self._phase_completed(
            comp,
            "review",
            t0,
            review.failure is None,
            review.failure.error if review.failure else "",
        )
        if review.failure is not None:
            return PipelineOutcome(
                transition=self._route_failure(comp, review.failure),
                verify=verify,
                diff=diff,
                review=review,
            )

        # PHASE 2.5: Security review
        t0 = self._phase_started(comp, "security")
        security = self._phase_security(
            comp,
            comp_result,
            wt_path,
        )
        self._phase_completed(
            comp,
            "security",
            t0,
            security.failure is None,
            security.failure.error if security.failure else "",
        )
        if security.failure is not None:
            return PipelineOutcome(
                transition=self._route_failure(comp, security.failure),
                verify=verify,
                diff=diff,
                review=review,
                security=security,
            )

        # Knowledge distillation: a NAMED PRE-PR step (R7.3 decision).
        t0 = self._phase_started(comp, "distill")
        distill = self._phase_distill(comp, comp_result, wt_path, diff.diff)
        self._phase_completed(
            comp,
            "distill",
            t0,
            True,
            distill.skip_reason or "",
        )

        # HITL checkpoint + PR create/merge (per-component PR mode only).
        checkpoint = CheckpointDecision.NOT_PROMPTED
        pr = PrPhaseResult(disposition=PrDisposition.SKIPPED)
        if self.factory_config.create_prs and not self.factory_config.single_pr:
            checkpoint = self._phase_checkpoint(
                comp,
                diff_text=diff.diff,
            )
            if checkpoint == CheckpointDecision.REJECTED:
                return PipelineOutcome(
                    transition=self.fail(
                        comp,
                        "Rejected at HITL checkpoint",
                        phase="pr",
                        check="hitl_reject",
                        # THE RULE FOR ALL THREE CHECKPOINT BRANCHES,
                        # stated here and pointed at from the other two.
                        # Without an explicit `signatures=` the PHASE
                        # becomes the check name, and `pr` is the row
                        # that holds push, create and merge plumbing -
                        # infrastructure since #315, which throws the
                        # whole run out of the autonomy ladder's
                        # evidence. So: a branch that carries a VERDICT
                        # about the change must name it, and a branch
                        # that carries no verdict must not. A person
                        # looking at the change and refusing it is the
                        # most decisive verdict about the factory's
                        # judgement there is; it reached the journal as
                        # `pr:rejected-at-hitl-checkpoint`.
                        #
                        # The phase stays "pr" because that is the
                        # vocabulary these branches have always used and
                        # `failed_phase` is written to the manifest.
                        # Note it is not where this happens:
                        # `_phase_started(comp, "pr")` fires below all
                        # three branches, so by the module's own
                        # accounting the checkpoint precedes the phase
                        # it is filed under. Renaming it is a separate
                        # decision, and #339 review declined to make it
                        # here because the three branches need three
                        # different categories, so no one phase name can
                        # serve them. The mechanism that would remove
                        # the literals entirely is keying the fallback
                        # on `check` rather than `phase` - the branches
                        # already carry hitl_reject / merge_gate /
                        # hitl_retry - and that is blocked on
                        # factory.py.
                        signatures=["review:hitl-rejected"],
                    ),
                    verify=verify,
                    diff=diff,
                    review=review,
                    security=security,
                    distill=distill,
                    checkpoint=checkpoint,
                )
            if checkpoint == CheckpointDecision.PARKED:
                # R8.3: the gate could not be answered, so the merge does
                # NOT happen. Terminal for this run and routed to the
                # inbox (the item was raised in _phase_checkpoint); the
                # typed marker stops fail() adding a generic halted_run
                # on top of it.
                self._inbox_suppress_generic(comp.id)
                return PipelineOutcome(
                    transition=self.fail(
                        comp,
                        "Parked awaiting merge approval "
                        "(pause_before_pr_merge, no interactive UI); "
                        "approve with `ks inbox retry <id>`",
                        phase="pr",
                        check="merge_gate",
                        # #339: deliberately NO signatures=. Nobody
                        # answered the gate, so there is no verdict to
                        # record and the `pr` fallback is correct: the
                        # ladder treats the run as an outage, which is
                        # what it was. See the rule at hitl_reject above.
                    ),
                    verify=verify,
                    diff=diff,
                    review=review,
                    security=security,
                    distill=distill,
                    checkpoint=checkpoint,
                )
            if checkpoint == CheckpointDecision.RETRY:
                ctx = IterationContext.from_json(
                    comp_result.context_json or "{}",
                )
                ctx.add_checkpoint_request(
                    "Human reviewer requested changes at PR checkpoint",
                    attempt=comp.retries + 1,
                )
                return PipelineOutcome(
                    transition=self.retry_or_fail(
                        comp,
                        "Retry requested at HITL checkpoint",
                        ctx.to_json(),
                        phase="pr",
                        check="hitl_retry",
                        # #339: a human asking for changes is a verdict
                        # ON the change, so it names one. Same rule as
                        # hitl_reject above, and it had the same defect
                        # until this round.
                        signatures=["review:hitl-changes-requested"],
                    ),
                    verify=verify,
                    diff=diff,
                    review=review,
                    security=security,
                    distill=distill,
                    checkpoint=checkpoint,
                )

            t0 = self._phase_started(comp, "pr")
            pr = self._phase_pr(comp)
            self._phase_completed(
                comp,
                "pr",
                t0,
                pr.disposition
                in (
                    PrDisposition.MERGED,
                    PrDisposition.NO_GH,
                    PrDisposition.SKIPPED,
                ),
                pr.error,
            )
            if pr.disposition == PrDisposition.CONFLICT:
                return PipelineOutcome(
                    transition=self._retry_after_merge_conflict(
                        comp,
                        comp_result,
                        pr,
                    ),
                    verify=verify,
                    diff=diff,
                    review=review,
                    security=security,
                    distill=distill,
                    checkpoint=checkpoint,
                    pr=pr,
                )
            if pr.disposition == PrDisposition.MERGE_PENDING:
                return PipelineOutcome(
                    transition=self._park_merge_pending(comp, pr.error),
                    verify=verify,
                    diff=diff,
                    review=review,
                    security=security,
                    distill=distill,
                    checkpoint=checkpoint,
                    pr=pr,
                )
            if pr.disposition == PrDisposition.FAILED:
                return PipelineOutcome(
                    transition=self._fail_pr_flow(comp, pr.error),
                    verify=verify,
                    diff=diff,
                    review=review,
                    security=security,
                    distill=distill,
                    checkpoint=checkpoint,
                    pr=pr,
                )

        # Clean up worktree now that code is merged
        if self.factory_config.use_worktrees and comp_id in self.worktree_paths:
            self.hooks.cleanup_worktree(comp_id, self.root_dir, self.run_id)
            del self.worktree_paths[comp_id]

        return PipelineOutcome(
            transition=self.complete(
                comp,
                comp_result.duration_seconds,
                comp_result.iterations,
            ),
            verify=verify,
            diff=diff,
            review=review,
            security=security,
            distill=distill,
            checkpoint=checkpoint,
            pr=pr,
        )

    def _route_failure(
        self,
        comp: Component,
        failure: PhaseFailure,
    ) -> Transition:
        """The single dispatch from a phase's typed failure into a
        component state transition."""
        if failure.action == FailureAction.TOKEN_BUDGET:
            return self.fail_for_budget(comp, failure.phase)
        if failure.action == FailureAction.FAIL:
            return self.fail(
                comp,
                failure.error,
                phase=failure.phase,
                check=failure.check,
                signatures=failure.signatures,
            )
        return self.retry_or_fail(
            comp,
            failure.error,
            failure.context_json,
            phase=failure.phase,
            check=failure.check,
            signatures=failure.signatures,
        )

    def _warn_not_measured(self, comp: Component, verification: VerificationResult) -> None:
        """Say out loud what Phase 1 was asked to measure and could not.

        #306. Not a gate failure: a gap does not reach
        ``verification.passed``, and it is deliberately kept out of
        ``as_context`` too, because no engineer iteration can install a
        missing binary and a retry would burn ``repair_max_runs``
        proving it. Not nothing either: before #306 the row said PASS,
        and omitting the row alone said nothing at all, so a
        permanently broken mutation gate looked exactly like a working
        one. This is the third option, and it is the only place the
        pipeline says it.
        """
        for gap in verification.not_measured:
            self.ui.warn(f"  {comp.id}: {gap.check} not measured ({gap.reason}) - {gap.detail}")

    def _phase_verify(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
    ) -> VerifyPhaseResult:
        """Phase 1: mechanical verification (tests / typecheck / lint /
        PRD stories / diff scope / bad patterns / fixtures)."""
        if self.factory_config.skip_verification:
            # R2.3: --no-verify. Previously verify_config=None fell
            # through to VerifyConfig() defaults here and Phase 1 ran
            # anyway - on a non-Python repo that burned every retry
            # against checks that could never pass. The empty
            # VerificationResult below is what downstream reviewers see:
            # no checks ran, none are claimed.
            self.ui.info(
                f"  Phase 1 SKIPPED for {comp.id}: mechanical verification disabled (--no-verify)"
            )
            comp.verification_passed = None
            self._record_phase_skip(
                comp,
                "verify",
                "mechanical verification disabled (--no-verify)",
            )
            return VerifyPhaseResult(
                ran=False,
                verification=VerificationResult(passed=True, checks=[]),
            )

        verify_config = self.factory_config.resolved_verify_config()
        self.ui.info(f"  Phase 1: mechanical verification for {comp.id}...")
        verify_start = time.monotonic()
        # #269: the run's plan-time snapshot, and no file read. Phase 1
        # used to load the component PRD from the WORKTREE and take
        # `allowedPaths` off it, while the in-loop guard deliberately
        # read the copy at root_dir - so the two guards could enforce
        # different allowlists on the same component in the same run,
        # and Phase 1's was the one the agent could edit. Both now read
        # RunScope, resolved once before the first engineer call.
        scope = self.run_scope.for_component(comp.id)
        # R7.2: fixtures config resolves from toml/env when the
        # caller did not inject one; enabled=false (the default)
        # makes run_mechanical_verification skip the check entirely.
        fixtures_cfg = self.factory_config.fixtures_config or FixturesConfig.load(self.root_dir)
        # R8.1 policy envelope: opt-in ([policy].enabled). enabled=false
        # (the default) makes run_mechanical_verification skip the check.
        policy_cfg = self.factory_config.policy_config or PolicyConfig.load(self.root_dir)
        adequacy_cfg = AdequacyConfig.load(self.root_dir)
        autonomy_cfg = AutonomyConfig.load(self.root_dir)
        level = AutonomyState.load(self.root_dir).level if autonomy_cfg.enabled else 0
        verification = self.hooks.run_mechanical_verification(
            wt_path,
            wt_path / comp.prd_path,
            self.manifest.base_branch,
            scope.allowed_paths,
            verify_config,
            allowed_paths_error=scope.error,
            harness_paths=scope.harness_paths,
            # The copy the run started with, for the defence in depth
            # the snapshot does NOT provide: the stories, criteria and
            # fixtures Phase 1 still has to read from the live file
            # (#269). Outside every worktree, so not agent-writable.
            pre_run_prd_path=self.root_dir / comp.prd_path,
            fixtures_config=fixtures_cfg,
            policy_config=policy_cfg,
            adequacy_config=adequacy_cfg,
            autonomy_level=level,
            component_id=comp.id,
        )
        verify_duration = time.monotonic() - verify_start
        comp.verification_passed = verification.passed
        # R8.1: mechanical checks that produce typed findings (today the
        # policy envelope) get them into the component's finding stream, so
        # a machine-made gate decision reaches the audit trail - PR body,
        # journal, evolution - and not just the retry context. Recorded for
        # passing checks too: a non-blocking advisory is still evidence.
        check_findings = [finding for check in verification.checks for finding in check.findings]
        if check_findings:
            self._add_findings(comp, check_findings)
            # R8.3: an envelope breach is the archetypal exception - a
            # machine decision a human may want to approve once, or
            # convert into a widened policy. Advisories stay out: they
            # are recorded, not blocking, and the inbox is for decisions.
            for finding in check_findings:
                if (
                    finding.category.startswith(POLICY_CATEGORY_PREFIX)
                    and finding.severity != "advisory"
                ):
                    self._inbox_add(
                        ItemKind.POLICY_EXCEPTION,
                        f"{comp.id}: {finding.category}",
                        detail=finding.explanation,
                        component=comp.id,
                        dedupe_key=f"policy:{comp.id}:{finding.category}",
                        evidence={
                            "category": finding.category,
                            "severity": finding.severity,
                            "location": finding.location,
                            "suggestion": finding.suggestion,
                        },
                    )
                # R8.5: same rule, same reason. A BLOCKING adequacy
                # finding stopped the change and needs a human to decide
                # whether the suite may weaken here; an ADVISORY one is
                # recorded in the finding stream and stops there, because
                # the inbox is a queue of decisions, not of notes. The
                # dedupe key is category + location so the same file
                # failing the same way across retries collapses onto one
                # item instead of fanning out.
                elif (
                    finding.category.startswith(ADEQUACY_CATEGORY_PREFIX)
                    and finding.severity != "advisory"
                ):
                    self._inbox_add(
                        ItemKind.TEST_ADEQUACY,
                        f"{comp.id}: {finding.category}",
                        detail=finding.explanation,
                        component=comp.id,
                        dedupe_key=(f"adequacy:{comp.id}:{finding.category}:{finding.location}"),
                        evidence={
                            "category": finding.category,
                            "severity": finding.severity,
                            "location": finding.location,
                            "suggestion": finding.suggestion,
                        },
                    )
        self.bus.emit(
            ev.VerificationResultEvent(
                component=comp.id,
                passed=verification.passed,
                checks=tuple(c.name for c in verification.checks),
                failures=tuple(c.message for c in verification.checks if not c.passed),
                duration_seconds=round(verify_duration, 2),
                not_measured=tuple(g.as_token() for g in verification.not_measured),
            )
        )

        self._warn_not_measured(comp, verification)

        if not verification.passed:
            failing = [c for c in verification.checks if not c.passed]
            self.ui.warn(f"  Phase 1 FAILED for {comp.id}: {', '.join(c.name for c in failing)}")
            ctx = IterationContext.from_json(
                comp_result.context_json or "{}",
            )
            ctx.add_verification_failure(
                verification.as_context(),
                attempt=comp.retries + 1,
            )
            # R6.1: carry the parser's structured codes (ruff rule,
            # mypy error code, pytest exception type) into the
            # journal instead of the flattened string.
            from kstrl.evolution import signatures_from_verification

            action, error = _verify_routing(failing)
            return VerifyPhaseResult(
                ran=True,
                verification=verification,
                failure=PhaseFailure(
                    action=action,
                    error=error,
                    phase="verify",
                    check=", ".join(c.name for c in failing),
                    context_json=ctx.to_json(),
                    signatures=signatures_from_verification(
                        verification.checks,
                    ),
                ),
            )

        self.ui.ok(f"  Phase 1 passed for {comp.id}")
        return VerifyPhaseResult(ran=True, verification=verification)

    def _phase_diff(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
    ) -> DiffPhaseResult:
        """Fetch the component diff once and share it with every phase
        that consumes one as text: fact-utilization measurement, the
        knowledge distiller, the HITL checkpoint and the PR body.
        Without this each would shell out to `git diff` independently,
        redundantly rebuilding the same patch on every component.

        #266: the review and security phases are no longer consumers.
        They read the worktree they already run in, so this diff is not
        on their path at all, and neither is the size cap that used to
        govern it.

        R1.3 (H-14): a git failure here used to yield "" and every
        consumer silently worked from an empty diff and passed. Now it
        is an infrastructure failure for the component: record the
        infra finding, journal it, and retry/fail closed.
        """
        try:
            shared_diff = git.get_diff_content(
                self.manifest.base_branch,
                wt_path,
            )
        except git.GitDiffError as exc:
            self.ui.err(f"  Diff fetch FAILED for {comp.id}: {exc}")
            self._add_findings(
                comp,
                [
                    Finding.infrastructure_error(
                        phase="diff",
                        explanation=(
                            f"git diff against {self.manifest.base_branch} failed; "
                            f"knowledge distillation and the PR body cannot "
                            f"be built: {exc}"
                        ),
                    )
                ],
            )
            self.bus.emit(ev.DiffFetchFailed(component=comp.id, error=str(exc)))
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_verification_failure(
                f"git diff against {self.manifest.base_branch} failed: {exc}",
                attempt=comp.retries + 1,
                phase="diff",
                infrastructure=True,
            )
            return DiffPhaseResult(
                failure=PhaseFailure(
                    action=FailureAction.RETRY_OR_FAIL,
                    error=f"Diff fetch failed (infrastructure): {exc}",
                    phase="diff",
                    check="git_diff",
                    context_json=ctx.to_json(),
                    signatures=["diff:fetch-failed"],
                )
            )

        return DiffPhaseResult(diff=shared_diff)

    def _divergence_failure(
        self,
        comp: Component,
        wt_path: Path,
        review_result: ReviewResult,
    ) -> str | None:
        """#265: record this attempt's reading and ask whether the retry
        loop is diverging. Returns the message that should FAIL the
        component, or ``None`` when nothing should.

        A trip is reported here whatever the mode - the line, the
        finding and the event all go out before this returns - so the
        return value carries the routing decision only, and ``None``
        covering both "not diverging" and "recorded, keep retrying" is
        not a lost fact: an advisory trip is already durable in the
        event stream by then.

        Every "cannot be told" path declines to record a reading rather
        than recording a guessed one. The predicate needs CONSECUTIVE
        attempts, so a missing reading breaks the streak by itself, which
        is the fail-open direction: the loop keeps its retries.
        """
        config = DivergenceConfig.load(self.root_dir)
        if not config.measures:
            return None
        if review_result.infrastructure_error:
            # A crashed reviewer produced no verdict, so there is nothing
            # to compare. Same rule as FailureEntry.infrastructure.
            return None
        keys = review_finding_keys(review_result)
        if not keys:
            # The review failed on something this predicate cannot key
            # (an empty-location concern family, a hand-built result).
            return None
        try:
            numstat = git.get_diff_numstat(
                self.manifest.base_branch,
                wt_path,
                strict=True,
            )
        except git.GitDiffError as exc:
            self.ui.warn(f"  Divergence detector could not measure {comp.id}: {exc}")
            return None
        # R8.1's size caps and this detector must agree about how large a
        # change is, so both count through the same helper - which also
        # brings its exclusion of machine-generated lockfiles, without
        # which a dependency bump could supply the size half of a trip.
        # The result is lines ADDED PLUS REMOVED, so it is churn rather
        # than file growth; see the module docstring for why that is what
        # the predicate wants.
        files_changed, lines_changed = count_diff_size(numstat)
        readings = self.review_readings.setdefault(comp.id, [])
        readings.append(
            AttemptReading(
                attempt=comp.retries + 1,
                lines_changed=lines_changed,
                files_changed=files_changed,
                finding_keys=keys,
                # The reviewer's own count, NOT len(keys): keys
                # deduplicate and the operator's line above this one
                # ("Phase 2 FAILED: N failures") does not.
                blocking_count=review_result.fail_count,
            )
        )
        verdict = detect_divergence(readings, config)
        if verdict is None:
            return None
        message = verdict.message
        self._add_findings(
            comp,
            [Finding.divergence(message, severity="fail" if config.blocks else "advisory")],
        )
        self.bus.emit(
            ev.ReviewDivergence(
                component=comp.id,
                attempts=tuple(r.attempt for r in verdict.readings),
                lines_changed=tuple(r.lines_changed for r in verdict.readings),
                files_changed=tuple(r.files_changed for r in verdict.readings),
                blocking_findings=tuple(r.blocking_count for r in verdict.readings),
                blocked=config.blocks,
            )
        )
        # One event, one print site. Severity of the LINE follows the
        # severity of the decision.
        if config.blocks:
            self.ui.err(f"  {message}")
            return message
        self.ui.warn(f"  {message}")
        return None

    #: The operator-facing account of a refused review, shared by both
    #: phases so the remedy is worded once. ``noun`` is what cannot be
    #: trusted (a verdict, or findings) and ``agent`` names the config
    #: knob to look at.
    _COVERAGE_UNVERIFIED = (
        "{role} coverage unverified: {disagreement}. The reviewer could not "
        "show it read the whole change, so its {noun} cannot be trusted and "
        "the engineer cannot fix it. Check that the {agent} can run git in "
        "the worktree, or select a model that honours the observedDiffstat "
        "field (#266)."
    )

    def _coverage_failure(
        self,
        comp: Component,
        *,
        phase: str,
        banner: str,
        role: str,
        noun: str,
        agent: str,
        disagreement: str,
        reported_a_stat: bool,
    ) -> PhaseFailure:
        """#266: the wall a review that cannot prove its coverage hits.

        FAIL, never RETRY_OR_FAIL. The refusal is a HARNESS-side fault -
        the reviewer could not show it read the change - and the
        engineer cannot fix it by writing code. Routed through the
        ordinary failure path it re-ran the engineer and handed it the
        coverage complaint as retry context.

        That is worse than useless, because unlike a crash or a
        truncated JSON reply this trigger is DETERMINISTIC: a reviewer
        model that does not emit ``observedDiffstat``, or counts it
        differently, produces the identical failure on every attempt.
        The component would burn its whole retry budget on engineer runs
        that cannot move the outcome and then fail anyway - precisely
        the #265 economics this issue exists to remove, reintroduced by
        its own fix. Same rule as the budget walls: retrying cannot
        change the input, so do not pay for the attempt.
        """
        error = self._COVERAGE_UNVERIFIED.format(
            role=role,
            disagreement=disagreement,
            noun=noun,
            agent=agent,
        )
        self.ui.err(f"  {banner} FAILED for {comp.id}: {error}")
        # The two refusals have different remedies and the journal
        # should not have to read prose to tell them apart. "no-diffstat"
        # is a model that never emits the field - swap the reviewer.
        # "range-mismatch" is a model that emitted the wrong numbers -
        # look at whether it can reach the repository. Both are
        # harness-side and neither is fixable by the engineer, which is
        # why they route the same way.
        reason = "range-mismatch" if reported_a_stat else "no-diffstat"
        return PhaseFailure(
            action=FailureAction.FAIL,
            error=error,
            phase=phase,
            check="coverage",
            signatures=[f"{phase}:coverage-unverified:{reason}"],
        )

    def _budget_refusal(
        self,
        comp: Component,
        *,
        phase: str,
        banner: str,
        role: str,
    ) -> PhaseFailure:
        """R10.5 (#226): how a hard-mode adversarial phase refuses when
        ``max_adversarial_calls`` is spent before it runs.

        FAIL, never RETRY_OR_FAIL: the budget only shrinks, so a retry
        would burn engineer iterations against the same exhausted cap.
        The infrastructure Finding is the record in the findings stream
        and the PR body; ``check=ADVERSARIAL_BUDGET_CHECK`` and the
        ``adversarial_budget:<phase>`` signature are the record in the
        journal, and ``ks serve`` reads the first of those to make the
        run terminal rather than retrying it (``serve.
        _budget_halt_outcome``). Advisory mode never reaches here: it
        keeps the recorded skip.

        THE SIGNATURE LEADS WITH THE CHECK, not with the phase, and that
        is what makes the journal and the replay agree about this run.
        ``evolution.split_signature`` takes everything before the first
        colon as the check name and ``_CATEGORY_BY_CHECK`` categorises
        it, which ``autonomy_replay.INFRA_FAILURE_PREFIXES`` is derived
        from. A ``review:`` prefix would file a reviewer that never ran
        under the reviewer's own category, and the replay would then
        count the run as a verdict about the factory's judgement while
        ``factory``'s live accounting, which asks the FINDING question,
        counts it as an infrastructure casualty and does not. #315's
        rule is that a taxonomy answering a question twice will
        eventually answer it two ways; ``adversarial_budget`` is
        enrolled as infrastructure, so both consumers read the same
        answer out of one table.
        """
        cap = self.factory_config.max_adversarial_calls
        # One sentence, used by the banner and by the Finding, so the two
        # cannot drift. The Finding carries ``phase`` as a field, so the
        # text does not name it.
        reason = (
            f"adversarial LLM budget ({cap}) exhausted before the phase ran; "
            "hard mode refuses to merge unreviewed"
        )
        error = f"{role} infrastructure error: {reason}"
        self.ui.err(f"  {banner} FAILED for {comp.id}: {error}")
        self._add_findings(
            comp,
            [Finding.infrastructure_error(phase=phase, explanation=reason)],
        )
        return PhaseFailure(
            action=FailureAction.FAIL,
            error=error,
            phase=phase,
            check=ADVERSARIAL_BUDGET_CHECK,
            signatures=[f"{ADVERSARIAL_BUDGET_CHECK}:{phase}"],
        )

    def _review_did_not_run(
        self,
        comp: Component,
        wt_path: Path,
        skip_reason: str | None,
        budget_downgraded: bool,
    ) -> ReviewPhaseResult:
        """Phase 2's tail for a review that was not executed: record the
        skip, then let the R10.3 set-point gate decide whether the
        component may proceed without one.

        Lifted out of ``_phase_review`` unchanged by #226, which added a
        branch above it and would otherwise have grown that method's
        branching past the ratchet.
        """
        comp.review_passed = None
        self._record_phase_skip(
            comp,
            "review",
            skip_reason or "review skipped",
        )
        # R10.3: this return is BEFORE the set-point gate, so a
        # component whose reviewer never ran would otherwise
        # complete with a story still claiming done and nothing
        # having checked it - the gate failing open, silently, at
        # exactly the moment the budget ran out. Only the budget
        # downgrade fails here: an explicit review_mode = "skip"
        # is the operator's decision, and run_factory already warns
        # at startup that the gate cannot fire under it.
        #
        # FAIL, not RETRY_OR_FAIL: retrying cannot recover budget,
        # so a retry would burn engineer iterations against a
        # deterministic refusal.
        #
        # #226 narrowed who reaches here: hard mode now refuses at the
        # exhausted budget instead of downgrading, so
        # ``budget_downgraded`` is only ever true for an advisory
        # reviewer. An advisory reviewer that never ran can still FAIL
        # the component here, so "advisory never blocks" is false as a
        # statement about this branch, whatever else it may describe.
        if (
            budget_downgraded
            and self._setpoint_blocking()[0]
            and self._has_unconfirmed_claim(comp, wt_path)
        ):
            error = (
                "Set-point agreement cannot be confirmed: the "
                "reviewer never ran (adversarial LLM budget "
                f"({self.factory_config.max_adversarial_calls}) "
                "exhausted) and a story is still marked passes=true"
            )
            self.ui.err(f"  Phase 2 FAILED for {comp.id}: {error}")
            return ReviewPhaseResult(
                ran=False,
                skip_reason=skip_reason,
                failure=PhaseFailure(
                    action=FailureAction.FAIL,
                    error=error,
                    phase="review",
                    check="setpoint",
                    # Same class as _budget_refusal's signature and swept
                    # with it (#226 round 2): the reviewer did not run, so
                    # a ``review:`` prefix would tell the replay this run
                    # produced a verdict about the factory's judgement.
                    # ``check`` stays "setpoint" - the field ``ks serve``
                    # reads is Component.failed_check, and this refusal is
                    # the set-point gate's, not the budget branch's.
                    signatures=[f"{ADVERSARIAL_BUDGET_CHECK}:setpoint"],
                ),
            )
        return ReviewPhaseResult(ran=False, skip_reason=skip_reason)

    def _security_budget_branch(
        self,
        comp: Component,
        sec_config: SecurityConfig,
    ) -> SecurityPhaseResult:
        """Phase 2.5's exhausted-budget branch, both modes.

        Same reason the review side keeps its mode split inside the
        budget check (see ``_phase_review``), and keyed the same way:
        the arm a mode added later would fall into is the REFUSAL, not
        the downgrade that merges a component no security reviewer
        looked at. ``SecurityConfig.__post_init__`` rejects any mode
        outside skip|advisory|hard, and skip already returned above, so
        the two arms are exactly hard and advisory today.
        """
        if sec_config.mode != SecurityMode.ADVISORY.value:
            # R10.5 (#226): same rule as Phase 2. Hard mode refuses to
            # merge a component no security reviewer looked at.
            return SecurityPhaseResult(
                ran=False,
                failure=self._budget_refusal(
                    comp,
                    phase="security",
                    banner="Phase 2.5",
                    role="Security review",
                ),
            )
        self.ui.warn(f"  Phase 2.5 SKIPPED for {comp.id}: adversarial LLM budget exhausted")
        self._record_phase_skip(
            comp,
            "security",
            "adversarial LLM budget exhausted",
        )
        return SecurityPhaseResult(
            ran=False,
            skip_reason="adversarial LLM budget exhausted",
        )

    def _review_failure(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
        review_result: ReviewResult,
    ) -> ReviewPhaseResult:
        """Phase 2's failure branch: build the retry context, then decide
        between retrying and the #265 divergence wall."""
        if review_result.coverage_refused:
            # BEFORE the "N failures" banner below: the coverage concern
            # is advisory, so a reviewer that returned passing criteria
            # would otherwise print "Phase 2 FAILED: 0 failures"
            # immediately above the line saying what actually went wrong.
            return ReviewPhaseResult(
                ran=True,
                result=review_result,
                failure=self._coverage_failure(
                    comp,
                    phase="review",
                    banner="Phase 2",
                    role="Review",
                    noun="verdict",
                    agent="review agent",
                    disagreement=review_result.diffstat_disagreement,
                    reported_a_stat=review_result.observed_diffstat is not None,
                ),
            )
        self.ui.warn(f"  Phase 2 FAILED for {comp.id}: {review_result.fail_count} failures")
        # #265: before spending another engineer run, ask whether the
        # last few have been spent moving away from a pass. Advisory by
        # default: this records the trip and keeps retrying, and returns
        # a message only under `[divergence] mode = "block"`.
        #
        # When it DOES block, it is FAIL rather than RETRY_OR_FAIL,
        # because the whole value is not paying for the attempt it
        # forecloses and an outcome that only reported would save
        # nothing. Stated plainly, because the other FAIL sites do not
        # work this way: budget exhaustion is a PROOF that retrying
        # cannot help (a budget only shrinks), while this is a FORECAST
        # from the loop's own trajectory. That gap is exactly why the
        # default is advisory, and why the forecast is built to fail
        # open - see the module docstring on which way its identity
        # heuristic errs. Do not read the precedent the other way round
        # and route a weaker forecast here.
        divergence = self._divergence_failure(comp, wt_path, review_result)
        if divergence is not None:
            return ReviewPhaseResult(
                ran=True,
                result=review_result,
                failure=PhaseFailure(
                    action=FailureAction.FAIL,
                    error=divergence,
                    phase="review",
                    check="divergence",
                    signatures=["review:divergence"],
                ),
            )
        # The retry context is built only on the branch that uses it: the
        # divergence wall above ends the component, so parsing and
        # re-rendering the accumulated context there would be work thrown
        # straight away.
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        ctx.add_review_finding(
            review_result.as_retry_context(),
            attempt=comp.retries + 1,
            phase="review",
            infrastructure=review_result.infrastructure_error,
        )
        # R6.1: journal the finding categories that failed the gate
        # ("review:scope_creep", "review:prd_criterion",
        # "review:infrastructure"), not the flattened reason.
        from kstrl.evolution import signatures_from_findings

        return ReviewPhaseResult(
            ran=True,
            result=review_result,
            failure=PhaseFailure(
                action=FailureAction.RETRY_OR_FAIL,
                error=(
                    "Review infrastructure error"
                    if review_result.infrastructure_error
                    else "Review failed"
                ),
                phase="review",
                check=("infrastructure" if review_result.infrastructure_error else "criteria"),
                context_json=ctx.to_json(),
                signatures=signatures_from_findings("review", review_result.as_findings()),
            ),
        )

    def _phase_review(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
        verification: VerificationResult,
    ) -> ReviewPhaseResult:
        """Phase 2: second-opinion review against the PRD."""
        review_mode = ReviewMode(self.factory_config.review_mode)
        review_skip_reason: str | None = None
        # R10.3: "the operator turned the reviewer off" and "the
        # reviewer ran out of budget" are both SKIP, and the set-point
        # gate has to tell them apart. The first is a choice, warned
        # about at startup and then honoured. The second is the reviewer
        # failing to run, which in blocking mode must not be spent as a
        # confirmation.
        budget_downgraded = False
        if review_mode == ReviewMode.SKIP:
            review_skip_reason = "review disabled (mode=skip)"
        elif not self.adversarial_budget_ok():
            # R10.5 (#226): hard mode refuses to merge unreviewed. The
            # reviewer is the sensor doing most of the catching, so an
            # exhausted budget must not shed it and let the component
            # through on mechanical checks alone. Nothing is skipped
            # and no event is invented for it (doctrine 6): the
            # Finding and the PhaseFailure are the record.
            #
            # The mode split is INSIDE the budget check, not a second
            # condition beside it, so the check stays closed over
            # ReviewMode and a mode added later cannot fall past both
            # branches and out of the budget check entirely. It keys on
            # ADVISORY rather than on HARD so that the arm it closes
            # ONTO is the refusal: keyed the other way, a mode added
            # later would merge unreviewed by default, which is the
            # fail-open direction and the exact outcome #226 exists to
            # remove. SKIP returned above and both remaining members are
            # named here, so this is the same behaviour today as
            # ``== HARD`` was, measured by the suite staying green.
            # Phase 2.5 has the same shape for the same reason.
            if review_mode != ReviewMode.ADVISORY:
                comp.review_passed = False
                return ReviewPhaseResult(
                    ran=False,
                    failure=self._budget_refusal(
                        comp,
                        phase="review",
                        banner="Phase 2",
                        role="Review",
                    ),
                )
            # Advisory downgrades to a recorded skip instead (R1.2
            # trace). That is not the same as "advisory cannot fail the
            # component": under setpoint_agreement = "block" the R10.3
            # gate in _review_did_not_run fails it a few lines below,
            # because a reviewer that never ran cannot confirm a story
            # the engineer marked passes=true.
            self.ui.warn(
                f"  Phase 2 SKIPPED for {comp.id}: "
                f"adversarial LLM budget "
                f"({self.factory_config.max_adversarial_calls}) exhausted"
            )
            review_skip_reason = (
                f"adversarial LLM budget ({self.factory_config.max_adversarial_calls}) exhausted"
            )
            review_mode = ReviewMode.SKIP
            budget_downgraded = True
        if review_mode == ReviewMode.SKIP:
            return self._review_did_not_run(
                comp,
                wt_path,
                review_skip_reason,
                budget_downgraded,
            )

        from kstrl.agents import get_agent

        self.adversarial_budget_consume()
        self.ui.info(f"  Phase 2: review ({review_mode.value}) for {comp.id}...")

        # Forensic home for full raw reviewer output on parse failures
        # (R1.2; mirrors knowledge.py's _debug/<run_id>/ layout).
        adversarial_debug_dir = self._debug_dir_for(comp.id)

        # R1.2: wrap the agent-driven work like Phase 2.5 does. A
        # reviewer crash degrades to a per-component infrastructure
        # failure; it must never abort the whole factory run.
        review_agent: Any = None
        try:
            # R7.1: the run-level selection (explicit config, or the
            # cross-family default, or the warned same-family
            # fallback) decides who reviews.
            review_agent = get_agent(
                self.review_selection.agent_cmd,
                self.review_selection.model,
                self.review_selection.reasoning,
                self.review_selection.agent_type,
                # #266: the reviewer reads the tree it is judging, so it
                # must not be able to write it. Not an operator knob:
                # measured, an unsandboxed reviewer left __pycache__/
                # behind in the worktree it was judging. The operator's
                # OS-level intent rides ALONGSIDE it - the two are
                # layered, not alternatives.
                #
                # One carve-out, and it is not silent: a custom
                # ``agent_cmd`` resolves to CustomAgent, which has no
                # generic sandbox surface and drops BOTH settings.
                # run_factory warns per reviewer role at startup rather
                # than letting an operator believe in a boundary that is
                # not there.
                sandbox=self.sandbox_config,
                read_only=True,
            )
            with self._phase_transcript(comp.id, "review") as on_line:
                review_result = self.hooks.run_review(
                    review_agent,
                    wt_path / comp.prd_path,
                    wt_path,
                    self.manifest.base_branch,
                    verification,
                    review_mode,
                    self.ui,
                    debug_dir=adversarial_debug_dir,
                    on_line=on_line,
                )
        except Exception as exc:  # noqa: BLE001
            self.ui.warn(f"  Review crashed: {exc}")
            review_result = ReviewResult(
                passed=review_mode != ReviewMode.HARD,
                mode=review_mode.value,
                overall_notes=f"Review agent crashed: {exc}",
                infrastructure_error=True,
                # R7.1: a crash before/inside the run is still
                # attributed to the selected reviewer identity.
                reviewer_model=self.review_selection.identity,
            )
        # R3.1: the instance is fresh per phase, so its accumulated
        # records are exactly this review's spend. Recorded before
        # pass/fail handling so a failed or crashed review still
        # counts.
        if review_agent is not None:
            self._record_usage(comp.id, "review", collect_usage(review_agent))
        breached = self.breached_ceiling()
        if breached is not None:
            return ReviewPhaseResult(
                ran=True,
                result=review_result,
                failure=PhaseFailure(
                    action=FailureAction.TOKEN_BUDGET,
                    error=f"budget exceeded ({breached})",
                    phase="review",
                ),
            )
        comp.review_passed = review_result.passed
        # E3: typed findings are the source of truth; the rendered
        # string is a derived view kept for backward-compat consumers.
        self._add_findings(comp, review_result.as_findings())
        comp.review_findings = review_result.as_pr_body_section()
        # Observability gets criterion-only counts to preserve the
        # historical meaning of fail_count = "failed PRD criteria".
        # Concern counts ride along separately via fail_concerns /
        # advisory_concerns so dashboards can distinguish.
        self.bus.emit(
            ev.ReviewResultEvent(
                component=comp.id,
                passed=review_result.passed,
                mode=review_mode.value,
                fail_count=review_result.criterion_fail_count,
                advisory_count=review_result.criterion_advisory_count,
                duration_seconds=round(review_result.duration_seconds, 2),
            )
        )

        # R10.3 set-point agreement. The engineer agent is the only
        # writer of the PRD's `passes` flag, so a story marked done is a
        # claim by the thing that did the work, not a measurement of it.
        # The reviewer's per-story verdicts are a second and independent
        # reading of the same question. Require them to agree.
        #
        # Ordering, which a reviewer will want to check: this block runs
        # BEFORE the hard-mode failure return below, so a criterion
        # failure and a set-point disagreement in the same attempt both
        # reach the findings stream. The existing failure path then
        # returns exactly as it always did, carrying its own criterion
        # text in the retry context. The new failure path further down
        # fires only when the review PASSED and a story it did not
        # confirm is still marked done.
        blocking, severity = self._setpoint_blocking()
        prd_path = wt_path / comp.prd_path
        setpoint_prd: PRD | None = None
        disagreements: list[Finding] = []
        try:
            setpoint_prd = PRD.load(prd_path)
        except (OSError, ValueError) as exc:
            # The sensor could not run. E9/E3-infra: record that, so
            # len(findings) == 0 keeps meaning "every sensor ran and
            # found nothing" rather than "one of them was silent".
            #
            # Recorded, but never blocking, even in blocking mode. An
            # unreadable PRD holds no claim to disagree with, and it is
            # already caught twice over: check_prd_stories fails Phase 1
            # on it, and run_review loads the same file to build its
            # coverage gate, so a review that PASSED is itself evidence
            # the file parsed moments earlier. A third gate here would
            # guard a state the second one rules out.
            self._add_findings(
                comp,
                [
                    Finding.infrastructure_error(
                        phase="review",
                        explanation=(
                            "Set-point agreement not measured: the PRD at "
                            f"{comp.prd_path} could not be read: {exc}"
                        ),
                    )
                ],
            )
        else:
            disagreements = setpoint_disagreements(
                setpoint_prd,
                review_result,
                severity=severity,
            )
            self._add_findings(comp, disagreements)

        if not review_result.passed:
            return self._review_failure(comp, comp_result, wt_path, review_result)

        # R10.3: the review itself passed. It can still have declined to
        # confirm a story the engineer marked done, and in blocking mode
        # that is a failure of the component, not a footnote on a pass.
        if (
            blocking
            and review_result.infrastructure_error
            and setpoint_prd is not None
            and any(st.passes for st in setpoint_prd.user_stories)
        ):
            # Halt over heroics. In ADVISORY review mode a crashed or
            # unparseable reviewer yields passed=True with
            # infrastructure_error=True (the crash handler above sets
            # `passed=review_mode != HARD`), so the failure path above
            # does not fire. setpoint_disagreements correctly returns
            # nothing - absence of a reading is not disagreement - but
            # in BLOCKING mode "the second sensor never reported" must
            # not be spent as "the second sensor confirmed". A story
            # still claims done and nothing independent has checked it.
            #
            # Nothing is reverted here: no evidence points at any
            # particular story, and retrying is what a reviewer outage
            # calls for. The outage itself is already in the findings
            # via ReviewResult.as_findings.
            self.ui.warn(
                f"  Phase 2 FAILED for {comp.id}: set-point agreement "
                "cannot be confirmed, the reviewer did not report"
            )
            return self._setpoint_failure(
                comp,
                comp_result,
                review_result,
                error=(
                    "Set-point disagreement: the reviewer produced no "
                    "usable verdict, so no story claimed done is confirmed"
                ),
                retry_text=(
                    "The reviewer did not produce a usable verdict this "
                    "attempt, so no story you marked done has been "
                    "independently confirmed. Nothing in the PRD was "
                    "changed. Re-run and make sure the work still stands "
                    "on its own evidence."
                ),
            )
        if blocking and disagreements and setpoint_prd is not None:
            reverted = revert_unconfirmed_stories(
                setpoint_prd,
                review_result,
                disagreements,
                attempt=comp.retries + 1,
            )
            saved = True
            try:
                setpoint_prd.save(prd_path)
            except OSError as exc:
                # process_result is not wrapped by its caller
                # (factory.py, the scheduler loop), so an exception
                # escaping here would abort the whole run and take every
                # other component's work with it. Degrade to a
                # per-component infrastructure finding, the same way a
                # reviewer crash does. The component still fails: the
                # disagreement is real whether or not the file could be
                # rewritten. What changes is the retry text, which must
                # not claim a revert that did not happen.
                saved = False
                self._add_findings(
                    comp,
                    [
                        Finding.infrastructure_error(
                            phase="review",
                            explanation=(
                                f"Set-point revert could not be written to {comp.prd_path}: {exc}"
                            ),
                        )
                    ],
                )
            self.ui.warn(
                f"  Phase 2 FAILED for {comp.id}: set-point disagreement "
                f"on {len(reverted)} story(ies)"
                + ("; passes reverted in the PRD" if saved else "; PRD could not be rewritten")
            )
            return self._setpoint_failure(
                comp,
                comp_result,
                review_result,
                error=(
                    f"Set-point disagreement: {len(reverted)} story(ies) "
                    "claimed done but not confirmed by review"
                ),
                retry_text=setpoint_retry_context(
                    disagreements,
                    review_result,
                    reverted=saved,
                ),
            )

        self.ui.ok(f"  Phase 2 passed for {comp.id}")
        return ReviewPhaseResult(ran=True, result=review_result)

    def _has_unconfirmed_claim(self, comp: Component, wt_path: Path) -> bool:
        """R10.3: whether the PRD still claims a story is done.

        Used on the skip path, where the reviewer never ran and there is
        no ReviewResult to compare against. A PRD that cannot be read
        answers False, for the same reason the main gate records but
        does not block on one: an unreadable PRD holds no claim to
        disagree with, and Phase 1's check_prd_stories fails on it
        first.
        """
        try:
            prd = PRD.load(wt_path / comp.prd_path)
        except (OSError, ValueError):
            return False
        return any(story.passes for story in prd.user_stories)

    def _setpoint_blocking(self) -> tuple[bool, str]:
        """R10.3: whether a set-point disagreement fails the component,
        and the severity its findings carry.

        The autonomy level is resolved exactly as ``_phase_verify``
        resolves it for the adequacy gate: the stored level when the
        ladder is on, and 0 when it is off.
        """
        autonomy_cfg = AutonomyConfig.load(self.root_dir)
        level = AutonomyState.load(self.root_dir).level if autonomy_cfg.enabled else 0
        blocking = setpoint_blocks(self.factory_config, level)
        return blocking, "fail" if blocking else "advisory"

    def _setpoint_failure(
        self,
        comp: Component,
        comp_result: ComponentResult,
        review_result: ReviewResult,
        *,
        error: str,
        retry_text: str,
    ) -> ReviewPhaseResult:
        """R10.3: the typed failure a blocked set-point check returns.

        Note for anyone tracing the retry context: this is the first
        site that builds an ``IterationContext`` on a review that
        PASSED. Every other site builds one only on a failure path,
        which is the asymmetry issue #247 is about. The entry is filed
        under phase "review" so a later review reading retires it.

        Two different failures route through here and they must not be
        recorded as the same thing. A DISAGREEMENT is a measurement: the
        reviewer ran, reported, and declined to confirm the claim. An
        OUTAGE is the absence of one. R10.2 draws that line explicitly
        (``FailureEntry.infrastructure``): only a measured entry retires
        its own phase, because a crashed sensor that retired a real
        earlier finding would silently drop it. Journalling an outage as
        ``review:setpoint_disagreement`` would also tell the evolution
        data that a reviewer disagreed when none reported.
        """
        infrastructure = review_result.infrastructure_error
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        ctx.add_review_finding(
            retry_text,
            attempt=comp.retries + 1,
            phase="review",
            infrastructure=infrastructure,
        )
        return ReviewPhaseResult(
            ran=True,
            result=review_result,
            failure=PhaseFailure(
                action=FailureAction.RETRY_OR_FAIL,
                error=error,
                phase="review",
                check="infrastructure" if infrastructure else "setpoint",
                context_json=ctx.to_json(),
                # R6.1: the journal signature. Not derived via
                # signatures_from_findings, which only emits for
                # severity fail/critical/high - true for a disagreement,
                # but the signature must not silently depend on that.
                signatures=[
                    "review:infrastructure"
                    if infrastructure
                    else f"review:{SETPOINT_DISAGREEMENT_CATEGORY}"
                ],
            ),
        )

    def _phase_security(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
    ) -> SecurityPhaseResult:
        """Phase 2.5: security review (adversarial pass focused on
        vulns). Runs as a separate LLM call with its own threat-model
        framing so it catches what the correctness reviewer misses.
        Hard-mode fails the component on findings at or above
        SecurityConfig.fail_threshold OR on infrastructure errors."""
        sec_config = self.factory_config.security_config
        if sec_config is None:
            self._record_phase_skip(
                comp,
                "security",
                "security review not configured",
            )
            return SecurityPhaseResult(
                ran=False,
                skip_reason="security review not configured",
            )
        if sec_config.mode == SecurityMode.SKIP.value:
            self._record_phase_skip(
                comp,
                "security",
                "security review disabled (mode=skip)",
            )
            return SecurityPhaseResult(
                ran=False,
                skip_reason="security review disabled (mode=skip)",
            )
        if not self.adversarial_budget_ok():
            return self._security_budget_branch(comp, sec_config)
        self.adversarial_budget_consume()
        from kstrl.agents import get_agent as _get_sec_agent

        self.ui.info(f"  Phase 2.5: security review ({sec_config.mode}) for {comp.id}...")
        sec_result = None
        sec_agent: Any = None
        # R7.1: the run-level selection already folded in the
        # explicit sec_config fields and the engineer fallbacks (or
        # picked the cross-family default). sec_config is non-None
        # and non-skip here, so the selection was resolved at run
        # start.
        assert self.security_selection is not None
        adversarial_debug_dir = self._debug_dir_for(comp.id)
        # The try/except deliberately wraps ONLY the agent-driven
        # work (getting the agent + running the review). Errors in
        # the retry-or-fail path below must NOT be swallowed - if
        # they were, a hard-mode security failure could fall through
        # to PR creation as if it had passed.
        try:
            sec_agent = _get_sec_agent(
                self.security_selection.agent_cmd,
                self.security_selection.model,
                self.security_selection.reasoning,
                self.security_selection.agent_type,
                # #266: same rule as Phase 2 - a reviewer must not be
                # able to write the tree it is judging, and the
                # operator's sandbox intent rides alongside.
                sandbox=self.sandbox_config,
                read_only=True,
            )
            with self._phase_transcript(comp.id, "security") as on_line:
                sec_result = self.hooks.run_security_review(
                    sec_agent,
                    wt_path / comp.prd_path,
                    wt_path,
                    self.manifest.base_branch,
                    sec_config,
                    self.ui,
                    debug_dir=adversarial_debug_dir,
                    on_line=on_line,
                )
        except Exception as exc:  # noqa: BLE001
            # Agent infrastructure failed before run_security_review
            # could classify the outcome. Synthesize an infra result
            # and fall through to the shared recording block below:
            # hard mode blocks via passed=False, advisory continues
            # but the infra finding stays in the findings stream and
            # the PR body instead of vanishing (R1.2, sec-pr-body).
            self.ui.warn(f"  Security review crashed: {exc}")
            sec_result = SecurityResult(
                passed=sec_config.mode != SecurityMode.HARD.value,
                mode=sec_config.mode,
                overall_notes=(f"Security review agent failed before completion: {exc}"),
                infrastructure_error=True,
                # R7.1: a crash before/inside the run is still
                # attributed to the selected reviewer identity.
                reviewer_model=self.security_selection.identity,
            )

        # R3.1: record security spend before pass/fail handling so
        # failed and crashed passes still count toward the meter.
        if sec_agent is not None:
            self._record_usage(comp.id, "security", collect_usage(sec_agent))
        breached = self.breached_ceiling()
        if breached is not None:
            return SecurityPhaseResult(
                ran=True,
                result=sec_result,
                failure=PhaseFailure(
                    action=FailureAction.TOKEN_BUDGET,
                    error=f"budget exceeded ({breached})",
                    phase="security",
                ),
            )

        if sec_result is not None:
            self.bus.emit(
                ev.ReviewResultEvent(
                    component=comp.id,
                    passed=sec_result.passed,
                    mode=f"security-{sec_config.mode}",
                    fail_count=sec_result.critical_count + sec_result.high_count,
                    advisory_count=len(sec_result.findings),
                    duration_seconds=round(sec_result.duration_seconds, 2),
                )
            )

            # E3: source-of-truth typed findings list, plus the
            # legacy rendered string for PR body / manifest readers.
            self._add_findings(comp, sec_result.as_findings())
            if sec_result.findings:
                if comp.review_findings:
                    comp.review_findings = (
                        comp.review_findings + "\n\n" + sec_result.as_pr_body_section()
                    )
                else:
                    comp.review_findings = sec_result.as_pr_body_section()

            if not sec_result.passed:
                return self._security_failure(comp, comp_result, sec_result)

        return SecurityPhaseResult(ran=True, result=sec_result)

    def _security_failure(
        self,
        comp: Component,
        comp_result: ComponentResult,
        sec_result: SecurityResult,
    ) -> SecurityPhaseResult:
        """Phase 2.5's failure branch, mirroring ``_review_failure``.

        Extracted so the two phases route a failure the same way and in
        the same shape - the coverage wall first, the retry path after -
        rather than one of them being a method and the other twenty
        lines inline in the middle of the phase.
        """
        if sec_result.coverage_refused:
            return SecurityPhaseResult(
                ran=True,
                result=sec_result,
                failure=self._coverage_failure(
                    comp,
                    phase="security",
                    banner="Phase 2.5",
                    role="Security review",
                    noun="findings",
                    agent="security agent",
                    disagreement=sec_result.diffstat_disagreement,
                    reported_a_stat=sec_result.observed_diffstat is not None,
                ),
            )
        reason = (
            "Security review crashed"
            if sec_result.infrastructure_error
            else "Security review failed"
        )
        self.ui.warn(
            f"  Phase 2.5 FAILED for {comp.id}: "
            f"{sec_result.critical_count} critical, "
            f"{sec_result.high_count} high"
        )
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        # as_retry_context is empty for infra results (no findings list);
        # fall back to the notes so the retry prompt still says what
        # went wrong.
        ctx.add_review_finding(
            sec_result.as_retry_context()
            or "Security review infrastructure error: " + sec_result.overall_notes,
            attempt=comp.retries + 1,
            phase="security",
            infrastructure=sec_result.infrastructure_error,
        )
        # R6.1: journal the vuln categories that failed the gate
        # ("security:injection", ...), not the reason.
        from kstrl.evolution import signatures_from_findings

        return SecurityPhaseResult(
            ran=True,
            result=sec_result,
            failure=PhaseFailure(
                action=FailureAction.RETRY_OR_FAIL,
                error=reason,
                phase="security",
                check=("infrastructure" if sec_result.infrastructure_error else "findings"),
                context_json=ctx.to_json(),
                signatures=signatures_from_findings("security", sec_result.as_findings()),
            ),
        )

    def _phase_distill(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
        shared_diff: str,
    ) -> DistillPhaseResult:
        """Knowledge distillation: the PRE-PR step (R7.3 decision).

        Voyager-style post-gate write: runs after Phase 2/2.5 succeed
        (or are skipped) but BEFORE the PR merge step pulls main into
        the worktree, so the distilled diff is the component's true
        delta. Placement is deliberate - moving it post-merge would
        hand the distiller a diff polluted by the merge commit and
        break the "true delta" invariant. Non-fatal on any failure.

        In single_pr mode every component shares one branch, which
        means `git diff base...HEAD` for component B also includes
        A's changes - distillation would write facts for B citing
        A's code as evidence. Skip the phase entirely until A2's
        follow-up wires up per-component diff isolation.

        Fact utilization is NOT measured here. It is measured as soon as
        the diff phase produces a diff, so that components which fail
        review or security are still sampled - see
        ``record_fact_utilization``. This phase only reads the stored
        result to put it on the ``DistillResult`` event.
        """
        knowledge_config = self.knowledge_config
        if not knowledge_config.enabled:
            return DistillPhaseResult(
                ran=False,
                skip_reason="knowledge disabled",
            )
        if self.manifest.single_pr:
            self.ui.info(
                f"  Knowledge: skipped for {comp.id} "
                f"(single_pr mode produces a polluted per-component diff)"
            )
            self._record_phase_skip(
                comp,
                "knowledge",
                "single_pr mode produces a polluted per-component diff",
            )
            return DistillPhaseResult(
                ran=False,
                skip_reason=("single_pr mode produces a polluted per-component diff"),
            )
        # Already measured at the diff phase, for every component that
        # got that far - including the ones that then failed review or
        # security and never reach this line. Read, do not re-measure:
        # measuring again here would score the same component twice and
        # re-introduce the ordering hazard that put the measurement
        # after distillation's writes in the first place.
        util = self.fact_utilization.get(comp.id) or FactUtilization()

        if not self.adversarial_budget_ok():
            self.ui.info(f"  Knowledge: skipped for {comp.id} (adversarial budget exhausted)")
            self._record_phase_skip(
                comp,
                "knowledge",
                "adversarial LLM budget exhausted",
            )
            return DistillPhaseResult(
                ran=False,
                skip_reason="adversarial LLM budget exhausted",
                utilization=util,
            )
        breached = self.breached_ceiling()
        if breached is not None:
            # R3.1: the gates all passed before the ceiling tripped, so
            # the component proceeds to PR - but no further LLM spend.
            # The skip is recorded, and the scheduling gate stops any
            # remaining components loudly.
            detail = (
                f"{self.run_usage.total_tokens} >= {self.factory_config.max_total_tokens}"
                if breached == "max_total_tokens"
                else (f"${self.run_usage.cost_usd:.6f} >= ${self.factory_config.max_cost_usd}")
            )
            self.ui.warn(
                f"  Knowledge: skipped for {comp.id} (budget exceeded, {breached}: {detail})"
            )
            self._record_phase_skip(
                comp,
                "knowledge",
                f"budget ({breached}) exceeded",
            )
            return DistillPhaseResult(
                ran=False,
                skip_reason=f"budget ({breached}) exceeded",
                utilization=util,
            )

        self.adversarial_budget_consume()
        distill_agent: Any = None
        try:
            from kstrl.agents import get_agent as _get_agent

            # Reuse the diff already fetched by the diff phase - the
            # worktree state hasn't changed between Phase 1 and here.
            diff_content = shared_diff
            distill_model = knowledge_config.distill_model or self.base_config.model
            distill_agent = _get_agent(
                self.base_config.agent_cmd,
                distill_model,
                self.base_config.model_reasoning_effort,
                self.base_config.agent_type,
            )
            distill_start = time.monotonic()
            with self._phase_transcript(comp.id, "distill") as on_line:
                written, status = self.hooks.distill_facts(
                    distill_agent,
                    comp,
                    diff_content,
                    wt_path / comp.prd_path,
                    comp_result.iterations,
                    self.run_id,
                    knowledge_config.knowledge_root,
                    knowledge_config,
                    wt_path,
                    comp.review_passed,
                    on_line=on_line,
                )
            self.bus.emit(
                ev.DistillResult(
                    component=comp.id,
                    facts_written=written,
                    duration_seconds=round(
                        time.monotonic() - distill_start,
                        2,
                    ),
                )
            )
            if written > 0:
                self.ui.ok(f"  Knowledge: {status}")
            else:
                self.ui.info(f"  Knowledge: {status}")
        except Exception as exc:  # noqa: BLE001 - non-fatal
            self.ui.warn(f"  Knowledge distillation failed: {exc}")

        # R3.1: distillation spend (recorded even when the distill
        # failed - the call still cost tokens). No fail-the-component
        # checkpoint here: every gate already passed; the scheduling
        # gate halts the run before any FURTHER spend.
        if distill_agent is not None:
            self._record_usage(
                comp.id,
                "distill",
                collect_usage(distill_agent),
            )

        return DistillPhaseResult(ran=True, utilization=util)

    def _measure_utilization(
        self,
        comp: Component,
        wt_path: Path,
        shared_diff: str,
    ) -> FactUtilization:
        """Did the engineer reference any fact we injected? (#191)

        Measured against the prefix the factory recorded at SUBMIT time
        (``record_injected_knowledge``), never a rebuild - see that
        method for why a rebuild reports a corrupted number.

        Costs zero tokens: it is a substring scan, no LLM call. That is
        why the caller runs it above the adversarial-budget and
        cost-ceiling guards and outside the distillation try block. The
        answer to "did the engineer use what we gave it" is in the diff
        and does not depend on distillation succeeding, on the run
        having budget left, or on anything else downstream.

        Never raises. A failure returns ``measured=False`` with the
        cause in ``reason``, which is deliberately NOT the same value
        as a measured zero.
        """
        _MISSING = "\x00missing"
        prefix = self.injected_knowledge.get(comp.id, _MISSING)
        if prefix == _MISSING:
            return FactUtilization(
                reason="no injected prefix recorded for this attempt",
            )
        if prefix is None:
            return FactUtilization(reason="knowledge retrieval failed")
        if not prefix:
            # Knowledge was on and retrieval succeeded with nothing to
            # inject - a cold store. A measured zero, not a failure:
            # "the layer has not warmed up yet" is honest evidence.
            return FactUtilization(measured=True)
        try:
            progress_text = ""
            # Resolved exactly the way the engineer's worker resolved
            # it, so the metric reads the file this component actually
            # wrote. The hardcoded scripts/kstrl/progress.txt this
            # replaced was a second copy of the out-of-scope default
            # and read nothing for every decomposed component.
            progress_path = wt_path / self.base_config.component_progress_file(
                comp.prd_path,
                self.root_dir,
            )
            try:
                progress_text = progress_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # One clause: ``progress_text`` stays "" either way and
                # the metric below degrades to "no evidence", which is
                # the same answer for both causes and is reported as
                # such. #320's separate-remedy rule needs a message to
                # separate; this site emits none.
                pass
            # The diff MUST go in as `diff=`, never as a positional
            # artifact. Artifacts are searched raw; only `diff=` is
            # reduced to added lines. Passing it positionally silently
            # restores the false positive where deleting the code that
            # expressed a fact scores as referencing it - and it does so
            # invisibly, because the signature accepts *artifacts, so
            # neither mypy nor a permissive test stub can see it.
            # TestFactUtilizationUsesTheRealMatcher pins this call shape
            # against the real matcher for exactly that reason.
            util = self.hooks.measure_fact_utilization(
                prefix,
                progress_text,
                diff=shared_diff,
            )
            # .get for the per-tier keys: this is an injected seam, and
            # a hook that only reports the totals must degrade to "no
            # tier breakdown", not blow up the measurement.
            return FactUtilization(
                measured=True,
                injected=int(util["injected"]),
                referenced=int(util["referenced"]),
                core_injected=int(util.get("core_injected", 0)),
                core_referenced=int(util.get("core_referenced", 0)),
                dependency_injected=int(util.get("dependency_injected", 0)),
                dependency_referenced=int(
                    util.get("dependency_referenced", 0),
                ),
                sibling_injected=int(util.get("sibling_injected", 0)),
                sibling_referenced=int(util.get("sibling_referenced", 0)),
            )
        except Exception as exc:  # noqa: BLE001 - non-fatal, never silent
            # Was a bare `except: pass`. Silence made a broken recorder
            # indistinguishable from "the engineer referenced nothing",
            # so the L2+ gate could never accrue and never say why.
            self.ui.warn(f"  Knowledge utilization measurement failed: {exc}")
            return FactUtilization(reason=f"{type(exc).__name__}: {exc}")

    def _phase_checkpoint(
        self,
        comp: Component,
        *,
        diff_text: str = "",
    ) -> CheckpointDecision:
        """E6: human-in-the-loop checkpoint. When opt-in, prompt
        before pushing+merging so a human can inspect the diff,
        the review findings, and the security findings before
        the PR goes through. Reject is terminal (R2.6): it marks
        the component FAILED and cascade-skips dependents with no
        retry and no re-prompt - routing it through the retry
        loop would re-run the full agent+review cycle and ask the
        human again, once per remaining retry. A human who wants
        a re-run says so explicitly via Retry, which consumes a
        retry like any other failure. When no UI is interactive
        the prompt is skipped but the gate is NOT: R8.3 returns
        PARKED, which withholds the merge and files a merge_gate
        inbox item - automation fails loudly rather than blocking
        indefinitely OR merging something nobody approved."""
        if not self.factory_config.pause_before_pr_merge:
            return CheckpointDecision.NOT_PROMPTED
        question = f"Approve PR creation and merge for {comp.id}?"
        self.bus.emit(
            ev.CheckpointRequested(
                component=comp.id,
                kind="pr_merge",
                question=question,
            )
        )
        request = PromptRequest(
            kind=PromptKind.CHECKPOINT,
            header=question,
            options=(
                "Approve",
                "Reject (fail component, skip dependents)",
                "Retry (consume a retry, re-run component)",
            ),
            default=0,
            component_id=comp.id,
            checkpoint=CheckpointContext(
                component_id=comp.id,
                diff_excerpt=git.truncate_diff_for_prompt(
                    diff_text,
                    CHECKPOINT_DIFF_CHAR_LIMIT,
                )
                if diff_text
                else "",
                review_findings=tuple(f for f in comp.findings if f.phase == "review"),
                security_findings=tuple(f for f in comp.findings if f.phase == "security"),
                usage=self.usage_totals_for(comp.id),
                branch=comp.branch_name,
            ),
        )
        if not self.interaction.can_prompt():
            # R8.3: the merge gate is the whole point of
            # pause_before_pr_merge, and proceeding here silently merged
            # without the approval that was asked for - in exactly the
            # unattended case R8.2's L1/L2 forces the gate ON for. Park
            # the component instead and route the decision to the inbox;
            # `ks inbox retry` requeues it once a human has looked.
            self.ui.warn(
                f"  pause_before_pr_merge requested but UI is "
                f"non-interactive; parking {comp.id} for approval "
                f"(see `ks inbox ls`)"
            )
            self.bus.emit(
                ev.CheckpointResolved(
                    component=comp.id,
                    kind="pr_merge",
                    decision="parked",
                    decided_by="inbox",
                )
            )
            self._inbox_add(
                ItemKind.MERGE_GATE,
                f"{comp.id} awaiting merge approval",
                detail=(
                    "pause_before_pr_merge is on but no interactive UI was "
                    "available, so the merge was NOT performed. Review the "
                    "component, then `ks inbox retry <id>` to requeue it."
                ),
                component=comp.id,
                dedupe_key=f"merge-gate:{comp.id}",
                evidence={
                    "branch": comp.branch_name,
                    "review_findings": len([f for f in comp.findings if f.phase == "review"]),
                },
            )
            return CheckpointDecision.PARKED
        self.ui.section(f"Human checkpoint: {comp.id}")
        self.ui.info(comp.review_findings or "(no review findings)")
        response = self.interaction.request(request)
        if not response.answered:
            # The channel lost its resolver between the guard and the
            # answer (detached TUI): same semantics as non-interactive.
            self.bus.emit(
                ev.CheckpointResolved(
                    component=comp.id,
                    kind="pr_merge",
                    decision="not_prompted",
                    decided_by="auto",
                )
            )
            return CheckpointDecision.NOT_PROMPTED
        decision = {
            1: CheckpointDecision.REJECTED,
            2: CheckpointDecision.RETRY,
        }.get(response.choice, CheckpointDecision.APPROVED)
        self.bus.emit(
            ev.CheckpointResolved(
                component=comp.id,
                kind="pr_merge",
                decision=decision.name.lower(),
                decided_by="operator",
            )
        )
        if decision == CheckpointDecision.REJECTED:
            self.ui.warn(f"  Human rejected {comp.id} at PR checkpoint")
        elif decision == CheckpointDecision.RETRY:
            self.ui.warn(f"  Human requested retry for {comp.id} at PR checkpoint")
        return decision

    def _phase_pr(self, comp: Component) -> PrPhaseResult:
        """Per-component PR create+merge. single_pr mode is exempt
        (handled by the caller): every component shares one branch, a
        single PR is created at end-of-run, and squash-merging the
        shared branch per component would destroy the history the
        remaining components build on."""
        from kstrl.pr import is_gh_available, push_create_and_merge_pr

        if not is_gh_available():
            # No gh: the PR/merge gate cannot run. Completing anyway
            # preserves local-only workflows, but say so loudly -
            # this component's code exists only on its local branch.
            self.ui.warn(
                f"  gh CLI not available: {comp.id} completes without "
                f"a PR; its code stays on branch {comp.branch_name}"
            )
            return PrPhaseResult(disposition=PrDisposition.NO_GH)

        self.ui.info(f"  Creating and merging PR for {comp.id}...")
        outcome = push_create_and_merge_pr(
            comp,
            self.manifest,
            self.root_dir,
            self.ui,
            merge_method="squash",
            merge_timeout=self.factory_config.merge_timeout,
        )
        if outcome.pr_url:
            self.factory_result.pr_urls.append(outcome.pr_url)
            self.bus.emit(
                ev.PrCreated(
                    component=comp.id,
                    pr_number=comp.pr_number or 0,
                    pr_url=outcome.pr_url,
                )
            )
        self.manifest.save(self.manifest_path)

        # R0.2 (CRIT-2): COMPLETED requires a CONFIRMED merge.
        # Anything less and dependents would cut worktrees from
        # a base that lacks this component's code.
        if not outcome.merged:
            if outcome.merge_conflict:
                # R7.5: conflicts route to the re-run doctrine.
                return PrPhaseResult(
                    disposition=PrDisposition.CONFLICT,
                    pr_url=outcome.pr_url,
                    error=outcome.error or "PR conflicts with base",
                )
            if outcome.merge_pending:
                return PrPhaseResult(
                    disposition=PrDisposition.MERGE_PENDING,
                    pr_url=outcome.pr_url,
                    error=outcome.error or "PR merge not confirmed",
                )
            return PrPhaseResult(
                disposition=PrDisposition.FAILED,
                pr_url=outcome.pr_url,
                error=outcome.error or "PR flow failed",
            )
        self.bus.emit(
            ev.PrMerged(
                component=comp.id,
                pr_number=comp.pr_number or 0,
                pr_url=outcome.pr_url,
            )
        )
        return PrPhaseResult(
            disposition=PrDisposition.MERGED,
            pr_url=outcome.pr_url,
        )
