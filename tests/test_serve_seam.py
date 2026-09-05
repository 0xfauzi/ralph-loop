"""R8.6 follow-up (#205): the serve-to-factory seam, executed for real.

Every other serve test injects a stub ``runner=`` - 46 of them in
``tests/test_serve.py`` alone - and ``subprocess_factory_runner`` is
only ever patched out. The supervision half is genuinely tested
(``run_supervised`` against a real process group), but the half that
decides **what to invoke** was never executed: the argv, the cwd, the
env, and whether remote work reaches the queue at all.

That is exactly where #189 F1 lived. ``serve_cycle`` drained a queue
nothing could fill because it never called the intake adapter, and both
halves were independently correct and independently green. No
stub-runner test could see it, because the stub replaced the boundary
the defect lived on.

Four checks, none of which spends money:

1. :class:`TestTheRealRunnerExecsItsArgv` runs the shipping
   ``subprocess_factory_runner`` against a stub interpreter. Only
   ``sys.executable`` is replaced - argv construction, the caffeinate
   prefix, the env mutation, the process-group spawn and the
   ``RunOutcome`` mapping are all production code.
2. :class:`TestTheArgvIsAcceptedByTheRealCli` hands that recorded argv
   to the real Click command, so a flag the daemon sends but the CLI no
   longer accepts is caught here rather than on the next unattended run.
3. :class:`TestACycleWithNoInjectedRunnerLaunchesTheRealCommand` drives
   ``serve_cycle`` with no ``runner=`` at all, which is the only way
   ``_default_runner`` is executed anywhere in the suite.
4. :class:`TestRemoteWorkSurvivesTheSeam` covers the two facts about a
   remotely-sourced item that only become observable at the runner
   boundary: the spec content, and the merge gate.

Measured, by mutation, against the rest of the suite:

- Renaming the merge-gate flag in ``subprocess_factory_runner`` is
  caught here and by NOTHING else: with that mutant applied the other
  3125 tests pass, 28 skip, zero fail.
- Making ``_default_runner`` forward a wrong ``project_name`` passed all
  3140 tests before check 3 existed. It had zero references in ``tests/``.
- Deleting ``*caffeinate_prefix(caffeinate)`` from the runner's command
  passed all 3140 tests, on macOS as well as CI's ubuntu, because
  caffeinate execs in place and so leaves no observable trace.
- Deleting the ``_run_intake`` call from ``serve_cycle`` - the #189 F1
  defect itself - is already caught by nine tests in
  ``tests/test_intake_github.py::TestServePollsIntake``, so this module
  deliberately does not re-assert it.
- Renaming the option on the ``ks factory`` side is caught both here and
  by ``test_config_control_plane.py::TestSafetyKnobs``.

What is NOT covered, so nobody reads more into it: no factory is run, so
nothing below ``ks factory``'s argument parsing is exercised, and the
classification of a real run's artifacts remains the stub-driven tests'
job. The end-to-end path is still only verified by hand
(``docs/continuous-intake.md`` section 7).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import click
import pytest

from kstrl.serve import (
    RunOutcome,
    ServeConfig,
    serve_cycle,
    subprocess_factory_runner,
)
from kstrl.workqueue import Queue, QueueConfig
from tests.test_intake_github import REPO, _GhStub, _issue, _issue_payload

# --------------------------------------------------------------------------
# A stub interpreter: the narrowest possible intercept
# --------------------------------------------------------------------------

#: Stands in for ``sys.executable``. Records what the real runner asked
#: for, then exits how the test told it to. Deliberately dependency-free
#: and tiny: it is spawned by the code under test, so a failure inside it
#: surfaces as a confusing RunOutcome rather than a test error.
_STUB_INTERPRETER = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["SEAM_RECORD"], "w") as handle:
    json.dump({
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "kstrl_env": {
            k: v for k, v in os.environ.items() if k.startswith("KSTRL_")
        },
    }, handle)
sys.stdout.write(os.environ.get("SEAM_STDOUT", ""))
sys.exit(int(os.environ.get("SEAM_EXIT", "0")))
"""

#: Stands in for ``/usr/bin/caffeinate``, and works on any platform.
#:
#: The real one execs its utility in place (measured; see
#: ``test_the_factory_still_runs_correctly_under_real_caffeinate``), so
#: this does too - otherwise the pid assertions would diverge from
#: production for reasons unrelated to the code under test. It drops the
#: leading ``-i`` exactly as the real one consumes its own flags, and
#: touches a marker first so its PRESENCE in the chain is observable.
#: Without that marker the wrapper is undetectable: exec-in-place leaves
#: the pid, the argv and the exit status all identical.
_FAKE_CAFFEINATE = """#!/bin/sh
: > "$SEAM_CAFFEINATE_MARKER"
shift
exec "$@"
"""


@pytest.fixture(autouse=True)
def _no_open_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the R10.7 open-PR bound open for this whole module.

    Nothing here is about flow control, but `max_open_prs` defaults to 1
    and a `tmp_path` is not a git checkout, so the real counter fails and
    the gate refuses - correctly, since an unknown number of open PRs is
    not zero. Stubbing the COUNTER rather than the gate keeps the gate
    itself in the code path these tests execute. The bound's own
    behaviour, including its presence in `serve_cycle`, is asserted in
    `tests/test_flow_control.py`.
    """
    monkeypatch.setattr("kstrl.serve.count_open_kstrl_prs", lambda root: 0)


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _install_stub_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exit_code: int = 0,
    stdout: str = "",
) -> Path:
    """Replace ``sys.executable`` with the recorder; return the record path."""
    interpreter = _write_executable(
        tmp_path / "stub_interpreter",
        _STUB_INTERPRETER,
    )
    record = tmp_path / "exec_record.json"
    monkeypatch.setenv("SEAM_RECORD", str(record))
    monkeypatch.setenv("SEAM_EXIT", str(exit_code))
    monkeypatch.setenv("SEAM_STDOUT", stdout)
    monkeypatch.setattr(sys, "executable", str(interpreter))
    return record


def _install_fake_caffeinate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Put a fake ``caffeinate`` on PATH and make the platform look like
    macOS; return the marker path it touches when it runs.

    PATH rather than patching ``shutil.which``, so the real lookup inside
    ``caffeinate_prefix`` is the thing being exercised. ``sys.platform``
    has to be patched because that prefix is macOS-only by design and all
    four CI jobs are ubuntu-latest - without it this wiring would be
    untested everywhere it actually runs. Verified load-bearing: set the
    patch to ``"linux"`` and both caffeinate tests fail.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    _write_executable(bindir / "caffeinate", _FAKE_CAFFEINATE)
    marker = tmp_path / "caffeinate_ran"
    monkeypatch.setenv("SEAM_CAFFEINATE_MARKER", str(marker))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "platform", "darwin")
    return marker


@dataclass(frozen=True)
class _Exec:
    """What the child process actually received."""

    argv: tuple[str, ...]
    cwd: Path
    pid: int
    kstrl_env: dict[str, str]

    def value_of(self, flag: str) -> str:
        """The argument following ``flag``."""
        return self.argv[self.argv.index(flag) + 1]


def _exec_real_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pause_before_pr_merge: bool = False,
    caffeinate: bool = False,
    exit_code: int = 0,
    stdout: str = "",
    timeout_seconds: float = 60.0,
    on_spawn: Callable[[int], None] | None = None,
    project_name: str = "seam-project",
) -> tuple[RunOutcome, _Exec]:
    """Run the REAL ``subprocess_factory_runner`` against a stub interpreter.

    ``sys.executable`` is the only thing replaced. Everything the runner
    decides - which module, which flags, which cwd, which env - is left
    to the shipping code and read back out of the child.
    """
    record = _install_stub_interpreter(
        tmp_path,
        monkeypatch,
        exit_code=exit_code,
        stdout=stdout,
    )
    root_dir = tmp_path / "root"
    root_dir.mkdir(exist_ok=True)
    # `--spec` is a click Path(exists=True), so the CLI-acceptance test
    # downstream needs this to be a real file.
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# Spec\n\nDo the thing.\n", encoding="utf-8")

    outcome = subprocess_factory_runner(
        root_dir=root_dir,
        spec_path=spec_path,
        project_name=project_name,
        pause_before_pr_merge=pause_before_pr_merge,
        timeout_seconds=timeout_seconds,
        on_spawn=on_spawn,
        caffeinate=caffeinate,
    )
    assert record.exists(), (
        "the stub interpreter never ran, so the runner did not exec what "
        f"this test thinks it did; outcome={outcome}"
    )
    raw = json.loads(record.read_text(encoding="utf-8"))
    return outcome, _Exec(
        argv=tuple(raw["argv"]),
        cwd=Path(raw["cwd"]),
        pid=raw["pid"],
        kstrl_env=raw["kstrl_env"],
    )


class TestTheRealRunnerExecsItsArgv:
    """The half that was only ever patched out."""

    def test_it_invokes_the_factory_as_a_module_of_this_interpreter(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not a bare ``ks`` off PATH: an installed LaunchAgent has no
        guarantee about PATH, so the runner names the interpreter and the
        module explicitly."""
        _, ran = _exec_real_runner(tmp_path, monkeypatch)
        assert ran.argv[:3] == ("-m", "kstrl", "factory")

    def test_it_passes_the_spec_project_and_root_it_was_given(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, ran = _exec_real_runner(
            tmp_path,
            monkeypatch,
            project_name="deckgen",
        )
        assert ran.value_of("--project-name") == "deckgen"
        assert Path(ran.value_of("--spec")) == tmp_path / "spec.md"
        assert Path(ran.value_of("--root")) == tmp_path / "root"

    def test_it_forces_non_interactive_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A daemon-spawned factory has no terminal. If any of these
        regress the child blocks on a prompt or emits escape codes into
        the captured output the classifier reads."""
        _, ran = _exec_real_runner(tmp_path, monkeypatch)
        assert "--yes" in ran.argv
        assert "--no-tui" in ran.argv
        assert "--no-color" in ran.argv
        assert ran.value_of("--ui") == "plain"
        assert ran.kstrl_env.get("KSTRL_NO_TUI") == "1"

    @pytest.mark.parametrize(
        ("gate_on", "expected", "forbidden"),
        [
            (True, "--pause-before-pr-merge", "--no-pause-before-pr-merge"),
            (False, "--no-pause-before-pr-merge", "--pause-before-pr-merge"),
        ],
    )
    def test_the_merge_gate_is_passed_explicitly_both_ways(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        gate_on: bool,
        expected: str,
        forbidden: str,
    ) -> None:
        """Explicit in both directions on purpose: the child must not
        inherit the gate from its own kstrl.toml, because the queue
        item's merge disposition is what decided it."""
        _, ran = _exec_real_runner(
            tmp_path,
            monkeypatch,
            pause_before_pr_merge=gate_on,
        )
        assert expected in ran.argv
        assert forbidden not in ran.argv

    def test_the_child_runs_in_the_target_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--root` and the cwd must agree; a factory that runs beside
        the repo it was pointed at would resolve every relative
        `.kstrl/` default somewhere else."""
        _, ran = _exec_real_runner(tmp_path, monkeypatch)
        assert ran.cwd.resolve() == (tmp_path / "root").resolve()
        assert Path(ran.value_of("--root")).resolve() == ran.cwd.resolve()

    def test_on_spawn_receives_the_pid_that_actually_ran(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The lease is adopted by this pid. If it is the daemon's own,
        a successor judges the lease dead and requeues a live run
        (#186 F1)."""
        seen: list[int] = []
        _, ran = _exec_real_runner(
            tmp_path,
            monkeypatch,
            on_spawn=seen.append,
            caffeinate=False,
        )
        assert seen == [ran.pid]

    def test_the_childs_exit_code_and_output_reach_the_outcome(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The classifier reads both: exit 2 is only disambiguated by
        what the child printed (#186 F6)."""
        outcome, _ = _exec_real_runner(
            tmp_path,
            monkeypatch,
            exit_code=2,
            stdout="halted: budget\n",
        )
        assert outcome.returncode == 2
        assert "halted: budget" in outcome.output_tail
        assert not outcome.timed_out
        assert outcome.launch_error == ""

    def test_a_missing_interpreter_is_a_launch_error_not_a_crash(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unlaunchable child must be a value the daemon can classify,
        never an exception that takes the loop down."""
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        monkeypatch.setattr(sys, "executable", str(tmp_path / "nope"))
        outcome = subprocess_factory_runner(
            root_dir=root_dir,
            spec_path=tmp_path / "spec.md",
            project_name="p",
            pause_before_pr_merge=False,
            timeout_seconds=30.0,
            caffeinate=False,
        )
        assert outcome.returncode == -1
        assert outcome.launch_error

    def test_the_runner_actually_wraps_the_child_in_caffeinate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Runs on every platform, because this wiring is invisible.

        Review finding on #208, and sharper than it first looks: deleting
        ``*caffeinate_prefix(caffeinate)`` from the runner's command
        passes the entire suite - all 3140 tests - on macOS as well as on
        CI's ubuntu. Exec-in-place is why. The wrapper leaves the pid, the
        argv and the exit status untouched, so no assertion over the
        child's observable state can detect its absence. The only way to
        see it is to make the wrapper itself report, which is what the
        fake on PATH does.

        Skipping this on non-darwin would put it back where it started:
        all four CI jobs are ubuntu-latest.
        """
        marker = _install_fake_caffeinate(tmp_path, monkeypatch)
        outcome, ran = _exec_real_runner(
            tmp_path,
            monkeypatch,
            caffeinate=True,
            exit_code=5,
        )
        assert marker.exists(), (
            "caffeinate was not in the child's exec chain, so runs are no "
            "longer holding the idle-sleep assertion"
        )
        assert ran.argv[:3] == ("-m", "kstrl", "factory"), "the wrapper mangled the factory's argv"
        assert outcome.returncode == 5, (
            "the wrapper swallowed the factory's exit status; the classifier reads it"
        )

    def test_caffeinate_is_omitted_when_it_is_turned_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The control for the test above. Without it, a runner that
        wrapped unconditionally would look correct."""
        marker = _install_fake_caffeinate(tmp_path, monkeypatch)
        _, ran = _exec_real_runner(tmp_path, monkeypatch, caffeinate=False)
        assert not marker.exists(), "caffeinate = false still wrapped the run"
        assert ran.argv[:3] == ("-m", "kstrl", "factory")

    @pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate is macOS-only")
    def test_the_factory_still_runs_correctly_under_real_caffeinate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The wrapper must not disturb the argv, the exit code, or the
        pid the lease is adopted by.

        Measured on macOS 25.5 while writing this: ``caffeinate -i cmd``
        **execs in place**, so the pid ``on_spawn`` reports is the
        factory's own - there is no intermediate process. That is pinned
        here as a canary rather than assumed: ``subprocess_factory_runner``'s
        docstring describes the factory as a GRANDCHILD of the daemon
        under caffeinate, and the process-group termination path is built
        around descendants outliving a signal to the direct child. If a
        future caffeinate forks instead of exec'ing, this fails and that
        reasoning wants re-checking.

        This asserts topology only. Whether the power assertion survives a
        dark wake is a separate, unmeasured question tracked in #203.
        """
        import shutil

        if shutil.which("caffeinate") is None:
            pytest.skip("caffeinate not installed")
        seen: list[int] = []
        outcome, ran = _exec_real_runner(
            tmp_path,
            monkeypatch,
            caffeinate=True,
            exit_code=7,
            on_spawn=seen.append,
        )
        assert ran.argv[:3] == ("-m", "kstrl", "factory")
        assert outcome.returncode == 7, (
            "caffeinate must pass the factory's exit status through; the classifier reads it"
        )
        assert seen == [ran.pid], (
            "caffeinate no longer execs in place, so the daemon's direct "
            "child is not the factory - re-check the process-group "
            "termination path in subprocess_factory_runner"
        )


class TestTheArgvIsAcceptedByTheRealCli:
    """A check a recording stub structurally cannot make.

    A stub records whatever it is handed and is happy. If someone deletes
    `--project-name` or changes an option's arity, every assertion in the
    class above still passes and the daemon breaks on its next real run.
    So the recorded argv is parsed by the actual command object.

    Honest scope: for the merge-gate flag specifically this overlaps with
    ``test_config_control_plane.py::TestSafetyKnobs``, which enumerates
    that knob's surfaces and catches a rename on the CLI side (measured).
    The value added here is that this parses the argv the daemon ACTUALLY
    BUILT rather than a list restated in a test, so it also covers the
    flags no safety-knob test enumerates.
    """

    @staticmethod
    def _parse(argv: tuple[str, ...]) -> click.Context:
        from kstrl.cli import cli

        # argv is "-m kstrl factory ..."; drop the module invocation.
        assert argv[:2] == ("-m", "kstrl")
        name, *rest = argv[2:]
        command = cli.commands[name]
        ctx = click.Context(command, info_name=name)
        command.parse_args(ctx, list(rest))
        return ctx

    def test_the_real_factory_command_accepts_the_runners_argv(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, ran = _exec_real_runner(
            tmp_path,
            monkeypatch,
            pause_before_pr_merge=True,
        )
        ctx = self._parse(ran.argv)
        assert ctx.params["pause_before_pr_merge"] is True
        assert ctx.params["project_name"] == "seam-project"

    def test_the_negated_merge_gate_also_parses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, ran = _exec_real_runner(
            tmp_path,
            monkeypatch,
            pause_before_pr_merge=False,
        )
        ctx = self._parse(ran.argv)
        assert ctx.params["pause_before_pr_merge"] is False

    def test_the_control_an_unknown_flag_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without this the two tests above prove nothing: they would
        pass just as well against a command that accepted anything."""
        _, ran = _exec_real_runner(tmp_path, monkeypatch)
        with pytest.raises(click.NoSuchOption):
            self._parse(ran.argv + ("--flag-that-does-not-exist",))


class TestACycleWithNoInjectedRunnerLaunchesTheRealCommand:
    """Closes the last stub in the chain: ``_default_runner``.

    Review finding on #208. The tests above call
    ``subprocess_factory_runner`` directly, and the composition tests
    inject a recorder, so ``_default_runner`` - the adapter that binds
    ``cfg.caffeinate`` and forwards the other five arguments - sat
    between two tested halves and was executed by nothing. It had zero
    references anywhere in ``tests/``. Confirmed by mutation: making it
    forward a hardcoded wrong ``project_name`` passed all 3140 tests.

    These call ``serve_cycle`` with NO ``runner=`` at all. Only
    ``sys.executable`` is replaced, so the whole path - claim, gate
    resolution, ``_default_runner``, ``subprocess_factory_runner``,
    ``run_supervised`` - runs as shipped.
    """

    @staticmethod
    def _argv_from(record: Path) -> list[str]:
        assert record.exists(), (
            "no factory was launched, so the cycle never reached the default runner"
        )
        raw: dict[str, Any] = json.loads(record.read_text(encoding="utf-8"))
        return list(raw["argv"])

    def _run_cycle(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        caffeinate: bool,
        project_name: str = "widget-svc",
    ) -> list[str]:
        record = _install_stub_interpreter(tmp_path, monkeypatch)
        queue = Queue(tmp_path, QueueConfig())
        queue.add(
            "# Spec\n\nDo the thing.\n",
            title="local work",
            project_name=project_name,
        )
        serve_cycle(
            tmp_path,
            config=ServeConfig(
                caffeinate=caffeinate,
                factory_timeout_seconds=60.0,
            ),
        )
        return self._argv_from(record)

    def test_the_cycle_forwards_the_items_project_name(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The exact mutation that survived the first round of this PR."""
        argv = self._run_cycle(tmp_path, monkeypatch, caffeinate=False)
        assert argv[:3] == ["-m", "kstrl", "factory"]
        assert argv[argv.index("--project-name") + 1] == "widget-svc"

    def test_the_cycle_points_the_factory_at_the_queued_spec(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        argv = self._run_cycle(tmp_path, monkeypatch, caffeinate=False)
        spec = Path(argv[argv.index("--spec") + 1])
        assert spec.name == "spec.md"
        assert tmp_path in spec.parents, f"the factory was pointed outside the queue root: {spec}"
        assert Path(argv[argv.index("--root") + 1]) == tmp_path

    def test_the_cycle_carries_the_merge_gate_into_the_command(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A queued item defaults to STOP_AT_PR, so the launched command
        must carry the gate ON. This is the flag whose rename nothing
        else in the suite can see."""
        argv = self._run_cycle(tmp_path, monkeypatch, caffeinate=False)
        assert "--pause-before-pr-merge" in argv
        assert "--no-pause-before-pr-merge" not in argv

    def test_serve_caffeinate_true_reaches_the_launched_command(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_default_runner`` is the ONLY place ``cfg.caffeinate`` is
        read, so nothing else can catch it being dropped or inverted."""
        marker = _install_fake_caffeinate(tmp_path, monkeypatch)
        self._run_cycle(tmp_path, monkeypatch, caffeinate=True)
        assert marker.exists(), "serve.caffeinate = true did not reach the launched command"

    def test_serve_caffeinate_false_reaches_the_launched_command(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        marker = _install_fake_caffeinate(tmp_path, monkeypatch)
        self._run_cycle(tmp_path, monkeypatch, caffeinate=False)
        assert not marker.exists(), "serve.caffeinate = false still wrapped the launched command"


# --------------------------------------------------------------------------
# The composition: intake -> queue -> run, in one cycle
# --------------------------------------------------------------------------


def _enable_github_intake(root: Path) -> None:
    (root / "kstrl.toml").write_text(
        f'[intake_github]\nenabled = true\nrepo = "{REPO}"\ncomment_on_result = false\n',
        encoding="utf-8",
    )


def _recording_runner(
    calls: list[dict[str, Any]],
    outcome: RunOutcome | None = None,
) -> Any:
    """A factory stand-in for the composition tests.

    These tests are about whether the daemon reaches the runner AT ALL
    with remotely-sourced work, so the runner records and returns; the
    stub-driven classification tests own everything past that point.
    """
    result = outcome or RunOutcome(0)

    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
        on_spawn: Callable[[int], None] | None = None,
    ) -> RunOutcome:
        # The spec is read at CALL time on purpose. The queue moves the
        # item out of running/ when the cycle finishes, so a path
        # captured here and read afterwards is already stale - which is
        # correct behaviour, and would otherwise read as a defect.
        calls.append(
            {
                "spec_path": spec_path,
                "spec_exists": spec_path.exists(),
                "spec_text": (spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""),
                "project_name": project_name,
                "pause_before_pr_merge": pause_before_pr_merge,
            }
        )
        return result

    return runner


class TestRemoteWorkSurvivesTheSeam:
    """What ``TestServePollsIntake`` in tests/test_intake_github.py does
    not already assert.

    That class is the regression test for #189 F1 and it is thorough:
    polled every cycle, disabled makes no calls, a synced item runs in
    the same cycle, failures and exceptions do not stop the cycle,
    repeated cycles do not re-admit, and intake precedes the gates.
    Verified by mutation: deleting the ``_run_intake`` call from
    ``serve_cycle`` turns 9 of those tests red.

    So the composition itself is covered. What is not is what the
    remote item CONTAINS by the time the factory is invoked - the two
    facts below, both of which sit on the runner boundary that every
    other test stubs.
    """

    def test_the_spec_handed_to_the_factory_is_the_issue_body(
        self,
        tmp_path: Path,
    ) -> None:
        """The seam is only real if the content survives it. Existing
        tests assert the item reaches DONE, which a cycle that handed the
        factory an empty or missing spec would also satisfy."""
        _enable_github_intake(tmp_path)
        gh = _GhStub(
            issues=_issue_payload(
                _issue(7, title="Add a widget", body="Build the widget."),
            )
        )
        calls: list[dict[str, Any]] = []
        with patch("kstrl.intake_github.run_gh", gh):
            serve_cycle(tmp_path, runner=_recording_runner(calls))

        assert len(calls) == 1
        assert calls[0]["spec_exists"], (
            "the factory was handed a spec path that did not exist at the "
            f"moment it was invoked: {calls[0]['spec_path']}"
        )
        assert "Build the widget." in calls[0]["spec_text"]

    def test_a_remote_item_keeps_its_merge_gate_through_the_seam(
        self,
        tmp_path: Path,
    ) -> None:
        """A remotely-triggered run may never auto-merge. That is decided
        at admission; this asserts it is still true at the point the
        factory is actually invoked."""
        _enable_github_intake(tmp_path)
        gh = _GhStub(issues=_issue_payload(_issue(4)))
        calls: list[dict[str, Any]] = []
        with patch("kstrl.intake_github.run_gh", gh):
            serve_cycle(tmp_path, runner=_recording_runner(calls))

        assert len(calls) == 1
        assert calls[0]["pause_before_pr_merge"] is True

        # Covered already in tests/test_intake_github.py::TestServePollsIntake
        # and deliberately not repeated here: that the adapter is polled
        # every cycle, that a disabled adapter makes no gh calls, that a
        # synced item runs in the same cycle, that a failure or exception
        # does not stop the cycle, that repeated cycles do not re-admit,
        # and that intake precedes the admission gates.
