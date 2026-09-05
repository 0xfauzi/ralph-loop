"""#320: every text read in ``kstrl/`` names utf-8 and answers for the decode.

THE RULE, which CLAUDE.md has stated since #291 and which nothing checked
until #320: a reader of any file kstrl writes must name
``encoding="utf-8"``, AND must catch ``ValueError`` alongside ``OSError``,
because ``UnicodeDecodeError`` is a ``ValueError`` and walks straight past
a fail-closed ``except OSError``. ``init_cmd._read_text_or_none`` is the
worked example. #319 fixed the seam every CLI command sits behind and said
so; this is the rest.

WHAT THE SWEEP FOUND, re-derived rather than trusted. #320 estimated
"roughly 15" from an audit whose line numbers had already gone stale. The
measured census over 52 read-mode text decodes in ``kstrl/``:

    15  guarded by an OSError-ish handler with nothing covering the
        decode - the escape, and the issue's estimate was exact
     6  no encoding named, so the read is whatever the locale says
     1  of those 6 is also one of the 15 (``fixtures.py``)
    --
    20  offender sites, now zero
     6  compliant by construction: ``errors=`` is not "strict", so the
        decode cannot raise at all (measured, not assumed)
    12  no ``try`` anywhere around them, so nothing is claimed and
        nothing escapes: not offenders under this rule

A 21st site is not in that table because no census of READ CALLS can see
it. ``factory.py``'s run lock reads the holder pid back out of an ``"a+"``
handle - ``fp.read(64)`` under ``except OSError`` - and the decode is at
the read, not at the ``open``. ``encodingwalk._through_handles`` is the
half of the walk that follows the handle, and that site is the only one
of its shape in the package.

WHAT IS OUTSIDE THIS POPULATION, named so it is not rediscovered as new.
The declared population is a file's text obtained through ``read_text``
or ``open``. A CHILD PROCESS's output is the same defect class and is not
in it: ``subprocess.run(..., text=True)`` with no ``encoding=`` decodes
with ``locale.getencoding()`` and no error handler, so it is strict.
Measured at ``0f5bf45``: 62 such calls in 16 modules, ``git.py`` holding
23 and ``pr.py`` 11, and a child writing one accented character raises
``UnicodeDecodeError`` under ``LC_ALL=C PYTHONUTF8=0`` while decoding
cleanly in a utf-8 locale. It is left alone here on the ground that
#320's rule is about the files KSTRL WRITES, and a commit message is
written by an operator; the number is recorded so the next sweep starts
from a measurement rather than an estimate.

The walk that produces these inventories is
``tests/helpers/encodingwalk.py``; what it cannot see is named beside
the control that covers it in ``tests/test_encoding_walk.py``, rather
than listed here where a disclosure can rot without anything failing.
"""

from __future__ import annotations

from tests.helpers.astwalk import Sites, assert_census, assert_sites, package_sources
from tests.helpers.encodingwalk import package_scan, reported_sites, spells_a_token

# --------------------------------------------------------------------------
# LAYER 1: the census that enumerates no node type and no field name.
# --------------------------------------------------------------------------

#: Every expression in ``kstrl/`` that spells ``read_text`` or ``open``,
#: per module. A reader in ANY shape built on those two tokens has to
#: change this dict first, whatever it does with the result - which is
#: what layer 2 cannot promise, and the reason this exists.
#:
#: Not "no module can obtain a file's text without naming one of them":
#: ``configparser``, ``linecache`` and ``fileinput`` each decode with the
#: locale and name neither. None is live in ``kstrl/``, and the claim
#: this pin makes is about the two tokens rather than about every decode.
#:
#: Adding a row is not forbidden, it is the point: the diff that adds one
#: is where somebody says why new code opens a file and how it answers for
#: the encoding and the decode. The count is deliberately generous - it
#: includes binary opens, write opens and the ``fcntl`` lock files -
#: because a net that decides what to leave out is a net that can be wrong
#: about what it left out.
EXPECTED_READ_SPELLINGS: dict[str, int] = {
    "agents/codex.py": 1,
    # The "a+b" append open, moved here from evolution.py by #331.
    "appendio.py": 1,
    "agents/logging.py": 1,
    "atomicio.py": 1,
    "autonomy.py": 1,
    "autonomy_replay.py": 1,
    "calibration.py": 1,
    "cli.py": 1,
    "commandrun.py": 1,
    "decisions.py": 1,
    "decompose.py": 2,
    "events.py": 2,
    "evolution.py": 3,
    "factory.py": 6,
    "feature_cmd.py": 2,
    "feedforward.py": 8,
    "fixtures.py": 3,
    "inbox.py": 2,
    "init_cmd.py": 3,
    "init_wizard.py": 1,
    "intake_github.py": 1,
    "knowledge.py": 4,
    "licensing.py": 1,
    "loop.py": 2,
    "manifest.py": 1,
    "observability.py": 2,
    "parsers.py": 1,
    "pipeline.py": 3,
    "prd.py": 1,
    "proposals.py": 4,
    "security.py": 1,
    "serve.py": 4,
    "statedir.py": 1,
    "tui/embed.py": 1,
    "tui/runs.py": 2,
    "tui/session.py": 1,
    "tui/tail.py": 2,
    "verify.py": 4,
    "workqueue.py": 7,
}


class TestNoModuleReadsAFileWithoutAppearingHere:
    """Layer 1. A census, so a shape nobody thought of still moves it."""

    def test_the_spellings_are_the_ones_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=spells_a_token,
            expected=EXPECTED_READ_SPELLINGS,
            # One control per disjunct. A single one would be a scalar
            # proof over ``read_text or open`` and would stay green with
            # either half deleted, which #324 round 2 measured happening.
            control=("p.read_text()", "open(p)"),
            message="a module's file-read spellings moved.",
        )


# --------------------------------------------------------------------------
# LAYER 2: the walk, over the package.
# --------------------------------------------------------------------------

#: Every read layer 2 CLEARS, keyed by module and expression rather than
#: by line, so an edit above a read does not fail the pin while adding a
#: read still does. Deduplicated through a set, so this cannot count; the
#: counting job is layer 1's.
EXPECTED_CLEARED_READS: tuple[str, ...] = (
    "agents/codex.py last_msg_file.read_text(encoding='utf-8')",
    "agents/logging.py self._log_path.open('a', encoding='utf-8')",
    "autonomy.py path.read_text(encoding='utf-8')",
    "autonomy_replay.py csv.DictReader(handle, delimiter='\\t') on an open() handle",
    "autonomy_replay.py path.open(encoding='utf-8', newline='')",
    "calibration.py path.read_text(encoding='utf-8')",
    "cli.py open(prd_file, encoding='utf-8')",
    "commandrun.py open(path, 'a', buffering=1, encoding='utf-8')",
    "decisions.py path.read_text(encoding='utf-8')",
    "decompose.py (spec_path / name).read_text(encoding='utf-8')",
    "decompose.py spec_path.read_text(encoding='utf-8')",
    "events.py open(path, encoding='utf-8', errors='replace')",
    "events.py open(self.path, 'a', encoding='utf-8')",
    "evolution.py open(self.config.experiments_path, 'a', encoding='utf-8')",
    "evolution.py self.config.experiments_path.read_text(encoding='utf-8')",
    "factory.py fp.read(64) on an open() handle",
    "factory.py open(lock_path, 'a+', encoding='utf-8')",
    "factory.py open(lock_path, 'w', encoding='utf-8')",
    "factory.py open(log_path, 'a', buffering=1, encoding='utf-8', errors='replace')",
    "factory.py open(run_paths.engineer_log(component_id), 'a', buffering=1, encoding=",
    "factory.py path.read_text(encoding='utf-8')",
    "feature_cmd.py open(latest_path, 'w', encoding='utf-8')",
    "feature_cmd.py open(repair_path, 'w', encoding='utf-8')",
    "feedforward.py filepath.read_text(encoding='utf-8', errors='replace')",
    "feedforward.py path.read_text(encoding='utf-8')",
    "feedforward.py path.read_text(encoding='utf-8', errors='replace')",
    "feedforward.py py_file.read_text(encoding='utf-8', errors='replace')",
    "fixtures.py full_path.read_text(encoding='utf-8')",
    "fixtures.py open(prd_path, encoding='utf-8')",
    "fixtures.py snapshot_path.read_text(encoding='utf-8')",
    "inbox.py open(path, 'a', encoding='utf-8')",
    "init_cmd.py open(prd_file, encoding='utf-8')",
    "init_cmd.py path.open('a', encoding='utf-8')",
    "init_cmd.py path.read_text(encoding='utf-8')",
    "init_wizard.py toml_path.read_text(encoding='utf-8')",
    "intake_github.py self.path.read_text(encoding='utf-8')",
    "knowledge.py path.read_text(encoding='utf-8')",
    "knowledge.py prd_path.read_text(encoding='utf-8')",
    "knowledge.py target.open('a', encoding='utf-8')",
    "knowledge.py target.read_text(encoding='utf-8')",
    "licensing.py Path(match).read_text(encoding='utf-8', errors='replace')",
    "loop.py claude_md_path.read_text(encoding='utf-8')",
    "loop.py config.prompt_file.read_text(encoding='utf-8')",
    "manifest.py open(path, encoding='utf-8')",
    "observability.py for line in f on an open() handle",
    "observability.py open(path, encoding='utf-8')",
    "observability.py open(self._path, 'a', encoding='utf-8')",
    "parsers.py source_path.read_text(encoding='utf-8')",
    "pipeline.py open(path, 'a', buffering=1, encoding='utf-8')",
    "pipeline.py progress_path.read_text(encoding='utf-8')",
    "prd.py open(path, encoding='utf-8')",
    "proposals.py claude_md.read_text(encoding='utf-8')",
    "proposals.py open(path, 'a', encoding='utf-8')",
    "proposals.py path.read_text(encoding='utf-8')",
    "security.py prd_path.read_text(encoding='utf-8')",
    "serve.py manifest_path.read_text(encoding='utf-8')",
    "serve.py open(lock_path, 'a+', encoding='utf-8')",
    "serve.py self.path.read_text(encoding='utf-8')",
    "statedir.py open(lock_path, 'a+', encoding='utf-8')",
    "tui/embed.py open(run_paths.root / 'orchestrator.log', 'a', buffering=1, encoding='",
    "tui/runs.py open(lock_path, 'a+', encoding='utf-8')",
    "tui/session.py open(run_paths.root / 'orchestrator.log', 'a', buffering=1, encoding='",
    "verify.py (root / 'CLAUDE.md').read_text(encoding='utf-8')",
    "verify.py full.read_text(encoding='utf-8', errors='replace')",
    "verify.py progress_path.read_text(encoding='utf-8')",
    "workqueue.py meta_path.read_text(encoding='utf-8')",
    "workqueue.py open(lock_path, 'a+', encoding='utf-8')",
    "workqueue.py open(self.journal_path, 'a', encoding='utf-8')",
    "workqueue.py self.journal_path.read_text(encoding='utf-8')",
    "workqueue.py self.pause_path.read_text(encoding='utf-8')",
    "workqueue.py self.spec_path(item).read_text(encoding='utf-8')",
    "workqueue.py spec_source.read_text(encoding='utf-8')",
)


#: Every ``read_text``/``open`` call the walk decides is NOT its subject,
#: keyed by module and callee. Without this the walk has a silent fourth
#: bucket and ``encodingwalk.Scan``'s claim to partition is untested.
#:
#: The direction that matters here is the opposite of the cleared pin's.
#: A row ARRIVING means a call that used to answer to the encoding rule
#: now answers to nothing, which is how a widened ``STDLIB_READERS`` or a
#: newly resolvable receiver would remove a site from the guard in
#: silence.
#: Every read the walk can see and CANNOT PROVE anything about, keyed by
#: module and expression. Seven rows, and the number is the whole point.
#:
#: #344 took five review rounds, and rounds one to four each ended the
#: same way: the walk cleared shapes that escaped at run time, a fix
#: landed, and the next round found more. Eight, nine, nineteen, then
#: nine again. Round 4 changed the rule - a site the walk cannot PROVE
#: compliant is undecided, never cleared - and round 5 applied that rule
#: to the three layers the round-4 change never reached: handle scope,
#: consumer timing, and members that change what decoding happens.
#:
#: THE COST, measured rather than hoped: 85 cleared with 19 known holes
#: became 78 cleared with 7 rows a reader can work through.
#:
#: SIX OF THE SEVEN ARE THE SAME FACT. A handle handed to a callee is a
#: read this walk cannot PLACE: ``json.load(f)`` reads eagerly and
#: ``csv.reader(f)`` reads nothing at all, and no amount of AST tells
#: them apart. All six were checked by hand and all six are compliant -
#: three cover the decode, two have no handler so the caller answers, and
#: ``factory.py`` stores the handle in a dataclass whose own read is
#: tracked separately. The rows say "I cannot establish when this callee
#: reads", which is true, rather than "fine", which was not.
#:
#: THE SEVENTH is ``verify.py``'s fixture read inside ``with
#: tempfile.TemporaryDirectory(...)``, whose ``__exit__`` returns None
#: and swallows nothing - but the walk does not know that, and #320's own
#: defect can be written ``contextlib.suppress(OSError)``, which is a
#: ``with`` that swallows everything. The walk proves ``open`` and reads
#: ``suppress``; a third context manager is a row here, not a guess there.
#:
#: A row ARRIVING is not a failure of the code under test. It means new
#: code put a read somewhere nobody can say when it happens, or inside a
#: construct nobody has read the ``__exit__`` of, and somebody should.
EXPECTED_UNDECIDED: tuple[str, ...] = (
    "cli.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "factory.py fp (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "fixtures.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "init_cmd.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "manifest.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "prd.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "verify.py full_path.read_text(encoding='utf-8') (sits inside `with "
    "tempfile.TemporaryDirectory(prefix='kstr`, whose __exit__ this walk "
    "cannot prove does not swallow the decode)",
)


EXPECTED_DECIDED_OUT: tuple[str, ...] = (
    # The journal's "a+b" probe-and-append. Binary, so no codec applies;
    # #331 moved it from evolution.py to appendio.open_for_append, where
    # six appenders now share it.
    "appendio.py open",
    "atomicio.py os.open",
    "factory.py EvolutionJournal.open",
    "pipeline.py EvolutionJournal.open",
    "tui/runs.py open",
    "tui/tail.py open",
)


class TestEveryReadInThePackageIsAccountedFor:
    """Layer 2 over ``kstrl/``: nothing reported, a pinned undecided row,
    and a named inventory of everything cleared.

    The inventory is here because ``reported == ()`` on its own is not a
    control - CLAUDE.md guard-design rule 2 - since it is also what a
    walk that has been switched off returns.
    Pinning the CLEARED half means a walk that stops seeing a read fails
    this test with the row it lost, which is the direction #324 records
    eleven guards failing in silently.

    THREE PINS AND NO FOURTH BUCKET. Every read the walk sees is in
    exactly one of ``EXPECTED_CLEARED_READS``, ``EXPECTED_UNDECIDED`` and
    the reported set, and every call it decides is not a read is in
    ``EXPECTED_DECIDED_OUT``. The undecided pin is the one #344 round 4
    added, and it exists because the alternative was worse: for three
    rounds the walk answered "clear" where the honest answer was "I did
    not look inside that", and each round's review found more of it.
    """

    def test_nothing_is_reported_and_the_undecided_row_is_the_pinned_one(self) -> None:
        assert_sites(
            reported_sites(package_scan()).without_line_numbers(),
            seen=(),
            undecided=EXPECTED_UNDECIDED,
            message=(
                "a text read in kstrl/ does not name utf-8, or sits under a "
                "fail-closed OSError handler with nothing covering "
                "UnicodeDecodeError. See this module's docstring for the rule."
            ),
        )

    def test_the_cleared_reads_are_the_ones_pinned(self) -> None:
        found = Sites(package_scan().clear).without_line_numbers()
        assert found.seen == EXPECTED_CLEARED_READS, (
            "the set of text reads this walk can see moved. A row that "
            "VANISHED is the dangerous direction: the walk stopped seeing a "
            f"read rather than the read being deleted. Found: {list(found.seen)}"
        )

    def test_the_decided_out_calls_are_the_ones_pinned(self) -> None:
        found = Sites(package_scan().decided_out).without_line_numbers()
        assert found.seen == EXPECTED_DECIDED_OUT, (
            "the set of read_text/open calls this walk decides are not its "
            "subject moved. A row ARRIVING is the dangerous direction: a "
            "text reader has been decided out and now answers to nothing. "
            f"Found: {list(found.seen)}"
        )
