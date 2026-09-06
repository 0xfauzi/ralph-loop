"""The transaction boundary ``append_entries`` rests on (#312, #327).

Round 2 of review on #327, F10. ``append_entries`` declines a lock, and
the argument for that decision is entirely in two properties: the probe
and the append go through ONE file description, so the path cannot be
replaced or a symlink retargeted between them, and the repair row plus
the batch go in ONE write, so nothing lands between the newline that
isolates a torn fragment and the entries the repair was for.

Both were argued in a docstring and neither was pinned. Measured, in a
copy of the tree with ``__pycache__`` cleared and
``PYTHONDONTWRITEBYTECODE=1``: two broken versions of
``append_entries``, one that reopens the file for the append and one
that writes the newline, the marker and the batch separately, each
pass 284 tests and 1 xfail across ``test_journal_torn_tail``,
``test_journal_one_writer``, ``test_decompose``,
``test_autonomy_ladder`` and ``test_config_control_plane``. A
mechanism nothing holds in place is the defect this PR has now hit
twice: round 1 of review found every helper of the AST guard stubbable
to a constant with that guard green.

Separate from ``test_journal_torn_tail.py`` because the subject is
different and the file-length ratchet is real: that file is what a tear
COSTS, measured in bytes on disk, and this one is HOW the write is
performed, measured in descriptors and syscalls, neither of which
leaves a trace on disk.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import pytest

import kstrl.appendio as appendio_mod
from kstrl.appendio import JOURNAL_REPAIR_EVENT
from kstrl.observability import read_progress_events
from tests.helpers.journal import TORN_FRAGMENT, audit, journal_at, tear


@dataclass
class JournalIO:
    """What one call did to the journal file, as opposed to with it."""

    #: The mode of every ``open`` of the journal, in order.
    opens: list[str] = field(default_factory=list)
    #: The payload of every ``write``, in order, across all of them.
    writes: list[bytes] = field(default_factory=list)


class CountingHandle:
    """A REAL handle that records what it was asked to do.

    Not a fake and not a stub: every call is delegated to the object
    ``open`` returned, so the bytes that reach the disk are the bytes
    the code under test wrote. That is asserted rather than claimed, by
    ``test_the_recorder_does_not_change_what_lands_on_disk``, because a
    recorder that swallowed a write would make the two tests above it
    pass by breaking the thing they measure.

    It exists because the property under test leaves NO trace on disk:
    a file written through two descriptors, or in three writes, is
    byte-for-byte the file written through one descriptor in one write.
    """

    def __init__(self, handle: IO[bytes], record: JournalIO) -> None:
        self._handle = handle
        self._record = record

    def write(self, data: bytes) -> int:
        self._record.writes.append(data)
        return self._handle.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def __enter__(self) -> CountingHandle:
        self._handle.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._handle.__exit__(*exc_info)


def without_timestamps(path: Path) -> list[dict[str, Any]]:
    """Every readable entry in the journal, minus its wall clock.

    Through ``read_progress_events``, not a private copy of it: these
    bytes are a REPAIRED journal, so one line is a torn fragment that
    is not supposed to parse, and the tolerance that skips it is the
    production reader's policy. A second copy would keep the old
    behaviour when the strict xfail in ``test_journal_torn_tail`` is
    closed, and this comparison would stop being a comparison of what
    readers see.
    """
    return [
        {key: value for key, value in entry.items() if key != "timestamp"}
        for entry in read_progress_events(path)
    ]


@contextlib.contextmanager
def recording(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
) -> Iterator[JournalIO]:
    """Count opens of ``path`` and writes through them, in ``evolution``.

    Shadows the module's global ``open`` rather than the builtin, so
    nothing outside ``kstrl.appendio`` is affected, and calls the real
    ``open`` for every other path so ``record_run``'s experiments.tsv
    behaves normally. ``kstrl.appendio`` since #331: the single
    ``"a+b"`` open moved to ``appendio.open_for_append`` when six
    appenders started sharing it, so patching ``kstrl.evolution``'s
    global would now record nothing and the two assertions below would
    both read an empty list.

    Through ``monkeypatch.context()`` rather than ``monkeypatch.undo()``
    in a ``finally``: ``undo`` empties the whole stack of the
    function-scoped fixture, which ``tests/conftest.py``'s two autouse
    guards share. Leaving this block would have taken the chdir into
    ``tmp_path``, the cleared ``KSTRL_*`` env and the agent-spend guard
    with it, so a bare ``EvolutionConfig()`` afterwards would resolve
    against the real checkout. Nothing does that today; a helper that
    invites it is worse than a test that does it once.
    """
    record = JournalIO()
    real_open = open

    def counting_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, mode, *args, **kwargs)
        if isinstance(file, (str, Path)) and Path(file) == path:
            record.opens.append(mode)
            return CountingHandle(handle, record)
        return handle

    with monkeypatch.context() as patched:
        patched.setattr(appendio_mod, "open", counting_open, raising=False)
        yield record


class TestTheTransactionBoundary:
    """#327 round 2, F10: the two properties the F1 decision RESTS on.

    F1 declined a lock and argued instead that ONE file description
    makes the probe and the append one transaction, and that ONE write
    keeps the repair and the entries it protects together. Both were
    argued in a docstring and neither was pinned; the module docstring
    above carries the measurement.

    Counting descriptors and writes rather than reading the source,
    because the property is about what the process does: a second open
    moved into a helper is the same defect, and an AST test would call
    it clean.
    """

    def test_the_probe_and_the_append_share_one_file_description(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One open, in the mode that makes it both readable and an append.

        Two opens is the shape this fix exists to remove: between the
        probe's close and the append's open, the path can be replaced or
        a symlink retargeted, and the append then lands somewhere the
        probe never looked. The mode is asserted too, because ``"a+b"``
        is what makes an unreadable journal raise here rather than be
        reported as "not torn" (round 1, F3).
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        tear(path)

        with recording(monkeypatch, path) as io:
            journal.append_entries([audit("beta")])

        assert io.opens == ["a+b"]

    def test_a_repair_and_the_entries_it_protects_are_one_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The newline, the marker and the batch go in a single write.

        Three writes is the shape that lets another appender land
        between the newline that isolates the fragment and the entries
        the repair was for. One write does not make that impossible
        (residual 3: a payload above the buffer is split), which is why
        the docstring says so, but it removes the two gaps that are
        certain.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        tear(path)

        with recording(monkeypatch, path) as io:
            journal.append_entries([audit("beta")])

        assert len(io.writes) == 1
        payload = io.writes[0]
        assert payload.startswith(b"\n")
        assert payload.count(b"\n") == 3
        assert JOURNAL_REPAIR_EVENT.encode() in payload
        assert b'"beta"' in payload

    def test_an_ordinary_append_is_one_open_and_one_write_too(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The un-torn path, so "one write" is not a repair-only property.

        Three entries, one write: a per-entry write would interleave a
        concurrent appender's line into the middle of this batch.
        """
        journal = journal_at(tmp_path)

        with recording(monkeypatch, journal.config.journal_path) as io:
            journal.append_entries([audit("a"), audit("b"), audit("c")])

        assert io.opens == ["a+b"]
        assert len(io.writes) == 1
        assert io.writes[0].count(b"\n") == 3

    def test_the_recorder_does_not_change_what_lands_on_disk(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The control for the two tests above.

        A recorder that swallowed a write would make both of them pass
        by breaking the thing they measure. The same sequence is run
        with the instrument and without it, and the two files are
        compared entry for entry and line for line. Timestamps are
        dropped from the comparison and nothing else is: the repair row
        carries a wall clock, so the two runs differ there by
        construction.
        """
        plain = journal_at(tmp_path / "plain")
        plain.append_entries([audit("alpha")])
        tear(plain.config.journal_path)
        plain.append_entries([audit("beta")])

        watched = journal_at(tmp_path / "watched")
        watched.append_entries([audit("alpha")])
        tear(watched.config.journal_path)
        with recording(monkeypatch, watched.config.journal_path):
            watched.append_entries([audit("beta")])

        expected = plain.config.journal_path
        actual = watched.config.journal_path
        assert without_timestamps(actual) == without_timestamps(expected)
        assert actual.read_bytes().count(b"\n") == expected.read_bytes().count(b"\n")
        assert TORN_FRAGMENT in actual.read_text(encoding="utf-8")
