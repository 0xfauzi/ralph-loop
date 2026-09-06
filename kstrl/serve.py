"""R8.6 continuous intake: the daemon that drains the work queue.

PR 1 built a place for work to wait. This is what runs it without a
human firing each run - and therefore the module where a mistake spends
money while nobody is watching. A measured factory engineer iteration
costs ~$1.70-2.60 on a first attempt and $3.99-7.42 on a retry (retries
carry accumulated context), and a 5-story component with adversarial
review ran ~$29 and 96 minutes without finishing. Those numbers are why
almost every decision below errs toward NOT launching a run.

**The retry rule, stated precisely.** R8.6 says "only
``infrastructure_error`` failures auto-retry". That phrasing leaves the
UNKNOWN case undefined, and the unknown case is where the money goes, so
this module implements it as *positive evidence only, failing closed*: an
item is retried only when we can read affirmative evidence that the
failure was infrastructural. An unreadable manifest, a missing artifact,
a run that died before recording anything, an exit code we do not
recognise - all poison, none retry. This is deliberately the inverse of
"retry unless proven to be a spec failure", which is the shape that
produces an overnight crash loop.

The same class of mistake is already on record in this repo: R8.1 review
correction #2 found the changed-file reads were fail-OPEN, so a
``kstrl.toml`` diff evaluated as "0 files, 0 lines" and passed every
path and size rule. Defaulting to permissive on missing evidence looks
harmless until the evidence goes missing.

**Four independent backstops**, because a correct classifier is not
sufficient - a *persistent* infrastructure fault is retryable by the
rules and still burns money:

1. ``max_attempts`` per item, enforced by ``Queue.start`` itself.
2. Exponential backoff between attempts, so a fast-failing item cannot
   spin.
3. ``daily_budget_usd``, checked BEFORE admitting each item.
4. A consecutive-poison breaker that pauses the whole queue. If ``main``
   is broken then every run fails verification, each failure is
   individually legitimate, and per-item bounds never notice; only a
   cross-item signal does.

**Honesty about the budget (H4).** ``daily_budget_usd`` can only count
cost that an adapter reported. The codex adapter reports tokens and no
cost, so with a cost-blind agent the budget is not approximate - it is
*unenforceable*, the same condition PR #184 named for ``max_cost_usd``.
This module therefore records the day's spend together with its
coverage, never converts unreported calls into an estimated dollar
figure, and refuses to run unattended when a budget is configured but
cost coverage is absent (``allow_uncovered_cost`` is the explicit
override).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

from kstrl.agents.base import ARCHITECT_COMPONENT, ARCHITECT_ROLE
from kstrl.manifest import ADVERSARIAL_BUDGET_CHECK, Component, Manifest
from kstrl.procdispose import drain_or_abandon
from kstrl.procgroup import (
    pid_is_alive,
    read_group_liveness,
    safe_pgid,
    signal_group,
    signal_probe_alive,
)
from kstrl.runid import run_kind
from kstrl.statedir import (
    CONTROL_SPEND,
    control_file,
    control_lock,
    control_untrusted_reason,
    ensure_control_state,
    state_dir,
)
from kstrl.workqueue import (
    ItemSource,
    ItemState,
    MergeDisposition,
    Queue,
    QueueBudgetExhausted,
    QueueConfig,
    QueueError,
    QueueItem,
    queue_lock,
    queue_root,
)

SERVE_LOCK_FILENAME = "serve.lock"

#: Substrings the factory prints on each of its two exit-2 refusals.
#: Matching output is EVIDENCE; a pre-launch lock probe is inference and
#: races (#186 F6). Both come from kstrl/factory.py and kstrl/cli.py.
_LOCK_REFUSAL_MARKER = "--force-lock"
_SPEC_BLOCKER_MARKER = "Spec issues written to:"

#: Retry backoff: ``base * 2 ** (attempts - 1)``, capped. Not tunable by
#: config on purpose - the cap is a safety floor on how fast an item can
#: consume its attempts, not a preference.
BACKOFF_BASE_SECONDS = 60.0
BACKOFF_CAP_SECONDS = 1800.0

#: How many recent cycles an unbounded daemon keeps for diagnostics.
RECENT_CYCLE_WINDOW = 100


class ServeError(RuntimeError):
    """The daemon cannot run."""


class ServeLockedError(ServeError):
    """Another ``ks serve`` holds the singleton lock on this root."""


class Verdict(StrEnum):
    """What the evidence says about a finished run.

    ``UNCLASSIFIABLE`` is a first-class outcome rather than an error
    path: it is the common case for a crash, and treating it as "probably
    fine, retry" is the bug this whole module is arranged to avoid.
    """

    SUCCESS = "success"
    RETRY_INFRA = "retry_infra"
    SPEC_FAILURE = "spec_failure"
    UNCLASSIFIABLE = "unclassifiable"
    #: The run halted itself on a configured ceiling. Deliberate and
    #: DETERMINISTIC - the only verdict that would otherwise have been
    #: read as retryable infrastructure. See budget_halt_reason.
    BUDGET_HALT = "budget_halt"

    @property
    def may_retry(self) -> bool:
        """Only ONE verdict authorizes spending again."""
        return self is Verdict.RETRY_INFRA


@dataclass(frozen=True)
class Outcome:
    """A verdict plus the evidence that produced it.

    ``evidence`` is written to the inbox item, so a human deciding
    whether to requeue sees what the classifier actually read rather
    than having to trust its label.
    """

    verdict: Verdict
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunOutcome:
    """Raw result of one factory invocation, before interpretation."""

    returncode: int
    timed_out: bool = False
    #: Non-empty when the launch itself failed (binary missing, etc).
    launch_error: str = ""
    #: Tail of the child's combined output. The factory's two exit-2
    #: refusals are only distinguishable from what it printed (#186 F6),
    #: so this is classification evidence rather than a log nicety.
    output_tail: str = ""
    #: True when the timeout path confirmed the whole process GROUP was
    #: signalled and reaped. False means a descendant may still be alive,
    #: which must never be treated as "the run is over" (#186 F1).
    group_reaped: bool = False
    #: Why the reap check could not measure, when it could not. A
    #: `group_reaped=False` reached this way means "we could not see",
    #: not "a factory is still running", and the poison reason an
    #: operator reads says so. Empty when the check measured cleanly.
    #: Set on the reaped path too, where it means the "gone" itself was
    #: not measured, which is the unsafe direction and so is reported.
    group_reap_detail: str = ""
    #: Positive evidence the group IS occupied, as opposed to merely
    #: unconfirmed. Kept apart from `group_reap_detail` because reporting
    #: a refused signal as "unknown" states the opposite of the truth.
    group_occupied_detail: str = ""


class FactoryRunner(Protocol):
    """How the daemon executes one queue item.

    A Protocol so tests drive the entire loop without spawning a factory.
    That is not only a speed concern: a suite that ran the real thing
    would cost dollars per assertion.
    """

    def __call__(
        self,
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
        on_spawn: Callable[[int], None] | None = None,
    ) -> RunOutcome: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _local_today() -> str:
    """Today's date in LOCAL time.

    Local rather than UTC because the budget resets "at the next day"
    from the operator's point of view; a UTC reset would land mid-evening
    for most of the world.
    """
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def next_local_midnight(now: datetime | None = None) -> str:
    """UTC ISO timestamp of the next local midnight."""
    local = (now or _utc_now()).astimezone()
    tomorrow = (local + timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return _iso(tomorrow.astimezone(UTC))


def backoff_seconds(attempts: int) -> float:
    """Delay before an item's next attempt."""
    if attempts <= 0:
        return 0.0
    grown = BACKOFF_BASE_SECONDS * float(2 ** (attempts - 1))
    return min(grown, BACKOFF_CAP_SECONDS)


@dataclass(frozen=True)
class ServeConfig:
    """``[serve]`` config: the daemon's knobs.

    ``daily_budget_usd`` lives here rather than in ``[queue]`` because
    only the daemon spends; it is nonetheless queue-LEVEL in the sense
    R8.6 means - it spans runs and it pauses the queue, as opposed to
    the per-run ``max_cost_usd`` ceiling.
    """

    poll_interval_seconds: float = 60.0
    #: 0 disables the budget entirely. Any positive value is a HARD stop.
    daily_budget_usd: float = 0.0
    #: Consecutive poisoned items that pause the whole queue. The
    #: cross-item signal per-item bounds cannot see.
    max_consecutive_poison: int = 3
    #: Hold ``caffeinate -i`` for the duration of each run so the machine
    #: does not sleep mid-factory, and sleeps freely between runs.
    caffeinate: bool = True
    #: 0 disables the per-run timeout.
    factory_timeout_seconds: float = 0.0
    #: Run unattended even when a configured budget cannot be enforced
    #: because no adapter reports cost. Explicit opt-out of the guard.
    allow_uncovered_cost: bool = False

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ServeError(
                f"serve.poll_interval_seconds must be > 0, got {self.poll_interval_seconds}"
            )
        if self.daily_budget_usd < 0:
            raise ServeError(f"serve.daily_budget_usd must be >= 0, got {self.daily_budget_usd}")
        if self.max_consecutive_poison < 1:
            raise ServeError(
                f"serve.max_consecutive_poison must be >= 1, got {self.max_consecutive_poison}"
            )
        if self.factory_timeout_seconds < 0:
            raise ServeError(
                f"serve.factory_timeout_seconds must be >= 0, got {self.factory_timeout_seconds}"
            )

    @classmethod
    def from_env(cls) -> ServeConfig:
        defaults = cls()
        poll = os.environ.get("KSTRL_SERVE_POLL_INTERVAL")
        budget = os.environ.get("KSTRL_SERVE_DAILY_BUDGET_USD")
        poison = os.environ.get("KSTRL_SERVE_MAX_CONSECUTIVE_POISON")
        caffeinate = os.environ.get("KSTRL_SERVE_CAFFEINATE")
        timeout = os.environ.get("KSTRL_SERVE_FACTORY_TIMEOUT")
        uncovered = os.environ.get("KSTRL_SERVE_ALLOW_UNCOVERED_COST")
        return cls(
            poll_interval_seconds=(defaults.poll_interval_seconds if poll is None else float(poll)),
            daily_budget_usd=(defaults.daily_budget_usd if budget is None else float(budget)),
            max_consecutive_poison=(
                defaults.max_consecutive_poison if poison is None else int(poison)
            ),
            caffeinate=(defaults.caffeinate if caffeinate is None else caffeinate == "1"),
            factory_timeout_seconds=(
                defaults.factory_timeout_seconds if timeout is None else float(timeout)
            ),
            allow_uncovered_cost=(
                defaults.allow_uncovered_cost if uncovered is None else uncovered == "1"
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> ServeConfig:
        """Precedence: env > toml > defaults; reads ``[serve]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "serve")
        defaults = cls()

        def _float(key: str, fallback: float) -> float:
            return float(section[key]) if key in section else fallback

        def _int(key: str, fallback: int) -> int:
            return int(section[key]) if key in section else fallback

        def _bool(key: str, fallback: bool) -> bool:
            return bool(section[key]) if key in section else fallback

        poll = _float("poll_interval_seconds", defaults.poll_interval_seconds)
        budget = _float("daily_budget_usd", defaults.daily_budget_usd)
        poison = _int("max_consecutive_poison", defaults.max_consecutive_poison)
        caffeinate = _bool("caffeinate", defaults.caffeinate)
        timeout = _float(
            "factory_timeout_seconds",
            defaults.factory_timeout_seconds,
        )
        uncovered = _bool("allow_uncovered_cost", defaults.allow_uncovered_cost)

        if "KSTRL_SERVE_POLL_INTERVAL" in os.environ:
            poll = float(os.environ["KSTRL_SERVE_POLL_INTERVAL"])
        if "KSTRL_SERVE_DAILY_BUDGET_USD" in os.environ:
            budget = float(os.environ["KSTRL_SERVE_DAILY_BUDGET_USD"])
        if "KSTRL_SERVE_MAX_CONSECUTIVE_POISON" in os.environ:
            poison = int(os.environ["KSTRL_SERVE_MAX_CONSECUTIVE_POISON"])
        if "KSTRL_SERVE_CAFFEINATE" in os.environ:
            caffeinate = os.environ["KSTRL_SERVE_CAFFEINATE"] == "1"
        if "KSTRL_SERVE_FACTORY_TIMEOUT" in os.environ:
            timeout = float(os.environ["KSTRL_SERVE_FACTORY_TIMEOUT"])
        if "KSTRL_SERVE_ALLOW_UNCOVERED_COST" in os.environ:
            uncovered = os.environ["KSTRL_SERVE_ALLOW_UNCOVERED_COST"] == "1"

        return cls(
            poll_interval_seconds=poll,
            daily_budget_usd=budget,
            max_consecutive_poison=poison,
            caffeinate=caffeinate,
            factory_timeout_seconds=timeout,
            allow_uncovered_cost=uncovered,
        )


# ---------------------------------------------------------------------------
# Persistent daemon state: spend, coverage, and the poison streak
# ---------------------------------------------------------------------------


class ServeStateError(ServeError):
    """The daemon's own state file could not be read.

    A distinct error because every consumer must FAIL CLOSED on it.
    Review #186 F4: the ledger previously read an unparseable file as a
    fresh zero day, so charging $9, corrupting the file, and setting a
    $5 budget allowed another run - and kept allowing them. The other
    backstops do not bound that: they are per-item, and this is the only
    queue-WIDE spend limit.
    """


@dataclass(frozen=True)
class DailySpend:
    """What the queue has spent today, and how well we know it.

    Coverage is recorded as CALL COUNTS, not inferred from whether the
    dollar total is positive. Review #186 F9: a fully-metered run that
    legitimately cost $0 was reported as having no cost coverage, and a
    launch failure that metered nothing counted as a run.

    ``covered_calls``/``total_calls`` make the three cases distinct:
    full coverage (equal), partial coverage (a floor - the cap still
    fires, just late), and ZERO coverage (the cap can never fire at all).
    Only the third is unenforceable.

    ``unmetered_phases`` names phases known to spend without reporting
    anything. What goes in it is derived per launch by
    :attr:`RunSpend.unmetered_phases`, which states when and why. Naming
    a phase there is the honest alternative to estimating it (#186 F3).
    """

    date: str = ""
    spent_usd: float = 0.0
    runs: int = 0
    covered_calls: int = 0
    total_calls: int = 0
    unmetered_phases: tuple[str, ...] = ()

    @property
    def uncovered_calls(self) -> int:
        return max(0, self.total_calls - self.covered_calls)

    @property
    def has_any_coverage(self) -> bool:
        """Whether ANY call reported cost. False = the cap cannot fire."""
        return self.covered_calls > 0

    @property
    def lower_bound(self) -> bool:
        """Whether the real spend exceeds ``spent_usd`` by an unmeasured amount."""
        return self.uncovered_calls > 0 or bool(self.unmetered_phases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "spent_usd": self.spent_usd,
            "runs": self.runs,
            "covered_calls": self.covered_calls,
            "total_calls": self.total_calls,
            "unmetered_phases": list(self.unmetered_phases),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailySpend:
        def _num(key: str) -> float:
            value = data.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0.0
            return float(value)

        raw_phases = data.get("unmetered_phases")
        phases = (
            tuple(str(p) for p in raw_phases if isinstance(p, str))
            if isinstance(raw_phases, list)
            else ()
        )
        return cls(
            date=str(data.get("date") or ""),
            spent_usd=_num("spent_usd"),
            runs=int(_num("runs")),
            covered_calls=int(_num("covered_calls")),
            total_calls=int(_num("total_calls")),
            unmetered_phases=phases,
        )


@dataclass(frozen=True)
class ServeState:
    """Everything the daemon must remember between cycles.

    The poison streak lives HERE rather than being derived from the
    queue journal. Review #186 F5: ``Queue._journal`` deliberately
    swallows append failures and ``journal_entries`` returns ``[]`` on
    any read error, so making the journal unreadable reported a zero
    streak and re-allowed spending while three poisoned items sat on
    disk. A money backstop cannot be built on narration the queue
    explicitly permits itself to lose.
    """

    spend: DailySpend = field(default_factory=DailySpend)
    consecutive_poison: int = 0
    #: Set once any run has reported a cost figure. Until then a
    #: configured budget is unenforceable and serve refuses to claim
    #: work (#186 F8) rather than discovering it after a run.
    cost_coverage_seen: bool = False
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "spend": self.spend.to_dict(),
            "consecutive_poison": self.consecutive_poison,
            "cost_coverage_seen": self.cost_coverage_seen,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServeState:
        raw_spend = data.get("spend")
        spend = DailySpend.from_dict(raw_spend) if isinstance(raw_spend, dict) else DailySpend()
        streak = data.get("consecutive_poison", 0)
        return cls(
            spend=spend,
            consecutive_poison=(
                streak if isinstance(streak, int) and not isinstance(streak, bool) else 0
            ),
            cost_coverage_seen=bool(data.get("cost_coverage_seen", False)),
        )


class SpendLedger:
    """Atomic store for :class:`ServeState` in the XDG control directory.

    Rewritten whole on every update rather than appended: the only
    questions asked of it are "how much today", "how many poisons in a
    row", and "has cost ever been reported", all of which must be cheap
    enough to evaluate before every item.
    """

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    @property
    def path(self) -> Path:
        return control_file(self.root_dir, CONTROL_SPEND)

    def read_state(self, today: str | None = None) -> ServeState:
        """Load state, rolling the spend over on a new local day.

        Raises :class:`ServeStateError` for every read failure EXCEPT a
        missing file, which is the genuine first-run case. Failing closed
        here is the same correction PR #185 F7 applied to the pause
        marker: a file we cannot parse is not evidence that spending is
        safe. After a failed/partial migrate (legacy still present, or
        control dir inaccessible), missing XDG is NOT first-run - raise
        rather than zero the budget.

        "Every read failure" includes the DECODE, which until #320 it did
        not: ``UnicodeDecodeError`` is a ``ValueError``, so one non-utf-8
        byte in the ledger walked straight past the ``OSError`` clause
        below and out of a money path whose entire argument is that it
        refuses to spend against a total it could not read.
        """
        ensure_control_state(self.root_dir)
        untrusted = control_untrusted_reason(self.root_dir)
        if untrusted is not None:
            raise ServeStateError(
                f"refusing to read the daemon spend ledger: {untrusted}. "
                "Fix the control-state location or finish migrating "
                "legacy `.kstrl/` control files before spending."
            )
        stamp = today or _local_today()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ServeState(spend=DailySpend(date=stamp))
        except OSError as exc:
            raise ServeStateError(
                f"cannot read the daemon spend ledger {self.path}: {exc}. "
                "Refusing to spend against an unknown daily total; fix the "
                "file's permissions or move it aside to start a fresh day."
            ) from exc
        except UnicodeDecodeError as exc:
            # Its own remedy, because it is its own problem: the file is
            # readable and the bytes are wrong, so chmod and chown are
            # the wrong advice. kstrl writes this ledger as ASCII JSON,
            # so a byte that will not decode means something else edited
            # it.
            raise ServeStateError(
                f"the daemon spend ledger {self.path} is not valid UTF-8 ({exc}). "
                "Refusing to spend against an unknown daily total; re-save it "
                "as UTF-8 or move it aside to start a fresh day."
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServeStateError(
                f"the daemon spend ledger {self.path} is malformed ({exc}). "
                "Refusing to spend against an unknown daily total; inspect "
                "it and move it aside to start a fresh day."
            ) from exc
        if not isinstance(data, dict):
            raise ServeStateError(
                f"the daemon spend ledger {self.path} is not an object. "
                "Refusing to spend against an unknown daily total."
            )
        state = ServeState.from_dict(data)
        if state.spend.date != stamp:
            # A new day resets the SPEND only. The poison streak and the
            # coverage flag are not daily facts.
            return replace(state, spend=DailySpend(date=stamp))
        return state

    def read(self, today: str | None = None) -> DailySpend:
        """Today's spend. Raises ServeStateError like ``read_state``."""
        return self.read_state(today).spend

    def _write_unlocked(self, state: ServeState) -> None:
        """Atomic rewrite; caller must already hold ``control_lock``."""
        from kstrl.workqueue import atomic_write

        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            path,
            json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )

    def _write(self, state: ServeState) -> None:
        ensure_control_state(self.root_dir)
        with control_lock(self.root_dir):
            self._write_unlocked(state)

    def charge(
        self,
        usd: float,
        *,
        covered_calls: int = 0,
        total_calls: int = 0,
        unmetered_phases: Sequence[str] = (),
        metered_run: bool = True,
        today: str | None = None,
    ) -> DailySpend:
        """Add one run's reported cost and coverage to today's total.

        ``metered_run=False`` records the spend without counting a run,
        for a launch that never reached an agent (#186 F9: a launch
        failure previously incremented ``runs`` and then tripped the
        coverage gate on the next cycle).

        The full read-modify-write holds ``control_lock`` so two
        origin-sharing checkouts cannot drop charges.
        """
        stamp = today or _local_today()
        ensure_control_state(self.root_dir)
        with control_lock(self.root_dir):
            state = self.read_state(stamp)
            current = state.spend
            merged_phases = tuple(
                sorted(set(current.unmetered_phases) | {p for p in unmetered_phases if p})
            )
            spend = DailySpend(
                date=stamp,
                spent_usd=round(current.spent_usd + max(0.0, usd), 6),
                runs=current.runs + (1 if metered_run else 0),
                covered_calls=current.covered_calls + max(0, covered_calls),
                total_calls=current.total_calls + max(0, total_calls),
                unmetered_phases=merged_phases,
            )
            self._write_unlocked(
                replace(
                    state,
                    spend=spend,
                    cost_coverage_seen=state.cost_coverage_seen or covered_calls > 0,
                )
            )
            return spend

    def record_terminal(self, *, poisoned: bool, today: str | None = None) -> int:
        """Update the authoritative poison streak; returns the new value."""
        ensure_control_state(self.root_dir)
        with control_lock(self.root_dir):
            state = self.read_state(today)
            streak = state.consecutive_poison + 1 if poisoned else 0
            self._write_unlocked(replace(state, consecutive_poison=streak))
            return streak

    def reset_poison_streak(self, today: str | None = None) -> None:
        ensure_control_state(self.root_dir)
        with control_lock(self.root_dir):
            state = self.read_state(today)
            self._write_unlocked(replace(state, consecutive_poison=0))


@dataclass(frozen=True)
class RunSpend:
    """One run's cost as read back from its event stream."""

    cost_usd: float = 0.0
    cost_calls: int = 0
    usage_calls: int = 0
    #: Calls the run recorded against the architect, read for one
    #: question only: see :attr:`unmetered_phases`.
    architect_calls: int = 0

    @property
    def uncovered_calls(self) -> int:
        return max(0, self.usage_calls - self.cost_calls)

    @property
    def lower_bound(self) -> bool:
        return self.uncovered_calls > 0

    @property
    def unmetered_phases(self) -> tuple[str, ...]:
        """Roles that spent on THIS RUN without reaching the meter.

        The one member of this class that does not survive summation,
        which is why a launch spanning several runs is a
        :class:`LaunchSpend` and not a bigger ``RunSpend``: the answer is
        all-or-nothing, so on a sum one run's architect would clear
        another's (#257 review). ``cost_usd`` and ``uncovered_calls``
        add up fine; this does not.

        It stopped being a constant in #257 piece B. `ks factory` now
        hands the architect's spend to the run it decomposed for, so a
        run that got as far as executing carries an architect row inside
        the dir the daemon just charged - already counted in
        ``cost_usd``, and naming it unmetered on top of that would call
        a measured figure a floor.

        Zero calls does NOT mean zero spend, which is why the fallback is
        the pessimistic one. Three separate cases land on it and all
        three deserve it: a blocker halt, which exits `ks factory` before
        any run directory exists so the decompose bill is nowhere on disk
        to charge; an adapter that reports no usage; and a resume that
        ran no architect at all. Naming the role keeps the day's total
        labelled a floor rather than estimated (#186 F3).
        """
        return () if self.architect_calls > 0 else (ARCHITECT_ROLE,)


@dataclass(frozen=True)
class LaunchSpend:
    """What ONE launch spent, across every run directory it produced.

    Separate from :class:`RunSpend` because a launch is not simply a
    bigger run. The dollar and call figures add up; ``unmetered_phases``
    does not, so summing runs into a single ``RunSpend`` and reading the
    property off the total let a run that reported an architect clear
    the claim for a sibling that had none, and the day was reported
    exact on the strength of a different run's spend (#257 review).

    Stating it as its own type is what makes that misuse unreachable
    rather than merely documented: there is no ``architect_calls`` here
    to sum, and ``unmetered_phases`` is correct by construction.
    """

    cost_usd: float = 0.0
    cost_calls: int = 0
    usage_calls: int = 0
    unmetered_phases: tuple[str, ...] = ()

    @classmethod
    def over(cls, spends: Sequence[RunSpend]) -> LaunchSpend:
        """Fold each run's reading into the launch's."""
        # A launch that produced no directory at all is accounted for as
        # one empty run rather than as nothing: it spent nothing this
        # code can see, but a blocker halt spends an architect's worth of
        # money and leaves it nowhere on disk. An empty RunSpend reports
        # exactly that, so the pessimistic answer arrives by the same
        # path as every other one.
        readings = list(spends) or [RunSpend()]
        return cls(
            cost_usd=sum(reading.cost_usd for reading in readings),
            cost_calls=sum(reading.cost_calls for reading in readings),
            usage_calls=sum(reading.usage_calls for reading in readings),
            unmetered_phases=tuple(
                sorted({phase for reading in readings for phase in reading.unmetered_phases})
            ),
        )


def read_run_spend(root_dir: Path, run_id: str) -> RunSpend:
    """Reported cost of ONE named run, with its coverage.

    ``run_id`` must be non-empty and owned by the invocation being
    charged. Review #186 F2: an empty id made ``load_run_state`` fall
    back to the NEWEST run on disk, so a failed invocation charged a
    previous run's spend and could be classified from a stale manifest.
    An empty id now returns zeros instead of silently reading someone
    else's run.

    #281: the architect's row moved to ``ARCHITECT_COMPONENT``, so a run
    recorded before that change - whose stream says ``"architect"`` -
    reads here as having no architect row, and the day's total is
    reported as a FLOOR rather than exact. That is the pessimistic
    direction, and it is the same answer this function already gives for
    a resume, a blocker halt and an adapter that reports nothing.

    There is deliberately no fallback to the old bare key. It would be
    unsafe rather than merely redundant: on a NEW run whose architect did
    not report and which happens to contain a component named
    `architect`, the fallback would read that component's calls as the
    architect's and clear the honesty flag - which is exactly the bug
    #281 removes. The narrower thing is not worth building either,
    because ``owned_run_spend`` only ever reads run dirs created inside
    the launch window it is charging, so the daemon never reads a
    pre-#281 run at all.
    """
    if not run_id:
        return RunSpend()
    from kstrl.reducer import load_run_state

    try:
        state, _source = load_run_state(root_dir, run_id)
    except OSError:
        return RunSpend()
    architect = state.components.get(ARCHITECT_COMPONENT)
    return RunSpend(
        cost_usd=state.cost_usd,
        cost_calls=state.cost_calls,
        usage_calls=state.usage_calls,
        architect_calls=architect.usage_calls if architect is not None else 0,
    )


#: Run-id kind prefix of the runs this daemon's own child produces;
#: ``owned_run_spend`` charges only these.
#:
#: Coupled to the spawned argv by CONVENTION, not derivation: `ks factory`
#: gets this prefix from ``mint_run_id``'s default, not from its command
#: name. Changing either without the other silently empties the charge,
#: so a test pins the two together.
SPAWNED_RUN_KIND: Final = "factory"


def owned_run_spend(
    root_dir: Path,
    runs_before: frozenset[str],
) -> tuple[list[str], LaunchSpend]:
    """The run dirs a launch produced, and what they add up to.

    "Produced" is narrower than "new" (#257 review). `ks decompose` takes
    no factory.lock, so an operator can start one on this repo at any
    time and its dir appears inside the window. That was harmless while
    decompose emitted no usage events; #257 gave it real spend, so
    without the kind filter the daemon would charge the queue item for
    money an operator spent by hand, and `max_daily_spend` could halt the
    queue on it.

    What the filter does and does not buy, stated honestly: it removes
    the decompose class completely, but it does NOT make the remaining
    attribution exact. ``.kstrl/factory.lock`` excludes concurrent
    factory EXECUTION, not concurrent factory DIRS - ``--force-lock``
    runs a second factory by design, and the embedded dashboard mkdirs
    its run dir in ``run_embedded`` before ``run_factory`` takes the
    lock. A foreign factory-kind dir can still land in the window. That
    hole predates #257 and is unchanged by it; what #257 changed, and
    this restores, is that decompose dirs now carry real money.

    That the window can hold more than one dir is exactly why the return
    is a :class:`LaunchSpend`: see its docstring for what does and does
    not survive being added together.
    """
    owned = sorted(
        rid for rid in run_dir_names(root_dir) - runs_before if run_kind(rid) == SPAWNED_RUN_KIND
    )
    return owned, LaunchSpend.over([read_run_spend(root_dir, rid) for rid in owned])


def run_dir_names(root_dir: Path) -> frozenset[str]:
    """Names of every run directory currently on disk.

    Every kind, deliberately: this is the raw disk fact. Snapshotted
    before and after a launch so the daemon can tell which dirs are new
    (#186 F2), but "new" alone does not mean "ours" - ``owned_run_spend``
    narrows the difference to ``SPAWNED_RUN_KIND`` before charging it,
    and states why there.
    """
    runs_root = state_dir(root_dir) / "runs"
    try:
        return frozenset(entry.name for entry in runs_root.iterdir() if entry.is_dir())
    except OSError:
        return frozenset()


# ---------------------------------------------------------------------------
# Classification - the money-critical decision
# ---------------------------------------------------------------------------


def budget_halt_reason(root_dir: Path, owned_run_ids: Sequence[str]) -> str:
    """Why an owned run halted on a RUN-LEVEL ceiling, or "" if none did.

    Covers the token and cost ceilings, which emit ``BudgetExceeded``.
    It does NOT cover the adversarial-call cap: R10.5 (#226) halts a
    hard-mode review or security phase on an exhausted
    ``max_adversarial_calls`` without emitting an event for it
    (doctrine 6), so this function is blind to that halt by
    construction. ``_classify_failed_components`` reads it from the
    manifest instead, and the two together are what make every
    deliberate ceiling terminal.

    Found by the first live `ks serve` run, and it is the most expensive
    thing this module could have got wrong. `pipeline.fail_for_budget`
    records a blown ceiling as ``Finding.infrastructure_error`` with no
    distinguishing category, so the classifier read a deliberate
    "stop spending" as transient infrastructure trouble and RETRIED it.

    A budget halt is deterministic, not transient. The retry re-runs the
    same work against the same ceiling, and retries cost MORE because
    they carry accumulated context ($3.99-7.42 versus $1.70-2.60
    measured). Three attempts of that is the crash loop R8.6 exists to
    prevent, arrived at through the one branch meant to be safe.

    Read from the typed ``BudgetExceeded`` event rather than by matching
    the finding's prose, so this cannot drift when the wording changes.
    """
    from kstrl import events as ev
    from kstrl.reducer import read_run_dir

    for run_id in owned_run_ids:
        if not run_id:
            continue
        try:
            events = read_run_dir(state_dir(root_dir) / "runs" / run_id)
        except OSError:
            continue
        for event in events:
            if isinstance(event, ev.BudgetExceeded):
                named = ", ".join(event.ceilings) or event.ceiling or "budget"
                # BudgetExceeded also carries condition="unenforceable",
                # where NO threshold was crossed - the ceiling simply
                # cannot fire because nothing reports that axis. Telling
                # that operator to "raise the ceiling" points them at a
                # breach that never happened (#197 M2). Routed through the
                # shared classifier so this cannot drift from the reducer's
                # and the Linear sink's reading of the same payload.
                kind = ev.budget_halt_kind(
                    event.condition,
                    event.ceilings,
                    event.ceiling,
                )
                if kind == "unenforceable":
                    return (
                        f"the run halted in {run_id} because no configured "
                        f"ceiling ({named}) can still fire: no metered call "
                        "reported that axis, so the cap could never stop the "
                        "spend. Fix the coverage or the configuration - "
                        "raising a limit would change nothing"
                    )
                return (
                    f"the run halted on a configured ceiling ({named}) in "
                    f"{run_id}: raising the ceiling or narrowing the spec is "
                    "a human decision, and retrying would re-run the same "
                    "work against the same limit at a higher cost"
                )
    return ""


def _infra_casualty(component: Any) -> bool:
    """Whether a component's failure was infrastructural.

    Mirrors ``factory._infra_casualty`` exactly, and reuses the same
    ``Finding.is_infrastructure_error`` predicate rather than
    re-deriving it. Two copies of this rule that drift apart is how a
    spec failure becomes retryable, so the shared predicate is the whole
    point.
    """
    return any(f.is_infrastructure_error for f in component.findings)


def classify_run(
    root_dir: Path,
    *,
    run: RunOutcome,
    manifest_path: Path | None,
    owned_run_ids: Sequence[str] = (),
) -> Outcome:
    """Decide what a finished factory run means for its queue item.

    Positive evidence only. Every branch that cannot prove the failure
    was infrastructural returns a non-retrying verdict, and the reason
    string always says which branch fired so a human reading the inbox
    can audit the decision.
    """
    if run.launch_error:
        # The factory never started, so nothing was spent. Retrying is
        # free, which is what makes this safe to retry at all.
        return Outcome(
            Verdict.RETRY_INFRA,
            f"launch failed before any spend: {run.launch_error}",
            {"launch_error": run.launch_error},
        )

    # Checked BEFORE every retry-authorizing branch, and immediately after
    # the launch-error case (the one place where no child artifacts can
    # exist). Review #197 M1: placing it after the timeout / signal /
    # exit-2 branches meant a run that blew its ceiling and then hung long
    # enough for factory_timeout_seconds to kill it was requeued against
    # the very ceiling this verdict exists to make terminal.
    halted = budget_halt_reason(root_dir, owned_run_ids)
    if halted:
        return Outcome(
            Verdict.BUDGET_HALT,
            halted,
            {"returncode": run.returncode, "owned_run_ids": list(owned_run_ids)},
        )

    if run.timed_out:
        return _timeout_outcome(manifest_path, run.returncode)

    if run.returncode == 0:
        return Outcome(Verdict.SUCCESS, "factory exited 0", {"returncode": 0})

    if run.returncode < 0:
        # Killed by a signal: SIGKILL from an OOM, a suspend that took
        # the process with it, or an operator. That is affirmative
        # evidence of an EXTERNAL cause rather than a verdict on the
        # spec, so it is the one crash shape that legitimately retries.
        return Outcome(
            Verdict.RETRY_INFRA,
            f"killed by signal {-run.returncode} (external cause, not a verdict on the spec)",
            {"signal": -run.returncode},
        )

    if run.returncode == 2:
        # The factory's own "refused to proceed" code, which covers BOTH
        # an architect halt on a blocker-severity spec issue and refusal
        # because another run holds .kstrl/factory.lock. Those need
        # opposite treatment, and a pre-launch lock probe cannot tell
        # them apart: the probe releases the lock, so a manual factory
        # can take it in the gap before our child does (#186 F6, a real
        # TOCTOU on exactly the distinction this branch relies on).
        #
        # So decide from the child's own output, which is evidence, not
        # inference - and stay UNCLASSIFIABLE when neither marker is
        # present rather than guessing.
        tail = run.output_tail
        if _LOCK_REFUSAL_MARKER in tail:
            return Outcome(
                Verdict.RETRY_INFRA,
                "factory exited 2 because another run held the run lock; "
                "contention is infrastructural",
                {"returncode": 2, "cause": "lock_contention"},
            )
        if _SPEC_BLOCKER_MARKER in tail:
            return Outcome(
                Verdict.SPEC_FAILURE,
                "factory exited 2: the architect halted on a blocker-severity "
                "spec issue; the spec needs a human",
                {"returncode": 2, "cause": "spec_blocker"},
            )
        return Outcome(
            Verdict.UNCLASSIFIABLE,
            "factory exited 2 but its output named neither a spec blocker "
            "nor lock contention; refusing to guess which refusal it was",
            {"returncode": 2, "output_tail": tail[-500:]},
        )

    # Everything else needs the manifest to say something specific - and
    # it must be a manifest THIS invocation wrote. ``None`` means the run
    # produced no owned artifacts, so there is nothing to read and the
    # honest verdict is unclassifiable (#186 F2: an empty run id used to
    # fall back to the newest run on disk).
    if manifest_path is None:
        return Outcome(
            Verdict.UNCLASSIFIABLE,
            f"exit {run.returncode} and this invocation produced no run "
            "artifacts of its own; refusing to classify from another run\u2019s "
            "manifest",
            {"returncode": run.returncode},
        )
    try:
        manifest = Manifest.load(manifest_path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return Outcome(
            Verdict.UNCLASSIFIABLE,
            f"exit {run.returncode} and the manifest could not be read "
            f"({exc}); refusing to guess whether this was infrastructural",
            {"returncode": run.returncode, "manifest": str(manifest_path)},
        )

    failed = [comp for comp in manifest.components if str(comp.status) == "failed"]
    if not failed:
        # Nonzero exit with nothing blamed: unconfirmed merges, contract
        # failures, a stop mid-run. Each may well be resumable, but none
        # is an infrastructure_error, and inventing that label here is
        # exactly the fail-open shape this module refuses.
        return Outcome(
            Verdict.UNCLASSIFIABLE,
            f"exit {run.returncode} with no failed component to attribute it "
            "to (unconfirmed merge, contract failure, or an interrupted "
            "run); a human decides whether to resume",
            {
                "returncode": run.returncode,
                "statuses": sorted({str(c.status) for c in manifest.components}),
            },
        )

    # R10.5 (#226). Checked FIRST among the manifest branches, because
    # every budget-halted component carries an infrastructure_error
    # finding and would otherwise reach the RETRY_INFRA return at the
    # bottom of _merits_outcome.
    budget = _budget_halt_outcome(failed, run.returncode)
    if budget is not None:
        return budget
    return _merits_outcome(failed, run.returncode)


def _timeout_outcome(manifest_path: Path | None, returncode: int) -> Outcome:
    """What a run WE killed on ``factory_timeout_seconds`` classifies as.

    Same rule as the ``budget_halt_reason`` check above ``classify_run``\'s
    timeout branch, one branch over. A run that halted a component on
    ``max_adversarial_calls`` and THEN hung long enough for the timeout
    to kill it is still a run that reached the cap, and requeuing it pays
    a whole run to re-reach a counter that starts again at zero. That is
    the shape #197 M1 already fixed once for the token and cost ceilings.

    The manifest is written by the pipeline from its own counter before
    the kill, so it is evidence about THIS run rather than inference. It
    is read for positive evidence only: no path, no file, or a file that
    will not parse all answer "no evidence" and leave the timeout verdict
    standing, which is what this branch returned for every run before
    #226. A separate function rather than two more branches inside
    ``classify_run``, which is already at the cyclomatic ratchet.
    """
    if manifest_path is not None:
        try:
            manifest = Manifest.load(manifest_path)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            manifest = None
        if manifest is not None:
            failed = [comp for comp in manifest.components if str(comp.status) == "failed"]
            halted = _budget_halt_outcome(failed, returncode)
            if halted is not None:
                return halted
    # WE killed it. A hang is an infrastructure symptom, and max_attempts
    # plus the daily budget bound the exposure.
    return Outcome(
        Verdict.RETRY_INFRA,
        "run exceeded serve.factory_timeout_seconds and was killed",
        {"timed_out": True},
    )


def _budget_halt_outcome(failed: Sequence[Component], returncode: int) -> Outcome | None:
    """BUDGET_HALT when the adversarial call cap ended the run, else None.

    The cap halting a hard-mode review or security phase is a deliberate
    "stop spending", not transient trouble. Reading it as the latter is
    the mistake ``budget_halt_reason`` exists to prevent, and that
    function cannot see this halt: it emits no ``BudgetExceeded`` event
    (doctrine 6), so the record is ``Component.failed_check`` in the
    manifest. Positive evidence from a field the pipeline sets from its
    own counter, never from anything a model reported.

    ANY halted component, not every: the counter is per pipeline
    instance and starts again at zero on the retry, so the retry gets
    exactly as far and stops at the same component. A manifest mixing a
    budget halt with a genuine infrastructure casualty would pay a whole
    run to re-reach the same cap, and retries cost more than the first
    attempt because they carry accumulated context.
    """
    halted = [comp.id for comp in failed if comp.failed_check == ADVERSARIAL_BUDGET_CHECK]
    if not halted:
        return None
    others = [comp for comp in failed if comp.failed_check != ADVERSARIAL_BUDGET_CHECK]
    # Written through the same two helpers _merits_outcome uses, and for
    # the same reason (#197 M3): a component that failed for another
    # cause must not lose its CAUSE just because this branch fired
    # first. An id alone sent a real operator looking in the wrong
    # place, and a sibling that produced no finding has nothing but
    # ``Component.error`` to be read from.
    other_evidence = _unevidenced_evidence(others, returncode)
    detail = _unevidenced_detail(others)
    also = f"; also failed, for other reasons - {detail}" if detail else ""
    return Outcome(
        Verdict.BUDGET_HALT,
        "the run halted on max_adversarial_calls: "
        + ", ".join(halted)
        + " reached a hard-mode review or security phase with the adversarial "
        "call budget already spent. Raise the cap or set the mode to advisory; "
        "retrying would re-run the same work against the same cap" + also,
        {
            "returncode": returncode,
            "budget_halted": halted,
            # Named ``other_failures`` rather than the helper's own key:
            # a sibling here may well carry findings, so calling it
            # unevidenced would be a claim this branch has not checked.
            "other_failures": other_evidence["unevidenced_failures"],
            "component_errors": other_evidence["component_errors"],
        },
    )


def _merits_outcome(failed: Sequence[Component], returncode: int) -> Outcome:
    """Whether named failures were the spec's fault, unproven, or infra.

    Split out of ``classify_run`` by #226 round 2, which added the branch
    above it and would otherwise have pushed that function past the
    cyclomatic ratchet, with the two duplicated joins written once. The
    verdicts, their order and their text are the ones that were here
    before the split; ``run.returncode`` became a parameter, and the
    ``sibling_note`` guard reads ``detail`` rather than ``unevidenced``,
    which is the same condition because every element of ``detail``
    formats to at least ``": no error recorded"``.
    """
    # A component that produced FINDINGS, none of them infrastructural, is
    # positive evidence of a merits-based failure. A component that
    # produced NO findings at all is not evidence of anything - and
    # claiming otherwise sent a real operator looking in the wrong place.
    #
    # Found by the first live `ks serve` run: a git worktree creation
    # failure ("fatal: invalid reference") set Component.error and emitted
    # no Finding, and this branch reported it as "failed on their own
    # merits, not on infrastructure". Both verdicts poison, so the money
    # behaviour was already right; the STATEMENT was false. Every unit
    # test had constructed manifests WITH findings, so nothing caught it.
    judged = [comp.id for comp in failed if comp.findings and not _infra_casualty(comp)]
    unevidenced = [comp for comp in failed if not comp.findings]
    # Collected BEFORE the SPEC_FAILURE return: a mixed manifest used to
    # report only the component with a finding, silently dropping a
    # sibling's "fatal: invalid reference" from both the reason and the
    # evidence - recreating the exact operator misdirection this change
    # exists to remove (#197 M3).
    detail = _unevidenced_detail(unevidenced)
    sibling_note = f"; also failed with no finding to explain it - {detail}" if detail else ""
    evidence = _unevidenced_evidence(unevidenced, returncode)
    if judged:
        return Outcome(
            Verdict.SPEC_FAILURE,
            "spec-level failure: "
            + ", ".join(judged)
            + " failed on their own merits, not on infrastructure"
            + sibling_note,
            {**evidence, "judged_failures": judged},
        )

    if unevidenced:
        return Outcome(
            Verdict.UNCLASSIFIABLE,
            f"failed with no finding to attribute it to, so the cause is unproven - {detail}",
            evidence,
        )

    return Outcome(
        Verdict.RETRY_INFRA,
        "every failed component carried an infrastructure_error finding: "
        + ", ".join(comp.id for comp in failed),
        {
            "returncode": returncode,
            "infra_failures": [comp.id for comp in failed],
        },
    )


def _unevidenced_detail(unevidenced: Sequence[Component]) -> str:
    """``id: error`` for each component the caller can only name.

    One writer for what was the same join written twice, so the reason a
    human reads in the inbox cannot say two different things about the
    same components. ``_budget_halt_outcome`` is the third caller and
    passes siblings that may carry findings: ``Component.error`` is set
    for those too, and it is the whole record for a component that
    produced no finding at all.
    """
    return "; ".join(f"{comp.id}: {comp.error or 'no error recorded'}" for comp in unevidenced)


def _unevidenced_evidence(unevidenced: Sequence[Component], returncode: int) -> dict[str, Any]:
    """The evidence a verdict records about failures it can only name.

    Same reason as ``_unevidenced_detail``: it was written out twice,
    and the inbox item is what a human reads before deciding to requeue.
    ``_budget_halt_outcome`` reads the two lists out under its own key
    names rather than merging the dict, because ``unevidenced_failures``
    would be a claim about its siblings that it has not checked.
    """
    return {
        "returncode": returncode,
        "unevidenced_failures": [comp.id for comp in unevidenced],
        "component_errors": {comp.id: comp.error for comp in unevidenced},
    }


# ---------------------------------------------------------------------------
# Lease reaping - sleep and crash recovery
# ---------------------------------------------------------------------------


def _pid_alive(pid: int, host: str) -> bool:
    """Whether a lease holder is still running.

    A lease from ANOTHER host is treated as alive: we cannot probe a
    foreign pid, and two-machine operation is an explicit R8.6 non-goal,
    so the TTL remains the only signal there rather than us reaping work
    that may be in flight elsewhere.

    The probe itself is :func:`kstrl.procgroup.pid_is_alive`. It was a
    fourth hand-rolled copy of the same shape - a pid guard written
    inline, next to a raw signal - which is the arrangement #308 removed
    for groups and #329 found still standing for bare ids. What stays
    here is the only part that is about leases: the host check.
    """
    if host and host != socket.gethostname():
        return True
    return pid_is_alive(pid)


@dataclass(frozen=True)
class ReapResult:
    """What one reaper pass did, for logging and tests."""

    requeued: tuple[str, ...] = ()
    poisoned: tuple[str, ...] = ()
    failed_for_retry: tuple[str, ...] = ()


def reap_leases(
    queue: Queue,
    *,
    now: datetime | None = None,
    actor: str = "reaper",
) -> ReapResult:
    """Recover items whose owner died or slept through its lease.

    The LEASED and RUNNING cases are genuinely different and that
    difference is the reason PR 1 kept two states:

    - A dead ``leased`` item spent nothing (the attempt is charged on the
      transition INTO running), so it goes back to ``queued`` untouched.
    - A dead ``running`` item spent real money. Its attempt is already
      charged, so it is treated as an infrastructure casualty - which is
      what a suspend or an OOM kill actually is - and retried only if it
      has attempts left. Otherwise it poisons.
    """
    moment = now or _utc_now()
    requeued: list[str] = []
    poisoned: list[str] = []
    failed: list[str] = []

    for item in queue.items((ItemState.LEASED,)):
        if item.lease_expired(moment) or not _pid_alive(
            item.lease_pid,
            item.lease_host,
        ):
            queue.requeue(
                item,
                reason="reaped: lease holder gone before any spend",
                actor=actor,
                not_before="",
            )
            requeued.append(item.item_id)

    for item in queue.items((ItemState.RUNNING,)):
        if not (item.lease_expired(moment) or not _pid_alive(item.lease_pid, item.lease_host)):
            continue
        detail = (
            f"run interrupted (lease holder pid {item.lease_pid} on "
            f"{item.lease_host or 'unknown host'} is gone or its lease "
            "lapsed); classified as infrastructure"
        )
        queue.finish_failed(item, error=detail, actor=actor)
        reread = queue.get(item.item_id)
        if reread is None:
            continue
        if reread.attempts_remaining > 0:
            queue.requeue(
                reread,
                reason="reaped: retrying an interrupted run",
                actor=actor,
                not_before=_iso(moment + timedelta(seconds=backoff_seconds(reread.attempts))),
            )
            failed.append(item.item_id)
        else:
            queue.poison(
                reread,
                reason=(f"{detail}; no attempts left ({reread.attempts}/{reread.max_attempts})"),
                actor=actor,
            )
            poisoned.append(item.item_id)

    return ReapResult(
        requeued=tuple(requeued),
        poisoned=tuple(poisoned),
        failed_for_retry=tuple(failed),
    )


# ---------------------------------------------------------------------------
# Merge disposition - the human gate must survive continuous intake
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeGate:
    """The merge-gate decision for one item.

    ``refusal`` non-empty means the item must NOT run: the autonomy
    ladder would auto-merge something that explicitly asked for a human,
    and silently proceeding is precisely the governance erosion R8.6
    lists as a failure mode.
    """

    pause_before_pr_merge: bool
    notes: tuple[str, ...] = ()
    refusal: str = ""


def resolve_merge_gate(item: QueueItem, root_dir: Path) -> MergeGate:
    """Reconcile the item's merge disposition with the autonomy ladder.

    Two directions, and they are not symmetric:

    - The item asks for AUTO_MERGE and the ladder withholds it: downgrade
      to a human gate. This is the ladder doing its job - it may always
      withhold a permission.
    - The item asks for STOP_AT_PR and the ladder's bundle forces
      ``pause_before_pr_merge=False`` (L3+): the ladder would GRANT
      auto-merge over an explicit request for a human. Refuse the item.

    The second case cannot be fixed here: ``run_factory`` assigns
    ``factory_config.pause_before_pr_merge = bundle.pause_before_pr_merge``
    unconditionally, so passing the flag would be overridden and logged
    as a "manual override ignored". Making the ladder honour a
    MORE-restrictive request is an R8.2 change, not an R8.6 one, so this
    refuses loudly instead of quietly letting a merge through.
    """
    from kstrl.autonomy import AutonomyConfig, AutonomyState, flag_bundle_for, resolve_runtime_level
    from kstrl.policy import PolicyConfig

    wants_gate = item.merge_disposition is MergeDisposition.STOP_AT_PR
    config = AutonomyConfig.load(root_dir)
    if not config.enabled:
        # No ladder: the item's own disposition is authoritative.
        return MergeGate(pause_before_pr_merge=wants_gate)

    policy = PolicyConfig.load(root_dir)
    level, clamps = resolve_runtime_level(
        AutonomyState.load(root_dir),
        config,
        policy_enabled=policy.enabled,
        root_dir=root_dir,
    )
    bundle = flag_bundle_for(level)
    notes = list(clamps)

    if not wants_gate and not bundle.auto_merge_when_green:
        notes.append(
            f"item requested auto-merge; {bundle.level.label} withholds it, "
            "so the PR waits for a human"
        )
        return MergeGate(pause_before_pr_merge=True, notes=tuple(notes))

    if wants_gate and not bundle.pause_before_pr_merge:
        return MergeGate(
            pause_before_pr_merge=True,
            notes=tuple(notes),
            refusal=(
                f"item requires a human merge gate but {bundle.level.label} "
                "auto-merges when green, and run_factory lets the ladder's "
                "bundle override the flag. Set the item to --auto-merge "
                "deliberately, or lower [autonomy] max_level, rather than "
                "having the gate removed silently."
            ),
        )

    return MergeGate(
        pause_before_pr_merge=bundle.pause_before_pr_merge,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# The default runner: a subprocess, under caffeinate
# ---------------------------------------------------------------------------


def caffeinate_prefix(enabled: bool) -> list[str]:
    """``caffeinate -i`` when it is available and wanted.

    ``-i`` prevents idle SLEEP without keeping the display awake. Held
    only for the duration of one run (it wraps the child process, so it
    dies with it), which is what lets the laptop sleep between items
    instead of being pinned awake by the daemon itself.
    """
    if not enabled or sys.platform != "darwin":
        return []
    binary = shutil.which("caffeinate")
    return [binary, "-i"] if binary else []


def subprocess_factory_runner(
    *,
    root_dir: Path,
    spec_path: Path,
    project_name: str,
    pause_before_pr_merge: bool,
    timeout_seconds: float,
    on_spawn: Callable[[int], None] | None = None,
    caffeinate: bool = True,
) -> RunOutcome:
    """Run ``ks factory`` as a supervised child process GROUP.

    A subprocess rather than an in-process ``run_factory`` call for three
    reasons: a crash or OOM in a run cannot take the daemon with it, the
    ``.kstrl/factory.lock`` flock is released by process death whatever
    happens, and a timeout is enforceable. The cost of that choice is
    that classification reads artifacts from disk instead of holding a
    ``FactoryResult`` - which is also what makes classification work
    after a crash.

    **Why a process group and not ``subprocess.run(timeout=...)``.**
    That helper signals only its DIRECT child. On macOS the direct child
    is the ``caffeinate`` wrapper, so the factory itself is a grandchild
    and outlived the timeout while this code recorded an infrastructure
    failure and requeued the item - two factories on one repo, which is
    exactly what ``factory.lock`` exists to prevent (#186 F1, reproduced:
    descendants were still running after ``TimeoutExpired``). So:
    ``start_new_session=True`` puts the child in its own process group,
    and the timeout path signals the GROUP and waits for it.

    ``on_spawn`` receives the child's pid so the caller can make it the
    lease owner. Without that the lease records the DAEMON's pid, and a
    successor daemon would see the daemon gone, judge the lease dead, and
    requeue a run that is still executing.
    """
    command = [
        *caffeinate_prefix(caffeinate),
        sys.executable,
        "-m",
        "kstrl",
        "factory",
        "--spec",
        str(spec_path),
        "--project-name",
        project_name,
        "--root",
        str(root_dir),
        "--yes",
        "--no-tui",
        "--ui",
        "plain",
        "--no-color",
    ]
    command.append(
        "--pause-before-pr-merge" if pause_before_pr_merge else "--no-pause-before-pr-merge"
    )
    env = dict(os.environ)
    env["KSTRL_NO_TUI"] = "1"

    return run_supervised(
        command,
        cwd=root_dir,
        env=env,
        timeout_seconds=timeout_seconds,
        on_spawn=on_spawn,
    )


#: How long a killed factory child is given to hand back its output
#: before it is abandoned to ``procdispose``. Was a bare ``10`` inline; it
#: is named because it is the last bound on a timed-out run, and it sits
#: ABOVE its two uses because a reader who meets the name first has to
#: scroll to find out what it is worth.
ABANDON_GRACE_SECONDS = 10.0


def run_supervised(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 0.0,
    on_spawn: Callable[[int], None] | None = None,
) -> RunOutcome:
    """Run ``command`` in its own process group, enforcing a deadline.

    Split out of :func:`subprocess_factory_runner` so the supervision -
    the money-critical half - is testable without spawning a factory. The
    runner above only builds the argv.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # The whole point: an own session/group so descendants are
            # signallable together.
            start_new_session=True,
        )
    except OSError as exc:
        return RunOutcome(returncode=-1, launch_error=str(exc))

    # Captured now, while the child is certainly alive: after it is
    # reaped the pgid is unrecoverable (#186 F1).
    pgid: int | None = safe_pgid(process)

    if on_spawn is not None:
        on_spawn(process.pid)

    try:
        output, _ = process.communicate(
            timeout=timeout_seconds if timeout_seconds > 0 else None,
        )
        return RunOutcome(
            returncode=process.returncode,
            output_tail=_tail(output),
        )
    except subprocess.TimeoutExpired:
        termination = terminate_process_group(process, pgid)
        # #326: this used to drain the pipes itself and, when the drain
        # expired, set `output = ""` and drop the child with no close and
        # no register. The daemon holds its singleton lock for its whole
        # life, so a child left to `Popen.__del__` here is the worst
        # placed one in the tree: under `PYTHONWARNINGS=error` that
        # `__del__` raises before it registers anything and the zombie
        # outlives the daemon.
        output, _ = drain_or_abandon(process, ABANDON_GRACE_SECONDS)
        return RunOutcome(
            returncode=(process.returncode if process.returncode is not None else -9),
            timed_out=True,
            output_tail=_tail(output),
            group_reaped=termination.reaped,
            group_reap_detail=termination.degraded,
            group_occupied_detail=termination.occupied,
        )
    except BaseException:
        # The same rule as `verify.run_scrubbed` and
        # `procgroup._read_ps`: a timeout is not the only way out of a
        # `communicate`. This one matters most of the three, because the
        # daemon holds its singleton lock for its whole process
        # lifetime, so a child dropped here outlives every run after it.
        drain_or_abandon(process, ABANDON_GRACE_SECONDS)
        raise


#: How much of a child's output to keep. Enough for the exit-2 markers
#: and a human-readable tail, not enough to hold a whole factory log.
OUTPUT_TAIL_CHARS = 8000


def _tail(output: str | None) -> str:
    if not output:
        return ""
    return output[-OUTPUT_TAIL_CHARS:]


#: Grace period between SIGTERM and SIGKILL for a run's process group.
GROUP_TERM_GRACE_SECONDS = 15.0

#: How long the escalation waits after SIGKILL before reporting the group
#: as still occupied. A DIFFERENT quantity from
#: ``ABANDON_GRACE_SECONDS`` despite both reading 10.0 - that one bounds
#: a drain of a child's output, this one bounds a wait for a group to
#: empty - so they are two names rather than one shared constant.
GROUP_KILL_GRACE_SECONDS = 10.0


#: How often to re-check the group while waiting out a signal's grace
#: period. Each check forks ``ps`` at ~11ms, so 0.25s costs ~4% of a core
#: on a path that runs once per timed-out run.
GROUP_POLL_SECONDS = 0.25


@dataclass(frozen=True)
class GroupTermination:
    """Whether the group was confirmed reaped, and what is known if not.

    A bare bool was not enough: `serve_cycle` turns `reaped=False` into a
    poison reason telling the operator a factory may still be running,
    and the three ways of not being sure need different words. The two
    strings are kept apart because conflating them told the operator the
    OPPOSITE of the truth in the one case where they must act.
    """

    reaped: bool
    #: Why the check could not MEASURE. Empty when it did. This is the
    #: "unknown" case: an unreadable `ps`, a filtered listing.
    degraded: str = ""
    #: Positive evidence the group IS occupied. EPERM from `killpg` means
    #: the kernel found processes in that group and refused us, so the
    #: group is definitely not empty. Reporting that as "unknown" would
    #: downgrade the strongest signal a factory is alive.
    occupied: str = ""


def terminate_process_group(
    process: subprocess.Popen[str],
    pgid: int | None = None,
) -> GroupTermination:
    """SIGTERM then SIGKILL a child's whole process group.

    ``reaped`` is False when the group could not be confirmed gone, so a
    caller never reports a timed-out run as finished while a factory may
    still be writing to the repo (#186 F1).

    ESCALATION IS DRIVEN BY THE GROUP, NOT BY THE DIRECT CHILD. An
    earlier version returned as soon as ``process.wait()`` returned, which
    made the SIGKILL leg reachable only when the DIRECT CHILD outlived the
    grace period. Measured: a leader that dies on SIGTERM with a
    descendant holding ``SIG_IGN`` for it returned
    ``GroupTermination(reaped=False)`` in 0.07s with the descendant still
    running and SIGKILL never sent, so nothing ever killed it and a
    factory kept writing to the repo. That is the exact hazard this
    function exists to prevent, so the grace period is now spent watching
    the GROUP and the next signal is sent whenever anything is left.

    ``pgid`` should be captured at SPAWN time. Looking it up here fails
    once the direct child has been reaped - and that is precisely the
    case where descendants may still be alive, so deriving it lazily
    would report "reaped" for the one situation this function exists to
    detect. Caught by the test that kills only the direct child.
    """
    if pgid is None:
        pgid = safe_pgid(process)
    if pgid is None:
        # No group to signal and no id to check. Whether the direct child
        # is gone is the most that can honestly be said, and saying so is
        # not the same as having measured the group.
        return GroupTermination(
            process.poll() is not None,
            degraded=(
                "no process-group id was available, so only the direct "
                "child was inspected and any descendant it left is "
                "unaccounted for"
            ),
        )

    outcome = GroupTermination(False, degraded="the group was never signalled")
    for sig, grace in (
        (signal.SIGTERM, GROUP_TERM_GRACE_SECONDS),
        (signal.SIGKILL, GROUP_KILL_GRACE_SECONDS),
    ):
        attempt = signal_group(pgid, sig)
        if attempt.vanished:
            return GroupTermination(True)
        if not attempt.sent:
            # EPERM is not "could not measure": the kernel FOUND processes
            # in this group and refused us. That is evidence of a live
            # group, and the operator has to act on it. A pgid `procgroup`
            # itself refuses is the OTHER column - evidence of nothing,
            # only that this integer must not be signalled - which is
            # #329: this used to take the caller's pgid straight to
            # `os.killpg` with no check of any kind.
            return GroupTermination(False, occupied=attempt.denied, degraded=attempt.refused)
        outcome = _wait_out_grace(process, pgid, grace)
        if outcome.reaped:
            return outcome
    return outcome


def _wait_out_grace(
    process: subprocess.Popen[str],
    pgid: int,
    grace: float,
) -> GroupTermination:
    """Reap the direct child, then watch the GROUP until it goes or time runs out."""
    deadline = time.monotonic() + grace
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    outcome = _confirm_group_gone(pgid)
    while not outcome.reaped and time.monotonic() < deadline:
        time.sleep(GROUP_POLL_SECONDS)
        outcome = _confirm_group_gone(pgid)
    return outcome


def _confirm_group_gone(pgid: int) -> GroupTermination:
    alive, degraded = _group_liveness_for_reap(pgid)
    return GroupTermination(not alive, degraded=degraded)


def _group_liveness_for_reap(pgid: int) -> tuple[bool, str]:
    """(is anything RUNNING in ``pgid``, why that answer is degraded).

    A zombie does not count. It has already died and is only waiting to
    be reaped, so a caller asking "may a factory still be writing to this
    repo" is answered No by it, and a signal probe answers Yes (#298; the
    reasoning and the measurements are in ``kstrl.procgroup``).

    FAIL DIRECTION when ``ps`` cannot be trusted: fall back to the signal
    probe. That is nearly the pre-#298 behaviour: #309 round 1 fixed the
    one branch of it that reported an unexplained error as GONE, so the
    claim below about its error direction is now true rather than
    aspirational. That is a deliberate choice
    between three options and not a default. Raising would take the
    daemon down over a diagnostic. Reporting "alive" unconditionally is
    the conservative direction for the one caller, but on a machine with
    no ``ps`` it makes EVERY timed-out run unreapable and therefore
    poisoned, which trades a rare wrong answer for a permanent one. The
    probe is right whenever the group is genuinely gone, and its only
    error is over-reporting alive - the safe direction for
    ``terminate_process_group``, which then declines to call the run
    reaped. So the degraded path is no worse than what this function did
    everywhere before, and the ``ps`` path is exact.

    The degrade is RETURNED, not warned. An earlier version called
    ``warnings.warn`` here, which under ``PYTHONWARNINGS=error`` (a common
    CI setting) RAISES out of the reap check: measured, it escaped
    ``run_supervised``'s ``except subprocess.TimeoutExpired``, which does
    not catch ``UserWarning``, and out of ``serve_cycle``, which wraps
    nothing - so on a machine without ``ps`` one timed-out run crashed the
    daemon. That is the same crash the undecodable-listing test exists to
    prevent, through a different door. The message also interpolated the
    pgid and an exception repr, so every such run added a distinct
    permanent key to ``__warningregistry__``.

    So the reason travels as a value and ``serve_cycle`` reports it
    through the ``ServeObserver`` the daemon already threads everywhere
    else, which is bounded, is the operator's real log channel, and
    cannot raise.
    """
    liveness = read_group_liveness(pgid)
    if liveness.live is not None:
        return liveness.live, ""
    return signal_probe_alive(pgid), (
        f"{liveness.reason} Falling back to a signal probe, which counts "
        f"an unreaped zombie as alive, so group {pgid} may be reported as "
        f"running when nothing in it is."
    )


def process_group_alive(pgid: int) -> bool:
    """Whether any RUNNING process remains in ``pgid``.

    The bool half of ``_group_liveness_for_reap``, which is where the
    reasoning lives. Callers that record why an answer was degraded want
    that function instead.
    """
    alive, _ = _group_liveness_for_reap(pgid)
    return alive


# ---------------------------------------------------------------------------
# Admission gates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    """Whether the daemon may start another item right now."""

    allowed: bool
    reason: str = ""
    #: Set when the refusal should PAUSE the queue rather than just wait.
    pause_reason: str = ""
    resume_after: str = ""


def check_budget(
    ledger: SpendLedger,
    config: ServeConfig,
    *,
    today: str | None = None,
) -> Admission:
    """The daily-budget hard stop, evaluated BEFORE admitting an item.

    Checked before rather than after because after is a post-mortem: the
    point of a cap is to not spend the money.

    The cap is a LOWER-BOUND cap whenever coverage is partial: it fires
    at or after the threshold, never before. That is a real safety
    property but not an exact one, and the reason string says so rather
    than presenting a floor as a measurement.
    """
    if config.daily_budget_usd <= 0:
        return Admission(allowed=True)
    spend = ledger.read(today)
    if spend.spent_usd < config.daily_budget_usd:
        return Admission(allowed=True)
    floor = _floor_note(spend)
    return Admission(
        allowed=False,
        reason=(
            f"daily budget reached: ${spend.spent_usd:.2f} of "
            f"${config.daily_budget_usd:.2f} over {spend.runs} run(s){floor}"
        ),
        pause_reason=(
            f"daily budget ${config.daily_budget_usd:.2f} reached "
            f"(${spend.spent_usd:.2f} spent{floor})"
        ),
        resume_after=next_local_midnight(),
    )


def _report_reap_degrade(obs: ServeObserver, outcome: RunOutcome) -> None:
    """Say out loud when the reap check could not measure.

    Reported on BOTH paths, and the reaped one is why this exists. When
    ``ps`` is blind the fallback signal probe can answer "gone", the run
    is called reaped and the item is released for another attempt - the
    unsafe direction, reached on a doubly-unverified answer, and until
    this was added it was recorded nowhere at all. The poison reason
    covers the not-reaped path; nothing covered this one.
    """
    if not outcome.timed_out or not outcome.group_reap_detail:
        return
    if outcome.group_reaped:
        obs.warn(
            f"  the timed-out run was released as reaped on an UNMEASURED "
            f"check: {outcome.group_reap_detail}"
        )
        return
    obs.warn(f"  the reap check could not measure: {outcome.group_reap_detail}")


def _unreaped_timeout_detail(outcome: RunOutcome) -> str:
    """The poison reason for a timed-out run whose group was not confirmed gone.

    Three different things are worth three different sentences, and
    collapsing them is how an operator acts on the wrong one. Positive
    evidence the group is occupied must NOT be softened into "unknown";
    an unmeasurable check must not be reported as a live factory.
    """
    detail = (
        "the run timed out and its process group could not be confirmed "
        "reaped, so a factory may still be executing against this repo"
    )
    if outcome.group_occupied_detail:
        return (
            f"{detail}. The group is CONFIRMED occupied, not merely "
            f"unconfirmed: {outcome.group_occupied_detail}"
        )
    if outcome.group_reap_detail:
        return (
            f"{detail}. The check could not measure, so this is 'unknown' "
            f"rather than 'still running': {outcome.group_reap_detail}"
        )
    return detail


def _floor_note(spend: DailySpend) -> str:
    """Say out loud when a total is a floor, and why."""
    if not spend.lower_bound:
        return ""
    parts: list[str] = []
    if spend.uncovered_calls:
        parts.append(f"{spend.uncovered_calls} call(s) reported no cost")
    if spend.unmetered_phases:
        parts.append("unmetered: " + ", ".join(spend.unmetered_phases))
    return f" (a FLOOR: {'; '.join(parts)})"


def check_cost_coverage(
    ledger: SpendLedger,
    config: ServeConfig,
    *,
    today: str | None = None,
) -> Admission:
    """Refuse to run under a budget that can never fire.

    Three cases, and only the third is unenforceable:

    - full coverage: the cap is exact.
    - PARTIAL coverage: the cap is a lower-bound cap. It still fires,
      just later than the threshold. Allowed, and labelled a floor
      everywhere it is reported.
    - ZERO coverage: no call has ever reported a cost figure, so the
      total stays at $0 forever and the cap can never fire. Refused.

    The distinction is drawn from CALL COUNTS, not from whether the
    dollar total is positive: a fully-metered run that legitimately cost
    $0 has perfect coverage (#186 F9).

    Evaluated before the first claim using the persisted
    ``cost_coverage_seen`` flag, so an unenforceable budget is caught
    without spending a run to discover it (#186 F8).
    """
    if config.daily_budget_usd <= 0 or config.allow_uncovered_cost:
        return Admission(allowed=True)
    state = ledger.read_state(today)
    if state.cost_coverage_seen:
        return Admission(allowed=True)
    return Admission(
        allowed=False,
        reason=(
            f"daily_budget_usd is ${config.daily_budget_usd:.2f} but no call "
            "has ever reported a cost figure on this repo, so the cap can "
            "never fire. The unreported spend is deliberately NOT estimated. "
            "Use a cost-reporting agent (the codex adapter reports tokens and "
            "no cost), or set [serve] allow_uncovered_cost = true to accept an "
            "unenforceable budget."
        ),
        pause_reason="daily budget is unenforceable: no cost coverage",
    )


def consecutive_poison_count(ledger: SpendLedger) -> int:
    """Poisons in a row, from the daemon's authoritative state.

    Read from :class:`ServeState`, not from the queue journal. The
    journal is best-effort by design (``Queue._journal`` swallows append
    failures; ``journal_entries`` returns ``[]`` on any read error), so
    deriving a spend backstop from it meant an unreadable journal
    reported a zero streak and re-allowed spending (#186 F5).
    """
    return ledger.read_state().consecutive_poison


def check_poison_breaker(ledger: SpendLedger, config: ServeConfig) -> Admission:
    """Pause everything after a run of poisoned items.

    The failure this catches is systemic rather than per-item: if the
    base branch is broken, every run fails verification, each failure is
    a legitimate spec-level verdict, and no per-item bound ever trips.
    Only a cross-item signal notices, and by then the queue has spent
    once per item.
    """
    streak = consecutive_poison_count(ledger)
    if streak < config.max_consecutive_poison:
        return Admission(allowed=True)
    return Admission(
        allowed=False,
        reason=f"{streak} items poisoned in a row",
        pause_reason=(
            f"{streak} consecutive items poisoned (limit "
            f"{config.max_consecutive_poison}); something systemic is "
            "failing, not one bad spec"
        ),
    )


def check_inbox_cap(root_dir: Path) -> Admission:
    """Stop admitting work when the human queue is already full.

    ``InboxConfig.open_item_cap`` was documented in R8.3 as "the backstop
    R8.6 consults before admitting more queue work"; this is that
    consultation. Producing more decisions for a human who is already
    behind is how an inbox becomes ignored.

    Unparseable inbox lines count as OPEN here (#190). The fold skips
    them by design so one torn write cannot hide the backlog from `ks
    inbox`, but a skipped emission line undercounts open items and a cap
    sitting on that tolerant count admits work past N - a safety gate
    failing open. Only this gate pays the stricter price; the display
    path keeps the tolerant fold.

    Open and unparseable counts come from ONE ``Inbox.scan()`` snapshot
    so a concurrent torn append cannot split them into an admitting
    world. A whole-file read/decode failure is its own state: refuse
    regardless of the configured cap - collapsing it to one skipped
    line would re-admit under any cap greater than one.
    """
    from kstrl.inbox import Inbox, InboxConfig

    config = InboxConfig.load(root_dir)
    if not config.enabled or config.open_item_cap <= 0:
        return Admission(allowed=True)
    scan = Inbox(root_dir, config).scan()
    if scan.unreadable:
        return Admission(
            allowed=False,
            reason=(
                "inbox is unreadable (.kstrl/inbox.jsonl could not be "
                "read or decoded); refusing admission until the log is "
                "inspectable - the open-item cap fails closed, #190"
            ),
        )
    open_count = scan.open_count()
    garbled = scan.unparseable_count()
    if open_count + garbled < config.open_item_cap:
        return Admission(allowed=True)
    detail = (
        f" ({garbled} unparseable inbox line(s) counted as open - the cap "
        "fails closed, #190; inspect .kstrl/inbox.jsonl to clear them)"
        if garbled
        else ""
    )
    return Admission(
        allowed=False,
        reason=(
            f"inbox has reached its open-item cap ({config.open_item_cap}); "
            f"triage before queueing more work{detail}"
        ),
    )


def factory_lock_held(root_dir: Path) -> bool:
    """Whether a factory run already owns this root.

    Probed BEFORE launching rather than discovered from an exit code:
    ``ks factory`` exits 2 both for a held lock and for an architect
    halt on a blocker-severity spec, and those two need opposite
    treatment. Checking here keeps exit 2 unambiguous.
    """
    lock_path = state_dir(root_dir) / "factory.lock"
    if not lock_path.exists():
        return False
    try:
        import fcntl
    except ImportError:
        return False
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        # Fail CLOSED: an unopenable lock file is not evidence that no
        # run owns this root (#186 F6).
        return True
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


@contextmanager
def serve_lock(root_dir: Path) -> Iterator[None]:
    """The daemon singleton lock.

    A THIRD lock, distinct from the queue's per-transition mutex and from
    ``.kstrl/factory.lock``. Held for the daemon's whole lifetime, so two
    ``ks serve`` processes cannot double-lease; the queue mutex stays
    short-lived so ``ks queue ls`` keeps working while the daemon runs.
    """
    lock_path = queue_root(root_dir) / SERVE_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield
        return
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ServeLockedError(f"another ks serve holds {lock_path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass
class CycleResult:
    """What one poll cycle did. Every field is something a test asserts."""

    ran_item: str = ""
    verdict: Verdict | None = None
    reason: str = ""
    #: An item ended in a state only a human can move. Set on EVERY
    #: poison and refusal path, including the reaper's and the merge-gate
    #: refusal, neither of which sets ``ran_item``. Review #186 F10: the
    #: CLI derived its exit status from ``Verdict.may_retry``, which
    #: stays true for an infra verdict whose last attempt was spent, so
    #: `ks serve --once` exited 0 on a poisoned item.
    needs_human: bool = False
    reaped: ReapResult = field(default_factory=ReapResult)
    swept_staging: int = 0
    paused: str = ""
    skipped: str = ""
    charged_usd: float = 0.0
    inbox_items: tuple[str, ...] = ()
    #: Remote refs admitted by the intake stage this cycle.
    synced: tuple[str, ...] = ()
    #: Intake errors. Non-empty does NOT stop the cycle: a front-end
    #: outage must never block the local queue (R8.6).
    sync_errors: tuple[str, ...] = ()


class ServeObserver(Protocol):
    """Where the daemon narrates. Keeps the loop free of UI concerns."""

    def info(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def err(self, message: str) -> None: ...


@dataclass
class _NullObserver:
    lines: list[str] = field(default_factory=list)

    def info(self, message: str) -> None:
        self.lines.append(f"info: {message}")

    def warn(self, message: str) -> None:
        self.lines.append(f"warn: {message}")

    def err(self, message: str) -> None:
        self.lines.append(f"err: {message}")


def _file_inbox_item(
    root_dir: Path,
    *,
    kind_name: str,
    title: str,
    detail: str,
    dedupe_key: str,
    evidence: dict[str, Any],
    run_id: str = "",
) -> str:
    """Record a decision for a human, never failing the caller.

    An inbox write must not be able to undo a queue transition that
    already happened, so failures here degrade to a warning. The queue
    journal remains the authoritative record either way.
    """
    try:
        from kstrl.inbox import Inbox, InboxConfig, ItemKind

        config = InboxConfig.load(root_dir)
        if not config.enabled:
            return ""
        box = Inbox(root_dir, config)
        item = box.add(
            ItemKind(kind_name),
            title,
            detail=detail,
            dedupe_key=dedupe_key,
            evidence=evidence,
            run_id=run_id,
        )
        return item.id
    except (OSError, ValueError, KeyError):
        return ""


def _run_intake(
    root_dir: Path,
    queue: Queue,
    observer: ServeObserver,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pull remote work into the queue, best effort.

    Review #189 F1: without this the daemon drained a queue nothing could
    fill. `ks queue sync` existed but only as a manual command, so an
    installed LaunchAgent could never admit a labelled issue - the
    adapter and the daemon were each correct and their composition did
    nothing.

    Strictly additive, like the adapter itself: every failure is recorded
    and the cycle continues to drain whatever is already queued. A GitHub
    outage must not stop local work.
    """
    try:
        from kstrl.intake_github import GitHubIntakeConfig
        from kstrl.intake_github import sync as intake_sync

        config = GitHubIntakeConfig.load(root_dir)
        if not config.enabled:
            return (), ()
        result = intake_sync(
            queue,
            config,
            root_dir,
            # Poll and authorize unlocked; take the queue mutex only for
            # the local commit, so a slow GitHub cannot block
            # `ks queue pause` or any other transition (#189 N1).
            commit_guard=lambda: queue_lock(root_dir, blocking=True),
        )
    except Exception as exc:  # noqa: BLE001 - additive by contract
        observer.warn(f"GitHub intake raised: {exc}")
        return (), (str(exc),)

    for ref in result.enqueued:
        observer.info(f"Intake queued {ref}")
    for error in result.errors:
        observer.warn(f"  intake: {error}")
    return result.enqueued, result.errors


def _report_remote_outcome(
    root_dir: Path,
    item: QueueItem | None,
    *,
    state: str,
    detail: str,
    observer: ServeObserver,
) -> None:
    """Tell the source front-end what happened, best effort.

    R8.6 requires the adapters be strictly additive, so this swallows
    every failure into a warning: the queue transition it describes has
    already committed locally, and a GitHub outage must not be able to
    roll it back or halt the daemon.
    """
    if item is None or item.source is not ItemSource.GITHUB:
        return
    try:
        from kstrl.intake_github import GitHubIntakeConfig, report_outcome

        config = GitHubIntakeConfig.load(root_dir)
        if not config.enabled:
            return
        error = report_outcome(
            item,
            state=state,
            detail=detail,
            config=config,
            root_dir=root_dir,
        )
        if error:
            observer.warn(f"  writeback to {item.source_ref} failed: {error}")
    except Exception as exc:  # noqa: BLE001 - additive by contract
        observer.warn(f"  writeback to {item.source_ref} raised: {exc}")


def _pause_queue(
    queue: Queue,
    admission: Admission,
    observer: ServeObserver,
    root_dir: Path,
) -> str:
    """Apply a pausing admission decision, under the queue mutex.

    Taken under the lock for the same reason the elapsed-pause clear is
    (#186 F7): a pause write that races an operator\u2019s write can lose one
    of them, and losing the operator\u2019s is the dangerous direction.
    """
    with queue_lock(root_dir, blocking=True):
        queue.pause(
            reason=admission.pause_reason or admission.reason,
            actor="serve",
            resume_after=admission.resume_after,
        )
    observer.warn(f"Queue paused: {admission.pause_reason or admission.reason}")
    return admission.pause_reason or admission.reason


def serve_cycle(
    root_dir: Path,
    *,
    config: ServeConfig | None = None,
    queue_config: QueueConfig | None = None,
    runner: FactoryRunner | None = None,
    observer: ServeObserver | None = None,
    now: datetime | None = None,
) -> CycleResult:
    """One poll cycle: recover, gate, maybe run exactly one item.

    One item per cycle on purpose. Concurrency here would mean two
    factory runs on one repo, and ``.kstrl/factory.lock`` exists
    precisely because that corrupts worktrees and the manifest.

    Order is deliberate. Recovery comes first so a crashed predecessor's
    work is reclaimed before anything new is admitted; every gate is
    checked before the CLAIM, not after, because a gate evaluated after
    the spend is a post-mortem.
    """
    cfg = config or ServeConfig.load(root_dir)
    qcfg = queue_config or QueueConfig.load(root_dir)
    obs: ServeObserver = observer or _NullObserver()
    queue = Queue(root_dir, qcfg)
    ledger = SpendLedger(root_dir)
    moment = now or _utc_now()
    result = CycleResult()

    queue.ensure_dirs()

    # The daemon's own state is consulted by three gates. If it cannot be
    # read, none of them can be evaluated, so stop before claiming
    # anything (#186 F4/F5) rather than proceeding with unknown totals.
    try:
        ledger.read_state()
    except ServeStateError as exc:
        result.skipped = str(exc)
        result.needs_human = True
        obs.err(str(exc))
        result.inbox_items += (
            _file_inbox_item(
                root_dir,
                kind_name="budget_overrun",
                title="Continuous intake halted: unreadable spend ledger",
                detail=str(exc),
                dedupe_key=f"serve-ledger:{ledger.path}",
                evidence={"path": str(ledger.path)},
            ),
        )
        return result

    # 1. Recovery, under the mutex: staging leftovers and dead leases.
    with queue_lock(root_dir, blocking=True):
        result.swept_staging = queue.sweep_staging()
        result.reaped = reap_leases(queue, now=moment)
        for _item_id in result.reaped.poisoned:
            ledger.record_terminal(poisoned=True)
    if result.swept_staging:
        obs.info(f"Swept {result.swept_staging} abandoned staging item(s)")
    for item_id in result.reaped.requeued + result.reaped.failed_for_retry:
        obs.warn(f"Reaped {item_id[:12]}: owner gone, requeued")
    for item_id in result.reaped.poisoned:
        obs.err(f"Reaped {item_id[:12]}: no attempts left, poisoned")
        result.needs_human = True
        _report_remote_outcome(
            root_dir,
            queue.get(item_id),
            state="poison",
            detail=("The run was interrupted and the item had no attempts left."),
            observer=obs,
        )
        result.inbox_items += (
            _file_inbox_item(
                root_dir,
                kind_name="halted_run",
                title=f"Queue item {item_id[:12]} poisoned after an interrupted run",
                detail=(
                    "The run was interrupted and the item had no attempts left. "
                    "Inspect with `ks queue show` and requeue with "
                    "`ks queue retry --reset-attempts` if it should run again."
                ),
                dedupe_key=f"queue-poison:{item_id}",
                evidence={"item_id": item_id, "cause": "interrupted run"},
            ),
        )

    # 2. Pull remote work in BEFORE the gates, so newly-admitted items face
    #    the same budget, breaker and cap checks as everything else. The
    #    mutex is taken INSIDE, around the commit only (#189 N1).
    result.synced, result.sync_errors = _run_intake(root_dir, queue, obs)

    # 3. The pause marker is read AND cleared under the mutex, re-reading
    #    inside it. Review #186 F7: read and clear outside the lock let an
    #    operator's fresh emergency pause be overwritten by the daemon
    #    clearing a previously-expired budget pause, after which the queue
    #    started work the operator had just stopped.
    with queue_lock(root_dir, blocking=True):
        pause = queue.pause_state()
        if pause.paused and not pause.active(moment):
            queue.resume(actor="serve")
            pause = queue.pause_state()
            cleared = True
        else:
            cleared = False
    if cleared:
        obs.info("Pause window elapsed; resuming intake")
    if pause.active(moment):
        result.skipped = f"paused: {pause.reason}"
        return result

    # 4. Gates, all evaluated BEFORE the claim.
    try:
        gates = (
            check_poison_breaker(ledger, cfg),
            check_cost_coverage(ledger, cfg),
            check_budget(ledger, cfg),
        )
    except ServeStateError as exc:
        result.skipped = str(exc)
        result.needs_human = True
        obs.err(str(exc))
        return result

    for admission in gates:
        if admission.allowed:
            continue
        if admission.pause_reason:
            result.paused = _pause_queue(queue, admission, obs, root_dir)
            result.needs_human = True
            result.inbox_items += (
                _file_inbox_item(
                    root_dir,
                    kind_name="budget_overrun",
                    title="Continuous intake paused",
                    detail=admission.reason,
                    dedupe_key=(f"serve-pause:{_local_today()}:{admission.pause_reason[:40]}"),
                    evidence={"reason": admission.reason},
                ),
            )
        result.skipped = admission.reason
        return result

    inbox_gate = check_inbox_cap(root_dir)
    if not inbox_gate.allowed:
        obs.warn(inbox_gate.reason)
        result.skipped = inbox_gate.reason
        return result

    if factory_lock_held(root_dir):
        # Not a failure and not the item's fault: something else owns the
        # repo. Wait rather than charging an attempt. This is a courtesy
        # check only - it cannot make exit 2 unambiguous, which is why
        # classify_run reads the child's output instead (#186 F6).
        result.skipped = "a factory run already holds this root"
        obs.info(result.skipped)
        return result

    # 5. Claim exactly one item.
    refused_item: QueueItem | None = None
    with queue_lock(root_dir, blocking=True):
        candidate = queue.next_ready(moment)
        if candidate is None:
            result.skipped = "nothing ready"
            return result
        gate = resolve_merge_gate(candidate, root_dir)
        if gate.refusal:
            queue.poison(
                candidate,
                reason=f"merge-gate conflict: {gate.refusal}",
                actor="serve",
            )
            ledger.record_terminal(poisoned=True)
            refused_item = queue.get(candidate.item_id)
        else:
            leased = queue.lease(candidate, actor="serve")

    if refused_item is not None:
        obs.err(f"{candidate.item_id[:12]}: {gate.refusal}")
        result.needs_human = True
        result.inbox_items += (
            _file_inbox_item(
                root_dir,
                kind_name="merge_gate",
                title=(f"Queue item {candidate.item_id[:12]} needs a merge decision"),
                detail=gate.refusal,
                dedupe_key=f"queue-merge-gate:{candidate.item_id}",
                evidence={"item_id": candidate.item_id},
            ),
        )
        # Outside the mutex: two gh calls at the configured timeout must
        # not block every local queue transition (#187 F10).
        _report_remote_outcome(
            root_dir,
            refused_item,
            state="poison",
            detail=gate.refusal,
            observer=obs,
        )
        result.skipped = gate.refusal
        return result

    for note in gate.notes:
        obs.warn(f"  {note}")

    # 6. Charge the attempt, then spend. Never the other way round.
    try:
        with queue_lock(root_dir, blocking=True):
            running = queue.start(leased, actor="serve")
    except QueueBudgetExhausted as exc:
        with queue_lock(root_dir, blocking=True):
            queue.poison(leased, reason=str(exc), actor="serve")
            ledger.record_terminal(poisoned=True)
            exhausted_item = queue.get(leased.item_id)
        obs.err(str(exc))
        result.skipped = str(exc)
        result.needs_human = True
        _report_remote_outcome(
            root_dir,
            exhausted_item,
            state="poison",
            detail=str(exc),
            observer=obs,
        )
        return result

    result.ran_item = running.item_id
    # The remote label now tracks the REAL queue state: admission leaves
    # the trigger label alone and `running` is applied here, when the item
    # actually starts (#187 F8).
    _report_remote_outcome(
        root_dir,
        running,
        state="running",
        detail=(
            f"Claimed by the queue as `{running.item_id}` "
            f"(attempt {running.attempts} of {running.max_attempts})."
        ),
        observer=obs,
    )
    obs.info(
        f"Running {running.item_id[:12]} ({running.title}) "
        f"attempt {running.attempts}/{running.max_attempts}, "
        f"merge gate {'on' if gate.pause_before_pr_merge else 'off'}"
    )

    manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
    # Snapshot BEFORE the launch so only runs this invocation created are
    # charged or trusted (#186 F2). Without it, an early failure charged a
    # previous run's spend and could be classified from a stale manifest.
    runs_before = run_dir_names(root_dir)

    def _adopt(child_pid: int) -> None:
        # The child, not the daemon, owns the lease for the duration of
        # the run (#186 F1).
        try:
            with queue_lock(root_dir, blocking=True):
                current = queue.get(running.item_id)
                if current is not None:
                    queue.adopt_lease(current, pid=child_pid, actor="serve")
        except (QueueError, OSError) as exc:
            obs.warn(f"  could not adopt the run\u2019s lease: {exc}")

    run_factory_fn = runner or _default_runner(cfg)
    spec_path = queue.spec_path(running)
    project_name = running.project_name or _derive_project_name(running)
    outcome = run_factory_fn(
        root_dir=root_dir,
        spec_path=spec_path,
        project_name=project_name,
        pause_before_pr_merge=gate.pause_before_pr_merge,
        timeout_seconds=cfg.factory_timeout_seconds,
        on_spawn=_adopt,
    )

    _report_reap_degrade(obs, outcome)

    if outcome.timed_out and not outcome.group_reaped:
        # A descendant may still be running against this repo. Do NOT
        # release the item for another attempt (#186 F1).
        detail = _unreaped_timeout_detail(outcome)
        with queue_lock(root_dir, blocking=True):
            current = queue.get(running.item_id)
            if current is not None:
                queue.poison(current, reason=detail, actor="serve")
                ledger.record_terminal(poisoned=True)
                current = queue.get(running.item_id)
        obs.err(f"  {running.item_id[:12]}: {detail}")
        _report_remote_outcome(
            root_dir,
            current,
            state="poison",
            detail=detail,
            observer=obs,
        )
        result.verdict = Verdict.UNCLASSIFIABLE
        result.reason = detail
        result.needs_human = True
        result.inbox_items += (
            _file_inbox_item(
                root_dir,
                kind_name="halted_run",
                title=f"Queue item {running.item_id[:12]}: orphaned factory process",
                detail=detail,
                dedupe_key=f"queue-orphan:{running.item_id}",
                evidence={"item_id": running.item_id},
            ),
        )
        return result

    # 7. Charge the spend before deciding anything, so a classification
    #    bug cannot also lose the accounting. NEW run dirs are not enough
    #    to go on; `owned_run_spend` says why.
    owned_runs, owned = owned_run_spend(root_dir, runs_before)
    total = owned.cost_usd
    covered_calls = owned.cost_calls
    total_calls = owned.usage_calls

    charged = ledger.charge(
        total,
        covered_calls=covered_calls,
        total_calls=total_calls,
        # Derived per run and unioned, not asserted: since #257 piece B
        # the architect reaches the meter on a run that executed, and
        # `RunSpend.unmetered_phases` states what zero calls does and
        # does not prove.
        unmetered_phases=owned.unmetered_phases,
        metered_run=bool(owned_runs),
    )
    result.charged_usd = total
    obs.info(
        f"  charged ${total:.2f} over {len(owned_runs)} run dir(s)"
        + (
            f"; {total_calls - covered_calls} call(s) reported no cost"
            if total_calls > covered_calls
            else ""
        )
        + f"; today ${charged.spent_usd:.2f}"
        + (" (a floor)" if charged.lower_bound else "")
    )

    # Classification may only read a manifest this invocation produced.
    manifest_run_after = _run_id_from_manifest(manifest_path)
    owns_manifest = bool(manifest_run_after) and manifest_run_after in owned_runs
    verdict = classify_run(
        root_dir,
        run=outcome,
        manifest_path=manifest_path if owns_manifest else None,
        owned_run_ids=owned_runs,
    )
    result.verdict = verdict.verdict
    result.reason = verdict.reason
    evidence = dict(verdict.evidence)
    evidence.update(
        {
            "item_id": running.item_id,
            "owned_run_ids": owned_runs,
            "attempts": running.attempts,
            "max_attempts": running.max_attempts,
            "cost_usd": total,
            "cost_is_lower_bound": charged.lower_bound,
        }
    )

    with queue_lock(root_dir, blocking=True):
        current = queue.get(running.item_id)
        if current is None:
            obs.err(f"{running.item_id[:12]} vanished mid-run")
            return result
        if verdict.verdict is Verdict.SUCCESS:
            queue.finish_ok(current, actor="serve")
            ledger.record_terminal(poisoned=False)
            finished = queue.get(running.item_id)
            succeeded = True
        else:
            finished = None
            succeeded = False

    if succeeded:
        obs.info(f"  {running.item_id[:12]} done")
        # Outside the mutex (#187 F10).
        _report_remote_outcome(
            root_dir,
            finished,
            state="done",
            detail="The factory run completed.",
            observer=obs,
        )
        return result

    # The failure branch. Every remote writeback below happens AFTER the
    # mutex is released (#187 F10): report_outcome makes two gh calls at
    # the configured timeout, and an unavailable GitHub must not block
    # every local queue transition.
    retried = False
    poisoned_item: QueueItem | None = None
    retry_delay = 0.0
    with queue_lock(root_dir, blocking=True):
        current = queue.get(running.item_id)
        if current is None:
            obs.err(f"{running.item_id[:12]} vanished mid-run")
            return result

        queue.finish_failed(current, error=verdict.reason, actor="serve")
        failed = queue.get(running.item_id)
        if failed is None:
            return result

        if verdict.verdict.may_retry and failed.attempts_remaining > 0:
            retry_delay = backoff_seconds(failed.attempts)
            queue.requeue(
                failed,
                reason=f"retry: {verdict.reason}",
                actor="serve",
                not_before=_iso(moment + timedelta(seconds=retry_delay)),
            )
            retried = True
        else:
            exhausted = (
                f"; no attempts left ({failed.attempts}/{failed.max_attempts})"
                if verdict.verdict.may_retry
                else ""
            )
            queue.poison(
                failed,
                reason=f"{verdict.reason}{exhausted}",
                actor="serve",
            )
            ledger.record_terminal(poisoned=True)
            poisoned_item = queue.get(running.item_id)

    if retried:
        obs.warn(
            f"  {running.item_id[:12]} retrying in {int(retry_delay)}s "
            f"({running.attempts}/{running.max_attempts} attempts used): "
            f"{verdict.reason}"
        )
        # A retry IS a state change the remote should see: the item is
        # back in the queue, not running (#187 F8).
        _report_remote_outcome(
            root_dir,
            queue.get(running.item_id),
            state="failed",
            detail=(
                f"{verdict.reason}\n\nRetrying in {int(retry_delay)}s "
                f"({running.attempts} of {running.max_attempts} attempts used)."
            ),
            observer=obs,
        )
        return result

    result.needs_human = True
    obs.err(f"  {running.item_id[:12]} poisoned: {verdict.reason}")
    _report_remote_outcome(
        root_dir,
        poisoned_item,
        state="poison",
        detail=verdict.reason,
        observer=obs,
    )
    result.inbox_items += (
        _file_inbox_item(
            root_dir,
            kind_name="halted_run",
            title=f"Queue item {running.item_id[:12]} poisoned",
            detail=(
                f"{verdict.reason}\n\n"
                f"Verdict: {verdict.verdict}. This item will NOT be retried "
                "automatically. Inspect with `ks queue show "
                f"{running.item_id[:12]}`."
            ),
            dedupe_key=f"queue-poison:{running.item_id}",
            evidence=evidence,
            run_id=owned_runs[-1] if owned_runs else "",
        ),
    )
    return result


def _default_runner(config: ServeConfig) -> FactoryRunner:
    """Bind the caffeinate preference into the subprocess runner."""

    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
        on_spawn: Callable[[int], None] | None = None,
    ) -> RunOutcome:
        return subprocess_factory_runner(
            root_dir=root_dir,
            spec_path=spec_path,
            project_name=project_name,
            pause_before_pr_merge=pause_before_pr_merge,
            timeout_seconds=timeout_seconds,
            on_spawn=on_spawn,
            caffeinate=config.caffeinate,
        )

    return runner


def _derive_project_name(item: QueueItem) -> str:
    """A factory project name for an item that did not supply one.

    Derived from the item id rather than the title: the title is free
    text from a remote issue and would become a branch name.
    """
    return f"queue-{item.item_id.split('-')[-1]}"


def _run_id_from_manifest(manifest_path: Path) -> str:
    """The run id the factory recorded, or "" if it never got that far.

    ONE clause for all three causes: every one of them returns "", the
    caller then falls back to selecting by mtime, and no message is
    emitted about which happened. #320 adds ``UnicodeDecodeError`` here
    rather than a second handler for that reason.
    """
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    # "runId", not "run_id": Manifest.to_dict is camelCase on disk.
    # Review #186 F2 - reading the snake_case key returned "" for every
    # real manifest, which then selected the NEWEST run instead.
    run_id = data.get("runId")
    return run_id if isinstance(run_id, str) else ""


def serve(
    root_dir: Path,
    *,
    once: bool = False,
    config: ServeConfig | None = None,
    queue_config: QueueConfig | None = None,
    runner: FactoryRunner | None = None,
    observer: ServeObserver | None = None,
    max_cycles: int = 0,
    sleeper: Any = None,
) -> list[CycleResult]:
    """Drain the queue, holding the daemon singleton lock.

    ``once`` runs exactly one cycle - the launchd/cron fallback mode, and
    the shape ``ks serve --once`` exposes. ``max_cycles`` bounds the loop
    for tests; 0 means run until interrupted.

    ``once`` returns BEFORE entering the loop rather than breaking out of
    it. That is deliberate: while mutation-testing this module, an
    injected fault in a ``break`` condition turned ``--once`` into an
    unbounded loop that slept 60s per iteration forever. Under launchd
    that is a job which never exits and blocks every later interval, so
    the single-shot path should not depend on a conditional being right.
    """
    cfg = config or ServeConfig.load(root_dir)
    obs: ServeObserver = observer or _NullObserver()
    sleep = sleeper or time.sleep

    def _cycle() -> CycleResult:
        return serve_cycle(
            root_dir,
            config=cfg,
            queue_config=queue_config,
            runner=runner,
            observer=obs,
        )

    with serve_lock(root_dir):
        if once:
            return [_cycle()]

        # A bounded window, not a growing list. Review #186 F11: the
        # unbounded daemon path appended one CycleResult per poll forever
        # with no consumer - the CLI catches KeyboardInterrupt outside
        # this call and discards the value - so memory grew by
        # construction. Bounded runs still return every result.
        if max_cycles:
            bounded: list[CycleResult] = []
            while True:
                bounded.append(_cycle())
                if len(bounded) >= max_cycles:
                    return bounded
                sleep(cfg.poll_interval_seconds)

        recent: deque[CycleResult] = deque(maxlen=RECENT_CYCLE_WINDOW)
        while True:
            recent.append(_cycle())
            sleep(cfg.poll_interval_seconds)


# ---------------------------------------------------------------------------
# launchd packaging
# ---------------------------------------------------------------------------

#: Reverse-DNS label prefix. The per-root suffix keeps two checkouts from
#: fighting over one launchd job.
LAUNCHD_LABEL_PREFIX = "com.kstrl.serve"

#: Minimum seconds launchd waits before RELAUNCHING an exited job.
#: launchd's own default is 10s, which for a crash-looping daemon means
#: six restarts a minute; at a measured $1.70-2.60 per engineer iteration
#: the throttle is a spend control. Note what it is NOT: it does not bound
#: how long a running job may take (review #189 F2).
LAUNCHD_THROTTLE_SECONDS = 60

#: Set by a scheduled (interval-mode) LaunchAgent. Its presence means the
#: job was installed on the promise of a bounded cycle, so `ks serve`
#: refuses to start without one (#189 N3).
REQUIRE_TIMEOUT_ENV = "KSTRL_SERVE_REQUIRE_TIMEOUT"


def launchd_label(root_dir: Path) -> str:
    """A launchd label unique to this checkout.

    Derived from the path rather than the directory name because two
    checkouts of the same repo (a worktree and its parent) would
    otherwise collide on one job, and launchd keeps only the last one
    loaded - silently.
    """
    import hashlib

    digest = hashlib.sha256(str(root_dir.resolve()).encode()).hexdigest()[:10]
    return f"{LAUNCHD_LABEL_PREFIX}.{digest}"


def launchd_log_dir(root_dir: Path) -> Path:
    """Where the LaunchAgent writes stdout/stderr.

    Its own function because the CLI must CREATE it: launchd creates the
    log file but not its parent, and a missing parent makes the job fail
    to spawn with nothing in the log explaining why.
    """
    return state_dir(root_dir) / "logs"


def calendar_schedule(interval_minutes: int) -> list[dict[str, int]]:
    """A ``StartCalendarInterval`` array firing every N minutes.

    ``StartCalendarInterval`` rather than ``StartInterval`` because only
    the former catches up after sleep. ``launchd.plist(5)``:

        StartInterval - "If the system is asleep during the time of the
        next scheduled interval firing, that interval will be missed due
        to shortcomings in kqueue(3)."

        StartCalendarInterval - "Unlike cron which skips job invocations
        when the computer is asleep, launchd will start the job the next
        time the computer wakes up."

    Review #189 F3: the guide previously claimed the opposite, and the
    R8.6 plan had recorded the correct behaviour all along.
    """
    if interval_minutes < 1:
        raise ServeError(f"launchd interval must be >= 1 minute, got {interval_minutes}")
    if interval_minutes <= 60:
        if 60 % interval_minutes:
            raise ServeError(
                f"a {interval_minutes}-minute interval does not divide an "
                "hour evenly, so it cannot be expressed as a calendar "
                "schedule; use a divisor of 60 (1, 2, 3, 4, 5, 6, 10, 12, "
                "15, 20, 30, 60)"
            )
        return [{"Minute": m} for m in range(0, 60, interval_minutes)]
    if interval_minutes % 60 or interval_minutes > 24 * 60:
        raise ServeError(
            f"a {interval_minutes}-minute interval must be a whole number "
            "of hours (and at most 24h) to be expressed as a calendar "
            "schedule"
        )
    step = interval_minutes // 60
    if 24 % step:
        # Review #189 N6: range(0, 24, 5) gives hours 0,5,10,15,20 - gaps
        # of 5,5,5,5,4 across midnight. That is not "every five hours",
        # and a job silently firing on an uneven cadence is worse than one
        # that refuses to be created.
        raise ServeError(
            f"a {step}-hour interval does not divide a day evenly, so the "
            "schedule would be uneven across midnight; use an hour count "
            "that divides 24 (1, 2, 3, 4, 6, 8, 12, 24)"
        )
    return [{"Hour": h, "Minute": 0} for h in range(0, 24, step)]


def launchd_plist_dict(
    root_dir: Path,
    *,
    mode: str = "keepalive",
    interval_minutes: int = 5,
    python: str = "",
    extra_path: str = "",
    factory_timeout_seconds: float = 0.0,
) -> dict[str, Any]:
    """Build the LaunchAgent as a plist DICT.

    A dict fed to :mod:`plistlib` rather than a formatted XML string:
    hand-escaping covered ``&``, ``<`` and ``>`` but not control
    characters, which XML 1.0 cannot represent at all - a checkout path
    containing one produced a plist that failed to parse (review #189
    F4). ``plistlib`` rejects those values outright, which is turned into
    a clear ``ServeError`` by :func:`render_launchd_plist`.

    Two modes, and neither gets a guarantee launchd does not provide:

    - ``keepalive`` - one long-lived ``ks serve`` pacing itself from
      ``[serve] poll_interval_seconds``, relaunched by launchd when it
      EXITS. Sleep is survived because the process simply resumes.
    - ``interval`` - ``ks serve --once`` on a calendar schedule, which
      catches up after wake.

    **launchd bounds neither runtime nor overlap.** ``launchd.plist(5)``:
    "If the job is running during an interval firing, that interval
    firing will likewise be missed." So a wedged cycle is not killed and
    not replaced - it silently blocks every later firing. The only real
    bound is ``[serve] factory_timeout_seconds``, which is why interval
    mode refuses to generate without one (review #189 F2).
    """
    if mode not in ("keepalive", "interval"):
        raise ServeError(f"launchd mode must be 'keepalive' or 'interval', got {mode!r}")
    if mode == "interval" and factory_timeout_seconds <= 0:
        raise ServeError(
            "interval mode needs [serve] factory_timeout_seconds > 0. "
            "launchd does not bound how long a job runs and does not "
            "replace a job still running at the next firing - it just "
            "skips it - so without a timeout one wedged cycle silently "
            "stops every later one."
        )

    root = root_dir.resolve()
    interpreter = python or sys.executable
    log_dir = launchd_log_dir(root)
    args = [interpreter, "-m", "kstrl", "serve", "--root", str(root)]
    if mode == "interval":
        args.append("--once")

    # A LaunchAgent inherits no shell environment; gh and git must be
    # findable or every poll fails silently.
    path_parts = [
        str(Path(interpreter).parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    if extra_path:
        path_parts.insert(0, extra_path)
    deduped: list[str] = []
    for part in path_parts:
        if part and part not in deduped:
            deduped.append(part)

    plist: dict[str, Any] = {
        "Label": launchd_label(root),
        "ProgramArguments": args,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PATH": ":".join(deduped),
            "KSTRL_NO_TUI": "1",
        },
        "RunAtLoad": True,
        "ThrottleInterval": LAUNCHD_THROTTLE_SECONDS,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "serve.out.log"),
        "StandardErrorPath": str(log_dir / "serve.err.log"),
    }
    if mode == "keepalive":
        plist["KeepAlive"] = True
    else:
        plist["StartCalendarInterval"] = calendar_schedule(interval_minutes)
        # Review #189 N3: checking the timeout at GENERATION only binds the
        # config as it was that day. An operator who later clears
        # factory_timeout_seconds leaves an installed job running
        # unbounded, and launchd will not notice. The marker travels WITH
        # the job, so every scheduled invocation re-checks and fails
        # closed.
        env = plist["EnvironmentVariables"]
        assert isinstance(env, dict)
        env[REQUIRE_TIMEOUT_ENV] = "1"
    return plist


def render_launchd_plist(
    root_dir: Path,
    *,
    mode: str = "keepalive",
    interval_minutes: int = 5,
    python: str = "",
    extra_path: str = "",
    factory_timeout_seconds: float = 0.0,
) -> str:
    """Serialize the LaunchAgent plist, refusing values XML cannot carry."""
    import plistlib

    payload = launchd_plist_dict(
        root_dir,
        mode=mode,
        interval_minutes=interval_minutes,
        python=python,
        extra_path=extra_path,
        factory_timeout_seconds=factory_timeout_seconds,
    )
    try:
        return plistlib.dumps(payload).decode("utf-8")
    except (ValueError, TypeError) as exc:
        # plistlib rejects control characters outright, which is the
        # honest place to fail: a path XML cannot represent has no valid
        # plist, and emitting one that fails to parse later is worse.
        raise ServeError(
            f"cannot express this checkout as a launchd plist ({exc}). "
            "The path most likely contains a control character; move the "
            "checkout somewhere with a plain path."
        ) from exc
