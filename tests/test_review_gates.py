"""R1.1-R1.3 reviewer-gate integrity tests.

A gate that can be passed by silence, case drift, or absence of data is
not a gate. These tests prove the parser-side fixes:

- R1.1: empty/partial reviews and unrecognized verdicts are
  infrastructure errors, never silent passes or advisories.
- R1.2: AgentOutputTooLarge / reviewer crashes / non-dict JSON degrade
  to per-component infrastructure failures; skipped phases leave a
  synthetic Finding + journal event; PR bodies show "did not run";
  parse failures dump the FULL raw output to disk.
- R1.3: a git error during diff fetch is an infrastructure failure,
  not an empty diff that reviews cleanly.
"""

from __future__ import annotations

import io
import json
import subprocess
import traceback
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, FactoryResult, run_factory
from kstrl.findings import Finding, render_findings_markdown
from kstrl.git import GitDiffError, get_diff_content
from kstrl.manifest import Component, Manifest
from kstrl.pipeline import ComponentPipeline
from kstrl.review import (
    ReviewMode,
    ReviewResult,
    parse_review_output,
    run_review,
)
from kstrl.security import (
    SecurityConfig,
    SecurityMode,
    SecurityResult,
    parse_security_output,
)
from kstrl.serve import RunOutcome, Verdict, classify_run
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig
from tests.conftest import ReviewRepo


class MockReviewAgent:
    """Mock agent that returns predetermined review JSON."""

    def __init__(self, output: str):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "mock-reviewer"

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        yield from self._output.splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


class CrashingAgent:
    """Agent whose run() raises mid-stream."""

    @property
    def name(self) -> str:
        return "crashing-reviewer"

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        raise RuntimeError("agent process exploded")
        yield  # pragma: no cover

    @property
    def final_message(self) -> str | None:
        return None


def _write_prd(path: Path, story_ids: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "branchName": "test",
                "userStories": [
                    {
                        "id": sid,
                        "title": f"Story {sid}",
                        "acceptanceCriteria": ["AC1"],
                        "priority": 1,
                        "passes": True,
                        "notes": "",
                    }
                    for sid in story_ids
                ],
            }
        )
    )


def _story(story_id: str, verdict: str) -> dict[str, object]:
    return {
        "storyId": story_id,
        "storyTitle": f"Story {story_id}",
        "criteria": [
            {
                "criterion": "AC1",
                "verdict": verdict,
                "explanation": "checked",
                "suggestion": "",
            }
        ],
    }


_VERIFICATION = VerificationResult(
    passed=True,
    checks=[CheckResult("test_suite", True, "ok")],
)


# ---------------------------------------------------------------------------
# R1.1 - criterion coverage: empty/partial reviews cannot pass
# ---------------------------------------------------------------------------


class TestR11Coverage:
    def test_empty_review_fails_hard_mode(self, review_repo: ReviewRepo) -> None:
        """{"stories":[],"concerns":[]} used to parse to passed=True
        (CRIT-5). With a PRD story expecting a verdict it is now an
        infrastructure error and hard mode blocks."""
        prd_path = review_repo.path / "prd.json"
        _write_prd(prd_path, ["US-001"])
        agent = MockReviewAgent(json.dumps({"stories": [], "concerns": []}))
        result = run_review(
            agent,
            prd_path,
            review_repo.path,
            review_repo.base_branch,
            _VERIFICATION,
            ReviewMode.HARD,
            PlainUI(no_color=True),
        )
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "US-001" in result.overall_notes

    def test_partial_review_is_infrastructure_error(self) -> None:
        output = json.dumps({"stories": [_story("US-001", "pass")]})
        result = parse_review_output(output, ["US-001", "US-002"])
        assert result.infrastructure_error is True
        assert result.passed is False
        # Only the uncovered id is reported as missing
        assert "story ids US-002" in result.overall_notes

    def test_full_coverage_passes(self) -> None:
        output = json.dumps(
            {
                "stories": [_story("US-001", "pass"), _story("US-002", "pass")],
            }
        )
        result = parse_review_output(output, ["US-001", "US-002"])
        assert result.infrastructure_error is False
        assert result.passed is True

    def test_story_id_match_is_case_insensitive(self) -> None:
        output = json.dumps({"stories": [_story("us-001 ", "pass")]})
        result = parse_review_output(output, ["US-001"])
        assert result.infrastructure_error is False

    def test_story_without_criteria_does_not_count_as_covered(self) -> None:
        output = json.dumps(
            {
                "stories": [{"storyId": "US-001", "criteria": []}],
            }
        )
        result = parse_review_output(output, ["US-001"])
        assert result.infrastructure_error is True

    def test_no_expected_ids_skips_coverage_check(self) -> None:
        """Direct callers without a PRD keep the old lenient behavior."""
        result = parse_review_output(
            json.dumps({"stories": [], "concerns": []}),
        )
        assert result.infrastructure_error is False
        assert result.passed is True


# ---------------------------------------------------------------------------
# R1.1 - verdict whitelist
# ---------------------------------------------------------------------------


class TestR11VerdictWhitelist:
    def test_uppercase_fail_blocks(self) -> None:
        """ "FAIL" was stored verbatim and matched neither gate,
        becoming a non-blocking advisory-alike."""
        output = json.dumps({"stories": [_story("US-001", "FAIL")]})
        result = parse_review_output(output, ["US-001"])
        assert result.infrastructure_error is False
        assert result.passed is False
        assert result.criteria[0].verdict == "fail"

    def test_pass_with_whitespace_passes(self) -> None:
        output = json.dumps({"stories": [_story("US-001", "PASS ")]})
        result = parse_review_output(output, ["US-001"])
        assert result.infrastructure_error is False
        assert result.passed is True
        assert result.criteria[0].verdict == "pass"

    def test_unknown_verdict_is_infrastructure_error(self) -> None:
        output = json.dumps({"stories": [_story("US-001", "Blocked")]})
        result = parse_review_output(output, ["US-001"])
        assert result.infrastructure_error is True
        assert result.passed is False
        assert "Blocked" in result.overall_notes

    def test_missing_verdict_is_infrastructure_error(self) -> None:
        output = json.dumps(
            {
                "stories": [
                    {
                        "storyId": "US-001",
                        "criteria": [{"criterion": "AC1", "explanation": "x"}],
                    }
                ],
            }
        )
        result = parse_review_output(output, ["US-001"])
        assert result.infrastructure_error is True

    def test_advisory_verdict_stays_valid(self) -> None:
        """The prompt schema promises pass|fail|advisory; a legitimate
        advisory verdict must not be treated as a parse failure."""
        output = json.dumps({"stories": [_story("US-001", "Advisory")]})
        result = parse_review_output(output, ["US-001"])
        assert result.infrastructure_error is False
        assert result.passed is True
        assert result.criteria[0].verdict == "advisory"


# ---------------------------------------------------------------------------
# R1.2 - oversized output, crashes, non-dict JSON
# ---------------------------------------------------------------------------


class TestR12InfrastructurePaths:
    def _run(self, mode: ReviewMode, repo: ReviewRepo) -> ReviewResult:
        from kstrl.decompose import AgentOutputTooLarge

        prd_path = repo.path / "prd.json"
        _write_prd(prd_path, ["US-001"])
        agent = MockReviewAgent("irrelevant")
        with patch(
            "kstrl.review.collect_agent_output",
            side_effect=AgentOutputTooLarge("output exceeded cap"),
        ):
            return run_review(
                agent,
                prd_path,
                repo.path,
                repo.base_branch,
                _VERIFICATION,
                mode,
                PlainUI(no_color=True),
            )

    def test_oversized_output_is_infra_and_blocks_hard_mode(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        result = self._run(ReviewMode.HARD, review_repo)
        assert result.infrastructure_error is True
        assert result.passed is False
        findings = result.as_findings()
        assert len(findings) == 1
        assert findings[0].is_infrastructure_error

    def test_oversized_output_in_advisory_passes_but_leaves_trace(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        result = self._run(ReviewMode.ADVISORY, review_repo)
        assert result.infrastructure_error is True
        assert result.passed is True
        assert result.as_findings()[0].is_infrastructure_error

    def test_agent_crash_never_raises(self, review_repo: ReviewRepo) -> None:
        prd_path = review_repo.path / "prd.json"
        _write_prd(prd_path, ["US-001"])
        result = run_review(
            CrashingAgent(),
            prd_path,
            review_repo.path,
            review_repo.base_branch,
            _VERIFICATION,
            ReviewMode.HARD,
            PlainUI(no_color=True),
        )
        assert result.infrastructure_error is True
        assert result.passed is False
        assert "exploded" in result.overall_notes

    @pytest.mark.parametrize("raw", ["null", "[1, 2]", '"just a string"'])
    def test_non_dict_json_is_infra_not_crash(self, raw: str) -> None:
        result = parse_review_output(raw, ["US-001"])
        assert result.infrastructure_error is True
        assert result.passed is False


# ---------------------------------------------------------------------------
# R1.2 - full raw-output debug dumps
# ---------------------------------------------------------------------------


class TestR12DebugDumps:
    def test_review_parse_failure_dumps_full_output(
        self,
        tmp_path: Path,
    ) -> None:
        raw = "not json " * 1000  # far beyond the 2000-char field cap
        result = parse_review_output(raw, ["US-001"], debug_dir=tmp_path)
        assert result.infrastructure_error is True
        dumped = (tmp_path / "_review_raw.txt").read_text(encoding="utf-8")
        assert dumped == raw  # FULL output, forensic tail intact
        assert len(result.raw_output) <= 2000
        assert str(tmp_path / "_review_raw.txt") in result.overall_notes

    def test_security_parse_failure_dumps_full_output(
        self,
        tmp_path: Path,
    ) -> None:
        raw = "garbage " * 1000
        result = parse_security_output(raw, "hard", debug_dir=tmp_path)
        assert result.infrastructure_error is True
        dumped = (tmp_path / "_security_raw.txt").read_text(encoding="utf-8")
        assert dumped == raw
        assert len(result.raw_output) <= 2000

    def test_clean_parse_writes_no_dump(self, tmp_path: Path) -> None:
        output = json.dumps({"stories": [_story("US-001", "pass")]})
        parse_review_output(output, ["US-001"], debug_dir=tmp_path)
        assert not (tmp_path / "_review_raw.txt").exists()

    def test_missing_debug_dir_is_not_an_error(self) -> None:
        result = parse_review_output("not json", ["US-001"], debug_dir=None)
        assert result.infrastructure_error is True


# ---------------------------------------------------------------------------
# R1.2 - phase_skipped Finding semantics
# ---------------------------------------------------------------------------


class TestPhaseSkippedFinding:
    def test_flags(self) -> None:
        f = Finding.phase_skipped("security", "budget exhausted")
        assert f.is_phase_skip is True
        assert f.is_infrastructure_error is False
        assert f.category == "phase_skipped"
        assert "non_execution" in f.tags

    def test_render_callout(self) -> None:
        md = render_findings_markdown(
            [Finding.phase_skipped("security", "budget exhausted")],
        )
        assert "PHASE SKIPPED" in md
        assert "budget exhausted" in md
        assert "### Security (0 findings)" in md


# ---------------------------------------------------------------------------
# R1.2 - PR body shows non-execution
# ---------------------------------------------------------------------------


class TestPrBodyDidNotRun:
    def _component(self, findings: list[Finding]) -> tuple[Component, Manifest]:
        comp = Component(
            "comp-a",
            "Component A",
            "Desc",
            [],
            "scripts/kstrl/feature/comp-a/prd.json",
            "kstrl/comp-a",
        )
        comp.findings = findings
        manifest = Manifest(
            version="1",
            spec_file="s",
            project_name="t",
            base_branch="main",
            single_pr=False,
            components=[comp],
        )
        return comp, manifest

    def test_security_infra_error_is_visible(self) -> None:
        from kstrl.pr import _generate_pr_body

        comp, manifest = self._component(
            [
                Finding.infrastructure_error(
                    phase="security",
                    explanation="agent crashed",
                ),
            ]
        )
        body = _generate_pr_body(comp, manifest)
        assert "INFRASTRUCTURE ERROR" in body
        assert "### Security" in body
        assert "did not actually run" in body

    def test_skipped_phase_is_visible(self) -> None:
        from kstrl.pr import _generate_pr_body

        comp, manifest = self._component(
            [
                Finding.phase_skipped("review", "mode=skip"),
            ]
        )
        body = _generate_pr_body(comp, manifest)
        assert "PHASE SKIPPED" in body

    def test_real_findings_do_not_duplicate_into_status_section(self) -> None:
        from kstrl.pr import _generate_pr_body

        comp, manifest = self._component(
            [
                Finding.from_review_concern(
                    category="dead_code",
                    severity="advisory",
                    location="x.py:1",
                    explanation="unused helper",
                ),
            ]
        )
        body = _generate_pr_body(comp, manifest)
        assert "Adversarial Findings" not in body


# ---------------------------------------------------------------------------
# R1.2/R1.3 - factory integration: skips, crashes, diff errors
# ---------------------------------------------------------------------------


def _scaffold(tmp_path: Path, comp_ids: list[str]) -> Path:
    (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
    (tmp_path / "scripts" / "kstrl" / "prompt.md").write_text("p")
    (tmp_path / "scripts" / "kstrl" / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    for comp_id in comp_ids:
        feature_dir = tmp_path / "scripts" / "kstrl" / "feature" / comp_id
        feature_dir.mkdir(parents=True)
        _write_prd(feature_dir / "prd.json", ["US-001"])
    return tmp_path


def _make_manifest(ids: list[str]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="s",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id=i,
                title=i,
                description="",
                dependencies=[],
                prd_path=f"scripts/kstrl/feature/{i}/prd.json",
                branch_name=f"kstrl/{i}",
            )
            for i in ids
        ],
    )


def _base_config(root: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts/kstrl/prompt.md",
        prd_file=root / "scripts/kstrl/prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _factory_config(**overrides: object) -> FactoryConfig:
    defaults: dict[str, object] = dict(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)  # type: ignore[arg-type]


def _read_events(log_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@dataclass
class _BudgetRun:
    """What a `_run_with_budget` call produced, so the R10.5 tests can
    assert on the manifest, the run result, the call counts and the
    files the run wrote without unpacking a six-element tuple."""

    root: Path
    manifest: Manifest
    result: FactoryResult
    review_calls: int
    security_calls: int
    log_path: Path
    #: Everything the run printed. `PlainUI` takes the stream, so this
    #: is the run's own output rather than whatever else the process
    #: wrote, and the banner a halted phase prints is assertable.
    output: str

    def events(self) -> list[dict[str, object]]:
        """The progress-log rows, in order."""
        return _read_events(self.log_path)

    def component_events(self, comp_id: str) -> list[str]:
        """The event names recorded for one component."""
        return [str(e["event"]) for e in self.events() if e.get("component") == comp_id]

    def component(self, comp_id: str) -> Component:
        comp = self.manifest.get_component(comp_id)
        assert comp is not None, comp_id
        return comp

    def journal_entry(self, comp_id: str) -> dict[str, object]:
        """The evolution-journal `component_result` row for one
        component. `EvolutionConfig.enabled` defaults to True, so the
        factory writes this file for every run these fixtures make."""
        path = self.root / ".kstrl" / "evolution.jsonl"
        entries = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matches = [
            e
            for e in entries
            if e.get("event_type") == "component_result" and e.get("component_id") == comp_id
        ]
        assert len(matches) == 1, matches
        return matches[0]


def _run_with_budget(
    tmp_path: Path,
    comp_ids: list[str],
    **overrides: object,
) -> _BudgetRun:
    """Run the factory over `comp_ids` with the reviewer, the security
    reviewer and the engineer all stubbed to pass, so the only thing
    that can end a component is the adversarial-budget cap.

    The reviewer stubs are what make `max_adversarial_calls` the
    variable under test: every component would otherwise pass every
    gate, so a component that does not complete did so because the cap
    was spent (R10.5, #226)."""
    root = _scaffold(tmp_path, comp_ids)
    manifest = _make_manifest(comp_ids)
    log_path = tmp_path / "progress.jsonl"
    config = _factory_config(progress_log_path=log_path, **overrides)
    stream = io.StringIO()
    with (
        patch(
            "kstrl.factory._run_component",
            side_effect=lambda comp_id, *a, **k: ComponentResult(
                comp_id, success=True, iterations=1
            ),
        ),
        patch(
            "kstrl.factory.run_review",
            return_value=ReviewResult(passed=True, mode="hard"),
        ) as mock_review,
        patch(
            "kstrl.factory.run_security_review",
            return_value=SecurityResult(passed=True, mode="hard"),
        ) as mock_security,
        patch("kstrl.git.get_diff_content", return_value=""),
    ):
        result = run_factory(
            manifest,
            config,
            _base_config(root),
            PlainUI(no_color=True, file=stream),
            root,
        )
    return _BudgetRun(
        root=root,
        manifest=manifest,
        result=result,
        review_calls=mock_review.call_count,
        security_calls=mock_security.call_count,
        log_path=log_path,
        output=stream.getvalue(),
    )


class TestFactorySkipTraces:
    def test_mode_skip_emits_finding_and_journal_event(
        self,
        tmp_path: Path,
    ) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        log_path = tmp_path / "progress.jsonl"
        config = _factory_config(
            review_mode="skip",
            progress_log_path=log_path,
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _base_config(root),
                PlainUI(no_color=True),
                root,
            )
        assert "comp-a" in result.completed
        comp = manifest.get_component("comp-a")
        assert comp is not None
        skips = [f for f in comp.findings if f.is_phase_skip]
        skip_phases = {f.phase for f in skips}
        # review skipped by mode, security not configured
        assert "review" in skip_phases
        assert "security" in skip_phases
        events = _read_events(log_path)
        skip_events = [e for e in events if e["event"] == "phase_skipped"]
        assert {
            e["data"]["phase"]  # type: ignore[index]
            for e in skip_events
        } >= {"review", "security"}

    def test_budget_exhaustion_emits_finding_and_journal_event(
        self,
        tmp_path: Path,
    ) -> None:
        """The R1.2 skip trace for an exhausted adversarial budget.

        This test read `review_mode="hard"` until R10.5 (#226), which is
        the documented breaking change: hard mode no longer downgrades
        to a skip when the budget is spent, it halts the component
        (`test_hard_mode_budget_exhausted_halts_component` below covers
        that). Advisory mode still produces the skip trace this test
        exists for, so the mode moved and the assertions did not.
        """
        root = _scaffold(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest(["comp-a", "comp-b"])
        log_path = tmp_path / "progress.jsonl"
        config = _factory_config(
            review_mode="advisory",
            max_adversarial_calls=1,
            progress_log_path=log_path,
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        passing = ReviewResult(passed=True, mode="hard")
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch(
                "kstrl.factory.run_review",
                return_value=passing,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _base_config(root),
                PlainUI(no_color=True),
                root,
            )
        # comp-a consumed the budget; comp-b's review was budget-skipped
        comp_b = manifest.get_component("comp-b")
        assert comp_b is not None
        review_skips = [f for f in comp_b.findings if f.is_phase_skip and f.phase == "review"]
        assert len(review_skips) == 1
        assert "budget" in review_skips[0].explanation
        events = _read_events(log_path)
        assert any(
            e["event"] == "phase_skipped"
            and e.get("component") == "comp-b"
            and e["data"]["phase"] == "review"  # type: ignore[index]
            for e in events
        )

    # -----------------------------------------------------------------
    # R10.5 (#226): an exhausted adversarial budget halts a hard-mode
    # component instead of merging it on mechanical checks alone.
    # -----------------------------------------------------------------

    def test_hard_mode_budget_exhausted_halts_component(
        self,
        tmp_path: Path,
    ) -> None:
        """comp-a spends the one call; comp-b's hard-mode review has no
        budget left and refuses rather than downgrading to a skip."""
        run = _run_with_budget(
            tmp_path,
            ["comp-a", "comp-b"],
            review_mode="hard",
            max_adversarial_calls=1,
            # NOT the fixture default of 0. At max_retries=0 a
            # RETRY_OR_FAIL routes straight to fail() and leaves retries
            # at 0 too, so the assertion below would hold under either
            # action and prove nothing (review of #349, S2). With 1, only
            # FailureAction.FAIL keeps it at 0.
            max_retries=1,
        )
        assert run.review_calls == 1
        assert "comp-a" in run.result.completed
        comp_b = run.component("comp-b")
        assert "comp-b" not in run.result.completed
        assert comp_b.status == "failed"
        assert comp_b.failed_phase == "review"
        assert comp_b.failed_check == "adversarial_budget"
        infra = [f for f in comp_b.findings if f.is_infrastructure_error]
        assert [f.phase for f in infra] == ["review"]
        # FAIL, not RETRY_OR_FAIL: a retry cannot recover budget, so the
        # component must not burn engineer iterations against the same cap.
        assert comp_b.retries == 0
        # The sentence, not just the shape. docs/runbook.md publishes it
        # as the symptom an operator greps for, and the Finding and the
        # banner are built from one literal so both are pinned here.
        refusal = (
            "adversarial LLM budget (1) exhausted before the phase ran; "
            "hard mode refuses to merge unreviewed"
        )
        assert comp_b.error == f"Review infrastructure error: {refusal}"
        assert f"Phase 2 FAILED for comp-b: Review infrastructure error: {refusal}" in run.output
        assert infra[0].explanation == refusal
        # The reviewer did not run, so it did not reject: review_passed
        # is the rejection record and must not read as one. False is how
        # the halting path spells "did not pass" and None is how the
        # advisory path, which lets the component continue, spells it:
        # the difference is between the two paths, not a claim about
        # what any reviewer decided.
        assert comp_b.review_passed is False
        # Nothing was skipped, so nothing claims it was - in the findings
        # or in the progress log, where the base commit emitted a
        # phase_skipped for review here.
        assert not any(f.is_phase_skip and f.phase == "review" for f in comp_b.findings)
        assert "phase_skipped" not in run.component_events("comp-b")

    def test_advisory_mode_budget_exhausted_still_skips(
        self,
        tmp_path: Path,
    ) -> None:
        """In advisory mode the exhausted budget degrades to a recorded
        skip and, with ``setpoint_agreement`` at its default, the
        component completes.

        Both halves of that sentence are configuration, not a rule about
        advisory mode: under ``setpoint_agreement = "block"`` the R10.3
        gate in ``_review_did_not_run`` fails this same component, which
        ``tests/test_setpoint_agreement.py`` covers.
        """
        run = _run_with_budget(
            tmp_path,
            ["comp-a", "comp-b"],
            review_mode="advisory",
            max_adversarial_calls=1,
        )
        comp_b = run.component("comp-b")
        assert "comp-b" in run.result.completed
        assert comp_b.status == "completed"
        assert any(f.is_phase_skip and f.phase == "review" for f in comp_b.findings)
        assert not any(f.is_infrastructure_error for f in comp_b.findings)

    def test_security_hard_mode_budget_exhausted_halts(
        self,
        tmp_path: Path,
    ) -> None:
        """Phase 2.5 follows the same rule as Phase 2.

        The cap is 2, not the issue's 1: comp-a's advisory review takes
        the first call and comp-a's own security takes the second, so a
        cap of 1 would strand comp-a's security rather than comp-b's.
        With 2, comp-b sees an advisory review skip and then a hard
        security refusal.
        """
        run = _run_with_budget(
            tmp_path,
            ["comp-a", "comp-b"],
            review_mode="advisory",
            max_adversarial_calls=2,
            security_config=SecurityConfig(mode=SecurityMode.HARD.value),
        )
        assert run.review_calls == 1
        assert run.security_calls == 1
        assert "comp-a" in run.result.completed
        comp_b = run.component("comp-b")
        assert "comp-b" not in run.result.completed
        assert comp_b.status == "failed"
        assert comp_b.failed_phase == "security"
        assert comp_b.failed_check == "adversarial_budget"
        infra = [f for f in comp_b.findings if f.is_infrastructure_error]
        assert [f.phase for f in infra] == ["security"]
        # Same sentence, the other role and banner. Both come from one
        # literal in _budget_refusal, and docs/runbook.md publishes both.
        refusal = (
            "adversarial LLM budget (2) exhausted before the phase ran; "
            "hard mode refuses to merge unreviewed"
        )
        assert comp_b.error == f"Security review infrastructure error: {refusal}"
        assert (
            f"Phase 2.5 FAILED for comp-b: Security review infrastructure error: {refusal}"
            in run.output
        )
        # The advisory review before it still records its skip.
        assert any(f.is_phase_skip and f.phase == "review" for f in comp_b.findings)
        entry = run.journal_entry("comp-b")
        assert entry["failed_check"] == "adversarial_budget"
        assert "adversarial_budget:security" in entry["failure_signatures"]  # type: ignore[operator]

    def test_unbounded_default_unchanged(
        self,
        tmp_path: Path,
    ) -> None:
        """`max_adversarial_calls = 0` is the default and means
        unbounded, so the refusal is unreachable for a default config."""
        run = _run_with_budget(
            tmp_path,
            ["comp-a", "comp-b", "comp-c"],
            review_mode="hard",
            max_adversarial_calls=0,
        )
        assert run.review_calls == 3
        assert set(run.result.completed) == {"comp-a", "comp-b", "comp-c"}
        for comp in run.manifest.components:
            assert comp.status == "completed"
            assert not any(f.is_infrastructure_error for f in comp.findings)

    def test_budget_exhausted_signature_reaches_journal(
        self,
        tmp_path: Path,
    ) -> None:
        """The halt is legible to the evolution journal, which is what a
        later run reads to see which sensor stopped the component."""
        run = _run_with_budget(
            tmp_path,
            ["comp-a", "comp-b"],
            review_mode="hard",
            max_adversarial_calls=1,
        )
        entry = run.journal_entry("comp-b")
        assert entry["failed_check"] == "adversarial_budget"
        # The signature leads with the CHECK, not with the phase. #226
        # specified "review:budget-exhausted"; that spelling made
        # autonomy_replay count a run whose reviewer never ran as a
        # verdict about the factory's judgement, because its prefix
        # taxonomy reads everything before the first colon as the check
        # name and "review" is not an infrastructure check. See
        # tests/test_infrastructure_category_consumers.py for the two
        # consumers agreeing on this name.
        assert "adversarial_budget:review" in entry["failure_signatures"]  # type: ignore[operator]

    def test_advisory_security_budget_exhausted_still_skips(
        self,
        tmp_path: Path,
    ) -> None:
        """Phase 2.5's advisory side, which #226 moved but did not change.

        The mode split inside the budget branch is the design point of
        this change, and this is the half that must NOT halt: an
        advisory security reviewer with no budget left records a
        `phase_skipped` and the component completes. Without this,
        replacing the mode check with `if True` leaves the suite green.
        """
        run = _run_with_budget(
            tmp_path,
            ["comp-a", "comp-b"],
            review_mode="advisory",
            max_adversarial_calls=2,
            security_config=SecurityConfig(mode=SecurityMode.ADVISORY.value),
        )
        # comp-a spent both calls (review, then security).
        assert run.review_calls == 1
        assert run.security_calls == 1
        comp_b = run.component("comp-b")
        assert "comp-b" in run.result.completed
        assert comp_b.status == "completed"
        assert comp_b.failed_check == ""
        skips = {f.phase for f in comp_b.findings if f.is_phase_skip}
        assert {"review", "security"} <= skips
        assert not any(f.is_infrastructure_error for f in comp_b.findings)
        assert "Phase 2.5 SKIPPED for comp-b" in run.output

    def test_budget_halt_is_terminal_for_serve(
        self,
        tmp_path: Path,
    ) -> None:
        """The halt reaches `ks serve` as terminal, not as a retry.

        Every finding on a budget-halted component is an
        `infrastructure_error`, which is the shape `classify_run` reads
        as retryable infrastructure. Measured on the review of #349: the
        real classifier returned RETRY_INFRA with `may_retry` True on
        the manifest a real halted run writes, so `ks serve` re-ran the
        whole factory against a cap that starts again at zero and stops
        at the same component, paying an engineer loop per component
        each time. This test reads the manifest the factory actually
        wrote rather than a hand-built one, because the hand-built
        manifests are what missed it.
        """
        run = _run_with_budget(
            tmp_path,
            ["comp-a", "comp-b"],
            review_mode="hard",
            max_adversarial_calls=1,
        )
        assert run.component("comp-b").failed_check == "adversarial_budget"
        manifest_path = run.root / "scripts" / "kstrl" / "manifest.json"
        assert manifest_path.exists(), "the factory writes its manifest here"
        outcome = classify_run(
            run.root,
            run=RunOutcome(returncode=1),
            manifest_path=manifest_path,
        )
        assert outcome.verdict is Verdict.BUDGET_HALT
        assert outcome.verdict.may_retry is False
        assert "comp-b" in outcome.reason
        assert outcome.evidence["budget_halted"] == ["comp-b"]

    def test_three_adversarial_calls_per_component(
        self,
        tmp_path: Path,
    ) -> None:
        """The number five doc sites quote, counted rather than restated.

        `kstrl.toml.example`, `kstrl/init_cmd.py`, `scripts/gen_docs.py`
        (and the README it generates), `docs/runbook.md` and the
        CHANGELOG all tell an operator to budget 3 calls per component.
        That is a property of the call sites of
        `adversarial_budget_consume`, so this counts them: if the
        distiller stops spending from this cap, or a fourth phase starts
        spending from it, all five prose sites go wrong at once and this
        goes red instead.
        """
        seen: list[str] = []
        real = ComponentPipeline.adversarial_budget_consume

        def spy(self: ComponentPipeline) -> None:
            # The phase attribution comes free from the calling frame,
            # so the spy needs no per-phase wiring to keep in step.
            seen.append(traceback.extract_stack(limit=2)[0].name)
            real(self)

        with patch.object(ComponentPipeline, "adversarial_budget_consume", spy):
            _run_with_budget(
                tmp_path,
                ["comp-a", "comp-b"],
                review_mode="hard",
                max_adversarial_calls=0,
                security_config=SecurityConfig(mode=SecurityMode.HARD.value),
            )

        assert seen == ["_phase_review", "_phase_security", "_phase_distill"] * 2

    def test_single_pr_knowledge_skip_leaves_trace(
        self,
        tmp_path: Path,
    ) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        manifest.single_pr = True
        config = _factory_config(review_mode="skip")
        success = ComponentResult("comp-a", success=True, iterations=1)
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                config,
                _base_config(root),
                PlainUI(no_color=True),
                root,
            )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert any(f.is_phase_skip and f.phase == "knowledge" for f in comp.findings)


class TestFactoryReviewerCrash:
    def test_hard_mode_crash_fails_one_component_not_the_run(
        self,
        tmp_path: Path,
    ) -> None:
        """comp-a's reviewer crashes; comp-b (independent) must still
        complete and run_factory must return, not raise."""
        root = _scaffold(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest(["comp-a", "comp-b"])
        config = _factory_config(review_mode="hard")

        def fake_run_review(*args: object, **kwargs: object) -> ReviewResult:
            if "comp-a" in str(args[1]):
                raise RuntimeError("reviewer exploded")
            return ReviewResult(passed=True, mode="hard")

        def fake_run_component(comp_id: str, *a: object, **k: object) -> ComponentResult:
            return ComponentResult(comp_id, success=True, iterations=1)

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_run_component,
            ),
            patch(
                "kstrl.factory.run_review",
                side_effect=fake_run_review,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _base_config(root),
                PlainUI(no_color=True),
                root,
            )
        assert "comp-a" in result.failed
        assert "comp-b" in result.completed
        assert result.exit_code == 1
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert any(f.is_infrastructure_error and f.phase == "review" for f in comp_a.findings)

    def test_advisory_mode_crash_completes_with_infra_finding(
        self,
        tmp_path: Path,
    ) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        config = _factory_config(review_mode="advisory")
        success = ComponentResult("comp-a", success=True, iterations=1)
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch(
                "kstrl.factory.run_review",
                side_effect=RuntimeError("reviewer exploded"),
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _base_config(root),
                PlainUI(no_color=True),
                root,
            )
        assert "comp-a" in result.completed
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert any(f.is_infrastructure_error and f.phase == "review" for f in comp.findings)

    def test_advisory_security_crash_leaves_infra_finding(
        self,
        tmp_path: Path,
    ) -> None:
        """The sec-pr-body hole: an advisory-mode security crash used to
        vanish entirely (no finding, no PR-body section)."""
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        config = _factory_config(
            review_mode="skip",
            security_config=SecurityConfig(
                mode=SecurityMode.ADVISORY.value,
            ),
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch(
                "kstrl.factory.run_security_review",
                side_effect=RuntimeError("security agent exploded"),
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                _base_config(root),
                PlainUI(no_color=True),
                root,
            )
        assert "comp-a" in result.completed
        comp = manifest.get_component("comp-a")
        assert comp is not None
        infra = [f for f in comp.findings if f.is_infrastructure_error and f.phase == "security"]
        assert len(infra) == 1
        # and the PR body renders the did-not-run callout from it
        from kstrl.pr import _generate_pr_body

        body = _generate_pr_body(comp, manifest)
        assert "INFRASTRUCTURE ERROR" in body


class TestR13DiffErrors:
    def test_get_diff_content_raises_outside_repo(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(GitDiffError):
            get_diff_content("main", tmp_path)

    def test_get_diff_content_empty_diff_is_not_an_error(
        self,
        tmp_path: Path,
    ) -> None:
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "--allow-empty",
                "-m",
                "init",
            ],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        assert get_diff_content("main", tmp_path) == ""

    def test_factory_maps_diff_error_to_infrastructure_failure(
        self,
        tmp_path: Path,
    ) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        log_path = tmp_path / "progress.jsonl"
        config = _factory_config(
            review_mode="hard",
            progress_log_path=log_path,
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch(
                "kstrl.git.get_diff_content",
                side_effect=GitDiffError("git diff exited 129"),
            ),
            patch("kstrl.factory.run_review") as mock_review,
        ):
            result = run_factory(
                manifest,
                config,
                _base_config(root),
                PlainUI(no_color=True),
                root,
            )
        # No phase consumed the empty string: review never ran
        mock_review.assert_not_called()
        assert "comp-a" in result.failed
        assert result.exit_code == 1
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert "Diff fetch failed" in comp.error
        assert any(f.is_infrastructure_error and f.phase == "diff" for f in comp.findings)
        events = _read_events(log_path)
        assert any(e["event"] == "diff_fetch_failed" for e in events)
