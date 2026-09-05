"""kstrl's own state directory is carved out at the guard (#274).

The in-loop scope guard counts UNTRACKED files against a component's
``allowedPaths``. kstrl writes its run journals, locks, queue and
worktrees into ``<root>/.kstrl/`` WHILE that walk is happening, so in a
repository that does not ignore ``.kstrl/`` the harness trips over its
own artifacts and the operator pays an engineer iteration for it.

``ks init`` scaffolds a ``.gitignore`` carrying ``.kstrl/`` (#273), which
cannot reach a repository that already exists. This carve-out travels
with the harness instead, and the tests here pin the four things that
keep it from becoming the blanket bypass ``check_violations`` warns
against:

1. It names only the entries kstrl itself creates, so a NEW top-level
   name (``.kstrl/notes.md``) is still a violation. It does NOT stop an
   agent hiding a file INSIDE a carved subtree, because those trees hold
   runtime-invented names and only a prefix can cover them. That
   residual is stated outright by
   ``test_a_carved_subtree_is_a_prefix_and_everything_under_it_is_uncounted``
   and bounded by
   ``test_the_subtrees_that_carry_authority_are_not_carved_out``.
2. The enumeration is checked against the CODE, not maintained by hand:
   ``TestTheEnumerationMatchesTheCode`` AST-walks ``kstrl/`` for the
   ``.kstrl/<entry>`` names the package spells out and fails on one that
   is missing. That scan is what found ``snapshots`` and the legacy
   control files while this change was being written, and one test pins
   the spellings it CANNOT see so the net is not sold as more than it is.
3. It is empty unless the tree the guard walks is the same directory as
   the state root the CALLER declares. A component worktree therefore
   gets nothing, because a ``.kstrl/`` there can only be the agent's.
4. It is reported apart from the operator's authored allowlist in the
   guard's failure block, and never REVERTED: making the authority
   entries countable again would otherwise have handed them to the
   guard's deleter (``TestTheRevertArmRefusesKstrlState``).
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl import git, guards, statedir
from kstrl.config import KstrlConfig
from kstrl.guards import check_violations, path_is_allowed
from kstrl.loop import COMPLETION_MARKER, run_loop
from kstrl.statedir import (
    STATE_FILES,
    STATE_NOT_CARVED,
    STATE_SUBDIRS,
    state_dir_carve_out,
)
from kstrl.ui.plain import PlainUI
from kstrl.verify import _diff_scope_details, check_dead_code_ruff, check_diff_scope
from tests.helpers import astwalk
from tests.test_loop import MockAgent

PROJECT = Path("/project")
WORKTREE = PROJECT / ".kstrl" / "worktrees" / "run-1" / "comp-a"

# The authored scope: product code only, as an architect writes it.
AUTHORED = ["src/", "tests/"]

# One file per entry kstrl creates under the state directory, spelled the
# way `git ls-files --others` reports it. Written to disk by the `repo`
# fixture, so the real-git tests measure the real walk.
STATE_ARTIFACTS = (
    ".kstrl/autonomy.json",
    ".kstrl/contract/merge-ab12/README.md",
    ".kstrl/control_relocated",
    ".kstrl/debug/run-1/comp-a/prompt.txt",
    ".kstrl/evolution.jsonl",
    ".kstrl/experiments.tsv",
    ".kstrl/factory.lock",
    ".kstrl/inbox.jsonl",
    ".kstrl/knowledge/comp-a/run-1/fact.md",
    ".kstrl/logs/feature_x/understand.log",
    ".kstrl/progress.jsonl",
    ".kstrl/proposals/p1.json",
    ".kstrl/queue/new/item-1/item.json",
    ".kstrl/queue/pause.json",
    ".kstrl/runs/run-1/events.jsonl",
    ".kstrl/snapshots/fixture-1.json",
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed repository with NO ``.gitignore`` at all.

    The case this change exists for, and the one PR #273 cannot reach: a
    project scaffolded before the ``.gitignore`` shipped, or one whose
    operator curates their own. Nothing here is ignored, so every file
    kstrl writes under ``.kstrl/`` reaches the guard's walk.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    assert not (root / ".gitignore").exists()
    for rel in STATE_ARTIFACTS:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    return root


# ---------------------------------------------------------------------------
# The carve-out itself
# ---------------------------------------------------------------------------


class TestStateDirCarveOut:
    def test_it_names_the_entries_kstrl_creates(self) -> None:
        assert state_dir_carve_out(PROJECT, PROJECT) == [
            ".kstrl/contract/",
            ".kstrl/control_relocated",
            ".kstrl/debug/",
            ".kstrl/evolution.jsonl",
            ".kstrl/experiments.tsv",
            ".kstrl/factory.lock",
            ".kstrl/knowledge/",
            ".kstrl/logs/",
            ".kstrl/progress.jsonl",
            ".kstrl/runs/",
            ".kstrl/snapshots/",
            ".kstrl/worktrees/",
        ]

    def test_the_state_directory_itself_is_never_an_entry(self) -> None:
        """The whole difference between this and the bypass the
        ``check_violations`` docstring refuses. ``.kstrl/`` as a bare
        prefix would authorise anything under it;
        ``decompose._ALLOWED_PATHS_EXCLUDE`` will not even let an
        architect write that entry into ``allowedPaths``."""
        entries = state_dir_carve_out(PROJECT, PROJECT)
        assert ".kstrl/" not in entries
        assert ".kstrl" not in entries
        for invented in (
            ".kstrl/notes.md",
            ".kstrl/payload.py",
            ".kstrl/.env",
            ".kstrl/runs.txt",
        ):
            assert not path_is_allowed(invented, entries), invented

    def test_a_carved_subtree_is_a_prefix_and_everything_under_it_is_uncounted(
        self,
    ) -> None:
        """The residual, pinned rather than papered over (#274 review).

        The first version of ``state_dir_carve_out``'s docstring claimed
        an agent "cannot invent a hiding place under the state
        directory". It can, inside any carved subtree, because kstrl
        invents those leaf names at runtime and no exact-path form
        exists. This test states exactly how far that goes, so the claim
        and the code can never drift apart again: shrink the carve-out
        and this fails, widen it and the exclusion test below fails.
        """
        entries = state_dir_carve_out(PROJECT, PROJECT)
        for hidden in (
            ".kstrl/runs/x/evil.py",
            ".kstrl/knowledge/x/y.md",
            ".kstrl/debug/x/y.sh",
            ".kstrl/contract/x/y",
            ".kstrl/logs/x/y",
            ".kstrl/snapshots/x.json",
            ".kstrl/worktrees/x/payload.sh",
        ):
            assert path_is_allowed(hidden, entries), hidden

    def test_the_subtrees_that_carry_authority_are_not_carved_out(self) -> None:
        """The bound on that residual.

        ``queue`` is the in-tree work queue ``ks serve`` drains, so a
        file written there can admit work; ``proposals`` is what
        ``ks evolve --apply`` reads to mutate config and prompts, and
        ``auto_apply_computational`` can skip its confirmation. The
        control files these subtrees hold are owned by
        ``test_no_legacy_control_file_is_carved_out``.
        """
        entries = state_dir_carve_out(PROJECT, PROJECT)
        for visible in (
            ".kstrl/queue/new/item-1/item.json",
            ".kstrl/proposals/prop-001.md",
            ".kstrl/proposals/evil.json",
        ):
            assert not path_is_allowed(visible, entries), visible

    def test_the_exclusions_name_entries_that_actually_exist(self) -> None:
        """An exclusion for something kstrl does not create excludes
        nothing, and would read as a bound that is not there."""
        assert set(STATE_NOT_CARVED) <= set(STATE_SUBDIRS) | set(STATE_FILES)

    def test_a_lookalike_directory_is_not_covered(self) -> None:
        entries = state_dir_carve_out(PROJECT, PROJECT)
        assert not path_is_allowed(".kstrl-backup/runs/x", entries)
        assert not path_is_allowed("sub/.kstrl/runs/x", entries)
        assert not path_is_allowed("kstrl/factory.py", entries)

    def test_no_legacy_control_file_is_carved_out(self) -> None:
        """R8.9 moved live control state to the XDG directory, but a
        repository that has not run a control command since still has
        these in the tree - and they are the highest-authority files
        there. All five stay countable, which is the same ranking
        ``policy.ENFORCEMENT_MACHINERY_PATHS`` gives them: a guard that
        stopped reporting the autonomy level while the envelope treats
        touching it as a non-overridable hard fail would be two
        mechanisms disagreeing about one file.
        """
        entries = state_dir_carve_out(PROJECT, PROJECT)
        for path in statedir.legacy_control_paths(PROJECT).values():
            rel = path.relative_to(PROJECT).as_posix()
            assert not path_is_allowed(rel, entries), rel

    def test_a_walk_root_that_is_not_the_state_root_gets_nothing(self) -> None:
        """The tightening, and the reason the function takes both paths.

        A component worktree is a different directory from the project
        root, and kstrl writes its journals, locks and queue only at the
        root - so a ``.kstrl/`` inside a worktree can only be the
        agent's. Carving it out there would hide files and clear
        nothing.
        """
        assert state_dir_carve_out(WORKTREE, PROJECT) == []
        assert state_dir_carve_out(PROJECT, WORKTREE) == []
        assert state_dir_carve_out(Path("/other"), PROJECT) == []

    def test_an_undeclared_state_root_gets_nothing(self) -> None:
        """The safe default. A caller that does not declare where its
        state directory lives gets the pre-#274 behaviour, which fails
        loudly on kstrl's own artifacts - never a carve-out applied to a
        tree kstrl does not own."""
        assert state_dir_carve_out(PROJECT, None) == []

    def test_the_two_paths_are_compared_as_directories_not_strings(
        self,
        tmp_path: Path,
    ) -> None:
        """macOS reaches ``/tmp`` through a symlink and the loop's
        ``cwd`` routinely arrives spelled differently from the project
        root it was derived from, so a string comparison would silently
        disable the carve-out on the platform this is developed on."""
        real = tmp_path / "project"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        assert state_dir_carve_out(link, real) != []
        assert state_dir_carve_out(real / "." / "sub" / "..", real) != []


# ---------------------------------------------------------------------------
# The enumeration is checked against the code, not maintained by hand
# ---------------------------------------------------------------------------


_EMBEDDED = re.compile(r"\.kstrl/([A-Za-z0-9_][A-Za-z0-9_.-]*)")

#: Names the AST scan will find that the carve-out is expected NOT to
#: cover, each for its own reason:
#:
#: - ``control.lock`` lives in the XDG control directory
#:   (``statedir.control_dir``), never in the tree, so carving it out
#:   would authorise a path the harness never writes.
#: - ``STATE_NOT_CARVED`` is the deliberate authority exclusion
#:   (#274 review), sourced from the constant rather than repeated so
#:   the drift net and the policy cannot disagree.
_EXPECTED_UNCOVERED = frozenset(
    {statedir.CONTROL_LOCK_FILENAME, *STATE_NOT_CARVED},
)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = <a string this walk can fold>``. Strictly
    wider than the single-target ``ast.Assign`` to a ``str`` Constant
    that stood here: an annotated constant, a multi-target assignment
    and a name assembled by ``+`` all resolve now."""
    found: dict[str, str] = {}
    for node in tree.body:
        targets, value = astwalk.assignment_parts(node)
        folded = astwalk.folded_str(value) if value is not None else None
        if folded is not None:
            found.update({t: folded for t in targets if t is not None and "." not in t})
    return found


def _is_state_anchor(node: ast.expr) -> bool:
    """Whether ``node`` evaluates to the state directory itself."""
    if astwalk.folded_str(node) == statedir.STATE_DIR_NAME:
        return True
    if isinstance(node, ast.Name):
        return node.id == "STATE_DIR_NAME"
    return isinstance(node, ast.Call) and astwalk.leaf_name(node.func) == "state_dir"


def _joined_entry(node: ast.BinOp, constants: dict[str, str]) -> set[str]:
    """The entry a ``<the state dir> / <name>`` join names, if any.
    ``ast.walk`` reaches every ``BinOp`` in an ``a / b / c`` chain, so
    each only has to look one step left for its anchor."""
    left = node.left
    anchor = left.right if isinstance(left, ast.BinOp) and isinstance(left.op, ast.Div) else left
    if not _is_state_anchor(anchor):
        return set()
    folded = astwalk.folded_str(node.right)
    if folded is not None:
        return {folded}
    if isinstance(node.right, ast.Name) and node.right.id in constants:
        return {constants[node.right.id]}
    return set()


def _named_entries(source: str) -> set[str]:
    """Every ``.kstrl/<entry>`` name this module's CODE spells out.

    Two forms, because those are the two the package uses: a ``/`` join
    off the state directory (``root / ".kstrl" / "runs"``,
    ``state_dir(root) / QUEUE_DIR_NAME``), and a literal ``.kstrl/...``
    inside a string (``Path(".kstrl/snapshots")``, ``policy.py``'s
    globs). Deliberately no more: a net under the current idiom, not a
    proof about every possible one, which is what ``STATE_SUBDIRS``'s
    comment says. The four that slip past are
    ``test_the_scan_does_not_claim_to_see_every_spelling``'s rows.
    """
    tree = astwalk.parse(source)
    constants = _module_constants(tree)
    # A string used as a bare statement is a docstring, and a comment
    # about a directory kstrl archived in 2026 is not one kstrl writes:
    # without this the scan reported `.kstrl/archive/` from evolution.py.
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            names |= _joined_entry(node, constants)
        elif id(node) not in prose:
            names.update(m.group(1) for m in _EMBEDDED.finditer(astwalk.folded_str(node) or ""))
    return names


def _package_entries() -> set[str]:
    """Every state-dir entry ``kstrl/`` names, computed once. Memoised
    so the two tests below share one pass; the parse under it is
    ``astwalk``'s. The per-module attribution this used to collect was
    read by nothing, and the census below pins that view asserted."""
    global _PACKAGE_ENTRIES
    if _PACKAGE_ENTRIES is None:
        _PACKAGE_ENTRIES = {
            name
            for module in astwalk.package_sources()
            for name in _named_entries(module.read_text(encoding="utf-8"))
        }
    return _PACKAGE_ENTRIES


_PACKAGE_ENTRIES: set[str] | None = None

#: Every node in ``kstrl/`` writing the state directory's own name or
#: the function that builds it, per module: the net under the scan
#: above, enumerating no node types, since a module cannot name an entry
#: under a directory it never spells. Adding a row is the point.
#:
#: Measured against the four idioms the scan itself is blind to: this
#: counts THREE of them (the local alias and the attribute both spell
#: ``state_dir``, ``os.path.join`` spells ``.kstrl``), and misses only
#: the f-string, whose leading piece is ``.kstrl/`` rather than
#: ``.kstrl``. So a hiding place added under one of them moves a count
#: here even though ``_named_entries`` reports nothing.
_EXPECTED_STATE_DIR_SPELLINGS: dict[str, int] = {
    "cli.py": 6,
    "contract.py": 1,
    "decompose.py": 1,
    "events.py": 1,
    "factory.py": 8,
    "feedforward.py": 1,
    "knowledge.py": 2,
    "pipeline.py": 1,
    "reducer.py": 3,
    "serve.py": 5,
    "statedir.py": 7,
    "tui/home_data.py": 1,
    "tui/runs.py": 2,
    "tui/screens/evolve.py": 1,
    "workqueue.py": 2,
}


class TestTheEnumerationMatchesTheCode:
    """The regression net that keeps the list honest.

    ``STATE_SUBDIRS`` and ``STATE_FILES`` are hand-written, and a
    hand-written list of what a package writes is exactly the thing that
    rots. This walks every module for the entries the code actually
    names and fails if one is not carved out, so a subtree added later
    reintroduces the untracked-file failure loudly rather than in a paid
    run.
    """

    def test_every_entry_the_package_names_is_carved_out(self) -> None:
        entries = state_dir_carve_out(PROJECT, PROJECT)
        missing = sorted(
            name
            for name in _package_entries()
            if name not in _EXPECTED_UNCOVERED and not path_is_allowed(f".kstrl/{name}", entries)
        )
        assert missing == [], (
            f"kstrl writes these under .kstrl/ but the carve-out does not "
            f"cover them: {missing}. Add each to "
            f"statedir.STATE_SUBDIRS or STATE_FILES."
        )

    def test_the_scan_would_notice_a_new_subtree(self) -> None:
        """The net itself, tested. Without this the scan could be
        silently matching nothing and every run would pass."""
        source = 'from pathlib import Path\nP = Path("/x") / ".kstrl" / "brand-new"\n'
        assert _named_entries(source) == {"brand-new"}
        assert not path_is_allowed(".kstrl/brand-new", state_dir_carve_out(PROJECT, PROJECT))

    def test_the_scan_reads_a_constant_rather_than_its_name(self) -> None:
        source = 'from pathlib import Path\nQ = "queue"\nP = Path("/x") / ".kstrl" / Q\n'
        assert _named_entries(source) == {"queue"}

    def test_the_scan_ignores_prose(self) -> None:
        source = '"""Wave 1 archived the old journals to .kstrl/archive/."""\n'
        assert _named_entries(source) == set()

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    @pytest.mark.parametrize(
        "source",
        [
            'S = state_dir(r)\nP = S / "cache"\n',
            'P = base / f".kstrl/{name}"\n',
            'P = os.path.join(root, ".kstrl", "cache")\n',
            'P = self.state_dir / "cache"\n',
        ],
    )
    def test_the_scan_does_not_claim_to_see_every_spelling(self, source: str) -> None:
        """The four idioms that slip past THIS scan. None appears in
        ``kstrl/`` today, the census below counts three of the four
        anyway, and a row XPASSes the day the scan widens, which
        ``strict=True`` makes a failure: ``STATE_SUBDIRS``'s comment
        then has to be edited in the same diff."""
        astwalk.blind_spot(_named_entries, source)

    def test_every_module_that_names_the_state_directory_is_pinned(self) -> None:
        """The net under the scan, and it enumerates no node types.

        ONE CONTROL PER HALF, and the first two attempts at this line are
        why the signature takes a sequence. A disjunction fires when
        EITHER half does, so a single control is a scalar over the pair:
        ``P = state_dir(root) / "runs"`` scores one hit through ``namer``
        and zero through ``anchor``, and deleting ``anchor`` entirely
        passed it. The second attempt widened the STRING to hit both
        halves and added a test proving the string did. That was worse,
        because it reads as a mechanism: ``assert_census`` still summed
        one scalar over the whole predicate, so deleting ``anchor``
        still passed the control, measured at 1 failed and 38 passed with
        the failure in the INVENTORY at 5 rows instead of 15. A control
        that cannot fail for the reason its docstring gives is the exact
        defect #324 exists to end, so the halves are proved separately
        and a dead one now fails naming its own control.
        """
        anchor, namer = astwalk.spells(statedir.STATE_DIR_NAME), astwalk.spells("state_dir")
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=lambda node: anchor(node) or namer(node),
            expected=_EXPECTED_STATE_DIR_SPELLINGS,
            control=(
                'P = state_dir(root) / "runs"\n',
                f'Q = root / "{statedir.STATE_DIR_NAME}"\n',
            ),
            message="the set of modules that name kstrl's state directory changed.",
        )

    def test_every_declared_entry_is_reachable(self) -> None:
        """The inverse: nothing in the lists that the package never
        writes. A carve-out entry with no writer is authorisation
        granted for nothing, which is the same defect the entries are
        meant to remove."""
        declared = set(STATE_SUBDIRS) | set(STATE_FILES)
        assert declared - _package_entries() == set()


def _imported_names(source_file: Path) -> set[str]:
    """Every ``kstrl.*`` dotted name one module imports.

    The ``from ... import`` arm resolves through ``astwalk.bindings``,
    which reads ``ImportFrom.level``. What stood here filtered on
    ``(node.module or "").startswith("kstrl")``, so every relative
    import was dropped in silence, and for THIS guard a dropped edge
    makes "not reachable" pass VACUOUSLY. One import at a time, because
    ``bindings`` keeps the FIRST binding of a name and a module
    importing one name twice would lose the second edge. ``ast.Import``
    is read off the node: an absolute import is written out in full.
    """
    package = astwalk.KSTRL_PACKAGE.name
    module = astwalk.module_name(source_file)
    found: set[str] = set()
    for node in ast.walk(astwalk.parsed(source_file)):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(package))
        elif isinstance(node, ast.ImportFrom):
            table = astwalk.bindings(astwalk.parse(ast.unparse(node)), module=module)
            found.update(
                part
                for origin in table.origins.values()
                if origin.startswith(package)
                for part in (origin, origin.rsplit(".", 1)[0])
            )
    return found


def _import_closure(module: str) -> set[str]:
    """Every ``kstrl.*`` module reachable from ``module`` by import.

    Static, and deliberately includes function-level imports, which this
    codebase uses to break cycles (``loop.py`` defers ``init_cmd``). A
    static closure over-approximates what actually runs, the safe
    direction here: the claim checked is that a writer is NOT reachable.
    A name with no file behind it, the package itself or a function
    reached through a ``from ... import``, is a dead end not an error.
    """
    seen: set[str] = set()
    pending = [module]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        parts = name.split(".")[1:]
        source = astwalk.KSTRL_PACKAGE.joinpath(*parts).with_suffix(".py") if parts else None
        if source is not None and source.is_file():
            pending.extend(_imported_names(source))
    return seen


class TestNothingTheLoopRunsWritesTheUncarvedEntries:
    """Criterion 2 of ``STATE_NOT_CARVED``, mechanised (#274 review).

    Keeping ``queue``, ``proposals``, ``autonomy.json`` and
    ``inbox.jsonl`` countable is only affordable because nothing the
    engineer loop invokes writes them - otherwise the guard would fire
    on kstrl's own artifacts again, which is the whole failure this
    change exists to remove. That was a comment; it is the half of the
    argument that carries the weight, and this file's convention is that
    load-bearing claims are checked against the code.

    A static import closure is a proxy for reachability, not a proof.
    It over-approximates (it counts an import that never executes), so a
    pass is meaningful and a failure means somebody has to think.
    """

    #: The modules that write the uncarved entries: the work queue
    #: itself, the two command entry points that drive it, the proposal
    #: writer, and the GitHub intake that admits work.
    WRITERS = frozenset(
        {"kstrl.workqueue", "kstrl.serve", "kstrl.cli", "kstrl.evolution", "kstrl.intake_github"}
    )

    def test_the_loop_cannot_reach_a_writer_of_an_uncarved_entry(self) -> None:
        reachable = _import_closure("kstrl.loop") & self.WRITERS
        assert reachable == set(), (
            f"kstrl.loop can now reach {sorted(reachable)}, which write the "
            f"entries STATE_NOT_CARVED keeps countable. Either the loop no "
            f"longer needs that import, or those entries have to be carved "
            f"out again and the authority argument revisited."
        )

    def test_the_closure_is_actually_walking_something(self) -> None:
        """Without this the assertion above could be passing because the
        closure is empty, which would make it a test of nothing."""
        closure = _import_closure("kstrl.loop")
        assert "kstrl.guards" in closure
        assert "kstrl.statedir" in closure
        assert _import_closure("kstrl.serve") & self.WRITERS


# ---------------------------------------------------------------------------
# A real repository with no .gitignore: the case #273 cannot reach
# ---------------------------------------------------------------------------


class TestRealRepositoryWithoutAGitignore:
    def test_without_the_carve_out_every_state_file_is_a_violation(
        self,
        repo: Path,
    ) -> None:
        """The bug, measured against real git rather than a fixture."""
        changed = git.get_changed_files(repo)
        violations = check_violations(changed, AUTHORED)
        assert sorted(violations) == sorted(STATE_ARTIFACTS)

    def test_with_the_carve_out_only_the_excluded_subtrees_remain(
        self,
        repo: Path,
    ) -> None:
        changed = git.get_changed_files(repo)
        assert check_violations(changed, AUTHORED, state_dir_carve_out(repo, repo)) == [
            ".kstrl/autonomy.json",
            ".kstrl/inbox.jsonl",
            ".kstrl/proposals/p1.json",
            ".kstrl/queue/new/item-1/item.json",
            ".kstrl/queue/pause.json",
        ]

    def test_the_baseline_is_what_clears_the_excluded_subtrees(
        self,
        repo: Path,
    ) -> None:
        """The measurement ``STATE_NOT_CARVED``'s criterion 2 rests on.

        The real guard does not walk the working tree naked: it
        subtracts everything that was already there when the agent
        started. That is what makes keeping the authority entries
        countable affordable, so it is measured rather than argued.
        """
        baseline = git.capture_workspace_baseline(repo)
        entries = state_dir_carve_out(repo, repo)
        # Control, for failure localisation: the second assertion below
        # cannot pass while this one fails.
        assert (
            check_violations(
                git.get_changed_files_since(baseline, repo),
                AUTHORED,
                entries,
            )
            == []
        )

        # kstrl writes on; the agent slips a file into each excluded
        # subtree and one into a carved one.
        (repo / ".kstrl" / "runs" / "run-1" / "heartbeat.jsonl").write_text("x\n")
        (repo / ".kstrl" / "queue" / "new" / "item-1" / "agent.json").write_text("x\n")
        (repo / ".kstrl" / "proposals" / "prop-999.md").write_text("x\n")
        assert check_violations(
            git.get_changed_files_since(baseline, repo),
            AUTHORED,
            entries,
        ) == [
            ".kstrl/proposals/prop-999.md",
            ".kstrl/queue/new/item-1/agent.json",
        ]

    def test_a_registered_worktree_is_covered(self, repo: Path) -> None:
        """git reports a linked worktree as one directory entry with a
        trailing slash, not as its contents. Measured: without
        ``.kstrl/worktrees/`` in the carve-out that entry is a
        violation, so every factory run in an unignored repository trips
        on the worktree it just created."""
        _git(repo, "branch", "comp-a")
        _git(repo, "worktree", "add", "-q", ".kstrl/worktrees/run-1/comp-a", "comp-a")
        changed = git.get_changed_files(repo)
        assert any(f.startswith(".kstrl/worktrees/") for f in changed)
        assert not any(
            f.startswith(".kstrl/worktrees/")
            for f in check_violations(changed, AUTHORED, state_dir_carve_out(repo, repo))
        )

    def test_an_agent_file_under_the_state_dir_is_still_a_violation(
        self,
        repo: Path,
    ) -> None:
        """The hiding case. The carve-out stops the guard counting what
        kstrl wrote; it does not stop it counting what the agent wrote
        next to it."""
        (repo / ".kstrl" / "notes.md").write_text("x\n")
        (repo / ".kstrl" / "runs.txt").write_text("x\n")
        changed = git.get_changed_files(repo)
        violations = check_violations(changed, AUTHORED, state_dir_carve_out(repo, repo))
        assert ".kstrl/notes.md" in violations
        assert ".kstrl/runs.txt" in violations

    def test_it_never_creates_a_scope_where_none_was_configured(
        self,
        repo: Path,
    ) -> None:
        changed = git.get_changed_files(repo)
        assert check_violations(changed, [], state_dir_carve_out(repo, repo)) == []

    def test_product_code_outside_scope_is_still_caught(self, repo: Path) -> None:
        (repo / "pyproject.toml").write_text("x\n")
        changed = git.get_changed_files(repo)
        assert "pyproject.toml" in check_violations(
            changed,
            AUTHORED,
            state_dir_carve_out(repo, repo),
        )


# ---------------------------------------------------------------------------
# Phase 1 is deliberately NOT given the carve-out
# ---------------------------------------------------------------------------


class TestPhase1KeepsSeeingTheStateDir:
    def test_a_committed_state_dir_file_still_fails_diff_scope(
        self,
        repo: Path,
    ) -> None:
        """``check_diff_scope`` judges ``git diff base...HEAD``: only
        what the agent COMMITTED. Nothing kstrl writes at ``<root>``
        appears in a component worktree's diff, so Phase 1 never needed
        the carve-out - and leaving it out is what keeps a ``.kstrl/``
        file the agent committed from riding into the PR. That is the
        backstop for the one iteration the in-loop guard now lets pass.
        """
        _git(repo, "checkout", "-q", "-b", "work")
        _git(repo, "add", "-A", "-f")
        _git(repo, "commit", "-q", "-m", "work")
        result = check_diff_scope(repo, "main", AUTHORED)
        assert result.passed is False
        assert ".kstrl/runs/run-1/events.jsonl" in "\n".join(result.details)

    @pytest.mark.skipif(shutil.which("ruff") is None, reason="needs ruff on PATH")
    def test_dead_code_cleanup_does_not_commit_the_state_dir(
        self,
        repo: Path,
    ) -> None:
        """The one path that could have committed it for the agent.

        ``check_dead_code_ruff`` auto-commits ruff's fixes so the tree stays
        clean for later checks. Under ``use_worktrees=False`` it runs
        with ``cwd`` at the PROJECT ROOT, so the ``git add -A`` it used
        to issue swept kstrl's own live journals onto the component
        branch the moment ruff fixed one finding - and since
        ``check_diff_scope`` is deliberately un-carved, the next pass
        then failed on them and they rode into the PR. That is precisely
        the configuration the in-loop carve-out targets, so the backstop
        had a hole in exactly the case it was meant to cover
        (#274 review).

        The fix excludes the state directory from that one commit. The
        agent's own work must still be staged, or the auto-commit stops
        doing its job, so both halves are asserted.
        """
        _git(repo, "checkout", "-q", "-b", "work")
        (repo / "src" / "app.py").write_text("import os\nVALUE = 1\n")
        (repo / "src" / "new.py").write_text("VALUE = 2\n")

        check_dead_code_ruff(repo, timeout=60)

        committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
        assert not any(f.startswith(".kstrl/") for f in committed), committed
        assert "src/app.py" in committed
        assert "src/new.py" in committed
        assert check_diff_scope(repo, "main", AUTHORED).passed is True


# ---------------------------------------------------------------------------
# The loop actually applies it, and only where the state dir is kstrl's
# ---------------------------------------------------------------------------


def _loop_config(root: Path, allowed: list[str]) -> KstrlConfig:
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    (kstrl_dir / "prd.json").write_text('{"branchName": "t", "userStories": []}')
    return KstrlConfig(
        max_iterations=1,
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        allowed_paths=allowed,
        kstrl_branch="",
        kstrl_branch_explicit=True,
    )


def _captured_ignored_paths(cwd: Path, config: KstrlConfig, **kwargs: Any) -> list[str]:
    """Run one loop iteration and return what the guard was handed."""
    seen: list[list[str]] = []

    def fake(*args: Any, **call_kwargs: Any) -> tuple[bool, list[str]]:
        seen.append(list(call_kwargs.get("ignored_paths") or ()))
        return True, []

    with patch.object(guards, "enforce_allowed_paths", side_effect=fake):
        run_loop(
            config,
            PlainUI(no_color=True),
            MockAgent(["working", COMPLETION_MARKER]),
            cwd,
            **kwargs,
        )
    assert len(seen) == 1
    return seen[0]


class TestTheLoopAppliesIt:
    def test_the_guard_is_handed_the_state_carve_out(self, repo: Path) -> None:
        captured = _captured_ignored_paths(
            repo,
            _loop_config(repo, AUTHORED),
            guard_state_root=repo,
        )
        assert captured == state_dir_carve_out(repo, repo)

    def test_the_callers_own_harness_files_come_first_and_survive(
        self,
        repo: Path,
    ) -> None:
        """#264's per-component files and #274's state directory are two
        carve-outs, not one. The loop unions them; neither replaces the
        other."""
        harness = ["scripts/kstrl/feature/comp-a/prd.json"]
        captured = _captured_ignored_paths(
            repo,
            _loop_config(repo, AUTHORED),
            guard_ignored_paths=harness,
            guard_state_root=repo,
        )
        assert captured == [*harness, *state_dir_carve_out(repo, repo)]

    def test_a_component_worktree_gets_only_the_callers_files(
        self,
        repo: Path,
    ) -> None:
        """The tightening, driven through the real loop.

        The factory hands ``cwd=<worktree>`` and
        ``guard_state_root=<root>``, so the loop adds nothing and an
        agent-written ``.kstrl/`` inside the worktree stays visible to
        the guard. The ``.kstrl/runs/<run_id>/`` entry this site used to
        append unconditionally, worktree or not, is gone with it.
        """
        _git(repo, "branch", "comp-a")
        _git(repo, "worktree", "add", "-q", ".kstrl/worktrees/run-1/comp-a", "comp-a")
        worktree = repo / ".kstrl" / "worktrees" / "run-1" / "comp-a"
        harness = ["scripts/kstrl/feature/comp-a/prd.json"]
        captured = _captured_ignored_paths(
            worktree,
            _loop_config(worktree, AUTHORED),
            guard_ignored_paths=harness,
            guard_state_root=repo,
        )
        assert captured == harness

    def test_a_caller_that_declares_nothing_gets_nothing(self, repo: Path) -> None:
        """No implicit default. The loop never guesses that its ``cwd``
        owns a state directory, so the carve-out cannot be applied to a
        tree the caller did not name."""
        captured = _captured_ignored_paths(repo, _loop_config(repo, AUTHORED))
        assert captured == []


# ---------------------------------------------------------------------------
# Every production caller declares it
# ---------------------------------------------------------------------------


#: The one function every state-root declaration hangs off.
_RUN_LOOP = "kstrl.loop.run_loop"


def _run_loop_calls(tree: ast.Module, module: str) -> list[ast.Call]:
    """Every call in one module that RESOLVES to ``run_loop``.

    Split out because the loop below costs 16 on the cognitive gate
    with it inline, and that hook fails rather than advises.
    """
    table = astwalk.bindings(tree, module=module)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and table.resolve(node.func) == _RUN_LOOP
    ]


class TestEveryCallerDeclaresTheStateRoot:
    """The cost of the safe default, paid once here.

    ``guard_state_root=None`` means "no carve-out", so a caller that
    forgets it gets the pre-#274 bug back rather than a carve-out
    applied to a tree kstrl does not own. That is the right failure
    direction, and it is only safe because something checks the callers
    actually pass it. This is that check, by source inspection: driving
    the five call sites end to end would cost five agent harnesses to
    assert one keyword.

    Five call sites across three modules, which is why the assertion
    counts per module rather than naming a total: ``feature_cmd`` calls
    ``run_loop`` three times, once per phase.
    """

    def test_every_run_loop_call_site_passes_it(self) -> None:
        """Resolved, not matched by bare name, and both halves claimed.
        Undecided rows are counted per FILE rather than pinned as
        ``file:line``, so an unrelated edit above one is not a failure
        here; nor is a call to somebody else's ``run_loop``."""
        callers: dict[str, int] = {}
        declared: dict[str, int] = {}
        undecided: tuple[str, ...] = ()
        for source_file in astwalk.package_sources():
            module = astwalk.module_name(source_file)
            if module == _RUN_LOOP.rsplit(".", 1)[0]:
                continue
            tree = astwalk.parsed(source_file)
            where = astwalk.label(source_file)
            undecided += astwalk.calls_to(tree, [_RUN_LOOP], where=where, module=module).undecided
            for node in _run_loop_calls(tree, module):
                callers[source_file.name] = callers.get(source_file.name, 0) + 1
                if any(kw.arg == "guard_state_root" for kw in node.keywords):
                    declared[source_file.name] = declared.get(source_file.name, 0) + 1
        assert Counter(site.split(":", 1)[0] for site in undecided) == {
            "gateparse.py": 2,
            "tui/app.py": 2,
        }, f"the walk could not decide these calls: {list(undecided)}"
        assert callers == {"cli.py": 1, "factory.py": 1, "feature_cmd.py": 3}
        assert declared == callers

    def test_the_run_loop_walk_resolves_a_name_rather_than_matching_it(self) -> None:
        """The control this check had none of: it matched the bare
        identifier, so an alias was a miss and a stranger of that name a
        hit, and an empty result reads exactly like a matcher that has
        stopped matching."""
        alias = astwalk.parse("from kstrl.loop import run_loop as _s\ndef go(c):\n    _s(c)\n")
        found = astwalk.calls_to(alias, [_RUN_LOOP], where="a.py", module="kstrl.x")
        assert found == astwalk.Sites((f"a.py:3 {_RUN_LOOP}",))
        other = astwalk.parse("from other import run_loop\ndef go():\n    run_loop()\n")
        assert astwalk.calls_to(other, [_RUN_LOOP], module="kstrl.x") == astwalk.Sites()
        opaque = astwalk.parse("def go(t):\n    t['run_loop']()\n")
        assert astwalk.calls_to(opaque, [_RUN_LOOP], where="t.py").undecided == (
            "t.py:2 t['run_loop']",
        )


# ---------------------------------------------------------------------------
# Reported apart from the operator's authored allowlist
# ---------------------------------------------------------------------------


class TestTheFailureBlockSeparatesTheTwoSets:
    def test_it_names_what_the_operator_authorised_and_what_kstrl_added(
        self,
        repo: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (repo / "pyproject.toml").write_text("x\n")
        config = _loop_config(repo, AUTHORED)
        config.interactive = False
        ok, violations = guards.enforce_allowed_paths(
            config,
            PlainUI(no_color=True),
            repo,
            ignored_paths=state_dir_carve_out(repo, repo),
        )
        assert ok is False
        assert "pyproject.toml" in violations
        assert ".kstrl/runs/run-1/events.jsonl" not in violations
        printed = capsys.readouterr()
        lines = (printed.out + printed.err).splitlines()
        allowed_line = next(line for line in lines if "ALLOWED_PATHS" in line)
        harness_line = next(line for line in lines if "HARNESS_PATHS" in line)
        assert allowed_line.endswith("src/, tests/")
        assert ".kstrl/runs/" in harness_line
        assert ".kstrl" not in allowed_line

    def test_it_makes_the_same_claim_the_other_two_guards_make(
        self,
        repo: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A retry agent must not be told two different things about the
        same files depending on which guard fired.

        Compared against what Phase 1 actually PRINTS, not against its
        source, so an edit to either wording breaks this rather than an
        edit to either layout. The factory's third copy of the sentence
        is pinned by ``tests/test_harness_path_scope.py``.
        """
        (repo / "pyproject.toml").write_text("x\n")
        config = _loop_config(repo, AUTHORED)
        config.interactive = False
        guards.enforce_allowed_paths(
            config,
            PlainUI(no_color=True),
            repo,
            ignored_paths=state_dir_carve_out(repo, repo),
        )
        printed = capsys.readouterr()
        claim = "already in scope, no need to widen allowedPaths"
        assert claim in printed.out + printed.err
        phase_1 = _diff_scope_details("main", AUTHORED, [".kstrl/runs/"], ["pyproject.toml"])
        assert claim in "\n".join(phase_1)

    def test_nothing_is_printed_when_there_is_no_carve_out(
        self,
        repo: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (repo / "pyproject.toml").write_text("x\n")
        config = _loop_config(repo, AUTHORED)
        config.interactive = False
        guards.enforce_allowed_paths(config, PlainUI(no_color=True), repo)
        printed = capsys.readouterr()
        assert "HARNESS_PATHS" not in printed.out + printed.err


# ---------------------------------------------------------------------------
# Reporting a file and destroying it are different powers
# ---------------------------------------------------------------------------


class _RevertingUI(PlainUI):
    """An operator who picks "Revert and continue" at the guard prompt."""

    def can_prompt(self) -> bool:
        return True

    def choose(self, header: str, options: list[str], default: int = 0) -> int:
        return options.index("Revert and continue")


class TestTheRevertArmRefusesKstrlState:
    """The consequence of keeping the authority entries countable.

    ``STATE_NOT_CARVED`` makes the queue, the proposals, the autonomy
    level and the pause marker VISIBLE to the guard again. The guard's
    interactive arm disposes of a violation by deleting it, and
    ``git.delete_untracked`` recurses into directories, so visibility
    alone would have handed those paths to a deleter: an operator
    choosing "Revert and continue" could destroy the pause marker they
    had just written to stop the run. Reporting and destroying are
    different powers (#274 review).
    """

    def test_it_reports_the_state_file_and_leaves_it_on_disk(
        self,
        repo: Path,
    ) -> None:
        pause = repo / ".kstrl" / "queue" / "pause.json"
        rogue = repo / "pyproject.toml"
        rogue.write_text("x\n")
        config = _loop_config(repo, AUTHORED)
        config.interactive = True

        ok, violations = guards.enforce_allowed_paths(
            config,
            _RevertingUI(no_color=True),
            repo,
            ignored_paths=state_dir_carve_out(repo, repo),
        )

        assert pause.exists(), "the guard deleted kstrl's own pause marker"
        assert not rogue.exists(), "the genuine violation was not reverted"
        assert ok is False, "a refused revert must not report success"
        assert ".kstrl/queue/pause.json" in violations
        assert "pyproject.toml" not in violations

    def test_a_carved_state_file_never_reaches_the_revert_arm_at_all(
        self,
        repo: Path,
    ) -> None:
        """Belt and braces from the other side: the carve-out means
        ``.kstrl/runs/`` is not a violation, so the refusal above is the
        second line of defence, not the only one."""
        events = repo / ".kstrl" / "runs" / "run-1" / "events.jsonl"
        config = _loop_config(repo, AUTHORED)
        config.interactive = True

        guards.enforce_allowed_paths(
            config,
            _RevertingUI(no_color=True),
            repo,
            ignored_paths=state_dir_carve_out(repo, repo),
        )
        assert events.exists()
