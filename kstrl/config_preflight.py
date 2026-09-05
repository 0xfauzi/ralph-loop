"""One configuration check, at command entry, before anything is spent.

kstrl.toml used to be parsed lazily, by whichever config dataclass first
needed its section, at whatever point in the run that class happened to
be constructed. A typo therefore did not fail at startup: it failed at
the first loader that reached the bad section. On the decompose path one
of those loaders is ``LinearConfig.load``, which runs AFTER the
architect agent has been invoked and paid for - measured at 119 to 210
seconds against a frontier model on a real spec - so a syntax error in
kstrl.toml, or a bad value in a section the architect never needed,
spent the architect and then aborted (#272).

The blast radius of a typo therefore depended on which section it was in
and which command was run. Nobody chose that property. This module
replaces it with one: EVERY section is resolved once, at command entry,
before the command body constructs anything.

FATAL VERSUS DEGRADING
----------------------
Not every section can honestly be degraded past, and the difference is
not about how likely the failure is - it is about what continuing would
mean.

- ``[evolution]`` configures an optional AUDIT TRAIL. Losing the journal
  degrades the record and nothing else, so continuing without it is
  honest, and four mid-run call sites already do exactly that through
  ``EvolutionConfig.load_or_none``. This preflight makes the same call
  earlier and louder: the warning arrives at startup instead of in the
  middle of paid work.
- Every other section configures a GATE, a BUDGET, a BOUNDARY or a
  DESTINATION. Substituting defaults for a verify command, a security
  threshold, a policy envelope or a cost ceiling the operator
  configured is a semantic substitution: the run proceeds, reports
  success, and was measured by something other than what was asked for.
  CLAUDE.md names that failure directly ("No silent semantic
  substitution. Retry identically or surface the failure"), so these
  fail the command instead.

WHY THE LOADERS THEMSELVES, NOT A SCHEMA
----------------------------------------
The check calls each dataclass's own ``load(root_dir)``. That is the
same code the run will use later, so the preflight cannot drift from
what is actually enforced - the drift failure this codebase has already
recorded twice (see ``config.reconcile_progress_config``). It also means
ENV coercion is covered for free: ``load`` overlays env on top of toml,
so ``KSTRL_SECURITY_TIMEOUT=many`` is rejected here by the same
``float()`` that would have rejected it mid-run. A toml-only preflight
would have missed both of the failures measured on #272.

The per-section ``from_env()`` / ``load(root_dir)`` convention is
untouched: this module is a caller of it, not a replacement for it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from kstrl.config import (
    ConfigError,
    load_toml_document,
    load_toml_section,
    resolve_config_file,
    toml_parse_scope,
)
from kstrl.config_report import environ_lock, scrubbed_environ

#: Exceptions a loader raises for input the operator has to fix, and the
#: complete set of them: these loaders read a file and coerce values, so
#: anything outside this tuple is a defect in kstrl and keeps its
#: traceback rather than being reported as the operator's fault.
#:
#: ``ValueError`` is the house type (``ConfigError``,
#: ``PolicyConfigError`` and ``BudgetConfigError`` all derive from it).
#: ``BudgetConfigError`` is deliberately collected like any other and
#: not re-raised: re-raising it abandoned the traversal at ``[factory]``,
#: which is second in the list, so one bad ceiling hid every later
#: section and the operator fixed the ceiling only to meet ``[verify]``
#: on the next run. ``_KstrlGroup.invoke`` still renders it for the
#: paths that raise it outside this check, such as ``--max-cost-usd``.
#: ``TypeError`` joins it because ``float(["600"])`` - a toml array where
#: a number belongs - raises that instead. ``RuntimeError`` is there for
#: the domain errors that derive from IT: ``ServeError`` rejects
#: ``[serve] max_consecutive_poison = 0`` that way, and ``QueueError``,
#: ``InboxError`` and ``IntakeError`` are its siblings.
REJECTIONS = (ValueError, TypeError, RuntimeError)

#: :data:`REJECTIONS` plus the read failure a long-lived surface has to
#: survive, for anything loading config AFTER command entry.
#:
#: ``OSError`` carries two unrelated rationales and both are why this
#: cannot be fixed by normalizing it inside ``load_toml_document``.
#: First, the entry check reads the document once itself and turns an
#: unreadable kstrl.toml into a ``ConfigError`` before any loader runs;
#: a screen re-reading the file minutes later has no such pass in front
#: of it, and a ``chmod`` between two refreshes raises ``OSError``
#: straight out of ``load_toml_section``. Second, a loader may read a
#: file that is not kstrl.toml at all: ``resolve_verify_commands``
#: reads the project's pyproject.toml, so ``init_wizard._detected_text``
#: needs ``OSError`` for a document this module never opens.
SURFACE_REJECTIONS = (*REJECTIONS, OSError)


def raise_if_defect(exc: BaseException) -> None:
    """Re-raise ``exc`` when it is kstrl's bug, not the operator's file.

    :data:`REJECTIONS` names ``RuntimeError`` only for the domain errors
    that DERIVE from it - ``ServeError`` for ``[serve]
    max_consecutive_poison = 0``, and its ``QueueError``, ``InboxError``
    and ``IntakeError`` siblings - which are operator input and are
    reported as such. Everything else that arrives as a ``RuntimeError``
    is ours: reporting it as "configuration unreadable" blames the
    operator for our defect and eats the traceback that would locate it.

    The test is DERIVED, not a list. The first cut wrote ``type(exc) is
    RuntimeError``, which is true only of a bare one, so
    ``NotImplementedError`` and ``RecursionError`` - both direct
    ``RuntimeError`` subclasses, both unambiguously defects - were
    reported as the operator's broken file. A hand-written tuple of the
    four domain errors would have fixed those two and gone stale the
    next time a fifth is added, which is the failure mode this codebase
    keeps recording. So the question asked is "did kstrl define this
    class": ten ``RuntimeError`` subclasses live in ``kstrl/`` today and
    every one of them is a condition we chose to raise, while
    ``builtins`` and any dependency's ``RuntimeError`` is not something
    we modelled and so not something we can honestly blame a file for.
    ``tests/test_tui_config_guard.py`` pins both halves.

    ``RecursionError`` is the one that needs an argument rather than a
    rule, and the argument is layering rather than inspection (#323). A
    kstrl.toml with 600 nested arrays exhausts the stack inside
    tomllib's recursive descent, and that is the operator's file, not a
    defect of ours. It never arrives here. There are four tomllib parses
    in ``kstrl/`` - ``tests/test_toml_readers.py`` pins the census - and
    every one of them ends on a bare ``except Exception``:
    ``config.load_toml_document`` re-raises ``ConfigError`` naming the
    path, and the pyproject.toml and ruff.toml readers in ``verify`` and
    ``feedforward`` fall back to a default. So a ``RecursionError`` that
    does reach this function is a cycle in kstrl's own code, and the
    traceback this re-raise keeps is what locates it.

    Inspecting the exception could not have settled it anyway. Measured
    on 3.12.8 and 3.13.2, both directions give
    ``builtins.RecursionError`` with ``str(exc)`` "maximum recursion
    depth exceeded" and no attribute of its own; only the frames differ,
    and by the time they could be read the boundary has already
    answered. ``tests/test_recursion_provenance.py`` pins both
    directions, at :func:`preflight_config` and at the ``ks status``
    seam.
    """
    if isinstance(exc, RuntimeError) and type(exc).__module__.split(".")[0] != "kstrl":
        raise exc


T = TypeVar("T")


@dataclass(frozen=True)
class ConfigSection:
    """One kstrl.toml section (or group of them) and how it is loaded.

    ``sections`` is a tuple because ``KstrlConfig`` fans out over five
    toml tables; every other entry names exactly one. ``fatal`` records
    the classification argued in the module docstring.
    """

    sections: tuple[str, ...]
    loader: Callable[[Path], Any]
    #: False for a section whose failure degrades rather than stops the
    #: command. Exactly one entry sets it; see the module docstring.
    fatal: bool = True

    @property
    def label(self) -> str:
        return "/".join(f"[{name}]" for name in self.sections)


def config_sections() -> list[ConfigSection]:
    """Every configuration section kstrl reads, with its loader.

    Imports are deferred the way ``config_report._phase_sections`` defers
    them, and the reason is ordering, not latency: this module is
    imported by ``kstrl.cli``, which several of these import from.
    Measured on this tree, only four of the twenty-two are new work
    (evolution, intake_github with workqueue, and serve; the rest arrive
    with ``kstrl.cli``), costing about 7 ms warm on a 151 ms process.

    ``tests/test_config_preflight.py`` walks ``kstrl/`` for config
    dataclasses and fails if one is missing from this list, so a section
    added later cannot quietly go unchecked.
    """
    from kstrl.adequacy import AdequacyConfig
    from kstrl.autonomy import AutonomyConfig
    from kstrl.breaker import BreakerConfig
    from kstrl.config import KstrlConfig
    from kstrl.contract import ContractConfig
    from kstrl.divergence import DivergenceConfig
    from kstrl.evolution import EvolutionConfig
    from kstrl.factory import FactoryConfig
    from kstrl.feedforward import FeedforwardConfig
    from kstrl.fixtures import FixturesConfig
    from kstrl.inbox import InboxConfig
    from kstrl.intake_github import GitHubIntakeConfig
    from kstrl.knowledge import KnowledgeConfig
    from kstrl.linear import LinearConfig
    from kstrl.observability import NotifyConfig
    from kstrl.policy import PolicyConfig
    from kstrl.sandbox import SandboxConfig
    from kstrl.security import SecurityConfig
    from kstrl.serve import ServeConfig
    from kstrl.timeout import TimeoutConfig
    from kstrl.verify import VerifyConfig
    from kstrl.workqueue import QueueConfig

    return [
        ConfigSection(("agent", "run", "paths", "git", "ui"), KstrlConfig.load),
        ConfigSection(("factory",), FactoryConfig.load),
        ConfigSection(("verify",), VerifyConfig.load),
        ConfigSection(("security",), SecurityConfig.load),
        ConfigSection(("contract",), ContractConfig.load),
        ConfigSection(("adequacy",), AdequacyConfig.load),
        ConfigSection(("policy",), PolicyConfig.load),
        ConfigSection(("autonomy",), AutonomyConfig.load),
        ConfigSection(("divergence",), DivergenceConfig.load),
        ConfigSection(("breaker",), BreakerConfig.load),
        ConfigSection(("sandbox",), SandboxConfig.load),
        ConfigSection(("timeout",), TimeoutConfig.load),
        ConfigSection(("feedforward",), FeedforwardConfig.load),
        ConfigSection(("knowledge",), KnowledgeConfig.load),
        ConfigSection(("fixtures",), FixturesConfig.load),
        ConfigSection(("queue",), QueueConfig.load),
        ConfigSection(("inbox",), InboxConfig.load),
        ConfigSection(("intake_github",), GitHubIntakeConfig.load),
        ConfigSection(("serve",), ServeConfig.load),
        ConfigSection(("notify",), NotifyConfig.load),
        ConfigSection(("linear",), LinearConfig.load),
        ConfigSection(("evolution",), EvolutionConfig.load, fatal=False),
    ]


def preflight_config(
    root_dir: Path,
    warn: Callable[[str], None],
    *,
    required: frozenset[str] = frozenset(),
) -> None:
    """Resolve every configuration section, or say exactly what to fix.

    Raises :class:`ConfigError` naming the section, the offending input
    and the loader's own message. Degrading sections (see the module
    docstring) go to ``warn`` instead and the command continues.

    ``required`` promotes named sections to fatal for THIS caller. It
    exists because "degrading" means "an audit trail attached to work
    that is about something else": ``ks evolve`` IS the journal, so it
    passes ``{"evolution"}`` and gets the error line, with the key and
    the offending value, instead of a warning followed two lines later
    by the traceback the warning promised would not come. A command
    that is ABOUT a section declares that here rather than remembering
    its own guard.
    """
    problems = collect_config_problems(root_dir, warn, required=required)
    if problems:
        raise ConfigError(
            "configuration rejected before anything was started; "
            "fix it and run again:\n  " + "\n  ".join(problems)
        )


def collect_config_problems(
    root_dir: Path,
    warn: Callable[[str], None],
    *,
    required: frozenset[str] = frozenset(),
) -> list[str]:
    """:func:`preflight_config`, but returning the problems instead of
    raising on them.

    Split out for ``ks config show``, which has to REPORT every rejected
    section next to the rows it could resolve rather than stop at the
    first one. A raising check and a reporting one reading different
    section lists is exactly the drift this module exists to prevent, so
    there is one traversal and the raise sits on top of it.

    A malformed document still raises: no section can be resolved when
    the file will not parse, so there is nothing to report beside.
    """
    toml_path = resolve_config_file(root_dir)
    problems: list[str] = []
    # One parse of the file for the whole check, blame helpers included.
    # Without the scope the 22 loaders lex the same bytes 22 times:
    # measured on the shipped 21 KB kstrl.toml.example, this check costs
    # 9.4 ms without it and 0.6 ms with it.
    with toml_parse_scope():
        if toml_path.exists():
            # The document first: a file that will not parse breaks
            # every section, and one line naming the fault beats 22
            # saying so. TWO faults reach here, not one - a syntax
            # error and a non-utf-8 byte (#318) - and only the first
            # carries a line and column. Both arrive as ``ConfigError``
            # and pass straight through this ``except`` on purpose.
            try:
                load_toml_document(toml_path)
            except OSError as exc:
                raise ConfigError(f"{toml_path} could not be read: {exc}") from exc

        for section in config_sections():
            try:
                section.loader(root_dir)
            except REJECTIONS as exc:
                # Same rule as every other catcher of this tuple: a
                # RuntimeError kstrl did not define is our defect, and
                # listing it under "configuration problems" blames the
                # operator's file for it. This is the seam all three
                # reporting surfaces route through, so the hole would
                # have been one call deep from each of them.
                raise_if_defect(exc)
                detail = _detail(section, toml_path, root_dir, exc, blame_env=True)
                if section.fatal or not required.isdisjoint(section.sections):
                    problems.append(detail)
                else:
                    warn(f"{detail} - continuing without it")
    return problems


def config_problem_lines(
    root_dir: Path,
    *,
    warn: Callable[[str], None],
) -> list[str]:
    """Every line the entry check would print for ``root_dir``.

    :func:`collect_config_problems` with the one failure it does not
    return folded back in: a document that will not parse raises, and
    the answer to "what is wrong with this configuration" is then that
    parse error and nothing else, because no section could be resolved
    behind it.

    Split out because ``ks config show`` and the TUI config screen are
    the two surfaces whose whole job is explaining a broken config, and
    they were carrying a copy of this each. The copies had already
    drifted in their handling of the empty case; a third surface would
    have made it three. Callers supply their own ``warn`` because one
    prints to stderr and the other must stay silent on a screen.
    """
    try:
        return collect_config_problems(root_dir, warn=warn)
    except SURFACE_REJECTIONS as exc:
        raise_if_defect(exc)
        return [str(exc)]


def load_or_report(
    loader: Callable[[Path], T],
    root_dir: Path,
    *,
    blame_env: bool,
) -> tuple[T | None, str | None]:
    """One section, resolved, or the line this module would have printed.

    Exactly one of the pair is ever None. For a long-lived surface that
    loads a section AFTER command entry - a TUI screen the home shell
    opens - and so has no seam in front of it to fail on its behalf.
    The message is produced by the same ``_detail`` the entry check
    uses, so the same broken file reads the same way on both surfaces,
    which is what #289 was about; ``tests/test_tui_config_guard.py``
    pins the two strings equal rather than trusting that.

    ``blame_env`` is required rather than defaulted because getting it
    wrong is not a cosmetic mistake. Naming the offending variable means
    measuring it (``_blamed_env_var``), and measuring it means clearing
    ``os.environ``, which is PROCESS-WIDE. At command entry nothing else
    of ours is running; on a screen a launched run may be on another
    thread, spawning subprocesses that inherit the environment. Pass
    False there and the line keeps everything except the variable's
    name. ``kstrl.tui.config_guard`` is where that decision is made.

    Wider than :data:`REJECTIONS` by ``OSError``: see
    :data:`SURFACE_REJECTIONS`.
    """
    # Scoped per call, never across calls: a screen's refresh action
    # exists to see the file as it is NOW (see ``toml_parse_scope``).
    with toml_parse_scope():
        try:
            return loader(root_dir), None
        except SURFACE_REJECTIONS as exc:
            # A RuntimeError kstrl did not define is a defect in kstrl,
            # not the operator's file. This is the same line
            # EvolutionConfig.load_or_none draws, and it is drawn here
            # too because that method's reason (a widening can only ever
            # swallow a defect) is about the exception, not the site.
            raise_if_defect(exc)
            # Looked up HERE, not before the try: `_section_for` calls
            # `config_sections()`, whose 22 deferred imports cost a
            # measured 6.2 ms on their first call in a process that has
            # imported kstrl.tui.app, and that first call otherwise
            # lands on the Textual event loop inside on_mount even when
            # kstrl.toml is perfectly valid (4.7 us warm). Only the
            # failure path needs a label, so only it pays. An
            # unenrolled loader still raises LookupError, on the path
            # that would have had to name it.
            return None, _detail(
                _section_for(loader),
                resolve_config_file(root_dir),
                root_dir,
                exc,
                blame_env=blame_env,
            )


def _section_for(loader: Callable[[Path], Any]) -> ConfigSection:
    """The registry entry for ``loader``.

    A caller passes the loader rather than a section name so that it
    cannot label itself with a section the entry check does not know,
    and so that the label and the blame helpers come from the one table
    :func:`config_sections` already keeps complete.

    Compared with ``==``, not ``is``: ``EvolutionConfig.load`` is a bound
    classmethod and Python builds a fresh object on every attribute
    access, so ``is`` is False even for the same method, while ``==``
    compares ``__func__`` and ``__self__``.
    """
    for section in config_sections():
        if section.loader == loader:
            return section
    raise LookupError(f"{loader!r} is not a loader config_sections() names")


def _detail(
    section: ConfigSection,
    toml_path: Path,
    root_dir: Path,
    exc: Exception,
    *,
    blame_env: bool,
) -> str:
    """One line: which section, what the loader said, and which input.

    The environment is asked FIRST because the environment wins: with
    the same bad value in both places, the variable is the one taking
    effect, so naming the file's key would send the operator to a line
    that changing does not help. ``blame_env`` False skips that question
    entirely rather than answering it unsafely; the caller that passes
    False says why (:func:`load_or_report`).
    """
    message = str(exc)
    # Two statements, not one expression: the environment-then-file
    # order is the rule the docstring above states, and an `or` split
    # across a ternary hides it.
    blamed = _blamed_env_var(section.loader, root_dir, message) if blame_env else None
    blamed = blamed or _blamed_toml_value(section.sections, toml_path, message)
    line = f"{section.label} {message}"
    return f"{line} ({blamed})" if blamed else line


def _blamed_env_var(
    loader: Callable[[Path], Any],
    root_dir: Path,
    message: str,
) -> str | None:
    """The environment variable whose REMOVAL makes this loader accept
    the configuration, if EXACTLY ONE does.

    Measured, not guessed: a variable is named only when taking it out
    of the environment demonstrably fixes the load. The sweep runs to
    the end and reports nothing when two variables each fix it on their
    own, because then neither is "the one to change" and naming the
    alphabetically first contradicts the message beside it. Reproduced
    on ``[linear]`` with ``KSTRL_LINEAR_ENABLED=1`` and an empty
    ``KSTRL_LINEAR_TEAM_ID``: removing either satisfies the loader, and
    the earlier code blamed ENABLED while the message told the operator
    to set TEAM_ID.

    An EMPTY environment is tried first, and a loader that still fails
    there ends the search: the file is at fault, and no variable can be.
    That gate keeps a file fault at one extra load rather than one per
    variable. Measured on a file fault with the 21 KB example config,
    INSIDE the parse scope: 0.54 ms with the gate against 0.84 ms
    without at 83 variables, and 0.95 against 2.09 at 303. The saving is
    small and linear in the size of the environment, which is the honest
    reason to keep it: CI environments are the big ones.

    An earlier version of this docstring claimed 34.3 ms for the
    ungated sweep. That was measured before ``toml_parse_scope`` covered
    the blame helpers, when every one of those loads reparsed the file.
    It is off by 40x now, which is why it is written down here re-taken
    rather than carried forward.

    Runs whenever a section is REJECTED, which includes a degrading
    section on an otherwise successful command. Both are before the
    command body, which is the property that matters: mutating
    ``os.environ`` is PROCESS-WIDE, and here nothing has been built and
    no other thread of ours is alive. That is the constraint
    ``config_report.scrubbed_environ``, reused here, already documents.
    """
    # The lock spans the WHOLE sweep, not just the scrubbed_environ
    # block: the per-variable pops below mutate os.environ outside it,
    # and they are the longer window of the two.
    with environ_lock():
        return _blame_sweep(loader, root_dir, message)


def _blame_sweep(
    loader: Callable[[Path], Any],
    root_dir: Path,
    message: str,
) -> str | None:
    """:func:`_blamed_env_var`'s body, under the environment lock."""
    with scrubbed_environ():
        try:
            loader(root_dir)
        except Exception:
            return None

    blamed: str | None = None
    for name in sorted(os.environ):
        saved = os.environ.pop(name)
        try:
            loader(root_dir)
        except Exception:
            # Still broken without it, or broken differently: either way
            # this variable is not the one thing to change.
            continue
        else:
            if blamed is not None:
                # A second variable that fixes it on its own. Neither is
                # "the one to change", so the sweep stops here rather
                # than finishing to discard the answer.
                return None
            # The value is echoed only where the loader's own message
            # already quotes it. Nothing has decided that an arbitrary
            # environment value may be printed, so nothing prints one.
            blamed = f"set by {name}={saved}" if repr(saved) in message else f"set by {name}"
        finally:
            os.environ[name] = saved
    return blamed


def _blamed_toml_value(
    sections: tuple[str, ...],
    toml_path: Path,
    message: str,
) -> str | None:
    """The kstrl.toml key holding the value the loader complained about.

    Python's coercion errors quote the offending value verbatim ("could
    not convert string to float: 'many'"), so a key in these sections
    whose value reprs into that message is the one to look at. Reported
    only when exactly one key matches, and phrased as what the file
    says rather than as a diagnosis.
    """
    hits = []
    for name in sections:
        with suppress(*SURFACE_REJECTIONS):
            for key, value in load_toml_section(toml_path, name).items():
                if repr(value) in message:
                    hits.append(f"kstrl.toml has [{name}] {key} = {value!r}")
    if len(hits) == 1:
        return hits[0]
    return None
