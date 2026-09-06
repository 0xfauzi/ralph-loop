"""R10.7: the open-PR bound, the daemon's flow control.

The property these tests exist to hold is narrow and easy to lose: the
daemon stops admitting work while `max_open_prs` kstrl-authored pull
requests are open, and it reaches GitHub to find that out ONLY when
every cheaper local gate has already admitted. The second half is not
decoration. The gates tuple in `serve_cycle` is built eagerly, so a
member of it runs even when an earlier member refuses; that is why this
gate is a standalone check after the factory lock rather than a fifth
tuple entry, and `test_no_gh_call_when_an_earlier_gate_refuses` is the
control that keeps it there.

The counter is exercised end to end through a fake `gh` on PATH rather
than a patched `subprocess.run` wherever the subprocess and the JSON
parse are part of what is being asserted.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import kstrl.pr
from kstrl.cli import cli
from kstrl.pr import GH_TIMEOUT, PR_FOOTER_MARKER
from kstrl.serve import (
    RunOutcome,
    ServeConfig,
    ServeError,
    SpendLedger,
    check_open_pr_bound,
    count_open_kstrl_prs,
    serve_cycle,
)
from kstrl.workqueue import ItemState
from tests.helpers.astwalk import assert_census, folds_to, package_sources
from tests.test_serve import _add, _no_spend, _queue, _stub_runner  # noqa: F401
from tests.test_serve_seam import _write_executable

#: Emits whatever JSON the test put in FAKE_GH_JSON. The real
#: `count_open_kstrl_prs` runs against it, subprocess and json.loads
#: included, so a change to the argv or the parse is caught here.
_FAKE_GH = """#!/bin/sh
cat "$FAKE_GH_JSON"
"""

#: Records that it ran, then fails. A test that asserts GitHub was NOT
#: reached needs the fake to be observable when it IS reached, and an
#: exit code the counter cannot mistake for a count.
_FAKE_GH_MARKER = """#!/bin/sh
touch "$FAKE_GH_MARKER_PATH"
exit 99
"""


def _boom(_: Path) -> int:
    """A counter that fails the test if the gate calls it."""
    raise AssertionError("the counter was called; the gate should have skipped it")


def _put_gh_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Make ``body`` the `gh` that a PATH lookup finds.

    PATH rather than patching `subprocess.run`, so the lookup, the
    process and the decode are all the real ones.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    _write_executable(bindir / "gh", body)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _install_fake_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
) -> Path:
    """A `gh` that prints ``rows``; returns the JSON file it reads.

    Rewrite the returned file to change what the next call sees.
    """
    _put_gh_on_path(tmp_path, monkeypatch, _FAKE_GH)
    payload = tmp_path / "fake_gh.json"
    payload.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setenv("FAKE_GH_JSON", str(payload))
    return payload


def _install_marker_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `gh` that touches a marker and exits 99; returns the marker."""
    _put_gh_on_path(tmp_path, monkeypatch, _FAKE_GH_MARKER)
    marker = tmp_path / "gh_was_called"
    monkeypatch.setenv("FAKE_GH_MARKER_PATH", str(marker))
    return marker


#: Where `gh` is actually spawned. `count_open_kstrl_prs` goes through
#: `intake_github.run_gh`, so patching `kstrl.serve.subprocess.run` would
#: patch nothing and the tests would silently reach the real `gh`.
_GH_RUN = "kstrl.intake_github.subprocess.run"


def _completed(returncode: int, *, stdout: str = "", stderr: str = "") -> CompletedProcess[str]:
    return CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


def _marked(number: int) -> dict[str, object]:
    return {"number": number, "body": f"Some body\n\n---\n{PR_FOOTER_MARKER}"}


def _unmarked(number: int) -> dict[str, object]:
    return {"number": number, "body": "A hand-written PR body"}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestCheckOpenPrBound:
    def test_bound_disabled_skips_counter(self, tmp_path: Path) -> None:
        """0 is off, and off means no GitHub call at all."""
        admission = check_open_pr_bound(
            ServeConfig(max_open_prs=0),
            tmp_path,
            counter=_boom,
        )
        assert admission.allowed
        assert admission.reason == "open-PR bound disabled"

    def test_bound_not_applicable_when_no_prs(self, tmp_path: Path) -> None:
        """Nothing to bound when the factory opens no PRs."""
        (tmp_path / "kstrl.toml").write_text("[factory]\ncreate_prs = false\n", encoding="utf-8")

        admission = check_open_pr_bound(
            ServeConfig(max_open_prs=1),
            tmp_path,
            counter=_boom,
        )

        assert admission.allowed
        assert "not applicable" in admission.reason
        assert "create_prs = false" in admission.reason

    def test_under_bound_allows(self, tmp_path: Path) -> None:
        admission = check_open_pr_bound(
            ServeConfig(max_open_prs=1),
            tmp_path,
            counter=lambda _: 0,
        )
        assert admission.allowed
        assert admission.reason == "0 of 1 kstrl PRs open"

    def test_at_bound_refuses_as_wait(self, tmp_path: Path) -> None:
        """A wait, not a pause: the daemon re-checks next cycle.

        `pause_reason` empty is the whole difference. A pause needs a
        human to lift it; an open PR lifts itself when it merges.
        """
        admission = check_open_pr_bound(
            ServeConfig(max_open_prs=1),
            tmp_path,
            counter=lambda _: 1,
        )

        assert admission.allowed is False
        assert admission.pause_reason == ""
        assert admission.resume_after == ""
        assert "1 kstrl PR(s) open" in admission.reason
        assert "bound 1" in admission.reason

    def test_over_bound_refuses(self, tmp_path: Path) -> None:
        """Counts above the bound refuse too, not only exact equality."""
        admission = check_open_pr_bound(
            ServeConfig(max_open_prs=2),
            tmp_path,
            counter=lambda _: 5,
        )
        assert admission.allowed is False
        assert "5 kstrl PR(s) open" in admission.reason

    def test_counter_failure_refuses(self, tmp_path: Path) -> None:
        """An unknown number of open PRs is not zero."""

        def failing(_: Path) -> int:
            raise RuntimeError("gh: not found")

        admission = check_open_pr_bound(
            ServeConfig(max_open_prs=1),
            tmp_path,
            counter=failing,
        )

        assert admission.allowed is False
        assert "cannot count" in admission.reason
        assert "gh: not found" in admission.reason
        assert admission.pause_reason == ""


# ---------------------------------------------------------------------------
# The counter
# ---------------------------------------------------------------------------


class TestCountOpenKstrlPrs:
    def test_filters_by_marker(self, tmp_path: Path) -> None:
        rows = [_marked(1), _unmarked(2), _marked(3)]

        with patch(_GH_RUN, return_value=_completed(0, stdout=json.dumps(rows))) as run:
            assert count_open_kstrl_prs(tmp_path) == 2

        assert run.call_args.args[0] == [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,body",
        ]
        assert run.call_args.kwargs["timeout"] == GH_TIMEOUT
        assert run.call_args.kwargs["cwd"] == str(tmp_path)

    def test_limit_reaches_the_argv(self, tmp_path: Path) -> None:
        with patch(_GH_RUN, return_value=_completed(0, stdout="[]")) as run:
            assert count_open_kstrl_prs(tmp_path, limit=7) == 0
        assert "7" in run.call_args.args[0]

    @pytest.mark.parametrize(
        ("patch_kwargs", "match"),
        [
            ({"return_value": _completed(1, stderr="gh: auth required\n")}, "auth required"),
            (
                {"side_effect": subprocess.TimeoutExpired(cmd=["gh"], timeout=GH_TIMEOUT)},
                "timed out",
            ),
            ({"side_effect": FileNotFoundError("gh")}, "could not run"),
            ({"return_value": _completed(0, stdout="not json")}, "unparseable"),
            ({"return_value": _completed(0, stdout='{"number": 1}')}, "expected a list"),
        ],
        ids=["gh error", "timeout", "exec failed", "unparseable", "non-list payload"],
    )
    def test_every_failure_shape_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        patch_kwargs: dict[str, object],
        match: str,
    ) -> None:
        """Each failure the counter can meet becomes a RuntimeError.

        `shutil.which` is pinned so these say the same thing on a machine
        with no `gh`; the missing-binary case is its own test below.
        """
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
        with patch(_GH_RUN, **patch_kwargs), pytest.raises(RuntimeError, match=match):
            count_open_kstrl_prs(tmp_path)

    def test_raises_when_gh_is_not_installed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _: None)
        with pytest.raises(RuntimeError, match="not installed"):
            count_open_kstrl_prs(tmp_path)

    def test_counts_through_a_real_subprocess(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end: PATH lookup, process, stdout, decode, filter."""
        _install_fake_gh(tmp_path, monkeypatch, [_marked(1), _unmarked(2)])
        assert count_open_kstrl_prs(tmp_path) == 1


# ---------------------------------------------------------------------------
# The gate inside the cycle
# ---------------------------------------------------------------------------


class TestServeCycleGate:
    def test_open_pr_holds_the_item_in_the_queue(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One marked PR open, bound 1: nothing is leased and nothing runs."""
        payload = _install_fake_gh(tmp_path, monkeypatch, [_marked(1)])
        queue = _queue(tmp_path)
        _add(queue)
        calls: list[dict[str, object]] = []

        result = serve_cycle(
            tmp_path,
            config=ServeConfig(max_open_prs=1),
            runner=_stub_runner(RunOutcome(0), calls),
        )

        assert result.ran_item == ""
        assert "bound 1" in result.skipped
        assert result.paused == ""
        assert calls == []
        assert _queue(tmp_path).items()[0].state is ItemState.QUEUED

        # The same repo with the PR merged admits the same item.
        payload.write_text("[]", encoding="utf-8")
        result = serve_cycle(
            tmp_path,
            config=ServeConfig(max_open_prs=1),
            runner=_stub_runner(RunOutcome(0), calls),
        )

        assert result.ran_item != ""
        assert result.skipped == ""
        assert len(calls) == 1

    def test_no_gh_call_when_an_earlier_gate_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The order property, asserted on the syscall rather than on prose.

        The gates tuple is built eagerly, so membership of it would run
        this gate even behind a refusing budget. The fake `gh` records
        that it ran; with the budget already spent, it must not have.
        """
        marker = _install_marker_gh(tmp_path, monkeypatch)
        (tmp_path / "kstrl.toml").write_text(
            "[serve]\ndaily_budget_usd = 5.0\nallow_uncovered_cost = true\n",
            encoding="utf-8",
        )
        SpendLedger(tmp_path).charge(10.0, covered_calls=1, total_calls=1)
        _add(_queue(tmp_path))

        result = serve_cycle(
            tmp_path,
            config=ServeConfig(
                max_open_prs=1,
                daily_budget_usd=5.0,
                allow_uncovered_cost=True,
            ),
            runner=_stub_runner(RunOutcome(0)),
        )

        assert "daily budget reached" in result.skipped
        assert not marker.exists(), "the open-PR gate reached gh behind a refusing budget gate"


# ---------------------------------------------------------------------------
# The dry-run listing
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_lists_the_gate_last(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--dry-run` lists gates in evaluation order; this one is last."""
        _install_fake_gh(tmp_path, monkeypatch, [])

        result = CliRunner().invoke(
            cli,
            ["serve", "--dry-run", "--root", str(tmp_path), "--no-color"],
        )

        assert result.exit_code == 0
        lines = result.output.splitlines()
        inbox = next(i for i, line in enumerate(lines) if "gate inbox cap" in line)
        bound = next(i for i, line in enumerate(lines) if "gate open-PR bound" in line)
        assert bound > inbox
        assert "gate open-PR bound: ok" in result.output

    def test_dry_run_reports_the_gate_blocking(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_gh(tmp_path, monkeypatch, [_marked(1)])

        result = CliRunner().invoke(
            cli,
            ["serve", "--dry-run", "--root", str(tmp_path), "--no-color"],
        )

        assert result.exit_code == 0
        assert "gate open-PR bound: BLOCKS - 1 kstrl PR(s) open (bound 1)" in result.output


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_env_beats_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[serve]\nmax_open_prs = 3\n", encoding="utf-8")
        assert ServeConfig.load(tmp_path).max_open_prs == 3

        monkeypatch.setenv("KSTRL_SERVE_MAX_OPEN_PRS", "0")
        assert ServeConfig.load(tmp_path).max_open_prs == 0
        assert ServeConfig.from_env().max_open_prs == 0

    def test_the_default_is_one(self) -> None:
        assert ServeConfig().max_open_prs == 1

    def test_a_negative_bound_is_a_config_error(self) -> None:
        with pytest.raises(ServeError, match="max_open_prs must be >= 0"):
            ServeConfig(max_open_prs=-1)

    def test_a_negative_bound_in_toml_is_a_config_error(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[serve]\nmax_open_prs = -1\n", encoding="utf-8")
        with pytest.raises(ServeError, match="max_open_prs must be >= 0"):
            ServeConfig.load(tmp_path)


# ---------------------------------------------------------------------------
# The marker constant
# ---------------------------------------------------------------------------


#: The marker is spelled ONCE in ``kstrl/``: the constant's own
#: definition. Anything else is a second spelling, which is the drift the
#: hoist exists to prevent - a reader in another module reaches for the
#: nearest spelling, and a footer reword then makes the bound count zero
#: while every test stays green.
EXPECTED_MARKER_SPELLINGS: dict[str, int] = {"pr.py": 1}


class TestFooterMarker:
    def test_the_marker_is_spelled_once_in_the_package(self) -> None:
        """Layer 1, the net: every expression in ``kstrl/`` that folds to
        the marker, counted per module, whatever it does with the string
        afterwards. Package-wide rather than scoped to ``pr.py``, because
        the modules that will grow a second spelling are the READERS -
        this bound, the dampener, the polled steering channel (#231) -
        and a guard that only reads ``pr.py`` cannot see them."""
        assert_census(
            sources=package_sources(),
            sees=folds_to(PR_FOOTER_MARKER),
            expected=EXPECTED_MARKER_SPELLINGS,
            control=f'footer = "{PR_FOOTER_MARKER}"\n',
            message=(
                "The set of places spelling the kstrl PR footer changed. A "
                "reader identifying a kstrl-authored PR must import "
                "PR_FOOTER_MARKER from kstrl.pr, not repeat the literal: the "
                "open-PR bound counts bodies containing it, so a second "
                "spelling that drifts makes the count silently zero."
            ),
        )

    def test_both_footer_sites_use_the_constant(self) -> None:
        """Layer 2, the message: ``pr.py``'s two writers still go through
        the constant. The census above cannot say this - deleting a
        footer site leaves the spelling count at 1 - and "you wrote the
        footer without the constant" is the wrong message for it."""
        source = Path(kstrl.pr.__file__).read_text(encoding="utf-8")
        assert source.count("lines.append(PR_FOOTER_MARKER)") == 2
