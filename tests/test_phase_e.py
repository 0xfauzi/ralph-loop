"""Phase E: architectural refinements (subset E2/E4/E5/E6/E9).

E3 (structured findings) and E8 (fact scope by import surface) are
deferred to follow-up PRs - see docs/adversarial-roadmap.md for the
rationale and exact follow-up scope.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.knowledge import _coerce_facts, _parse_fact_md
from kstrl.manifest import Component, Manifest
from kstrl.review import REVIEWER_PROMPT, ReviewResult, parse_review_output
from kstrl.security import SECURITY_PROMPT
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig

# ---------------------------------------------------------------------------
# E5 - confidence rename + backwards compat
# ---------------------------------------------------------------------------


class TestE5ConfidenceRename:
    def test_new_review_passed_accepted(self) -> None:
        raw = [
            {
                "id": "fact-001",
                "scope": "handler",
                "confidence": "review_passed",
                "evidence": ["x:1"],
                "claim": "ok",
            }
        ]
        facts = _coerce_facts(raw, "c", 1, "r", 7)
        assert len(facts) == 1
        assert facts[0].confidence == "review_passed"

    def test_test_verified_tier_accepted(self) -> None:
        raw = [
            {
                "id": "fact-001",
                "scope": "handler",
                "confidence": "test_verified",
                "evidence": ["x:1"],
                "claim": "ok",
            }
        ]
        facts = _coerce_facts(raw, "c", 1, "r", 7)
        assert facts[0].confidence == "test_verified"

    def test_legacy_verified_value_maps_on_read(self, tmp_path: Path) -> None:
        """An old fact file with confidence=verified must still load,
        with the value rewritten to review_passed on read."""
        legacy = (
            "---\n"
            '{"id":"fact-001","component_id":"x","created_iter":1,'
            '"created_run_id":"factory-20260101-120000-aaaaaa",'
            '"scope":"handler","evidence":["x:1"],'
            '"confidence":"verified","tags":[]}\n'
            "---\n\n"
            "Legacy fact body.\n"
        )
        fact = _parse_fact_md(legacy)
        assert fact.confidence == "review_passed"


# ---------------------------------------------------------------------------
# E4 - LLM budget cap
# ---------------------------------------------------------------------------


class TestE4BudgetCap:
    def _scaffold(self, tmp_path: Path, comp_id: str) -> Path:
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "scripts" / "kstrl" / "prompt.md").write_text("p")
        (tmp_path / "scripts" / "kstrl" / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )
        feature_dir = tmp_path / "scripts" / "kstrl" / "feature" / comp_id
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "t",
                    "userStories": [
                        {
                            "id": "US-1",
                            "title": "t",
                            "acceptanceCriteria": ["AC"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        return tmp_path

    def _make_manifest(self, ids: list[str]) -> Manifest:
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

    def _base_config(self, root: Path) -> KstrlConfig:
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

    def test_budget_zero_means_unbounded(self, tmp_path: Path) -> None:
        """max_adversarial_calls=0 is the default and must not change
        the existing review/security behavior."""
        root = self._scaffold(tmp_path, "comp-a")
        manifest = self._make_manifest(["comp-a"])
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            review_mode="hard",
            max_adversarial_calls=0,
            verify_config=VerifyConfig(
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=False,
                check_bad_patterns=False,
                subprocess_timeout=5.0,
            ),
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        passing_review = ReviewResult(passed=True, mode="hard")
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch(
                "kstrl.factory.run_review",
                return_value=passing_review,
            ) as mock_review,
            patch(
                "kstrl.git.get_diff_content",
                return_value="",
            ),
        ):
            run_factory(
                manifest,
                config,
                self._base_config(root),
                PlainUI(no_color=True),
                root,
            )
        # Unbounded budget means review fires
        assert mock_review.called

    def test_budget_one_stops_second_component_review(
        self,
        tmp_path: Path,
    ) -> None:
        """The cap is what it says: one adversarial call, so the second
        component's reviewer never runs. Named "stops" rather than
        "skips" since R10.5 (#226): in hard mode, which this test uses,
        the second component now halts at the budget wall instead of
        being skipped past it. What the cap does to the CALL COUNT is
        the same either way, and that is all this test measures.
        """
        root = self._scaffold(tmp_path, "comp-a")
        feature_b = root / "scripts/kstrl/feature/comp-b"
        feature_b.mkdir(parents=True)
        (feature_b / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "t",
                    "userStories": [
                        {
                            "id": "US-B",
                            "title": "t",
                            "acceptanceCriteria": ["AC"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        manifest = self._make_manifest(["comp-a", "comp-b"])
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            review_mode="hard",
            max_adversarial_calls=1,
            verify_config=VerifyConfig(
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=False,
                check_bad_patterns=False,
                subprocess_timeout=5.0,
            ),
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        passing_review = ReviewResult(passed=True, mode="hard")
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch(
                "kstrl.factory.run_review",
                return_value=passing_review,
            ) as mock_review,
            patch(
                "kstrl.git.get_diff_content",
                return_value="",
            ),
        ):
            run_factory(
                manifest,
                config,
                self._base_config(root),
                PlainUI(no_color=True),
                root,
            )
        # Budget=1; the second component's review never gets a call
        assert mock_review.call_count == 1


# ---------------------------------------------------------------------------
# E6 - HITL checkpoint (non-interactive path)
# ---------------------------------------------------------------------------


class TestE6HitlCheckpoint:
    def test_non_interactive_ui_parks_for_approval(self, tmp_path: Path) -> None:
        """When pause_before_pr_merge=True but the UI can't prompt
        (PlainUI in tests), the factory must NOT merge unapproved. R8.3:
        it parks the component (fails it at phase=pr/check=merge_gate)
        and routes the decision to the inbox as a merge_gate item, so a
        human can approve it later with `ks inbox retry`. Proceeding
        would defeat the gate in exactly the unattended case R8.2's
        L1/L2 forces the gate on for."""
        from kstrl.factory import ComponentResult
        from kstrl.inbox import Inbox, InboxConfig, ItemKind

        scaffold = tmp_path / "scripts" / "kstrl"
        scaffold.mkdir(parents=True)
        (scaffold / "prompt.md").write_text("p")
        (scaffold / "prd.json").write_text('{"branchName": "t", "userStories": []}')
        feature_dir = scaffold / "feature" / "comp-a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "t",
                    "userStories": [
                        {
                            "id": "US-1",
                            "title": "t",
                            "acceptanceCriteria": ["AC"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        manifest = Manifest(
            version="1",
            spec_file="s",
            project_name="t",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    id="comp-a",
                    title="A",
                    description="",
                    dependencies=[],
                    prd_path="scripts/kstrl/feature/comp-a/prd.json",
                    branch_name="kstrl/a",
                )
            ],
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=True,
            max_parallel=1,
            review_mode="skip",
            pause_before_pr_merge=True,
            verify_config=VerifyConfig(
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=False,
                check_bad_patterns=False,
                subprocess_timeout=5.0,
            ),
        )
        base = KstrlConfig(
            prompt_file=scaffold / "prompt.md",
            prd_file=scaffold / "prd.json",
            sleep_seconds=0,
            agent_cmd="echo test",
            kstrl_branch="",
            kstrl_branch_explicit=True,
            ui_mode="plain",
            no_color=True,
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success,
            ),
            patch(
                "kstrl.pr.is_gh_available",
                return_value=False,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                config,
                base,
                PlainUI(no_color=True),
                tmp_path,
            )
        # PlainUI returns False for can_prompt(), so the gate cannot be
        # answered: the component is parked, not merged.
        assert "comp-a" not in result.completed
        assert "comp-a" in result.failed
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.failed_phase == "pr"
        assert comp.failed_check == "merge_gate"
        # ...and the decision is queued for a human, exactly once.
        items = Inbox(tmp_path, InboxConfig()).open_items()
        assert [i.kind for i in items] == [ItemKind.MERGE_GATE]
        assert items[0].component == "comp-a"


# ---------------------------------------------------------------------------
# E2 - the engineer's Self-Critique must not anchor the reviewer
# ---------------------------------------------------------------------------


class TestE2SelfCritiqueIsNotEvidence:
    """#266 moved E2 from a mechanism to an instruction, and that is a
    real weakening worth pinning.

    Before, the harness held the diff and deleted the engineer's
    ``## Self-Critique`` block out of it with a regex before either
    reviewer saw it, so the anchoring was impossible rather than
    discouraged. The reviewers now read the repository themselves and
    the harness no longer stands between them and the bytes, so the
    block IS visible to them and the only remaining defence is telling
    them what it is worth. These tests assert the instruction is
    present in both prompts; nothing can assert that it is obeyed, and
    the calibration fixtures carry no self-critique block, so no
    measurement covers it either.
    """

    @pytest.mark.parametrize(
        "prompt",
        [REVIEWER_PROMPT, SECURITY_PROMPT],
        ids=["reviewer", "security"],
    )
    def test_prompt_says_the_self_critique_is_not_evidence(self, prompt: str) -> None:
        assert "SELF-CRITIQUE IS NOT EVIDENCE" in prompt
        assert "## Self-Critique" in prompt
        assert "author's account of its own work" in prompt

    @pytest.mark.parametrize(
        "prompt,sentence",
        [
            (REVIEWER_PROMPT, "confirm it in the code or report it"),
            (
                SECURITY_PROMPT,
                "confirm the mitigation in the code or report the vulnerability",
            ),
        ],
        ids=["reviewer", "security"],
    )
    def test_prompt_demands_independent_confirmation(
        self,
        prompt: str,
        sentence: str,
    ) -> None:
        """The instruction has to say what to DO, not just what to
        distrust: a named failure mode must be confirmed in the code or
        reported, which is the behaviour the deleted regex bought.

        Paired per prompt rather than disjoined over both: an ``or``
        lets each prompt pass on the OTHER's sentence, so deleting the
        reviewer's line would go unnoticed."""
        assert sentence in prompt


# ---------------------------------------------------------------------------
# E9 - infrastructure_error on ReviewResult
# ---------------------------------------------------------------------------


class TestE9ReviewInfrastructureError:
    def test_parse_failure_sets_infrastructure_error(self) -> None:
        result = parse_review_output("not json at all")
        assert result.passed is False
        assert result.infrastructure_error is True

    def test_clean_review_has_no_infrastructure_error(self) -> None:
        result = parse_review_output(
            json.dumps(
                {
                    "stories": [],
                    "concerns": [],
                    "exhaustively_searched": True,
                }
            )
        )
        assert result.passed is True
        assert result.infrastructure_error is False
