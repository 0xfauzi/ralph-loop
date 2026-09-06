"""Tests for ``ks sense`` (R10.1): the mechanical sensors run standalone.

Each test builds a real git repository under ``tmp_path`` whose
``[verify]`` commands are fast no-op Python one-liners, then drives the
command through ``CliRunner`` and reads the ``--json`` document back.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from tests.conftest import snapshot_kstrl_dir
from tests.spine_utils import git

_OK_COMMAND = f"{sys.executable} -c 'print(1)'"
_LINT_FAIL_COMMAND = (
    f"{sys.executable} -c 'import sys; print(\"x.py:1:1: E501 line too long\"); sys.exit(1)'"
)

# CheckResult.name values, read from kstrl/verify.py, not guessed.
_ALWAYS_ON_CHECKS = {"test_suite", "typecheck", "linter", "diff_scope", "bad_patterns"}


def _kstrl_toml(lint_command: str = _OK_COMMAND) -> str:
    # json.dumps yields a valid TOML basic string for these commands
    # (the failing lint command carries embedded double quotes).
    return (
        "[verify]\n"
        f"test_command = {json.dumps(_OK_COMMAND)}\n"
        f"typecheck_command = {json.dumps(_OK_COMMAND)}\n"
        f"lint_command = {json.dumps(lint_command)}\n"
    )


def _make_repo(tmp_path: Path, lint_command: str = _OK_COMMAND) -> Path:
    """One-commit git repo on ``main`` with a module, a test and kstrl.toml."""
    root = tmp_path / "proj"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "sense@test", cwd=root)
    git("config", "user.name", "Sense Test", cwd=root)
    (root / "pyproject.toml").write_text('[project]\nname = "proj"\nversion = "0.0.1"\n')
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("def a() -> int:\n    return 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text(
        "from src.a import a\n\n\ndef test_a() -> None:\n    assert a() == 1\n"
    )
    (root / "kstrl.toml").write_text(_kstrl_toml(lint_command))
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    return root


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli, ["sense", *args])


def _sense_json(root: Path, *extra: str) -> tuple[Result, dict[str, Any]]:
    result = _invoke("--root", str(root), "--json", *extra)
    document: dict[str, Any] = json.loads(result.stdout)
    return result, document


def _check(document: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [c for c in document["checks"] if c["name"] == name]
    assert len(matches) == 1, f"expected one {name!r} check, got {matches!r}"
    return matches[0]


def test_sense_passes_on_clean_tree(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result, document = _sense_json(root)

    assert result.exit_code == 0, result.output
    # 3, not 2 (#335); 2, not 1, was #306. The literal is pinned
    # deliberately: this document is a published surface, and the bump
    # is the only thing that tells a reader an ABSENT check row no
    # longer means "turned off in kstrl.toml" - it can now also mean
    # "asked for, measured nothing", which `not_measured` below
    # disambiguates. #335 extended that to the dead-code gate and added
    # a new row name, `dead_code_ruff`, to `checks`.
    assert document["schema_version"] == 3
    assert document["path"] == str(root)
    assert document["passed"] is True
    # Present and empty on a tree where every enabled check measured
    # something: a reader can always index it, and an empty list is a
    # positive statement rather than a missing key.
    assert document["not_measured"] == []
    names = {c["name"] for c in document["checks"]}
    assert _ALWAYS_ON_CHECKS <= names
    for check in document["checks"]:
        assert set(check) == {
            "name",
            "passed",
            "message",
            "details",
            "duration_seconds",
            "findings",
        }
        assert check["passed"] is True
        assert isinstance(check["duration_seconds"], float)


def test_sense_reports_failure_and_exits_1(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, lint_command=_LINT_FAIL_COMMAND)

    result, document = _sense_json(root)

    assert result.exit_code == 1
    assert document["passed"] is False
    linter = _check(document, "linter")
    assert linter["passed"] is False
    assert linter["message"]
    # The other sensors still ran and still pass: no short-circuit.
    assert _check(document, "test_suite")["passed"] is True
    assert _check(document, "typecheck")["passed"] is True


def test_sense_skips_prd_checks_without_prd(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result, document = _sense_json(root)

    assert result.exit_code == 0, result.output
    names = {c["name"] for c in document["checks"]}
    assert "prd_stories" not in names
    assert "fixtures" not in names


def test_sense_runs_prd_checks_with_prd(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    prd = tmp_path / "prd.json"
    prd.write_text(
        json.dumps(
            {
                "branchName": "test",
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Test",
                        "acceptanceCriteria": ["AC"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            }
        )
    )

    result, document = _sense_json(root, "--prd", str(prd))

    assert result.exit_code == 1
    stories = _check(document, "prd_stories")
    assert stories["passed"] is False
    assert "US-001" in "".join(stories["details"])


def test_sense_no_scope_constraints_without_allowed_path(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result, document = _sense_json(root)

    assert result.exit_code == 0, result.output
    scope = _check(document, "diff_scope")
    assert scope["passed"] is True
    assert "No scope constraints" in scope["message"]


def test_sense_enforces_allowed_path(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    git("checkout", "-q", "-b", "feature", cwd=root)
    (root / "src" / "a.py").write_text("def a() -> int:\n    return 2\n")
    git("commit", "-q", "-am", "change a", cwd=root)

    result, document = _sense_json(root, "--allowed-path", "docs/**")

    assert result.exit_code == 1
    # No origin in this repo, so detection reaches the candidate rung
    # and finds the local `main` the feature commit diverged from.
    assert document["base_branch"] == "main"
    scope = _check(document, "diff_scope")
    assert scope["passed"] is False
    assert "src/a.py" in "".join(scope["details"])


def test_sense_exit_2_on_missing_path(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    missing = str(tmp_path / "nonexistent")

    result = _invoke("--root", str(root), "--path", missing)
    assert result.exit_code == 2
    assert result.stderr.startswith("error:")
    assert result.stdout == ""

    result = _invoke("--root", str(root), "--path", missing, "--json")
    assert result.exit_code == 2
    assert result.stderr.startswith("error:")
    document = json.loads(result.stdout)
    assert document["schema_version"] == 3
    assert "error" in document
    assert missing in document["error"]


def test_sense_exit_2_on_malformed_kstrl_toml(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    (root / "kstrl.toml").write_text("[verify\nthis is not toml\n")

    result = _invoke("--root", str(root), "--json")

    assert result.exit_code == 2
    assert result.stderr.startswith("error:")
    assert "error" in json.loads(result.stdout)


def test_sense_writes_nothing(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    kstrl_dir = root / ".kstrl"
    assert not kstrl_dir.exists()
    before = snapshot_kstrl_dir(kstrl_dir)
    tracked_before = git("status", "--porcelain", cwd=root)

    result, _document = _sense_json(root)

    assert result.exit_code == 0, result.output
    assert snapshot_kstrl_dir(kstrl_dir) == before
    assert not kstrl_dir.exists()
    # The no-op verify commands leave the checkout untouched too.
    assert git("status", "--porcelain", cwd=root) == tracked_before


def test_sense_help_lists_every_option() -> None:
    result = CliRunner().invoke(cli, ["sense", "--help"])

    assert result.exit_code == 0
    for option in (
        "--root",
        "--path",
        "--base",
        "--prd",
        "--allowed-path",
        "--json",
        "--ui",
        "--no-color",
    ):
        assert option in result.output


# --- Read-only contract (R10.1 review, P1) ------------------------------
#
# `ks sense` measures the operator's LIVE checkout, not a worktree kstrl
# owns. Before the fix, `[verify] dead_code_cleanup = true` made it run
# `ruff --fix`, `git add -A` and `git commit`: HEAD moved and an
# unrelated untracked file was swept into a commit nobody asked for.


def _dead_code_repo(tmp_path: Path) -> Path:
    """Repo on a feature branch whose diff carries an unused import.

    The unused import is ruff F401 - exactly what the factory's
    dead-code phase auto-removes and commits. `unrelated.txt` is the
    bystander a `git add -A` would have swept in.
    """
    root = _make_repo(tmp_path)
    (root / "kstrl.toml").write_text(_kstrl_toml() + "dead_code_cleanup = true\n")
    git("commit", "-q", "-am", "enable dead code cleanup", cwd=root)
    git("checkout", "-q", "-b", "feature", cwd=root)
    (root / "src" / "b.py").write_text("import os\n\n\ndef b() -> int:\n    return 2\n")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "add b", cwd=root)
    (root / "unrelated.txt").write_text("bystander\n")
    return root


def _without_vulture() -> Callable[..., str | None]:
    """``shutil.which`` with vulture hidden and everything else real.

    Patched for that ONE name and delegating the rest: `ks sense` runs
    in-process under ``CliRunner``, so a blanket patch would take ruff
    and the operator's own commands down with it.
    """
    real_which = shutil.which

    def which(name: str, *args: Any, **kwargs: Any) -> str | None:
        return None if name == "vulture" else real_which(name, *args, **kwargs)

    return which


def test_sense_never_edits_stages_or_commits(tmp_path: Path) -> None:
    root = _dead_code_repo(tmp_path)
    head_before = git("rev-parse", "HEAD", cwd=root)
    log_before = git("log", "--oneline", cwd=root)
    status_before = git("status", "--porcelain", cwd=root)
    b_before = (root / "src" / "b.py").read_text()

    result, document = _sense_json(root)

    assert result.exit_code in (0, 1), result.output
    assert git("rev-parse", "HEAD", cwd=root) == head_before
    assert git("log", "--oneline", cwd=root) == log_before
    # The bystander is still untracked, and still the only change.
    assert git("status", "--porcelain", cwd=root) == status_before
    assert "unrelated.txt" in status_before
    assert (root / "src" / "b.py").read_text() == b_before
    # The full account of what the two phases REPORTED is asserted in
    # test_sense_reports_the_dead_code_phases_separately below, which
    # needs ruff on PATH to have a measurement to read. What this test
    # keeps unconditionally is the read-only contract itself, stated
    # about the document rather than only about the tree: `assert
    # document["checks"]` alone is true of any run that produced one row
    # at all, and would still pass with both dead-code phases deleted -
    # on a machine without ruff, that left nothing in this file
    # asserting anything about either of them.
    dead_code_rows = [c for c in document["checks"] if c["name"].startswith("dead_code")]
    gaps = [g for g in document["not_measured"] if g["check"].startswith("dead_code")]
    assert dead_code_rows or gaps
    assert not any("auto-fixed" in row["message"] for row in dead_code_rows)


@pytest.mark.skipif(shutil.which("ruff") is None, reason="needs ruff on PATH")
def test_sense_reports_the_dead_code_phases_separately(tmp_path: Path) -> None:
    """#335 end to end, on a command where one phase can measure and the
    other cannot.

    ``check_dead_code`` fused the ruff auto-fix and the vulture scan
    into one row, so with vulture absent ``ks sense`` printed
    ``dead_code  pass  ruff reports 1 auto-removable, not removed;
    vulture not installed`` - and ``build_review_prompt`` handed the
    same row to an adversarial reviewer as ``dead_code: PASS``. Omitting
    the fused row would have thrown away the ruff measurement with it,
    which is why the fix is a split and not an omission. Both halves are
    asserted: the ruff row with its real message, and a reason for the
    scan that did not happen.
    """
    root = _dead_code_repo(tmp_path)

    with patch("shutil.which", side_effect=_without_vulture()):
        result, document = _sense_json(root)

    assert result.exit_code == 0, result.output
    ruff_phase = _check(document, "dead_code_ruff")
    assert ruff_phase["passed"] is True
    assert "auto-removable, not removed" in ruff_phase["message"]
    assert [c for c in document["checks"] if c["name"] == "dead_code"] == []
    assert document["not_measured"] == [
        {
            "check": "dead_code",
            "reason": "tool_missing",
            "detail": (
                "vulture is not on PATH and no [verify] dead_code_command is set, "
                "so nothing scanned for dead code"
            ),
        }
    ]
    # Still a pass: a check that measured nothing neither passes nor
    # fails, so the sidecar cannot become a gate by the back door.
    assert document["passed"] is True


@pytest.mark.skipif(shutil.which("ruff") is None, reason="needs ruff on PATH")
def test_sense_table_names_the_dead_code_scan_it_did_not_run(tmp_path: Path) -> None:
    """The terminal half. Most operators read the table, not the JSON."""
    root = _dead_code_repo(tmp_path)

    with patch("shutil.which", side_effect=_without_vulture()):
        result = _invoke("--root", str(root), "--ui", "plain", "--no-color")

    assert result.exit_code == 0, result.output
    assert "dead_code  not measured" in result.output
    assert "vulture is not on PATH" in result.output
    assert "sense: PASS" in result.output


def test_sense_leaves_no_bytecode_or_lint_cache(tmp_path: Path) -> None:
    root = _dead_code_repo(tmp_path)

    result, _document = _sense_json(root)

    assert result.exit_code in (0, 1), result.output
    assert list(root.rglob("__pycache__")) == []
    assert not (root / ".ruff_cache").exists()


# --- Git preflight (R10.1 review, P1) -----------------------------------
#
# The diff-consuming checks read git through the LENIENT helpers, which
# map a bad ref or a missing repository onto an EMPTY file list. Before
# the fix, an unreachable base made diff_scope report "0 files, all
# within scope" and bad_patterns "scanned 0 Python files", and the
# command exited 0 having measured nothing.


def _diverged_repo(tmp_path: Path, base: str = "main") -> Path:
    """Repo whose feature branch carries a change away from ``base``."""
    root = _make_repo(tmp_path)
    git("branch", "-m", "main", base, cwd=root)
    git("checkout", "-q", "-b", "feature", cwd=root)
    (root / "src" / "a.py").write_text("def a() -> int:\n    return 2\n")
    git("commit", "-q", "-am", "change a", cwd=root)
    return root


def test_sense_exit_2_when_explicit_base_is_unreachable(tmp_path: Path) -> None:
    root = _diverged_repo(tmp_path)

    result = _invoke("--root", str(root), "--json", "--base", "no-such-branch")

    assert result.exit_code == 2
    assert result.stderr.startswith("error:")
    document = json.loads(result.stdout)
    assert "no-such-branch" in document["error"]
    assert "from --base" in document["error"]
    # No verdict was invented for a diff git could not produce.
    assert "passed" not in document
    assert "checks" not in document


def test_sense_does_not_demand_a_diff_no_dead_code_phase_reads(tmp_path: Path) -> None:
    """The preflight asks for a base on behalf of the checks that read
    one, and `[verify] dead_code_cleanup` stopped being that question.

    It is one toggle over two phases (#335): `dead_code_ruff` scans `.`,
    and with `[verify] dead_code_command` set the detector is the
    operator's own program, run without the diff read that only ever
    existed to build vulture's argument list. With both diff-reading
    gates off and the toggle on, demanding a base is the same false
    exit 2 mutation_testing is already excluded for - the run that could
    have measured two phases measures none.
    """
    root = _diverged_repo(tmp_path)
    (root / "kstrl.toml").write_text(
        _kstrl_toml()
        + "check_diff_scope = false\n"
        + "check_bad_patterns = false\n"
        + "dead_code_cleanup = true\n"
        + 'dead_code_command = "true"\n'
    )

    result, document = _sense_json(root, "--base", "no-such-branch")

    assert result.exit_code != 2, result.output
    assert [c["name"] for c in document["checks"] if c["name"].startswith("dead_code")]


def test_sense_detects_the_base_branch_that_exists(tmp_path: Path) -> None:
    """No origin, and the base is `trunk` rather than `main` (#259).

    This repo used to be the exit-2 fixture below: detection returned
    the literal `main`, the diff failed, and the test pinned that as
    correct. The ladder now asks the repo, so the diff is real and the
    measurement is against the branch that exists.
    """
    root = _diverged_repo(tmp_path, base="trunk")

    result, document = _sense_json(root, "--allowed-path", "docs/**")

    assert document["base_branch"] == "trunk"
    # A real diff was read against trunk: the out-of-scope file is
    # named, rather than a clean tree nobody managed to measure.
    scope = _check(document, "diff_scope")
    assert scope["passed"] is False
    assert "src/a.py" in "".join(scope["details"])
    assert result.exit_code == 1


def test_sense_exit_2_when_detected_base_is_missing(tmp_path: Path) -> None:
    """No origin and no branch the ladder knows: detection falls back to
    a branch that does not exist, and the fallback must not read as a
    clean diff.

    `release-2.0` is deliberately outside the candidate set, which is
    the only way the fallback is still reachable now that the ladder
    checks the repo (#259). The branch HEAD is on is NOT a rung, so
    standing on `feature` cannot rescue this into a silent empty diff.
    """
    root = _diverged_repo(tmp_path, base="release-2.0")

    result = _invoke("--root", str(root), "--json")

    assert result.exit_code == 2
    document = json.loads(result.stdout)
    assert "'main'" in document["error"]
    assert "--base" in document["error"]


def test_sense_exit_2_outside_a_git_repository(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "kstrl.toml").write_text(_kstrl_toml())

    result = _invoke("--root", str(root), "--json")

    assert result.exit_code == 2
    assert "cannot measure the diff" in json.loads(result.stdout)["error"]


def test_sense_runs_without_git_when_no_check_reads_the_diff(
    tmp_path: Path,
) -> None:
    """The preflight guards the diff-based checks, not the command: turn
    them all off and a plain directory is measurable again."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "kstrl.toml").write_text(
        _kstrl_toml() + "check_diff_scope = false\ncheck_bad_patterns = false\n"
    )

    result, document = _sense_json(root)

    assert result.exit_code == 0, result.output
    assert document["passed"] is True
    names = {c["name"] for c in document["checks"]}
    assert names == {"test_suite", "typecheck", "linter"}


def test_sense_reports_mutation_as_not_measured_not_as_a_pass(tmp_path: Path) -> None:
    """#306 end to end, on a command that can NEVER measure this check.

    `ks sense` is read-only and mutmut works by rewriting the files it
    mutates, so an operator who turns mutation testing on gets no score
    here, ever. Before #306 that produced a green ``mutation_testing``
    row carrying a "skipped" message, which ``all(passed)`` and the LLM
    reviewer's prompt both read as a pass. Removing the row alone would
    be honest and mute. Both halves are asserted here: no row, and a
    reason a human can read.
    """
    root = _make_repo(tmp_path)
    (root / "kstrl.toml").write_text(_kstrl_toml() + "mutation_testing = true\n")

    result, document = _sense_json(root)

    assert result.exit_code == 0, result.output
    assert [c for c in document["checks"] if c["name"] == "mutation_testing"] == []
    assert document["not_measured"] == [
        {
            "check": "mutation_testing",
            "reason": "read_only",
            "detail": "mutmut rewrites the files it mutates and cannot run read-only",
        }
    ]
    # Still a pass: a check that measured nothing neither passes nor
    # fails, so the sidecar cannot become a gate by the back door.
    assert document["passed"] is True


def test_sense_table_names_what_it_did_not_measure(tmp_path: Path) -> None:
    """The terminal half of the same fix.

    Most operators read the table, not the JSON. If the gap reached only
    ``--json`` the sidecar would serve machines and leave the human with
    the silence that omission alone leaves.
    """
    root = _make_repo(tmp_path)
    (root / "kstrl.toml").write_text(_kstrl_toml() + "mutation_testing = true\n")

    result = _invoke("--root", str(root), "--ui", "plain", "--no-color")

    assert result.exit_code == 0, result.output
    assert "mutation_testing  not measured" in result.output
    assert "cannot run read-only" in result.output
    # The verdict line is unchanged: it counts checks, and a gap is not
    # a check.
    assert "sense: PASS" in result.output
