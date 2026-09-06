"""R8.6 PR 2: `ks serve` regression tests.

The centre of gravity here is the retry classifier and the four
backstops, because this is the module that spends money unattended. The
rule under test is not "infrastructure errors retry" but the stronger
"NOTHING retries without positive evidence that it was infrastructural",
so most of these tests assert that an ambiguous situation does NOT
retry.

No test runs a real factory. The `FactoryRunner` Protocol exists so the
whole loop is drivable with a stub - a suite that spawned the real thing
would cost dollars per assertion at a measured $1.70-2.60 per iteration.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kstrl.agents.base import ARCHITECT_COMPONENT, ARCHITECT_ROLE
from kstrl.findings import Finding
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.manifest import (
    ADVERSARIAL_BUDGET_CHECK,
    Component,
    ComponentStatus,
    Manifest,
)
from kstrl.procgroup import safe_pgid
from kstrl.reducer import ComponentState, RunState
from kstrl.serve import (
    BACKOFF_CAP_SECONDS,
    GROUP_TERM_GRACE_SECONDS,
    SPAWNED_RUN_KIND,
    DailySpend,
    LaunchSpend,
    RunOutcome,
    RunSpend,
    ServeConfig,
    ServeError,
    ServeLockedError,
    ServeStateError,
    SpendLedger,
    Verdict,
    _group_liveness_for_reap,
    _NullObserver,
    _unreaped_timeout_detail,
    backoff_seconds,
    caffeinate_prefix,
    check_budget,
    check_cost_coverage,
    check_inbox_cap,
    check_poison_breaker,
    classify_run,
    consecutive_poison_count,
    factory_lock_held,
    next_local_midnight,
    owned_run_spend,
    process_group_alive,
    reap_leases,
    resolve_merge_gate,
    run_supervised,
    serve,
    serve_cycle,
    serve_lock,
    terminate_process_group,
)

#: Captured at import, BEFORE the autouse _no_spend fixture patches
#: kstrl.serve.read_run_spend. A test that imports the name in its body
#: gets the stub instead of the function under test - which silently made
#: the empty-run-id test pass no matter what the function did.
from kstrl.serve import read_run_spend as REAL_READ_RUN_SPEND
from kstrl.workqueue import (
    ItemSource,
    ItemState,
    MergeDisposition,
    Queue,
    QueueConfig,
)
from tests.helpers import procs

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _queue(root: Path, **kwargs: object) -> Queue:
    return Queue(root, QueueConfig(**kwargs))  # type: ignore[arg-type]


def _add(queue: Queue, **kwargs: object) -> object:
    return queue.add("# Spec\n\nDo the thing.\n", **kwargs)  # type: ignore[arg-type]


def _manifest(
    path: Path,
    components: list[Component],
    run_id: str = "factory-20260730-000000.000000-aaa",
) -> None:
    """Write a manifest through the real Manifest.save.

    Built from the real dataclasses rather than hand-written JSON: an
    earlier version of this helper invented key names and every
    classification test silently exercised the unreadable-manifest branch
    instead of the branch it claimed to test.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="p",
        base_branch="main",
        single_pr=False,
        components=components,
        run_id=run_id,
    )
    manifest.save(path)


def _make_run_dir(root: Path, run_id: str) -> Path:
    """Create the run directory a factory invocation would leave behind.

    Required because serve now charges and classifies only artifacts the
    invocation OWNS (#186 F2): a manifest with no matching NEW run dir is
    treated as someone else's and never read.
    """
    run_dir = root / ".kstrl" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").touch()
    return run_dir


def _component(
    comp_id: str,
    status: str,
    findings: list[Finding] | None = None,
    failed_check: str = "",
) -> Component:
    component = Component(
        comp_id,
        comp_id,
        "",
        [],
        f"{comp_id}.json",
        f"branch/{comp_id}",
    )
    component.status = ComponentStatus(status)
    component.findings = list(findings or [])
    component.failed_check = failed_check
    return component


def _infra_finding() -> Finding:
    return Finding.infrastructure_error("review", "agent CLI timed out")


def _spec_finding() -> Finding:
    return Finding(
        phase="review",
        category="test_quality",
        severity="fail",
        location="tests/test_x.py",
        explanation="tests assert nothing",
        tags=(),
    )


def _spend_with_component_keyed(tmp_path: Path, key: str) -> RunSpend:
    """``read_run_spend`` over a run whose ONE component row is ``key``.

    The double is a REAL ``RunState``: a hand-rolled object carrying only
    the fields this function read at the time silently became wrong the
    moment it read another (#257 piece B).

    Shared because #281's two cases differ only in that key - the role's
    namespaced row, versus the bare word that is now only ever a
    component (or a pre-#281 role row, which reads the same way and is
    the reason no compat fallback is safe).
    """
    state = RunState(cost_usd=2.0, cost_calls=1, usage_calls=1)
    state.components[key] = ComponentState(
        component_id=key,
        usage_calls=1,
        cost_calls=1,
        cost_usd=2.0,
    )
    with patch("kstrl.reducer.load_run_state") as load:
        load.return_value = (state, None)
        return REAL_READ_RUN_SPEND(tmp_path, "factory-abc")


def _stub_runner(
    outcome: RunOutcome,
    calls: list[dict[str, object]] | None = None,
    *,
    run_id: str = "factory-20260730-000000.000000-aaa",
    make_run_dir: bool = True,
    child_pid: int | None = None,
    extra_run_ids: tuple[str, ...] = (),
):
    """A factory stand-in that leaves the artifacts a real run would.

    ``make_run_dir`` creates the owned run directory; turning it off
    models a launch that died before producing anything, which serve must
    treat as having no artifacts of its own rather than reading the
    newest run on disk.

    ``extra_run_ids`` are run dirs that appear DURING the launch window
    without belonging to it - an operator running another kstrl command
    by hand on the same repo (#257 review).
    """

    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
        on_spawn: object = None,
    ) -> RunOutcome:
        if calls is not None:
            calls.append(
                {
                    "spec_path": spec_path,
                    "project_name": project_name,
                    "pause_before_pr_merge": pause_before_pr_merge,
                    "timeout_seconds": timeout_seconds,
                }
            )
        if child_pid is not None and callable(on_spawn):
            on_spawn(child_pid)
        if make_run_dir:
            _make_run_dir(root_dir, run_id)
        for extra in extra_run_ids:
            _make_run_dir(root_dir, extra)
        return outcome

    return runner


@pytest.fixture(autouse=True)
def _no_spend(monkeypatch: pytest.MonkeyPatch):
    """Default every test to a zero-cost run.

    Reading real spend needs a run dir; tests that care about cost patch
    this explicitly. Defaulting to zero keeps the accounting out of the
    way of the classification tests.
    """
    monkeypatch.setattr(
        "kstrl.serve.read_run_spend",
        lambda root, run_id: RunSpend(),
    )


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------


class TestClassifierRetriesOnlyWithEvidence:
    """The rule that stands between the queue and an overnight crash loop."""

    def test_exit_zero_is_success(self, tmp_path: Path) -> None:
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=0),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.SUCCESS

    def test_all_infra_failures_retry(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [_infra_finding()])])
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.RETRY_INFRA
        assert "comp-a" in outcome.reason

    def test_a_budget_halt_is_terminal_not_infrastructure(self, tmp_path: Path) -> None:
        """R10.5 (#226): the adversarial cap is a decision, not a fault.

        The halted component carries an infrastructure_error finding,
        which is exactly the evidence the RETRY_INFRA branch reads, so
        without the failed_check branch this manifest retries. The cap
        starts again at zero on the retry and the run stops at the same
        component, so the retry buys nothing and costs a full run.
        """
        path = tmp_path / "m.json"
        _manifest(
            path,
            [
                _component(
                    "comp-a",
                    "failed",
                    [_infra_finding()],
                    failed_check=ADVERSARIAL_BUDGET_CHECK,
                )
            ],
        )
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.BUDGET_HALT
        assert outcome.verdict.may_retry is False
        assert "max_adversarial_calls" in outcome.reason
        assert outcome.evidence["budget_halted"] == ["comp-a"]

    def test_a_budget_halt_beside_real_infra_still_refuses_to_retry(
        self,
        tmp_path: Path,
    ) -> None:
        """ANY halted component makes the run terminal, not every one.

        A mixed manifest retried would re-reach the same cap, so the
        genuine infrastructure casualty beside it does not buy a retry.
        It is still named in the reason, for the same reason the
        unevidenced sibling note exists (#197 M3): a human deciding
        whether to requeue must see everything that failed.
        """
        path = tmp_path / "m.json"
        _manifest(
            path,
            [
                _component(
                    "comp-a",
                    "failed",
                    [_infra_finding()],
                    failed_check=ADVERSARIAL_BUDGET_CHECK,
                ),
                _component("comp-b", "failed", [_infra_finding()]),
            ],
        )
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.BUDGET_HALT
        assert outcome.evidence["budget_halted"] == ["comp-a"]
        assert outcome.evidence["other_failures"] == ["comp-b"]
        assert "comp-b" in outcome.reason

    def test_a_budget_halt_beside_a_spec_failure_names_both(self, tmp_path: Path) -> None:
        """Both verdicts are terminal, so the only thing at stake is
        which one the operator is told to act on. The budget halt wins
        the verdict because it is the one that makes a retry pointless,
        and the spec failure is still named."""
        path = tmp_path / "m.json"
        _manifest(
            path,
            [
                _component(
                    "comp-a",
                    "failed",
                    [_infra_finding()],
                    failed_check=ADVERSARIAL_BUDGET_CHECK,
                ),
                _component("comp-b", "failed", [_spec_finding()], failed_check="review"),
            ],
        )
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.BUDGET_HALT
        assert outcome.verdict.may_retry is False
        assert "comp-b" in outcome.reason

    def test_a_budget_halt_beside_an_unevidenced_sibling_names_its_error(
        self,
        tmp_path: Path,
    ) -> None:
        """The third mixed shape, and the one carrying an error string.

        ``test_an_unevidenced_sibling_is_never_dropped`` asserts the same
        invariant for the spec-failure path (#197 M3), and the budget
        branch returns before that path can apply it. A sibling that
        produced no finding has ``Component.error`` and nothing else, so
        dropping it leaves the operator an id and no cause - the exact
        misdirection the sibling note exists to remove.
        """
        path = tmp_path / "m.json"
        halted = _component(
            "comp-a",
            "failed",
            [_infra_finding()],
            failed_check=ADVERSARIAL_BUDGET_CHECK,
        )
        silent = _component("comp-b", "failed", [])
        silent.error = "Failed to create worktree: fatal: invalid reference"
        _manifest(path, [halted, silent])
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.BUDGET_HALT
        assert outcome.verdict.may_retry is False
        assert "comp-b" in outcome.reason
        assert "invalid reference" in outcome.reason, (
            "the sibling's real cause must reach the operator"
        )
        assert outcome.evidence["other_failures"] == ["comp-b"]
        assert outcome.evidence["component_errors"]["comp-b"] == silent.error

    def test_a_budget_halt_after_a_timeout_still_refuses_to_retry(
        self,
        tmp_path: Path,
    ) -> None:
        """A halt followed by a hang is the shape #197 M1 already fixed
        once, for the token and cost ceilings.

        ``budget_halt_reason`` sits above the timeout branch precisely
        because a run that blew a ceiling and then hung long enough for
        ``factory_timeout_seconds`` to kill it was being requeued against
        the ceiling that verdict exists to make terminal. The adversarial
        cap is as deterministic as those two, and the manifest is written
        by the pipeline from its own counter before the kill, so it is
        positive evidence that the cap was reached in THIS run.
        """
        path = tmp_path / "m.json"
        _manifest(
            path,
            [
                _component(
                    "comp-a",
                    "failed",
                    [_infra_finding()],
                    failed_check=ADVERSARIAL_BUDGET_CHECK,
                )
            ],
        )
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1, timed_out=True),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.BUDGET_HALT
        assert outcome.verdict.may_retry is False
        assert outcome.evidence["budget_halted"] == ["comp-a"]

    def test_a_timeout_with_no_budget_halt_still_retries(self, tmp_path: Path) -> None:
        """The other direction, or the branch above would be a rename of
        the timeout verdict. A hang with no halted component in the
        manifest is still an infrastructure symptom and still retries."""
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [_infra_finding()])])
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1, timed_out=True),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.RETRY_INFRA
        assert outcome.verdict.may_retry is True

    def test_a_timeout_with_an_unreadable_manifest_still_retries(
        self,
        tmp_path: Path,
    ) -> None:
        """The timeout branch reads the manifest for positive evidence
        only. No manifest, or one that will not parse, proves nothing
        about the cap, so the verdict stays what it was before #226."""
        for path in (None, tmp_path / "missing.json"):
            outcome = classify_run(
                tmp_path,
                run=RunOutcome(returncode=1, timed_out=True),
                manifest_path=path,
            )
            assert outcome.verdict is Verdict.RETRY_INFRA, path
            assert outcome.evidence == {"timed_out": True}

    def test_one_judged_failure_blocks_the_retry(self, tmp_path: Path) -> None:
        """A mixed run is a SPEC failure: the spec failure is the verdict."""
        path = tmp_path / "m.json"
        _manifest(
            path,
            [
                _component("comp-a", "failed", [_infra_finding()]),
                _component("comp-b", "failed", [_spec_finding()]),
            ],
        )
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.SPEC_FAILURE
        assert "comp-b" in outcome.reason

    def test_a_failure_with_no_findings_is_UNCLASSIFIABLE(
        self,
        tmp_path: Path,
    ) -> None:
        """Corrected by the first live run.

        This used to assert SPEC_FAILURE on the reasoning that "no
        findings is not evidence OF infrastructure trouble". True - but
        it is not evidence of a merits-based failure either, and the
        reason string asserted one. A real `ks serve` run hit a git
        worktree failure that set Component.error with no Finding, and
        was told the component "failed on their own merits, not on
        infrastructure". Both verdicts poison, so the money behaviour was
        always right; the claim was false and pointed the operator at the
        spec instead of at git.
        """
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [])])
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.UNCLASSIFIABLE
        assert not outcome.verdict.may_retry, "still must not retry"

    def test_an_unevidenced_failure_surfaces_the_component_error(
        self,
        tmp_path: Path,
    ) -> None:
        """The operator needs the actual cause, not a guess about it."""
        path = tmp_path / "m.json"
        comp = _component("comp-a", "failed", [])
        comp.error = "Failed to create worktree: fatal: invalid reference"
        _manifest(path, [comp])
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert "invalid reference" in outcome.reason
        assert outcome.evidence["component_errors"]["comp-a"] == comp.error

    def test_a_findings_backed_failure_is_still_a_spec_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """The positive case must keep its stronger, accurate label."""
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [_spec_finding()])])
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.SPEC_FAILURE
        assert "on their own merits" in outcome.reason

    def test_a_spec_finding_outweighs_an_unevidenced_sibling(
        self,
        tmp_path: Path,
    ) -> None:
        """Real evidence beats the absence of it."""
        path = tmp_path / "m.json"
        _manifest(
            path,
            [
                _component("comp-a", "failed", []),
                _component("comp-b", "failed", [_spec_finding()]),
            ],
        )
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.SPEC_FAILURE
        assert "comp-b" in outcome.reason

    def test_an_unreadable_manifest_does_not_retry(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text("{not json")
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.UNCLASSIFIABLE
        assert not outcome.verdict.may_retry

    def test_a_missing_manifest_does_not_retry(self, tmp_path: Path) -> None:
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=tmp_path / "absent.json",
        )
        assert outcome.verdict is Verdict.UNCLASSIFIABLE

    def test_nonzero_exit_with_nothing_failed_does_not_retry(
        self,
        tmp_path: Path,
    ) -> None:
        """Merge-pending / contract failure: resumable, but not by us."""
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "completed")])
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.UNCLASSIFIABLE
        assert "no failed component" in outcome.reason

    def test_exit_two_with_a_spec_blocker_marker_is_a_spec_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """The architect halted on a blocker; re-running spends the same."""
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(
                returncode=2,
                output_tail="error: blockers found\nSpec issues written to: /x.md\n",
            ),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.SPEC_FAILURE
        assert "architect" in outcome.reason

    def test_exit_two_from_lock_contention_retries(
        self,
        tmp_path: Path,
    ) -> None:
        """#186 F6: exit 2 also means "another run holds the lock".

        A pre-launch probe cannot distinguish the two - it releases the
        lock, so a manual factory can take it in the gap. The child's own
        output can.
        """
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(
                returncode=2,
                output_tail="another factory run holds the lock; --force-lock\n",
            ),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.RETRY_INFRA
        assert outcome.evidence["cause"] == "lock_contention"

    def test_exit_two_with_no_marker_is_unclassifiable(
        self,
        tmp_path: Path,
    ) -> None:
        """Neither refusal named: refuse to guess which one it was."""
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=2, output_tail="something else entirely"),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.UNCLASSIFIABLE
        assert not outcome.verdict.may_retry

    def test_a_manifest_this_invocation_does_not_own_is_unclassifiable(
        self,
        tmp_path: Path,
    ) -> None:
        """#186 F2: never classify from another run's artifacts."""
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=None,
        )
        assert outcome.verdict is Verdict.UNCLASSIFIABLE
        assert "no run artifacts of its own" in outcome.reason

    def test_a_signal_kill_retries(self, tmp_path: Path) -> None:
        """SIGKILL is evidence of an external cause, not a spec verdict."""
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=-9),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.RETRY_INFRA
        assert "signal 9" in outcome.reason

    def test_a_timeout_retries(self, tmp_path: Path) -> None:
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=-9, timed_out=True),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.RETRY_INFRA
        assert "timeout" in outcome.reason or "timed" in outcome.reason.lower()

    def test_a_launch_failure_retries(self, tmp_path: Path) -> None:
        """Nothing was spent, so retrying is free."""
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=-1, launch_error="No such file"),
            manifest_path=tmp_path / "m.json",
        )
        assert outcome.verdict is Verdict.RETRY_INFRA
        assert "before any spend" in outcome.reason

    def test_only_retry_infra_authorizes_spending(self) -> None:
        assert Verdict.RETRY_INFRA.may_retry
        assert not Verdict.SUCCESS.may_retry
        assert not Verdict.SPEC_FAILURE.may_retry
        assert not Verdict.UNCLASSIFIABLE.may_retry

    def test_every_verdict_carries_a_reason(self, tmp_path: Path) -> None:
        """A machine decision that spends money must say why."""
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [_infra_finding()])])
        for run in (
            RunOutcome(returncode=0),
            RunOutcome(returncode=1),
            RunOutcome(returncode=2),
            RunOutcome(returncode=-9),
            RunOutcome(returncode=-9, timed_out=True),
            RunOutcome(returncode=-1, launch_error="boom"),
        ):
            outcome = classify_run(tmp_path, run=run, manifest_path=path)
            assert outcome.reason.strip()

    def test_the_shared_infra_predicate_is_reused(self, tmp_path: Path) -> None:
        """Not a second copy of factory._infra_casualty.

        Two copies of this rule drifting apart is how a spec failure
        becomes retryable, so the classifier must go through
        Finding.is_infrastructure_error rather than string-matching.
        """
        from kstrl.findings import Finding
        from kstrl.serve import _infra_casualty

        class _Comp:
            def __init__(self, findings: list[Finding]) -> None:
                self.findings = findings

        assert _infra_casualty(_Comp([Finding.infrastructure_error("review", "cli died")]))
        assert not _infra_casualty(_Comp([]))


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------


class TestBackoff:
    def test_grows_exponentially(self) -> None:
        assert backoff_seconds(1) == 60.0
        assert backoff_seconds(2) == 120.0
        assert backoff_seconds(3) == 240.0

    def test_is_capped(self) -> None:
        assert backoff_seconds(50) == BACKOFF_CAP_SECONDS

    def test_zero_attempts_has_no_delay(self) -> None:
        assert backoff_seconds(0) == 0.0

    def test_an_item_inside_its_backoff_is_not_claimed(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        queue.transition(
            item,
            ItemState.LEASED,
            reason="t",
            not_before=future,  # type: ignore[arg-type]
        )
        queue.requeue(queue.items()[0], not_before=future)
        assert queue.next_ready() is None

    def test_a_backed_off_item_does_not_starve_the_others(
        self,
        tmp_path: Path,
    ) -> None:
        """A flaking item must not block the ones that would succeed."""
        queue = _queue(tmp_path)
        stuck = _add(queue, title="stuck", priority=9)
        fresh = _add(queue, title="fresh")
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        queue.requeue(
            queue.lease(stuck),  # type: ignore[arg-type]
            not_before=future,
        )
        ready = queue.next_ready()
        assert ready is not None
        assert ready.item_id == fresh.item_id  # type: ignore[attr-defined]

    def test_an_unparseable_not_before_holds_off(self, tmp_path: Path) -> None:
        """Err away from launching: the opposite of the lease default."""
        queue = _queue(tmp_path)
        item = _add(queue)
        queue.requeue(queue.lease(item), not_before="not-a-date")  # type: ignore[arg-type]
        assert queue.next_ready() is None


# --------------------------------------------------------------------------
# Spend ledger and the budget
# --------------------------------------------------------------------------


class TestSpendLedger:
    def test_a_fresh_day_starts_at_zero(self, tmp_path: Path) -> None:
        assert SpendLedger(tmp_path).read("2026-07-30").spent_usd == 0.0

    def test_charges_accumulate(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(1.50, covered_calls=1, total_calls=1, today="2026-07-30")
        spend = ledger.charge(2.25, covered_calls=1, total_calls=1, today="2026-07-30")
        assert spend.spent_usd == pytest.approx(3.75)
        assert spend.runs == 2

    def test_a_new_day_resets(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(10.0, covered_calls=1, total_calls=1, today="2026-07-30")
        assert ledger.read("2026-07-31").spent_usd == 0.0

    def test_a_floor_stays_a_floor_for_the_day(self, tmp_path: Path) -> None:
        """Once any run under-reported, the day's total is a floor.

        Derived from accumulated CALL COUNTS rather than a sticky flag, so
        it cannot disagree with the counts it is meant to summarise.
        """
        ledger = SpendLedger(tmp_path)
        ledger.charge(1.0, covered_calls=1, total_calls=3, today="d")
        spend = ledger.charge(1.0, covered_calls=2, total_calls=2, today="d")
        assert spend.lower_bound
        assert spend.uncovered_calls == 2
        assert spend.covered_calls == 3
        assert spend.total_calls == 5

    def test_an_unmetered_phase_makes_the_total_a_floor(
        self,
        tmp_path: Path,
    ) -> None:
        """#186 F3: the architect spends and reports nothing at all."""
        ledger = SpendLedger(tmp_path)
        spend = ledger.charge(
            1.0,
            covered_calls=2,
            total_calls=2,
            unmetered_phases=("architect",),
            today="d",
        )
        assert spend.uncovered_calls == 0
        assert spend.lower_bound, "an unmetered phase is unmeasured spend"
        assert "architect" in spend.unmetered_phases

    def test_a_launch_failure_is_not_counted_as_a_metered_run(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = SpendLedger(tmp_path)
        spend = ledger.charge(0.0, metered_run=False, today="d")
        assert spend.runs == 0

    def test_negative_charges_are_ignored(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        assert ledger.charge(-5.0, covered_calls=1, total_calls=1, today="d").spent_usd == 0.0

    def test_a_corrupt_ledger_fails_closed(self, tmp_path: Path) -> None:
        """#186 F4: reading it as a fresh day disabled the only queue-wide cap.

        Reproduced in review: charge $9, corrupt the file, set a $5
        budget, and another run was allowed - indefinitely, because the
        per-item backstops do not bound spend across distinct items.
        """
        ledger = SpendLedger(tmp_path)
        ledger.path.parent.mkdir(parents=True, exist_ok=True)
        ledger.path.write_text("{not json")
        with pytest.raises(ServeStateError, match="malformed"):
            ledger.read("d")

    def test_an_unreadable_ledger_fails_closed(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(9.0, covered_calls=1, total_calls=1)
        with patch.object(
            Path,
            "read_text",
            side_effect=PermissionError("denied"),
        ):
            with pytest.raises(ServeStateError, match="cannot read"):
                ledger.read()

    def test_a_missing_ledger_is_the_first_run(self, tmp_path: Path) -> None:
        """FileNotFoundError is the ONE read failure that is not a fault."""
        assert SpendLedger(tmp_path).read("d").spent_usd == 0.0

    def test_a_corrupt_ledger_blocks_the_budget_gate(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(9.0, covered_calls=1, total_calls=1)
        ledger.path.write_text("{corrupt")
        with pytest.raises(ServeStateError):
            check_budget(ledger, ServeConfig(daily_budget_usd=5.0))

    def test_round_trip(self) -> None:
        spend = DailySpend("d", 1.5, 2, 3, 5, ("architect",))
        assert DailySpend.from_dict(spend.to_dict()) == spend

    def test_non_numeric_fields_decode_to_zero(self) -> None:
        spend = DailySpend.from_dict({"date": "d", "spent_usd": "lots"})
        assert spend.spent_usd == 0.0

    def test_a_legacy_payload_without_call_counts_decodes(self) -> None:
        spend = DailySpend.from_dict({"date": "d", "spent_usd": 2.0, "runs": 1})
        assert spend.covered_calls == 0
        assert spend.total_calls == 0
        assert not spend.has_any_coverage


class TestBudgetGate:
    def test_an_unset_budget_never_blocks(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(1000.0, covered_calls=1, total_calls=1, today="d")
        assert check_budget(ledger, ServeConfig(), today="d").allowed

    def test_under_budget_is_allowed(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(5.0, covered_calls=1, total_calls=1, today="d")
        config = ServeConfig(daily_budget_usd=20.0)
        assert check_budget(ledger, config, today="d").allowed

    def test_at_budget_blocks_and_pauses(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(20.0, covered_calls=1, total_calls=1, today="d")
        config = ServeConfig(daily_budget_usd=20.0)
        admission = check_budget(ledger, config, today="d")
        assert not admission.allowed
        assert admission.pause_reason
        assert admission.resume_after, "the pause must clear itself"

    def test_the_pause_targets_the_next_local_midnight(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(20.0, covered_calls=1, total_calls=1, today="d")
        admission = check_budget(
            ledger,
            ServeConfig(daily_budget_usd=20.0),
            today="d",
        )
        deadline = datetime.fromisoformat(admission.resume_after)
        assert deadline > datetime.now(UTC)
        assert deadline <= datetime.now(UTC) + timedelta(days=1, minutes=1)

    def test_a_floor_total_is_labelled_in_the_reason(
        self,
        tmp_path: Path,
    ) -> None:
        """H4: the operator must not read a floor as a measurement."""
        ledger = SpendLedger(tmp_path)
        ledger.charge(20.0, covered_calls=1, total_calls=5, today="d")
        admission = check_budget(
            ledger,
            ServeConfig(daily_budget_usd=20.0),
            today="d",
        )
        assert "FLOOR" in admission.reason
        assert "4 call(s) reported no cost" in admission.reason

    def test_next_local_midnight_is_in_the_future(self) -> None:
        assert datetime.fromisoformat(next_local_midnight()) > datetime.now(UTC)


class TestCostCoverageGate:
    """A budget over a cost-blind adapter can never fire.

    Three cases, and only the third is unenforceable: full coverage (the
    cap is exact), PARTIAL coverage (a lower-bound cap - it fires late,
    which is still a real bound), and ZERO coverage (the total stays $0
    forever). #186 F9: the gate used to test whether dollars were
    positive, so a fully-metered $0 run was rejected with a false message.
    """

    def test_no_budget_means_no_coverage_requirement(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(0.0, covered_calls=0, total_calls=4, today="d")
        assert check_cost_coverage(ledger, ServeConfig(), today="d").allowed

    def test_a_fully_covered_zero_dollar_run_is_allowed(
        self,
        tmp_path: Path,
    ) -> None:
        """The false-positive #186 F9 reported."""
        ledger = SpendLedger(tmp_path)
        ledger.charge(0.0, covered_calls=3, total_calls=3, today="d")
        config = ServeConfig(daily_budget_usd=10.0)
        assert check_cost_coverage(ledger, config, today="d").allowed

    def test_full_coverage_is_allowed(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.charge(2.0, covered_calls=2, total_calls=2, today="d")
        config = ServeConfig(daily_budget_usd=10.0)
        assert check_cost_coverage(ledger, config, today="d").allowed

    def test_partial_coverage_is_allowed_as_a_lower_bound_cap(
        self,
        tmp_path: Path,
    ) -> None:
        """A cap that fires late is still a cap; it is labelled a floor."""
        ledger = SpendLedger(tmp_path)
        ledger.charge(2.0, covered_calls=1, total_calls=5, today="d")
        config = ServeConfig(daily_budget_usd=10.0)
        assert check_cost_coverage(ledger, config, today="d").allowed
        assert ledger.read("d").lower_bound

    def test_zero_coverage_blocks_BEFORE_the_first_run(
        self,
        tmp_path: Path,
    ) -> None:
        """#186 F8: this used to be discovered only after a run had spent."""
        ledger = SpendLedger(tmp_path)
        config = ServeConfig(daily_budget_usd=10.0)
        admission = check_cost_coverage(ledger, config, today="d")
        assert not admission.allowed
        assert "can never fire" in admission.reason

    def test_coverage_once_seen_persists(self, tmp_path: Path) -> None:
        """The flag is not a daily fact; capability does not reset nightly."""
        ledger = SpendLedger(tmp_path)
        ledger.charge(1.0, covered_calls=1, total_calls=1, today="2026-07-30")
        config = ServeConfig(daily_budget_usd=10.0)
        assert check_cost_coverage(ledger, config, today="2026-07-31").allowed

    def test_the_reason_does_not_estimate_the_missing_spend(
        self,
        tmp_path: Path,
    ) -> None:
        """Never convert unreported calls into a dollar figure (H4)."""
        admission = check_cost_coverage(
            SpendLedger(tmp_path),
            ServeConfig(daily_budget_usd=10.0),
            today="d",
        )
        assert "NOT estimated" in admission.reason

    def test_the_override_is_explicit(self, tmp_path: Path) -> None:
        config = ServeConfig(daily_budget_usd=10.0, allow_uncovered_cost=True)
        assert check_cost_coverage(
            SpendLedger(tmp_path),
            config,
            today="d",
        ).allowed


# --------------------------------------------------------------------------
# The poison breaker
# --------------------------------------------------------------------------


class TestPoisonBreaker:
    """#186 F5: the streak is authoritative state, not journal narration."""

    def test_no_poison_no_streak(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.record_terminal(poisoned=False)
        assert consecutive_poison_count(ledger) == 0

    def test_counts_a_trailing_run_of_poison(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        for _ in range(3):
            ledger.record_terminal(poisoned=True)
        assert consecutive_poison_count(ledger) == 3

    def test_a_success_resets_the_streak(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.record_terminal(poisoned=True)
        ledger.record_terminal(poisoned=True)
        ledger.record_terminal(poisoned=False)
        assert consecutive_poison_count(ledger) == 0

    def test_the_streak_survives_losing_the_journal(
        self,
        tmp_path: Path,
    ) -> None:
        """The whole reason it moved off the journal.

        Queue._journal swallows append failures and journal_entries
        returns [] on any read error, so a streak derived from it read as
        zero and re-allowed spending while poisoned items sat on disk.
        """
        queue = _queue(tmp_path)
        ledger = SpendLedger(tmp_path)
        for _ in range(3):
            queue.poison(_add(queue), reason="bad")  # type: ignore[arg-type]
            ledger.record_terminal(poisoned=True)

        queue.journal_path.write_text("")
        assert consecutive_poison_count(ledger) == 3
        assert not check_poison_breaker(
            ledger,
            ServeConfig(max_consecutive_poison=3),
        ).allowed

    def test_the_streak_survives_a_new_day(self, tmp_path: Path) -> None:
        """A poison streak is not a daily fact; the spend is."""
        ledger = SpendLedger(tmp_path)
        for _ in range(2):
            ledger.record_terminal(poisoned=True)
        ledger.charge(1.0, covered_calls=1, total_calls=1, today="2026-07-30")
        assert ledger.read_state("2026-07-31").consecutive_poison == 2
        assert ledger.read_state("2026-07-31").spend.spent_usd == 0.0

    def test_the_breaker_blocks_at_the_limit(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        for _ in range(3):
            ledger.record_terminal(poisoned=True)
        admission = check_poison_breaker(
            ledger,
            ServeConfig(max_consecutive_poison=3),
        )
        assert not admission.allowed
        assert "systemic" in admission.pause_reason

    def test_the_breaker_allows_below_the_limit(self, tmp_path: Path) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.record_terminal(poisoned=True)
        assert check_poison_breaker(
            ledger,
            ServeConfig(max_consecutive_poison=3),
        ).allowed

    def test_the_breaker_pause_does_not_auto_resume(
        self,
        tmp_path: Path,
    ) -> None:
        """Unlike the budget: something systemic needs a human, not a clock."""
        ledger = SpendLedger(tmp_path)
        for _ in range(3):
            ledger.record_terminal(poisoned=True)
        assert check_poison_breaker(ledger, ServeConfig()).resume_after == ""

    def test_an_unreadable_state_file_fails_closed(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = SpendLedger(tmp_path)
        ledger.record_terminal(poisoned=True)
        ledger.path.write_text("{corrupt")
        with pytest.raises(ServeStateError):
            check_poison_breaker(ledger, ServeConfig())


# --------------------------------------------------------------------------
# The inbox open-item cap
# --------------------------------------------------------------------------


class TestInboxCapGate:
    """Issue #190: the open-item cap must fail CLOSED on garbled lines.

    The inbox fold skips unparseable lines by design (a torn tail must
    not make the backlog invisible), but a skipped emission line
    undercounts open items, so a cap sitting on the tolerant fold admits
    work past N. The gate therefore counts every unparseable line as an
    open item; the tolerant display path is untouched.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("KSTRL_INBOX_ENABLED", "KSTRL_INBOX_OPEN_CAP"):
            monkeypatch.delenv(var, raising=False)

    def _inbox(self, tmp_path: Path, cap: int) -> Inbox:
        (tmp_path / "kstrl.toml").write_text(f"[inbox]\nopen_item_cap = {cap}\n")
        return Inbox(tmp_path, InboxConfig.load(tmp_path))

    def test_open_items_below_the_cap_admit(self, tmp_path: Path) -> None:
        box = self._inbox(tmp_path, cap=2)
        box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        assert check_inbox_cap(tmp_path).allowed

    def test_a_garbled_line_counts_toward_the_cap(
        self,
        tmp_path: Path,
    ) -> None:
        """Same backlog as above plus one torn line: admission refused."""
        box = self._inbox(tmp_path, cap=2)
        box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        with box.path.open("a", encoding="utf-8") as handle:
            handle.write('{"id": "torn-emission", "kind": "halted_r\n')
        admission = check_inbox_cap(tmp_path)
        assert not admission.allowed
        assert "unparseable" in admission.reason
        # the display fold stays tolerant: the torn line is invisible there
        assert len(box.open_items()) == 1

    def test_garbled_lines_alone_can_fill_the_cap(
        self,
        tmp_path: Path,
    ) -> None:
        """No readable open items at all - the fold reads an empty inbox -
        yet the gate must refuse: every torn line MIGHT be an open item."""
        box = self._inbox(tmp_path, cap=1)
        box.path.parent.mkdir(parents=True, exist_ok=True)
        box.path.write_text("{not json\n", encoding="utf-8")
        admission = check_inbox_cap(tmp_path)
        assert not admission.allowed
        assert box.open_items() == []

    def test_unreadable_inbox_refuses_regardless_of_cap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Review P1: collapsing a whole-file read failure to one skipped
        line admitted under the default cap of 50. Unreadable is its own
        state - deny independently of open_item_cap."""
        box = self._inbox(tmp_path, cap=50)
        box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        real_read_bytes = Path.read_bytes

        def flaky_read(self: Path) -> bytes:
            if self == box.path:
                raise OSError("EIO")
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", flaky_read)
        admission = check_inbox_cap(tmp_path)
        assert not admission.allowed
        assert "unreadable" in admission.reason

    def test_invalid_utf8_refuses_admission(
        self,
        tmp_path: Path,
    ) -> None:
        """Review P1: ``read_text`` raised UnicodeDecodeError on a torn
        multibyte write, escaping the gate entirely."""
        box = self._inbox(tmp_path, cap=50)
        box.path.parent.mkdir(parents=True, exist_ok=True)
        box.path.write_bytes(b"\xff\n")
        admission = check_inbox_cap(tmp_path)
        assert not admission.allowed
        assert "unreadable" in admission.reason

    def test_gate_uses_one_scan_snapshot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Review P1: unparseable_line_count() and open_items() each
        scanned the log. A torn append between those calls produced
        garbled=0 then open=1, so at cap 2 the gate admitted. One
        snapshot keeps the counts consistent; the gate must not re-scan.
        """
        box = self._inbox(tmp_path, cap=2)
        box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        with box.path.open("a", encoding="utf-8") as handle:
            handle.write('{"id": "torn-between-scans", "kind": "halted_r\n')

        scans = 0
        real_scan = Inbox.scan

        def counted_scan(self: Inbox) -> object:
            nonlocal scans
            scans += 1
            return real_scan(self)

        monkeypatch.setattr(Inbox, "scan", counted_scan)
        admission = check_inbox_cap(tmp_path)
        assert not admission.allowed
        assert scans == 1

    def test_interleaved_append_cannot_split_gate_counts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Deterministic reproduction of the dual-scan race: after the
        first scan returns, inject a torn line. A second scan would see
        a different world; the gate must decide from the first snapshot
        alone (and therefore must not call scan again).
        """
        box = self._inbox(tmp_path, cap=2)
        box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        # File currently: 1 open. Cap 2. A torn line mid-gate would tip
        # a dual-scan world into open=1 + garbled=0 (admit) if unparseable
        # ran first on the clean file, then open_items ran after inject.

        scans = 0
        real_scan = Inbox.scan

        def inject_after_first(self: Inbox) -> object:
            nonlocal scans
            scans += 1
            snap = real_scan(self)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write('{"id": "injected-after-scan", "kind": "h\n')
            return snap

        monkeypatch.setattr(Inbox, "scan", inject_after_first)
        admission = check_inbox_cap(tmp_path)
        # Snapshot was clean (1 open, 0 garbled) → under cap → admit.
        # The inject is visible to the NEXT cycle, not this one.
        assert admission.allowed
        assert scans == 1
        # And the next evaluation sees the torn line and refuses.
        monkeypatch.setattr(Inbox, "scan", real_scan)
        assert not check_inbox_cap(tmp_path).allowed


# --------------------------------------------------------------------------
# The lease reaper
# --------------------------------------------------------------------------


class TestReaper:
    def test_a_dead_leased_item_returns_to_queued_for_free(
        self,
        tmp_path: Path,
    ) -> None:
        """Leasing spends nothing, so recovery costs nothing."""
        queue = _queue(tmp_path)
        item = queue.lease(_add(queue), pid=999999)  # type: ignore[arg-type]
        result = reap_leases(queue)
        assert item.item_id in result.requeued
        reread = queue.items()[0]
        assert reread.state is ItemState.QUEUED
        assert reread.attempts == 0

    def test_a_live_lease_is_left_alone(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.lease(_add(queue), pid=os.getpid())  # type: ignore[arg-type]
        result = reap_leases(queue)
        assert result.requeued == ()
        assert queue.items()[0].state is ItemState.LEASED

    def test_an_expired_lease_is_reaped_even_with_a_live_pid(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path, lease_ttl_seconds=1)
        queue.lease(_add(queue), pid=os.getpid())  # type: ignore[arg-type]
        future = datetime.now(UTC) + timedelta(hours=2)
        result = reap_leases(queue, now=future)
        assert len(result.requeued) == 1

    def test_a_dead_running_item_retries_with_backoff(
        self,
        tmp_path: Path,
    ) -> None:
        """The sleep/crash path: the attempt is spent, the cause is external."""
        queue = _queue(tmp_path, max_attempts=3)
        item = queue.start(queue.lease(_add(queue), pid=999999))  # type: ignore[arg-type]
        result = reap_leases(queue)
        assert item.item_id in result.failed_for_retry
        reread = queue.items()[0]
        assert reread.state is ItemState.QUEUED
        assert reread.attempts == 1, "the spent attempt stays charged"
        assert reread.not_before, "a reaped retry waits out a backoff"

    def test_a_dead_running_item_poisons_when_out_of_attempts(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path, max_attempts=1)
        item = queue.start(queue.lease(_add(queue), pid=999999))  # type: ignore[arg-type]
        result = reap_leases(queue)
        assert item.item_id in result.poisoned
        reread = queue.items()[0]
        assert reread.state is ItemState.POISON
        assert "no attempts left" in reread.poison_reason

    def test_a_foreign_host_lease_is_not_reaped_on_pid(
        self,
        tmp_path: Path,
    ) -> None:
        """We cannot probe a foreign pid; the TTL is the only signal."""
        queue = _queue(tmp_path)
        queue.lease(_add(queue), pid=999999, host="some-other-machine")  # type: ignore[arg-type]
        assert reap_leases(queue).requeued == ()

    def test_a_foreign_host_lease_still_expires(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, lease_ttl_seconds=1)
        queue.lease(_add(queue), pid=999999, host="some-other-machine")  # type: ignore[arg-type]
        future = datetime.now(UTC) + timedelta(hours=2)
        assert len(reap_leases(queue, now=future).requeued) == 1

    def test_the_reaper_records_why_in_the_journal(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        item = queue.lease(_add(queue), pid=999999)  # type: ignore[arg-type]
        reap_leases(queue)
        reasons = [e["reason"] for e in queue.journal_entries(item.item_id)]
        assert any("reaped" in r for r in reasons)


# --------------------------------------------------------------------------
# The merge gate must survive continuous intake
# --------------------------------------------------------------------------


class TestMergeGate:
    def test_stop_at_pr_without_the_ladder(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue, merge_disposition=MergeDisposition.STOP_AT_PR)
        gate = resolve_merge_gate(item, tmp_path)  # type: ignore[arg-type]
        assert gate.pause_before_pr_merge
        assert not gate.refusal

    def test_auto_merge_without_the_ladder(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue, merge_disposition=MergeDisposition.AUTO_MERGE)
        gate = resolve_merge_gate(item, tmp_path)  # type: ignore[arg-type]
        assert not gate.pause_before_pr_merge

    def test_the_ladder_withholds_auto_merge_at_l1(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ladder may always withhold a permission."""
        monkeypatch.setenv("KSTRL_AUTONOMY_ENABLED", "1")
        queue = _queue(tmp_path)
        item = _add(queue, merge_disposition=MergeDisposition.AUTO_MERGE)
        gate = resolve_merge_gate(item, tmp_path)  # type: ignore[arg-type]
        assert gate.pause_before_pr_merge
        assert not gate.refusal
        assert any("withholds" in note for note in gate.notes)

    def test_remote_items_default_to_stop_at_pr(self, tmp_path: Path) -> None:
        """Continuous intake must not silently delete the merge gate."""
        queue = _queue(tmp_path)
        item = _add(
            queue,
            source=ItemSource.GITHUB,
            source_ref="o/r#1",
        )
        gate = resolve_merge_gate(item, tmp_path)  # type: ignore[arg-type]
        assert gate.pause_before_pr_merge


# --------------------------------------------------------------------------
# caffeinate
# --------------------------------------------------------------------------


class TestCaffeinate:
    def test_disabled_yields_no_prefix(self) -> None:
        assert caffeinate_prefix(False) == []

    def test_non_darwin_yields_no_prefix(self) -> None:
        with patch("kstrl.serve.sys.platform", "linux"):
            assert caffeinate_prefix(True) == []

    def test_darwin_with_the_binary_uses_idle_only(self) -> None:
        with patch("kstrl.serve.sys.platform", "darwin"):
            with patch("kstrl.serve.shutil.which", return_value="/usr/bin/caffeinate"):
                assert caffeinate_prefix(True) == ["/usr/bin/caffeinate", "-i"]

    def test_a_missing_binary_degrades_rather_than_failing(self) -> None:
        with patch("kstrl.serve.sys.platform", "darwin"):
            with patch("kstrl.serve.shutil.which", return_value=None):
                assert caffeinate_prefix(True) == []


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class TestServeConfig:
    def test_defaults(self) -> None:
        config = ServeConfig()
        assert config.poll_interval_seconds == 60.0
        assert config.daily_budget_usd == 0.0
        assert config.max_consecutive_poison == 3
        assert config.caffeinate
        assert not config.allow_uncovered_cost

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"poll_interval_seconds": 0},
            {"daily_budget_usd": -1},
            {"max_consecutive_poison": 0},
            {"factory_timeout_seconds": -1},
        ],
    )
    def test_invalid_values_are_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ServeError):
            ServeConfig(**kwargs)  # type: ignore[arg-type]

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_SERVE_DAILY_BUDGET_USD", "25.5")
        monkeypatch.setenv("KSTRL_SERVE_CAFFEINATE", "0")
        config = ServeConfig.from_env()
        assert config.daily_budget_usd == 25.5
        assert not config.caffeinate

    def test_load_reads_the_toml_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[serve]\ndaily_budget_usd = 12.0\nmax_consecutive_poison = 5\n"
        )
        config = ServeConfig.load(tmp_path)
        assert config.daily_budget_usd == 12.0
        assert config.max_consecutive_poison == 5

    def test_env_beats_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[serve]\ndaily_budget_usd = 12.0\n")
        monkeypatch.setenv("KSTRL_SERVE_DAILY_BUDGET_USD", "3.0")
        assert ServeConfig.load(tmp_path).daily_budget_usd == 3.0


# --------------------------------------------------------------------------
# The singleton lock
# --------------------------------------------------------------------------


class TestServeLock:
    def test_a_second_daemon_is_refused(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        with serve_lock(tmp_path):
            with pytest.raises(ServeLockedError):
                with serve_lock(tmp_path):
                    pass

    def test_it_is_released_on_exit(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        with serve_lock(tmp_path):
            pass
        with serve_lock(tmp_path):
            pass

    def test_it_is_distinct_from_the_queue_mutex(self, tmp_path: Path) -> None:
        """`ks queue ls` must keep working while the daemon runs."""
        pytest.importorskip("fcntl")
        from kstrl.workqueue import queue_lock

        with serve_lock(tmp_path):
            with queue_lock(tmp_path):
                pass

    def test_it_records_the_holder_pid(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        from kstrl.serve import SERVE_LOCK_FILENAME
        from kstrl.workqueue import queue_root

        with serve_lock(tmp_path):
            content = (queue_root(tmp_path) / SERVE_LOCK_FILENAME).read_text()
        assert content.strip() == str(os.getpid())


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


class TestServeCycle:
    def test_an_empty_queue_does_nothing(self, tmp_path: Path) -> None:
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert result.ran_item == ""
        assert result.skipped == "nothing ready"

    def test_a_successful_item_finishes_done(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert result.verdict is Verdict.SUCCESS
        assert queue.items()[0].state is ItemState.DONE

    def test_one_item_per_cycle(self, tmp_path: Path) -> None:
        """Two factory runs on one repo is what factory.lock exists to stop."""
        queue = _queue(tmp_path)
        _add(queue)
        _add(queue)
        calls: list[dict[str, object]] = []
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        assert len(calls) == 1

    def test_a_spec_failure_poisons_without_retrying(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_spec_finding()])],
        )
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert result.verdict is Verdict.SPEC_FAILURE
        item = queue.items()[0]
        assert item.state is ItemState.POISON
        assert item.attempts == 1, "poisoned after ONE attempt, not three"

    def test_an_infra_failure_retries_with_backoff(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_infra_finding()])],
        )
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert result.verdict is Verdict.RETRY_INFRA
        item = queue.items()[0]
        assert item.state is ItemState.QUEUED
        assert item.not_before, "the retry waits out a backoff"

    def test_an_infra_failure_poisons_once_attempts_run_out(
        self,
        tmp_path: Path,
    ) -> None:
        """Even a legitimately retryable failure is bounded."""
        queue = _queue(tmp_path, max_attempts=1)
        _add(queue)
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_infra_finding()])],
        )
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        item = queue.items()[0]
        assert item.state is ItemState.POISON
        assert "no attempts left" in item.poison_reason

    def test_an_unclassifiable_failure_poisons(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert result.verdict is Verdict.UNCLASSIFIABLE
        assert queue.items()[0].state is ItemState.POISON

    def test_the_attempt_is_charged_before_the_run(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        seen: list[int] = []

        def runner(**kwargs: object) -> RunOutcome:
            seen.append(queue.items()[0].attempts)
            return RunOutcome(0)

        serve_cycle(tmp_path, runner=runner)  # type: ignore[arg-type]
        assert seen == [1], "the attempt must be on disk before any spend"

    def test_the_merge_gate_reaches_the_runner(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue, merge_disposition=MergeDisposition.STOP_AT_PR)
        calls: list[dict[str, object]] = []
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        assert calls[0]["pause_before_pr_merge"] is True

    def test_auto_merge_reaches_the_runner(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue, merge_disposition=MergeDisposition.AUTO_MERGE)
        calls: list[dict[str, object]] = []
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        assert calls[0]["pause_before_pr_merge"] is False

    def test_a_paused_queue_runs_nothing(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        queue.pause(reason="operator")
        calls: list[dict[str, object]] = []
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        assert calls == []
        assert "paused" in result.skipped

    def test_an_elapsed_pause_window_resumes_itself(
        self,
        tmp_path: Path,
    ) -> None:
        """What makes the daily-budget stop self-healing.

        Asserts the marker is actually CLEARED, not merely that it reads
        as inactive. An elapsed `resume_after` already makes
        `is_paused()` false on its own, so the weaker assertion passed
        with the `resume()` call removed and pinned nothing.
        """
        queue = _queue(tmp_path)
        _add(queue)
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        queue.pause(reason="budget", resume_after=past)
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert result.ran_item
        assert not queue.is_paused()
        assert queue.pause_state().paused is False, "the marker must be cleared"
        assert any(
            entry["to"] == "running" and entry["reason"] == "resumed"
            for entry in queue.journal_entries()
        ), "the resume must be journaled"

    def test_an_exhausted_budget_pauses_before_spending(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        SpendLedger(tmp_path).charge(50.0, covered_calls=1, total_calls=1)
        calls: list[dict[str, object]] = []
        result = serve_cycle(
            tmp_path,
            config=ServeConfig(daily_budget_usd=10.0, allow_uncovered_cost=True),
            runner=_stub_runner(RunOutcome(0), calls),
        )
        assert calls == [], "the budget must block BEFORE the run"
        assert result.paused
        assert queue.is_paused()

    def test_repeated_poisons_pause_the_queue_on_their_own(
        self,
        tmp_path: Path,
    ) -> None:
        """End to end: serve must RECORD each poison, not just read a count.

        Seeding the ledger directly cannot catch a missing
        record_terminal call, which is the whole mechanism.
        """
        queue = _queue(tmp_path, max_attempts=1)
        for _ in range(3):
            _add(queue)
        config = ServeConfig(max_consecutive_poison=3)
        for _ in range(3):
            serve_cycle(
                tmp_path,
                config=config,
                runner=_stub_runner(RunOutcome(1)),
            )
        assert consecutive_poison_count(SpendLedger(tmp_path)) == 3

        _add(queue)
        calls: list[dict[str, object]] = []
        result = serve_cycle(
            tmp_path,
            config=config,
            runner=_stub_runner(RunOutcome(0), calls),
        )
        assert calls == [], "the breaker must stop the fourth run"
        assert result.paused
        assert queue.is_paused()

    def test_the_poison_breaker_pauses_the_queue(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        ledger = SpendLedger(tmp_path)
        for _ in range(3):
            queue.poison(_add(queue), reason="bad")  # type: ignore[arg-type]
            ledger.record_terminal(poisoned=True)
        _add(queue)
        calls: list[dict[str, object]] = []
        result = serve_cycle(
            tmp_path,
            config=ServeConfig(max_consecutive_poison=3),
            runner=_stub_runner(RunOutcome(0), calls),
        )
        assert calls == []
        assert result.paused
        assert queue.is_paused()

    def test_a_held_factory_lock_waits_without_charging(
        self,
        tmp_path: Path,
    ) -> None:
        """Someone else owns the repo; that is not the item's fault."""
        queue = _queue(tmp_path)
        _add(queue)
        calls: list[dict[str, object]] = []
        with patch("kstrl.serve.factory_lock_held", return_value=True):
            result = serve_cycle(
                tmp_path,
                runner=_stub_runner(RunOutcome(0), calls),
            )
        assert calls == []
        assert "already holds" in result.skipped
        item = queue.items()[0]
        assert item.state is ItemState.QUEUED
        assert item.attempts == 0

    def test_the_cycle_reaps_before_admitting(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        stale = queue.lease(_add(queue), pid=999999)  # type: ignore[arg-type]
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert stale.item_id in result.reaped.requeued

    def test_the_cycle_sweeps_staging(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ensure_dirs()
        (queue.staging_path / "q-ghost").mkdir(parents=True)
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert result.swept_staging == 1

    def test_spend_is_charged_even_on_a_failure(self, tmp_path: Path) -> None:
        """A classification bug must not also lose the accounting."""
        queue = _queue(tmp_path)
        _add(queue)
        with patch(
            "kstrl.serve.read_run_spend",
            return_value=RunSpend(cost_usd=2.50, cost_calls=1, usage_calls=1),
        ):
            result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert result.charged_usd == pytest.approx(2.50)
        assert SpendLedger(tmp_path).read().spent_usd == pytest.approx(2.50)

    def test_a_poisoned_item_files_an_inbox_item(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert any(result.inbox_items)
        from kstrl.inbox import Inbox, InboxConfig

        items = Inbox(tmp_path, InboxConfig.load(tmp_path)).open_items()
        assert any("poisoned" in item.title for item in items)

    def test_a_full_inbox_stops_admitting_work(self, tmp_path: Path) -> None:
        """R8.3 documented open_item_cap as R8.6's backstop; this uses it."""
        queue = _queue(tmp_path)
        _add(queue)
        calls: list[dict[str, object]] = []
        with patch("kstrl.serve.check_inbox_cap") as gate:
            from kstrl.serve import Admission

            gate.return_value = Admission(allowed=False, reason="inbox full")
            result = serve_cycle(
                tmp_path,
                runner=_stub_runner(RunOutcome(0), calls),
            )
        assert calls == []
        assert result.skipped == "inbox full"

    def test_priority_order_is_honored(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue, title="low")
        _add(queue, title="urgent", priority=9)
        calls: list[dict[str, object]] = []
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        done = [i for i in queue.items() if i.state is ItemState.DONE]
        assert done[0].title == "urgent"


class TestServeLoop:
    def test_once_runs_a_single_cycle(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        _add(queue)
        results = serve(tmp_path, once=True, runner=_stub_runner(RunOutcome(0)))
        assert len(results) == 1
        assert len([i for i in queue.items() if i.state is ItemState.DONE]) == 1

    def test_max_cycles_bounds_the_loop(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        for _ in range(3):
            _add(queue)
        slept: list[float] = []
        results = serve(
            tmp_path,
            runner=_stub_runner(RunOutcome(0)),
            max_cycles=3,
            sleeper=slept.append,
        )
        assert len(results) == 3
        assert len([i for i in queue.items() if i.state is ItemState.DONE]) == 3

    def test_the_loop_sleeps_between_cycles(self, tmp_path: Path) -> None:
        slept: list[float] = []
        serve(
            tmp_path,
            config=ServeConfig(poll_interval_seconds=42.0),
            runner=_stub_runner(RunOutcome(0)),
            max_cycles=2,
            sleeper=slept.append,
        )
        assert slept == [42.0]

    def test_once_does_not_sleep(self, tmp_path: Path) -> None:
        slept: list[float] = []
        serve(
            tmp_path,
            once=True,
            runner=_stub_runner(RunOutcome(0)),
            sleeper=slept.append,
        )
        assert slept == []

    def test_the_loop_holds_the_singleton_lock(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        with serve_lock(tmp_path):
            with pytest.raises(ServeLockedError):
                serve(tmp_path, once=True, runner=_stub_runner(RunOutcome(0)))


class TestProcessGroupSupervision:
    """#186 F1: a timeout must reap the whole tree, not the direct child."""

    def test_killing_only_the_direct_child_leaks_a_descendant(self) -> None:
        """Establishes WHY the runner does not use subprocess.run(timeout=).

        That helper signals only its DIRECT child. On macOS the direct
        child is the caffeinate wrapper, so the factory is a grandchild
        and survives - after which the daemon would requeue the item
        while a factory was still writing to the repo.
        """
        import subprocess

        # The script prints its grandchild's pid so the assertion names a
        # specific process rather than probing the group (whose leader we
        # are deliberately killing).
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 20 & echo $! ; sleep 20"],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        grandchild = int(proc.stdout.readline().strip())
        pgid = os.getpgid(proc.pid)
        leaked = False
        try:
            proc.terminate()  # what subprocess.run's timeout does
            proc.wait(timeout=10)
            try:
                os.kill(grandchild, 0)
                leaked = True
            except (ProcessLookupError, OSError):
                leaked = False
        finally:
            terminate_process_group(proc, pgid)
            try:
                os.kill(grandchild, 9)
            except (ProcessLookupError, OSError):
                pass
        assert leaked, (
            "killing only the direct child should leave the grandchild "
            "running - the reason the runner signals the whole group"
        )

    def test_terminate_process_group_reaps_the_tree(self) -> None:
        import subprocess

        proc = subprocess.Popen(
            ["sh", "-c", "sh -c 'sleep 20' & sleep 20"],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        pgid = os.getpgid(proc.pid)
        assert terminate_process_group(proc).reaped is True
        assert not process_group_alive(pgid)

    def test_a_mocked_popen_never_signals_a_broad_group(self) -> None:
        """killpg(1, sig) is kill(-1, sig): every process this user owns.

        The guard itself moved to `procgroup.safe_pgid` in #308 and is
        unit-tested there. What stays here is the half that move cannot
        prove: that THIS caller still routes through it, so a mocked Popen
        reaching `terminate_process_group` signals nothing at all.

        THE SPY IS LOAD-BEARING, not decoration. Round-1 review deleted
        the `safe_pgid(process)` call from `terminate_process_group` and
        this test stayed green: with no lookup, `pgid` is None, the
        function takes its no-pgid branch, and "nothing was signalled" is
        true for the wrong reason. Asserting the guard was ASKED is the
        only thing here that tells the two apart. Complete removal of
        signalling is still caught next door by the real-tree tests,
        so this was never silent overall - but this test did not pin
        what its name says.
        """
        fake = MagicMock()
        with (
            patch("kstrl.serve.safe_pgid", wraps=safe_pgid) as guard,
            patch("kstrl.serve.os.killpg") as killpg,
        ):
            # A plausible group, so the pgid guard cannot be what rejects
            # it and only the isinstance check can.
            with patch("kstrl.serve.os.getpgid", return_value=99999):
                fake.poll.return_value = 0
                assert terminate_process_group(fake).reaped is True
            guard.assert_called_once_with(fake)
            assert killpg.call_count == 0, "must never signal a bogus group"

    # The own-group half of this routing question is
    # `TestARefusedSignalIsNotAnUnknown::test_a_missing_pgid_is_reported_as_unmeasured`,
    # which builds the identical fake and asserts the identical outcome
    # WITHOUT patching `killpg`. That makes it the stronger of the two, so
    # #308 deleted the copy that used to live here rather than keep both.

    def test_the_supervised_timeout_reaps_descendants(
        self,
        tmp_path: Path,
    ) -> None:
        """The runner's OWN timeout path, not just the helper.

        subprocess_factory_runner hardcodes `ks factory` as its argv, so
        the supervision is split into run_supervised precisely so this
        path is reachable without spawning a factory.
        """
        outcome = run_supervised(
            ["sh", "-c", "sleep 30 & echo started ; sleep 30"],
            cwd=tmp_path,
            timeout_seconds=1.0,
        )
        assert outcome.timed_out
        assert outcome.group_reaped, "the timeout must confirm the whole group is gone"

    def test_the_timeout_path_uses_the_pgid_captured_at_spawn(
        self,
        tmp_path: Path,
    ) -> None:
        """Wiring, not behaviour.

        On the timeout path the child is still alive, so a lazy pgid
        lookup would work too - the captured value matters for the
        already-reaped case, which cannot be provoked here. So assert the
        argument is threaded through rather than pretending the outcome
        differs.
        """
        seen: list[int | None] = []
        real = terminate_process_group

        def spy(process: object, pgid: int | None = None) -> bool:
            seen.append(pgid)
            return real(process, pgid)  # type: ignore[arg-type]

        with patch("kstrl.serve.terminate_process_group", side_effect=spy):
            run_supervised(
                ["sh", "-c", "sleep 30"],
                cwd=tmp_path,
                timeout_seconds=1.0,
            )
        assert seen and seen[0] is not None and seen[0] > 1, (
            "the pgid captured at spawn must be passed to the killer"
        )

    def test_the_supervised_run_returns_the_exit_code_and_output(
        self,
        tmp_path: Path,
    ) -> None:
        outcome = run_supervised(
            ["sh", "-c", "echo hello; exit 3"],
            cwd=tmp_path,
        )
        assert outcome.returncode == 3
        assert "hello" in outcome.output_tail
        assert not outcome.timed_out

    def test_a_failed_launch_is_reported_not_raised(
        self,
        tmp_path: Path,
    ) -> None:
        outcome = run_supervised(
            [str(tmp_path / "no-such-binary")],
            cwd=tmp_path,
        )
        assert outcome.launch_error
        assert outcome.returncode == -1

    def test_the_supervised_run_reports_the_child_pid(
        self,
        tmp_path: Path,
    ) -> None:
        seen: list[int] = []
        run_supervised(
            ["sh", "-c", "exit 0"],
            cwd=tmp_path,
            on_spawn=seen.append,
        )
        assert seen and seen[0] > 1

    def test_the_group_is_signalled_not_just_the_direct_child(self) -> None:
        """Precise: assert killpg is what fires, whatever the shell does."""
        import subprocess

        proc = subprocess.Popen(
            ["sh", "-c", "sleep 20 & sleep 20"],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        pgid = os.getpgid(proc.pid)
        real_killpg = os.killpg
        signalled: list[tuple[int, int]] = []

        def recording_killpg(target: int, sig: int) -> None:
            # signal 0 is a liveness PROBE, not a kill; counting it would
            # let "never signal the group" pass, since a probe runs
            # either way. The filter is NOT redundant now the probe lives
            # in kstrl.procgroup: `os` is one shared module object, so
            # patching kstrl.serve.os.killpg patches procgroup's too.
            # Measured - under this patch, procgroup.signal_probe_alive(4242)
            # records calls == [(4242, 0)]. Drop the filter and on any run
            # where ps is blind the probe's (pgid, 0) satisfies
            # "the group must actually be signalled" on its own.
            if sig != 0:
                signalled.append((target, sig))
            real_killpg(target, sig)

        try:
            with patch("kstrl.serve.os.killpg", side_effect=recording_killpg):
                reaped = terminate_process_group(proc, pgid).reaped
        finally:
            if process_group_alive(pgid):
                real_killpg(pgid, 9)
        assert signalled, "the group must actually be signalled"
        assert all(target == pgid for target, _ in signalled)
        assert reaped, "and the group must be confirmed gone"

    def test_an_already_dead_child_counts_as_reaped(self) -> None:
        import subprocess

        proc = subprocess.Popen(
            ["sh", "-c", "exit 0"],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        proc.wait(timeout=10)
        assert terminate_process_group(proc).reaped is True

    def test_an_unreaped_timeout_poisons_instead_of_retrying(
        self,
        tmp_path: Path,
    ) -> None:
        """The whole point: never hand the item back while a run may live.

        A timed-out run whose group could not be confirmed dead is NOT a
        retryable infrastructure failure - retrying it would put a second
        factory on a repo the first may still be writing to.
        """
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        result = serve_cycle(
            tmp_path,
            runner=_stub_runner(
                RunOutcome(returncode=-9, timed_out=True, group_reaped=False),
            ),
        )
        item = queue.items()[0]
        assert item.state is ItemState.POISON
        assert "could not be confirmed reaped" in item.poison_reason
        assert result.needs_human

    def test_a_cleanly_reaped_timeout_retries(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        result = serve_cycle(
            tmp_path,
            runner=_stub_runner(
                RunOutcome(returncode=-9, timed_out=True, group_reaped=True),
            ),
        )
        assert result.verdict is Verdict.RETRY_INFRA
        assert queue.items()[0].state is ItemState.QUEUED

    def test_the_run_child_becomes_the_lease_owner(
        self,
        tmp_path: Path,
    ) -> None:
        """Otherwise a successor reaper requeues a live run (#186 F1)."""
        queue = _queue(tmp_path)
        _add(queue)
        serve_cycle(
            tmp_path,
            runner=_stub_runner(RunOutcome(0), child_pid=424242),
        )
        adopted = [e for e in queue.journal_entries() if e["reason"].startswith("lease adopted")]
        assert adopted, "the child pid must be recorded as the lease owner"
        assert adopted[0]["detail"]["lease_pid"] == 424242


class TestGroupLivenessDegradesRatherThanGuessing:
    """#298: what the reap check does when `ps` gives no answer.

    Only the POLICY is here. The reading's tri-state, its parse and the
    conditions under which a "gone" is evidence are pinned in
    `tests/test_procgroup.py`, and the zombie case itself needs a real
    unreaped tree and lives in
    `tests/test_shutdown.py::test_a_zombie_does_not_count_as_a_live_member`.

    NOTHING HERE WARNS. An earlier version called `warnings.warn` on the
    degraded path, which under `PYTHONWARNINGS=error` raises out of the
    reap check and, measured, escaped `run_supervised`'s
    `except subprocess.TimeoutExpired` and `serve_cycle` alike, crashing
    the daemon on every timed-out run on a ps-less machine. The reason
    now travels as a value and is reported through the ServeObserver.
    """

    #: Every way `ps` can give no answer, and the id says which is which.
    #: The assertion is the same for all of them because the contract is:
    #: whatever went wrong, fall back rather than guess.
    #:
    #: `raises` holds FACTORIES, not exception instances. An instance
    #: built here at class-body evaluation lives for the session, and each
    #: `raise` appends a frame to its `__traceback__` and sets its
    #: `__context__`, so it would retain every test frame that ever raised
    #: it and their locals until interpreter exit.
    BLIND_PS = [
        pytest.param({"returncode": 127, "stderr": "ps: not found"}, id="nonzero_exit"),
        pytest.param(
            {"raises": lambda: FileNotFoundError(2, "no ps")},
            id="binary_absent",
        ),
        pytest.param(
            # TimeoutExpired is a SubprocessError, not an OSError, so it
            # takes a different branch from the one above.
            {"raises": lambda: subprocess.TimeoutExpired(cmd=["ps"], timeout=5.0)},
            id="wedged",
        ),
        pytest.param(
            # UnicodeDecodeError is a ValueError, which escapes a
            # fail-closed `except OSError` entirely.
            {"raises": lambda: UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")},
            id="undecodable",
        ),
        pytest.param(
            # rc=0, but no pid 1, so the view is filtered to our uid and a
            # member owned by another uid would be invisible.
            {"stdout": "  90 517 Ss\n"},
            id="filtered_output",
        ),
    ]

    @pytest.mark.parametrize("blind", BLIND_PS)
    def test_a_blind_ps_falls_back_to_the_signal_probe(
        self,
        monkeypatch: pytest.MonkeyPatch,
        blind: dict[str, object],
    ) -> None:
        """Positive control: the fallback must still see a live group."""
        procs.fake_ps(monkeypatch, **blind)  # type: ignore[arg-type]
        assert process_group_alive(os.getpgrp()) is True

    @pytest.mark.parametrize("blind", BLIND_PS)
    def test_a_blind_ps_never_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        blind: dict[str, object],
    ) -> None:
        """Under `-W error` a warning IS an exception, and this call sits
        inside `run_supervised`'s `except TimeoutExpired`, which does not
        catch UserWarning, under a `serve_cycle` that wraps nothing. So
        one timed-out run on a ps-less machine took the daemon down.
        `error` here turns any warning back into the crash it would be."""
        procs.fake_ps(monkeypatch, **blind)  # type: ignore[arg-type]
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert process_group_alive(os.getpgrp()) is True

    def test_the_fallback_still_reports_a_group_that_is_really_gone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The guard must not make absence unreportable, which would
        poison every timed-out run on a machine with no `ps`."""
        pgid = procs.dead_group()
        procs.fake_ps(monkeypatch, returncode=127, stderr="boom")
        assert process_group_alive(pgid) is False

    def test_a_blind_ps_and_an_unexplained_signal_error_is_not_reaped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#309 round 1, F1, at the level where it actually costs money.

        Both readings fail at once: `ps` cannot be trusted, so the answer
        falls back to the signal probe, and the probe's `killpg` fails
        with an errno POSIX does not define for signal 0. Before F1 that
        pair returned "nothing is running", which this function's own
        docstring explains is the direction that releases the item. It
        must report alive and carry a reason.

        Written against a group that IS running - our own - so a False
        here is the fail-open and never a true answer arrived at by luck.
        """
        procs.fake_ps(monkeypatch, returncode=127, stderr="ps: command not found")

        def broken(pgid: int, sig: int) -> None:
            raise OSError(5, "Input/output error")

        monkeypatch.setattr("kstrl.procgroup.os.killpg", broken)
        alive, degraded = _group_liveness_for_reap(os.getpgrp())
        assert alive is True, "an unexplained error must never read as reaped"
        assert degraded, "and the operator has to be told the answer was degraded"

    def test_the_degrade_reason_is_returned_not_warned(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A warning goes to stderr, which a detached daemon does not
        keep, and cannot be recorded. The reason has to come back as a
        value, carrying both what went wrong and which way the fallback
        is wrong."""
        procs.fake_ps(monkeypatch, returncode=127, stderr="ps: command not found")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            alive, degraded = _group_liveness_for_reap(os.getpgrp())
        assert alive is True
        assert "ps failed" in degraded, "the operator must learn ps was the cause"
        assert "zombie" in degraded, "and which way the fallback is wrong"

    def test_a_measured_answer_carries_no_degrade_reason(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The negative control: an empty string must mean "measured
        cleanly", or the poison reason gains a caveat on every run."""
        procs.fake_ps(monkeypatch, stdout=f"1 1 Ss\n50 {os.getpgrp()} Ss\n")
        assert _group_liveness_for_reap(os.getpgrp()) == (True, "")

    def test_a_working_ps_is_the_answer_and_the_probe_is_not_consulted(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ordering, so a later edit cannot quietly restore the probe as
        the primary and reintroduce #298.

        Scoped to the case where `ps` reports a RUNNING member. A `ps`
        that reports no row does consult `killpg`, deliberately: that is
        the kernel control that makes a "gone" evidence rather than a
        listing's silence.
        """
        calls: list[int] = []

        def recording_killpg(target: int, sig: int) -> None:
            calls.append(sig)

        procs.fake_ps(monkeypatch, stdout=f"1 1 Ss\n50 {os.getpgrp()} Ss\n")
        monkeypatch.setattr("kstrl.procgroup.os.killpg", recording_killpg)
        assert process_group_alive(os.getpgrp()) is True
        assert calls == [], "a listing that shows a runner settles it on its own"


class TestTheSignalEscalationIsDrivenByTheGroup:
    """#298 round 2: the SIGKILL leg was unreachable in the case that matters.

    `terminate_process_group` returned as soon as `process.wait()`
    returned, so the second leg only ran when the DIRECT CHILD outlived
    the grace period. A leader that dies on SIGTERM with a descendant
    holding SIG_IGN for it therefore never got SIGKILL: measured,
    `GroupTermination(reaped=False)` in 0.07s with the descendant still
    running. Nothing then kills it, and `serve_cycle` poisons and moves
    on, so a factory keeps writing to the repo forever.
    """

    STUBBORN = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "print('ready', flush=True);"
        "time.sleep(120)"
    )

    def test_a_descendant_that_ignores_sigterm_is_still_killed(self) -> None:
        proc = subprocess.Popen(
            ["sh", "-c", f'{sys.executable} -c "{self.STUBBORN}" & sleep 120'],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(proc.pid)
        try:
            # Let the stubborn child install its handler before signalling,
            # or the race decides the test rather than the code.
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not procs.group_has_live_member(pgid):
                time.sleep(0.05)

            result = terminate_process_group(proc, pgid)
            assert result.reaped is True, (
                "the group outlived SIGTERM, so SIGKILL had to follow; "
                "returning on the direct child's exit skips it entirely"
            )
            assert procs.wait_for_group_to_die(pgid), "a descendant was left running"
        finally:
            procs.kill_group(pgid)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_a_group_that_dies_on_sigterm_does_not_wait_out_the_grace(self) -> None:
        """The escalation must not cost 15s on the ordinary path."""
        proc = subprocess.Popen(
            ["sh", "-c", "sh -c 'sleep 20' & sleep 20"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(proc.pid)
        try:
            started = time.monotonic()
            assert terminate_process_group(proc, pgid).reaped is True
            assert time.monotonic() - started < GROUP_TERM_GRACE_SECONDS
        finally:
            procs.kill_group(pgid)


class TestARefusedSignalIsNotAnUnknown:
    """#298 round 2: EPERM is the strongest evidence a group IS occupied.

    The kernel found processes in that group and refused us. Feeding that
    into the same field as an unreadable `ps` made the operator-facing
    poison reason say "unknown rather than still running" in the one case
    where the group is definitely not empty and they must act.
    """

    def test_eperm_reports_occupied_not_degraded(self) -> None:
        proc = subprocess.Popen(
            ["sleep", "30"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(proc.pid)
        try:
            with patch(
                "kstrl.serve.os.killpg",
                side_effect=PermissionError(1, "Operation not permitted"),
            ):
                result = terminate_process_group(proc, pgid)
            assert result.reaped is False
            assert result.occupied, "a refused signal is positive evidence"
            assert "refused" in result.occupied
            assert result.degraded == "", "EPERM is not 'could not measure'; the kernel answered"
        finally:
            procs.kill_group(pgid)
            proc.wait(timeout=10)

    def test_the_poison_reason_does_not_soften_it_to_unknown(self) -> None:
        detail = _unreaped_timeout_detail(
            RunOutcome(
                returncode=-9,
                timed_out=True,
                group_reaped=False,
                group_occupied_detail="the kernel refused signal 15 to group 42",
            )
        )
        assert "CONFIRMED occupied" in detail
        assert "unknown" not in detail

    def test_a_missing_pgid_is_reported_as_unmeasured(self) -> None:
        """The other end of the same contract: `degraded` is empty only
        when the group was measured, and this path inspects no group."""
        fake = MagicMock(spec=subprocess.Popen)
        # Our own group, which `procgroup.safe_pgid` refuses. Nothing here
        # patches `killpg`, so this test is also the loudest proof the
        # guard is live: with the own-group check removed it issues a real
        # `killpg(<our pgid>, SIGTERM)` and kills the test runner (#308,
        # measured - the run died on signal 15 during this test).
        fake.pid = os.getpid()
        fake.poll.return_value = None
        result = terminate_process_group(fake)
        assert result.reaped is False
        assert "no process-group id" in result.degraded


class TestTheReapDegradeReachesTheDurableRecord:
    """#298 follow-up: the poison reason an operator actually reads.

    `serve_cycle` writes "a factory may still be executing against this
    repo" into the queue item, the ledger and the remote report. When the
    real cause was a blind reap check that sentence is a misdiagnosis.
    """

    def test_a_degraded_reap_says_so_in_the_poison_reason(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        serve_cycle(
            tmp_path,
            runner=_stub_runner(
                RunOutcome(
                    returncode=-9,
                    timed_out=True,
                    group_reaped=False,
                    group_reap_detail="ps failed (rc=127): 'not found'.",
                ),
            ),
        )
        item = queue.items()[0]
        assert item.state is ItemState.POISON
        assert "could not be confirmed reaped" in item.poison_reason
        assert "ps failed (rc=127)" in item.poison_reason, (
            "the recorded reason must say the check could not measure"
        )
        assert "unknown" in item.poison_reason

    def test_an_undegraded_reap_reason_gains_no_caveat(
        self,
        tmp_path: Path,
    ) -> None:
        """The negative control, or every poison reason grows a tail."""
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        serve_cycle(
            tmp_path,
            runner=_stub_runner(
                RunOutcome(returncode=-9, timed_out=True, group_reaped=False),
            ),
        )
        item = queue.items()[0]
        assert "could not be confirmed reaped" in item.poison_reason
        assert "could not measure" not in item.poison_reason

    def test_a_reaped_but_UNMEASURED_run_is_still_reported(
        self,
        tmp_path: Path,
    ) -> None:
        """The hole this closes. When `ps` is blind the fallback probe can
        answer "gone", the run is called reaped and the item is RELEASED
        for another attempt - the unsafe direction, on a doubly
        unverified answer. `serve_cycle` only ever read the detail inside
        `if not group_reaped`, so this was recorded nowhere at all.
        """
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        obs = _NullObserver()
        result = serve_cycle(
            tmp_path,
            observer=obs,
            runner=_stub_runner(
                RunOutcome(
                    returncode=-9,
                    timed_out=True,
                    group_reaped=True,
                    group_reap_detail="ps failed (rc=127): 'not found'.",
                ),
            ),
        )
        assert result.verdict is Verdict.RETRY_INFRA
        lines = "\n".join(obs.lines)
        assert "UNMEASURED" in lines, "releasing an item on an unmeasured 'gone' must not be silent"
        assert "ps failed (rc=127)" in lines

    def test_a_measured_reap_is_not_announced(self, tmp_path: Path) -> None:
        """The negative control, or the daemon narrates every clean run."""
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        obs = _NullObserver()
        serve_cycle(
            tmp_path,
            observer=obs,
            runner=_stub_runner(
                RunOutcome(returncode=-9, timed_out=True, group_reaped=True),
            ),
        )
        assert "UNMEASURED" not in "\n".join(obs.lines)


class TestRunOwnership:
    """#186 F2: charge and classify only artifacts this invocation made."""

    def test_a_launch_with_no_artifacts_charges_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        # A previous run's dir and spend already exist.
        _make_run_dir(tmp_path, "factory-OLD-000")
        with patch(
            "kstrl.serve.read_run_spend",
            return_value=RunSpend(cost_usd=99.0, cost_calls=1, usage_calls=1),
        ):
            result = serve_cycle(
                tmp_path,
                runner=_stub_runner(RunOutcome(1), make_run_dir=False),
            )
        assert result.charged_usd == 0.0, "a prior run's spend is not ours"
        assert SpendLedger(tmp_path).read().spent_usd == 0.0

    def test_a_stale_manifest_is_not_used_to_classify(
        self,
        tmp_path: Path,
    ) -> None:
        """An old manifest saying "infra failure" must not authorize a retry."""
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_infra_finding()])],
            run_id="factory-OLD-000",
        )
        _make_run_dir(tmp_path, "factory-OLD-000")
        result = serve_cycle(
            tmp_path,
            runner=_stub_runner(RunOutcome(1), make_run_dir=False),
        )
        assert result.verdict is Verdict.UNCLASSIFIABLE
        assert queue.items()[0].state is ItemState.POISON

    def test_an_owned_manifest_is_read_and_classified(
        self,
        tmp_path: Path,
    ) -> None:
        """The positive case, which only works if the run id key is right.

        The stale-manifest test alone cannot catch a wrong key: a
        misread id yields "" and lands on the same unclassifiable branch
        as an unowned manifest.
        """
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_infra_finding()])],
        )
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert result.verdict is Verdict.RETRY_INFRA, (
            "an owned manifest must be read, which needs the runId key"
        )
        assert queue.items()[0].state is ItemState.QUEUED

    def test_only_new_run_dirs_are_charged(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        _make_run_dir(tmp_path, "factory-OLD-000")
        seen: list[str] = []

        def fake_spend(root: Path, run_id: str) -> RunSpend:
            seen.append(run_id)
            return RunSpend(cost_usd=1.0, cost_calls=1, usage_calls=1)

        with patch("kstrl.serve.read_run_spend", side_effect=fake_spend):
            serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert "factory-OLD-000" not in seen
        assert seen == ["factory-20260730-000000.000000-aaa"]

    def test_read_run_spend_refuses_an_empty_run_id(
        self,
        tmp_path: Path,
    ) -> None:
        """An empty id used to select the NEWEST run on disk.

        Asserts the reducer is never CONSULTED, not merely that the result
        is zero: on an empty repo it would be zero either way.
        """
        with patch("kstrl.reducer.load_run_state") as load:
            assert REAL_READ_RUN_SPEND(tmp_path, "") == RunSpend()
            assert load.call_count == 0, "must not read another run"

    def test_read_run_spend_reads_a_named_run(self, tmp_path: Path) -> None:
        """The positive case, so the guard cannot be over-broad.

        The double is a REAL ``RunState``: a hand-rolled object carrying
        only the three fields this function read at the time silently
        became wrong the moment it read a fourth (#257 piece B).
        """
        with patch("kstrl.reducer.load_run_state") as load:
            load.return_value = (
                RunState(cost_usd=1.25, cost_calls=2, usage_calls=3),
                None,
            )
            spend = REAL_READ_RUN_SPEND(tmp_path, "factory-abc")
        assert spend.cost_usd == 1.25
        assert spend.uncovered_calls == 1
        # No architect row on this run, so nothing claims one.
        assert spend.architect_calls == 0

    def test_a_concurrent_decompose_is_not_charged_to_the_queue(
        self,
        tmp_path: Path,
    ) -> None:
        """#257 review: `ks decompose` takes no factory.lock, so an
        operator can start one on this repo while a queue item is
        executing. Its run dir appears inside the launch window, and
        since #257 it carries REAL spend. Charging it would bill the
        queue for money the daemon never spent, and could halt the queue
        on `max_daily_spend` because of it.
        """
        queue = _queue(tmp_path)
        _add(queue)
        seen: list[str] = []

        def fake_spend(root: Path, run_id: str) -> RunSpend:
            seen.append(run_id)
            return RunSpend(cost_usd=4.0, cost_calls=1, usage_calls=1)

        # The operator's decompose lands mid-run, so it is NEW since the
        # snapshot and the set difference alone would take it.
        runner = _stub_runner(
            RunOutcome(0),
            extra_run_ids=("decompose-20260730-000001.000000-bbb",),
        )
        with patch("kstrl.serve.read_run_spend", side_effect=fake_spend):
            serve_cycle(tmp_path, runner=runner)

        assert seen == ["factory-20260730-000000.000000-aaa"]
        # Charged once, not twice: the hand-run decompose is someone
        # else's spend even though it is inside the window.
        assert SpendLedger(tmp_path).read().spent_usd == pytest.approx(4.0)

    def test_the_charged_kind_matches_what_a_factory_actually_mints(
        self,
    ) -> None:
        """#257 review: the filter and the spawned argv agree with each
        other by convention, not by derivation - the kind on disk comes
        from ``mint_run_id``'s default, a third literal. If that ever
        moved, both would still agree and both would disagree with the
        disk, and the daemon would charge $0.00 for every queue item
        forever. Silent under-billing is exactly what max_daily_spend
        exists to catch, so pin the third leg.
        """
        from kstrl.knowledge import current_run_id
        from kstrl.runid import KNOWN_KINDS, run_kind

        assert SPAWNED_RUN_KIND in KNOWN_KINDS
        assert run_kind(current_run_id()) == SPAWNED_RUN_KIND

    def test_an_architect_that_reached_no_run_dir_is_named_unmetered(
        self,
        tmp_path: Path,
    ) -> None:
        """#186 F3, and still true after #257 piece B.

        This launch left a run dir with no architect row, which is what
        every case in ``RunSpend.unmetered_phases`` looks like from here:
        the daemon cannot see the spend, so it must not pretend the day's
        figure is exact.
        """
        queue = _queue(tmp_path)
        _add(queue)
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        spend = SpendLedger(tmp_path).read()
        assert "architect" in spend.unmetered_phases
        assert spend.lower_bound, "unmetered architect spend makes it a floor"

    def test_a_metered_architect_is_not_also_named_unmetered(
        self,
        tmp_path: Path,
    ) -> None:
        """#257 piece B: the claim stopped being a constant.

        `ks factory` now seeds the architect's spend into the run it
        decomposed for, so a launch that executed carries an architect
        row inside the very run dir this daemon charged. Repeating
        "unmetered: architect" on top of that would brand every honest
        day's total a floor, which is the failure mode ``lower_bound``
        exists to flag.
        """
        queue = _queue(tmp_path)
        _add(queue)

        def fake_spend(root: Path, run_id: str) -> RunSpend:
            return RunSpend(
                cost_usd=3.0,
                cost_calls=2,
                usage_calls=2,
                architect_calls=1,
            )

        with patch("kstrl.serve.read_run_spend", side_effect=fake_spend):
            serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))

        spend = SpendLedger(tmp_path).read()
        assert spend.unmetered_phases == ()
        assert not spend.lower_bound
        # Charged ONCE. The architect's dollars are already inside the
        # run's cost_usd; architect_calls only answers "did it report".
        assert spend.spent_usd == pytest.approx(3.0)

    def test_the_architect_row_is_read_off_the_run_state(
        self,
        tmp_path: Path,
    ) -> None:
        """The seat #257 piece B writes to and the one serve reads from
        are the same key, which is the only reason the check above can
        distinguish a metered architect from a silent one.

        #281 moved that key into the role namespace. It is spelled from
        the constant, so a future move keeps writer and reader together
        rather than leaving this passing on a literal the writer no
        longer uses.
        """
        spend = _spend_with_component_keyed(tmp_path, ARCHITECT_COMPONENT)

        assert spend.architect_calls == 1

    def test_a_bare_architect_key_never_clears_the_honesty_flag(
        self,
        tmp_path: Path,
    ) -> None:
        """#281, and the consequence that is not cosmetic.

        ONE state, because two readings of it are indistinguishable here
        and that is the whole argument:

        - a NEW run whose architect never reported (a resume, or an
          adapter that reports no usage) which also carries a component
          the architect genuinely NAMED `architect` - a legal id, and an
          ordinary one for a spec about design tooling; or
        - an OLD run, recorded before the role key moved, whose stream
          still says ``"architect"`` for the role itself.

        Both put ``usage_calls`` on a component row spelled
        ``architect``. While the role's own row was keyed by that same
        bare word, the first case answered "did the architect report?"
        with a different role's calls, cleared ``unmetered_phases``, and
        let the daemon announce a day's total as exact on no evidence -
        the honesty property #257 piece B exists to establish.

        Both now read as unmetered. For the first that is correct; for
        the second it is a lost decimal place - the money is still
        counted, only the exactness claim degrades - and it is the same
        answer already given for a resume, a blocker halt and a silent
        adapter. Never a false exact.

        This is also why there is no fallback to the old key: nothing at
        this seam can tell the two readings apart, so a fallback would
        reintroduce case one in order to prettify case two.
        ``read_run_spend`` states why nothing narrower is worth building.
        """
        spend = _spend_with_component_keyed(tmp_path, ARCHITECT_ROLE)

        assert spend.architect_calls == 0, "a component's calls are not the architect's"
        assert spend.unmetered_phases == (ARCHITECT_ROLE,)
        assert spend.cost_usd == 2.0, "the money is still read; only the claim degrades"
        # ``RunSpend.lower_bound`` is the per-axis coverage question
        # (usage_calls vs cost_calls) and is deliberately NOT asserted
        # here: it is ``DailySpend.lower_bound`` that reads
        # ``unmetered_phases`` and brands the day a floor, which
        # ``test_an_architect_that_reached_no_run_dir_is_named_unmetered``
        # already covers end to end.

    def test_a_decompose_run_dir_never_supplies_the_architect_row(
        self,
        tmp_path: Path,
    ) -> None:
        """The kind filter and the unmetered claim must not fight.

        An operator's hand-run `ks decompose` reports an architect and
        is deliberately NOT charged (piece A). It must not silently
        satisfy this launch's architect claim either, or the daemon
        would call the day exact on the strength of somebody else's
        spend.
        """
        queue = _queue(tmp_path)
        _add(queue)

        def fake_spend(root: Path, run_id: str) -> RunSpend:
            architect = 1 if run_id.startswith("decompose-") else 0
            return RunSpend(
                cost_usd=1.0,
                cost_calls=1,
                usage_calls=1,
                architect_calls=architect,
            )

        runner = _stub_runner(
            RunOutcome(0),
            extra_run_ids=("decompose-20260730-000001.000000-bbb",),
        )
        with patch("kstrl.serve.read_run_spend", side_effect=fake_spend):
            serve_cycle(tmp_path, runner=runner)

        spend = SpendLedger(tmp_path).read()
        assert "architect" in spend.unmetered_phases
        assert spend.spent_usd == pytest.approx(1.0)

    def test_one_runs_architect_cannot_clear_anothers(
        self,
        tmp_path: Path,
    ) -> None:
        """#257 review: ``unmetered_phases`` is all-or-nothing, so
        reading it off the SUM let a sibling borrow an architect it never
        had.

        Two factory-kind dirs land in one launch window - a documented
        hole this module already names (``--force-lock``, the embedded
        dashboard's early mkdir). One reports an architect, one does not.
        The day must stay a floor, because half its money is still
        unaccounted for.
        """
        queue = _queue(tmp_path)
        _add(queue)

        def fake_spend(root: Path, run_id: str) -> RunSpend:
            metered = run_id.endswith("aaa")
            return RunSpend(
                cost_usd=2.0,
                cost_calls=1,
                usage_calls=1,
                architect_calls=1 if metered else 0,
            )

        runner = _stub_runner(
            RunOutcome(0),
            extra_run_ids=("factory-20260730-000001.000000-ccc",),
        )
        with patch("kstrl.serve.read_run_spend", side_effect=fake_spend):
            serve_cycle(tmp_path, runner=runner)

        spend = SpendLedger(tmp_path).read()
        assert "architect" in spend.unmetered_phases
        assert spend.lower_bound
        # Both dirs are still charged; only the CLAIM is per run.
        assert spend.spent_usd == pytest.approx(4.0)

    def test_a_launch_with_no_run_dir_still_names_the_architect(
        self,
        tmp_path: Path,
    ) -> None:
        """The fold is over an empty list on the halt path, and an empty
        union would say "nothing unmetered" about a launch that may have
        spent an architect's worth of money."""
        runs, launch = owned_run_spend(tmp_path, frozenset())

        assert runs == []
        assert launch == LaunchSpend(unmetered_phases=("architect",))


class TestPauseIsAtomic:
    """#186 F7: the elapsed-pause clear must not lose an operator's pause."""

    def test_an_operator_pause_is_never_cleared_by_the_daemon(
        self,
        tmp_path: Path,
    ) -> None:
        """A pause with no resume_after is a human decision; only a human lifts it."""
        queue = _queue(tmp_path)
        _add(queue)
        queue.pause(reason="operator emergency stop", actor="operator")
        calls: list[dict[str, object]] = []
        result = serve_cycle(
            tmp_path,
            runner=_stub_runner(RunOutcome(0), calls),
        )
        assert calls == []
        assert "paused" in result.skipped
        assert queue.pause_state().paused, "the daemon must not clear it"

    def test_only_an_expired_window_is_cleared(self, tmp_path: Path) -> None:
        """A live budget pause must survive the cycle that observes it."""
        queue = _queue(tmp_path)
        _add(queue)
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        queue.pause(reason="budget", resume_after=future)
        calls: list[dict[str, object]] = []
        serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0), calls))
        assert calls == []
        assert queue.pause_state().paused

    def test_the_daemon_pause_write_is_also_under_the_mutex(self) -> None:
        """Losing an operator's pause is the dangerous direction (#186 F7)."""
        import inspect

        from kstrl.serve import _pause_queue

        source = inspect.getsource(_pause_queue)
        assert "with queue_lock(root_dir, blocking=True):" in source

    def test_the_pause_read_is_held_under_the_queue_mutex(self) -> None:
        """Structural: the read and the conditional clear share one lock."""
        import inspect
        import textwrap

        lines = textwrap.dedent(inspect.getsource(serve_cycle)).splitlines()
        read_idx = next(i for i, ln in enumerate(lines) if "pause = queue.pause_state()" in ln)
        indent = len(lines[read_idx]) - len(lines[read_idx].lstrip())
        guard = ""
        for i in range(read_idx - 1, -1, -1):
            candidate = lines[i]
            if not candidate.strip():
                continue
            if len(candidate) - len(candidate.lstrip()) < indent:
                guard = candidate.strip()
                break
        assert "queue_lock" in guard, f"pause read guarded by {guard!r}"


class TestFactoryLockProbe:
    """#186 F6: the probe is a courtesy check that must fail closed."""

    def test_no_lock_file_means_free(self, tmp_path: Path) -> None:
        assert not factory_lock_held(tmp_path)

    def test_an_unopenable_lock_file_reads_as_HELD(
        self,
        tmp_path: Path,
    ) -> None:
        """An unopenable lock is not evidence that no run owns the root."""
        lock = tmp_path / ".kstrl" / "factory.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        with patch("kstrl.serve.open", side_effect=PermissionError("denied")):
            assert factory_lock_held(tmp_path)

    def test_a_held_lock_is_detected(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        import fcntl

        lock = tmp_path / ".kstrl" / "factory.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert factory_lock_held(tmp_path)
        finally:
            handle.close()

    def test_a_free_lock_is_detected(self, tmp_path: Path) -> None:
        lock = tmp_path / ".kstrl" / "factory.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        assert not factory_lock_held(tmp_path)


class TestNeedsHuman:
    """#186 F10: exit status follows the terminal state, not may_retry."""

    def test_an_exhausted_infra_verdict_needs_a_human(
        self,
        tmp_path: Path,
    ) -> None:
        """may_retry stays True here, which is what fooled the old filter."""
        queue = _queue(tmp_path, max_attempts=1)
        _add(queue)
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_infra_finding()])],
        )
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert result.verdict is Verdict.RETRY_INFRA
        assert result.verdict.may_retry, "the trap the old filter fell into"
        assert queue.items()[0].state is ItemState.POISON
        assert result.needs_human

    def test_a_retryable_failure_with_attempts_left_does_not(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_infra_finding()])],
        )
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(1)))
        assert not result.needs_human

    def test_success_does_not(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        assert not serve_cycle(
            tmp_path,
            runner=_stub_runner(RunOutcome(0)),
        ).needs_human

    def test_a_reaper_poison_needs_a_human_without_running_an_item(
        self,
        tmp_path: Path,
    ) -> None:
        """A path with no ran_item at all, which the old filter skipped."""
        queue = _queue(tmp_path, max_attempts=1)
        queue.start(queue.lease(_add(queue), pid=999999))  # type: ignore[arg-type]
        result = serve_cycle(tmp_path, runner=_stub_runner(RunOutcome(0)))
        assert result.reaped.poisoned
        assert result.needs_human

    def test_a_budget_pause_needs_a_human(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        SpendLedger(tmp_path).charge(50.0, covered_calls=1, total_calls=1)
        result = serve_cycle(
            tmp_path,
            config=ServeConfig(daily_budget_usd=10.0),
            runner=_stub_runner(RunOutcome(0)),
        )
        assert result.paused
        assert result.needs_human

    def test_an_unreadable_ledger_needs_a_human(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        ledger = SpendLedger(tmp_path)
        ledger.charge(1.0, covered_calls=1, total_calls=1)
        ledger.path.write_text("{corrupt")
        calls: list[dict[str, object]] = []
        result = serve_cycle(
            tmp_path,
            runner=_stub_runner(RunOutcome(0), calls),
        )
        assert calls == []
        assert result.needs_human
        # The pre-flight guard is what files this; the gate handler does
        # not, so asserting it pins the earlier check specifically.
        from kstrl.inbox import Inbox, InboxConfig

        titles = [i.title for i in Inbox(tmp_path, InboxConfig.load(tmp_path)).items()]
        assert any("unreadable spend ledger" in t for t in titles)


class TestBoundedRetention:
    """#186 F11: the daemon path must not grow a list forever."""

    def test_a_bounded_run_returns_every_result(self, tmp_path: Path) -> None:
        results = serve(
            tmp_path,
            runner=_stub_runner(RunOutcome(0)),
            max_cycles=5,
            sleeper=lambda _s: None,
        )
        assert len(results) == 5

    def test_the_unbounded_path_uses_a_bounded_window(self) -> None:
        import inspect

        source = inspect.getsource(serve)
        assert "deque(maxlen=RECENT_CYCLE_WINDOW)" in source
        assert "bounded.append" in source


class TestBudgetHaltIsNotRetryableInfrastructure:
    """The most expensive defect the live run found.

    `pipeline.fail_for_budget` records a blown ceiling as
    `Finding.infrastructure_error` with no distinguishing category, so
    the classifier read a deliberate "stop spending" as transient
    infrastructure trouble and retried it. The retry re-runs the same
    work against the same ceiling and costs MORE, because retries carry
    accumulated context. Three attempts of that is the crash loop this
    module exists to prevent, reached through the one branch meant to be
    safe.

    Every unit test had built `Finding.infrastructure_error(...)` by hand
    to mean "the CLI died". None encoded that the factory ALSO uses that
    category for a self-imposed halt.
    """

    @staticmethod
    def _budget_run(root: Path, run_id: str = "factory-budget") -> None:
        """Write a run dir whose stream carries a real BudgetExceeded."""
        from kstrl import events as ev
        from kstrl.events import JsonlSink, RunPaths

        paths = RunPaths.for_run(root, run_id)
        sink = JsonlSink(paths.events_file)
        sink.emit(
            ev.BudgetExceeded(
                component="comp-a",
                total_tokens=716348,
                max_total_tokens=400000,
                cost_usd=2.53,
                max_cost_usd=0.0,
                ceiling="max_total_tokens",
                condition="breached",
                ceilings=("max_total_tokens",),
            )
        )

    def test_a_budget_halt_does_NOT_retry(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [_infra_finding()])])
        self._budget_run(tmp_path)
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
            owned_run_ids=["factory-budget"],
        )
        assert outcome.verdict is Verdict.BUDGET_HALT
        assert not outcome.verdict.may_retry, (
            "retrying a ceiling breach re-runs the same work at higher cost"
        )

    def test_the_reason_names_the_ceiling_and_the_decision(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [_infra_finding()])])
        self._budget_run(tmp_path)
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
            owned_run_ids=["factory-budget"],
        )
        assert "max_total_tokens" in outcome.reason
        assert "human decision" in outcome.reason

    def test_a_genuine_infra_failure_still_retries(
        self,
        tmp_path: Path,
    ) -> None:
        """The fix must not swallow the case it was carved out of."""
        path = tmp_path / "m.json"
        _manifest(path, [_component("comp-a", "failed", [_infra_finding()])])
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
            owned_run_ids=["factory-no-budget-event"],
        )
        assert outcome.verdict is Verdict.RETRY_INFRA

    def test_the_check_reads_the_typed_event_not_the_prose(
        self,
        tmp_path: Path,
    ) -> None:
        """A finding whose text merely mentions a budget is not a halt."""
        from kstrl.findings import Finding

        path = tmp_path / "m.json"
        misleading = Finding.infrastructure_error(
            "engineer",
            "token budget exceeded: agent CLI printed this",
        )
        _manifest(path, [_component("comp-a", "failed", [misleading])])
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
            owned_run_ids=["factory-no-budget-event"],
        )
        assert outcome.verdict is Verdict.RETRY_INFRA, (
            "prose must not decide this; only the typed event may"
        )

    def test_a_budget_halt_poisons_the_item_in_a_full_cycle(
        self,
        tmp_path: Path,
    ) -> None:
        """End to end: the item must not be requeued for another attempt."""
        queue = _queue(tmp_path, max_attempts=3)
        _add(queue)
        run_id = "factory-20260730-000000.000000-aaa"
        _manifest(
            tmp_path / "scripts" / "kstrl" / "manifest.json",
            [_component("comp-a", "failed", [_infra_finding()])],
            run_id=run_id,
        )

        def runner(*, root_dir: Path, **kwargs: object) -> RunOutcome:
            _make_run_dir(root_dir, run_id)
            self._budget_run(root_dir, run_id)
            return RunOutcome(returncode=1)

        result = serve_cycle(tmp_path, runner=runner)  # type: ignore[arg-type]
        assert result.verdict is Verdict.BUDGET_HALT
        item = queue.items()[0]
        assert item.state is ItemState.POISON, "a ceiling breach must not consume further attempts"
        assert result.needs_human


class TestBudgetHaltPrecedence:
    """#197 M1/M2/M3: the halt must win over every retry-authorizing branch."""

    @staticmethod
    def _budget_run(
        root: Path,
        run_id: str = "factory-budget",
        *,
        condition: str = "breached",
        ceilings: tuple[str, ...] = ("max_total_tokens",),
    ) -> None:
        from kstrl import events as ev
        from kstrl.events import JsonlSink, RunPaths

        paths = RunPaths.for_run(root, run_id)
        JsonlSink(paths.events_file).emit(
            ev.BudgetExceeded(
                component="comp-a",
                total_tokens=716348,
                max_total_tokens=400000,
                cost_usd=2.53,
                max_cost_usd=0.0,
                ceiling=ceilings[0] if ceilings else "",
                condition=condition,
                ceilings=ceilings,
            )
        )

    def test_a_timed_out_run_that_blew_its_ceiling_does_not_retry(
        self,
        tmp_path: Path,
    ) -> None:
        """The exact hole: halt, then hang until our timeout kills it.

        The timeout branch returns RETRY_INFRA, so checking the budget
        after it requeued the item against the very ceiling this verdict
        exists to make terminal.
        """
        self._budget_run(tmp_path)
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=-9, timed_out=True, group_reaped=True),
            manifest_path=None,
            owned_run_ids=["factory-budget"],
        )
        assert outcome.verdict is Verdict.BUDGET_HALT
        assert not outcome.verdict.may_retry

    @pytest.mark.parametrize(
        "run",
        [
            RunOutcome(returncode=-9, timed_out=True, group_reaped=True),
            RunOutcome(returncode=-9),
            RunOutcome(returncode=2, output_tail="--force-lock"),
            RunOutcome(returncode=1),
        ],
    )
    def test_no_retry_authorizing_branch_outranks_the_halt(
        self,
        tmp_path: Path,
        run: RunOutcome,
    ) -> None:
        self._budget_run(tmp_path)
        outcome = classify_run(
            tmp_path,
            run=run,
            manifest_path=None,
            owned_run_ids=["factory-budget"],
        )
        assert outcome.verdict is Verdict.BUDGET_HALT, f"{run} outranked the ceiling"

    def test_a_launch_failure_still_retries(self, tmp_path: Path) -> None:
        """The one branch that must precede it: no child, no artifacts."""
        self._budget_run(tmp_path)
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=-1, launch_error="No such file"),
            manifest_path=None,
            owned_run_ids=["factory-budget"],
        )
        assert outcome.verdict is Verdict.RETRY_INFRA

    def test_an_unenforceable_halt_does_not_claim_a_breach(
        self,
        tmp_path: Path,
    ) -> None:
        """#197 M2: no threshold was crossed, so do not report one."""
        self._budget_run(
            tmp_path,
            condition="unenforceable",
            ceilings=("max_cost_usd",),
        )
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=None,
            owned_run_ids=["factory-budget"],
        )
        assert outcome.verdict is Verdict.BUDGET_HALT
        assert "can still fire" in outcome.reason
        assert "raising a limit would change nothing" in outcome.reason
        assert "same limit at a higher cost" not in outcome.reason, (
            "the breach wording must not appear for an unenforceable halt"
        )

    def test_a_breached_halt_still_says_so(self, tmp_path: Path) -> None:
        self._budget_run(tmp_path, condition="breached")
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=None,
            owned_run_ids=["factory-budget"],
        )
        assert "human decision" in outcome.reason
        assert "can still fire" not in outcome.reason

    def test_an_unevidenced_sibling_is_never_dropped(
        self,
        tmp_path: Path,
    ) -> None:
        """#197 M3: a spec finding must not hide a sibling's real error."""
        path = tmp_path / "m.json"
        spec_comp = _component("comp-a", "failed", [_spec_finding()])
        silent = _component("comp-b", "failed", [])
        silent.error = "Failed to create worktree: fatal: invalid reference"
        _manifest(path, [spec_comp, silent])
        outcome = classify_run(
            tmp_path,
            run=RunOutcome(returncode=1),
            manifest_path=path,
        )
        assert outcome.verdict is Verdict.SPEC_FAILURE, "terminal verdict stands"
        assert "comp-a" in outcome.reason
        assert "invalid reference" in outcome.reason, (
            "the sibling's real cause must reach the operator"
        )
        assert outcome.evidence["component_errors"]["comp-b"] == silent.error
