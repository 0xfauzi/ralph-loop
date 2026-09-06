"""#289: a home-shell screen must name a broken config, not raise on it.

The measurement this file replaces a claim with: on 5abbc91, pushing
``EvolveScreen`` at a project whose kstrl.toml holds
``[evolution] lookback_runs = "many"`` raised

    ValueError: invalid literal for int() with base 10: 'many'

out of ``EvolveScreen.on_mount`` -> ``reload`` ->
``_load_patterns_and_trends`` -> ``EvolutionConfig.load``, which in a
real shell is an unhandled exception in a message handler and takes the
app down. ``ks evolve --root <same dir>`` printed one named line and
exited 1 for the same file. ``InboxScreen`` had the same shape on a
malformed document.

The screens are reachable because the entry check DEGRADES
``[evolution]``: it warns and lets the shell open (measured - see
``test_the_shell_opens_on_the_value_that_crashes_the_screen``), and the
evolve screen is the screen that section is about.
"""

from __future__ import annotations

import ast
import importlib
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import DataTable, Static

from kstrl.config import ConfigError
from kstrl.config_preflight import (
    collect_config_problems,
    load_or_report,
    preflight_config,
    raise_if_defect,
)
from kstrl.evolution import EvolutionConfig
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.tui.config_guard import env_scrub_is_safe
from kstrl.tui.screens.inbox import InboxScreen
from kstrl.tui.widgets.config_problem import ConfigProblemBanner
from tests.helpers import astwalk
from tests.helpers.settle import mounted, settled
from tests.helpers.tui_screens import evolve_screen

BAD_KNOB = '[evolution]\nlookback_runs = "many"\n'
BAD_DOCUMENT = "[evolution\nlookback_runs = 5\n"
GOOD_KNOB = "[evolution]\nlookback_runs = 5\n"


# --- the domain rule, derived from kstrl/ rather than listed --------------

#: The base ``raise_if_defect`` draws its line at.
_DOMAIN_BASE = "RuntimeError"

#: Every node in ``kstrl/`` that writes the identifier ``RuntimeError``:
#: the net under the walk below, enumerating no node types, since a
#: class cannot subclass what its module never spells. Rows that are not
#: a class base are bare raises and one ``isinstance``, pinned anyway
#: because separating them by shape is the guessing #324 costs.
EXPECTED_RUNTIMEERROR_SPELLINGS: dict[str, int] = {
    "autonomy.py": 1,
    "config_preflight.py": 2,  # the rule itself, and its docstring
    "contract.py": 1,
    "decisions.py": 1,  # DecisionRegisterError, added by #332
    "decompose.py": 1,
    "factory.py": 3,  # one subclass, two bare raises
    "git.py": 1,
    "inbox.py": 1,
    "intake_github.py": 1,
    "pr.py": 4,  # no subclass: four bare raises
    "serve.py": 1,
    # One since #232: ControlLockedError and ControlUnavailableError now
    # derive from a shared ControlStateError, which is the only class in
    # the module that spells RuntimeError. They are still kstrl's, so
    # raise_if_defect still treats them as operator input, and the walk
    # below follows them by subclass rather than by spelling.
    "statedir.py": 1,
    "workqueue.py": 1,
}


def _domain_subclass_names(tree: ast.Module, module: str) -> list[str]:
    """The classes in one module that derive DIRECTLY from RuntimeError.

    The base is resolved through the module's own imports before its
    last segment is read, so ``class X(builtins.RuntimeError)`` and an
    aliased import both count; matching ``ast.Name`` alone, which is
    what this walk did until #324, missed both.
    """
    table = astwalk.bindings(tree, module=module)
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for base in node.bases
        if (table.resolve(base) or astwalk.dotted(base) or "").rsplit(".", 1)[-1] == _DOMAIN_BASE
    ]


def _kstrl_domain_errors() -> list[type[BaseException]]:
    """Every ``RuntimeError`` subclass ``kstrl/`` declares, imported.

    TRANSITIVELY, since #232 gave ``statedir`` a shared
    ``ControlStateError`` base: the AST walk above finds the classes that
    SPELL ``RuntimeError``, which is the census this file pins, and the
    two that now derive from one of those would otherwise have dropped
    out of the behaviour check below without any of them changing what
    ``raise_if_defect`` does with them. ``__subclasses__`` is exact here
    because the comprehension has already imported every module in the
    package.
    """
    direct = [
        getattr(importlib.import_module(astwalk.module_name(path)), name)
        for path in astwalk.package_sources()
        for name in _domain_subclass_names(astwalk.parsed(path), astwalk.module_name(path))
    ]
    seen: dict[str, type[BaseException]] = {}
    pending = list(direct)
    while pending:
        cls = pending.pop()
        key = f"{cls.__module__}.{cls.__qualname__}"
        if key in seen:
            continue
        seen[key] = cls
        pending.extend(
            sub
            for sub in cls.__subclasses__()
            if sub.__module__.split(".")[0] == "kstrl"  # ours, not a dependency's
        )
    return list(seen.values())


class _Harness(App[None]):
    """A bare app: no run_context, so the env sweep is allowed."""

    def compose(self) -> ComposeResult:
        yield from ()


def _banner_text(screen: Screen[Any]) -> str:
    return str(screen.query_one(ConfigProblemBanner).render())


@asynccontextmanager
async def _inbox(tmp_path: Path) -> AsyncIterator[tuple[InboxScreen, Pilot[None]]]:
    """The inbox screen open on ``tmp_path``.

    This screen mounts its table directly, with no tab panes to wait
    for, so there is one condition rather than the evolve screen's
    three: its own on_mount adds the four columns and THEN calls
    ``action_refresh``, in one synchronous call. A poll runs only
    between messages, so a table that has columns is a screen whose
    on_mount has returned - the list and the config-problem banner are
    both drawn. It is weaker than anything a caller asserts: it says
    the screen finished loading, not what it loaded.
    """
    app = _Harness()
    async with app.run_test() as pilot:
        await app.push_screen(InboxScreen(tmp_path))
        table = await mounted(pilot, lambda: app.screen, "#inbox-table")
        await settled(
            pilot,
            lambda: cast(DataTable, table).columns,
            what="the inbox screen's on_mount to draw the list",
        )
        screen = app.screen
        assert isinstance(screen, InboxScreen)
        yield screen, pilot


# --------------------------------------------------------------------------
# Why the screen is reachable at all
# --------------------------------------------------------------------------
def test_the_shell_opens_on_the_value_that_crashes_the_screen(tmp_path: Path) -> None:
    """The entry check warns for [evolution] and lets the shell open.

    This is the whole reason #289 is not covered by #272: the seam ran,
    and classified this section as degrading.
    """
    (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
    warnings: list[str] = []
    preflight_config(tmp_path, warn=warnings.append)
    assert len(warnings) == 1
    assert "[evolution]" in warnings[0]
    assert "continuing without it" in warnings[0]


def test_ks_evolve_still_stops_on_the_same_file(tmp_path: Path) -> None:
    """The command that IS the journal keeps its fatal classification."""
    (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        preflight_config(tmp_path, warn=lambda _m: None, required=frozenset({"evolution"}))
    assert "[evolution]" in str(caught.value)


# --------------------------------------------------------------------------
# The evolve screen
# --------------------------------------------------------------------------
class TestEvolveScreen:
    async def test_a_bad_knob_is_named_instead_of_raised(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            text = _banner_text(screen)
            assert "configuration unreadable" in text
            # The section name survives: it is bracketed, and Rich reads
            # a bracketed token as markup and deletes it, so this pins
            # the banner rendering a Text rather than a str.
            assert "[evolution]" in text
            assert "invalid literal for int() with base 10: 'many'" in text
            assert "lookback_runs" in text
            assert screen.query_one(ConfigProblemBanner).display is True

    async def test_the_tables_are_empty_and_the_emptiness_is_explained(
        self,
        tmp_path: Path,
    ) -> None:
        """Not a silent degrade: no rows, and a line saying why."""
        (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            assert screen.query_one("#patterns-table", DataTable).row_count == 0
            assert screen.query_one("#trends-table", DataTable).row_count == 0
            assert "[evolution]" in _banner_text(screen)

    async def test_a_malformed_document_is_named_too(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(BAD_DOCUMENT, encoding="utf-8")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            text = _banner_text(screen)
            assert "Invalid TOML" in text
            assert "line 1" in text

    async def test_the_environment_variable_is_named_when_it_is_the_cause(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "lots")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            assert "set by KSTRL_EVOLUTION_LOOKBACK_RUNS=lots" in _banner_text(screen)

    async def test_the_banner_is_hidden_on_a_config_that_resolves(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(GOOD_KNOB, encoding="utf-8")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            assert screen.query_one(ConfigProblemBanner).display is False
            assert screen.query_one(ConfigProblemBanner).problem is None

    async def test_reload_clears_the_banner_once_the_file_is_repaired(
        self,
        tmp_path: Path,
    ) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(BAD_KNOB, encoding="utf-8")
        async with evolve_screen(tmp_path) as (screen, _pilot):
            assert screen.query_one(ConfigProblemBanner).display is True
            toml.write_text(GOOD_KNOB, encoding="utf-8")
            # action_reload is a direct call and reload is synchronous
            # all the way down to the banner's own `show`, so the
            # display below is already final when it returns.
            screen.action_reload()
            assert screen.query_one(ConfigProblemBanner).display is False


# --------------------------------------------------------------------------
# The inbox screen: the same shape, found by the survey #289 asked for
# --------------------------------------------------------------------------
class TestInboxScreen:
    async def test_a_malformed_document_is_named_instead_of_raised(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(BAD_DOCUMENT, encoding="utf-8")
        async with _inbox(tmp_path) as (screen, _pilot):
            text = _banner_text(screen)
            assert "configuration unreadable" in text
            assert "[inbox]" in text
            assert "Invalid TOML" in text

    async def test_it_never_claims_the_inbox_is_clear(self, tmp_path: Path) -> None:
        """An item IS waiting; the config is what cannot be read.

        "Inbox clear: nothing is waiting on you." from an empty list
        that only means the config failed is the silent degrade #289
        rules out, and here it would be false as well as silent.
        """
        Inbox(tmp_path, InboxConfig()).add(
            ItemKind.POLICY_EXCEPTION,
            "comp-a: denied path",
            component="comp-a",
            dedupe_key="p1",
        )
        (tmp_path / "kstrl.toml").write_text(BAD_DOCUMENT, encoding="utf-8")
        async with _inbox(tmp_path) as (screen, _pilot):
            assert screen._items == []
            detail = str(screen.query_one("#inbox-detail", Static).render())
            assert "Inbox clear" not in detail

    async def test_a_decision_taken_after_the_file_broke_redraws(
        self,
        tmp_path: Path,
    ) -> None:
        """The list was drawn from a good file; the file then broke."""
        Inbox(tmp_path, InboxConfig()).add(
            ItemKind.POLICY_EXCEPTION,
            "comp-a: denied path",
            component="comp-a",
            dedupe_key="p1",
        )
        async with _inbox(tmp_path) as (screen, _pilot):
            assert len(screen._items) == 1
            (tmp_path / "kstrl.toml").write_text(BAD_DOCUMENT, encoding="utf-8")
            # action_approve is a direct call and _decide is
            # synchronous through to the redraw, so the list and the
            # banner below are already final when it returns.
            screen.action_approve()
            assert screen._items == []
            assert "Invalid TOML" in _banner_text(screen)
            # The decision did NOT land: refusing is the honest answer
            # when the file that configures the log will not parse.
            assert len(Inbox(tmp_path, InboxConfig()).open_items()) == 1


# --------------------------------------------------------------------------
# The shared pattern
# --------------------------------------------------------------------------
class TestSharedGuard:
    def test_the_screen_says_exactly_what_the_command_says(self, tmp_path: Path) -> None:
        """The point of #289: one file, one wording, two surfaces."""
        (tmp_path / "kstrl.toml").write_text(BAD_KNOB, encoding="utf-8")
        from_cli = collect_config_problems(
            tmp_path,
            warn=lambda _m: None,
            required=frozenset({"evolution"}),
        )
        _config, from_screen = load_or_report(
            EvolutionConfig.load,
            tmp_path,
            blame_env=True,
        )
        assert from_cli == [from_screen]

    def test_env_blame_is_skipped_while_a_run_is_in_flight(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Measuring the blame clears os.environ process-wide.

        A launched home-shell session runs the factory on another
        thread of THIS process and its subprocesses inherit the
        environment, so the variable's name is given up rather than the
        run corrupted. Everything else in the line survives.
        """
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "lots")
        _config, problem = load_or_report(EvolutionConfig.load, tmp_path, blame_env=False)
        assert problem is not None
        assert "[evolution]" in problem
        assert "invalid literal for int() with base 10: 'lots'" in problem
        assert "set by" not in problem

        _again, blamed = load_or_report(EvolutionConfig.load, tmp_path, blame_env=True)
        assert blamed is not None
        assert "set by KSTRL_EVOLUTION_LOOKBACK_RUNS=lots" in blamed

    def test_env_scrub_is_safe_reads_the_launched_session(self) -> None:
        class _Handle:
            def __init__(self, done: bool) -> None:
                self._done = done

            def done(self) -> bool:
                return self._done

        class _Ctx:
            def __init__(self, handle: object | None) -> None:
                self.handle = handle

        class _App:
            def __init__(self, ctx: object | None) -> None:
                self.run_context = ctx

        assert env_scrub_is_safe(object()) is True  # no attribute at all
        assert env_scrub_is_safe(_App(None)) is True
        assert env_scrub_is_safe(_App(_Ctx(None))) is True
        assert env_scrub_is_safe(_App(_Ctx(_Handle(True)))) is True
        assert env_scrub_is_safe(_App(_Ctx(_Handle(False)))) is False

    def test_a_clean_config_returns_the_object_and_no_problem(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[evolution]\nlookback_runs = 3\n", encoding="utf-8")
        config, problem = load_or_report(EvolutionConfig.load, tmp_path, blame_env=True)
        assert problem is None
        assert config is not None
        assert config.lookback_runs == 3

    def test_an_unenrolled_loader_is_a_defect_not_a_message(self, tmp_path: Path) -> None:
        """The label comes from config_sections(), so a screen cannot
        invent a section the entry check does not know.

        Raised on the FAILURE path only: the lookup moved into the
        except branch because `config_sections()` costs a measured
        6.2 ms on its first call and the happy path never needs a
        label. An unenrolled loader that resolves cleanly has nothing
        to mislabel.
        """

        def _rejecting_unenrolled_loader(_root: Path) -> int:
            raise ValueError("nope")

        with pytest.raises(LookupError):
            load_or_report(_rejecting_unenrolled_loader, tmp_path, blame_env=False)

        def _clean_unenrolled_loader(_root: Path) -> int:
            return 1

        assert load_or_report(_clean_unenrolled_loader, tmp_path, blame_env=False) == (1, None)

    def test_a_bare_runtimeerror_is_re_raised_not_blamed_on_the_operator(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round-one review, finding 10.

        SURFACE_REJECTIONS names RuntimeError for the domain errors
        that DERIVE from it, which are operator input. A bare one is a
        kstrl defect, and rendering it as "configuration unreadable"
        would blame the operator's file and eat the traceback. This is
        the same line EvolutionConfig.load_or_none draws.
        """
        import kstrl.evolution

        def _explode(root_dir: Path | None = None) -> None:
            raise RuntimeError("journal config exploded")

        monkeypatch.setattr(kstrl.evolution.EvolutionConfig, "load", _explode)
        with pytest.raises(RuntimeError, match="journal config exploded"):
            load_or_report(EvolutionConfig.load, tmp_path, blame_env=False)

    @pytest.mark.parametrize(
        "defect",
        [
            RuntimeError("bare"),
            NotImplementedError("abstract loader"),
            RecursionError("cycle in the config graph"),
        ],
        ids=["bare", "not_implemented", "recursion"],
    )
    def test_every_runtimeerror_kstrl_did_not_define_is_re_raised(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        defect: RuntimeError,
    ) -> None:
        """Round-two review, finding 2.

        The first cut of the test above wrote ``type(exc) is
        RuntimeError``, which is true only of a BARE one.
        NotImplementedError and RecursionError are direct RuntimeError
        subclasses and unambiguously defects, and both were reported to
        the operator as "configuration unreadable" with the traceback
        eaten. RecursionError is the one that hurts: a cycle in the
        config graph would have been rendered as their broken file.
        """
        import kstrl.evolution

        def _explode(root_dir: Path | None = None) -> None:
            raise defect

        monkeypatch.setattr(kstrl.evolution.EvolutionConfig, "load", _explode)
        with pytest.raises(type(defect)):
            load_or_report(EvolutionConfig.load, tmp_path, blame_env=False)

    def test_the_domain_rule_is_derived_from_kstrl_not_listed(self) -> None:
        """No ledger to go stale.

        The reviewer's suggested fix was a tuple of the four domain
        errors named in REJECTIONS' docstring. kstrl defines TEN
        RuntimeError subclasses, so that tuple would have been wrong on
        the day it was written and staler with each new one. The rule
        asks a derivable question instead - did kstrl define this class
        - and this test walks the package to check the answer holds for
        every one of them.
        """
        subclasses = _kstrl_domain_errors()
        assert len(subclasses) >= 10, subclasses
        # Closed under subclassing within kstrl: a domain error that
        # derives from another domain error rather than from
        # RuntimeError directly is still the operator's input, and
        # #232 added the first two of those. Derived, not listed.
        for cls in subclasses:
            for descendant in cls.__subclasses__():
                if descendant.__module__.split(".")[0] == "kstrl":
                    assert descendant in subclasses, descendant
        for cls in subclasses:
            raise_if_defect(cls("operator input"))  # must not raise
        for defect in (RuntimeError, NotImplementedError, RecursionError):
            with pytest.raises(defect):
                raise_if_defect(defect("ours"))
        # And nothing outside RuntimeError is this rule's business.
        raise_if_defect(ValueError("a knob"))
        raise_if_defect(OSError("a file"))

    def test_the_walk_sees_a_base_it_has_to_resolve_to_recognise(self) -> None:
        """The control: an empty subclass list and a switched-off
        matcher read the same, and until #324 this walk matched a bare
        ``ast.Name``, so both spellings below were invisible."""
        dotted = astwalk.parse("class X(builtins.RuntimeError): pass\n")
        aliased = astwalk.parse("from builtins import RuntimeError as RE\nclass Y(RE): pass\n")
        assert _domain_subclass_names(dotted, "kstrl.made_up") == ["X"]
        assert _domain_subclass_names(aliased, "kstrl.made_up") == ["Y"]
        # ... and an unrelated base is still not this rule's business.
        assert _domain_subclass_names(astwalk.parse("class Z(ValueError): pass\n"), "m") == []

    def test_every_module_that_spells_the_domain_base_is_pinned(self) -> None:
        """The net: no class subclasses what its module never spells."""
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=astwalk.spells(_DOMAIN_BASE),
            expected=EXPECTED_RUNTIMEERROR_SPELLINGS,
            control="class X(RuntimeError):\n    pass\n",
            message="the set of places kstrl/ spells RuntimeError changed. Add the row.",
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_subclass_of_a_subclass_is_a_disclosed_limit(self) -> None:
        """``class Deeper(ServeError)`` is not seen, and need not be.

        Direct bases only. The bound: the rule asks
        ``type(exc).__module__``, so a transitive subclass declared in
        ``kstrl/`` is operator input at run time either way. Coverage is
        what is lost, not correctness. Measured: zero exist today.
        """
        astwalk.blind_spot(
            lambda source: _domain_subclass_names(astwalk.parse(source), "kstrl.made_up"),
            "from kstrl.serve import ServeError\nclass Deeper(ServeError):\n    pass\n",
        )

    def test_a_domain_runtimeerror_is_still_reported(self, tmp_path: Path) -> None:
        """The other side of the same line: ServeError and friends are
        operator input and must still reach the banner."""
        from kstrl.serve import ServeConfig

        (tmp_path / "kstrl.toml").write_text(
            "[serve]\nmax_consecutive_poison = 0\n",
            encoding="utf-8",
        )
        _config, problem = load_or_report(ServeConfig.load, tmp_path, blame_env=False)
        assert problem is not None
        assert "[serve]" in problem

    @pytest.mark.skipif(os.name == "nt" or os.getuid() == 0, reason="root reads a 0000 file")
    def test_an_unreadable_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        """OSError is in scope for a surface, not for the entry check.

        The entry check reads the document itself before any loader
        runs; a screen re-reading minutes later has no such pass in
        front of it, and a chmod between two refreshes lands here.
        """
        toml = tmp_path / "kstrl.toml"
        toml.write_text(GOOD_KNOB, encoding="utf-8")
        toml.chmod(0o000)
        try:
            _config, problem = load_or_report(
                EvolutionConfig.load,
                tmp_path,
                blame_env=False,
            )
        finally:
            toml.chmod(stat.S_IRUSR | stat.S_IWUSR)
        assert problem is not None
        assert "[evolution]" in problem
        assert "Permission denied" in problem
