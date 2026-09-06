"""An enrolled event name is spelled once where a journal row is written or selected.

That one place is ``kstrl/evolution.py``. The scope of the claim is the
row, not the word: ``tests/`` holds 46 spellings of ``spec_issues`` that
are the architect's own JSON key, and both layers deliberately leave
them alone. Which corpus each layer walks is stated under TWO LAYERS
below, and each layer's claim is exactly the corpus it walks (#337).

``spec_issues`` used to be spelled twice: a constant in ``decompose``,
which writes the row, and a literal in ``evolution``, which selects on
it. The cost of that placement was not that the two disagreed - a
round-trip test made that loud - but that a reader added in
``evolution`` reaches for the nearest spelling, and the nearest spelling
was the literal.

Split out of ``tests/test_evolution.py`` on #336, and the seam is the one
``tests/test_journal_one_writer.py`` already documents about its own
split: that file is about what the journal DOES, measured against real
rows in a real file, and this one is a static guard over the source
trees with no journal in it at all. Different subject, different
failure message, different reason to fail. The 800-line ratchet could not
prompt the split, because it reports rather than fails once a file is
already over, and ``test_evolution.py`` was at 1407 lines.

TWO LAYERS, because one of them is a net and the other is a message.

LAYER 1, the predicate :func:`spells_an_enrolled_event` run through
``astwalk.census``, counts every expression in ``kstrl/`` whose folded
value IS an enrolled event name, per module. (:func:`event_name_spellings`
is one row of that same census, and exists so the shape controls next
door can ask about a single file; round 1 of review on #337 found it as
a third raw ``ast.walk``, certifying a copy of the executed path.) It
is the net: a row cannot be written or selected by a name the module
never spells, so a second spelling in any shape has to appear here
first, whatever it does with the string afterwards. It resolves nothing
and enumerates no node types, which is the point. #324 records that this
repo has about eleven AST guards each re-implementing that resolution and
each holed independently, and layer 2 below was the twelfth: round 1 of
review on #336 measured six ordinary shapes walking straight past it.
It stops at ``kstrl/`` deliberately (#337). Measured over ``tests/`` it
counts 46 spellings in 13 modules, 24 of them in ``test_decompose.py``
alone, where they are the architect's own JSON key inside a fixture
payload. A pinned dict over that would move on every fixture that adds
an architect payload, which is the guard that gets silenced
``_assignment_hits`` warns about.

LAYER 2, :func:`literal_event_names`, enumerates shapes and names the
offending line and its direction. It walks ``kstrl/`` AND ``tests/``
(#337): a test that spells the name for itself while asserting on
production behaviour is asserting partly on its own copy of it. It is
not redundant. Layer 1 can only say "this module's spelling count
moved", which is the wrong message when the answer is "you wrote a
journal row with a bare literal, import the constant". Layer 1 in turn
catches what layer 2 cannot, and after #336 that is a longer list than
layer 2's residual disclosure: a dispatch table keyed by the name, a
read behind a function boundary, a parameter default, ``setattr``, a
tuple-unpacked assignment, a function returning the bare name. All
measured.

What NEITHER layer sees is one thing, and it is disclosed on
``folded_str`` next door and pinned in
``tests/test_event_name_shapes.py``: a name the interpreter has to build.
``"{}_issues".format("spec")``, ``"".join(...)``, ``%``-formatting, a
run-time lookup. Constant folding answers ``None`` for all of them.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from pathlib import Path

from kstrl.evolution import JOURNAL_REPAIR_EVENT, SPEC_ISSUES_EVENT

# Reached through the module for readability rather than for safety:
# ``test_sources`` carries ``__test__ = False``, so a from-import of it
# would not be collected here either. Its docstring carries both
# measurements.
from tests.helpers import astwalk
from tests.helpers.astwalk import (
    REPO_ROOT,
    TESTS_DIR,
    Sees,
    all_nodes,
    assert_census,
    bound_names,
    census,
    folded_str,
    label,
    package_sources,
    parsed,
)

#: The journal event names that have been hoisted to a constant. What
#: the two layers below actually enforce about them, said as narrowly as
#: they enforce it, because a claim wider than its walk is #337 itself:
#: in ``kstrl/`` the name must not be spelled at all, in any shape, and
#: layer 1's census is what says so; in ``tests/`` only the shape
#: ATTACHED TO THE ``event_type`` COLUMN is forbidden, which is layer 2,
#: and the 46 spellings layer 1 counts over ``tests/`` are unenforced by
#: design. One exception, in ``tests/`` and deliberate: a pin on the
#: WIRE value, where the literal is the assertion rather than a second
#: spelling of it. There is exactly one, in ``test_decompose.py``, and
#: it is written against the bytes on disk so that layer 2 does not see
#: it. Five more names are still bare literals (``component_result`` nine times in
#: ``evolution.py`` alone, which is exactly what layer 2 counts there,
#: plus ``role_usage``, ``contract_result``, ``autonomy_transition`` and
#: ``findings_superseded``); converting them is a follow-up, and
#: enrolling each one here is what makes that follow-up enforceable
#: rather than merely intended.
ENROLLED_EVENT_CONSTANTS = {
    "SPEC_ISSUES_EVENT": SPEC_ISSUES_EVENT,
    "JOURNAL_REPAIR_EVENT": JOURNAL_REPAIR_EVENT,
}

#: The journal column every row is written under and selected on. A bare
#: literal of the COLUMN is not the defect; a bare literal of the event
#: NAME attached to it is.
EVENT_TYPE_KEY = "event_type"


# --- layer 1: the net -----------------------------------------------------


def spells_an_enrolled_event() -> Sees:
    """The net's predicate: does this ONE node fold to an enrolled name?

    Hoisted out of the walk so ``assert_census`` can run it against a
    control. Before #324 the net was a hand-rolled loop whose only
    assertion was that its inventory matched, and an inventory that
    matches is also what a predicate returning False always returns.
    """
    names = set(ENROLLED_EVENT_CONSTANTS.values())
    return lambda node: folded_str(node) in names


def event_name_spellings(source_file: Path) -> int:
    """How many expressions in one module fold to an enrolled event name.

    EQUALITY of the folded value, not substring, which is the opposite
    of the choice ``folded_filename_sites`` makes next door and for the
    opposite reason. That guard looks for a filename, and prose
    mentioning ``evolution.jsonl`` folds to a whole docstring that
    CONTAINS it. This one looks for a whole event name, and
    ``DECOMPOSE_PROMPT`` spells ``spec_issues`` several times in a
    prompt body that folds to thousands of characters. Substring would
    put every one of those in the inventory and make the guard something
    to be silenced; equality picks out the six places that actually hold
    the bare string.

    Counted per module so an unrelated edit does not fail it.

    ONE ROW OF ``census``, not a second implementation of it. Layer 1's
    assertion runs ``assert_census``, which counts through
    ``astwalk.census``; this function's only callers are the shape
    controls in ``tests/test_event_name_shapes.py``. Round 1 of review
    on #337 found it here as a third raw ``ast.walk``, so the controls
    were certifying a copy of the code the guard executes rather than
    the code itself. That is the same defect as the test-side copy of
    the selection rule this PR deletes, one file over.
    """
    key = label(source_file)
    return census([source_file], spells_an_enrolled_event()).get(key, 0)


#: Every place in ``kstrl/`` that spells an enrolled event name outright,
#: with how many times each module does it.
#:
#: Adding a row is not forbidden, it is the point: the diff that adds one
#: is where somebody says why new code spells the name for itself instead
#: of importing the constant. Three modules today, and only ONE of the
#: six occurrences is the journal's own vocabulary. The other five are
#: two other vocabularies that happen to share the word, which is why
#: layer 2 draws its line at the ``event_type`` column rather than at the
#: string, and why this layer is a pinned COUNT rather than an assertion
#: that the count is zero.
EXPECTED_EVENT_NAME_SPELLINGS: dict[str, int] = {
    # The architect's own JSON key, twice (validation and _parse_spec_issues),
    # plus the TUI artifact label emitted with ArtifactWritten.
    "decompose.py": 3,
    # The two declarations. This is the one home.
    "evolution.py": 2,
    # The TUI reading that artifact label back.
    "tui/screens/decompose.py": 1,
}


# --- layer 2: the message -------------------------------------------------


def literal_event_names(
    tree: ast.Module,
    event_name: str,
    aliases: frozenset[str] | None = None,
) -> list[str]:
    """Every place this module names ``event_name`` as a bare literal.

    Two directions, because a journal row is WRITTEN in one and SELECTED
    in the other, and a bare spelling in either is the defect. The shapes
    each direction covers are listed on :func:`_literal_event_writes`,
    :func:`_call_event_hits` and :func:`_literal_event_reads`, and every
    one of them has a positive control in
    ``tests/test_event_name_shapes.py``, because a matcher that quietly
    stops matching returns the same empty list as a clean tree.

    Round 1 of review on #336 is why that list is long, and why layer 1
    exists at all. This function looked at ``ast.Dict`` and at
    ``ast.Compare`` and at nothing else, and six ordinary shapes walked
    past it, all six measured rather than argued: ``dict(event_type=...)``,
    an item assignment after the dict was built, ``setdefault``,
    ``update(**kwargs)``, ``match``/``case`` and a walrus. Two were
    planted as real methods on ``EvolutionJournal`` and the whole fast
    tier stayed green. Every miss was in the skip direction, which is the
    defect class #324 exists to record. Enumerating node types does not
    converge by inspection, so coverage is layer 1's job now and this
    layer's job is to say which line and which direction.

    Deliberately NOT "the string appears in this file". ``spec_issues``
    is also the architect's own JSON key and the TUI's artifact label, so
    ``DECOMPOSE_PROMPT`` spells it in prose and ``_parse_spec_issues``
    reads ``data.get("spec_issues")`` off the agent's output. Those are
    other vocabularies that happen to share a word, and flagging them
    would make this layer something to be silenced rather than obeyed.
    ``TestTheOtherVocabularyIsLeftAlone`` feeds the matcher both kinds,
    so neither half is taken on trust. The line is the ``event_type``
    column: a spelling that never reaches it is somebody else's word, and
    layer 1 is what still counts those.

    ``aliases`` is a loop invariant the caller may hoist. It depends on
    the tree alone, so recomputing it per event name is pure waste:
    measured over ``kstrl/`` at 254 calls over 127 modules for two names,
    of which 127 were duplicates, and the guard test went 0.61 s to
    0.46 s with this and the node walk hoisted. It is O(names x modules)
    on a computation that does not depend on names, and this file commits
    to five more names. The traversal is still per name, but it goes
    through ``all_nodes``, which memoises it, so the second name reuses
    the first name's walk instead of repeating it.

    Measured as process CPU time over the 332-module corpus this layer
    walks, three sweeps each, parse cache warmed first so the number is
    about the walk: 1.26-1.27 s a sweep with a raw ``ast.walk``,
    0.75-0.76 s with the memo warm. The earlier wall-clock version of
    this number could not be reproduced without knowing what else the
    machine was running, which is why it is stated as CPU here.
    """
    resolved = event_type_aliases(tree) if aliases is None else aliases
    return [hit for node in all_nodes(tree) for hit in _hits_at(node, event_name, resolved)]


def _hits_at(node: ast.AST, event_name: str, aliases: frozenset[str]) -> list[str]:
    """One node, handed to whichever half of the guard owns its shape."""
    if isinstance(node, ast.Compare):
        return _literal_event_reads(node, event_name, aliases)
    if isinstance(node, ast.Match):
        return _match_event_reads(node, event_name, aliases)
    if isinstance(node, ast.MatchMapping):
        return _mapping_pattern_reads(node, event_name)
    if isinstance(node, ast.Call):
        return _call_event_hits(node, event_name, aliases)
    return _literal_event_writes(node, event_name)


# --- the writing half -----------------------------------------------------


def _literal_event_writes(node: ast.AST, event_name: str) -> list[str]:
    """Every shape that BINDS the event-type column to a bare name.

    ``{"event_type": <name>}``, the same pair inside a dict
    comprehension, a bare two-element pair (which is what
    ``dict([("event_type", <name>)])`` is made of), and an assignment
    whose TARGET is the column.

    ``{**base, "event_type": <name>}`` needs nothing extra: the explicit
    pair is still a pair, and the ``**`` entry is the ``None`` key this
    skips. Measured, not assumed.
    """
    if isinstance(node, ast.Dict):
        return _pair_hits(zip(node.keys, node.values, strict=True), event_name)
    if isinstance(node, ast.DictComp):
        return _pair_hits([(node.key, node.value)], event_name)
    if isinstance(node, ast.Tuple | ast.List) and len(node.elts) == 2:
        return _pair_hits([(node.elts[0], node.elts[1])], event_name)
    if isinstance(node, ast.Assign | ast.AnnAssign):
        return _assignment_hits(node, event_name)
    return []


def _pair_hits(
    pairs: Iterable[tuple[ast.expr | None, ast.expr]],
    event_name: str,
) -> list[str]:
    """``<the column>: <the name>`` pairs, whatever holds them.

    Only the KEY is optional, and only because ``ast.Dict`` spells
    ``{**base}`` as a ``None`` key. A value slot is never ``None``:
    measured over 1626 dict literals in ``kstrl/`` and ``tests/``, zero.
    """
    return [
        _write_hit(value)
        for key, value in pairs
        if key is not None and folded_str(key) == EVENT_TYPE_KEY and folded_str(value) == event_name
    ]


def _assignment_hits(node: ast.Assign | ast.AnnAssign, event_name: str) -> list[str]:
    """``row["event_type"] = <name>`` and ``self.event_type = <name>``.

    The column discipline, kept. An earlier version of this rule flagged
    ANY assignment whose value folded to an enrolled name, so that a
    second constant declaring the string was caught. That reached one
    shape and broke the line every other rule here holds: it would fail
    on ``SPEC_ISSUES_KEY = "spec_issues"``, which is the obvious next
    cleanup of the architect's own JSON vocabulary in ``decompose.py``,
    with a message telling the author to import the JOURNAL's constant.
    It would also fail on ``kstrl/events.py`` lines 343 and 646, which
    are the event STREAM's discriminator, the moment the enrolment
    follow-up this file commits to reaches ``contract_result`` and
    ``autonomy_transition``. A guard that fails the follow-up it exists
    to enable is a guard that gets silenced.

    A second constant is still caught, by layer 1, where "here is the row
    and here is the reason" is the normal outcome rather than a failure
    to argue with. That is what having a net underneath buys.
    """
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if node.value is None or folded_str(node.value) != event_name:
        return []
    return [_write_hit(node.value) for target in targets if _names_the_column(target)]


def _names_the_column(target: ast.expr) -> bool:
    """``x["event_type"]`` or ``x.event_type`` as an assignment target."""
    if isinstance(target, ast.Subscript):
        return folded_str(target.slice) == EVENT_TYPE_KEY
    return isinstance(target, ast.Attribute) and target.attr == EVENT_TYPE_KEY


def _call_event_hits(node: ast.Call, event_name: str, aliases: frozenset[str]) -> list[str]:
    """The three ways a CALL names the event with no dict literal in sight."""
    return (
        _keyword_hits(node, event_name)
        + _keyed_argument_hits(node, event_name)
        + _method_on_a_read_hits(node, event_name, aliases)
    )


def _keyword_hits(node: ast.Call, event_name: str) -> list[str]:
    """``event_type=<name>`` on any call.

    Any call rather than ``dict`` alone: the same keyword builds a
    ``TypedDict``, a dataclass or model, a ``functools.partial`` and an
    ``update(**kwargs)``, and singling out ``dict`` by name would leave
    every one of those free. Ruff is no help either. This repo selects
    ``E``, ``F``, ``I``, ``UP`` and ``B``, so ``C408`` is not on and
    ``dict(event_type=...)`` lints clean. ``F401`` catches the shape in
    ``decompose.py`` only by accident, because line 1031 is that module's
    sole USE of the imported constant, so replacing it leaves the import
    unused. Inside ``evolution.py``, where the constant is declared,
    there is no import to go unused and nothing fires at all.
    """
    return [
        _write_hit(keyword.value)
        for keyword in node.keywords
        if keyword.arg == EVENT_TYPE_KEY and folded_str(keyword.value) == event_name
    ]


def _keyed_argument_hits(node: ast.Call, event_name: str) -> list[str]:
    """``<method>("event_type", <name>)``: the value, or the default.

    ``setdefault`` writes the name. ``get`` and ``pop`` spell it as the
    fallback, which is the same bare literal deciding the same thing:
    ``entry.get("event_type", "component_result")`` is how four rows in
    ``evolution.py`` classify an entry that predates the column. Matched
    on the shape rather than on a list of method names, so a wrapper of
    any of them counts too.

    Folded EQUALITY on each remaining argument, not a deep walk, for the
    same reason ``_assignment_hits`` keeps its column: an argument that
    merely CONTAINS the name somewhere is a different claim.
    """
    if not _keys_on_the_column(node):
        return []
    return [_write_hit(arg) for arg in node.args[1:] if folded_str(arg) == event_name]


def _keys_on_the_column(node: ast.AST) -> bool:
    """``<anything>.<method>("event_type", ...)``.

    Shared by both halves, because one call shape does both jobs:
    ``get``, ``pop`` and ``setdefault`` name the column as their first
    argument and are reads, and the same three spell the event name as
    their second and are writes. ``operator.itemgetter("event_type")``
    is the read half with no second argument at all.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and bool(node.args)
        and folded_str(node.args[0]) == EVENT_TYPE_KEY
    )


def _method_on_a_read_hits(
    node: ast.Call,
    event_name: str,
    aliases: frozenset[str],
) -> list[str]:
    """``<a read>.startswith(<name>)``: a selection with no ``==`` in it.

    A deep walk on the argument here, unlike ``_keyed_argument_hits``,
    because the receiver has already established that this expression is
    about the column, so ``in ("spec_issues", "other")``-style nesting on
    the argument is the same claim rather than a wider one.
    """
    if not isinstance(node.func, ast.Attribute):
        return []
    if not _reads_event_type(node.func.value, aliases):
        return []
    return [_read_hit(arg) for arg in node.args if _names_the_event(arg, event_name)]


# --- the reading half -----------------------------------------------------


def _literal_event_reads(
    node: ast.Compare,
    event_name: str,
    aliases: frozenset[str],
) -> list[str]:
    """``entry.get("event_type") == <literal>`` and its ``!=`` and ``in``
    spellings, in either operand order."""
    operands = [node.left, *node.comparators]
    if not any(_reads_event_type(operand, aliases) for operand in operands):
        return []
    return [
        _read_hit(operand)
        for operand in operands
        if not _reads_event_type(operand, aliases) and _names_the_event(operand, event_name)
    ]


def _match_event_reads(node: ast.Match, event_name: str, aliases: frozenset[str]) -> list[str]:
    """``match entry.get("event_type"): case <name>:``, or-patterns and all.

    ``ast.Match`` holds no comparison operator at all, so a guard that
    walks ``ast.Compare`` cannot see a dispatch written this way even
    though it is the shape somebody adding a reader of several event
    types reaches for first.
    """
    if not _reads_event_type(node.subject, aliases):
        return []
    return [
        _read_hit(value)
        for case in node.cases
        for value in _pattern_literals(case.pattern)
        if folded_str(value) == event_name
    ]


def _mapping_pattern_reads(node: ast.MatchMapping, event_name: str) -> list[str]:
    """``case {"event_type": <name>}``: the column and the value, in the
    pattern itself.

    Needs no subject read, because the pattern names the column.
    """
    return [
        _read_hit(value)
        for key, pattern in zip(node.keys, node.patterns, strict=True)
        if folded_str(key) == EVENT_TYPE_KEY
        for value in _pattern_literals(pattern)
        if folded_str(value) == event_name
    ]


def _pattern_literals(pattern: ast.pattern) -> list[ast.expr]:
    """The literals a case pattern matches against, at any depth."""
    return [node.value for node in ast.walk(pattern) if isinstance(node, ast.MatchValue)]


def _reads_event_type(node: ast.expr, aliases: frozenset[str]) -> bool:
    """Does this expression read the event-type column, at any depth?

    Walks rather than testing the top node alone, so ``str(e["event_type"])``,
    the walrus ``(found := e.get("event_type"))`` and a local the module
    bound to a read all count.
    """
    return any(_is_event_type_read(child, aliases) for child in ast.walk(node))


def _is_event_type_read(node: ast.AST, aliases: frozenset[str]) -> bool:
    """One node: ``x["event_type"]``, any method call whose first argument
    is the column (``get``, ``pop``, ``setdefault``,
    ``operator.itemgetter``), or a name bound to one of those."""
    if isinstance(node, ast.Subscript):
        return folded_str(node.slice) == EVENT_TYPE_KEY
    if isinstance(node, ast.Name):
        return node.id in aliases
    return _keys_on_the_column(node)


def _names_the_event(operand: ast.expr, event_name: str) -> bool:
    """Does this operand spell the event name, at any depth?

    Walks children so ``in ("spec_issues", "component_result")`` is seen,
    and folds each one so an assembled spelling is not a way past.
    """
    return any(folded_str(child) == event_name for child in ast.walk(operand))


# --- names that stand in for a read ---------------------------------------


def event_type_aliases(tree: ast.Module) -> frozenset[str]:
    """Local names bound to a read of the column, to a fixed point.

    ``found = entry.get("event_type")`` on one line and ``found ==
    "spec_issues"`` on the next is the plainest reader there is, and
    neither half is an ``ast.Compare`` operand that reads the column. So
    is the walrus bound on one line and compared on a later one.
    Iterated to a fixed point the way ``journal_aliases`` does next door,
    so a chain of any length closes.

    Message quality rather than coverage, now that layer 1 exists: every
    shape this reaches spells the name outright, so the net counts it
    either way. What this buys is "line 42 compares event_type against
    'spec_issues'" instead of "decompose.py's spelling count moved".

    Collected per MODULE rather than per scope, which is the trade-off
    that function makes too: a name bound to a read anywhere means "the
    event type" everywhere in the file. That over-reports rather than
    under-reports, and over-reporting is the direction a guard is allowed
    to be wrong in.

    Measured over the corpus this function now runs on, by running it
    (#337 round 1: the earlier sentence gave the ``kstrl/`` number only,
    which was two thirds short of the walk). ``kstrl/``: 1 of 129
    modules binds any alias, 6 names, all in ``evolution.py``.
    ``tests/``: 4 of 204 modules, 10 names, in ``test_factory.py``,
    ``test_journal_torn_tail.py``, ``test_resume_ergonomics.py`` and
    ``test_usage_meter.py``. Three of those names are generic
    (``entry``, ``rows``, ``results``), so an unrelated ``entry ==
    "<an enrolled name>"`` later in ``test_factory.py`` would read here
    as an event-type comparison. That is the over-report direction, so
    it costs a false positive somebody reads rather than a miss.
    """
    nodes = all_nodes(tree)
    names: frozenset[str] = frozenset()
    while True:
        found = _alias_sweep(nodes, names)
        if found <= names:
            return names
        names |= found


def _alias_sweep(nodes: Sequence[ast.AST], names: frozenset[str]) -> frozenset[str]:
    """The names ONE pass binds to a read, given what is known so far."""
    found: set[str] = set()
    for node in nodes:
        targets, value = bound_names(node)
        if value is not None and _reads_event_type(value, names):
            found.update(targets)
    return frozenset(found)


# --- how a hit reads ------------------------------------------------------


def _write_hit(value: ast.expr) -> str:
    return f"line {value.lineno}: writes {ast.unparse(value)} as the event_type"


def _read_hit(operand: ast.expr) -> str:
    return f"line {operand.lineno}: compares event_type against {ast.unparse(operand)}"


class TestJournalEventNamesHaveOneHome:
    """#314 item 3, and #336 round 1 for the shape of the mechanism.

    Two assertions, one per layer, and neither is "this list is empty"
    alone, because an empty list is also what a switched-off detector
    returns. They reach that differently and the difference is worth
    stating, because round 1 of review on #337 found this docstring
    claiming both were pinned inventories when only one is. Layer 1 IS
    a pinned inventory, plus ``assert_census``'s own control. Layer 2 is
    an emptiness assertion, held up by two other things: the shape
    controls in ``tests/test_event_name_shapes.py``, where every one of
    the twenty matcher functions was stubbed to a constant and each stub
    noticed, and the corpus control on the assertion itself, which fails
    if the ``tests/`` half of the walk goes missing.
    """

    def test_nobody_spells_an_enrolled_event_name_for_themselves(self) -> None:
        """Layer 1, the net: pin every spelling of the name itself.

        A row cannot be written or selected by a name the module never
        spells, so NEW code that spells one has to change this dict,
        whatever shape it uses. That is why this layer resolves nothing
        and enumerates no node types: an exact count of folded values has
        no shape list to be incomplete.

        Measured, on the shapes layer 2 discloses that it misses: a
        dispatch table keyed by the name, a read behind a function
        boundary, a parameter default, ``setattr``, a tuple-unpacked
        assignment and a function returning the bare name are all caught
        HERE. What survives both layers is one thing, disclosed on
        ``folded_str`` and pinned in ``test_event_name_shapes.py``: a
        name the interpreter has to build.
        """
        assert_census(
            sources=package_sources(),
            sees=spells_an_enrolled_event(),
            expected=EXPECTED_EVENT_NAME_SPELLINGS,
            control='row = {"event_type": "spec_issues"}\n',
            message=(
                "The set of places that spell an enrolled journal event name "
                "changed. If this is a journal row being written or selected, "
                "import the constant evolution.py declares for it. If it is the "
                "architect's JSON key or the TUI's artifact label, which share the "
                "word, add the row with a reason."
            ),
        )

    def test_no_module_names_an_enrolled_event_as_a_literal(self) -> None:
        """Layer 2, the message: name the offending line and its direction.

        Over ``kstrl/`` AND ``tests/`` (#337); the module docstring
        carries why. Widening the corpus found six sites in the test
        tree, three writes and three reads, across ``test_decompose.py``,
        ``test_evolution.py`` and ``test_journal_torn_tail.py``. None was
        deliberate, so this layer needs no allowlist today.

        THE CORPUS IS THE CONTROL, and it needs one because ``found ==
        {}`` does not distinguish a clean tree from a walk that was
        narrowed back. Measured in round 1 of review on #337: deleting
        ``astwalk.test_sources(...)`` from the line below left this file
        at 2 passed and the full suite at 5564 passed, byte-identical
        counts, so the subject of this whole change was revertible with
        the suite green. ``ruff`` does report the unused import that
        revert leaves, but ``ruff check --fix`` is what the repo's own
        pre-commit hook runs, and it deletes the import and hands back a
        clean tree with the mechanism gone. So the assertion below
        counts what it was HANDED, from the same list it iterates, which
        is closed by construction rather than a threshold to maintain.

        ``exclude=Path(__file__)`` for the reason the two peers pass it
        (``tests/test_procgroup.py`` and ``tests/test_process_scoping.py``):
        a guard that names the shapes it forbids should not scan itself.
        DISCLOSED, because ``exclude`` takes one path and the exposure
        is a sibling: ``tests/test_event_name_shapes.py`` is in this
        corpus and its whole subject is the shapes this layer forbids.
        It is clean today only because every control there is a source
        STRING that ``folded_str`` folds whole. The first control
        somebody writes as real code instead of a snippet makes this
        assertion fail on its own positive-control file, and the only
        green route would be to weaken the control. That is the point at
        which this layer needs a named allowlist, and the same is true
        from the other direction the day ``component_result`` is
        enrolled: ``tests/test_decompose.py`` writes a deliberate legacy
        on-disk row with it.
        """
        sources = package_sources() + astwalk.test_sources(exclude=Path(__file__))
        walked_tests = [path for path in sources if path.is_relative_to(TESTS_DIR)]
        assert walked_tests, (
            f"layer 2 was handed {len(sources)} modules and none of them is under "
            f"{TESTS_DIR}, so this assertion covers kstrl/ only and the corpus #337 "
            "added is gone. Nothing else in the suite fails when that happens."
        )

        found: dict[str, list[str]] = {}
        for source_file in sources:
            tree = parsed(source_file)
            aliases = event_type_aliases(tree)
            hits = [
                f"{constant}: {hit}"
                for constant, event_name in sorted(ENROLLED_EVENT_CONSTANTS.items())
                for hit in literal_event_names(tree, event_name, aliases)
            ]
            if hits:
                found[label(source_file, REPO_ROOT)] = hits

        assert found == {}, (
            "A journal row is written or selected by a bare literal instead of the "
            f"constant evolution.py declares for it. Import the constant. Sites: {found}"
        )
