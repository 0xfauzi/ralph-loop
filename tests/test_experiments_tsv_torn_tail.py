"""What an interrupted write costs experiments.tsv, the one that RENDERS it.

Split out of ``tests/test_append_torn_tail.py`` (#352) rather than left
beside its five siblings, and the seam is not arbitrary. Every appender
in that file writes JSONL and every reader of one DROPS a line it cannot
parse; this file is TSV, its reader zips fields against a header, and a
concatenated line therefore comes back as a run with the previous run's
id and this run's timestamp in its ``completed`` column. That is a
different failure and it needs a different fix: a bare pad on the write
side, because TSV has no marker a reader would not render as a run, plus
a width filter on the read side, because no write can reach a line that
is already on disk.

The other file was at 796 lines against the 800-line ratchet, so the
next thing added to it had to split it first. This is that split, and
the class below is moved unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.journal import lose_the_newline, tear


class TestExperimentsTsvSurvivesATornTail:
    """``record_run``'s TSV row, read by ``ks evolve --status`` and the TUI.

    The worst of the seven, and the only one whose reader RENDERS the
    corruption instead of dropping it: ``csv.DictReader`` split the
    concatenated line on tabs and handed back a row carrying the
    previous run's id with THIS run's timestamp in its ``completed``
    column, and that row was displayed while the run being recorded
    vanished.

    BARE PAD, because TSV has no marker a reader would not render as a
    run, plus a reader-side width filter, which is what makes a file
    already corrupted on disk readable rather than only new appends
    safe.
    """

    def journal_at(self, tmp_path: Path) -> Any:
        from tests.helpers.journal import journal_at

        return journal_at(tmp_path)

    def record(self, journal: Any, run_id: str) -> None:
        from kstrl.factory import FactoryResult
        from kstrl.manifest import Manifest

        journal.record_run(
            run_id,
            Manifest(
                version="1",
                spec_file="s.md",
                project_name="p",
                base_branch="main",
                single_pr=False,
                components=[],
            ),
            FactoryResult(),
        )

    def test_the_run_after_a_torn_row_survives_uncorrupted(self, tmp_path: Path) -> None:
        journal = self.journal_at(tmp_path)
        self.record(journal, "run-1")
        tear(journal.config.experiments_path)
        self.record(journal, "run-2")

        rows = journal.get_experiment_trends()
        assert [r["run_id"] for r in rows] == ["run-1", "run-2"]
        assert all(None not in r and None not in r.values() for r in rows)

    def test_the_repair_is_logged_because_the_file_cannot_carry_a_row(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The bare pad is not an excuse for a silent repair (#352).

        Five appenders record a repair as a row their own reader
        returns; this one cannot, because every marker a TSV can carry
        is a field and a row of fields is a run. Without the log line a
        crash that tore this file and cost a run is something nothing in
        the product ever says: the pad leaves a short fragment,
        ``experiment_rows`` drops it on width, and no counter exists on
        that path.
        """
        journal = self.journal_at(tmp_path)
        self.record(journal, "run-1")
        tear(journal.config.experiments_path)

        with caplog.at_level(logging.WARNING, logger="kstrl.evolution"):
            self.record(journal, "run-2")

        assert [r.getMessage() for r in caplog.records if "experiments.tsv" in r.getMessage()]

    def test_an_untorn_write_logs_nothing(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The control: a repair message on every ordinary run is noise."""
        journal = self.journal_at(tmp_path)
        self.record(journal, "run-1")

        with caplog.at_level(logging.WARNING, logger="kstrl.evolution"):
            self.record(journal, "run-2")

        assert [r.getMessage() for r in caplog.records if "experiments.tsv" in r.getMessage()] == []

    def test_a_row_that_lost_its_newline_is_recovered(self, tmp_path: Path) -> None:
        journal = self.journal_at(tmp_path)
        self.record(journal, "run-1")
        self.record(journal, "run-2")
        lose_the_newline(journal.config.experiments_path)
        self.record(journal, "run-3")

        rows = journal.get_experiment_trends()
        assert [r["run_id"] for r in rows] == ["run-1", "run-2", "run-3"]

    def test_a_row_with_extra_columns_is_not_rendered(self, tmp_path: Path) -> None:
        """The reader-side half, for a file already corrupted on disk.

        The pad protects future appends. This protects the operator
        whose experiments.tsv was torn before the fix landed, and it is
        a separate mechanism because no write can reach a line that is
        already there.
        """
        journal = self.journal_at(tmp_path)
        self.record(journal, "run-1")
        with open(journal.config.experiments_path, "a", encoding="utf-8") as handle:
            handle.write("run-2\tstuck\textra\tcolumns\tbeyond\tthe\theader\n")

        assert [r["run_id"] for r in journal.get_experiment_trends()] == ["run-1"]

    def test_a_row_with_missing_columns_is_not_rendered(self, tmp_path: Path) -> None:
        journal = self.journal_at(tmp_path)
        self.record(journal, "run-1")
        with open(journal.config.experiments_path, "a", encoding="utf-8") as handle:
            handle.write("run-2\t2026-01-01\n")

        assert [r["run_id"] for r in journal.get_experiment_trends()] == ["run-1"]

    def test_a_pre_r3_1_file_keeps_every_row(self, tmp_path: Path) -> None:
        """The width filter is two legal widths, not one, and this is why.

        A file written before R3.1 has a SHORTER header, and this writer
        appends the full-width row onto it; the comment on
        ``EXPERIMENTS_HEADER`` promises that is tolerated. A filter that
        took the file header's own width as the only legal one would
        answer a rendering defect by deleting every row of a legacy
        file, which is worse than the defect. Both widths are legal
        while the file's header is a prefix of the current one.
        """
        from kstrl.evolution import EXPERIMENTS_HEADER

        legacy = "\t".join(EXPERIMENTS_HEADER.split("\t")[:-3])
        journal = self.journal_at(tmp_path)
        path = journal.config.experiments_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            legacy + "\n" + "\t".join(["run-0"] + ["0"] * 10) + "\n",
            encoding="utf-8",
        )
        self.record(journal, "run-1")

        rows = journal.get_experiment_trends()
        assert [r["run_id"] for r in rows] == ["run-0", "run-1"]

    def test_an_untorn_file_is_appended_to_byte_for_byte(self, tmp_path: Path) -> None:
        journal = self.journal_at(tmp_path)
        self.record(journal, "run-1")
        before = journal.config.experiments_path.read_bytes()
        self.record(journal, "run-2")
        after = journal.config.experiments_path.read_bytes()

        assert after.startswith(before)
        assert b"\n\n" not in after

    def test_the_header_is_still_written_once(self, tmp_path: Path) -> None:
        """The ``needs_header`` stat check is unchanged, and an empty file
        is not a torn one, so the first record still gets a header."""
        from kstrl.evolution import EXPERIMENTS_HEADER

        journal = self.journal_at(tmp_path)
        self.record(journal, "run-1")
        self.record(journal, "run-2")

        text = journal.config.experiments_path.read_text(encoding="utf-8")
        assert text.count(EXPERIMENTS_HEADER) == 1
        assert text.startswith(EXPERIMENTS_HEADER)
