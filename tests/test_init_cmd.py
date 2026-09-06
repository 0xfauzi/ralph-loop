"""Direct tests for ``run_init`` - the scaffold `ks init` writes.

Nothing exercised ``run_init`` itself before this file: the wizard tests
cover ``plan_scaffold`` and the agent-settings write, and the TUI screen
test patches ``run_init`` out. Both issues fixed here (#201 .gitignore,
#256 next steps) are properties of what init WRITES and PRINTS, so they
are tested against the real function with a real UI.
"""

from __future__ import annotations

import inspect
import io
import re
from pathlib import Path

import pytest

from kstrl import init_cmd
from kstrl.cli import cli
from kstrl.init_cmd import (
    _LANGUAGE_IGNORES,
    _LANGUAGE_LOCKFILES,
    GITIGNORE_BLOCK_MARKER,
    NEXT_STEPS,
    _detect_project_context,
    run_init,
)
from kstrl.init_wizard import plan_scaffold
from kstrl.policy import LOCKFILE_BASENAMES
from kstrl.ui.plain import PlainUI
from kstrl.ui.rich_ui import RichUI
from tests.spine_utils import git


def run_init_capturing(root: Path, *, upgrade_prompts: bool = False) -> tuple[int, str]:
    """Run init against ``root``, returning (exit code, printed output)."""
    buffer = io.StringIO()
    code = run_init(root, PlainUI(no_color=True, file=buffer), upgrade_prompts=upgrade_prompts)
    return code, buffer.getvalue()


def make_repo(parent: Path, name: str, manifest: str, contents: str) -> Path:
    """A git repo with one commit, holding one project manifest.

    The manifest is what `_detect_project_context` keys on, so it is the
    only thing that differs between a Python, Rust or Java fixture.
    """
    repo = parent / name
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / manifest).write_text(contents)
    git("add", manifest, cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    return repo


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    """A git repo with one commit and a uv-style Python project in it."""
    return make_repo(tmp_path, "repo", "pyproject.toml", '[project]\nname = "demo"\n')


class TestGitignoreScaffold:
    def test_python_project_gets_build_artifacts_and_kstrl_state(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        code, _ = run_init_capturing(tmp_path)

        assert code == 0
        content = (tmp_path / ".gitignore").read_text()
        for entry in (
            "__pycache__/",
            "*.py[cod]",
            ".venv/",
            ".pytest_cache/",
            ".mypy_cache/",
            ".ruff_cache/",
            "dist/",
            ".kstrl/",
        ):
            assert entry in content, entry

    def test_lockfile_is_never_ignored(self, tmp_path: Path) -> None:
        """#201: uv.lock belongs in version control, not in .gitignore."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        run_init_capturing(tmp_path)

        assert "uv.lock" not in (tmp_path / ".gitignore").read_text()

    def test_block_follows_the_detected_language(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name": "demo"}')

        run_init_capturing(tmp_path)

        content = (tmp_path / ".gitignore").read_text()
        assert "node_modules/" in content
        assert "__pycache__/" not in content

    def test_unknown_language_still_ignores_kstrl_runtime_state(self, tmp_path: Path) -> None:
        run_init_capturing(tmp_path)

        content = (tmp_path / ".gitignore").read_text()
        assert ".kstrl/" in content
        assert "__pycache__/" not in content

    def test_rerun_does_not_duplicate_the_block(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        run_init_capturing(tmp_path)
        after_first = (tmp_path / ".gitignore").read_text()
        _, output = run_init_capturing(tmp_path)

        assert (tmp_path / ".gitignore").read_text() == after_first
        assert after_first.count(GITIGNORE_BLOCK_MARKER) == 1
        assert "already has the kstrl block" in output

    def test_existing_gitignore_is_appended_to_not_rewritten(self, tmp_path: Path) -> None:
        existing = "# mine\nsecrets.env\ndist/\n"
        (tmp_path / ".gitignore").write_text(existing)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        _, output = run_init_capturing(tmp_path)

        content = (tmp_path / ".gitignore").read_text()
        assert content.startswith(existing)
        assert GITIGNORE_BLOCK_MARKER in content
        assert ".kstrl/" in content
        assert "Appended the kstrl block" in output

    def test_append_separates_from_a_file_with_no_trailing_newline(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("secrets.env")

        run_init_capturing(tmp_path)

        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert lines[0] == "secrets.env"
        assert GITIGNORE_BLOCK_MARKER in lines

    def test_empty_gitignore_gains_the_block_without_leading_blanks(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("")

        run_init_capturing(tmp_path)

        assert (tmp_path / ".gitignore").read_text().startswith(GITIGNORE_BLOCK_MARKER)

    def test_a_rule_written_between_the_read_and_the_write_is_not_glued_to(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The read-then-write window ``appendio`` closes (#352).

        ``_gitignore_state`` reads the file and ``_ensure_gitignore``
        appends to it, and until the append was routed the separator was
        decided from that earlier read. The two disagree whenever the
        file changes in between, which is a user saving in an editor or
        a second tool appending a rule.

        The window is simulated at the seam rather than raced, because a
        race that reproduces one run in a thousand is not a test: the
        state read reports the file as empty, and the file on disk holds
        an unterminated rule by the time of the write. Before the
        routing that produced ``secrets.env# kstrl`` on one line, so the
        user's rule and the block header were both wrong. Now the probe
        happens on the handle being written through, so the rule keeps
        its own line.
        """
        path = tmp_path / ".gitignore"
        path.write_text("secrets.env", encoding="utf-8")
        real_state = init_cmd._gitignore_state

        def stale_read(root: Path) -> tuple[str, str | None]:
            action, _ = real_state(root)
            return action, ""

        monkeypatch.setattr(init_cmd, "_gitignore_state", stale_read)
        run_init_capturing(tmp_path)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "secrets.env"
        assert GITIGNORE_BLOCK_MARKER in lines

    def test_user_edits_below_the_block_survive_a_rerun(self, tmp_path: Path) -> None:
        run_init_capturing(tmp_path)
        path = tmp_path / ".gitignore"
        path.write_text(path.read_text() + "\n# added later\nmy-scratch/\n")
        before = path.read_text()

        run_init_capturing(tmp_path)

        assert path.read_text() == before


class TestLockfileTracking:
    def test_untracked_lockfile_is_staged_and_reported(self, python_repo: Path) -> None:
        (python_repo / "uv.lock").write_text("version = 1\n")
        commits_before = git("rev-list", "--count", "HEAD", cwd=python_repo)

        _, output = run_init_capturing(python_repo)

        untracked = git("ls-files", "--others", "--exclude-standard", cwd=python_repo)
        assert git("ls-files", "--", "uv.lock", cwd=python_repo) == "uv.lock"
        assert "uv.lock" not in untracked.split()
        assert "Staged uv.lock" in output
        assert "no commit was created" in output
        assert git("rev-list", "--count", "HEAD", cwd=python_repo) == commits_before

    def test_tracked_lockfile_is_left_alone(self, python_repo: Path) -> None:
        (python_repo / "uv.lock").write_text("version = 1\n")
        git("add", "uv.lock", cwd=python_repo)
        git("commit", "-qm", "lock", cwd=python_repo)

        _, output = run_init_capturing(python_repo)

        assert "uv.lock is tracked" in output
        assert "Staged uv.lock" not in output

    def test_missing_lockfile_is_a_warning_with_the_fix(self, python_repo: Path) -> None:
        _, output = run_init_capturing(python_repo)

        assert "No lockfile yet (uv.lock, poetry.lock, Pipfile.lock)" in output
        assert "create it and commit it" in output

    def test_an_ignored_lockfile_names_the_rule_rather_than_failing(
        self,
        python_repo: Path,
    ) -> None:
        """`git add` refuses an ignored path, so advising `git add
        uv.lock` there is advising the command that just failed. The
        population this hits is the one that copied the old example
        .gitignore, which ignored uv.lock (#201 review)."""
        (python_repo / ".gitignore").write_text("uv.lock\n")
        (python_repo / "uv.lock").write_text("version = 1\n")

        _, output = run_init_capturing(python_repo)

        assert "uv.lock is ignored by .gitignore:1" in output
        assert "Delete that rule and `git add uv.lock`" in output
        assert "Staged uv.lock" not in output
        assert git("ls-files", "--", "uv.lock", cwd=python_repo) == ""

    def test_a_rust_project_stages_cargo_lock(self, tmp_path: Path) -> None:
        """Measured: `cargo test` writes Cargo.lock when it is absent, so
        Rust has the same untracked out-of-scope file Python had."""
        repo = make_repo(tmp_path, "rust", "Cargo.toml", '[package]\nname = "demo"\n')
        (repo / "Cargo.lock").write_text("version = 3\n")

        _, output = run_init_capturing(repo)

        assert "Staged Cargo.lock" in output
        assert git("ls-files", "--", "Cargo.lock", cwd=repo) == "Cargo.lock"

    def test_a_language_with_no_lockfile_says_nothing(self, tmp_path: Path) -> None:
        """Java's empty tuple is a stated policy, not an omission: no
        warning, because there is no lockfile to be missing."""
        repo = make_repo(tmp_path, "java", "pom.xml", "<project/>\n")

        _, output = run_init_capturing(repo)

        assert "No lockfile yet" not in output

    def test_non_git_directory_skips_the_lockfile_step(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (tmp_path / "uv.lock").write_text("version = 1\n")

        _, output = run_init_capturing(tmp_path)

        assert "uv.lock" not in output

    def test_non_python_repo_skips_the_lockfile_step(self, python_repo: Path) -> None:
        (python_repo / "pyproject.toml").unlink()

        _, output = run_init_capturing(python_repo)

        assert "uv.lock" not in output


class TestNextSteps:
    def test_leads_with_the_spec_workflow(self, tmp_path: Path) -> None:
        """#256: the two commands implementing the README headline."""
        _, output = run_init_capturing(tmp_path)

        assert "ks decompose --spec" in output
        assert "ks factory --spec" in output
        spec_at = output.index("ks decompose --spec")
        assert spec_at < output.index("ks run [iterations]")

    def test_single_component_path_is_labelled_as_such(self, tmp_path: Path) -> None:
        _, output = run_init_capturing(tmp_path)

        assert "ks run [iterations]" in output
        assert "no PR" in output

    def test_the_block_fits_an_eighty_column_terminal(self) -> None:
        """Rich word-wraps to the console width, so a longer line
        arrives split across two rows with its comment orphaned.
        80 columns is the narrowest terminal kstrl designs for."""
        longest = max(NEXT_STEPS.splitlines(), key=len)

        assert len(longest) <= 80, longest

    def test_names_the_free_measurement(self, tmp_path: Path) -> None:
        _, output = run_init_capturing(tmp_path)

        assert "ks sense" in output
        assert "ks understand [iterations]" in output
        assert "ks feature [iterations]" in output


class TestRichRendering:
    """The default UI is Rich, so the block has to survive Rich.

    tests/test_rich_ui.py owns the invariant for every method; this is
    the one command whose transcript regressed on it (#256 review).
    """

    def test_the_block_reaches_the_default_ui_intact(self, tmp_path: Path) -> None:
        buffer = io.StringIO()
        run_init(tmp_path, RichUI(no_color=True, file=buffer))

        assert "ks run [iterations]" in buffer.getvalue()


class TestUnreadableGitignore:
    def test_a_non_utf8_gitignore_is_left_alone(self, tmp_path: Path) -> None:
        """UnicodeDecodeError is a ValueError, not an OSError, so an
        unguarded read_text killed `ks init` with a traceback instead of
        an exit code (#201 review)."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_bytes(b"build\xff/\n")

        code, output = run_init_capturing(tmp_path)

        assert code == 0
        assert "could not be read as text" in output
        assert gitignore.read_bytes() == b"build\xff/\n"

    def test_a_gitignore_directory_is_left_alone(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").mkdir()

        code, output = run_init_capturing(tmp_path)

        assert code == 0
        assert "could not be read as text" in output
        assert (tmp_path / ".gitignore").is_dir()


class TestScaffoldContract:
    def test_plan_scaffold_lists_exactly_what_run_init_writes(self, tmp_path: Path) -> None:
        """The wizard preview and the write cannot drift apart silently."""
        code, _ = run_init_capturing(tmp_path)

        assert code == 0
        written = {p for p in tmp_path.rglob("*") if p.is_file()}
        assert written == {entry.path for entry in plan_scaffold(tmp_path)}

    def test_plan_stops_calling_gitignore_an_append_once_init_ran(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("secrets.env\n")

        run_init_capturing(tmp_path)

        planned = {e.path.name: e for e in plan_scaffold(tmp_path)}
        assert planned[".gitignore"].action == "keep"

    def test_every_detected_language_has_an_ignore_block(self) -> None:
        """A language the detector can return but the ignore table cannot
        is #201 recurring in the way that is hardest to see: the scaffold
        writes a block with no build artifacts in it."""
        source = inspect.getsource(_detect_project_context)
        assigned = set(re.findall(r'ctx\["language"\] = "([^"]+)"', source))

        assert assigned, "the language-assignment shape changed; fix this test"
        assert assigned - {"unknown"} <= set(_LANGUAGE_IGNORES)

    def test_every_detected_language_has_a_lockfile_policy(self) -> None:
        """Ignoring a language's build output while saying nothing about
        its lockfile is #201 half-fixed, which is how it survived the
        first pass. An empty tuple is a policy; a missing key is not."""
        assert set(_LANGUAGE_LOCKFILES) == set(_LANGUAGE_IGNORES)

    def test_lockfile_names_come_from_the_policy_vocabulary(self) -> None:
        """policy.LOCKFILE_BASENAMES already decides what counts as a
        lockfile, for the merge-policy size caps. Two lists would drift:
        a name init stages but policy does not know still counts against
        max_lines_changed."""
        named = {name for names in _LANGUAGE_LOCKFILES.values() for name in names}

        assert named <= LOCKFILE_BASENAMES

    def test_every_command_named_in_next_steps_is_real(self) -> None:
        """The block is the first thing a new user reads; a renamed
        command or flag must not leave it printing something that errors."""
        checked = 0
        for line in NEXT_STEPS.splitlines():
            match = re.search(r"\bks ([a-z]+)(.*)", line)
            if not match:
                continue
            name, rest = match.group(1), match.group(2)
            command = cli.commands.get(name)
            assert command is not None, f"`ks {name}` is not a command"
            known = {opt for param in command.params for opt in param.opts}
            assert set(re.findall(r"--[a-z-]+", rest)) <= known, line
            checked += 1

        assert checked >= 5


class TestExitCodes:
    def test_missing_directory_returns_2(self, tmp_path: Path) -> None:
        code, output = run_init_capturing(tmp_path / "nope")

        assert code == 2
        assert "Directory not found" in output

    def test_unparseable_prd_returns_1(self, tmp_path: Path) -> None:
        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "prd.json").write_text("{not json")

        code, output = run_init_capturing(tmp_path)

        assert code == 1
        assert "Invalid JSON" in output
