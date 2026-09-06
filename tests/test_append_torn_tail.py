"""What an interrupted write costs each of the four JSONL appenders (#331).

``tests/test_journal_torn_tail.py`` is the worked example and stays
where it is: the evolution journal was #312 and has a whole surface of
its own (``get_repair_count``, ``ks evolve --status``, the trends tab).
This file is the same measurement for the appenders #331 found
afterwards, one class per file, and it is deliberately a COST test:
every assertion goes through the file's own production reader, because
"the bytes on disk look right" is not the claim. The claim is that the
record written after a crash is still there when the thing that reads
it asks.

Two crash shapes per file, both from ``tests/helpers/journal.py`` so
they are one claim spelled once:

- ``tear`` leaves a torn FRAGMENT. That fragment was never a record and
  is never coming back. What the repair saves is the NEXT record, which
  without it is concatenated onto the fragment and dropped with it.
- ``lose_the_newline`` leaves a COMPLETE record whose terminator never
  landed. This is the costlier one and the one the repair RECOVERS: two
  records are lost without it, and the earlier one was readable.

Each class also pins the un-torn path byte for byte. A writer that
padded unconditionally would pass every repair assertion here and
corrupt every ordinary append with a blank line, so that control is not
optional.

The fifth appender #331 found, ``record_run``'s experiments.tsv, is in
``tests/test_experiments_tsv_torn_tail.py``: its reader RENDERS a
concatenated line instead of dropping it, so its fix has a read half
these four do not have.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kstrl.appendio import JOURNAL_REPAIR_EVENT
from tests.helpers.journal import lose_the_newline, tear
from tests.helpers.rootperms import skip_as_root


def lines_of(path: Path) -> list[bytes]:
    return path.read_bytes().split(b"\n")


def repair_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if e.get("event") == JOURNAL_REPAIR_EVENT]


class TestProgressLogSurvivesATornTail:
    """``ProgressLog.emit``, read by ``ks status`` and the v1 reducer.

    ONE writer process: the factory builds one ``ProgressLog`` per run
    and every phase emits through it, so this file takes no lock. The
    ``"a+b"`` open is the trade stated in ``appendio``: a progress log
    this process can write but not read stops being appendable, and it
    is a file under the run root that the same user created.
    """

    def emit_three(self, tmp_path: Path) -> Any:
        from kstrl.observability import ProgressLog

        log = ProgressLog(tmp_path / "progress.jsonl", run_id="r1")
        log.emit("alpha", component_id="c1")
        return log

    def test_the_entry_after_a_torn_fragment_survives(self, tmp_path: Path) -> None:
        """Without the repair: ``['alpha']``, and beta is gone with the fragment."""
        from kstrl.observability import read_progress_events

        log = self.emit_three(tmp_path)
        tear(log.path)
        log.emit("beta", component_id="c1")

        assert [e.get("event") for e in read_progress_events(log.path)] == [
            "alpha",
            JOURNAL_REPAIR_EVENT,
            "beta",
        ]

    def test_a_record_that_lost_only_its_newline_is_recovered(
        self,
        tmp_path: Path,
    ) -> None:
        """Two records are at stake here, not one. Measured before the fix:
        ``['a1', 'a2']`` where three had been written and a fourth followed."""
        from kstrl.observability import read_progress_events

        log = self.emit_three(tmp_path)
        log.emit("a2", component_id="c1")
        lose_the_newline(log.path)
        log.emit("a3", component_id="c1")

        assert [e.get("event") for e in read_progress_events(log.path)] == [
            "alpha",
            "a2",
            JOURNAL_REPAIR_EVENT,
            "a3",
        ]

    def test_an_untorn_log_is_appended_to_byte_for_byte(self, tmp_path: Path) -> None:
        """The control: no pad, no row, no blank line on the ordinary path."""
        log = self.emit_three(tmp_path)
        before = log.path.read_bytes()
        log.emit("beta", component_id="c1")
        after = log.path.read_bytes()

        assert after.startswith(before)
        assert b"\n\n" not in after
        assert JOURNAL_REPAIR_EVENT.encode() not in after

    def test_the_repair_row_is_invisible_to_the_status_summary(
        self,
        tmp_path: Path,
    ) -> None:
        """It carries no ``component``, so ``summarize_events`` skips it.

        A repair row that named a component would invent an activity
        row for it, and ``ks status`` would report a phase for a
        component that did nothing. Measured through the real reader
        rather than argued from the schema.
        """
        from kstrl.observability import (
            latest_run_id,
            read_progress_events,
            summarize_events,
        )

        log = self.emit_three(tmp_path)
        tear(log.path)
        log.emit("beta", component_id="c1")
        events = read_progress_events(log.path)

        activity = summarize_events(events)
        assert set(activity.components) == {"c1"}
        assert activity.components["c1"].last_event == "beta"
        assert latest_run_id(events) == "r1"

    def test_the_repair_row_does_not_reach_the_sinks(self, tmp_path: Path) -> None:
        """Sinks are told about the RUN; this row is about the FILE.

        A Linear sink that received it would post a comment about a
        torn journal onto the issue for whatever component happened to
        be running.
        """
        from kstrl.observability import ProgressLog

        seen: list[dict[str, Any]] = []

        class Recorder:
            def handle_event(self, event: dict[str, Any]) -> None:
                seen.append(event)

        log = ProgressLog(tmp_path / "progress.jsonl", run_id="r1")
        log.attach_sink(Recorder())
        log.emit("alpha", component_id="c1")
        tear(log.path)
        log.emit("beta", component_id="c1")

        assert [e["event"] for e in seen] == ["alpha", "beta"]

    def test_the_repair_is_warned_about(self, tmp_path: Path) -> None:
        """Through the log's own ``warn``, which is the run's UI.

        The row is the durable trace and this is the live one; #312's
        argument for both applies unchanged here.
        """
        from kstrl.observability import ProgressLog

        warnings: list[str] = []
        log = ProgressLog(
            tmp_path / "progress.jsonl",
            run_id="r1",
            warn=warnings.append,
        )
        log.emit("alpha")
        assert warnings == []
        tear(log.path)
        log.emit("beta")

        assert len(warnings) == 1
        assert str(log.path) in warnings[0]

    def test_a_lost_component_completed_no_longer_strands_the_component(
        self,
        tmp_path: Path,
    ) -> None:
        """The cost in the reducer, which is what the TUI renders.

        Measured before the fix: the component stays ``running``,
        because ``component_completed`` was the row the tear swallowed.
        """
        from kstrl.observability import ProgressLog
        from kstrl.reducer import load_run_state

        log = ProgressLog(tmp_path / ".kstrl" / "progress.jsonl", run_id="r1")
        log.emit("factory_started")
        log.emit("component_started", component_id="c1")
        tear(log.path)
        log.component_completed("c1", 1.0, 1)

        state, _source = load_run_state(tmp_path, run_id="r1")
        assert {cid: c.status for cid, c in state.components.items()} == {"c1": "completed"}


class TestQueueJournalSurvivesATornTail:
    """``Queue._journal``, read by ``ks queue show``.

    No lock here either. Callers hold ``queue_lock`` by convention
    around the transition this narrates, and ``_journal`` itself has
    never taken one; adding one inside would nest a second lock under
    the first for no measured gain.
    """

    def queue_at(self, tmp_path: Path) -> Any:
        from kstrl.workqueue import Queue

        return Queue(tmp_path / "queue")

    def add(self, queue: Any, item_id: str) -> None:
        from kstrl.workqueue import JournalEntry

        queue._journal(
            JournalEntry(
                ts="2026-09-05T00:00:00Z",
                item_id=item_id,
                from_state="",
                to_state="queued",
            )
        )

    def test_the_transition_after_a_torn_fragment_survives(self, tmp_path: Path) -> None:
        queue = self.queue_at(tmp_path)
        self.add(queue, "a")
        tear(queue.journal_path)
        self.add(queue, "b")

        assert [e.get("item_id") for e in queue.journal_entries()] == [
            "a",
            None,
            "b",
        ]

    def test_a_transition_that_lost_its_newline_is_recovered(self, tmp_path: Path) -> None:
        queue = self.queue_at(tmp_path)
        self.add(queue, "a")
        self.add(queue, "b")
        lose_the_newline(queue.journal_path)
        self.add(queue, "c")

        assert [e.get("item_id") for e in queue.journal_entries() if e.get("item_id")] == [
            "a",
            "b",
            "c",
        ]

    def test_the_repair_row_carries_no_item_id(self, tmp_path: Path) -> None:
        """So a per-item history never shows it.

        ``ks queue show <id>`` filters on ``item_id``, and a repair row
        that borrowed the id of whichever item happened to be next
        would appear in that item's history as something that happened
        to it. The whole-journal read still returns it, which is where
        an operator looking for the incident would go.
        """
        queue = self.queue_at(tmp_path)
        self.add(queue, "a")
        tear(queue.journal_path)
        self.add(queue, "b")

        assert repair_rows(queue.journal_entries())
        assert [e.get("item_id") for e in queue.journal_entries("b")] == ["b"]
        # a's history is where a repair row that borrowed an id would
        # show up, since the tear was planted right after it. This line
        # compared one call to itself until #352 and passed for any
        # result, including the one it exists to refuse.
        assert [e.get("item_id") for e in queue.journal_entries("a")] == ["a"]

    def test_an_untorn_journal_is_appended_to_byte_for_byte(self, tmp_path: Path) -> None:
        queue = self.queue_at(tmp_path)
        self.add(queue, "a")
        before = queue.journal_path.read_bytes()
        self.add(queue, "b")
        after = queue.journal_path.read_bytes()

        assert after.startswith(before)
        assert b"\n\n" not in after
        assert JOURNAL_REPAIR_EVENT.encode() not in after

    @skip_as_root
    def test_a_journal_that_cannot_be_written_still_warns(self, tmp_path: Path) -> None:
        """``_journal`` swallowed ``OSError`` before this change and still does.

        The directory is the truth and the journal is the narration, so
        a failed append must not undo a rename that already happened.
        The ``"a+b"`` open widens what can fail (a journal this process
        cannot READ now raises where it used to be appended to blind),
        which is why this is pinned rather than assumed.
        """
        queue = self.queue_at(tmp_path)
        self.add(queue, "a")
        queue.journal_path.chmod(0o200)
        try:
            with pytest.warns(RuntimeWarning, match="journal append failed"):
                self.add(queue, "b")
        finally:
            queue.journal_path.chmod(0o600)


class TestJsonlSinkSurvivesATornTail:
    """``events.JsonlSink``, the v2 event log the reducer and TUI read.

    The only appender of the six that holds its handle open across a
    whole run, which is what ``handle_ends_without_newline`` taking a
    HANDLE was designed for: it probes ONCE, at the first emit, and
    every later emit writes bytes straight through.

    A typed ``JournalRepaired`` event rather than a raw line, because a
    raw line decodes to ``UnknownEvent`` and "unknown" is false for a
    row this build wrote deliberately. ``reducer.apply`` falls through
    every isinstance branch for it and touches only the clock.
    """

    def sink_at(self, path: Path) -> Any:
        from kstrl.events import JsonlSink

        return JsonlSink(path)

    def emit(self, sink: Any, component: str) -> None:
        from kstrl.events import ComponentStarted

        sink.emit(ComponentStarted(component=component))

    def names_in(self, path: Path) -> list[str]:
        from kstrl.events import read_events

        return [e.type for e in read_events(path)]

    def test_the_event_after_a_torn_fragment_survives(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = self.sink_at(path)
        self.emit(sink, "c1")
        sink.close()
        tear(path)

        reopened = self.sink_at(path)
        self.emit(reopened, "c2")
        reopened.close()

        assert self.names_in(path) == [
            "component_started",
            JOURNAL_REPAIR_EVENT,
            "component_started",
        ]

    def test_an_event_that_lost_its_newline_is_recovered(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = self.sink_at(path)
        self.emit(sink, "c1")
        self.emit(sink, "c2")
        sink.close()
        lose_the_newline(path)

        reopened = self.sink_at(path)
        self.emit(reopened, "c3")
        reopened.close()

        assert self.names_in(path).count("component_started") == 3

    def test_the_probe_happens_once_per_sink_not_once_per_event(
        self,
        tmp_path: Path,
    ) -> None:
        """The reason the handle-taking form exists.

        A sink that re-probed would pay a seek and a read on every
        event in the run, and would write a second repair row for a
        tear it had already repaired. Counted by shadowing the module's
        ``open``, the same way the write-boundary test does.
        """
        import kstrl.appendio as appendio_mod
        from kstrl.events import JsonlSink

        path = tmp_path / "events.jsonl"
        opens: list[str] = []
        real_open = open

        def counting_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if isinstance(file, (str, Path)) and Path(file) == path:
                opens.append(mode)
            return real_open(file, mode, *args, **kwargs)

        path.write_bytes(b'{"event":"x"')  # a torn tail waiting for the first emit

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(appendio_mod, "open", counting_open, raising=False)
            sink = JsonlSink(path)
            self.emit(sink, "c1")
            self.emit(sink, "c2")
            self.emit(sink, "c3")
            sink.close()

        assert opens == ["a+b"]
        assert self.names_in(path).count(JOURNAL_REPAIR_EVENT) == 1

    def test_a_first_write_that_raises_leaves_the_probe_to_the_next_emit(
        self,
        tmp_path: Path,
    ) -> None:
        """The handle is bound only once the write it was opened for lands.

        Binding it first put the sink in the no-probe branch for the
        rest of the run: ``EventBus.emit`` swallows the exception and
        increments a counter nothing reads, the next emit writes
        straight through onto the unterminated tail, and ``read_events``
        returns one ``UnknownEvent`` where two records were written.
        That is the defect this class exists to close, re-entered
        through the one path that reports nothing.

        The failure planted here is a raising ``append_terminated``,
        which is the shape of an ``ENOSPC`` on the write or an ``EIO``
        on the probe's own read.
        """
        import kstrl.events as events_mod

        path = tmp_path / "events.jsonl"
        path.write_bytes(b'{"event":"x"')  # a torn tail the first emit must repair

        handles: list[Any] = []
        real_open_for_append = events_mod.open_for_append
        real_append_terminated = events_mod.append_terminated
        failures = [OSError(28, "No space left on device")]

        def recording_open(target: Path) -> Any:
            handle = real_open_for_append(target)
            handles.append(handle)
            return handle

        def failing_once(*args: Any, **kwargs: Any) -> bool:
            if failures:
                raise failures.pop()
            return real_append_terminated(*args, **kwargs)

        sink = self.sink_at(path)
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(events_mod, "open_for_append", recording_open)
            patched.setattr(events_mod, "append_terminated", failing_once)
            with pytest.raises(OSError, match="No space left"):
                self.emit(sink, "c1")
            assert handles and handles[0].closed, (
                "the handle opened for a write that never landed must be closed, "
                "not leaked for the life of the run"
            )
            self.emit(sink, "c2")
        sink.close()

        assert self.names_in(path) == [JOURNAL_REPAIR_EVENT, "component_started"]

    def test_the_repair_row_is_not_an_unknown_event(self, tmp_path: Path) -> None:
        """``fold`` counts unknown events, and this is not one of them.

        A raw appended line would decode to ``UnknownEvent`` and show
        up in that count as an event this build did not recognise,
        which is the wrong thing to tell an operator about a row this
        build wrote on purpose.
        """
        from kstrl.events import read_events
        from kstrl.reducer import fold

        path = tmp_path / "events.jsonl"
        sink = self.sink_at(path)
        self.emit(sink, "c1")
        sink.close()
        tear(path)
        reopened = self.sink_at(path)
        self.emit(reopened, "c2")
        reopened.close()

        state = fold(read_events(path))
        assert state.unknown_events == 0
        assert set(state.components) == {"c1", "c2"}

    def test_an_untorn_log_is_appended_to_byte_for_byte(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = self.sink_at(path)
        self.emit(sink, "c1")
        before = path.read_bytes()
        self.emit(sink, "c2")
        after = path.read_bytes()
        sink.close()

        assert after.startswith(before)
        assert b"\n\n" not in after
        assert JOURNAL_REPAIR_EVENT.encode() not in after

    def test_every_line_is_still_one_json_object(self, tmp_path: Path) -> None:
        """The format contract the reducer rests on, checked on bytes.

        Binary writes replaced text writes here, so a line that grew a
        stray ``\\r`` or lost its encoding would still pass the reader
        tests above on this platform.
        """
        path = tmp_path / "events.jsonl"
        sink = self.sink_at(path)
        self.emit(sink, "c1")
        sink.close()
        tear(path)
        reopened = self.sink_at(path)
        self.emit(reopened, "c2")
        reopened.close()

        payload = [line for line in lines_of(path) if line.strip()]
        assert len(payload) == 4
        # Index 1 is the torn fragment, isolated on a line of its own
        # and not parseable by anything. That is the point: it was never
        # a record. The other three are.
        for index in (0, 2, 3):
            assert isinstance(json.loads(payload[index].decode("utf-8")), dict)


# ---------------------------------------------------------------------------
# the inbox log
# ---------------------------------------------------------------------------


class TestInboxSurvivesATornTail:
    """``Inbox._append``, under ``control_lock``, repaired with a BARE PAD.

    No repair row, and the reason is measured rather than stylistic: a
    valid-JSON row that ``InboxItem.from_dict`` returns None for is
    counted by ``scan().unparseable_count()``, and ``serve.py`` adds
    that count to ``open_count`` against the #190 admission cap. A
    repair row would therefore consume admission capacity until the
    next compaction, which is a running factory refusing work because a
    previous one crashed.

    The tear is still surfaced, by that same count: one unparseable
    line before and after the pad for a fragment, and none for a record
    that only lost its newline.

    No lock argument to make either. ``control_lock`` already wraps the
    whole probe and append, so #330 does not reach this file.
    """

    def inbox_at(self, root: Path) -> Any:
        from kstrl.inbox import Inbox

        return Inbox(root)

    def add(self, inbox: Any, title: str) -> None:
        from kstrl.inbox import ItemKind

        inbox.add(ItemKind.HALTED_RUN, title, dedupe_key=title)

    def test_the_item_after_a_torn_fragment_survives(self, tmp_path: Path) -> None:
        inbox = self.inbox_at(tmp_path)
        self.add(inbox, "first")
        tear(inbox.path)
        self.add(inbox, "second")

        assert sorted(i.title for i in inbox.items()) == ["first", "second"]

    def test_an_item_that_lost_its_newline_is_recovered(self, tmp_path: Path) -> None:
        inbox = self.inbox_at(tmp_path)
        self.add(inbox, "first")
        self.add(inbox, "second")
        lose_the_newline(inbox.path)
        self.add(inbox, "third")

        assert sorted(i.title for i in inbox.items()) == ["first", "second", "third"]

    def test_the_pad_adds_no_unparseable_line_and_no_admission_cost(
        self,
        tmp_path: Path,
    ) -> None:
        """The whole reason this file gets a pad and not a row.

        A fragment is one unparseable line before the pad and one
        after: the pad isolates it, it does not delete it. A record
        that lost its newline is none either way, because it was always
        a complete record. Either way the repair itself adds nothing to
        the count ``serve`` charges against the #190 cap, which a
        ``JOURNAL_REPAIR_EVENT`` row would, because ``from_dict``
        returns None for a row with no item fields.
        """
        inbox = self.inbox_at(tmp_path)
        self.add(inbox, "first")
        tear(inbox.path)
        before = inbox.scan().unparseable_count()
        self.add(inbox, "second")

        assert before == 1
        assert inbox.scan().unparseable_count() == 1

        clean = self.inbox_at(tmp_path / "clean")
        self.add(clean, "first")
        lose_the_newline(clean.path)
        self.add(clean, "second")

        assert clean.scan().unparseable_count() == 0
        assert repair_rows(list(clean._read_lines())) == []

    def test_an_untorn_inbox_is_appended_to_byte_for_byte(self, tmp_path: Path) -> None:
        inbox = self.inbox_at(tmp_path)
        self.add(inbox, "first")
        before = inbox.path.read_bytes()
        self.add(inbox, "second")
        after = inbox.path.read_bytes()

        assert after.startswith(before)
        assert b"\n\n" not in after


# ---------------------------------------------------------------------------
# the E8 dependency-scope telemetry log
# ---------------------------------------------------------------------------


class TestKnowledgeTelemetrySurvivesATornTail:
    """``record_dependency_scope_gap``, written by ``build_knowledge_context``.

    No lock, and the reason was read in the source rather than assumed.
    ``build_knowledge_context`` has one caller, ``factory._submit_args``,
    which is a closure the scheduler loop calls on the orchestrator
    thread BEFORE ``executor.submit``, so no worker process ever reaches
    this writer; the run-level ``factory.lock`` excludes a second
    orchestrator on the same root.

    A repair ROW here rather than the inbox's bare pad, because nothing
    counts these rows against anything: this reader has no production
    consumer, so the row costs nothing and is the durable trace.
    """

    def record(self, root: Path, component_id: str) -> None:
        from kstrl.knowledge import record_dependency_scope_gap

        record_dependency_scope_gap(component_id, 1, 1, root)

    def telemetry_path(self, root: Path) -> Path:
        from kstrl.knowledge import _E8_TELEMETRY_RELATIVE_PATH

        return root / _E8_TELEMETRY_RELATIVE_PATH

    def read(self, root: Path) -> list[str]:
        from kstrl.knowledge import read_dependency_scope_telemetry

        return [
            str(e.get("component_id"))
            for e in read_dependency_scope_telemetry(root)
            if e.get("component_id")
        ]

    def test_the_row_after_a_torn_fragment_survives(self, tmp_path: Path) -> None:
        self.record(tmp_path, "alpha")
        tear(self.telemetry_path(tmp_path))
        self.record(tmp_path, "beta")

        assert self.read(tmp_path) == ["alpha", "beta"]

    def test_a_row_that_lost_its_newline_is_recovered(self, tmp_path: Path) -> None:
        self.record(tmp_path, "a1")
        self.record(tmp_path, "a2")
        lose_the_newline(self.telemetry_path(tmp_path))
        self.record(tmp_path, "a3")

        assert self.read(tmp_path) == ["a1", "a2", "a3"]

    def test_an_untorn_log_is_appended_to_byte_for_byte(self, tmp_path: Path) -> None:
        self.record(tmp_path, "a1")
        before = self.telemetry_path(tmp_path).read_bytes()
        self.record(tmp_path, "a2")
        after = self.telemetry_path(tmp_path).read_bytes()

        assert after.startswith(before)
        assert b"\n\n" not in after

    def test_the_repair_row_carries_no_component_id(self, tmp_path: Path) -> None:
        """On disk with a ``timestamp`` and an ``event`` and nothing else.

        Every caller of the reader selects on ``component_id``, so a row
        without one counts towards no gap and can invent no withheld
        fact.
        """
        from kstrl.knowledge import read_dependency_scope_telemetry

        self.record(tmp_path, "alpha")
        tear(self.telemetry_path(tmp_path))
        self.record(tmp_path, "beta")

        rows = read_dependency_scope_telemetry(tmp_path)
        markers = repair_rows(rows)
        assert len(markers) == 1
        assert "component_id" not in markers[0]

    @skip_as_root
    def test_a_write_failure_is_still_swallowed(self, tmp_path: Path) -> None:
        """Telemetry must never block a factory run.

        The ``"a+b"`` open widens what can fail: a log this process can
        write but cannot read is refused where a plain ``"a"`` would
        have appended to it blind. The refusal lands in the OSError
        handler that was already there, so the caller still sees
        nothing, which is the whole contract of this writer.
        """
        self.record(tmp_path, "a1")
        self.telemetry_path(tmp_path).chmod(0o200)
        try:
            self.record(tmp_path, "a2")
        finally:
            self.telemetry_path(tmp_path).chmod(0o600)

        assert self.read(tmp_path) == ["a1"]
