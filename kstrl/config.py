"""Configuration handling for kstrl."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


def _parse_bool(value: str | None) -> bool:
    """Parse boolean from environment variable."""
    if value is None:
        return False
    return bool(re.match(r"^(1|true|yes)$", value.lower()))


def _parse_paths(value: str | None) -> list[str]:
    """Parse comma-separated paths, trimming whitespace."""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _resolve_path(value: str, root_dir: Path) -> Path:
    """Resolve a path string against root_dir if relative."""
    p = Path(value)
    if p.is_absolute():
        return p
    return root_dir / p


def relative_to_root(path: Path, root_dir: Path) -> str:
    """Render ``path`` relative to ``root_dir`` for use inside a
    per-component worktree. Falls back to the absolute path string when
    relativization fails (e.g. a path on a different mount)."""
    if not path.is_absolute():
        return str(path)
    try:
        return path.relative_to(root_dir).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(root_dir.resolve()).as_posix()
        except ValueError:
            return str(path)


# A FACTORY component's progress log lives NEXT TO that component's PRD.
# DECOMPOSE_PROMPT rule 12 tells the architect that a component's
# allowedPaths include exactly `scripts/kstrl/feature/<component-id>/`
# "(the agent updates progress.txt and PRD passes there)", so a progress
# path derived from prdPath is inside the component's write scope BY
# CONSTRUCTION. Pointing the engineer at the repo-root
# scripts/kstrl/progress.txt instead put it one level ABOVE that
# subtree: the engineer wrote the file the harness told it to write,
# Phase 1 `diff_scope` reported "files outside allowed scope", and the
# component was failed and retried from base. Measured cost of one such
# retry on a real paid run: $12.93.
COMPONENT_PROGRESS_FILENAME = "progress.txt"

# Where the STANDALONE loop (`ks understand`, `ks feature`) writes its
# progress log when nothing is configured. There is no component PRD to
# derive a sibling from there, and the prompt template needs a concrete
# path, so this historical default is materialized by
# KstrlConfig.resolved_progress_file - never stored in the field itself,
# which stays None so "unset" remains distinguishable from "set to the
# default" (R8 review finding 2).
DEFAULT_PROGRESS_FILE = "scripts/kstrl/progress.txt"


def reconcile_progress_paths(
    writer_explicit: str | Path | None,
    reader_explicit: str | Path | None,
) -> tuple[str | None, str | None]:
    """Keep the progress log's WRITER and READER pointing at one file.

    ``[paths] progress`` (or ``PROGRESS_FILE``) configures where the
    engineer WRITES its progress log; ``[verify] progress_file_path``
    (or ``KSTRL_VERIFY_PROGRESS_FILE``) configures where the
    self-critique check READS it. Both default to the same derivation,
    so they agree until exactly one of them is set - and then the
    reader silently inspects a file the engineer never wrote, which
    fails the self-critique gate for a reason the operator cannot see.

    Setting one propagates it to the other. Setting BOTH is left alone,
    including when they differ: an operator who named two paths meant
    two paths, and the caller warns rather than overriding. Returns
    ``(writer, reader)`` as strings, or None where nothing is set.
    """
    writer = str(writer_explicit) if writer_explicit is not None else None
    reader = str(reader_explicit) if reader_explicit is not None else None
    if writer is not None and reader is None:
        return writer, writer
    if reader is not None and writer is None:
        return reader, reader
    return writer, reader


class ProgressReaderConfig(Protocol):
    """The one field ``reconcile_progress_config`` needs from a
    ``VerifyConfig``. Declared structurally so this module does not
    import kstrl.verify, which imports this one."""

    progress_file_path: str | None


def reconcile_progress_config(
    base_config: KstrlConfig,
    verify_config: ProgressReaderConfig,
    root_dir: Path,
) -> str | None:
    """Reconcile the progress log's writer and reader IN ONE PATH DOMAIN.

    Returns a warning message when both were set to different files, or
    None. Mutates both configs.

    The domain is ROOT-RELATIVE for anything inside ``root_dir``, which
    is the domain both consumers actually resolve in: the engineer's
    worker joins the writer path onto its WORKTREE
    (``factory._run_component``), and the self-critique check joins the
    reader path onto the same worktree (``verify.run_mechanical_
    verification``). Reconciling the raw values instead (R8 review
    finding 3) compared an ABSOLUTE writer - ``KstrlConfig.load``
    resolves ``[paths] progress`` against the main checkout - against a
    verbatim-relative reader. The two STRINGS came out equal while the
    runtime paths did not: the worker wrote ``<worktree>/docs/p.md`` and
    the check read ``<root>/docs/p.md``, so self-critique still failed,
    or passed against a stale file in the main checkout.

    A path OUTSIDE ``root_dir`` stays absolute (``relative_to_root``
    cannot relativize it), which is also self-consistent: joining an
    absolute path onto a worktree is a no-op, so writer and reader both
    land on that one file, in the main checkout, for every component.
    """
    writer_domain = (
        relative_to_root(base_config.progress_file, root_dir)
        if base_config.progress_file is not None
        else None
    )
    reader_domain = (
        relative_to_root(Path(verify_config.progress_file_path), root_dir)
        if verify_config.progress_file_path
        else None
    )
    writer, reader = reconcile_progress_paths(writer_domain, reader_domain)
    verify_config.progress_file_path = reader
    if writer is not None:
        base_config.progress_file = Path(writer)
    if writer is not None and reader is not None and writer != reader:
        # Both named, and named differently. Not overridden - an operator
        # who set two paths meant two paths - but said out loud, because
        # the failure it produces is otherwise unattributable.
        return (
            f"progress log writer ([paths] progress = {writer}) and "
            f"reader ([verify] progress_file_path = {reader}) point at "
            "different files; the self-critique check will inspect a "
            "file the engineer does not write"
        )
    return None


def component_progress_path(
    prd_path: str | Path,
    configured: str | Path | None = None,
) -> Path:
    """Progress-log path for the component whose PRD is at ``prd_path``.

    ``configured`` is an explicitly set progress path ([paths] progress
    or PROGRESS_FILE) and wins verbatim for every component - explicit
    configuration is never silently rewritten. With nothing configured
    the log is a SIBLING of the PRD, which reproduces the historical
    ``scripts/kstrl/progress.txt`` for the single-component layout (PRD
    at ``scripts/kstrl/prd.json``) and lands inside the component's own
    feature subtree for a decomposed one.
    """
    if configured is not None:
        return Path(configured)
    return Path(prd_path).parent / COMPONENT_PROGRESS_FILENAME


def component_harness_paths(
    prd_path: str | Path,
    progress_path: str | Path,
    codebase_map_path: str | Path,
) -> list[str]:
    """The harness's OWN files for one component, as EXACT paths.

    kstrl's mechanical checks require the engineer to write three files
    that are not product code: the component PRD (``check_prd_stories``
    re-reads it and only the agent can set ``passes``), the component
    progress log (``check_self_critique`` reads the Self-Critique block
    out of it), and the codebase map (the engineer prompt tells the
    agent to append durable facts to it). kstrl knows all three; the
    operator should not have to guess them into ``allowedPaths``.

    The list is the carve-out both scope guards apply on top of the
    AUTHORED ``allowedPaths`` - the in-loop guard through
    ``loop.run_loop(guard_ignored_paths=...)`` and Phase 1 through
    ``verify.check_diff_scope(harness_paths=...)``. It is reported
    separately from the authored list at both sites so an operator can
    still see what THEY authorised.

    Every entry is an exact path, never a directory prefix: a trailing
    slash would widen the carve-out to a whole subtree, and
    ``scripts/kstrl/`` is precisely the blanket prefix operators resort
    to today and that DECOMPOSE_PROMPT rule 12 refuses. Entries are
    de-duplicated (the single-component layout can point two of the
    three at one file) and sorted so the reported set is stable.

    An entry that is absolute, or that escapes the root, is kept rather
    than dropped, and is harmless: such a file lives outside every
    worktree (joining an absolute path onto one is a no-op), so it never
    appears in a component's ``git diff`` and can never be a scope
    violation in the first place. ``config.reconcile_progress_config``
    documents that configuration as supported.

    Every caller reaches this through
    ``KstrlConfig.component_harness_files``, or
    ``standalone_harness_files`` for the loop whose progress log is not
    a sibling of a component PRD. ``factory._run_component`` used to
    call it directly, from a pool worker that had the three paths as
    strings and no config to ask; #269 stopped that, because the worker
    now receives the carve-out as part of the plan-time scope snapshot
    rather than rebuilding one that merely agreed with Phase 1's.
    """
    return sorted(
        {Path(p).as_posix() for p in (prd_path, progress_path, codebase_map_path)},
    )


@dataclass
class KstrlConfig:
    """Configuration for the kstrl agentic loop."""

    max_iterations: int = 10
    prompt_file: Path = field(default_factory=lambda: Path("scripts/kstrl/prompt.md"))
    prd_file: Path = field(default_factory=lambda: Path("scripts/kstrl/prd.json"))
    # None = UNSET, and unset is the safe default: every factory
    # component then derives its own log next to its own PRD, inside its
    # allowedPaths (see component_progress_path). Any non-None value -
    # from [paths] progress, PROGRESS_FILE, a constructor argument, or a
    # plain attribute assignment - is an explicit setting and is forced
    # on every component verbatim.
    #
    # The sentinel replaced a separate progress_file_explicit flag (R8
    # review finding 2): the flag was set only by the toml/env loaders,
    # so a programmatic caller doing
    # KstrlConfig(progress_file=Path("docs/p.md")) had its value silently
    # ignored - a regression for tests, embedders and the SDK, which
    # could previously pass a base config to run_factory and be obeyed.
    # It is NOT a compare-against-default heuristic (R2.1 deliberately
    # removed that pattern from VerifyConfig.load): pinning the
    # historical path explicitly still counts as explicit, because the
    # field holds a Path only when someone put one there.
    #
    # Standalone callers that need a concrete path (the prompt template's
    # $progress_path) call resolved_progress_file(root_dir).
    progress_file: Path | None = None
    codebase_map_file: Path = field(default_factory=lambda: Path("scripts/kstrl/codebase_map.md"))
    sleep_seconds: float = 2.0
    interactive: bool = False
    allowed_paths: list[str] = field(default_factory=list)

    # Branch config - None means use PRD, "" means skip
    kstrl_branch: str | None = None
    kstrl_branch_explicit: bool = False  # Was KSTRL_BRANCH env var set?
    auto_checkout: bool = True

    # Agent config
    agent_cmd: str | None = None
    model: str | None = None
    model_reasoning_effort: str | None = None
    # "claude-code", "claude-sdk", "codex", "auto", or None
    agent_type: str | None = None
    # R7.6: in-loop USD budget ceiling; enforced only by the claude-sdk
    # adapter (per-turn, inside the agent loop). None = no ceiling.
    agent_budget_usd: float | None = None

    # Timeouts live in kstrl.timeout.TimeoutConfig (the single source
    # for agent_iteration / component_total; R0.1). KstrlConfig used to
    # duplicate them as dead fields - deliberately deleted, do not re-add.

    # UI config
    ui_mode: str = "auto"  # auto|rich|plain
    no_color: bool = False
    ascii_only: bool = False

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> KstrlConfig:
        """Load configuration from environment variables only."""
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        # Default file paths are resolved against root_dir so the config is
        # immediately usable regardless of cwd at the call site.
        config.prompt_file = root_dir / "scripts/kstrl/prompt.md"
        config.prd_file = root_dir / "scripts/kstrl/prd.json"
        config.codebase_map_file = root_dir / "scripts/kstrl/codebase_map.md"
        _apply_env_overrides(config, root_dir)
        return config

    @classmethod
    def from_toml(cls, toml_path: Path, root_dir: Path | None = None) -> KstrlConfig:
        """Load configuration from a kstrl.toml file (no env overlay)."""
        if root_dir is None:
            root_dir = toml_path.parent if toml_path.is_absolute() else Path.cwd()
        config = cls()
        config.prompt_file = root_dir / "scripts/kstrl/prompt.md"
        config.prd_file = root_dir / "scripts/kstrl/prd.json"
        config.codebase_map_file = root_dir / "scripts/kstrl/codebase_map.md"
        if toml_path.exists():
            _apply_toml_overrides(config, toml_path, root_dir)
        return config

    @classmethod
    def load(
        cls,
        root_dir: Path | None = None,
        toml_path: Path | None = None,
    ) -> KstrlConfig:
        """Load configuration with precedence: env > toml > dataclass defaults.

        If ``toml_path`` is omitted, ``<root_dir>/kstrl.toml`` is
        auto-discovered.
        Missing TOML file is fine (defaults are used). Malformed TOML raises.
        """
        if root_dir is None:
            root_dir = Path.cwd()
        if toml_path is None:
            toml_path = resolve_config_file(root_dir)

        config = cls()
        config.prompt_file = root_dir / "scripts/kstrl/prompt.md"
        config.prd_file = root_dir / "scripts/kstrl/prd.json"
        config.codebase_map_file = root_dir / "scripts/kstrl/codebase_map.md"

        if toml_path.exists():
            _apply_toml_overrides(config, toml_path, root_dir)
        _apply_env_overrides(config, root_dir)
        return config

    def component_progress_file(
        self,
        prd_path: str | Path,
        root_dir: Path,
    ) -> str:
        """Worktree-relative progress path for one factory component.

        The single place the factory decides where a component's
        engineer writes its progress log: an explicit configuration wins
        for every component, otherwise the path is derived from the
        component's own PRD so it sits inside the component's
        allowedPaths (see ``component_progress_path``).
        """
        configured = (
            relative_to_root(self.progress_file, root_dir)
            if self.progress_file is not None
            else None
        )
        return component_progress_path(prd_path, configured).as_posix()

    def component_harness_files(
        self,
        prd_path: str | Path,
        root_dir: Path,
    ) -> list[str]:
        """kstrl's OWN files for one component, as exact root-relative paths.

        The single place the factory decides WHICH files the harness
        requires a component's engineer to write, mirroring
        ``component_progress_file``'s role for the progress log alone.
        Both scope guards carve out exactly this list (#264): the in-loop
        guard via ``loop.run_loop(guard_ignored_paths=...)`` and Phase 1
        via ``verify.check_diff_scope(harness_paths=...)``. Deriving the
        three arguments here rather than at each call site is what stops
        the two guards judging different sets.
        """
        return component_harness_paths(
            prd_path,
            self.component_progress_file(prd_path, root_dir),
            relative_to_root(self.codebase_map_file, root_dir),
        )

    def standalone_harness_files(self, root_dir: Path) -> list[str]:
        """The same three files for the STANDALONE loop (``ks understand``).

        Deliberately NOT ``component_harness_files``: that derives the
        progress log as a SIBLING of the component PRD, while the
        standalone loop writes ``resolved_progress_file``. The two
        coincide only in the default layout - point ``[paths] prd`` at
        ``docs/prd.json`` and the factory rule yields
        ``docs/progress.txt`` while the loop still writes
        ``scripts/kstrl/progress.txt`` - so reusing the factory method
        here would carve out a file nothing writes and leave the real
        one exposed. The one place the two rules diverge belongs beside
        them both, not in the CLI.
        """
        return component_harness_paths(
            relative_to_root(self.prd_file, root_dir),
            relative_to_root(self.resolved_progress_file(root_dir), root_dir),
            relative_to_root(self.codebase_map_file, root_dir),
        )

    def resolved_progress_file(self, root_dir: Path) -> Path:
        """Concrete progress path for the STANDALONE loop.

        ``progress_file`` is None until someone sets it, but the loop
        substitutes ``$progress_path`` into the engineer prompt and needs
        a real path there. Standalone runs have no component PRD to
        derive a sibling from, so they get the historical repo-root
        default; a relative explicit setting is anchored to ``root_dir``
        the same way the loaders anchor one.

        The factory does NOT come through here: its workers are handed an
        already-concrete per-component path (factory._run_component), and
        this method returns it untouched.
        """
        if self.progress_file is None:
            return root_dir / DEFAULT_PROGRESS_FILE
        if self.progress_file.is_absolute():
            return self.progress_file
        return root_dir / self.progress_file

    def validate(self) -> list[str]:
        """Validate configuration, returning list of errors."""
        errors: list[str] = []

        if self.max_iterations < 0:
            errors.append(f"MAX_ITERATIONS must be non-negative (got: {self.max_iterations})")

        if not self.prompt_file.exists():
            errors.append(f"Prompt file not found: {self.prompt_file}")

        return errors


CONFIG_FILE_NAME = "kstrl.toml"


def resolve_config_file(root_dir: Path) -> Path:
    """Return the config file for ``root_dir``.

    Resolving the name in one place keeps it from drifting between the
    loaders. The path is returned whether or not it exists; loaders
    no-op on a missing file.
    """
    return root_dir / CONFIG_FILE_NAME


class ConfigError(ValueError):
    """Configuration the operator has to fix before anything can run.

    A ``ValueError`` SUBCLASS, deliberately: every loader in this package
    has always raised ``ValueError`` for a rejected value, and several
    callers catch exactly that on purpose (``ks config show``,
    ``EvolutionConfig.load_or_none``, the TUI config screen). Narrowing
    the type would silently step outside those guards. What the subclass
    adds is a name the CLI can catch at its entry seam and render as an
    ``error:`` line, the way ``BudgetConfigError`` (also a ValueError)
    already is - so a typo in kstrl.toml stops being an unhandled
    traceback from wherever the run happened to reach first (#272).
    """


#: What :func:`load_toml_document`'s catch-all says when the parser
#: rejected the file for a reason it has not established. Deliberately
#: says nothing about the cause; see that function's docstring.
#:
#: A named constant because the tests assert on its ABSENCE from the two
#: specific handlers' messages, which is how the handler order is
#: pinned. A reworded literal restated in a test would make those
#: assertions vacuously true rather than failing, so the test imports
#: this. Same reason ``agents.proc.TIMEOUT_MESSAGE_PREFIX`` is a
#: constant the tests import rather than a string they repeat.
UNPARSEABLE_TOML_MESSAGE = "could not be parsed as TOML"

#: Documents parsed inside the innermost :func:`toml_parse_scope`, or
#: None outside one. A ContextVar rather than a module global so a
#: scope cannot leak into another thread or task.
_PARSE_SCOPE: ContextVar[dict[Path, dict[str, Any]] | None] = ContextVar(
    "kstrl_toml_parse_scope",
    default=None,
)


@contextmanager
def toml_parse_scope() -> Iterator[None]:
    """Parse each kstrl.toml at most once for the duration of the block.

    Every config dataclass reads its own section, and each read reparses
    the whole file: resolving all 22 sections at command entry lexed the
    same bytes 23 times. Measured on the shipped 21 KB
    kstrl.toml.example, the entry check cost 9.4 ms without this scope
    and 0.6 ms with it, which took `ks status` from 159.9 ms to
    157.7 ms against the same file.

    SCOPED, not process-wide, and the distinction is the whole design.
    A process-wide snapshot would freeze the file for the life of the
    process, which two surfaces would be wrong about: the TUI config
    screen has a refresh action whose job is to re-read the file on
    demand (``tui/screens/config.py``), and ``ks serve`` is a long-lived
    daemon that re-reads config per queue item. Inside a block that runs
    for under a millisecond there is no window for either, and no
    caller outside one sees any change at all.

    Nothing in this package mutates what ``load_toml_section`` returns,
    which is what makes handing out the same sub-dict twice safe.
    """
    token = _PARSE_SCOPE.set({})
    try:
        yield
    finally:
        _PARSE_SCOPE.reset(token)


def load_toml_document(path: Path) -> dict[str, Any]:
    """Load and parse a TOML file.

    Raises :class:`ConfigError` for anything the PARSE raises, naming the
    file in all of them. Two things deliberately pass through instead:
    anything deriving from ``BaseException`` rather than ``Exception``,
    and every I/O failure, which cannot reach the guard because all of
    the I/O happens before it.

    Inside a :func:`toml_parse_scope` the parsed document is reused
    rather than re-read.

    ``tomllib.load`` raises for a whole family of bad input, and the
    family is not enumerable from the outside. #318 tried three times
    and the sequence is the argument for where it stopped:

    - ``TOMLDecodeError``, a syntax error. All the original named.
    - ``UnicodeDecodeError``, a file that is not utf-8, because
      ``tomllib.load`` decodes the stream ITSELF before it lexes
      anything. A ``ValueError``, NOT a ``TOMLDecodeError``, so it
      walked past that. Same defect
      ``verify._default_typecheck_command`` fixed for pyproject.toml in
      #288, and the encoding rule CLAUDE.md states from #291.
    - A plain ``ValueError``, for input the parser accepts and Python
      then refuses to build: ``max_iterations = <4301 digits>`` raises
      "Exceeds the limit (4300 digits) for integer string conversion"
      from ``sys.get_int_max_str_digits``. Walked past round 1.
    - ``RecursionError``, from tomllib's recursive-descent parser, at no
      one depth: at the default limit nested arrays first fail at 497
      levels from a one-frame caller and 396 under 200 more, inline
      tables at 331 and 264; the caller's own stack sets it. It derives from
      ``RuntimeError``, NOT ``ValueError``, so it walked past round 2 -
      whose docstring, whose AST guard and whose CLAUDE.md line all
      asserted that ``ValueError`` WAS the whole class. Round 2 stated
      the right thesis and then named the wrong ceiling for it, which
      is a worse failure than round 1: a future author could satisfy
      every guard it left behind and still take the CLI down.

    So the catch-all is ``Exception``, and that is a ceiling rather than
    a fourth guess. Everything a parser can say about a DOCUMENT derives
    from ``Exception``. What does not derive from it is
    ``KeyboardInterrupt`` and ``SystemExit`` - which are about the
    PROCESS, not the file, and must never be relabelled as the
    operator's broken config. ``MemoryError`` on a hostile-sized file IS
    covered, since it derives from ``Exception``, with the honest caveat
    that no handler can promise the interpreter has the headroom left to
    render the message.

    Provenance follows (#323): the parse runs at stack depth 9 to 16
    under ``ks status``, so a ``RecursionError`` here is the document's,
    and every block ``config_preflight`` guards reaches tomllib only
    through this function, so one above is ours - see ``raise_if_defect``.

    ``OSError`` is not in the catch-all's reach at all, which is the
    stronger form of the guarantee two callers depend on. An earlier
    draft re-raised it from inside the guard; that clause was correct
    and unpinnable, because with the I/O already hoisted out there was
    no way to make a test reach it. A special case no test can enter is
    a special case that has not been deleted yet.

    The rule, after being wrong three times: a parser's error taxonomy
    belongs to the parser, and a reader naming any class narrower than
    "an exception, out of this call" is asserting something about the
    standard library that it cannot check.

    Fail closed WITHOUT overclaiming, though. The catch-all names the
    file and repeats what the parser said; it does not diagnose a cause
    it has not established. Telling an operator to re-save a file that
    is already good utf-8 would be a silent semantic substitution one
    step on from swallowing the error.

    That is what earns the ``UnicodeDecodeError`` rung specifically: the
    remedy "re-save as utf-8" is nowhere in ``str(exc)``, so a reader
    who saw only the catch-all's line would lose it. It is NOT what
    earns the ``TOMLDecodeError`` rung, and saying so would be
    over-fitting the rule to two cases. That rung's ``{exc}`` carries
    the line and column, and the catch-all interpolates ``{exc}``
    identically, so it would lose nothing diagnostic. What keeps it is
    narrower and duller: "Invalid TOML" is a message contract, asserted
    at thirteen sites across five test files including the TUI banner an
    operator reads. It stays for compatibility, not because it tells
    anyone more.

    Order is load-bearing, and only because of the catch-all.
    ``TOMLDecodeError`` and ``UnicodeDecodeError`` are SIBLINGS - both
    derive from ``ValueError``, neither from the other - so their
    relative order is free, and a round-1 test that claimed to pin it
    passed with the two reversed. The broad clause is the real
    constraint: it is a supertype of every clause above it, so it must
    come last or it swallows them and relabels every syntax error and
    every bad byte as an unspecified parse failure.
    ``test_the_broad_handler_must_come_last`` fails if it moves; it was
    watched failing, with ``__pycache__`` purged, because a handler
    permutation leaves the file byte-identical and a same-second rewrite
    is otherwise served from a stale ``.pyc``.

    See ``config_preflight.SURFACE_REJECTIONS`` for the two callers that
    depend on telling an unreadable file from an unparseable one.
    """
    scope = _PARSE_SCOPE.get()
    if scope is not None and path in scope:
        return scope[path]
    # ALL the I/O happens here, OUTSIDE the guard, so that nothing the
    # guard catches can be an I/O fault. That is what lets the catch-all
    # be as wide as it is without lying: it cannot reach an ``OSError``
    # that ``config_preflight.SURFACE_REJECTIONS`` needs to stay raw,
    # and it cannot reach ``open``'s own ``ValueError("embedded null
    # byte")`` on ``Path("bad\\x00path.toml")``, which an earlier draft
    # relabelled "could not be parsed as TOML" for a file it had never
    # opened. ``tomllib.load`` is ``fp.read()``, ``b.decode()``,
    # ``loads(s)``; splitting it here changes no exception type (all
    # four faults measured identical both ways) and removes the need for
    # an ``except OSError: raise`` clause that no test could pin.
    raw = path.read_bytes()
    try:
        data: dict[str, Any] = tomllib.loads(raw.decode())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid UTF-8, which TOML requires; re-save the file as UTF-8: {exc}"
        ) from exc
    except Exception as exc:
        # LAST, and ``Exception`` rather than ``ValueError``: see the
        # docstring. Says what the parser said and names the file;
        # claims no cause beyond that.
        raise ConfigError(f"{path} {UNPARSEABLE_TOML_MESSAGE}: {exc}") from exc
    if scope is not None:
        scope[path] = data
    return data


def load_toml_section(toml_path: Path, section: str) -> dict[str, Any]:
    """Read a named section from a kstrl.toml file.

    Shared by every config dataclass that has a corresponding
    ``[section]`` in the canonical kstrl.toml. Returns ``{}`` when the
    file or the section is absent; raises :class:`ConfigError` (a
    ``ValueError``) with a clear message when the file will not parse -
    a syntax error OR a non-utf-8 byte, see :func:`load_toml_document` -
    so every loader behaves consistently. ``OSError`` is NOT normalized
    into that, so a caller reading this file without the entry check in
    front of it catches both. Sub-section keys that are not
    dicts (e.g. someone
    wrote ``factory = "hi"`` instead of ``[factory]``) return ``{}``
    rather than crashing later in the per-key cast.
    """
    if not toml_path.exists():
        return {}
    data = load_toml_document(toml_path)
    section_data = data.get(section, {})
    if not isinstance(section_data, dict):
        return {}
    return section_data


def _apply_toml_overrides(
    config: KstrlConfig,
    toml_path: Path,
    root_dir: Path,
) -> None:
    """Mutate config in place from a kstrl.toml file.

    Maps the documented section structure (agent, run, paths, git, ui) onto
    the flat KstrlConfig dataclass. Unknown keys are silently ignored.
    """
    data = load_toml_document(toml_path)

    agent = data.get("agent")
    if isinstance(agent, dict):
        agent_type = agent.get("type")
        if isinstance(agent_type, str) and agent_type:
            config.agent_type = agent_type
        command = agent.get("command")
        if isinstance(command, str) and command:
            config.agent_cmd = command
        model = agent.get("model")
        if isinstance(model, str) and model:
            config.model = model
        reasoning = agent.get("reasoning_effort")
        if isinstance(reasoning, str) and reasoning:
            config.model_reasoning_effort = reasoning
        budget = agent.get("budget_usd")
        if isinstance(budget, (int, float)) and not isinstance(budget, bool):
            if budget > 0:
                config.agent_budget_usd = float(budget)

    run = data.get("run")
    if isinstance(run, dict):
        if "max_iterations" in run:
            config.max_iterations = int(run["max_iterations"])
        if "sleep_seconds" in run:
            config.sleep_seconds = float(run["sleep_seconds"])
        if "interactive" in run:
            config.interactive = bool(run["interactive"])

    paths = data.get("paths")
    if isinstance(paths, dict):
        if isinstance(paths.get("prompt"), str) and paths["prompt"]:
            config.prompt_file = _resolve_path(paths["prompt"], root_dir)
        if isinstance(paths.get("prd"), str) and paths["prd"]:
            config.prd_file = _resolve_path(paths["prd"], root_dir)
        if isinstance(paths.get("progress"), str) and paths["progress"]:
            config.progress_file = _resolve_path(paths["progress"], root_dir)
        if isinstance(paths.get("codebase_map"), str) and paths["codebase_map"]:
            config.codebase_map_file = _resolve_path(paths["codebase_map"], root_dir)
        allowed = paths.get("allowed")
        if isinstance(allowed, list):
            config.allowed_paths = [str(p) for p in allowed if isinstance(p, str)]

    git_section = data.get("git")
    if isinstance(git_section, dict):
        if "branch" in git_section:
            branch = git_section["branch"]
            # Only treat the TOML branch as an explicit override when it
            # is non-empty. `branch = ""` in the shipped example means
            # "no override, fall back to PRD branchName", whereas the env
            # var `KSTRL_BRANCH=""` (handled below) keeps its historical
            # meaning of "explicit skip".
            if isinstance(branch, str) and branch:
                config.kstrl_branch = branch
                config.kstrl_branch_explicit = True
        if "auto_checkout" in git_section:
            config.auto_checkout = bool(git_section["auto_checkout"])

    ui = data.get("ui")
    if isinstance(ui, dict):
        if "ascii" in ui:
            config.ascii_only = bool(ui["ascii"])


def _apply_env_overrides(config: KstrlConfig, root_dir: Path) -> None:
    """Mutate config in place from environment variables.

    Only env vars that are explicitly set in the environment are applied -
    unset vars leave the existing config value untouched.
    """
    if "MAX_ITERATIONS" in os.environ:
        config.max_iterations = int(os.environ["MAX_ITERATIONS"])
    if "PROMPT_FILE" in os.environ:
        config.prompt_file = _resolve_path(os.environ["PROMPT_FILE"], root_dir)
    if "PRD_FILE" in os.environ:
        config.prd_file = _resolve_path(os.environ["PRD_FILE"], root_dir)
    if "PROGRESS_FILE" in os.environ:
        config.progress_file = _resolve_path(os.environ["PROGRESS_FILE"], root_dir)
    if "CODEBASE_MAP_FILE" in os.environ:
        config.codebase_map_file = _resolve_path(os.environ["CODEBASE_MAP_FILE"], root_dir)
    if "SLEEP_SECONDS" in os.environ:
        config.sleep_seconds = float(os.environ["SLEEP_SECONDS"])
    if "INTERACTIVE" in os.environ:
        config.interactive = _parse_bool(os.environ.get("INTERACTIVE"))
    if "ALLOWED_PATHS" in os.environ:
        config.allowed_paths = _parse_paths(os.environ.get("ALLOWED_PATHS"))
    if "KSTRL_BRANCH" in os.environ:
        config.kstrl_branch = os.environ["KSTRL_BRANCH"]
        config.kstrl_branch_explicit = True
    if "KSTRL_AUTO_CHECKOUT" in os.environ:
        config.auto_checkout = _parse_bool(os.environ.get("KSTRL_AUTO_CHECKOUT"))
    if "AGENT_CMD" in os.environ:
        config.agent_cmd = os.environ["AGENT_CMD"]
    if "MODEL" in os.environ:
        config.model = os.environ["MODEL"]
    if "MODEL_REASONING_EFFORT" in os.environ:
        config.model_reasoning_effort = os.environ["MODEL_REASONING_EFFORT"]
    if "KSTRL_AGENT_TYPE" in os.environ:
        config.agent_type = os.environ["KSTRL_AGENT_TYPE"]
    if "KSTRL_AGENT_BUDGET_USD" in os.environ:
        try:
            budget_value = float(os.environ["KSTRL_AGENT_BUDGET_USD"])
        except ValueError:
            budget_value = 0.0
        if budget_value > 0:
            config.agent_budget_usd = budget_value
    if "KSTRL_UI" in os.environ:
        config.ui_mode = os.environ["KSTRL_UI"]
    if "NO_COLOR" in os.environ:
        config.no_color = True
    if "KSTRL_ASCII" in os.environ:
        config.ascii_only = _parse_bool(os.environ.get("KSTRL_ASCII"))
