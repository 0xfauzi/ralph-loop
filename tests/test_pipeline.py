"""R7.3: ComponentPipeline state-machine tests, in isolation.

Drives the pipeline directly - stub hooks, no subprocesses, no LLM
calls, no scheduler - and asserts every transition the roadmap names:
retry, retry exhaustion, cascade-skip, MERGE_PENDING (park and re-poll),
HITL checkpoint reject/retry, and both budget-exhaustion walls
(adversarial call cap and token cap). The review's critical bugs lived
exactly in these transitions while they were closures inside
run_factory; these tests are the regression net that extraction bought.
"""

from __future__ import annotations

import dataclasses
import io
from pathlib import Path
from typing import Any

import pytest

from kstrl import events as ev
from kstrl import git
from kstrl.agents.base import UsageRecord, UsageTotals
from kstrl.config import KstrlConfig
from kstrl.context import IterationContext
from kstrl.events import CallbackSink, Event, EventBus, PhaseCompleted, V1CompatSink
from kstrl.evolution import category_for_check
from kstrl.factory import (
    AdversarialAgentSelection,
    ComponentResult,
    FactoryConfig,
    FactoryResult,
)
from kstrl.findings import Finding
from kstrl.fixtures import FixturesConfig
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.knowledge import Fact, KnowledgeConfig, measure_fact_utilization
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.observability import NotifyConfig, NotifyHooks, ProgressLog
from kstrl.pipeline import (
    CheckpointDecision,
    ComponentPipeline,
    FactUtilization,
    PipelineHooks,
    PrDisposition,
    Transition,
)
from kstrl.pr import PrOutcome
from kstrl.review import ReviewConcern, ReviewResult
from kstrl.scope import RunScope
from kstrl.security import SecurityConfig, SecurityResult
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig
from tests.test_context import CURRENT, NOT_REMEASURED, RESOLVED, section


class _ChoiceUI(PlainUI):
    """Interactive-capable UI with a scripted HITL checkpoint answer."""

    def __init__(self, choice: int) -> None:
        super().__init__(no_color=True, file=io.StringIO())
        self._choice = choice

    def can_prompt(self) -> bool:
        return True

    def choose(
        self,
        header: str,
        options: list[str],
        default: int = 0,
    ) -> int:
        return self._choice


def _component(comp_id: str, deps: list[str] | None = None) -> Component:
    return Component(
        comp_id,
        comp_id.title(),
        "Desc",
        deps or [],
        f"scripts/kstrl/feature/{comp_id}/prd.json",
        f"kstrl/factory/{comp_id}",
    )


def _make_manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=components,
    )


def _base_config(root: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _factory_config(**overrides: Any) -> FactoryConfig:
    defaults: dict[str, Any] = dict(
        max_parallel=1,
        max_retries=1,
        retry_delay=0,
        create_prs=False,
        use_worktrees=False,
        review_mode="skip",
        verify_config=VerifyConfig(),
        fixtures_config=FixturesConfig(),
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)


def _selection(phase: str) -> AdversarialAgentSelection:
    return AdversarialAgentSelection(
        phase=phase,
        agent_cmd=None,
        agent_type=None,
        model=None,
        reasoning=None,
        source="explicit",
        identity=f"test-{phase}",
    )


def _recording_hooks(
    calls: list[str],
    **overrides: Any,
) -> PipelineHooks:
    """Hooks whose stubs append their name to ``calls`` when invoked."""

    def _rec(name: str, ret: Any) -> Any:
        def _f(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return ret

        return _f

    defaults: dict[str, Any] = dict(
        run_mechanical_verification=_rec(
            "verify",
            VerificationResult(passed=True, checks=[]),
        ),
        run_review=_rec("review", ReviewResult(passed=True, mode="advisory")),
        run_security_review=_rec(
            "security",
            SecurityResult(passed=True, mode="advisory"),
        ),
        distill_facts=_rec("distill", (1, "1 fact written")),
        measure_fact_utilization=_rec(
            "utilization",
            {"injected": 0, "referenced": 0},
        ),
        cleanup_worktree=_rec("cleanup_worktree", None),
    )
    defaults.update(overrides)
    return PipelineHooks(**defaults)


def _make_pipeline(
    tmp_path: Path,
    *,
    components: list[Component] | None = None,
    config: FactoryConfig | None = None,
    ui: PlainUI | None = None,
    knowledge: KnowledgeConfig | None = None,
    security_selection: AdversarialAgentSelection | None = None,
    calls: list[str] | None = None,
    hooks_overrides: dict[str, Any] | None = None,
) -> tuple[ComponentPipeline, Manifest, FactoryResult, list[str]]:
    comps = (
        components
        if components is not None
        else [
            _component("comp-a"),
            _component("comp-b", deps=["comp-a"]),
        ]
    )
    manifest = _make_manifest(comps)
    factory_config = config or _factory_config()
    factory_result = FactoryResult()
    call_log = calls if calls is not None else []
    ui = ui or PlainUI(no_color=True, file=io.StringIO())
    pipeline = ComponentPipeline(
        manifest=manifest,
        manifest_path=tmp_path / "manifest.json",
        factory_config=factory_config,
        base_config=_base_config(tmp_path),
        ui=ui,
        root_dir=tmp_path,
        run_id="run-test",
        bus=EventBus(
            V1CompatSink(ProgressLog(tmp_path / "progress.jsonl", run_id="run-test")),
            run_id="run-test",
        ),
        journal_path=tmp_path / "progress.jsonl",
        notify=NotifyHooks(
            NotifyConfig(),
            run_id="run-test",
            project="test",
            warn=ui.warn,
        ),
        review_selection=_selection("review"),
        security_selection=security_selection,
        knowledge_config=knowledge or KnowledgeConfig(enabled=False),
        factory_result=factory_result,
        # #269: the plan-time snapshot the factory resolves before the
        # first engineer call, built here the same way run_factory
        # builds it so the pipeline is judged against a real one.
        run_scope=RunScope.resolve(manifest, tmp_path, _base_config(tmp_path)),
        hooks=_recording_hooks(call_log, **(hooks_overrides or {})),
        worktree_paths={},
        component_contexts={},
        fresh_base_retry_ids=set(),
        component_failure_signatures={},
    )
    return pipeline, manifest, factory_result, call_log


def _raise(exc: Exception) -> Any:
    """A hook stub that raises, for the non-fatal error paths."""

    def _f(*args: Any, **kwargs: Any) -> Any:
        raise exc

    return _f


def _success(comp_id: str, usage: UsageTotals | None = None) -> ComponentResult:
    return ComponentResult(
        comp_id,
        success=True,
        iterations=2,
        duration_seconds=1.0,
        usage=usage,
    )


def _usage(total: int) -> UsageTotals:
    totals = UsageTotals()
    totals.add_record(
        UsageRecord(
            input_tokens=total // 2,
            output_tokens=total - total // 2,
            total_tokens=total,
            duration_seconds=1.0,
            source="claude-stream-json",
        )
    )
    return totals


def _events(tmp_path: Path) -> list[dict[str, Any]]:
    return ProgressLog(tmp_path / "progress.jsonl").read_events()


@pytest.fixture(autouse=True)
def _no_real_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pipeline is exercised without git: the shared-diff fetch and
    the agent factory are stubbed at their source modules (the same
    seams the factory-level tests use)."""
    monkeypatch.setattr(
        "kstrl.git.get_diff_content",
        lambda *a, **k: "diff --git a b\n",
    )
    monkeypatch.setattr(
        "kstrl.agents.get_agent",
        lambda *a, **k: object(),
    )


class TestEngineerTransitions:
    def test_unknown_component_returns_none(self, tmp_path: Path) -> None:
        pipeline, _, _, _ = _make_pipeline(tmp_path)
        assert pipeline.process_result("ghost", _success("ghost")) is None

    def test_engineer_failure_retries_with_context(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, manifest, result, _ = _make_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result(
            "comp-a",
            ComponentResult(
                "comp-a",
                success=False,
                iterations=3,
                error="Did not complete",
            ),
        )
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.status == ComponentStatus.PENDING.value
        assert comp.retries == 1
        assert comp.failed_phase == "engineer"
        assert comp.failed_check == "loop"
        assert "Did not complete" in pipeline.component_contexts["comp-a"]
        assert result.failed == []

    def test_retry_exhaustion_fails_and_cascade_skips(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(max_retries=1),
        )
        comp = manifest.get_component("comp-a")
        dep = manifest.get_component("comp-b")
        assert comp is not None and dep is not None
        pipeline.begin_attempt(comp)
        first = pipeline.process_result(
            "comp-a",
            ComponentResult(
                "comp-a",
                success=False,
                error="boom",
            ),
        )
        assert first is not None and first.transition == Transition.RETRYING
        pipeline.begin_attempt(comp)
        second = pipeline.process_result(
            "comp-a",
            ComponentResult(
                "comp-a",
                success=False,
                error="boom again",
            ),
        )
        assert second is not None and second.transition == Transition.FAILED
        assert comp.status == ComponentStatus.FAILED.value
        assert dep.status == ComponentStatus.SKIPPED.value
        assert result.failed == ["comp-a"]
        assert result.skipped == ["comp-b"]

    def test_timeout_failure_marks_worktree_hygiene(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(use_worktrees=True),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result(
            "comp-a",
            ComponentResult(
                "comp-a",
                success=False,
                error="component timeout: exceeded 5.0s wall clock",
            ),
        )
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert "comp-a" in pipeline.fresh_base_retry_ids
        assert "worktree recreated from base" in comp.error

    def test_token_budget_at_engineer_checkpoint_fails(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(max_total_tokens=100),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result(
            "comp-a",
            _success("comp-a", usage=_usage(150)),
        )
        assert outcome is not None
        assert outcome.transition == Transition.FAILED
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "token_budget"
        # Loud, typed, journaled: the synthetic finding and the event.
        assert any(f.is_infrastructure_error for f in comp.findings)
        assert any(e["event"] == "budget_exceeded" for e in _events(tmp_path))
        assert result.failed == ["comp-a"]

    def test_scheduling_gate_budget_failure(self, tmp_path: Path) -> None:
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(max_total_tokens=10),
        )
        pipeline.run_usage.merge(_usage(50))
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert pipeline.token_budget_exceeded()
        assert pipeline.fail_for_budget(comp, "scheduling") == Transition.FAILED
        assert comp.failed_phase == "scheduling"
        assert result.failed == ["comp-a"]


class TestVerifyAndDiffTransitions:
    def test_success_path_completes(self, tmp_path: Path) -> None:
        pipeline, manifest, result, calls = _make_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.verify is not None and outcome.verify.ran
        assert outcome.review is not None and not outcome.review.ran
        assert outcome.checkpoint == CheckpointDecision.NOT_PROMPTED
        assert outcome.pr is not None
        assert outcome.pr.disposition == PrDisposition.SKIPPED
        assert comp.status == ComponentStatus.COMPLETED.value
        assert result.completed == ["comp-a"]
        assert calls == ["verify"]

    def test_verify_failure_retries(self, tmp_path: Path) -> None:
        failing = VerificationResult(
            passed=False,
            checks=[
                CheckResult(name="tests", passed=False, message="1 failed"),
            ],
        )
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            hooks_overrides={
                "run_mechanical_verification": lambda *a, **k: failing,
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.failed_phase == "verify"
        assert comp.failed_check == "tests"
        assert comp.verification_passed is False
        assert "tests" in pipeline.component_contexts["comp-a"]

    def test_skip_verification_records_skip_and_completes(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            config=_factory_config(skip_verification=True),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.verify is not None and not outcome.verify.ran
        assert comp.verification_passed is None
        assert "verify" not in calls
        assert any(f.is_phase_skip for f in comp.findings)

    def test_diff_fetch_failure_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kstrl.git import GitDiffError

        def _boom(*args: Any, **kwargs: Any) -> str:
            raise GitDiffError("git diff exploded")

        monkeypatch.setattr("kstrl.git.get_diff_content", _boom)
        pipeline, manifest, _, _ = _make_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.failed_phase == "diff"
        assert comp.failed_check == "git_diff"


class TestReviewerSandboxPlumbing:
    """#295 finding 3. ``read_only=True`` is a permission-layer posture
    on the claude adapters; the operator's ``[sandbox]`` intent is a
    separate OS-level payload. Both reviewer roles were built with the
    first and NOT the second, so an operator who turned sandboxing on
    got it for the engineer and silently not for the two roles that now
    run shell commands inside the tree under review."""

    def _captured_kwargs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[dict[str, Any]]:
        (tmp_path / "kstrl.toml").write_text(
            "[sandbox]\nenabled = true\nallow_network = false\n",
            encoding="utf-8",
        )
        seen: list[dict[str, Any]] = []

        def _fake_get_agent(*args: Any, **kwargs: Any) -> Any:
            # The returned object is never driven: the pipeline hands it
            # straight to hooks.run_review, which _recording_hooks stubs.
            seen.append(kwargs)
            return object()

        monkeypatch.setattr("kstrl.agents.get_agent", _fake_get_agent)
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="advisory",
                security_config=SecurityConfig(mode="advisory"),
            ),
            security_selection=_selection("security"),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        pipeline.process_result("comp-a", _success("comp-a"))
        return seen

    def test_both_reviewers_receive_the_operator_sandbox(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = self._captured_kwargs(tmp_path, monkeypatch)
        assert len(seen) == 2, "expected a review agent and a security agent"
        for kwargs in seen:
            assert kwargs.get("read_only") is True
            sandbox = kwargs.get("sandbox")
            assert sandbox is not None, "the operator's sandbox intent never arrived"
            assert sandbox.enabled is True
            assert sandbox.allow_network is False


class TestReviewAndSecurityTransitions:
    def test_review_failure_retries(self, tmp_path: Path) -> None:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="hard"),
            hooks_overrides={
                "run_review": lambda *a, **k: ReviewResult(
                    passed=False,
                    mode="hard",
                ),
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.failed_phase == "review"
        assert comp.failed_check == "criteria"
        assert comp.review_passed is False

    def test_review_budget_exhausted_skips_but_completes(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="advisory",
                max_adversarial_calls=1,
            ),
        )
        pipeline.adversarial_budget_consume()
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.review is not None and not outcome.review.ran
        assert comp.review_passed is None
        assert "review" not in calls
        assert any(f.is_phase_skip for f in comp.findings)

    def test_unverified_review_coverage_fails_without_re_running_engineer(
        self,
        tmp_path: Path,
    ) -> None:
        """#295 finding 1. A reviewer that cannot show it read the change
        is a HARNESS fault, and the trigger is deterministic: the same
        reviewer omits or miscounts ``observedDiffstat`` on every
        attempt. Routed as RETRY_OR_FAIL it burned the whole retry budget
        on engineer runs that could not change the input - the #265
        economics this issue exists to remove.

        The assertion that matters is ``comp.retries == 0``: retries
        REMAIN available and are deliberately not spent, so this cannot
        pass by accident on an exhausted budget."""
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="hard", max_retries=3),
            hooks_overrides={
                "run_review": lambda *a, **k: ReviewResult(
                    passed=False,
                    mode="hard",
                    infrastructure_error=True,
                    diffstat_disagreement=(
                        "the reviewer reported no diffstat, so nothing says it read the change"
                    ),
                ),
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.FAILED
        assert comp.retries == 0
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_phase == "review"
        assert comp.failed_check == "coverage"
        assert result.failed == ["comp-a"]
        # No retry context was built, so nothing is queued for a rerun.
        assert "comp-a" not in pipeline.component_contexts

    def test_unverified_security_coverage_fails_without_re_running_engineer(
        self,
        tmp_path: Path,
    ) -> None:
        """#295 finding 1, Phase 2.5's half."""
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="skip",
                security_config=SecurityConfig(mode="hard"),
                max_retries=3,
            ),
            security_selection=_selection("security"),
            hooks_overrides={
                "run_security_review": lambda *a, **k: SecurityResult(
                    passed=False,
                    mode="hard",
                    infrastructure_error=True,
                    diffstat_disagreement="git reports 4 files, +900/-12",
                ),
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.FAILED
        assert comp.retries == 0
        assert comp.failed_phase == "security"
        assert comp.failed_check == "coverage"
        assert result.failed == ["comp-a"]
        assert "comp-a" not in pipeline.component_contexts

    @pytest.mark.parametrize("phase", ["review", "security"])
    def test_advisory_mode_is_never_failed_by_the_coverage_wall(
        self,
        tmp_path: Path,
        phase: str,
    ) -> None:
        """``apply_coverage_check`` records ``diffstat_disagreement`` in
        EVERY mode - that is how an advisory pass stays visibly
        unverified - and only refuses in hard mode. The finding-1 wall
        must therefore key on the REFUSAL, not on the disagreement
        alone, or advisory mode starts blocking, which is the one thing
        it promises never to do.

        Caught while writing the fix: Phase 2's wall lives inside
        ``_review_failure`` and a passing review never reaches it, but
        Phase 2.5's sat in the open and did fire."""
        unverified = "the reviewer reported no diffstat"
        overrides: dict[str, Any] = (
            {
                "run_review": lambda *a, **k: ReviewResult(
                    passed=True,
                    mode="advisory",
                    diffstat_disagreement=unverified,
                ),
            }
            if phase == "review"
            else {
                "run_security_review": lambda *a, **k: SecurityResult(
                    passed=True,
                    mode="advisory",
                    diffstat_disagreement=unverified,
                ),
            }
        )
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="advisory",
                security_config=SecurityConfig(mode="advisory"),
            ),
            security_selection=_selection("security"),
            hooks_overrides=overrides,
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert comp.status != ComponentStatus.FAILED.value
        assert result.failed == []

    def test_an_ordinary_review_failure_still_retries(self, tmp_path: Path) -> None:
        """The finding-1 wall must be specific to unverified coverage.
        A reviewer that DID prove it read the change and found real
        problems is exactly the case retrying exists for."""
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="hard", max_retries=3),
            hooks_overrides={
                "run_review": lambda *a, **k: ReviewResult(
                    passed=False,
                    mode="hard",
                ),
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.failed_check == "criteria"

    def test_security_failure_retries(self, tmp_path: Path) -> None:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="skip",
                security_config=SecurityConfig(mode="hard"),
            ),
            security_selection=_selection("security"),
            hooks_overrides={
                "run_security_review": lambda *a, **k: SecurityResult(
                    passed=False,
                    mode="hard",
                ),
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.failed_phase == "security"
        assert comp.failed_check == "findings"

    def test_review_crash_in_advisory_mode_degrades_and_completes(
        self,
        tmp_path: Path,
    ) -> None:
        def _crash(*args: Any, **kwargs: Any) -> ReviewResult:
            raise RuntimeError("reviewer exploded")

        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="advisory"),
            hooks_overrides={"run_review": _crash},
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        # Advisory mode: the crash degrades to an infra-annotated pass,
        # but the infra marker survives in the typed result.
        assert outcome.transition == Transition.COMPLETED
        assert outcome.review is not None
        assert outcome.review.result is not None
        assert outcome.review.result.infrastructure_error


class TestDivergenceDetector:
    """#265: the retry loop must stop paying for attempts that are
    growing the change away from a passing review."""

    @staticmethod
    def _review(fails: list[str]) -> ReviewResult:
        return ReviewResult(
            passed=False,
            mode="hard",
            concerns=[
                ReviewConcern("test_quality", "fail", "tests/a.py:12", f"{name} is weak")
                for name in fails
            ],
        )

    def _run_attempts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        reviews: list[ReviewResult],
        sizes: list[int],
        numstat_error: bool = False,
        mode: str = "block",
    ) -> tuple[Any, Component, list[Transition], list[Event]]:
        """Drive one component through N attempts, one review and one
        change size per attempt, and return the transitions plus every
        event the bus saw (the v2-only ones never reach progress.jsonl)."""
        attempt = {"n": 0}

        def _numstat(*args: Any, **kwargs: Any) -> list[tuple[int | None, int | None, str]]:
            if numstat_error:
                raise git.GitDiffError("no such ref")
            lines = sizes[attempt["n"] - 1]
            # The lockfile row must never reach the count: policy's size
            # caps exclude it, and the detector counts through the same
            # helper so a dependency bump cannot supply the growth.
            return [(lines, 0, "tests/a.py"), (0, 0, "src/b.py"), (9999, 0, "uv.lock")]

        def _run_review(*args: Any, **kwargs: Any) -> ReviewResult:
            attempt["n"] += 1
            return reviews[attempt["n"] - 1]

        monkeypatch.setattr("kstrl.git.get_diff_numstat", _numstat)
        (tmp_path / "kstrl.toml").write_text(f'[divergence]\nmode = "{mode}"\n', encoding="utf-8")
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="hard", max_retries=5),
            hooks_overrides={"run_review": _run_review},
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        seen: list[Event] = []
        pipeline.bus.add_sink(CallbackSink(seen.append))
        transitions: list[Transition] = []
        for _ in reviews:
            pipeline.begin_attempt(comp)
            outcome = pipeline.process_result("comp-a", _success("comp-a"))
            assert outcome is not None
            transitions.append(outcome.transition)
            if outcome.transition != Transition.RETRYING:
                break
        return pipeline, comp, transitions, seen

    def test_diverging_component_fails_without_burning_another_attempt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pipeline, comp, transitions, events = self._run_attempts(
            tmp_path,
            monkeypatch,
            reviews=[
                self._review(["a", "b"]),
                self._review(["a", "b", "c"]),
                self._review(["a", "b", "c", "d"]),
            ],
            sizes=[600, 1400, 2900],
        )
        assert transitions == [
            Transition.RETRYING,
            Transition.RETRYING,
            Transition.FAILED,
        ]
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "divergence"
        assert comp.failed_phase == "review"
        # Retries remained (max_retries=5), and the detector refused to
        # spend one: that saving is the whole point of the change.
        assert comp.retries == 2
        finding = next(f for f in comp.findings if f.category == "review_divergence")
        assert "2900" in finding.explanation
        event = next(e for e in events if isinstance(e, ev.ReviewDivergence))
        assert event.attempts == (1, 2, 3)
        assert event.lines_changed == (600, 1400, 2900)
        # The reviewer's own fail_count per attempt, not the key count.
        assert event.blocking_findings == (2, 3, 4)
        assert event.blocked is True
        assert pipeline.review_readings["comp-a"][0].files_changed == 2

    def test_converging_component_keeps_its_retries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """It grows just as fast, and it never retires more than one
        finding at a time while the reviewer keeps drawing a new one.
        That is the ordinary shape of answering a review, and the
        strict-subset reset this predicate does NOT use would have
        condemned it."""
        _, comp, transitions, _events_seen = self._run_attempts(
            tmp_path,
            monkeypatch,
            reviews=[
                self._review(["a", "b"]),
                self._review(["b", "c"]),
                self._review(["c", "d"]),
            ],
            sizes=[600, 1400, 2900],
        )
        assert transitions == [Transition.RETRYING] * 3
        assert comp.status == ComponentStatus.PENDING.value
        assert comp.failed_check == "criteria"
        assert comp.retries == 3

    def test_first_attempt_has_nothing_to_compare_against(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pipeline, comp, transitions, _events_seen = self._run_attempts(
            tmp_path,
            monkeypatch,
            reviews=[self._review(["a"])],
            sizes=[600],
        )
        assert transitions == [Transition.RETRYING]
        assert len(pipeline.review_readings["comp-a"]) == 1
        assert not any(f.category == "review_divergence" for f in comp.findings)

    def test_unmeasurable_change_records_no_reading(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The detector fails open on its own infrastructure: a git
        failure costs the component nothing."""
        pipeline, _, transitions, _events_seen = self._run_attempts(
            tmp_path,
            monkeypatch,
            reviews=[self._review(["a", "b"])] * 3,
            sizes=[600, 1400, 2900],
            numstat_error=True,
        )
        assert transitions == [Transition.RETRYING] * 3
        assert pipeline.review_readings.get("comp-a", []) == []

    def test_crashed_reviewer_records_no_reading(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An infrastructure error is not a verdict, so it cannot be one
        of the steps that condemn a component."""
        crashed = ReviewResult(passed=False, mode="hard", infrastructure_error=True)
        pipeline, _, transitions, _events_seen = self._run_attempts(
            tmp_path,
            monkeypatch,
            reviews=[self._review(["a"]), crashed, self._review(["a", "b"])],
            sizes=[600, 1400, 2900],
        )
        assert transitions == [Transition.RETRYING] * 3
        assert [r.attempt for r in pipeline.review_readings["comp-a"]] == [1, 3]

    def test_advisory_mode_records_the_trip_and_keeps_retrying(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The shipped default. The same trajectory that fails the
        component under mode = "block" is recorded here and costs it
        nothing, which is what makes graduation a decision with
        evidence."""
        _, comp, transitions, events = self._run_attempts(
            tmp_path,
            monkeypatch,
            reviews=[
                self._review(["a", "b"]),
                self._review(["a", "b", "c"]),
                self._review(["a", "b", "c", "d"]),
            ],
            sizes=[600, 1400, 2900],
            mode="advisory",
        )
        assert transitions == [Transition.RETRYING] * 3
        assert comp.failed_check == "criteria"
        finding = next(f for f in comp.findings if f.category == "review_divergence")
        assert finding.severity == "advisory"
        event = next(e for e in events if isinstance(e, ev.ReviewDivergence))
        assert event.blocked is False

    def test_skip_mode_takes_no_readings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pipeline, comp, transitions, _events_seen = self._run_attempts(
            tmp_path,
            monkeypatch,
            reviews=[
                self._review(["a", "b"]),
                self._review(["a", "b", "c"]),
                self._review(["a", "b", "c", "d"]),
            ],
            sizes=[600, 1400, 2900],
            mode="skip",
        )
        assert transitions == [Transition.RETRYING] * 3
        assert pipeline.review_readings == {}
        assert not any(f.category == "review_divergence" for f in comp.findings)


class TestCheckpointAndPrTransitions:
    def _pr_config(self, **overrides: Any) -> FactoryConfig:
        return _factory_config(create_prs=True, **overrides)

    def test_checkpoint_reject_fails_component(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(pause_before_pr_merge=True, max_retries=3),
            ui=_ChoiceUI(choice=1),
        )
        comp = manifest.get_component("comp-a")
        dep = manifest.get_component("comp-b")
        assert comp is not None and dep is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.checkpoint == CheckpointDecision.REJECTED
        assert outcome.transition == Transition.FAILED
        # R2.6: reject is terminal - no retry consumed, dependents skipped.
        assert comp.retries == 0
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "hitl_reject"
        # #339 P2-1: the journal signature, not the phase. Without an
        # explicit `signatures=` the phase becomes the check name and a
        # human refusing the change is recorded as
        # `pr:rejected-at-hitl-checkpoint`, which _CATEGORY_BY_CHECK
        # files as infrastructure and the autonomy replay then discards
        # as an outage. Asserted on the recorded signature rather than
        # on the argument, because the argument is not what the journal
        # reads.
        assert pipeline.component_failure_signatures["comp-a"] == ["review:hitl-rejected"]
        assert category_for_check("review") == "review"
        assert dep.status == ComponentStatus.SKIPPED.value
        assert result.failed == ["comp-a"]

    def test_checkpoint_retry_consumes_a_retry(self, tmp_path: Path) -> None:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(pause_before_pr_merge=True),
            ui=_ChoiceUI(choice=2),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.checkpoint == CheckpointDecision.RETRY
        assert outcome.transition == Transition.RETRYING
        assert comp.retries == 1
        assert comp.status == ComponentStatus.PENDING.value
        assert comp.failed_check == "hitl_retry"
        # #339 A4: the sibling of the rejection branch, and it had the
        # same defect 46 lines below the fix. A human asking for changes
        # is a verdict on the change, so the signature says `review:`
        # rather than letting the phase file it under `pr`, which
        # _CATEGORY_BY_CHECK carries as infrastructure.
        recorded = pipeline.component_failure_signatures["comp-a"]
        assert recorded == ["review:hitl-changes-requested"]
        assert category_for_check("review") == "review"
        assert "Human reviewer requested changes" in (pipeline.component_contexts["comp-a"])

    def test_merge_pending_parks_component(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "kstrl.pr.push_create_and_merge_pr",
            lambda *a, **k: PrOutcome(
                pushed=True,
                pr_number=7,
                pr_url="https://x/pull/7",
                merged=False,
                merge_pending=True,
                error="merge not confirmed",
            ),
        )
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.MERGE_PENDING
        assert outcome.pr is not None
        assert outcome.pr.disposition == PrDisposition.MERGE_PENDING
        assert comp.status == ComponentStatus.MERGE_PENDING.value
        # Parked, not terminal: no completion stamp, in neither bucket.
        assert comp.completed_at == ""
        assert result.completed == []
        assert result.failed == []
        assert result.pr_urls == ["https://x/pull/7"]
        assert any(e["event"] == "merge_pending" for e in _events(tmp_path))

    def test_pr_flow_failure_fails_and_cascade_skips(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "kstrl.pr.push_create_and_merge_pr",
            lambda *a, **k: PrOutcome(
                pushed=False,
                merged=False,
                error="push rejected",
            ),
        )
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(),
        )
        comp = manifest.get_component("comp-a")
        dep = manifest.get_component("comp-b")
        assert comp is not None and dep is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.FAILED
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_phase == "pr"
        assert comp.failed_check == "pr_flow"
        assert comp.error == "push rejected"
        assert dep.status == ComponentStatus.SKIPPED.value
        assert result.failed == ["comp-a"]

    def test_confirmed_merge_completes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "kstrl.pr.push_create_and_merge_pr",
            lambda *a, **k: PrOutcome(
                pushed=True,
                pr_number=8,
                pr_url="https://x/pull/8",
                merged=True,
            ),
        )
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.pr is not None
        assert outcome.pr.disposition == PrDisposition.MERGED
        assert comp.status == ComponentStatus.COMPLETED.value
        assert result.completed == ["comp-a"]
        assert result.pr_urls == ["https://x/pull/8"]

    def test_no_gh_completes_without_pr(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: False)
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.pr is not None
        assert outcome.pr.disposition == PrDisposition.NO_GH
        assert result.completed == ["comp-a"]
        assert result.pr_urls == []


class TestMergeConflictDoctrine:
    """R7.5: a CONFLICTING PR re-runs the component against the freshly
    merged base (re-run, don't rebase) instead of failing terminally."""

    def _pr_config(self, **overrides: Any) -> FactoryConfig:
        return _factory_config(create_prs=True, **overrides)

    def _conflict_outcome(self) -> PrOutcome:
        return PrOutcome(
            pushed=True,
            pr_number=7,
            pr_url="https://x/pull/7",
            merged=False,
            merge_conflict=True,
            error="PR #7 conflicts with main",
        )

    def test_conflict_routes_to_fresh_base_retry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "kstrl.pr.push_create_and_merge_pr",
            lambda *a, **k: self._conflict_outcome(),
        )
        closed: list[tuple[int, str]] = []

        def fake_close(pr_number: int, branch: str, cwd: Path) -> None:
            closed.append((pr_number, branch))
            return None

        monkeypatch.setattr("kstrl.pr.close_pr_for_rerun", fake_close)
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(max_retries=1, use_worktrees=True),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        comp.pr_number = 7
        comp.pr_url = "https://x/pull/7"
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert outcome.pr is not None
        assert outcome.pr.disposition == PrDisposition.CONFLICT
        # Scheduled for a re-run, not failed.
        assert comp.status == ComponentStatus.PENDING.value
        assert comp.retries == 1
        assert result.failed == []
        # The re-run recreates worktree AND branch from origin/<base>.
        assert "comp-a" in pipeline.fresh_base_retry_ids
        assert "[conflict retry" in comp.error
        # The old PR was closed and its pointers cleared, so the retry
        # creates a fresh PR instead of re-polling the closed one.
        assert closed == [(7, comp.branch_name)]
        assert comp.pr_number is None
        assert comp.pr_url == ""
        assert pipeline.component_failure_signatures["comp-a"] == [
            "pr:merge-conflict",
        ]
        # The next attempt's context explains the re-run.
        assert "freshly merged base" in pipeline.component_contexts["comp-a"]

    def test_conflict_with_retries_exhausted_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "kstrl.pr.push_create_and_merge_pr",
            lambda *a, **k: self._conflict_outcome(),
        )
        monkeypatch.setattr(
            "kstrl.pr.close_pr_for_rerun",
            lambda *a: None,
        )
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(max_retries=0),
        )
        comp = manifest.get_component("comp-a")
        dep = manifest.get_component("comp-b")
        assert comp is not None and dep is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.FAILED
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "merge_conflict"
        assert dep.status == ComponentStatus.SKIPPED.value
        assert result.failed == ["comp-a"]

    def test_conflict_close_failure_is_nonfatal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "kstrl.pr.push_create_and_merge_pr",
            lambda *a, **k: self._conflict_outcome(),
        )
        monkeypatch.setattr(
            "kstrl.pr.close_pr_for_rerun",
            lambda *a: "gh pr close #7 failed",
        )
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(max_retries=1),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        comp.pr_number = 7
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.status == ComponentStatus.PENDING.value


class TestMergePendingRepoll:
    def _parked(
        self, tmp_path: Path
    ) -> tuple[
        ComponentPipeline,
        Manifest,
        FactoryResult,
    ]:
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(create_prs=True),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        comp.status = ComponentStatus.MERGE_PENDING.value
        comp.pr_number = 7
        comp.pr_url = "https://x/pull/7"
        return pipeline, manifest, result

    def _seed_merge_gate(self, tmp_path: Path) -> Inbox:
        """The merge_gate item the park itself would have raised."""
        box = Inbox(tmp_path, InboxConfig())
        box.add(
            ItemKind.MERGE_GATE,
            "comp-a merge unconfirmed",
            component="comp-a",
            dedupe_key="merge:comp-a",
        )
        return box

    def test_repoll_merged_completes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "kstrl.pr.wait_for_merge",
            lambda *a, **k: "merged",
        )
        monkeypatch.setattr(
            "kstrl.git.fetch_base_branch",
            lambda *a, **k: None,
        )
        pipeline, manifest, result = self._parked(tmp_path)
        box = self._seed_merge_gate(tmp_path)
        pipeline.repoll_merge_pending()
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.status == ComponentStatus.COMPLETED.value
        assert result.completed == ["comp-a"]
        # R8.3: reality answered the gate, so the item must not survive
        # to hold a cap slot and ask for a decision already made.
        assert box.open_items() == []

    def test_repoll_closed_fails_and_cascade_skips(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "kstrl.pr.wait_for_merge",
            lambda *a, **k: "closed",
        )
        pipeline, manifest, result = self._parked(tmp_path)
        box = self._seed_merge_gate(tmp_path)
        pipeline.repoll_merge_pending()
        comp = manifest.get_component("comp-a")
        dep = manifest.get_component("comp-b")
        assert comp is not None and dep is not None
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "pr_closed"
        assert dep.status == ComponentStatus.SKIPPED.value
        assert result.failed == ["comp-a"]
        assert result.skipped == ["comp-b"]
        # R8.3: it stopped being a merge decision and became a halt, so
        # the merge_gate item is resolved and a halted_run replaces it.
        assert [str(i.kind) for i in box.open_items()] == ["halted_run"]

    def test_repoll_unconfirmed_stays_parked(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "kstrl.pr.wait_for_merge",
            lambda *a, **k: "unknown",
        )
        pipeline, manifest, result = self._parked(tmp_path)
        pipeline.repoll_merge_pending()
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.status == ComponentStatus.MERGE_PENDING.value
        assert result.completed == []
        assert result.failed == []


class TestSchedulerFacingTransitions:
    def test_provisioning_failure_via_fail(self, tmp_path: Path) -> None:
        pipeline, manifest, result, _ = _make_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        transition = pipeline.fail(
            comp,
            "worktree add failed",
            phase="provisioning",
            check="worktree_setup",
        )
        assert transition == Transition.FAILED
        assert comp.failed_phase == "provisioning"
        assert result.failed == ["comp-a"]

    def test_scheduler_backstop_failure(self, tmp_path: Path) -> None:
        pipeline, manifest, result, _ = _make_pipeline(tmp_path)
        captured: list[Event] = []
        pipeline.bus.add_sink(CallbackSink(captured.append))
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        pipeline.worktree_paths["comp-a"] = tmp_path / "wt-a"
        pipeline.fail_scheduler_backstop("comp-a", 120.0)
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "scheduler_backstop"
        assert comp.error == "component timeout"
        assert comp.evidence_worktree == str(tmp_path / "wt-a")
        assert result.failed == ["comp-a"]
        # The worktree entry survives: a leaked worker may still own it.
        assert "comp-a" in pipeline.worktree_paths
        completed = [e for e in captured if isinstance(e, PhaseCompleted)]
        assert len(completed) == 1
        assert completed[0].phase == "engineer"
        assert completed[0].passed is False
        assert completed[0].detail == "component timeout"


class TestDistillPlacement:
    def test_distiller_runs_pre_pr(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The R7.3 placement decision, as a test: distillation happens
        after the review gates and BEFORE the PR step, so the distilled
        diff is the component's true delta."""
        calls: list[str] = []
        monkeypatch.setattr("kstrl.pr.is_gh_available", lambda: True)

        def _pr(*args: Any, **kwargs: Any) -> PrOutcome:
            calls.append("pr")
            return PrOutcome(
                pushed=True,
                pr_number=9,
                pr_url="https://x/pull/9",
                merged=True,
            )

        monkeypatch.setattr("kstrl.pr.push_create_and_merge_pr", _pr)
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(create_prs=True),
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
            calls=calls,
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.distill is not None and outcome.distill.ran
        assert "distill" in calls and "pr" in calls
        assert calls.index("distill") < calls.index("pr")

    def test_distill_skipped_on_exhausted_adversarial_budget(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="skip",
                max_adversarial_calls=1,
            ),
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
        )
        pipeline.adversarial_budget_consume()
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.distill is not None and not outcome.distill.ran
        assert "distill" not in calls
        assert any(f.is_phase_skip for f in comp.findings)


class TestFactUtilizationUsesTheRealMatcher:
    """Integration cover for the pipeline -> matcher seam.

    Every test in TestFactUtilizationRecording stubs
    `measure_fact_utilization`, so none of them can see the pipeline
    calling it with the wrong shape. That is exactly how the diff came
    to be passed positionally - bypassing added-line filtering - while
    the matcher's own unit tests stayed green. These wire the REAL
    matcher into the pipeline so the contract cannot drift again.
    """

    CLAIM = "The widget parser rejects trailing commas."

    def _prefix(self) -> str:
        from kstrl.knowledge import _format_section

        return _format_section(
            "Dependencies",
            [
                Fact(
                    id="fact-001",
                    component_id="comp-x",
                    created_iter=1,
                    created_run_id="factory-20260101-120000-aaaaaa",
                    scope="contract",
                    evidence=["src/x.py:1"],
                    confidence="review_passed",
                    claim=self.CLAIM,
                )
            ],
        )

    def _run_with_diff(
        self,
        tmp_path: Path,
        diff: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> FactUtilization:
        monkeypatch.setattr("kstrl.git.get_diff_content", lambda *a, **k: diff)
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="skip"),
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
            # The real function, not a stub.
            hooks_overrides={
                "measure_fact_utilization": measure_fact_utilization,
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.record_injected_knowledge("comp-a", self._prefix())
        pipeline.begin_attempt(comp)
        pipeline.process_result("comp-a", _success("comp-a"))
        return pipeline.fact_utilization["comp-a"]

    def _diff(self, body: str) -> str:
        return "diff --git a/w.py b/w.py\n--- a/w.py\n+++ b/w.py\n@@ -1,3 +1,3 @@\n" + body

    def test_added_line_is_referenced_end_to_end(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        util = self._run_with_diff(
            tmp_path,
            self._diff(f"+# {self.CLAIM}\n"),
            monkeypatch,
        )
        assert util.measured is True
        assert util.injected == 1
        assert util.referenced == 1

    def test_deleted_line_is_not_referenced_end_to_end(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The regression this seam let through: deleting the code that
        expressed a fact must not satisfy the utilization gate."""
        util = self._run_with_diff(
            tmp_path,
            self._diff(f"-# {self.CLAIM}\n+def parse2(): pass\n"),
            monkeypatch,
        )
        assert util.measured is True
        assert util.injected == 1
        assert util.referenced == 0

    def test_context_line_is_not_referenced_end_to_end(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        util = self._run_with_diff(
            tmp_path,
            self._diff(f" # {self.CLAIM}\n-x = 1\n+x = 2\n"),
            monkeypatch,
        )
        assert util.referenced == 0


class TestFactUtilizationRecording:
    """#191: the metric is recorded, measured against the prefix the
    engineer actually saw, and an unmeasured result never reads as a
    measured zero."""

    def _run(
        self,
        tmp_path: Path,
        *,
        prefix: str | None = "FACT: alpha is durable",
        measure: Any = None,
        distill: Any = None,
        config: FactoryConfig | None = None,
        record_prefix: bool = True,
    ) -> tuple[ComponentPipeline, Any, list[Event], list[str]]:
        overrides: dict[str, Any] = {}
        if measure is not None:
            overrides["measure_fact_utilization"] = measure
        if distill is not None:
            overrides["distill_facts"] = distill
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            config=config or _factory_config(review_mode="skip"),
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
            hooks_overrides=overrides,
        )
        captured: list[Event] = []
        pipeline.bus.add_sink(CallbackSink(captured.append))
        comp = manifest.get_component("comp-a")
        assert comp is not None
        if record_prefix:
            pipeline.record_injected_knowledge("comp-a", prefix)
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        return pipeline, outcome, captured, calls

    @staticmethod
    def _util_events(
        captured: list[Event],
    ) -> list[ev.FactUtilizationMeasured]:
        return [e for e in captured if isinstance(e, ev.FactUtilizationMeasured)]

    def test_utilization_event_carries_the_measurement(
        self,
        tmp_path: Path,
    ) -> None:
        _, outcome, captured, _ = self._run(
            tmp_path,
            measure=lambda *a, **k: {"injected": 4, "referenced": 2},
        )
        events = self._util_events(captured)
        assert len(events) == 1
        assert events[0].measured is True
        assert events[0].injected == 4
        assert events[0].referenced == 2
        assert outcome.distill.utilization == FactUtilization(
            measured=True,
            injected=4,
            referenced=2,
        )

    def test_distill_result_does_not_duplicate_the_measurement(
        self,
        tmp_path: Path,
    ) -> None:
        """One measurement, one event. Carrying it on DistillResult too
        would let a consumer folding both double count."""
        _, _, captured, _ = self._run(
            tmp_path,
            measure=lambda *a, **k: {"injected": 4, "referenced": 2},
        )
        distills = [e for e in captured if isinstance(e, ev.DistillResult)]
        assert len(distills) == 1
        assert not any(
            f.name.startswith(("facts_injected", "facts_referenced", "utilization"))
            for f in dataclasses.fields(distills[0])
        )

    def test_measured_against_the_injected_prefix(
        self,
        tmp_path: Path,
    ) -> None:
        """The prefix handed to measure_fact_utilization is the one the
        factory recorded at submit time, verbatim."""
        seen: list[str] = []

        def measure(
            prefix: str,
            *artifacts: str,
            **kwargs: Any,
        ) -> dict[str, int]:
            seen.append(prefix)
            return {"injected": 1, "referenced": 1}

        self._run(
            tmp_path,
            prefix="FACT: the sentinel prefix",
            measure=measure,
        )
        assert seen == ["FACT: the sentinel prefix"]

    def test_prefix_is_not_rebuilt_after_distillation(
        self,
        tmp_path: Path,
    ) -> None:
        """The regression pin for the bug #191 actually fixed.

        The phase used to rebuild the knowledge prefix here, by which
        time distill_facts had written this run's facts into the store
        and the core tier read them straight back - counting facts the
        engineer never saw and matching them against the very diff they
        were distilled from. A distiller that writes into the knowledge
        root must not move the measured numbers.
        """
        knowledge_root = tmp_path / "knowledge"

        def distill(*args: Any, **kwargs: Any) -> tuple[int, str]:
            # Stand-in for write_facts: land new facts for this same
            # component, exactly what a rebuild would then pick up.
            dest = knowledge_root / "comp-a" / "run-test"
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "new.md").write_text(
                "- **comp-a**[api] {review_passed}: freshly distilled fact\n"
            )
            return 1, "1 fact written"

        seen: list[str] = []

        def measure(
            prefix: str,
            *artifacts: str,
            **kwargs: Any,
        ) -> dict[str, int]:
            seen.append(prefix)
            return {"injected": 1, "referenced": 1}

        _, outcome, captured, _ = self._run(
            tmp_path,
            prefix="FACT: only what the engineer saw",
            measure=measure,
            distill=distill,
        )
        assert seen == ["FACT: only what the engineer saw"]
        assert self._util_events(captured)[0].injected == 1

    def test_unmeasured_when_no_prefix_recorded(
        self,
        tmp_path: Path,
    ) -> None:
        _, outcome, captured, calls = self._run(
            tmp_path,
            record_prefix=False,
        )
        util = outcome.distill.utilization
        assert util.measured is False
        assert util.reason == "no injected prefix recorded for this attempt"
        assert "utilization" not in calls
        # The event is emitted even when nothing could be measured, so
        # the stream carries the same population the journal does.
        assert self._util_events(captured)[0].measured is False

    def test_unmeasured_when_retrieval_failed(self, tmp_path: Path) -> None:
        """A None record (the factory's retrieval-failure path) is not a
        zero: nothing was injected, so nothing can be referenced."""
        _, outcome, _, calls = self._run(tmp_path, prefix=None)
        util = outcome.distill.utilization
        assert util.measured is False
        assert util.reason == "knowledge retrieval failed"
        assert "utilization" not in calls

    def test_empty_prefix_is_a_measured_zero(self, tmp_path: Path) -> None:
        """Knowledge on, store cold. That is honest evidence the layer
        has not warmed up, not a failure to measure."""
        _, outcome, _, calls = self._run(tmp_path, prefix="")
        util = outcome.distill.utilization
        assert util.measured is True
        assert (util.injected, util.referenced) == (0, 0)
        assert "utilization" not in calls

    def test_measurement_failure_warns_and_is_unmeasured(
        self,
        tmp_path: Path,
    ) -> None:
        """Was a bare `except: pass`. A broken recorder must not be
        indistinguishable from an engineer that referenced nothing."""
        buf = io.StringIO()
        ui = PlainUI(no_color=True, file=buf)
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            ui=ui,
            config=_factory_config(review_mode="skip"),
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
            hooks_overrides={
                "measure_fact_utilization": _raise(RuntimeError("boom")),
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.record_injected_knowledge("comp-a", "FACT: alpha")
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        util = outcome.distill.utilization
        assert util.measured is False
        assert util.reason == "RuntimeError: boom"
        assert "utilization measurement failed" in buf.getvalue()

    def test_measured_zero_is_distinct_from_unmeasured(
        self,
        tmp_path: Path,
    ) -> None:
        """Facts injected and demonstrably unused is a real negative
        result; being unable to measure is no result at all."""
        _, measured_zero, _, _ = self._run(
            tmp_path,
            measure=lambda *a, **k: {"injected": 3, "referenced": 0},
        )
        _, unmeasured, _, _ = self._run(tmp_path, record_prefix=False)
        assert measured_zero.distill.utilization.measured is True
        assert measured_zero.distill.utilization.referenced == 0
        assert unmeasured.distill.utilization.measured is False
        assert measured_zero.distill.utilization != unmeasured.distill.utilization

    def test_utilization_recorded_when_distillation_raises(
        self,
        tmp_path: Path,
    ) -> None:
        """Utilization answers "did the engineer use what we gave it",
        which does not depend on the distiller succeeding."""
        pipeline, outcome, captured, _ = self._run(
            tmp_path,
            measure=lambda *a, **k: {"injected": 5, "referenced": 3},
            distill=_raise(RuntimeError("distiller down")),
        )
        assert [e for e in captured if isinstance(e, ev.DistillResult)] == []
        # ...but the utilization event still fires: it no longer rides
        # on distillation succeeding.
        assert self._util_events(captured)[0].measured is True
        assert pipeline.fact_utilization["comp-a"] == FactUtilization(
            measured=True,
            injected=5,
            referenced=3,
        )

    def test_utilization_recorded_when_distill_budget_skipped(
        self,
        tmp_path: Path,
    ) -> None:
        """The metric costs zero tokens, so an exhausted LLM budget must
        not throw away evidence the run already paid for."""
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="skip",
                max_adversarial_calls=1,
            ),
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
            hooks_overrides={
                "measure_fact_utilization": (lambda *a, **k: {"injected": 6, "referenced": 4}),
            },
        )
        pipeline.adversarial_budget_consume()
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.record_injected_knowledge("comp-a", "FACT: alpha")
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.distill.ran is False
        assert "distill" not in calls
        assert outcome.distill.utilization == FactUtilization(
            measured=True,
            injected=6,
            referenced=4,
        )
        assert pipeline.fact_utilization["comp-a"].measured is True

    def test_nothing_recorded_when_knowledge_disabled(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="skip"),
            knowledge=KnowledgeConfig(enabled=False),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        pipeline.process_result("comp-a", _success("comp-a"))
        assert pipeline.fact_utilization == {}
        assert "utilization" not in calls

    def test_nothing_recorded_in_single_pr_mode(self, tmp_path: Path) -> None:
        """single_pr's shared diff carries sibling changes, which would
        inflate `referenced` - the same reason distillation skips."""
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="skip"),
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
        )
        pipeline.manifest.single_pr = True
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.record_injected_knowledge("comp-a", "FACT: alpha")
        pipeline.begin_attempt(comp)
        pipeline.process_result("comp-a", _success("comp-a"))
        assert pipeline.fact_utilization == {}
        assert "utilization" not in calls

    def test_review_failed_component_is_still_measured(
        self,
        tmp_path: Path,
    ) -> None:
        """The sampling-bias fix. Distillation runs only after review
        and security pass, so measuring there sampled successful
        components exclusively - a component that failed review had
        facts injected and may well have used them."""
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="hard"),
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
            hooks_overrides={
                "run_review": lambda *a, **k: ReviewResult(
                    passed=False,
                    mode="hard",
                ),
                "measure_fact_utilization": (lambda *a, **k: {"injected": 4, "referenced": 3}),
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.record_injected_knowledge("comp-a", "FACT: alpha")
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        # The component never reached the distill phase...
        assert outcome.distill is None
        # ...and is measured anyway.
        assert pipeline.fact_utilization["comp-a"] == FactUtilization(
            measured=True,
            injected=4,
            referenced=3,
        )

    def test_security_failed_component_is_still_measured(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="hard",
                security_config=SecurityConfig(mode="hard"),
            ),
            security_selection=_selection("security"),
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
            hooks_overrides={
                "run_security_review": lambda *a, **k: SecurityResult(
                    passed=False,
                    mode="hard",
                ),
                "measure_fact_utilization": (lambda *a, **k: {"injected": 2, "referenced": 1}),
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.record_injected_knowledge("comp-a", "FACT: alpha")
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.distill is None
        assert pipeline.fact_utilization["comp-a"].measured is True
        assert pipeline.fact_utilization["comp-a"].referenced == 1

    def _verify_failing_pipeline(
        self,
        tmp_path: Path,
    ) -> tuple[ComponentPipeline, Manifest, list[str]]:
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
            hooks_overrides={
                "run_mechanical_verification": (
                    lambda *a, **k: VerificationResult(
                        passed=False,
                        checks=[
                            CheckResult(
                                name="test_suite",
                                passed=False,
                                message="boom",
                                duration_seconds=0.1,
                            )
                        ],
                    )
                ),
                "measure_fact_utilization": (lambda *a, **k: {"injected": 5, "referenced": 2}),
            },
        )
        return pipeline, manifest, calls

    def test_verify_failed_component_is_still_measured(
        self,
        tmp_path: Path,
    ) -> None:
        """A verification failure means the diff phase has not run yet,
        NOT that no diff exists - the engineer's change is committed and
        is exactly what verification just inspected. Writing these off
        would drop every test, typecheck and lint failure from the
        sample, which is most of the failure population."""
        pipeline, manifest, _ = self._verify_failing_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.record_injected_knowledge("comp-a", "FACT: alpha")
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition != Transition.COMPLETED
        assert pipeline.fact_utilization["comp-a"] == FactUtilization(
            measured=True,
            injected=5,
            referenced=2,
        )

    def test_unmeasured_only_when_the_diff_fetch_actually_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`measured: false` is reserved for a real inability to
        measure, and it says so rather than going silently absent."""

        def _boom(*args: Any, **kwargs: Any) -> str:
            raise git.GitDiffError("git exploded")

        monkeypatch.setattr("kstrl.git.get_diff_content", _boom)
        pipeline, manifest, calls = self._verify_failing_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.record_injected_knowledge("comp-a", "FACT: alpha")
        pipeline.begin_attempt(comp)
        pipeline.process_result("comp-a", _success("comp-a"))
        util = pipeline.fact_utilization["comp-a"]
        assert util.measured is False
        assert util.reason == "diff unavailable: git exploded"
        assert "utilization" not in calls

    def test_the_real_matcher_is_called_with_the_diff_as_a_keyword(
        self,
        tmp_path: Path,
    ) -> None:
        """Pins the call SHAPE, not just the matcher.

        `measure_fact_utilization` filters only its keyword-only
        `diff=`; positional artifacts are searched raw by design. So
        passing the diff positionally silently restores the
        deletion/context false positive - and nothing else can see it:
        the signature takes *artifacts so mypy is happy, and every other
        test here injects a permissive `lambda *a, **k` stub that
        accepts either shape. This one asserts the keyword directly.
        """
        seen: dict[str, Any] = {}

        def spy(
            prefix: str,
            *artifacts: str,
            **kwargs: Any,
        ) -> dict[str, int]:
            seen["artifacts"] = artifacts
            seen["diff"] = kwargs.get("diff")
            return {"injected": 1, "referenced": 1}

        self._run(tmp_path, measure=spy)
        assert seen["diff"] == "diff --git a b\n", (
            "the diff must be passed as diff=, not as an artifact"
        )
        assert "diff --git" not in "".join(seen["artifacts"])

    def test_per_tier_counts_are_carried_through(
        self,
        tmp_path: Path,
    ) -> None:
        """The denominator-bias fix: the sibling tier inflates the
        overall ratio, so the core tier is recorded separately."""
        _, outcome, _, _ = self._run(
            tmp_path,
            measure=lambda *a, **k: {
                "injected": 6,
                "referenced": 2,
                "core_injected": 2,
                "core_referenced": 2,
                "dependency_injected": 1,
                "dependency_referenced": 0,
                "sibling_injected": 3,
                "sibling_referenced": 0,
            },
        )
        util = outcome.distill.utilization
        # Overall reads 2/6; the core tier reads 2/2.
        assert (util.injected, util.referenced) == (6, 2)
        assert (util.core_injected, util.core_referenced) == (2, 2)
        assert (util.sibling_injected, util.sibling_referenced) == (3, 0)
        assert util.to_dict()["by_tier"]["core"] == {
            "injected": 2,
            "referenced": 2,
        }

    def test_hook_without_tier_keys_degrades_to_no_breakdown(
        self,
        tmp_path: Path,
    ) -> None:
        """measure_fact_utilization is an injected seam; a hook that
        reports only the totals must not break the measurement."""
        _, outcome, _, _ = self._run(
            tmp_path,
            measure=lambda *a, **k: {"injected": 3, "referenced": 1},
        )
        util = outcome.distill.utilization
        assert util.measured is True
        assert (util.injected, util.referenced) == (3, 1)
        assert util.core_injected == 0

    def test_none_record_supersedes_a_previous_attempt(
        self,
        tmp_path: Path,
    ) -> None:
        """A retry whose capture fails must not be measured against the
        previous attempt's prefix - it would score this attempt's diff
        against facts this attempt's engineer never received."""
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="skip"),
            knowledge=KnowledgeConfig(
                enabled=True,
                knowledge_root=tmp_path / "knowledge",
            ),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.record_injected_knowledge("comp-a", "FACT: first attempt")
        pipeline.record_injected_knowledge("comp-a", None)
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.distill.utilization.measured is False
        assert "utilization" not in calls


class TestJournalConfigNeverGatesAnAttempt:
    """#257 sweep: the third caller of ``EvolutionConfig.load`` that
    could raise ValueError into work already paid for."""

    def test_an_unparseable_journal_config_does_not_break_a_retry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``journal_superseded_findings`` runs between a failed attempt
        and the retry that supersedes it. A bad [evolution] knob raised
        straight through it, so a typo turned a normal retry into a
        crashed run. The entry is an audit trail; losing it is the
        acceptable outcome, losing the retry is not.
        """
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "many")
        console = io.StringIO()
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            ui=PlainUI(no_color=True, file=console),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        comp.findings = [
            Finding(
                phase="review",
                category="scope_creep",
                severity="major",
                location="a.py",
                explanation="unrelated refactor",
            )
        ]

        pipeline.journal_superseded_findings(comp)

        assert "Evolution config unreadable" in console.getvalue()
        assert not (tmp_path / ".kstrl" / "evolution.jsonl").exists()


class TestPhaseReadingsRetireSkippableFindings:
    """#247: a review or security finding from an earlier attempt is
    dropped from the next attempt's prompt only when its phase actually
    ran again and returned a verdict.

    Drives the real ``process_result`` twice per case, feeding the stored
    context back in as the next attempt's ``ComponentResult.context_json``
    exactly as the factory's ``_submit_args`` does, and asserts on the
    block the worker would render into the engineer's prompt.

    ON THE FAILING GATE IN THE BUDGET CASES. The issue's second
    acceptance criterion asks for an attempt that skips review on an
    exhausted budget and fails SECURITY. That is unreachable, measured
    rather than assumed: both phases consult the same counter and review
    runs first, so a budget that has skipped review has already skipped
    security. The reachable shape puts the failing gate above security,
    and these tests use the HITL checkpoint (rank ``pr``). The
    criterion's intent - a skippable phase that did not run retires
    nothing - is pinned three ways: here on the budget cause, here on the
    operator's explicit skip, and at the unit layer in
    ``tests/test_context.py``.
    """

    def _attempt(
        self,
        pipeline: ComponentPipeline,
        manifest: Manifest,
    ) -> Any:
        """One attempt, wired the way the factory wires it."""
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        return pipeline.process_result(
            "comp-a",
            ComponentResult(
                "comp-a",
                success=True,
                iterations=1,
                duration_seconds=1.0,
                context_json=pipeline.component_contexts.get("comp-a"),
            ),
        )

    def _prompt_block(self, pipeline: ComponentPipeline) -> str:
        """What the worker would build for the next attempt."""
        raw = pipeline.component_contexts.get("comp-a", "{}")
        return IterationContext.from_json(raw).format_for_prompt()

    def _pipeline(
        self,
        tmp_path: Path,
        *,
        config: FactoryConfig,
        reviews: list[ReviewResult] | None = None,
        securities: list[SecurityResult] | None = None,
        review_raises: Exception | None = None,
        ui: PlainUI | None = None,
    ) -> tuple[ComponentPipeline, Manifest]:
        review_queue = iter(reviews or [])
        security_queue = iter(securities or [])

        def _review(*args: Any, **kwargs: Any) -> ReviewResult:
            """Serve the queued results, then raise: the crash is what
            the attempt after the last queued verdict does."""
            queued = next(review_queue, None)
            if queued is None:
                assert review_raises is not None
                raise review_raises
            return queued

        overrides: dict[str, Any] = {
            "run_review": lambda *a, **k: next(review_queue),
            "run_security_review": lambda *a, **k: next(security_queue),
        }
        if review_raises is not None:
            overrides["run_review"] = _review
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            components=[_component("comp-a")],
            config=config,
            ui=ui,
            security_selection=_selection("security"),
            hooks_overrides=overrides,
        )
        return pipeline, manifest

    def test_a_review_that_ran_and_passed_retires_its_own_earlier_finding(
        self,
        tmp_path: Path,
    ) -> None:
        """Acceptance criterion 1. Attempt 1 fails review, attempt 2
        passes review and fails security, and attempt 3 is not told to
        re-check the criterion the reviewer cleared."""
        pipeline, manifest = self._pipeline(
            tmp_path,
            config=_factory_config(
                max_retries=5,
                review_mode="hard",
                security_config=SecurityConfig(mode="hard"),
            ),
            reviews=[
                ReviewResult(passed=False, mode="hard", overall_notes="CRITERION-X-UNMET"),
                ReviewResult(passed=True, mode="hard"),
            ],
            securities=[
                SecurityResult(passed=False, mode="hard", overall_notes="SQL-IN-USERS"),
            ],
        )
        for _ in range(2):
            assert self._attempt(pipeline, manifest).transition == Transition.RETRYING

        block = self._prompt_block(pipeline)
        assert "SQL-IN-USERS" in block
        assert "CRITERION-X-UNMET" not in block
        assert "from review passed or were re-measured in attempt 2" in block

    def test_a_security_review_that_passed_retires_its_own_earlier_finding(
        self,
        tmp_path: Path,
    ) -> None:
        """The mirror of criterion 1 for the other skippable phase, so
        dropping either recording site is caught behaviourally and not
        only by the site census."""
        pipeline, manifest = self._pipeline(
            tmp_path,
            config=_factory_config(
                max_retries=5,
                review_mode="hard",
                security_config=SecurityConfig(mode="hard"),
            ),
            reviews=[
                ReviewResult(passed=True, mode="hard"),
                ReviewResult(passed=True, mode="hard"),
            ],
            securities=[
                SecurityResult(passed=False, mode="hard", overall_notes="SQL-IN-USERS"),
                SecurityResult(passed=True, mode="hard"),
            ],
        )
        assert self._attempt(pipeline, manifest).transition == Transition.RETRYING
        assert self._attempt(pipeline, manifest).transition == Transition.COMPLETED
        pipeline.record_contract_failure("comp-a", 2, "tier 0 broke")

        block = self._prompt_block(pipeline)
        assert "tier 0 broke" in block
        assert "SQL-IN-USERS" not in block
        assert "from security passed or were re-measured in attempt 2" in block

    def test_a_review_the_budget_skipped_retires_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        """Acceptance criterion 2, budget cause. Attempt 2's reviewer
        never ran, so attempt 1's finding is still shown."""
        pipeline, manifest = self._pipeline(
            tmp_path,
            config=_factory_config(
                max_retries=5,
                review_mode="advisory",
                security_config=SecurityConfig(mode="advisory"),
                max_adversarial_calls=1,
                create_prs=True,
                pause_before_pr_merge=True,
            ),
            reviews=[
                ReviewResult(passed=False, mode="advisory", overall_notes="CRITERION-X-UNMET"),
            ],
            ui=_ChoiceUI(choice=2),
        )
        for _ in range(2):
            assert self._attempt(pipeline, manifest).transition == Transition.RETRYING

        block = self._prompt_block(pipeline)
        assert "Human reviewer requested changes" in block
        assert "CRITERION-X-UNMET" in section(block, NOT_REMEASURED)
        assert section(block, RESOLVED) == ""

    def test_a_review_turned_off_by_the_operator_retires_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        """Acceptance criterion 2, the other live skip cause. The
        operator sets ``review_mode = "skip"`` between attempts."""
        pipeline, manifest = self._pipeline(
            tmp_path,
            config=_factory_config(
                max_retries=5,
                review_mode="hard",
                security_config=SecurityConfig(mode="hard"),
            ),
            reviews=[
                ReviewResult(passed=False, mode="hard", overall_notes="CRITERION-X-UNMET"),
            ],
            securities=[
                SecurityResult(passed=False, mode="hard", overall_notes="SQL-IN-USERS"),
            ],
        )
        assert self._attempt(pipeline, manifest).transition == Transition.RETRYING
        pipeline.factory_config.review_mode = "skip"
        assert self._attempt(pipeline, manifest).transition == Transition.RETRYING

        block = self._prompt_block(pipeline)
        assert "SQL-IN-USERS" in section(block, CURRENT)
        assert "CRITERION-X-UNMET" in section(block, NOT_REMEASURED)
        assert section(block, RESOLVED) == ""

    def test_a_crashed_review_retires_nothing(self, tmp_path: Path) -> None:
        """The fail-open guard. In advisory mode a reviewer that raises
        is reported as PASSING with ``infrastructure_error=True``, and
        ``ran`` is True. Keying the record on ``ran`` would retire a live
        finding on the strength of an exception."""
        pipeline, manifest = self._pipeline(
            tmp_path,
            config=_factory_config(
                max_retries=5,
                review_mode="advisory",
                security_config=SecurityConfig(mode="advisory"),
                create_prs=True,
                pause_before_pr_merge=True,
            ),
            reviews=[
                ReviewResult(passed=False, mode="advisory", overall_notes="CRITERION-X-UNMET"),
            ],
            securities=[
                SecurityResult(passed=True, mode="advisory"),
                SecurityResult(passed=True, mode="advisory"),
            ],
            review_raises=RuntimeError("reviewer exploded"),
            ui=_ChoiceUI(choice=2),
        )
        assert self._attempt(pipeline, manifest).transition == Transition.RETRYING
        outcome = self._attempt(pipeline, manifest)
        assert outcome is not None
        assert outcome.review is not None
        assert outcome.review.ran
        assert outcome.review.result is not None
        assert outcome.review.result.infrastructure_error
        assert not outcome.review.produced_a_reading

        block = self._prompt_block(pipeline)
        assert "CRITERION-X-UNMET" in section(block, NOT_REMEASURED)

    def test_the_contract_gate_retires_a_review_that_passed(
        self,
        tmp_path: Path,
    ) -> None:
        """The second writer of the retry context, which is why
        ``record_contract_failure`` lives on the pipeline.

        Attempt 1 fails review; attempt 2 passes review and COMPLETES;
        the tier's contract test then fails. Without the merge at this
        writer the contract entry re-raises the cleared review finding.
        It also pins that completing a component does not clear the
        record: the contract loop reads it afterwards.
        """
        pipeline, manifest = self._pipeline(
            tmp_path,
            config=_factory_config(
                max_retries=5,
                review_mode="hard",
                security_config=SecurityConfig(mode="hard"),
            ),
            reviews=[
                ReviewResult(passed=False, mode="hard", overall_notes="CRITERION-X-UNMET"),
                ReviewResult(passed=True, mode="hard"),
            ],
            securities=[SecurityResult(passed=True, mode="hard")],
        )
        assert self._attempt(pipeline, manifest).transition == Transition.RETRYING
        assert self._attempt(pipeline, manifest).transition == Transition.COMPLETED

        pipeline.record_contract_failure("comp-a", 2, "tier 0 broke")

        block = self._prompt_block(pipeline)
        assert "tier 0 broke" in section(block, CURRENT)
        assert "CRITERION-X-UNMET" not in block
        assert "from review passed or were re-measured in attempt 2" in block
