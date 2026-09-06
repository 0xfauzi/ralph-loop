"""Factory orchestrator - parallel component execution with 3-phase verification."""

from __future__ import annotations

import functools
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Protocol, TextIO

from kstrl.agents.base import UsageTotals, collect_usage, print_usage_rollup
from kstrl.agents.proc import kill_active_process_groups
from kstrl.atomicio import atomic_write_json
from kstrl.autonomy import (
    AutonomyConfig,
    AutonomyLevel,
    AutonomyState,
    DemotionTrigger,
    apply_demotion,
    flag_bundle_for,
    manual_override_notes,
    resolve_runtime_level,
    save_ladder_state,
)
from kstrl.breaker import BreakerConfig
from kstrl.commandrun import start_heartbeat as _start_heartbeat
from kstrl.config import (
    KstrlConfig,
    component_progress_path,
    relative_to_root,
)
from kstrl.context import IterationContext
from kstrl.contract import (
    ContractCleanupError,
    ContractConfig,
    ContractMode,
    ContractResult,
    run_contract_testing,
)
from kstrl.decisions import (
    DecisionRegisterError,
    SpecDecision,
    bind_register,
    build_decisions_context,
    read_decisions,
)
from kstrl.events import (
    AdversarialAgentSelected,
    AutonomyLevelApplied,
    ComponentFailed,
    ComponentScopeResolved,
    ComponentStarted,
    EventBus,
    EventSink,
    JsonlSink,
    PhaseStarted,
    RunCompleted,
    RunPaths,
    RunPlan,
    RunStarted,
    V1CompatSink,
)
from kstrl.events import (
    ContractResult as ContractResultEvent,
)
from kstrl.feedforward import FeedforwardConfig, build_feedforward_context
from kstrl.findings import POLICY_CATEGORY_PREFIX
from kstrl.fixtures import FixturesConfig
from kstrl.git import fetch_base_branch, resolve_base_ref
from kstrl.guards import ScopeHazard, scope_entry_hazard
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.interaction import InteractionChannel
from kstrl.knowledge import (
    KnowledgeConfig,
    build_knowledge_context,
    current_run_id,
    distill_facts,
    measure_fact_utilization,
)
from kstrl.linear import LinearConfig, build_linear_sink
from kstrl.loop import LoopBudget
from kstrl.manifest import (
    COMPONENT_STATUS_VALUES,
    Component,
    ComponentStatus,
    Manifest,
)
from kstrl.observability import (
    NotifyConfig,
    NotifyHooks,
    NullProgressLog,
    ProgressLog,
)
from kstrl.pipeline import ComponentPipeline, PipelineHooks, _iso_now
from kstrl.policy import PolicyConfig
from kstrl.pr import create_prs_in_order, create_single_pr
from kstrl.review import (
    ReviewMode,
    run_review,
)
from kstrl.sandbox import SandboxConfig
from kstrl.scope import ComponentScope, RunScope
from kstrl.security import (
    SecurityConfig,
    SecurityMode,
    run_security_review,
)
from kstrl.shutdown import StopController
from kstrl.statedir import ControlStateError
from kstrl.timeout import TimeoutConfig
from kstrl.ui.bridge import EventBridgeUI
from kstrl.verify import (
    SCOPE_UNREADABLE_CHECK,
    VerifyConfig,
    resolve_verify_commands,
    run_mechanical_verification,
    scope_unreadable_error,
    scrub_project_claude_md,
)

if TYPE_CHECKING:
    from kstrl.agents.liveness import ProbeResult
    from kstrl.ui.base import UI


class BudgetConfigError(ValueError):
    """A budget ceiling was configured with a value that cannot bound
    anything.

    Raised rather than coerced because these are SAFETY limits and every
    bad value fails in a different silent direction: ``nan`` makes
    ``max_cost_usd > 0`` false, so the ceiling disables itself while
    reading as configured; a negative value disables it the same way;
    ``inf`` produces a ceiling that is enabled and can never be reached.
    All three are indistinguishable from "off" at the moment they
    matter, which is the failure mode a budget cap must never have.
    """


def validate_cost_ceiling(value: float, source: str) -> float:
    """A cost ceiling must be finite and non-negative. 0 means unbounded.

    Public because the CLI has to reject a bad ``--max-cost-usd`` in
    preflight, before the architect spends a call - the flag reaches
    ``run_factory`` without passing any config loader.
    """
    import math

    if not math.isfinite(value):
        raise BudgetConfigError(
            f"{source} must be a finite number, got {value!r}; use 0 to "
            "disable the ceiling. A non-finite ceiling silently stops "
            "bounding anything."
        )
    if value < 0:
        raise BudgetConfigError(
            f"{source} must be >= 0, got {value!r}; use 0 to disable the "
            "ceiling rather than a negative value, which disables it "
            "without saying so."
        )
    return value


def validate_token_ceiling(value: int, source: str) -> int:
    """A token ceiling must be non-negative. 0 means unbounded.

    The same defect as :func:`validate_cost_ceiling`, in the knob that
    predates it: ``max_total_tokens = -5`` made ``max_total_tokens > 0``
    false, so the ceiling disabled itself while still reading as
    configured - measured, not assumed. Only the finiteness check is
    absent, because this one is an int.
    """
    if value < 0:
        raise BudgetConfigError(
            f"{source} must be >= 0, got {value!r}; use 0 to disable the "
            "ceiling rather than a negative value, which disables it "
            "without saying so."
        )
    return value


#: R10.3: the two settings [factory] setpoint_agreement accepts.
VALID_SETPOINT_AGREEMENT = ("advisory", "block")


def _validate_setpoint_agreement(value: str, source: str) -> str:
    """Reject an unrecognised set-point mode at load time.

    ``review_mode`` next door is validated only when the pipeline builds
    a ``ReviewMode`` from it, deep inside Phase 2, which turns a typo in
    kstrl.toml into a crash several minutes and one agent run later.
    This one fails where the value is read, in the shape
    ``AdequacyConfig.__post_init__`` uses. Silently treating an
    unrecognised value as "advisory" would be worse than either: the
    operator would believe a gate was blocking when it was not.
    """
    if value not in VALID_SETPOINT_AGREEMENT:
        raise ValueError(
            f"invalid {source} {value!r}; expected "
            + " or ".join(repr(v) for v in VALID_SETPOINT_AGREEMENT)
        )
    return value


@dataclass
class FactoryConfig:
    """Configuration for factory orchestration."""

    max_parallel: int = 4
    max_retries: int = 3
    retry_delay: float = 5.0
    use_worktrees: bool = True
    single_pr: bool = False
    create_prs: bool = True
    verify_command: str | None = None
    # Phase 1: mechanical verification
    verify_config: VerifyConfig | None = None
    # R2.3 (CRIT-8): explicit skip sentinel for Phase 1. verify_config=None
    # keeps its historical meaning of "use the default checks"; only this
    # flag (set by --no-verify) genuinely skips mechanical verification.
    # The skip is stated in the run output and recorded as a phase_skipped
    # finding so "ran clean" and "never ran" stay distinguishable.
    skip_verification: bool = False
    # Phase 2: reviewer agent
    review_mode: str = ReviewMode.HARD.value
    # R10.3 set-point agreement: what to do when the engineer marked a
    # story passes=true and the reviewer did not independently confirm
    # it. "advisory" records a finding and lets the component proceed;
    # "block" also reverts the flag in the PRD and retries the story.
    # Ships advisory so the gate's first output is a measurement rather
    # than a wall. The autonomy ladder can force blocking on from L1
    # upward (see review.setpoint_blocks); it can never turn it off.
    setpoint_agreement: str = "advisory"
    review_agent_cmd: str | None = None
    review_agent_type: str | None = None
    review_model: str | None = None
    # Phase 2.5: security review (separate LLM call after Phase 2 review)
    security_config: SecurityConfig | None = None
    # Phase 3: contract testing
    contract_config: ContractConfig | None = None
    # Phase 0: feedforward
    feedforward_config: FeedforwardConfig | None = None
    # Observability. R3.2: the progress log defaults ON so a walk-away
    # run always leaves a consumable event trail; progress_log_enabled
    # = false (toml/env) turns it off. progress_log_path=None means the
    # default <root>/.kstrl/progress.jsonl.
    progress_log_path: Path | None = None
    progress_log_enabled: bool = True
    # R3.2: [notify] hooks (on_complete / on_first_failure shell
    # commands). None means run_factory loads NotifyConfig.load(root_dir).
    notify_config: NotifyConfig | None = None
    # R7.4: [linear] integration. None means run_factory loads
    # LinearConfig.load(root_dir). Observability only: the sink attaches
    # to the progress log and its failures never affect the run.
    linear_config: LinearConfig | None = None
    # E4: per-run hard cap on adversarial LLM calls (review + security
    # + knowledge distill). 0 means unbounded. Once exceeded the
    # remaining components skip those phases with an informational log
    # line; mechanical verify + the implementing agent continue. This
    # protects against runaway-cost factory runs.
    max_adversarial_calls: int = 0
    # E6: when True, pause and prompt the user before each component's
    # PR creation step. Off by default; opt-in for sensitive projects.
    pause_before_pr_merge: bool = False
    # R3.1: run-level token budget. 0 means unbounded. Compared against
    # the run's aggregated total_tokens (a lower bound when some calls
    # report no usage); on breach the factory halts LOUDLY - the current
    # component fails with a synthetic budget finding and pending
    # components fail at scheduling instead of burning more spend.
    # Enforcement granularity is the phase boundary plus, since R8, the
    # gap between engineer iterations: an in-flight engineer loop or
    # review call can still overshoot before the parent sees its usage.
    #
    # CAVEAT, measured: total_tokens counts CACHE READS at par with
    # input tokens. A real run reached 1,864,081 total tokens of which
    # 1,781,669 (95.6%) were cache reads, for $1.22 of actual spend, so
    # a 500k "budget" halted at $1.22. This ceiling measures something
    # real but nearly uncorrelated with money; max_cost_usd below is the
    # one an operator usually means.
    max_total_tokens: int = 0
    # R8: run-level COST budget in USD. 0.0 means unbounded, matching the
    # max_total_tokens convention. Both ceilings may be set; whichever is
    # reached first halts. Compared against the run's aggregated
    # cost_usd, which - exactly like the token total - is a CLI
    # self-report and a LOWER BOUND whenever some calls report no cost.
    #
    # NOT a hard cap, and it must never be described as one: it has the
    # same between-iterations enforcement granularity as the token
    # ceiling. Measured: one engineer call of 376s overshot the entire
    # 500k token cap by 3.7x, and nothing in this design would have
    # interrupted it. Distinct from [agent] budget_usd, which is
    # adapter-internal (claude-sdk only) and bounds a single turn.
    max_cost_usd: float = 0.0
    # R0.1: timeout limits (agent iteration, component wall clock,
    # scheduler backstop margin). None means run_factory loads
    # TimeoutConfig.load(root_dir) - toml [timeout] section + env.
    timeout_config: TimeoutConfig | None = None
    # R0.2: how long push_create_and_merge_pr waits for merge
    # confirmation before the component is parked as MERGE_PENDING.
    merge_timeout: float = 300.0
    # R0.5: proceed even when another invocation holds the run-level
    # .kstrl/factory.lock. Deliberately CLI-only (no toml/env source):
    # forcing past the lock can corrupt a live run's worktrees and
    # manifest, so it must be an explicit per-invocation decision.
    force_lock: bool = False
    # R3.3: keep a FAILED component's worktree at end-of-run cleanup so
    # the operator can post-mortem it (the failure summary points at
    # it). Kept worktrees are recorded as evidence pointers in the
    # manifest and survive the next run's stale-worktree prune for as
    # long as the component stays FAILED.
    keep_worktrees_on_failure: bool = False
    # R7.2: approved-fixtures oracle for Phase 1. None means run_factory
    # loads FixturesConfig.load(root_dir) - toml [fixtures] section +
    # env - so `ks factory` honors the config with no CLI wiring.
    # Default-off ([fixtures].enabled = false, roadmap user decision 4):
    # fixtures execute PRD-defined commands, so the operator opts in.
    fixtures_config: FixturesConfig | None = None
    # R8.1: declarative merge-policy envelope for Phase 1. None means
    # run_factory loads PolicyConfig.load(root_dir) - toml [policy] section
    # + env. Opt-in ([policy].enabled = false): existing runs unchanged.
    policy_config: PolicyConfig | None = None

    def resolved_verify_config(self) -> VerifyConfig:
        """The VerifyConfig Phase 1 runs with (#261).

        ``verify_config=None`` has always meant "use the defaults" here
        (``skip_verification`` is the separate, explicit skip sentinel),
        so the fallback is a bare ``VerifyConfig()`` and NOT a reload
        from disk. ``pipeline._phase_verify`` calls this, and so does
        ``engineer_verify_config`` below, which is what stops the gate
        and the engineer prompt answering the question two ways.
        """
        return self.verify_config or VerifyConfig()

    def engineer_verify_config(self) -> VerifyConfig | None:
        """What the engineer may be told Phase 1 will run, or None.

        None when ``--no-verify`` disabled Phase 1: no gate runs, so
        naming commands would claim a check that never happens.

        A method rather than a free function taking the two fields: the
        coupling between them is the point, and unpacking them at each
        call site made ``(cfg.verify_config, False)`` a legal miscall.
        """
        return None if self.skip_verification else self.resolved_verify_config()

    def __post_init__(self) -> None:
        # R10.3: catch a bad set-point mode wherever the config is
        # built, not only in load(). A FactoryConfig assembled from CLI
        # flags or in a test goes through here too.
        _validate_setpoint_agreement(
            self.setpoint_agreement,
            "[factory] setpoint_agreement",
        )

    @classmethod
    def from_env(cls) -> FactoryConfig:
        """Load factory config from environment variables."""
        from kstrl.config import _parse_bool

        return cls(
            max_parallel=int(os.environ.get("FACTORY_MAX_PARALLEL", "4")),
            max_retries=int(os.environ.get("FACTORY_MAX_RETRIES", "3")),
            retry_delay=float(os.environ.get("FACTORY_RETRY_DELAY", "5.0")),
            merge_timeout=float(os.environ.get("FACTORY_MERGE_TIMEOUT", "300.0")),
            max_adversarial_calls=int(os.environ.get("KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS", "0")),
            max_total_tokens=validate_token_ceiling(
                int(os.environ.get("KSTRL_FACTORY_MAX_TOTAL_TOKENS", "0")),
                "KSTRL_FACTORY_MAX_TOTAL_TOKENS",
            ),
            max_cost_usd=validate_cost_ceiling(
                float(os.environ.get("KSTRL_FACTORY_MAX_COST_USD", "0")),
                "KSTRL_FACTORY_MAX_COST_USD",
            ),
            pause_before_pr_merge=_parse_bool(
                os.environ.get("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE")
            ),
            progress_log_enabled=_parse_bool(
                os.environ.get("KSTRL_FACTORY_PROGRESS_LOG_ENABLED", "1")
            ),
            keep_worktrees_on_failure=_parse_bool(
                os.environ.get("KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE")
            ),
            # R10.3: unlike review_mode next door, this key HAS an env
            # var, so from_env must read it. `ks factory` uses from_env
            # as the environment-only baseline that _collect_toml_notes
            # diffs against, and a field missing here is reported to the
            # operator as "from kstrl.toml" when it came from the
            # environment - a false provenance claim in the one place
            # they look to find out where a setting came from.
            setpoint_agreement=_validate_setpoint_agreement(
                os.environ.get("KSTRL_FACTORY_SETPOINT_AGREEMENT", "advisory"),
                "KSTRL_FACTORY_SETPOINT_AGREEMENT",
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> FactoryConfig:
        """Load factory config with precedence: env > toml > defaults.

        Reads the ``[factory]`` section from ``<root_dir>/kstrl.toml`` if
        present, then overlays any matching env vars on top.
        """
        from kstrl.config import _parse_bool, load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "factory")
        if "max_parallel" in section:
            config.max_parallel = int(section["max_parallel"])
        if "max_retries" in section:
            config.max_retries = int(section["max_retries"])
        if "retry_delay" in section:
            config.retry_delay = float(section["retry_delay"])
        if "use_worktrees" in section:
            config.use_worktrees = bool(section["use_worktrees"])
        if "single_pr" in section:
            config.single_pr = bool(section["single_pr"])
        if "create_prs" in section:
            config.create_prs = bool(section["create_prs"])
        if "review_mode" in section:
            config.review_mode = str(section["review_mode"])
        if "setpoint_agreement" in section:
            config.setpoint_agreement = _validate_setpoint_agreement(
                str(section["setpoint_agreement"]),
                "[factory] setpoint_agreement",
            )
        if "merge_timeout" in section:
            config.merge_timeout = float(section["merge_timeout"])
        # R2.2: the two safety knobs are reachable via toml (here), env
        # (below) and CLI flags (cli.py factory command).
        if "max_adversarial_calls" in section:
            config.max_adversarial_calls = int(section["max_adversarial_calls"])
        if "max_total_tokens" in section:
            config.max_total_tokens = validate_token_ceiling(
                int(section["max_total_tokens"]),
                "[factory] max_total_tokens",
            )
        if "max_cost_usd" in section:
            config.max_cost_usd = validate_cost_ceiling(
                float(section["max_cost_usd"]),
                "[factory] max_cost_usd",
            )
        if "pause_before_pr_merge" in section:
            config.pause_before_pr_merge = bool(section["pause_before_pr_merge"])
        if "progress_log_enabled" in section:
            config.progress_log_enabled = bool(section["progress_log_enabled"])
        if "keep_worktrees_on_failure" in section:
            config.keep_worktrees_on_failure = bool(section["keep_worktrees_on_failure"])
        # Env overrides (consistent with from_env)
        if "FACTORY_MAX_PARALLEL" in os.environ:
            config.max_parallel = int(os.environ["FACTORY_MAX_PARALLEL"])
        if "FACTORY_MAX_RETRIES" in os.environ:
            config.max_retries = int(os.environ["FACTORY_MAX_RETRIES"])
        if "FACTORY_RETRY_DELAY" in os.environ:
            config.retry_delay = float(os.environ["FACTORY_RETRY_DELAY"])
        if "FACTORY_MERGE_TIMEOUT" in os.environ:
            config.merge_timeout = float(os.environ["FACTORY_MERGE_TIMEOUT"])
        if "KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS" in os.environ:
            config.max_adversarial_calls = int(os.environ["KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS"])
        if "KSTRL_FACTORY_MAX_TOTAL_TOKENS" in os.environ:
            config.max_total_tokens = validate_token_ceiling(
                int(os.environ["KSTRL_FACTORY_MAX_TOTAL_TOKENS"]), "KSTRL_FACTORY_MAX_TOTAL_TOKENS"
            )
        if "KSTRL_FACTORY_MAX_COST_USD" in os.environ:
            config.max_cost_usd = validate_cost_ceiling(
                float(os.environ["KSTRL_FACTORY_MAX_COST_USD"]), "KSTRL_FACTORY_MAX_COST_USD"
            )
        if "KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE" in os.environ:
            config.pause_before_pr_merge = _parse_bool(
                os.environ["KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE"]
            )
        if "KSTRL_FACTORY_PROGRESS_LOG_ENABLED" in os.environ:
            config.progress_log_enabled = _parse_bool(
                os.environ["KSTRL_FACTORY_PROGRESS_LOG_ENABLED"]
            )
        if "KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE" in os.environ:
            config.keep_worktrees_on_failure = _parse_bool(
                os.environ["KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE"]
            )
        if "KSTRL_FACTORY_SETPOINT_AGREEMENT" in os.environ:
            config.setpoint_agreement = _validate_setpoint_agreement(
                os.environ["KSTRL_FACTORY_SETPOINT_AGREEMENT"],
                "KSTRL_FACTORY_SETPOINT_AGREEMENT",
            )
        return config


def merge_gate_unreachable_warning(config: FactoryConfig) -> str | None:
    """Warning when the merge gate is on but can never run, else None.

    The pipeline reaches ``_phase_checkpoint`` only when ``create_prs`` is
    on AND ``single_pr`` is off, so a config whose FINAL resolved values
    have the gate on while the checkpoint is unreachable is a governance
    control failing open (#207). The flag can arrive in that state three
    ways: set explicitly (kstrl.toml / env) under `ks run`, which forces
    ``create_prs = False``; flipped on by the L1/L2 autonomy bundle AFTER
    any command-level notice already ran; or combined with ``single_pr``
    mode, whose aggregate PR is created without a checkpoint. Called from
    ``run_factory`` after autonomy resolution - the one point that sees
    the final values on every path - so all three cases warn.
    """
    if not config.pause_before_pr_merge:
        return None
    if not config.create_prs:
        return (
            "pause_before_pr_merge is on but create_prs is off: no PR is "
            "created, so the merge gate can never run. The gate is "
            "honoured only by PR-creating invocations (`ks factory` / "
            "`ks serve`), not `ks run`."
        )
    if config.single_pr:
        return (
            "pause_before_pr_merge is on but single_pr mode creates one "
            "aggregate PR without a per-component checkpoint, so the "
            "merge gate never runs. Disable single_pr to honour the "
            "merge gate."
        )
    return None


def review_enabled(config: FactoryConfig) -> bool:
    """Will Phase 2 run a reviewer at all?

    One predicate rather than a repeated ``!= ReviewMode.SKIP.value``,
    because both readers carry the same caveat about WHEN they may ask
    (the autonomy ladder can force review back on) and two copies of a
    caveat drift.
    """
    return config.review_mode != ReviewMode.SKIP.value


def security_enabled(config: FactoryConfig) -> bool:
    """Will Phase 2.5 run a security reviewer at all?"""
    return (
        config.security_config is not None
        and config.security_config.mode != SecurityMode.SKIP.value
    )


def setpoint_gate_unreachable_warning(config: FactoryConfig) -> str | None:
    """Warning when the set-point gate is on but can never run, else None.

    ``setpoint_agreement = "block"`` asks the harness to fail a component
    whose story the reviewer did not confirm, and `review_mode = "skip"`
    means no reviewer runs, so there is never a verdict to confirm with.
    `_phase_review` returns before the set-point check on that path, so
    the gate is not merely lenient, it is absent. Same shape and same
    reason as ``merge_gate_unreachable_warning``: a governance control
    silently failing open is worse than one that was never configured,
    because the operator believes it is on.

    Called from ``run_factory`` after autonomy resolution, because the
    L1/L2 bundle forces ``review_mode`` to hard and can therefore make a
    config that looked unreachable reachable.
    """
    if config.setpoint_agreement != "block":
        return None
    if not review_enabled(config):
        return (
            "setpoint_agreement is 'block' but review_mode is 'skip': no "
            "reviewer runs, so no story can be confirmed and the "
            "set-point gate never fires. Set review_mode to 'advisory' "
            "or 'hard' to honour it."
        )
    return None


# R7.1: cross-model review rotation. Self-preference bias means a
# same-family reviewer systematically misses the bug classes its own
# family produces, so when no explicit reviewer config is given the
# review and security phases default to the OPPOSITE model family from
# the engineer (user decision 2: the OpenAI family via the codex CLI
# reviews Claude-engineered code; a codex engineer flips the default to
# claude-code). The engineer always keeps the primary family.
_CROSS_FAMILY_TYPE: dict[str, str] = {
    "claude-code": "codex",
    "codex": "claude-code",
}


def _cli_family(
    agent_cmd: str | None,
    agent_type: str | None,
    claude_available: bool,
) -> str | None:
    """Which MODEL family a (cmd, type) config resolves to, mirroring
    ``agents.get_agent`` dispatch exactly: a custom command is an
    unknown family (None); "claude-sdk" is the Claude family through
    the SDK transport (R7.6 - without this branch it would fall through
    to codex and INVERT the R7.1 rotation for SDK engineers);
    "auto"/None auto-detects claude-code first.

    Canonicalizes through the SAME table ``get_agent`` uses, and that is
    load-bearing rather than tidiness. This function decides which family
    the ENGINEER belongs to, and the R7.1 rotation then picks a reviewer
    from the other family. If the two disagree - as they did when
    ``get_agent`` learned the "claude" alias and this mirror did not -
    a Claude engineer is recorded as codex, the rotation "cross-family"
    reviewer resolves to claude-code, and the run gets SAME-family review
    while reporting the opposite. Correlated blind spots are exactly what
    R7.1 exists to avoid, so a silent mismatch here is worse than a
    crash: the safety property fails while the audit trail says it held.

    An unrecognized type raises, matching ``get_agent`` - a config that
    cannot construct an agent must not first be assigned a family.
    """
    # Imported here, matching this module's lazy-agent-import pattern.
    from kstrl.agents import (
        VALID_AGENT_TYPES,
        UnknownAgentTypeError,
        canonical_agent_type,
    )

    if agent_cmd:
        return None
    if agent_type is None:
        # Unset means auto-detect, NOT unknown. canonical_agent_type
        # returns None for both, so they must be split before the
        # unknown-type check or the ordinary "no [agent] type
        # configured" case would raise.
        return "claude-code" if claude_available else "codex"
    canonical = canonical_agent_type(agent_type)
    if canonical is None:
        raise UnknownAgentTypeError(
            f"unknown agent type {agent_type!r}; expected one of {', '.join(VALID_AGENT_TYPES)}"
        )
    if canonical in ("claude-code", "claude-sdk"):
        return "claude-code"
    if canonical == "auto":
        return "claude-code" if claude_available else "codex"
    if canonical == "custom":
        return None
    return "codex"


def _agent_identity(
    agent_cmd: str | None,
    agent_type: str | None,
    model: str | None,
    claude_available: bool,
) -> str:
    """Reviewing-model identity for a configuration, matching the agent
    adapters' ``name`` property ("codex (gpt-5)", "claude-code",
    "claude-sdk (haiku)", "custom (<cmd>)") so findings attributed
    before an agent exists match what a live run stamps on its
    results. "claude-sdk" keeps its own identity label (the adapter
    name is the transport, distinct from its claude-code FAMILY used
    for rotation)."""
    if agent_cmd:
        return f"custom ({agent_cmd})"
    if agent_type == "claude-sdk":
        label = "claude-sdk"
    else:
        label = _cli_family(agent_cmd, agent_type, claude_available) or "unknown"
    if model:
        return f"{label} ({model})"
    return label


@dataclass(frozen=True)
class AdversarialAgentSelection:
    """Resolved agent configuration for one adversarial phase (R7.1).

    ``source`` records how the resolution went: "explicit" (operator
    config always wins), "cross-family-default" (the R7.1 rotation
    default), or "same-family-fallback" (heterogeneity unavailable;
    ``warning`` then carries the homogeneity risk statement to print).
    """

    phase: str
    agent_cmd: str | None
    agent_type: str | None
    model: str | None
    reasoning: str | None
    source: str
    identity: str
    warning: str | None = None


@dataclass(frozen=True)
class _AdversarialGates:
    """What this run's config permits the adversarial phases to do."""

    review: bool
    security: bool
    may_dispatch: bool


def _ladder_can_force_review() -> bool:
    """Could any autonomy level turn a skip-mode run back into a
    reviewing one? Computed rather than asserted in prose, so it cannot
    quietly stop being true when a bundle changes."""
    return any(
        flag_bundle_for(level).review_mode != ReviewMode.SKIP.value for level in AutonomyLevel
    )


def _adversarial_phase_gates(
    factory_config: FactoryConfig,
    autonomy_config: AutonomyConfig,
) -> _AdversarialGates:
    """Which adversarial phases are on, and may any of them dispatch.

    ``may_dispatch`` is a spend decision (#262 review): resolving the
    reviewer is free, but the cross-family LIVENESS PROBE costs a real
    CLI turn, so it must not fire for a run that will never dispatch an
    adversarial call.

    ``review_mode = "skip"`` is only proof of that when the autonomy
    ladder cannot put review back, and the ladder resolves further down
    ``_run_factory_locked``, after the pipeline is already holding this
    selection.
    """
    review = review_enabled(factory_config)
    security = security_enabled(factory_config)
    may_dispatch = review or security or (autonomy_config.enabled and _ladder_can_force_review())
    return _AdversarialGates(review=review, security=security, may_dispatch=may_dispatch)


def _resolve_cross_family(
    cross_type: str | None,
    *,
    claude_available: bool,
    codex_available: bool,
    may_dispatch_adversarial: bool,
) -> ProbeResult | None:
    """The opposite family's liveness, or None when there is none to ask.

    Liveness is deliberately NOT folded into
    ``claude_available``/``codex_available``: those two also decide which
    family the ENGINEER belongs to, and a claude CLI that is installed
    but dead must still be recorded as a claude engineer, because
    ``get_agent`` will construct it from PATH regardless. Mixing
    liveness in there would invert the rotation while the audit trail
    claimed it held - the exact failure ``_cli_family`` warns about.

    The probe sits behind the installed check, so a machine with one CLI
    pays nothing and only the family this run would actually dispatch to
    is ever probed. ``probe_family`` caches per process, so the review
    and security resolutions share one probe, and it is inert when
    ``KSTRL_AGENT_PROBE=0``.

    ``may_dispatch_adversarial=False`` resolves on PATH alone, exactly
    as this did before #262: the answer cannot change a run that will
    never call a reviewer, so buying it with a CLI turn would be spend
    for nothing.
    """
    if cross_type is None:
        return None
    installed = codex_available if cross_type == "codex" else claude_available
    if not installed:
        return None
    from kstrl.agents.liveness import UNPROBED, probe_family

    return probe_family(cross_type) if may_dispatch_adversarial else UNPROBED


def _homogeneity_warning(
    phase: str,
    engineer_family: str | None,
    cross_type: str | None,
    cross: ProbeResult | None,
) -> str:
    """The self-preference risk statement for a same-family fallback.

    Three causes, three remedies: an unknowable engineer family, a
    cross CLI that is not installed, and (#262) one that is installed
    but cannot run. The last quotes the CLI's own refusal, because
    "install codex" is useless advice to someone who already has it.
    """
    tail = (
        "Self-preference bias means a same-family reviewer systematically "
        "misses the bug classes its own family produces. "
    )
    if engineer_family is None:
        return (
            "Homogeneity risk (R7.1): the engineer runs a custom agent "
            "command, so its model family is unknown and the "
            f"cross-family default cannot be applied; the {phase} "
            f"reviewer falls back to the same configuration. {tail}"
            f"Set an explicit {phase} agent config on a different model "
            "family to restore cross-family review."
        )
    same_family = (
        f"so the {phase} reviewer runs on the same model family as the "
        f"engineer ({engineer_family}). {tail}"
    )
    if cross is not None:
        because = f" ({cross.detail})" if cross.detail else ""
        return (
            f"Homogeneity risk (R7.1): the {cross_type} CLI is installed "
            f"but cannot run a turn{because}, {same_family}"
            f"Fix the {cross_type} CLI (authentication, quota, config) for "
            f"cross-family review, or set an explicit {phase} agent config "
            "to accept the risk silently."
        )
    return (
        f"Homogeneity risk (R7.1): the {cross_type} CLI is not available, "
        f"{same_family}"
        f"Install the {cross_type} CLI for cross-family review, or set an "
        f"explicit {phase} agent config to accept the risk silently."
    )


def resolve_adversarial_selection(
    phase: str,
    *,
    explicit_cmd: str | None,
    explicit_type: str | None,
    explicit_model: str | None,
    fallback_cmd: str | None,
    fallback_type: str | None,
    fallback_model: str | None,
    fallback_reasoning: str | None,
    engineer_cmd: str | None,
    engineer_type: str | None,
    claude_available: bool | None = None,
    codex_available: bool | None = None,
    may_dispatch_adversarial: bool = True,
) -> AdversarialAgentSelection:
    """Resolve which agent reviews this run's diffs (R7.1).

    Precedence:
    1. Any explicit field (cmd/type/model) makes the whole selection
       explicit: each unset field falls back to the phase's historical
       fallback, exactly as before R7.1. No warning - an operator who
       pins a same-family reviewer has decided so deliberately.
    2. Otherwise, when the engineer's family is known and the opposite
       family's CLI is both installed and able to complete a turn, the
       reviewer defaults to that family (adapter-default model;
       reasoning deliberately not inherited - effort strings do not
       transfer across families).
    3. Otherwise the reviewer falls back to the same configuration as
       today (same family as the engineer) and ``warning`` names the
       self-preference risk.

    ``claude_available``/``codex_available`` default to asking PATH;
    tests inject both.

    Step 2 also LIVENESS-PROBES the cross family (#262), at most once
    per process and at most twice per family - an installed CLI that
    cannot authenticate or has exhausted its quota used to be selected
    here, and the run then paid the whole engineer bill before finding
    out. A dead cross CLI takes the SAME path a missing one always took:
    downgrade to same-family review with the homogeneity warning, never
    a hard failure. ``KSTRL_AGENT_PROBE=0`` switches the probe off
    globally; ``may_dispatch_adversarial=False`` switches it off for one
    resolution whose answer cannot change the run.
    """
    from kstrl.agents import ClaudeCodeAgent, CodexAgent

    if claude_available is None:
        claude_available = ClaudeCodeAgent.is_available()
    if codex_available is None:
        codex_available = CodexAgent.is_available()

    explicit = any(v is not None for v in (explicit_cmd, explicit_type, explicit_model))
    if explicit:
        cmd = explicit_cmd if explicit_cmd is not None else fallback_cmd
        agent_type = explicit_type if explicit_type is not None else fallback_type
        model = explicit_model if explicit_model is not None else fallback_model
        return AdversarialAgentSelection(
            phase=phase,
            agent_cmd=cmd,
            agent_type=agent_type,
            model=model,
            reasoning=fallback_reasoning,
            source="explicit",
            identity=_agent_identity(cmd, agent_type, model, claude_available),
        )

    engineer_family = _cli_family(engineer_cmd, engineer_type, claude_available)
    cross_type = _CROSS_FAMILY_TYPE.get(engineer_family) if engineer_family else None
    cross = _resolve_cross_family(
        cross_type,
        claude_available=claude_available,
        codex_available=codex_available,
        may_dispatch_adversarial=may_dispatch_adversarial,
    )
    if cross is not None and cross.live:
        return AdversarialAgentSelection(
            phase=phase,
            agent_cmd=None,
            agent_type=cross_type,
            model=None,
            reasoning=None,
            source="cross-family-default",
            identity=_agent_identity(None, cross_type, None, claude_available),
        )

    warning = _homogeneity_warning(phase, engineer_family, cross_type, cross)
    return AdversarialAgentSelection(
        phase=phase,
        agent_cmd=fallback_cmd,
        agent_type=fallback_type,
        model=fallback_model,
        reasoning=fallback_reasoning,
        source="same-family-fallback",
        identity=_agent_identity(
            fallback_cmd,
            fallback_type,
            fallback_model,
            claude_available,
        ),
        warning=warning,
    )


@dataclass
class ComponentResult:
    """Result from running a single component."""

    component_id: str
    success: bool
    iterations: int = 0
    error: str | None = None
    duration_seconds: float = 0.0
    context_json: str | None = None
    # R3.1: engineer-loop usage aggregated by the worker; pickled back
    # across the ProcessPoolExecutor boundary. None means the worker
    # predates the meter or crashed before the loop started.
    usage: UsageTotals | None = None
    # R7.5: the no-progress circuit breaker halted the loop. Routed by
    # the pipeline to a direct FAILED transition (retrying the same
    # prompt against the same state is the exact spend the breaker
    # exists to stop) with a distinct journal event.
    no_progress: bool = False
    # R8: the engineer loop halted ITSELF on a run-level ceiling
    # (max_total_tokens or max_cost_usd, between iterations). Routed to
    # pipeline.fail_for_budget so the audit trail is identical to a
    # breach caught at a phase boundary; ``error`` carries WHICH ceiling
    # and which condition fired. The typed pair below carries the same
    # facts structurally (R8 review #180) so the pipeline never re-derives
    # the identity from its own totals.
    budget_exceeded: bool = False
    budget_halt_condition: str = ""
    budget_halt_ceilings: tuple[str, ...] = ()
    # Files the in-loop scope guard rejected. Carried so the pipeline can
    # file the halt under the retry context's verification-failures
    # section, where R0.4 established the retry agent looks for scope
    # guidance - catching the violation earlier must not relocate it.
    guard_violations: tuple[str, ...] = ()


@dataclass
class FactoryResult:
    """Overall result from the factory run."""

    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # R0.2: components whose PR merge was initiated but not confirmed.
    # Not failed - a factory re-run re-polls them - but their dependents
    # were not scheduled, so the run is incomplete (nonzero exit code).
    merge_pending: list[str] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)
    # R0.3: unresolved contract failures (one human-readable line per
    # failed check). Non-empty forces a nonzero exit code even when no
    # single component could be blamed.
    contract_failures: list[str] = field(default_factory=list)
    # Components this run actually handed to the executor, in launch
    # order (a retried component appears once per attempt). The counters
    # above cannot stand in for this: they are all empty both when the
    # scheduler found nothing to do and when there was nothing left to
    # do, and only the first of those is a failure (#263).
    scheduled: list[str] = field(default_factory=list)
    exit_code: int = 0


def resolve_exit_code(
    factory_result: FactoryResult,
    manifest: Manifest,
    ui: UI,
    *,
    stopped: bool,
) -> int:
    """Map a finished run's state to its process exit code.

    Emits the diagnostic for the nothing-was-scheduled case, which is the
    only branch whose cause the summary counters do not already name.
    """
    if stopped:
        return 130
    if factory_result.failed or factory_result.contract_failures:
        return 1
    if factory_result.merge_pending:
        # Incomplete, not failed: unconfirmed merges blocked their
        # dependents. Nonzero so automation notices; a re-run re-polls.
        return 1
    if factory_result.skipped and not factory_result.completed:
        return 1

    # #263: components the manifest ends the run short of COMPLETED. Read
    # from the manifest rather than the run counters, because the counters
    # are equally empty whether the scheduler found nothing it COULD do or
    # nothing it NEEDED to do, and only the first of those is a failure.
    # Same reasoning the merge_pending list above is rebuilt from the
    # manifest rather than accumulated during the run.
    unfinished = [c.id for c in manifest.components if c.status != ComponentStatus.COMPLETED.value]
    if not factory_result.scheduled and unfinished:
        # The manifest held work and the scheduler launched none of it:
        # an off-enum status, a component left FAILED or SKIPPED by an
        # earlier run, or a future scheduling bug. Reported as a failure
        # so `ks factory && deploy` cannot deploy a run that built
        # nothing. An empty manifest and a fully COMPLETED one both leave
        # `unfinished` empty and stay at 0.
        _report_nothing_scheduled(manifest, unfinished, ui)
        return 1
    return 0


def _report_nothing_scheduled(
    manifest: Manifest,
    unfinished: list[str],
    ui: UI,
) -> None:
    """Say what the run did not do, and name an action that will work.

    The remedy has to be executable for the state it names, so the retry
    targets come from ``Manifest.retryable_component_ids`` - the same
    definition ``reset_for_retry`` enforces - rather than from a second
    reading of the statuses here. A skipped component is never named:
    it becomes runnable by retrying the failure that cascaded onto it,
    and `ks retry <skipped-id>` exits 2.
    """
    ui.err(
        f"No component was scheduled from {len(manifest.components)} in the "
        f"manifest, and {len(unfinished)} did not complete: "
        f"{', '.join(unfinished)}"
    )
    retryable = manifest.retryable_component_ids()
    if retryable:
        advice = (
            f"  `ks retry <component-id>` resets a failed component to "
            f"'{ComponentStatus.PENDING.value}', and with it every dependent "
            f"it cascade-skipped. Failed here: {', '.join(retryable)}."
        )
    else:
        advice = (
            "  Check each component's status against ComponentStatus "
            f"({', '.join(COMPONENT_STATUS_VALUES)}). `ks retry` accepts only "
            f"a component in '{ComponentStatus.FAILED.value}', and none is."
        )
    ui.info(advice)


class FactoryLockHeldError(RuntimeError):
    """Another factory invocation holds the run-level lock on this root."""


@dataclass
class _RunLock:
    """Handle for the run-level factory lock.

    ``held=True`` means we hold an exclusive flock for the whole run and
    may safely prune state left by previous runs. ``held=False`` means we
    are running WITHOUT exclusion (Windows/no-fcntl degrade, or
    ``--force-lock``): stale-state cleanup must be skipped because another
    live invocation may own it.
    """

    fp: IO[str] | None
    held: bool

    def release(self) -> None:
        if self.fp is None:
            return
        try:
            import fcntl

            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self.fp.close()
        self.fp = None


def _acquire_run_lock(root_dir: Path, ui: UI, force: bool) -> _RunLock:
    """Take the run-level flock on ``.kstrl/factory.lock`` (R0.5, H-7).

    Held for the entire run so a second ``ks factory`` / ``ks run``
    on the same root refuses to start instead of destroying the first
    invocation's in-flight worktrees and clobbering its manifest. flock
    releases automatically if the holder dies, so a crashed run never
    wedges the root.

    POSIX only, like the A4 per-component lock: without fcntl we degrade
    to no exclusion with a warning. ``force=True`` proceeds past a held
    lock with a warning instead of raising FactoryLockHeldError.
    """
    from kstrl.statedir import state_dir

    lock_path = state_dir(root_dir) / "factory.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        ui.warn(
            "Run-level factory lock unavailable on this platform (no "
            "fcntl); concurrent invocations on this root are not excluded"
        )
        return _RunLock(fp=None, held=False)

    # "a+" so a refused attempt can read the holder's pid without
    # truncating it.
    fp = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = ""
        try:
            fp.seek(0)
            holder = fp.read(64).strip()
        except (OSError, UnicodeDecodeError):
            # ONE clause: ``holder`` stays "" either way, the refusal
            # message below just omits the pid, and nothing reports the
            # cause - so #320's separate-remedy rule has no message to
            # separate. What matters is that the decode is caught at
            # all: the read is on a TEXT handle, so a lock file holding
            # anything but utf-8 raised a UnicodeDecodeError - a
            # ValueError - straight past this OSError clause and turned
            # a "another run holds the lock" refusal into a traceback.
            # A census of read CALLS cannot see this site; the handle
            # walk in tests/helpers/encodingwalk.py found it, and it is
            # the only one of its shape in the package.
            pass
        fp.close()
        holder_note = f" (pid {holder})" if holder else ""
        if force:
            ui.warn(
                f"--force-lock: proceeding while {lock_path} is held"
                f"{holder_note}; concurrent runs can corrupt each "
                f"other's worktrees and manifest"
            )
            return _RunLock(fp=None, held=False)
        raise FactoryLockHeldError(
            f"Another kstrl invocation{holder_note} holds {lock_path}; "
            f"refusing to start a second factory run on this root. "
            f"Wait for it to finish, or re-run with --force-lock to "
            f"override."
        ) from None

    # Holder pid is diagnostic only (shown in the refusal message of a
    # contending invocation); the flock itself is the exclusion.
    try:
        fp.seek(0)
        fp.truncate()
        fp.write(f"{os.getpid()}\n")
        fp.flush()
    except OSError:
        pass
    return _RunLock(fp=fp, held=True)


def _remove_stale_index_lock(root_dir: Path, component_id: str) -> None:
    """Remove a stale index.lock left behind by a killed git operation.

    A timed-out agent is SIGKILLed and can die mid-git-op inside its
    worktree; git then refuses every subsequent operation there. The lock
    for a worktree lives under the MAIN repo's .git/worktrees/<name>/.
    Only the component's own lock is touched - the main repo's
    .git/index.lock may belong to a live operator process and is left
    alone.
    """
    lock = root_dir / ".git" / "worktrees" / component_id / "index.lock"
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        pass


def _setup_worktree(
    component_id: str,
    branch_name: str,
    base_branch: str,
    root_dir: Path,
    run_id: str,
    fresh_from_base: bool = False,
) -> Path:
    """Create a git worktree for a component.

    Worktrees are keyed ``.kstrl/worktrees/<run_id>/<component_id>``
    (R0.5, H-7): two invocations never share a worktree path, so setup
    can only ever remove a leftover from an earlier attempt of THIS run
    (a retry), never another invocation's in-flight worktree. Run-level
    exclusion itself is the ``.kstrl/factory.lock`` flock in run_factory.

    A per-host fcntl flock on ``.kstrl/worktrees/<component_id>.lock``
    (run-agnostic on purpose) still serializes the git commands here for
    the degraded modes that run without the run-level lock (Windows,
    ``--force-lock``), where two invocations could otherwise race on the
    shared branch and .git metadata.

    ``fresh_from_base=True`` (used for retries after a timeout kill, and
    for merge-conflict re-runs under the R7.5 re-run doctrine)
    additionally deletes the component branch so the worktree is recreated
    from ``base_branch`` instead of silently reusing possibly-dirty state
    from the killed attempt (R0.1).

    POSIX only. On Windows the fcntl import fails; we degrade to the
    pre-lock behavior and document the limitation in the runbook.
    """
    worktree_base = root_dir / ".kstrl" / "worktrees" / run_id
    worktree_base.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_base / component_id
    lock_path = root_dir / ".kstrl" / "worktrees" / f"{component_id}.lock"

    lock_fp = None
    try:
        try:
            import fcntl

            # utf-8 named though nothing is ever written through this
            # handle: it exists for flock on its fileno. Naming it costs
            # nothing at run time and keeps the package free of any text
            # open whose encoding the locale decides (#320).
            lock_fp = open(lock_path, "w", encoding="utf-8")
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # Windows / unusual filesystems where flock isn't available.
            # We've already opened lock_fp on the Windows path? No -
            # ImportError on fcntl skips the open above. Just continue
            # without the lock; documented as a Windows non-support
            # caveat.
            lock_fp = None

        # A killed prior attempt may have left git mid-operation.
        _remove_stale_index_lock(root_dir, component_id)

        # Unconditional: a crashed attempt's directory may be gone (tmp
        # cleaner, operator rm -rf) while its .git/worktrees/<comp>/
        # registration survives, and `git worktree add` refuses over a
        # registered-but-missing entry. remove --force clears the
        # registration in that state too (measured on git 2.47); when
        # nothing is registered it fails harmlessly, like `branch -D`.
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )

        if fresh_from_base:
            # Delete the branch from the killed attempt so the add below
            # recreates it from base rather than reusing its commits.
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=root_dir,
                capture_output=True,
                timeout=30,
            )

        # R0.2: cut from origin/<base> when a remote exists so this
        # component builds on the squash-merged history of its
        # dependencies, not a stale local base ref. The fetch is
        # freshness-only and non-fatal: offline runs fall back to the
        # current tracking ref, local-only repos to the local base.
        fetch_base_branch(base_branch, root_dir, timeout=60.0)
        base_ref = resolve_base_ref(base_branch, root_dir)

        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch_name, base_ref],
            cwd=root_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            # Branch already exists: reuse it WITH its commits. After the
            # run-start preflight (_preflight_component_branches) this can
            # only be a branch created during THIS run - a non-timeout
            # retry resuming its own progress, or single_pr components
            # stacking on the shared branch. Stale branches from previous
            # runs were deleted (fully merged) or refused at preflight,
            # never silently reused here (R0.5).
            result = subprocess.run(
                ["git", "worktree", "add", str(worktree_path), branch_name],
                cwd=root_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )

        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Failed to create worktree for '{component_id}': {error}")

        return worktree_path
    finally:
        if lock_fp is not None:
            try:
                import fcntl

                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            lock_fp.close()


def _cleanup_worktree(component_id: str, root_dir: Path, run_id: str) -> None:
    """Remove a git worktree for a component of the current run."""
    worktree_path = root_dir / ".kstrl" / "worktrees" / run_id / component_id
    if not worktree_path.exists():
        return
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=root_dir,
        capture_output=True,
        timeout=30,
    )


def _evidence_worktrees_to_keep(manifest: Manifest) -> set[str]:
    """Worktree paths the stale-prune pass must preserve (R3.3).

    A worktree kept by ``keep_worktrees_on_failure`` stays referenced as
    the FAILED component's evidence pointer; once the component leaves
    FAILED (retried, or reset) the reference is cleared and the next
    prune removes it. Both the recorded and resolved spellings are
    included so path normalization differences cannot defeat the match.
    """
    keep: set[str] = set()
    for comp in manifest.components:
        if comp.status == ComponentStatus.FAILED.value and comp.evidence_worktree:
            keep.add(comp.evidence_worktree)
            try:
                keep.add(str(Path(comp.evidence_worktree).resolve()))
            except OSError:
                pass
    return keep


def _prune_stale_worktrees(
    root_dir: Path,
    run_id: str,
    ui: UI,
    keep: set[str] | None = None,
) -> None:
    """Remove worktrees left behind by previous (crashed/aborted) runs.

    Only called when the run-level flock is genuinely held: any prior
    holder has exited (flock dies with its process), so everything under
    ``.kstrl/worktrees/`` that is not ours - other runs' ``<run_id>/``
    dirs, and pre-R0.5 flat-layout ``<component_id>/`` worktrees - is
    orphaned and safe to remove. Includes worktrees kept for leaked
    workers (R0.1): their owning run is gone, so by the next invocation
    they are stale state, matching the pre-R0.5 force-remove behavior.

    ``keep`` (R3.3) lists evidence worktrees of still-FAILED components
    (kept via keep_worktrees_on_failure); those are preserved so a
    resume does not destroy the post-mortem state it exists to protect.
    """
    keep = keep or set()

    def _kept(path: Path) -> bool:
        if str(path) in keep:
            return True
        try:
            return str(path.resolve()) in keep
        except OSError:
            return False

    worktree_root = root_dir / ".kstrl" / "worktrees"
    if not worktree_root.exists():
        return
    removed = 0
    kept = 0
    for entry in sorted(worktree_root.iterdir()):
        if entry.name == run_id or not entry.is_dir():
            continue  # our own run dir, or a per-component .lock file
        if (entry / ".git").exists():
            # Pre-R0.5 flat layout: the entry itself is a worktree.
            if _kept(entry):
                kept += 1
                continue
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(entry)],
                cwd=root_dir,
                capture_output=True,
                timeout=30,
            )
            removed += 1
        else:
            # <run_id>/ dir from a previous run: remove each component
            # worktree inside it.
            entry_kept = 0
            for wt in sorted(entry.iterdir()):
                if wt.is_dir():
                    if _kept(wt):
                        entry_kept += 1
                        continue
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(wt)],
                        cwd=root_dir,
                        capture_output=True,
                        timeout=30,
                    )
                    # Whatever git could not remove goes with the dir.
                    shutil.rmtree(wt, ignore_errors=True)
                    removed += 1
            kept += entry_kept
            if entry_kept:
                # Evidence lives inside: keep the run dir itself.
                continue
        # Whatever git could not remove (or non-worktree debris) goes
        # with the dir; `git worktree prune` below drops any metadata
        # orphaned by this.
        shutil.rmtree(entry, ignore_errors=True)
    if removed:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )
        ui.info(f"  Pruned {removed} stale worktree(s) from previous runs")
    if kept:
        ui.info(
            f"  Preserved {kept} evidence worktree(s) of failed "
            f"components (keep_worktrees_on_failure)"
        )


def _preflight_component_branches(
    manifest: Manifest,
    root_dir: Path,
    ui: UI,
) -> list[str]:
    """Refuse to silently reuse component branches from previous runs.

    For every branch a PENDING component would be provisioned on: if it
    already exists and is fully merged into the base branch, delete it
    (setup recreates it from base); if it exists with unmerged commits,
    return an error naming it - the caller refuses the run and the
    operator decides (merge or ``git branch -D``). Previously such
    branches were silently reused with their old commits via the
    worktree-add fallback (R0.5, H-7).

    Note: a squash-merged branch is NOT an ancestor of base (the squash
    rewrites history), so leftovers from squash-merge flows are refused
    rather than auto-deleted. Loud beats lossy.
    """
    errors: list[str] = []
    fetch_base_branch(manifest.base_branch, root_dir, timeout=60.0)
    base_ref = resolve_base_ref(manifest.base_branch, root_dir)
    seen: set[str] = set()
    for comp in manifest.components:
        if comp.status != ComponentStatus.PENDING.value:
            continue
        branch = comp.branch_name
        if branch in seen:
            continue  # single_pr: all components share one branch
        seen.add(branch)
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )
        if exists.returncode != 0:
            continue
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, base_ref],
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )
        if merged.returncode == 0:
            deleted = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=root_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if deleted.returncode == 0:
                ui.info(
                    f"  Deleted stale branch '{branch}' from a previous "
                    f"run (fully merged into {manifest.base_branch})"
                )
            else:
                errors.append(
                    f"stale branch '{branch}' (component '{comp.id}') is "
                    f"fully merged but could not be deleted: "
                    f"{deleted.stderr.strip()}"
                )
        else:
            errors.append(
                f"branch '{branch}' (component '{comp.id}') already exists "
                f"with commits not merged into '{manifest.base_branch}'; "
                f"refusing to silently reuse it. Merge it or delete it "
                f"(git branch -D {branch}) and re-run."
            )
    return errors


_SCOPE_HAZARD_REASONS: dict[ScopeHazard, str] = {
    "absolute": "it is an absolute path, and diff names are repository-relative",
    "traversal": "it traverses outside the repository with '..'",
    "root": "it reduces to the repository root, which matches no path prefix",
    "whitespace": "it has leading or trailing whitespace and scope matching is exact",
}


def _authored_scope_errors(
    comp_id: str,
    allowed: list[str],
    harness: list[str],
) -> list[str]:
    """allowedPaths entries that authorise nothing.

    The backstop for HAND-WRITTEN manifests, which never went through
    ``decompose._validate_allowed_path_entry``. An entry that cannot
    match is worse than a missing one: it reads as authorisation and
    grants none, so every file the operator meant to authorise is
    reported outside scope - the same unwinnable retry loop #264
    describes, from the other end, and just as expensive because it is
    discovered only after a full engineer attempt.

    """
    errors: list[str] = []
    for entry in allowed:
        hazard = scope_entry_hazard(entry)
        if hazard is None:
            continue
        errors.append(
            f"component '{comp_id}': allowedPaths entry '{entry}' can never "
            f"match a changed file because {_SCOPE_HAZARD_REASONS[hazard]}, so "
            "every file it was meant to authorise will be reported outside "
            "scope. Use a repository-relative prefix ('src/') or an exact "
            "repository-relative file path. Allowed paths (complete list): "
            f"{', '.join(allowed)}; plus harness artifacts: {', '.join(harness)}."
        )
    return errors


def _preflight_component_scope(
    manifest: Manifest,
    run_scope: RunScope,
) -> list[str]:
    """Refuse a component whose scope cannot work, before any spend.

    Pure path comparison against the plan-time snapshot: no git, no
    agent, no LLM, and (#269) no second reading of a PRD - the list
    judged here is the identical object both guards will enforce, so a
    scope this accepts cannot be a different scope by the time it is
    used. The measured failure this backstops (#264) cost $14.49 and 41
    minutes across three engineer attempts that each produced the
    identical ``diff_scope`` rejection. One line of output in the first
    second is the right price for that.

    Two checks the issue asked for are deliberately NOT here, both
    because they would assert something that cannot happen:

    - "does ``allowedPaths`` cover the component's prdPath and progress
      log?" is answered structurally.
      ``KstrlConfig.component_harness_files`` carves those files out at
      both guards, so the required set and the carve-out are one list
      and the assertion is a tautology.
    - "is any HARNESS path unmatchable?" (an absolute or escaping
      ``[paths] progress`` / ``codebase_map`` / ``prdPath``) was refused
      here until the #268 review pointed out that
      ``config.reconcile_progress_config`` documents exactly that
      configuration as SUPPORTED and self-consistent: joining an
      absolute path onto a worktree is a no-op, so writer and reader
      both land on that one file in the main checkout. Such a file is
      outside every worktree, so it never appears in a component's
      ``git diff`` and can never BE a scope violation. There was nothing
      to guard, and refusing the run contradicted a documented feature.

    Two things are refused, and both are unwinnable rather than merely
    wrong:

    - An ``unresolved`` snapshot: the pre-run PRD would not read and no
      run-wide flag stood in for it (#293 review). This is the one
      verdict the engineer cannot change. ``check_scope_unreadable``
      fails closed on ``scope.error``, and the snapshot is FIXED for the life
      of the run, so every retry re-runs an identical attempt into an
      identical failure - the measured cost above, in the one branch
      the plan-time snapshot would otherwise have kept. It is reported
      per component, not deduplicated, because each names its own file.
    - An authored ``allowedPaths`` entry that cannot match a changed
      file at all. Each distinct SCOPE is examined once, not each
      component: the snapshot's fallback is the run-wide
      ``--allowed-paths`` flag, so one bad entry there is shared by
      every component and N copies of the same paragraph help nobody.
      Two components with genuinely different authored lists are both
      reported, each naming its own complete list.
    """
    errors: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for comp in manifest.components:
        if comp.status != ComponentStatus.PENDING.value:
            continue
        scope = run_scope.for_component(comp.id)
        if not scope.is_trustworthy:
            errors.append(
                f"component '{comp.id}': {scope.error}. Phase 1 fails "
                "closed on a scope it could not establish, and the "
                "snapshot is fixed for the run, so every attempt would "
                "fail identically. Restore that file in the main "
                "checkout. A run-wide --allowed-paths cannot stand in "
                "for it: a scope that could not be READ is not a scope "
                "that does not exist."
            )
            continue
        if scope.allowed_paths is None or tuple(scope.allowed_paths) in seen:
            continue
        seen.add(tuple(scope.allowed_paths))
        errors.extend(_authored_scope_errors(comp.id, scope.allowed_paths, scope.harness_paths))
    return errors


def _record_run_scope(run_scope: RunScope, bus: EventBus, ui: UI) -> None:
    """Write down what each component may change, and why (#269).

    A scope resolved once has to be RECORDED once, or the only way to
    answer "why was this component allowed to write that?" after the run
    is to re-derive it from files the run has since changed - which is
    the thing the snapshot exists to stop anyone doing. One
    ``component_scope_resolved`` event per component carries the
    authored list, kstrl's carve-out, and which authority supplied them.

    The terminal gets a summary rather than N paragraphs, because the
    per-component detail is already in the journal and an operator
    watching a 12-component run does not need 12 identical lines.

    A component whose scope could not be established is deliberately
    NOT warned about here (#293 review). This function is handed every
    component in the manifest, including ones this run will never
    schedule, so a warning saying "Phase 1 will fail it closed" was
    false for a completed or failed component whose PRD has since been
    archived - alarming an operator about work that is not going to
    happen. The PENDING ones are refused outright by
    ``_preflight_component_scope`` moments later, which is louder than
    a warning and stops the run; the event above keeps the record for
    every component either way.
    """
    resolved = run_scope.by_component
    if not resolved:
        return
    for comp_id, scope in resolved.items():
        bus.emit(
            ComponentScopeResolved(
                component=comp_id,
                scope_source=scope.source,
                origin=scope.origin,
                allowed_paths=tuple(scope.allowed_paths or ()),
                harness_paths=tuple(scope.harness_paths),
                error=scope.error or "",
            )
        )
    counts = Counter(scope.source for scope in resolved.values())
    ui.info(
        f"  Scope resolved for {len(resolved)} component(s): "
        + ", ".join(f"{count} from {source}" for source, count in sorted(counts.items()))
    )


def _report_preflight(ui: UI, headline: str, errors: list[str]) -> bool:
    """Print a refusal and say whether one happened."""
    if not errors:
        return False
    ui.err(f"Refusing to run: {headline}")
    for line in errors:
        ui.err(f"  {line}")
    return True


def _warn_claude_md_divergence(
    root_dir: Path,
    factory_config: FactoryConfig,
    ui: UI,
) -> None:
    """Tell the OPERATOR that CLAUDE.md disagrees with the gate (#261).

    The worker does the same scrub when it builds the engineer prompt,
    but its ``ui`` is an EventBridgeUI writing to that component's
    engineer.jsonl, and in pool mode ``live_line`` is None, so nothing
    reaches the terminal. The whole point of the warning is that a human
    deletes the stale section, so it has to be said once, here, on the
    surface they are actually watching. Never refuses: a stale CLAUDE.md
    is already handled correctly for the agent.
    """
    verify_config = factory_config.engineer_verify_config()
    if verify_config is None:
        return
    scrubbed = scrub_project_claude_md(
        root_dir,
        resolve_verify_commands(verify_config, root_dir),
    )
    if scrubbed is None:
        return
    for divergence in scrubbed.divergences:
        ui.warn(f"  {divergence}")


def _preflight_decision_register(
    manifest: Manifest,
    root_dir: Path,
) -> tuple[list[str], tuple[SpecDecision, ...]]:
    """The architect's decisions for this run, or the reason there are none (#260).

    Read once per run because it is a run-wide artifact the decompose
    wrote, and BOUND to this manifest before anything is scheduled.

    Round 2: the path is fixed, so without the bind a register left by
    another project sits exactly where this run looks. Reproduced: a
    factory run on project-b/b.md with project A's register beside it
    handed the engineer project A's binding instruction, because both
    happened to have a component called comp-a. ``bind_register``
    refuses that, a halted register, and an unreadable one. A MISSING
    register is legal and quiet: it is the normal state for every
    project that predates this, and a write that FAILED now fails the
    decompose outright rather than leaving one behind. A manifest with
    no ``spec_file`` is legal and quiet too: it did not come from a
    decompose, so nothing binds it.

    Round 3: this is a pre-spend refusal like the scope and branch
    checks, so it reports and exits like one. Letting the error out of
    ``run_factory`` gave the operator a traceback and exit 1, where
    every sibling refusal gives a sentence and exit 2.
    """
    try:
        return [], bind_register(
            read_decisions(root_dir),
            manifest.project_name,
            manifest.spec_file,
        )
    except DecisionRegisterError as exc:
        return [str(exc)], ()


def _run_preflights(
    manifest: Manifest,
    run_scope: RunScope,
    root_dir: Path,
    factory_config: FactoryConfig,
    run_id: str,
    ui: UI,
    *,
    lock_held: bool,
) -> tuple[SpecDecision, ...] | None:
    """Every pre-spend refusal, cheapest first, and what survives them.

    Returns the architect decisions this run may use, or ``None`` to
    refuse. It returns a value rather than a bool because the register
    check is one of the refusals and its result is the thing the
    engineers need; the empty tuple is a normal, proceeding answer, so
    the caller checks ``is None``.

    The register goes first (#260 round 3): it is one small file read,
    no git and no agent, so it is cheaper than the scope comparison and
    far cheaper than the branch walk.

    Scope goes next (#264): it is a pure path comparison with no git
    and no agent behind it, and a component that cannot pass diff_scope
    must not reach the engineer - the measured cost of finding out later
    was $14.49 across three identical attempts. It also runs without
    worktrees, unlike the R0.5 branch policy below, which only applies
    to worktree mode: without worktrees the factory neither creates
    branches nor worktree dirs.
    """
    _warn_claude_md_divergence(root_dir, factory_config, ui)
    register_errors, run_decisions = _preflight_decision_register(manifest, root_dir)
    if _report_preflight(ui, "the architect decision register cannot bind", register_errors):
        return None
    if _report_preflight(
        ui,
        "components cannot pass the scope check",
        _preflight_component_scope(manifest, run_scope),
    ):
        return None
    if not factory_config.use_worktrees:
        return run_decisions
    if lock_held:
        _prune_stale_worktrees(
            root_dir,
            run_id,
            ui,
            keep=_evidence_worktrees_to_keep(manifest),
        )
    else:
        ui.warn(
            "  Run lock not held; skipping stale-worktree cleanup "
            "(another live invocation may own them)"
        )
    if _report_preflight(
        ui,
        "stale component branches found",
        _preflight_component_branches(manifest, root_dir, ui),
    ):
        return None
    return run_decisions


def _write_partial_usage(path: Path, totals: UsageTotals) -> None:
    """Publish the engineer loop's usage-so-far, atomically (R8).

    The worker owns its UsageRecords in memory; the abort path
    (``_abort_inflight``) SIGKILLs the process, so without this file the
    spend of a killed worker is simply lost. ``atomicio`` owns the
    atomic-write convention (#291) and is what makes the file safe to
    read from a process that may be killing the writer: a reader sees the
    previous complete snapshot or the new one, never a torn one.

    Accounting only - every failure is swallowed. A worker must not die
    because its usage file could not be written.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, totals.to_dict())
    except (OSError, TypeError, ValueError):
        return


def _read_partial_usage(path: Path) -> UsageTotals | None:
    """Rehydrate a killed worker's last usage snapshot (R8).

    Returns None when the file is absent, unreadable, or not a usage
    dict - the abort path then records nothing rather than guessing.
    ``unreported_calls``, ``tokenless_calls`` and ``costless_calls`` are
    derived properties and are deliberately not read back.

    Backward compatible on read: ``token_calls`` (added in R8 after the
    review) and ``cost_calls`` (added with the cost ceiling) are optional
    and default to 0 when the payload predates them. That under-reports
    coverage rather than inventing it, and the file is rewritten at the
    next iteration boundary anyway - it is a per-run, per-attempt scratch
    file, never a long-lived record.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    totals = UsageTotals()
    int_fields = (
        "calls",
        "known_calls",
        "token_calls",
        "cost_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "total_tokens",
    )
    for name in int_fields:
        value = data.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            setattr(totals, name, value)
    for name in ("cost_usd", "duration_seconds"):
        value = data.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            setattr(totals, name, float(value))
    if totals.calls == 0:
        return None
    return totals


def _clear_partial_usage(path: Path) -> bool:
    """Retire the previous attempt's usage snapshot (R8 review, P2-c).

    The snapshot is keyed by run + component only, and a normal result
    leaves the file on disk, so without this the NEXT attempt inherits
    it: a retry cancelled before its own first iteration boundary got
    salvaged with the previous attempt's tokens, which
    ``process_result`` had already recorded - a clean double count.

    Called by the scheduler in the PARENT immediately before every
    submission, not by the worker, because the window that matters is
    exactly the one where the worker never starts (cancelled or killed
    during pool startup).

    Returns True when the slot is provably clean - deleted, or already
    absent. Returns False when a snapshot may still be on disk, and the
    caller must then refuse disk salvage for that attempt: deletion IS
    the attempt-scoping invariant, so swallowing the error left the
    previous attempt's totals addressable as the current one and
    re-counted them (review finding on 22e99b4). A worker that cannot
    delete the file generally cannot overwrite it either, so "we failed
    to clear it" and "what is there is stale" travel together.

    Never fails the component: an accounting risk is not a reason to
    stop work, but it IS a reason to distrust the number.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return True  # nothing to retire; the slot is clean
    except OSError:
        return False
    return True


def _worker_scope(scope: ComponentScope | None) -> tuple[list[str], list[str]]:
    """The snapshot's two halves as the worker's guard wants them.

    Its own function so ``_run_component`` spends no branching on
    unpacking an argument: that function is already the largest in this
    module and the complexity ratchet judges it against its own previous
    value, so a fix that reads as two harmless ternaries there is a
    refusal at commit time.

    ``None`` means the caller declared no scope, which is the documented
    no-guard behaviour: an empty authored list leaves
    ``guards.enforce_allowed_paths`` inert, and an empty carve-out
    declares nothing (reporting more, never less). Fresh lists, so the
    worker's config cannot write back into a snapshot the parent holds
    for the rest of the run.

    An UNTRUSTWORTHY snapshot gets the same answer, deliberately and by
    name rather than by ``allowed_paths or ()`` quietly reading None as
    "no constraint" (#293 review). This guard is a tripwire that fails
    open; the two layers that fail closed on that state are
    ``_preflight_component_scope``, which refuses the run before this
    function is ever reached, and Phase 1. Asking
    ``is_trustworthy`` keeps the collapse a stated decision rather than
    an accident of the expression.
    """
    if scope is None or not scope.is_trustworthy:
        return [], []
    return list(scope.allowed_paths or ()), list(scope.harness_paths)


def _run_component(
    component_id: str,
    prd_path_str: str,
    worktree_path_str: str,
    root_dir_str: str,
    prompt_file_str: str,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str | None,
    sleep_seconds: float,
    previous_context_json: str | None = None,
    feedforward_config_dict: dict[str, Any] | None = None,
    scaffold_cmd: str | None = None,
    component_deps: list[str] | None = None,
    knowledge_prefix: str = "",
    decisions_prefix: str = "",
    progress_file_str: str | None = None,
    codebase_map_file_str: str = "scripts/kstrl/codebase_map.md",
    agent_iteration_timeout: float = 1800.0,
    component_timeout: float = 7200.0,
    max_iterations: int = 10,
    interactive: bool = False,
    scope: ComponentScope | None = None,
    breaker_iterations: int = 3,
    breaker_test_command: str | None = None,
    breaker_test_timeout: float = 300.0,
    sandbox_enabled: bool = False,
    sandbox_allow_network: bool = False,
    agent_budget_usd: float | None = None,
    events_dir_str: str | None = None,
    usage_dir_str: str | None = None,
    run_id: str = "",
    token_budget: LoopBudget | None = None,
    redirect_output: bool = True,
    live_line: Callable[[str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    base_branch: str = "main",
    verify_config: VerifyConfig | None = None,
) -> ComponentResult:
    """Run a single component's implementation loop.

    Top-level function (picklable for ProcessPoolExecutor).
    Creates all objects internally - no shared state.

    Chunk 6 (TUI rewrite): with ``events_dir_str`` set, the worker
    writes typed events to <events_dir>/components/<id>/engineer.jsonl,
    the raw agent transcript to engineer.log, and (pool mode) dup2's
    its inherited stdout/stderr onto that same log so parallel workers
    stop interleaving raw lines on the parent terminal. ``live_line``
    (inline mode only - never pickled) mirrors each transcript line to
    the parent's UI so sequential runs keep live engineer output.
    Without ``events_dir_str`` the legacy PlainUI-on-stderr behavior is
    preserved for direct callers.

    R8: ``usage_dir_str`` is where the per-iteration usage snapshot is
    published so a killed worker's spend is recoverable. It is a
    SEPARATE argument from ``events_dir_str`` on purpose - the factory
    always sets it, including when progress logging is off, because
    accounting must not be switchable off by an observability setting
    (review finding P2-d). Direct callers that leave it None simply do
    not publish snapshots.

    R8: ``token_budget`` carries the run-level ``max_total_tokens`` plus
    the spend recorded before this worker launched, so the engineer loop
    can halt itself BETWEEN iterations instead of waiting for the
    parent's next phase boundary. None disables the in-loop check.

    #261: ``verify_config`` is forwarded to the loop so the engineer is
    told exactly what Phase 1 will run. It must be the SAME object
    ``_phase_verify`` reads, which is why both come from
    ``engineer_verify_config``: a CLI override or an uncommitted
    kstrl.toml edit lives only in the parent, and the parent's fallback
    is ``VerifyConfig()`` rather than a reload from disk. None means no
    gate runs, and the loop then states no commands at all.

    ``progress_file_str`` is None unless the operator explicitly
    configured a progress path; None means "derive it next to this
    component's PRD", which is what keeps the file inside the
    component's allowedPaths (see config.component_progress_path).

    #269: ``scope`` is the component's plan-time scope snapshot,
    resolved once by ``scope.RunScope`` before any engineer ran and
    handed down WHOLE rather than as two lists. This function derives
    nothing and reads no PRD to obtain it, which is what makes the
    in-loop guard's scope a value the agent never had access to, in
    worktree mode and under ``use_worktrees=False`` alike. One argument
    rather than two because the two halves are one decision: split into
    separate positional slots they would be kept in step by index
    alignment across two files, and the provenance recorded with them
    would stop at the pool boundary. ``None`` is the documented
    no-scope behaviour - the in-loop guard is inert without an authored
    list, and declares no carve-out.
    """
    from kstrl.agents import (
        get_agent,
    )
    from kstrl.loop import run_loop
    from kstrl.ui.bridge import EventBridgeUI, NullPrompter
    from kstrl.ui.plain import PlainUI

    start = time.monotonic()
    worktree_path = Path(worktree_path_str)
    # R0.4: every copy source below resolves against root_dir, never the
    # worker's inherited CWD. prompt.md and the PRD live under gitignored
    # scripts/kstrl/, so a fresh worktree NEVER contains them via git; if
    # a CWD-relative lookup missed them (e.g. --root from another
    # directory) the copies silently no-op'd and the engineer fell back
    # to the harness DEFAULT_PROMPT (phase-f e2e validation, line 38).
    root_dir = Path(root_dir_str)

    ui: UI
    worker_bus: EventBus | None = None
    transcript_fh: TextIO | None = None
    stop_heartbeat: Callable[[], None] | None = None
    run_paths: RunPaths | None = None
    if events_dir_str is None:
        ui = PlainUI(no_color=True)
    else:
        run_paths = RunPaths(root=Path(events_dir_str))
        comp_dir = run_paths.component_dir(component_id)
        try:
            comp_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if redirect_output:
            # dup2 BEFORE any threads start (chunk 6 invariant); stray
            # library writes land in the transcript, never the terminal.
            _redirect_worker_output(run_paths.engineer_log(component_id))
            # PR B: a shutdown SIGTERM from the parent must group-kill
            # this worker's agent subprocess (its own session leader),
            # not orphan it. Pool mode only - inline mode runs in the
            # parent, whose handlers belong to the cli/TUI.
            _install_worker_signal_forwarding()

    agent = get_agent(
        agent_cmd,
        model,
        reasoning,
        agent_type,
        sandbox=SandboxConfig(
            enabled=sandbox_enabled,
            allow_network=sandbox_allow_network,
        ),
        max_budget_usd=agent_budget_usd,
    )

    # Copy PRD into worktree if needed.
    #
    # shutil.copyfile, not read_text/write_text: these are COPIES, and a
    # copy that decodes and re-encodes is only byte-exact when the
    # locale's codec round-trips. #291 made the PRD utf-8 on disk, which
    # under LC_ALL=C a bare read_text cannot decode at all, and #286's
    # scaffold digests depend on prompt.md copying byte for byte. A byte
    # copy removes the encoding question rather than answering it four
    # times.
    worktree_prd = worktree_path / prd_path_str
    prd_source = root_dir / prd_path_str
    if not worktree_prd.exists() and prd_source.exists():
        worktree_prd.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prd_source, worktree_prd)

    # The prompt template's $prd_path placeholder (shipped in
    # DEFAULT_PROMPT >= 1.1.0 and the scaffolded prompt.md) is substituted
    # at runtime by loop.py with config.prd_file, so the agent reads the
    # SAME per-component PRD that check_prd_stories re-reads (R2.3, H-11)
    # without overwriting scripts/kstrl/prd.json.

    # Copy prompt into worktree if needed
    worktree_prompt = worktree_path / prompt_file_str
    prompt_source = root_dir / prompt_file_str
    if not worktree_prompt.exists() and prompt_source.exists():
        worktree_prompt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prompt_source, worktree_prompt)

    # Copy CLAUDE.md / AGENTS.md into the worktree from root_dir. When
    # use_worktrees=False, worktree_path IS the repo root so the files are
    # already in place - the .exists() guards handle this correctly.
    claude_dest = worktree_path / "CLAUDE.md"
    claude_src = root_dir / "CLAUDE.md"
    if not claude_dest.exists() and claude_src.exists():
        shutil.copyfile(claude_src, claude_dest)
    agents_dest = worktree_path / "AGENTS.md"
    agents_src = root_dir / "AGENTS.md"
    if not agents_dest.exists():
        if agents_src.is_symlink() and claude_dest.exists():
            # Preserve the AGENTS.md -> CLAUDE.md symlink convention.
            agents_dest.symlink_to("CLAUDE.md")
        elif agents_src.exists():
            shutil.copyfile(agents_src, agents_dest)
        elif claude_dest.exists():
            agents_dest.symlink_to("CLAUDE.md")

    # Run scaffold script if configured
    if scaffold_cmd:
        try:
            subprocess.run(
                scaffold_cmd,
                shell=True,
                cwd=worktree_path,
                capture_output=True,
                timeout=120,
            )
        except Exception:
            pass  # scaffold failure is non-fatal

    # Build feedforward context (Phase 0)
    feedforward_prefix: str = ""
    if feedforward_config_dict:
        try:
            ff_config = FeedforwardConfig(**feedforward_config_dict)
            feedforward_prefix = build_feedforward_context(
                worktree_path,
                ff_config,
                component_id=component_id,
                component_deps=component_deps,
            )
        except Exception:
            pass  # feedforward failure is non-fatal

    # Build context prefix from previous retries
    context_prefix: str | None = None
    # One list rather than one `if` per source: three context blocks
    # reach the engineer the same way and differ only in where they were
    # built, so adding the fourth should not mean adding a branch.
    parts: list[str] = [
        block for block in (knowledge_prefix, decisions_prefix, feedforward_prefix) if block
    ]
    if previous_context_json:
        ctx = IterationContext.from_json(previous_context_json)
        formatted = ctx.format_for_prompt()
        if formatted.strip():
            parts.append(formatted)
    if parts:
        context_prefix = "\n\n".join(parts)

    # R2.3 (CRIT-8): max_iterations, interactive, and allowed_paths come
    # from the invoking config via _submit_args. They were previously
    # hardcoded here (30 / False / unset), which made `ks run N`, -i,
    # and --allowed-paths silent no-ops under the factory pipeline.
    component_progress_rel = component_progress_path(
        prd_path_str,
        progress_file_str,
    )
    # #264/#269: the plan-time snapshot handed down by _submit_args.
    # The worker derives NOTHING from it. It used to rebuild the
    # carve-out here from its own three path strings, which agreed with
    # Phase 1 only because both derivations happened to be fed the same
    # inputs; the snapshot makes that agreement structural, and it is
    # the same value Phase 1 reads (pipeline._phase_verify ->
    # check_diff_scope). This guard fires FIRST, so a scope only one of
    # them had would still kill the component before Phase 1 was
    # reached.
    authored_paths, harness_paths = _worker_scope(scope)
    config = KstrlConfig(
        max_iterations=max_iterations,
        prompt_file=worktree_prompt,
        prd_file=worktree_prd,
        progress_file=worktree_path / component_progress_rel,
        codebase_map_file=worktree_path / codebase_map_file_str,
        sleep_seconds=sleep_seconds,
        interactive=interactive,
        allowed_paths=authored_paths,
        kstrl_branch="",
        kstrl_branch_explicit=True,
        agent_cmd=agent_cmd,
        model=model,
        model_reasoning_effort=reasoning,
        agent_type=agent_type,
        ui_mode="plain",
        no_color=True,
    )

    timeouts = TimeoutConfig(
        agent_iteration=agent_iteration_timeout,
        component_total=component_timeout,
    )
    breaker_config = BreakerConfig(
        no_progress_iterations=breaker_iterations,
        test_command=breaker_test_command,
        test_timeout=breaker_test_timeout,
    )

    # Start event-owned resources only after setup succeeds. A get_agent,
    # file-copy, or config failure therefore cannot leak a heartbeat thread
    # or open JSONL/transcript handles in a reusable pool worker.
    if run_paths is not None:
        try:
            transcript_fh = open(
                run_paths.engineer_log(component_id),
                "a",
                buffering=1,
                encoding="utf-8",
            )
        except OSError:
            transcript_fh = None

        def _transcript(line: str) -> None:
            if transcript_fh is not None:
                transcript_fh.write(line + "\n")
            if live_line is not None:
                live_line(line)

        worker_bus = EventBus(
            JsonlSink(run_paths.engineer_events(component_id)),
            run_id=run_id,
            source="worker",
            component=component_id,
        )
        ui = EventBridgeUI(
            worker_bus,
            prompter=NullPrompter(),
            transcript=_transcript,
        )
        stop_heartbeat = _start_heartbeat(worker_bus)

    # R8: durable per-iteration usage snapshot. The parent reads it only
    # when a worker was killed before it could return a ComponentResult
    # (PR B abort path), so it can never double count with result.usage.
    # Keyed off usage_dir_str, NOT events_dir_str: the accounting file
    # is written even when progress logging is disabled (P2-d).
    on_iteration_usage: Callable[[UsageTotals], None] | None = None
    if usage_dir_str is not None:
        usage_path = RunPaths(root=Path(usage_dir_str)).engineer_usage(
            component_id,
        )
        on_iteration_usage = functools.partial(
            _write_partial_usage,
            usage_path,
        )

    try:
        result = run_loop(
            config,
            ui,
            agent,
            worktree_path,
            context_prefix=context_prefix,
            timeouts=timeouts,
            breaker_config=breaker_config,
            bus=worker_bus,
            stop_check=stop_check,
            budget=token_budget,
            on_iteration_usage=on_iteration_usage,
            guard_base_ref=base_branch,
            guard_ignored_paths=harness_paths,
            # #274: the project root, NOT worktree_path. The two are the
            # same directory only under use_worktrees=False, which is
            # exactly when `.kstrl/` reaches the guard's walk; in a real
            # worktree they differ and the loop carves nothing out, so a
            # `.kstrl/` the AGENT wrote there stays a violation.
            guard_state_root=root_dir,
            verify_config=verify_config,
        )
        # Report which limit fired so the retry/fail path can act on it
        # (timeout errors trigger the recreate-from-base retry hygiene).
        if result.completed:
            error = None
        elif result.budget_halt_reason:
            # First in the chain: the budget halt is the reason the loop
            # stopped short, and its message carries the numbers the
            # pipeline needs when its own totals do not yet show a
            # breach (unreported-usage case).
            error = result.budget_halt_reason
        elif result.guard_violations:
            # R8 review: name the files. The guard used to fail the
            # iteration and fall through to "Did not complete", so the
            # retry agent was told only that something went wrong - and
            # the cheapest strategy for an agent with no diagnosis is to
            # redo the same edit. Mirrors check_diff_scope's wording so
            # the two scope failures read identically wherever they are
            # caught.
            #
            # R0.4 is why the base branch and the COMPLETE allowed-paths
            # list are repeated verbatim rather than paraphrased: the
            # recorded e2e run guessed `main` as the base and reverted
            # base-branch content with `git checkout main -- ...`,
            # failing again. Those two lines used to arrive from Phase 1;
            # now that the in-loop guard fires FIRST, this message is
            # what reaches attempt 2, so it must carry them itself.
            shown = list(result.guard_violations[:15])
            more = len(result.guard_violations) - len(shown)
            listed = ", ".join(shown) + (f" ... and {more} more" if more else "")
            error = (
                f"{len(result.guard_violations)} file(s) outside the "
                f"component's allowed scope, caught in-loop after "
                f"iteration {result.iterations}: {listed}. "
                f"Base branch: {base_branch} "
                f"(scope is judged on `git diff {base_branch}...HEAD`; "
                f"do NOT `git checkout {base_branch} -- <path>`, revert "
                "only your own out-of-scope commits/edits). "
                f"Allowed paths (complete list): "
                f"{', '.join(authored_paths)}; "
                # #264: the two sets stay separate. The harness artifacts
                # are already in scope, so naming them here stops the
                # retry agent reading its own PRD or progress log as the
                # thing it must stop writing - which is the one edit it
                # cannot make and still pass prd_stories.
                f"plus harness artifacts (kstrl's own files, already in "
                f"scope, no need to widen allowedPaths): "
                f"{', '.join(harness_paths)}. "
                "Do not widen allowedPaths."
            )
        elif result.no_progress:
            error = (
                "no-progress circuit breaker tripped: "
                f"{breaker_iterations} consecutive iteration(s) produced an "
                "unchanged diff hash and test signature"
            )
        elif result.timeout_limit == "component":
            error = (
                f"component timeout: exceeded {component_timeout}s wall clock "
                f"after {result.iterations} iteration(s)"
            )
        elif result.timed_out_iterations:
            error = (
                f"Did not complete ({result.timed_out_iterations} iteration(s) "
                f"hit the {agent_iteration_timeout}s agent iteration timeout)"
            )
        else:
            error = "Did not complete"
        return ComponentResult(
            component_id=component_id,
            success=result.completed,
            iterations=result.iterations,
            error=error,
            duration_seconds=time.monotonic() - start,
            context_json=previous_context_json,
            usage=result.usage,
            no_progress=result.no_progress,
            budget_exceeded=bool(result.budget_halt_reason),
            budget_halt_condition=result.budget_halt_condition,
            budget_halt_ceilings=result.budget_halt_ceilings,
            guard_violations=result.guard_violations,
        )
    except Exception as exc:
        return ComponentResult(
            component_id=component_id,
            success=False,
            iterations=0,
            error=str(exc),
            duration_seconds=time.monotonic() - start,
            context_json=previous_context_json,
            # The loop crashed, but any iterations that did run still
            # cost tokens; collect what the agent recorded (R3.1).
            usage=collect_usage(agent),
        )
    finally:
        if stop_heartbeat is not None:
            stop_heartbeat()
        if worker_bus is not None:
            worker_bus.close()
        if transcript_fh is not None:
            try:
                transcript_fh.close()
            except OSError:
                pass


def _install_worker_signal_forwarding() -> None:
    """Pool-worker SIGTERM handler: kill the agent's process group,
    then exit 130. Installed only on a worker's main thread."""
    if threading.current_thread() is not threading.main_thread():
        return

    def _on_term(signum: int, frame: object) -> None:
        del signum, frame
        try:
            kill_active_process_groups()
        finally:
            os._exit(130)

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        pass


def _redirect_worker_output(log_path: Path) -> None:
    """Point the worker's fds 1/2 (and sys.stdout/stderr) at its
    transcript so nothing reaches the parent terminal (chunk 6). Best
    effort: a worker that cannot redirect keeps inherited fds rather
    than dying."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        os.dup2(f.fileno(), 1)
        os.dup2(f.fileno(), 2)
        sys.stdout = f
        sys.stderr = f
    except OSError:
        pass


class _InlineExecutor:
    """Synchronous stand-in for ProcessPoolExecutor when max_parallel
    is 1 (R7.3): the worker runs in-process (no pickling, no process
    spawn - the historical sequential path), wrapped in an already-
    resolved Future so ONE scheduling loop serves both modes. A worker
    exception lands on the future and surfaces at ``future.result()``,
    exactly where a pool worker's would.
    """

    def submit(
        self,
        fn: Callable[..., ComponentResult],
        /,
        *args: Any,
    ) -> Future[ComponentResult]:
        future: Future[ComponentResult] = Future()
        try:
            result = fn(*args)
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        return future

    def shutdown(
        self,
        wait: bool = True,
        cancel_futures: bool = False,
    ) -> None:
        """Nothing to shut down: every submit already ran to completion."""


def _wait_interruptible(
    futures: set[Future[ComponentResult]],
    timeout: float | None,
    stop: StopController | None,
    slice_seconds: float = 0.5,
) -> tuple[set[Future[ComponentResult]], bool]:
    """concurrent.futures.wait in stop-checkable slices (PR B).

    Returns (done, stopped). Worst-case stop latency is one slice;
    the backstop deadline math is preserved by honoring ``timeout``.
    """
    if stop is None:
        done, _ = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
        return done, False
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        if stop.is_set():
            return set(), True
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        slice_t = slice_seconds if remaining is None else min(slice_seconds, remaining)
        done, _ = wait(futures, timeout=slice_t, return_when=FIRST_COMPLETED)
        if done:
            return done, False
        if remaining is not None and remaining <= slice_seconds:
            return set(), False


def _resolve_max_parallel(factory_config: FactoryConfig, ui: UI) -> int:
    """Effective parallelism, and a warning for every setting it discards.

    Extracted from ``_run_factory_locked`` (#292) so the decision has a
    name and can be tested. It is also the decision that hid a fake test
    for months: the spine SIGTERM test asked for ``max_parallel=2``,
    silently got 1 and therefore ``_InlineExecutor``, and nothing said so.

    Both discards are REPORTED, and only when they actually discard
    something. The worktree branch used to fire unconditionally as an
    info line that never named ``max_parallel``, so an operator who
    configured 8 was never told the 8 was thrown away.
    ``ui.kv("Max parallel", ...)`` prints the effective value, which
    cannot distinguish "I asked for 1" from "I asked for 8 and lost it".
    """
    max_parallel = factory_config.max_parallel
    if not factory_config.use_worktrees:
        if max_parallel > 1:
            ui.warn(
                f"worktrees disabled: components cannot run in parallel; "
                f"forcing max_parallel=1 (you configured {max_parallel})"
            )
        return 1
    if factory_config.single_pr and max_parallel > 1:
        # R0.5 (H-8): single_pr components all live on ONE branch, and a
        # branch can only be checked out in one worktree at a time -
        # parallel same-tier components would hard-fail on "already
        # checked out". Sequential is the only layout that works.
        ui.warn(
            f"single_pr mode: components share one branch; forcing "
            f"max_parallel=1 (you configured {max_parallel})"
        )
        return 1
    return max_parallel


def _refused_before_launch(
    pipeline: ComponentPipeline,
    comp: Component,
    run_scope: RunScope,
) -> bool:
    """Every reason to fail a READY component instead of launching it.

    Returns True when the component was transitioned without a launch,
    which the caller counts so the scheduling loop re-derives its ready
    set rather than stopping while schedulable siblings remain.

    One function because the three refusals share a shape and a
    contract - fail loudly, spend nothing, do not halt the run - and
    because the scheduling loop is already far past the repo's
    complexity gate, so a fourth inline branch is a refusal at commit
    time. None of them halts: by this point siblings may have merged,
    and discarding their work to punish one component is the worse
    trade.
    """
    # R3.1 scheduling gate: a blown ceiling (tokens or cost) fails
    # pending components loudly instead of launching an engineer loop
    # that would only add spend.
    if pipeline.budget_exceeded():
        pipeline.fail_for_budget(comp, "scheduling")
        return True
    # R8 review: a loop that emits COMPLETE returns before its own
    # budget check, so an adapter that finishes on its first silent call
    # never halts itself - the ordinary success path for a custom
    # agent_cmd. This gate is what stops the run handing out NEW work
    # under a ceiling that can no longer fire. Halts only when EVERY
    # configured ceiling is dead: a cost-reporting adapter still
    # enforces max_cost_usd even though its token cap is beyond saving.
    #
    # Identity is derived inside fail_for_budget: nothing was BREACHED
    # here, so the rule falls through to the dead ceilings and names
    # only those actually configured.
    unenforceable = pipeline.budget_unenforceable()
    if unenforceable is not None:
        pipeline.fail_for_budget(comp, "scheduling", reason=unenforceable)
        return True
    # #294: the LAST point before spend. The plan-time preflight refuses
    # an unresolved scope before the run starts, but it only inspects
    # components that are PENDING then, and the contract breaker resets
    # a COMPLETED component to PENDING inside the scheduling loop, long
    # after _run_preflights returned. Without this gate such a component
    # runs a full engineer loop - up to max_iterations LLM calls - and
    # only then does Phase 1 refuse it. Measured against the same
    # dogfooding run _preflight_component_scope cites (14.49 dollars /
    # 41 minutes for three attempts), Phase 1's FAIL alone still left
    # roughly 4.83 dollars and 14 minutes on the table per reset.
    #
    # This also closes --no-verify, which returns from _phase_verify
    # before the ungated check can run: a component with no trustworthy
    # scope now never launches, so it cannot merge with the guard inert.
    comp_scope = run_scope.for_component(comp.id)
    if not comp_scope.is_trustworthy:
        pipeline.fail(
            comp,
            scope_unreadable_error(f"Error: {comp_scope.error}"),
            phase="scope",
            check=SCOPE_UNREADABLE_CHECK,
            signatures=[f"{SCOPE_UNREADABLE_CHECK}:no-trustworthy-scope"],
        )
        return True
    return False


def _abort_inflight(
    executor: ProcessPoolExecutor | _InlineExecutor,
    running_futures: dict[Future[ComponentResult], str],
    pipeline: ComponentPipeline,
    ui: UI,
    stop: StopController,
    term_grace: float = 5.0,
) -> None:
    """Group-terminate in-flight workers and record their components as
    aborted (PR B). Pool mode SIGTERMs each worker pid - the worker's
    forwarding handler group-kills its agent subprocess and exits 130;
    stragglers get SIGKILL after the grace period. A second stop request
    skips the rest of that grace period. `_processes` is private executor
    API, so process objects are inspected defensively and only workers
    still reported alive are killed.
    """
    procs = getattr(executor, "_processes", None)
    workers: list[Any] = list(procs.values()) if procs else []
    if workers:
        alive = []
        for worker in workers:
            try:
                pid = worker.pid
                if isinstance(pid, int) and pid > 1 and pid != os.getpid() and worker.is_alive():
                    worker.terminate()
                    alive.append(worker)
            except (AssertionError, AttributeError, OSError, ValueError):
                pass
        deadline = time.monotonic() + term_grace
        while alive and not stop.force and time.monotonic() < deadline:
            still_alive = []
            for worker in alive:
                try:
                    if worker.is_alive():
                        still_alive.append(worker)
                except (AssertionError, AttributeError, OSError, ValueError):
                    pass
            alive = still_alive
            if alive and not stop.force:
                time.sleep(0.1)
        for worker in alive:
            try:
                if worker.is_alive():
                    worker.kill()
            except (AssertionError, AttributeError, OSError, ValueError):
                pass
    elif isinstance(executor, _InlineExecutor):
        # Inline executor: the agents live in THIS process, so group-kill
        # them directly.
        killed = kill_active_process_groups()
        if killed:
            ui.warn(f"  Terminated {killed} in-flight agent process group(s)")
    for future, comp_id in list(running_futures.items()):
        _salvage_aborted_usage(future, comp_id, pipeline)
        pipeline.fail_aborted(comp_id, stop.reason)
        running_futures.pop(future, None)
    ui.warn(f"  Aborted in-flight work: {stop.reason}")
    executor.shutdown(wait=False, cancel_futures=True)


def _salvage_aborted_usage(
    future: Future[ComponentResult],
    comp_id: str,
    pipeline: ComponentPipeline,
) -> None:
    """Record what an aborted worker spent before it was stopped (R8).

    Organic failures carry usage back on the ComponentResult
    (``pipeline.process_result`` records it before the success branch -
    "failed attempts cost real tokens too"). The abort path used to lose
    it: the worker is killed and its future is cancelled without anyone
    reading a result, so the tokens vanished from the meter and the run
    under-reported its own spend.

    Two recovery routes, deliberately exclusive so nothing is counted
    twice:
    - The future already carries a result (inline executor, or a pool
      worker that finished as the stop landed): that result is
      authoritative. ``result()`` cannot block here - the future is
      done.
    - Otherwise the worker was killed mid-loop: fall back to the
      iteration-boundary snapshot it wrote to disk. That file is
      rewritten atomically, so reading it while the writer is being
      killed yields a complete (if slightly stale) snapshot. Spend
      inside the interrupted iteration is NOT recoverable - it was never
      reported to anyone before the kill.

    The snapshot is attempt-scoped by the scheduler, which deletes it
    immediately before every submission (``_clear_partial_usage``). That
    is what makes this route safe: without it a completed attempt left
    its file behind and a later attempt aborted before its own first
    iteration boundary salvaged the PREVIOUS attempt's tokens on top of
    the ones ``process_result`` had already recorded (review finding
    P2-c, reproduced as a clean 2x double count).

    Reads ``pipeline.usage_paths``, not ``run_paths``: accounting
    storage exists even when progress logging is off (P2-d).
    """
    if future.done() and not future.cancelled():
        try:
            result = future.result(timeout=0)
        except Exception:  # noqa: BLE001 - a crashed worker reported nothing
            return
        if result is not None and result.usage is not None:
            pipeline.record_engineer_usage(comp_id, result.usage)
        return
    usage_paths = pipeline.usage_paths
    if usage_paths is None:
        return
    if not pipeline.usage_salvage_is_safe(comp_id):
        return
    totals = _read_partial_usage(usage_paths.engineer_usage(comp_id))
    if totals is not None:
        pipeline.record_engineer_usage(comp_id, totals)


def _next_backstop_wait(
    running: Mapping[Future[ComponentResult], str],
    deadlines: Mapping[Future[ComponentResult], float],
    now: float,
) -> float | None:
    """Seconds until the nearest scheduler-backstop deadline among running
    futures. None means no deadline is armed (wait indefinitely for the
    next completion, the pre-R0.1 behavior)."""
    pending = [deadlines[f] for f in running if f in deadlines]
    if not pending:
        return None
    return max(0.0, min(pending) - now)


def _expired_futures(
    running: Mapping[Future[ComponentResult], str],
    deadlines: Mapping[Future[ComponentResult], float],
    now: float,
) -> list[Future[ComponentResult]]:
    """Running futures whose scheduler-backstop deadline has passed."""
    return [f for f in running if not f.done() and f in deadlines and now >= deadlines[f]]


class _HealthBreach(Protocol):
    """The R8.4 breach record this seam reads and #151 will supply.

    Structural rather than imported: ``kstrl/health.py`` does not exist
    yet, so the factory must not take a hard dependency on it. These five
    attributes are the whole contract between #232 and #151; anything
    else the eventual dataclass carries is invisible here.

    READ-ONLY members, declared as properties rather than as the plain
    ``metric: str`` a Protocol usually carries. A plain variable member
    demands a SETTABLE attribute, and the record #151 is contracted to
    supply is a ``@dataclass(frozen=True)``, whose attributes are
    read-only: written the obvious way, this Protocol rejects the one
    implementation it exists to describe, and would have done so at
    exactly the moment the two changes met.
    :func:`_health_breach_seam_accepts_the_contract` below is the guard
    for that: it hands mypy the mandated frozen shape where a
    ``_HealthBreach`` is expected, so ``uv run mypy kstrl/ --strict``
    fails here rather than in #151.
    """

    @property
    def metric(self) -> str: ...

    @property
    def rule(self) -> str: ...

    @property
    def value(self) -> float: ...

    @property
    def limit(self) -> float: ...

    @property
    def window_runs(self) -> int: ...


if TYPE_CHECKING:

    @dataclass(frozen=True)
    class _HealthBreachContract:
        """The record #151 is contracted to supply, spelled as the issue
        and ``docs/dark-factory-roadmap.md`` spell it."""

        metric: str
        rule: str
        value: float
        limit: float
        window_runs: int

    def _health_breach_seam_accepts_the_contract(
        breach: _HealthBreachContract,
    ) -> _HealthBreach:
        """A static assertion, and the only guard this seam can have.

        The seam reads a module ``importlib`` returns as ``Any``, so
        nothing about the two sides is checked where they meet. This
        function is checked: it hands mypy the FROZEN dataclass the
        contract mandates where a ``_HealthBreach`` is expected, so a
        Protocol that dataclass cannot satisfy fails
        ``uv run mypy kstrl/ --strict`` here rather than in #151.
        ``TYPE_CHECKING`` only - nothing calls it and nothing is
        constructed.
        """
        return breach


def _open_health_breach_items(
    root_dir: Path,
    breaches: list[_HealthBreach],
    *,
    run_id: str,
    ui: UI,
) -> None:
    """Write one advisory item per breach, non-fatally.

    Honours ``[inbox] enabled``; a failed write warns and never fails the
    run, because losing the notice must not also lose the demotion the
    caller goes on to apply.

    One guard PER BREACH, not one around the loop: a failure on the third
    of five would otherwise drop the remaining two as well, under a
    single warning that named none of them. The warning names the breach
    it lost, so the operator can tell which observation is missing.
    """
    try:
        inbox_config = InboxConfig.load(root_dir)
    except (OSError, TypeError, ValueError, ControlStateError) as exc:
        # TypeError is this callee's own surface: InboxConfig.load casts
        # per key, so `[inbox] open_item_cap = 1979-05-27` (a valid TOML
        # date) raises it rather than a ValueError, and a raw traceback
        # from a seam whose whole contract is "bookkeeping cannot fail a
        # run" is the outcome this guard exists to prevent.
        ui.warn(f"Inbox write failed (non-fatal): {exc}")
        return
    if not inbox_config.enabled:
        return
    inbox = Inbox(root_dir, inbox_config)
    for breach in breaches:
        try:
            inbox.add(
                ItemKind.HEALTH_BREACH,
                f"Health breach: {breach.metric} {breach.rule}",
                detail=(
                    f"value {breach.value} beyond limit {breach.limit} "
                    f"over {breach.window_runs} run(s)"
                ),
                run_id=run_id,
                dedupe_key=f"health:{breach.metric}:{breach.rule}",
                evidence={
                    "metric": breach.metric,
                    "rule": breach.rule,
                    "value": breach.value,
                    "limit": breach.limit,
                    "window_runs": breach.window_runs,
                },
            )
        except (OSError, ValueError, ControlStateError) as exc:
            # ControlStateError is a RuntimeError, raised by the control
            # lock Inbox._append takes on every write.
            ui.warn(f"Inbox write failed for {breach.metric} {breach.rule} (non-fatal): {exc}")


def _record_health_breaches(
    root_dir: Path,
    state: AutonomyState,
    autonomy_config: AutonomyConfig,
    *,
    run_id: str,
    ui: UI,
    bus: EventBus,
) -> None:
    """Open an inbox item per R8.4 control-limit breach, and maybe demote.

    Inert until ``kstrl/health.py`` exists (#151). The import guard
    swallows exactly one thing: THAT module not being importable at all,
    which is what ``exc.name != "kstrl.health"`` decides. A
    ``kstrl.health`` that exists and fails on a dependency the operator
    has not installed raises ``ModuleNotFoundError`` naming that
    dependency, and must reach the caller's "Autonomy state update
    failed" warning rather than read as "#151 has not landed". A
    ``kstrl.health`` that exists and has renamed ``health_breaches``
    raises ``AttributeError`` and must do the same, so the attribute
    lookup is INSIDE the guarded block where widening the clause can
    swallow it and a test can see that it did. A seam that disarms itself
    silently is worse than one that is loudly broken, and a bare
    ``except ImportError`` cannot tell the two cases apart.

    Advisory first: the item is always written, the demotion happens only
    under ``[autonomy] demote_on_health_breach``. It is additionally
    suppressed while a cool-down is running, because a breach is a
    WINDOWED TREND rather than an event: it persists across runs, so an
    ungated trigger would take one level per run down to L1 before the
    operator had read the first notice. The suppression is announced, not
    silent: a windowed trend can hold for the whole cool-down, and an
    unexplained still level is the failure this ladder is meant to make
    visible.

    ``autonomy_config`` is the run's own snapshot, taken once at run
    start, rather than a second read here: a run must not have its
    permissions decided by one resolution of ``[autonomy]`` and its
    demotion by another.

    The false-alarm arithmetic #151 must choose its rule set against is
    in ``docs/dark-factory-roadmap.md`` under R8.4, with the cost of a
    false alarm attached. It is written there once rather than here as
    well: two copies of an arithmetic nothing keeps in step is how one of
    them goes wrong.
    """
    try:
        health = importlib.import_module("kstrl.health")
        breaches_of = health.health_breaches
    except ModuleNotFoundError as exc:
        if exc.name != "kstrl.health":
            raise
        return
    # OUTSIDE the guard on purpose: a ModuleNotFoundError raised by
    # #151's own code, for its own missing dependency, is that module
    # being broken and not this one being absent.
    breaches: list[_HealthBreach] = list(breaches_of(root_dir))
    if not breaches:
        return
    _open_health_breach_items(root_dir, breaches, run_id=run_id, ui=ui)
    if not autonomy_config.demote_on_health_breach:
        return
    if state.cooldown_runs_remaining > 0:
        ui.warn(
            f"Autonomy: {len(breaches)} health breach(es) recorded; not demoting during "
            f"cool-down ({state.cooldown_runs_remaining} decisive run(s) remaining)"
        )
        return
    first = breaches[0]
    apply_demotion(
        root_dir,
        DemotionTrigger.HEALTH_BREACH,
        f"{first.metric}: {first.rule}",
        evidence={
            "breaches": [
                {
                    "metric": breach.metric,
                    "rule": breach.rule,
                    "value": breach.value,
                    "limit": breach.limit,
                    "window_runs": breach.window_runs,
                }
                for breach in breaches
            ],
            "run_id": run_id,
        },
        run_id=run_id,
        ui=ui,
        bus=bus,
        state=state,
    )


def _record_autonomy_outcome(
    *,
    root_dir: Path,
    manifest: Manifest,
    factory_result: FactoryResult,
    autonomy_config: AutonomyConfig,
    bus: EventBus,
    run_id: str,
    ui: UI,
) -> None:
    """Fold one run's terminal outcome into the persisted ladder state.

    Evidence accrues only from what the run actually demonstrated:

    - **Decisive run**: at least one component reached a terminal verdict
      that was actually ABOUT the factory's judgement. A run that produced
      nothing, or whose components died on infrastructure (git diff
      failure, timeout kill, agent crash), says nothing about whether the
      factory can be trusted with more autonomy - counting those would let
      a string of broken runs burn down a cool-down and accrue promotion
      evidence. This mirrors the replay tool's decisive-run definition;
      the two must agree or the replay predicts nothing.
    - **Merged components**: the completed set. A component the human had
      to edit is not a clean merge - that signal arrives with R8.3's
      inbox, so for now every completion counts as clean and the
      clean-streak threshold stays deliberately unmeasured.
    - **Policy violations**: any component carrying an R8.1 policy finding.
      These both block promotion AND fire an immediate demotion, because a
      breach of the envelope is the clearest evidence that the current
      level is not warranted.
    - **Health breaches** (R8.4, seam only until #151): control-limit
      breaches over run metrics, opened as inbox items on every path and
      demoting only under an explicit switch. Read from ``kstrl.health``
      if that module exists; see ``_record_health_breaches``.

    Automatic demotion happens here rather than mid-run: demoting while
    components are still executing would change the flag bundle underneath
    them, and the run's own PRs were already gated by the level in force
    when it started.
    """
    state = AutonomyState.load(root_dir)

    by_id = {comp.id: comp for comp in manifest.components}

    def _infra_casualty(comp_id: str) -> bool:
        comp = by_id.get(comp_id)
        if comp is None:
            return False
        return any(f.is_infrastructure_error for f in comp.findings)

    judged_failures = [comp_id for comp_id in factory_result.failed if not _infra_casualty(comp_id)]
    decisive = bool(factory_result.completed or judged_failures)
    if decisive:
        state.record_decisive_run()
    for _comp_id in factory_result.completed:
        state.record_merged_component()

    violations = [
        comp.id
        for comp in manifest.components
        if any(
            finding.category.startswith(POLICY_CATEGORY_PREFIX) and finding.severity != "advisory"
            for finding in comp.findings
        )
    ]
    if violations:
        state.record_policy_violation(len(violations))
        apply_demotion(
            root_dir,
            DemotionTrigger.POLICY_VIOLATION,
            f"policy violation in {', '.join(sorted(violations))}",
            evidence={"components": sorted(violations), "run_id": run_id},
            run_id=run_id,
            ui=ui,
            bus=bus,
            state=state,
        )
    else:
        # Through the same save every other path uses, which is what
        # refuses to overwrite a file ``load`` failed closed on. This
        # branch is the one an ordinary run takes, and it was the branch
        # a guard placed in the demotion path could not see.
        save_ladder_state(state, root_dir, ui)
        if decisive:
            ui.kv(
                "Autonomy evidence",
                f"L{state.level}: {state.decisive_runs_at_level} decisive run(s), "
                f"{state.components_merged_at_level} merged",
            )

    # The health seam runs on every path, the demoting one included: the
    # cool-down that demotion just set is what then stops a second
    # revocation inside one run.
    _record_health_breaches(root_dir, state, autonomy_config, run_id=run_id, ui=ui, bus=bus)


def run_factory(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    ui: UI,
    root_dir: Path,
    manifest_path: Path | None = None,
    *,
    interaction: InteractionChannel | None = None,
    stop: StopController | None = None,
    run_id: str | None = None,
    notify_capture_output: bool = False,
    architect_usage: UsageTotals | None = None,
) -> FactoryResult:
    """Run the factory orchestrator with 3-phase verification.

    Phase 1: Mechanical verification (tests, typecheck, lint, PRD, diff scope)
    Phase 2: Second-opinion review (separate agent reviews diff against spec)
    Phase 3: Contract testing (merge tier branches, run integration tests)

    ``manifest_path`` is where run state is SAVED as well as where it was
    loaded from (R0.5, H-15): ``--manifest /custom.json`` must persist to
    /custom.json and ``ks run`` to its own run-manifest.json, never to
    another invocation's resumable ``scripts/kstrl/manifest.json``. None
    keeps the historical default of ``<root>/scripts/kstrl/manifest.json``.

    Holds the run-level ``.kstrl/factory.lock`` flock for the whole run
    (R0.5, H-7); a contending invocation is refused with exit code 2
    unless it passes ``--force-lock``.

    ``architect_usage`` is what the caller already spent ON THIS RUN's
    behalf, before the run existed: `ks factory` decomposes the spec
    itself and only then calls this (#257). Passing it is what makes
    ``max_cost_usd`` bound the architect - see
    :meth:`ComponentPipeline.record_architect_usage`. Named for the role
    rather than generically, because the body records it as the
    architect unconditionally: a second pre-run role would have to grow
    the signature rather than quietly borrow this one's row. None for
    every caller that resumes from a manifest, which ran no architect.
    """
    # The ceilings are validated at every CONFIG path, but a FactoryConfig
    # can also be constructed programmatically (tests, embedders, the SDK
    # path), which bypasses those. Re-check at the boundary: a safety
    # limit that only holds when you came in through the front door is
    # not a safety limit.
    validate_cost_ceiling(factory_config.max_cost_usd, "max_cost_usd")
    validate_token_ceiling(factory_config.max_total_tokens, "max_total_tokens")

    try:
        run_lock = _acquire_run_lock(
            root_dir,
            ui,
            force=factory_config.force_lock,
        )
    except FactoryLockHeldError as exc:
        ui.err(str(exc))
        refused = FactoryResult()
        refused.exit_code = 2
        return refused
    try:
        return _run_factory_locked(
            manifest,
            factory_config,
            base_config,
            ui,
            root_dir,
            manifest_path=manifest_path,
            lock_held=run_lock.held,
            interaction=interaction,
            stop=stop,
            run_id_override=run_id,
            notify_capture_output=notify_capture_output,
            architect_usage=architect_usage,
        )
    finally:
        run_lock.release()


def _warn_unsandboxable_reviewers(
    ui: UI,
    review_selection: AdversarialAgentSelection | None,
    security_selection: AdversarialAgentSelection | None,
) -> None:
    """#266: say so when a reviewer cannot be held read-only.

    ``get_agent`` returns ``CustomAgent`` BEFORE any adapter branch, so a
    custom command drops the read-only posture as well as the sandbox -
    and the reviewer selections fall back to the engineer's
    ``agent_cmd``, so ``[agent] command`` alone reaches them. The
    read-only guarantee is the one an operator is most likely to assume
    holds unconditionally, which is exactly why it is worth saying out
    loud when it does not.
    """
    for role, selection in (
        ("review", review_selection),
        ("security", security_selection),
    ):
        if selection is not None and selection.agent_cmd:
            ui.warn(
                f"  the {role} reviewer is a custom command; it CANNOT be "
                "sandboxed or held read-only, so it may write the worktree "
                "it is judging (#266)"
            )


def _run_factory_locked(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    ui: UI,
    root_dir: Path,
    manifest_path: Path | None,
    lock_held: bool,
    interaction: InteractionChannel | None = None,
    stop: StopController | None = None,
    run_id_override: str | None = None,
    notify_capture_output: bool = False,
    architect_usage: UsageTotals | None = None,
) -> FactoryResult:
    """run_factory body; runs with the run-level lock resolved (held, or
    explicitly degraded via --force-lock / no-fcntl platforms)."""
    factory_start = time.monotonic()
    factory_result = FactoryResult()
    # Stable run id shared by evolution journal and knowledge layer.
    # current_run_id() carries microseconds plus a random nonce, so two
    # factory invocations launched within the same UTC second neither
    # collide on .kstrl/knowledge/<comp>/<run_id>/ directories nor
    # order ambiguously (R1.6: same-second knowledge run dirs must sort
    # by creation time, not by nonce).
    # PR F needs the run dir known before the TUI starts: the caller
    # may mint the id (format unchanged - knowledge.current_run_id).
    run_id = run_id_override if run_id_override else current_run_id()

    # R6.1: structured "<check>:<code>" failure signatures per component
    # (e.g. "linter:E501", "review:scope_creep"), recorded at each
    # failure site from the parser/finding stream and handed to the
    # evolution journal at record_run. In-memory only: the manifest
    # already persists failed_phase/failed_check; the full signature
    # list is a journal concern.
    component_failure_signatures: dict[str, list[str]] = {}

    # Set up progress log. R3.2: defaults ON under .kstrl/ so a
    # walk-away run always leaves an event trail `ks status` can
    # join; [factory] progress_log_enabled = false (or env) opts out.
    # Every event carries run_id so runs sharing the default file stay
    # distinguishable.
    # Dual-write (TUI rewrite chunk 3): typed schema-v2 events go to
    # .kstrl/runs/<run_id>/events.jsonl via the EventBus; V1CompatSink
    # delegates the v1-named subset to a real ProgressLog so the
    # progress.jsonl byte format AND its attached ProgressSink
    # observers (Linear, R7.4) stay untouched. progress_log_enabled =
    # false suppresses BOTH files (symmetric opt-out).
    progress_log: ProgressLog
    # Chunk 7: when the caller's UI is the event bridge (cli commands
    # via build_console), reuse ITS bus so the run's file sinks also
    # capture every imperative Log narration - the imperative call
    # sites become replayable. Tests passing a bare PlainUI get a
    # private bus (their UI already prints directly).
    if isinstance(ui, EventBridgeUI):
        bus = ui.bus
        bus.run_id = run_id
    else:
        bus = EventBus(run_id=run_id)
    journal_path: Path | None = None
    run_paths: RunPaths | None = None
    run_file_sinks: list[EventSink] = []
    # R8 (review finding P2-d): accounting storage is allocated for
    # EVERY run, including progress_log_enabled = false. It used to be
    # `run_paths`, which is gated on progress logging, so turning the
    # observability opt-out on also deleted the only place a worker
    # could publish its spend - a killed worker then recorded nothing
    # and the run silently under-reported. Money is not observability:
    # an opt-out may drop the narration, never the meter. This is the
    # same directory as `run_paths` when progress logging is on; when it
    # is off the run writes exactly one small JSON file per component
    # attempt under .kstrl/runs/<run_id>/ and no events at all.
    usage_paths = RunPaths.for_run(root_dir, run_id)
    if not factory_config.progress_log_enabled:
        progress_log = NullProgressLog()
    else:
        log_path = factory_config.progress_log_path or root_dir / ".kstrl" / "progress.jsonl"
        progress_log = ProgressLog(log_path, run_id=run_id, warn=ui.warn)
        journal_path = log_path
        run_paths = RunPaths.for_run(root_dir, run_id)
        run_file_sinks = [
            JsonlSink(run_paths.events_file),
            V1CompatSink(progress_log),
        ]
        for _sink in run_file_sinks:
            bus.add_sink(_sink)

    # R7.4: Linear sink - mirrors failure/budget events onto the issues
    # the decompose hook mapped in the manifest. Observability only;
    # build_linear_sink returns None (with a warning) rather than ever
    # failing the run, and emit() isolates sink exceptions.
    linear_sink = build_linear_sink(
        manifest,
        factory_config.linear_config or LinearConfig.load(root_dir),
        run_id=run_id,
        warn=ui.warn,
    )
    if linear_sink is not None:
        progress_log.attach_sink(linear_sink)

    # R3.2: notification hooks - each condition fires at most once per
    # run, and hook failures only ever warn.
    notify = NotifyHooks(
        factory_config.notify_config or NotifyConfig.load(root_dir),
        run_id=run_id,
        project=manifest.project_name,
        warn=ui.warn,
        capture_output=notify_capture_output,
    )

    bus.emit(
        RunStarted(
            project=manifest.project_name,
            components=len(manifest.components),
        )
    )
    # Chunk 4: the component DAG + budget caps as one event, so a
    # dashboard can draw the board without reading the manifest.
    bus.emit(
        RunPlan(
            components=tuple(
                {"id": c.id, "title": c.title, "deps": list(c.dependencies)}
                for c in manifest.components
            ),
            max_total_tokens=factory_config.max_total_tokens,
            max_adversarial_calls=factory_config.max_adversarial_calls,
            max_cost_usd=factory_config.max_cost_usd,
        )
    )

    # Loaded here rather than at the ladder resolution below because the
    # #262 probe gate needs it first; it is a pure config read, and the
    # ladder still resolves in its original place for the reason its own
    # comment gives (the policy hash must record the clamped envelope).
    autonomy_config = AutonomyConfig.load(root_dir)

    # R7.1: resolve which model family reviews this run's diffs ONCE so
    # the choice is stable across components and the homogeneity warning
    # prints once per run, not per component. Explicit config always
    # wins; otherwise review/security default to the opposite family
    # from the engineer when that CLI is available. The selection is an
    # audit-trail event: same-family and cross-family runs must stay
    # distinguishable in the progress log.
    gates = _adversarial_phase_gates(factory_config, autonomy_config)
    review_selection = resolve_adversarial_selection(
        "review",
        may_dispatch_adversarial=gates.may_dispatch,
        explicit_cmd=factory_config.review_agent_cmd,
        explicit_type=factory_config.review_agent_type,
        explicit_model=factory_config.review_model,
        fallback_cmd=None,
        fallback_type=base_config.agent_type,
        fallback_model=None,
        fallback_reasoning=None,
        engineer_cmd=base_config.agent_cmd,
        engineer_type=base_config.agent_type,
    )
    security_selection: AdversarialAgentSelection | None = None
    if factory_config.security_config is not None:
        sec_cfg = factory_config.security_config
        security_selection = resolve_adversarial_selection(
            "security",
            may_dispatch_adversarial=gates.may_dispatch,
            explicit_cmd=sec_cfg.agent_cmd,
            explicit_type=sec_cfg.agent_type,
            explicit_model=sec_cfg.model,
            fallback_cmd=base_config.agent_cmd,
            fallback_type=base_config.agent_type,
            fallback_model=base_config.model,
            fallback_reasoning=base_config.model_reasoning_effort,
            engineer_cmd=base_config.agent_cmd,
            engineer_type=base_config.agent_type,
        )
    for _sel, _enabled in (
        (review_selection, gates.review),
        (security_selection, gates.security),
    ):
        if _sel is None or not _enabled:
            continue
        if _sel.warning:
            ui.warn(f"  {_sel.warning}")
        bus.emit(
            AdversarialAgentSelected(
                phase=_sel.phase,
                agent_source=_sel.source,
                identity=_sel.identity,
                agent_type=_sel.agent_type,
                model=_sel.model,
                homogeneous=_sel.warning is not None,
            )
        )

    if manifest_path is None:
        manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"

    # Load knowledge config once for the entire factory run, BEFORE the
    # pipeline is constructed. Binding it at construction removes the
    # late-binding accident where the old _handle_result closure read a
    # name that was only assigned further down the function (R7.3).
    knowledge_config = KnowledgeConfig.load(root_dir)

    # Scheduling state shared between the scheduler and the pipeline.
    worktree_paths: dict[str, Path] = {}
    component_contexts: dict[str, str] = {}  # comp_id -> context JSON
    # Components whose last failure was a timeout kill: their retry must
    # not trust the surviving worktree/branch state (R0.1 requirement 5).
    fresh_base_retry_ids: set[str] = set()
    # Components abandoned by the scheduler backstop; their workers may
    # still be alive, so their worktrees are never cleaned up here.
    leaked_component_ids: set[str] = set()

    # #269: every component's write scope, resolved HERE and nowhere
    # else. Both guards read this one snapshot - the in-loop guard
    # through _submit_args -> _run_component -> run_loop, and Phase 1
    # through the pipeline below - so neither has to read a PRD to learn
    # what a component may write, and the file the agent is allowed to
    # edit stops being the file that decides what it may edit. Resolved
    # before the pipeline exists and long before the scheduler can
    # launch anything, which is what makes the value one the agent never
    # had access to, with or without worktrees.
    run_scope = RunScope.resolve(manifest, root_dir, base_config)
    _record_run_scope(run_scope, bus, ui)

    # R7.3: the per-component phase chain and every component state
    # transition live in ComponentPipeline. Hooks are resolved from this
    # module's globals HERE, at run start, so tests patching
    # kstrl.factory.run_review (and friends) keep intercepting the
    # phase functions.
    #
    # Constructed BEFORE the DAG check below, and that placement is
    # load-bearing rather than incidental: the pipeline owns the meter,
    # so the meter's lifetime has to start with the run directory's
    # rather than with the first phase's. The record just below says
    # what breaks otherwise (#257 review). Construction is pure
    # attribute assignment, so doing it ahead of a check that can reject
    # the run costs nothing.
    pipeline = ComponentPipeline(
        manifest=manifest,
        manifest_path=manifest_path,
        factory_config=factory_config,
        base_config=base_config,
        ui=ui,
        root_dir=root_dir,
        run_id=run_id,
        bus=bus,
        journal_path=journal_path,
        run_paths=run_paths,
        usage_paths=usage_paths,
        interaction=interaction,
        notify=notify,
        review_selection=review_selection,
        security_selection=security_selection,
        knowledge_config=knowledge_config,
        factory_result=factory_result,
        run_scope=run_scope,
        hooks=PipelineHooks(
            run_mechanical_verification=run_mechanical_verification,
            run_review=run_review,
            run_security_review=run_security_review,
            distill_facts=distill_facts,
            measure_fact_utilization=measure_fact_utilization,
            cleanup_worktree=_cleanup_worktree,
        ),
        worktree_paths=worktree_paths,
        component_contexts=component_contexts,
        fresh_base_retry_ids=fresh_base_retry_ids,
        component_failure_signatures=component_failure_signatures,
    )

    # #257: the architect's spend, incurred by the caller before this run
    # existed, enters the meter here - the first thing done to the
    # pipeline, and deliberately AHEAD of the DAG check below.
    #
    # The money is already gone by this point, so every early exit from
    # here has to leave it recorded rather than report a run that cost
    # nothing. That is not a hypothetical: `decompose_spec` only WARNS on
    # DAG errors and returns the manifest anyway, so an LLM-produced
    # cyclic or dangling-dependency manifest reaches the return below on
    # an ordinary `--spec` run. With the meter built after it, the run
    # directory existed, the architect row did not, and `serve` charged
    # $0 for a launch that had spent real money (#257 review).
    #
    # The invariant this rests on - nothing between the run directory's
    # sinks and this line returns early - holds today and is checked by
    # hand, not by a mechanism. An early exit inserted above would break
    # it silently; the cyclic-manifest test pins only the case that was
    # actually reachable.
    #
    # The cost of the ordering is cosmetic: a coverage warning about an
    # unpriced architect prints just ahead of the "Cost ceiling" line
    # rather than just after it.
    pipeline.record_architect_usage(architect_usage)

    # Validate DAG
    ui.section("Factory: Validating DAG")
    dag_errors = manifest.validate_dag()
    if dag_errors:
        for err in dag_errors:
            ui.err(f"  {err}")
        factory_result.exit_code = 1
        return factory_result

    topo_order = manifest.topological_order()
    ui.ok(f"DAG valid: {len(topo_order)} components in dependency order")

    # Crash recovery: reset intermediate states
    for comp in manifest.components:
        if comp.status in (
            ComponentStatus.RUNNING.value,
            ComponentStatus.VERIFYING.value,
        ):
            ui.info(f"  Resetting '{comp.id}' from {comp.status} to PENDING")
            comp.status = ComponentStatus.PENDING.value

    # R3.3: persist which run owns this manifest state. completed_at is
    # blanked while the run is in flight and stamped in the summary
    # epilogue, so "did the last run finish?" is answerable from the
    # manifest alone (and later, from Linear).
    manifest.run_id = run_id
    manifest.completed_at = ""
    # R8.1: record the resolved policy envelope's hash so the manifest is
    # a self-contained audit record of what merge guardrails were in force
    # for this run. Computed from the same source the Phase 1 check reads.
    policy_config = factory_config.policy_config or PolicyConfig.load(root_dir)

    # R8.2: derive this run's permissions from the autonomy level. The
    # bundle is computed at run start and WINS over contradicting config,
    # so a hand-edited flag cannot grant autonomy the ladder never
    # awarded; contradictions are recorded rather than silently dropped.
    # Opt-in: when [autonomy] is disabled the config's own flags stand.
    #
    # Ordering matters: the level is resolved BEFORE the policy hash is
    # taken, because the bundle can clamp the envelope (deps_allow_new),
    # and the manifest must record the envelope actually enforced.
    autonomy_active = autonomy_config.enabled
    autonomy_level: AutonomyLevel | None = None
    if autonomy_active:
        autonomy_state = AutonomyState.load(root_dir)
        autonomy_level, clamps = resolve_runtime_level(
            autonomy_state,
            autonomy_config,
            policy_enabled=policy_config.enabled,
            root_dir=root_dir,
        )
        bundle = flag_bundle_for(autonomy_level)
        overrides = manual_override_notes(
            bundle,
            configured_pause_before_pr_merge=factory_config.pause_before_pr_merge,
            configured_review_mode=factory_config.review_mode,
        )
        factory_config.pause_before_pr_merge = bundle.pause_before_pr_merge
        factory_config.review_mode = bundle.review_mode
        # The ladder can only ever WITHHOLD a permission the envelope
        # grants, never add one: below L3, new dependencies are refused
        # even if [policy] deps_allow_new is true.
        if not bundle.deps_allow_new_permitted and policy_config.deps_allow_new:
            policy_config = replace(policy_config, deps_allow_new=False)
            overrides.append(
                f"[policy] deps_allow_new=true withheld at "
                f"{bundle.level.label} (ladder clamps to false)"
            )
        bus.emit(
            AutonomyLevelApplied(
                level=int(autonomy_level),
                label=autonomy_level.label,
                flags=tuple(bundle.describe()),
                overrides=tuple(clamps + overrides),
            )
        )
        ui.kv("Autonomy", f"L{int(autonomy_level)} - {autonomy_level.label}")
        for note in clamps:
            ui.warn(f"  {note}")
        for note in overrides:
            ui.warn(f"  Manual override ignored: {note}")
        # The pipeline must see the clamped envelope, not the raw config.
        factory_config.policy_config = policy_config

    # Issue #207 (review P1): checked AFTER autonomy resolution, because
    # the L1/L2 bundle can flip pause_before_pr_merge on when no config
    # flag ever set it - any earlier check misses that path and the gate
    # would be reported ON while _phase_checkpoint stays unreachable.
    gate_warning = merge_gate_unreachable_warning(factory_config)
    if gate_warning is not None:
        ui.warn(gate_warning)
    # R10.3: same timing, same reason - the L1/L2 bundle forces
    # review_mode to hard above, so a set-point gate that looked
    # unreachable before autonomy resolved may be reachable now.
    setpoint_warning = setpoint_gate_unreachable_warning(factory_config)
    if setpoint_warning is not None:
        ui.warn(setpoint_warning)

    manifest.policy_hash = policy_config.envelope_hash()
    manifest.save(manifest_path)

    # R0.2 crash recovery: MERGE_PENDING is re-pollable, not failed.
    # Re-poll before scheduling so confirmed merges unblock dependents.
    pipeline.repoll_merge_pending()

    max_parallel = _resolve_max_parallel(factory_config, ui)

    # R0.1: TimeoutConfig is the single source for the agent-iteration and
    # component wall-clock limits. Enforcement layers: the adapters kill
    # their subprocess group, run_loop aborts on the component wall clock,
    # and the scheduler backstop below catches a worker that hangs outside
    # both (e.g. a stuck scaffold or feedforward step).
    timeout_cfg = factory_config.timeout_config or TimeoutConfig.load(root_dir)
    backstop_seconds = (
        timeout_cfg.component_total + timeout_cfg.scheduler_backstop_margin
        if timeout_cfg.component_total > 0
        else 0.0
    )

    # R7.5: no-progress circuit breaker limits, forwarded into every
    # engineer loop. When [breaker].test_command is unset, the stall
    # probe falls back to the explicitly configured Phase 1 test command
    # (never the smart default: the probe runs inside the engineer loop
    # and must only execute commands the operator chose).
    breaker_cfg = BreakerConfig.load(root_dir)
    if (
        breaker_cfg.test_command is None
        and factory_config.verify_config is not None
        and factory_config.verify_config.test_command
    ):
        breaker_cfg = replace(
            breaker_cfg,
            test_command=factory_config.verify_config.test_command,
        )

    # R7.5: OS-level sandbox intent for engineer agent subprocesses.
    # A custom agent command has no generic sandbox surface, so intent
    # that cannot be honored is refused loudly instead of silently
    # dropped (an operator who opted in must not believe the boundary
    # exists when it does not).
    sandbox_cfg = SandboxConfig.load(root_dir)
    if sandbox_cfg.enabled and base_config.agent_cmd:
        ui.warn(
            "  [sandbox] enabled but the agent is a custom command; "
            "sandbox settings CANNOT be applied to it and are ignored "
            "(worktree isolation remains the only boundary)"
        )
    _warn_unsandboxable_reviewers(ui, review_selection, security_selection)

    run_decisions = _run_preflights(
        manifest,
        run_scope,
        root_dir,
        factory_config,
        run_id,
        ui,
        lock_held=lock_held,
    )
    # ``is None`` and not falsiness: a clean run with no decisions binds
    # the empty tuple, which is the normal state for every project that
    # predates #260, and treating that as a refusal would stop the
    # factory on every one of them.
    if run_decisions is None:
        factory_result.exit_code = 2
        return factory_result

    ui.section("Factory: Execution")
    ui.kv("Max parallel", str(max_parallel))
    ui.kv("Max retries", str(factory_config.max_retries))
    ui.kv("Review mode", factory_config.review_mode)
    contract_mode = (
        factory_config.contract_config.mode if factory_config.contract_config else "skip"
    )
    ui.kv("Contract check", contract_mode)
    ui.kv(
        "Agent timeout",
        f"{timeout_cfg.agent_iteration}s" if timeout_cfg.agent_iteration > 0 else "<disabled>",
    )
    ui.kv(
        "Component timeout",
        f"{timeout_cfg.component_total}s" if timeout_cfg.component_total > 0 else "<disabled>",
    )
    # R8 (measured): state each ceiling AND what it counts, before the
    # run spends anything. An operator set --max-cost-usd 25.0 on a run
    # whose cross-family reviewer reports tokens and no cost, and got a
    # ceiling that bounded the engineer alone; nothing had told them the
    # ceiling is denominated in REPORTED dollars.
    #
    # Deliberately says nothing about which roles will be covered. No
    # call has been made yet, so any per-role claim here would be a
    # prediction from a hard-coded adapter capability table - a table
    # this repo does not have and that would go stale the day an adapter
    # starts reporting cost. The measured per-role figure follows as a
    # `budget_coverage` event at the first call that reports nothing,
    # which is still early enough to act on.
    if factory_config.max_total_tokens > 0:
        ui.kv(
            "Token ceiling",
            f"{factory_config.max_total_tokens} total tokens "
            "(counts only calls whose agent reports a token count)",
        )
    if factory_config.max_cost_usd > 0:
        ui.kv(
            "Cost ceiling",
            f"${factory_config.max_cost_usd} "
            "(counts only calls whose agent reports a cost; roles whose "
            "agent reports none are unpriced and unbounded by it)",
        )

    def _path_relative_to_root(path: Path) -> str:
        """Render `path` relative to root_dir for use inside per-component
        worktrees. Falls back to the absolute path string when relativization
        fails (e.g. a path on a different mount)."""
        return relative_to_root(path, root_dir)

    prompt_file_rel = _path_relative_to_root(base_config.prompt_file)
    # The progress path is deliberately NOT hoisted out of the per-component
    # loop: unless it was explicitly configured, each component derives its
    # own next to its PRD so the engineer writes inside its allowedPaths
    # (base_config.component_progress_file, called from _submit_args).
    codebase_map_file_rel = _path_relative_to_root(base_config.codebase_map_file)

    def _launch_component(comp: Component) -> Path | None:
        """Set up worktree for a component. Returns worktree path or None."""
        try:
            if factory_config.use_worktrees:
                fresh_from_base = comp.id in fresh_base_retry_ids
                fresh_base_retry_ids.discard(comp.id)
                wt_path = _setup_worktree(
                    comp.id,
                    comp.branch_name,
                    manifest.base_branch,
                    root_dir,
                    run_id,
                    fresh_from_base=fresh_from_base,
                )
            else:
                wt_path = root_dir
            worktree_paths[comp.id] = wt_path
            return wt_path
        except RuntimeError as exc:
            ui.err(f"  Worktree setup failed for '{comp.id}': {exc}")
            # pipeline.fail covers the R3.2 notify + progress-log
            # calls and stamps the R3.3 failure/evidence fields.
            pipeline.fail(
                comp,
                str(exc),
                phase="provisioning",
                check="worktree_setup",
            )
            return None

    # Build feedforward config dict for serialization to worker processes
    ff_config_dict: dict[str, Any] | None = None
    if factory_config.feedforward_config and factory_config.feedforward_config.enabled:
        fc = factory_config.feedforward_config
        ff_config_dict = {
            "enabled": fc.enabled,
            "module_map": fc.module_map,
            "public_interfaces": fc.public_interfaces,
            "dependency_graph": fc.dependency_graph,
            "conventions": fc.conventions,
            "max_context_tokens": fc.max_context_tokens,
        }

    def _submit_args(comp: Component, wt_path: Path) -> tuple[Any, ...]:
        ctx_json = component_contexts.get(comp.id)
        engineer_usage = pipeline.engineer_usage_totals()
        scope = run_scope.for_component(comp.id)
        knowledge_prefix = ""
        if knowledge_config.enabled:
            try:
                knowledge_prefix = build_knowledge_context(
                    manifest,
                    comp,
                    knowledge_config.knowledge_root,
                    knowledge_config,
                )
            except Exception as exc:  # noqa: BLE001 - non-fatal, never silent
                # Non-fatal, but NOT a metrics detail: the engineer runs
                # without any of its facts when this fires. That is a
                # real degradation of the run, and it used to be a bare
                # `except: pass` that said nothing (#191).
                ui.warn(f"  Knowledge retrieval failed for {comp.id}: {exc}")
                pipeline.record_injected_knowledge(comp.id, None)
            else:
                pipeline.record_injected_knowledge(comp.id, knowledge_prefix)
        else:
            pipeline.record_injected_knowledge(comp.id, None)
        return (
            comp.id,
            comp.prd_path,
            str(wt_path),
            str(root_dir),
            prompt_file_rel,
            base_config.agent_cmd,
            base_config.model,
            base_config.model_reasoning_effort,
            base_config.agent_type,
            base_config.sleep_seconds,
            ctx_json,
            ff_config_dict,
            comp.scaffold or None,
            comp.dependencies or None,
            knowledge_prefix,
            # #260: rides the same context-prefix path the distilled
            # facts already ride, so the register reaches the engineer
            # without a second delivery mechanism. Per component: its
            # own decisions in full, the rest of the run summarised.
            build_decisions_context(run_decisions, comp.id),
            # Per-component, not run-wide: KstrlConfig.component_progress_file
            # keeps the engineer's progress log inside allowedPaths.
            base_config.component_progress_file(comp.prd_path, root_dir),
            codebase_map_file_rel,
            timeout_cfg.agent_iteration,
            timeout_cfg.component_total,
            # R2.3 (CRIT-8): forward the invoking config's loop settings;
            # they were previously dropped here and _run_component ran a
            # hardcoded 30 non-interactive iterations with no path guard.
            base_config.max_iterations,
            base_config.interactive,
            # R8 review: the COMPONENT's allowedPaths, not the run-wide
            # --allowed-paths flag. That flag is empty in every ordinary
            # factory run, so `if config.allowed_paths` in the loop was
            # False and guards.enforce_allowed_paths never ran: the
            # in-loop guard existed and was inert. A scope violation was
            # therefore only caught at Phase 1, AFTER the whole engineer
            # loop had been paid for - measured at $12.93 for one such
            # component, versus ~$2.50 for the single iteration it takes
            # to catch it here.
            #
            # #269: the plan-time snapshot, resolved from root_dir
            # before the first engineer call and the SAME object Phase 1
            # is judged with. Read here rather than re-derived per
            # submission, which is what makes a retry inherit the scope
            # the run started with: this closure runs once per attempt,
            # so a per-attempt re-read would let an agent that edited
            # the PRD in attempt 1 widen its own guard for attempt 2
            # (which, under use_worktrees=False, it can do to the file
            # this used to read).
            scope,
            # R7.5: no-progress circuit breaker limits.
            breaker_cfg.no_progress_iterations,
            breaker_cfg.test_command,
            breaker_cfg.test_timeout,
            # R7.5: OS-level sandbox intent for the engineer's agent CLI.
            sandbox_cfg.enabled,
            sandbox_cfg.allow_network,
            # R7.6: in-loop USD budget for the claude-sdk engineer.
            base_config.agent_budget_usd,
            # Chunk 6: worker event channel (None when progress logging
            # is disabled - transcripts and events off together).
            str(run_paths.root) if run_paths is not None else None,
            # R8: accounting channel. Always set, precisely because the
            # line above may be None (review finding P2-d).
            str(usage_paths.root),
            run_id,
            # R8: run-level ceilings (tokens AND cost) + the spend
            # recorded so far, snapshotted AT LAUNCH so the engineer loop
            # can halt itself between iterations. With max_parallel > 1 a
            # worker cannot see a concurrent sibling's spend, only what
            # the parent had recorded when this worker started.
            # prior_calls plus prior_token_calls/prior_cost_calls carry
            # the ENGINEER's non-reporting call counts down so the
            # unenforceable rule does not reset on every attempt and
            # component (review finding P1-a), while staying blind to
            # other roles' timeouts - those say nothing about whether the
            # engineer's adapter reports tokens or cost. The two counters
            # are separate because the two axes have separate coverage:
            # codex reports tokens and no cost, claude can report cost
            # with no usage dict. prior_total_tokens/prior_cost_usd stay
            # run-wide: the overrun checks ask what the RUN has spent.
            LoopBudget(
                max_total_tokens=factory_config.max_total_tokens,
                prior_total_tokens=pipeline.run_usage.total_tokens,
                prior_known_calls=pipeline.run_usage.known_calls,
                prior_calls=engineer_usage.calls,
                prior_token_calls=engineer_usage.token_calls,
                max_cost_usd=factory_config.max_cost_usd,
                prior_cost_usd=pipeline.run_usage.cost_usd,
                prior_cost_calls=engineer_usage.cost_calls,
            ),
        )

    # #261: run-invariant, so it is resolved once here rather than per
    # component. None when Phase 1 is off.
    engineer_verify = factory_config.engineer_verify_config()

    def _run_scheduling_pass() -> None:
        """Run ready components until nothing is PENDING-and-ready.

        Called once per contract pass: a contract breaker reset to
        PENDING after a failed contract phase re-enters scheduling via
        the outer loop in run_factory (R0.3) - previously the reset
        happened after the only scheduling loop had exited, so the
        promised retry never ran.

        ONE loop serves sequential and parallel scheduling (R7.3): the
        launch protocol (budget gate -> begin_attempt -> provisioning ->
        submit) appears exactly once. max_parallel <= 1 swaps the
        process pool for _InlineExecutor, which runs the worker
        synchronously in-process - the historical sequential behavior -
        behind the identical submit/wait/result flow.

        Manual executor lifecycle: on a backstop breach we must NOT wait
        for the (possibly hung) worker at shutdown, which the
        `with ProcessPoolExecutor(...)` form would do.
        """
        executor: ProcessPoolExecutor | _InlineExecutor
        if max_parallel <= 1:
            executor = _InlineExecutor()
        else:
            executor = ProcessPoolExecutor(max_workers=max_parallel)
        slots_cap = max(1, max_parallel)
        running_futures: dict[Future[ComponentResult], str] = {}
        future_deadlines: dict[Future[ComponentResult], float] = {}
        try:
            while True:
                if stop is not None and stop.is_set():
                    _abort_inflight(
                        executor,
                        running_futures,
                        pipeline,
                        ui,
                        stop,
                    )
                    return
                ready = manifest.get_ready_components()
                slots = slots_cap - len(running_futures)

                # Components transitioned WITHOUT a launch this pass
                # (budget gate, provisioning failure). When that happens
                # and nothing is running, the loop must re-derive the
                # ready set rather than stop - R3.1's scheduling gate
                # promises to fail every remaining pending component
                # loudly, and a provisioning failure must not strand
                # still-schedulable siblings.
                transitioned_without_launch = 0
                for comp in ready[:slots]:
                    # Budget ceilings and an untrustworthy plan-time
                    # scope: three conditions that fail the component
                    # loudly rather than spending an engineer loop on it.
                    if _refused_before_launch(pipeline, comp, run_scope):
                        transitioned_without_launch += 1
                        continue
                    pipeline.begin_attempt(comp)
                    manifest.save(manifest_path)
                    bus.emit(ComponentStarted(component=comp.id))
                    ui.info(f"  Starting: {comp.id}")

                    wt_path = _launch_component(comp)
                    if wt_path is None:
                        transitioned_without_launch += 1
                        continue

                    # Provisioning succeeded: the engineer phase starts
                    # immediately before submission. process_result closes
                    # normal exits; fail_scheduler_backstop closes timeouts.
                    bus.emit(
                        PhaseStarted(
                            component=comp.id,
                            phase="engineer",
                            attempt=comp.retries + 1,
                        )
                    )
                    args = _submit_args(comp, wt_path)

                    # R8 (P2-c): the usage snapshot is attempt-scoped by
                    # deletion, and the deletion happens HERE - before
                    # the worker exists - so an attempt cancelled during
                    # pool startup cannot salvage its predecessor's
                    # tokens on top of the ones already on the meter.
                    if _clear_partial_usage(usage_paths.engineer_usage(comp.id)):
                        pipeline.mark_usage_salvage_safe(comp.id)
                    else:
                        # A snapshot we could not delete may still hold
                        # the PREVIOUS attempt's totals, which
                        # process_result already counted. Refuse disk
                        # salvage for this attempt rather than risk
                        # counting them twice - losing an aborted
                        # attempt's spend understates a total; salvaging
                        # a stale one corrupts it.
                        pipeline.mark_usage_salvage_unsafe(comp.id)
                        ui.warn(
                            f"  Could not retire the previous usage "
                            f"snapshot for {comp.id}; disk salvage is "
                            f"disabled for this attempt"
                        )
                    if isinstance(executor, _InlineExecutor):
                        # In-process worker: no fd redirection (it would
                        # hijack the parent terminal), and each transcript
                        # line mirrors to the parent UI so sequential runs
                        # keep live engineer output. functools.partial
                        # binds kstrl.factory._run_component AT SUBMIT
                        # TIME, so tests patching it still intercept.
                        # mypy cannot prove the unknown-length *args
                        # tuple stops before the kwargs; _submit_args
                        # ends at token_budget by construction.
                        task = functools.partial(
                            _run_component,
                            *args,
                            # Keyword, NOT appended to args: the tuple
                            # ends at token_budget by construction (see
                            # above) and a positional extra silently
                            # lands on redirect_output.
                            base_branch=manifest.base_branch,
                            verify_config=engineer_verify,
                            redirect_output=False,  # type: ignore[misc]
                            live_line=functools.partial(
                                ui.stream_line,
                                "AI",
                            ),
                            stop_check=(stop.is_set if stop is not None else None),
                        )
                        future = executor.submit(task)
                    else:
                        # Bound through partial rather than passed as a
                        # submit() kwarg: Executor.submit types its own
                        # **kwargs, so a keyword meant for the worker
                        # reads as a duplicate of submit's. Picklable -
                        # _run_component is module-level and the base
                        # branch is a plain str.
                        future = executor.submit(
                            functools.partial(
                                _run_component,
                                *args,
                                # Same unprovable-*args limitation the
                                # inline branch annotates above.
                                base_branch=manifest.base_branch,  # type: ignore[misc]
                                verify_config=engineer_verify,
                            ),
                        )
                    running_futures[future] = comp.id
                    factory_result.scheduled.append(comp.id)
                    if backstop_seconds > 0:
                        future_deadlines[future] = time.monotonic() + backstop_seconds

                if not running_futures:
                    if transitioned_without_launch:
                        continue
                    break

                # Wait for the next completion, bounded by the nearest
                # backstop deadline. The worker enforces its own timeouts
                # (adapter kill + loop wall clock); this scheduler-side
                # deadline is the last line of defense when a worker hangs
                # outside those layers.
                wait_timeout = _next_backstop_wait(
                    running_futures,
                    future_deadlines,
                    time.monotonic(),
                )
                done, stopped = _wait_interruptible(
                    set(running_futures),
                    wait_timeout,
                    stop,
                )
                if stopped:
                    assert stop is not None
                    _abort_inflight(
                        executor,
                        running_futures,
                        pipeline,
                        ui,
                        stop,
                    )
                    return

                if done:
                    # Preserve pre-R0.1 semantics: process one completion
                    # per pass so freed slots are refilled promptly.
                    future = next(iter(done))
                    comp_id = running_futures.pop(future)
                    future_deadlines.pop(future, None)
                    try:
                        comp_result = future.result()
                    except Exception as exc:
                        comp_result = ComponentResult(
                            component_id=comp_id,
                            success=False,
                            error=str(exc),
                        )
                    pipeline.process_result(comp_id, comp_result)
                    continue

                # Nothing completed inside the window: fail every
                # component past its backstop deadline and keep going.
                now = time.monotonic()
                for future in _expired_futures(
                    running_futures,
                    future_deadlines,
                    now,
                ):
                    comp_id = running_futures.pop(future)
                    future_deadlines.pop(future, None)
                    leaked_component_ids.add(comp_id)
                    pipeline.fail_scheduler_backstop(comp_id, backstop_seconds)
        finally:
            if leaked_component_ids:
                ui.warn(
                    "Shutting down worker pool without waiting: "
                    f"{len(leaked_component_ids)} worker(s) may still be running"
                )
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=True)

    def _cleanup_pass_worktrees() -> None:
        """Remove component worktrees left behind by a scheduling pass.

        With ``keep_worktrees_on_failure`` (R3.3), a FAILED component's
        worktree is kept and recorded as its evidence pointer instead of
        removed, so the failure summary can point the operator at it.
        """
        if not factory_config.use_worktrees:
            return
        ui.section("Factory: Cleanup")
        kept_evidence = False
        for comp_id in worktree_paths:
            if comp_id in leaked_component_ids:
                # A possibly-live worker still owns this worktree; removing
                # it under the worker risks corrupting the main repo's
                # worktree metadata.
                ui.warn(f"  Keeping worktree for '{comp_id}' (leaked worker may still be running)")
                continue
            comp = manifest.get_component(comp_id)
            if (
                factory_config.keep_worktrees_on_failure
                and comp is not None
                and comp.status == ComponentStatus.FAILED.value
            ):
                comp.evidence_worktree = str(worktree_paths[comp_id])
                kept_evidence = True
                ui.info(f"  Keeping failed worktree for post-mortem: {worktree_paths[comp_id]}")
                continue
            _cleanup_worktree(comp_id, root_dir, run_id)
        if kept_evidence:
            manifest.save(manifest_path)
        # Drop the run's now-empty worktree dir; leaked workers' and
        # failed components' kept worktrees leave it non-empty and it
        # stays for the next run's prune pass (which preserves recorded
        # evidence worktrees of still-FAILED components).
        try:
            os.rmdir(root_dir / ".kstrl" / "worktrees" / run_id)
        except OSError:
            pass
        ui.ok("Worktrees cleaned up")

    def _record_contract_event(cr: ContractResult) -> None:
        """Append a contract_result event to the evolution journal.

        Written for pass AND fail (R0.3): the journal is the audit trail
        for every contract phase outcome, including intermediate failures
        that a breaker retry later resolves. Non-fatal on I/O errors,
        matching EvolutionJournal.record_run.
        """
        from kstrl.evolution import JOURNAL_SCHEMA_VERSION, EvolutionJournal

        # R2.1: honor [evolution] in kstrl.toml + env, resolved against
        # the factory root rather than whatever the process CWD is.
        journal = EvolutionJournal.open(root_dir, warn=ui.warn)
        if journal is None:
            return
        entry = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_id": run_id,
            "project": manifest.project_name,
            "component_id": cr.breaker or "",
            "event_type": "contract_result",
            "tier": cr.tier,
            "passed": cr.passed,
            "breaker": cr.breaker,
            "components_tested": cr.components_tested,
            "test_output": cr.test_output[:2000],
            "duration_seconds": round(cr.duration_seconds, 2),
        }
        try:
            journal.append_entries([entry])
        except OSError as exc:
            # Evolution recording is non-fatal, but never silent (R6.1).
            ui.warn(f"  Evolution journal write failed (non-fatal): {exc}")

    # Per-component PRs are squash-merged into base as each component
    # completes, so at contract time tier re-merges would be content
    # no-ops and blame attribution would be meaningless: the contract
    # phase instead tests the integrated base branch (R0.3). single_pr
    # defers its one PR until after the contract phase, so it stays in
    # deferred-merge (tier merge + bisection) mode.
    components_merged = factory_config.create_prs and not factory_config.single_pr

    # R0.3: scheduling + contract testing form one outer loop so a
    # contract breaker reset to PENDING actually re-enters scheduling.
    # Termination: every reset consumes one of the breaker's bounded
    # retries, and any pass without a reset breaks out.
    while True:
        _run_scheduling_pass()
        _cleanup_pass_worktrees()

        if stop is not None and stop.is_set():
            ui.warn(f"  Run stopped: {stop.reason}")
            break

        # PHASE 3: Contract testing
        contract_config = factory_config.contract_config
        if contract_config is None or contract_config.mode == ContractMode.SKIP.value:
            break

        try:
            contract_results = run_contract_testing(
                manifest,
                root_dir,
                contract_config,
                ui,
                components_merged=components_merged,
            )
        except ContractCleanupError as exc:
            # A contract temp worktree survived removal. The user's
            # checkout is untouched, but .kstrl/contract holds stale
            # state - fail the run loudly instead of continuing.
            ui.err(f"  Contract cleanup FAILED: {exc}")
            factory_result.contract_failures.append(f"contract cleanup failed: {exc}")
            break

        for cr in contract_results:
            bus.emit(
                ContractResultEvent(
                    tier=cr.tier,
                    passed=cr.passed,
                    breaker=cr.breaker,
                    duration_seconds=round(cr.duration_seconds, 2),
                )
            )
            _record_contract_event(cr)

        failures = [cr for cr in contract_results if not cr.passed]
        if not failures:
            break

        # Reset retryable breakers to PENDING; the outer loop then
        # re-enters scheduling so the promised retry actually runs.
        any_breaker_reset = False
        for cr in failures:
            if not cr.breaker:
                continue
            breaker = manifest.get_component(cr.breaker)
            if breaker and breaker.retries < factory_config.max_retries:
                # R3.3: the completed attempt's findings are superseded
                # by the contract-triggered re-run; journal them before
                # the retry increments the attempt counter.
                pipeline.journal_superseded_findings(breaker)
                breaker.retries += 1
                breaker.status = ComponentStatus.PENDING.value
                breaker.error = f"Contract test failed at tier {cr.tier}"
                component_failure_signatures[cr.breaker] = [
                    f"contract:tier_{cr.tier}",
                ]
                # Remove from completed list
                if cr.breaker in factory_result.completed:
                    factory_result.completed.remove(cr.breaker)
                ctx = IterationContext.from_json(component_contexts.get(cr.breaker, "{}"))
                # breaker.retries was already incremented above, so it
                # now names the attempt whose contract test failed, not
                # the next one. (Issue #223's table says retries + 1;
                # that holds at the pipeline sites, where the increment
                # happens inside retry_or_fail AFTER the entry is
                # recorded. Here it would be off by one.)
                ctx.add_contract_failure(
                    cr.test_output[:500],
                    attempt=breaker.retries,
                )
                component_contexts[cr.breaker] = ctx.to_json()
                manifest.save(manifest_path)
                any_breaker_reset = True
                ui.warn(f"  Contract breaker '{cr.breaker}' sent back for retry")

        if any_breaker_reset:
            continue

        # Terminal contract failure: nothing left to retry. Record it in
        # the run result so the summary shows it and the exit code is
        # nonzero (previously this fell through silently and the run
        # exited 0 with broken integrated code).
        for cr in failures:
            detail = (cr.test_output or "").strip()
            summary_line = detail.splitlines()[-1][:200] if detail else ""
            if cr.breaker:
                breaker = manifest.get_component(cr.breaker)
                if breaker is not None:
                    breaker.status = ComponentStatus.FAILED.value
                    breaker.error = f"Contract test failed at tier {cr.tier} (retries exhausted)"
                    breaker.completed_at = _iso_now()
                    breaker.failed_phase = "contract"
                    breaker.failed_check = f"tier_{cr.tier}"
                    component_failure_signatures[cr.breaker] = [
                        f"contract:tier_{cr.tier}",
                    ]
                if cr.breaker in factory_result.completed:
                    factory_result.completed.remove(cr.breaker)
                if cr.breaker not in factory_result.failed:
                    factory_result.failed.append(cr.breaker)
                bus.emit(
                    ComponentFailed(
                        component=cr.breaker,
                        error=(f"Contract test failed at tier {cr.tier} (retries exhausted)"),
                    )
                )
                notify.fire_first_failure(
                    cr.breaker,
                    f"Contract test failed at tier {cr.tier} (retries exhausted)",
                )
                factory_result.contract_failures.append(
                    f"tier {cr.tier}: breaker '{cr.breaker}' (retries exhausted): {summary_line}"
                )
            else:
                factory_result.contract_failures.append(
                    f"tier {cr.tier}: contract tests failed, no blame "
                    f"attributed (components: "
                    f"{', '.join(cr.components_tested)}): {summary_line}"
                )
            ui.err(f"  Contract failure recorded for tier {cr.tier}; run will exit nonzero")
        manifest.save(manifest_path)
        break

    # Create PRs for any remaining components that weren't handled per-component
    # (e.g. single-pr mode, or stragglers from parallel execution)
    if factory_config.create_prs:
        if factory_config.single_pr:
            result = create_single_pr(manifest, root_dir, ui)
            if result:
                factory_result.pr_urls.append(result[1])
            manifest.save(manifest_path)
        else:
            # Per-component PRs are created in _handle_result; only handle stragglers
            remaining = [c for c in manifest.components if c.status == "completed" and not c.pr_url]
            if remaining:
                pr_results = create_prs_in_order(manifest, root_dir, ui)
                factory_result.pr_urls.extend(url for _, url in pr_results)
                manifest.save(manifest_path)

    # Summary
    factory_duration = time.monotonic() - factory_start
    bus.emit(
        RunCompleted(
            completed=len(factory_result.completed),
            failed=len(factory_result.failed),
            skipped=len(factory_result.skipped),
            duration_seconds=round(factory_duration, 2),
        )
    )
    # Detach (not close) the console bus: post-run cli narration must
    # not reopen the run's files. The file sinks themselves close.
    for _sink in run_file_sinks:
        bus.remove_sink(_sink)
        _sink.close()

    # R0.2: collect components parked awaiting merge confirmation. Built
    # from the manifest (not accumulated during the run) so it reflects
    # the final state after any crash-recovery re-poll.
    factory_result.merge_pending = [
        c.id for c in manifest.components if c.status == ComponentStatus.MERGE_PENDING.value
    ]

    ui.section("Factory: Summary")
    ui.kv("Completed", str(len(factory_result.completed)))
    ui.kv("Failed", str(len(factory_result.failed)))
    ui.kv("Skipped", str(len(factory_result.skipped)))
    # R3.3 failure summary: per failed component, which gate fired and
    # where the last attempt's evidence lives, so the run is diagnosable
    # without reading raw JSON.
    if factory_result.failed:
        ui.subsection("Failure summary")
        for failed_id in factory_result.failed:
            failed_comp = manifest.get_component(failed_id)
            if failed_comp is None:
                continue
            ui.err(
                f"  {failed_id}: "
                f"phase={failed_comp.failed_phase or 'unknown'} "
                f"check={failed_comp.failed_check or 'unknown'} "
                f"(attempt {failed_comp.retries + 1})"
            )
            if failed_comp.error:
                ui.info(f"    error: {failed_comp.error[:160]}")
            if failed_comp.evidence_worktree:
                ui.info(f"    worktree: {failed_comp.evidence_worktree}")
            elif factory_config.use_worktrees:
                ui.info(
                    "    worktree: removed (re-run with --keep-worktrees-on-failure to keep it)"
                )
            if failed_comp.evidence_debug_dir:
                ui.info(f"    raw outputs: {failed_comp.evidence_debug_dir}")
            if failed_comp.journal_offset_start >= 0 and journal_path is not None:
                end = (
                    str(failed_comp.journal_offset_end)
                    if failed_comp.journal_offset_end >= 0
                    else "end"
                )
                ui.info(
                    f"    journal: {journal_path} bytes [{failed_comp.journal_offset_start}:{end}]"
                )
            ui.info(f"    retry with: ks retry {failed_id}")
    if factory_result.contract_failures:
        ui.kv("Contract failures", str(len(factory_result.contract_failures)))
        for line in factory_result.contract_failures:
            ui.err(f"  {line}")
    if factory_result.merge_pending:
        ui.kv("Merge pending", str(len(factory_result.merge_pending)))
    ui.kv("Duration", f"{factory_duration:.0f}s")
    # R3.1 usage rollup: per component, per phase, plus the run total.
    print_usage_rollup(
        ui,
        pipeline.usage_meter,
        pipeline.run_usage,
        title="Usage rollup",
    )
    if pipeline.token_budget_exceeded():
        ui.err(
            f"TOKEN BUDGET EXCEEDED: {pipeline.run_usage.total_tokens} total tokens "
            f"recorded >= max_total_tokens ({factory_config.max_total_tokens})"
        )
    if pipeline.cost_budget_exceeded():
        ui.err(
            f"COST BUDGET EXCEEDED: ${pipeline.run_usage.cost_usd:.6f} recorded "
            f">= max_cost_usd (${factory_config.max_cost_usd})"
        )
    if factory_result.pr_urls:
        ui.kv("PRs created", str(len(factory_result.pr_urls)))
        for url in factory_result.pr_urls:
            ui.info(f"  {url}")
    if factory_result.merge_pending:
        ui.warn(
            "Some PR merges are unconfirmed; re-run the factory to "
            "re-poll them: " + ", ".join(factory_result.merge_pending)
        )

    factory_result.exit_code = resolve_exit_code(
        factory_result,
        manifest,
        ui,
        stopped=stop is not None and stop.is_set(),
    )

    # R3.3: the run reached its terminal state; stamp the manifest so a
    # resume (and Linear, later) can tell a finished run from a crash.
    # Stamped BEFORE the completion notification so a hook that reads
    # the manifest sees the terminal state.
    manifest.completed_at = _iso_now()
    manifest.save(manifest_path)

    # R8.2: fold this run's outcome into the ladder. Without this the
    # counters never move, promotion is unreachable except by --force,
    # and the promised automatic demotion never fires - the state machine
    # would be real but inert. Runs INSIDE the factory lock, before it is
    # released, so two runs cannot interleave read-modify-write.
    if autonomy_active:
        try:
            _record_autonomy_outcome(
                root_dir=root_dir,
                manifest=manifest,
                factory_result=factory_result,
                autonomy_config=autonomy_config,
                bus=bus,
                run_id=run_id,
                ui=ui,
            )
        except Exception as exc:  # noqa: BLE001 - never fail a run on bookkeeping
            ui.warn(f"Autonomy state update failed (non-fatal): {exc}")

    # R3.2: run-end notification. Fires on every run that reached the
    # summary, whatever the outcome; early refusals (invalid DAG, held
    # lock, stale branches) never notify because no work was started.
    notify.fire_complete(
        f"completed={len(factory_result.completed)} "
        f"failed={len(factory_result.failed)} "
        f"skipped={len(factory_result.skipped)} "
        f"merge_pending={len(factory_result.merge_pending)} "
        f"exit_code={factory_result.exit_code}"
    )

    # Record run to evolution journal
    try:
        from kstrl.evolution import EvolutionConfig, EvolutionJournal

        evo_config = EvolutionConfig.load(root_dir)
        if evo_config.enabled:
            journal = EvolutionJournal(evo_config)
            journal.record_run(
                run_id,
                manifest,
                factory_result,
                usage_by_component={
                    comp_id: {phase: totals.to_dict() for phase, totals in phases.items()}
                    for comp_id, phases in pipeline.usage_meter.items()
                },
                run_usage=pipeline.run_usage.to_dict(),
                failure_signatures=component_failure_signatures,
                fact_utilization={
                    comp_id: util.to_dict() for comp_id, util in pipeline.fact_utilization.items()
                },
            )
    except Exception as exc:
        # Evolution recording is non-fatal, but never silent (R6.1).
        ui.warn(f"Evolution journal recording failed (non-fatal): {exc}")

    return factory_result
