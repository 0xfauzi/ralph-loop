"""R8.2 autonomy ladder: earned, bounded, revocable autonomy (L1-L4).

Autonomy today is a scatter of independent flags (``pause_before_pr_merge``,
``review_mode``, ``deps_allow_new``, deploy). Any of them can be flipped in
isolation, so "how much is this factory allowed to do without me?" has no
single answer and no audit record. This module makes autonomy one ordered,
named level with a derived flag bundle, so the question has exactly one
answer at any moment and every change of that answer is journaled.

The shape is borrowed from continuous-authorization practice, not invented
here: autonomy is **earned** (entry criteria backed by evidence), **bounded**
(the R8.1 policy envelope defines what even L3 may touch), **continuously
monitored**, and **revocable** with automatic reversion to human-gated mode.

Three invariants carry the trust:

1. **Agents cannot promote themselves.** Promotion requires evidence, a
   named actor, an ack, AND an interactive terminal - a signal an
   unattended agent subprocess does not have (``--actor``/``--ack`` are
   just strings any caller could pass, so they prove nothing alone). The
   live state file lives outside the agent-reachable tree (XDG control
   dir, R8.9); the legacy in-tree path and this module remain in the
   R8.1 enforcement-machinery halt set so a diff cannot rewrite either.
   L3+ additionally refuses to proceed while control state still
   resolves in-tree (migration incomplete or ``XDG_STATE_HOME`` under
   the repo).
2. **Fast down, slow up.** Demotion is automatic and immediate on a trigger;
   re-promotion is locked for a cool-down period afterwards.
3. **The flag bundle is derived, never stored.** It is computed from the
   level at run start, so editing a flag by hand cannot silently grant
   autonomy the ladder never awarded - and a config that contradicts the
   level is recorded as a manual override rather than honored in silence.

Opt-in (``[autonomy] enabled = false``): L1 is stricter than today's
defaults (it forces the merge gate on), so enabling the ladder must be a
deliberate act rather than a surprise upgrade for existing repos.

**Every threshold in this module is an UNMEASURED PLACEHOLDER** (the R8
"no assumed thresholds" rule). ``kstrl.autonomy_replay`` replays them
against historical run data and reports what would have fired; until that
output is recorded in ``docs/dark-factory-roadmap.md``, no threshold here
should be trusted to gate anything.
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.atomicio import atomic_write_json
from kstrl.statedir import (
    CONTROL_AUTONOMY,
    ControlStateError,
    control_file,
    control_is_external,
    control_lock,
    ensure_control_state,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kstrl.events import EventBus
    from kstrl.ui.base import UI

STATE_SCHEMA_VERSION = 1


class AutonomyLevel(IntEnum):
    """Ordered autonomy levels, defined by the human's remaining role."""

    L1_SUPERVISED = 1  # human approves the plan AND the merge
    L2_GATED_MERGE = 2  # plans auto-accepted; human still gates merge
    L3_ENVELOPED_AUTO = 3  # merge auto when fully green AND inside envelope
    L4_DEPLOY = 4  # L3 plus the release stage (R8.7)

    @property
    def label(self) -> str:
        return {
            AutonomyLevel.L1_SUPERVISED: "L1 Supervised",
            AutonomyLevel.L2_GATED_MERGE: "L2 Gated-merge",
            AutonomyLevel.L3_ENVELOPED_AUTO: "L3 Enveloped auto-merge",
            AutonomyLevel.L4_DEPLOY: "L4 Deploy",
        }[self]


class DemotionTrigger(IntEnum):
    """Why a level was revoked. Each demotion drops exactly one level."""

    POLICY_VIOLATION = 1  # R8.1 envelope breach
    CALIBRATION_REGRESSION = 2  # adversarial detection rate fell
    HEALTH_BREACH = 3  # R8.4 control-limit breach
    HUMAN_REJECTED_AUTO_MERGE = 4  # a human rejected an L3 candidate
    MANUAL = 5  # operator demoted by hand

    @property
    def label(self) -> str:
        return self.name.lower()


# ---------------------------------------------------------------------------
# Thresholds - ALL UNMEASURED PLACEHOLDERS (R8 "no assumed thresholds")
# ---------------------------------------------------------------------------
# Every number below is a guess taken from the roadmap table. None has been
# replayed against real run data yet, and with the data on hand (see
# `ks autonomy replay`) none can be. They live together, named, so the
# replay tool can report on them and a future measured value replaces one
# constant rather than a scattered literal.

#: Components that must merge cleanly at L1 before L2 is offered.
L2_MERGED_COMPONENTS_REQUIRED = 5
#: Consecutive L2 merges approved without human edits before L3 is offered.
L3_CLEAN_MERGES_REQUIRED = 15
#: Components merged while holding L3 before L4 is offered.
L4_MERGED_COMPONENTS_REQUIRED = 30
#: Decisive runs during which re-promotion is locked after any demotion.
#: "Fast down, slow up" - the cool-down is the slow part.
DEMOTION_COOLDOWN_RUNS = 10
#: Minimum decisive runs before ANY automatic transition may fire. Guards
#: against demotion flapping on small-sample noise.
MIN_DECISIVE_RUNS = 8

#: Every threshold constant, for the replay tool and `ks autonomy status`.
THRESHOLDS: dict[str, int] = {
    "L2_MERGED_COMPONENTS_REQUIRED": L2_MERGED_COMPONENTS_REQUIRED,
    "L3_CLEAN_MERGES_REQUIRED": L3_CLEAN_MERGES_REQUIRED,
    "L4_MERGED_COMPONENTS_REQUIRED": L4_MERGED_COMPONENTS_REQUIRED,
    "DEMOTION_COOLDOWN_RUNS": DEMOTION_COOLDOWN_RUNS,
    "MIN_DECISIVE_RUNS": MIN_DECISIVE_RUNS,
}


class AutonomyError(RuntimeError):
    """A ladder transition was refused (criteria unmet, cool-down active,
    missing ack). Raised rather than returned so no caller can ignore a
    refusal and proceed as though autonomy had been granted."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _warn_rejected_state(path: Path, reason: str) -> str:
    """Warn that a ladder state was rejected; return the same sentence.

    Returned as well as warned so ``AutonomyState.load`` can put the
    identical text on the transient ``degraded_reason`` field. One
    string, two consumers: a second copy of the wording could drift from
    the warning an operator actually sees.
    """
    message = f"autonomy: rejected ladder state {path} ({reason}); failing closed to L1 Supervised"
    warnings.warn(message, RuntimeWarning, stacklevel=3)
    return message


def _require_int(data: dict[str, Any], key: str, default: int) -> int:
    """Read an int field strictly: a wrong TYPE is a rejection, not a coerce.

    ``bool`` is excluded explicitly - it is an int subclass in Python, and
    silently reading ``true`` as ``1`` would launder a malformed record
    into a plausible-looking counter.
    """
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer, got {type(value).__name__}")
    return value


def _parse_history(raw: Any) -> list[Transition]:
    """Parse the transition history, rejecting any malformed entry."""
    if not isinstance(raw, list):
        raise ValueError("history must be an array")
    history: list[Transition] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("history entries must be objects")
        evidence = item.get("evidence", {}) or {}
        if not isinstance(evidence, dict):
            raise ValueError("history evidence must be an object")
        for text_key in ("at", "direction", "actor", "reason", "trigger"):
            if text_key in item and not isinstance(item[text_key], str):
                raise ValueError(f"history {text_key} must be a string")
        history.append(
            Transition(
                at=str(item.get("at", "")),
                from_level=_require_int(item, "from_level", 1),
                to_level=_require_int(item, "to_level", 1),
                direction=str(item.get("direction", "")),
                actor=str(item.get("actor", "")),
                reason=str(item.get("reason", "")),
                trigger=str(item.get("trigger", "")),
                evidence=evidence,
            )
        )
    return history


@dataclass(frozen=True)
class FlagBundle:
    """The permissions a level grants, derived fresh at run start.

    Never persisted: storing it would let the stored copy drift from the
    level that justified it. ``deps_allow_new_permitted`` and
    ``deploy_permitted`` are ceilings the R8.1 envelope and R8.7 release
    config must also agree to - the ladder can only ever withhold
    permission, never grant something those gates deny.
    """

    level: AutonomyLevel
    pause_before_pr_merge: bool
    review_mode: str
    auto_accept_plan: bool
    deps_allow_new_permitted: bool
    auto_merge_when_green: bool
    deploy_permitted: bool

    def describe(self) -> list[str]:
        return [
            f"merge gate: {'ON (human approves)' if self.pause_before_pr_merge else 'off'}",
            f"review mode: {self.review_mode}",
            f"plans: {'auto-accepted' if self.auto_accept_plan else 'human-approved'}",
            f"new dependencies: {'permitted' if self.deps_allow_new_permitted else 'blocked'}",
            f"auto-merge when green: {'yes' if self.auto_merge_when_green else 'no'}",
            f"deploy: {'permitted' if self.deploy_permitted else 'blocked'}",
        ]


def flag_bundle_for(level: AutonomyLevel) -> FlagBundle:
    """Derive the flag bundle a level grants.

    L1 is deliberately stricter than the harness defaults (merge gate ON,
    hard review): the ladder's floor is "human approves everything", not
    "whatever the config happened to say".
    """
    if level is AutonomyLevel.L1_SUPERVISED:
        return FlagBundle(
            level=level,
            pause_before_pr_merge=True,
            review_mode="hard",
            auto_accept_plan=False,
            deps_allow_new_permitted=False,
            auto_merge_when_green=False,
            deploy_permitted=False,
        )
    if level is AutonomyLevel.L2_GATED_MERGE:
        return FlagBundle(
            level=level,
            pause_before_pr_merge=True,
            review_mode="hard",
            auto_accept_plan=True,
            deps_allow_new_permitted=False,
            auto_merge_when_green=False,
            deploy_permitted=False,
        )
    if level is AutonomyLevel.L3_ENVELOPED_AUTO:
        return FlagBundle(
            level=level,
            pause_before_pr_merge=False,
            review_mode="hard",
            auto_accept_plan=True,
            deps_allow_new_permitted=True,
            auto_merge_when_green=True,
            deploy_permitted=False,
        )
    return FlagBundle(
        level=AutonomyLevel.L4_DEPLOY,
        pause_before_pr_merge=False,
        review_mode="hard",
        auto_accept_plan=True,
        deps_allow_new_permitted=True,
        auto_merge_when_green=True,
        deploy_permitted=True,
    )


@dataclass(frozen=True)
class Transition:
    """One recorded level change. Append-only history."""

    at: str
    from_level: int
    to_level: int
    direction: str  # "promote" | "demote"
    actor: str  # human identity for promotions; "system" for auto
    reason: str
    trigger: str = ""  # DemotionTrigger label, demotions only
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class AutonomyState:
    """Persisted ladder state (``.kstrl/autonomy.json``).

    Counters are per-level and reset on every transition: evidence earned
    at one level does not carry into the next, and a demotion does not
    leave a half-full promotion counter behind.
    """

    level: int = int(AutonomyLevel.L1_SUPERVISED)
    since: str = field(default_factory=_utc_now_iso)
    components_merged_at_level: int = 0
    clean_merges_at_level: int = 0
    policy_violations_at_level: int = 0
    decisive_runs_at_level: int = 0
    #: Decisive runs still to elapse before re-promotion is allowed.
    cooldown_runs_remaining: int = 0
    last_promoted_by: str = ""
    history: list[Transition] = field(default_factory=list)
    #: Why ``load`` discarded the stored record and fell back to a fresh
    #: L1 state, or None when nothing was discarded. TRANSIENT: set by
    #: ``load`` on its fail-closed paths, never parsed from disk, and
    #: never written by ``save`` (whose payload is built key by key, not
    #: from ``asdict``). It exists so ``safemode.safe_mode_reasons`` can
    #: report the fallback without re-implementing the parser that
    #: detected it.
    degraded_reason: str | None = None

    @property
    def autonomy_level(self) -> AutonomyLevel:
        return AutonomyLevel(self.level)

    def flag_bundle(self) -> FlagBundle:
        return flag_bundle_for(self.autonomy_level)

    # -- persistence -------------------------------------------------------
    @classmethod
    def path_for(cls, root_dir: Path) -> Path:
        return control_file(root_dir, CONTROL_AUTONOMY)

    @classmethod
    def load(cls, root_dir: Path) -> AutonomyState:
        """Read state, failing CLOSED to L1 on anything unrecognizable.

        Every field is validated, not just the level: a syntactically
        valid file carrying ``"components_merged_at_level": "not-an-int"``
        must not crash `ks autonomy status` or an enabled factory run.
        Any malformed field, history entry, or out-of-range level
        discards the whole file and returns a fresh L1 state, because the
        safe direction for unknown autonomy is the LEAST autonomy - never
        a partially-trusted level assembled from a damaged record.

        The rejection is warned about rather than silent (mirroring
        ``knowledge.read_facts``): losing an earned level deserves a note.
        It is also recorded on the returned state's transient
        ``degraded_reason``, because a warning is only seen by whoever
        was watching the terminal at the time, and ``ks status`` needs to
        answer the question afterwards.

        A missing file is NOT a fallback: that is first run, and
        ``degraded_reason`` stays None.
        """
        ensure_control_state(root_dir)
        path = cls.path_for(root_dir)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return cls(
                degraded_reason=_warn_rejected_state(
                    path,
                    f"unreadable: {exc}",
                ),
            )
        except UnicodeDecodeError as exc:
            # Its own reason string, because this one is surfaced: it
            # reaches the warning AND ``ks status`` through
            # ``degraded_reason``, and "unreadable" would send the
            # operator to the file's permissions when the permissions
            # are fine and the bytes are not. Failing to L1 is unchanged;
            # only the explanation is.
            return cls(
                degraded_reason=_warn_rejected_state(
                    path,
                    f"not valid UTF-8: {exc}",
                ),
            )
        if not isinstance(data, dict):
            return cls(
                degraded_reason=_warn_rejected_state(
                    path,
                    "top-level value is not an object",
                ),
            )
        try:
            level = int(AutonomyLevel(_require_int(data, "level", 1)))
            history = _parse_history(data.get("history", []))
            since = data.get("since", "")
            if not isinstance(since, str):
                raise ValueError("since must be a string")
            promoted_by = data.get("last_promoted_by", "")
            if not isinstance(promoted_by, str):
                raise ValueError("last_promoted_by must be a string")
            state = cls(
                level=level,
                since=since or _utc_now_iso(),
                components_merged_at_level=_require_int(
                    data,
                    "components_merged_at_level",
                    0,
                ),
                clean_merges_at_level=_require_int(data, "clean_merges_at_level", 0),
                policy_violations_at_level=_require_int(
                    data,
                    "policy_violations_at_level",
                    0,
                ),
                decisive_runs_at_level=_require_int(data, "decisive_runs_at_level", 0),
                cooldown_runs_remaining=_require_int(
                    data,
                    "cooldown_runs_remaining",
                    0,
                ),
                last_promoted_by=promoted_by,
                history=history,
            )
        except (ValueError, TypeError) as exc:
            return cls(degraded_reason=_warn_rejected_state(path, str(exc)))
        return state

    def save(self, root_dir: Path) -> None:
        """Atomic write, through the one helper that owns the pattern (#291)."""
        ensure_control_state(root_dir)
        path = self.path_for(root_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "level": self.level,
            "since": self.since,
            "components_merged_at_level": self.components_merged_at_level,
            "clean_merges_at_level": self.clean_merges_at_level,
            "policy_violations_at_level": self.policy_violations_at_level,
            "decisive_runs_at_level": self.decisive_runs_at_level,
            "cooldown_runs_remaining": self.cooldown_runs_remaining,
            "last_promoted_by": self.last_promoted_by,
            "history": [asdict(h) for h in self.history],
        }
        with control_lock(root_dir):
            atomic_write_json(path, payload)

    # -- transitions -------------------------------------------------------
    def _reset_level_counters(self) -> None:
        self.components_merged_at_level = 0
        self.clean_merges_at_level = 0
        self.policy_violations_at_level = 0
        self.decisive_runs_at_level = 0

    def promotion_blockers(self, target: AutonomyLevel | None = None) -> list[str]:
        """Unmet criteria for the next level; empty means eligible.

        Returns human-readable reasons rather than a bool so `ks autonomy
        status` can show exactly what is missing and by how much.
        """
        current = self.autonomy_level
        target = target or AutonomyLevel(min(int(current) + 1, int(AutonomyLevel.L4_DEPLOY)))
        blockers: list[str] = []
        if int(target) <= int(current):
            blockers.append(f"already at {current.label}")
            return blockers
        if int(target) > int(current) + 1:
            blockers.append(f"cannot skip levels: {current.label} -> {target.label}")
        if self.cooldown_runs_remaining > 0:
            blockers.append(
                f"demotion cool-down active: {self.cooldown_runs_remaining} "
                "more decisive run(s) required"
            )
        if self.decisive_runs_at_level < MIN_DECISIVE_RUNS:
            blockers.append(
                f"insufficient evidence: {self.decisive_runs_at_level} decisive "
                f"run(s) at this level, need {MIN_DECISIVE_RUNS}"
            )
        if self.policy_violations_at_level:
            blockers.append(
                f"{self.policy_violations_at_level} policy violation(s) at this level; need zero"
            )
        if target is AutonomyLevel.L2_GATED_MERGE:
            if self.components_merged_at_level < L2_MERGED_COMPONENTS_REQUIRED:
                blockers.append(
                    f"{self.components_merged_at_level}/"
                    f"{L2_MERGED_COMPONENTS_REQUIRED} components merged at L1"
                )
        elif target is AutonomyLevel.L3_ENVELOPED_AUTO:
            if self.clean_merges_at_level < L3_CLEAN_MERGES_REQUIRED:
                blockers.append(
                    f"{self.clean_merges_at_level}/{L3_CLEAN_MERGES_REQUIRED} "
                    "consecutive merges approved without edits"
                )
        elif target is AutonomyLevel.L4_DEPLOY:
            if self.components_merged_at_level < L4_MERGED_COMPONENTS_REQUIRED:
                blockers.append(
                    f"{self.components_merged_at_level}/"
                    f"{L4_MERGED_COMPONENTS_REQUIRED} components merged while "
                    "holding L3"
                )
        return blockers

    def promote(
        self,
        actor: str,
        ack: str,
        *,
        force: bool = False,
        evidence: dict[str, Any] | None = None,
    ) -> Transition:
        """Raise one level. Requires a human actor AND an explicit ack.

        There is deliberately no unattended path into this method: an empty
        ``actor`` or ``ack`` raises. ``force`` records an override of unmet
        criteria - it still demands the ack, and the override is written
        into the transition's evidence so the audit trail shows the
        criteria were bypassed rather than met.
        """
        if not actor.strip():
            raise AutonomyError("promotion requires an actor: agents cannot promote themselves")
        if not ack.strip():
            raise AutonomyError("promotion requires an explicit acknowledgement of the evidence")
        current = self.autonomy_level
        if current is AutonomyLevel.L4_DEPLOY:
            raise AutonomyError("already at the highest level (L4 Deploy)")
        target = AutonomyLevel(int(current) + 1)
        blockers = self.promotion_blockers(target)
        if blockers and not force:
            raise AutonomyError(
                f"cannot promote {current.label} -> {target.label}: " + "; ".join(blockers)
            )
        record = Transition(
            at=_utc_now_iso(),
            from_level=int(current),
            to_level=int(target),
            direction="promote",
            actor=actor,
            reason=ack,
            evidence={
                **(evidence or {}),
                "components_merged_at_level": self.components_merged_at_level,
                "clean_merges_at_level": self.clean_merges_at_level,
                "decisive_runs_at_level": self.decisive_runs_at_level,
                **({"forced_over_blockers": blockers} if blockers else {}),
            },
        )
        self.level = int(target)
        self.since = record.at
        self.last_promoted_by = actor
        self._reset_level_counters()
        self.history.append(record)
        return record

    def demote(
        self,
        trigger: DemotionTrigger,
        reason: str,
        *,
        actor: str = "system",
        evidence: dict[str, Any] | None = None,
    ) -> Transition | None:
        """Drop exactly one level and start the cool-down.

        Returns None at L1 (nothing below it) so a repeated trigger at the
        floor is a no-op rather than an error - the floor is already the
        safe state. Unlike promotion this needs no ack: revoking autonomy
        must never wait on a human.
        """
        current = self.autonomy_level
        if current is AutonomyLevel.L1_SUPERVISED:
            return None
        target = AutonomyLevel(int(current) - 1)
        record = Transition(
            at=_utc_now_iso(),
            from_level=int(current),
            to_level=int(target),
            direction="demote",
            actor=actor,
            reason=reason,
            trigger=trigger.label,
            evidence=evidence or {},
        )
        self.level = int(target)
        self.since = record.at
        self._reset_level_counters()
        self.cooldown_runs_remaining = DEMOTION_COOLDOWN_RUNS
        self.history.append(record)
        return record

    # -- evidence accumulation --------------------------------------------
    def record_decisive_run(self, count: int = 1) -> None:
        """Count a run that produced a verdict, and burn down the cool-down.

        Infra-aborted runs are NOT decisive: a run that died on a git push
        is not evidence about the factory's judgement, and counting it
        would let a string of broken runs unlock a promotion.
        """
        self.decisive_runs_at_level += count
        if self.cooldown_runs_remaining > 0:
            self.cooldown_runs_remaining = max(
                0,
                self.cooldown_runs_remaining - count,
            )

    def record_merged_component(self, *, human_edited: bool = False) -> None:
        """Count a merged component; edits break the clean-merge streak."""
        self.components_merged_at_level += 1
        if human_edited:
            self.clean_merges_at_level = 0
        else:
            self.clean_merges_at_level += 1

    def record_policy_violation(self, count: int = 1) -> None:
        self.policy_violations_at_level += count


def _strict_bool(section: Mapping[str, Any], key: str, default: bool) -> bool:
    """Read one ``[autonomy]`` boolean, refusing anything that is not one.

    ``bool("false")`` is True, so the ``bool(section[key])`` reading this
    package uses everywhere else arms a switch the operator wrote
    ``"false"`` against. The two keys that use this one revoke autonomy,
    and a typo that ARMS a safety switch is worse than one that disarms
    it, so a non-boolean is named and refused rather than coerced.

    Deliberately local to those two keys. The coercion is repo-wide (29
    ``bool(section[...])`` sites in ``kstrl/``, counted by grep) and
    tightening all of them changes how existing configs load, which is
    its own change with its own guard.
    """
    if key not in section:
        return default
    from kstrl.config import ConfigError

    value = section[key]
    if not isinstance(value, bool):
        # No ``[autonomy]`` prefix: ``config_preflight`` puts the section
        # label in front of whatever the loader raises, and a message
        # that carries its own reads "[autonomy] [autonomy] ..." there.
        # The key is unique to this section, so it identifies itself on
        # the one surface that prints the exception bare, which is
        # ``python -m kstrl.calibration compare``.
        raise ConfigError(
            f"{key} must be a boolean (true or false), got {value!r}. "
            "A quoted value is a string, and every non-empty string reads as true."
        )
    return value


@dataclass(frozen=True)
class AutonomyConfig:
    """``[autonomy]`` config. Opt-in, like the R8.1 envelope.

    Off by default because L1 is STRICTER than the harness defaults (it
    forces the merge gate on): switching the ladder on must be deliberate,
    never a silent behavioural upgrade for an existing repo. When enabled,
    the level in ``.kstrl/autonomy.json`` derives the flag bundle at run
    start and any config flag that contradicts it is logged as a manual
    override rather than quietly honored.
    """

    enabled: bool = False
    #: Refuse to run above this level regardless of stored state. A hard
    #: local ceiling for operators who want the ladder's bookkeeping
    #: without its upper levels.
    max_level: int = int(AutonomyLevel.L4_DEPLOY)
    #: Demote one level when ``python -m kstrl.calibration compare`` finds
    #: a regression. Advisory first (#232): a regression always opens a
    #: ``calibration_drift`` inbox item while the ladder is enabled, and
    #: revokes a level only when this is true. Off by default because
    #: every ladder threshold is still an unmeasured placeholder.
    demote_on_calibration_regression: bool = False
    #: Demote one level on an R8.4 health control-limit breach. Advisory
    #: first for the same reason, and additionally suppressed while a
    #: cool-down is running: a breach is a windowed trend, so it persists
    #: across runs and would otherwise cost a level per run.
    demote_on_health_breach: bool = False

    @classmethod
    def from_env(cls) -> AutonomyConfig:
        defaults = cls()
        enabled_raw = os.environ.get("KSTRL_AUTONOMY_ENABLED")
        max_raw = os.environ.get("KSTRL_AUTONOMY_MAX_LEVEL")
        calibration_raw = os.environ.get("KSTRL_AUTONOMY_DEMOTE_ON_CALIBRATION")
        health_raw = os.environ.get("KSTRL_AUTONOMY_DEMOTE_ON_HEALTH")
        return cls(
            enabled=defaults.enabled if enabled_raw is None else enabled_raw == "1",
            max_level=defaults.max_level if max_raw is None else int(max_raw),
            demote_on_calibration_regression=(
                defaults.demote_on_calibration_regression
                if calibration_raw is None
                else calibration_raw == "1"
            ),
            demote_on_health_breach=(
                defaults.demote_on_health_breach if health_raw is None else health_raw == "1"
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> AutonomyConfig:
        """Precedence: env > toml > defaults; reads ``[autonomy]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "autonomy")
        defaults = cls()
        enabled = bool(section["enabled"]) if "enabled" in section else defaults.enabled
        max_level = int(section["max_level"]) if "max_level" in section else defaults.max_level
        demote_calibration = _strict_bool(
            section,
            "demote_on_calibration_regression",
            defaults.demote_on_calibration_regression,
        )
        demote_health = _strict_bool(
            section, "demote_on_health_breach", defaults.demote_on_health_breach
        )
        if "KSTRL_AUTONOMY_ENABLED" in os.environ:
            enabled = os.environ["KSTRL_AUTONOMY_ENABLED"] == "1"
        if "KSTRL_AUTONOMY_MAX_LEVEL" in os.environ:
            max_level = int(os.environ["KSTRL_AUTONOMY_MAX_LEVEL"])
        if "KSTRL_AUTONOMY_DEMOTE_ON_CALIBRATION" in os.environ:
            demote_calibration = os.environ["KSTRL_AUTONOMY_DEMOTE_ON_CALIBRATION"] == "1"
        if "KSTRL_AUTONOMY_DEMOTE_ON_HEALTH" in os.environ:
            demote_health = os.environ["KSTRL_AUTONOMY_DEMOTE_ON_HEALTH"] == "1"
        return cls(
            enabled=enabled,
            max_level=max_level,
            demote_on_calibration_regression=demote_calibration,
            demote_on_health_breach=demote_health,
        )

    def __post_init__(self) -> None:
        valid = {int(level) for level in AutonomyLevel}
        if self.max_level not in valid:
            raise AutonomyError(
                f"invalid max_level {self.max_level}; expected one of {sorted(valid)}"
            )


def effective_level(state: AutonomyState, config: AutonomyConfig) -> AutonomyLevel:
    """The level actually in force: stored level clamped by ``max_level``.

    Clamping here (rather than rewriting state) keeps the earned level
    intact when an operator temporarily lowers the ceiling.
    """
    return AutonomyLevel(min(state.level, config.max_level))


def envelope_ceiling(policy_enabled: bool) -> AutonomyLevel:
    """The highest level defensible given the R8.1 envelope's state.

    L3 is *Enveloped* auto-merge: the envelope is the boundary that makes
    unattended merging defensible at all. With ``[policy] enabled=false``
    there is no boundary, so "auto-merge inside the envelope" would mean
    auto-merge inside nothing. L2 (human gates the merge) is then the
    ceiling regardless of the level the ladder has awarded.
    """
    return AutonomyLevel.L4_DEPLOY if policy_enabled else AutonomyLevel.L2_GATED_MERGE


def resolve_runtime_level(
    state: AutonomyState,
    config: AutonomyConfig,
    *,
    policy_enabled: bool,
    root_dir: Path,
) -> tuple[AutonomyLevel, list[str]]:
    """The level a run may actually use, plus why it was clamped.

    Independent ceilings apply, and the LOWEST wins: the operator's
    ``max_level``, the envelope ceiling, and (R8.9) the control-state
    relocation gate. Returned rather than raised so the run can proceed
    at the safe level while saying loudly what it withheld.
    """
    notes: list[str] = []
    level = state.autonomy_level
    if int(level) > config.max_level:
        level = AutonomyLevel(config.max_level)
        notes.append(
            f"clamped to L{config.max_level} by [autonomy] max_level "
            f"(earned level L{state.level} retained)"
        )
    ceiling = envelope_ceiling(policy_enabled)
    if int(level) > int(ceiling):
        notes.append(
            f"clamped to {ceiling.label}: L3+ requires the R8.1 policy "
            "envelope, but [policy] enabled=false - there is no envelope "
            "to auto-merge inside"
        )
        level = ceiling
    ensure_control_state(root_dir)
    if int(level) >= int(AutonomyLevel.L3_ENVELOPED_AUTO) and not control_is_external(root_dir):
        notes.append(
            "clamped to L2 Gated-merge: L3+ requires control state outside "
            "the agent-reachable tree (R8.9); leftover `.kstrl/` control "
            "files or an XDG_STATE_HOME under the repo block unattended "
            "auto-merge"
        )
        level = AutonomyLevel.L2_GATED_MERGE
    return level, notes


def control_relocation_error(
    root_dir: Path,
    *,
    target_level: AutonomyLevel,
) -> str | None:
    """Why L3+ is refused for control-state placement, or None if allowed."""
    if int(target_level) < int(AutonomyLevel.L3_ENVELOPED_AUTO):
        return None
    ensure_control_state(root_dir)
    if control_is_external(root_dir):
        return None
    return (
        "L3+ requires control state outside the agent-reachable tree "
        "(R8.9). Finish migrating leftover `.kstrl/` control files, or "
        "point XDG_STATE_HOME outside the repo, then retry."
    )


def promotion_authority_error(*, force: bool) -> str | None:
    """Why this process may not authorize a promotion, or None if it may.

    ``--actor``/``--ack`` are strings any caller can supply, so on their
    own they prove nothing about who is asking: an unattended agent can
    pass ``--actor human``. The out-of-band signal is a controlling TTY -
    an interactive terminal the agent's subprocess does not have. Live
    autonomy state lives in the XDG control dir (R8.9); the legacy
    in-tree path remains in the R8.1 enforcement-machinery halt set so a
    diff cannot rewrite it. Stated plainly rather than overclaimed.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        detail = "forced promotions bypass evidence and " if force else ""
        return (
            f"promotion requires an interactive terminal ({detail}a "
            "caller-supplied --actor string does not establish human "
            "acknowledgement). Run this from a terminal; an unattended "
            "process cannot promote."
        )
    return None


def commit_transition(
    state: AutonomyState,
    record: Transition,
    root_dir: Path,
    *,
    bus: EventBus | None = None,
    run_id: str = "",
) -> None:
    """Persist a transition to state AND both audit streams, together.

    One function so the three writes cannot drift apart: before this, the
    CLI saved ``autonomy.json`` and nothing reached the journal, which made
    the documented "every transition is recorded" claim false. The state
    save happens first (it is the load-bearing one); audit-append failures
    are warned about, never fatal - losing the log must not strand the
    ladder in an unsaved state.

    ``bus`` is present only inside a factory run; CLI transitions have no
    run stream, so the evolution journal is their durable record.
    """
    state.save(root_dir)

    from kstrl.evolution import JOURNAL_SCHEMA_VERSION, EvolutionConfig, EvolutionJournal

    entry = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "timestamp": record.at,
        "run_id": run_id,
        "event_type": "autonomy_transition",
        "direction": record.direction,
        "from_level": record.from_level,
        "to_level": record.to_level,
        "actor": record.actor,
        "trigger": record.trigger,
        "reason": record.reason,
        "evidence": record.evidence,
    }
    # load_or_none, not load: a typo in an [evolution] knob raises
    # ValueError, which this OSError guard does not catch, and the
    # ladder would then fail to record a transition it has already
    # SAVED - the exact drift this function exists to prevent.
    config = EvolutionConfig.load_or_none(
        root_dir,
        warn=lambda message: warnings.warn(message, RuntimeWarning, stacklevel=2),
    )
    # Through append_entries (the one writer of the line format, #312)
    # rather than a second raw open. EvolutionJournal is constructed
    # directly rather than through .open(), which would also gate on
    # config.enabled and stop recording transitions this function
    # records today.
    try:
        if config is not None:
            EvolutionJournal(config).append_entries([entry])
    except OSError as exc:
        warnings.warn(
            f"autonomy: journal append failed (non-fatal): {exc}",
            RuntimeWarning,
            stacklevel=2,
        )

    if bus is not None:
        from kstrl.events import AutonomyTransition

        bus.emit(
            AutonomyTransition(
                direction=record.direction,
                from_level=record.from_level,
                to_level=record.to_level,
                actor=record.actor,
                trigger=record.trigger,
                reason=record.reason,
            )
        )


def apply_demotion(
    root_dir: Path,
    trigger: DemotionTrigger,
    reason: str,
    *,
    evidence: dict[str, Any],
    run_id: str,
    ui: UI,
    bus: EventBus | None = None,
    state: AutonomyState | None = None,
) -> Transition | None:
    """Demote one level for ``trigger``, persist, emit, and open the notice.

    Returns None when already at the floor: L1 is the safe state, so a
    repeated trigger there is a no-op rather than an error. The state is
    still saved on that path, because the caller's own counters (a policy
    violation, say) were mutated before the demotion was attempted and
    they are what blocks the next promotion. One exception, warned about:
    a state that ``load`` already failed closed on is not written back,
    because saving a fresh L1 over damaged bytes destroys the only thing
    an operator could have repaired, and the counters that save would
    carry were lost with the file they came from anyway.

    ``state`` is for callers that already hold a mutated, unsaved state.
    ``factory._record_autonomy_outcome`` counts the run's violations on an
    in-memory state and then demotes; re-loading here would silently drop
    that count. Callers with nothing pending pass nothing and this loads.

    Every automatic demotion goes through this one function so the four
    writes it performs - state, evolution journal, run event stream and
    inbox notice - cannot drift apart per trigger. An inbox failure is
    warned about, never fatal: losing the notice must not strand the
    ladder with a saved demotion and no record of why.
    """
    # Imported here rather than at module scope, matching
    # ``commit_transition`` above: the ladder state machine is imported
    # by config and safe-mode code that has no business pulling the
    # inbox in with it.
    from kstrl.inbox import Inbox, InboxConfig, ItemKind

    if state is None:
        state = AutonomyState.load(root_dir)
    trigger_text = trigger.label.replace("_", " ")
    record = state.demote(trigger, reason, evidence=evidence)
    if record is None:
        if state.degraded_reason is None:
            state.save(root_dir)
        else:
            # ``load`` fails closed to a fresh L1 when the stored record
            # is damaged, and those bytes are the only thing an operator
            # could repair. Saving here replaces them with an empty
            # ladder, and the counters that save would carry were lost
            # with the file they were read from.
            ui.warn(
                f"Autonomy: ladder state is degraded ({state.degraded_reason}); not overwriting it"
            )
        ui.warn(f"Autonomy: {trigger_text} recorded ({reason}); already at L1, nothing to revoke")
        return None
    commit_transition(state, record, root_dir, bus=bus, run_id=run_id)
    # R8.2 promised this and R8.3 delivers it: a demotion is exactly the
    # boundary condition an over-the-loop operator must see, and the
    # triggering evidence is perishable - it belongs on the item.
    try:
        inbox_config = InboxConfig.load(root_dir)
        if inbox_config.enabled:
            Inbox(root_dir, inbox_config).add(
                ItemKind.DEMOTION_NOTICE,
                f"Autonomy demoted L{record.from_level} -> L{record.to_level}",
                detail=record.reason,
                run_id=run_id,
                dedupe_key=f"demotion:{run_id}:{record.to_level}",
                evidence={
                    "trigger": record.trigger,
                    "from_level": record.from_level,
                    "to_level": record.to_level,
                    **record.evidence,
                },
            )
    except (OSError, ValueError, ControlStateError) as exc:
        # ControlStateError is a RuntimeError, so the (OSError,
        # ValueError) pair every inbox site was written with does not
        # catch it - and Inbox._append takes the control lock on every
        # write, which is where it comes from.
        ui.warn(f"Inbox write failed (non-fatal): {exc}")
    ui.warn(
        f"Autonomy DEMOTED L{record.from_level} -> L{record.to_level} "
        f"({AutonomyLevel(record.to_level).label}) on {trigger_text}; cool-down "
        f"{state.cooldown_runs_remaining} decisive run(s)"
    )
    return record


def manual_override_notes(
    bundle: FlagBundle,
    *,
    configured_pause_before_pr_merge: bool | None = None,
    configured_review_mode: str | None = None,
) -> list[str]:
    """Config values that contradict the level's bundle.

    Named rather than silently honored: the roadmap's stale-ladder failure
    mode is a hand-edited flag granting autonomy the ladder never awarded.
    The bundle still wins; these notes exist so the divergence is visible
    in the run log and the transition record.
    """
    notes: list[str] = []
    if (
        configured_pause_before_pr_merge is not None
        and configured_pause_before_pr_merge != bundle.pause_before_pr_merge
    ):
        notes.append(
            f"[factory] pause_before_pr_merge={configured_pause_before_pr_merge} "
            f"contradicts {bundle.level.label} "
            f"(bundle: {bundle.pause_before_pr_merge}); bundle wins"
        )
    if configured_review_mode is not None and configured_review_mode != bundle.review_mode:
        notes.append(
            f"[factory] review_mode={configured_review_mode!r} contradicts "
            f"{bundle.level.label} (bundle: {bundle.review_mode!r}); bundle wins"
        )
    return notes
