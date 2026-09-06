"""The shared resolver's own controls: every feature, and its sole killer.

#324's seventh instance is why this file exists rather than a docstring
claiming the resolver is correct. A guard was written, holed by a
reviewer, repaired, and holed again on the next rung inside the same PR.
The same pass then found two pieces of that repaired resolver that NO test
could tell apart from their absence: the fixed-point loop (all 127 files
converge in one pass, so collapsing it left everything green) and an
attribute-target branch reached 0 times across 147 inputs. A shared
resolver with those two properties would rot in one place instead of
eleven, which is worse, not better.

So the standing rule for ``tests/helpers/astwalk.py`` is that every
resolution feature has a test here that is its SOLE killer. Measured by
stubbing each feature in turn and counting the tests that go red; the
count recorded in each docstring is that measurement, not an intention.

The seam with ``tests/test_astwalk_nets.py`` next door: this file is about
what the walk can DECIDE (names, calls, the undecided half). That one is
about what it can SEE without deciding anything (folding, the census,
scope, handlers). Different subject, different failure message.
"""

from __future__ import annotations

import ast

import pytest

from tests.helpers import astwalk

TARGET = "os.getpgid"


def origin_of(source: str, expression: str = "pgid", module: str = "") -> str | None:
    """What the resolver says one module-level name refers to."""
    return astwalk.bindings(astwalk.parse(source), module=module).origins.get(expression)


def calls(source: str, targets: frozenset[str] = frozenset({TARGET})) -> astwalk.Sites:
    return astwalk.calls_to(astwalk.parse(source), targets)


# --- imports --------------------------------------------------------------


class TestEveryImportForm:
    """The forms #324 records five separate guards resolving in five
    different subsets, each subset holed where the others were strong."""

    def test_a_plain_import(self) -> None:
        assert origin_of("import os\n", "os") == "os"

    def test_a_renamed_import(self) -> None:
        """``import os as _o`` is #324's seventh instance in two
        characters: the guard that shipped had resolved a rebound CALLABLE
        and not a rebound MODULE."""
        assert origin_of("import os as _o\n", "_o") == "os"

    def test_a_dotted_import_binds_its_head(self) -> None:
        """``import os.path`` binds ``os``, not ``os.path``, which is what
        the interpreter does and what ``os.path.join`` then needs."""
        assert origin_of("import os.path\n", "os") == "os"

    def test_a_dotted_import_with_an_alias_binds_the_whole_path(self) -> None:
        assert origin_of("import os.path as _p\n", "_p") == "os.path"

    def test_a_from_import(self) -> None:
        assert origin_of("from os import getpgid\n", "getpgid") == "os.getpgid"

    def test_a_renamed_from_import(self) -> None:
        """``from subprocess import Popen as Spawn`` defeated the timeout
        audit twice, once after it had been repaired."""
        assert origin_of("from os import getpgid as _g\n", "_g") == "os.getpgid"

    def test_a_relative_import_resolves_against_the_module(self) -> None:
        """``ImportFrom.level``, dropped by three guards independently.

        The sole killer of ``_import_base``'s level branch: measured, with
        that branch removed every other test in this file still passes.
        """
        source = "from .config_report import build_config_report\n"
        got = origin_of(source, "build_config_report", module="kstrl.tui.home")
        assert got == "kstrl.tui.config_report.build_config_report"

    def test_a_bare_relative_import(self) -> None:
        """``from . import x`` has no ``module`` at all, and one guard's
        ``and node.module`` guard discarded the whole statement."""
        got = origin_of("from . import evolution\n", "evolution", module="kstrl.tui.home")
        assert got == "kstrl.tui.evolution"

    def test_a_relative_import_with_no_module_name_given(self) -> None:
        """Left without a module the origin is useless rather than wrong.

        A leading dot cannot collide with an absolute origin, so a guard
        matching ``os.getpgid`` cannot be fooled by ``from .os import``.
        """
        assert origin_of("from .os import getpgid\n", "getpgid") == ".os.getpgid"

    def test_a_relative_import_is_not_the_stdlib(self) -> None:
        """The false positive the dropped level produced, asserted."""
        assert calls("from .os import getpgid\npgid = getpgid(1)\n").seen == ()


# --- rebinds --------------------------------------------------------------


class TestEveryRebindForm:
    def test_a_module_rebind(self) -> None:
        assert origin_of("import os\n_o = os\n", "_o") == "os"

    def test_a_callable_rebind(self) -> None:
        assert origin_of("import os\n_g = os.getpgid\n", "_g") == TARGET

    def test_an_annotated_rebind(self) -> None:
        """#324's second logged instance: ``_p: object = tomllib`` made a
        TOML parse invisible to a guard that walked ``ast.Assign`` only,
        reported neither guarded nor unguarded. Sole killer of
        ``assignment_parts``'s ``AnnAssign`` branch.
        """
        assert origin_of("import os\n_o: object = os\n", "_o") == "os"

    def test_a_walrus_rebind(self) -> None:
        assert origin_of("import os\nif (_g := os.getpgid):\n    pass\n", "_g") == TARGET

    def test_an_attribute_target(self) -> None:
        """``self.lookup = os.getpgid``. Sole killer of the dotted-target
        half of ``assignment_parts``."""
        source = "import os\nclass C:\n    def __init__(self):\n        self.lookup = os.getpgid\n"
        assert origin_of(source, "self.lookup") == TARGET

    def test_a_class_body_binding(self) -> None:
        """``class G: lookup = os.getpgid`` binds a bare name as far as
        the AST is concerned, and every use of it spells ``G.lookup``.
        Sole killer of ``_class_body_names``."""
        source = "import os\n\n\nclass G:\n    lookup = os.getpgid\n\n\npgid = G.lookup(1)\n"
        assert calls(source).seen == ("8 os.getpgid",)

    def test_a_class_attribute_through_an_instance(self) -> None:
        """``G().lookup(pid)``: the receiver is not a name at all, so only
        ``Bindings.attributes`` can answer. Sole killer of that branch."""
        source = "import os\n\n\nclass G:\n    lookup = os.getpgid\n\n\npgid = G().lookup(1)\n"
        assert calls(source).seen == ("8 os.getpgid",)

    def test_a_module_held_in_a_class_attribute(self) -> None:
        """``class G: mod = os`` then ``G.mod.getpgid(pid)``.

        #324's subprocess lane measured this as a SILENT miss: no known
        prefix, and the outermost attribute is ``getpgid``, which is not
        an attribute anything bound, so it answered None and the leaf
        filter dropped it. The no-third-case rule in ``_classify_call``
        made it a reported undecided row, which was honest but still a
        miss; resolving the RECEIVER answers it outright. Sole killer of
        the recursive step in ``_through_attribute``.
        """
        source = "import os\n\n\nclass G:\n    mod = os\n\n\npgid = G.mod.getpgid(1)\n"
        found = calls(source)
        assert found.seen == ("8 os.getpgid",)
        assert found.undecided == ()

    def test_a_receiver_that_resolves_to_nothing_stays_undecided(self) -> None:
        """The bound on the step above. Resolving a receiver must not
        invent an origin for one nothing bound: ``other.lookup`` shares a
        leaf with a target, so it is a candidate, and it stays in the
        reported half rather than becoming a false ``seen``."""
        found = calls("import os\nlookup = os.getpgid\npgid = other.lookup(1)\n")
        assert found.seen == ()
        assert found.undecided == ("3 other.lookup",)

    def test_a_getattr_with_a_foldable_name(self) -> None:
        """``getattr(os, "getpgid")`` was a PINNED accepted miss in
        ``tests/test_safe_pgid.py``. Folding decides it, so the row moved
        into the caught set on #324."""
        assert calls('import os\npgid = getattr(os, "getpgid")(1)\n').seen == ("2 os.getpgid",)

    def test_a_getattr_whose_name_is_assembled(self) -> None:
        """And the assembly is not a way past it either."""
        source = 'import os\npgid = getattr(os, "get" + "pgid")(1)\n'
        assert calls(source).seen == ("2 os.getpgid",)


class TestTheFixedPoint:
    """The loop #328 measured as indistinguishable from its own absence.

    Every module in ``kstrl/`` converges in one pass, so on the real tree
    collapsing the loop changes nothing. These three are its sole killers.
    """

    def test_a_chain_of_length_two(self) -> None:
        assert origin_of("import os\n_a = os\n_b = _a\n", "_b") == "os"

    def test_a_chain_bound_in_reverse_order(self) -> None:
        """``_b = _a`` written ABOVE ``_a = os``. One pass in source order
        cannot close this; the loop is the only thing that can."""
        assert origin_of("import os\n\n\ndef f():\n    _b = _a\n\n\n_a = os\n", "_b") == "os"

    def test_a_self_referential_rebind_terminates(self) -> None:
        """``p = p.parent`` is why FIRST BINDING WINS.

        An earlier draft let a later binding overwrite an earlier one and
        this grew the origin string without bound: the probe ran for two
        minutes against ``kstrl/`` before it was killed. Sole killer of
        the ``if target in table.origins: return False`` line.
        """
        assert origin_of("import os\np = os\np = p.parent\n", "p") == "os"


# --- the undecided half ---------------------------------------------------


class TestTheSkipDirectionIsReported:
    """Every #324 instance was a matcher that could not decide and then
    reported clean. These are the shapes that now land in ``undecided``
    instead of vanishing."""

    def test_a_callee_with_no_name_at_all(self) -> None:
        """``TABLE[key](...)``. Undecided whatever the targets are,
        because there is no identifier to compare."""
        found = calls('T = {"g": None}\npgid = T["g"](1)\n')
        assert found.seen == ()
        assert found.undecided == ("2 T['g']",)

    def test_a_call_through_a_name_the_resolver_could_not_follow(self) -> None:
        """``importlib.import_module("os").getpgid`` was a pinned silent
        miss. It is a reported one now."""
        source = 'import importlib\npgid = importlib.import_module("os").getpgid(1)\n'
        found = calls(source)
        assert found.seen == ()
        assert found.undecided == ("2 importlib.import_module('os').getpgid",)

    def test_a_call_on_an_object_bound_to_something_opaque(self) -> None:
        """``proc = subprocess.Popen(...)`` then ``proc.wait()``: the
        clause that matters for a guard about the object rather than the
        module. ``proc`` is opaque, so the wait is undecided."""
        source = "import subprocess\nproc = subprocess.Popen(argv)\nproc.wait()\n"
        found = calls(source, frozenset({"subprocess.Popen.wait"}))
        assert found.undecided == ("3 proc.wait",)

    def test_a_bare_name_spelled_like_a_target_and_bound_nowhere(self) -> None:
        assert calls("pgid = getpgid(1)\n").undecided == ("1 getpgid",)

    def test_an_ordinary_call_is_not_a_candidate(self) -> None:
        """The false-positive side. Without this the partition could pass
        every test above by calling everything undecided."""
        source = "from pathlib import Path\np = Path('x')\np.parent.mkdir()\n', '.join(bits)\n"
        assert calls(source) == astwalk.Sites((), ())

    def test_a_decided_receiver_that_is_not_the_target(self) -> None:
        source = "import shutil\npgid = shutil.getpgid(1)\n"
        assert calls(source) == astwalk.Sites((), ())

    def test_a_dotted_callee_whose_head_was_never_bound(self) -> None:
        """Lane B of #324 measured this going neither seen nor undecided.

        An earlier draft asked "is the head opaque?" and let an UNKNOWN
        head fall through as decided, so a planted ``tempfile.mkstemp()``
        in a module with no ``import tempfile`` was invisible: this
        issue's own defect, inside its own fix. Unresolved is undecided,
        with no third case.
        """
        found = calls("fd, path = tempfile.mkstemp()\n", frozenset({"tempfile.mkstemp"}))
        assert found.seen == () and found.undecided == ("1 tempfile.mkstemp",)


class TestAssertSitesWillNotTakeHalfAnAnswer:
    """The mechanism, not the docstring: a guard cannot assert the clean
    half alone. To report nothing undecided it must write ``undecided=()``
    and that claim is checked."""

    def test_an_undecided_site_fails_even_when_seen_is_right(self) -> None:
        found = astwalk.Sites((), ("gateparse.py:111 TOOL_PARSERS[chosen]",))
        with pytest.raises(AssertionError, match="could not decide"):
            astwalk.assert_sites(found, seen=(), undecided=(), message="x")

    def test_both_halves_matching_passes(self) -> None:
        found = astwalk.Sites(("a",), ("b",))
        astwalk.assert_sites(found, seen=("a",), undecided=("b",), message="x")

    def test_the_seen_half_is_still_checked(self) -> None:
        with pytest.raises(AssertionError, match="the message"):
            astwalk.assert_sites(
                astwalk.Sites(("a",), ()), seen=(), undecided=(), message="the message"
            )


# --- the package, pinned --------------------------------------------------

#: Every call in ``kstrl/`` whose callee holds no identifier for the walk
#: to compare. This is the ONE inventory every guard built on
#: :func:`astwalk.calls_to` inherits, whatever it is looking for, because
#: a call through a table cannot be decided against any target set.
#:
#: Adding a row is not forbidden, it is the point: the diff that adds one
#: is where somebody says why a call is dispatched through a value.
#: Keyed by module and expression, not by line: rebasing this branch onto
#: a moved main failed this pin four times on line numbers alone, and none
#: of those diffs was about a dispatch table.
OPAQUE_CALLEES = (
    "gateparse.py TOOL_PARSERS[chosen]",
    "gateparse.py TOOL_PARSERS[name]",
    "tui/app.py initial_screens_for_kind(kind, observe_only=False)",
    "tui/app.py initial_screens_for_kind(kind, observe_only=True)",
)


def package_calls(targets: frozenset[str]) -> astwalk.Sites:
    """One sweep of ``kstrl/``, the way a migrated guard makes one."""
    found = astwalk.Sites()
    for source_file in astwalk.package_sources():
        found = found + astwalk.calls_to(
            astwalk.parsed(source_file),
            targets,
            where=astwalk.label(source_file),
            module=astwalk.module_name(source_file),
        )
    return found


class TestTheWalkAgainstTheRealPackage:
    """A resolver whose only tests are snippets is a resolver nobody has
    run. These three are against ``kstrl/`` itself."""

    def test_the_only_undecidable_callees_are_the_four_dispatch_tables(self) -> None:
        """Measured over 13,145 calls in ``kstrl/``: four.

        That number is what makes the undecided half something a guard can
        pin rather than a list it would be silenced for printing.
        """
        found = package_calls(frozenset({TARGET})).without_line_numbers()
        assert found.undecided == OPAQUE_CALLEES

    def test_the_one_pgid_lookup_is_found_where_procgroup_declares_it(self) -> None:
        """Anti-vacuity against the real tree: without this the test above
        would pass on a sweep that resolved nothing at all.

        KEYED BY MODULE, and the line number came off after a measurement
        rather than a preference. Merged against the open PR that rewrites
        ``kstrl/procgroup.py``, this row failed on ``:284`` becoming
        ``:270`` and nothing else: a textually clean merge, two branches
        independently green, one red tree, and the diff that caused it was
        not about this walk at all. The claim here is that there is exactly
        ONE such lookup and it lives in ``procgroup.py``. Where in the file
        is not the answer.
        """
        found = package_calls(frozenset({TARGET})).without_line_numbers()

        assert found.seen == ("procgroup.py os.getpgid",)

    def test_the_spawn_sweep_reproduces_the_timeout_audit_count(self) -> None:
        """68 spawn sites, the same number the private resolver in
        ``tests/test_timeout_enforcement.py`` found before it was
        migrated, plus ten expressions this walk will not pretend to have
        decided. Ten rows is the price of the rule, and it is what a guard
        pins instead of being silently narrower than it sounds."""
        spawns = frozenset(
            {
                "subprocess.run",
                "subprocess.Popen",
                "subprocess.call",
                "subprocess.check_output",
                "subprocess.check_call",
            }
        )
        found = package_calls(spawns)
        assert len(found.seen) == 68
        assert found.without_line_numbers().undecided == tuple(
            sorted(
                [
                    *OPAQUE_CALLEES,
                    # A Textual App bound to a local.
                    "cli.py app.run",
                    "tui/embed.py app.run",
                    "tui/home.py app.run",
                    # An agent adapter reached through a parameter or an
                    # attribute. Not a subprocess, and the walk cannot
                    # say so.
                    "agents/logging.py self._agent.run",
                    "decompose.py agent.run",
                    "loop.py agent.run",
                ]
            )
        )


# --- what it still cannot do ----------------------------------------------


#: Shapes the resolver provably does not resolve, each with the source
#: that proves it. Run under a STRICT xfail below, so the day one of them
#: closes the row fails and the docstrings claiming the limit have to be
#: edited in the same diff.
DISCLOSED_MISSES = [
    pytest.param(
        "import os\n(_a, _b) = (os.getpgid, 1)\npgid = _a(1)\n",
        id="tuple destructuring",
    ),
    pytest.param(
        "import os\n_t = [os.getpgid]\npgid = _t[0](1)\n",
        id="a callable out of a list",
    ),
    pytest.param(
        'import os\n_n = "getpgid"\npgid = getattr(os, _n)(1)\n',
        id="a getattr name the interpreter has to build",
    ),
    pytest.param(
        "import os\n\n\ndef call(fn):\n    return fn(1)\n\n\npgid = call(os.getpgid)\n",
        id="a target passed in as a parameter",
    ),
    # The three STORE targets `assignment_parts` does not enumerate,
    # found by round 2 of #324 and disclosed here rather than left as an
    # absence. `assignment_parts` names three node types, so a shape list
    # that can be incomplete sits under a package whose census layer
    # exists because shape lists are incomplete. Widening it to every
    # `ctx=ast.Store` target is the deeper fix and it is not this PR's:
    # it re-answers `bindings` for sixteen migrated guards, which is a
    # measurement each of them owes, not a side effect of a disclosure.
    pytest.param(
        "import os\nfor f in [os.getpgid]:\n    f(1)\n",
        id="a for-loop target",
    ),
    pytest.param(
        "import os\n[f(1) for f in [os.getpgid]]\n",
        id="a comprehension target",
    ),
    pytest.param(
        "import os\n\n\ndef h(f=os.getpgid):\n    return f(1)\n",
        id="a default argument",
    ),
]


@pytest.mark.xfail(strict=True, raises=AssertionError)
@pytest.mark.parametrize("source", DISCLOSED_MISSES)
def test_the_shapes_the_resolver_does_not_reach(source: str) -> None:
    """The disclosed residual, pinned so the disclosure cannot rot.

    Seven shapes in three groups. Three are dataflow through a container,
    which needs an interpreter rather than a walk. One is a call graph.
    Three are a binding target ``assignment_parts`` does not enumerate: a
    ``for``, a comprehension and a default argument. All seven are bounded
    by the same argument the guards rest on: the module still had to
    OBTAIN the target, so a census of the acquisition counts it even when
    this half cannot name the call.

    ``strict=True`` makes an XPASS a failure, so closing a hole forces the
    row out of this list; ``raises=AssertionError`` makes a resolver that
    CRASHES on the input fail too. #328 measured an open hole, a closed
    hole and a resolver raising on entry all passing green without both.
    """
    astwalk.blind_spot(lambda text: calls(text).seen, source)


class TestTheDisclosedMissesAreNotSilent:
    """Some of them are REPORTED misses, and the count is the point.

    A shape the walk cannot decide should land in ``undecided``, not
    vanish. Two of the seven above do. The other five read as calls on
    some other object, and this class is where that is stated rather than
    implied. The ratio is not comfortable and it is not hidden: the bound
    on all five is the census of the ACQUISITION, which counts a site
    whose call this half cannot name, and each of the two tests at the
    bottom shows that bound holding on its own input.
    """

    def test_a_callable_out_of_a_list_is_reported(self) -> None:
        found = calls("import os\n_t = [os.getpgid]\npgid = _t[0](1)\n")
        assert found.seen == () and found.undecided == ("3 _t[0]",)

    def test_an_unfoldable_getattr_is_reported(self) -> None:
        found = calls('import os\n_n = "getpgid"\npgid = getattr(os, _n)(1)\n')
        assert found.seen == () and found.undecided == ("3 getattr(os, _n)",)

    def test_a_tuple_unpack_is_silent_and_that_is_the_residual(self) -> None:
        """``assignment_parts`` answers ``None`` for a tuple target rather
        than guessing which element went where, so the name is not even
        opaque. This is the honest bottom of the walk."""
        assert calls("import os\n(_a, _b) = (os.getpgid, 1)\npgid = _a(1)\n") == astwalk.Sites()

    @pytest.mark.parametrize(
        "source",
        [
            "import os\nfor f in [os.getpgid]:\n    f(1)\n",
            "import os\n[f(1) for f in [os.getpgid]]\n",
            "import os\n\n\ndef h(f=os.getpgid):\n    return f(1)\n",
        ],
        ids=["for-loop target", "comprehension target", "default argument"],
    )
    def test_a_store_target_the_parts_do_not_enumerate_is_silent(self, source: str) -> None:
        """The three shapes round 2 of #324 measured, stated rather than
        left as an absence.

        ``assignment_parts`` reads ``Assign``, ``AnnAssign`` and the
        walrus. A ``for`` target, a comprehension target and a default
        argument are none of those, so the name is not even opaque and the
        call reads as somebody else's. The census bound holds: the source
        still spells the target.
        """
        assert calls(source) == astwalk.Sites()
        sees = astwalk.spells("getpgid")
        spelled = sum(1 for node in ast.walk(astwalk.parse(source)) if sees(node))
        assert spelled == 1, "the binding still spells the name, so the net counts it"

    def test_a_parameter_is_silent_and_the_census_is_the_bound(self) -> None:
        """A target handed to a helper reads as a call on some other
        object. The caller still had to spell ``os.getpgid`` to pass it,
        so the spelling census in the guard above it counts the site.
        """
        source = "import os\n\n\ndef call(fn):\n    return fn(1)\n\n\npgid = call(os.getpgid)\n"
        assert calls(source).seen == ()
        sees = astwalk.spells("getpgid")
        spelled = sum(1 for node in ast.walk(astwalk.parse(source)) if sees(node))
        assert spelled == 1, "the argument still spells the name, so the net counts it"


# --- the over-match, and the direction it is safe in ----------------------


def _resolved(source: str, expression: str) -> astwalk.Origin | None:
    """What the resolver makes of one expression, provenance and all."""
    tree = astwalk.parse(source)
    table = astwalk.bindings(tree)
    node = next(
        n.value
        for n in astwalk.all_nodes(tree)
        if isinstance(n, ast.Expr) and ast.unparse(n.value) == expression
    )
    return table.origin_of(node)


class TestTheBareNameOverMatchIsAGuess:
    """Round 3's blocking finding, and the three plants that measured it.

    ``Bindings.attributes`` is keyed on a bare attribute NAME, module
    wide, so after any class body or dotted assignment binds that name,
    every ``<anything>.<that name>`` in the file resolves. The class
    docstring used to call that "the direction a guard may be wrong in".
    It is, for a guard that resolves in order to FLAG: an over-report
    costs a reader a line. Three of the sixteen migrated guards resolve
    in order to CLEAR, and there the identical guess is the skip
    direction this whole issue is about.
    """

    def test_a_receiver_that_resolves_is_not_a_guess(self) -> None:
        """The receiver is tried FIRST now. ``G.mod`` is itself a guess,
        because ``mod`` is only known as a bare attribute name, and the
        step from it to ``getpgid`` adds no further doubt."""
        source = "import os\n\n\nclass G:\n    mod = os\n\n\nG.mod.getpgid\n"
        found = _resolved(source, "G.mod.getpgid")
        assert found == astwalk.Origin("os.getpgid", guessed=True)

        plain = "import os\nos.getpgid\n"
        assert _resolved(plain, "os.getpgid") == astwalk.Origin("os.getpgid")

    def test_an_unknown_receiver_falls_back_to_the_table_and_says_so(self) -> None:
        """The two pinned ``tests/test_safe_pgid.py`` rows need exactly
        this, and the AST cannot type ``x``."""
        source = "import os\n\n\nclass G:\n    lookup = os.getpgid\n\n\nx.lookup\n"
        assert _resolved(source, "x.lookup") == astwalk.Origin("os.getpgid", guessed=True)

    def test_a_guess_that_lands_in_the_target_set_is_still_a_hit(self) -> None:
        """What the asymmetry costs, measured: nothing. Both pinned rows
        are in-``wanted`` cases and both survive."""
        source = "import os\n\n\nclass G:\n    lookup = os.getpgid\n\n\npgid = x.lookup(1)\n"
        assert calls(source) == astwalk.Sites(("8 os.getpgid",), ())

    def test_a_guess_outside_the_target_set_is_undecided_not_silent(self) -> None:
        """The measurement that changed the rule.

        Four innocuous lines used to make a genuinely undecidable call
        disappear from BOTH halves: the guess resolved to something real,
        it was not the target, so the walk returned as though it had
        decided. On ``tests/test_toml_readers.py`` that took 1 failed to
        37 passed and the call left ``seen``, ``undecided`` and the
        guard's own three inventories.
        """
        source = "import os\n\n\nclass _Meter:\n    load = os.getloadavg\n\n\nmod.load(handle)\n"
        found = astwalk.calls_to(astwalk.parse(source), {"tomllib.load"})
        assert found == astwalk.Sites((), ("8 mod.load",))

    def test_a_guessed_clause_is_undecided_rather_than_named(self) -> None:
        """Plant C. Naming a clause is a decision, so a guess cannot make
        one: ``except other.Exc:`` would otherwise read as a bare
        ``Exception`` for a guard whose rule is "``Exception`` exactly"."""
        source = (
            "import builtins\n\n\nclass X:\n    Exc = builtins.Exception\n\n\n"
            "try:\n    pass\nexcept other.Exc:\n    pass\n"
        )
        tree = astwalk.parse(source)
        block = next(n for n in astwalk.all_nodes(tree) if isinstance(n, ast.Try))
        clause = astwalk.handler_clauses(block, astwalk.bindings(tree))[0]
        assert clause.names == frozenset() and not clause.decided
        assert clause.origins == frozenset()

    def test_a_bare_clause_name_is_resolved_through_its_import(self) -> None:
        """#318's shipped defect, and this branch's own finding turned on
        it. The old code returned ``part.id`` for a bare ``Name`` and said
        nothing could rebind ``Exception`` without an assignment. An
        import is not an assignment, and ``origins`` held the answer.
        """
        source = (
            "from json import JSONDecodeError as Exception\n"
            "try:\n    pass\nexcept Exception:\n    pass\n"
        )
        tree = astwalk.parse(source)
        block = next(n for n in astwalk.all_nodes(tree) if isinstance(n, ast.Try))
        clause = astwalk.handler_clauses(block, astwalk.bindings(tree))[0]
        assert clause.names == frozenset({"JSONDecodeError"})
        assert clause.origins == frozenset({"json.JSONDecodeError"})

    def test_an_unbound_builtin_clause_is_still_its_own_name(self) -> None:
        """The negative control for the row above: with nothing bound,
        ``Exception`` has to keep meaning the builtin or every ladder in
        the package becomes undecidable."""
        source = "try:\n    pass\nexcept Exception:\n    pass\n"
        tree = astwalk.parse(source)
        block = next(n for n in astwalk.all_nodes(tree) if isinstance(n, ast.Try))
        clause = astwalk.handler_clauses(block, astwalk.bindings(tree))[0]
        assert clause.names == frozenset({"Exception"}) and clause.decided
