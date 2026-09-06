"""R8.3 exception inbox: one surface for everything awaiting a human.

Over-the-loop operation needs a single place to look. Today the things
that want a decision are scattered across surfaces that share nothing: a
FAILED component lives in the manifest, a MERGE_PENDING park lives in a
status string, an R8.1 policy violation lives in a finding, an R8.2
demotion lives in the evolution journal, a budget overrun lives in a log
line. Each is discoverable only if you already know to look for it, which
is the opposite of what "humans handle exceptions" requires.

This module is the thin substrate: an append-only inbox log (XDG control
dir under R8.9; legacy path ``.kstrl/inbox.jsonl``) plus the fold that
turns it into a current view. Deliberately small - the value is in the
emitters that feed it and the actions that resolve it, not in the store.

**Append-only, never rewritten.** Every emission and every decision is one
appended line; the current state of an item is the fold of its lines in
order. A crash mid-write can lose at most the last line, never corrupt a
prior decision, and "what did I approve and when?" is answerable by
reading the file rather than trusting a mutated record.

Design notes that carry weight:

- **The open-item cap is a real backstop, not decoration.** An inbox that
  grows without bound is a second job, and the roadmap's own failure mode.
  ``InboxConfig.open_item_cap`` is what R8.6 will consult before admitting
  more queue work.
- **Snooze has a TTL, not a hide button.** A snoozed item returns; that is
  the difference between deferring a decision and losing one.
- **Notifications are one-way.** Items are actioned in ``ks inbox``, never
  by a callback into the harness, so kstrl runs no inbound HTTP surface.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from kstrl.atomicio import atomic_write_text
from kstrl.statedir import CONTROL_INBOX, control_file, control_lock, ensure_control_state

INBOX_SCHEMA_VERSION = 1


class ItemKind(StrEnum):
    """What kind of decision an item is asking for.

    The kinds map one-to-one onto the halt paths that existed before this
    module; adding a kind means adding an emitter, never just a label.
    """

    POLICY_EXCEPTION = "policy_exception"  # R8.1 envelope violation
    MERGE_GATE = "merge_gate"  # a merge awaiting approval
    HALTED_RUN = "halted_run"  # a component/run stopped
    BUDGET_OVERRUN = "budget_overrun"  # a cap was hit
    DEMOTION_NOTICE = "demotion_notice"  # R8.2 autonomy revoked
    CALIBRATION_DRIFT = "calibration_drift"  # detection rate moved
    TEST_ADEQUACY = "test_adequacy"  # R8.5 Layer 0 blocked a change
    HEALTH_BREACH = "health_breach"  # R8.4 control-limit breach (#232)

    @property
    def action_required(self) -> bool:
        """Whether this kind blocks on a human, as opposed to informing.

        Drives notification: action-required kinds and demotions notify;
        informational kinds stay silent and batch into a digest. Alert
        fatigue is the failure mode being defended against - a surface
        that pages on success trains you to ignore it.
        """
        return self in {
            ItemKind.POLICY_EXCEPTION,
            ItemKind.MERGE_GATE,
            ItemKind.HALTED_RUN,
            ItemKind.BUDGET_OVERRUN,
            # A BLOCKING adequacy finding stopped a change: someone has to
            # decide whether the suite really may get weaker here. The
            # advisory ones never reach the inbox at all (see pipeline).
            ItemKind.TEST_ADEQUACY,
        }


class ItemStatus(StrEnum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"
    SNOOZED = "snoozed"
    RESOLVED = "resolved"  # actioned elsewhere / no longer relevant


class Priority(StrEnum):
    """Notification tier. Not a sort key alone - age matters too."""

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


#: Default priority per kind. A demotion is high because autonomy was
#: revoked and the evidence is perishable; drift is low because it is a
#: trend, not an event. A health breach sits between the two: it is a
#: trend like drift, but one that may have cost a level in the same run.
DEFAULT_PRIORITY: dict[ItemKind, Priority] = {
    ItemKind.POLICY_EXCEPTION: Priority.HIGH,
    ItemKind.MERGE_GATE: Priority.NORMAL,
    ItemKind.HALTED_RUN: Priority.NORMAL,
    ItemKind.BUDGET_OVERRUN: Priority.HIGH,
    ItemKind.DEMOTION_NOTICE: Priority.HIGH,
    ItemKind.CALIBRATION_DRIFT: Priority.LOW,
    ItemKind.TEST_ADEQUACY: Priority.NORMAL,
    ItemKind.HEALTH_BREACH: Priority.NORMAL,
}

_PRIORITY_ORDER = {Priority.HIGH: 0, Priority.NORMAL: 1, Priority.LOW: 2}


class InboxError(RuntimeError):
    """An inbox action was refused (unknown id, already decided, cap hit)."""


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class InboxItem:
    """One decision awaiting (or having received) a human.

    ``dedupe_key`` collapses repeats: the same component failing the same
    way across three retries is one item with a bumped count, not three
    items. Without it the inbox becomes noise on exactly the runs where it
    matters most.
    """

    id: str
    kind: ItemKind
    title: str
    created_at: str
    detail: str = ""
    priority: Priority = Priority.NORMAL
    component: str = ""
    run_id: str = ""
    dedupe_key: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    status: ItemStatus = ItemStatus.OPEN
    occurrences: int = 1
    last_seen_at: str = ""
    decided_at: str = ""
    decided_by: str = ""
    decision_comment: str = ""
    snooze_until: str = ""

    @property
    def is_open(self) -> bool:
        """Open now: never decided, or snoozed past its TTL.

        A lapsed snooze reads as open rather than needing a sweeper - the
        deferral expires by the clock, so nothing has to remember to
        un-hide it.
        """
        if self.status is ItemStatus.OPEN:
            return True
        if self.status is ItemStatus.SNOOZED:
            until = _parse_iso(self.snooze_until)
            return until is None or until <= _utc_now()
        return False

    @property
    def snooze_active(self) -> bool:
        return self.status is ItemStatus.SNOOZED and not self.is_open

    def sort_key(self) -> tuple[int, str]:
        return (_PRIORITY_ORDER.get(self.priority, 1), self.created_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": str(self.kind),
            "title": self.title,
            "created_at": self.created_at,
            "detail": self.detail,
            "priority": str(self.priority),
            "component": self.component,
            "run_id": self.run_id,
            "dedupe_key": self.dedupe_key,
            "evidence": self.evidence,
            "status": str(self.status),
            "occurrences": self.occurrences,
            "last_seen_at": self.last_seen_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "decision_comment": self.decision_comment,
            "snooze_until": self.snooze_until,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InboxItem | None:
        """Rebuild an item, returning None for anything unrecognizable.

        Tolerant by design: one malformed line must not make the whole
        inbox unreadable, because an unreadable inbox is an invisible
        backlog. The line is skipped, not fatal.
        """
        item_id = data.get("id")
        if not isinstance(item_id, str) or not item_id:
            return None
        try:
            kind = ItemKind(str(data.get("kind", "")))
            status = ItemStatus(str(data.get("status", ItemStatus.OPEN)))
            priority = Priority(str(data.get("priority", Priority.NORMAL)))
        except ValueError:
            return None
        evidence = data.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        occurrences = data.get("occurrences", 1)
        if not isinstance(occurrences, int) or isinstance(occurrences, bool):
            occurrences = 1
        return cls(
            id=item_id,
            kind=kind,
            title=str(data.get("title", "")),
            created_at=str(data.get("created_at", "")),
            detail=str(data.get("detail", "")),
            priority=priority,
            component=str(data.get("component", "")),
            run_id=str(data.get("run_id", "")),
            dedupe_key=str(data.get("dedupe_key", "")),
            evidence=evidence,
            status=status,
            occurrences=occurrences,
            last_seen_at=str(data.get("last_seen_at", "")),
            decided_at=str(data.get("decided_at", "")),
            decided_by=str(data.get("decided_by", "")),
            decision_comment=str(data.get("decision_comment", "")),
            snooze_until=str(data.get("snooze_until", "")),
        )


@dataclass(frozen=True)
class InboxConfig:
    """``[inbox]`` config. On by default - unlike the R8.1/R8.2 gates.

    Recording an exception changes no behaviour: nothing blocks, nothing
    merges differently, a file gains a line. The thing an operator can
    lose by having it off is the record of a decision they already had to
    make, so the safe default is on. ``open_item_cap`` is the backstop
    R8.6 consults before admitting more queue work.
    """

    enabled: bool = True
    open_item_cap: int = 50
    snooze_hours: float = 24.0
    notify_action_required: bool = True

    @classmethod
    def from_env(cls) -> InboxConfig:
        defaults = cls()
        enabled = os.environ.get("KSTRL_INBOX_ENABLED")
        cap = os.environ.get("KSTRL_INBOX_OPEN_CAP")
        snooze = os.environ.get("KSTRL_INBOX_SNOOZE_HOURS")
        notify = os.environ.get("KSTRL_INBOX_NOTIFY")
        return cls(
            enabled=defaults.enabled if enabled is None else enabled == "1",
            open_item_cap=defaults.open_item_cap if cap is None else int(cap),
            snooze_hours=(defaults.snooze_hours if snooze is None else float(snooze)),
            notify_action_required=(
                defaults.notify_action_required if notify is None else notify == "1"
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> InboxConfig:
        """Precedence: env > toml > defaults; reads ``[inbox]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "inbox")
        defaults = cls()
        enabled = bool(section["enabled"]) if "enabled" in section else defaults.enabled
        open_item_cap = (
            int(section["open_item_cap"]) if "open_item_cap" in section else defaults.open_item_cap
        )
        snooze_hours = (
            float(section["snooze_hours"]) if "snooze_hours" in section else defaults.snooze_hours
        )
        notify_action_required = (
            bool(section["notify_action_required"])
            if "notify_action_required" in section
            else defaults.notify_action_required
        )
        if "KSTRL_INBOX_ENABLED" in os.environ:
            enabled = os.environ["KSTRL_INBOX_ENABLED"] == "1"
        if "KSTRL_INBOX_OPEN_CAP" in os.environ:
            open_item_cap = int(os.environ["KSTRL_INBOX_OPEN_CAP"])
        if "KSTRL_INBOX_SNOOZE_HOURS" in os.environ:
            snooze_hours = float(os.environ["KSTRL_INBOX_SNOOZE_HOURS"])
        if "KSTRL_INBOX_NOTIFY" in os.environ:
            notify_action_required = os.environ["KSTRL_INBOX_NOTIFY"] == "1"
        return cls(
            enabled=enabled,
            open_item_cap=open_item_cap,
            snooze_hours=snooze_hours,
            notify_action_required=notify_action_required,
        )


@dataclass(frozen=True)
class InboxScan:
    """One pass over ``.kstrl/inbox.jsonl``.

    ``unreadable`` means the gate has no positive evidence the backlog
    is under its cap - admission must refuse regardless of
    ``open_item_cap``. Display callers ignore the flag and treat the
    (empty) records as a clear inbox, same as the pre-#190 fold.
    """

    records: tuple[dict[str, Any], ...] = ()
    skipped_lines: int = 0
    unreadable: bool = False

    def folded_items(self) -> list[InboxItem]:
        folded: dict[str, InboxItem] = {}
        for record in self.records:
            item = InboxItem.from_dict(record)
            if item is None:
                continue
            folded[item.id] = item
        return list(folded.values())

    def open_count(self) -> int:
        return sum(1 for item in self.folded_items() if item.is_open)

    def unparseable_count(self) -> int:
        """Lines the display fold would skip: torn JSON, non-dicts, and
        dicts ``InboxItem.from_dict`` cannot rebuild."""
        return self.skipped_lines + sum(
            1 for record in self.records if InboxItem.from_dict(record) is None
        )


class Inbox:
    """Append-only store over the XDG control-dir inbox log.

    Reads fold the log into current items; writes append one line. No
    method rewrites history - ``compact`` exists but writes a fresh file
    from the folded state and is never called implicitly.
    """

    def __init__(self, root_dir: Path, config: InboxConfig | None = None) -> None:
        self.root_dir = root_dir
        self.config = config or InboxConfig()

    @property
    def path(self) -> Path:
        return control_file(self.root_dir, CONTROL_INBOX)

    # -- reading -----------------------------------------------------------
    def scan(self) -> InboxScan:
        """One pass over the log: readable records + skip count + readability.

        Tolerant by design for the DISPLAY path: a torn tail must not
        make the whole backlog unreadable. Safety gates (#190) must not
        sit on that tolerance - they consume this snapshot once and treat
        ``unreadable`` or every skipped line as open capacity. An
        existing-but-unreadable file (OSError on exists/read, or a
        UnicodeDecodeError on a torn multibyte write) is its own state:
        the gate has no positive evidence the backlog is under the cap,
        so collapsing it to ``skipped=1`` would re-admit under any cap
        greater than one.
        """
        ensure_control_state(self.root_dir)
        try:
            exists = self.path.exists()
        except OSError:
            return InboxScan(unreadable=True)
        if not exists:
            return InboxScan()
        try:
            raw = self.path.read_bytes()
        except OSError:
            return InboxScan(unreadable=True)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return InboxScan(unreadable=True)
        records: list[dict[str, Any]] = []
        skipped = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1  # tolerate a torn tail; skip, never raise
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                skipped += 1
        return InboxScan(records=tuple(records), skipped_lines=skipped)

    def _read_lines(self) -> list[dict[str, Any]]:
        return list(self.scan().records)

    def unparseable_line_count(self) -> int:
        """Nonempty log lines the display fold would skip (#190).

        Prefer ``scan()`` when open and unparseable counts must come from
        the same snapshot (the serve admission gate). This helper remains
        for callers that only need the skip side.
        """
        return self.scan().unparseable_count()

    def _folded(self) -> list[InboxItem]:
        """Latest state per id, in the order the ids first appeared.

        Log order is a total order; ``created_at`` is not (it is rounded
        to the second, so two items raised in the same second tie). Any
        "which generation is newer?" question has to be answered here,
        not from the timestamp.
        """
        folded: dict[str, InboxItem] = {}
        for record in self._read_lines():
            item = InboxItem.from_dict(record)
            if item is None:
                continue
            folded[item.id] = item  # later lines supersede earlier ones
        return list(folded.values())

    def items(self) -> list[InboxItem]:
        """Every item, latest state per id, newest-first by priority/age."""
        return sorted(self._folded(), key=lambda i: i.sort_key())

    def open_items(self) -> list[InboxItem]:
        return [item for item in self.items() if item.is_open]

    def get(self, item_id: str) -> InboxItem | None:
        """Find by exact id or unique short prefix (ids are uuid4 hex)."""
        items = self.items()
        for item in items:
            if item.id == item_id:
                return item
        matches = [item for item in items if item.id.startswith(item_id)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise InboxError(
                f"ambiguous item id {item_id!r}: matches "
                + ", ".join(m.id[:8] for m in matches[:5])
            )
        return None

    def find_by_dedupe_key(self, key: str) -> InboxItem | None:
        """The generation of ``key`` that a repeat should collapse onto.

        An OPEN generation always wins; otherwise the NEWEST decided one.
        Returning the oldest match (which ``items()`` yields first, being
        sorted ascending by age) meant a second repeat after a decision
        re-found the decided original and opened yet another item, so a
        recurring failure fanned out into one item per occurrence -
        exactly the noise dedupe exists to prevent.

        "Newest" is by log position, not ``created_at``: timestamps are
        second-resolution and two generations of the same key can share
        one, which would put the tie-break back where the bug was.
        """
        if not key:
            return None
        matches = [item for item in self._folded() if item.dedupe_key == key]
        if not matches:
            return None
        for item in matches:
            if item.status is ItemStatus.OPEN:
                return item
        return matches[-1]

    # -- writing -----------------------------------------------------------
    def _append(self, item: InboxItem) -> None:
        """Append one line, creating the file atomically on first write."""
        ensure_control_state(self.root_dir)
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": INBOX_SCHEMA_VERSION, **item.to_dict()}
        line = json.dumps(payload, separators=(",", ":"), default=str) + "\n"
        with control_lock(self.root_dir):
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)

    def add(
        self,
        kind: ItemKind,
        title: str,
        *,
        detail: str = "",
        component: str = "",
        run_id: str = "",
        dedupe_key: str = "",
        evidence: dict[str, Any] | None = None,
        priority: Priority | None = None,
    ) -> InboxItem:
        """Record an exception, collapsing repeats onto one item.

        A repeat of a still-open item bumps its occurrence count instead of
        adding a row. A repeat of a DECIDED item opens a fresh one: you
        approved that failure once, and its recurrence is new information.

        A repeat refreshes ``detail`` AND ``evidence`` together. They are
        two descriptions of the same observation, and refreshing only the
        prose left the structured half - the half ``ks inbox`` and the
        TUI render, and the durable one - reporting the first occurrence
        while the text reported the latest. Measured on a health-breach
        item: ``detail`` said value 0.9 over 20 runs while ``evidence``
        still said 0.4 over 8. ``title`` is deliberately NOT refreshed:
        it is the row's label, and a repeat must not relabel a row an
        operator has already read.
        """
        now = _utc_now()
        existing = self.find_by_dedupe_key(dedupe_key)
        if existing is not None and existing.status is ItemStatus.OPEN:
            existing.occurrences += 1
            existing.last_seen_at = _iso(now)
            if detail:
                existing.detail = detail
            if evidence:
                existing.evidence = evidence
            self._append(existing)
            return existing
        item = InboxItem(
            id=uuid.uuid4().hex,
            kind=kind,
            title=title,
            created_at=_iso(now),
            detail=detail,
            priority=priority or DEFAULT_PRIORITY.get(kind, Priority.NORMAL),
            component=component,
            run_id=run_id,
            dedupe_key=dedupe_key,
            evidence=evidence or {},
            last_seen_at=_iso(now),
        )
        self._append(item)
        return item

    def _decide(
        self,
        item_id: str,
        status: ItemStatus,
        *,
        actor: str,
        comment: str = "",
        snooze_until: datetime | None = None,
    ) -> InboxItem:
        item = self.get(item_id)
        if item is None:
            raise InboxError(f"no inbox item matching {item_id!r}")
        item.status = status
        item.decided_at = _iso(_utc_now())
        item.decided_by = actor
        item.decision_comment = comment
        item.snooze_until = _iso(snooze_until) if snooze_until else ""
        self._append(item)
        return item

    def approve(self, item_id: str, *, actor: str, comment: str = "") -> InboxItem:
        return self._decide(
            item_id,
            ItemStatus.APPROVED,
            actor=actor,
            comment=comment,
        )

    def reject(self, item_id: str, *, actor: str, comment: str) -> InboxItem:
        """Reject. A comment is required: a bare "no" is not a decision
        anyone can act on later, least of all the person who made it."""
        if not comment.strip():
            raise InboxError("rejection requires a comment explaining why")
        return self._decide(
            item_id,
            ItemStatus.REJECTED,
            actor=actor,
            comment=comment,
        )

    def resolve(self, item_id: str, *, actor: str = "system", comment: str = "") -> InboxItem:
        return self._decide(
            item_id,
            ItemStatus.RESOLVED,
            actor=actor,
            comment=comment,
        )

    def snooze(
        self,
        item_id: str,
        *,
        actor: str,
        hours: float | None = None,
    ) -> InboxItem:
        ttl = self.config.snooze_hours if hours is None else hours
        if ttl <= 0:
            raise InboxError("snooze needs a positive TTL; use approve/reject to close")
        return self._decide(
            item_id,
            ItemStatus.SNOOZED,
            actor=actor,
            comment=f"snoozed {ttl}h",
            snooze_until=_utc_now() + timedelta(hours=ttl),
        )

    # -- capacity ----------------------------------------------------------
    def over_cap(self) -> bool:
        """Whether the open backlog has reached its cap.

        R8.6 consults this before admitting queue work: an operator who is
        already behind should not be handed more to be behind on.
        """
        cap = self.config.open_item_cap
        return cap > 0 and len(self.open_items()) >= cap

    def compact(self) -> int:
        """Rewrite the log as one line per item from the folded state.

        Never called implicitly - history is the audit trail. Returns the
        number of items retained. Atomic, through the one helper that
        owns that pattern (#291).
        """
        items = self.items()
        ensure_control_state(self.root_dir)
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with control_lock(self.root_dir):
            atomic_write_text(
                path,
                "".join(
                    json.dumps(
                        {"schema_version": INBOX_SCHEMA_VERSION, **item.to_dict()},
                        separators=(",", ":"),
                        default=str,
                    )
                    + "\n"
                    for item in items
                ),
            )
        return len(items)


def summarize(items: Sequence[InboxItem]) -> str:
    """One-line summary for run epilogues and notifications."""
    open_items = [item for item in items if item.is_open]
    if not open_items:
        return "inbox clear"
    by_kind: dict[str, int] = {}
    for item in open_items:
        by_kind[str(item.kind)] = by_kind.get(str(item.kind), 0) + 1
    parts = ", ".join(f"{count} {kind}" for kind, count in sorted(by_kind.items()))
    return f"{len(open_items)} open ({parts})"


def notifiable(items: Iterable[InboxItem]) -> list[InboxItem]:
    """Items worth interrupting a human for.

    Action-required kinds and demotion notices only: successes stay
    silent, and informational kinds batch into a digest instead of
    paging. Silence on success is what keeps the signal worth reading.
    """
    return [
        item
        for item in items
        if item.is_open and (item.kind.action_required or item.kind is ItemKind.DEMOTION_NOTICE)
    ]
