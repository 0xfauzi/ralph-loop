"""R8.6 continuous intake: the local work queue substrate.

Intake before this module is one-shot - a human fires ``ks factory
--spec`` and the run ends. The queue is what lets work arrive without a
human firing each run, so it is the survival capability R8.6 exists to
add, not plumbing around one.

**Why a directory queue and not a library.** Queue libraries were
evaluated and rejected in the R8.6 verdict (litequeue is near-dormant and
conflicts with our Python floor; persist-queue buys nothing at a
concurrency of one; huey inverts control - it would own the process the
factory needs to own). A maildir-style directory tree gives durability,
crash recovery, and human inspectability with no dependency and no
schema migration story.

**Layout.** Each state is a directory under ``.kstrl/queue/``; each item
is a DIRECTORY holding its spec file plus ``meta.json``::

    .kstrl/queue/
      queued/<item_id>/{<spec>, meta.json}
      leased/   claimed by a worker, nothing spent yet
      running/  executing; money is being spent
      done/     finished green
      failed/   finished red and eligible to retry
      poison/   finished red and NOT eligible to retry
      .staging/ items being assembled; NEVER scanned
      journal.jsonl
      queue.lock    (short-lived per-transition mutex)

Pause and spend ledgers live in the XDG control directory (R8.9), not
under ``.kstrl/queue/``, so an agent in a worktree cannot edit them.

The item is a directory rather than two sibling files so that one
``os.replace`` moves the spec and its sidecar together. Two sibling files
would need two renames and could be interrupted between them, leaving an
item whose spec and metadata disagree about what state it is in.

**The directory is the source of truth.** ``meta.json`` also carries a
``state`` field, but it is a convenience mirror: readers derive state
from the parent directory name and overwrite the mirror. That makes the
crash window between "write meta" and "rename dir" harmless in both
directions - whichever step got interrupted, the directory still says
what is true and the mirror self-heals on the next write.

**Ordering is a money-safety property, not a style choice.** Every
transition writes ``meta.json`` FIRST and renames SECOND, because the
rename is the commit point. ``attempts`` is therefore incremented before
the rename that starts execution: a crash in the window over-counts an
attempt (the item gets one fewer retry - safe) instead of under-counting
one (the item retries without the attempt being recorded - an unbounded
retry loop). A measured factory engineer iteration costs ~$1.70-2.60 on
a first attempt and $3.99-7.42 on a retry, so an uncounted attempt is
not a bookkeeping slip.

The retry POLICY - which failures may be retried at all - deliberately
does not live here. This module records attempts and moves items; R8.6
PR 2 (``ks serve``) decides, on positive evidence only, whether a
failure was an infrastructure error. See ``docs/dark-factory-roadmap.md``
R8.6.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

from kstrl.appendio import JOURNAL_REPAIR_EVENT, append_records
from kstrl.atomicio import atomic_write_text
from kstrl.statedir import (
    CONTROL_PAUSE,
    control_file,
    control_lock,
    control_untrusted_reason,
    ensure_control_state,
    state_dir,
)

QUEUE_DIR_NAME = "queue"
META_FILENAME = "meta.json"
JOURNAL_FILENAME = "journal.jsonl"
LOCK_FILENAME = "queue.lock"
QUEUE_SCHEMA_VERSION = 1


class QueueError(RuntimeError):
    """A queue operation could not be completed."""


class QueueLockedError(QueueError):
    """Another process holds the queue mutex."""


class QueueBudgetExhausted(QueueError):
    """An item was asked to run with no attempts left.

    Raised by :meth:`Queue.start` - the substrate's own enforcement of
    ``max_attempts``, independent of whatever the caller believes. The
    daemon catches this and poisons the item; a caller that ignores it
    leaves the item leased for the reaper, never running.
    """


#: Names a queue path component may never take. ``.`` and ``..`` are the
#: traversal primitives; the empty string collapses a join silently.
_UNSAFE_NAMES = frozenset({"", ".", ".."})

#: Prefix for the hidden staging directory used while publishing an item.
#: It lives OUTSIDE the state directories so a scan cannot reach it even
#: if the name filter were removed (review #185 F3).
STAGING_DIR_NAME = ".staging"


def is_safe_component(name: str) -> bool:
    """Whether ``name`` is a single path component safe to join.

    Rejects separators, traversal, empty names, NUL, and leading dots.
    Every string that becomes part of a queue path passes through here,
    because two of them (``item_id`` and ``spec_filename``) are read back
    from a sidecar an operator or a remote adapter can write. Review
    #185 F1/F2 demonstrated both: an ``item_id`` of ``../../../outside``
    made ``remove()`` delete an unrelated directory, and a
    ``spec_filename`` of ``../../../../escaped.md`` wrote outside the
    queue while still publishing a normal-looking item.
    """
    if name in _UNSAFE_NAMES:
        return False
    if name.startswith("."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return Path(name).name == name


class ItemState(StrEnum):
    """Where an item is. Also the on-disk directory name.

    ``LEASED`` and ``RUNNING`` are deliberately distinct even though a
    worker passes through both in quick succession: the reaper (PR 2)
    treats them differently. A leased item whose owner died spent
    nothing, so it returns to ``QUEUED`` for free. A running item whose
    owner died spent real money and needs its failure classified before
    anything decides to spend more.
    """

    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    POISON = "poison"


#: Every state directory, created eagerly so a scan never has to
#: distinguish "no such directory" from "no items in it".
ALL_STATES: tuple[ItemState, ...] = tuple(ItemState)

#: Legal moves. Anything absent raises rather than being silently
#: allowed: an illegal transition means a caller's state machine is
#: wrong, and in this module a wrong state machine spends money.
_LEGAL_TRANSITIONS: dict[ItemState, frozenset[ItemState]] = {
    ItemState.QUEUED: frozenset({ItemState.LEASED, ItemState.POISON}),
    # A lease can be dropped back to queued by the reaper (owner died
    # before spending) or fail outright if the worker could not start.
    ItemState.LEASED: frozenset(
        {
            ItemState.QUEUED,
            ItemState.RUNNING,
            ItemState.FAILED,
            ItemState.POISON,
        }
    ),
    ItemState.RUNNING: frozenset(
        {
            ItemState.DONE,
            ItemState.FAILED,
            ItemState.POISON,
        }
    ),
    # Terminal-ish: a human (or the retry policy) can requeue, and a
    # failed item that exhausts its attempts is poisoned.
    ItemState.FAILED: frozenset({ItemState.QUEUED, ItemState.POISON}),
    ItemState.POISON: frozenset({ItemState.QUEUED}),
    ItemState.DONE: frozenset(),
}


class MergeDisposition(StrEnum):
    """Whether a finished item's PR merges without a human.

    ``STOP_AT_PR`` is the default for everything, and R8.6 requires it be
    the default for remote-sourced items specifically: continuous intake
    must not silently delete the human merge gate. ``AUTO_MERGE`` is
    per-item opt-in AND ladder-gated - the R8.2 flag bundle
    (``auto_merge_when_green``) can withhold it, and this field can only
    ever request it, never grant it.
    """

    STOP_AT_PR = "stop_at_pr"
    AUTO_MERGE = "auto_merge"


class ItemSource(StrEnum):
    """Where the item came from. ``source_ref`` names the specific one."""

    LOCAL = "local"
    GITHUB = "github"
    LINEAR = "linear"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def mint_item_id() -> str:
    """``q-YYYYMMDD-HHMMSS.ffffff-<nonce>``.

    Shaped like :func:`kstrl.runid.mint_run_id` and for the same reason:
    the microsecond stamp makes same-second ids order deterministically
    by creation time, which is what gives the queue FIFO ordering within
    a priority band, and the nonce guards same-microsecond collisions.
    """
    stamp = _utc_now().strftime("%Y%m%d-%H%M%S.%f")
    return f"q-{stamp}-{secrets.token_hex(3)}"


def _warn_rejected(path: Path, reason: str) -> None:
    """Announce a skipped item rather than swallowing it.

    A silently dropped queue item is work that vanished. Mirrors
    ``autonomy._warn_rejected_state``.
    """
    warnings.warn(
        f"queue: rejected item {path} ({reason}); skipping",
        RuntimeWarning,
        stacklevel=3,
    )


def _as_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _as_str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    return value if isinstance(value, str) else default


@dataclass
class QueueItem:
    """One unit of intake: a spec plus everything decided about it.

    Not frozen: an item's ``state``, ``attempts``, and lease fields are
    exactly what a transition mutates. The frozen container in this
    module is :class:`QueueConfig`, which genuinely never changes after
    load.
    """

    item_id: str
    title: str
    spec_filename: str
    state: ItemState = ItemState.QUEUED
    #: Higher runs first; ties break by ``item_id`` (creation order).
    priority: int = 0
    created_at: str = ""
    updated_at: str = ""
    #: Execution attempts CHARGED so far - incremented before the rename
    #: into ``running/``, never after. See the module docstring.
    attempts: int = 0
    max_attempts: int = 3
    merge_disposition: MergeDisposition = MergeDisposition.STOP_AT_PR
    source: ItemSource = ItemSource.LOCAL
    #: Identifies the specific origin, e.g. ``0xfauzi/kstrl#153``. The
    #: processed-ids ledger (PR 3) dedupes on this.
    source_ref: str = ""
    #: Forward-compatibility for a global ``~/.kstrl/queue`` (roadmap
    #: open question 3). Empty means "the repo this queue lives in".
    #: Carried from day one so moving to a global queue is a config
    #: change rather than a migration of on-disk items.
    target_repo: str = ""
    project_name: str = ""
    lease_pid: int = 0
    lease_host: str = ""
    lease_expires_at: str = ""
    last_error: str = ""
    last_run_id: str = ""
    #: Why this item may never be retried automatically. Set only on the
    #: transition into ``poison/``.
    poison_reason: str = ""
    #: Earliest time this item may be claimed again (retry backoff, R8.6
    #: PR 2). Empty means "now". A payload written before this field
    #: existed decodes to empty, i.e. immediately ready.
    not_before: str = ""
    schema_version: int = QUEUE_SCHEMA_VERSION

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    def ready_at(self, now: datetime | None = None) -> bool:
        """Whether the retry backoff has elapsed.

        An UNPARSEABLE ``not_before`` counts as not-ready, the opposite of
        the lease-expiry default: there the fail-safe direction is to
        reclaim a stuck item, here it is to hold off spending. Both
        choices err away from launching a run.
        """
        if not self.not_before:
            return True
        deadline = _parse_iso(self.not_before)
        if deadline is None:
            return False
        return (now or _utc_now()) >= deadline

    @property
    def sort_key(self) -> tuple[int, str]:
        """Highest priority first, then oldest first."""
        return (-self.priority, self.item_id)

    def lease_expired(self, now: datetime | None = None) -> bool:
        """Whether this item's lease has lapsed.

        A missing or unparseable expiry counts as EXPIRED. An item
        holding a lease nobody can read is exactly the sleep/crash case
        the reaper exists for; treating it as live would wedge the
        queue forever.
        """
        expires = _parse_iso(self.lease_expires_at)
        if expires is None:
            return True
        return (now or _utc_now()) >= expires

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "title": self.title,
            "spec_filename": self.spec_filename,
            "state": str(self.state),
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "merge_disposition": str(self.merge_disposition),
            "source": str(self.source),
            "source_ref": self.source_ref,
            "target_repo": self.target_repo,
            "project_name": self.project_name,
            "lease_pid": self.lease_pid,
            "lease_host": self.lease_host,
            "lease_expires_at": self.lease_expires_at,
            "last_error": self.last_error,
            "last_run_id": self.last_run_id,
            "poison_reason": self.poison_reason,
            "not_before": self.not_before,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueueItem | None:
        """Decode a sidecar, tolerating fields written by older versions.

        Returns None only when the item has no identity or no spec -
        without those there is nothing to run. Every other malformed
        field falls back to its default, because dropping a whole unit
        of work over an unreadable priority would lose real work.
        """
        item_id = _as_str(data, "item_id")
        spec_filename = _as_str(data, "spec_filename")
        if not item_id or not spec_filename:
            return None
        # Both become path components. A sidecar is operator-writable and
        # (from PR 3) remote-adapter-writable, so it is untrusted input:
        # reject traversal here rather than at each join site (#185 F2).
        if not is_safe_component(spec_filename):
            return None

        raw_state = _as_str(data, "state", str(ItemState.QUEUED))
        try:
            state = ItemState(raw_state)
        except ValueError:
            state = ItemState.QUEUED

        raw_disposition = _as_str(
            data,
            "merge_disposition",
            str(MergeDisposition.STOP_AT_PR),
        )
        try:
            disposition = MergeDisposition(raw_disposition)
        except ValueError:
            # An unreadable disposition falls back to the gated value,
            # never to auto-merge: a corrupt field must not be able to
            # grant a permission nobody asked for.
            disposition = MergeDisposition.STOP_AT_PR

        raw_source = _as_str(data, "source", str(ItemSource.LOCAL))
        try:
            source = ItemSource(raw_source)
        except ValueError:
            source = ItemSource.LOCAL

        return cls(
            item_id=item_id,
            title=_as_str(data, "title") or item_id,
            spec_filename=spec_filename,
            state=state,
            priority=_as_int(data, "priority", 0),
            created_at=_as_str(data, "created_at"),
            updated_at=_as_str(data, "updated_at"),
            attempts=_as_int(data, "attempts", 0),
            max_attempts=_as_int(data, "max_attempts", 3),
            merge_disposition=disposition,
            source=source,
            source_ref=_as_str(data, "source_ref"),
            target_repo=_as_str(data, "target_repo"),
            project_name=_as_str(data, "project_name"),
            lease_pid=_as_int(data, "lease_pid", 0),
            lease_host=_as_str(data, "lease_host"),
            lease_expires_at=_as_str(data, "lease_expires_at"),
            last_error=_as_str(data, "last_error"),
            last_run_id=_as_str(data, "last_run_id"),
            poison_reason=_as_str(data, "poison_reason"),
            not_before=_as_str(data, "not_before"),
            schema_version=_as_int(
                data,
                "schema_version",
                QUEUE_SCHEMA_VERSION,
            ),
        )


@dataclass(frozen=True)
class QueueConfig:
    """``[queue]`` config.

    Only the fields the substrate itself needs. The daemon's knobs
    (poll interval, ``daily_budget_usd``, the poison breaker) land with
    ``ks serve`` in PR 2 rather than being declared here unused.
    """

    #: Execution attempts per item before it is poisoned. Bounds the
    #: retry loop even when the classifier is wrong.
    max_attempts: int = 3
    #: How long a claim stays valid without a heartbeat. The reaper
    #: recovers anything past this; it is the sleep/crash recovery knob.
    lease_ttl_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise QueueError(f"queue.max_attempts must be >= 1, got {self.max_attempts}")
        if self.lease_ttl_seconds <= 0:
            raise QueueError(f"queue.lease_ttl_seconds must be > 0, got {self.lease_ttl_seconds}")

    @classmethod
    def from_env(cls) -> QueueConfig:
        defaults = cls()
        attempts = os.environ.get("KSTRL_QUEUE_MAX_ATTEMPTS")
        ttl = os.environ.get("KSTRL_QUEUE_LEASE_TTL")
        return cls(
            max_attempts=(defaults.max_attempts if attempts is None else int(attempts)),
            lease_ttl_seconds=(defaults.lease_ttl_seconds if ttl is None else float(ttl)),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> QueueConfig:
        """Precedence: env > toml > defaults; reads ``[queue]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "queue")
        defaults = cls()
        max_attempts = (
            int(section["max_attempts"]) if "max_attempts" in section else defaults.max_attempts
        )
        lease_ttl_seconds = (
            float(section["lease_ttl_seconds"])
            if "lease_ttl_seconds" in section
            else defaults.lease_ttl_seconds
        )
        if "KSTRL_QUEUE_MAX_ATTEMPTS" in os.environ:
            max_attempts = int(os.environ["KSTRL_QUEUE_MAX_ATTEMPTS"])
        if "KSTRL_QUEUE_LEASE_TTL" in os.environ:
            lease_ttl_seconds = float(os.environ["KSTRL_QUEUE_LEASE_TTL"])
        return cls(
            max_attempts=max_attempts,
            lease_ttl_seconds=lease_ttl_seconds,
        )


@dataclass(frozen=True)
class PauseState:
    """Whether intake is paused, and until when.

    ``resume_after`` is what makes the R8.6 daily-budget stop
    self-clearing: the budget pause sets tomorrow's local midnight and
    the queue admits work again on its own, so a Friday-night budget hit
    does not mean a dead queue all weekend.
    """

    paused: bool = False
    reason: str = ""
    since: str = ""
    resume_after: str = ""

    def active(self, now: datetime | None = None) -> bool:
        """Paused right now, accounting for a lapsed ``resume_after``."""
        if not self.paused:
            return False
        deadline = _parse_iso(self.resume_after)
        if deadline is None:
            return True
        return (now or _utc_now()) < deadline

    def to_dict(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "reason": self.reason,
            "since": self.since,
            "resume_after": self.resume_after,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PauseState:
        return cls(
            paused=bool(data.get("paused", False)),
            reason=_as_str(data, "reason"),
            since=_as_str(data, "since"),
            resume_after=_as_str(data, "resume_after"),
        )


@dataclass
class JournalEntry:
    """One recorded transition. The queue's audit trail."""

    ts: str
    item_id: str
    from_state: str
    to_state: str
    reason: str = ""
    actor: str = ""
    attempts: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "item_id": self.item_id,
            "from": self.from_state,
            "to": self.to_state,
            "reason": self.reason,
            "actor": self.actor,
            "attempts": self.attempts,
            "detail": self.detail,
        }


def _journal_repair_entry() -> dict[str, Any]:
    """The row :meth:`Queue._journal` writes on finding a torn tail.

    Deliberately NOT a :class:`JournalEntry`: it is not a transition,
    it has no item and no states, and giving it the transition shape
    would make it one to every reader that walks the journal. It
    carries ``ts`` and ``event`` and nothing else, so
    ``journal_entries(item_id)`` filters it out by the same
    ``item_id`` check it already applies.
    """
    return {"ts": _iso(_utc_now()), "event": JOURNAL_REPAIR_EVENT}


def atomic_write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically, creating its directory.

    The write is ``atomicio.atomic_write_text`` (#291, where the mode and
    encoding rules are explained). What this adds is the ``mkdir``, for
    callers that write into a directory they have not created yet, which
    is every publish path in this module.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, content)


@contextmanager
def queue_lock(root_dir: Path, *, blocking: bool = False) -> Iterator[None]:
    """Hold the per-transition queue mutex.

    Short-lived by design: taken around one transition and released, so
    ``ks queue ls`` stays responsive while a run is in flight. The
    daemon's singleton lock is a SEPARATE file (``serve.lock``, PR 2) -
    conflating them would make listing the queue block on a running
    factory.

    POSIX only, like the A4 per-component lock and the run-level factory
    lock. Without ``fcntl`` we degrade to no exclusion rather than
    refusing to work; the sequencing that protects ``attempts`` does not
    depend on the lock.
    """
    lock_path = queue_root(root_dir) / LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield
        return

    handle: IO[str] = open(lock_path, "a+", encoding="utf-8")
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            raise QueueLockedError(f"queue is locked by another process ({lock_path})") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def queue_root(root_dir: Path) -> Path:
    """The queue directory for ``root_dir``."""
    return state_dir(root_dir) / QUEUE_DIR_NAME


class Queue:
    """Maildir-style work queue over ``.kstrl/queue/``.

    Reads scan the state directories; writes rename an item directory
    between them. No method rewrites history - the journal is
    append-only and the item directories carry current state.
    """

    def __init__(self, root_dir: Path, config: QueueConfig | None = None) -> None:
        self.root_dir = root_dir
        self.config = config or QueueConfig()

    @property
    def path(self) -> Path:
        return queue_root(self.root_dir)

    @property
    def journal_path(self) -> Path:
        return self.path / JOURNAL_FILENAME

    def state_path(self, state: ItemState) -> Path:
        return self.path / str(state)

    @property
    def staging_path(self) -> Path:
        """Where items are assembled before being published.

        Deliberately a sibling of the state directories rather than a
        hidden entry inside ``queued/``: a scan cannot reach it even if
        the name filter were removed. Same filesystem, so the publishing
        ``os.replace`` is still atomic (#185 F3).
        """
        return self.path / STAGING_DIR_NAME

    def item_dir(self, item: QueueItem) -> Path:
        if not is_safe_component(item.item_id):
            raise QueueError(f"unsafe queue item id {item.item_id!r}")
        return self.state_path(item.state) / item.item_id

    def ensure_dirs(self) -> None:
        for state in ALL_STATES:
            self.state_path(state).mkdir(parents=True, exist_ok=True)
        self.staging_path.mkdir(parents=True, exist_ok=True)

    def sweep_staging(self) -> int:
        """Delete every half-published item, returning how many went.

        A staging directory only exists while ``add`` is mid-publish, and
        ``add`` runs under the queue mutex, so anything found here while
        holding that mutex is by definition abandoned - no age heuristic
        is needed. Call it under ``queue_lock``; calling it without the
        lock can race a concurrent enqueue. This is the recovery half of
        the staging policy the scan filter enforces (#185 F3).
        """
        if not self.staging_path.is_dir():
            return 0
        swept = 0
        for entry in sorted(self.staging_path.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
                swept += 1
        if swept:
            self._journal(
                JournalEntry(
                    ts=_iso(_utc_now()),
                    item_id="",
                    from_state="staging",
                    to_state="swept",
                    reason="abandoned mid-publish",
                    detail={"count": swept},
                )
            )
        return swept

    # ---------------------------------------------------------------- read

    def _load_item_dir(self, item_path: Path, state: ItemState) -> QueueItem | None:
        # The DIRECTORY ENTRY is the item's identity, exactly as it is the
        # item's state. A sidecar that disagrees is corrupt or tampered
        # with, and trusting its ``item_id`` let a crafted value redirect
        # rmtree outside the queue (#185 F1).
        if item_path.is_symlink():
            _warn_rejected(item_path, "queue items may not be symlinks")
            return None
        if not is_safe_component(item_path.name):
            _warn_rejected(item_path, "unsafe item directory name")
            return None

        meta_path = item_path / META_FILENAME
        try:
            raw = meta_path.read_text(encoding="utf-8")
        except OSError as exc:
            _warn_rejected(item_path, f"unreadable {META_FILENAME}: {exc}")
            return None
        except UnicodeDecodeError as exc:
            # Its own message: "unreadable" points the operator at
            # permissions, and this file's permissions are fine.
            _warn_rejected(item_path, f"{META_FILENAME} is not valid UTF-8: {exc}")
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _warn_rejected(item_path, f"malformed {META_FILENAME}: {exc}")
            return None
        if not isinstance(data, dict):
            _warn_rejected(item_path, f"{META_FILENAME} is not an object")
            return None
        item = QueueItem.from_dict(data)
        if item is None:
            _warn_rejected(
                item_path,
                "missing/unsafe item_id or spec_filename",
            )
            return None
        # The DIRECTORY is authoritative for both identity and state; the
        # sidecar's copies are mirrors that may be one crash behind. See
        # the module docstring.
        if item.item_id != item_path.name:
            warnings.warn(
                f"queue: sidecar item_id {item.item_id!r} disagrees with "
                f"directory {item_path.name!r}; using the directory",
                RuntimeWarning,
                stacklevel=3,
            )
            item.item_id = item_path.name
        item.state = state
        return item

    def items(self, states: tuple[ItemState, ...] | None = None) -> list[QueueItem]:
        """Every item in the given states, in run order."""
        found: list[QueueItem] = []
        for state in states or ALL_STATES:
            directory = self.state_path(state)
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if not entry.is_dir():
                    continue
                # Dotted entries are never published items: staging lives
                # under its own directory now, but the filter stays as
                # defence in depth so a leftover from any source is inert
                # rather than surfacing as a phantom item (#185 F3).
                if entry.name.startswith("."):
                    continue
                item = self._load_item_dir(entry, state)
                if item is not None:
                    found.append(item)
        found.sort(key=lambda candidate: candidate.sort_key)
        return found

    def get(self, item_id: str) -> QueueItem | None:
        """Find one item by full id or unique prefix.

        A prefix matching more than one item raises rather than picking
        one: silently operating on the wrong unit of work is worse than
        making the operator type more characters.
        """
        exact: QueueItem | None = None
        prefixed: list[QueueItem] = []
        for item in self.items():
            if item.item_id == item_id:
                exact = item
                break
            if item.item_id.startswith(item_id):
                prefixed.append(item)
        if exact is not None:
            return exact
        if not prefixed:
            return None
        if len(prefixed) > 1:
            matches = ", ".join(candidate.item_id for candidate in prefixed)
            raise QueueError(f"{item_id!r} matches multiple items: {matches}")
        return prefixed[0]

    def find_by_source_ref(self, source_ref: str) -> QueueItem | None:
        """First item from a given origin, in any state.

        The in-queue half of PR 3's idempotency: a re-seen GitHub issue
        must not enqueue a second copy of the same work.
        """
        if not source_ref:
            return None
        for item in self.items():
            if item.source_ref == source_ref:
                return item
        return None

    def spec_path(self, item: QueueItem) -> Path:
        """The item's spec file, guaranteed to be inside the item.

        Belt and braces over the decode-time filter: an item constructed
        in memory rather than loaded from disk never passed through
        ``from_dict``, so the containment check is re-done here (#185 F2).
        """
        directory = self.item_dir(item)
        if not is_safe_component(item.spec_filename):
            raise QueueError(f"unsafe spec filename {item.spec_filename!r} on {item.item_id}")
        candidate = directory / item.spec_filename
        if directory.resolve() not in candidate.resolve().parents:
            raise QueueError(f"spec path for {item.item_id} escapes its item directory")
        return candidate

    def read_spec(self, item: QueueItem) -> str:
        return self.spec_path(item).read_text(encoding="utf-8")

    # ------------------------------------------------------------- journal

    def _journal(self, entry: JournalEntry) -> None:
        """Append one transition. Never raises into a caller's path.

        A failed journal write must not undo a transition that already
        happened on disk - the directory is the truth and the journal is
        the narration. Losing a line is visible (journal replay vs a
        directory scan disagree); rolling back a committed rename would
        be silent.

        #331: through ``appendio``, which repairs an unterminated tail
        before appending onto it. Without that, a crash mid-write cost
        the NEXT transition as well as the torn one, measured through
        :meth:`journal_entries`: ``['a']`` where a and b were both
        recorded. The ``"a+b"`` open widens what can fail - a journal
        this process can write but not read is refused rather than
        appended to blind - and the ``OSError`` handler below is what
        that widening lands in, unchanged.

        The repair row carries NO ``item_id``. Measured: that keeps it
        out of ``journal_entries(item_id)``, which is what ``ks queue
        show <id>`` renders, so a repair does not appear in one item's
        history as something that happened to it. The whole-journal
        read still returns it, which is where an operator looking for
        the incident would go.

        No lock. The callers hold ``queue_lock`` around the transition
        this narrates, and this method has never taken one; a lock here
        would nest a second under the first for no measured gain, and
        the journal is one file with one writer (#330's argument for
        the descriptor being its own lock does not apply either, because
        the exclusion the callers already hold covers it).
        """
        self.path.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        try:
            append_records(
                self.journal_path,
                line + "\n",
                repair=json.dumps(_journal_repair_entry(), ensure_ascii=False) + "\n",
            )
        except OSError as exc:
            warnings.warn(
                f"queue: journal append failed ({exc}); the transition itself succeeded",
                RuntimeWarning,
                stacklevel=3,
            )

    def journal_entries(self, item_id: str = "") -> list[dict[str, Any]]:
        """Read the journal, optionally filtered to one item.

        ONE clause for both causes here, and that is not the collapse
        #320 forbids: the two causes get separate handlers wherever a
        site has something to SAY, and this one says nothing to anybody -
        it returns ``[]`` and the caller renders an empty history. Two
        handlers with the same empty body would be a distinction no
        reader could ever observe. The malformed-line case below is
        already skipped per line for the same reason.
        """
        try:
            raw = self.journal_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        entries: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if item_id and data.get("item_id") != item_id:
                continue
            entries.append(data)
        return entries

    # --------------------------------------------------------------- write

    def _write_meta(self, item: QueueItem, directory: Path) -> None:
        atomic_write(
            directory / META_FILENAME,
            json.dumps(item.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )

    def add(
        self,
        spec_source: Path | str,
        *,
        title: str = "",
        priority: int = 0,
        merge_disposition: MergeDisposition = MergeDisposition.STOP_AT_PR,
        source: ItemSource = ItemSource.LOCAL,
        source_ref: str = "",
        target_repo: str = "",
        project_name: str = "",
        max_attempts: int | None = None,
        spec_filename: str = "spec.md",
        actor: str = "",
    ) -> QueueItem:
        """Enqueue a spec.

        ``spec_source`` is either a path to copy or literal spec text.
        The spec is COPIED into the item directory rather than
        referenced: an item whose spec file was edited or deleted after
        enqueue would run something other than what was reviewed.
        """
        if isinstance(spec_source, Path):
            content = spec_source.read_text(encoding="utf-8")
            spec_filename = spec_source.name
            resolved_title = title or spec_source.stem
        else:
            content = spec_source
            resolved_title = title or "untitled"
        if not content.strip():
            raise QueueError("refusing to enqueue an empty spec")
        # The text form of this API is what PR 3's remote adapters call,
        # so the filename is untrusted: a caller-supplied
        # "../../../escaped.md" wrote outside the queue while still
        # publishing a normal-looking item (#185 F2).
        if not is_safe_component(spec_filename):
            raise QueueError(f"spec filename must be a plain basename, got {spec_filename!r}")
        resolved_attempts = self.config.max_attempts if max_attempts is None else max_attempts
        # QueueConfig rejects this, but a per-item override bypassed it and
        # admitted an item with no execution budget at all (#185 F4).
        if resolved_attempts < 1:
            raise QueueError(f"max_attempts must be >= 1, got {resolved_attempts}")

        now = _iso(_utc_now())
        item = QueueItem(
            item_id=mint_item_id(),
            title=resolved_title,
            spec_filename=spec_filename,
            state=ItemState.QUEUED,
            priority=priority,
            created_at=now,
            updated_at=now,
            max_attempts=resolved_attempts,
            merge_disposition=merge_disposition,
            source=source,
            source_ref=source_ref,
            target_repo=target_repo,
            project_name=project_name,
        )

        self.ensure_dirs()
        directory = self.state_path(ItemState.QUEUED) / item.item_id
        if directory.exists():
            raise QueueError(f"queue item {item.item_id} already exists")
        # Build under the staging directory and rename into place, so a
        # scan never sees an item whose spec has not landed yet. Staging
        # sits outside the state directories entirely (#185 F3).
        staging = self.staging_path / item.item_id
        staging.mkdir(parents=True, exist_ok=False)
        try:
            atomic_write(staging / spec_filename, content)
            self._write_meta(item, staging)
            os.replace(str(staging), str(directory))
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        self._journal(
            JournalEntry(
                ts=now,
                item_id=item.item_id,
                from_state="",
                to_state=str(ItemState.QUEUED),
                reason="added",
                actor=actor,
                attempts=item.attempts,
                detail={"source": str(source), "source_ref": source_ref},
            )
        )
        return item

    def transition(
        self,
        item: QueueItem,
        to_state: ItemState,
        *,
        reason: str = "",
        actor: str = "",
        charge_attempt: bool = False,
        **updates: Any,
    ) -> QueueItem:
        """Move ``item`` to ``to_state``, recording why.

        Writes ``meta.json`` first and renames second - the rename is the
        commit point (see the module docstring). ``charge_attempt``
        increments ``attempts`` in the pre-rename write, so an interrupted
        transition over-counts rather than under-counts.
        """
        from_state = item.state
        legal = _LEGAL_TRANSITIONS.get(from_state, frozenset())
        if to_state not in legal:
            allowed = ", ".join(sorted(str(s) for s in legal)) or "nothing"
            raise QueueError(
                f"illegal queue transition {from_state} -> {to_state} "
                f"for {item.item_id} (legal: {allowed})"
            )

        source_dir = self.item_dir(item)
        if not source_dir.is_dir():
            raise QueueError(
                f"queue item {item.item_id} is not in {from_state} (expected {source_dir})"
            )
        target_dir = self.state_path(to_state) / item.item_id
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            raise QueueError(f"queue item {item.item_id} already exists in {to_state}")

        for key, value in updates.items():
            if not hasattr(item, key):
                raise QueueError(f"unknown QueueItem field {key!r}")
            setattr(item, key, value)
        if charge_attempt:
            item.attempts += 1
        item.state = to_state
        item.updated_at = _iso(_utc_now())

        # Order is load-bearing: meta first (so a charged attempt is
        # durable before any spend), rename second (the commit).
        self._write_meta(item, source_dir)
        try:
            os.replace(str(source_dir), str(target_dir))
        except OSError:
            # The move did not commit, so the in-memory object must stop
            # claiming it did - a caller that kept using this item would
            # otherwise address the wrong directory. ``attempts`` is
            # deliberately NOT rolled back: it is already durable on disk
            # and an over-count is the safe direction.
            item.state = from_state
            raise

        self._journal(
            JournalEntry(
                ts=item.updated_at,
                item_id=item.item_id,
                from_state=str(from_state),
                to_state=str(to_state),
                reason=reason,
                actor=actor,
                attempts=item.attempts,
            )
        )
        return item

    def lease(
        self,
        item: QueueItem,
        *,
        pid: int = 0,
        host: str = "",
        actor: str = "",
    ) -> QueueItem:
        """Claim a queued item for this worker. Spends nothing."""
        import socket

        expires = _utc_now() + timedelta(seconds=self.config.lease_ttl_seconds)
        return self.transition(
            item,
            ItemState.LEASED,
            reason="leased",
            actor=actor,
            lease_pid=pid or os.getpid(),
            lease_host=host or socket.gethostname(),
            lease_expires_at=_iso(expires),
        )

    def start(self, item: QueueItem, *, run_id: str = "", actor: str = "") -> QueueItem:
        """Begin execution. THIS is where the attempt is charged.

        Charged here rather than on completion because completion is the
        step a crash, a sleep, or a killed process can skip. An attempt
        that ran but was never counted is an unbounded retry loop.

        ``max_attempts`` is enforced HERE, at the spending boundary, not
        only in the CLI's retry policy. Review #185 F5 showed the bound
        was advertised but not enforced: start -> fail -> requeue ->
        lease -> start produced a running item with ``attempts == 2``
        against ``max_attempts == 1``. A bound that only holds when every
        caller remembers to check it is not a bound, and the callers here
        will be an unattended daemon and a reaper.
        """
        if item.attempts_remaining <= 0:
            raise QueueBudgetExhausted(
                f"{item.item_id} has used all {item.max_attempts} attempts; "
                "refusing to start (poison it or reset attempts explicitly)"
            )
        return self.transition(
            item,
            ItemState.RUNNING,
            reason="started",
            actor=actor,
            charge_attempt=True,
            last_run_id=run_id,
        )

    def adopt_lease(
        self,
        item: QueueItem,
        *,
        pid: int,
        host: str = "",
        actor: str = "",
    ) -> QueueItem:
        """Re-point a held lease at the process actually doing the work.

        Not a transition: the item stays where it is and only its lease
        fields move, so this is the one write that rewrites ``meta.json``
        in place. Needed because ``lease``/``start`` record the DAEMON's
        pid, while the run executes in a child process. Review #186 F1:
        if the daemon dies and the child survives, a successor's reaper
        sees the daemon gone, judges the lease dead, and requeues a run
        that is still executing - two factories on one repo.

        Refuses unless the item is leased or running: adopting a lease on
        a queued item would invent one.
        """
        if item.state not in (ItemState.LEASED, ItemState.RUNNING):
            raise QueueError(f"cannot adopt a lease on {item.item_id} in state {item.state}")
        import socket

        directory = self.item_dir(item)
        if not directory.is_dir():
            raise QueueError(f"queue item {item.item_id} is not at {directory}")
        item.lease_pid = pid
        item.lease_host = host or socket.gethostname()
        item.lease_expires_at = _iso(_utc_now() + timedelta(seconds=self.config.lease_ttl_seconds))
        item.updated_at = _iso(_utc_now())
        self._write_meta(item, directory)
        self._journal(
            JournalEntry(
                ts=item.updated_at,
                item_id=item.item_id,
                from_state=str(item.state),
                to_state=str(item.state),
                reason="lease adopted by the run process",
                actor=actor,
                attempts=item.attempts,
                detail={"lease_pid": pid},
            )
        )
        return item

    def finish_ok(self, item: QueueItem, *, actor: str = "") -> QueueItem:
        return self.transition(
            item,
            ItemState.DONE,
            reason="completed",
            actor=actor,
            last_error="",
        )

    def finish_failed(
        self,
        item: QueueItem,
        *,
        error: str = "",
        actor: str = "",
    ) -> QueueItem:
        """Record a red finish that MAY be retried.

        Whether it actually is retried is the daemon's decision (PR 2),
        made on positive evidence of an infrastructure error. Landing in
        ``failed/`` is not permission to retry.
        """
        return self.transition(
            item,
            ItemState.FAILED,
            reason="failed",
            actor=actor,
            last_error=error,
        )

    def poison(
        self,
        item: QueueItem,
        *,
        reason: str,
        actor: str = "",
    ) -> QueueItem:
        """Park an item that must never be retried automatically.

        The terminal state for spec-level failures and for anything whose
        failure could not be positively classified. Requires a reason:
        an item a human has to look at should say what it is waiting for.
        """
        if not reason.strip():
            raise QueueError("poison requires a reason")
        return self.transition(
            item,
            ItemState.POISON,
            reason="poisoned",
            actor=actor,
            poison_reason=reason,
        )

    def requeue(
        self,
        item: QueueItem,
        *,
        reason: str = "requeued",
        actor: str = "",
        reset_attempts: bool = False,
        not_before: str | None = None,
    ) -> QueueItem:
        """Send an item back to ``queued/``.

        ``reset_attempts`` is for an explicit human retry only. The
        automatic paths never reset the counter - a retry policy that
        can zero its own bound is not a bound.
        """
        updates: dict[str, Any] = {
            "lease_pid": 0,
            "lease_host": "",
            "lease_expires_at": "",
        }
        if not_before is not None:
            updates["not_before"] = not_before
        if reset_attempts:
            updates["attempts"] = 0
            updates["poison_reason"] = ""
            # A human authorizing a fresh run should not then wait out a
            # backoff computed for the automatic path.
            updates["not_before"] = ""
        return self.transition(
            item,
            ItemState.QUEUED,
            reason=reason,
            actor=actor,
            **updates,
        )

    def remove(self, item: QueueItem, *, actor: str = "") -> None:
        """Delete an item outright.

        Refuses while the item is running: removing the directory out
        from under a live worker loses the audit trail for money already
        spent.
        """
        if item.state is ItemState.RUNNING:
            raise QueueError(f"{item.item_id} is running; stop the run before removing it")
        directory = self.item_dir(item)
        # NOT ignore_errors: that swallowed permission and filesystem
        # failures, after which this journaled a "removed" record and the
        # CLI printed success for an item still on disk - a false operator
        # result AND a false audit trail (#185 F6).
        shutil.rmtree(directory)
        if directory.exists():
            raise QueueError(f"failed to remove {item.item_id}: {directory} still exists")
        self._journal(
            JournalEntry(
                ts=_iso(_utc_now()),
                item_id=item.item_id,
                from_state=str(item.state),
                to_state="removed",
                reason="removed",
                actor=actor,
                attempts=item.attempts,
            )
        )

    # ---------------------------------------------------------------- pause

    @property
    def pause_path(self) -> Path:
        return control_file(self.root_dir, CONTROL_PAUSE)

    def pause_state(self) -> PauseState:
        ensure_control_state(self.root_dir)
        untrusted = control_untrusted_reason(self.root_dir)
        if untrusted is not None:
            return PauseState(paused=True, reason=untrusted)
        try:
            raw = self.pause_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # The ONLY read failure that means "not paused". Everything
            # else is a marker we could not read, and a broad `except
            # OSError` here made a PermissionError read as RUNNING -
            # exactly the fail-open this module claims not to do (#185 F7).
            return PauseState()
        except OSError as exc:
            return PauseState(
                paused=True,
                reason=f"unreadable pause marker: {exc}",
            )
        except UnicodeDecodeError as exc:
            # The same fail-closed direction as the clause above, for the
            # failure that clause never caught. ``UnicodeDecodeError`` is
            # a ``ValueError``, so until #320 one non-utf-8 byte in the
            # marker escaped this handler entirely - not fail-open, which
            # is what #185 F7 fixed, but no answer at all: the traceback
            # left the caller with neither PAUSED nor RUNNING.
            return PauseState(
                paused=True,
                reason=f"pause marker is not valid UTF-8: {exc}",
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # An unreadable pause marker means PAUSED. Failing open here
            # would resume unattended spending on the strength of a
            # corrupt file.
            return PauseState(paused=True, reason="unreadable pause marker")
        if not isinstance(data, dict):
            return PauseState(paused=True, reason="malformed pause marker")
        return PauseState.from_dict(data)

    def is_paused(self, now: datetime | None = None) -> bool:
        return self.pause_state().active(now)

    def pause(
        self,
        *,
        reason: str = "",
        actor: str = "",
        resume_after: str = "",
    ) -> PauseState:
        state = PauseState(
            paused=True,
            reason=reason,
            since=_iso(_utc_now()),
            resume_after=resume_after,
        )
        ensure_control_state(self.root_dir)
        path = self.pause_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with control_lock(self.root_dir):
            atomic_write(
                path,
                json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n",
            )
        self._journal(
            JournalEntry(
                ts=state.since,
                item_id="",
                from_state="",
                to_state="paused",
                reason=reason,
                actor=actor,
                detail={"resume_after": resume_after},
            )
        )
        return state

    def resume(self, *, actor: str = "") -> PauseState:
        state = PauseState()
        ensure_control_state(self.root_dir)
        path = self.pause_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with control_lock(self.root_dir):
            atomic_write(
                path,
                json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n",
            )
        self._journal(
            JournalEntry(
                ts=_iso(_utc_now()),
                item_id="",
                from_state="paused",
                to_state="running",
                reason="resumed",
                actor=actor,
            )
        )
        return state

    # --------------------------------------------------------------- report

    def counts(self) -> dict[ItemState, int]:
        tally = dict.fromkeys(ALL_STATES, 0)
        for item in self.items():
            tally[item.state] += 1
        return tally

    def next_ready(self, now: datetime | None = None) -> QueueItem | None:
        """The item a worker should claim next, or None.

        Returns None while paused: the pause is an admission gate, and
        checking it anywhere other than the point of claiming would let
        a racing worker slip one more run past a budget stop.

        Items still inside their retry backoff are skipped rather than
        blocking the queue behind them - a flaking item must not starve
        the ones that would succeed.
        """
        moment = now or _utc_now()
        if self.is_paused(moment):
            return None
        for item in self.items((ItemState.QUEUED,)):
            if item.ready_at(moment):
                return item
        return None


def summarize(counts: dict[ItemState, int]) -> str:
    """One-line queue summary, omitting empty states."""
    parts = [f"{count} {state}" for state, count in counts.items() if count]
    return ", ".join(parts) if parts else "empty"
