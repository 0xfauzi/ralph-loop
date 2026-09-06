"""R8.6 PR 3: GitHub Issues intake adapter tests.

Every test here stubs `gh`. The live round-trip against a real repo is a
separate, deliberate exercise (recorded in the PR), because a suite that
hit the API would be slow, rate-limited, and would post public comments
on every run.

The properties that matter most are the ones that protect the queue from
the front-end rather than the other way round: a GitHub outage must not
stall intake, a re-seen issue must not re-enqueue, and no label or config
may grant a remote item auto-merge.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.intake_github import (
    MAX_SPEC_CHARS,
    Decision,
    GhResult,
    GitHubIntakeConfig,
    IntakeError,
    ProcessedLedger,
    RemoteIssue,
    SyncResult,
    apply_state_label,
    issue_number_from_ref,
    parse_issue_list,
    plan_sync,
    poll_queued,
    post_comment,
    repo_from_ref,
    report_outcome,
    resolve_repo,
    run_gh,
    spec_from_issue,
    sync,
    verify_authorization,
)
from kstrl.workqueue import (
    ItemSource,
    ItemState,
    MergeDisposition,
    Queue,
    QueueConfig,
)

REPO = "0xfauzi/claude-skills"


#: Nothing here is about flow control; the fixture's docstring in
#: tests/conftest.py says why the R10.7 bound has to be held open.
pytestmark = pytest.mark.usefixtures("no_open_prs")


def _queue(root: Path, **kwargs: object) -> Queue:
    return Queue(root, QueueConfig(**kwargs))  # type: ignore[arg-type]


def _config(**kwargs: object) -> GitHubIntakeConfig:
    base: dict[str, object] = {"enabled": True, "repo": REPO}
    base.update(kwargs)
    return GitHubIntakeConfig(**base)  # type: ignore[arg-type]


def _issue_payload(*issues: dict[str, object]) -> str:
    return json.dumps(list(issues))


def _issue(
    number: int,
    title: str = "Add a thing",
    body: str = "Build the thing.",
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/{REPO}/issues/{number}",
        "labels": [{"name": "kstrl:queued"}],
    }


def _auth_payload(
    *,
    labeled_at: str = "2026-07-30T10:00:00Z",
    last_edited_at: str | None = None,
    actor: str = "0xfauzi",
    label: str = "kstrl:queued",
    nodes: list[dict[str, object]] | None = None,
) -> str:
    """A GraphQL authorization response."""
    timeline = (
        nodes
        if nodes is not None
        else [
            {"createdAt": labeled_at, "label": {"name": label}, "actor": {"login": actor}},
        ]
    )
    return json.dumps(
        {
            "data": {
                "repository": {
                    "issue": {
                        "lastEditedAt": last_edited_at,
                        "timelineItems": {"nodes": timeline},
                    }
                }
            }
        }
    )


class _GhStub:
    """Routes canned results by `gh` subcommand and records every argv.

    Routing rather than a strict result queue: the adapter's call sequence
    changed twice during review (a checkout-repo probe and a GraphQL
    authorization call were added), and an order-sensitive stub made every
    test fail for reasons unrelated to what it was testing.
    """

    def __init__(
        self,
        *,
        issues: str | GhResult = "[]",
        checkout: str | GhResult = REPO,
        auth: str | GhResult | None = None,
        edit: GhResult | None = None,
        comment: GhResult | None = None,
    ) -> None:
        self.issues = issues
        self.checkout = checkout
        self.auth = auth if auth is not None else _auth_payload()
        self.edit = edit or GhResult(ok=True)
        self.comment = comment or GhResult(ok=True)
        self.calls: list[list[str]] = []

    @staticmethod
    def _as_result(value: str | GhResult) -> GhResult:
        return (
            value
            if isinstance(value, GhResult)
            else GhResult(
                ok=True,
                stdout=value,
            )
        )

    def __call__(
        self,
        args: list[str],
        *,
        timeout: float,
        cwd: Path | None = None,
    ) -> GhResult:
        self.calls.append(list(args))
        head = args[:2]
        if head == ["repo", "view"]:
            value = self.checkout
            if isinstance(value, GhResult):
                return value
            return GhResult(ok=True, stdout=json.dumps({"nameWithOwner": value}))
        if head == ["issue", "list"]:
            return self._as_result(self.issues)
        if head == ["api", "graphql"]:
            return self._as_result(self.auth)
        if head == ["issue", "edit"]:
            return self.edit
        if head == ["issue", "comment"]:
            return self.comment
        return GhResult(ok=True, stdout="")

    def argv_for(self, *head: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(head)] == list(head)]


# --------------------------------------------------------------------------
# run_gh: every failure becomes a value
# --------------------------------------------------------------------------


class TestRunGhNeverRaises:
    """The adapter is additive by contract, so nothing may escape."""

    def test_a_missing_binary_is_reported(self) -> None:
        with patch("shutil.which", return_value=None):
            result = run_gh(["issue", "list"], timeout=1.0)
        assert not result.ok
        assert "not installed" in result.error

    def test_a_timeout_is_reported(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/gh"):
            with patch(
                "kstrl.intake_github.subprocess.run",
                side_effect=subprocess.TimeoutExpired("gh", 1.0),
            ):
                result = run_gh(["issue", "list"], timeout=1.0)
        assert not result.ok
        assert "timed out" in result.error

    def test_an_os_error_is_reported(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/gh"):
            with patch(
                "kstrl.intake_github.subprocess.run",
                side_effect=OSError("exec format error"),
            ):
                result = run_gh(["issue", "list"], timeout=1.0)
        assert not result.ok
        assert "could not run" in result.error

    def test_a_nonzero_exit_is_reported_with_stderr(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gh"],
            returncode=1,
            stdout="",
            stderr="HTTP 403 rate limited",
        )
        with patch("shutil.which", return_value="/usr/bin/gh"):
            with patch(
                "kstrl.intake_github.subprocess.run",
                return_value=completed,
            ):
                result = run_gh(["issue", "list"], timeout=1.0)
        assert not result.ok
        assert "rate limited" in result.error

    def test_success_carries_stdout(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gh"],
            returncode=0,
            stdout="[]",
            stderr="",
        )
        with patch("shutil.which", return_value="/usr/bin/gh"):
            with patch(
                "kstrl.intake_github.subprocess.run",
                return_value=completed,
            ):
                result = run_gh(["issue", "list"], timeout=1.0)
        assert result.ok
        assert result.stdout == "[]"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class TestParseIssueList:
    def test_parses_a_normal_payload(self) -> None:
        issues, error = parse_issue_list(_issue_payload(_issue(7), _issue(3)))
        assert error == ""
        assert [i.number for i in issues] == [3, 7], "oldest first"

    def test_malformed_json_is_an_ERROR_not_an_empty_poll(self) -> None:
        """#187 F7: this used to be indistinguishable from a healthy `[]`."""
        issues, error = parse_issue_list("{not json")
        assert issues == []
        assert "could not parse" in error

    def test_a_non_list_payload_is_an_error(self) -> None:
        issues, error = parse_issue_list('{"number": 1}')
        assert issues == []
        assert "expected a list" in error

    def test_a_valid_empty_payload_is_not_an_error(self) -> None:
        assert parse_issue_list("[]") == ([], "")

    def test_one_bad_entry_does_not_discard_the_rest(self) -> None:
        """A single unparseable issue must not stall the whole queue."""
        payload = json.dumps([{"title": "no number"}, _issue(5)])
        issues, error = parse_issue_list(payload)
        assert [i.number for i in issues] == [5]
        assert error == "", "per-entry tolerance survives the strict top level"

    def test_labels_are_extracted(self) -> None:
        issues, _ = parse_issue_list(_issue_payload(_issue(1)))
        assert issues[0].labels == ("kstrl:queued",)

    def test_a_missing_title_falls_back(self) -> None:
        issues, _ = parse_issue_list(json.dumps([{"number": 9}]))
        assert issues[0].title == "issue #9"

    def test_a_boolean_number_is_rejected(self) -> None:
        issues, error = parse_issue_list(json.dumps([{"number": True}]))
        assert issues == []
        assert error == ""


# --------------------------------------------------------------------------
# Spec construction
# --------------------------------------------------------------------------


class TestSpecFromIssue:
    def test_carries_provenance(self) -> None:
        """A spec that cannot be traced back to a request is a liability."""
        spec = spec_from_issue(RemoteIssue(12, "Add X", "Body text", "u"), REPO)
        assert "# Add X" in spec
        assert f"{REPO}#12" in spec
        assert "Body text" in spec

    def test_truncates_a_pathological_body_and_says_so(self) -> None:
        spec = spec_from_issue(
            RemoteIssue(1, "T", "x" * (MAX_SPEC_CHARS * 2), "u"),
            REPO,
        )
        assert len(spec) <= MAX_SPEC_CHARS + 200
        assert "truncated by kstrl" in spec, "truncation must never be silent"

    def test_a_short_body_is_untouched(self) -> None:
        spec = spec_from_issue(RemoteIssue(1, "T", "short", "u"), REPO)
        assert "truncated" not in spec


# --------------------------------------------------------------------------
# The processed-ids ledger
# --------------------------------------------------------------------------


class TestProcessedLedger:
    def test_round_trips(self, tmp_path: Path) -> None:
        ledger = ProcessedLedger(tmp_path).load()
        ledger.record(f"{REPO}#1", item_id="q-1", when="2026-07-30T00:00:00Z")
        assert ProcessedLedger(tmp_path).load().contains(f"{REPO}#1")

    def test_an_absent_ledger_is_empty(self, tmp_path: Path) -> None:
        assert not ProcessedLedger(tmp_path).load().contains(f"{REPO}#1")

    def test_a_corrupt_ledger_reads_as_empty(self, tmp_path: Path) -> None:
        """Fails OPEN, deliberately: stalling intake is worse than a re-poll.

        The blast radius is bounded by find_by_source_ref, the per-sync
        cap, and the daily budget - unlike the SPEND ledger, where failing
        open disabled the only queue-wide cap.
        """
        ledger = ProcessedLedger(tmp_path).load()
        ledger.record(f"{REPO}#1", item_id="q-1", when="t")
        ledger.path.write_text("{corrupt")
        assert not ProcessedLedger(tmp_path).load().contains(f"{REPO}#1")

    def test_forget_allows_readmission(self, tmp_path: Path) -> None:
        ledger = ProcessedLedger(tmp_path).load()
        ledger.record(f"{REPO}#1", item_id="q-1", when="t")
        assert ledger.forget(f"{REPO}#1")
        assert not ledger.contains(f"{REPO}#1")

    def test_forget_of_an_unknown_ref_is_false(self, tmp_path: Path) -> None:
        assert not ProcessedLedger(tmp_path).load().forget("nope#1")


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------


class TestSync:
    def test_enqueues_a_labelled_issue(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub(issues=_issue_payload(_issue(4)))
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(), tmp_path)
        assert result.enqueued == (f"{REPO}#4",)
        items = queue.items()
        assert len(items) == 1
        assert items[0].source is ItemSource.GITHUB
        assert items[0].source_ref == f"{REPO}#4"
        assert items[0].target_repo == REPO

    def test_a_remote_item_can_never_auto_merge(self, tmp_path: Path) -> None:
        """No label and no config setting may delete the human merge gate."""
        queue = _queue(tmp_path)
        labelled = _issue(4)
        labelled["labels"] = [
            {"name": "kstrl:queued"},
            {"name": "auto-merge"},
            {"name": "kstrl:auto-merge"},
        ]
        stub = _GhStub(issues=_issue_payload(labelled))
        with patch("kstrl.intake_github.run_gh", stub):
            sync(queue, _config(), tmp_path)
        assert queue.items()[0].merge_disposition is MergeDisposition.STOP_AT_PR

    def test_a_disabled_adapter_does_nothing(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(enabled=False), tmp_path)
        assert result.enqueued == ()
        assert stub.calls == [], "a disabled adapter must not call gh at all"
        assert queue.items() == []

    def test_a_poll_failure_leaves_the_queue_untouched(
        self,
        tmp_path: Path,
    ) -> None:
        """A GitHub outage must never stall or corrupt the local queue."""
        queue = _queue(tmp_path)
        queue.add("# local\n", title="local work")
        stub = _GhStub(issues=GhResult(ok=False, error="HTTP 503"))
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(), tmp_path)
        assert not result.ok
        assert "503" in result.errors[0]
        assert len(queue.items()) == 1, "the local item is undisturbed"

    def test_an_already_processed_issue_is_not_re_enqueued(
        self,
        tmp_path: Path,
    ) -> None:
        """The half find_by_source_ref cannot cover: the item is GONE."""
        queue = _queue(tmp_path)
        payload = _issue_payload(_issue(4))
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(issues=payload),
        ):
            sync(queue, _config(), tmp_path)
        # The item completes and leaves the queue entirely.
        item = queue.items()[0]
        queue.remove(queue.finish_ok(queue.start(queue.lease(item))))
        assert queue.items() == []

        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(issues=payload),
        ):
            second = sync(queue, _config(), tmp_path)
        assert second.enqueued == ()
        assert second.skipped[f"{REPO}#4"] == "already processed"
        assert queue.items() == []

    def test_an_issue_already_in_the_queue_is_skipped(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        payload = _issue_payload(_issue(4))
        for _ in range(2):
            with patch(
                "kstrl.intake_github.run_gh",
                _GhStub(issues=payload),
            ):
                result = sync(queue, _config(), tmp_path)
        assert len(queue.items()) == 1
        assert f"{REPO}#4" in result.skipped

    def test_a_lost_ledger_does_not_duplicate_a_queued_item(
        self,
        tmp_path: Path,
    ) -> None:
        """The claim the ledger's fail-open rests on.

        ProcessedLedger reads a corrupt file as EMPTY, and the stated
        justification is that find_by_source_ref still catches an item
        that is in the queue. That is the ONLY scenario where the
        in-queue check does work the ledger cannot, so it is the only
        scenario that can pin it: with both guards intact and the ledger
        gone, a re-sync must still not duplicate.
        """
        queue = _queue(tmp_path)
        payload = _issue_payload(_issue(4))
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(issues=payload),
        ):
            sync(queue, _config(), tmp_path)
        assert len(queue.items()) == 1

        # Lose the ledger entirely; the item is still queued.
        ProcessedLedger(tmp_path).path.unlink()

        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(issues=payload),
        ):
            second = sync(queue, _config(), tmp_path)
        assert second.enqueued == ()
        assert second.skipped[f"{REPO}#4"] == "already in the queue"
        assert len(queue.items()) == 1, "a lost ledger must not duplicate work"

    def test_an_empty_body_is_skipped_without_spending(
        self,
        tmp_path: Path,
    ) -> None:
        """Paying an architect call to learn the issue says nothing is waste."""
        queue = _queue(tmp_path)
        stub = _GhStub(issues=_issue_payload(_issue(4, body="   ")))
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(), tmp_path)
        assert result.enqueued == ()
        assert "empty" in result.skipped[f"{REPO}#4"]
        assert queue.items() == []

    def test_the_per_sync_cap_bounds_intake(self, tmp_path: Path) -> None:
        """A label applied to fifty issues must not queue fifty runs."""
        queue = _queue(tmp_path)
        payload = _issue_payload(*[_issue(n) for n in range(1, 9)])
        stub = _GhStub(issues=payload)
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(max_items_per_sync=3), tmp_path)
        assert len(result.enqueued) == 3
        assert len(queue.items()) == 3
        assert any("cap of 3" in r for r in result.skipped.values())

    def test_oldest_issues_are_admitted_first(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        payload = _issue_payload(_issue(9), _issue(2), _issue(5))
        stub = _GhStub(issues=payload)
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(max_items_per_sync=2), tmp_path)
        assert result.enqueued == (f"{REPO}#2", f"{REPO}#5")

    def test_admission_does_NOT_claim_the_item_is_running(
        self,
        tmp_path: Path,
    ) -> None:
        """#187 F8: the remote label must reflect the real queue state.

        Admission puts the item in QUEUED, not RUNNING. Labelling it
        `running` here made a paused or backlogged item read as running
        for hours with no process behind it. `serve` applies `running`
        when the item actually starts.
        """
        queue = _queue(tmp_path)
        stub = _GhStub(issues=_issue_payload(_issue(4)))
        with patch("kstrl.intake_github.run_gh", stub):
            sync(queue, _config(), tmp_path)
        assert queue.items()[0].state is ItemState.QUEUED
        assert stub.argv_for("issue", "edit") == [], (
            "admission must not relabel; serve drives labels from transitions"
        )

    def test_a_gh_edit_failure_cannot_lose_an_admitted_item(
        self,
        tmp_path: Path,
    ) -> None:
        """Remote writeback is best-effort; the local queue is the record."""
        queue = _queue(tmp_path)
        stub = _GhStub(
            issues=_issue_payload(_issue(4)),
            edit=GhResult(ok=False, error="HTTP 403"),
        )
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(), tmp_path)
        assert result.enqueued == (f"{REPO}#4",)
        assert len(queue.items()) == 1

    def test_dry_run_is_side_effect_FREE(self, tmp_path: Path) -> None:
        """#187 F4: dry_run suppressed only the remote writes.

        It still called queue.add and ledger.record, so with `ks serve`
        active a supposed dry run could launch paid work.
        """
        queue = _queue(tmp_path)
        stub = _GhStub(issues=_issue_payload(_issue(4)))
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(dry_run=True), tmp_path)
        assert queue.items() == [], "no local item may be created"
        assert not ProcessedLedger(tmp_path).path.exists(), "no ledger write"
        assert result.enqueued == (), "nothing was actually enqueued"
        assert result.would_enqueue == (f"{REPO}#4",), "but it says what it would"
        assert stub.argv_for("issue", "edit") == []
        assert stub.argv_for("issue", "comment") == []

    def test_the_polled_count_is_reported(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        payload = _issue_payload(_issue(1), _issue(2))
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(issues=payload),
        ):
            result = sync(queue, _config(max_items_per_sync=1), tmp_path)
        assert result.polled == 2
        assert len(result.enqueued) == 1


# --------------------------------------------------------------------------
# Repo resolution and refs
# --------------------------------------------------------------------------


class TestRepoResolution:
    def test_an_explicit_repo_needs_no_gh_call(self, tmp_path: Path) -> None:
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            repo, error = resolve_repo(_config(), tmp_path)
        assert (repo, error) == (REPO, "")
        assert stub.calls == []

    def test_resolution_falls_back_to_the_checkout(self, tmp_path: Path) -> None:
        stub = _GhStub(checkout=REPO)
        with patch("kstrl.intake_github.run_gh", stub):
            repo, error = resolve_repo(_config(repo=""), tmp_path)
        assert (repo, error) == (REPO, "")

    def test_a_resolution_failure_is_reported_not_raised(
        self,
        tmp_path: Path,
    ) -> None:
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(checkout=GhResult(ok=False, error="not a repo")),
        ):
            repo, error = resolve_repo(_config(repo=""), tmp_path)
        assert repo == ""
        assert "could not resolve" in error

    def test_unparseable_resolution_output_is_reported(
        self,
        tmp_path: Path,
    ) -> None:
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(checkout=GhResult(ok=True, stdout="{bad")),
        ):
            _repo, error = resolve_repo(_config(repo=""), tmp_path)
        assert "could not parse" in error

    @pytest.mark.parametrize(
        ("ref", "number", "repo"),
        [
            (f"{REPO}#12", 12, REPO),
            ("owner/name#1", 1, "owner/name"),
            ("no-hash", 0, ""),
            ("owner/name#abc", 0, "owner/name"),
        ],
    )
    def test_ref_parsing(self, ref: str, number: int, repo: str) -> None:
        assert issue_number_from_ref(ref) == number
        assert repo_from_ref(ref) == repo


# --------------------------------------------------------------------------
# Writeback
# --------------------------------------------------------------------------


class TestWriteback:
    def _github_item(self, tmp_path: Path) -> object:
        queue = _queue(tmp_path)
        return queue.add(
            "# spec\n",
            title="t",
            source=ItemSource.GITHUB,
            source_ref=f"{REPO}#4",
            target_repo=REPO,
        )

    def test_reports_a_poison_with_a_recovery_hint(
        self,
        tmp_path: Path,
    ) -> None:
        item = self._github_item(tmp_path)
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            error = report_outcome(
                item,
                state="poison",
                detail="tests failed",  # type: ignore[arg-type]
                config=_config(),
                root_dir=tmp_path,
            )
        assert error == ""
        comment = [c for c in stub.calls if "comment" in c]
        assert comment
        body = comment[0][comment[0].index("--body") + 1]
        assert "poison" in body
        assert "tests failed" in body
        assert "reset-attempts" in body, "say what the human can do"

    def test_a_done_comment_states_the_merge_gate(
        self,
        tmp_path: Path,
    ) -> None:
        item = self._github_item(tmp_path)
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            report_outcome(
                item,
                state="done",
                detail="completed",  # type: ignore[arg-type]
                config=_config(),
                root_dir=tmp_path,
            )
        comment = [c for c in stub.calls if "comment" in c][0]
        body = comment[comment.index("--body") + 1]
        assert "stop at the PR" in body

    def test_a_local_item_is_never_reported(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = queue.add("# spec\n", title="local")
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            error = report_outcome(
                item,
                state="done",
                detail="",
                config=_config(),
                root_dir=tmp_path,
            )
        assert error == ""
        assert stub.calls == []

    def test_a_disabled_adapter_reports_nothing(self, tmp_path: Path) -> None:
        item = self._github_item(tmp_path)
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            report_outcome(
                item,
                state="done",
                detail="",  # type: ignore[arg-type]
                config=_config(enabled=False),
                root_dir=tmp_path,
            )
        assert stub.calls == []

    def test_a_writeback_failure_is_returned_not_raised(
        self,
        tmp_path: Path,
    ) -> None:
        item = self._github_item(tmp_path)
        stub = _GhStub(
            edit=GhResult(ok=False, error="HTTP 403"),
            comment=GhResult(ok=False, error="HTTP 500"),
        )
        with patch("kstrl.intake_github.run_gh", stub):
            error = report_outcome(
                item,
                state="poison",
                detail="x",  # type: ignore[arg-type]
                config=_config(),
                root_dir=tmp_path,
            )
        assert "403" in error and "500" in error

    def test_an_unmappable_ref_is_reported(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = queue.add(
            "# spec\n",
            title="t",
            source=ItemSource.GITHUB,
            source_ref="garbage",
        )
        error = report_outcome(
            item,
            state="done",
            detail="",
            config=_config(repo=""),
            root_dir=tmp_path,
        )
        assert "cannot map" in error

    def test_the_state_label_replaces_every_managed_label(
        self,
        tmp_path: Path,
    ) -> None:
        """An issue must never carry two contradictory kstrl states."""
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            apply_state_label(_config(), REPO, 4, "done", tmp_path)
        argv = stub.calls[0]
        assert argv[argv.index("--add-label") + 1] == "kstrl:done"
        removed = [argv[i + 1] for i, a in enumerate(argv) if a == "--remove-label"]
        assert set(removed) == {
            "kstrl:queued",
            "kstrl:running",
            "kstrl:failed",
            "kstrl:poison",
        }

    def test_comments_can_be_switched_off(self, tmp_path: Path) -> None:
        stub = _GhStub()
        with patch("kstrl.intake_github.run_gh", stub):
            error = post_comment(
                _config(comment_on_result=False),
                REPO,
                4,
                "body",
                tmp_path,
            )
        assert error == ""
        assert stub.calls == []


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class TestConfig:
    def test_off_by_default(self) -> None:
        config = GitHubIntakeConfig()
        assert not config.enabled
        assert config.queued_label == "kstrl:queued"
        assert config.max_items_per_sync == 5

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"queued_label": "  "},
            {"max_items_per_sync": 0},
            {"timeout_seconds": 0},
            {"repo": "not-a-repo"},
            {"repo": "a/b/c"},
        ],
    )
    def test_invalid_values_are_rejected(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(IntakeError):
            GitHubIntakeConfig(**kwargs)  # type: ignore[arg-type]

    def test_managed_labels_cover_every_state(self) -> None:
        labels = GitHubIntakeConfig().managed_labels
        assert labels == (
            "kstrl:queued",
            "kstrl:running",
            "kstrl:done",
            "kstrl:failed",
            "kstrl:poison",
        )

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_INTAKE_GITHUB_ENABLED", "1")
        monkeypatch.setenv("KSTRL_INTAKE_GITHUB_REPO", REPO)
        monkeypatch.setenv("KSTRL_INTAKE_GITHUB_MAX_ITEMS", "2")
        config = GitHubIntakeConfig.from_env()
        assert config.enabled
        assert config.repo == REPO
        assert config.max_items_per_sync == 2

    def test_load_reads_the_toml_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            f'[intake_github]\nenabled = true\nrepo = "{REPO}"\nmax_items_per_sync = 7\n'
        )
        config = GitHubIntakeConfig.load(tmp_path)
        assert config.enabled
        assert config.repo == REPO
        assert config.max_items_per_sync == 7

    def test_env_beats_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[intake_github]\nmax_items_per_sync = 7\n")
        monkeypatch.setenv("KSTRL_INTAKE_GITHUB_MAX_ITEMS", "1")
        assert GitHubIntakeConfig.load(tmp_path).max_items_per_sync == 1

    def test_defaults_without_a_config_file(self, tmp_path: Path) -> None:
        assert not GitHubIntakeConfig.load(tmp_path).enabled


# --------------------------------------------------------------------------
# Polling argv
# --------------------------------------------------------------------------


class TestPollArgv:
    def test_polls_open_issues_oldest_first_with_the_trigger_label(
        self,
        tmp_path: Path,
    ) -> None:
        """#187 F6: FIFO must be requested, not inferred from one page."""
        stub = _GhStub(issues="[]")
        with patch("kstrl.intake_github.run_gh", stub):
            poll_queued(_config(), REPO, tmp_path)
        argv = stub.calls[0]
        assert argv[:2] == ["issue", "list"]
        assert argv[argv.index("--repo") + 1] == REPO
        search = argv[argv.index("--search") + 1]
        assert "kstrl:queued" in search
        assert "state:open" in search
        assert "sort:created-asc" in search, "ordering must be requested"

    def test_a_custom_label_is_honored(self, tmp_path: Path) -> None:
        stub = _GhStub(issues="[]")
        with patch("kstrl.intake_github.run_gh", stub):
            poll_queued(_config(queued_label="factory:go"), REPO, tmp_path)
        assert "factory:go" in stub.calls[0][stub.calls[0].index("--search") + 1]

    def test_a_poll_error_is_returned(self, tmp_path: Path) -> None:
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(issues=GhResult(ok=False, error="boom")),
        ):
            issues, error, _exhausted = poll_queued(_config(), REPO, tmp_path)
        assert issues == []
        assert error == "boom"

    def test_a_malformed_page_is_returned_as_an_error(
        self,
        tmp_path: Path,
    ) -> None:
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(issues=GhResult(ok=True, stdout="{bad")),
        ):
            issues, error, _exhausted = poll_queued(_config(), REPO, tmp_path)
        assert issues == []
        assert "could not parse" in error

    def test_a_short_page_stops_paging(self, tmp_path: Path) -> None:
        """A page smaller than the limit means the inbox is exhausted."""
        stub = _GhStub(issues=_issue_payload(_issue(1), _issue(2)))
        with patch("kstrl.intake_github.run_gh", stub):
            issues, error, exhausted = poll_queued(_config(), REPO, tmp_path)
        assert error == ""
        assert len(issues) == 2
        assert exhausted, "a short page means the inbox is exhausted"
        assert len(stub.argv_for("issue", "list")) == 1, "no needless second page"

    def test_the_window_grows_when_every_issue_is_skippable(
        self,
        tmp_path: Path,
    ) -> None:
        """#187 F6: skips do not consume the cap, so the window must grow.

        A full page of already-processed issues used to exhaust a fixed
        window, leaving an eligible issue just outside it invisible
        indefinitely.
        """
        queue = _queue(tmp_path)
        ledger = ProcessedLedger(tmp_path).load()
        for n in range(1, 31):
            ledger.record(f"{REPO}#{n}", item_id=f"q-{n}", when="t")
        # A full first page (30) of processed issues, plus one eligible.
        full_page = _issue_payload(*[_issue(n) for n in range(1, 31)])
        second_page = _issue_payload(*[_issue(n) for n in range(1, 32)])
        pages = [
            GhResult(ok=True, stdout=full_page),
            GhResult(ok=True, stdout=second_page),
        ]

        class _Paging(_GhStub):
            def __call__(self, args, *, timeout, cwd=None):  # type: ignore[no-untyped-def]
                if args[:2] == ["issue", "list"]:
                    self.calls.append(list(args))
                    return (
                        pages.pop(0)
                        if pages
                        else GhResult(
                            ok=True,
                            stdout=second_page,
                        )
                    )
                return super().__call__(args, timeout=timeout, cwd=cwd)

        stub = _Paging()
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(max_items_per_sync=1), tmp_path)
        assert len(stub.argv_for("issue", "list")) >= 2, "must page past skips"
        assert result.enqueued == (f"{REPO}#31",), (
            "the eligible issue beyond the first page must be found"
        )


class TestRepoMatch:
    """#187 F3: target_repo is metadata; serve always runs in root_dir."""

    def test_a_mismatched_inbox_is_refused(self, tmp_path: Path) -> None:
        """Admitting B's issue in checkout A would PR into the wrong repo."""
        queue = _queue(tmp_path)
        stub = _GhStub(
            issues=_issue_payload(_issue(4)),
            checkout="someone/else",
        )
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(repo=REPO), tmp_path)
        assert result.enqueued == ()
        assert queue.items() == []
        assert any("cross-repository" in e for e in result.errors)

    def test_a_matching_inbox_is_admitted(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub(issues=_issue_payload(_issue(4)), checkout=REPO)
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(repo=REPO), tmp_path)
        assert result.enqueued == (f"{REPO}#4",)

    def test_the_match_is_case_insensitive(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub(issues=_issue_payload(_issue(4)), checkout=REPO.upper())
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(repo=REPO), tmp_path)
        assert result.enqueued == (f"{REPO}#4",)

    def test_an_unresolvable_checkout_refuses_rather_than_guessing(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub(
            issues=_issue_payload(_issue(4)),
            checkout=GhResult(ok=False, error="not a git repo"),
        )
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(repo=REPO), tmp_path)
        assert result.enqueued == ()
        assert any("without confirming" in e for e in result.errors)


class TestAuthorizationBinding:
    """#187 F1: bind the label to the bytes it authorized."""

    def test_an_unedited_issue_is_authorized(self, tmp_path: Path) -> None:
        stub = _GhStub(auth=_auth_payload(last_edited_at=None))
        with patch("kstrl.intake_github.run_gh", stub):
            auth = verify_authorization(_config(), REPO, 4, tmp_path)
        assert auth.ok
        assert auth.actor == "0xfauzi"

    def test_an_edit_BEFORE_the_label_is_fine(self, tmp_path: Path) -> None:
        stub = _GhStub(
            auth=_auth_payload(
                labeled_at="2026-07-30T12:00:00Z",
                last_edited_at="2026-07-30T11:00:00Z",
            )
        )
        with patch("kstrl.intake_github.run_gh", stub):
            assert verify_authorization(_config(), REPO, 4, tmp_path).ok

    def test_an_edit_AFTER_the_label_is_refused(self, tmp_path: Path) -> None:
        """The attack: benign body, get it labelled, then rewrite it."""
        stub = _GhStub(
            auth=_auth_payload(
                labeled_at="2026-07-30T10:00:00Z",
                last_edited_at="2026-07-30T11:00:00Z",
            )
        )
        with patch("kstrl.intake_github.run_gh", stub):
            auth = verify_authorization(_config(), REPO, 4, tmp_path)
        assert not auth.ok
        assert "edited at" in auth.reason
        assert "re-apply the label" in auth.reason

    def test_a_missing_label_event_is_refused(self, tmp_path: Path) -> None:
        """A label of unknown provenance is not authorization."""
        stub = _GhStub(auth=_auth_payload(nodes=[]))
        with patch("kstrl.intake_github.run_gh", stub):
            auth = verify_authorization(_config(), REPO, 4, tmp_path)
        assert not auth.ok
        assert "no 'kstrl:queued' labelling event" in auth.reason

    def test_only_the_trigger_labels_events_count(self, tmp_path: Path) -> None:
        stub = _GhStub(
            auth=_auth_payload(
                nodes=[
                    {
                        "createdAt": "2026-07-30T12:00:00Z",
                        "label": {"name": "kstrl:running"},
                        "actor": {"login": "bot"},
                    },
                ]
            )
        )
        with patch("kstrl.intake_github.run_gh", stub):
            auth = verify_authorization(_config(), REPO, 4, tmp_path)
        assert not auth.ok, "a state label is not the trigger"

    @pytest.mark.parametrize(
        "auth_result",
        [
            GhResult(ok=False, error="HTTP 502"),
            GhResult(ok=True, stdout="{bad"),
            GhResult(ok=True, stdout=json.dumps({"data": {"repository": {}}})),
        ],
    )
    def test_every_uncertainty_fails_closed(
        self,
        tmp_path: Path,
        auth_result: GhResult,
    ) -> None:
        """ "We could not check" is not evidence the bytes are authorized."""
        with patch("kstrl.intake_github.run_gh", _GhStub(auth=auth_result)):
            assert not verify_authorization(_config(), REPO, 4, tmp_path).ok

    def test_sync_refuses_an_issue_edited_after_authorization(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub(
            issues=_issue_payload(_issue(4)),
            auth=_auth_payload(
                labeled_at="2026-07-30T10:00:00Z",
                last_edited_at="2026-07-30T11:00:00Z",
            ),
        )
        with patch("kstrl.intake_github.run_gh", stub):
            result = sync(queue, _config(), tmp_path)
        assert result.enqueued == ()
        assert queue.items() == []
        assert "edited at" in result.skipped[f"{REPO}#4"]

    def test_verification_can_be_switched_off_for_tests(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub(issues=_issue_payload(_issue(4)))
        with patch("kstrl.intake_github.run_gh", stub):
            sync(queue, _config(), tmp_path, verify=False)
        assert stub.argv_for("api", "graphql") == []
        assert len(queue.items()) == 1


class TestTransactionalAdmission:
    """#187 F5: admission and dedupe must commit together."""

    def test_a_ledger_failure_rolls_back_the_queued_item(
        self,
        tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        stub = _GhStub(issues=_issue_payload(_issue(4)))
        with patch("kstrl.intake_github.run_gh", stub):
            with patch.object(
                ProcessedLedger,
                "record",
                side_effect=OSError("disk full"),
            ):
                result = sync(queue, _config(), tmp_path)
        assert queue.items() == [], "a half-admitted item must not survive"
        assert result.enqueued == ()
        assert any("rolled back" in e for e in result.errors)

    def test_a_ledger_failure_does_not_escape_as_an_exception(
        self,
        tmp_path: Path,
    ) -> None:
        """It used to abort the whole batch by propagating OSError."""
        queue = _queue(tmp_path)
        stub = _GhStub(issues=_issue_payload(_issue(4), _issue(5)))
        with patch("kstrl.intake_github.run_gh", stub):
            with patch.object(
                ProcessedLedger,
                "record",
                side_effect=OSError("disk full"),
            ):
                result = sync(queue, _config(max_items_per_sync=2), tmp_path)
        assert len(result.errors) == 2, "both items reported, batch not aborted"


class TestPlanner:
    """One decision tree, shared by sync and the CLI's --dry-run."""

    def test_the_cap_is_applied_in_the_plan(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        issues, _ = parse_issue_list(
            _issue_payload(*[_issue(n) for n in range(1, 6)]),
        )
        planned = plan_sync(
            queue,
            _config(max_items_per_sync=2),
            REPO,
            issues,
            ProcessedLedger(tmp_path).load(),
        )
        assert sum(1 for p in planned if p.decision.admits) == 2
        assert [p.decision for p in planned][2:] == [Decision.SKIP_CAP] * 3

    def test_the_planner_mutates_nothing(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        issues, _ = parse_issue_list(_issue_payload(_issue(1)))
        plan_sync(
            queue,
            _config(),
            REPO,
            issues,
            ProcessedLedger(tmp_path).load(),
        )
        assert queue.items() == []
        assert not ProcessedLedger(tmp_path).path.exists()

    def test_every_skip_carries_a_reason(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        issues, _ = parse_issue_list(
            _issue_payload(_issue(1, body=""), _issue(2)),
        )
        planned = plan_sync(
            queue,
            _config(max_items_per_sync=0 + 1),
            REPO,
            issues,
            ProcessedLedger(tmp_path).load(),
        )
        for entry in planned:
            if not entry.decision.admits:
                assert entry.reason.strip(), f"{entry.decision} has no reason"


# --------------------------------------------------------------------------
# serve integration
# --------------------------------------------------------------------------


class TestServeWriteback:
    """serve must report outcomes without letting the front-end break it."""

    def test_a_writeback_exception_cannot_break_the_cycle(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.serve import RunOutcome, RunSpend, serve_cycle

        queue = _queue(tmp_path)
        queue.add(
            "# spec\n",
            title="remote",
            source=ItemSource.GITHUB,
            source_ref=f"{REPO}#4",
            target_repo=REPO,
        )

        def runner(**kwargs: object) -> RunOutcome:
            run_dir = tmp_path / ".kstrl" / "runs" / "factory-x"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "events.jsonl").touch()
            return RunOutcome(returncode=0)

        with patch(
            "kstrl.serve.read_run_spend",
            lambda root, run_id: RunSpend(),
        ):
            with patch(
                "kstrl.intake_github.GitHubIntakeConfig.load",
                side_effect=RuntimeError("adapter exploded"),
            ):
                result = serve_cycle(tmp_path, runner=runner)  # type: ignore[arg-type]

        assert result.verdict is not None
        assert queue.items()[0].state is ItemState.DONE, (
            "a broken front-end must not change the local outcome"
        )


class TestServeDrivesRemoteLabels:
    """#187 F8/F9/F10: labels follow real transitions, on every path."""

    @staticmethod
    def _remote_item(root: Path, **kwargs: object) -> object:
        queue = Queue(root, QueueConfig(**kwargs))  # type: ignore[arg-type]
        return queue.add(
            "# spec\n",
            title="remote",
            source=ItemSource.GITHUB,
            source_ref=f"{REPO}#4",
            target_repo=REPO,
        )

    @staticmethod
    def _capture() -> tuple[list[tuple[str, str]], object]:
        """Record (state, detail) for every writeback the daemon makes."""
        seen: list[tuple[str, str]] = []

        def fake(item, *, state, detail, config, root_dir):  # type: ignore[no-untyped-def]
            seen.append((state, detail))
            return ""

        return seen, fake

    def _run(
        self,
        tmp_path: Path,
        runner: object,
        **queue_kwargs: object,
    ) -> list[tuple[str, str]]:
        from kstrl.serve import RunSpend, serve_cycle

        seen, fake = self._capture()
        with patch("kstrl.serve.read_run_spend", lambda root, rid: RunSpend()):
            with patch(
                "kstrl.intake_github.GitHubIntakeConfig.load",
                return_value=_config(),
            ):
                with patch("kstrl.intake_github.report_outcome", fake):
                    serve_cycle(tmp_path, runner=runner)  # type: ignore[arg-type]
        return seen

    @staticmethod
    def _runner(returncode: int = 0, run_id: str = "factory-x"):  # type: ignore[no-untyped-def]
        from kstrl.serve import RunOutcome

        def runner(*, root_dir: Path, **kwargs: object) -> RunOutcome:
            run_dir = root_dir / ".kstrl" / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "events.jsonl").touch()
            return RunOutcome(returncode=returncode)

        return runner

    def test_running_is_labelled_when_the_item_actually_starts(
        self,
        tmp_path: Path,
    ) -> None:
        self._remote_item(tmp_path)
        seen = self._run(tmp_path, self._runner(0))
        assert [state for state, _ in seen][0] == "running", (
            "the remote must learn the item started, not that it was admitted"
        )

    def test_a_successful_run_reports_done(self, tmp_path: Path) -> None:
        self._remote_item(tmp_path)
        seen = self._run(tmp_path, self._runner(0))
        assert [state for state, _ in seen] == ["running", "done"]

    def test_a_retry_reports_failed_not_running(self, tmp_path: Path) -> None:
        """A queued retry is not running; the label must say so."""
        from kstrl.findings import Finding
        from kstrl.manifest import Component, ComponentStatus, Manifest

        self._remote_item(tmp_path, max_attempts=3)
        comp = Component("comp-a", "A", "", [], "a.json", "b/a")
        comp.status = ComponentStatus("failed")
        comp.findings = [Finding.infrastructure_error("review", "cli died")]
        path = tmp_path / "scripts" / "kstrl" / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        Manifest(
            version="1",
            spec_file="s.md",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[comp],
            run_id="factory-x",
        ).save(path)

        seen = self._run(tmp_path, self._runner(1), max_attempts=3)
        assert [state for state, _ in seen] == ["running", "failed"]

    def test_a_poisoned_run_reports_poison(self, tmp_path: Path) -> None:
        self._remote_item(tmp_path)
        seen = self._run(tmp_path, self._runner(1))
        assert [state for state, _ in seen] == ["running", "poison"]

    def test_a_merge_gate_refusal_still_reports(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#187 F9: this path poisons and returns; it used to report nothing."""
        from kstrl.serve import MergeGate

        self._remote_item(tmp_path)
        with patch(
            "kstrl.serve.resolve_merge_gate",
            return_value=MergeGate(
                pause_before_pr_merge=True,
                refusal="ladder conflict",
            ),
        ):
            seen = self._run(tmp_path, self._runner(0))
        assert [state for state, _ in seen] == ["poison"], (
            "a refused item must not be left labelled running forever"
        )

    def test_a_reaper_poison_still_reports(self, tmp_path: Path) -> None:
        """#187 F9: another terminal path with no ran_item."""
        queue = Queue(tmp_path, QueueConfig(max_attempts=1))
        item = queue.add(
            "# spec\n",
            title="remote",
            source=ItemSource.GITHUB,
            source_ref=f"{REPO}#4",
            target_repo=REPO,
        )
        queue.start(queue.lease(item, pid=999999))
        seen = self._run(tmp_path, self._runner(0), max_attempts=1)
        assert ("poison",) == tuple(s for s, _ in seen if s == "poison")

    def test_an_unreaped_timeout_still_reports(self, tmp_path: Path) -> None:
        """#187 F9: the orphaned-process path."""
        from kstrl.serve import RunOutcome

        self._remote_item(tmp_path)

        def runner(*, root_dir: Path, **kwargs: object) -> RunOutcome:
            return RunOutcome(returncode=-9, timed_out=True, group_reaped=False)

        seen = self._run(tmp_path, runner)
        assert "poison" in [state for state, _ in seen]

    def test_writeback_happens_OUTSIDE_the_queue_mutex(
        self,
        tmp_path: Path,
    ) -> None:
        """#187 F10: two gh calls must not block every queue transition."""
        from kstrl.serve import RunSpend, serve_cycle
        from kstrl.workqueue import QueueLockedError, queue_lock

        self._remote_item(tmp_path)
        held: list[bool] = []

        def probing(item, *, state, detail, config, root_dir):  # type: ignore[no-untyped-def]
            try:
                with queue_lock(root_dir):
                    held.append(False)  # acquired: the mutex was free
            except QueueLockedError:
                held.append(True)  # blocked: I/O runs under the lock
            return ""

        with patch("kstrl.serve.read_run_spend", lambda root, rid: RunSpend()):
            with patch(
                "kstrl.intake_github.GitHubIntakeConfig.load",
                return_value=_config(),
            ):
                with patch("kstrl.intake_github.report_outcome", probing):
                    serve_cycle(tmp_path, runner=self._runner(0))  # type: ignore[arg-type]

        assert held, "the writeback must have run"
        assert not any(held), "every remote writeback must run with the queue mutex released"


class TestServePollsIntake:
    """#189 F1: the daemon must FILL the queue, not only drain it.

    `ks queue sync` existed as a manual command and the serve cycle never
    called it, so an installed LaunchAgent could never admit a labelled
    issue. The adapter and the daemon were each correct; their
    composition did nothing. Covers enabled, disabled, failure, once, and
    repeated cycles, as the review asked.
    """

    @staticmethod
    def _runner(returncode: int = 0):  # type: ignore[no-untyped-def]
        from kstrl.serve import RunOutcome

        def runner(*, root_dir: Path, **kwargs: object) -> RunOutcome:
            run_dir = root_dir / ".kstrl" / "runs" / "factory-x"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "events.jsonl").touch()
            return RunOutcome(returncode=returncode)

        return runner

    @staticmethod
    def _enable(root: Path) -> None:
        (root / "kstrl.toml").write_text(f'[intake_github]\nenabled = true\nrepo = "{REPO}"\n')

    def _cycle(self, root: Path, **kwargs: object):  # type: ignore[no-untyped-def]
        from kstrl.serve import RunSpend, serve_cycle

        with patch("kstrl.serve.read_run_spend", lambda r, i: RunSpend()):
            return serve_cycle(root, runner=self._runner(), **kwargs)  # type: ignore[arg-type]

    def test_an_enabled_adapter_is_polled_every_cycle(
        self,
        tmp_path: Path,
    ) -> None:
        self._enable(tmp_path)
        _queue(tmp_path).ensure_dirs()
        with patch("kstrl.intake_github.sync") as sync:
            sync.return_value = SyncResult(repo=REPO)
            self._cycle(tmp_path)
        assert sync.call_count == 1

    def test_a_disabled_adapter_makes_no_gh_calls(
        self,
        tmp_path: Path,
    ) -> None:
        """Off must mean no outbound traffic at all, not a wasted call."""
        _queue(tmp_path).ensure_dirs()
        with patch("kstrl.intake_github.run_gh") as gh:
            result = self._cycle(tmp_path)
        assert gh.call_count == 0
        # And no spurious error every cycle. `sync` itself reports
        # "enabled is false" as an error, so without the early return the
        # common case (intake off) would log a failure on every poll.
        assert result.sync_errors == (), (
            "a disabled adapter must be silent, not report an error each cycle"
        )

    def test_a_synced_item_is_RUN_in_the_same_cycle(
        self,
        tmp_path: Path,
    ) -> None:
        """The end-to-end property the missing wiring broke."""
        self._enable(tmp_path)
        queue = _queue(tmp_path)
        stub = _GhStub(issues=_issue_payload(_issue(4)))
        with patch("kstrl.intake_github.run_gh", stub):
            result = self._cycle(tmp_path)
        assert result.synced == (f"{REPO}#4",)
        assert result.ran_item, "the freshly synced item must also run"
        assert queue.items()[0].state is ItemState.DONE

    def test_an_intake_failure_does_not_stop_the_cycle(
        self,
        tmp_path: Path,
    ) -> None:
        """A front-end outage must never block local work."""
        self._enable(tmp_path)
        queue = _queue(tmp_path)
        queue.add("# local\n", title="local work")
        with patch(
            "kstrl.intake_github.run_gh",
            _GhStub(issues=GhResult(ok=False, error="HTTP 503")),
        ):
            result = self._cycle(tmp_path)
        assert result.sync_errors, "the failure is recorded"
        assert result.ran_item, "and the local item still runs"
        assert queue.items()[0].state is ItemState.DONE

    def test_an_intake_exception_does_not_stop_the_cycle(
        self,
        tmp_path: Path,
    ) -> None:
        self._enable(tmp_path)
        queue = _queue(tmp_path)
        queue.add("# local\n", title="local work")
        with patch(
            "kstrl.intake_github.sync",
            side_effect=RuntimeError("boom"),
        ):
            result = self._cycle(tmp_path)
        assert any("boom" in e for e in result.sync_errors)
        assert queue.items()[0].state is ItemState.DONE

    def test_repeated_cycles_do_not_re_admit(self, tmp_path: Path) -> None:
        """Idempotency has to hold across cycles, not just across syncs."""
        from kstrl.serve import RunSpend, serve

        self._enable(tmp_path)
        queue = _queue(tmp_path)
        with patch("kstrl.intake_github.run_gh", _GhStub(issues=_issue_payload(_issue(4)))):
            with patch("kstrl.serve.read_run_spend", lambda r, i: RunSpend()):
                serve(
                    tmp_path,
                    runner=self._runner(),
                    max_cycles=3,
                    sleeper=lambda _s: None,
                )
        assert len(queue.items()) == 1, "one issue must yield one item"

    def test_once_mode_still_polls_intake(self, tmp_path: Path) -> None:
        """--once is the launchd shape; it must admit work too."""
        from kstrl.serve import RunSpend, serve

        self._enable(tmp_path)
        queue = _queue(tmp_path)
        with patch("kstrl.intake_github.run_gh", _GhStub(issues=_issue_payload(_issue(4)))):
            with patch("kstrl.serve.read_run_spend", lambda r, i: RunSpend()):
                results = serve(tmp_path, once=True, runner=self._runner())
        assert results[0].synced == (f"{REPO}#4",)
        assert len(queue.items()) == 1

    def test_intake_runs_BEFORE_the_admission_gates(
        self,
        tmp_path: Path,
    ) -> None:
        """Newly synced work must face the same budget and breaker checks.

        Syncing after the gates would let a fresh item bypass a budget
        pause that had already stopped everything else.
        """
        from kstrl.serve import ServeConfig, SpendLedger

        self._enable(tmp_path)
        queue = _queue(tmp_path)
        SpendLedger(tmp_path).charge(50.0, covered_calls=1, total_calls=1)
        with patch("kstrl.intake_github.run_gh", _GhStub(issues=_issue_payload(_issue(4)))):
            from kstrl.serve import RunSpend, serve_cycle

            with patch("kstrl.serve.read_run_spend", lambda r, i: RunSpend()):
                result = serve_cycle(
                    tmp_path,
                    config=ServeConfig(
                        daily_budget_usd=10.0,
                        allow_uncovered_cost=True,
                    ),
                    runner=self._runner(),
                )
        assert result.synced == (f"{REPO}#4",), "the item was admitted"
        assert not result.ran_item, "but the budget still stopped it running"
        assert queue.items()[0].state is ItemState.QUEUED


class TestIntakeLockDiscipline:
    """#189 N1: the queue mutex must not span remote I/O.

    Wrapping intake in the lock reintroduced exactly the problem #187 F10
    removed from writeback: a slow GitHub blocks `ks queue pause` and
    every other transition.
    """

    def test_the_mutex_is_free_during_the_network_phase(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.serve import RunOutcome, RunSpend, serve_cycle
        from kstrl.workqueue import QueueLockedError, queue_lock

        (tmp_path / "kstrl.toml").write_text(f'[intake_github]\nenabled = true\nrepo = "{REPO}"\n')
        _queue(tmp_path).ensure_dirs()
        held: list[bool] = []
        real_gh = _GhStub(issues=_issue_payload(_issue(4)))

        def probing_gh(args, *, timeout, cwd=None):  # type: ignore[no-untyped-def]
            # Every network call must find the mutex free.
            try:
                with queue_lock(tmp_path):
                    held.append(False)
            except QueueLockedError:
                held.append(True)
            return real_gh(args, timeout=timeout, cwd=cwd)

        with patch("kstrl.intake_github.run_gh", probing_gh):
            with patch("kstrl.serve.read_run_spend", lambda r, i: RunSpend()):
                serve_cycle(tmp_path, runner=lambda **k: RunOutcome(0))

        assert held, "the network phase must have run"
        assert not any(held), "no gh call may happen while the queue mutex is held"

    def test_serve_guards_the_COMMIT_even_though_the_poll_is_free(
        self,
        tmp_path: Path,
    ) -> None:
        """Unlocking the network must not unlock the enqueue.

        The free-during-network test alone cannot catch a dropped guard:
        with no guard at all the mutex is free everywhere, so that test
        passes even harder. This asserts the other half.
        """
        from kstrl.serve import RunOutcome, RunSpend, serve_cycle
        from kstrl.workqueue import Queue as _Queue
        from kstrl.workqueue import QueueLockedError, queue_lock

        (tmp_path / "kstrl.toml").write_text(f'[intake_github]\nenabled = true\nrepo = "{REPO}"\n')
        _queue(tmp_path).ensure_dirs()
        held_during_add: list[bool] = []
        real_add = _Queue.add

        def probing_add(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            try:
                with queue_lock(tmp_path):
                    held_during_add.append(False)
            except QueueLockedError:
                held_during_add.append(True)
            return real_add(self, *args, **kwargs)

        with patch("kstrl.intake_github.run_gh", _GhStub(issues=_issue_payload(_issue(4)))):
            with patch.object(_Queue, "add", probing_add):
                with patch(
                    "kstrl.serve.read_run_spend",
                    lambda r, i: RunSpend(),
                ):
                    serve_cycle(tmp_path, runner=lambda **k: RunOutcome(0))

        assert held_during_add, "intake must have enqueued something"
        assert all(held_during_add), "every intake enqueue must happen with the queue mutex held"

    def test_the_commit_still_happens_under_the_guard(
        self,
        tmp_path: Path,
    ) -> None:
        """Unlocking the poll must not unlock the enqueue."""
        from contextlib import contextmanager

        entered: list[str] = []

        @contextmanager
        def guard():  # type: ignore[no-untyped-def]
            entered.append("in")
            yield
            entered.append("out")

        queue = _queue(tmp_path)
        with patch("kstrl.intake_github.run_gh", _GhStub(issues=_issue_payload(_issue(4)))):
            result = sync(queue, _config(), tmp_path, commit_guard=guard)
        assert entered == ["in", "out"], "the commit must be guarded"
        assert result.enqueued == (f"{REPO}#4",)

    def test_the_plan_is_refreshed_under_the_guard(
        self,
        tmp_path: Path,
    ) -> None:
        """Another process may have queued the same ref while we polled.

        Re-planning inside the guard is what stops a TOCTOU double-admit.
        """
        from contextlib import contextmanager

        queue = _queue(tmp_path)

        @contextmanager
        def guard():  # type: ignore[no-untyped-def]
            # Simulate a racing writer that admits the same issue first.
            queue.add(
                "# raced\n",
                title="raced",
                source=ItemSource.GITHUB,
                source_ref=f"{REPO}#4",
                target_repo=REPO,
            )
            yield

        with patch("kstrl.intake_github.run_gh", _GhStub(issues=_issue_payload(_issue(4)))):
            result = sync(queue, _config(), tmp_path, commit_guard=guard)
        assert result.enqueued == (), "the refreshed plan must see the race"
        assert len(queue.items()) == 1, "no duplicate admission"
