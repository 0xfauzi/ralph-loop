"""#288: `ks feature` reports what the mechanical checks find.

Behaviour, not presence. Every test here drives ``run_feature`` with a
stubbed ``run_loop`` (no agent) but the REAL
``run_mechanical_verification``, against a project whose ``[verify]``
commands are real subprocesses. The failing-lint tests would go green
against a report that never ran, so they measure the report rather than
the code path that reaches it.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl import events as ev
from kstrl.commandrun import CommandRun
from kstrl.config import KstrlConfig
from kstrl.feature_cmd import FeatureParams, run_feature
from kstrl.feature_verify import resolve_feature_verify_config
from kstrl.loop import STOP_EXIT_CODE, LoopResult
from kstrl.verify import (
    DEFAULT_TYPECHECK_COMMAND,
    DIFF_DEPENDENT_CHECKS,
    CheckResult,
    VerificationResult,
    VerifyConfig,
    _default_typecheck_command,
    run_mechanical_verification,
    run_undiffed_verification,
)
from tests.test_feature_cmd import (
    NOOP_VERIFY_COMMAND,
    ScriptedChannel,
    StubAgent,
    _loop_results,
    _params,
    _ui,
)

#: The three checks that read no diff and so are honest on this path.
HONEST_CHECKS = ("test_suite", "typecheck", "linter")

#: Everything ``run_mechanical_verification`` is ALLOWED to produce here.
#: ``self_critique`` is opt-in (both ``require_self_critique`` and
#: ``progress_file_path``), reads no diff, and is announced when it runs.
#: A name outside this set reaching the report is the regression: either
#: it reads a diff, in which case it reports a pass over an empty one, or
#: it does not, in which case somebody has to decide whether it belongs
#: and update the announcement and the runbook with it.
ALLOWED_CHECKS = frozenset({*HONEST_CHECKS, "self_critique"})


SABOTAGE_LINE = "SABOTAGE: 3 findings"
#: A gate command that fails and says why, as a shell one-liner: the
#: gates run through ``verify.run_scrubbed``, which hands a string to
#: ``/bin/sh``, so no interpreter is exec'd and ``echo`` is a builtin.
#: POSIX-only, like the rest of this suite.
FAILING_VERIFY_COMMAND = f"echo '{SABOTAGE_LINE}'; exit 1"


def _write_kstrl_toml(root: Path, *, failing: str = "", extra: str = "") -> dict[str, str]:
    """Write a ``[verify]`` section whose commands really run.

    ``failing`` names the one gate whose command exits 1 ("test",
    "typecheck" or "lint"); the rest are no-ops. Returns the commands so
    a test can assert the project's own values were the ones read.

    Written BEFORE ``_params`` is called, and ``_write_fast_verify_toml``
    leaves an existing file alone, so this wins.
    """
    root.mkdir(parents=True, exist_ok=True)
    commands = {
        gate: FAILING_VERIFY_COMMAND if gate == failing else NOOP_VERIFY_COMMAND
        for gate in ("test", "typecheck", "lint")
    }
    body = "[verify]\n" + "".join(
        f"{gate}_command = {json.dumps(command)}\n" for gate, command in commands.items()
    )
    (root / "kstrl.toml").write_text(body + extra, encoding="utf-8")
    return commands


#: Every opt-in check turned on, to prove the narrowing wins regardless.
ALL_CHECKS_ON = (
    "check_diff_scope = true\n"
    "check_bad_patterns = true\n"
    "dead_code_cleanup = true\n"
    "mutation_testing = true\n"
)
ALL_CHECKS_ON_WITH_PHASES = (
    ALL_CHECKS_ON + "\n[policy]\nenabled = true\n\n[adequacy]\nenabled = true\n"
)


def _feature_params(tmp_path: Path, **kwargs: Any) -> FeatureParams:
    return _params(tmp_path, implementation_auto_run=True, **kwargs)


def _drive(
    tmp_path: Path,
    *,
    codes: tuple[int, ...] = (0, 0),
    iterations: int = 1,
    params: FeatureParams | None = None,
    channel: ScriptedChannel | None = None,
    loop: Any = None,
    stop_check: Any = None,
) -> tuple[int, list[ev.Event], str]:
    """Run the flow, returning (exit code, emitted events, UI text)."""
    ui, stream = _ui()
    captured: list[ev.Event] = []
    run = CommandRun(
        run_id="test-run",
        kind="feature",
        bus=ev.EventBus(ev.CallbackSink(captured.append), run_id="test-run", component="demo"),
        paths=None,
    )
    with (
        patch("kstrl.feature_cmd.run_loop", loop or _loop_results(*codes, iterations=iterations)),
        patch("kstrl.feature_cmd.get_agent", return_value=StubAgent()),
    ):
        code = run_feature(
            params if params is not None else _feature_params(tmp_path),
            KstrlConfig(),
            StubAgent(),
            ui,
            tmp_path,
            interaction=channel,
            run=run,
            stop_check=stop_check,
        )
    return code, captured, stream.getvalue()


def _verifications(captured: list[ev.Event]) -> list[ev.VerificationResultEvent]:
    return [e for e in captured if isinstance(e, ev.VerificationResultEvent)]


def _uncapped(root: Path) -> list[CheckResult]:
    """The same measurement, run again with no rendering cap applied.

    Used to show the cap is a RENDERING limit: the CheckResult the report
    was built from still carries every line, so nothing is lost from the
    ``parsed`` payload the factory's repair loop reads.
    """
    result = run_undiffed_verification(root, resolve_feature_verify_config(root))
    return list(result.checks)


#: Seconds a stubbed measurement blocks for, when a test needs the
#: report's duration to be distinguishable from the stubbed loop's. Big
#: enough to survive rounding to 2dp and scheduler jitter, small enough
#: that two reports per driven run cost under a second.
_SLOW = 0.25


def _slow_verification() -> Any:
    """``run_undiffed_verification`` that really takes ``_SLOW`` seconds."""

    def slow(root: Path, config: VerifyConfig) -> VerificationResult:
        time.sleep(_SLOW)
        return run_undiffed_verification(root, config)

    return slow


def _phases(captured: list[ev.Event]) -> list[str]:
    return [e.phase for e in _verifications(captured)]


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(root: Path) -> None:
    """A real repository with one commit on ``main``.

    Real git, because the question ``baseline_skip_reason`` asks is
    whether a checkout would move the working tree, and that is a fact
    about a repository rather than about a mock.
    """
    _run_git(root, "init", "-q", "-b", "main", ".")
    _run_git(root, "config", "user.email", "test@example.com")
    _run_git(root, "config", "user.name", "Test")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-q", "-m", "seed")


def _run_config(params: FeatureParams) -> KstrlConfig:
    """The KstrlConfig ``run_feature`` hands the implement loop, as far as
    the branch decision is concerned."""
    config = KstrlConfig()
    config.prd_file = params.prd_path
    if params.branch_override is not None:
        config.kstrl_branch = params.branch_override
        config.kstrl_branch_explicit = True
    return config


def _name_the_branch(params: FeatureParams, branch: str) -> FeatureParams:
    """Point the PRD at ``branch``, on disk as well as in memory.

    ``loop.determine_branch`` re-reads ``config.prd_file``; it never sees
    ``params.prd_doc``. A test that set only the object would be testing
    a branch decision nothing makes.
    """
    params.prd_doc.branch_name = branch
    params.prd_path.write_text(
        json.dumps(
            {
                "branchName": branch,
                "userStories": [
                    {
                        "id": "US-0",
                        "title": "story 0",
                        "acceptanceCriteria": ["tests pass"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return params


def _report(captured: list[ev.Event], phase: str) -> ev.VerificationResultEvent:
    """The one report for ``phase``. Raises if there is not exactly one."""
    matches = [e for e in _verifications(captured) if e.phase == phase]
    assert len(matches) == 1, [e.phase for e in _verifications(captured)]
    return matches[0]


class TestTheCheckActuallyRuns:
    def test_a_failing_lint_is_reported_to_the_terminal(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path, failing="lint")
        code, _, text = _drive(tmp_path)

        assert "Verification report (implement)" in text
        assert "linter" in text and "FAIL" in text
        assert "verification: FAIL (1 of 3 checks failed)" in text
        # The linter's own output, not just a verdict: a report that
        # cannot say WHAT failed is not a report.
        assert SABOTAGE_LINE in text
        # Report only. The flow's exit code is what it always was.
        assert code == 0

    def test_a_failing_lint_is_reported_to_the_event_stream(self, tmp_path: Path) -> None:
        """Under --implementation-auto-run nobody is at the screen, so
        events.jsonl is the report that counts."""
        _write_kstrl_toml(tmp_path, failing="lint")
        _, captured, _ = _drive(tmp_path)

        report = _report(captured, "implement")
        assert report.passed is False
        assert report.checks == HONEST_CHECKS
        assert report.failures == ("Linter failed (exit code 1)",)
        assert report.component == "demo"
        # #288: which loop was measured, and that nothing gated on it.
        # Without these a consumer filtering events.jsonl by type reads
        # this next to phase_completed(implement, passed=True) as a
        # contradiction.
        assert report.advisory is True

    def test_the_report_does_not_change_the_phase_verdict(self, tmp_path: Path) -> None:
        """A failing check reports; it does not halt, and it does not
        rewrite what the implement phase said about itself."""
        _write_kstrl_toml(tmp_path, failing="lint")
        _, captured, _ = _drive(tmp_path)

        implement = [
            e for e in captured if isinstance(e, ev.PhaseCompleted) and e.phase == "implement"
        ]
        assert len(implement) == 1
        assert implement[0].passed is True
        assert any(isinstance(e, ev.ComponentCompleted) for e in captured)

    def test_green_commands_report_a_pass(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path)
        code, captured, text = _drive(tmp_path)

        assert "verification: PASS (3 checks)" in text
        report = _report(captured, "implement")
        assert report.passed is True
        assert report.checks == HONEST_CHECKS
        assert report.failures == ()
        assert code == 0

    def test_the_commands_that_run_are_the_project_s_own(self, tmp_path: Path) -> None:
        """Not a default: moving the sabotage onto the test command must
        move which gate fails, which is only true if the project's
        [verify] section is what ran."""
        _write_kstrl_toml(tmp_path, failing="test")
        _, captured, text = _drive(tmp_path)

        assert _report(captured, "implement").failures == ("Tests failed (exit code 1)",)
        assert SABOTAGE_LINE in text


class TestOnlyHonestChecksRun:
    def test_every_diff_based_check_stays_off_however_kstrl_toml_is_written(
        self,
        tmp_path: Path,
    ) -> None:
        """The anti-staleness pin. tmp_path is not a git repository, so a
        diff-based check that DID run here would read an empty file list
        and report a pass over nothing measured. Turning all of them on in
        kstrl.toml must not reach the report.
        """
        _write_kstrl_toml(tmp_path, extra=ALL_CHECKS_ON_WITH_PHASES)
        _, captured, _ = _drive(tmp_path)

        assert _report(captured, "implement").checks == HONEST_CHECKS

    def test_no_verify_setting_of_any_type_can_add_an_unannounced_check(
        self,
        tmp_path: Path,
    ) -> None:
        """The fail-OPEN hole, closed by introspection rather than by a
        list somebody has to remember to extend.

        An earlier version of this test enabled every BOOLEAN
        ``[verify]`` field and claimed to close the hole. It did not: the
        one field that gates a check and is not a bool is
        ``progress_file_path``, a str, and setting it plus
        ``require_self_critique`` put a fourth row in the report that the
        announcement never named and the runbook said could not run
        (#288 review, verified). So drive EVERY field of the dataclass,
        whatever its type, and assert against a declared allowlist rather
        than an equality that only holds for the defaults.
        """
        settings: list[str] = []
        blank = VerifyConfig()
        for field in dataclasses.fields(VerifyConfig):
            if field.name.endswith("_command") or field.name.endswith("_tool"):
                continue  # driven by _write_kstrl_toml; a tool name is validated
            current = getattr(blank, field.name)
            if isinstance(current, bool):
                settings.append(f"{field.name} = true\n")
            elif isinstance(current, (int, float)):
                settings.append(f"{field.name} = 5\n")
            elif current is None or isinstance(current, str):
                settings.append(f'{field.name} = "progress.txt"\n')
        assert any("check_diff_scope" in line for line in settings), settings
        assert any("progress_file_path" in line for line in settings), settings

        _write_kstrl_toml(
            tmp_path,
            extra="".join(settings) + "\n[policy]\nenabled = true\n\n[adequacy]\nenabled = true\n",
        )
        _, captured, text = _drive(tmp_path)

        produced = set(_report(captured, "implement").checks)
        assert produced & set(DIFF_DEPENDENT_CHECKS) == set(), produced
        assert produced <= ALLOWED_CHECKS, produced
        # And whatever DID run was announced. Anything in the table that
        # the announcement never named is the defect this test exists for.
        for name in produced - set(HONEST_CHECKS):
            assert name in text

    def test_the_report_never_hands_the_checker_a_scope_error(
        self,
        tmp_path: Path,
    ) -> None:
        """``allowed_paths_error`` is a call argument, not a config field,
        so no kstrl.toml can drive it and the assertion above cannot see
        it. #294 rewrote how it is read: ``_scope_checks`` consults it
        BEFORE the toggles and, on any non-None value, appends
        ``scope_unreadable``, which is ungated and fails closed by
        design.

        Three halves, in the end. This flow has no component scope and
        never had one, so a scope_unreadable row in an advisory report
        would be a verdict invented rather than measured; the argument
        really is live, so the refusal is load bearing rather than
        incidental; and since round 2 the guarantee is STRUCTURAL rather
        than conventional - ``run_undiffed_verification`` has no
        parameter for it, so no caller can supply one by mistake.
        """
        _write_kstrl_toml(tmp_path)
        _, captured, _ = _drive(tmp_path)
        assert "scope_unreadable" not in set(_report(captured, "implement").checks)

        # Structural: the safe entry point owns every argument that could
        # turn a diff-consuming check back on, so none of them is a
        # parameter a caller could pass.
        params = set(inspect.signature(run_undiffed_verification).parameters)
        assert params == {"worktree_path", "config"}, params

        # And the argument it refuses to expose really would refuse.
        config = resolve_feature_verify_config(tmp_path)
        with_error = run_mechanical_verification(
            worktree_path=tmp_path,
            prd_path=None,
            base_branch="",
            allowed_paths_error="",  # empty is still non-None, and still refuses
            allowed_paths=None,
            config=config,
            read_only=True,
        )
        produced = {check.name: check.passed for check in with_error.checks}
        assert produced.get("scope_unreadable") is False, produced
        assert with_error.passed is False

    def test_self_critique_is_announced_when_the_operator_opts_into_it(
        self,
        tmp_path: Path,
    ) -> None:
        """It reads no diff, so it is allowed here, but it is a FOURTH
        check and the announcement has to say so before it blocks."""
        _write_kstrl_toml(
            tmp_path,
            extra='require_self_critique = true\nprogress_file_path = "progress.txt"\n',
        )
        (tmp_path / "progress.txt").write_text(
            "## Self-Critique\n- If a happens, b, which is wrong because c.\n"
            "- If d happens, e, which is wrong because f.\n"
            "- If g happens, h, which is wrong because i.\n",
            encoding="utf-8",
        )
        _, captured, text = _drive(tmp_path)

        report = _report(captured, "implement")
        assert "self_critique" in report.checks
        assert report.passed is True
        assert "reading:" in text
        assert "self_critique" in text

    def test_the_narration_names_what_was_not_measured(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path)
        _, _, text = _drive(tmp_path)

        for name in DIFF_DEPENDENT_CHECKS:
            assert name in text

    def test_the_narration_names_the_dead_code_phase_that_reads_no_diff(
        self, tmp_path: Path
    ) -> None:
        """#335: `dead_code_ruff` is correctly absent from
        DIFF_DEPENDENT_CHECKS, since ruff scans `.` and needs no base, so
        the loop above cannot cover it. Without a line of its own the
        split left it suppressed here with no row, no gap and nothing in
        the report. The toggle is named as the reason because appending
        the name to the diff sentence would state a false one."""
        _write_kstrl_toml(tmp_path)
        _, _, text = _drive(tmp_path)

        assert "dead_code_ruff" in text
        assert "dead_code_cleanup" in text

    def test_resolve_keeps_the_commands_and_drops_the_diff_checks(
        self,
        tmp_path: Path,
    ) -> None:
        commands = _write_kstrl_toml(tmp_path, extra=ALL_CHECKS_ON)
        config = resolve_feature_verify_config(tmp_path)

        assert config.check_diff_scope is False
        assert config.check_bad_patterns is False
        assert config.dead_code_cleanup is False
        assert config.mutation_testing is False
        # Untouched: these three are what the engineer prompt states and
        # what the report runs, and they must agree.
        assert config.test_command == commands["test"]
        assert config.typecheck_command == commands["typecheck"]
        assert config.lint_command == commands["lint"]


class TestExitPathRule:
    """A path where no production code was written gets no report."""

    def test_no_report_when_understand_fails(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path, failing="lint")
        code, captured, _ = _drive(tmp_path, codes=(1,))
        assert code == 1
        assert _verifications(captured) == []

    def test_no_report_when_the_operator_quits_to_amend(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path, failing="lint")
        code, captured, _ = _drive(
            tmp_path,
            codes=(0,),
            params=_params(tmp_path),
            channel=ScriptedChannel(choice=1),
        )
        assert code == 0
        assert _verifications(captured) == []

    def test_no_report_when_the_gate_cannot_prompt(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path, failing="lint")
        code, captured, _ = _drive(
            tmp_path,
            codes=(0,),
            params=_params(tmp_path),
            channel=ScriptedChannel(choice=0, promptable=False),
        )
        assert code == 2
        assert _verifications(captured) == []

    def test_no_report_when_the_prd_has_no_stories(self, tmp_path: Path) -> None:
        """The baseline sits AFTER this check, so a run that will never
        start an engineer loop does not pay for a test suite either."""
        _write_kstrl_toml(tmp_path, failing="lint")
        code, captured, _ = _drive(
            tmp_path, codes=(0,), params=_feature_params(tmp_path, stories=0)
        )
        assert code == 0
        assert _verifications(captured) == []

    def test_no_loop_report_when_the_engineer_never_ran_an_iteration(
        self,
        tmp_path: Path,
    ) -> None:
        """iterations == 0 is the loop halting in preflight (a failed
        branch checkout, a stop request before iteration 1). There is no
        agent output to measure, so only the baseline is reported."""
        _write_kstrl_toml(tmp_path, failing="lint")
        _, captured, _ = _drive(tmp_path, codes=(0, 1), iterations=0)
        assert [e.phase for e in _verifications(captured)] == ["baseline"]

    def test_no_report_when_the_operator_asked_the_loop_to_stop(
        self,
        tmp_path: Path,
    ) -> None:
        """Exit 130 is a stop request honoured mid-loop, so iterations is
        non-zero and the iterations guard alone would let the report
        through. Somebody who pressed stop must not then be made to wait
        out a test suite: measured on this repo at 246s, and bounded only
        by 3 x subprocess_timeout (900s at the default)."""
        _write_kstrl_toml(tmp_path, failing="lint")
        _, captured, _ = _drive(tmp_path, codes=(0, STOP_EXIT_CODE))
        assert [e.phase for e in _verifications(captured)] == ["baseline"]

    def test_no_report_when_a_stop_arrived_during_the_final_iteration(
        self,
        tmp_path: Path,
    ) -> None:
        """#288 review finding 2. ``run_loop`` returns STOP_EXIT_CODE only
        from its top-of-iteration probe, so a stop pressed DURING the last
        iteration comes back as an ordinary completed exit 0 and the
        exit-code guard never fires. The stop flag is still set, and that
        is what has to be consulted."""
        _write_kstrl_toml(tmp_path, failing="lint")
        calls: list[int] = []
        stopped = [False]

        def fake(config: Any, ui: Any, agent: Any, *args: Any, **kwargs: Any) -> LoopResult:
            calls.append(1)
            if len(calls) == 2:  # during the implement loop, not before it
                stopped[0] = True
            return LoopResult(completed=True, iterations=1, exit_code=0)

        _, captured, text = _drive(
            tmp_path,
            loop=fake,
            stop_check=lambda: stopped[0],
        )
        # The baseline ran (nothing was stopped yet); nothing after it did.
        assert [e.phase for e in _verifications(captured)] == ["baseline"]
        assert "Verification report (implement)" not in text

    def test_every_repair_attempt_reports_and_says_which(self, tmp_path: Path) -> None:
        """One report per engineer loop, each naming its own loop. Without
        the phase field a consumer filtering events.jsonl by type could
        count four and not tell them apart."""
        _write_kstrl_toml(tmp_path, failing="lint")
        params = _feature_params(tmp_path, repair_max_runs=2)
        _, captured, _ = _drive(tmp_path, codes=(0, 1, 1, 1), params=params)

        results = _verifications(captured)
        assert [r.phase for r in results] == ["baseline", "implement", "repair-1", "repair-2"]
        assert all(r.passed is False for r in results)
        assert all(r.advisory is True for r in results)


class TestTheReportCannotKillTheRun:
    """#288 review finding 1: an advisory report may not halt the flow."""

    def test_a_crashing_measurement_is_recorded_not_raised(self, tmp_path: Path) -> None:
        """``check_test_suite`` catches TimeoutExpired and nothing else, so
        a Popen that fails on EMFILE or a removed cwd would otherwise
        propagate out of an advisory report and truncate the run record."""
        _write_kstrl_toml(tmp_path)

        def boom(*args: Any, **kwargs: Any) -> VerificationResult:
            raise OSError(24, "Too many open files")

        with patch("kstrl.feature_verify.run_undiffed_verification", boom):
            code, captured, text = _drive(tmp_path)

        assert code == 0
        assert "verification: could not run" in text
        assert "Too many open files" in text
        report = _report(captured, "implement")
        # An EMPTY checks tuple with passed=False is the unambiguous
        # "nothing was measured": never mistakable for a pass.
        assert report.passed is False
        assert report.checks == ()
        assert report.failures == ("OSError: [Errno 24] Too many open files",)

    def test_resolving_the_commands_cannot_kill_the_run_either(
        self,
        tmp_path: Path,
    ) -> None:
        """#288 review round 2 finding 2: round 1's wrapper started one
        statement too low.

        ``resolve_verify_commands``, ``self_critique_progress_path`` and
        the announcement all ran OUTSIDE the try, and the first of those
        does file I/O: it reaches ``_default_typecheck_command``, which
        opens and parses pyproject.toml. Anything it raises escaped an
        ADVISORY report and took the command down at the BASELINE, before
        the agent had run.
        """
        _write_kstrl_toml(tmp_path)

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise UnicodeDecodeError("utf-8", b"\x80", 0, 1, "invalid start byte")

        with patch("kstrl.feature_verify.resolve_verify_commands", boom):
            code, captured, text = _drive(tmp_path)

        assert code == 0
        assert "verification: could not run" in text
        for phase in ("baseline", "implement"):
            report = _report(captured, phase)
            assert report.passed is False
            assert report.checks == ()
        assert any(isinstance(e, ev.RunCompleted) for e in captured)

    def test_a_pyproject_that_is_not_utf_8_does_not_raise(self, tmp_path: Path) -> None:
        """The concrete producer, at the site that let it through.

        ``tomllib.load`` decodes as utf-8 itself, so one stray byte
        raises UnicodeDecodeError, which IS a ValueError and so walked
        straight past ``except (TOMLDecodeError, OSError)``. This is the
        rule CLAUDE.md states: catch ValueError alongside OSError.
        """
        (tmp_path / "pyproject.toml").write_bytes(b'[project]\nname = "d\x80emo"\nversion = "0"\n')
        # Would have raised UnicodeDecodeError before the widening.
        assert _default_typecheck_command(tmp_path) == DEFAULT_TYPECHECK_COMMAND

    def test_the_run_record_is_still_complete(self, tmp_path: Path) -> None:
        """The failure mode: events.jsonl ending at phase_started with no
        phase_completed and no run_completed, which a dashboard reads as a
        component running forever."""
        _write_kstrl_toml(tmp_path)

        def boom(*args: Any, **kwargs: Any) -> VerificationResult:
            raise OSError(24, "Too many open files")

        with patch("kstrl.feature_verify.run_undiffed_verification", boom):
            _, captured, _ = _drive(tmp_path)

        assert any(isinstance(e, ev.PhaseCompleted) and e.phase == "implement" for e in captured)
        assert any(isinstance(e, ev.ComponentCompleted) for e in captured)
        assert any(isinstance(e, ev.RunCompleted) for e in captured)


class TestTheReportDoesNotDistortTheRunRecord:
    def test_the_phase_duration_excludes_the_report(self, tmp_path: Path) -> None:
        """The implement phase's duration is the engineer loop's own, so
        it stays comparable to a pre-#288 run.

        The previous version of this test asserted
        ``implement.duration < report.duration + 1.0``, which held in
        BOTH worlds and so measured nothing (#288 review round 2). The
        fix is to give the report a duration the stubbed loop cannot
        reach: a measurement that takes ``_SLOW`` seconds separates the
        two worlds by that whole margin, because folding it into the
        phase is precisely what the defect does.
        """
        _write_kstrl_toml(tmp_path)
        with patch(
            "kstrl.feature_verify.run_undiffed_verification",
            _slow_verification(),
        ):
            _, captured, _ = _drive(tmp_path)

        implement = next(
            e for e in captured if isinstance(e, ev.PhaseCompleted) and e.phase == "implement"
        )
        report = _report(captured, "implement")
        assert report.seq < implement.seq
        # The report really was slow, so the margin exists to be missed.
        assert report.duration_seconds >= _SLOW, report.duration_seconds
        # And the phase did not pay for it. The stubbed loop returns
        # immediately, so anything at or above _SLOW here is the report's
        # own time folded in.
        assert implement.duration_seconds < _SLOW, implement.duration_seconds

    def test_the_announced_command_is_the_command_that_ran(self, tmp_path: Path) -> None:
        """#288 review finding 9: the announcement and the run must not
        resolve the commands independently, or the terminal can name one
        command while a different one produces the verdict. Asserted on
        the command the failing gate itself recorded, not on the config.
        """
        commands = _write_kstrl_toml(tmp_path, failing="lint")
        _, _, text = _drive(tmp_path)

        linter = next(c for c in _uncapped(tmp_path) if c.name == "linter")
        assert linter.parsed is not None
        ran = linter.parsed.command
        assert ran == commands["lint"]
        announced = [line for line in text.splitlines() if "running:" in line and ran in line]
        assert announced, (ran, text)

    def test_the_terminal_says_what_it_is_running_before_it_blocks(
        self,
        tmp_path: Path,
    ) -> None:
        """The gates capture their output, so without this the terminal
        is dead for the length of the project's test suite with nothing
        said about why."""
        commands = _write_kstrl_toml(tmp_path)
        _, _, text = _drive(tmp_path)

        running = [line for line in text.splitlines() if "running:" in line]
        assert len(running) == 6  # baseline plus implement, three each
        for command in commands.values():
            assert any(command in line for line in running)
        # Before, not after: the announcement has to precede the verdict
        # it is warning the operator to wait for.
        assert text.index("running:") < text.index("verification: PASS")

    def test_failure_details_are_capped_and_the_truncation_is_stated(self) -> None:
        """#288 review finding 8, at the renderer.

        Under the embedded TUI every printed line is one Log event on the
        run bus. Measured: a 40-finding ruff run against files that exist
        renders 81 detail lines from ONE check (10 shown findings, each
        with a 7-line source snippet, plus the "and 30 more" line). The
        truncation must be stated, because a report that quietly shows 12
        of 200 lines is one you would act on wrongly.
        """
        check = CheckResult(
            name="linter",
            passed=False,
            message="Linter failed (exit code 1)",
            details=["\n".join(f"finding {n}" for n in range(200))],
        )
        result = VerificationResult(passed=False, checks=[check])

        capped = result.report_lines(durations=False, max_detail_lines=12)
        assert len(capped) == 14  # verdict + 12 details + the count
        assert capped[-1].strip() == "... 188 more line(s) not shown"
        # ks sense is uncapped: there the measurement IS the whole output.
        assert len(result.report_lines()) == 201

    def test_the_feature_report_caps_but_ks_sense_does_not(self, tmp_path: Path) -> None:
        """End to end on a REAL parsed lint failure, so the cap is wired
        and not merely available.

        Measured on a 40-finding ruff run against files that exist: the
        parser shows 10 findings, each with a 7-line source snippet, for
        81 detail lines from ONE check. Three checks in a report and one
        report per engineer loop, so the uncapped number reaching the run
        bus grows with the number of loops.
        """
        tmp_path.mkdir(parents=True, exist_ok=True)
        findings = []
        for n in range(1, 41):
            (tmp_path / f"mod_{n}.py").write_text(
                "\n".join(f"code line {i}" for i in range(1, 30)), encoding="utf-8"
            )
            findings.append(f"mod_{n}.py:10:1: F401 `os` imported but unused")
        (tmp_path / "findings.txt").write_text("\n".join(findings) + "\n", encoding="utf-8")
        lint = "cat findings.txt; exit 1"
        (tmp_path / "kstrl.toml").write_text(
            "[verify]\n"
            f"test_command = {json.dumps(NOOP_VERIFY_COMMAND)}\n"
            f"typecheck_command = {json.dumps(NOOP_VERIFY_COMMAND)}\n"
            f"lint_command = {json.dumps(lint)}\n",
            encoding="utf-8",
        )
        _, captured, text = _drive(tmp_path)

        # The measurement itself is unharmed: only the rendering is cut.
        check = next(c for c in _uncapped(tmp_path) if c.name == "linter")
        uncapped = sum(len(d.splitlines()) for d in check.details)
        assert uncapped > 40, uncapped

        assert "mod_1.py:10 [F401]" in text
        assert "more line(s) not shown" in text
        assert "mod_10.py:10 [F401]" not in text
        assert _report(captured, "implement").failures == ("Linter failed (exit code 1)",)
