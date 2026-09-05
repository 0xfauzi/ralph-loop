"""What an interrupted write costs each of the five other appenders (#331).

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
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kstrl.appendio import JOURNAL_REPAIR_EVENT
from tests.helpers.journal import lose_the_newline, tear


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
