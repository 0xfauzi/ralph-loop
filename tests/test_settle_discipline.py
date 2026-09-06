"""No test reads async-settled state after a fixed number of pauses.

``test_the_banners_do_not_overlap_each_other_or_the_topbar`` set
``display = True`` on two banners, counted two ``pilot.pause()`` calls
and read ``region.y``. It failed once on Linux CI with ``[0, 0, 1]``,
passed on rerun of the same commit, and passes locally: the banner had
not been laid out when the read happened. That is a class, not an
incident. Measured across this tree: 235 pilot waits, and of the tests
that own them 48 FAIL outright when their fixed pauses are deleted, 35
of those with ``NoMatches`` on a widget that had not mounted yet.

The blessed wait is ``tests/helpers/settle.py``: a predicate and a
wall-clock deadline. Wall clock rather than an iteration count, because
on a loaded runner ``for _ in range(40)`` measures nothing.

TWO LAYERS, covering different halves. One is closed by construction.
The other is a bespoke AST matcher of exactly the kind #324 exists to
delete. NEITHER SUBSUMES THE OTHER, and the temptation to rank them is
answered below with a mutation rather than an opinion, because an
earlier draft of this docstring ranked them and was measurably wrong.

LAYER 1, :func:`await_sites`, is the census of the resource. A coroutine
cannot settle without suspending, Python suspends in exactly four
spellings, and all four are SYNTAX: no import to rename, no name to
alias, nothing behind ``getattr``, nothing assembled at run time. So
this layer resolves NOTHING and enumerates no names, and a new wait in
any shape whatsoever has to move a row in
:data:`EXPECTED_AWAIT_SITES` first. That is the
``EXPECTED_JOURNAL_PATH_SITES`` shape from
``tests/test_journal_one_writer.py``: an inventory of every place the
resource is obtained, closed by construction, rather than a ledger of
the places a walk gave up, which is closed only over the shapes the walk
already looks at.

LAYER 2, :func:`settle_reads`, resolves. It says "line 341 reads
``banner.region.y`` after the fixed wait on line 339" where layer 1 can
only say "this file's count moved". It is hand-rolled, and #324 records
ten logged instances of such a matcher being holed, two of them twice,
so treat it as provisional.

``tests/helpers/astwalk/`` has since landed (#324, #342) and is where
this layer belongs, but the port is NOT mechanical and the reason is the
direction. astwalk's resolver deliberately over-reports:
``Bindings.attributes`` says so itself, and ``Origin`` carries
``guessed=True`` to record it, so after any ``class G: settled =
settle.settled`` an unrelated ``x.settled(...)`` resolves to the helper.
For a guard that FLAGS, that is free - it costs a false alarm. This
layer CLEARS: :func:`is_enrolled` decides a read is safe BECAUSE a
settle happened above it, so an over-match here silently blesses a read
that nothing settled. Porting ``is_enrolled`` onto a resolver whose
documented behaviour is to guess inverts the failure mode. Layer 3b next
door FLAGS and so may use the wide form; that difference is deliberate
and is why the two matchers twenty lines apart are not the same shape.

THE TWO ARE NOT ORDERED, and an earlier draft of this docstring said
they were - "layer 1 is the guard and layer 2 is a good error message".
That is measurably false, and it was false in the direction that gets
1200 lines deleted by the next reader acting on it. The mutation: add
``assert app.screen.region.y == 0`` immediately after an existing
``await pilot.press("f2")``. No new await, so layer 1's count for the
file does not move and layer 1 PASSES. Layer 2 names line 213. Run it
before believing this paragraph;
``test_a_read_added_under_an_existing_fixed_wait`` in
``tests/test_settle_shapes.py`` pins it.

So they cover different halves and neither subsumes the other. A new
WAIT of any shape has to move a row in :data:`EXPECTED_AWAIT_SITES`,
which layer 2 can be evaded on. A new READ under a wait that already
exists changes no count, which layer 1 cannot see at all.

WHAT LAYER 2 SEES. A read is any expression that reaches app-derived
state and whose value is consumed. No attribute vocabulary and no method
list, so it needs no widening as widgets grow. The taint that decides
"app-derived" is seeded from ``run_test``, from any awaited receiver and
from the first argument of a settle call, and propagates per MODULE
through bindings, through the siblings of a tainted target and into
arguments handed to a call on the app. The one thing it does enumerate
is the SETTLE side, and only in the safe direction: an await counts as
the blessed wait only when it is a plain call to a name imported from
``tests.helpers.settle``, so ``settle.settled(...)``, an alias or a
wrapper all fall back to "fixed" and FLAG. There is no skip direction on
the wait side to be wrong in.

WHAT LAYER 2 DOES NOT SEE, stated plainly rather than implied away:

- app state reached through a name this module never binds from the app.
  A fixture parameter, a module-level global assigned elsewhere. Needs a
  cross-module call graph. ``tests/helpers/astwalk/`` has landed and does
  NOT supply one: it resolves within a single module, so this miss
  survives the rebuild described above and is work in its own right.
- a read in a function that does no awaiting of its own. Usually that is
  correct attribution and the helper is where the fix belongs. Once in
  this tree it was a real miss, and it is worth naming:
  ``tests/test_tui_snapshots.py::test_component_detail_snapshot`` gives
  ``snap_compare`` a callback that opens a screen and pauses, and the
  plugin captures the SVG after the callback returns, so the read is in
  neither function. Found by reading the file, fixed by hand.
- control flow. The governing wait is the last one lexically above the
  read, so a loop's back edge is not modelled. That over-reports rather
  than under-reports, which is why the hand-rolled condition loops were
  converted rather than exempted.

Both of the first two are pinned in ``tests/test_settle_shapes.py``, so
this disclosure fails if it stops being true. Layer 1 counts the wait in
all three cases, which is the whole reason the net is the layer that
carries coverage.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The tree under test, located the way every other AST-walking guard in
#: this suite locates its subject (test_atomicio, test_journal_one_writer).
TEST_TREE = Path(__file__).resolve().parent

#: The module that owns the blessed wait. An await of a name imported
#: from here is a settle; everything else is a fixed wait.
SETTLE_MODULE = "tests.helpers.settle"


def label(source_file: Path) -> str:
    """How a file is named in a key and in a failure message."""
    return str(source_file.relative_to(TEST_TREE.parent))


def tree_sources() -> list[Path]:
    """Every module in ``tests/``, in a stable order."""
    return sorted(TEST_TREE.rglob("*.py"))


def parsed(source_file: Path) -> ast.Module:
    """The module's AST. Parsed on every call, deliberately.

    It was a module-level dict keyed on the file's text, which is the
    only key that works here - the positive controls next door rewrite
    one ``other.py`` several times inside a single test, so a path key
    would hand the second call the first snippet's tree. Measured, that
    cache held 137 MB of AST for the rest of the pytest session, because
    nothing clears it and the module stays imported: peak RSS 158.8 MB
    against 22.2 MB without it. It bought 0.37s of parsing across 170
    files, once. Memory retained for a whole session is the worse half
    of that trade, and shared mutable state between two tests is the
    worse half again.
    """
    return ast.parse(source_file.read_text(encoding="utf-8"))


# --- layer 1: the net -----------------------------------------------------


#: Every way Python suspends a coroutine, and there are four. All four
#: are SYNTAX: no import to rename, no name to alias, no ``__await__``
#: call a test can make instead, nothing to assemble at run time.
SUSPENSION_NODES = (ast.Await, ast.AsyncFor, ast.AsyncWith)


def await_sites(tree: ast.Module) -> int:
    """How many times this module yields to the event loop.

    Counting syntax is what makes this layer closed by construction over
    the wait half of the defect, which is the only half any static rule
    can close without a read vocabulary.

    Four spellings, not one. ``await`` was the whole list until
    ``test_a_wait_inside_a_comprehension_or_a_nested_function`` in
    ``tests/test_settle_shapes.py`` measured ``[x async for x in s]``
    walking past it: an async comprehension holds no ``ast.Await`` node
    at all, and neither does ``async for`` or ``async with``. Every one
    of them suspends, so every one of them is a chance for the app to
    settle.
    """
    # One walk, not two. The two node sets are disjoint, so an
    # ``elif`` counts exactly what two passes counted: measured over
    # the 170 files in this tree, 794,580 node visits become 397,290
    # and the layer costs 0.183s of CPU rather than 0.312s.
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, SUSPENSION_NODES):
            total += 1
        elif isinstance(node, ast.comprehension) and node.is_async:
            total += 1
    return total


#: Every file in ``tests/`` that yields to the event loop, and how often.
#:
#: Moving a row is not forbidden, it is the point: the diff that moves
#: one is where somebody says whether the new await gates a read. This
#: is a COUNT rather than an assertion that the count is zero, because
#: driving an app - ``pilot.press``, ``widget.mount``, ``run_test``
#: itself - is awaiting and is not the defect.
EXPECTED_AWAIT_SITES: dict[str, int] = {
    "tests/helpers/settle.py": 3,
    "tests/helpers/tui_screens.py": 5,
    "tests/test_config_guard_survey.py": 4,
    "tests/test_config_screen.py": 36,
    "tests/test_decompose_screens.py": 32,
    "tests/test_evolve_screen.py": 37,
    "tests/test_evolve_screen_encoding.py": 1,
    # Four ``async with evolve_screen(...)``, added by #333. The
    # decision each one needs: every read after them is of
    # ``#evolve-repairs``, and ``evolve_screen`` already waits on
    # ``tests/helpers/tui_screens.py``'s conditions, the last of which
    # is that ``EvolveScreen.on_mount`` has RETURNED. The repair line is
    # written inside the ``reload()`` that on_mount's last statement
    # calls, so it is drawn before any of these reads can run. No pause
    # is counted and no new settle predicate was needed.
    "tests/test_evolve_screen_repairs.py": 4,
    "tests/test_feature_run.py": 5,
    "tests/test_home_data.py": 4,
    "tests/test_home_shell.py": 52,
    "tests/test_inbox.py": 4,
    "tests/test_init_wizard.py": 47,
    "tests/test_launch_session.py": 57,
    "tests/test_settle_helper.py": 47,
    "tests/test_tui_app.py": 23,
    "tests/test_tui_config_guard.py": 13,
    "tests/test_tui_detail.py": 39,
    "tests/test_tui_embed.py": 52,
    "tests/test_tui_safe_mode.py": 61,
    "tests/test_tui_snapshots.py": 2,
    "tests/test_understand_run.py": 3,
}


# --- layer 2: the message -------------------------------------------------


def self_attr(node: ast.AST) -> str | None:
    """``self.x`` read as the module-scoped name ``x``, in one place.

    Three functions below need this rule and all three had their own
    copy of it, which is three chances for it to drift apart. The rule
    itself is in :func:`anchor_name`'s docstring: a pilot stashed on the
    test instance is how ``tests/test_init_wizard.py`` carries one
    between its helper and its tests.
    """
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self":
            return node.attr
    return None


def anchor_name(node: ast.expr) -> str | None:
    """The name an expression hangs off.

    ``app.screen.query_one(X)`` anchors on ``app``; ``self._pilot.pause``
    anchors on ``_pilot``, because a pilot stashed on the test instance
    is how ``tests/test_init_wizard.py`` carries one between its helper
    and its tests, and nine of its tests were measured to be racing.
    Attributes of ``self`` are treated as module-scoped names for that
    reason alone.
    """
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            attribute = self_attr(node)
            if attribute is not None:
                return attribute
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Subscript | ast.Await | ast.Starred):
            node = node.value
        else:
            return None


def enrolled_waits(tree: ast.Module) -> frozenset[str]:
    """Names this module imported from the settle helper.

    Narrow on purpose, and safe because it is narrow: a spelling this
    does not recognise is treated as a FIXED wait, so the guard
    over-reports rather than falling silent. That is the direction #324
    says a guard is allowed to be wrong in, and every one of the ten
    logged instances was wrong in the other one.
    """
    return frozenset(
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == SETTLE_MODULE
        for alias in node.names
    )


def touches_app(node: ast.AST, names: frozenset[str]) -> bool:
    """Does this expression reach app-derived state, at any depth?"""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in names:
            return True
        if self_attr(child) in names:
            return True
    return False


def bound_names(target: ast.expr) -> list[str]:
    """The plain names one binding target binds, ``self`` attributes and all.

    ``self`` itself is not one of them. Walking ``self._pilot`` reaches
    the ``Name`` node too, so returning it would taint ``self`` and with
    it EVERY attribute the test class hangs off ``self`` - the tmp_path
    it stashed, the fixture it cached - and the module would then report
    reads that have nothing to do with the app.
    ``test_a_self_attribute_that_is_not_the_app`` in
    ``tests/test_settle_shapes.py`` is that case, and it was written
    after stubbing :func:`self_attr` to a constant changed no control at
    all: the rule was there, and the over-approximation next to it was
    quietly doing the same work less precisely.
    """
    found = []
    for sub in ast.walk(target):
        if isinstance(sub, ast.Name):
            if sub.id != "self":
                found.append(sub.id)
        else:
            attribute = self_attr(sub)
            if attribute is not None:
                found.append(attribute)
    return found


def bindings(tree: ast.Module) -> list[tuple[list[ast.expr], ast.expr]]:
    """Every binding in the module, as (targets, value).

    Six forms, because a name can be bound by any of them and the taint
    below has to follow all six: assignment, annotated assignment,
    walrus, ``with ... as``, ``async with ... as``, and a loop target.
    """
    found: list[tuple[list[ast.expr], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            found.append((node.targets, node.value))
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            found.append(([node.target], node.value))
        elif isinstance(node, ast.With | ast.AsyncWith):
            found.extend(
                ([item.optional_vars], item.context_expr)
                for item in node.items
                if item.optional_vars is not None
            )
        elif isinstance(node, ast.For | ast.AsyncFor):
            found.append(([node.target], node.iter))
    return found


def taint_seeds(tree: ast.Module, helper: frozenset[str]) -> set[str]:
    """The names that are app-derived before any propagation.

    Three sources, all of them structural rather than a vocabulary:

    - the receiver of ``run_test``, which is the only way to get a
      pilot at all;
    - the receiver of any AWAITED method call, because awaiting on a
      thing is what makes it the thing that settles. This is what
      reaches ``self._pilot``;
    - the first argument of an enrolled settle call, because the helper
      takes the pilot. Without it a module whose every wait is already
      converted would lose its seed and go quiet, which is the shape
      ``tests/test_tui_config_guard.py`` has after the sweep.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found.update(_call_seeds(node, helper))
        elif isinstance(node, ast.Await):
            found.update(_await_seeds(node))
    return found


def _call_seeds(node: ast.Call, helper: frozenset[str]) -> list[str]:
    """``x.run_test(...)`` and ``settled(pilot, ...)``."""
    if isinstance(node.func, ast.Attribute) and node.func.attr == "run_test":
        name = anchor_name(node.func.value)
        return [name] if name else []
    if isinstance(node.func, ast.Name) and node.func.id in helper and node.args:
        name = anchor_name(node.args[0])
        return [name] if name else []
    return []


def _await_seeds(node: ast.Await) -> list[str]:
    """``await x.anything()``: you awaited on it, so it settles."""
    call = node.value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return []
    name = anchor_name(call.func.value)
    return [name] if name else []


def app_derived_names(tree: ast.Module, helper: frozenset[str]) -> frozenset[str]:
    """Every name in the module that stands for app-derived state.

    Iterated to a fixed point, and collected per MODULE rather than per
    scope, the way ``event_type_aliases`` is next door and for the same
    reason: over-reporting is the direction a guard is allowed to be
    wrong in. A name bound from the app anywhere in the file means "the
    app" everywhere in it.

    Propagates three ways. Through a binding whose VALUE reaches the
    app. Through the SIBLINGS of a tainted target, because
    ``async with evolve_screen(p) as (screen, pilot)`` binds both halves
    from one expression and only the pilot is seeded. And into any name
    handed as an argument to a call ON the app, because ``await
    app.screen.mount(probe)`` puts ``probe`` into the app's graph, and
    the next line reads ``probe.value``.

    A fourth way was written and then measured away: a one-hop rule for
    ``app, screen = await self._run_wizard(tmp_path)``, where the call
    mentions neither the app nor a pilot. Stubbing it to a constant left
    every control in ``tests/test_settle_shapes.py`` green and left all
    137 reads in ``tests/`` unchanged, because the module scope already
    covers it: a function that RETURNS app state either built the app in
    this module, which seeds the name, or was handed it by a caller
    whose own binding therefore mentions it. An unexercised branch in a
    guard is where the next hole goes, so it was deleted rather than
    kept for symmetry.

    What it does NOT reach, stated rather than implied: app state
    obtained from another module through a name this file never binds
    from the app - a fixture parameter, a module-level global assigned
    elsewhere. Layer 1 still counts the await that would have to precede
    the read, and
    ``test_a_pilot_arriving_from_another_module_is_missed`` asserts this
    miss, so the disclosure fails if it stops being true.
    """
    names = frozenset(taint_seeds(tree, helper))
    if not names:
        # 152 of this tree's 170 modules never bind anything from an
        # app, and both sweeps of the empty set are empty by
        # construction, so the fixed point below can only return it
        # unchanged. Leaving before ``bindings`` measured layer 2 at
        # 0.566s of CPU against 1.183s, with the finding set identical.
        return names
    binds = bindings(tree)
    while True:
        grown = names | _binding_sweep(binds, names) | _argument_sweep(tree, names)
        if grown <= names:
            return names
        names = grown


def _binding_sweep(
    binds: list[tuple[list[ast.expr], ast.expr]],
    names: frozenset[str],
) -> frozenset[str]:
    """One pass over the bindings, given what is known so far."""
    found: set[str] = set()
    for targets, value in binds:
        group = [name for target in targets for name in bound_names(target)]
        if touches_app(value, names) or any(name in names for name in group):
            found.update(group)
    return frozenset(found)


def _argument_sweep(tree: ast.Module, names: frozenset[str]) -> frozenset[str]:
    """Names handed to a call ON the app join the app's graph."""
    return frozenset(
        argument.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and touches_app(node.func, names)
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
        if isinstance(argument, ast.Name)
    )


def parent_map(tree: ast.Module) -> dict[int, ast.AST]:
    """Child id -> parent, for the walks that have to look upward."""
    return {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def own_awaits(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[int, ast.AST],
) -> list[ast.Await]:
    """The awaits this function makes itself, not those of a nested one."""
    found = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Await) and enclosing_function(node, parents) is fn
    ]
    return sorted(found, key=lambda node: node.lineno)


def enclosing_function(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST | None:
    """The innermost function a node sits in."""
    current = parents.get(id(node))
    while current is not None and not isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
        current = parents.get(id(current))
    return current


def is_enrolled(node: ast.Await, helper: frozenset[str]) -> bool:
    """Is this await the blessed wait?

    A plain call to an imported name and nothing else. Anything less
    recognisable is a fixed wait, which flags. Deliberately not widened
    to ``settle.settled(...)``: widening the shapes a guard ACCEPTS is
    the one direction where being wrong hides a defect.
    """
    call = node.value
    return isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in helper


def inside_enrolled_wait(
    node: ast.AST,
    parents: dict[int, ast.AST],
    helper: frozenset[str],
) -> bool:
    """Is this read part of a settle call's own arguments?

    ``await settled(pilot, lambda: app.screen.query(sel), ...)`` reads
    the app inside the wait that exists to make that read safe.
    Exempting the ENROLLED await only, never any await: a read passed to
    ``pilot.press(...)`` really does happen after the previous wait.
    """
    current = parents.get(id(node))
    while current is not None and not isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
        if isinstance(current, ast.Await) and is_enrolled(current, helper):
            return True
        current = parents.get(id(current))
    return False


def value_is_consumed(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Is this expression's value used, or discarded as a statement?

    The line between a READ and a DRIVE, and it needs no vocabulary.
    ``app.push_screen(Panel())`` and ``await pilot.press("f2")`` are
    statements whose value nobody wants: they change the app rather than
    ask it anything, and a pause after one is not yet a defect.
    ``rows = [banner.region.y]`` consumes a value that async settling
    decides.
    """
    parent = parents.get(id(node))
    if isinstance(parent, ast.Expr):
        return False
    if isinstance(parent, ast.Await):
        return value_is_consumed(parent, parents)
    return True


def is_the_callee(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Is this expression the thing being CALLED, rather than a value?

    ``app.push_screen`` in ``app.push_screen(Panel())`` and
    ``self._pilot`` in ``await self._pilot.pause(0.2)`` both reach the
    app and both have their value consumed, by the call. Neither is a
    read of anything settling decides; the call is the drive, and
    naming its receiver would report every drive in the tree. Measured:
    without this, ``test_a_drive_after_a_pause_is_not_a_read`` and
    ``test_a_pilot_stashed_on_the_test_instance`` both fail.

    Walks the whole ``func`` chain rather than testing the parent, so
    the receiver deep inside ``a.b.c()`` is covered too. A node reached
    as an ARGUMENT stops the walk, which is what keeps
    ``await pilot.press(app.screen.focused.id)`` reported.
    """
    current = node
    parent = parents.get(id(current))
    while isinstance(parent, ast.Attribute | ast.Subscript | ast.Call):
        if isinstance(parent, ast.Call):
            return parent.func is current
        if parent.value is not current:
            return False
        current = parent
        parent = parents.get(id(current))
    return False


def has_a_read_above_it(
    node: ast.AST,
    parents: dict[int, ast.AST],
    reads: set[int],
) -> bool:
    """Is this read already covered by a wider one?

    ``str(app.screen.query_one("#x").render())`` is one read spelled in
    four nested expressions, and reporting all four buries the message.
    Dropping a candidate that has a candidate above it reports the
    widest, which is the one a reader can act on.

    Deliberately not "the parent is a Call": that rule also silenced
    ``await pilot.press(app.screen.focused.id)``, where the enclosing
    call is a DRIVE and so is not a read at all, and
    ``test_a_read_passed_to_a_drive_is_not_exempt`` pins that.
    """
    current = parents.get(id(node))
    while current is not None and not isinstance(current, ast.stmt):
        if id(current) in reads:
            return True
        current = parents.get(id(current))
    return False


def hands_the_app_away(
    node: ast.AST,
    parents: dict[int, ast.AST],
    names: frozenset[str],
) -> bool:
    """``check(screen)`` as a bare statement: a read inside a callee.

    A statement whose value nobody uses is normally a drive, but the
    distinction that matters is WHO is called. Calling a method on the
    app changes the app. Handing the app to something else means that
    something else is about to look at it, and an assertion in a called
    helper is exactly the shape the evasion table lists.
    """
    if not isinstance(node, ast.Call) or touches_app(node.func, names):
        return False
    if value_is_consumed(node, parents):
        return False
    arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
    return any(touches_app(argument, names) for argument in arguments)


def settle_reads(source_file: Path) -> list[str]:
    """Every read of app-derived state governed by a fixed wait.

    A read is any expression that reaches the app and whose value is
    consumed, plus the bare call that hands the app to a helper. No list
    of attributes, no list of methods: the brief's definition, applied.

    The governing wait is the last await lexically above the read. Line
    order rather than control flow, which is what a reader sees and what
    a reviewer can check; a loop's back edge is not modelled, and the
    reads inside a hand-rolled ``while not cond: await pause()`` are
    therefore attributed to whatever preceded the loop. That
    over-reports, and it is why the hand-rolled loops were converted
    rather than exempted: one blessed wait means one shape to recognise.

    A read in a function that does no awaiting of its own has no
    governing wait here and is not reported. Usually that is
    attribution rather than a hole: the wait is in the helper the
    function calls, and the helper's own reads are reported against the
    helper, which is how one fix to
    ``tests/helpers/tui_screens.py::evolve_screen`` covered seven tests.
    Once it really is a hole, and it is worth naming:
    ``tests/test_tui_snapshots.py::test_component_detail_snapshot``
    hands ``snap_compare`` a callback that opens a screen and pauses,
    and the plugin captures the SVG after the callback returns, so the
    read is in neither function. Found by reading, not by this layer.
    ``test_a_read_with_no_await_in_its_own_function_is_missed`` pins the
    shape.
    """
    tree = parsed(source_file)
    helper = enrolled_waits(tree)
    names = app_derived_names(tree, helper)
    if not names:
        return []
    parents = parent_map(tree)
    return [
        hit
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for hit in _reads_in(fn, parents, names, helper)
    ]


def _reads_in(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[int, ast.AST],
    names: frozenset[str],
    helper: frozenset[str],
) -> list[str]:
    """The offending reads in one function."""
    waits = [(node.lineno, is_enrolled(node, helper)) for node in own_awaits(fn, parents)]
    # Narrowed to ``ast.expr`` rather than left as ``ast.AST``: a read is
    # always an expression, and the base class carries no ``lineno``, so
    # without this the two ``node.lineno`` uses below are the only two
    # ``mypy --strict`` errors in this file. No gate in this repo checks
    # tests/, which is exactly why they survived.
    candidates = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.expr) and _is_settle_read(node, parents, names, helper)
    ]
    reads = {id(node) for node in candidates}
    hits = []
    for node in candidates:
        if has_a_read_above_it(node, parents, reads):
            continue
        line = _governing(waits, node.lineno)
        if line is None:
            continue
        hits.append(
            f"line {node.lineno}: reads {ast.unparse(node)} after the fixed wait on line {line}"
        )
    return sorted(hits)


def _governing(waits: list[tuple[int, bool]], lineno: int) -> int | None:
    """The line of the fixed wait this read sits under, if there is one."""
    earlier = [wait for wait in waits if wait[0] < lineno]
    if not earlier or earlier[-1][1]:
        return None
    return earlier[-1][0]


def _is_settle_read(
    node: ast.AST,
    parents: dict[int, ast.AST],
    names: frozenset[str],
    helper: frozenset[str],
) -> bool:
    """Is this node a read of app-derived state at all?

    The enrolled exemption is tested FIRST, and that ordering is not
    cosmetic: ``settled(pilot, ...)`` is itself a call that hands the
    app to something, so a version of this function that asked
    :func:`hands_the_app_away` first reported every settle call in the
    tree as the defect it exists to fix. Measured, on the four in
    ``tests/test_tui_safe_mode.py``.
    """
    if inside_enrolled_wait(node, parents, helper):
        return False
    if hands_the_app_away(node, parents, names):
        return True
    if not isinstance(node, ast.Attribute | ast.Subscript | ast.Call):
        return False
    if isinstance(node, ast.Attribute | ast.Subscript) and not isinstance(node.ctx, ast.Load):
        return False  # `self._pilot = pilot` writes the name, it does not read it
    if is_the_callee(node, parents):
        return False
    if not touches_app(node, names):
        return False
    return value_is_consumed(node, parents)


class TestSettleDiscipline:
    """The two layers, one assertion each."""

    def test_no_await_is_added_without_a_settle_decision(self) -> None:
        """Layer 1, the net: pin every place a test yields to the loop.

        An app cannot settle without an await, so NEW code that waits
        has to change this dict whatever shape the wait takes. That is
        why this layer resolves nothing: ``await`` is syntax and an
        exact count of it has no aliasing to be wrong about.

        Measured, on the shapes layer 2 discloses that it misses: a
        pilot arriving from another module and a read in a function that
        does no awaiting itself are both caught HERE, because both still
        need an await somewhere in the file.
        """
        found = {
            label(source_file): sites
            for source_file in tree_sources()
            if (sites := await_sites(parsed(source_file)))
        }

        assert found == EXPECTED_AWAIT_SITES, (
            "The set of places a test yields to the event loop changed. Every await "
            "is a chance for the app to settle, so if any new one is followed by a "
            "read of async-settled state - a region, a rendered string, a mounted "
            "widget, an app attribute a worker sets - route that read through "
            "tests/helpers/settle.py instead of counting pauses. Then update "
            f"EXPECTED_AWAIT_SITES. Found: {found}"
        )

    def test_no_test_reads_settled_state_after_a_fixed_wait(self) -> None:
        """Layer 2, the message: name the line and the value.

        Empty, not a pinned inventory. An inventory here would be an
        allowlist, and an allowlist is the thing a new test gets added
        to without anybody deciding. Every one of the 163 sites this
        found on ``main`` was converted rather than recorded.
        """
        found = {
            label(source_file): hits
            for source_file in tree_sources()
            if (hits := settle_reads(source_file))
        }

        assert found == {}, (
            "A test reads state that async settling decides, after a fixed number "
            "of pauses. A pause count is not a settle condition however often it "
            "happens to be enough: this exact shape failed on CI as [0, 0, 1] and "
            "passed on rerun. Wait on the condition instead, with "
            "tests.helpers.settle.settled / mounted / drained, and make the "
            f"predicate weaker than the assertion. Sites: {found}"
        )
