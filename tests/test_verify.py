"""Tests for verify module."""

from __future__ import annotations

import inspect
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest

from kstrl.events import VerificationResultEvent
from kstrl.fixtures import FixturesConfig
from kstrl.review import build_review_prompt
from kstrl.verify import (
    MUTATION_TESTING_CHECK,
    NOT_MEASURED_COMMAND_FAILED,
    NOT_MEASURED_NO_MUTANTS,
    NOT_MEASURED_NO_TARGET,
    NOT_MEASURED_READ_ONLY,
    NOT_MEASURED_TIMED_OUT,
    NOT_MEASURED_TOOL_MISSING,
    CheckResult,
    MechanicalVerification,
    NotMeasured,
    VerificationResult,
    VerifyConfig,
    _count_before,
    check_bad_patterns,
    check_dead_code,
    check_dead_code_ruff,
    check_diff_scope,
    check_linter,
    check_mutation_score,
    check_prd_stories,
    check_self_critique,
    check_test_suite,
    check_typecheck,
    run_mechanical_verification,
)
from tests.helpers.tool_output import tool_output
from tests.helpers.verify_phase import CHEAP_GATES, phase_verify_surfaces

VITEST_FAILURE_OUTPUT = tool_output("vitest-2.1.9-writers-room.txt")


class TestCheckPrdStories:
    def test_all_passing(self, tmp_path: Path) -> None:
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
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        result = check_prd_stories(prd)
        assert result.passed is True

    def test_story_not_passing(self, tmp_path: Path) -> None:
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
        result = check_prd_stories(prd)
        assert result.passed is False
        assert "US-001" in result.details[0]

    def test_invalid_prd(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text("not json")
        result = check_prd_stories(prd)
        assert result.passed is False
        assert "Failed to load" in result.message

    def test_empty_stories(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [],
                }
            )
        )
        result = check_prd_stories(prd)
        assert result.passed is True


class TestCheckTestSuite:
    def test_passing_command(self, tmp_path: Path) -> None:
        result = check_test_suite(tmp_path, command="true", timeout=5.0)
        assert result.passed is True

    def test_failing_command(self, tmp_path: Path) -> None:
        result = check_test_suite(tmp_path, command="false", timeout=5.0)
        assert result.passed is False

    def test_timeout(self, tmp_path: Path) -> None:
        result = check_test_suite(tmp_path, command="sleep 10", timeout=0.1)
        assert result.passed is False
        assert "timed out" in result.message

    def test_vitest_output_reaches_the_gate_parsed(self, tmp_path: Path) -> None:
        """#258: a vitest failure reached the engineer tagged [pytest]
        with every actionable line stripped out.

        The label was the first half of the fix and this is the second:
        the gate dispatches, so the retry detail now carries the failing
        file, its line, the test name and the assertion message that were
        all in the raw output and all dropped.
        """
        script = tmp_path / "fake_vitest.py"
        script.write_text(f"import sys\nsys.stdout.write({VITEST_FAILURE_OUTPUT!r})\nsys.exit(1)\n")
        command = f"{sys.executable} {script}"

        result = check_test_suite(tmp_path, command=command, timeout=30.0)

        assert result.passed is False
        assert result.parsed is not None
        assert result.parsed.tool == "vitest"
        assert "[pytest]" not in "".join(result.details)
        detail = "".join(result.details)
        assert "tests/failing.test.ts:5" in detail
        assert "shows what a real vitest failure looks like" in detail
        assert "expected false to be true" in detail

    def test_output_no_parser_reads_is_still_labelled_with_the_command(
        self, tmp_path: Path
    ) -> None:
        """The #258 labelling floor, kept for a toolchain kstrl has no
        parser for. The command is the one name that cannot be wrong."""
        script = tmp_path / "fake_cargo.py"
        script.write_text("import sys\nprint('error: could not compile `draft`')\nsys.exit(101)\n")
        command = f"{sys.executable} {script}"

        result = check_test_suite(tmp_path, command=command, timeout=30.0)

        assert result.passed is False
        assert result.details[0].startswith(f"[{command}]")
        assert "[pytest]" not in "".join(result.details)

    def test_parsed_pytest_output_keeps_the_tool_label(self, tmp_path: Path) -> None:
        script = tmp_path / "fake_pytest.py"
        script.write_text(
            "import sys\n"
            "print('=========== short test summary info ===========')\n"
            "print('FAILED tests/test_a.py::test_x - AssertionError: nope')\n"
            "print('=========== 1 failed in 0.10s ===========')\n"
            "sys.exit(1)\n"
        )

        result = check_test_suite(tmp_path, command=f"{sys.executable} {script}", timeout=30.0)

        assert result.passed is False
        assert result.details[0].startswith("[pytest]")


class TestLinterGateReadsRuffDefaults:
    """#258 review: the lint gate could not read its own default command.

    `DEFAULT_LINT_COMMAND` is `uv run ruff check .`, and ruff's default
    output format has been `full` since 0.9. The parser read only
    `--output-format=concise`, so the gate's primary parser returned
    zero failures on the harness's own default invocation and the whole
    retry detail was the `Found N errors.` footer.
    """

    def _run(self, tmp_path: Path, fixture: str) -> CheckResult:
        raw = tool_output(fixture)
        script = tmp_path / "fake_ruff.py"
        script.write_text(f"import sys\nsys.stdout.write({raw!r})\nsys.exit(1)\n")
        return check_linter(tmp_path, command=f"{sys.executable} {script}", timeout=30.0)

    @pytest.mark.parametrize(
        "fixture",
        ["ruff-0.16.1-full.txt", "ruff-0.16.1-concise.txt"],
        ids=["default-full", "concise"],
    )
    def test_the_gate_carries_file_line_and_rule(self, tmp_path: Path, fixture: str) -> None:
        result = self._run(tmp_path, fixture)

        assert result.passed is False
        assert result.parsed is not None
        assert result.parsed.tool == "ruff"
        detail = "".join(result.details)
        assert "draft.py:1 [F401]" in detail
        assert "loader.py:1 [invalid-syntax]" in detail


class TestCheckTypecheck:
    def test_passing(self, tmp_path: Path) -> None:
        result = check_typecheck(tmp_path, command="true", timeout=5.0)
        assert result.passed is True

    def test_failing(self, tmp_path: Path) -> None:
        result = check_typecheck(tmp_path, command="false", timeout=5.0)
        assert result.passed is False

    def test_tsc_output_reaches_the_gate_parsed(self, tmp_path: Path) -> None:
        """#258: the typecheck gate parsed everything as mypy, so a real
        `tsc` failure arrived with 0 findings under a `[mypy]` label. The
        gate dispatches now, and the assertion is on the DETAIL rather
        than the label: file, line, error code and message all present."""
        raw = tool_output("tsc-5.6.3-plain.txt")
        script = tmp_path / "fake_tsc.py"
        script.write_text(f"import sys\nsys.stdout.write({raw!r})\nsys.exit(2)\n")
        command = f"{sys.executable} {script}"

        result = check_typecheck(tmp_path, command=command, timeout=30.0)

        assert result.passed is False
        assert result.parsed is not None
        assert result.parsed.tool == "tsc"
        assert "  src/broken.ts:7 [TS2322] Type 'string' is not assignable" in result.details[0]
        assert "[mypy]" not in "".join(result.details)

    def test_output_no_parser_reads_is_still_labelled_with_the_command(
        self, tmp_path: Path
    ) -> None:
        """The #258 labelling floor, kept: an unrecognised toolchain
        falls back to the raw tail named by the command that ran, never
        by a parser that did not read it."""
        script = tmp_path / "fake_checker.py"
        script.write_text("import sys\nprint('go: cannot find package')\nsys.exit(2)\n")
        command = f"{sys.executable} {script}"

        result = check_typecheck(tmp_path, command=command, timeout=30.0)

        assert result.passed is False
        assert result.details[0].startswith(f"[{command}]")
        assert "[mypy]" not in "".join(result.details)


class TestDefaultTypecheckCommand:
    """Gap 2 fix: when ``check_typecheck`` is called without an explicit
    command, it should defer to the project's pyproject.toml mypy
    configuration rather than overriding it with ``uv run mypy .``.

    The end-to-end factory validation run on 2026-05-27 surfaced this
    bug: the agent self-reported all checks passing (using CLAUDE.md's
    contract command), but Phase 1 failed because the factory ran
    ``uv run mypy .`` which scanned tests/ and pulled in errors that
    weren't in the agent's diff."""

    def _default(self, cwd: Path) -> str:
        from kstrl.verify import _default_typecheck_command

        return _default_typecheck_command(cwd)

    def test_no_pyproject_falls_back_to_dot(self, tmp_path: Path) -> None:
        """A directory without pyproject.toml keeps the broad default
        so greenfield projects still get typecheck coverage."""
        assert self._default(tmp_path) == "uv run mypy ."

    def test_pyproject_without_mypy_section_falls_back_to_dot(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'foo'\n",
        )
        assert self._default(tmp_path) == "uv run mypy ."

    def test_mypy_files_present_uses_no_arg_form(self, tmp_path: Path) -> None:
        """With ``[tool.mypy] files`` configured, the default defers
        to the project's mypy config by passing no path argument."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.mypy]\nstrict = true\nfiles = ["my_pkg"]\n',
        )
        assert self._default(tmp_path) == "uv run mypy"

    def test_mypy_packages_present_uses_no_arg_form(
        self,
        tmp_path: Path,
    ) -> None:
        """``[tool.mypy] packages`` also triggers the no-arg default."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.mypy]\nstrict = true\npackages = ["my_pkg"]\n',
        )
        assert self._default(tmp_path) == "uv run mypy"

    def test_mypy_section_without_files_or_packages_falls_back(
        self,
        tmp_path: Path,
    ) -> None:
        """A [tool.mypy] section that only sets strict / python_version
        but doesn't constrain scope keeps the broad default."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.mypy]\nstrict = true\npython_version = "3.11"\n',
        )
        assert self._default(tmp_path) == "uv run mypy ."

    def test_malformed_pyproject_falls_back_to_dot(
        self,
        tmp_path: Path,
    ) -> None:
        """Don't crash on malformed TOML -- fall back to the broad
        default, which the operator can then override explicitly."""
        (tmp_path / "pyproject.toml").write_text("not [valid toml")
        assert self._default(tmp_path) == "uv run mypy ."


class TestCheckDiffScope:
    def test_no_constraints(self, tmp_path: Path) -> None:
        result = check_diff_scope(tmp_path, "main", allowed_paths=None)
        assert result.passed is True

    def test_all_in_scope(self, tmp_path: Path) -> None:
        with patch("kstrl.verify.git.get_diff_names", return_value=["src/main.py"]):
            result = check_diff_scope(tmp_path, "main", allowed_paths=["src/"])
        assert result.passed is True

    def test_out_of_scope(self, tmp_path: Path) -> None:
        with patch(
            "kstrl.verify.git.get_diff_names",
            return_value=["src/main.py", "config/secret.py"],
        ):
            result = check_diff_scope(tmp_path, "main", allowed_paths=["src/"])
        assert result.passed is False
        assert any("config/secret.py" in d for d in result.details)
        # In-scope files are not listed as violations
        assert not any("src/main.py" in d for d in result.details)

    def test_failure_names_base_branch_and_allowed_paths(
        self,
        tmp_path: Path,
    ) -> None:
        """R0.4: the failure details must name the base branch and the
        allowed paths -- without them the retry agent guesses both (the
        recorded e2e run guessed `main`, checked out base-branch content,
        and failed again)."""
        with patch(
            "kstrl.verify.git.get_diff_names",
            return_value=["evil.py"],
        ):
            result = check_diff_scope(
                tmp_path,
                "feat/retrospective-cleanup-2",
                allowed_paths=["src/", "tests/"],
            )
        assert result.passed is False
        assert "feat/retrospective-cleanup-2" in result.message
        joined = "\n".join(result.details)
        assert "Base branch: feat/retrospective-cleanup-2" in joined
        assert "Allowed paths (complete list): src/, tests/" in joined

    def test_retry_context_carries_base_and_full_allowed_paths(
        self,
        tmp_path: Path,
    ) -> None:
        """The retry prompt is built via VerificationResult.as_context()
        (which slices details[:10]) and IterationContext.format_for_prompt.
        The base branch, EVERY allowed path, EVERY shown violation, and the
        truncation marker must survive that pipeline verbatim."""
        from kstrl.context import IterationContext
        from kstrl.verify import VerificationResult

        allowed = [f"pkg{i}/" for i in range(12)]
        violations = [f"rogue{i}.py" for i in range(20)]
        with patch(
            "kstrl.verify.git.get_diff_names",
            return_value=violations,
        ):
            result = check_diff_scope(tmp_path, "main", allowed_paths=allowed)

        verification = VerificationResult(passed=False, checks=[result])
        ctx = IterationContext()
        ctx.add_verification_failure(verification.as_context(), attempt=1)
        prompt_text = ctx.format_for_prompt()

        assert "Base branch: main" in prompt_text
        for path in allowed:
            assert path in prompt_text
        for shown in violations[:15]:
            assert shown in prompt_text
        assert "and 5 more" in prompt_text


class TestCheckBadPatterns:
    def test_clean_files(self, tmp_path: Path) -> None:
        py_file = tmp_path / "clean.py"
        py_file.write_text("x = 1\n")
        with patch("kstrl.verify.git.get_diff_names", return_value=["clean.py"]):
            result = check_bad_patterns(tmp_path, "main")
        assert result.passed is True

    def test_empty_py_file(self, tmp_path: Path) -> None:
        py_file = tmp_path / "empty.py"
        py_file.write_text("")
        with patch("kstrl.verify.git.get_diff_names", return_value=["empty.py"]):
            result = check_bad_patterns(tmp_path, "main")
        assert result.passed is False
        assert any("empty" in d for d in result.details)

    def test_syntax_error(self, tmp_path: Path) -> None:
        py_file = tmp_path / "bad.py"
        py_file.write_text("def f(\n")
        with patch("kstrl.verify.git.get_diff_names", return_value=["bad.py"]):
            result = check_bad_patterns(tmp_path, "main")
        assert result.passed is False
        assert any("syntax" in d.lower() for d in result.details)

    def test_secret_detected(self, tmp_path: Path) -> None:
        py_file = tmp_path / "leak.py"
        py_file.write_text('API_KEY = "sk-abcdefghijklmnopqrstuvwxyz"\n')
        with patch("kstrl.verify.git.get_diff_names", return_value=["leak.py"]):
            result = check_bad_patterns(tmp_path, "main")
        assert result.passed is False
        assert any("secret" in d.lower() for d in result.details)

    def test_non_py_files_skipped(self, tmp_path: Path) -> None:
        txt_file = tmp_path / "data.txt"
        txt_file.write_text("")
        with patch("kstrl.verify.git.get_diff_names", return_value=["data.txt"]):
            result = check_bad_patterns(tmp_path, "main")
        assert result.passed is True


class TestRunMechanicalVerification:
    def test_all_pass(self, tmp_path: Path) -> None:
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
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        config = VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        )
        result = run_mechanical_verification(
            tmp_path,
            prd,
            "main",
            None,
            config,
        )
        assert result.passed is True
        assert len(result.checks) == 4  # prd + test + typecheck + lint

    def test_partial_failure(self, tmp_path: Path) -> None:
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
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        config = VerifyConfig(
            test_command="false",  # Tests fail
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        )
        result = run_mechanical_verification(
            tmp_path,
            prd,
            "main",
            None,
            config,
        )
        assert result.passed is False
        # All checks should have run (no short-circuit)
        assert len(result.checks) == 4
        assert result.checks[0].passed is True  # PRD stories
        assert result.checks[1].passed is False  # Test suite
        assert result.checks[2].passed is True  # Typecheck

    def test_as_context_formatting(self) -> None:
        result = VerificationResult(
            passed=False,
            checks=[
                CheckResult("test_suite", False, "2 failures", ["FAIL: test_a"]),
                CheckResult("typecheck", True, "ok"),
            ],
        )
        ctx = result.as_context()
        assert "test_suite: FAIL" in ctx
        assert "typecheck" not in ctx  # passed checks excluded


class TestCheckSelfCritique:
    """Tests for the engineer-prompt self-critique mechanical check."""

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        result = check_self_critique(tmp_path / "missing.txt")
        assert result.passed is False
        assert "Could not read" in result.message

    def test_no_block_fails(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1 - US-001\n- did stuff\n- ran tests\n",
        )
        result = check_self_critique(progress)
        assert result.passed is False
        assert "No '## Self-Critique'" in result.message

    def test_block_with_three_bullets_passes(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1 - US-001\n"
            "- did stuff\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: invalid input crashes parser\n"
            "  - Failure mode 2: concurrent writes race\n"
            "  - Failure mode 3: timeout swallowed silently\n"
        )
        result = check_self_critique(progress)
        assert result.passed is True
        assert "3 failure modes" in result.message

    def test_fewer_than_min_fails(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1 - US-001\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: x\n"
            "  - Failure mode 2: y\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False
        assert "2 bullets" in result.message

    def test_tbd_bullets_dont_count(self, tmp_path: Path) -> None:
        """The check should reject placeholder content like TBD/TODO/N/A."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "- **Self-Critique:**\n"
            "  - TBD\n"
            "  - TODO write later\n"
            "  - N/A\n"
            "  - Failure mode: empty input crashes the parser\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False  # only 1 substantive bullet

    def test_latest_iteration_block_used(self, tmp_path: Path) -> None:
        """Multiple Self-Critique blocks in one file - the LAST one is
        evaluated so previous iterations don't carry the current one."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "- **Self-Critique:**\n"
            "  - mode1: x\n"
            "  - mode2: y\n"
            "  - mode3: z\n"
            "\n## Iteration 2\n"
            "- **Self-Critique:**\n"
            "  - only one this time\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False  # latest iteration has only 1

    def test_h2_style_heading_recognized(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "Some narrative.\n"
            "## Self-Critique\n"
            "- failure A: detailed reason\n"
            "- failure B: detailed reason\n"
            "- failure C: detailed reason\n"
        )
        result = check_self_critique(progress)
        assert result.passed is True

    def test_does_not_match_self_critique_in_prose(self, tmp_path: Path) -> None:
        """A reference to 'self-critique' in body text must not be
        treated as the heading. Only proper headings count."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "I wrote a self-critique that lists three failure modes:\n"
            "- mode A\n"
            "- mode B\n"
            "- mode C\n"
            "\nDone.\n"
        )
        result = check_self_critique(progress)
        assert result.passed is False
        assert "No '## Self-Critique'" in result.message

    def test_fuzz_corpus_of_accepted_headings(self, tmp_path: Path) -> None:
        """Forms the engineer prompt's loose phrasing might produce."""
        accepted = [
            "## Self-Critique",
            "## self-critique",  # case-insensitive
            "### Self-Critique",  # H3 also OK
            "- **Self-Critique:**",
            "- **Self-Critique**",
            "* **Self-Critique:**",
            "## Self Critique",  # space instead of hyphen
        ]
        for heading in accepted:
            progress = tmp_path / "progress.txt"
            progress.write_text(
                f"## Iteration 1\nbody\n{heading}\n"
                "- failure 1: realistic description with details\n"
                "- failure 2: realistic description with details\n"
                "- failure 3: realistic description with details\n"
            )
            result = check_self_critique(progress)
            assert result.passed is True, f"heading {heading!r} should be accepted"

    def test_fuzz_corpus_of_rejected_lines(self, tmp_path: Path) -> None:
        """Lines that mention self-critique but aren't a heading."""
        rejected = [
            "the self-critique below lists failure modes",
            "self-critique: yes",  # no leading marker
            "**self-critique:**",  # bare bold, no list marker
            "selfcritique",  # no separator
            "see Self-Critique above",
        ]
        for line in rejected:
            progress = tmp_path / "progress.txt"
            progress.write_text(
                f"## Iteration 1\n{line}\n"
                "- failure: realistic\n"
                "- failure: realistic\n"
                "- failure: realistic\n"
            )
            result = check_self_critique(progress)
            assert result.passed is False, f"line {line!r} should not be treated as heading"

    def test_missing_block_in_latest_iteration_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """R5.4 regression: an earlier iteration's Self-Critique block
        must not satisfy the check when the LATEST entry omits it."""
        earlier_entries = [
            # Documented '## [YYYY-MM-DD] - [Story ID]' form
            "## [2026-07-17] - US-001\n",
            # Unbracketed date form
            "## 2026-07-17 - US-001\n",
            # Loose 'Iteration N' form
            "## Iteration 1\n",
        ]
        for earlier in earlier_entries:
            progress = tmp_path / "progress.txt"
            progress.write_text(
                f"{earlier}"
                "- implemented the parser\n"
                "- **Self-Critique:**\n"
                "  - Failure mode 1: realistic detail\n"
                "  - Failure mode 2: realistic detail\n"
                "  - Failure mode 3: realistic detail\n"
                "---\n"
                "## [2026-07-18] - US-002\n"
                "- implemented the serializer\n"
                "- ran the tests\n"
                "---\n"
            )
            result = check_self_critique(progress, min_bullets=3)
            assert result.passed is False, (
                f"earlier entry {earlier!r} must not mask the latest entry's missing block"
            )
            assert "latest iteration entry" in result.message

    def test_next_section_bullets_do_not_inflate_count(
        self,
        tmp_path: Path,
    ) -> None:
        """R5.4 regression: bullets belonging to a FOLLOWING bold-label
        section (e.g. Interpretations in the engineer prompt's format)
        must not count toward min_bullets."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## [2026-07-18] - US-002\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: only one substantive failure mode\n"
            "- **Interpretations** (PRD was ambiguous): assumed idempotency\n"
            "- **Learnings:**\n"
            "  - discovered a pattern\n"
            "  - hit a gotcha\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False
        assert "1 bullets" in result.message

    def test_default_prompt_entry_format_counts_exact_bullets(
        self,
        tmp_path: Path,
    ) -> None:
        """Positive control: a full entry in the engineer prompt's
        documented Progress Format passes with EXACTLY the critique
        bullets counted - surrounding sections contribute nothing."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "# kstrl Progress Log\n\n"
            "## Codebase Patterns\n"
            "- (add reusable patterns here)\n\n"
            "## Iteration Notes\n"
            "- (append entries below using the format in prompt.md)\n\n"
            "---\n"
            "## [2026-07-18] - US-002\n"
            "- What was implemented: the serializer\n"
            "- Files changed: serializer.py\n"
            "- Verification run: uv run pytest -q\n"
            "- **Learnings:**\n"
            "  - Patterns discovered: registry pattern\n"
            "  - Gotchas encountered: import cycle\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: malformed input crashes the encoder\n"
            "  - Failure mode 2: concurrent flushes interleave\n"
            "  - Failure mode 3: timeout error swallowed silently\n"
            "- **Interpretations** (only if PRD was ambiguous): assumed utf-8\n"
            "---\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is True
        assert "3 failure modes" in result.message

    def test_entry_separator_terminates_bullet_count(
        self,
        tmp_path: Path,
    ) -> None:
        """Bullets after the closing `---` belong to no entry and must
        not count."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## [2026-07-18] - US-002\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: realistic detail\n"
            "---\n"
            "- stray bullet after the separator\n"
            "- another stray bullet\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False
        assert "1 bullets" in result.message

    def test_no_iteration_heading_falls_back_to_whole_file(
        self,
        tmp_path: Path,
    ) -> None:
        """Free-form progress files without a recognized iteration
        heading are treated as a single entry (documented fallback)."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "Free-form notes, no iteration headings anywhere.\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: realistic detail\n"
            "  - Failure mode 2: realistic detail\n"
            "  - Failure mode 3: realistic detail\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is True


def _mutmut_completed(stdout: str) -> CompletedProcess[str]:
    """A finished ``mutmut`` invocation with ``stdout`` to parse."""
    return CompletedProcess(args="mutmut", returncode=0, stdout=stdout, stderr="")


#: ``CHEAP_GATES`` plus the opt-in mutation check, so the only row worth
#: looking at is the one under test. Reused from ``tests.helpers`` rather
#: than patching ``check_test_suite`` / ``check_typecheck`` /
#: ``check_linter`` / ``check_diff_scope`` / ``check_bad_patterns`` by
#: hand, which is a 13-line ``ExitStack`` this file already contained one
#: copy of.
MUTATION_GATES = replace(CHEAP_GATES, mutation_testing=True)


def _verify_with_stub_mutation(
    root: Path,
    measured: CheckResult | NotMeasured,
    *,
    config: VerifyConfig | None = None,
    **kwargs: bool,
) -> VerificationResult:
    """Phase 1 over ``root`` with ``check_mutation_score`` stubbed.

    ``measured`` is whichever of the two things that function can now
    return, so a caller has to say which arm it is exercising - a row or
    a gap - instead of passing None and letting the plumbing decide.

    The whole result, not just the mutation rows: a test that only ever
    sees the rows cannot tell whether a FAIL still fails the run, nor
    whether a gap was recorded beside it.
    """
    with patch("kstrl.verify.check_mutation_score", return_value=measured):
        return run_mechanical_verification(
            root,
            None,
            "main",
            None,
            config or MUTATION_GATES,
            **kwargs,
        )


def _mutation_rows(result: VerificationResult) -> list[CheckResult]:
    return [c for c in result.checks if c.name == "mutation_testing"]


def _gap(detail: str = "mutmut is not on PATH") -> NotMeasured:
    """The record every not-measured test in this module works from."""
    return NotMeasured(MUTATION_TESTING_CHECK, NOT_MEASURED_TOOL_MISSING, detail)


def _result_with_gap() -> VerificationResult:
    """A passing result carrying one gap beside one real row.

    The row matters: a result with nothing in ``checks`` cannot show
    that the gap stayed OUT of them.
    """
    return VerificationResult(
        passed=True,
        checks=[CheckResult("test_suite", True, "Tests passed")],
        not_measured=[_gap()],
    )


class TestCheckMutationScore:
    """#306: every path that measures no score returns NotMeasured.

    Each test names its own ``reason`` token AND asserts that the
    collaborators downstream of the guard it is named for never ran.
    Both halves are the round-2 fix, and they exist because round 1
    asserted only ``result is None``: with that assertion, DELETING the
    missing-binary guard, the no-Python-file guard or the timeout guard
    left all three of their own tests passing, because a later exit also
    returned None. Absence had five causes and the tests could not tell
    them apart, which is the same defect that made the read-only test
    wrong. A test that cannot fail for its own named reason is not a
    test.
    """

    def test_mutmut_not_installed_measures_nothing(self, tmp_path: Path) -> None:
        """Everything downstream is stubbed to SUCCEED, deliberately: a
        deleted guard then reaches a 90% pass rather than an exception,
        so this fails on its own reason and not on a TypeError."""
        with (
            patch("shutil.which", return_value=None),
            patch(
                "kstrl.verify.git.get_diff_names",
                return_value=["src/main.py"],
            ) as diff_names,
            patch(
                "kstrl.verify.run_scrubbed",
                return_value=_mutmut_completed("9 killed\n1 survived\n"),
            ) as run_scrubbed,
        ):
            result = check_mutation_score(tmp_path, "main")
        assert isinstance(result, NotMeasured)
        assert result.reason == NOT_MEASURED_TOOL_MISSING
        # The guard's own observable: nothing downstream of it ran.
        diff_names.assert_not_called()
        run_scrubbed.assert_not_called()

    @pytest.mark.parametrize(
        "changed",
        [["readme.md"], ["test_main.py", "tests/test_foo.py"]],
        ids=["no-python", "only-test-python"],
    )
    def test_no_mutable_file_changed_measures_nothing(
        self,
        tmp_path: Path,
        changed: list[str],
    ) -> None:
        """Both halves of the filter: the ``.py`` clause and the
        ``startswith("test")`` clause."""
        with (
            patch("shutil.which", return_value="/usr/bin/mutmut"),
            patch("kstrl.verify.git.get_diff_names", return_value=changed),
            patch(
                "kstrl.verify.run_scrubbed",
                return_value=_mutmut_completed("9 killed\n1 survived\n"),
            ) as run_scrubbed,
        ):
            result = check_mutation_score(tmp_path, "main")
        assert isinstance(result, NotMeasured)
        assert result.reason == NOT_MEASURED_NO_TARGET
        # Delete the guard and mutmut is invoked with an EMPTY
        # --paths-to-mutate and this stub hands back a 90% score, so the
        # check reports a pass it never measured. That is the shape of
        # #306 itself.
        run_scrubbed.assert_not_called()

    def test_timeout_measures_nothing(self, tmp_path: Path) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/mutmut"),
            patch("kstrl.verify.git.get_diff_names", return_value=["src/main.py"]),
            patch(
                "kstrl.verify.run_scrubbed",
                side_effect=TimeoutExpired("mutmut", 600),
            ) as run_scrubbed,
        ):
            result = check_mutation_score(tmp_path, "main", timeout=600)
        assert isinstance(result, NotMeasured)
        assert result.reason == NOT_MEASURED_TIMED_OUT
        # Exactly the `mutmut run` call and no `mutmut results` after
        # it: returning on timeout is the point, and falling through
        # would read counts off a run that never finished.
        assert run_scrubbed.call_count == 1
        assert "mutmut run" in str(run_scrubbed.call_args)

    def test_a_failing_mutmut_is_not_an_empty_one(self, tmp_path: Path) -> None:
        """A broken mutation command and a clean run with nothing to do
        produce byte-identical empty output; only ``returncode``
        separates them, and round 1 reported both as the same silence."""
        broken = CompletedProcess(
            args="mutmut",
            returncode=2,
            stdout="",
            stderr="error: unrecognised option --paths-to-mutate\n",
        )
        with (
            patch("shutil.which", return_value="/usr/bin/mutmut"),
            patch("kstrl.verify.git.get_diff_names", return_value=["src/main.py"]),
            patch("kstrl.verify.run_scrubbed", return_value=broken),
        ):
            result = check_mutation_score(tmp_path, "main")
        assert isinstance(result, NotMeasured)
        assert result.reason == NOT_MEASURED_COMMAND_FAILED
        assert "unrecognised option" in result.detail

    def test_no_mutants_generated_measures_nothing(self, tmp_path: Path) -> None:
        """mutmut exited zero and produced neither count. 0/0 is not a
        score, and round 1 reported it as ``passed=True``."""
        with (
            patch("shutil.which", return_value="/usr/bin/mutmut"),
            patch("kstrl.verify.git.get_diff_names", return_value=["src/main.py"]),
            patch(
                "kstrl.verify.run_scrubbed",
                return_value=_mutmut_completed("nothing to report\n"),
            ),
        ):
            result = check_mutation_score(tmp_path, "main")
        assert isinstance(result, NotMeasured)
        assert result.reason == NOT_MEASURED_NO_MUTANTS

    def test_every_reason_is_a_distinct_token(self) -> None:
        """Six reasons, six values. A token that duplicated another would
        make two of them indistinguishable in `ks sense --json` and in
        `events.jsonl` while every test above still passed."""
        reasons = [
            NOT_MEASURED_READ_ONLY,
            NOT_MEASURED_TOOL_MISSING,
            NOT_MEASURED_NO_TARGET,
            NOT_MEASURED_TIMED_OUT,
            NOT_MEASURED_COMMAND_FAILED,
            NOT_MEASURED_NO_MUTANTS,
        ]
        assert len(set(reasons)) == len(reasons) == 6

    @pytest.mark.parametrize(
        "stdout, passed, pct",
        [
            ("9 killed\n1 survived\n", True, "90.0%"),
            ("1 killed\n9 survived\n", False, "10.0%"),
        ],
        ids=["above-threshold", "below-threshold"],
    )
    def test_a_measured_score_is_the_only_thing_that_gets_a_row(
        self,
        tmp_path: Path,
        stdout: str,
        passed: bool,
        pct: str,
    ) -> None:
        """The other half of the contract: a real score still reports.

        Without these two the class would be satisfied by a function
        that returns NotMeasured unconditionally.
        """
        with (
            patch("shutil.which", return_value="/usr/bin/mutmut"),
            patch("kstrl.verify.git.get_diff_names", return_value=["src/main.py"]),
            patch("kstrl.verify.run_scrubbed", return_value=_mutmut_completed(stdout)),
        ):
            result = check_mutation_score(tmp_path, "main", threshold=50.0)
        assert isinstance(result, CheckResult)
        assert result.name == "mutation_testing"
        assert result.passed is passed
        assert pct in result.message


class TestCountBefore:
    """The mutmut counter, extracted in #306 round 2."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("12 killed", 12),
            ("Killed: nope", 0),
            ("", 0),
            ("killed", 0),
            ("3 killed\n7 killed\n", 7),
            ("- 5 KILLED mutants", 5),
        ],
        ids=[
            "plain",
            "non-numeric",
            "empty",
            "no-preceding-token",
            "last-wins",
            "case-and-prose",
        ],
    )
    def test_counts(self, text: str, expected: int) -> None:
        assert _count_before("killed", text) == expected

    def test_a_coloured_line_counts_as_zero(self) -> None:
        """The limit, named rather than left to look robust.

        Every other tool-output parser in this repo reads text that has
        been through ``parsers.strip_ansi``; this one reads mutmut's
        stdout raw, so an ANSI-coloured count is invisible to it. That
        is pre-existing behaviour carried through the #306 extraction,
        not something it introduced, and stripping here would be a
        behaviour change with no measurement behind it. What it must not
        do is go unrecorded: 0 killed and 0 survived is
        ``no_mutants``, and this is a way to reach that wrongly.
        """
        assert _count_before("killed", "\x1b[32m12\x1b[0m killed") == 0

    def test_words_do_not_bleed(self) -> None:
        """``survived`` must not be read off a ``killed`` line. Two
        independent loops made that structurally impossible; one shared
        helper has to keep it true."""
        text = "9 killed\n1 survived\n"
        assert _count_before("killed", text) == 9
        assert _count_before("survived", text) == 1


class TestMutationRowOnlyExistsWhenMeasured:
    """#306 at the level every consumer reads: ``checks``, and the
    sidecar beside it."""

    def test_no_measurement_appends_no_row_but_records_why(self, tmp_path: Path) -> None:
        gap = _gap()
        result = _verify_with_stub_mutation(tmp_path, gap)
        assert _mutation_rows(result) == []
        assert result.not_measured == [gap]

    def test_a_measurement_appends_its_row_and_records_no_gap(self, tmp_path: Path) -> None:
        """The other half: without this, dropping every row would pass."""
        measured = CheckResult("mutation_testing", True, "Mutation score 90.0%")
        result = _verify_with_stub_mutation(tmp_path, measured)
        assert _mutation_rows(result) == [measured]
        assert result.not_measured == []
        assert result.passed is True

    def test_a_failing_measurement_still_fails_the_run(self, tmp_path: Path) -> None:
        """Omission must not become a way to lose a real FAIL.

        ``result.passed`` is the assertion that earns the name. Without
        it this is the test above with a boolean flipped, and an
        aggregation that skipped the mutation row would satisfy both.
        """
        measured = CheckResult("mutation_testing", False, "below threshold")
        result = _verify_with_stub_mutation(tmp_path, measured)
        assert _mutation_rows(result) == [measured]
        assert result.passed is False

    def test_toggle_off_records_nothing_at_all(self, tmp_path: Path) -> None:
        """The sixth state, and the line the sidecar draws. A check the
        operator turned OFF gets no row AND no gap: silence is a complete
        answer to a question nobody asked, and an incomplete one to a
        question they did.

        ``measured`` is a row deliberately, so a toggle read as ``True``
        would produce one and fail here.
        """
        result = _verify_with_stub_mutation(
            tmp_path,
            CheckResult("mutation_testing", True),
            config=CHEAP_GATES,
        )
        assert _mutation_rows(result) == []
        assert result.not_measured == []


class TestNotMeasuredIsReportedButNeverGates:
    """#306 round 2: the sidecar is visible everywhere a human looks and
    invisible everywhere a machine decides.

    Round 1 omitted the row and stopped there, which was honest and
    mute: an operator who set ``mutation_testing = true`` with no mutmut
    installed got a clean report and a gate that silently never fired.
    """

    def test_a_gap_does_not_fail_the_run(self, tmp_path: Path) -> None:
        """Through the real aggregation, not the constructor: a check
        that measured nothing neither passes nor fails."""
        assert _verify_with_stub_mutation(tmp_path, _gap()).passed is True

    def test_a_gap_is_never_retry_context(self) -> None:
        """``as_context`` feeds the engineer's next iteration. A missing
        binary is not something a diff can fix, so it must not appear
        there and spend ``repair_max_runs`` proving it."""
        assert _result_with_gap().as_context() == ""

    def test_a_gap_is_never_a_check_row(self) -> None:
        """The #306 invariant: nothing in the sidecar can be read as a
        pass by ``all(c.passed ...)``, ``report_lines``' verdict column,
        the reviewer prompt or ``ks sense --json``'s ``checks`` array,
        because it is not in ``checks``."""
        assert [c.name for c in _result_with_gap().checks] == ["test_suite"]

    def test_a_gap_is_rendered_in_the_report(self) -> None:
        """The half that makes absence readable rather than merely
        honest."""
        lines = _result_with_gap().report_lines(durations=False)
        assert any("mutation_testing  not measured" in line for line in lines)
        assert any("mutmut is not on PATH" in line for line in lines)

    def test_the_rendered_gap_never_says_pass(self) -> None:
        """The word matters: `pass` in the verdict column is exactly what
        #306 removed, so the sidecar line must not reintroduce it."""
        line = next(
            line for line in _result_with_gap().report_lines() if "mutation_testing" in line
        )
        assert "not measured" in line
        assert "  pass  " not in line


class TestPhase1SaysWhatItDidNotMeasure:
    """#306 through the REAL Phase 1, not the dataclass.

    ``TestNotMeasuredIsReportedButNeverGates`` above proves the object
    behaves; this proves the pipeline actually carries it to the two
    places anyone would look. Both are needed: a sidecar the pipeline
    drops is the same silence as no sidecar, and it would leave every
    assertion in that class green.
    """

    def test_the_operator_is_warned_with_the_reason_and_the_detail(
        self,
        tmp_path: Path,
    ) -> None:
        """The warning is the whole point of round 2: before it, an
        enabled mutation gate that could never run looked identical to
        one that ran and passed."""
        surfaces = phase_verify_surfaces(tmp_path, _result_with_gap())

        assert "mutation_testing not measured" in surfaces.narration
        assert f"({NOT_MEASURED_TOOL_MISSING})" in surfaces.narration
        assert "mutmut is not on PATH" in surfaces.narration

    def test_the_event_carries_the_gap_beside_the_checks_not_inside_them(
        self,
        tmp_path: Path,
    ) -> None:
        """``events.jsonl`` is the integration substrate. A consumer
        counting ``checks`` must not see a gap there, and a consumer
        asking "did anything go unmeasured" must have somewhere to
        look."""
        surfaces = phase_verify_surfaces(tmp_path, _result_with_gap())
        emitted = [e for e in surfaces.events if isinstance(e, VerificationResultEvent)]

        assert len(emitted) == 1
        assert emitted[0].not_measured == ("mutation_testing:tool_missing",)
        assert emitted[0].checks == ("test_suite",)
        assert emitted[0].passed is True

    def test_a_run_with_no_gaps_says_nothing(self, tmp_path: Path) -> None:
        """The quiet default. If this warned on every run the warning
        would be noise within a day and unread within a week."""
        clean = VerificationResult(
            passed=True,
            checks=[CheckResult("test_suite", True, "Tests passed")],
        )

        surfaces = phase_verify_surfaces(tmp_path, clean)
        emitted = [e for e in surfaces.events if isinstance(e, VerificationResultEvent)]

        assert "not measured" not in surfaces.narration
        assert len(emitted) == 1
        assert emitted[0].not_measured == ()

    def test_a_gap_does_not_route_phase_1_to_a_failure(self, tmp_path: Path) -> None:
        """Stated once at the layer that decides. The no-FAIL reasoning
        is that a missing binary is not something the engineer's next
        diff can fix, so this must not become a retry or a halt by
        arriving through the pipeline instead of the aggregation."""
        surfaces = phase_verify_surfaces(tmp_path, _result_with_gap())

        assert surfaces.result.ran is True
        assert surfaces.result.failure is None


class TestVerificationSignatureIsKeywordOnly:
    """#316: nothing after ``config`` can be passed positionally.

    Three of the arguments in that block - ``allowed_paths_error``,
    ``harness_paths`` and, beside them, ``allowed_paths`` - are
    near-identical types carrying opposite meanings. See
    ``verify.MechanicalVerification`` for why the type system was not
    catching a transposition between them.

    Two tests, not three: an "everything after index 4 is keyword-only"
    assertion is derivable from the exact positional list below, since
    the function has no ``*args`` or ``**kwargs`` for the two to
    disagree about.
    """

    def test_only_the_first_five_parameters_are_positional(self) -> None:
        params = list(inspect.signature(run_mechanical_verification).parameters.values())
        positional = [p.name for p in params if p.kind is p.POSITIONAL_OR_KEYWORD]
        assert positional == [
            "worktree_path",
            "prd_path",
            "base_branch",
            "allowed_paths",
            "config",
        ]

    def test_a_sixth_positional_argument_is_refused(self) -> None:
        """The rule as behaviour, not only as a signature assertion.

        ``allowed_paths_error`` sat in slot six. ``Signature.bind``
        rather than a real call: ``pytest.raises`` does not stop the body
        running, so if the ``*`` this test guards were ever removed, a
        real call here would shell out to the default test, typecheck and
        lint commands at 300s timeouts apiece to prove a signature point.
        ``bind`` raises the same ``TypeError`` without entering the
        function.
        """
        with pytest.raises(TypeError, match="positional argument"):
            inspect.signature(run_mechanical_verification).bind(
                Path("."),
                None,
                "main",
                None,
                VerifyConfig(),
                "scope unreadable",
            )


def test_the_protocol_says_exactly_what_the_function_says() -> None:
    """#316: ``MechanicalVerification`` has not drifted from its function.

    One assertion rather than four, because ``Signature.__eq__``
    compares name, kind, annotation, default VALUE and return
    annotation in one go - it is strictly stronger than the four
    field-by-field checks it replaces, which let a Protocol saying
    ``autonomy_level: int = 1`` through against a function saying 0.

    Most drift is caught before this runs: ``kstrl/factory.py`` binds
    the real function to the Protocol-typed field, so a Protocol with a
    wrong annotation, a missing default or a wrong return type is a
    ``mypy --strict`` error in CI. Measured on this branch, widening
    ``allowed_paths_error`` in the Protocol produced
    ``factory.py: error: Argument "run_mechanical_verification" to
    "PipelineHooks" has incompatible type ... expected
    "MechanicalVerification"``. What mypy CANNOT see is drift that
    leaves the function MORE permissive than the Protocol - a parameter
    the function grows and the Protocol lacks, or a kind that relaxes -
    because the function still satisfies the Protocol. Those reach the
    hook's callers as arguments they cannot pass, and this is what
    catches them.

    ``MechanicalVerification.__call__`` therefore spells its defaults as
    real values (``= None``, ``= 0``, ``= False``) rather than the
    conventional ``= ...``. Do not "tidy" that back: ``...`` is a
    distinct default value, so it makes this comparison fail on every
    optional parameter and forces the four weaker checks back.
    """
    proto = inspect.signature(MechanicalVerification.__call__)
    params = list(proto.parameters.values())
    assert params[0].name == "self"
    without_self = proto.replace(parameters=params[1:])
    assert without_self == inspect.signature(run_mechanical_verification)


def _completed(
    args: object = "cmd",
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> CompletedProcess[str]:
    return CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def _which_only(*present: str) -> Callable[[str], str | None]:
    """A ``shutil.which`` that finds exactly ``present`` and nothing else."""

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return which


class TestCheckDeadCode:
    """The vulture-or-custom-command phase, on its own (#335).

    ``check_dead_code`` used to fuse this phase with the ruff auto-fix
    that runs before it, so four states in which nothing was scanned
    still produced ``CheckResult(passed=True)`` - and
    ``review.build_review_prompt`` copied that row into an adversarial
    reviewer's prompt as ``dead_code: PASS``. Each of them is a
    :class:`NotMeasured` now, and each test below names its own
    ``reason`` token AND asserts the collaborator that would have
    measured never ran, so a deleted guard cannot pass by falling
    through to a later exit.

    The flag went the other way from ``check_mutation_score``'s: this
    function no longer takes ``read_only``. vulture and an operator's
    own ``dead_code_command`` are read-only by nature, and the two
    things the flag suppressed - ``ruff --fix`` and the commit after it -
    both live in :func:`check_dead_code_ruff` now.
    """

    def test_no_detector_available_measures_nothing(self, tmp_path: Path) -> None:
        with (
            patch("shutil.which", side_effect=_which_only()),
            patch("kstrl.verify.run_scrubbed") as run,
        ):
            outcome = check_dead_code(tmp_path, "main")

        assert isinstance(outcome, NotMeasured)
        assert (outcome.check, outcome.reason) == ("dead_code", NOT_MEASURED_TOOL_MISSING)
        assert "vulture" in outcome.detail
        assert run.call_count == 0

    @pytest.mark.parametrize(
        "diff",
        [["README.md"], ["tests/test_x.py"], []],
        ids=["docs-only", "test-only", "empty-diff"],
    )
    def test_no_scannable_file_in_the_diff_measures_nothing(
        self,
        tmp_path: Path,
        diff: list[str],
    ) -> None:
        """Not a fault, and not a pass either: nothing was looked at."""
        with (
            patch("shutil.which", side_effect=_which_only("vulture")),
            patch("kstrl.verify.git.get_diff_names", return_value=diff),
            patch("kstrl.verify.run_scrubbed") as run,
        ):
            outcome = check_dead_code(tmp_path, "main")

        assert isinstance(outcome, NotMeasured)
        assert (outcome.check, outcome.reason) == ("dead_code", NOT_MEASURED_NO_TARGET)
        assert run.call_count == 0

    def test_a_timed_out_scan_measures_nothing(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def run(cmd: object, **_: object) -> CompletedProcess[str]:
            calls.append(str(cmd))
            raise TimeoutExpired(str(cmd), 300)

        with (
            patch("shutil.which", side_effect=_which_only("vulture")),
            patch("kstrl.verify.git.get_diff_names", return_value=["src/main.py"]),
            patch("kstrl.verify.run_scrubbed", side_effect=run),
        ):
            outcome = check_dead_code(tmp_path, "main", timeout=300.0)

        assert isinstance(outcome, NotMeasured)
        assert (outcome.check, outcome.reason) == ("dead_code", NOT_MEASURED_TIMED_OUT)
        assert "300.0" in outcome.detail
        assert len(calls) == 1

    def test_a_crashed_scan_with_no_output_measures_nothing(self, tmp_path: Path) -> None:
        """Measured on vulture 2.16: exit 3 is findings, 1 is invalid
        input and 2 is a bad command line. A non-zero exit that printed
        nothing is the tool failing, and it used to fall through to
        ``no remaining dead code``."""
        with (
            patch("shutil.which", side_effect=_which_only("vulture")),
            patch("kstrl.verify.git.get_diff_names", return_value=["src/main.py"]),
            patch("kstrl.verify.run_scrubbed", return_value=_completed("vulture", 2)),
        ):
            outcome = check_dead_code(tmp_path, "main")

        assert isinstance(outcome, NotMeasured)
        assert (outcome.check, outcome.reason) == ("dead_code", NOT_MEASURED_COMMAND_FAILED)
        assert "2" in outcome.detail

    def test_vulture_findings_fail(self, tmp_path: Path) -> None:
        """The CONTROL. Without it the class is satisfiable by a
        function that returns a gap unconditionally."""
        with (
            patch("shutil.which", side_effect=_which_only("vulture")),
            patch("kstrl.verify.git.get_diff_names", return_value=["src/main.py", "src/utils.py"]),
            patch(
                "kstrl.verify.run_scrubbed",
                return_value=_completed(
                    "vulture",
                    3,
                    "src/main.py:10: unused function 'old_handler' (60% confidence)\n"
                    "src/utils.py:25: unused variable 'temp' (90% confidence)\n",
                ),
            ),
        ):
            outcome = check_dead_code(tmp_path, "main")

        assert isinstance(outcome, CheckResult)
        assert outcome.passed is False
        assert outcome.message == "2 dead code issues remaining"
        assert len(outcome.details) == 2

    def test_a_clean_scan_is_a_passing_row(self, tmp_path: Path) -> None:
        with (
            patch("shutil.which", side_effect=_which_only("vulture")),
            patch("kstrl.verify.git.get_diff_names", return_value=["src/main.py"]),
            patch("kstrl.verify.run_scrubbed", return_value=_completed("vulture", 0)),
        ):
            outcome = check_dead_code(tmp_path, "main")

        assert isinstance(outcome, CheckResult)
        assert outcome.passed is True
        assert outcome.message == "no remaining dead code"

    def test_custom_command_used(self, tmp_path: Path) -> None:
        """The operator's own detector replaces vulture, and replaces
        the diff read that only exists to build vulture's argument
        list."""
        calls: list[str] = []

        def run(cmd: object, **_: object) -> CompletedProcess[str]:
            calls.append(str(cmd))
            return _completed(cmd)

        with (
            patch("shutil.which", side_effect=_which_only()),
            patch("kstrl.verify.git.get_diff_names") as diff,
            patch("kstrl.verify.run_scrubbed", side_effect=run),
        ):
            outcome = check_dead_code(tmp_path, "main", command="my-custom-checker src/")

        assert isinstance(outcome, CheckResult)
        assert outcome.passed is True
        assert calls == ["my-custom-checker src/"]
        assert diff.call_count == 0


class TestCheckDeadCodeRuff:
    """The ruff F401/F811/F841 phase, on its own (#335).

    It always produces a row when ruff ran, because it always measured
    something - zero fixes is a measurement. The three states in which
    it did not run were invisible before the split: they were swallowed
    inside the fused row, which reported the vulture verdict and said
    nothing about ruff at all.
    """

    def test_ruff_not_installed_measures_nothing(self, tmp_path: Path) -> None:
        with (
            patch("shutil.which", side_effect=_which_only()),
            patch("kstrl.verify.run_scrubbed") as run,
        ):
            outcome = check_dead_code_ruff(tmp_path)

        assert isinstance(outcome, NotMeasured)
        assert (outcome.check, outcome.reason) == ("dead_code_ruff", NOT_MEASURED_TOOL_MISSING)
        assert run.call_count == 0

    def test_a_timed_out_run_measures_nothing_and_commits_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        calls: list[str] = []

        def run(cmd: object, **_: object) -> CompletedProcess[str]:
            calls.append(str(cmd))
            raise TimeoutExpired(str(cmd), 300)

        with (
            patch("shutil.which", side_effect=_which_only("ruff")),
            patch("kstrl.verify.run_scrubbed", side_effect=run),
        ):
            outcome = check_dead_code_ruff(tmp_path, 300.0)

        assert isinstance(outcome, NotMeasured)
        assert (outcome.check, outcome.reason) == ("dead_code_ruff", NOT_MEASURED_TIMED_OUT)
        assert not any("git" in c for c in calls)

    def test_a_failed_run_measures_nothing_and_commits_nothing(self, tmp_path: Path) -> None:
        """Measured on ruff 0.16.1: exit 0 clean or fixed, 1 findings, 2
        a configuration error. Before the split, exit 2 parsed to zero
        fixes and read as a clean auto-fix phase."""
        calls: list[str] = []

        def run(cmd: object, **_: object) -> CompletedProcess[str]:
            calls.append(str(cmd))
            return _completed(cmd, 2, "", "error: Failed to parse ruff.toml: TOML parse error")

        with (
            patch("shutil.which", side_effect=_which_only("ruff")),
            patch("kstrl.verify.run_scrubbed", side_effect=run),
        ):
            outcome = check_dead_code_ruff(tmp_path)

        assert isinstance(outcome, NotMeasured)
        assert (outcome.check, outcome.reason) == ("dead_code_ruff", NOT_MEASURED_COMMAND_FAILED)
        assert "2" in outcome.detail
        assert "TOML parse error" in outcome.detail
        assert not any("git" in c for c in calls)

    def test_fixes_are_counted_staged_and_committed(self, tmp_path: Path) -> None:
        """The factory path is untouched by the split: it still fixes,
        stages everything except the state directory (#274) and commits.

        ``run_scrubbed`` takes a shell string OR an argv list, and the
        staging call is a list so the ``:(exclude)`` pathspec reaches git
        unmangled. Rendered to one string per call so the assertions read
        the same for both forms.
        """
        calls: list[str] = []

        def run(cmd: str | list[str], **_: object) -> CompletedProcess[str]:
            rendered = cmd if isinstance(cmd, str) else " ".join(cmd)
            calls.append(rendered)
            if "ruff check" in rendered:
                return _completed(cmd, 0, "Found 3 errors (2 fixed, 1 remaining).")
            return _completed(cmd)

        with (
            patch("shutil.which", side_effect=_which_only("ruff")),
            patch("kstrl.verify.run_scrubbed", side_effect=run),
        ):
            outcome = check_dead_code_ruff(tmp_path)

        assert isinstance(outcome, CheckResult)
        assert outcome.name == "dead_code_ruff"
        assert outcome.passed is True
        assert outcome.message == "ruff auto-fixed 2"
        assert any("ruff check --fix" in c for c in calls)
        staged = next(c for c in calls if c.startswith("git add"))
        assert staged == "git add -A -- . :(exclude).kstrl"
        assert any("git commit" in c for c in calls)

    def test_nothing_to_fix_is_still_a_row_and_still_commits_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        """Zero fixes is a measurement, so it is a row and not a gap."""
        calls: list[str] = []

        def run(cmd: object, **_: object) -> CompletedProcess[str]:
            calls.append(str(cmd))
            return _completed(cmd)

        with (
            patch("shutil.which", side_effect=_which_only("ruff")),
            patch("kstrl.verify.run_scrubbed", side_effect=run),
        ):
            outcome = check_dead_code_ruff(tmp_path)

        assert isinstance(outcome, CheckResult)
        assert outcome.passed is True
        assert outcome.message == "ruff auto-fixed 0"
        assert not any("git" in c for c in calls)

    def test_read_only_reports_instead_of_removing(self, tmp_path: Path) -> None:
        """R10.1: ``ks sense`` runs against the operator's live checkout,
        where ``ruff --fix`` rewrites their files, ``git add -A`` sweeps
        in every unrelated untracked file and the commit moves their
        HEAD. Read-only runs the SAME rule set and reports what the
        factory would have removed."""
        calls: list[str] = []

        def run(cmd: object, **_: object) -> CompletedProcess[str]:
            calls.append(str(cmd))
            return _completed(cmd, 1, "Found 3 errors.\n")

        with (
            patch("shutil.which", side_effect=_which_only("ruff")),
            patch("kstrl.verify.run_scrubbed", side_effect=run),
        ):
            outcome = check_dead_code_ruff(tmp_path, read_only=True)

        ruff_calls = [c for c in calls if "ruff check" in c]
        assert len(ruff_calls) == 1
        # --no-fix is explicit, not merely implied by omitting --fix: a
        # project can set `fix = true` under [tool.ruff].
        assert "--no-fix" in ruff_calls[0]
        assert "--fix " not in ruff_calls[0]
        # --no-cache: not even .ruff_cache appears in the measured tree.
        assert "--no-cache" in ruff_calls[0]
        assert not any("git add" in c for c in calls)
        assert not any("git commit" in c for c in calls)
        assert isinstance(outcome, CheckResult)
        assert outcome.passed is True
        assert outcome.message == "ruff reports 3 auto-removable, not removed"


#: ``CHEAP_GATES`` plus the opt-in dead-code phases, with the diff
#: comparison off so the only rows are the three cheap gates and
#: whichever of ``dead_code_ruff`` / ``dead_code`` actually measured
#: something. Same reuse as ``MUTATION_GATES`` above.
DEAD_CODE_GATES = replace(CHEAP_GATES, dead_code_cleanup=True, check_diff_scope=False)


def _dead_code_verification(
    root: Path,
    *,
    tools: tuple[str, ...] = ("ruff", "vulture"),
    ruff: CompletedProcess[str] | BaseException | None = None,
    detect: CompletedProcess[str] | BaseException | None = None,
    custom: str | None = None,
    diff: list[str] | None = None,
    seen: list[str] | None = None,
    config: VerifyConfig | None = None,
    read_only: bool = False,
) -> VerificationResult:
    """Phase 1 over ``root`` with both dead-code phases driven by stubs.

    Through the real ``run_mechanical_verification`` rather than the
    check functions, because the rows and the sidecar are what every
    consumer reads and the split is exactly about which of the two a
    phase lands in.

    ``ruff`` and ``detect`` are each a finished process to return or an
    exception to raise; ``tools`` is what is on PATH. Dispatch is by
    command PREFIX and never by "everything else", so the three cheap
    gates and the ``git add`` / ``git commit`` pair cannot be mistaken
    for the detector.

    ``read_only`` is forwarded to ``run_mechanical_verification`` rather
    than to a check, because that is how ``ks sense`` reaches it.
    """
    recorded = [] if seen is None else seen

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in tools else None

    def run(cmd: object, **_: object) -> CompletedProcess[str]:
        text = cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd)  # type: ignore[union-attr]
        recorded.append(text)
        if text.startswith("ruff check"):
            outcome = ruff
        elif text.startswith("vulture") or (custom is not None and text == custom):
            outcome = detect
        else:
            outcome = None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome if outcome is not None else _completed(cmd)

    with (
        patch("shutil.which", side_effect=which),
        patch("kstrl.verify.run_scrubbed", side_effect=run),
        patch(
            "kstrl.verify.git.get_diff_names",
            return_value=["src/main.py"] if diff is None else diff,
        ),
    ):
        return run_mechanical_verification(
            root,
            None,
            "main",
            None,
            config or DEAD_CODE_GATES,
            read_only=read_only,
        )


def _dead_code_rows(result: VerificationResult) -> list[str]:
    return [c.name for c in result.checks if c.name.startswith("dead_code")]


def _dead_code_prd(root: Path) -> Path:
    path = root / "prd.json"
    path.write_text(
        json.dumps(
            {
                "branchName": "feature",
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Story",
                        "acceptanceCriteria": ["AC1"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class TestDeadCodeRowsOnlyExistWhenMeasured:
    """#335 at the level every consumer reads: ``checks``, and the
    sidecar beside it.

    ``check_dead_code`` fused two phases into one row - a ruff auto-fix
    that ran and a vulture scan that did not - so omitting the row would
    have discarded a real measurement and keeping it reported a pass for
    a scan that never happened. Split, each phase answers for itself.
    """

    @pytest.mark.parametrize(
        "stubs, gap, survivor, survivor_message",
        [
            pytest.param(
                {"tools": ("ruff",)},
                ("dead_code", NOT_MEASURED_TOOL_MISSING),
                "dead_code_ruff",
                "ruff auto-fixed 0",
                id="vulture-missing",
            ),
            pytest.param(
                {
                    "tools": ("ruff",),
                    "ruff": _completed("ruff", 0, "Found 2 errors (2 fixed, 0 remaining)."),
                },
                ("dead_code", NOT_MEASURED_TOOL_MISSING),
                "dead_code_ruff",
                "ruff auto-fixed 2",
                id="vulture-missing-after-ruff-fixed-2",
            ),
            pytest.param(
                {"detect": TimeoutExpired("vulture", 30)},
                ("dead_code", NOT_MEASURED_TIMED_OUT),
                "dead_code_ruff",
                "ruff auto-fixed 0",
                id="vulture-timed-out",
            ),
            pytest.param(
                {"detect": _completed("vulture", 2)},
                ("dead_code", NOT_MEASURED_COMMAND_FAILED),
                "dead_code_ruff",
                "ruff auto-fixed 0",
                id="vulture-exit-2-no-output",
            ),
            pytest.param(
                {"diff": ["README.md", "tests/test_x.py"]},
                ("dead_code", NOT_MEASURED_NO_TARGET),
                "dead_code_ruff",
                "ruff auto-fixed 0",
                id="nothing-scannable-in-the-diff",
            ),
            pytest.param(
                {
                    "tools": ("ruff",),
                    "read_only": True,
                    "ruff": _completed("ruff", 1, "Found 3 errors.\n"),
                },
                ("dead_code", NOT_MEASURED_TOOL_MISSING),
                "dead_code_ruff",
                "ruff reports 3 auto-removable, not removed",
                id="vulture-missing-read-only",
            ),
            pytest.param(
                {"tools": ("vulture",)},
                ("dead_code_ruff", NOT_MEASURED_TOOL_MISSING),
                "dead_code",
                "no remaining dead code",
                id="ruff-missing",
            ),
            pytest.param(
                {"ruff": TimeoutExpired("ruff check", 30)},
                ("dead_code_ruff", NOT_MEASURED_TIMED_OUT),
                "dead_code",
                "no remaining dead code",
                id="ruff-timed-out",
            ),
            pytest.param(
                {"ruff": _completed("ruff", 2, "", "error: Failed to parse ruff.toml")},
                ("dead_code_ruff", NOT_MEASURED_COMMAND_FAILED),
                "dead_code",
                "no remaining dead code",
                id="ruff-exit-2",
            ),
        ],
    )
    def test_a_phase_that_did_not_run_leaves_a_gap_and_the_other_row(
        self,
        tmp_path: Path,
        stubs: dict[str, object],
        gap: tuple[str, str],
        survivor: str,
        survivor_message: str,
    ) -> None:
        """The nine states the issue was filed over, as one census.

        It said four; measuring the fused function found nine, and every
        one of them printed ``dead_code  pass``. Five are the detector
        not running while ruff did (``vulture-missing`` is the path the
        issue was filed from, and ``read-only`` is the same path under
        ``ks sense``); four are ruff not running while the detector did,
        which the fused row hid completely because it only ever reported
        the vulture verdict.

        Three assertions per row, and the third is the one that makes
        this a split rather than an omission: the phase that DID measure
        keeps its row AND its real message. Omitting the fused row would
        have satisfied the first two and thrown a real measurement away.
        """
        result = _dead_code_verification(tmp_path, **stubs)  # type: ignore[arg-type]

        assert _dead_code_rows(result) == [survivor]
        assert [(g.check, g.reason) for g in result.not_measured] == [gap]
        row = next(c for c in result.checks if c.name == survivor)
        assert row.passed is True
        assert row.message == survivor_message
        # A check that measured nothing neither passes nor fails.
        assert result.passed is True

    def test_both_phases_measured_gives_two_rows_and_no_gap(self, tmp_path: Path) -> None:
        """The other half: without it, dropping every row would pass."""
        result = _dead_code_verification(
            tmp_path,
            ruff=_completed("ruff", 0, "Found 1 error (1 fixed, 0 remaining)."),
            detect=_completed("vulture", 0),
        )

        assert _dead_code_rows(result) == ["dead_code_ruff", "dead_code"]
        assert result.not_measured == []
        assert result.passed is True

    def test_a_vulture_finding_still_fails_the_run(self, tmp_path: Path) -> None:
        """Omission must not become a way to lose a real FAIL."""
        result = _dead_code_verification(
            tmp_path,
            detect=_completed(
                "vulture",
                3,
                "src/main.py:10: unused function 'old' (90% confidence)\n"
                "src/main.py:20: unused variable 'tmp' (90% confidence)\n",
            ),
        )

        row = next(c for c in result.checks if c.name == "dead_code")
        assert row.passed is False
        assert "2 dead code issues remaining" in row.message
        assert len(row.details) == 2
        assert result.passed is False

    def test_the_toggle_off_records_no_row_and_no_gap(self, tmp_path: Path) -> None:
        """The line the sidecar draws: silence is a complete answer to a
        question nobody asked, and an incomplete one to a question they
        did. Both stubs would produce rows if the toggle were read as
        True, so this cannot pass vacuously."""
        result = _dead_code_verification(
            tmp_path,
            ruff=_completed("ruff", 0, "Found 1 error (1 fixed, 0 remaining)."),
            detect=_completed("vulture", 0),
            config=replace(DEAD_CODE_GATES, dead_code_cleanup=False),
        )

        assert _dead_code_rows(result) == []
        assert result.not_measured == []

    def test_ruff_runs_before_the_detector(self, tmp_path: Path) -> None:
        """The factory deletes the ruff-fixable subset BEFORE vulture
        looks, so vulture sees the cleaner tree. Splitting the function
        made the order a property of the caller rather than of one
        function body, which is exactly the kind of thing a later edit
        reverses silently."""
        seen: list[str] = []
        _dead_code_verification(
            tmp_path,
            ruff=_completed("ruff", 0, "Found 1 error (1 fixed, 0 remaining)."),
            detect=_completed("vulture", 0),
            seen=seen,
        )

        ruff_at = next(i for i, c in enumerate(seen) if c.startswith("ruff check"))
        detect_at = next(i for i, c in enumerate(seen) if c.startswith("vulture"))
        assert ruff_at < detect_at

    def test_the_reviewer_summary_never_says_pass_for_a_scan_that_did_not_run(
        self,
        tmp_path: Path,
    ) -> None:
        """Why #335 is not a display bug.

        ``build_review_prompt`` renders every row as PASS or FAIL and
        hands the result to an adversarial LLM role. Before the split
        that summary carried ``- dead_code: PASS - ruff auto-fixed 2;
        vulture not installed``: a gate the reviewer is told passed and
        which never ran.
        """
        result = _dead_code_verification(
            tmp_path,
            tools=("ruff",),
            ruff=_completed("ruff", 0, "Found 2 errors (2 fixed, 0 remaining)."),
        )

        with patch("kstrl.review.git.repo_change_source", return_value="<change source>"):
            prompt = build_review_prompt(_dead_code_prd(tmp_path), "main", result)

        assert "- dead_code_ruff: PASS - ruff auto-fixed 2" in prompt
        assert "- dead_code:" not in prompt

    def test_phase_1_narrates_and_emits_a_dead_code_gap(self, tmp_path: Path) -> None:
        """The pipeline plumbing, which is shared with #306: a gap the
        pipeline drops is the same silence as no gap at all."""
        gap = NotMeasured(
            "dead_code",
            NOT_MEASURED_TOOL_MISSING,
            "vulture is not on PATH and no [verify] dead_code_command is set",
        )
        surfaces = phase_verify_surfaces(
            tmp_path,
            VerificationResult(
                passed=True,
                checks=[CheckResult("dead_code_ruff", True, "ruff auto-fixed 0")],
                not_measured=[gap],
            ),
        )
        emitted = [e for e in surfaces.events if isinstance(e, VerificationResultEvent)]

        assert "dead_code not measured (tool_missing)" in surfaces.narration
        assert len(emitted) == 1
        assert emitted[0].not_measured == ("dead_code:tool_missing",)
        assert emitted[0].checks == ("dead_code_ruff",)


class TestRunMechanicalVerificationWithoutPrd:
    """R10.1: ``prd_path=None`` skips exactly the PRD-dependent checks."""

    @staticmethod
    def _config() -> VerifyConfig:
        return VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        )

    def test_run_mechanical_verification_without_prd(self, tmp_path: Path) -> None:
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
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        fixtures = FixturesConfig(enabled=True)

        with_prd = run_mechanical_verification(
            tmp_path,
            prd,
            "main",
            None,
            self._config(),
            fixtures_config=fixtures,
        )
        without_prd = run_mechanical_verification(
            tmp_path,
            None,
            "main",
            None,
            self._config(),
            fixtures_config=fixtures,
        )

        # The Path call keeps its full list, PRD-dependent checks included.
        assert [c.name for c in with_prd.checks] == [
            "prd_stories",
            "test_suite",
            "typecheck",
            "linter",
            "fixtures",
        ]
        # None drops exactly prd_stories and fixtures; nothing else moves.
        assert [c.name for c in without_prd.checks] == [
            "test_suite",
            "typecheck",
            "linter",
        ]
        assert without_prd.passed is True

    def test_without_prd_self_critique_needs_an_explicit_progress_path(
        self,
        tmp_path: Path,
    ) -> None:
        """No PRD means no sibling log to derive: the check is skipped,
        unless [verify] progress_file_path names the log explicitly."""
        config = self._config()
        config.require_self_critique = True

        result = run_mechanical_verification(tmp_path, None, "main", None, config)
        assert not any(c.name == "self_critique" for c in result.checks)

        config.progress_file_path = "progress.txt"
        result = run_mechanical_verification(tmp_path, None, "main", None, config)
        critique = [c for c in result.checks if c.name == "self_critique"]
        assert len(critique) == 1
        # The configured file is absent, so the check fails closed
        # against tmp_path/progress.txt rather than being skipped.
        assert critique[0].passed is False
        assert "Could not read progress file" in critique[0].message


class TestReadOnlyVerification:
    """R10.1 review (P1): ``read_only=True`` measures without changing.

    The factory owns the worktree it verifies, so editing and committing
    there is free. ``ks sense`` runs against the operator's live
    checkout, where ``ruff --fix`` rewrites their files, ``git add -A``
    sweeps in every unrelated untracked file, and the commit moves their
    HEAD.
    """

    @pytest.mark.parametrize(
        "read_only, expect_row",
        [(True, False), (False, True)],
        ids=["read-only", "writable"],
    )
    def test_read_only_is_the_only_reason_the_mutation_row_is_missing(
        self,
        tmp_path: Path,
        read_only: bool,
        expect_row: bool,
    ) -> None:
        """mutmut rewrites the source it mutates, so read-only cannot run
        it - and #306: what it leaves behind is NO ROW, not a pass.

        Asserted through ``run_mechanical_verification`` because the row
        is what every consumer sees, and this is the path the issue was
        filed from: `ks sense --json` printed ``mutation_testing  True``.

        Both cases, so the flag is provably the only thing that moves:
        mutmut is on PATH in both, the diff names a non-test Python file
        in both, and mutmut reports 90% in both. Written first with
        mutmut ABSENT, and it passed with the read-only gate deleted -
        the row was missing for the wrong reason. That is the shape of
        guard this repo keeps shipping, so the writable half is here to
        make it impossible.
        """
        seen: list[str] = []

        def run(cmd: object, **kwargs: object) -> CompletedProcess[str]:
            seen.append(str(cmd))
            if "mutmut" in str(cmd):
                return _mutmut_completed("9 killed\n1 survived\n")
            return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with (
            patch("shutil.which", return_value="/usr/bin/mutmut"),
            patch("kstrl.verify.run_scrubbed", side_effect=run),
            patch("kstrl.verify.git.get_diff_names", return_value=["src/main.py"]),
        ):
            result = run_mechanical_verification(
                tmp_path,
                None,
                "main",
                None,
                MUTATION_GATES,
                read_only=read_only,
            )

        assert bool(_mutation_rows(result)) is expect_row
        assert any("mutmut" in c for c in seen) is expect_row
        # And the round-2 half: the read-only case is not merely
        # row-less, it says why. Without this, deleting the gate and
        # letting some later path swallow the run would still show an
        # absent row.
        gaps = [g.reason for g in result.not_measured if g.check == "mutation_testing"]
        assert gaps == ([] if expect_row else [NOT_MEASURED_READ_ONLY])

    def test_bad_patterns_writes_no_bytecode_beside_the_source(
        self,
        tmp_path: Path,
    ) -> None:
        """``py_compile`` defaults its output to ``__pycache__`` NEXT TO
        the file it compiles; scanning must not leave that behind."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "ok.py").write_text("x = 1\n")
        (src / "broken.py").write_text("def f(\n")

        with patch(
            "kstrl.verify.git.get_diff_names",
            return_value=["src/ok.py", "src/broken.py"],
        ):
            result = check_bad_patterns(tmp_path, "main")

        # The syntax error is still reported: only the destination moved.
        assert result.passed is False
        assert any("syntax error" in d for d in result.details)
        assert list(tmp_path.rglob("__pycache__")) == []
        assert list(tmp_path.rglob("*.pyc")) == []

    @staticmethod
    def _dead_code_read_only_and_mutation_consulted(
        tmp_path: Path,
        **kwargs: bool,
    ) -> tuple[bool, bool]:
        """``(read_only as forwarded to dead_code, mutmut was consulted)``.

        The two halves stopped being symmetric with #306, and that
        asymmetry is the fix. ``check_dead_code_ruff`` still TAKES the
        flag: read-only runs the same ruff rule set with ``--no-fix``
        and reports what the factory would have removed, so there is a
        narrower thing for it to do. ``check_mutation_score`` no longer
        takes the flag at all, because there is no narrower thing mutmut
        can do - so read-only does not call it, and a flag that could
        only ever return a row is gone.

        The ruff phase is the one asked, because #335 moved the flag
        there: ``check_dead_code`` is the vulture scan now and reads the
        tree without changing it, so it has no use for the flag at all.
        """
        config = replace(MUTATION_GATES, dead_code_cleanup=True)
        with (
            patch(
                "kstrl.verify.check_dead_code_ruff",
                return_value=CheckResult("dead_code_ruff", True),
            ) as dc,
            patch(
                "kstrl.verify.check_dead_code",
                return_value=CheckResult("dead_code", True),
            ),
            patch(
                "kstrl.verify.check_mutation_score",
                return_value=CheckResult("mutation_testing", True),
            ) as ms,
        ):
            run_mechanical_verification(tmp_path, None, "main", None, config, **kwargs)
        return bool(dc.call_args.kwargs["read_only"]), ms.called

    def test_verification_defaults_to_writable(self, tmp_path: Path) -> None:
        """No existing caller passes the flag, so the factory path must
        keep auto-fixing and mutating exactly as it did."""
        assert self._dead_code_read_only_and_mutation_consulted(tmp_path) == (False, True)

    def test_verification_forwards_read_only(self, tmp_path: Path) -> None:
        """dead_code is told; mutation is not called at all."""
        assert self._dead_code_read_only_and_mutation_consulted(
            tmp_path,
            read_only=True,
        ) == (True, False)
