"""Every append-mode open in ``kstrl/`` has one home, or a reason on file.

#312 found the torn-tail defect on the evolution journal and #331 found
six more appenders with the same one: a crash leaves a tail with no
newline, the next append concatenates onto it, and the tolerant reader
drops BOTH lines. The fix is ``kstrl/appendio.py``, and the thing that
keeps it a fix is that a NEW appender cannot quietly open a file in
append mode and start writing records into it.

TWO LAYERS, because one of them is a net and the other is a message.

LAYER 1, :func:`append_open_census`, counts every call in ``kstrl/``
that opens something in a mode containing ``"a"``, keyed by module,
callee and mode, and pins the answer. It is CLOSED BY CONSTRUCTION in
the sense #324 asks for: it inventories every place an append handle is
OBTAINED rather than keeping a ledger of the places the walk gave up, so
a new appender in any shape shows up as an unexplained census delta
whatever it does with the handle afterwards. An unfoldable mode is
COUNTED rather than skipped, so ``os.open(p, flags)`` cannot hide in the
gap between "not a literal" and "not an append".

LAYER 2, :func:`unrouted_append_opens`, attributes each of layer 1's
sites to its innermost SCOPE and demands a row in
:data:`ALLOWED_APPEND_OPENS`. It is not a nicer message for layer 1: it
is the half that says WHICH line and WHAT to do about it, and it keeps
being true when an unrelated edit moves a line number, which layer 1's
count does not care about and a line-keyed pin would fail on.

WHICH DIRECTION EACH LAYER IS WRONG IN, per CLAUDE.md's rule 3. Both
FLAG. Layer 1 over-matches on purpose: it treats an unfoldable mode as
an append and it pools ``open``'s aliases across the whole package
rather than per module, so a name bound to ``open`` in one module makes
a call to that name count in another. The cost of that is a row somebody
has to read once; the cost of the other direction is a hole. Nothing
here CLEARS a site on its own evidence. What clears a site is a row a
person wrote in the table below, which is why adding one is the point of
the guard rather than a way around it: the diff that adds a row is where
somebody says why new code opens a file for append and is not going
through ``appendio``.

WHAT THE REASON CHECKS DO AND DO NOT DO. Each ``Reason`` carries a
check that can FAIL a row whose stated reason has stopped being true. A
``LOCK_FILE`` scope that no longer spells ``flock`` fails; a
``TEXT_LOG`` scope that starts spelling ``dumps`` fails, because a scope
that serialises JSON and appends it is a record writer and records are
what this guard is about; a ``PADS_ITSELF`` scope with no leading
newline anywhere in it fails; a ``NOT_AN_APPEND`` row whose mode starts
folding to a string fails. None of them can clear a site that has no
row, and ``PADS_ITSELF``'s is the weakest of the four: four other scopes
in ``kstrl/`` today would also satisfy it, so it can catch the pad being
deleted and cannot prove the pad is the right one.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

import pytest

from tests.helpers.astwalk import (
    Sees,
    all_nodes,
    assert_census,
    blind_spot,
    census,
    folded_str,
    label,
    leaf_name,
    own_nodes,
    package_sources,
    parse,
    parsed,
    scopes,
)
from tests.test_journal_one_writer import mode_argument, open_aliases

#: Openers that are not ``open`` and take a mode of their own. Imported
#: shapes rather than resolved ones: ``leaf_name`` answers the last name
#: in the callee, so ``tempfile.NamedTemporaryFile`` and a bare
#: ``NamedTemporaryFile`` are the same row.
_OTHER_OPENERS = frozenset({"fdopen", "NamedTemporaryFile", "TemporaryFile"})

#: Receivers for which the mode is the SECOND positional argument.
#: ``os.open(path, flags)`` and ``io.open(path, mode)`` take the path
#: first; ``path.open(mode)`` does not.
_MODE_AT_ONE = frozenset({"builtins", "io", "os"})

#: The one home. A site whose innermost scope is this one is the module
#: every other appender is routed through, so it is exempt by identity
#: rather than by a row.
THE_ONE_HOME = "appendio.py:open_for_append"


def mode_of(node: ast.Call) -> ast.expr | None:
    """The mode argument of an open-like call, or None if it takes none.

    ``mode_argument`` from the one-writer guard answers the keyword form
    and one positional index; what this adds is WHICH index, which
    differs by receiver. An absent mode is not an append: ``open(p)``
    reads.
    """
    func = node.func
    index = 1
    if isinstance(func, ast.Attribute):
        index = 1 if ast.unparse(func.value).split(".")[-1] in _MODE_AT_ONE else 0
    return mode_argument(node, index)


def append_open_names(trees: Iterable[ast.Module]) -> frozenset[str]:
    """``open``, every name bound to it, and the other stdlib openers.

    Pooled across the whole corpus rather than resolved per module,
    which over-matches and is the direction a flagging guard is allowed
    to be wrong in. There are no aliases in ``kstrl/`` today; the shape
    is here because ``_o = open`` is one line and would otherwise walk
    straight past a census that only knows the word ``open``.
    """
    names = {"open"}
    for tree in trees:
        names |= open_aliases(list(ast.walk(tree)))
    return frozenset(names | _OTHER_OPENERS)


def opens_for_append(names: frozenset[str]) -> Sees:
    """Layer 1's predicate: does this ONE node obtain an append handle?

    Two disjuncts, and the second is the one that matters. A mode that
    FOLDS is an append when it contains ``"a"``. A mode that does not
    fold is counted anyway, because "the walk could not decide" and
    "not an append" are different answers and only one of them is safe
    to act on. Today that counts three sites, all pinned with a reason.
    """

    def sees(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call) or leaf_name(node.func) not in names:
            return False
        mode = mode_of(node)
        if mode is None:
            return False
        folded = folded_str(mode)
        return "a" in folded if folded is not None else True

    return sees


def census_key(source_file: Path, node: ast.AST) -> str:
    """``module: callee(mode)``, with no line number in it.

    The census keys on the module by default, which would say only "this
    module opens one more file for append". Naming the callee and the
    mode says which one. Deliberately NOT the line: an edit above a site
    would then fail this guard while changing nothing about it, which is
    how a pin becomes something to be silenced.
    """
    assert isinstance(node, ast.Call)
    mode = mode_of(node)
    return f"{label(source_file)}: {ast.unparse(node.func)}({ast.unparse(mode)})"


def append_open_census() -> dict[str, int]:
    """Layer 1's inventory over ``kstrl/``, for re-deriving it by RUNNING it.

    Public because a census is re-derived by executing the walk, never
    by reading the pinned literal next door: PR #341 recorded a lane
    that reported two identical pins as evidence a refactor moved
    nothing, while executing the guard at those revisions was red.
    """
    sources = package_sources()
    sees = opens_for_append(append_open_names(parsed(source) for source in sources))
    return census(sources, sees, key=census_key)


class Reason(StrEnum):
    """Why a site outside ``appendio`` is allowed to open in append mode.

    Closed, because an open vocabulary is how "it is fine" gets written
    down as a reason. Each member's check is described on the module
    docstring; ``NOT_AN_APPEND`` is the one that says layer 1 counted
    something that is not an append at all, which is a deliberate
    over-match rather than a defect.
    """

    LOCK_FILE = "lock file"
    TEXT_LOG = "line-oriented text log"
    PADS_ITSELF = "pads its own tail"
    NOT_AN_APPEND = "not an append: the mode does not fold"


#: Every append-mode open in ``kstrl/`` outside ``appendio``, and why it
#: is not routed through it. Keyed ``module.py:innermost scope``, so a
#: line move does not touch it.
#:
#: The LOCK_FILE rows open a file only to hold ``flock`` on it; nothing
#: is ever written to them, so a torn tail is not a state they have. The
#: TEXT_LOG rows append lines a person or ``tail`` reads, where a tear
#: merges two lines and no parser drops a record; that is a real cost
#: and a smaller one than the record loss this module exists for, and it
#: is the reason the check is "does not serialise JSON" rather than
#: something stronger. The PADS_ITSELF rows already write their own
#: leading newline. The NOT_AN_APPEND rows are layer 1 over-matching on
#: an unfoldable mode: two are ``EvolutionJournal.open(root_dir)``, a
#: classmethod that happens to be called ``open``, and one is
#: ``atomicio``'s ``os.open`` with ``O_EXCL`` flags, which creates.
ALLOWED_APPEND_OPENS: dict[str, tuple[Reason, str]] = {
    "factory.py:_acquire_run_lock": (Reason.LOCK_FILE, "the run lock"),
    "serve.py:factory_lock_held": (Reason.LOCK_FILE, "reads whether the run lock is held"),
    "serve.py:serve_lock": (Reason.LOCK_FILE, "the daemon's own lock"),
    "statedir.py:control_lock": (Reason.LOCK_FILE, "the control-directory lock"),
    "tui/runs.py:factory_lock_held": (Reason.LOCK_FILE, "the TUI's copy of the same read"),
    "workqueue.py:queue_lock": (Reason.LOCK_FILE, "the queue lock"),
    "agents/logging.py:LoggingAgent.run": (Reason.TEXT_LOG, "the agent transcript"),
    "commandrun.py:CommandRun.transcript_writer": (Reason.TEXT_LOG, "a command transcript"),
    "factory.py:_redirect_worker_output": (Reason.TEXT_LOG, "a worker's stdout and stderr"),
    "factory.py:_run_component": (Reason.TEXT_LOG, "the component's own log"),
    "pipeline.py:ComponentPipeline._phase_transcript": (Reason.TEXT_LOG, "a phase transcript"),
    "tui/embed.py:run_embedded": (Reason.TEXT_LOG, "the embedded run's log"),
    "tui/session.py:start_run_session": (Reason.TEXT_LOG, "the session log"),
    "init_cmd.py:_ensure_gitignore": (Reason.PADS_ITSELF, "computes its own separator"),
    "proposals.py:mark_applied": (Reason.PADS_ITSELF, "writes a leading newline"),
    "atomicio.py:_create_temp": (Reason.NOT_AN_APPEND, "os.open with O_WRONLY|O_CREAT|O_EXCL"),
    "factory.py:_run_factory_locked._record_contract_event": (
        Reason.NOT_AN_APPEND,
        "EvolutionJournal.open(root_dir), a classmethod called open",
    ),
    "pipeline.py:ComponentPipeline.journal_superseded_findings": (
        Reason.NOT_AN_APPEND,
        "EvolutionJournal.open(self.root_dir), the same classmethod",
    ),
}

#: Layer 1's pinned inventory. Nineteen sites in seventeen rows;
#: ``factory.py`` opens two different text logs with the same spelling,
#: which is why the values are counts and not a set.
#:
#: Was twenty-five at 568bca4. #331 routed seven of them through
#: ``appendio`` (``observability`` progress.jsonl, ``events``' JsonlSink,
#: the ``workqueue`` journal, the ``inbox``, ``knowledge``'s telemetry,
#: and both of ``evolution``'s: the journal and experiments.tsv) and
#: added one, ``appendio``'s own.
EXPECTED_APPEND_OPENS: dict[str, int] = {
    "agents/logging.py: self._log_path.open('a')": 1,
    "appendio.py: open('a+b')": 1,
    "atomicio.py: os.open(os.O_WRONLY | os.O_CREAT | os.O_EXCL)": 1,
    "commandrun.py: open('a')": 1,
    "factory.py: EvolutionJournal.open(root_dir)": 1,
    "factory.py: open('a')": 2,
    "factory.py: open('a+')": 1,
    "init_cmd.py: path.open('a')": 1,
    "pipeline.py: EvolutionJournal.open(self.root_dir)": 1,
    "pipeline.py: open('a')": 1,
    "proposals.py: open('a')": 1,
    "serve.py: open('a+')": 2,
    "statedir.py: open('a+')": 1,
    "tui/embed.py: open('a')": 1,
    "tui/runs.py: open('a+')": 1,
    "tui/session.py: open('a')": 1,
    "workqueue.py: open('a+')": 1,
}


def scope_of(tree: ast.Module) -> dict[int, str]:
    """Every node in a module mapped to the qualified name of its scope.

    ``own_nodes`` stops at a nested function, so a helper defined inside
    another one is credited to itself rather than to its enclosing
    scope.
    """
    owner: dict[int, str] = {}
    for node, qualified in scopes(tree):
        for child in own_nodes(node):
            owner[id(child)] = qualified
    return owner


def spells(nodes: Iterable[ast.AST], token: str) -> bool:
    """Does this scope name ``token``, as a bare name or an attribute?

    ``fcntl.flock`` and a bare ``flock`` both count, which is the point:
    the check is about what the scope DOES, and the import style is not
    part of that.
    """
    return any(
        (isinstance(node, ast.Name) and node.id == token)
        or (isinstance(node, ast.Attribute) and node.attr == token)
        for node in nodes
    )


def reason_still_holds(reason: Reason, body: list[ast.AST], node: ast.Call) -> str | None:
    """None if the row's stated reason is still true here, else why not.

    A negative check: it can fail a row, and it can never clear a site
    that has no row.
    """
    if reason is Reason.LOCK_FILE and not spells(body, "flock"):
        return "claims to be a lock file, but the scope no longer spells flock"
    if reason is Reason.TEXT_LOG and spells(body, "dumps"):
        return "claims to be a text log, but the scope serialises JSON now"
    if reason is Reason.PADS_ITSELF and not any(
        (folded_str(child) or "").startswith("\n") for child in body
    ):
        return "claims to pad its own tail, but nothing in the scope starts with a newline"
    if reason is Reason.NOT_AN_APPEND and folded_str(mode_of(node)) is not None:
        return "claims not to be an append, but its mode folds to a string now"
    return None


def unrouted_append_opens(source_file: Path, names: frozenset[str]) -> list[str]:
    """Layer 2: the sites in one module with no row, or a row gone stale."""
    tree = parsed(source_file)
    sees = opens_for_append(names)
    owner = scope_of(tree)
    bodies = {qualified: own_nodes(node) for node, qualified in scopes(tree)}
    found: list[str] = []
    for node in all_nodes(tree):
        if not sees(node):
            continue
        assert isinstance(node, ast.Call)
        scope = owner.get(id(node), "<module>")
        row = f"{label(source_file)}:{scope}"
        if row == THE_ONE_HOME:
            continue
        allowed = ALLOWED_APPEND_OPENS.get(row)
        if allowed is None:
            found.append(f"{row} (line {node.lineno}): {ast.unparse(node)[:80]} has no row")
            continue
        stale = reason_still_holds(allowed[0], bodies.get(scope, []), node)
        if stale is not None:
            found.append(f"{row} (line {node.lineno}): {stale}")
    return found


def append_opens_in(source: str) -> list[str]:
    """Layer 1's predicate over a snippet, for the anti-vacuity body.

    Names are pooled from the snippet itself, so an alias written in it
    is resolved the way one in ``kstrl/`` would be.
    """
    tree = parse(source)
    sees = opens_for_append(append_open_names([tree]))
    return [ast.unparse(node) for node in all_nodes(tree) if sees(node)]


class TestEveryAppendOpenHasOneHome:
    def test_the_set_of_append_mode_opens_is_pinned(self) -> None:
        """Layer 1, the net.

        A new appender has to obtain a handle in append mode, so it has
        to change this inventory whatever it does with the handle
        afterwards. Adding a row is not forbidden; it is where somebody
        says why the new file is not going through ``appendio``.
        """
        sources = package_sources()
        assert_census(
            sources=sources,
            sees=opens_for_append(append_open_names(parsed(src) for src in sources)),
            key=census_key,
            expected=EXPECTED_APPEND_OPENS,
            control=(
                # One per disjunct of the predicate, plus one per shape
                # of the mode argument, because a single control is a
                # scalar proof over the whole thing: with only the first
                # of these, deleting the keyword branch of `mode_of` left
                # the control passing and the inventory failing, which
                # makes the control read as a mechanism it is not.
                'open(p, "a")\n',
                'p.open(mode="a")\n',
                "os.open(p, flags)\n",
                'os.fdopen(fd, "a")\n',
                'tempfile.NamedTemporaryFile(mode="a")\n',
            ),
            message=(
                "The set of append-mode opens in kstrl/ changed. If this is a new "
                "writer of records, route it through kstrl.appendio.append_records: "
                "an unguarded append concatenates onto an unterminated tail and the "
                "tolerant reader then drops BOTH lines (#312, #331). If it is not, "
                "add it to EXPECTED_APPEND_OPENS and give it a row in "
                "ALLOWED_APPEND_OPENS with a reason."
            ),
        )

    def test_every_append_open_is_routed_or_has_a_reason(self) -> None:
        """Layer 2, the message: name the line and what to do about it."""
        sources = package_sources()
        names = append_open_names(parsed(src) for src in sources)
        offenders = [
            offender for source in sources for offender in unrouted_append_opens(source, names)
        ]

        assert offenders == [], (
            "An append-mode open outside kstrl/appendio.py with no row. Route it "
            "through kstrl.appendio.append_records, or add a row to "
            f"ALLOWED_APPEND_OPENS with a reason. Offenders: {offenders}"
        )

    def test_every_allowed_row_names_a_site_that_still_exists(self) -> None:
        """The table is not allowed to outlive its sites.

        A row for a site that has been routed through ``appendio`` or
        deleted is a standing permission nobody is watching, and the
        next file opened in that scope inherits it silently.

        It turns out to be a second detector for the walk going blind,
        which was not the reason it was written. Measured: switching off
        the unfoldable-mode disjunct, and making the mode always the
        second positional argument, each fail THIS assertion as well as
        their own, because a walk that stops seeing a site leaves the
        table holding a row for it. That is the useful direction, since
        the shape #324 records eleven times is a guard going quiet.
        """
        sources = package_sources()
        names = append_open_names(parsed(src) for src in sources)
        live = {
            f"{label(source)}:{scope_of(parsed(source)).get(id(node), '<module>')}"
            for source in sources
            for node in all_nodes(parsed(source))
            if opens_for_append(names)(node)
        }

        assert set(ALLOWED_APPEND_OPENS) <= live, (
            "ALLOWED_APPEND_OPENS has rows for scopes that no longer open anything "
            f"in append mode: {sorted(set(ALLOWED_APPEND_OPENS) - live)}. Delete them."
        )


class TestTheGuardDetects:
    """The net fired at source it is supposed to see, and stayed quiet at
    source it is not. A guard whose only assertion is that an inventory
    matches cannot notice its own detector being switched off, and #324
    records eleven instances of exactly that, every one in the direction
    of going blind."""

    @pytest.mark.parametrize(
        "source",
        [
            'open(p, "a")\n',
            'open(p, mode="a")\n',
            'open(p, "ab")\n',
            'open(p, "a+b")\n',
            'p.open("a")\n',
            'p.open(mode="a")\n',
            "os.open(p, flags)\n",
            'os.fdopen(fd, "a")\n',
            'tempfile.NamedTemporaryFile(mode="a")\n',
            '_o = open\n_o(p, "a")\n',
        ],
    )
    def test_an_append_open_is_seen(self, source: str) -> None:
        assert append_opens_in(source), f"the walk missed an append open: {source!r}"

    @pytest.mark.parametrize(
        "source",
        [
            'open(p, "r")\n',
            'open(p, "w")\n',
            'open(p, "rb")\n',
            "open(p)\n",
            'p.write_text("x")\n',
        ],
    )
    def test_a_read_or_a_truncating_write_is_not_seen(self, source: str) -> None:
        assert not append_opens_in(source), f"the walk over-matched: {source!r}"

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_an_append_reached_by_seeking_to_the_end_is_missed(self) -> None:
        """The disclosed limit, pinned so the disclosure cannot rot.

        ``"r+"`` opens for update without truncating, and a seek to the
        end turns it into an append. The mode contains no ``"a"``, so
        layer 1 does not count it and layer 2 never sees it. It is not
        merely unpinned: nothing in this suite would notice such a
        writer arriving.

        Not fixed by widening the mode test to ``"+"``: every ``"a+"``
        lock file above would stay counted while every ``"r+"`` reader
        that never seeks would join them, which trades a disclosed miss
        for undisclosed noise. The day somebody does widen it, this row
        XPASSes and ``strict=True`` makes that a failure, so the
        disclosure is edited in the same diff.
        """
        blind_spot(append_opens_in, 'h = open(p, "r+")\nh.seek(0, 2)\nh.write(line)\n')
