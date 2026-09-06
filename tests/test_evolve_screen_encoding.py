"""The evolve screen against files kstrl wrote and cannot read back.

Split out of ``test_tui_config_guard.py`` when the file-length ratchet
fired a second time, and it was right a second time: that file is about
a broken kstrl.toml, and this one is about a broken DATA file. Different
contract, different write side, different fix.

The contract is the one CLAUDE.md states: kstrl writes utf-8, so every
reader of a file kstrl writes must NAME utf-8 and must catch
``ValueError`` alongside ``OSError``, because ``UnicodeDecodeError`` is
a ``ValueError`` and walks straight out of a fail-closed ``except
OSError``. ``EvolveScreen.on_mount`` reads three such files in a row and
all three had the defect; the round-one fix caught two of them and the
test that claimed "both" is why the third survived a review.
"""

from __future__ import annotations

from pathlib import Path

from textual.widgets import DataTable

from kstrl.evolution import EvolutionConfig
from kstrl.tui.widgets.config_problem import ConfigProblemBanner
from tests.helpers.tui_screens import evolve_screen


def test_a_non_utf8_experiments_file_does_not_crash_the_evolve_screen(
    tmp_path: Path,
) -> None:
    """Finding 4, and the crash class #289 is about.

    UnicodeDecodeError IS a ValueError, so it escaped the `except
    OSError` in get_experiment_trends and raised out of on_mount two
    lines after the config banner. CLAUDE.md names this rule verbatim.
    """
    from kstrl.evolution import EvolutionJournal

    kstrl_dir = tmp_path / ".kstrl"
    kstrl_dir.mkdir()
    (kstrl_dir / "experiments.tsv").write_bytes(b"run_id\tcompleted\n2026-\xe9x\t1\n")
    journal = EvolutionJournal(EvolutionConfig.load(tmp_path))
    assert journal.get_experiment_trends(last_n=5) == []


def test_a_non_utf8_proposal_does_not_crash_the_evolve_screen(tmp_path: Path) -> None:
    """The same defect one line earlier in the same on_mount:
    list_proposals read with no encoding behind `except OSError`."""
    from kstrl.proposals import existing_proposal_titles, list_proposals

    proposals = tmp_path / ".kstrl" / "proposals"
    proposals.mkdir(parents=True)
    (proposals / "prop-001.md").write_bytes(
        b"# PROP-001: \xe9\xe9\xe9 title\n**Type**: computational\n"
    )
    assert list_proposals(proposals) == []
    assert existing_proposal_titles(proposals) == set()


def test_a_non_utf8_journal_does_not_crash_the_evolve_screen(tmp_path: Path) -> None:
    """The THIRD undecodable reader on the same on_mount.

    ``get_cross_run_patterns`` reads the journal through
    ``observability.read_progress_events``, which opened with the
    locale encoding behind ``except OSError``. Measured on bea6c30:
    UnicodeDecodeError out of ``for line in f`` at observability.py:418,
    straight into the Textual event loop, with the two sibling readers
    already fixed. "Both" was three.
    """
    from kstrl.evolution import EvolutionJournal

    kstrl_dir = tmp_path / ".kstrl"
    kstrl_dir.mkdir()
    (kstrl_dir / "evolution.jsonl").write_bytes(
        b'{"event_type": "component_result", "run_id": "r1", "component_id": "\xe9x"}\n'
    )
    journal = EvolutionJournal(EvolutionConfig.load(tmp_path))
    assert journal.get_cross_run_patterns() == []


def test_the_journal_reader_is_shared_with_ks_status() -> None:
    """So the fix is not the evolve screen's alone.

    ``read_progress_events`` is the one reader for the progress log:
    ``ks status`` renders from it, the reducer replays from it and the
    evolution journal derives its cross-run patterns from it. Another
    agent flagged the same gap from ``ks status``; it is the same line.
    """
    src = Path(__file__).resolve().parent.parent / "kstrl"
    cli = (src / "cli.py").read_text(encoding="utf-8")
    reducer = (src / "reducer.py").read_text(encoding="utf-8")
    evolution = (src / "evolution.py").read_text(encoding="utf-8")
    assert "read_progress_events(progress_log_path)" in cli
    assert "read_progress_events(v1_path)" in reducer
    assert "read_progress_events(self.config.journal_path)" in evolution
    observability = (src / "observability.py").read_text(encoding="utf-8")
    assert 'with open(path, encoding="utf-8") as f:' in observability
    assert "except (OSError, ValueError):" in observability


async def test_the_evolve_screen_survives_all_three_undecodable_files(tmp_path: Path) -> None:
    """The three above, through the screen, which is where it mattered."""
    kstrl_dir = tmp_path / ".kstrl"
    (kstrl_dir / "proposals").mkdir(parents=True)
    (kstrl_dir / "proposals" / "prop-001.md").write_bytes(b"# PROP-001: \xe9\xe9\xe9\n")
    (kstrl_dir / "experiments.tsv").write_bytes(b"run_id\tcompleted\n2026-\xe9x\t1\n")
    (kstrl_dir / "evolution.jsonl").write_bytes(
        b'{"event_type": "component_result", "run_id": "r1", "component_id": "\xe9x"}\n'
    )
    async with evolve_screen(tmp_path) as (screen, _pilot):
        assert screen.query_one("#trends-table", DataTable).row_count == 0
        assert screen.query_one("#proposals-table", DataTable).row_count == 0
        assert screen.query_one("#patterns-table", DataTable).row_count == 0
        # The config itself is fine, so the banner must NOT claim it is
        # unreadable: these are data files, not configuration.
        assert screen.query_one(ConfigProblemBanner).display is False


def test_experiments_tsv_is_written_with_the_encoding_it_is_read_with(tmp_path: Path) -> None:
    """Encoding is a two-sided contract (CLAUDE.md): naming utf-8 on the
    read while the write follows the locale just moves the failure.

    #331 moved the write into ``kstrl.appendio``, so the pair is spelled
    across two files now and both halves are asserted. The helper is the
    ONE place the bytes are encoded, which is what makes checking it
    here enough: evolution hands it ``str`` and names no codec.
    """
    root = Path(__file__).resolve().parent.parent
    source = (root / "kstrl" / "evolution.py").read_text(encoding="utf-8")
    assert "append_records(self.config.experiments_path" in source
    assert 'read_text(encoding="utf-8")' in source
    helper = (root / "kstrl" / "appendio.py").read_text(encoding="utf-8")
    assert 'payload.encode("utf-8")' in helper


def test_a_proposal_survives_being_written_under_an_ascii_locale(tmp_path: Path) -> None:
    """The WRITE side of the proposal reader fixed two tests up.

    `save_proposals` wrote prop-*.md with the locale encoding, and its
    text is LLM output: one curly quote makes that a UnicodeEncodeError,
    which is a ValueError and so walks out of the adjacent `except
    OSError`. Measured: under a US-ASCII preferred encoding,
    `write_text` raises. That write is explicitly non-fatal ("proposal
    write failed (non-fatal)"), so this took the run down instead.

    A child process, because `locale.getpreferredencoding` is read at
    interpreter start; the helper is IMPORTED from test_atomicio rather
    than copied, which is what #291 set it up for.
    """
    from tests.test_atomicio import run_under_c_locale

    body = (
        "from pathlib import Path\n"
        "from kstrl.evolution import EvolutionConfig, EvolutionJournal, HarnessProposal\n"
        "out = Path(sys.argv[1])\n"
        "journal = EvolutionJournal(EvolutionConfig.load(out))\n"
        "written = journal.save_proposals([HarnessProposal(\n"
        "    id='PROP-001',\n"
        "    title='quote',\n"
        "    description='the agent\\u2019s note',\n"
        "    proposal_type='computational',\n"
        "    target='claude_md',\n"
        "    suggested_change='c',\n"
        "    source_patterns=[],\n"
        ")], out)\n"
        "print(len(written))\n"
    )
    result = run_under_c_locale(tmp_path, body, str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1", result.stdout
    written = (tmp_path / "prop-001.md").read_bytes()
    assert "the agent\u2019s note".encode() in written
