"""Pytest fixtures for kstrl tests.

Suite isolation (R4.1): before this conftest grew the fixtures below, the
suite appended hundreds of junk entries to the repository's real
``.kstrl/evolution.jsonl`` / ``.kstrl/experiments.tsv`` (837 of 910 journal
entries at review time were test pollution), corrupting the data the
learning loop consumes. Two layers fix that:

1. ``isolate_kstrl_state`` (autouse, function-scoped) redirects every
   relative ``.kstrl/...`` default write path into the test's ``tmp_path``.
2. ``guard_repo_kstrl_state`` (autouse, session-scoped) is the enforcement:
   it fingerprints the repo's real ``.kstrl/`` before the session and fails
   the run loudly if any test mutated it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from kstrl.git import DiffStat, get_diff_stat

# Repository root that contains this test suite, independent of CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Environment-variable families consumed by the from_env/load paths of the
# phase configs (FactoryConfig, TimeoutConfig, ContractConfig,
# SecurityConfig, VerifyConfig, EvolutionConfig, KnowledgeConfig). Cleared
# by prefix so ambient dev-machine env cannot alter from_env/load tests,
# and so newly added vars in a family are covered without editing this list.
# The bare FACTORY_ family predates the KSTRL_ namespace and is still read
# directly by factory config, so it is scrubbed alongside the rest.
KSTRL_ENV_PREFIXES: tuple[str, ...] = (
    "FACTORY_",
    "KSTRL_FACTORY_",
    "KSTRL_TIMEOUT_",
    "KSTRL_CONTRACT_",
    "KSTRL_SECURITY_",
    "KSTRL_VERIFY_",
    "KSTRL_EVOLUTION_",
    "KSTRL_KNOWLEDGE_",
    "KSTRL_FEEDFORWARD_",
    "KSTRL_MUTATION_",
    "KSTRL_DEAD_CODE_",
    "KSTRL_LINEAR_",
    "KSTRL_NOTIFY_",
    "KSTRL_QUEUE_",
)

# Legacy single-loop env vars (exact names, no shared prefix).
_LEGACY_ENV_VARS: tuple[str, ...] = (
    "MAX_ITERATIONS",
    "AGENT_CMD",
    "MODEL",
    "MODEL_REASONING_EFFORT",
    "SLEEP_SECONDS",
    "INTERACTIVE",
    "PROMPT_FILE",
    "ALLOWED_PATHS",
    "KSTRL_BRANCH",
    "KSTRL_BRANCH",
    "PRD_FILE",
    "KSTRL_UI",
    "KSTRL_UI",
    "GUM_FORCE",
    "NO_COLOR",
    "KSTRL_ASCII",
    "KSTRL_ASCII",
    "KSTRL_AGENT_TYPE",
    "KSTRL_AGENT_TYPE",
    "KSTRL_AUTO_CHECKOUT",
    "KSTRL_AUTO_CHECKOUT",
    "KSTRL_AGENT_BUDGET_USD",
    "KSTRL_AGENT_BUDGET_USD",
)


def _clear_kstrl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every kstrl-related env var (legacy names + config families)."""
    for var in _LEGACY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for var in list(os.environ):
        if var.startswith(KSTRL_ENV_PREFIXES):
            monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def isolate_kstrl_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every evolution/experiments/knowledge write path to tmp_path.

    Why chdir is the mechanism: the bare ``EvolutionConfig()``
    constructor defaults ``journal_path``/``experiments_path`` to
    relative ``.kstrl/...`` paths resolved against CWD at write time
    (since R2.1 ``run_factory`` uses ``EvolutionConfig.load(root_dir)``,
    which anchors them to the factory root, but direct constructions in
    tests and legacy call sites remain CWD-relative).
    ``KnowledgeConfig`` likewise defaults ``knowledge_root`` to a
    relative ``.kstrl/knowledge`` and ``KnowledgeConfig.load(None)``
    resolves it against ``Path.cwd()``; there is no env override for the
    root. Pointing CWD at ``tmp_path`` therefore redirects every relative
    default in one move (journal, experiments, knowledge root, snapshot
    dirs, proposals) without touching kstrl source.

    Ambient env is cleared too, so a dev machine exporting FACTORY_* /
    KSTRL_* values cannot alter from_env/load tests.

    R8.9: ``XDG_STATE_HOME`` is pointed at a *sibling* of ``tmp_path`` so
    control-plane files land outside any repo root that uses ``tmp_path``
    itself - otherwise ``control_is_external`` would falsely fail and L3
    clamps would fire in unrelated tests.

    This redirect is convenience; ``guard_repo_kstrl_state`` is the
    enforcement.
    """
    monkeypatch.chdir(tmp_path)
    _clear_kstrl_env(monkeypatch)
    xdg = tmp_path.parent / f"{tmp_path.name}.xdg-state"
    xdg.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    from kstrl.statedir import clear_xdg_state_home_cache

    clear_xdg_state_home_cache()
    return tmp_path


@pytest.fixture(autouse=True)
def forbid_agent_cli_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two locks on the #262 liveness probe, because it spends money.

    Dozens of tests reach ``_agent_preflight`` and a handful get far
    enough to probe. A probe shells out to a real ``claude`` or
    ``codex`` and bills a real account, so:

    1. ``KSTRL_AGENT_PROBE=0`` switches probing off for every test, the
       same switch an operator has.
    2. The subprocess seam is replaced with one that FAILS the test, so a
       test that deliberately re-enables the switch (or reaches the probe
       by a path nobody predicted) still cannot spawn a CLI. Tests that
       exercise the probe re-arm both through
       ``tests.helpers.agent_probe.stub_probe``; a later ``setattr`` on
       the same attribute wins while it is active.

    The result cache is process-global, so it is cleared per test too.
    """
    from kstrl.agents import liveness

    def _forbidden(*args: object, **kwargs: object) -> tuple[list[str], bool]:
        raise AssertionError(
            "A test reached the agent liveness probe and would have spawned "
            "a real CLI. Use tests.helpers.agent_probe.stub_probe, or leave "
            "KSTRL_AGENT_PROBE=0 as this fixture sets it."
        )

    monkeypatch.setenv(liveness.PROBE_ENV_VAR, "0")
    monkeypatch.setattr(liveness, "_stream", _forbidden)
    liveness.reset_probe_cache()


def snapshot_kstrl_dir(kstrl_dir: Path) -> dict[str, str]:
    """Fingerprint every entry under ``kstrl_dir``.

    Maps the path relative to ``kstrl_dir`` to a sha256 hex digest for
    files, ``"dir"`` for directories, and ``"symlink:<target>"`` for
    symlinks. Returns an empty mapping when the directory does not exist,
    so absent-before/absent-after compares equal.
    """
    snapshot: dict[str, str] = {}
    if not kstrl_dir.exists():
        return snapshot
    for path in sorted(kstrl_dir.rglob("*")):
        rel = str(path.relative_to(kstrl_dir))
        if path.is_symlink():
            snapshot[rel] = f"symlink:{os.readlink(path)}"
        elif path.is_dir():
            snapshot[rel] = "dir"
        elif path.is_file():
            snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def describe_snapshot_diff(before: dict[str, str], after: dict[str, str]) -> str:
    """Human-readable created/deleted/modified summary of two snapshots."""
    lines: list[str] = []
    for rel in sorted(after.keys() - before.keys()):
        lines.append(f"  created:  {rel}")
    for rel in sorted(before.keys() - after.keys()):
        lines.append(f"  deleted:  {rel}")
    for rel in sorted(before.keys() & after.keys()):
        if before[rel] != after[rel]:
            lines.append(f"  modified: {rel}")
    return "\n".join(lines)


def _guard_root() -> Path:
    """Root whose .kstrl/ the session guard protects.

    ``KSTRL_SUITE_GUARD_ROOT`` exists so the guard's failure path can be
    exercised end-to-end by a nested pytest run against a synthetic repo
    (tests/test_suite_isolation.py); it is not a knob for disabling the
    guard.
    """
    override = os.environ.get("KSTRL_SUITE_GUARD_ROOT")
    return Path(override) if override else REPO_ROOT


@pytest.fixture(scope="session", autouse=True)
def guard_repo_kstrl_state() -> Generator[None, None, None]:
    """FAIL the run loudly if any test mutated the repo's real .kstrl/.

    This is the enforcement behind the per-test redirect: the redirect
    covers the known relative-default write paths, but any test that
    reaches the real ``.kstrl/`` through an absolute path (or a future
    write path the redirect does not know about) is caught here and fails
    the whole run, so pollution of the learning loop's data can never land
    silently again.
    """
    kstrl_dir = _guard_root() / ".kstrl"
    before = snapshot_kstrl_dir(kstrl_dir)
    yield
    after = snapshot_kstrl_dir(kstrl_dir)
    if before != after:
        pytest.fail(
            "Test suite mutated the repository's real .kstrl/ directory "
            f"({kstrl_dir}).\n"
            "Tests must write only under tmp_path; the autouse "
            "isolate_kstrl_state fixture redirects the default relative "
            ".kstrl/ paths there, so a mutation here means a test used an "
            "absolute path to the repo. Changes detected:\n"
            + describe_snapshot_diff(before, after),
            pytrace=False,
        )


@pytest.fixture
def temp_project(tmp_path: Path) -> Generator[Path, None, None]:
    """Create a temporary project directory with kstrl structure."""
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)

    # Create minimal prompt.md
    (kstrl_dir / "prompt.md").write_text("Test prompt\n")

    # Create minimal prd.json
    (kstrl_dir / "prd.json").write_text('{"branchName": "test-branch", "userStories": []}\n')

    # Save current directory
    original_dir = os.getcwd()
    os.chdir(tmp_path)

    yield tmp_path

    # Restore directory
    os.chdir(original_dir)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear kstrl-related environment variables.

    Covers the legacy single-loop names plus the FACTORY_*/KSTRL_* config
    families. The autouse ``isolate_kstrl_state`` fixture already clears
    these for every test; this fixture remains for tests that want to
    state the dependency explicitly.
    """
    _clear_kstrl_env(monkeypatch)


@pytest.fixture
def no_open_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the R10.7 open-PR bound open for a whole module.

    For the serve and intake suites, none of which is about flow
    control. ``max_open_prs`` defaults to 1 and a ``tmp_path`` is not a
    git checkout, so the real counter fails there and the gate refuses -
    correctly, since an unknown number of open PRs is not zero. Stubbing
    the COUNTER rather than the gate keeps the gate itself in the code
    path those tests execute.

    Opt-in via ``pytestmark = pytest.mark.usefixtures("no_open_prs")``,
    NOT autouse: ``tests/test_flow_control.py`` asserts the bound's real
    behaviour, including the counter, and must not be stubbed out.
    """
    from kstrl.serve import OpenPrCount

    monkeypatch.setattr(
        "kstrl.serve.count_open_kstrl_prs",
        lambda root: OpenPrCount(count=0, saturated=False),
    )


# ---------------------------------------------------------------------------
# A real repository for the reviewer roles (#266)
# ---------------------------------------------------------------------------
#
# The review and security phases stopped taking a diff as a string and
# started reading the worktree they run in, which makes "a git repo with
# a change on it" the minimum context they need. A stub cannot stand in:
# the harness resolves the base ref and measures ``git diff --numstat``
# against it, so a directory that is not a repository is (correctly) an
# infrastructure error rather than a reviewable change.


@dataclass(frozen=True)
class ReviewRepo:
    """A git repo on a feature branch with a committed change on it."""

    path: Path
    base_branch: str
    stat: DiffStat

    @property
    def prd_path(self) -> Path:
        """Where ``make_review_repo`` seeds the component PRD."""
        return self.path / "prd.json"

    def review_json(self, **overrides: object) -> str:
        """A reviewer reply whose diffstat matches this repo's."""
        payload: dict[str, object] = {
            "observedDiffstat": self.stat.as_payload(),
            "stories": [
                {
                    "storyId": "US-001",
                    "storyTitle": "Story US-001",
                    "criteria": [
                        {
                            "criterion": "AC1",
                            "verdict": "pass",
                            "explanation": "checked",
                            "suggestion": "",
                        }
                    ],
                }
            ],
            "concerns": [],
            "exhaustively_searched": True,
            "overallNotes": "",
        }
        payload.update(overrides)
        return json.dumps(payload)

    def security_json(self, **overrides: object) -> str:
        """A security reply whose diffstat matches this repo's."""
        payload: dict[str, object] = {
            "observedDiffstat": self.stat.as_payload(),
            "findings": [],
            "exhaustively_searched": True,
            "overallNotes": "",
        }
        payload.update(overrides)
        return json.dumps(payload)


def git_in(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


DEFAULT_REVIEW_PRD = json.dumps(
    {
        "branchName": "test",
        "userStories": [
            {
                "id": "US-001",
                "title": "Story US-001",
                "acceptanceCriteria": ["AC1"],
                "priority": 1,
                "passes": True,
                "notes": "",
            }
        ],
    }
)


def make_review_repo(
    path: Path,
    files: dict[str, str] | None = None,
    base_files: dict[str, str] | None = None,
) -> ReviewRepo:
    """Create a repo on ``main`` whose feature branch holds ``files``.

    ``base_files`` are committed on ``main`` first, so a test can build a
    change that MODIFIES something rather than only adding.

    No remote, so ``git.resolve_base_ref`` leaves the base as ``main``
    and the reviewer's range and the harness's are the same range by
    construction.

    A ``prd.json`` is written into the worktree AFTER the change commit,
    so it is untracked and contributes nothing to the diffstat: every
    caller needs a PRD on disk and none of them wants it counted.

    An EMPTY ``files`` dict is rejected rather than defaulted (#266
    review finding 5). ``files or {...}`` treated "no files" as "give me
    the default one-file change", so a caller that computed an empty set
    - ``_materialize_fixture_repo`` does exactly that for any input with
    no ``diff --git`` line - would have measured a reviewer against
    fabricated code and reported a NUMBER rather than failing. That is
    the same trap this issue closed in the harness elsewhere, sitting in
    the harness that produces the calibration figure.
    """
    if files is not None and not files:
        raise ValueError(
            "make_review_repo: files={} is an empty change, not a default one. "
            "Pass None for the default, or the files you meant."
        )
    if base_files is not None and not base_files:
        raise ValueError("make_review_repo: base_files={} would leave nothing to commit")
    path.mkdir(parents=True, exist_ok=True)
    git_in(path, "init", "-q")
    git_in(path, "config", "user.email", "kstrl@test.invalid")
    git_in(path, "config", "user.name", "kstrl tests")
    _write_all(path, base_files if base_files is not None else {"README.md": "base\n"})
    git_in(path, "add", "-A")
    git_in(path, "commit", "-qm", "base")
    git_in(path, "branch", "-M", "main")
    git_in(path, "checkout", "-qb", "feature")
    _write_all(
        path,
        files if files is not None else {"src/mod.py": "def added() -> int:\n    return 1\n"},
    )
    git_in(path, "add", "-A")
    git_in(path, "commit", "-qm", "change")
    repo = ReviewRepo(path=path, base_branch="main", stat=get_diff_stat("main", path))
    repo.prd_path.write_text(DEFAULT_REVIEW_PRD, encoding="utf-8")
    return repo


def _write_all(root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def with_observed_diffstat(raw_output: str, repo: ReviewRepo) -> str:
    """Stamp ``repo``'s real diffstat onto a canned reviewer reply.

    #266 made ``observedDiffstat`` mandatory, so a fixture reply without
    one is discarded as unverified coverage - correct, and useless for a
    test whose subject is something else (verdict downgrading, budget
    accounting, transcript streaming). This keeps those tests about
    their own subject; the coverage check has its own tests in
    ``test_review_payload.py``.
    """
    payload = json.loads(raw_output)
    payload["observedDiffstat"] = repo.stat.as_payload()
    return json.dumps(payload)


@pytest.fixture(autouse=True)
def isolate_abandoned_children() -> Generator[None, None, None]:
    """A fresh ``procdispose._ABANDONED`` per test.

    #309 round 2 added a module-level register of children a kill did not
    reach, so that something can still reap them. Module-level means it
    outlives any one test: a double left on it is swept by the next
    test's read, and a test asserting the register is empty fails because
    of what some earlier test abandoned. Both are the cross-test
    contamination this conftest exists to prevent, so the reset lives
    here once rather than in each of the three places that wanted it.

    Only ours is reset. CPython's ``subprocess._active`` is process-wide
    interpreter state that every ``Popen`` sweeps, and it drains itself.
    """
    from kstrl import procdispose

    saved = procdispose._ABANDONED[:]
    procdispose._ABANDONED.clear()
    try:
        yield
    finally:
        procdispose._ABANDONED[:] = saved


@pytest.fixture
def review_repo(tmp_path: Path) -> ReviewRepo:
    """The default one-file change, for tests that only need a repo."""
    return make_review_repo(tmp_path / "review-repo")
