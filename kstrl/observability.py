"""Observability - structured progress logging for factory runs."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class ProgressSink(Protocol):
    """Observer of progress-log events (R7.4).

    ``handle_event`` receives the exact dict written as the JSONL line
    (``ts``/``event``/``run_id``/``component``/``data``), so a sink can
    equally be fed live events or a replayed journal. Sinks are
    observability, never control flow: ``ProgressLog.emit`` isolates
    sink exceptions, and a sink must not mutate the event it receives.
    """

    def handle_event(self, event: dict[str, Any]) -> None: ...


def _iso_now() -> str:
    """Return current time as ISO 8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_event_ts(ts: str) -> datetime | None:
    """Parse a progress-log ``ts`` field. None on malformed input."""
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC,
        )
    except ValueError:
        return None


def event_age_seconds(ts: str, now: datetime | None = None) -> float | None:
    """Seconds between an event ``ts`` and ``now``. None on bad input."""
    parsed = parse_event_ts(ts)
    if parsed is None:
        return None
    if now is None:
        now = datetime.now(UTC)
    return (now - parsed).total_seconds()


def format_age(seconds: float) -> str:
    """Render an age in seconds as a compact human string."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"
    return f"{seconds / 86400:.1f}d"


@dataclass
class ComponentTiming:
    """Timing data for a single component."""

    component_id: str
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0
    iteration_count: int = 0
    verification_duration: float = 0.0
    review_duration: float = 0.0


class ProgressLog:
    """Append-only JSONL event log.

    Each event is a single JSON line. JSONL is crash-safe since each line
    is a complete object - partial writes only lose the last event.

    R3.2: ``run_id`` is stamped on every event so multiple runs appending
    to the same default log stay distinguishable; consumers pick the
    latest run via :func:`latest_run_id`.
    """

    def __init__(
        self,
        log_path: Path,
        run_id: str = "",
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self._path = log_path
        self._run_id = run_id
        self._warn = warn or (lambda _msg: None)
        self._sinks: list[ProgressSink] = []
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def attach_sink(self, sink: ProgressSink) -> None:
        """Register a sink to receive every subsequent event."""
        self._sinks.append(sink)

    def emit(
        self,
        event_type: str,
        component_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Append one JSON event line to the log."""
        event: dict[str, Any] = {
            "ts": _iso_now(),
            "event": event_type,
        }
        if self._run_id:
            event["run_id"] = self._run_id
        if component_id is not None:
            event["component"] = component_id
        if data:
            event["data"] = data
        # utf-8 pinned to match the reader below. ``json.dumps`` leaves
        # ensure_ascii at its default, so what lands here is pure ASCII
        # today and the locale cannot corrupt it; naming the encoding is
        # what keeps that true if a field ever carries a raw string
        # (#291's two-sided contract, #320's sweep).
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        # R7.4: sink fan-out AFTER the journal write - the JSONL line is
        # the source of truth and must land even if every sink dies. A
        # sink exception warns and never propagates into the run.
        for sink in self._sinks:
            try:
                sink.handle_event(event)
            except Exception as exc:  # noqa: BLE001 - isolation contract
                self._warn(
                    f"progress sink {type(sink).__name__} failed on {event_type}: {exc} (non-fatal)"
                )

    def factory_started(self, project_name: str, component_count: int) -> None:
        self.emit(
            "factory_started",
            data={
                "project": project_name,
                "components": component_count,
            },
        )

    def component_started(self, component_id: str) -> None:
        self.emit("component_started", component_id=component_id)

    def component_completed(
        self,
        component_id: str,
        duration: float,
        iterations: int,
    ) -> None:
        self.emit(
            "component_completed",
            component_id=component_id,
            data={
                "duration_seconds": round(duration, 2),
                "iterations": iterations,
            },
        )

    def component_failed(self, component_id: str, error: str) -> None:
        self.emit(
            "component_failed",
            component_id=component_id,
            data={
                "error": error,
            },
        )

    def circuit_breaker_tripped(
        self,
        component_id: str,
        iterations: int,
        error: str,
    ) -> None:
        """R7.5: the engineer loop halted on the no-progress breaker.
        Emitted IN ADDITION to component_failed so a stall stays
        distinguishable from an ordinary engineer failure in the log."""
        self.emit(
            "circuit_breaker_tripped",
            component_id=component_id,
            data={
                "iterations": iterations,
                "error": error,
            },
        )

    def component_retrying(
        self,
        component_id: str,
        attempt: int,
        reason: str,
    ) -> None:
        self.emit(
            "component_retrying",
            component_id=component_id,
            data={
                "attempt": attempt,
                "reason": reason,
            },
        )

    def verification_result(
        self,
        component_id: str,
        passed: bool,
        check_names: list[str] | None = None,
        failures: list[str] | None = None,
        duration: float = 0.0,
    ) -> None:
        self.emit(
            "verification_result",
            component_id=component_id,
            data={
                "passed": passed,
                "checks": check_names or [],
                "failures": failures or [],
                "duration_seconds": round(duration, 2),
            },
        )

    def review_result(
        self,
        component_id: str,
        passed: bool,
        mode: str = "",
        fail_count: int = 0,
        advisory_count: int = 0,
        duration: float = 0.0,
    ) -> None:
        self.emit(
            "review_result",
            component_id=component_id,
            data={
                "passed": passed,
                "mode": mode,
                "fail_count": fail_count,
                "advisory_count": advisory_count,
                "duration_seconds": round(duration, 2),
            },
        )

    def component_usage(
        self,
        component_id: str,
        phase: str,
        usage: dict[str, Any],
    ) -> None:
        """R3.1: one event per (component, phase) usage capture. ``usage``
        is a UsageTotals.to_dict() - token/cost values are CLI
        self-reports and lower bounds when ``unreported_calls`` > 0."""
        self.emit(
            "component_usage",
            component_id=component_id,
            data={
                "phase": phase,
                **usage,
            },
        )

    def budget_exceeded(
        self,
        component_id: str,
        total_tokens: int,
        max_total_tokens: int,
        cost_usd: float = 0.0,
        max_cost_usd: float = 0.0,
        ceiling: str = "",
        condition: str = "",
        ceilings: Sequence[str] = (),
        coverage: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """R3.1/R8: a run-level ceiling tripped; the named component was
        failed with a synthetic budget finding.

        ``ceiling`` is the joined legacy string. ``condition``
        (``"breached"`` / ``"unenforceable"``) and ``ceilings`` carry the
        halt structurally, matching :class:`events.BudgetExceeded`.
        ``coverage`` says what each named ceiling actually counted (one
        ``CeilingCoverage.to_dict()`` per ceiling), because a total
        without its coverage reads as a measurement when it is a lower
        bound over a subset of the run's calls.

        This emitter had a hand-written field list, so when the
        structural pair was added the progress log kept writing only
        ``ceiling`` - the durable ``events.jsonl`` carried both fields
        while ``progress.jsonl`` silently dropped them. Caught by a real
        factory run, not by the suite, because nothing asserted the two
        sinks agreed. Every field added here must be added there too;
        ``TestBothSinksCarryTheSameBudgetHalt`` fails otherwise.
        """
        self.emit(
            "budget_exceeded",
            component_id=component_id,
            data={
                "total_tokens": total_tokens,
                "max_total_tokens": max_total_tokens,
                "cost_usd": cost_usd,
                "max_cost_usd": max_cost_usd,
                "ceiling": ceiling,
                "condition": condition,
                "ceilings": list(ceilings),
                "coverage": [dict(entry) for entry in coverage],
            },
        )

    def budget_coverage(
        self,
        *,
        ceiling: str = "",
        axis: str = "",
        calls: int = 0,
        covered_calls: int = 0,
        uncovered_calls: int = 0,
        uncovered_tokens: int = 0,
        uncovered_roles: Sequence[str] = (),
        detail: str = "",
    ) -> None:
        """R8: a configured ceiling stopped covering every metered call.

        Run-scoped, so no ``component_id`` - the gap belongs to the run's
        adapters. Written at the first phase that exposes the gap rather
        than at the halt, because a coverage fact an operator learns
        after the spend is a post-mortem, not a control.
        """
        self.emit(
            "budget_coverage",
            data={
                "ceiling": ceiling,
                "axis": axis,
                "calls": calls,
                "covered_calls": covered_calls,
                "uncovered_calls": uncovered_calls,
                "uncovered_tokens": uncovered_tokens,
                "uncovered_roles": list(uncovered_roles),
                "detail": detail,
            },
        )

    def contract_result(
        self,
        tier: int,
        passed: bool,
        breaker: str | None = None,
        duration: float = 0.0,
    ) -> None:
        self.emit(
            "contract_result",
            data={
                "tier": tier,
                "passed": passed,
                "breaker": breaker,
                "duration_seconds": round(duration, 2),
            },
        )

    def factory_completed(
        self,
        completed: int,
        failed: int,
        skipped: int,
        duration: float = 0.0,
    ) -> None:
        self.emit(
            "factory_completed",
            data={
                "completed": completed,
                "failed": failed,
                "skipped": skipped,
                "duration_seconds": round(duration, 2),
            },
        )

    def read_events(self) -> list[dict[str, Any]]:
        """Read all events from the log. Useful for testing."""
        return read_progress_events(self._path)


class NullProgressLog(ProgressLog):
    """No-op progress log for when logging is disabled."""

    def __init__(self) -> None:
        # Do not call super().__init__() since we have no path
        self._path = Path("/dev/null")
        self._run_id = ""
        self._warn = lambda _msg: None
        # Sinks attached to a disabled log never fire: emit is a no-op,
        # so a Linear sink requires progress_log_enabled (the default).
        self._sinks = []

    def emit(
        self,
        event_type: str,
        component_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        pass


def read_progress_events(path: Path) -> list[dict[str, Any]]:
    """Read all events from a JSONL progress log.

    Malformed lines (a crash mid-write leaves at most one) are skipped
    rather than raised: the log is an observability surface and a torn
    tail line must not make `ks status` unusable.

    ``encoding="utf-8"`` is named and ``ValueError`` is caught for the
    same reason: kstrl writes this file as utf-8 through ``atomicio``,
    so a reader that leaves the codec to the locale decodes it wrongly
    under ``LC_ALL=C``, and ``UnicodeDecodeError`` is a ``ValueError``,
    which walks straight out of a fail-closed ``except OSError``. Two
    callers meet that: ``ks status``, and ``EvolutionJournal`` on the
    TUI evolve screen, where it reached the Textual event loop (#289).
    """
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    events.append(parsed)
    except (OSError, ValueError):
        # ValueError is UnicodeDecodeError: bytes this reader cannot
        # decode at all, as opposed to the torn line the inner handler
        # skips. Same answer as an unreadable file - no events - because
        # the alternative is a traceback out of an observability read.
        return []
    return events


def latest_run_id(events: list[dict[str, Any]]) -> str:
    """run_id of the most recent event that carries one, else ""."""
    for event in reversed(events):
        run_id = event.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    return ""


def _phase_for_event(event: dict[str, Any]) -> str | None:
    """Map an event to the phase it implies; None keeps the prior phase."""
    etype = event.get("event")
    data = event.get("data") or {}
    if etype == "component_started":
        return "engineer"
    if etype == "component_usage":
        phase = data.get("phase")
        return str(phase) if phase else None
    if etype == "verification_result":
        return "verify"
    if etype == "review_result":
        mode = str(data.get("mode") or "")
        return "security" if mode.startswith("security") else "review"
    if etype == "component_retrying":
        return "retrying"
    if etype == "component_failed":
        return "failed"
    if etype == "component_completed":
        return "done"
    if etype == "budget_exceeded":
        return "budget-halt"
    return None


@dataclass
class ComponentActivity:
    """Per-component view derived from one run's progress-log events."""

    component_id: str
    phase: str = ""
    attempt: int = 0
    last_event: str = ""
    last_event_ts: str = ""
    usage_calls: int = 0
    unreported_calls: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class RunActivity:
    """One factory run's progress-log events, joined per component."""

    run_id: str = ""
    started_ts: str = ""
    last_event_ts: str = ""
    finished: bool = False
    components: dict[str, ComponentActivity] = field(default_factory=dict)


def _as_int_field(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def summarize_events(
    events: list[dict[str, Any]],
    run_id: str = "",
) -> RunActivity:
    """Fold progress-log events into a per-component activity summary.

    ``run_id`` non-empty filters to that run's events; empty includes
    everything (pre-R3.2 logs have no run_id to filter on).
    """
    activity = RunActivity(run_id=run_id)
    for event in events:
        if run_id and event.get("run_id") != run_id:
            continue
        ts = str(event.get("ts") or "")
        etype = str(event.get("event") or "")
        if not activity.started_ts:
            activity.started_ts = ts
        activity.last_event_ts = ts
        if etype == "factory_completed":
            activity.finished = True
        comp_id = event.get("component")
        if not isinstance(comp_id, str) or not comp_id:
            continue
        comp = activity.components.setdefault(
            comp_id,
            ComponentActivity(component_id=comp_id),
        )
        comp.last_event = etype
        comp.last_event_ts = ts
        phase = _phase_for_event(event)
        if phase:
            comp.phase = phase
        data = event.get("data") or {}
        if not isinstance(data, dict):
            continue
        if etype == "component_retrying":
            comp.attempt = max(comp.attempt, _as_int_field(data, "attempt"))
        elif etype == "component_usage":
            comp.usage_calls += _as_int_field(data, "calls")
            comp.unreported_calls += _as_int_field(data, "unreported_calls")
            comp.total_tokens += _as_int_field(data, "total_tokens")
            cost = data.get("cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                comp.cost_usd += float(cost)
    return activity


@dataclass
class NotifyConfig:
    """R3.2 ``[notify]`` section: shell commands fired on run milestones.

    Example one-liners for kstrl.toml::

        [notify]
        # Terminal bell when the run finishes:
        on_complete = "printf '\\a'"
        # Webhook ping the moment something needs attention:
        on_first_failure = "curl -fsS -X POST -d \\"$KSTRL_NOTIFY_EVENT $KSTRL_NOTIFY_COMPONENT\\" https://example.com/hook"

    The commands run via the shell with these context variables set:
    ``KSTRL_NOTIFY_EVENT`` (run_complete | first_failure | merge_pending
    | inbox_<kind>), ``KSTRL_NOTIFY_RUN_ID``, ``KSTRL_NOTIFY_PROJECT``,
    ``KSTRL_NOTIFY_COMPONENT`` and ``KSTRL_NOTIFY_DETAIL``.

    ``on_inbox_item`` is deliberately its OWN command rather than a reuse
    of ``on_first_failure``: an R8.3 inbox item is usually raised for the
    same event that already fired a failure hook, so sharing the command
    would notify twice for one thing. Empty by default - an operator who
    wants per-item pushes opts in.
    """

    on_complete: str = ""
    on_first_failure: str = ""
    on_inbox_item: str = ""
    hook_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> NotifyConfig:
        """Load notify config from environment variables only."""
        config = cls()
        _apply_notify_env(config)
        return config

    @classmethod
    def load(cls, root_dir: Path | None = None) -> NotifyConfig:
        """Load notify config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "notify")
        if isinstance(section.get("on_complete"), str):
            config.on_complete = section["on_complete"]
        if isinstance(section.get("on_first_failure"), str):
            config.on_first_failure = section["on_first_failure"]
        if isinstance(section.get("on_inbox_item"), str):
            config.on_inbox_item = section["on_inbox_item"]
        if "hook_timeout" in section:
            config.hook_timeout = float(section["hook_timeout"])
        _apply_notify_env(config)
        return config


def _apply_notify_env(config: NotifyConfig) -> None:
    if "KSTRL_NOTIFY_ON_COMPLETE" in os.environ:
        config.on_complete = os.environ["KSTRL_NOTIFY_ON_COMPLETE"]
    if "KSTRL_NOTIFY_ON_FIRST_FAILURE" in os.environ:
        config.on_first_failure = os.environ["KSTRL_NOTIFY_ON_FIRST_FAILURE"]
    if "KSTRL_NOTIFY_ON_INBOX_ITEM" in os.environ:
        config.on_inbox_item = os.environ["KSTRL_NOTIFY_ON_INBOX_ITEM"]
    if "KSTRL_NOTIFY_HOOK_TIMEOUT" in os.environ:
        config.hook_timeout = float(os.environ["KSTRL_NOTIFY_HOOK_TIMEOUT"])


class NotifyHooks:
    """Fires user-configured shell commands on factory-run milestones.

    Each condition fires at most once per run (R3.2):

    - run completion -> ``on_complete``
    - first component failure -> ``on_first_failure``
    - first MERGE_PENDING park -> ``on_first_failure`` (the attention
      channel: the run is blocked on a human confirming a merge). The
      hook can tell the conditions apart via ``KSTRL_NOTIFY_EVENT``.
    - first R8.3 inbox item of each kind -> ``on_inbox_item``, which is
      empty by default. Kept off ``on_first_failure`` on purpose: the
      item and the failure usually describe the same event, and one
      event must not page twice.

    Hooks are observability, never control flow: a hook that fails to
    launch, exits nonzero, or times out produces a warning and nothing
    else. Output is NOT captured - a terminal bell must reach the tty.
    """

    def __init__(
        self,
        config: NotifyConfig,
        run_id: str = "",
        project: str = "",
        warn: Callable[[str], None] | None = None,
        capture_output: bool = False,
    ) -> None:
        self._config = config
        self._run_id = run_id
        self._project = project
        self._warn = warn or (lambda _msg: None)
        self._fired: set[str] = set()
        # PR F: when a TUI owns the terminal, hook output must not
        # reach the inherited fds (measured alt-screen corruption in
        # the Stage 0 spike). Default False keeps terminal bells
        # working for plain-mode users.
        self.capture_output = capture_output

    def fire_complete(self, detail: str = "") -> None:
        self._fire("run_complete", self._config.on_complete, detail=detail)

    def fire_first_failure(self, component_id: str, error: str) -> None:
        self._fire(
            "first_failure",
            self._config.on_first_failure,
            component_id=component_id,
            detail=error,
        )

    def fire_inbox_item(self, kind: str, title: str, component_id: str = "") -> None:
        """R8.3: an exception needs a human. Opt-in via its OWN
        ``on_inbox_item`` command, empty by default.

        It must not share ``on_first_failure``: a failing component
        already fires that hook, and the inbox item it raises describes
        the same event, so reusing the command would notify twice for
        one thing. One-way as ever - the hook pushes, it never calls
        back into the harness. Only action-required kinds and demotions
        reach here, so success stays silent.
        """
        self._fire(
            f"inbox_{kind}",
            self._config.on_inbox_item,
            component_id=component_id,
            detail=title,
        )

    def fire_merge_pending(self, component_id: str, detail: str = "") -> None:
        self._fire(
            "merge_pending",
            self._config.on_first_failure,
            component_id=component_id,
            detail=detail,
        )

    def _fire(
        self,
        condition: str,
        command: str,
        component_id: str = "",
        detail: str = "",
    ) -> None:
        if not command or condition in self._fired:
            return
        # Marked before launching: a hook that crashes must not get a
        # second chance on the next failure - once means once.
        self._fired.add(condition)
        env = dict(os.environ)
        notify_vars = {
            "NOTIFY_EVENT": condition,
            "NOTIFY_RUN_ID": self._run_id,
            "NOTIFY_PROJECT": self._project,
            "NOTIFY_COMPONENT": component_id,
            "NOTIFY_DETAIL": detail,
        }
        # KSTRL_* is primary; KSTRL_* mirrors kept one release so existing
        # user hooks keep reading their values during the rename.
        for suffix, value in notify_vars.items():
            env[f"KSTRL_{suffix}"] = value
            env[f"KSTRL_{suffix}"] = value
        try:
            sink = subprocess.DEVNULL if self.capture_output else None
            proc = subprocess.run(
                command,
                shell=True,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=sink,
                timeout=self._config.hook_timeout,
            )
            if proc.returncode != 0:
                self._warn(f"notify hook '{condition}' exited {proc.returncode} (non-fatal)")
        except subprocess.TimeoutExpired:
            self._warn(
                f"notify hook '{condition}' timed out after "
                f"{self._config.hook_timeout}s (non-fatal)"
            )
        except OSError as exc:
            self._warn(f"notify hook '{condition}' failed to launch: {exc} (non-fatal)")
