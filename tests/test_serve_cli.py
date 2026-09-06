"""R8.6 PR 2: `ks serve` CLI tests.

`--dry-run` is the surface an operator uses to answer "would this spend
money right now, and why not", so the tests below pin that it reports the
same gates the loop evaluates. A dry run that disagrees with the real
cycle is worse than no dry run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from kstrl.serve import RunOutcome, RunSpend, SpendLedger
from kstrl.workqueue import ItemState, Queue, QueueConfig


def _queue(root: Path) -> Queue:
    return Queue(root, QueueConfig())


def _invoke(args: list[str], root: Path) -> Result:
    return CliRunner().invoke(cli, [*args, "--root", str(root), "--no-color"])


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "feature.md"
    path.write_text("# Feature\n\nDo the thing.\n")
    return path


@pytest.fixture(autouse=True)
def _no_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kstrl.serve.read_run_spend",
        lambda root, run_id: RunSpend(),
    )


#: Nothing here is about flow control; the fixture's docstring in
#: tests/conftest.py says why the R10.7 bound has to be held open.
pytestmark = pytest.mark.usefixtures("no_open_prs")


class TestServeHelp:
    def test_the_command_is_registered(self) -> None:
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "serve" in result.output

    def test_help_documents_once_and_dry_run(self) -> None:
        result = CliRunner().invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--once" in result.output
        assert "--dry-run" in result.output

    def test_help_states_the_retry_rule(self) -> None:
        """The most consequential behaviour should be discoverable."""
        result = CliRunner().invoke(cli, ["serve", "--help"])
        assert "infrastructure" in result.output
        assert "poison" in result.output


class TestDryRun:
    def test_dry_run_spends_nothing(
        self,
        tmp_path: Path,
        spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        with patch("kstrl.serve.subprocess_factory_runner") as runner:
            result = _invoke(["serve", "--dry-run"], tmp_path)
        assert result.exit_code == 0
        assert runner.call_count == 0
        assert _queue(tmp_path).items()[0].state is ItemState.QUEUED

    def test_dry_run_names_the_next_item(
        self,
        tmp_path: Path,
        spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        result = _invoke(["serve", "--dry-run"], tmp_path)
        item = _queue(tmp_path).items()[0]
        assert item.item_id[:12] in result.output

    def test_dry_run_on_an_empty_queue(self, tmp_path: Path) -> None:
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert result.exit_code == 0
        assert "nothing ready" in result.output

    def test_dry_run_reports_every_gate(self, tmp_path: Path) -> None:
        result = _invoke(["serve", "--dry-run"], tmp_path)
        for gate in (
            "poison breaker",
            "cost coverage",
            "budget",
            "inbox cap",
            "open-PR bound",
        ):
            assert gate in result.output

    def test_dry_run_reports_safe_mode_above_the_gates(
        self,
        tmp_path: Path,
    ) -> None:
        """R10.4. Above the gates because it frames them: an operator
        reading "budget: ok" while the queue is paused for an unreadable
        control file has been told the truth and still misled."""
        result = _invoke(["serve", "--dry-run"], tmp_path)

        lines = result.output.splitlines()
        safe = next(i for i, line in enumerate(lines) if "safe mode:" in line)
        gate = next(i for i, line in enumerate(lines) if "gate budget" in line)
        assert safe < gate
        assert "nominal" in lines[safe]

    def test_dry_run_names_a_safe_mode_reason(self, tmp_path: Path) -> None:
        _queue(tmp_path).pause(reason="poison breaker tripped", actor="test")

        result = _invoke(["serve", "--dry-run"], tmp_path)

        assert "safe mode:" in result.output
        assert "1 reason(s)" in result.output
        assert "[queue] poison breaker tripped" in result.output

    def test_safe_mode_blocks_nothing_by_itself(self, tmp_path: Path) -> None:
        """It is a report, not a gate. Every signal it reads already
        refuses where refusing is right, so the predicate adding a second
        refusal would be a behaviour change this issue does not make."""
        _queue(tmp_path).pause(reason="paused", actor="test")

        result = _invoke(["serve", "--dry-run"], tmp_path)

        assert result.exit_code == 0
        for gate in ("poison breaker", "cost coverage", "budget", "inbox cap"):
            assert f"gate {gate}: ok" in result.output

    def test_dry_run_reports_a_blocking_budget(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[serve]\ndaily_budget_usd = 5.0\nallow_uncovered_cost = true\n"
        )
        SpendLedger(tmp_path).charge(10.0, covered_calls=1, total_calls=1)
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert "BLOCKS" in result.output
        assert "daily budget reached" in result.output

    def test_dry_run_labels_a_floor_total(self, tmp_path: Path) -> None:
        """H4: never let a floor read as a measurement."""
        SpendLedger(tmp_path).charge(3.0, covered_calls=1, total_calls=3)
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert "FLOOR" in result.output

    def test_dry_run_reports_a_paused_queue(self, tmp_path: Path) -> None:
        _invoke(["queue", "pause", "--reason", "operator"], tmp_path)
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert "operator" in result.output

    def test_dry_run_reports_an_unset_budget_as_uncapped(
        self,
        tmp_path: Path,
    ) -> None:
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert "unset (no cap)" in result.output


class TestServeOnce:
    def test_once_drains_a_single_item(
        self,
        tmp_path: Path,
        spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        with patch(
            "kstrl.serve.subprocess_factory_runner",
            return_value=RunOutcome(returncode=0),
        ):
            result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 0
        assert _queue(tmp_path).items()[0].state is ItemState.DONE

    def test_a_poisoned_item_exits_nonzero(
        self,
        tmp_path: Path,
        spec_file: Path,
    ) -> None:
        """launchd reads the exit status; a poisoned item is not success."""
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        with patch(
            "kstrl.serve.subprocess_factory_runner",
            return_value=RunOutcome(returncode=1),
        ):
            result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 1
        assert _queue(tmp_path).items()[0].state is ItemState.POISON

    def test_an_exhausted_infra_verdict_also_exits_nonzero(
        self,
        tmp_path: Path,
        spec_file: Path,
    ) -> None:
        """#186 F10: the case the may_retry filter let through.

        An infrastructure verdict whose last attempt is spent is poisoned
        by serve_cycle, but Verdict.RETRY_INFRA.may_retry stays true - so
        the old filter excluded it and `ks serve --once` exited 0 on work
        that was waiting for a human.
        """
        from kstrl.findings import Finding
        from kstrl.manifest import Component, ComponentStatus, Manifest

        _invoke(
            ["queue", "add", str(spec_file), "--max-attempts", "1"],
            tmp_path,
        )
        run_id = "factory-20260730-000000.000000-aaa"
        comp = Component("comp-a", "A", "", [], "a.json", "b/a")
        comp.status = ComponentStatus("failed")
        comp.findings = [Finding.infrastructure_error("review", "cli died")]
        manifest_path = tmp_path / "scripts" / "kstrl" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        Manifest(
            version="1",
            spec_file="s.md",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[comp],
            run_id=run_id,
        ).save(manifest_path)

        def fake_runner(**kwargs: object) -> RunOutcome:
            run_dir = tmp_path / ".kstrl" / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "events.jsonl").touch()
            return RunOutcome(returncode=1)

        with patch("kstrl.serve.subprocess_factory_runner", fake_runner):
            result = _invoke(["serve", "--once"], tmp_path)
        assert _queue(tmp_path).items()[0].state is ItemState.POISON
        assert result.exit_code == 1, "poisoned work must not report success"

    def test_a_reaped_poison_with_no_item_run_exits_nonzero(
        self,
        tmp_path: Path,
        spec_file: Path,
    ) -> None:
        """A path that sets no ran_item, which the old filter also skipped."""
        from kstrl.workqueue import QueueConfig as _QC

        _invoke(
            ["queue", "add", str(spec_file), "--max-attempts", "1"],
            tmp_path,
        )
        queue = Queue(tmp_path, _QC(max_attempts=1))
        queue.start(queue.lease(queue.items()[0], pid=999999))
        with patch(
            "kstrl.serve.subprocess_factory_runner",
            return_value=RunOutcome(returncode=0),
        ):
            result = _invoke(["serve", "--once"], tmp_path)
        assert _queue(tmp_path).items()[0].state is ItemState.POISON
        assert result.exit_code == 1

    def test_an_empty_queue_exits_zero(self, tmp_path: Path) -> None:
        result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 0

    def test_a_held_singleton_lock_exits_two(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        from kstrl.serve import serve_lock

        with serve_lock(tmp_path):
            result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 2
        assert "another ks serve" in result.output

    def test_an_invalid_config_exits_two(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[serve]\nmax_consecutive_poison = 0\n")
        result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 2
        assert "max_consecutive_poison" in result.output

    def test_the_banner_reports_the_effective_settings(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[serve]\ndaily_budget_usd = 7.5\ncaffeinate = false\n"
        )
        result = _invoke(["serve", "--once"], tmp_path)
        assert "$7.50/day" in result.output
        assert "caffeinate off" in result.output


class TestLaunchdPlist:
    """R8.6 PR 4: the plist must be valid to macOS, not just to us."""

    @staticmethod
    def _plist(root: Path, *args: str) -> str:
        result = CliRunner().invoke(
            cli,
            ["serve", "--print-plist", "--root", str(root), *args],
        )
        assert result.exit_code == 0, result.output
        return result.output

    @staticmethod
    def _timeout_toml(root: Path, seconds: float = 1800.0) -> None:
        (root / "kstrl.toml").write_text(f"[serve]\nfactory_timeout_seconds = {seconds}\n")

    def test_the_plist_parses_with_macos_own_parser(
        self,
        tmp_path: Path,
    ) -> None:
        import plistlib

        parsed = plistlib.loads(self._plist(tmp_path).encode())
        assert parsed["Label"].startswith("com.kstrl.serve.")
        assert parsed["RunAtLoad"] is True
        assert parsed["ProcessType"] == "Background"

    def test_keepalive_mode_is_the_default(self, tmp_path: Path) -> None:
        import plistlib

        parsed = plistlib.loads(self._plist(tmp_path).encode())
        assert parsed["KeepAlive"] is True
        assert "StartCalendarInterval" not in parsed
        assert "--once" not in parsed["ProgramArguments"]

    def test_interval_mode_uses_a_WAKE_CATCHING_scheduler(
        self,
        tmp_path: Path,
    ) -> None:
        """#189 F3: StartInterval MISSES firings that elapse during sleep.

        launchd.plist(5) is explicit: a StartInterval firing during sleep
        "will be missed due to shortcomings in kqueue(3)", whereas
        StartCalendarInterval "will start the job the next time the
        computer wakes up". On a laptop the difference is the whole point.
        """
        import plistlib

        self._timeout_toml(tmp_path)
        parsed = plistlib.loads(
            self._plist(
                tmp_path,
                "--plist-mode",
                "interval",
                "--plist-interval",
                "10",
            ).encode()
        )
        assert "StartInterval" not in parsed, "misses sleep firings"
        assert parsed["StartCalendarInterval"] == [{"Minute": m} for m in range(0, 60, 10)]
        assert parsed["ProgramArguments"][-1] == "--once"

    def test_interval_mode_refuses_without_a_run_timeout(
        self,
        tmp_path: Path,
    ) -> None:
        """#189 F2: launchd bounds neither runtime nor overlap.

        launchd.plist(5): "If the job is running during an interval
        firing, that interval firing will likewise be missed." So a
        wedged cycle is not killed and not replaced - it silently blocks
        every later firing. factory_timeout_seconds is the only real
        bound, so the mode that schedules cycles requires one.
        """
        result = CliRunner().invoke(
            cli,
            [
                "serve",
                "--print-plist",
                "--root",
                str(tmp_path),
                "--plist-mode",
                "interval",
            ],
        )
        assert result.exit_code == 2
        assert "factory_timeout_seconds" in result.output

    def test_keepalive_does_not_require_a_run_timeout(
        self,
        tmp_path: Path,
    ) -> None:
        """It paces itself; there is no schedule to fall behind."""
        assert "KeepAlive" in self._plist(tmp_path)

    @pytest.mark.parametrize("minutes", [7, 11, 90, 2000])
    def test_an_inexpressible_interval_is_refused(
        self,
        tmp_path: Path,
        minutes: int,
    ) -> None:
        """A calendar schedule cannot express 'every 7 minutes'."""
        self._timeout_toml(tmp_path)
        result = CliRunner().invoke(
            cli,
            [
                "serve",
                "--print-plist",
                "--root",
                str(tmp_path),
                "--plist-mode",
                "interval",
                "--plist-interval",
                str(minutes),
            ],
        )
        assert result.exit_code == 2

    def test_hourly_intervals_are_expressed_as_hours(
        self,
        tmp_path: Path,
    ) -> None:
        import plistlib

        self._timeout_toml(tmp_path)
        parsed = plistlib.loads(
            self._plist(
                tmp_path,
                "--plist-mode",
                "interval",
                "--plist-interval",
                "360",
            ).encode()
        )
        assert parsed["StartCalendarInterval"] == [{"Hour": h, "Minute": 0} for h in (0, 6, 12, 18)]

    def test_the_restart_throttle_is_set(self, tmp_path: Path) -> None:
        """launchd's 10s default would restart a crash-loop 6x a minute."""
        import plistlib

        from kstrl.serve import LAUNCHD_THROTTLE_SECONDS

        parsed = plistlib.loads(self._plist(tmp_path).encode())
        assert parsed["ThrottleInterval"] == LAUNCHD_THROTTLE_SECONDS
        assert LAUNCHD_THROTTLE_SECONDS >= 60, "a spend control, not politeness"

    def test_PATH_includes_the_interpreter_and_homebrew(
        self,
        tmp_path: Path,
    ) -> None:
        """A LaunchAgent inherits no shell env; gh and git must be findable."""
        import plistlib

        parsed = plistlib.loads(self._plist(tmp_path).encode())
        path = parsed["EnvironmentVariables"]["PATH"]
        assert "/usr/bin" in path
        assert "/opt/homebrew/bin" in path
        assert str(Path(parsed["ProgramArguments"][0]).parent) in path

    def test_paths_are_absolute_and_root_scoped(self, tmp_path: Path) -> None:
        import plistlib

        parsed = plistlib.loads(self._plist(tmp_path).encode())
        root = str(tmp_path.resolve())
        assert parsed["WorkingDirectory"] == root
        assert parsed["StandardOutPath"].startswith(root)
        assert "--root" in parsed["ProgramArguments"]

    def test_the_label_is_unique_per_checkout(self, tmp_path: Path) -> None:
        """launchd keeps only the last job per Label, silently."""
        from kstrl.serve import launchd_label

        a = tmp_path / "checkout-a"
        b = tmp_path / "checkout-b"
        a.mkdir()
        b.mkdir()
        assert launchd_label(a) != launchd_label(b)
        assert launchd_label(a) == launchd_label(a)

    def test_the_log_directory_is_created(self, tmp_path: Path) -> None:
        """launchd creates the log file but not its parent."""
        from kstrl.serve import launchd_log_dir

        assert not launchd_log_dir(tmp_path).exists()
        self._plist(tmp_path)
        assert launchd_log_dir(tmp_path).is_dir()

    def test_printing_a_plist_spends_nothing_and_starts_no_daemon(
        self,
        tmp_path: Path,
    ) -> None:
        with patch("kstrl.serve.serve") as loop:
            with patch("kstrl.serve.subprocess_factory_runner") as runner:
                self._plist(tmp_path)
        assert loop.call_count == 0
        assert runner.call_count == 0

    def test_xml_special_characters_are_escaped(self, tmp_path: Path) -> None:
        import plistlib

        awkward = tmp_path / "a & b <test>"
        awkward.mkdir()
        parsed = plistlib.loads(self._plist(awkward).encode())
        assert parsed["WorkingDirectory"] == str(awkward.resolve())

    def test_a_path_XML_cannot_represent_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        """#189 F4: a control char produced a plist that failed to parse.

        XML 1.0 cannot represent them at all, so there is no correct
        escaping - the only honest outcome is a clear refusal.
        """
        awkward = tmp_path / "bad\x01name"
        awkward.mkdir()
        result = CliRunner().invoke(
            cli,
            ["serve", "--print-plist", "--root", str(awkward)],
        )
        assert result.exit_code == 2
        assert "control character" in result.output

    def test_every_generated_plist_round_trips(self, tmp_path: Path) -> None:
        """The property that matters: whatever we emit, macOS can read."""
        import plistlib

        self._timeout_toml(tmp_path)
        for args in (
            (),
            ("--plist-mode", "interval", "--plist-interval", "5"),
            ("--plist-mode", "interval", "--plist-interval", "60"),
        ):
            raw = self._plist(tmp_path, *args).encode()
            assert plistlib.loads(raw)["Label"].startswith("com.kstrl.serve.")


class TestServeDryRunIncludesIntake:
    """#189 N2: a dry run that ignores intake contradicts the real cycle."""

    @staticmethod
    def _stub(issues: str, checkout: str = "o/r"):  # type: ignore[no-untyped-def]
        import json as _json

        from kstrl.intake_github import GhResult

        def fake(args, *, timeout, cwd=None):  # type: ignore[no-untyped-def]
            head = args[:2]
            if head == ["repo", "view"]:
                return GhResult(
                    ok=True,
                    stdout=_json.dumps({"nameWithOwner": checkout}),
                )
            if head == ["issue", "list"]:
                return GhResult(ok=True, stdout=issues)
            if head == ["api", "graphql"]:
                return GhResult(
                    ok=True,
                    stdout=_json.dumps(
                        {
                            "data": {
                                "repository": {
                                    "issue": {
                                        "lastEditedAt": None,
                                        "timelineItems": {
                                            "nodes": [
                                                {
                                                    "createdAt": "2026-07-30T10:00:00Z",
                                                    "label": {"name": "kstrl:queued"},
                                                    "actor": {"login": "o"},
                                                }
                                            ]
                                        },
                                    }
                                },
                            }
                        }
                    ),
                )
            return GhResult(ok=True)

        return fake

    @staticmethod
    def _issues() -> str:
        import json as _json

        return _json.dumps(
            [
                {
                    "number": 4,
                    "title": "remote work",
                    "body": "Build it.",
                    "url": "u",
                    "labels": [{"name": "kstrl:queued"}],
                }
            ]
        )

    def test_dry_run_reports_what_intake_would_admit(
        self,
        tmp_path: Path,
    ) -> None:
        """An empty queue plus enabled intake is NOT 'nothing ready'."""
        (tmp_path / "kstrl.toml").write_text('[intake_github]\nenabled = true\nrepo = "o/r"\n')
        with patch("kstrl.intake_github.run_gh", self._stub(self._issues())):
            result = _invoke(["serve", "--dry-run"], tmp_path)
        assert result.exit_code == 0
        assert "would admit o/r#4" in result.output
        assert "nothing ready" not in result.output, (
            "reporting 'nothing ready' while intake is about to admit work "
            "is exactly the disagreement this guards against"
        )

    def test_dry_run_still_writes_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text('[intake_github]\nenabled = true\nrepo = "o/r"\n')
        with patch("kstrl.intake_github.run_gh", self._stub(self._issues())):
            _invoke(["serve", "--dry-run"], tmp_path)
        assert _queue(tmp_path).items() == [], "a dry run admits nothing"

    def test_dry_run_says_so_when_intake_is_disabled(
        self,
        tmp_path: Path,
    ) -> None:
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert "intake" in result.output
        assert "disabled" in result.output


class TestScheduledJobRequiresATimeout:
    """#189 N3: generation-time checks bind only the config of that day."""

    def test_the_marker_travels_with_the_scheduled_job(
        self,
        tmp_path: Path,
    ) -> None:
        import plistlib

        from kstrl.serve import REQUIRE_TIMEOUT_ENV

        (tmp_path / "kstrl.toml").write_text("[serve]\nfactory_timeout_seconds = 1800.0\n")
        result = CliRunner().invoke(
            cli,
            [
                "serve",
                "--print-plist",
                "--root",
                str(tmp_path),
                "--plist-mode",
                "interval",
                "--plist-interval",
                "10",
            ],
        )
        parsed = plistlib.loads(result.output.encode())
        assert parsed["EnvironmentVariables"][REQUIRE_TIMEOUT_ENV] == "1"

    def test_keepalive_carries_no_marker(self, tmp_path: Path) -> None:
        """It paces itself; there is no schedule to fall behind."""
        import plistlib

        from kstrl.serve import REQUIRE_TIMEOUT_ENV

        result = CliRunner().invoke(
            cli,
            ["serve", "--print-plist", "--root", str(tmp_path)],
        )
        parsed = plistlib.loads(result.output.encode())
        assert REQUIRE_TIMEOUT_ENV not in parsed["EnvironmentVariables"]

    def test_a_scheduled_run_fails_closed_when_the_timeout_is_removed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The operator cleared the timeout after installing the job."""
        from kstrl.serve import REQUIRE_TIMEOUT_ENV

        monkeypatch.setenv(REQUIRE_TIMEOUT_ENV, "1")
        (tmp_path / "kstrl.toml").write_text("[serve]\n")
        result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 2
        assert "factory_timeout_seconds" in result.output

    def test_a_scheduled_run_proceeds_when_the_timeout_is_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kstrl.serve import REQUIRE_TIMEOUT_ENV

        monkeypatch.setenv(REQUIRE_TIMEOUT_ENV, "1")
        (tmp_path / "kstrl.toml").write_text("[serve]\nfactory_timeout_seconds = 600.0\n")
        assert _invoke(["serve", "--once"], tmp_path).exit_code == 0

    def test_an_unmarked_run_is_unaffected(
        self,
        tmp_path: Path,
    ) -> None:
        """A hand-run `ks serve` keeps working without a timeout."""
        assert _invoke(["serve", "--once"], tmp_path).exit_code == 0


class TestUniformCadence:
    """#189 N6: an hour count must divide 24 or the schedule is uneven."""

    @pytest.mark.parametrize("hours", [5, 7, 9, 10, 11])
    def test_an_hour_count_that_does_not_divide_24_is_refused(
        self,
        tmp_path: Path,
        hours: int,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[serve]\nfactory_timeout_seconds = 600.0\n")
        result = CliRunner().invoke(
            cli,
            [
                "serve",
                "--print-plist",
                "--root",
                str(tmp_path),
                "--plist-mode",
                "interval",
                "--plist-interval",
                str(hours * 60),
            ],
        )
        assert result.exit_code == 2
        assert "divide a day evenly" in result.output

    @pytest.mark.parametrize("hours", [2, 3, 4, 6, 8, 12, 24])
    def test_every_accepted_hour_count_yields_a_uniform_cadence(
        self,
        hours: int,
    ) -> None:
        from kstrl.serve import calendar_schedule

        entries = calendar_schedule(hours * 60)
        marks = [e["Hour"] for e in entries]
        gaps = {(marks + [marks[0] + 24])[i + 1] - marks[i] for i in range(len(marks))}
        assert gaps == {hours}, f"uneven cadence for {hours}h: {marks}"

    def test_exactly_one_hour_is_expressed_as_an_hourly_minute_mark(
        self,
    ) -> None:
        """60 minutes takes the minute branch, which is still hourly.

        A StartCalendarInterval entry treats unspecified fields as
        wildcards, so {"Minute": 0} means "every hour at :00".
        """
        from kstrl.serve import calendar_schedule

        assert calendar_schedule(60) == [{"Minute": 0}]

    @pytest.mark.parametrize("minutes", [1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60])
    def test_every_accepted_minute_count_yields_a_uniform_cadence(
        self,
        minutes: int,
    ) -> None:
        from kstrl.serve import calendar_schedule

        entries = calendar_schedule(minutes)
        marks = [e["Minute"] for e in entries]
        gaps = {(marks + [marks[0] + 60])[i + 1] - marks[i] for i in range(len(marks))}
        assert gaps == {minutes}


class TestCliHelpContract:
    """#189 N4: every user-facing copy must state one verified contract."""

    def test_the_cli_help_states_the_verified_contract(self) -> None:
        result = CliRunner().invoke(cli, ["serve", "--help"])
        assert "StartCalendarInterval" in result.output
        assert "fires immediately" not in result.output, (
            "the reversed StartInterval-on-wake claim must be gone"
        )

    def test_the_help_mentions_intake_polling(self) -> None:
        """The cycle now syncs; the contract should say so."""
        result = CliRunner().invoke(cli, ["serve", "--help"])
        assert "inbox" in result.output or "intake" in result.output
