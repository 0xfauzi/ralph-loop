"""#330: the journal's probe and append happen under one exclusive lock.

``tests/test_journal_torn_tail.py`` is #312, the cost of an interrupted
write to a SINGLE writer, and this is the other question #327 left open:
what concurrent writers cost each other. It is a separate file for the
reason that file's own docstring gives for the static half being
separate, the file-length ratchet, and the seam is the same one: nothing
here asserts about a torn tail in isolation, and nothing there starts a
process.

#330 listed three residuals of an unlocked probe-and-append. Two are
observable from outside the process and are measured here:

- a STALE PROBE, which costs RECORDS: another writer's fragment lands
  between this one's probe and its write, so this write concatenates
  onto it and both lines go;
- a DOUBLED REPAIR ROW, which costs the COUNT: two writers repair the
  same tear and write a row each.

The third, interleaved lines from a short write, is not reachable from a
test: it needs the OS to return a short write on a regular file, which
means a signal or ENOSPC.

THE FUSE IS REAL TIME, not a mocked clock and not the suite's own
timeout. The defect this file would catch if the lock were taken wrongly
is a DEADLOCK, and a deadlock does not fail a test, it hangs it: every
``join`` here has a wall-clock bound and a hang is an explicit failure
with its own message. That is the correction from the #341 sweep, where
a fuse that lived inside the code path the defect disables ran for 26
minutes instead of failing.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest

from kstrl.appendio import JOURNAL_REPAIR_EVENT

fcntl = pytest.importorskip("fcntl", reason="flock is POSIX-only, as the helper says")

#: Wall-clock bound on each child. Measured, each run of the four
#: writers below takes under a second on this machine; this is the fuse,
#: not the expected duration, so it is generous by two orders of
#: magnitude. A child still alive at this point is a deadlock and is
#: reported as one.
JOIN_TIMEOUT_S = 90.0

#: Sized by measurement, not by taste: see the class docstring. Rounds
#: per writer, bytes of padding per entry, how often a writer "crashes"
#: and leaves a fragment, and how many writers contend.
ROUNDS = 200
PAD_BYTES = 200_000
TEAR_EVERY = 2
WRITERS = ("A", "B", "C", "D")


def _journal(path: Path) -> Any:
    from kstrl.evolution import EvolutionConfig, EvolutionJournal

    return EvolutionJournal(EvolutionConfig(journal_path=path))


def _contend(path_str: str, who: str) -> None:
    """One writer: real appends, with a fragment left behind periodically.

    Module level and argument-only so it survives the ``spawn`` start
    method, which is the default on macOS.

    The fragment is planted THROUGH the same lock the append takes,
    which is what makes this a measurement of the probe rather than of
    luck: it is a writer that died mid-line while holding the lock, so
    another writer must find the torn tail on its next probe. A
    fragment written outside the lock would be racing another writer's
    payload, which no probe can help with and which nothing here claims
    to fix.
    """
    from kstrl.appendio import appending

    path = Path(path_str)
    journal = _journal(path)
    for i in range(ROUNDS):
        journal.append_entries(
            [{"event_type": "component_result", "run_id": f"{who}-{i}", "pad": "x" * PAD_BYTES}]
        )
        if i % TEAR_EVERY == TEAR_EVERY - 1:
            with appending(path, lock=True) as handle:
                handle.write(b'{"torn": "' + who.encode("utf-8"))
    # A real append LAST, so the run does not end on a fragment nobody
    # has appended after. A trailing fragment is unrepaired by
    # construction rather than by defect, and it made the row count off
    # by exactly one in a way that says nothing about the lock.
    journal.append_entries([{"event_type": "component_result", "run_id": f"{who}-last"}])


def _read(path: Path) -> tuple[set[str], int, int]:
    """(run ids readable, repair rows, unparseable lines) straight off disk.

    Deliberately not through ``get_spec_audits``: this asks what a
    tolerant reader can still see, so it does its own tolerating.
    """
    run_ids: set[str] = set()
    repairs = 0
    unparseable = 0
    for raw in path.read_bytes().split(b"\n"):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            unparseable += 1
            continue
        if entry.get("event_type") == JOURNAL_REPAIR_EVENT:
            repairs += 1
        elif "run_id" in entry:
            run_ids.add(str(entry["run_id"]))
    return run_ids, repairs, unparseable


def _run_writers(path: Path) -> tuple[set[str], int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    procs = [multiprocessing.Process(target=_contend, args=(str(path), who)) for who in WRITERS]
    for proc in procs:
        proc.start()
    try:
        for proc in procs:
            proc.join(JOIN_TIMEOUT_S)
            if proc.is_alive():
                for other in procs:
                    other.kill()
                pytest.fail(
                    f"a writer was still alive after {JOIN_TIMEOUT_S}s. A deadlock "
                    "HANGS rather than failing, so this bound is the only thing "
                    "that turns it into a red test."
                )
        for proc in procs:
            assert proc.exitcode == 0, f"a writer died: exitcode {proc.exitcode}"
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.kill()
    return _read(path)


class TestConcurrentWritersDoNotCostEachOtherRecords:
    """The measurement that decided #330, run as a test.

    SIZED BY MEASUREMENT, and the first size was wrong. The mutant
    (``lock=False`` on the append) must fail this EVERY time, or the
    test is a coin flip that reports a fixed defect. Measured here, ten
    runs of each candidate, bytecode deleted before every run:

    - 2 writers, 120 rounds, a fragment every 4th round: 7 of 10 red.
      That is a test which calls the defect fixed three times in ten.
    - 4 writers, 200 rounds, a fragment every 2nd round: 10 of 10 red,
      confirmed at 30 of 30, with the unmutated head green 30 of 30.

    So the size below is four writers, not the two #330's text talks
    about. Two is the case the issue describes and four is what makes
    the measurement repeatable; nothing about the lock is specific to
    the count, and the extra writers only widen the window that has to
    be entered for the defect to show.

    200 KB is not decoration either. A small entry lands in one raw
    write and is unlikely to be caught mid-flight; the point of a large
    one is that the window between another writer's probe and its write
    is wide enough to actually be entered.

    The standalone two-process experiment in the lane directory measures
    the same thing without pytest, eight runs of each arm: unlocked, 244
    to 269 of 300 records readable and 76 to 86 repair rows for 74
    tears; locked, 300 of 300 and exactly 74 rows on every run.
    """

    def test_no_record_is_lost_to_a_concurrent_writer(self, tmp_path: Path) -> None:
        run_ids, _, _ = _run_writers(tmp_path / "evolution.jsonl")

        expected = {f"{who}-{i}" for who in WRITERS for i in range(ROUNDS)}
        missing = expected - run_ids
        assert not missing, (
            f"{len(missing)} of {len(expected)} records were lost to a concurrent "
            "writer. That is #330 residual 1: a probe that went stale before its write."
        )

    def test_one_unreadable_line_gets_one_repair_row(self, tmp_path: Path) -> None:
        """#330 residual 2, which costs ``get_repair_count`` its accuracy.

        The invariant is per LINE on disk, not per fragment planted, and
        that is deliberate. Two writers can plant fragments back to back
        with no append between them, and those two fragments are one
        unreadable line that one repair row correctly accounts for; a
        count against fragments planted would call that a defect, and it
        is a real state: the standalone experiment's UNLOCKED arm left
        70 to 74 unreadable lines for 74 fragments planted, over eight
        runs. What the lock guarantees is
        that no unreadable line gets a SECOND row, because the next
        writer probes after the first has written and finds a terminated
        file.
        """
        path = tmp_path / "evolution.jsonl"
        _, repairs, unparseable = _run_writers(path)

        assert unparseable > 0, (
            "no fragment survived to be counted, so this measured nothing. The "
            "writers plant them; if this fires, the planting is broken, not the lock."
        )
        assert repairs == unparseable, (
            f"{repairs} repair rows for {unparseable} unreadable lines. More rows "
            "than lines is #330 residual 2, two writers recording one incident; "
            "fewer is a line that was concatenated onto without being repaired."
        )


class TestTheLockIsOnTheJournalsOwnDescriptor:
    """The lock is TAKEN, and on the right file, and released after the flush.

    The contention test above measures the effect. This one names the
    mechanism, because an effect test on a fast machine can pass for
    reasons that have nothing to do with the lock.
    """

    def _record_flock(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, int]]:
        """Wrap ``fcntl.flock``, recording (inode, operation, file size).

        The real call still happens: a recorder that replaced it would
        pass on a build that took no lock at all. The size is read at
        call time and is what pins the flush ordering, since the bytes
        are only on disk once ``flush`` has run.
        """
        calls: list[tuple[int, int, int]] = []
        real = fcntl.flock

        def recording(fd: int, operation: int) -> None:
            stat = os.fstat(fd)
            calls.append((stat.st_ino, operation, stat.st_size))
            real(fd, operation)

        monkeypatch.setattr(fcntl, "flock", recording)
        return calls

    def test_an_append_locks_and_unlocks_the_journal_itself(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "evolution.jsonl"
        calls = self._record_flock(monkeypatch)
        _journal(path).append_entries([{"event_type": "component_result", "run_id": "r1"}])

        inode = path.stat().st_ino
        assert [(ino, op) for ino, op, _ in calls] == [
            (inode, fcntl.LOCK_EX),
            (inode, fcntl.LOCK_UN),
        ], (
            "the append must take LOCK_EX and release it on the journal's own "
            "descriptor, not on a sibling lock file and not on another file"
        )

    def test_the_bytes_are_flushed_before_the_lock_drops(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unlocking first lets this process's bytes land unprotected.

        The handle is a ``BufferedRandom``, so leaving the flush to
        ``close()`` would put the write after the exclusion dropped.
        Measured here by the file's size at the moment of LOCK_UN.
        """
        path = tmp_path / "evolution.jsonl"
        calls = self._record_flock(monkeypatch)
        _journal(path).append_entries([{"event_type": "component_result", "run_id": "r1"}])

        size_at_unlock = [size for _, op, size in calls if op == fcntl.LOCK_UN]
        assert size_at_unlock == [path.stat().st_size]
        assert size_at_unlock[0] > 0

    def test_an_empty_append_still_writes_nothing(self, tmp_path: Path) -> None:
        """The lock does not turn an empty append into a file-creating one.

        ``appending`` opens the path before it decides anything, so the
        journal comes into existence; what must not happen is a byte
        landing in it.
        """
        path = tmp_path / "evolution.jsonl"
        _journal(path).append_entries([])

        assert path.read_bytes() == b""
