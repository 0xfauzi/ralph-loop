"""#333: the evolve screen reports repaired journal writes, and only then.

``ks evolve --status`` has reported them since #312 and this screen did
not, which is the gap #333 names. The argument for writing a durable
``journal_repair`` row at all was that under the TUI the logger warning
goes to ``orchestrator.log`` where nobody is looking; the TUI was then
the one surface that built an ``EvolutionJournal``, read the journal for
its patterns tab, and said nothing about the rows.

Its own file rather than a fourth case in ``test_evolve_screen.py``, for
the reason ``test_evolve_screen_encoding.py`` is separate too: that file
is D4's three tabs, this is one line above them, and a shared file would
give the two subjects one failure message.

The journal here is torn for real and appended to for real, through
``tests/helpers/journal.py``, so the row under test is a row
``append_entries`` wrote rather than one a test typed. A hand-written
row would pass with the repair mechanism deleted.
"""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Static

from tests.helpers.journal import audit, journal_at, tear
from tests.helpers.tui_screens import evolve_screen


def _seed_repaired_journal(tmp_path: Path) -> None:
    """One real repair: append, crash mid-line, append again."""
    journal = journal_at(tmp_path)
    journal.append_entries([audit("alpha")])
    tear(journal.config.journal_path)
    journal.append_entries([audit("beta")])
    assert journal.get_repair_count() == 1, "the fixture did not produce a repair row"


class TestTheEvolveScreenReportsRepairs:
    async def test_a_repaired_journal_puts_the_count_on_the_screen(
        self,
        tmp_path: Path,
    ) -> None:
        """The acceptance criterion of #333, and the mutation target.

        Asserts on the COUNT and on the path, not merely on the line
        being visible: a line that appears with the wrong number is the
        same defect as no line, and the path is what tells an operator
        which file to open.
        """
        _seed_repaired_journal(tmp_path)

        async with evolve_screen(tmp_path) as (screen, _pilot):
            line = screen.query_one("#evolve-repairs", Static)
            rendered = str(line.content)

            assert line.display is True
            assert "1 interrupted write(s) repaired" in rendered
            assert str(tmp_path / ".kstrl" / "evolution.jsonl") in rendered
            assert "readable again" in rendered

    async def test_an_intact_journal_says_nothing(self, tmp_path: Path) -> None:
        """Silence at zero, which is the whole reason the line is worth
        having: one that prints on every healthy journal is one an
        operator learns to skip. The journal here is real and non-empty,
        so this is "nothing to report" rather than "nothing to read"."""
        journal_at(tmp_path).append_entries([audit("alpha")])

        async with evolve_screen(tmp_path) as (screen, _pilot):
            assert screen.query_one("#evolve-repairs", Static).display is False

    async def test_no_journal_at_all_says_nothing_either(self, tmp_path: Path) -> None:
        """A project that has never run a factory. Separate from the
        case above because it reaches the count through a file that does
        not exist, and ``get_repair_count`` answering 0 rather than
        raising is what this screen depends on."""
        async with evolve_screen(tmp_path) as (screen, _pilot):
            assert screen.query_one("#evolve-repairs", Static).display is False

    async def test_a_reload_drops_a_count_the_journal_no_longer_has(
        self,
        tmp_path: Path,
    ) -> None:
        """``r`` re-runs the loader, so the line has to be able to go
        DOWN as well as up. A ``_show_repairs`` that only ever showed
        would leave a stale count on the screen after the journal was
        replaced, and the screen would be reporting a file that no
        longer says that."""
        _seed_repaired_journal(tmp_path)

        async with evolve_screen(tmp_path) as (screen, _pilot):
            assert screen.query_one("#evolve-repairs", Static).display is True

            (tmp_path / ".kstrl" / "evolution.jsonl").unlink()
            screen.reload()

            assert screen.query_one("#evolve-repairs", Static).display is False
