"""Every write into the inbox from ``kstrl/``, and what it catches (#232).

The defect this exists to catch is one the review found twice in one PR.
``Inbox._append`` takes the control lock on every write, so a write can
raise ``ControlStateError``, a ``RuntimeError`` that the
``(OSError, ValueError)`` pair every inbox site was hand-written with
does not catch. Round 1 fixed the four sites it had a finding for; two
more had the identical hole and were found by reading, and a seventh
(the TUI's decide path) by this walk.

``InboxConfig.load`` casts per key, so ``[inbox] open_item_cap =
1979-05-27`` raises ``TypeError``, which is not a ``ValueError`` either.
Same shape, one call earlier, and it was missing from three of the sites
written to close the first one.

So the guard is not a list of the sites somebody remembered. It is a
census of where an ``Inbox`` is OBTAINED, which is closed by
construction: you cannot write to an inbox you did not construct, and
every construction in the package resolves. An eighth write site either
constructs one, and fails the census below, or takes one from a
constructor already pinned in that module, and fails the mutation
inventory. Both halves carry a control, because an inventory that
matches is also what a walk pointed at nothing returns.

What this does NOT see, stated rather than left implicit: a mutating
call through a receiver the resolver cannot place. That residual is what
the construction census is for; it is why the census is the first layer
and not a convenience.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers import astwalk

INBOX = "kstrl.inbox.Inbox"
INBOX_CONFIG_LOAD = "kstrl.inbox.InboxConfig.load"
CONTROL_ERROR = "kstrl.statedir.ControlStateError"

#: Every method of ``Inbox`` that reaches ``_append`` and therefore takes
#: the control lock. ``_decide`` is the shared body behind four of them,
#: so they are enumerated by their public spellings.
MUTATORS = frozenset({"add", "approve", "compact", "reject", "resolve", "snooze"})

#: Every place ``kstrl/`` constructs an ``Inbox``, line numbers dropped.
#: A new one is an unexplained delta: adding a row here is how you say
#: you have decided what the new site catches.
EXPECTED_CONSTRUCTIONS = (
    "autonomy.py kstrl.inbox.Inbox",
    "calibration_ladder.py kstrl.inbox.Inbox",
    "cli.py kstrl.inbox.Inbox",
    "factory.py kstrl.inbox.Inbox",
    "pipeline.py kstrl.inbox.Inbox",
    "serve.py kstrl.inbox.Inbox",
    "tui/screens/inbox.py kstrl.inbox.Inbox",
)

#: The same census BY COUNT. ``without_line_numbers`` deduplicates, so
#: the tuple above cannot see a second construction in a module that
#: already has one: ``pipeline`` and ``serve`` have two each. A pin whose
#: subject is "how many" needs its own row.
EXPECTED_CONSTRUCTION_COUNTS = {
    "autonomy.py": 1,
    "calibration_ladder.py": 1,
    "cli.py": 1,
    "factory.py": 1,
    "pipeline.py": 2,
    "serve.py": 2,
    "tui/screens/inbox.py": 1,
}

#: Calls whose callee has no identifier at all, which ``calls_to`` treats
#: as a candidate for every target set. Neither is an inbox, and both are
#: pinned rather than filtered so a third one has to be looked at.
EXPECTED_UNDECIDED = (
    "gateparse.py TOOL_PARSERS[chosen]",
    "gateparse.py TOOL_PARSERS[name]",
    "tui/app.py initial_screens_for_kind(kind, observe_only=False)",
    "tui/app.py initial_screens_for_kind(kind, observe_only=True)",
)


@dataclass(frozen=True)
class Disposition:
    """What a site is contracted to do with a control-state failure.

    ``guarded`` means an enclosing ``try`` in the same function names
    ``ControlStateError`` BY ORIGIN. ``propagates`` means the exception is
    the caller's answer, and the reason has to say why that is right
    there; a row without one fails.
    """

    guarded: bool
    reason: str = ""


_GUARDED = Disposition(guarded=True)

#: Every inbox mutation in ``kstrl/``, and what it is contracted to do.
#: Keyed ``module::scope::method`` so an edit above the site cannot break
#: the pin.
EXPECTED_MUTATIONS: dict[str, Disposition] = {
    "autonomy.py::apply_demotion::add": _GUARDED,
    "calibration_ladder.py::_open_drift_item::add": _GUARDED,
    "factory.py::_open_health_breach_items::add": _GUARDED,
    "pipeline.py::ComponentPipeline._inbox_add::add": _GUARDED,
    "pipeline.py::ComponentPipeline._inbox_resolve::resolve": _GUARDED,
    "serve.py::_file_inbox_item::add": _GUARDED,
    "tui/screens/inbox.py::InboxScreen._decide::approve": _GUARDED,
    "tui/screens/inbox.py::InboxScreen._decide::reject": _GUARDED,
    "tui/screens/inbox.py::InboxScreen._decide::snooze": _GUARDED,
    "cli.py::_decide_and_report::approve": Disposition(
        guarded=False,
        reason=(
            "an operator typed this command and is watching it; a control "
            "lock it could not take is the command's answer, not a "
            "bookkeeping failure to absorb behind a transition that has "
            "already happened"
        ),
    ),
    "cli.py::_decide_and_report::reject": Disposition(
        guarded=False, reason="as _decide_and_report::approve"
    ),
    "cli.py::_decide_and_report::snooze": Disposition(
        guarded=False, reason="as _decide_and_report::approve"
    ),
    "cli.py::_decide_and_report::resolve": Disposition(
        guarded=False, reason="as _decide_and_report::approve"
    ),
    "cli.py::inbox_retry::resolve": Disposition(
        guarded=False,
        reason=(
            "the manifest reset above it has already been saved, but this "
            "is an operator command in the foreground: it must not report "
            "a requeue it could not finish"
        ),
    ),
}

#: Every ``InboxConfig.load`` call in ``kstrl/``. ``guarded`` here means
#: an enclosing ``try`` names ``TypeError``, which is what a per-key cast
#: raises on a TOML date or array.
EXPECTED_CONFIG_LOADS: dict[str, Disposition] = {
    "autonomy.py::apply_demotion": _GUARDED,
    "calibration_ladder.py::_open_drift_item": _GUARDED,
    "factory.py::_open_health_breach_items": _GUARDED,
    "pipeline.py::ComponentPipeline._inbox_add": _GUARDED,
    "pipeline.py::ComponentPipeline._inbox_resolve": _GUARDED,
    "serve.py::_file_inbox_item": _GUARDED,
    "serve.py::check_inbox_cap": Disposition(
        guarded=False,
        reason=(
            "a read-only admission gate. [inbox] is a preflight section, so "
            "a malformed value is reported as a configuration problem "
            "before the daemon reaches here, and a gate that cannot read "
            "its own cap must refuse rather than admit"
        ),
    ),
    "cli.py::_inbox_for": Disposition(
        guarded=False,
        reason=(
            "an operator command in the foreground, and the preflight has "
            "already named a malformed [inbox] as a configuration problem"
        ),
    ),
}


def _construction_sites() -> astwalk.Sites:
    """Every ``Inbox(...)`` in the package, with line numbers kept.

    Kept because two pins read this: one over the rows, which
    deduplicates, and one over the counts, which must not.
    """
    found = astwalk.Sites()
    for source in astwalk.package_sources():
        found += astwalk.calls_to(
            astwalk.parsed(source),
            {INBOX},
            where=astwalk.label(source),
            module=astwalk.module_name(source),
        )
    return found.sorted()


def _returns_an_inbox(scope: ast.AST, table: astwalk.Bindings) -> bool:
    """Whether this function hands an ``Inbox`` back to its caller.

    Anywhere in the returned expression, so ``return root_dir,
    Inbox(...)`` counts: ``cli._inbox_for`` returns the pair and every
    ``ks inbox`` command unpacks it.
    """
    return any(
        isinstance(inner, ast.Call) and table.resolve(inner.func) == INBOX
        for node in astwalk.own_nodes(scope)
        if isinstance(node, ast.Return) and node.value is not None
        for inner in ast.walk(node.value)
    )


def _inbox_holders(tree: ast.Module, table: astwalk.Bindings) -> frozenset[str]:
    """Names in one module that hold an ``Inbox``.

    Bound from a construction directly, or from a call to a function in
    this module that returns one. Over-matching is the safe direction
    here: an extra holder adds a row somebody has to enrol, and a missing
    one is the skip direction the construction census covers.
    """
    returning = {
        scope.name
        for scope, _ in astwalk.scopes(tree)
        if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef)
        and _returns_an_inbox(scope, table)
    }
    holders: set[str] = set()
    for node in astwalk.all_nodes(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            continue
        value = node.value
        if value is None:
            continue
        gives_inbox = any(
            isinstance(inner, ast.Call)
            and (table.resolve(inner.func) == INBOX or astwalk.leaf_name(inner.func) in returning)
            for inner in ast.walk(value)
        )
        if gives_inbox:
            holders.update(_target_names(node))
    return frozenset(holders)


def _target_names(node: ast.Assign | ast.AnnAssign | ast.NamedExpr) -> set[str]:
    """Every dotted target one binding binds, tuple targets expanded.

    ``astwalk.assignment_parts`` answers None for a tuple target, and
    ``root_dir, box = _inbox_for(root)`` is how six ``ks inbox`` commands
    get theirs.
    """
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: set[str] = set()
    for target in targets:
        parts = target.elts if isinstance(target, ast.Tuple | ast.List) else [target]
        names.update(name for part in parts if (name := astwalk.dotted(part)) is not None)
    return names


def _mutation_rows(source: Path) -> dict[str, tuple[ast.Call, ast.AST]]:
    """Every inbox mutation in one module, keyed ``module::scope::method``."""
    tree = astwalk.parsed(source)
    table = astwalk.bindings(tree, module=astwalk.module_name(source))
    holders = _inbox_holders(tree, table)
    rows: dict[str, tuple[ast.Call, ast.AST]] = {}
    for scope, qualified in astwalk.scopes(tree):
        for node in astwalk.own_nodes(scope):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in MUTATORS:
                continue
            receiver = node.func.value
            on_an_inbox = (
                isinstance(receiver, ast.Call) and table.resolve(receiver.func) == INBOX
            ) or astwalk.dotted(receiver) in holders
            if on_an_inbox:
                rows[f"{astwalk.label(source)}::{qualified}::{node.func.attr}"] = (node, scope)
    return rows


def _config_load_rows(source: Path) -> dict[str, tuple[ast.Call, ast.AST]]:
    """Every ``InboxConfig.load`` call in one module, keyed by scope."""
    tree = astwalk.parsed(source)
    table = astwalk.bindings(tree, module=astwalk.module_name(source))
    rows: dict[str, tuple[ast.Call, ast.AST]] = {}
    for scope, qualified in astwalk.scopes(tree):
        for node in astwalk.own_nodes(scope):
            if isinstance(node, ast.Call) and table.resolve(node.func) == INBOX_CONFIG_LOAD:
                rows[f"{astwalk.label(source)}::{qualified}"] = (node, scope)
    return rows


def _catching(scope: ast.AST, call: ast.Call, table: astwalk.Bindings) -> list[astwalk.Clause]:
    """The clauses of every ``try`` in this scope whose BODY holds the call.

    ``try_body_nodes`` rather than ``ast.walk``, so a handler is not
    credited with guarding a call in a function merely DEFINED in its
    body.
    """
    found: list[astwalk.Clause] = []
    for node in astwalk.own_nodes(scope):
        if isinstance(node, ast.Try | ast.TryStar) and call in astwalk.try_body_nodes(node):
            found.extend(astwalk.handler_clauses(node, table))
    return found


def _all_rows(subject: str) -> dict[str, tuple[ast.Call, ast.AST, astwalk.Bindings]]:
    """Every row of one kind over the whole package, with its module's table.

    ``subject`` is ``"mutations"`` or ``"configs"``; the table travels
    with the row because a handler is resolved against the module that
    wrote it, not against the one the guard lives in.
    """
    built: dict[str, tuple[ast.Call, ast.AST, astwalk.Bindings]] = {}
    for source in astwalk.package_sources():
        table = astwalk.bindings(astwalk.parsed(source), module=astwalk.module_name(source))
        rows = _mutation_rows(source) if subject == "mutations" else _config_load_rows(source)
        for key, (call, scope) in rows.items():
            built[key] = (call, scope, table)
    return built


CONTROL_MUTATION = """
from kstrl.inbox import Inbox, InboxConfig

def emit(root):
    box = Inbox(root, InboxConfig.load(root))
    box.add("kind", "title")
"""


class TestConstructionCensus:
    def test_every_inbox_construction_is_pinned(self) -> None:
        """The closed half: you cannot write to an inbox you did not obtain."""
        found = _construction_sites().without_line_numbers().sorted()
        astwalk.assert_sites(
            found,
            seen=EXPECTED_CONSTRUCTIONS,
            undecided=EXPECTED_UNDECIDED,
            message=(
                "kstrl/ constructs an Inbox somewhere new. Decide what that "
                "site catches, enrol its mutations in EXPECTED_MUTATIONS, "
                "and add the row here."
            ),
        )

    def test_the_construction_count_is_pinned(self) -> None:
        counts: dict[str, int] = {}
        for row in _construction_sites().seen:
            module = row.split(":", 1)[0]
            counts[module] = counts.get(module, 0) + 1
        assert counts == EXPECTED_CONSTRUCTION_COUNTS, (
            "a module gained or lost an Inbox construction. The row-based "
            f"census above deduplicates and cannot see this. Found: {counts}"
        )

    def test_the_census_net_fires(self) -> None:
        """A matching inventory is also what a switched-off net returns."""
        control = astwalk.parse(CONTROL_MUTATION)
        assert astwalk.calls_to(control, {INBOX}, where="control").seen


class TestMutationInventory:
    def test_every_mutation_is_pinned(self) -> None:
        rows = _all_rows("mutations")
        assert set(rows) == set(EXPECTED_MUTATIONS), (
            "the inbox mutation sites in kstrl/ have moved. Every one of "
            "them takes the control lock, so each needs a disposition: "
            f"found {sorted(rows)}"
        )

    def test_the_mutation_net_fires(self) -> None:
        """The control, planted through the same resolver the walk uses."""
        tree = astwalk.parse(CONTROL_MUTATION)
        table = astwalk.bindings(tree)
        holders = _inbox_holders(tree, table)
        assert "box" in holders
        found = [
            node
            for scope, _ in astwalk.scopes(tree)
            for node in astwalk.own_nodes(scope)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MUTATORS
            and astwalk.dotted(node.func.value) in holders
        ]
        assert len(found) == 1

    @pytest.mark.parametrize("key", sorted(EXPECTED_MUTATIONS))
    def test_each_mutation_matches_its_disposition(self, key: str) -> None:
        call, scope, table = _all_rows("mutations")[key]
        clauses = _catching(scope, call, table)
        caught = {origin for clause in clauses for origin in clause.origins}
        expected = EXPECTED_MUTATIONS[key]
        if expected.guarded:
            assert CONTROL_ERROR in caught, (
                f"{key} writes to the inbox without an enclosing handler "
                f"naming {CONTROL_ERROR} by origin. Inbox._append takes the "
                "control lock on every write, so this site can raise a "
                f"RuntimeError its clause does not catch. Caught: {sorted(caught)}"
            )
        else:
            assert expected.reason, f"{key} propagates and says nothing about why"
            assert CONTROL_ERROR not in caught, (
                f"{key} is enrolled as propagating and now catches "
                f"{CONTROL_ERROR}. Move it to guarded."
            )


class TestConfigLoadInventory:
    def test_every_config_load_is_pinned(self) -> None:
        rows = _all_rows("configs")
        assert set(rows) == set(EXPECTED_CONFIG_LOADS), (
            "InboxConfig.load is read somewhere new in kstrl/. It casts per "
            "key, so a TOML date raises TypeError; decide what this site "
            f"does with that. Found {sorted(rows)}"
        )

    @pytest.mark.parametrize("key", sorted(EXPECTED_CONFIG_LOADS))
    def test_each_config_load_matches_its_disposition(self, key: str) -> None:
        call, scope, table = _all_rows("configs")[key]
        names = {name for clause in _catching(scope, call, table) for name in clause.names}
        expected = EXPECTED_CONFIG_LOADS[key]
        if expected.guarded:
            assert "TypeError" in names, (
                f"{key} reads InboxConfig without catching TypeError. "
                "int(section['open_item_cap']) on a TOML date raises one, "
                "and it is not a ValueError. Caught: " + repr(sorted(names))
            )
        else:
            assert expected.reason, f"{key} propagates and says nothing about why"
