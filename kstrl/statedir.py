"""Runtime state-dir resolution: artifacts in-tree, control state outside.

Journals, worktrees, queue items, locks, and evidence stay under
``<root>/.kstrl/`` (the agent-reachable audit/artifact tree). Control-plane
files that govern what the factory may do without a human - autonomy level,
spend ledger, pause marker, inbox, GitHub processed-ids - live under the
XDG state directory:

    ${XDG_STATE_HOME:-~/.local/state}/kstrl/<repo-id>/

Clones that share the same ``origin`` remote share one control directory
(deliberate: one autonomy level and one daily spend ledger per project).
Do not run two ``ks serve`` daemons against that shared ledger concurrently
without holding ``control.lock``. Checkouts with no ``origin`` remote get a
path-hashed id and stay checkout-local.

R8.9 / #194.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

STATE_DIR_NAME = ".kstrl"
CONTROL_APP_NAME = "kstrl"
CONTROL_LOCK_FILENAME = "control.lock"
CONTROL_RELOCATED_MARKER = "control_relocated"

#: Flat filenames under the XDG control directory (not nested under queue/).
CONTROL_AUTONOMY = "autonomy.json"
CONTROL_INBOX = "inbox.jsonl"
CONTROL_SPEND = "spend.json"
CONTROL_PAUSE = "pause.json"
CONTROL_GITHUB_PROCESSED = "github_processed.json"

CONTROL_FILENAMES: tuple[str, ...] = (
    CONTROL_AUTONOMY,
    CONTROL_INBOX,
    CONTROL_SPEND,
    CONTROL_PAUSE,
    CONTROL_GITHUB_PROCESSED,
)

#: Directories kstrl creates directly under ``.kstrl/``. A statement of
#: FACT about what the package writes, kept complete: which of them the
#: scope guard actually carves out is the separate policy in
#: ``STATE_NOT_CARVED`` below. Complete because this is the one place to
#: read what kstrl puts in its state directory, and because the AST
#: drift net and the exclusion policy are both checked against it.
#:
#: kstrl fills each of these with names it invents at runtime - a run
#: id, a component id, a queue item, a fact digest - so a carved one is
#: emitted as a directory PREFIX. That is the narrowest form available
#: for a tree whose leaf names do not exist until the run does, and it
#: means everything beneath a carved prefix is uncounted by the in-loop
#: guard. ``state_dir_carve_out`` says what that does and does not buy.
#:
#: Every name here is written by kstrl and by nothing else:
#:
#: - ``contract``   ``contract.py`` throwaway merge worktrees
#: - ``debug``      ``pipeline.py`` per-component debug dumps
#: - ``knowledge``  ``knowledge.py`` distilled facts (default root)
#: - ``logs``       ``cli.py`` feature transcripts, ``serve.py`` launchd logs
#: - ``proposals``  ``cli.py`` evolution proposals
#: - ``queue``      ``workqueue.py`` (``QUEUE_DIR_NAME``)
#: - ``runs``       ``events.py`` event journals and transcripts
#: - ``snapshots``  ``fixtures.py`` (``FixturesConfig.snapshot_dir``)
#: - ``worktrees``  ``factory.py`` component worktrees and their locks
#:
#: ``tests/test_state_dir_scope.py`` AST-walks ``kstrl/`` for the entries
#: the package names and fails on one that is missing here. Its reach is
#: exactly the two spellings kstrl uses today - a ``/`` join off the
#: state directory (``root / ".kstrl" / "runs"``,
#: ``state_dir(root) / QUEUE_DIR_NAME``) and a literal ``.kstrl/<name>``
#: inside a string. It would NOT see a local alias, an f-string or an
#: ``os.path.join``, so it is a net under the current idiom, not a proof
#: about the next one. Write a new state path in one of those two forms
#: and the net catches the omission; write it another way and this list
#: is what has to be updated by hand.
#:
#: ``queue`` re-spells ``workqueue.QUEUE_DIR_NAME`` as a literal because
#: ``workqueue`` imports this module; importing back would cycle. The
#: AST net is what keeps the two in step.
STATE_SUBDIRS: tuple[str, ...] = (
    "contract",
    "debug",
    "knowledge",
    "logs",
    "proposals",
    "queue",
    "runs",
    "snapshots",
    "worktrees",
)

#: Single files kstrl writes directly in the state directory. Exact
#: paths, never prefixes. The last two are the flat legacy in-tree
#: control files; R8.9 moved live control state to the XDG directory,
#: but a repository that has not run a control command since still has
#: them here. The other three legacy control files live under ``queue/``
#: and are covered by that subtree.
STATE_FILES: tuple[str, ...] = (
    CONTROL_RELOCATED_MARKER,
    "evolution.jsonl",
    "experiments.tsv",
    "factory.lock",
    "progress.jsonl",
    CONTROL_AUTONOMY,
    CONTROL_INBOX,
)

#: Entries kstrl creates but the guard deliberately keeps COUNTING
#: (#274 review). Two criteria, and BOTH must hold before a name goes
#: here:
#:
#: 1. It carries AUTHORITY over what kstrl does next, so losing sight of
#:    it is worse than a false scope failure. ``queue`` is the in-tree
#:    work queue ``ks serve`` drains (``workqueue.py``), so a file
#:    written there can admit work; the pause marker, spend ledger and
#:    GitHub processed-ids sit inside it. ``proposals`` is what
#:    ``ks evolve --apply`` reads to mutate config and prompts, and
#:    ``[evolution] auto_apply_computational`` can skip its confirmation.
#:    ``autonomy.json`` is the autonomy level itself and ``inbox.jsonl``
#:    the human-decision stream. All five are exactly
#:    ``policy.ENFORCEMENT_MACHINERY_PATHS``, where touching one is a
#:    non-overridable hard fail; a guard that stopped reporting them
#:    would disagree with the envelope about the same files.
#: 2. NOTHING reachable from ``run_loop`` writes it, so keeping it
#:    countable cannot reintroduce the failure this carve-out exists to
#:    fix. They are written only by ``serve.py`` and ``cli.py`` command
#:    entry points, and ``serve`` moves a queue item to running BEFORE it
#:    launches the factory and to a terminal state after it returns, so
#:    its writes bracket the run rather than land inside it. The R8.9
#:    migration only ever moves a control file OUT, and a deletion is
#:    not a violation. ``tests/test_state_dir_scope.py`` checks this
#:    against the loop's static import closure rather than leaving it as
#:    a claim.
#:
#: Criterion 2 is affordable because the guard already subtracts
#: whatever was on disk when the agent started
#: (``git.capture_workspace_baseline``). Measured, not assumed: an
#: operator running ``ks serve`` against this repo does not trip over
#: queue items that predate the run, only over one written while the
#: agent was working, which is the case worth seeing.
STATE_NOT_CARVED: tuple[str, ...] = (
    CONTROL_AUTONOMY,
    CONTROL_INBOX,
    "proposals",
    "queue",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def state_dir(root_dir: Path) -> Path:
    """Return the in-tree artifact/lock directory for ``root_dir``."""
    return root_dir / STATE_DIR_NAME


def _same_directory(left: Path, right: Path) -> bool:
    """Whether two paths name one directory, symlinks resolved.

    ``realpath`` rather than ``Path.resolve`` because neither side is
    required to exist - the walk root may be a worktree the caller is
    about to create - and because macOS reaches ``/tmp`` through a
    symlink, so the project root and the loop's ``cwd`` are routinely
    the same directory spelled two ways. Falls back to ``normpath`` if
    the filesystem refuses to answer.
    """
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except OSError:
        return os.path.normpath(left) == os.path.normpath(right)


def state_dir_carve_out(walk_root: Path, state_root: Path | None) -> list[str]:
    """Paths under ``.kstrl/`` the scope guard must not count (#274).

    The in-loop scope guard walks UNTRACKED files
    (``git ls-files --others --exclude-standard``) and counts every one
    against the component's ``allowedPaths``. kstrl writes its own run
    journals, locks and queue into ``<state_root>/.kstrl/`` while that
    walk is happening, so in a repository that does not ignore
    ``.kstrl/`` the harness trips over its own artifacts and the
    operator pays an engineer iteration for it. ``ks init`` now
    scaffolds a ``.gitignore`` (#273), which cannot reach a repository
    that already exists; this carve-out travels with the harness
    instead.

    **Not a blanket ``.kstrl/`` bypass**, which is what
    ``guards.check_violations`` warns against and what
    ``decompose._ALLOWED_PATHS_EXCLUDE`` refuses to let an architect
    authorise. The return value is ``STATE_SUBDIRS`` and ``STATE_FILES``
    minus ``STATE_NOT_CARVED``, so a NEW top-level name under the state
    directory - ``.kstrl/notes.md``, ``.kstrl/payload.py`` - is still a
    violation.

    **What it does NOT claim.** The carved subtrees are directory
    PREFIXES, so a file the agent places INSIDE one
    (``.kstrl/runs/x/evil.py``) is not counted by the in-loop guard. No
    exact-path form exists for those trees, because kstrl invents their
    leaf names at runtime, and a shape constraint on the leaves would be
    a mechanism an agent satisfies by choosing a filename. The residual
    is accepted and pinned by test rather than papered over, and it is
    bounded three ways: ``STATE_NOT_CARVED`` keeps every
    authority-carrying entry countable, so what is left is audit and
    artifact trees; Phase 1's ``verify.check_diff_scope`` is
    deliberately un-carved, so anything the agent COMMITS under
    ``.kstrl/`` still fails there and never reaches a PR; and it applies
    only where the walk root IS the state root, so a component worktree
    gets nothing.

    None of this is prevention. The agent has a filesystem; a scope
    guard reports out-of-bounds edits, it does not authenticate
    journals. What changes here is only what gets REPORTED, and
    reporting under ``.kstrl/`` was already off in every repository
    whose ``.gitignore`` lists it.

    ``walk_root`` is the tree the guard is about to walk;
    ``state_root`` is the project root whose ``.kstrl/`` kstrl actually
    writes, which only the caller knows. Empty unless the two name the
    same directory, so a component worktree - whose ``.kstrl/`` can only
    be the agent's, because kstrl writes nothing there - keeps full
    guard visibility. ``state_root=None`` means the caller has no state
    directory to declare and also yields nothing: a caller that forgets
    to pass it gets the pre-#274 behaviour, which fails loudly on
    kstrl's own artifacts, rather than a carve-out applied to a tree
    kstrl does not own.

    Entries are relative to the walk root because that is what
    ``git ls-files`` reports and what ``guards.path_is_allowed``
    matches on. Directory entries carry a trailing slash so
    ``path_is_allowed`` treats them as prefixes; file entries do not.
    """
    if state_root is None or not _same_directory(walk_root, state_root):
        return []
    prefix = f"{STATE_DIR_NAME}/"
    carved = (
        *(f"{name}/" for name in STATE_SUBDIRS if name not in STATE_NOT_CARVED),
        *(name for name in STATE_FILES if name not in STATE_NOT_CARVED),
    )
    return sorted(f"{prefix}{name}" for name in carved)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_remote_url(url: str) -> str:
    """Canonicalize a git remote URL for stable hashing.

    Casefolds first, strips userinfo and default ports, maps
    ``git@host:path`` / ``ssh://`` forms to ``host/path``, and strips a
    trailing ``.git`` (any case).
    """
    raw = url.strip()
    if not raw:
        return ""
    value = raw.casefold()
    if value.startswith("git@"):
        # git@github.com:org/repo.git -> github.com/org/repo
        rest = value[len("git@") :]
        if ":" in rest:
            host, path = rest.split(":", 1)
            value = f"{host}/{path}"
    elif "://" in value:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.port and parts.port not in (80, 443, 22):
            host = f"{host}:{parts.port}"
        path = parts.path.lstrip("/")
        value = f"{host}/{path}" if path else host
    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[: -len(".git")]
    return value


def _origin_url(root_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root_dir), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _slug_from_identity(identity: str) -> str:
    base = identity.rsplit("/", 1)[-1] if identity else "repo"
    slug = _SLUG_RE.sub("-", base.lower()).strip("-")
    if not slug:
        slug = "repo"
    return slug[:32]


def repo_id(root_dir: Path) -> str:
    """Stable control-dir id for ``root_dir``.

    Prefer a hash of the normalized ``origin`` URL so every clone of the
    same remote shares control state. With no origin, hash the resolved
    absolute path (checkout-local).
    """
    origin = _origin_url(root_dir)
    if origin:
        identity = normalize_remote_url(origin)
        source = "origin"
    else:
        try:
            identity = str(root_dir.resolve())
        except OSError:
            identity = str(root_dir)
        source = "path"
    digest = hashlib.sha256(f"{source}:{identity}".encode()).hexdigest()[:16]
    return f"{_slug_from_identity(identity)}-{digest}"


_resolved_xdg_home: Path | None = None
_resolved_xdg_raw: str | None = None


def xdg_state_home() -> Path:
    """XDG state home, overridable via ``XDG_STATE_HOME``.

    Overrides are expanded and resolved so a relative value cannot drift
    with ``chdir`` (pause/spend paths must stay absolute). The first
    resolution of a given override string is cached for the process so
    ``XDG_STATE_HOME=rel`` set once keeps pointing at the same directory
    after later ``chdir`` calls.
    """
    global _resolved_xdg_home, _resolved_xdg_raw
    override = os.environ.get("XDG_STATE_HOME", "").strip()
    if not override:
        return (Path.home() / ".local" / "state").resolve()
    if _resolved_xdg_home is not None and _resolved_xdg_raw == override:
        return _resolved_xdg_home
    resolved = Path(override).expanduser().resolve()
    _resolved_xdg_home = resolved
    _resolved_xdg_raw = override
    return resolved


def clear_xdg_state_home_cache() -> None:
    """Test helper: drop the cached ``XDG_STATE_HOME`` resolution."""
    global _resolved_xdg_home, _resolved_xdg_raw
    _resolved_xdg_home = None
    _resolved_xdg_raw = None


def control_dir(root_dir: Path) -> Path:
    """XDG control directory for ``root_dir`` (outside the agent tree)."""
    return xdg_state_home() / CONTROL_APP_NAME / repo_id(root_dir)


def control_file(root_dir: Path, name: str) -> Path:
    """Path to a named control file under the XDG control directory."""
    if name not in CONTROL_FILENAMES:
        raise ValueError(f"unknown control file {name!r}; expected one of {CONTROL_FILENAMES}")
    return control_dir(root_dir) / name


def legacy_control_paths(root_dir: Path) -> dict[str, Path]:
    """Former in-tree locations for each control file (migration + halt set)."""
    queue = state_dir(root_dir) / "queue"
    return {
        CONTROL_AUTONOMY: state_dir(root_dir) / CONTROL_AUTONOMY,
        CONTROL_INBOX: state_dir(root_dir) / CONTROL_INBOX,
        CONTROL_SPEND: queue / CONTROL_SPEND,
        CONTROL_PAUSE: queue / CONTROL_PAUSE,
        CONTROL_GITHUB_PROCESSED: queue / CONTROL_GITHUB_PROCESSED,
    }


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def legacy_control_remaining(root_dir: Path) -> tuple[str, ...]:
    """Filenames still present at legacy in-tree locations.

    Any leftover means migration is incomplete or dual-state: live XDG
    readers must fail closed rather than treat missing XDG as first-run.
    """
    remaining: list[str] = []
    for name, path in legacy_control_paths(root_dir).items():
        try:
            if path.exists() or path.is_symlink():
                remaining.append(name)
        except OSError:
            remaining.append(name)
    return tuple(remaining)


def _control_files_compromised(root_dir: Path) -> str | None:
    """Why live control files are untrustworthy, or None if plain files."""
    try:
        cdir = control_dir(root_dir).resolve()
    except OSError as exc:
        return f"control directory unresolvable: {exc}"
    for name in CONTROL_FILENAMES:
        path = cdir / name
        try:
            if path.is_symlink():
                return f"control file {name} is a symlink"
            if not path.exists():
                continue
            resolved = path.resolve()
            if not _is_under(resolved, cdir):
                return f"control file {name} resolves outside the control directory"
        except OSError as exc:
            return f"control file {name} unreadable: {exc}"
    return None


def control_dir_accessible(root_dir: Path) -> bool:
    """Whether the control directory can be created and listed.

    Used by fail-closed pause: an inaccessible control plane must not
    look like "no pause marker → running".
    """
    try:
        target = control_dir(root_dir)
        target.mkdir(parents=True, exist_ok=True)
        # Probe readability; ignore the listing itself.
        list(target.iterdir())
        return True
    except OSError:
        return False


def control_untrusted_reason(root_dir: Path) -> str | None:
    """Why the control plane must fail closed, or None if usable."""
    if not control_dir_accessible(root_dir):
        return "control state directory inaccessible"
    try:
        if _is_under(control_dir(root_dir), root_dir):
            return "XDG_STATE_HOME resolves under the repository tree"
    except OSError as exc:
        return f"control directory unresolvable: {exc}"
    remaining = legacy_control_remaining(root_dir)
    if remaining:
        return (
            "legacy in-tree control files remain after migration "
            f"({', '.join(remaining)}); refuse to treat empty XDG as first-run"
        )
    compromised = _control_files_compromised(root_dir)
    if compromised is not None:
        return compromised
    return None


def control_is_external(root_dir: Path) -> bool:
    """True when live control state is outside the agent-reachable tree.

    False when the control dir resolves under ``root_dir`` (mis-set
    ``XDG_STATE_HOME``), when the control dir is inaccessible, when any
    legacy in-tree control file still exists, or when a control file is a
    symlink / resolves outside the control dir.
    """
    return control_untrusted_reason(root_dir) is None


def _write_relocated_marker(root_dir: Path, *, moved: list[str]) -> None:
    marker = state_dir(root_dir) / CONTROL_RELOCATED_MARKER
    try:
        state_dir(root_dir).mkdir(parents=True, exist_ok=True)
        payload = {
            "repo_id": repo_id(root_dir),
            "control_dir": str(control_dir(root_dir)),
            "migrated_at": _utc_now_iso(),
            "moved": moved,
        }
        marker.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # Marker is operator convenience, not load-bearing.
        pass


def _move_control_file(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst``, including across devices (EXDEV)."""
    try:
        os.replace(src, dst)
        return
    except OSError:
        pass
    # Cross-device or other replace failure: copy then unlink. If unlink
    # fails, legacy remains and consumers fail closed via
    # ``legacy_control_remaining``.
    shutil.copy2(src, dst)
    src.unlink()


def migrate_control_state(root_dir: Path) -> list[str]:
    """Move legacy in-tree control files into the XDG control dir once.

    Returns the list of filenames moved. When both XDG and legacy copies
    exist (dual-state), the legacy file is left in place so
    ``legacy_control_remaining`` keeps pause/spend fail-closed until an
    operator reconciles. Idempotent when only XDG exists.
    """
    if not control_dir_accessible(root_dir):
        return []
    target_root = control_dir(root_dir)
    moved: list[str] = []
    legacy = legacy_control_paths(root_dir)
    for name in CONTROL_FILENAMES:
        src = legacy[name]
        dst = target_root / name
        try:
            src_present = src.exists() or src.is_symlink()
            dst_present = dst.exists() or dst.is_symlink()
        except OSError:
            continue
        if not src_present:
            continue
        if dst_present:
            # Dual-state: do not silently prefer XDG. Leave legacy so
            # fail-closed readers refuse until the leftover is removed.
            warnings.warn(
                f"kstrl: dual-state control file {name}: both {src} and "
                f"{dst} exist; leaving legacy in place so pause/spend "
                "fail closed until reconciled (R8.9)",
                stacklevel=2,
            )
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            _move_control_file(src, dst)
        except OSError as exc:
            warnings.warn(
                f"kstrl: failed to migrate control file {src} -> {dst}: {exc}",
                stacklevel=2,
            )
            continue
        moved.append(name)
        warnings.warn(
            f"kstrl: relocated control file {name} from {src} to {dst} "
            "(R8.9; legacy in-tree path is no longer written)",
            DeprecationWarning,
            stacklevel=2,
        )
    if moved:
        _write_relocated_marker(root_dir, moved=moved)
    return moved


def ensure_control_state(root_dir: Path) -> Path:
    """Ensure the XDG control dir exists and migrate any legacy files.

    Call at the start of every control read/write path so CLI and daemon
    both migrate before touching state.
    """
    migrate_control_state(root_dir)
    target = control_dir(root_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


@contextmanager
def control_lock(root_dir: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold the cross-checkout mutex on the XDG control directory.

    Serializes spend / pause / autonomy / inbox / GitHub-ledger writes so
    two checkouts sharing an origin cannot corrupt the shared ledger.
    POSIX ``fcntl`` only; without it we degrade to no exclusion (same
    pattern as ``queue_lock`` / factory lock).

    Fails closed if the control directory cannot be created: yielding
    without a lock would silently disable exclusion while writers proceed.
    """
    ensure_control_state(root_dir)
    lock_path = control_dir(root_dir) / CONTROL_LOCK_FILENAME
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ControlUnavailableError(
            f"control state directory unavailable ({lock_path.parent}): {exc}"
        ) from exc
    try:
        import fcntl
    except ImportError:
        yield
        return

    try:
        handle: IO[str] = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        raise ControlUnavailableError(f"cannot open control lock {lock_path}: {exc}") from exc
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            raise ControlLockedError(
                f"control state is locked by another process ({lock_path})"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class ControlStateError(RuntimeError):
    """The control state would not serve this caller.

    One name for the whole surface :func:`control_lock` refuses on, so a
    caller writes what the callee can raise rather than an enumeration
    of the causes it happens to have thought of. Both subclasses stay
    ``RuntimeError`` subclasses, so nothing that already catches one of
    them by name changes.
    """


class ControlLockedError(ControlStateError):
    """Another process holds ``control.lock``."""


class ControlUnavailableError(ControlStateError):
    """Control state directory cannot be created or locked."""
