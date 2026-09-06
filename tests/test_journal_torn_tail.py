"""#312: what an interrupted journal write costs the entries after it.

The read side of this was already covered, by
``tests/test_decompose.py::TestExcludedHistory::
test_a_torn_line_does_not_cost_the_note``: a torn tail must not make the
rest of the history unreadable. This file is the WRITE side, which the
read-side test cannot see. ``append_entries`` opened the file in append
mode and wrote, without ever asking whether the file ended in a newline,
so the next entry was concatenated onto the fragment and the pair became
one unparseable line. The fragment was already lost; the entry after it
was not, until the append destroyed it too.

Every assertion here runs against real bytes on a real file, torn by
truncating or by writing a partial line. A mock append cannot see the
defect, because the defect IS the bytes.

The blast radius is measured rather than asserted from the issue text,
and it is not uniform: ``test_a_tail_that_lost_only_its_newline_keeps_
its_record`` is the case where the interrupted write cost TWO records
rather than one, because a tail that lost only its terminator is a
complete record that the concatenation then destroys as well.

The static half of #312 - that ``append_entries`` is the only writer of
this file, which is what a second copy of the defect cost - lives in
``tests/test_journal_one_writer.py``. It was split out when the
file-length ratchet fired, and the split is along the seam: nothing in
that file opens a journal.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from kstrl.evolution import EXPERIMENTS_HEADER, JOURNAL_REPAIR_EVENT, SPEC_ISSUES_EVENT
from kstrl.observability import handle_ends_without_newline, read_progress_events
from tests.helpers.journal import (
    DANGLING_UTF8,
    TORN_FRAGMENT,
    audit,
    audits_in,
    component_result,
    journal_at,
    lose_the_newline,
    repair_rows_in,
    tear,
    terminate,
)


class TestTheEntryAfterATear:
    def test_the_entry_after_a_torn_line_survives(self, tmp_path: Path) -> None:
        """The issue, reproduced: two audits written, one readable.

        Measured on cbdff7c before the fix: ``['alpha']``. The torn
        fragment ate 'beta', which was written after the crash and had
        nothing to do with it.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        journal.append_entries([audit("beta")])

        assert audits_in(journal.config.journal_path) == ["alpha", "beta"]

    def test_the_torn_fragment_is_not_resurrected(self, tmp_path: Path) -> None:
        """The repair isolates the fragment, it does not repair it.

        A partial JSON object was never a record and cannot become one.
        What the fix owes is that it stops costing its successor, and
        claiming more than that in a docstring would be the defect this
        suite exists to catch.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        journal.append_entries([audit("beta")])

        path = journal.config.journal_path
        assert TORN_FRAGMENT in path.read_text(encoding="utf-8")
        parsed_types = [e.get("event_type") for e in read_progress_events(path)]
        assert parsed_types == [SPEC_ISSUES_EVENT, JOURNAL_REPAIR_EVENT, SPEC_ISSUES_EVENT]

    def test_a_tail_that_lost_only_its_newline_keeps_its_record(self, tmp_path: Path) -> None:
        """The tear that costs TWO records, not one.

        When the interruption lands between the last byte of a record
        and its newline, the record on disk is complete and readable.
        Appending onto it concatenates two whole objects into
        ``{...}{...}``, which ``json.loads`` rejects as "Extra data", so
        the reader loses the old record AND the new one. Measured
        before the fix: ``['a1', 'a2']`` from a file that held three
        audits and had just been handed a fourth.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("a1"), audit("a2"), audit("a3")])
        path = journal.config.journal_path
        lose_the_newline(path)
        assert audits_in(path) == ["a1", "a2", "a3"]

        journal.append_entries([audit("a4")])

        assert audits_in(path) == ["a1", "a2", "a3", "a4"]

    def test_an_autonomy_transition_after_a_tear_survives(self, tmp_path: Path) -> None:
        """The second writer, which the fix to the first would not reach.

        ``commit_transition`` had its own raw ``open(journal_path, "a")``
        and so had its own copy of #312. It now goes through
        ``append_entries``, which is what makes "the one writer of the
        journal's line format" true rather than asserted.
        """
        from kstrl.autonomy import AutonomyState, Transition, commit_transition

        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        commit_transition(
            AutonomyState(level=1),
            Transition(
                at="2026-08-20T00:00:00Z",
                direction="promote",
                from_level=0,
                to_level=1,
                actor="tester",
                trigger="manual",
                reason="test",
                evidence={},
            ),
            tmp_path,
        )

        events = read_progress_events(journal.config.journal_path)
        assert [e.get("event_type") for e in events] == [
            SPEC_ISSUES_EVENT,
            JOURNAL_REPAIR_EVENT,
            "autonomy_transition",
        ]


class TestWhatIsNotATear:
    def test_an_intact_journal_is_appended_to_unchanged(self, tmp_path: Path) -> None:
        """No blank line, no repair row, byte for byte what it was.

        The guard has to be silent on the overwhelmingly common path or
        it becomes noise that an operator learns to ignore.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        before = path.read_bytes()

        journal.append_entries([audit("beta")])

        after = path.read_bytes()
        assert after.startswith(before)
        assert after == before + json.dumps(audit("beta"), separators=(",", ":")).encode() + b"\n"
        assert repair_rows_in(path) == []

    def test_an_empty_journal_file_is_not_a_tear(self, tmp_path: Path) -> None:
        """Zero bytes has no unterminated line in it."""
        journal = journal_at(tmp_path)
        journal.config.journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal.config.journal_path.write_bytes(b"")

        journal.append_entries([audit("alpha")])

        assert audits_in(journal.config.journal_path) == ["alpha"]
        assert repair_rows_in(journal.config.journal_path) == []

    def test_a_terminated_but_malformed_tail_is_not_a_tear(self, tmp_path: Path) -> None:
        """Residual 4 of ``append_entries``, pinned rather than implied.

        The mechanism is argued there and summarised for operators in
        ``docs/evolution-metrics.md``; this is the file it describes,
        and the two assertions are what that argument claims: every
        record survives, and the incident is invisible to the count.

        Asserting 0 records an ACCEPTED residual, not a wish. Whoever
        closes it has three things to change together and this is the
        list: residual 4 on ``append_entries``, the "It is also a LOWER
        bound" sentence in ``docs/evolution-metrics.md``, and the 0
        below, which becomes 1.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        tear(path)
        terminate(path)

        journal.append_entries([audit("beta")])

        assert audits_in(path) == ["alpha", "beta"]
        assert journal.get_repair_count() == 0

    def test_a_missing_journal_file_is_not_a_tear(self, tmp_path: Path) -> None:
        """``"a+b"`` creates it, and a zero-length file has no
        unterminated line in it. Distinct from the empty-file case above
        in what it exercises: there the file is already there."""
        journal = journal_at(tmp_path)
        assert not journal.config.journal_path.exists()

        journal.append_entries([audit("alpha")])

        assert audits_in(journal.config.journal_path) == ["alpha"]
        assert repair_rows_in(journal.config.journal_path) == []

    def test_an_empty_append_repairs_nothing(self, tmp_path: Path) -> None:
        """Nothing to protect, so nothing is written.

        Repairing here would mutate the file on a call that was asked to
        add no records, and the next real append repairs it anyway.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)
        before = journal.config.journal_path.read_bytes()

        journal.append_entries([])

        assert journal.config.journal_path.read_bytes() == before

    def test_a_directory_where_the_journal_should_be_raises(self, tmp_path: Path) -> None:
        """An unopenable path raises for the caller to surface, and the
        three callers each already handle ``OSError`` their own way."""
        journal = journal_at(tmp_path)
        journal.config.journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal.config.journal_path.mkdir()

        with pytest.raises(OSError):
            journal.append_entries([audit("alpha")])


class TestTheRepairIsReportable:
    """#327 round 1, F5: a durable row nothing reports is reachable only
    by an operator who already suspects the problem."""

    def status_output(self, tmp_path: Path) -> str:
        """`ks evolve --status` against a real root, through the real CLI."""
        from click.testing import CliRunner

        import kstrl.cli as cli_mod

        result = CliRunner().invoke(
            cli_mod.cli,
            ["evolve", "--status", "--root", str(tmp_path), "--ui", "plain", "--no-color"],
        )
        assert result.exit_code == 0, result.output
        return str(result.output)

    def experiments_row(self, tmp_path: Path) -> None:
        """--status prints trends from experiments.tsv and exits early
        when there are none, so the repair line needs a row to reach.

        The header is the one ``record_run`` writes, read out of the
        source rather than retyped: a shorter hand-written header passes
        only because ``csv.DictReader`` tolerates it, which is not
        evidence that --status reaches the repair line on a real file.
        """
        path = journal_at(tmp_path).config.experiments_path
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = EXPERIMENTS_HEADER.split("\t")
        row = ["r1", "2026-08-20T00:00:00Z"] + ["0"] * (len(columns) - 2)
        path.write_text(
            EXPERIMENTS_HEADER + "\n" + "\t".join(row) + "\n",
            encoding="utf-8",
        )

    def test_a_repaired_journal_says_so_in_ks_evolve_status(self, tmp_path: Path) -> None:
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)
        journal.append_entries([audit("beta")])
        self.experiments_row(tmp_path)

        output = self.status_output(tmp_path)

        assert "1 interrupted write(s) repaired" in output
        assert "journal_repair" in output

    def test_a_repair_is_reported_when_there_are_no_experiments_yet(
        self,
        tmp_path: Path,
    ) -> None:
        """#327 round 2, F7: the state that PRODUCES repairs reported none.

        Only ``ks factory`` writes experiments.tsv; decompose and
        autonomy write to the journal. So "a repaired journal and no
        experiments" is the ordinary state of a project that ran
        ``ks decompose`` and crashed, and --status used to exit on the
        empty trends before it reached the repair line. No
        experiments.tsv is created here, deliberately: the pair of this
        test and the one above it is what pins the ORDER of those two
        lines in ``cli.evolve``.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)
        journal.append_entries([audit("beta")])
        assert not journal.config.experiments_path.exists()

        output = self.status_output(tmp_path)

        assert "1 interrupted write(s) repaired" in output
        assert "No experiments recorded yet" in output

    def test_a_recovered_record_is_not_reported_as_lost(self, tmp_path: Path) -> None:
        """#327 round 2, F8: possible loss reported as certain loss.

        A tail that lost only its newline is a COMPLETE record, and the
        repair is exactly what makes it readable again. Both audits are
        readable below, so a status line saying the line above the
        marker "was lost" reports data loss on the case this fix
        RECOVERS. ``docs/evolution-metrics.md`` always described it
        correctly; only the status wording was wrong.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        lose_the_newline(path)
        journal.append_entries([audit("beta")])
        assert audits_in(path) == ["alpha", "beta"]

        output = self.status_output(tmp_path)

        assert "1 interrupted write(s) repaired" in output
        assert "readable again" in output
        assert "was lost" not in output

    def test_a_healthy_journal_says_nothing(self, tmp_path: Path) -> None:
        """Silence at zero is the point: a line that prints on every
        healthy journal is a line an operator learns to skip."""
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        self.experiments_row(tmp_path)

        output = self.status_output(tmp_path)

        assert "interrupted write" not in output

    def test_the_count_is_of_rows_not_of_incidents(self, tmp_path: Path) -> None:
        """What ``get_repair_count`` promises, and no more. Two tears
        are two rows; #330 is the case where one tear becomes two."""
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)
        journal.append_entries([audit("beta")])
        tear(journal.config.journal_path)
        journal.append_entries([audit("gamma")])

        assert journal.get_repair_count() == 2

    def test_the_count_survives_an_unreadable_journal(self, tmp_path: Path) -> None:
        """It reads through ``read_progress_events``, which answers []
        rather than raising, so a status command cannot be taken down by
        the very file it is reporting on."""
        journal = journal_at(tmp_path)
        journal.config.journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal.config.journal_path.write_bytes(b"\xff\xfe not utf-8 at all\n")

        assert journal.get_repair_count() == 0


class TestTheSharedPredicate:
    """``handle_ends_without_newline`` is public and #331 is four other
    appenders that need it, so its contract is tested on its own rather
    than only through the journal."""

    def probe(self, tmp_path: Path, payload: bytes) -> bool:
        path = tmp_path / "sample.jsonl"
        path.write_bytes(payload)
        with open(path, "a+b") as handle:
            return handle_ends_without_newline(handle)

    def test_a_terminated_file_is_not_torn(self, tmp_path: Path) -> None:
        assert self.probe(tmp_path, b'{"a":1}\n') is False

    def test_an_unterminated_file_is_torn(self, tmp_path: Path) -> None:
        assert self.probe(tmp_path, b'{"a":1}') is True

    def test_an_empty_file_is_not_torn(self, tmp_path: Path) -> None:
        assert self.probe(tmp_path, b"") is False

    def test_an_undecodable_last_byte_is_answered_not_raised(self, tmp_path: Path) -> None:
        """The case a text-mode probe cannot serve at all: the last byte
        is the lead of a multi-byte sequence that was never finished."""
        assert self.probe(tmp_path, b'{"a":1}\xc3') is True

    def test_the_probe_does_not_move_where_the_next_write_lands(
        self,
        tmp_path: Path,
    ) -> None:
        """It seeks in order to read. In append mode the write position
        is the end regardless, and a caller that lost bytes to a stray
        seek would be a worse defect than the one this exists for."""
        path = tmp_path / "sample.jsonl"
        path.write_bytes(b'{"a":1}')
        with open(path, "a+b") as handle:
            handle_ends_without_newline(handle)
            handle.write(b'\n{"b":2}\n')

        assert path.read_bytes() == b'{"a":1}\n{"b":2}\n'


class TestAnUnreadableJournal:
    """#327 round 1, F3: a probe that could not read must never be taken
    for "not torn"."""

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root ignores the mode bits this test sets",
    )
    def test_a_write_only_journal_refuses_rather_than_appending_blind(
        self,
        tmp_path: Path,
    ) -> None:
        """The fail-open path this fix closes, on a real file.

        Mode 0200 is the reachable case where the read fails and the
        write would succeed. The earlier shape probed the tail through a
        separate ``open(path, "rb")`` and answered False on every
        ``OSError``, which means "not torn, go ahead": the append then
        joined the new entry to the fragment and lost it, exactly the
        defect #312 is about, arrived at from the other direction.

        Opening ``"a+b"`` once is what closes it. The permission needed
        to check the tail IS the permission the write demands, so an
        unreadable journal raises instead of being appended to blind.
        The cost is stated rather than hidden: on a write-only journal
        this now refuses, and the entry is not written at all. That is
        the fail-closed direction, and the caller warns.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        tear(path)
        before = path.read_bytes()
        path.chmod(0o200)

        try:
            with pytest.raises(PermissionError):
                journal.append_entries([audit("beta")])
        finally:
            path.chmod(0o600)

        assert path.read_bytes() == before
        assert b"beta" not in before


class TestTheTearIsVisible:
    def test_the_repair_is_recorded_in_the_journal(self, tmp_path: Path) -> None:
        """Healing forward is a judgement call, so it leaves a record.

        The row is the durable half: the process that tore the file is
        the process whose stderr nobody kept, and this is what an
        operator can grep months later.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        journal.append_entries([audit("beta")])

        rows = repair_rows_in(journal.config.journal_path)
        assert len(rows) == 1
        assert rows[0]["timestamp"]
        assert "not newline-terminated" in rows[0]["detail"]

    def test_the_repair_is_logged(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The live half, for whoever is watching the run it happened in."""
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        with caplog.at_level(logging.WARNING, "kstrl.evolution"):
            journal.append_entries([audit("beta")])

        assert any("did not end in a newline" in record.message for record in caplog.records)

    def test_the_repair_row_says_what_was_and_was_not_lost(self, tmp_path: Path) -> None:
        """The durable half of the F8 distinction, which nothing pinned.

        ``ks evolve --status`` says the line above the marker is either
        a lost fragment or a recovered record, and a test pins that. The
        ROW says the same thing to whoever greps the file months later,
        and until now only its first clause was pinned, so an edit to
        the either/or half was caught by nothing. Not shared as one
        constant with the CLI on purpose: this is data written into a
        file, that is a line printed by a command, and they are read by
        different people at different times.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)
        journal.append_entries([audit("beta")])

        detail = str(repair_rows_in(journal.config.journal_path)[0]["detail"])
        assert "torn fragment that was never readable" in detail
        assert "lost only its newline" in detail

    def test_one_tear_records_one_repair(self, tmp_path: Path) -> None:
        """The file ends in a newline once repaired, so later appends
        are ordinary appends. A repeated row would be a loop that
        reported a fresh incident on every write."""
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        journal.append_entries([audit("beta")])
        journal.append_entries([audit("gamma")])

        assert len(repair_rows_in(journal.config.journal_path)) == 1

    def test_the_repair_row_counts_towards_no_aggregate(self, tmp_path: Path) -> None:
        """A repair row must not move a number anyone reads.

        Every aggregate in ``evolution`` selects on ``event_type`` or
        windows by ``run_id``, and the row has its own type and no
        ``run_id``. This compares a torn journal against a clean one
        holding the same records, and pins that every clean answer is
        non-trivial so no comparison here is two empty results agreeing.
        """
        records = [component_result("r1", "c1"), component_result("r2", "c2"), audit("p")]

        clean = journal_at(tmp_path / "clean")
        clean.append_entries(records)

        torn = journal_at(tmp_path / "torn")
        torn.append_entries(records[:1])
        tear(torn.config.journal_path)
        torn.append_entries(records[1:])
        assert len(repair_rows_in(torn.config.journal_path)) == 1

        assert clean.get_concern_hit_rate()["components"] == 2
        assert torn.get_concern_hit_rate() == clean.get_concern_hit_rate()
        assert clean.get_fact_utilization()["measured"] == 2
        assert torn.get_fact_utilization() == clean.get_fact_utilization()
        assert len(clean.get_cross_run_patterns()) == 1
        assert [p.description for p in torn.get_cross_run_patterns()] == [
            p.description for p in clean.get_cross_run_patterns()
        ]
        # The spec-audit reader takes a different route to the file
        # (_read_all_entries, no run window), so it gets a record of its
        # own to find. Round 1 of review, F6: comparing two EMPTY
        # results is an assertion that cannot fail.
        assert len(clean.get_spec_issue_runs("p")) == 1
        assert clean.get_spec_issue_runs("p") == torn.get_spec_issue_runs("p")

    def test_the_repair_row_cannot_push_a_run_out_of_the_lookback_window(
        self,
        tmp_path: Path,
    ) -> None:
        """The second reason the row is invisible, and the reason it
        carries no ``run_id``.

        ``_read_journal_entries`` keeps the last N DISTINCT run_ids. A
        repair row with a run_id of its own would be one of those N, so
        a tear would silently shorten the history every aggregate reads
        by one run. Sized so that is exactly what would happen: lookback
        2, two real runs, one tear between them.
        """
        journal = journal_at(tmp_path)
        journal.config.lookback_runs = 2
        journal.append_entries([component_result("r1", "c1")])
        tear(journal.config.journal_path)
        journal.append_entries([component_result("r2", "c2")])

        assert journal.get_concern_hit_rate(lookback_runs=2)["runs"] == 2


class TestTheUndecodableTail:
    """A tear inside a multi-byte character, which is the case the write
    side survives and the read side does not."""

    def test_a_tail_torn_mid_utf8_sequence_is_still_repaired_on_disk(
        self,
        tmp_path: Path,
    ) -> None:
        """The probe reads bytes, so it works where a decode cannot."""
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        assert DANGLING_UTF8.endswith(b"\xc3")
        path.write_bytes(path.read_bytes() + DANGLING_UTF8)

        journal.append_entries([audit("beta")])

        lines = path.read_bytes().split(b"\n")
        assert DANGLING_UTF8 in lines
        assert any(b'"project":"beta"' in line for line in lines)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "read_progress_events answers [] to a decode error instead of "
            "skipping the one line, so an undecodable tail costs the whole "
            "journal. Deferred from #312, not fixed here. Delete this marker "
            "when the reader is fixed: strict=True makes an unexpected PASS "
            "a failure, so this test cannot be left claiming a gap that has "
            "been closed."
        ),
    )
    def test_an_undecodable_tail_should_cost_one_line_not_the_file(
        self,
        tmp_path: Path,
    ) -> None:
        """Asserts the DESIRED behaviour, and currently fails.

        Round 1 of review on #327, F4: the earlier version of this
        asserted ``read_progress_events(path) == []``, which is a
        passing test that requires the defect to stay. Whoever fixed the
        reader would have seen it go red and reasonably concluded they
        had broken something.

        The gap itself: ``read_progress_events`` names utf-8 and catches
        ``ValueError``, so it satisfies both halves of the CLAUDE.md
        encoding rule and is not one of the #320 sites. What it does
        with the failure is the problem. ``UnicodeDecodeError`` comes
        out of the line ITERATION, outside the per-line
        ``JSONDecodeError`` handler, so one bad byte anywhere returns
        nothing at all. That contradicts ``_read_all_entries``'s own
        stated policy that "one unreadable line must not cost the reader
        the rest of the history", and the write side above already
        survives this exact file.

        Deferred rather than fixed because it is a read-side change to a
        function three callers share, which deserves its own change and
        its own measurement. The deferral is defensible because kstrl's
        own writer emits pure ASCII; these bytes arrive from an
        operator's editor or a foreign writer.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        path.write_bytes(path.read_bytes() + DANGLING_UTF8)

        journal.append_entries([audit("beta")])

        assert audits_in(path) == ["alpha", "beta"]
