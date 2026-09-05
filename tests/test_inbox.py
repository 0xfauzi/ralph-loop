"""R8.3 exception inbox tests.

Covers the substrate (append-only log, decision folding, dedupe, snooze
TTLs, the open-item cap), the config surface, and - the part that matters
most after PR #174 - that every halt path ACTUALLY emits an item during a
real factory run, rather than the store being real but unfed.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.autonomy import AutonomyLevel, AutonomyState
from kstrl.findings import Finding
from kstrl.inbox import (
    Inbox,
    InboxConfig,
    InboxError,
    InboxItem,
    ItemKind,
    ItemStatus,
    Priority,
    notifiable,
    summarize,
)
from tests.helpers.component_prd import write_component_prd
from tests.helpers.settle import drained, mounted


def _box(tmp_path: Path, **kwargs: object) -> Inbox:
    return Inbox(tmp_path, InboxConfig(**kwargs))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Substrate
# --------------------------------------------------------------------------
class TestSubstrate:
    def test_add_and_list(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        item = box.add(ItemKind.HALTED_RUN, "comp-a halted", component="comp-a")
        assert box.open_items() == [item]
        assert item.status is ItemStatus.OPEN
        assert item.occurrences == 1

    def test_empty_inbox_reads_clean(self, tmp_path: Path) -> None:
        assert _box(tmp_path).items() == []
        assert summarize([]) == "inbox clear"

    def test_log_is_append_only(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        item = box.add(ItemKind.MERGE_GATE, "m", dedupe_key="k")
        box.approve(item.id, actor="human")
        lines = box.path.read_text().strip().splitlines()
        assert len(lines) == 2  # emission + decision, nothing rewritten
        assert len(box.items()) == 1  # folded to one current item

    def test_decision_folds_to_latest(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        item = box.add(ItemKind.MERGE_GATE, "m")
        box.snooze(item.id, actor="h", hours=5)
        box.approve(item.id, actor="h")
        assert box.get(item.id).status is ItemStatus.APPROVED  # type: ignore[union-attr]

    def test_torn_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        # An unreadable inbox is an invisible backlog; tolerate the tear.
        box = _box(tmp_path)
        box.add(ItemKind.HALTED_RUN, "good")
        with open(box.path, "a", encoding="utf-8") as handle:
            handle.write('{"id": "broken", "kind": "nonsense"}\n{not json\n')
        assert len(box.items()) == 1

    def test_prefix_lookup(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        item = box.add(ItemKind.HALTED_RUN, "x")
        assert box.get(item.id[:8]) is not None
        assert box.get("nope") is None

    def test_compact_preserves_state_and_shrinks_log(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        first = box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        box.add(ItemKind.MERGE_GATE, "b", dedupe_key="b")
        box.approve(first.id, actor="h")
        before = len(box.path.read_text().strip().splitlines())
        kept = box.compact()
        after = len(box.path.read_text().strip().splitlines())
        assert kept == 2 and after == 2 and after < before
        assert box.get(first.id).status is ItemStatus.APPROVED  # type: ignore[union-attr]


class TestDedupe:
    def test_repeat_while_open_bumps_occurrences(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        first = box.add(ItemKind.HALTED_RUN, "comp-a halted", dedupe_key="h:comp-a")
        again = box.add(ItemKind.HALTED_RUN, "comp-a halted", dedupe_key="h:comp-a")
        assert again.id == first.id
        assert again.occurrences == 2
        assert len(box.open_items()) == 1

    def test_repeat_after_decision_opens_a_new_item(self, tmp_path: Path) -> None:
        # You approved that failure once; its recurrence is new information.
        box = _box(tmp_path)
        first = box.add(ItemKind.HALTED_RUN, "comp-a halted", dedupe_key="h:comp-a")
        box.approve(first.id, actor="h")
        again = box.add(ItemKind.HALTED_RUN, "comp-a halted", dedupe_key="h:comp-a")
        assert again.id != first.id
        assert len(box.open_items()) == 1

    def test_no_dedupe_key_never_collapses(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        box.add(ItemKind.HALTED_RUN, "a")
        box.add(ItemKind.HALTED_RUN, "a")
        assert len(box.open_items()) == 2


class TestDecisions:
    def test_reject_requires_a_comment(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        item = box.add(ItemKind.POLICY_EXCEPTION, "x")
        with pytest.raises(InboxError, match="requires a comment"):
            box.reject(item.id, actor="h", comment="   ")

    def test_decision_records_actor_and_time(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        item = box.add(ItemKind.POLICY_EXCEPTION, "x")
        decided = box.approve(item.id, actor="wumpini", comment="once")
        assert decided.decided_by == "wumpini"
        assert decided.decision_comment == "once"
        assert decided.decided_at

    def test_unknown_id_raises(self, tmp_path: Path) -> None:
        with pytest.raises(InboxError, match="no inbox item"):
            _box(tmp_path).approve("missing", actor="h")

    def test_ambiguous_prefix_raises(self, tmp_path: Path) -> None:
        # Two ids sharing a prefix must not silently resolve to one.
        box = _box(tmp_path)
        for suffix in ("aa", "bb"):
            box._append(
                InboxItem(
                    id=f"abcdef{suffix}",
                    kind=ItemKind.HALTED_RUN,
                    title=f"item {suffix}",
                    created_at="2026-07-27T00:00:00+00:00",
                )
            )
        with pytest.raises(InboxError, match="ambiguous"):
            box.get("abcdef")

    def test_unambiguous_prefix_still_resolves(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        box._append(
            InboxItem(
                id="abcdefaa",
                kind=ItemKind.HALTED_RUN,
                title="only",
                created_at="2026-07-27T00:00:00+00:00",
            )
        )
        assert box.get("abcdef") is not None


class TestSnooze:
    def test_snooze_hides_until_ttl(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        item = box.add(ItemKind.MERGE_GATE, "m")
        snoozed = box.snooze(item.id, actor="h", hours=5)
        assert snoozed.status is ItemStatus.SNOOZED
        assert snoozed.snooze_active
        assert box.open_items() == []

    def test_lapsed_snooze_reads_open_again(self, tmp_path: Path) -> None:
        # Deferring a decision must not be the same as losing it.
        box = _box(tmp_path)
        item = box.add(ItemKind.MERGE_GATE, "m")
        box.snooze(item.id, actor="h", hours=1)
        stored = box.get(item.id)
        assert stored is not None
        stored.snooze_until = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        box._append(stored)
        assert len(box.open_items()) == 1

    def test_non_positive_ttl_refused(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        item = box.add(ItemKind.MERGE_GATE, "m")
        with pytest.raises(InboxError, match="positive TTL"):
            box.snooze(item.id, actor="h", hours=0)


class TestCapacityAndNotification:
    def test_open_cap(self, tmp_path: Path) -> None:
        box = _box(tmp_path, open_item_cap=2)
        box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        assert box.over_cap() is False
        box.add(ItemKind.HALTED_RUN, "b", dedupe_key="b")
        assert box.over_cap() is True

    def test_cap_zero_is_unbounded(self, tmp_path: Path) -> None:
        box = _box(tmp_path, open_item_cap=0)
        for i in range(5):
            box.add(ItemKind.HALTED_RUN, str(i), dedupe_key=str(i))
        assert box.over_cap() is False

    def test_decided_items_free_capacity(self, tmp_path: Path) -> None:
        box = _box(tmp_path, open_item_cap=1)
        item = box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        assert box.over_cap() is True
        box.approve(item.id, actor="h")
        assert box.over_cap() is False

    @pytest.mark.parametrize(
        "kind,expected",
        [
            (ItemKind.POLICY_EXCEPTION, True),
            (ItemKind.MERGE_GATE, True),
            (ItemKind.HALTED_RUN, True),
            (ItemKind.BUDGET_OVERRUN, True),
            (ItemKind.DEMOTION_NOTICE, False),  # informational, but notified
            (ItemKind.CALIBRATION_DRIFT, False),
            (ItemKind.TEST_ADEQUACY, True),  # a blocked change waits
            (ItemKind.HEALTH_BREACH, False),  # a trend, reported not gated
        ],
    )
    def test_action_required_taxonomy(self, kind: ItemKind, expected: bool) -> None:
        assert kind.action_required is expected

    def test_notifiable_covers_demotions_but_not_drift(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        box.add(ItemKind.DEMOTION_NOTICE, "demoted", dedupe_key="d")
        box.add(ItemKind.CALIBRATION_DRIFT, "drift", dedupe_key="c")
        kinds = {str(i.kind) for i in notifiable(box.items())}
        assert kinds == {"demotion_notice"}

    def test_decided_items_are_not_notifiable(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        item = box.add(ItemKind.POLICY_EXCEPTION, "x")
        box.approve(item.id, actor="h")
        assert notifiable(box.items()) == []

    def test_priority_defaults_and_ordering(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        box.add(ItemKind.CALIBRATION_DRIFT, "low", dedupe_key="l")
        box.add(ItemKind.POLICY_EXCEPTION, "high", dedupe_key="h")
        first = box.open_items()[0]
        assert first.priority is Priority.HIGH


class TestUnparseableLineCount:
    """Issue #190: the fold tolerates garbled lines for DISPLAY; a safety
    gate must not sit on that tolerance. ``unparseable_line_count`` is the
    fail-closed signal the serve admission cap adds to the open total.
    """

    def test_missing_and_clean_logs_count_zero(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        assert box.unparseable_line_count() == 0  # no file yet
        box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        assert box.unparseable_line_count() == 0  # every line parses

    def test_torn_json_line_counts(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        with box.path.open("a", encoding="utf-8") as handle:
            handle.write('{"id": "torn-tail", "kind": "halted_r\n')
        assert box.unparseable_line_count() == 1
        # the tolerant display fold is unchanged: still one readable item
        assert len(box.items()) == 1

    def test_non_dict_and_idless_lines_count(self, tmp_path: Path) -> None:
        """json-decodable is not enough: a non-dict line and a dict the
        fold cannot rebuild (no id) are equally invisible open items."""
        box = _box(tmp_path)
        box.path.parent.mkdir(parents=True, exist_ok=True)
        box.path.write_text('[1, 2]\n{"title": "no id"}\n', encoding="utf-8")
        assert box.unparseable_line_count() == 2
        assert box.items() == []

    def test_blank_lines_do_not_count(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        item = box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        with box.path.open("a", encoding="utf-8") as handle:
            handle.write("\n\n")
        assert box.unparseable_line_count() == 0
        assert box.open_items() == [item]

    def test_read_oserror_is_unreadable_not_one_skip(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whole-file read failure is not 'one garbled line' - the gate
        has no positive evidence the backlog is under the cap (#190 P1)."""
        box = _box(tmp_path)
        box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        real_read_bytes = Path.read_bytes

        def flaky_read(self: Path) -> bytes:
            if self == box.path:
                raise OSError("EIO")
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", flaky_read)
        scan = box.scan()
        assert scan.unreadable is True
        assert box.items() == []  # display stays tolerant

    def test_exists_oserror_is_unreadable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        box = _box(tmp_path)
        real_exists = Path.exists

        def flaky_exists(self: Path) -> bool:
            if self == box.path:
                raise OSError("EACCES")
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", flaky_exists)
        assert box.scan().unreadable is True
        assert box.items() == []

    def test_invalid_utf8_is_unreadable_and_display_tolerant(
        self,
        tmp_path: Path,
    ) -> None:
        """A write torn inside a multibyte character must not raise out of
        the fold or the gate (#190 P1). Whole-file decode failure is the
        same fail-closed state as OSError - replacement-decoding the
        damaged line as one skip would still admit under a cap > 1."""
        box = _box(tmp_path)
        box.path.parent.mkdir(parents=True, exist_ok=True)
        box.path.write_bytes(b"\xff\n")
        assert box.scan().unreadable is True
        assert box.items() == []  # no UnicodeDecodeError escape


class TestConfig:
    def test_enabled_by_default(self) -> None:
        assert InboxConfig().enabled is True

    def test_load_reads_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[inbox]\nenabled = false\nopen_item_cap = 7\nsnooze_hours = 2.5\n"
        )
        config = InboxConfig.load(tmp_path)
        assert config.enabled is False
        assert config.open_item_cap == 7
        assert config.snooze_hours == 2.5

    def test_env_overrides_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[inbox]\nopen_item_cap = 7\n")
        monkeypatch.setenv("KSTRL_INBOX_OPEN_CAP", "3")
        assert InboxConfig.load(tmp_path).open_item_cap == 3


# --------------------------------------------------------------------------
# Emitters: every halt path must actually feed the inbox (PR #174 lesson)
# --------------------------------------------------------------------------
def _init_git_repo(root: Path) -> None:
    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )

    run("init")
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "tester")
    (root / "README.md").write_text("base\n")
    run("add", ".")
    run("commit", "-m", "base")


def _usage(total: int, cost: float = 0.0) -> object:
    from kstrl.agents.base import UsageRecord, UsageTotals

    totals = UsageTotals()
    totals.add_record(
        UsageRecord(
            input_tokens=total // 3,
            output_tokens=total - total // 3,
            total_tokens=total,
            cost_usd=cost or None,
            duration_seconds=1.0,
            source="claude-stream-json",
        )
    )
    return totals


def _run_factory(
    tmp_path: Path,
    *,
    verification: object | None = None,
    level: AutonomyLevel = AutonomyLevel.L1_SUPERVISED,
    autonomy: bool = False,
    inbox_enabled: bool = True,
    max_total_tokens: int = 0,
    max_cost_usd: float = 0.0,
    engineer_tokens: int = 0,
    engineer_cost: float = 0.0,
    create_prs: bool = False,
    pause_before_pr_merge: bool = False,
) -> None:
    from kstrl.config import KstrlConfig
    from kstrl.factory import ComponentResult, FactoryConfig, run_factory
    from kstrl.manifest import Component, Manifest
    from kstrl.review import ReviewResult
    from kstrl.ui.plain import PlainUI
    from kstrl.verify import CheckResult, VerificationResult, VerifyConfig

    _init_git_repo(tmp_path)
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("p")
    write_component_prd(tmp_path, "scripts/kstrl/feature/comp-a/prd.json")
    (tmp_path / "kstrl.toml").write_text(
        f"[autonomy]\nenabled = {'true' if autonomy else 'false'}\n"
        "[policy]\nenabled = true\n"
        f"[inbox]\nenabled = {'true' if inbox_enabled else 'false'}\n"
    )
    AutonomyState(level=int(level)).save(tmp_path)
    manifest = Manifest(
        version="1",
        spec_file="s",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                "comp-a",
                "A",
                "D",
                [],
                "scripts/kstrl/feature/comp-a/prd.json",
                "kstrl/factory/comp-a",
            )
        ],
    )
    config = FactoryConfig(
        use_worktrees=False,
        create_prs=create_prs,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        max_total_tokens=max_total_tokens,
        max_cost_usd=max_cost_usd,
        pause_before_pr_merge=pause_before_pr_merge,
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    base = KstrlConfig(
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    result = verification or VerificationResult(
        passed=True,
        checks=[CheckResult("diff_scope", True, "ok")],
    )
    component_result = ComponentResult(
        "comp-a",
        success=True,
        iterations=1,
        usage=(
            _usage(engineer_tokens, engineer_cost) if engineer_tokens or engineer_cost else None
        ),
    )
    with (
        patch(
            "kstrl.factory._run_component",
            return_value=component_result,
        ),
        patch("kstrl.factory.run_mechanical_verification", return_value=result),
        patch(
            "kstrl.factory.run_review",
            return_value=ReviewResult(passed=True, mode="hard"),
        ),
        patch("kstrl.pr.is_gh_available", return_value=False),
    ):
        run_factory(manifest, config, base, PlainUI(no_color=True), tmp_path)


class TestEmittersFireDuringRuns:
    def test_policy_violation_emits_policy_exception(self, tmp_path: Path) -> None:
        from kstrl.verify import CheckResult, VerificationResult

        violation = Finding.policy_violation(
            category="paths_deny",
            explanation="touched .env",
        )
        _run_factory(
            tmp_path,
            verification=VerificationResult(
                passed=False,
                checks=[
                    CheckResult(
                        "policy_envelope",
                        False,
                        "1 violation",
                        findings=[violation],
                    )
                ],
            ),
        )
        kinds = {str(i.kind) for i in Inbox(tmp_path, InboxConfig()).items()}
        assert "policy_exception" in kinds

    def test_blocking_adequacy_finding_emits_one_item(
        self,
        tmp_path: Path,
    ) -> None:
        # R8.5 / P2-f: the claim that adequacy findings reach the inbox
        # was made before the wiring existed. This is the wiring.
        from kstrl.verify import CheckResult, VerificationResult

        finding = Finding.adequacy_finding(
            category="test_deleted",
            explanation="tests/test_core.py::test_subs: test removed",
            location="tests/test_core.py",
            severity="high",
        )
        _run_factory(
            tmp_path,
            verification=VerificationResult(
                passed=False,
                checks=[
                    CheckResult(
                        "test_adequacy",
                        False,
                        "1 finding [blocking]",
                        findings=[finding],
                    )
                ],
            ),
        )
        items = [
            i for i in Inbox(tmp_path, InboxConfig()).items() if i.kind is ItemKind.TEST_ADEQUACY
        ]
        assert len(items) == 1, [str(i.kind) for i in items]
        assert items[0].component == "comp-a"
        assert items[0].evidence.get("location") == "tests/test_core.py"

    def test_advisory_adequacy_finding_emits_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        # The inbox is a queue of decisions. An advisory is a note, and it
        # is already recorded in the component's finding stream.
        from kstrl.verify import CheckResult, VerificationResult

        finding = Finding.adequacy_finding(
            category="weak_oracle",
            explanation="tests/test_new.py: no strong-oracle assertion",
            location="tests/test_new.py",
        )
        _run_factory(
            tmp_path,
            verification=VerificationResult(
                passed=True,
                checks=[
                    CheckResult(
                        "test_adequacy",
                        True,
                        "1 finding [advisory]",
                        findings=[finding],
                    )
                ],
            ),
        )
        kinds = [str(i.kind) for i in Inbox(tmp_path, InboxConfig()).items()]
        assert "test_adequacy" not in kinds, kinds

    def test_failed_component_emits_halted_run(self, tmp_path: Path) -> None:
        from kstrl.verify import CheckResult, VerificationResult

        _run_factory(
            tmp_path,
            verification=VerificationResult(
                passed=False,
                checks=[CheckResult("test_suite", False, "boom")],
            ),
        )
        items = Inbox(tmp_path, InboxConfig()).items()
        halted = [i for i in items if i.kind is ItemKind.HALTED_RUN]
        assert halted
        assert halted[0].component == "comp-a"
        assert halted[0].evidence.get("phase") == "verify"

    def test_demotion_emits_notice_with_evidence(self, tmp_path: Path) -> None:
        from kstrl.verify import CheckResult, VerificationResult

        violation = Finding.policy_violation(
            category="paths_deny",
            explanation="touched .env",
        )
        _run_factory(
            tmp_path,
            verification=VerificationResult(
                passed=False,
                checks=[
                    CheckResult(
                        "policy_envelope",
                        False,
                        "1 violation",
                        findings=[violation],
                    )
                ],
            ),
            level=AutonomyLevel.L3_ENVELOPED_AUTO,
            autonomy=True,
        )
        items = Inbox(tmp_path, InboxConfig()).items()
        notices = [i for i in items if i.kind is ItemKind.DEMOTION_NOTICE]
        assert notices, [str(i.kind) for i in items]
        assert notices[0].evidence.get("trigger") == "policy_violation"
        assert notices[0].priority is Priority.HIGH

    def test_clean_run_emits_nothing(self, tmp_path: Path) -> None:
        # Silence on success is what keeps the surface worth reading.
        _run_factory(tmp_path)
        assert Inbox(tmp_path, InboxConfig()).items() == []

    def test_disabled_inbox_writes_nothing(self, tmp_path: Path) -> None:
        from kstrl.verify import CheckResult, VerificationResult

        _run_factory(
            tmp_path,
            verification=VerificationResult(
                passed=False,
                checks=[CheckResult("test_suite", False, "boom")],
            ),
            inbox_enabled=False,
        )
        assert Inbox(tmp_path, InboxConfig()).items() == []

    def test_inbox_write_failure_does_not_fail_the_run(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.verify import CheckResult, VerificationResult

        with patch(
            "kstrl.pipeline.Inbox.add",
            side_effect=OSError("disk full"),
        ):
            _run_factory(
                tmp_path,
                verification=VerificationResult(
                    passed=False,
                    checks=[CheckResult("test_suite", False, "boom")],
                ),
            )
        # The run completed; that is the assertion.


class TestInboxRetryRoundTrip:
    def test_retry_resets_the_component_to_pending(self, tmp_path: Path) -> None:
        from kstrl.manifest import Component, ComponentStatus, Manifest

        manifest_path = tmp_path / "scripts" / "kstrl" / "manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest = Manifest(
            version="1",
            spec_file="s",
            project_name="t",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    "comp-a",
                    "A",
                    "D",
                    [],
                    "prd.json",
                    "kstrl/comp-a",
                    status=ComponentStatus.FAILED.value,
                )
            ],
        )
        manifest.save(manifest_path)
        reset = manifest.reset_for_retry("comp-a")
        manifest.save(manifest_path)
        reloaded = Manifest.load(manifest_path)
        assert reloaded.components[0].status == ComponentStatus.PENDING.value
        assert "comp-a" in reset or reset == []


class TestSummaries:
    def test_summarize_counts_by_kind(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        box.add(ItemKind.HALTED_RUN, "a", dedupe_key="a")
        box.add(ItemKind.HALTED_RUN, "b", dedupe_key="b")
        box.add(ItemKind.MERGE_GATE, "c", dedupe_key="c")
        text = summarize(box.items())
        assert "3 open" in text and "2 halted_run" in text

    def test_schema_version_recorded(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        box.add(ItemKind.HALTED_RUN, "a")
        record = json.loads(box.path.read_text().strip().splitlines()[0])
        assert record["schema_version"] == 1


# --------------------------------------------------------------------------
# TUI screen
# --------------------------------------------------------------------------
class TestInboxScreen:
    def test_screen_imports_and_registers(self) -> None:
        from kstrl.tui.screens.home import HOME_COMMANDS
        from kstrl.tui.screens.inbox import InboxScreen, priority_marker

        assert InboxScreen is not None
        assert any(c.command_id == "inbox" for c in HOME_COMMANDS), [
            c.command_id for c in HOME_COMMANDS
        ]
        assert priority_marker("high").plain == "!"

    @pytest.mark.asyncio
    async def test_screen_renders_and_approves(self, tmp_path: Path) -> None:
        from textual.app import App, ComposeResult

        from kstrl.tui.screens.inbox import InboxScreen

        box = _box(tmp_path)
        box.add(
            ItemKind.POLICY_EXCEPTION,
            "comp-a: denied path",
            component="comp-a",
            dedupe_key="p1",
        )

        class _Harness(App[None]):
            def compose(self) -> ComposeResult:
                yield from ()

        app = _Harness()
        async with app.run_test() as pilot:
            await app.push_screen(InboxScreen(tmp_path))
            await mounted(pilot, lambda: app.screen, "#inbox-table")
            screen = app.screen
            assert isinstance(screen, InboxScreen)
            # The table is queryable as soon as compose mounts it, which
            # is BEFORE the screen's own on_mount reads the log.
            # Draining the screen observes that on_mount ran without
            # asserting what it found.
            await drained(
                pilot,
                screen,
                what="the inbox screen's on_mount to read the log",
            )
            assert len(screen._items) == 1
            # No wait after this one: action_approve is a direct call
            # that appends the decision to the log before it returns, so
            # the assertion below reads a file on disk rather than
            # anything the app still has to settle.
            screen.action_approve()
        assert Inbox(tmp_path, InboxConfig()).open_items() == []


# --------------------------------------------------------------------------
# Review regressions (PR #175)
# --------------------------------------------------------------------------
class TestDedupeAcrossGenerations:
    """A repeat must collapse onto the OPEN generation, not fan out."""

    def test_two_repeats_after_a_decision_share_one_item(
        self,
        tmp_path: Path,
    ) -> None:
        box = _box(tmp_path)
        first = box.add(ItemKind.HALTED_RUN, "x", dedupe_key="k")
        box.approve(first.id, actor="h")
        second = box.add(ItemKind.HALTED_RUN, "x", dedupe_key="k")
        third = box.add(ItemKind.HALTED_RUN, "x", dedupe_key="k")
        assert second.id == third.id != first.id
        open_items = box.open_items()
        assert len(open_items) == 1
        assert open_items[0].occurrences == 2

    def test_many_repeats_stay_one_open_item(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        first = box.add(ItemKind.HALTED_RUN, "x", dedupe_key="k")
        box.approve(first.id, actor="h")
        for _ in range(5):
            box.add(ItemKind.HALTED_RUN, "x", dedupe_key="k")
        assert len(box.open_items()) == 1
        assert box.open_items()[0].occurrences == 5

    def test_open_generation_wins_over_older_decided(
        self,
        tmp_path: Path,
    ) -> None:
        """The exact regression: two generations of one key exist, and
        the lookup must return the OPEN one even though the decided one
        sorts first (items() is ascending by age)."""
        box = _box(tmp_path)
        decided = box.add(ItemKind.HALTED_RUN, "x", dedupe_key="k")
        box.approve(decided.id, actor="h")
        reopened = box.add(ItemKind.HALTED_RUN, "x", dedupe_key="k")
        assert reopened.id != decided.id
        found = box.find_by_dedupe_key("k")
        assert found is not None
        assert found.id == reopened.id

    def test_all_decided_falls_back_to_the_newest(
        self,
        tmp_path: Path,
    ) -> None:
        box = _box(tmp_path)
        first = box.add(ItemKind.HALTED_RUN, "x", dedupe_key="k")
        box.approve(first.id, actor="h")
        second = box.add(ItemKind.HALTED_RUN, "x", dedupe_key="k")
        box.reject(second.id, actor="h", comment="no")
        found = box.find_by_dedupe_key("k")
        assert found is not None
        # Not the oldest: a decision on the stale generation must not be
        # what a fresh occurrence collapses onto.
        assert found.id == second.id


class TestBudgetEmitsOneItem:
    def test_budget_halt_does_not_also_emit_halted_run(
        self,
        tmp_path: Path,
    ) -> None:
        """One event must not consume two cap slots or bury its reason.

        Drives the REAL halt: the engineer's recorded spend trips
        max_total_tokens, so fail_for_budget raises budget_overrun and
        the generic halted_run fail() would otherwise add is suppressed.
        """
        _run_factory(tmp_path, max_total_tokens=100, engineer_tokens=500)
        kinds = [str(i.kind) for i in _box(tmp_path).open_items()]
        assert kinds == ["budget_overrun"]

    def test_cost_halt_also_emits_exactly_one_item(
        self,
        tmp_path: Path,
    ) -> None:
        """The cost ceiling reuses the SAME suppression, so it must not
        regress it: one budget_overrun, no duplicate halted_run."""
        _run_factory(tmp_path, max_cost_usd=0.10, engineer_cost=0.25)
        items = _box(tmp_path).open_items()
        assert [str(i.kind) for i in items] == ["budget_overrun"]

    def test_cost_item_names_the_ceiling_that_tripped(
        self,
        tmp_path: Path,
    ) -> None:
        """With two ceilings, an item that always said "token" would send
        the operator to raise the wrong knob."""
        _run_factory(tmp_path, max_cost_usd=0.10, engineer_cost=0.25)
        item = _box(tmp_path).open_items()[0]
        assert "max_cost_usd" in item.title
        assert "cost budget exceeded" in item.detail
        assert item.evidence["ceiling"] == "max_cost_usd"

    def test_token_item_still_names_the_token_ceiling(
        self,
        tmp_path: Path,
    ) -> None:
        _run_factory(tmp_path, max_total_tokens=100, engineer_tokens=500)
        item = _box(tmp_path).open_items()[0]
        assert "max_total_tokens" in item.title
        assert "token budget exceeded" in item.detail
        assert item.evidence["ceiling"] == "max_total_tokens"

    def test_zero_cost_ceiling_is_inert(self, tmp_path: Path) -> None:
        """max_cost_usd = 0 matches the max_total_tokens convention:
        unbounded, no item, the component completes."""
        _run_factory(tmp_path, max_cost_usd=0.0, engineer_cost=99.0)
        assert _box(tmp_path).open_items() == []


class TestNotifyWiring:
    def test_inbox_hook_is_silent_without_its_own_command(
        self,
        tmp_path: Path,
    ) -> None:
        """Review #5: the knob had no consumer. It has one now, but it
        must NOT borrow on_first_failure - a failing component already
        fires that hook and raises an item for the same event."""
        from kstrl.observability import NotifyConfig, NotifyHooks

        marker = tmp_path / "fired.txt"
        cmd = f"echo \"$KSTRL_NOTIFY_EVENT\" >> '{marker}'"
        hooks = NotifyHooks(NotifyConfig(on_first_failure=cmd))
        hooks.fire_inbox_item("policy_exception", "denied path", "comp-a")
        assert not marker.exists()

    def test_action_required_item_fires_its_own_command(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.observability import NotifyConfig, NotifyHooks

        marker = tmp_path / "fired.txt"
        cmd = f"echo \"$KSTRL_NOTIFY_EVENT $KSTRL_NOTIFY_COMPONENT\" >> '{marker}'"
        hooks = NotifyHooks(NotifyConfig(on_inbox_item=cmd))
        hooks.fire_inbox_item("policy_exception", "denied path", "comp-a")
        hooks.fire_inbox_item("policy_exception", "denied again", "comp-b")
        # Once per condition, as with every other hook.
        assert marker.read_text().splitlines() == [
            "inbox_policy_exception comp-a",
        ]

    def test_notifiable_excludes_low_priority_kinds(self, tmp_path: Path) -> None:
        box = _box(tmp_path)
        box.add(ItemKind.CALIBRATION_DRIFT, "drift", dedupe_key="d")
        assert notifiable(box.items()) == []


class TestMergeGateIsTheHumanGate:
    """The gate is the pre-merge checkpoint, not the post-merge timeout."""

    def test_parked_decision_exists(self) -> None:
        from kstrl.pipeline import CheckpointDecision

        assert CheckpointDecision.PARKED.value == "parked"

    def test_non_interactive_gate_parks_and_emits(self, tmp_path: Path) -> None:
        # pause_before_pr_merge with no interactive UI must NOT merge:
        # it parks the component and files exactly one merge_gate item.
        _run_factory(
            tmp_path,
            create_prs=True,
            pause_before_pr_merge=True,
        )
        items = _box(tmp_path).open_items()
        assert [str(i.kind) for i in items] == ["merge_gate"]
        assert items[0].component == "comp-a"
