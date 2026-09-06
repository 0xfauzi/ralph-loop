"""The shared helper's other half: what it can SEE without deciding.

The seam with ``tests/test_astwalk.py`` next door: that file is about what
the walk can DECIDE, which is names and calls and the undecided half. This
one is about the parts that resolve nothing at all, which is the folding,
the census, scope attribution and the ``except`` ladder.

That split is not tidiness. The census is the CLOSED-BY-CONSTRUCTION layer
and the resolver is not, so they fail for different reasons and should say
so with different messages. #324's instance 10 is the argument: a ledger
of the places a walk gave up is closed only over the shapes that walk
already enumerates, and a producer whose shape was never enumerated walked
past a well-built one with 4737 tests green. A count of every node that
spells the name has no shape list to be incomplete.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers import astwalk
from tests.helpers.astwalk import corpus


def folded(source: str) -> list[str]:
    """Every value the folder can decide in one expression's tree."""
    tree = astwalk.parse(source)
    return [got for node in ast.walk(tree) if (got := astwalk.folded_str(node)) is not None]


# --- folding --------------------------------------------------------------


class TestFoldingDecidesWhatAReaderCanDecide:
    """The rule the journal guard left behind: a shape a reader can decide
    by looking at it is a shape the walk must decide, and anything else is
    disclosed."""

    def test_a_plain_literal(self) -> None:
        assert folded('x = "evolution.jsonl"\n') == ["evolution.jsonl"]

    def test_adjacent_literals_need_no_code(self) -> None:
        """CPython folds ``"a" "b"`` into one ``Constant`` at parse time.
        Asserted rather than assumed, because if that ever changed this
        would be a silent hole rather than a failure."""
        assert folded('x = "evolution" ".jsonl"\n') == ["evolution.jsonl"]

    def test_a_concatenation(self) -> None:
        """``"journal_" + "path"`` is how round 2 of #327 defeated three
        layers of a guard at once."""
        assert "journal_path" in folded('x = "journal_" + "path"\n')

    def test_an_fstring_does_not_fold_itself(self) -> None:
        """The measurement behind handling ``JoinedStr`` explicitly: an
        f-string is NOT collapsed at parse time the way adjacent literals
        are, so a walk that only checks ``ast.Constant`` misses it."""
        assert "evolution.jsonl" in folded("x = f\"evolution{'.jsonl'}\"\n")

    def test_a_conversion_is_not_decidable(self) -> None:
        """``!r`` changes the result, so the placeholder does not fold."""
        assert "ab" not in folded("x = f\"a{'b'!r}\"\n")

    def test_a_format_spec_is_not_decidable(self) -> None:
        assert "ab" not in folded("x = f\"a{'b':>5}\"\n")

    def test_subtraction_is_not_concatenation(self) -> None:
        """Sole killer of ``_folded_concat``'s ``ast.Add`` check: without
        it a ``%`` or ``*`` BinOp would fold as though it were a join."""
        assert folded('x = "a" % "b"\n') == ["a", "b"]

    def test_one_unknown_piece_makes_the_whole_thing_unknown(self) -> None:
        assert "ab" not in folded('x = "a" + b\n')

    def test_a_non_string_constant_is_not_a_string(self) -> None:
        assert folded("x = 3\n") == []


@pytest.mark.xfail(strict=True, raises=AssertionError)
@pytest.mark.parametrize(
    "source",
    [
        pytest.param('x = "".join(("evolution", ".jsonl"))', id="join"),
        pytest.param('x = "%s.jsonl" % stem', id="percent formatting"),
        pytest.param('x = "{}.jsonl".format(stem)', id="str.format"),
        pytest.param('x = "evolution.txt".replace("txt", "jsonl")', id="replace"),
    ],
)
def test_the_values_the_interpreter_has_to_build(source: str) -> None:
    """Folding's disclosed residual, pinned so the disclosure cannot rot.

    Every guard that folds inherits this list and says so. The bound on it
    is the same one the nets rest on: a value the interpreter builds still
    has to be built out of pieces, and the pieces are spelled.
    """
    astwalk.blind_spot(lambda text: "evolution.jsonl" in folded(text), source)


# --- the census -----------------------------------------------------------


def hits(source: str, sees: astwalk.Sees) -> int:
    return sum(1 for node in ast.walk(astwalk.parse(source)) if sees(node))


class TestSpellsEnumeratesNoFieldNames:
    """Each of these reaches a DIFFERENT string field, and none of them is
    named in the implementation.

    That is the property worth having. ``ast.iter_fields`` is asked for
    every string the node holds, so a slot a future CPython adds is
    covered by construction and a slot the author forgot cannot exist.
    """

    def test_a_bare_name(self) -> None:
        assert hits("getpgid(1)\n", astwalk.spells("getpgid")) == 1

    def test_an_attribute(self) -> None:
        assert hits("os.getpgid(1)\n", astwalk.spells("getpgid")) == 1

    def test_an_imported_name(self) -> None:
        assert hits("from os import getpgid\n", astwalk.spells("getpgid")) == 1

    def test_an_import_alias(self) -> None:
        """``alias.asname`` is a different field from ``alias.name``, and
        the local name is what later code spells."""
        assert hits("from os import kill as getpgid\n", astwalk.spells("getpgid")) == 1

    def test_a_parameter_name(self) -> None:
        assert hits("def f(getpgid):\n    pass\n", astwalk.spells("getpgid")) == 1

    def test_a_keyword_argument_name(self) -> None:
        assert hits("f(getpgid=1)\n", astwalk.spells("getpgid")) == 1

    def test_a_function_definition(self) -> None:
        assert hits("def getpgid():\n    pass\n", astwalk.spells("getpgid")) == 1

    def test_a_caught_exception_name(self) -> None:
        assert hits("try:\n    pass\nexcept OSError as getpgid:\n    pass\n", _pg()) == 1

    def test_a_global_declaration(self) -> None:
        """``Global.names`` is a LIST of strings, not a string. Sole killer
        of the list branch."""
        assert hits("def f():\n    global getpgid\n", astwalk.spells("getpgid")) == 1

    def test_a_string_literal(self) -> None:
        assert hits('x = "getpgid"\n', astwalk.spells("getpgid")) == 1

    def test_an_assembled_string_literal(self) -> None:
        """Folding and the field walk in one predicate: ``"get" + "pgid"``
        is two nodes, the ``BinOp`` that folds and nothing else."""
        assert hits('x = "get" + "pgid"\n', astwalk.spells("getpgid")) == 1


def _pg() -> astwalk.Sees:
    return astwalk.spells("getpgid")


class TestTheNetIsNotSimplyCountingEverything:
    """The false-positive side. Without these a predicate that returned
    True would pass every test above."""

    def test_a_longer_name_containing_the_token(self) -> None:
        assert hits("safe_getpgid(1)\n", _pg()) == 0

    def test_prose_that_mentions_it(self) -> None:
        """Equality, not substring, is what keeps a docstring out of the
        inventory and the inventory small enough to be obeyed."""
        assert hits('def f():\n    """Calls getpgid."""\n', _pg()) == 0

    def test_an_unrelated_module(self) -> None:
        assert hits("import os\nos.kill(1, 9)\n", _pg()) == 0


class TestFoldsToAndFoldsContaining:
    def test_folds_to_is_about_the_value_not_the_spelling(self) -> None:
        assert hits("def f(event_type):\n    pass\n", astwalk.folds_to("event_type")) == 0
        assert hits('x = "event_type"\n', astwalk.folds_to("event_type")) == 1

    def test_folds_containing_reaches_a_name_inside_a_path(self) -> None:
        """``root / ".kstrl/evolution.jsonl"`` folds to the whole relative
        path, and equality would miss it."""
        source = 'p = root / ".kstrl/evolution.jsonl"\n'
        assert hits(source, astwalk.folds_to("evolution.jsonl")) == 0
        assert hits(source, astwalk.folds_containing("evolution.jsonl")) == 1


#: The ``getpgid`` half of ``getpgid or rglob``, deleted. A module-level
#: def rather than a lambda because that is what the linter asks for, and
#: because two tests below need the SAME dead predicate.
def _only_the_rglob_half(node: ast.AST) -> bool:
    return astwalk.spells("rglob")(node)


class TestAssertCensusWillNotPinAnEmptyNet:
    """The mechanism that makes the right thing easy and the wrong thing
    hard. ``built == expected`` is exactly what a switched-off predicate
    returns, so the control is not optional, and it is exactly what an
    EMPTY CORPUS returns too, which is why the control alone is not
    enough."""

    def test_a_control_the_predicate_misses_fails_before_the_inventory(self) -> None:
        with pytest.raises(AssertionError, match="matched nothing in this control"):
            astwalk.assert_census(
                sources=astwalk.package_sources(),
                sees=astwalk.spells("no_such_identifier_anywhere"),
                expected={},
                control="x = 1\n",
                message="unused",
            )

    def test_a_live_control_lets_the_inventory_through(self) -> None:
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=astwalk.spells("getpgid"),
            expected={"procgroup.py": 2},
            control="import os\nos.getpgid(1)\n",
            message="the places kstrl derives a process group changed.",
        )

    def test_the_inventory_is_still_checked(self) -> None:
        with pytest.raises(AssertionError, match="the message"):
            astwalk.assert_census(
                sources=astwalk.package_sources(),
                sees=astwalk.spells("getpgid"),
                expected={},
                control="import os\nos.getpgid(1)\n",
                message="the message",
            )

    def test_an_empty_corpus_fails_even_though_the_control_fires(self) -> None:
        """The second control, and the reason there are two.

        The first one parses a string, so it says nothing about where the
        census was pointed. #324 round 2 repointed ``REPO_ROOT`` one
        directory too high, which makes ``package_sources()`` return
        ``[]``, and measured four assertions in this suite going green
        while looking at nothing.
        """
        with pytest.raises(AssertionError, match="empty corpus"):
            astwalk.assert_census(
                sources=[],
                sees=astwalk.spells("getpgid"),
                expected={},
                control="import os\nos.getpgid(1)\n",
                message="unused",
            )

    def test_the_empty_corpus_check_runs_before_the_predicate_control(self) -> None:
        """Order matters for the message: a caller who broke the corpus is
        told that, not that their predicate is dead."""
        with pytest.raises(AssertionError, match="empty corpus"):
            astwalk.assert_census(
                sources=[],
                sees=astwalk.spells("no_such_identifier_anywhere"),
                expected={},
                control="x = 1\n",
                message="unused",
            )

    def test_a_pin_that_is_empty_on_purpose_is_the_case_that_needed_this(
        self, tmp_path: Path
    ) -> None:
        """``tests/test_atomicio.py`` pins ``{}`` deliberately: a row there
        would be a hand-rolled temp file. That pin is green by
        construction against a broken corpus, and no ``expected`` value a
        caller could write would have caught it. Measured on a corpus of
        one real file, so the assertion is about the corpus and not about
        the emptiness of the pin.
        """
        source_file = tmp_path / "clean.py"
        source_file.write_text("x = 1\n", encoding="utf-8")

        astwalk.assert_census(
            sources=[source_file],
            sees=astwalk.spells("mkstemp"),
            expected={},
            control="import tempfile\ntempfile.mkstemp()\n",
            message="unused",
        )
        with pytest.raises(AssertionError, match="empty corpus"):
            astwalk.assert_census(
                sources=[],
                sees=astwalk.spells("mkstemp"),
                expected={},
                control="import tempfile\ntempfile.mkstemp()\n",
                message="unused",
            )

    def test_a_generator_corpus_is_read_once_and_still_counted(self) -> None:
        """``sources`` is typed ``Iterable``, so a caller may pass a
        generator. Materialised once, or the control would consume it and
        the census would count nothing."""
        astwalk.assert_census(
            sources=(path for path in astwalk.package_sources()),
            sees=astwalk.spells("getpgid"),
            expected={"procgroup.py": 2},
            control="import os\nos.getpgid(1)\n",
            message="unused",
        )

    def test_one_control_cannot_isolate_a_dead_half_of_a_disjunction(self) -> None:
        """The measurement that changed this signature.

        A single control is a SCALAR over the whole predicate, so a live
        disjunct keeps it green whichever half died. Here the ``getpgid``
        half is deleted and the control still fires, on the ``rglob``
        half, so the census sails past its own control and the only thing
        that objects is the inventory.
        """
        both = 'import os\nos.getpgid(1)\nroot.rglob("*.py")\n'
        half_dead = _only_the_rglob_half

        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=half_dead,
            expected={"feedforward.py": 3},
            control=both,
            message="unused",
        )

    def test_a_control_per_disjunct_names_the_half_that_went_quiet(self) -> None:
        """Same dead half, two controls. Now it fails HERE, and the
        message carries the control whose disjunct is gone rather than an
        inventory diff the reader has to interpret."""
        half_dead = _only_the_rglob_half

        with pytest.raises(AssertionError, match="getpgid"):
            astwalk.assert_census(
                sources=astwalk.package_sources(),
                sees=half_dead,
                expected={"feedforward.py": 3},
                control=("import os\nos.getpgid(1)\n", 'import feedforward\nroot.rglob("*.py")\n'),
                message="unused",
            )

    def test_a_live_disjunction_passes_every_one_of_its_controls(self) -> None:
        """The negative control for the two above: nothing is deleted, so
        both halves fire and the inventory is the union."""
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=lambda node: astwalk.spells("getpgid")(node) or astwalk.spells("rglob")(node),
            expected={"procgroup.py": 2, "feedforward.py": 3},
            control=("import os\nos.getpgid(1)\n", 'import feedforward\nroot.rglob("*.py")\n'),
            message="unused",
        )

    def test_a_single_control_is_still_a_sequence_of_one(self) -> None:
        """``str`` IS a ``Sequence[str]``, so the normalisation is not
        cosmetic: iterating a bare control string would prove the
        predicate against each CHARACTER of it."""
        with pytest.raises(AssertionError, match="matched nothing in this control"):
            astwalk.assert_census(
                sources=astwalk.package_sources(),
                sees=astwalk.spells("getpgid"),
                expected={},
                control="x = 1\n",
                message="unused",
            )

    def test_counting_or_operands_is_not_the_mechanism(self) -> None:
        """Why the one-control-per-disjunct contract has no static guard.

        The obvious one counts ``or`` operands in the ``sees=`` predicate
        and compares. It is FALSE, not weak: all four compound predicates
        in this suite report two operands, and their real branch counts
        are 2, 4, 3 and 2. ``_searches_the_machine``'s only ``or`` is
        ``folded_str(node) or ""``, a default rather than a disjunction,
        and its four-way choice is a set membership inside a helper. Such
        a guard would have demanded two controls from the site that needs
        four and passed it, which is a check that cannot fail for the
        reason it names.

        Pinned here rather than argued in prose, so that the day somebody
        finds a real mechanism this row is where they see that the
        counting one was already ruled out by measurement.
        """
        subjects = {
            "tests/test_journal_one_writer.py": "obtains_the_journal_path",
            "tests/test_process_scoping.py": "_searches_the_machine",
            "tests/test_prompt_enrollment_walk.py": "_spells_a_prompt_name",
        }
        counted = {}
        for rel, name in subjects.items():
            tree = astwalk.parsed(astwalk.REPO_ROOT / rel)
            fn = next(
                node
                for node in astwalk.all_nodes(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name
            )
            counted[name] = sum(
                len(node.values)
                for node in astwalk.all_nodes(fn)
                if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
            )

        assert counted == {
            "obtains_the_journal_path": 2,
            "_searches_the_machine": 2,
            "_spells_a_prompt_name": 2,
        }, counted

    def test_modules_with_no_hits_are_left_out(self) -> None:
        built = astwalk.census(astwalk.package_sources(), astwalk.spells("getpgid"))
        assert built == {"procgroup.py": 2}


# --- scope ----------------------------------------------------------------


class TestScopeAttribution:
    def test_a_helper_defined_in_a_try_is_not_credited_to_it(self) -> None:
        """The attribution #318 needed: a function DEFINED inside a
        ``try`` and called elsewhere does not run under that handler."""
        tree = astwalk.parse("try:\n    def f():\n        risky()\nexcept OSError:\n    pass\n")
        block = next(node for node in ast.walk(tree) if isinstance(node, ast.Try))
        names = [node.id for node in astwalk.own_nodes(block) if isinstance(node, ast.Name)]
        assert "risky" not in names

    def test_a_call_in_the_try_body_is_credited_to_it(self) -> None:
        """Without this the test above passes on an ``own_nodes`` that
        returns nothing at all."""
        tree = astwalk.parse("try:\n    risky()\nexcept OSError:\n    pass\n")
        block = next(node for node in ast.walk(tree) if isinstance(node, ast.Try))
        names = [node.id for node in astwalk.own_nodes(block) if isinstance(node, ast.Name)]
        assert "risky" in names

    def test_try_body_nodes_stops_at_a_def_the_body_only_defines(self) -> None:
        """The same boundary, applied to a LIST of statements. Two guards
        wanted it and wrote it separately before it was hoisted."""
        tree = astwalk.parse("try:\n    def f():\n        risky()\nexcept OSError:\n    pass\n")
        block = next(node for node in ast.walk(tree) if isinstance(node, ast.Try))
        names = [n.id for n in astwalk.try_body_nodes(block) if isinstance(n, ast.Name)]
        assert "risky" not in names

    def test_try_body_nodes_sees_a_call_in_the_body(self) -> None:
        """Without this the test above passes on a helper that returns
        nothing at all."""
        tree = astwalk.parse("try:\n    risky()\nexcept OSError:\n    pass\n")
        block = next(node for node in ast.walk(tree) if isinstance(node, ast.Try))
        names = [n.id for n in astwalk.try_body_nodes(block) if isinstance(n, ast.Name)]
        assert "risky" in names

    def test_try_body_nodes_excludes_the_handler_body(self) -> None:
        """The difference from ``own_nodes`` on the same ``try`` node,
        which yields the handlers too. A guard attributing a call to the
        handler that guards it must not find the call INSIDE that
        handler and call it guarded."""
        tree = astwalk.parse("try:\n    a()\nexcept OSError:\n    b()\n")
        block = next(node for node in ast.walk(tree) if isinstance(node, ast.Try))
        body = [n.id for n in astwalk.try_body_nodes(block) if isinstance(n, ast.Name)]
        whole = [n.id for n in astwalk.own_nodes(block) if isinstance(n, ast.Name)]
        assert body == ["a"]
        assert "b" in whole

    def test_two_closures_of_the_same_name_get_different_qualified_names(self) -> None:
        """One exemption row must be able to name ONE closure. A bare
        function name cannot, which is what an earlier exemption key in
        ``tests/test_tui_config_walk.py`` did before it was replaced."""
        source = (
            "def a():\n    def build():\n        pass\n"
            "\n\n"
            "def b():\n    def build():\n        pass\n"
        )
        names = [name for _node, name in astwalk.scopes(astwalk.parse(source))]
        assert "a.build" in names and "b.build" in names

    def test_a_method_is_qualified_by_its_class(self) -> None:
        source = "class C:\n    def load(self):\n        pass\n"
        names = [name for _node, name in astwalk.scopes(astwalk.parse(source))]
        assert names == ["<module>", "C.load"]

    def test_declared_in_resolves_through_the_class(self) -> None:
        """An exemption by function NAME gives a free pass to any method
        that shares it, which is what round 1 of #327 shipped."""
        source = (
            "class Journal:\n"
            "    def append_entries(self):\n"
            "        pass\n"
            "\n\n"
            "class Other:\n"
            "    def append_entries(self):\n"
            "        pass\n"
        )
        lines = astwalk.declared_in(astwalk.parse(source), "Journal", "append_entries")
        assert lines == {2, 3}

    def test_declared_in_answers_nothing_for_a_class_it_cannot_find(self) -> None:
        source = "class Journal:\n    def append_entries(self):\n        pass\n"
        assert astwalk.declared_in(astwalk.parse(source), "Nope", "append_entries") == set()


# --- the except ladder ----------------------------------------------------


def clauses(source: str, *, resolved: bool = True) -> list[astwalk.Clause]:
    """The ladder of the first ``try`` in ``source``.

    ``resolved=False`` is the fail-closed default a caller gets by leaving
    the table out, and it is a different answer for a dotted clause.
    """
    tree = astwalk.parse(source)
    block = next(node for node in ast.walk(tree) if isinstance(node, ast.Try))
    table = astwalk.bindings(tree) if resolved else None
    return astwalk.handler_clauses(block, table)


class TestTheExceptLadder:
    def test_the_order_is_the_source_order(self) -> None:
        """A broad clause above a narrow one makes the narrow one dead, so
        a guard that sorts cannot tell a correct ladder from a dead one."""
        source = "try:\n    pass\nexcept ValueError:\n    pass\nexcept Exception:\n    pass\n"
        assert [sorted(clause.names) for clause in clauses(source)] == [
            ["ValueError"],
            ["Exception"],
        ]

    def test_a_tuple_of_types_is_one_clause(self) -> None:
        source = "try:\n    pass\nexcept (ValueError, OSError):\n    pass\n"
        assert sorted(clauses(source)[0].names) == ["OSError", "ValueError"]

    def test_a_dotted_type_the_resolver_can_place_is_named_by_its_leaf(self) -> None:
        source = "import tomllib\ntry:\n    pass\nexcept tomllib.TOMLDecodeError:\n    pass\n"
        assert clauses(source)[0] == astwalk.Clause(
            frozenset({"TOMLDecodeError"}), True, 4, frozenset({"tomllib.TOMLDecodeError"})
        )

    def test_a_bare_name_the_module_imported_carries_its_origin(self) -> None:
        """What a guard whose rule is a project constant has to read.

        The name is a spelling; the origin is the identity. Round 2 of
        #324 measured a module-level ``SURFACE_REJECTIONS = (ValueError,)``
        clearing a config load in ``kstrl/tui/screens/`` on the spelling
        alone, with both TUI guard files green at 49 passed.
        """
        source = (
            "from kstrl.config_preflight import SURFACE_REJECTIONS\n"
            "try:\n    pass\nexcept SURFACE_REJECTIONS:\n    pass\n"
        )
        clause = clauses(source)[0]
        assert clause.names == frozenset({"SURFACE_REJECTIONS"})
        assert clause.origins == frozenset({"kstrl.config_preflight.SURFACE_REJECTIONS"})

    def test_a_bare_name_bound_to_a_tuple_has_a_name_and_no_origin(self) -> None:
        """The mutation itself. Same spelling, no import, so a rule stated
        in origins reports it and a rule stated in names clears on it."""
        source = (
            "SURFACE_REJECTIONS = (ValueError,)\n"
            "try:\n    pass\nexcept SURFACE_REJECTIONS:\n    pass\n"
        )
        clause = clauses(source)[0]
        assert clause.names == frozenset({"SURFACE_REJECTIONS"})
        assert clause.origins == frozenset()

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_rebound_builtin_is_a_disclosed_miss(self) -> None:
        """The residual the origins field does NOT close, stated here
        rather than implied away.

        A builtin has no import, so a guard naming ``Exception`` has to
        name it by spelling, and ``Exception = ValueError`` above the
        handler is a name this table records nowhere a caller can read:
        ``_bind_one`` puts an unfollowable target in a PRIVATE memo. No
        module in ``kstrl/`` binds either builtin name, measured, which is
        why this is disclosed rather than fixed with a public field whose
        only reader would be this row.
        """
        source = "Exception = ValueError\ntry:\n    pass\nexcept Exception:\n    pass\n"
        astwalk.blind_spot(lambda _: clauses(source)[0].origins != frozenset(), source)

    def test_a_dotted_type_the_resolver_cannot_place_is_not_its_leaf(self) -> None:
        """The narrowing round 2 of #324 caught, pinned.

        Reading a dotted clause's leaf as its name moved three shapes in
        ``tests/test_tui_config_walk.py`` from reported to CLEARED, and
        the pre-#324 walk reported all three: nothing in the module binds
        ``shim``, so ``shim.Exception`` is not the builtin and clearing a
        config load on it is #289 coming back.
        """
        source = "try:\n    pass\nexcept shim.Exception:\n    pass\n"
        assert clauses(source)[0] == astwalk.Clause(frozenset(), False, 3)

    def test_a_tuple_mixing_a_bare_name_with_an_unplaceable_one_is_undecided(self) -> None:
        source = "try:\n    pass\nexcept (ValueError, shim.Exception):\n    pass\n"
        assert clauses(source)[0] == astwalk.Clause(frozenset({"ValueError"}), False, 3)

    def test_leaving_the_table_out_resolves_nothing_dotted(self) -> None:
        """The default is fail-closed, so a caller who forgot the table
        reports a dotted clause rather than clearing on it."""
        source = "import tomllib\ntry:\n    pass\nexcept tomllib.TOMLDecodeError:\n    pass\n"
        assert clauses(source, resolved=False)[0] == astwalk.Clause(frozenset(), False, 4)

    def test_a_bare_except_is_baseexception_and_it_is_decided(self) -> None:
        """It is not the same thing as an undecidable handler, and a guard
        that forbids it needs the NAME rather than a flag."""
        clause = clauses("try:\n    pass\nexcept:\n    pass\n")[0]
        assert clause.names == frozenset({"BaseException"}) and clause.decided

    def test_a_handler_the_walk_cannot_name_says_so(self) -> None:
        """Sole killer of ``decided``. Without the flag this clause's
        empty name set reads exactly like "catches nothing", which is the
        skip direction in one field."""
        clause = clauses("try:\n    pass\nexcept ERRORS[0]:\n    pass\n")[0]
        assert clause.names == frozenset() and not clause.decided

    def test_a_partly_nameable_tuple_is_undecided(self) -> None:
        source = "try:\n    pass\nexcept (ValueError, ERRORS[0]):\n    pass\n"
        clause = clauses(source)[0]
        assert clause.names == frozenset({"ValueError"}) and not clause.decided


# --- the corpus -----------------------------------------------------------


class TestTheCorpusIsTheOneEveryGuardMeant:
    def test_the_package_is_where_the_guards_think_it_is(self) -> None:
        """Ten guards derived this path for themselves. If it ever
        resolves wrong every one of them globs nothing and passes green
        forever, which is the cheapest possible vacuous guard."""
        assert (astwalk.KSTRL_PACKAGE / "evolution.py").is_file()
        assert len(astwalk.package_sources()) > 100

    def test_the_test_suite_is_too(self) -> None:
        assert len(astwalk.test_sources()) > 100

    def test_an_empty_corpus_is_refused_at_the_chokepoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Where the emptiness check has to live, and why not only in
        ``assert_census``.

        Four assertions in this suite walk this list themselves and never
        reach the census: ``test_journal_one_writer``'s single-writer
        sweep, ``test_event_names_have_one_home``'s literal sweep and both
        prompt walks in ``test_prompt_enrollment_walk``. #324 round 2
        measured all four passing GREEN under a ``REPO_ROOT`` one
        directory too high, with only a sibling census in the same FILE
        going red, which is a property of the file rather than of the
        assertion. With the check here all four fail.
        """
        monkeypatch.setattr(corpus, "KSTRL_PACKAGE", tmp_path / "nothing")
        with pytest.raises(AssertionError, match="no modules found under kstrl/"):
            astwalk.package_sources()

        monkeypatch.setattr(corpus, "TESTS_DIR", tmp_path / "nothing")
        with pytest.raises(AssertionError, match="no modules found under tests/"):
            astwalk.test_sources()

    def test_a_guard_can_leave_its_own_file_out(self) -> None:
        """A guard that names the shapes it forbids in its own fixtures
        would otherwise scan itself."""
        own = Path(__file__)
        assert own.resolve() not in [p.resolve() for p in astwalk.test_sources(exclude=own)]

    def test_the_corpus_helper_is_not_collected_as_a_test(self) -> None:
        """A from-import of ``test_sources`` into a guard module makes pytest
        collect the helper as a test that passes. ``__test__ = False`` is
        the mechanism; this is its control, since deleting the line fails
        nothing else (#337 round 2)."""
        assert astwalk.test_sources.__test__ is False  # type: ignore[attr-defined]

    def test_a_label_distinguishes_two_files_of_the_same_basename(self) -> None:
        """Ten basenames occur twice in ``kstrl/``, and a message naming a
        file the reader cannot find is worse than no message."""
        assert astwalk.label(astwalk.KSTRL_PACKAGE / "decompose.py") == "decompose.py"
        nested = astwalk.KSTRL_PACKAGE / "tui" / "screens" / "decompose.py"
        assert astwalk.label(nested) == "tui/screens/decompose.py"

    def test_a_module_name_is_what_a_relative_import_resolves_against(self) -> None:
        assert astwalk.module_name(astwalk.KSTRL_PACKAGE / "tui" / "home.py") == "kstrl.tui.home"

    def test_a_package_init_names_the_package(self) -> None:
        """``from . import x`` inside a package's own ``__init__`` has to
        land on the package, not one level below it."""
        init = astwalk.KSTRL_PACKAGE / "tui" / "__init__.py"
        assert astwalk.module_name(init) == "kstrl.tui"

    def test_the_parse_cache_is_keyed_on_the_text(self) -> None:
        """Several guards rewrite one ``other.py`` more than once inside a
        single test, and a path-keyed cache would hand the second call the
        first snippet's tree."""
        assert astwalk.parse("x = 1\n") is astwalk.parse("x = 1\n")
        assert astwalk.parse("x = 1\n") is not astwalk.parse("x = 2\n")


class TestBlindSpotHasAFailingState:
    """#328 found a disclosed-miss test with no failing state at all:
    ``strict=False`` with no ``raises`` made an open hole, a closed hole
    and a resolver raising on entry all green."""

    def test_it_fails_when_the_walk_still_cannot_see(self) -> None:
        with pytest.raises(AssertionError, match="still cannot see this"):
            astwalk.blind_spot(lambda _text: False, "x = 1\n")

    def test_it_passes_when_the_walk_can(self) -> None:
        astwalk.blind_spot(lambda _text: True, "x = 1\n")
