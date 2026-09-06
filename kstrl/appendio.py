"""The one terminated append, so the torn-tail rule lives in one place.

#312 found it on the evolution journal, #331 found six more appenders
with the same defect, and every one was reproduced with a real tear, a
real append and the production reader rather than read and reasoned
about:

- ``observability.ProgressLog.emit`` (progress.jsonl): ``['alpha']``,
  the entry after the tear lost, and ``reducer.load_run_state`` leaves a
  component ``running`` when the lost row was its ``component_completed``.
- ``events.JsonlSink`` (events.jsonl, engineer.jsonl):
  ``['factory_started']``, the next event lost, and ``fold`` reports no
  components at all.
- ``workqueue.Queue._journal``: ``['a']``, the transition after it lost.
- ``inbox.Inbox._append``: ``['first']``, the item after it lost, and
  ``scan()`` reports one unparseable line.
- ``knowledge.record_dependency_scope_gap``: ``['alpha']``, the row
  after it lost.
- ``evolution.EvolutionJournal.record_run`` (experiments.tsv): the run
  after it lost AND the run before it RENDERED with shifted columns by
  ``ks evolve --status`` and the TUI trends tab. Worse than the JSONL
  cases, because that reader displays the corruption instead of
  dropping it.

The mechanism is one mechanism: a crash leaves a tail with no newline,
the next append concatenates onto it, and the tolerant reader drops BOTH
lines. Writing a newline FIRST repairs the tail into a line of its own.
That drops a genuine fragment, which was never readable and was never
going to be, and RECOVERS a tail that lost only its terminator, which
was a complete record. The second case is why the cost is not one entry:
appending onto a whole record destroys that record as well as the new
one.

What is NOT shared is what each file should WRITE on finding a tear, so
that is the caller's ``repair`` argument and nothing here has an opinion
about it. Four callers pass "" for a bare pad, and each has a reason
written at the call site: the inbox, because a repair row is counted by
``scan().unparseable_count()`` and would consume admission capacity
against the #190 cap; experiments.tsv, because TSV has no marker that a
reader would not render as a run; and the two files that are prose
rather than records, a proposal's applied stamp and the .gitignore
block, where a repair line would be a line a person reads.

``handle_ends_without_newline`` and the ``JOURNAL_REPAIR_EVENT`` name
moved here from ``observability`` and ``evolution`` when the second
caller arrived, which is #291's argument applied before the tenth copy
rather than after it. This module imports stdlib only, so every appender
in the package can reach it without an import cycle.

COST, measured on this machine at 2000 appends to a local disk: an
ordinary ``"ab"`` append with no probe is 32.8 us and an ``"a+b"`` open
plus the probe is 36.8 us, a difference of 4.0 us per append. Rerun it
before trusting it; the shape of the answer (a seek and a one-byte read)
is what matters, not the constant.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import IO

#: The journal row that records "an interrupted write was repaired
#: here". Its home is this module rather than ``evolution`` because the
#: appenders that write it and the aggregates that must ignore it are
#: now in six modules, and ``tests/test_event_names_have_one_home.py``
#: is the guard that keeps a second spelling from appearing.
JOURNAL_REPAIR_EVENT = "journal_repair"

#: What the repair row TELLS an operator, for the three appenders whose
#: row carries a detail field (``evolution``, ``observability``,
#: ``events``). Here for the same reason the name above is: it was
#: written out three times, once per appender, and the three had already
#: drifted into "a complete event" against "a complete record" for one
#: incident. It says nothing about WHICH appender ran, because the file
#: the row is in answers that and a per-site clause is what the three
#: copies differed in.
REPAIR_DETAIL = (
    "the preceding line was not newline-terminated when this row was written, so a "
    "write was interrupted. It is either a torn fragment that was never readable or a "
    "complete record that lost only its newline; both are on their own line now."
)


def handle_ends_without_newline(handle: IO[bytes]) -> bool:
    """True when the open binary file holds bytes and the last is not ``\\n``.

    Takes an OPEN HANDLE rather than a path, which is the whole design:

    - the caller opens once, in ``"a+b"``, and probes and appends
      through one file description, so there is no window between the
      two in which the path can be replaced, retargeted or deleted;
    - a file this process cannot read cannot be opened ``"a+b"`` at all,
      so an unreadable journal raises ``PermissionError`` out of the
      open instead of being reported as "not torn" and appended to
      blind (#327 round 1, F3: a path-taking probe that answered False
      on every ``OSError`` was fail-OPEN on a mode-0200 journal);
    - a long-lived appender such as ``events.JsonlSink`` can ask this
      once when it opens, which a path-taking predicate cannot serve.

    The consequence of ``"a+b"`` is stated rather than assumed, because
    it is a real trade: a file this process can write but not read stops
    being appendable. Every file routed through this module is under
    ``.kstrl/`` or the XDG control directory, created and read by the
    same user, with ONE exception: ``init_cmd._ensure_gitignore`` writes
    ``.gitignore`` at the repo root, which kstrl did not create. That
    one is safe for a reason of its own rather than by the rule, and it
    is checked rather than assumed: ``_gitignore_state`` reads the file
    through ``_read_text_or_none`` first and answers ``("keep", None)``
    for one it cannot read, and ``_ensure_gitignore`` returns on that
    answer before it reaches the append. So an unreadable .gitignore is
    refused with a message one step earlier, where this widening would
    refuse it with an ``OSError``.

    Each caller says so where it opens. ``proposals.mark_applied`` was
    the one that did not, for a round: the S1 routing added the call
    after the S2 sweep wrote the clause at the other seven.

    Binary, which is the point: the last byte of a file torn
    mid-utf-8-sequence cannot be decoded, and a text-mode probe would
    raise ``UnicodeDecodeError`` on exactly the file that most needs the
    repair. Seeks are reads; in append mode the write position is the
    end regardless, so this does not disturb where the caller's next
    write lands. Raises nothing of its own: an IO error belongs to the
    caller that owns the handle.
    """
    if handle.seek(0, os.SEEK_END) == 0:
        return False
    handle.seek(-1, os.SEEK_END)
    return handle.read(1) != b"\n"


def open_for_append(path: Path) -> IO[bytes]:
    """Open ``path`` for a probe-and-append through one file description.

    The ONE place in ``kstrl/`` that opens a file in append mode for a
    record writer, which is what ``tests/test_append_opens_have_one_home
    .py`` is a census over. Binary, so the probe above can read the last
    byte of a file torn mid-utf-8-sequence; ``"a+"`` so the probe and
    the append share a description.

    No ``mkdir``: the callers know whether the directory is theirs to
    create, and several of them already do it for other files in the
    same call. Raises ``OSError`` to the caller, which is the point of
    the mode.
    """
    return open(path, "a+b")


def append_terminated(handle: IO[bytes], payload: str, *, repair: str) -> bool:
    """Write ``payload`` through ``handle`` in ONE write, repairing a torn tail.

    ``payload`` is the complete text to append and must end in ``"\\n"``
    unless it is empty; a caller that hands over an unterminated payload
    is re-creating the defect this module exists for, so that is a
    ``ValueError`` rather than a silent fix. An empty payload writes
    nothing and repairs nothing: there is no record to protect and the
    next real append will do it.

    ``repair`` is a full line, terminator included, that goes between
    the newline and the payload when the tail was torn, or ``""`` for a
    bare pad. Returns True when a tear was found and repaired. Its
    terminator is required for the same reason the payload's is, and
    for a while it was not: a repair line without a trailing newline is
    glued to the first record, which loses that record. The check is
    unconditional rather than on the tear path, because the tear path
    is the one no caller exercises in ordinary running, so a caller
    that got it wrong would find out in a torn file years later.

    ONE ``write``, not three, and that is not an optimisation: it is
    what stops another appender landing between the newline that
    isolates a torn fragment and the records the repair was for.
    ``tests/test_journal_write_boundary.py`` counts the writes, because
    a version that wrote them separately passed 284 tests.

    The bytes are encoded here, once, in utf-8. Callers hand over
    ``str`` and name no codec, which is the write half of the two-sided
    contract in ``atomicio``: what lands is utf-8, and every reader of
    it names ``encoding="utf-8"`` and catches ``ValueError`` beside
    ``OSError``.

    Healing forward rather than raising, because every caller is a
    record-keeper: refusing to append would answer the loss of one
    record by losing every later one.
    """
    if payload and not payload.endswith("\n"):
        raise ValueError("append_terminated payload must end in a newline")
    if repair and not repair.endswith("\n"):
        raise ValueError("append_terminated repair must end in a newline")
    if not payload:
        return False
    repaired = handle_ends_without_newline(handle)
    if repaired:
        payload = "\n" + repair + payload
    handle.write(payload.encode("utf-8"))
    return repaired


@contextmanager
def _flock(handle: IO[bytes]) -> Iterator[None]:
    """Hold ``LOCK_EX`` on ``handle``'s own descriptor, flushed before it drops.

    The shape ``statedir.control_lock`` and ``workqueue.queue_lock``
    already use: the import is the first thing, so the no-``fcntl``
    platform yields once and returns rather than being a third branch
    inside the caller.

    TWO ways the exclusion is unavailable, and both take the same path,
    which is to append WITHOUT it. An entry must never be lost to the
    lock that was added to protect it.

    1. No ``fcntl`` module: Windows.
    2. A ``flock`` that RAISES. It is not supported on every mount:
       ``ENOLCK`` and ``EINVAL`` are what some FUSE, 9p and DrvFs
       mounts answer. Measured while this call was unguarded,
       ``append_entries`` raised ``BlockingIOError`` and left zero
       bytes on disk, where the same call before the lock existed
       wrote the entry. ``OSError`` exactly, not an enumeration of the
       codes we thought of: the kernel and the filesystem own that
       taxonomy, and #318 records three rounds of enumerating one and
       being wrong each time.

    Not a retry and not a spin: ``ENOLCK`` on a mount is a property of
    the mount, not a transient, so a loop would burn the caller's
    thread against a condition that will not change.

    The degradation is REPORTED the way the no-``fcntl`` one is, which
    is here and nowhere at run time. A warning per append would put a
    line in the log for every record written on such a filesystem, and
    the callers that would surface it are the record-keepers whose
    output the operator is reading for something else.

    BLOCKING, unconditionally, and there is no switch to ask otherwise.
    Of the two locks above, that matches ``control_lock``'s default
    (``blocking=True``) and NOT ``queue_lock``'s (``blocking=False``,
    which raises ``QueueLockedError`` on contention). The alternatives
    for a record write are dropping the record or spinning, and the
    lock is held for one probe and one write. The residual is real and
    is stated rather than assumed: a writer stopped inside the critical
    section stalls every other writer on that file, where before #330
    they could not wait at all.

    ``flush()`` before ``LOCK_UN`` is mandatory and is why the unlock is
    written out here rather than left to ``close()``: the handle is a
    ``BufferedRandom``, and unlocking first lets this process's bytes
    land after the exclusion has dropped. The release is NOT guarded:
    it is reached only when the acquisition succeeded, and by then the
    bytes are flushed, so an error there is a real anomaly with
    something to say rather than a lost entry.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        yield
        return
    try:
        yield
    finally:
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def appending(path: Path, *, lock: bool = False) -> Iterator[IO[bytes]]:
    """Hold an append handle on ``path``, optionally under an exclusive flock.

    The lock is taken on the journal's OWN descriptor, not on a sibling
    lock file. ``control_lock`` and ``queue_lock`` need an external file
    because each serializes MANY files; a record file has one writer
    function and the descriptor already open is the lock. That removes
    a mechanism instead of adding one, and #330's "the lock file cannot
    be created" case does not exist, because an unopenable file already
    raises ``OSError`` out of the open.

    POSIX only, and best-effort even there. Without ``fcntl``, and on a
    mount whose ``flock`` raises, this yields no exclusion and the
    append still happens; :func:`_flock` has both cases and why they
    take one path. That is the same degradation ``control_lock``,
    ``queue_lock`` and the factory lock already take on Windows.
    Callers that ask for a lock get one where the platform and the
    filesystem provide it and get today's behaviour everywhere else.

    Deadlock ordering: this lock is a LEAF. Nothing reached from inside
    a ``with appending(...)`` block takes another kstrl lock, and
    ``flock`` is per open file description, so two threads in one
    process serialize against each other too.

    MEASURED, two processes x 150 appends of 200 KB lines with 74 torn
    fragments planted between them, eight runs of each arm: without the
    lock 244 to 269 of 300 records were readable and 76 to 86 repair
    rows were written for 74 tears; with it 300 of 300 and exactly 74
    rows on every run. 0.08 to 0.11 s against 0.11 to 0.14 s. The
    unlocked loss varies by 25 records run to run, which is why the
    range is quoted rather than a single figure. All three residuals
    #330 lists (a stale probe, a doubled repair row, interleaved lines)
    go in one change. What is left is a SHORT write inside the one
    write, a foreign appender that does not lock, and readers, which are
    tolerant and unlocked and unchanged.

    Only ``evolution.EvolutionJournal.append_entries`` asks for the lock
    today. The other eight appendio callers either hold an outer lock
    already (``inbox`` under ``control_lock``, ``workqueue`` under the
    caller's ``queue_lock``) or have one writer process per file
    (``progress.jsonl``, ``events.jsonl``, ``engineer.jsonl``, the E8
    telemetry log, ``experiments.tsv``, a proposal file and
    ``.gitignore``), and each says which at its call site. Eight rather
    than six because #352 routed the last two that were standing on a
    reason in the append-open guard instead; the count is the routed
    census minus this module's own two rows and the journal.
    """
    handle = open_for_append(path)
    exclusion: AbstractContextManager[None] = _flock(handle) if lock else nullcontext()
    try:
        with exclusion:
            yield handle
    finally:
        handle.close()


def append_records(path: Path, payload: str, *, repair: str, lock: bool = False) -> bool:
    """Append ``payload`` to ``path``, repairing an unterminated tail first.

    The path-taking form of :func:`append_terminated`, for the five
    appenders that open per write. ``events.JsonlSink`` holds its handle
    open across a whole run and calls the handle-taking pair directly.

    Raises ``OSError`` to the caller, which is deliberate: the callers
    disagree about what a failed append means. The evolution journal
    lets it out, the queue journal warns, and the two telemetry writers
    swallow it. Deciding that here would take the choice away from the
    only code that knows.

    No ``mkdir`` here either; see :func:`open_for_append`.
    """
    with appending(path, lock=lock) as handle:
        return append_terminated(handle, payload, repair=repair)
