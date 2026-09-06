"""The files every guard walks, and the values a reader can decide.

Split out of one 800-line module because the length ratchet is right:
the corpus and the folding are the vocabulary, and everything above
them is built from it. ``astwalk/__init__.py`` re-exports the lot.
"""

from __future__ import annotations

import ast
from collections.abc import MutableMapping
from pathlib import Path
from weakref import WeakKeyDictionary

#: The checkout, located from this file rather than from a caller's, so
#: that ten guards stop each deriving it and disagreeing about the answer.
#: The depth is pinned by ``test_astwalk_nets.py``, which is how splitting
#: this module into a package was caught rather than silently emptying the
#: corpus and turning every census in the suite green.
REPO_ROOT = Path(__file__).resolve().parents[3]
KSTRL_PACKAGE = REPO_ROOT / "kstrl"
TESTS_DIR = REPO_ROOT / "tests"


# --- corpus ---------------------------------------------------------------


def label(source_file: Path, root: Path | None = None) -> str:
    """How a file is named in an inventory key and in a failure message.

    Not ``source_file.name``: ten basenames occur twice in ``kstrl/``,
    once at the top level and once under ``tui/screens/``, and a message
    naming a file the reader cannot find is worse than none. Falls back
    to the repo-relative path, then to the basename, so a snippet written
    to a ``tmp_path`` still labels itself.
    """
    for base in (root or KSTRL_PACKAGE, REPO_ROOT):
        try:
            return str(source_file.relative_to(base))
        except ValueError:
            continue
    return source_file.name


def module_name(source_file: Path) -> str:
    """``kstrl/tui/session.py`` -> ``kstrl.tui.session``.

    What a relative import resolves against. ``__init__.py`` is stripped
    so ``from . import x`` inside a package's own ``__init__`` lands on
    the package rather than one level below it.
    """
    try:
        relative = source_file.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return source_file.stem
    dotted_path = str(relative.with_suffix("")).replace("/", ".")
    return dotted_path[: -len(".__init__")] if dotted_path.endswith(".__init__") else dotted_path


def package_sources() -> list[Path]:
    """Every module in ``kstrl/``, in a stable order. Never empty.

    The emptiness check is HERE, at the chokepoint, and not only in
    :func:`~.net.assert_census`, because #324 round 2 measured four
    assertions that walk this list themselves and never reach the census:
    ``test_journal_one_writer``'s single-writer sweep,
    ``test_event_names_have_one_home``'s literal sweep and both prompt
    walks in ``test_prompt_enrollment_walk``. Repointed one directory too
    high, all four pass GREEN while looking at nothing, and their only
    protection is that a sibling census in the same FILE goes red, which
    is a property of the file rather than of the assertion.
    """
    found = sorted(KSTRL_PACKAGE.rglob("*.py"))
    assert found, _EMPTY.format(what="kstrl/", root=KSTRL_PACKAGE)
    return found


def test_sources(exclude: Path | None = None) -> list[Path]:
    """Every module in ``tests/``, minus the caller's own file if given.

    A guard naming the shapes it forbids in its own fixtures would
    otherwise scan itself. Never empty, for the reason above: excluding
    the caller cannot empty a directory that holds the caller.

    The hazard this name carries is a MODULE PYTEST COLLECTS, not a
    from-import: pytest collects any module-level name matching
    ``test*``, so a from-import binds an extra "test" there and pytest
    runs the helper as one. ``__test__ = False`` below is what makes
    that safe rather than a rule every caller has to remember, which
    is why the rule is stated as the hazard and not as the import
    style. ``astwalk/__init__.py`` from-imports this name and must;
    pytest does not collect ``tests/helpers/``. Measured in
    ``test_event_names_have_one_home.py``: with a from-import and no
    ``__test__``, 2 collected became 3 and the third PASSED, emitting
    only a ``PytestReturnNotNoneWarning`` into a suite that already
    emits eight. With ``__test__ = False`` the same from-import
    collects 2.
    """
    skip = exclude.resolve() if exclude is not None else None
    found = [path for path in sorted(TESTS_DIR.rglob("*.py")) if path.resolve() != skip]
    assert found, _EMPTY.format(what="tests/", root=TESTS_DIR)
    return found


test_sources.__test__ = False  # type: ignore[attr-defined]


#: What an empty corpus means, said once. It is never a real answer: both
#: directories are in the repository this file is in, so an empty glob is
#: a wrong root, and every net downstream would return ``{}`` and pass.
_EMPTY = (
    "no modules found under {what}, so every guard walking this corpus is "
    "an assertion about nothing. This is a derivation bug, not an answer: "
    "check REPO_ROOT's depth, which is {root}."
)


#: Source text -> its parsed tree, shared by every guard in the suite.
#: Keyed on the TEXT rather than the path, because the positive controls
#: in several guards rewrite one ``other.py`` several times inside a
#: single test, and a path-keyed cache would hand the second call the
#: first snippet's tree.
_PARSED: dict[str, ast.Module] = {}


def parsed(source_file: Path) -> ast.Module:
    """The module's AST, parsed once for the whole session.

    Measured: 127 modules, 237,105 nodes, 123 ms a pass. Before this
    cache ``tests/test_tui_config_walk.py`` alone made four of those
    passes per session and nine other guards made one each.
    """
    return parse(source_file.read_text(encoding="utf-8"))


def parse(source: str) -> ast.Module:
    """One snippet's AST, from the same cache. Reads well in a control."""
    tree = _PARSED.get(source)
    if tree is None:
        tree = ast.parse(source)
        _PARSED[source] = tree
    return tree


#: One tree -> its nodes, walked once. A WEAK key, so a tree a guard built
#: with a bare ``ast.parse`` and then dropped takes its row with it; the
#: strong-keyed first draft of the sibling ``_BINDINGS`` cache held 158
#: such trees alive, measured at 71 MB.
_NODES: MutableMapping[ast.AST, tuple[ast.AST, ...]] = WeakKeyDictionary()


def all_nodes(tree: ast.AST) -> tuple[ast.AST, ...]:
    """Every node under this one, walked once for the whole session.

    ``ast.walk`` is not free and the package walked the same tree up to
    four times in one :func:`~..resolve.calls_to`: the bindings sweep, the
    class-body scan, the call loop and the bound-target scan. Measured
    over a full guard run: 50,623 traversals, 29,938 of them (59 percent)
    repeats of a tree already walked, and 4.72 s of the run's 24 s of CPU
    spent inside ``ast.walk`` alone. With this memo that is 0.303 s, and
    ``calls_to`` over the 128-module package drops from 173.8 ms a pass to
    45.0 ms.

    A TUPLE, not a list, because a shared answer a caller can mutate is
    not a shared answer. Its cost is measured too: about 8.6 MB of slots
    for 1,070,958 references, against the 71 MB the weak key gives back.
    """
    hit = _NODES.get(tree)
    if hit is None:
        hit = tuple(ast.walk(tree))
        _NODES[tree] = hit
    return hit


# --- constant folding -----------------------------------------------------


def folded_str(node: ast.AST) -> str | None:
    """The string this expression is KNOWN to evaluate to, or None.

    Round 2 of review on #327 is why this exists: two writers defeated
    three layers of a guard by never spelling in one piece what they
    reached, ``getattr(config, "journal_" + "path")`` and
    ``root / ".kstrl" / ("evolution" + ".jsonl")``. CPython folds adjacent
    literals (``"a" "b"``) into one ``Constant`` at parse time, so that
    needs nothing here; an f-string does NOT fold, measured, so
    ``JoinedStr`` and ``FormattedValue`` are handled explicitly.

    Decidable cases only. Anything whose value needs the interpreter
    (``"".join(parts)``, ``%``-formatting, a name, an env var) returns
    None, and a guard that folds must disclose that residual and pin it
    with :func:`blind_spot` rather than imply it away.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp):
        return _folded_concat(node)
    if isinstance(node, ast.FormattedValue):
        return _folded_placeholder(node)
    if isinstance(node, ast.JoinedStr):
        return _folded_parts(node.values)
    return None


def _folded_concat(node: ast.BinOp) -> str | None:
    """``"journal_" + "path"``, and nothing else that uses ``+``."""
    if not isinstance(node.op, ast.Add):
        return None
    return _folded_parts([node.left, node.right])


def _folded_placeholder(node: ast.FormattedValue) -> str | None:
    """The ``{...}`` of an f-string, when it is decidable.

    ``!r`` and a format spec both change the result, so only the plain
    case folds. Measured: ``ast.parse`` gives ``conversion == -1`` for a
    plain placeholder and 114 for ``!r``, and ``None`` never appears on
    the parse path.
    """
    if node.conversion == -1 and node.format_spec is None:
        return folded_str(node.value)
    return None


def _folded_parts(nodes: list[ast.expr]) -> str | None:
    """Every piece folded and joined, or None if any piece is unknown."""
    parts: list[str] = []
    for node in nodes:
        folded = folded_str(node)
        if folded is None:
            return None
        parts.append(folded)
    return "".join(parts)
