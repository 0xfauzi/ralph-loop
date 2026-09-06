"""Tests for decompose module."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.decompose import (
    _MAX_RETRY_MESSAGES,
    DECOMPOSE_PROMPT,
    AuditSnapshot,
    ExcludedHistory,
    SpecBlockerError,
    SpecConvergence,
    SpecIssue,
    _build_convergence,
    _counted_audits,
    _excluded_history,
    _excluded_lines,
    _excluded_projects,
    _extract_json,
    _issue_dicts,
    _journal_snapshot,
    _parse_spec_issues,
    _retry_feedback,
    _stored_issues,
    _validate_decompose_output,
    _write_decompose_artifact,
    decompose_spec,
)
from kstrl.evolution import SPEC_ISSUES_EVENT, EvolutionConfig, EvolutionJournal
from kstrl.prd import PRD
from kstrl.ui.plain import PlainUI
from tests.helpers.journal import audit, journal_at, tear


class MockDecomposeAgent:
    """Mock agent that returns predetermined JSON output."""

    def __init__(self, output: str):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "mock-decompose"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        yield from self._output.splitlines()
        if self._output.strip():
            self._final_message = self._output.splitlines()[-1]

    @property
    def final_message(self) -> str | None:
        return self._final_message


VALID_DECOMPOSE_OUTPUT = json.dumps(
    {
        "components": [
            {
                "id": "database",
                "title": "Database Schema",
                "description": "Create the database tables",
                "dependencies": [],
                "allowedPaths": [
                    "src/",
                    "tests/",
                    "scripts/kstrl/feature/database/",
                ],
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Create users table",
                        "acceptanceCriteria": ["Users table exists", "Tests pass"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            },
            {
                "id": "api",
                "title": "API Endpoints",
                "description": "Create REST API endpoints",
                "dependencies": ["database"],
                "allowedPaths": [
                    "src/",
                    "tests/",
                    "scripts/kstrl/feature/api/",
                ],
                "userStories": [
                    {
                        "id": "US-002",
                        "title": "GET /users endpoint",
                        "acceptanceCriteria": ["Returns user list", "Tests pass"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            },
        ],
        # v3.0.0 requires the array, even empty: a payload that omitted
        # it used to pass with zero decisions and zero escalations, and
        # zero agrees with any count.
        "spec_issues": [],
        "decisions": [],
    }
)


class TestExtractJson:
    """Tests for _extract_json."""

    def test_plain_json(self) -> None:
        result = _extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_with_whitespace(self) -> None:
        result = _extract_json('  \n  {"key": "value"}  \n  ')
        assert result == {"key": "value"}

    def test_json_in_code_fence(self) -> None:
        text = '```json\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_json_in_plain_code_fence(self) -> None:
        text = '```\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_json_with_surrounding_text(self) -> None:
        text = 'Here is the output:\n{"key": "value"}\nDone.'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_no_json_raises(self) -> None:
        with pytest.raises(ValueError, match="No valid JSON"):
            _extract_json("no json here")

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="No valid JSON"):
            _extract_json("{invalid json}")

    def test_nested_json(self) -> None:
        data = {"components": [{"id": "test", "nested": {"a": 1}}]}
        result = _extract_json(json.dumps(data))
        assert result == data


class TestValidateDecomposeOutput:
    """Tests for _validate_decompose_output."""

    def test_valid_output(self) -> None:
        data = json.loads(VALID_DECOMPOSE_OUTPUT)
        assert _validate_decompose_output(data) == []

    def test_not_a_dict(self) -> None:
        errors = _validate_decompose_output("not a dict")
        assert any("object" in e for e in errors)

    def test_missing_components(self) -> None:
        errors = _validate_decompose_output({})
        assert any("components" in e for e in errors)

    def test_components_not_array(self) -> None:
        errors = _validate_decompose_output({"components": "not array"})
        assert any("array" in e for e in errors)

    def test_empty_components(self) -> None:
        errors = _validate_decompose_output({"components": []})
        assert any("empty" in e for e in errors)

    def test_duplicate_component_id(self) -> None:
        data = {
            "components": [
                {
                    "id": "same",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "userStories": [],
                },
                {
                    "id": "same",
                    "title": "B",
                    "description": "B",
                    "dependencies": [],
                    "userStories": [],
                },
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("duplicate" in e.lower() for e in errors)

    def test_unknown_dependency(self) -> None:
        data = {
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "description": "A",
                    "dependencies": ["nonexistent"],
                    "userStories": [],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("nonexistent" in e for e in errors)

    def test_allowed_paths_required(self) -> None:
        """DECOMPOSE_PROMPT v1.2.0+ requires allowedPaths on every
        component. The architect output gate rejects emissions that
        omit it; the diff-scope check would otherwise be silently
        disabled at Phase 1."""
        data = {
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "userStories": [],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("allowedPaths" in e and "required" in e for e in errors)

    def test_allowed_paths_must_be_array(self) -> None:
        data = {
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "allowedPaths": "src/",
                    "userStories": [],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("allowedPaths" in e and "array" in e for e in errors)

    def test_allowed_paths_empty_rejected(self) -> None:
        """An empty allowedPaths silently disables the diff-scope check
        which is worse than not setting it at all; reject explicitly."""
        data = {
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "allowedPaths": [],
                    "userStories": [],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("allowedPaths" in e and "non-empty" in e for e in errors)

    def test_allowed_paths_non_string_item_rejected(self) -> None:
        data = {
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "allowedPaths": ["src/", 42],
                    "userStories": [],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("allowedPaths" in e for e in errors)

    def test_allowed_paths_valid(self) -> None:
        # userStories must be non-empty since R1.8's vacuous-PRD gate,
        # so this fixture carries one real story.
        data: dict[str, object] = {
            "spec_issues": [],
            "decisions": [],
            "components": [
                {
                    "id": "comp-a",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "allowedPaths": [
                        "src/",
                        "tests/",
                        "scripts/kstrl/feature/comp-a/",
                    ],
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "S1",
                            "acceptanceCriteria": ["AC1", "AC2"],
                            "priority": 1,
                            "passes": False,
                            "notes": "",
                        }
                    ],
                }
            ],
        }
        errors = _validate_decompose_output(data)
        assert errors == []


class TestSpecIssues:
    """Tests for the red-team / spec-audit surface."""

    def test_parse_typed_issues(self) -> None:
        data = {
            "spec_issues": [
                {
                    "severity": "blocker",
                    "kind": "ambiguity",
                    "summary": "What 'fast' means is not defined",
                    "location": "Performance section",
                    "suggestion": "Specify a P95 latency budget",
                },
                {
                    "severity": "major",
                    "kind": "undefined_failure_mode",
                    "summary": "No error path for db unavailable",
                },
            ],
        }
        issues = _parse_spec_issues(data)
        assert len(issues) == 2
        assert issues[0].severity == "blocker"
        assert issues[1].kind == "undefined_failure_mode"

    def test_invalid_severity_dropped(self) -> None:
        data = {
            "spec_issues": [
                {
                    "severity": "critical",  # not valid
                    "kind": "ambiguity",
                    "summary": "x",
                }
            ]
        }
        assert _parse_spec_issues(data) == []

    def test_invalid_kind_dropped(self) -> None:
        data = {
            "spec_issues": [
                {
                    "severity": "major",
                    "kind": "made_up_kind",
                    "summary": "x",
                }
            ]
        }
        assert _parse_spec_issues(data) == []

    def test_missing_summary_dropped(self) -> None:
        data = {
            "spec_issues": [
                {
                    "severity": "minor",
                    "kind": "ambiguity",
                    "summary": "",
                }
            ]
        }
        assert _parse_spec_issues(data) == []

    def test_empty_components_allowed_when_escalated(self) -> None:
        data = {
            "components": [],
            "spec_issues": [
                {
                    "id": "too-vague",
                    "severity": "blocker",
                    "kind": "ambiguity",
                    "summary": "spec is too vague",
                }
            ],
            "decisions": [
                {
                    "issue": "too-vague",
                    "question": "which product ships first",
                    "disposition": "escalated",
                    "resolution": "the owner must name the smallest slice",
                }
            ],
        }
        assert _validate_decompose_output(data) == []

    def test_empty_components_rejected_without_escalation(self) -> None:
        data: dict[str, object] = {"components": [], "spec_issues": [], "decisions": []}
        errors = _validate_decompose_output(data)
        assert errors
        assert "components" in errors[0]

    def test_a_blocker_without_an_escalation_is_rejected(self) -> None:
        """#260: the halt keys on the escalation, so a blocker with no
        escalated decision would proceed on a question the architect
        called un-guessable and never closed. Retryable, not silent."""
        data = json.loads(
            _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE], decisions=[])
        )
        errors = _validate_decompose_output(data)
        assert any("was raised and never closed" in e for e in errors)

    def test_an_escalation_without_a_blocker_is_rejected(self) -> None:
        data = json.loads(
            _single_component_output(
                [_story()],
                spec_issues=[MINOR_ISSUE],
                decisions=[
                    {
                        "issue": "edge-case-unspecified",
                        "question": "which product ships first",
                        "disposition": "escalated",
                        "resolution": "the owner must name the smallest slice",
                    }
                ],
            )
        )
        errors = _validate_decompose_output(data)
        assert any("'escalated' decision needs a" in e for e in errors)

    def test_a_decision_naming_an_unknown_component_is_rejected(self) -> None:
        """#260: the renderer matches the id exactly, so a typo would
        silently demote a binding decision to the summary tier for every
        engineer. Same join the validator already does for deps."""
        data = json.loads(
            _single_component_output(
                [_story()],
                spec_issues=[MINOR_ISSUE],
                decisions=[
                    {
                        "issue": "edge-case-unspecified",
                        "question": "what does the serializer emit",
                        "disposition": "decided",
                        "resolution": "an empty list",
                        "component": "comp-typo",
                    }
                ],
            )
        )
        errors = _validate_decompose_output(data)
        assert any("unknown component 'comp-typo'" in e for e in errors)

    def test_a_decision_binding_the_whole_run_names_no_component(self) -> None:
        data = json.loads(
            _single_component_output(
                [_story()],
                spec_issues=[MINOR_ISSUE],
                decisions=[
                    {
                        "issue": "edge-case-unspecified",
                        "question": "what does the serializer emit",
                        "disposition": "decided",
                        "resolution": "an empty list",
                        "component": "",
                    }
                ],
            )
        )
        assert _validate_decompose_output(data) == []

    def test_a_disposed_issue_needs_no_escalation(self) -> None:
        """The whole point of #260: an issue the architect closed itself
        rides along with the components instead of stopping the run."""
        data = json.loads(
            _single_component_output(
                [_story()],
                spec_issues=[MINOR_ISSUE],
                decisions=[
                    {
                        "issue": "edge-case-unspecified",
                        "question": "what does the empty-input path do",
                        "disposition": "assumed",
                        "resolution": "return an empty list; pinned by AC2",
                        "component": "comp-a",
                    }
                ],
            )
        )
        assert _validate_decompose_output(data) == []

    def test_decompose_raises_on_escalation(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec\nDo something good.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps(
            {
                "spec_issues": [
                    {
                        "id": "spec-empty",
                        "severity": "blocker",
                        "kind": "ambiguity",
                        "summary": "Spec is empty",
                        "location": "everywhere",
                        "suggestion": "Write actual requirements",
                    }
                ],
                "decisions": [
                    {
                        "issue": "spec-empty",
                        "question": "what is this product for",
                        "disposition": "escalated",
                        "resolution": "the owner must say",
                    }
                ],
                "components": [],
            }
        )
        agent = MockDecomposeAgent(output)
        ui = PlainUI(no_color=True)
        with pytest.raises(SpecBlockerError) as exc_info:
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=ui,
                root_dir=tmp_path,
            )
        assert len(exc_info.value.escalations) == 1
        assert exc_info.value.escalations[0].question == "what is this product for"
        # R1.7: the halt points at BOTH durable records. The audit holds
        # the finding; only the register holds the question the owner
        # has to answer and the reason it was not answered.
        lines = exc_info.value.artifact_lines()
        assert any("spec-issues.json" in line for line in lines)
        assert any("decisions.json" in line for line in lines)

    def test_decompose_continues_on_non_blockers(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps(
            {
                "spec_issues": [
                    {
                        "id": "edge-case",
                        "severity": "minor",
                        "kind": "missing_detail",
                        "summary": "Edge case unspecified",
                    }
                ],
                "decisions": [
                    {
                        "issue": "edge-case",
                        "question": "what does the empty-input path do",
                        "disposition": "assumed",
                        "resolution": "return an empty list; pinned by AC2",
                    }
                ],
                "components": [
                    {
                        "id": "comp-a",
                        "title": "A",
                        "description": "x",
                        "dependencies": [],
                        "allowedPaths": [
                            "src/",
                            "tests/",
                            "scripts/kstrl/feature/comp-a/",
                        ],
                        "userStories": [
                            {
                                "id": "US-001",
                                "title": "S1",
                                "acceptanceCriteria": ["AC1", "AC2"],
                                "priority": 1,
                                "passes": False,
                                "notes": "",
                            }
                        ],
                    },
                ],
            }
        )
        agent = MockDecomposeAgent(output)
        ui = PlainUI(no_color=True)
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
        )
        assert len(manifest.components) == 1
        assert manifest.components[0].id == "comp-a"


class TestDecomposeSpec:
    """Tests for decompose_spec end-to-end."""

    def test_successful_decomposition(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# My Feature\nBuild a user management system.")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        ui = PlainUI(no_color=True)

        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test-project",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
        )

        assert len(manifest.components) == 2
        assert manifest.components[0].id == "database"
        assert manifest.components[1].id == "api"
        assert manifest.components[1].dependencies == ["database"]
        assert manifest.project_name == "test-project"

        # Verify PRD files were created
        db_prd = tmp_path / "scripts" / "kstrl" / "feature" / "database" / "prd.json"
        assert db_prd.exists()
        prd = PRD.load(db_prd)
        assert len(prd.user_stories) == 1
        assert prd.user_stories[0].id == "US-001"

        # Verify manifest was saved
        manifest_path = tmp_path / "scripts" / "kstrl" / "manifest.json"
        assert manifest_path.exists()

    def test_single_pr_mode_uses_shared_branch(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        ui = PlainUI(no_color=True)

        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="my-project",
            base_branch="main",
            single_pr=True,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
        )

        # All components should share the same branch
        branches = {c.branch_name for c in manifest.components}
        assert len(branches) == 1
        assert "my-project" in branches.pop()

    def test_multi_pr_mode_uses_separate_branches(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        ui = PlainUI(no_color=True)

        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
        )

        branches = {c.branch_name for c in manifest.components}
        assert len(branches) == 2
        assert any("database" in b for b in branches)
        assert any("api" in b for b in branches)

    def test_retries_on_invalid_json(self, tmp_path: Path) -> None:
        """Agent returns invalid output first, then valid."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        call_count = 0

        class RetryAgent:
            @property
            def name(self) -> str:
                return "retry-mock"

            def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    yield "not valid json"
                else:
                    yield VALID_DECOMPOSE_OUTPUT

            @property
            def final_message(self) -> str | None:
                return None

        ui = PlainUI(no_color=True)
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=RetryAgent(),
            ui=ui,
            root_dir=tmp_path,
        )

        assert call_count == 2
        assert len(manifest.components) == 2

    def test_fails_after_max_retries(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent("always invalid")
        ui = PlainUI(no_color=True)

        with pytest.raises(ValueError, match="Failed to decompose"):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=ui,
                root_dir=tmp_path,
                max_retries=2,
            )


class SequenceAgent:
    """Agent returning one canned output per invocation, recording prompts."""

    def __init__(self, outputs: list[str]):
        self._outputs = outputs
        self._final_message: str | None = None
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "sequence-agent"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        self.prompts.append(prompt)
        output = self._outputs[min(len(self.prompts) - 1, len(self._outputs) - 1)]
        self._final_message = output
        yield from output.splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


def _story(**overrides: object) -> dict[str, object]:
    story: dict[str, object] = {
        "id": "US-001",
        "title": "S1",
        "acceptanceCriteria": ["AC1", "AC2"],
        "priority": 1,
        "passes": False,
        "notes": "",
    }
    story.update(overrides)
    return story


def _with_ids(spec_issues: list[dict[str, object]]) -> list[dict[str, object]]:
    """The v3.0.0 schema requires a unique id on every issue."""
    return [
        entry if entry.get("id") else {**entry, "id": f"issue-{index}"}
        for index, entry in enumerate(spec_issues)
    ]


def _closures_for(spec_issues: list[dict[str, object]]) -> list[dict[str, object]]:
    """One decision per issue, as the v3.0.0 prompt requires.

    #260 made "blocker" severity and an escalated decision two views of
    one fact, and round 2 made the correspondence a per-record JOIN on
    the issue id rather than a count. Derived here rather than written
    out at every call site so a test that adds an issue cannot forget
    the half that makes the halt real.
    """
    return [
        {
            "issue": issue["id"],
            "question": f"who decides: {issue['summary']}",
            "disposition": ("escalated" if issue.get("severity") == "blocker" else "decided"),
            "resolution": "the owner must choose",
        }
        for issue in spec_issues
    ]


def _single_component_output(
    stories: list[dict[str, object]],
    spec_issues: list[dict[str, object]] | None = None,
    decisions: list[dict[str, object]] | None = None,
) -> str:
    payload: dict[str, object] = {
        "components": [
            {
                "id": "comp-a",
                "title": "A",
                "description": "x",
                "dependencies": [],
                "allowedPaths": [
                    "src/",
                    "tests/",
                    "scripts/kstrl/feature/comp-a/",
                ],
                "userStories": stories,
            }
        ],
    }
    issues = _with_ids(spec_issues or [])
    payload["spec_issues"] = issues
    if decisions is not None:
        payload["decisions"] = decisions
    else:
        payload["decisions"] = _closures_for(issues)
    return json.dumps(payload)


class TestTheJoinKeyIsValidatedRaw:
    """#260 round 2 (F1). The join is only as good as the ids it joins
    on, and a mutation that auto-numbered a missing id survived the
    first pass of this suite: the gate still closed, but one level away,
    with an error naming `decisions[i].issue` for a fault that was in
    `spec_issues[i].id`. A retry can only fix the record the message
    names.
    """

    def _payload(self, spec_issues: list[dict[str, object]]) -> dict[str, object]:
        data = json.loads(_single_component_output([_story()], spec_issues=spec_issues))
        return dict(data)

    def test_an_issue_without_an_id_is_named_and_indexed(self) -> None:
        data = self._payload([])
        data["spec_issues"] = [{"severity": "minor", "kind": "missing_detail", "summary": "s"}]
        data["decisions"] = []
        errors = _validate_decompose_output(data)
        assert any(e.startswith("spec_issues[0].id:") for e in errors)

    def test_a_blank_id_is_rejected(self) -> None:
        data = self._payload([])
        data["spec_issues"] = [
            {"id": "   ", "severity": "minor", "kind": "missing_detail", "summary": "s"}
        ]
        data["decisions"] = []
        errors = _validate_decompose_output(data)
        assert any(e.startswith("spec_issues[0].id:") for e in errors)

    def test_a_non_string_id_is_rejected(self) -> None:
        data = self._payload([])
        data["spec_issues"] = [
            {"id": 7, "severity": "minor", "kind": "missing_detail", "summary": "s"}
        ]
        data["decisions"] = []
        errors = _validate_decompose_output(data)
        assert any(e.startswith("spec_issues[0].id:") for e in errors)

    def test_a_duplicate_id_is_rejected(self) -> None:
        entry = {"id": "same", "severity": "minor", "kind": "missing_detail", "summary": "s"}
        data = self._payload([])
        data["spec_issues"] = [entry, dict(entry, summary="other")]
        data["decisions"] = [
            {"issue": "same", "question": "q", "disposition": "decided", "resolution": "r"}
        ]
        errors = _validate_decompose_output(data)
        assert any("already used by an earlier issue" in e for e in errors)

    def test_an_id_that_names_no_issue_is_rejected(self) -> None:
        """One of the three rejections DECOMPOSE_PROMPT 3.0.0 promises,
        and the round-2 /simplify pass measured that nothing tested it:
        a mutation letting an unknown id through left the whole suite
        green, 4713 passed."""
        data = self._payload([])
        data["spec_issues"] = [
            {"id": "a", "severity": "minor", "kind": "missing_detail", "summary": "s"}
        ]
        data["decisions"] = [
            {"issue": "a", "question": "q", "disposition": "decided", "resolution": "r"},
            {"issue": "ghost", "question": "q", "disposition": "decided", "resolution": "r"},
        ]
        errors = _validate_decompose_output(data)
        assert any("'ghost' is not the id of any entry in 'spec_issues'" in e for e in errors)

    def test_a_second_decision_closing_the_same_issue_is_rejected(self) -> None:
        """The other untested promise. Without it a payload can close one
        issue twice and leave another unclosed while the counts agree,
        which is the shape of the round-1 defect."""
        data = self._payload([])
        data["spec_issues"] = [
            {"id": "a", "severity": "minor", "kind": "missing_detail", "summary": "s"}
        ]
        data["decisions"] = [
            {"issue": "a", "question": "q1", "disposition": "decided", "resolution": "r"},
            {"issue": "a", "question": "q2", "disposition": "assumed", "resolution": "r"},
        ]
        errors = _validate_decompose_output(data)
        assert any("'a' is already closed by decisions[0]" in e for e in errors)

    def test_the_prompt_states_every_rule_the_validator_enforces(self) -> None:
        """The two statements of the contract must fail together.

        The prompt tells the model; the validator refuses to trust it.
        That split is deliberate, but the coupling was one-directional:
        editing the prompt breaks the H3 hash, and editing the validator
        broke nothing, so the English could quietly become false. It
        already had: 3.0.0 says field values are matched exactly and
        ``severity`` was not checked at all.
        """
        # Fragments, not sentences: the body is hard-wrapped, so a
        # sentence-length needle would fail on the line break rather
        # than on the meaning.
        for fragment in (
            "an issue is unclosed",
            "decisions close the same issue",
            "names an id that",
            "is not in `spec_issues`",
            "Field values are matched EXACTLY",
            'A "blocker" issue MUST be closed by',
        ):
            assert fragment in DECOMPOSE_PROMPT, fragment

    def test_a_non_object_issue_entry_is_rejected(self) -> None:
        data = self._payload([])
        data["spec_issues"] = ["not an object"]
        data["decisions"] = []
        errors = _validate_decompose_output(data)
        assert any(e.startswith("spec_issues[0]: must be an object") for e in errors)

    def test_a_non_list_spec_issues_is_rejected(self) -> None:
        data = self._payload([])
        data["spec_issues"] = {}
        data["decisions"] = []
        errors = _validate_decompose_output(data)
        assert any("'spec_issues' must be an array" in e for e in errors)


class TestAnIssueTheParserWouldDropIsARejection:
    """#260 round 3. The round-2 /simplify pass found F1's capital
    letter one field over.

    ``_spec_issue_errors`` took ``severity`` verbatim and compared it
    only to the literal ``"blocker"``, while ``_parse_spec_issues``
    checks it against ``_VALID_SEVERITIES``. So the two disagreed about
    which entries existed. Measured on the round-2 code: a severity of
    ``"Blocker"`` VALIDATED, was closed by a ``decided`` decision, and
    then parsed to 0 of 1 issues, so the blocker reached neither the
    halt gate, nor ``spec-issues.json``, nor ``route_spec_issues``, nor
    the UI. Five such shapes were accepted.

    The property: anything the validator accepts, the parser reproduces
    faithfully. There are two ways to break that and the suite covers
    both. The parser DROPS an entry whose severity or kind is not in the
    vocabulary, or whose summary is blank. It MANGLES a non-string
    summary, because it reads it as ``str(entry.get("summary", ""))``
    and a ``[]`` becomes the two-character summary ``"[]"``, which is
    not blank and so survives. A fabricated summary is worse than a
    dropped one, and the validator now refuses both.
    """

    def _payload(self, issue: dict[str, object], disposition: str) -> dict[str, object]:
        data = dict(json.loads(_single_component_output([_story()])))
        data["spec_issues"] = [issue]
        data["decisions"] = [
            {"issue": "a", "question": "q", "disposition": disposition, "resolution": "r"}
        ]
        return data

    @pytest.mark.parametrize(
        ("name", "issue", "disposition", "field", "parser"),
        [
            (
                "capitalised severity",
                {"id": "a", "severity": "Blocker", "kind": "ambiguity", "summary": "s"},
                "decided",
                "spec_issues[0].severity:",
                "drops",
            ),
            (
                "unknown severity word",
                {"id": "a", "severity": "critical", "kind": "ambiguity", "summary": "s"},
                "decided",
                "spec_issues[0].severity:",
                "drops",
            ),
            (
                "severity absent",
                {"id": "a", "kind": "ambiguity", "summary": "s"},
                "decided",
                "spec_issues[0].severity:",
                "drops",
            ),
            (
                "non-string severity",
                {"id": "a", "severity": 3, "kind": "ambiguity", "summary": "s"},
                "decided",
                "spec_issues[0].severity:",
                "drops",
            ),
            (
                "capitalised kind",
                {"id": "a", "severity": "blocker", "kind": "Ambiguity", "summary": "s"},
                "escalated",
                "spec_issues[0].kind:",
                "drops",
            ),
            (
                "unknown kind",
                {"id": "a", "severity": "blocker", "kind": "typo", "summary": "s"},
                "escalated",
                "spec_issues[0].kind:",
                "drops",
            ),
            (
                "blank summary",
                {"id": "a", "severity": "blocker", "kind": "ambiguity", "summary": "   "},
                "escalated",
                "spec_issues[0].summary:",
                "drops",
            ),
            (
                "non-string summary",
                {"id": "a", "severity": "blocker", "kind": "ambiguity", "summary": []},
                "escalated",
                "spec_issues[0].summary:",
                "mangles",
            ),
        ],
    )
    def test_the_parser_and_the_validator_cannot_disagree(
        self,
        name: str,
        issue: dict[str, object],
        disposition: str,
        field: str,
        parser: str,
    ) -> None:
        data = self._payload(issue, disposition)
        errors = _validate_decompose_output(data)
        assert errors, f"{name}: accepted a payload the parser would not reproduce"
        assert any(e.startswith(field) for e in errors), f"{name}: {errors}"
        # The premise, stated per case so it cannot rot silently.
        parsed = _parse_spec_issues(data)
        if parser == "drops":
            assert parsed == [], f"{name}: expected the parser to drop this"
        else:
            assert parsed and parsed[0].summary != issue["summary"], (
                f"{name}: expected the parser to mangle this"
            )

    def test_the_control_still_validates_and_still_parses(self) -> None:
        data = self._payload(
            {"id": "a", "severity": "blocker", "kind": "ambiguity", "summary": "s"},
            "escalated",
        )
        assert _validate_decompose_output(data) == []
        assert len(_parse_spec_issues(data)) == 1

    def test_the_message_names_the_whole_vocabulary(self) -> None:
        """A retry can only fix what the message spells out."""
        data = self._payload(
            {"id": "a", "severity": "Blocker", "kind": "ambiguity", "summary": "s"},
            "decided",
        )
        message = next(
            e for e in _validate_decompose_output(data) if e.startswith("spec_issues[0].severity:")
        )
        assert "'blocker', 'major', 'minor'" in message
        assert "case-exact" in message


class TestARegisterThatDidNotLandFailsTheDecompose:
    """#260 round 3. A swallowed write error silently disabled the whole
    register.

    The round-2 /simplify pass measured it: ``_write_decompose_artifact``
    caught ``OSError``, printed one line and returned ``None``, and the
    success path ignored the return, so a decompose whose register write
    failed still reported success. Every later ``ks factory`` run against
    that manifest then read status ``missing``, bound ``()`` and said
    nothing, because a missing register is the legal pre-#260 state.
    One full disk, one read-only mount, and the feature is off for good
    with no message anywhere.

    The rule: the register beside a saved manifest IS part of the result,
    so its write failure is the decompose's failure. The halting copy is
    not, because the halt reaches the operator through
    ``SpecBlockerError`` whether or not the file landed.
    """

    def _capture(self) -> tuple[PlainUI, io.StringIO]:
        buffer = io.StringIO()
        return PlainUI(no_color=True, file=buffer), buffer

    def _boom(self) -> Path:
        raise OSError("no space left on device")

    def test_an_optional_artifact_is_announced_and_survived(self) -> None:
        ui, buffer = self._capture()
        result = _write_decompose_artifact(
            "spec_issues",
            "spec issues",
            self._boom,
            ui=ui,
            emit=lambda event: None,
            rel_display=str,
        )
        assert result is None
        assert "no space left on device" in buffer.getvalue()

    def test_a_required_artifact_raises(self) -> None:
        ui, _ = self._capture()
        with pytest.raises(OSError, match="no space left on device"):
            _write_decompose_artifact(
                "decisions",
                "architect decisions",
                self._boom,
                ui=ui,
                emit=lambda event: None,
                rel_display=str,
                required=True,
            )

    def test_the_success_path_actually_asks_for_required(self, tmp_path: Path) -> None:
        """Mutation guard, and the reason this class exists.

        Deleting ``required=True`` from the one call site left all 288
        tests passing: the unit tests above prove the helper honours the
        flag, and nothing proved the caller passes it. So this drives a
        real decompose with a register write that cannot land and
        asserts the run fails rather than reporting success.
        """
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# My Feature\nBuild a user management system.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        with (
            patch(
                "kstrl.decompose.write_decisions",
                side_effect=OSError("no space left on device"),
            ),
            pytest.raises(OSError, match="no space left on device"),
        ):
            decompose_spec(
                spec_path=spec_file,
                project_name="test-project",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT),
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
            )

    def test_the_halt_path_still_halts_when_its_register_cannot_land(self, tmp_path: Path) -> None:
        """The other half of the policy, and the reason it is a flag
        rather than a rule. A halt reaches the operator through
        ``SpecBlockerError`` whether or not the file landed, so the
        halting copy must not turn an escalation into an OSError."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature\nTODO")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        output = json.dumps(
            {
                "spec_issues": [
                    {
                        "id": "spec-empty",
                        "severity": "blocker",
                        "kind": "missing_detail",
                        "summary": "The spec has no requirements",
                        "location": "everywhere",
                        "suggestion": "Write actual requirements",
                    }
                ],
                "decisions": [
                    {
                        "issue": "spec-empty",
                        "question": "what is this product for",
                        "disposition": "escalated",
                        "resolution": "the owner must say",
                    }
                ],
                "components": [],
            }
        )
        with (
            patch(
                "kstrl.decompose.write_decisions",
                side_effect=OSError("no space left on device"),
            ),
            pytest.raises(SpecBlockerError) as exc_info,
        ):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(output),
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
            )
        assert len(exc_info.value.escalations) == 1
        # The halt still names what it can: the audit landed, the
        # register did not, and the message must not point at a file
        # that is not there.
        lines = exc_info.value.artifact_lines()
        assert not any("decisions.json" in line for line in lines)

    def test_a_required_artifact_that_lands_is_announced_like_any_other(
        self, tmp_path: Path
    ) -> None:
        ui, buffer = self._capture()
        target = tmp_path / "decisions.json"
        target.write_text("{}", encoding="utf-8")
        emitted: list[object] = []
        result = _write_decompose_artifact(
            "decisions",
            "architect decisions",
            lambda: target,
            ui=ui,
            emit=emitted.append,
            rel_display=str,
            required=True,
        )
        assert result == target
        assert "Architect decisions written" in buffer.getvalue()
        assert len(emitted) == 1


class TestTheRetryFeedbackIsBounded:
    """#260 round 3. Per-record messages are better feedback and a
    worse bill.

    Round 1 answered a whole class of malformed decisions with one
    aggregate line. Round 2 answers with one message per bad record,
    which is what lets a retry fix the exact entry, but the round-2
    /simplify pass measured the other side of that: 32 decisions with
    every field the wrong type produce 224 messages and 11,480
    characters, pasted verbatim into a prompt that already carries the
    whole spec, up to max_retries times. Round 1's figure for the same
    fault was 278 characters.
    """

    def test_a_short_list_is_passed_through_whole(self) -> None:
        errors = [f"decisions[{i}].issue: must be a string" for i in range(3)]
        assert _retry_feedback(errors) == "; ".join(errors)

    def test_a_long_list_is_cut_and_says_how_much_it_cut(self) -> None:
        errors = [f"decisions[{i}].issue: must be a string" for i in range(224)]
        feedback = _retry_feedback(errors)
        assert feedback.count("; ") == _MAX_RETRY_MESSAGES
        assert feedback.endswith(f"... and {224 - _MAX_RETRY_MESSAGES} more of the same kind")
        assert len(feedback) < len("; ".join(errors)) // 4

    def test_the_kept_messages_are_the_first_ones_and_keep_their_indices(self) -> None:
        """Ordered by record, so the prefix is a usable sample and the
        index in each message still names the record it belongs to."""
        errors = [f"decisions[{i}].issue: must be a string" for i in range(50)]
        feedback = _retry_feedback(errors)
        assert feedback.startswith("decisions[0].issue:")
        assert f"decisions[{_MAX_RETRY_MESSAGES - 1}].issue:" in feedback
        assert f"decisions[{_MAX_RETRY_MESSAGES}].issue:" not in feedback

    def test_an_empty_list_is_empty(self) -> None:
        assert _retry_feedback([]) == ""


class TestVacuousPrdRejection:
    """R1.8: vacuous shapes that previously sailed through validation."""

    def test_empty_user_stories_rejected(self) -> None:
        data = json.loads(_single_component_output([]))
        errors = _validate_decompose_output(data)
        assert any("userStories" in e and "must not be empty" in e for e in errors)

    def test_empty_acceptance_criteria_rejected(self) -> None:
        data = json.loads(_single_component_output([_story(acceptanceCriteria=[])]))
        errors = _validate_decompose_output(data)
        assert any("acceptanceCriteria" in e and "must not be empty" in e for e in errors)

    def test_passes_true_rejected(self) -> None:
        data = json.loads(_single_component_output([_story(passes=True)]))
        errors = _validate_decompose_output(data)
        assert any("passes" in e and "must be false" in e for e in errors)

    def test_vacuous_output_is_retryable(self, tmp_path: Path) -> None:
        """passes:true fails attempt 1; the retry prompt carries the
        error and attempt 2 succeeds."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        agent = SequenceAgent(
            [
                _single_component_output([_story(passes=True)]),
                _single_component_output([_story()]),
            ]
        )
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=PlainUI(no_color=True),
            root_dir=tmp_path,
        )

        assert len(agent.prompts) == 2
        assert "PREVIOUS ATTEMPT FAILED" in agent.prompts[1]
        assert "passes" in agent.prompts[1]
        assert len(manifest.components) == 1


BLOCKER_ISSUE: dict[str, object] = {
    "id": "fast-undefined",
    "severity": "blocker",
    "kind": "ambiguity",
    "summary": "What 'fast' means is not defined",
    "location": "Performance section",
    "suggestion": "Specify a P95 latency budget",
}

MINOR_ISSUE: dict[str, object] = {
    "id": "edge-case-unspecified",
    "severity": "minor",
    "kind": "missing_detail",
    "summary": "Edge case unspecified",
    "location": "API section",
    "suggestion": "Document the empty-input path",
}


def _blockers(count: int) -> list[dict[str, object]]:
    """``count`` blockers the de-duplicator keeps apart, so an audit's
    recorded blocker count is the number asked for.

    The id varies with the summary because the v3.0.0 schema requires a
    unique id per issue and ``_with_ids`` only fills one in when it is
    absent: ``BLOCKER_ISSUE`` carries its own, so without this every
    entry would repeat it and validation would reject the payload.
    """
    return [
        {**BLOCKER_ISSUE, "id": f"blocker-{n}", "summary": f"blocker {n}"} for n in range(count)
    ]


def _run_decompose(
    tmp_path: Path,
    output: str,
    *,
    spec_name: str = "spec.md",
    project_name: str = "test",
) -> str:
    """Decompose a spec against a mock agent; returns the UI output.

    A blocker halt is swallowed, because what decompose printed and
    wrote before raising is what these tests are about.
    """
    spec_file = tmp_path / spec_name
    spec_file.write_text("# Spec\nBuild it.")
    (tmp_path / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    try:
        decompose_spec(
            spec_path=spec_file,
            project_name=project_name,
            base_branch="main",
            single_pr=False,
            agent=MockDecomposeAgent(output),
            ui=PlainUI(no_color=True, file=buffer),
            root_dir=tmp_path,
        )
    except SpecBlockerError:
        pass
    return buffer.getvalue()


def _journal_with(tmp_path: Path, entries: list[dict[str, object]]) -> EvolutionJournal:
    """A real journal on disk holding ``entries``, written its own way."""
    journal = EvolutionJournal(EvolutionConfig.load(tmp_path))
    journal.append_entries(entries)
    return journal


def _journal_rows(tmp_path: Path) -> list[dict[str, Any]]:
    """Every line of the journal file, parsed STRICTLY, in file order.

    Not a second copy of any selection rule - it selects nothing. It is
    here because ``get_spec_audits`` reads through
    ``read_progress_events``, which is deliberately tolerant and SKIPS a
    line it cannot parse. A test whose subject is what the WRITER put on
    disk cannot ask that reader: a malformed row would be skipped and
    the count would still come out right. The helper #337 deleted parsed
    strictly as a side effect of being a copy; this keeps the strictness
    and drops the copy.
    """
    journal = tmp_path / ".kstrl" / "evolution.jsonl"
    return [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestSpecIssuesPersistence:
    """R1.7: red-team output becomes a durable artifact + journal event."""

    def _run(self, tmp_path: Path, output: str) -> Path:
        _run_decompose(tmp_path, output)
        return tmp_path / "scripts" / "kstrl" / "spec-issues.json"

    def test_artifact_written_on_halt(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps(
            {
                "components": [],
                "spec_issues": [BLOCKER_ISSUE],
                "decisions": _closures_for([BLOCKER_ISSUE]),
            }
        )
        with pytest.raises(SpecBlockerError) as exc_info:
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(output),
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
            )

        artifact = tmp_path / "scripts" / "kstrl" / "spec-issues.json"
        assert artifact.exists()
        assert exc_info.value.artifact_path == artifact

        content = json.loads(artifact.read_text(encoding="utf-8"))
        assert content["project"] == "test"
        assert content["specFile"] == "spec.md"
        assert content["halted"] is True
        assert content["counts"] == {"blocker": 1, "major": 0, "minor": 0}
        assert content["issues"] == [
            {
                "id": "fast-undefined",
                "severity": "blocker",
                "kind": "ambiguity",
                "summary": "What 'fast' means is not defined",
                "location": "Performance section",
                "suggestion": "Specify a P95 latency budget",
            }
        ]

    def test_artifact_written_on_success(self, tmp_path: Path) -> None:
        artifact = self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[MINOR_ISSUE]),
        )
        assert artifact.exists()
        content = json.loads(artifact.read_text(encoding="utf-8"))
        assert content["halted"] is False
        assert content["counts"] == {"blocker": 0, "major": 0, "minor": 1}
        assert content["issues"][0]["summary"] == "Edge case unspecified"
        assert content["issues"][0]["location"] == "API section"

    def test_artifact_written_on_clean_audit(self, tmp_path: Path) -> None:
        """An empty issues array is the record that the audit ran and
        found nothing - distinct from no record at all."""
        artifact = self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[]),
        )
        assert artifact.exists()
        content = json.loads(artifact.read_text(encoding="utf-8"))
        assert content["halted"] is False
        assert content["issues"] == []

    def test_journal_event_on_halt(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps(
            {
                "components": [],
                "spec_issues": [BLOCKER_ISSUE],
                "decisions": _closures_for([BLOCKER_ISSUE]),
            }
        )
        with pytest.raises(SpecBlockerError):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(output),
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
            )

        assert len(_journal_rows(tmp_path)) == 1, "the writer put more than the audit on disk"
        events = journal_at(tmp_path).get_spec_audits()
        assert len(events) == 1
        assert events[0]["halted"] is True
        assert events[0]["counts"] == {"blocker": 1, "major": 0, "minor": 0}
        assert events[0]["artifact"] == "scripts/kstrl/spec-issues.json"

    def test_journal_event_on_success(self, tmp_path: Path) -> None:
        self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[MINOR_ISSUE]),
        )
        assert len(_journal_rows(tmp_path)) == 1, "the writer put more than the audit on disk"
        events = journal_at(tmp_path).get_spec_audits()
        assert len(events) == 1
        assert events[0]["halted"] is False
        assert events[0]["counts"] == {"blocker": 0, "major": 0, "minor": 1}

    def test_the_event_type_on_disk_is_the_wire_value(self, tmp_path: Path) -> None:
        """The one deliberate literal in the test tree, and why it is one.

        Every other site writes the row and reads it back through
        SPEC_ISSUES_EVENT, so renaming the constant renames both sides
        and nothing goes red. Measured in round 1 of review on #337:
        with SPEC_ISSUES_EVENT set to "spec_audit_row" and the two
        guard-side literals updated the way somebody doing the rename
        would update them, 437 passed and 1 xfailed while the journal
        wrote an event_type no journal already on disk carries. Rows
        already written are what makes the wire value not the constant's
        to change, so it is pinned here, once, against the bytes.

        Asserted as a substring of the file rather than as
        ``row["event_type"] == ...`` because that second shape is
        exactly what layer 2 of the event-name guard forbids, and one
        deliberate site is not worth an allowlist in a layer that has
        none.
        """
        self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[MINOR_ISSUE]),
        )

        raw = (tmp_path / ".kstrl" / "evolution.jsonl").read_text(encoding="utf-8")

        assert '"event_type":"spec_issues"' in raw, (
            "the spec audit on disk no longer carries the wire value spec_issues. "
            f"Renaming SPEC_ISSUES_EVENT does not rename rows already written: {raw}"
        )


class TestPrdValidationInsideRetryLoop:
    """R1.8: PRD schema errors are retryable and never leave partial files."""

    def test_malformed_story_triggers_retry(self, tmp_path: Path) -> None:
        """A story missing the 'notes' key passes decompose-output
        validation but fails PRD schema validation; the error must feed
        back through the retry loop instead of crashing after it."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        malformed = _story()
        del malformed["notes"]
        agent = SequenceAgent(
            [
                _single_component_output([malformed]),
                _single_component_output([_story()]),
            ]
        )
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=PlainUI(no_color=True),
            root_dir=tmp_path,
        )

        assert len(agent.prompts) == 2
        assert "PREVIOUS ATTEMPT FAILED" in agent.prompts[1]
        assert "notes" in agent.prompts[1]
        assert len(manifest.components) == 1
        prd_path = tmp_path / "scripts" / "kstrl" / "feature" / "comp-a" / "prd.json"
        assert prd_path.exists()
        assert PRD.load(prd_path).user_stories[0].id == "US-001"

    def test_no_partial_files_after_terminal_failure(self, tmp_path: Path) -> None:
        """Terminal validation failure must not leave prd.json, feature
        dirs, or a manifest behind."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        malformed = _story()
        del malformed["notes"]
        agent = MockDecomposeAgent(_single_component_output([malformed]))
        with pytest.raises(ValueError, match="Failed to decompose"):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
                max_retries=2,
            )

        assert not (tmp_path / "scripts" / "kstrl" / "feature").exists()
        assert not (tmp_path / "scripts" / "kstrl" / "manifest.json").exists()
        assert list(tmp_path.rglob("prd.json")) == []

    def test_write_failure_cleans_up_partial_prds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If writing component 2's PRD fails, component 1's already
        written PRD and the directories created for it are removed; the
        spec-issues audit artifact survives."""
        import kstrl.decompose as decompose_mod

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        real_generate = decompose_mod._generate_component_prd
        calls: list[str] = []

        def flaky_generate(
            comp_data: dict[str, object],
            root_dir: Path,
            branch_name: str,
            spec_issues: Sequence[dict[str, str]] = (),
        ) -> Path:
            calls.append(str(comp_data["id"]))
            if len(calls) == 2:
                raise OSError("disk full")
            return real_generate(  # type: ignore[arg-type]
                comp_data,
                root_dir,
                branch_name,
                spec_issues,
            )

        monkeypatch.setattr(decompose_mod, "_generate_component_prd", flaky_generate)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        with pytest.raises(OSError, match="disk full"):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
            )

        assert calls == ["database", "api"]
        assert list(tmp_path.rglob("prd.json")) == []
        assert not (tmp_path / "scripts" / "kstrl" / "feature").exists()
        assert not (tmp_path / "scripts" / "kstrl" / "manifest.json").exists()
        # The audit artifact is deliberately kept.
        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()


class TestSpecConvergenceReport:
    """#260: what this audit says about the previous one."""

    def _issue(self, severity: str, kind: str, summary: str) -> SpecIssue:
        return SpecIssue(severity=severity, kind=kind, summary=summary)

    def _entry(
        self,
        issues: list[SpecIssue] | None,
        spec_file: str = "spec.md",
    ) -> dict[str, object]:
        """One prior journal entry, in the shape decompose writes."""
        entry: dict[str, object] = {"event_type": SPEC_ISSUES_EVENT, "spec_file": spec_file}
        if issues is not None:
            entry["issues"] = _issue_dicts(issues)
        return entry

    def test_first_run_has_nothing_to_compare(self) -> None:
        assert _build_convergence([self._issue("blocker", "ambiguity", "a")], "spec.md", []) is None

    def test_entry_without_an_issue_list_is_not_a_comparison(self) -> None:
        """An entry that cannot be counted is not evidence, and a
        journal holding only such entries reads as no history."""
        assert _build_convergence([], "spec.md", [self._entry(None)]) is None

    def test_counts_and_deltas_against_the_previous_run(self) -> None:
        report = _build_convergence(
            [
                self._issue("blocker", "ambiguity", "new one"),
                self._issue("blocker", "contradiction", "another"),
                self._issue("minor", "other", "small"),
            ],
            "spec.md",
            [self._entry([self._issue("blocker", "ambiguity", "old one")])],
        )

        assert report is not None
        assert report.current_counts == {"blocker": 2, "major": 0, "minor": 1}
        assert report.previous_counts == {"blocker": 1, "major": 0, "minor": 0}
        assert report.previous_total == 1

    def test_repeats_match_on_normalized_text(self) -> None:
        """Collapsed whitespace and folded case still match, and so does
        a changed severity; different wording does not."""
        report = _build_convergence(
            [
                self._issue("major", "ambiguity", "What   'FAST' means\nis not defined"),
                self._issue("blocker", "ambiguity", "What fast means is undefined"),
            ],
            "spec.md",
            [
                self._entry(
                    [
                        self._issue("blocker", "ambiguity", "What 'fast' means is not defined"),
                        self._issue("minor", "other", "gone"),
                    ]
                )
            ],
        )

        assert report is not None
        assert report.repeated == 1
        assert report.previous_total == 2

    def test_same_summary_under_a_different_kind_is_not_a_repeat(self) -> None:
        report = _build_convergence(
            [self._issue("blocker", "contradiction", "same words")],
            "spec.md",
            [self._entry([self._issue("blocker", "ambiguity", "same words")])],
        )

        assert report is not None
        assert report.repeated == 0

    def test_trend_spans_every_recorded_run_and_ends_with_this_one(self) -> None:
        history = [
            self._entry([self._issue("blocker", "ambiguity", f"r1-{n}") for n in range(7)]),
            self._entry([self._issue("blocker", "ambiguity", f"r2-{n}") for n in range(11)]),
            self._entry([self._issue("blocker", "ambiguity", "r3-0")]),
            self._entry([self._issue("blocker", "ambiguity", f"r4-{n}") for n in range(3)]),
        ]

        report = _build_convergence(
            [self._issue("blocker", "ambiguity", f"r5-{n}") for n in range(4)],
            "spec-slice-1.md",
            history,
        )

        assert report is not None
        assert report.blocker_trend == (7, 11, 1, 3, 4)

    def test_previous_spec_file_is_carried_for_the_rename_case(self) -> None:
        report = _build_convergence(
            [],
            "spec-slice-1.md",
            [self._entry([], spec_file="spec.md")],
        )

        assert report is not None
        assert report.previous_spec_file == "spec.md"
        assert report.current_spec_file == "spec-slice-1.md"

    def test_malformed_stored_issues_do_not_crash_the_reader(self) -> None:
        """Journals written by older versions, and any entry an
        operator hand-edited, must read rather than raise."""
        history: list[dict[str, object]] = [
            {
                "event_type": SPEC_ISSUES_EVENT,
                "issues": [
                    "not a dict",
                    {"severity": "blocker"},
                    {"summary": None, "kind": 7, "severity": "major"},
                ],
            }
        ]

        report = _build_convergence([], "spec.md", history)

        assert report is not None
        assert report.previous_counts == {"blocker": 1, "major": 1, "minor": 0}
        assert report.previous_spec_file == ""

    def test_two_current_issues_matching_one_previous_cannot_overcount(self) -> None:
        """`repeated` is rendered as a statement about the previous
        run, so it must be counted over that side. Counting the current
        side let two current issues match one previous issue and made
        "did not come back" negative: `_issue_identity` drops severity
        and normalizes text, and `_parse_spec_issues` de-duplicates
        nothing, so this shape is reachable from real architect output.
        """
        report = _build_convergence(
            [
                self._issue("blocker", "ambiguity", "What fast means is undefined"),
                self._issue("major", "ambiguity", "What  FAST  means is undefined"),
            ],
            "spec.md",
            [self._entry([self._issue("blocker", "ambiguity", "What fast means is undefined")])],
        )

        assert report is not None
        assert report.previous_total == 1
        assert report.repeated == 1
        assert report.previous_total - report.repeated == 0

    def test_a_previous_issue_raised_twice_counts_twice_when_it_returns(self) -> None:
        """The mirror case, and why this counts the previous list
        rather than intersecting two identity sets: both of the
        previous run's issues did come back, so 0 of 2 did not."""
        duplicated = self._issue("blocker", "ambiguity", "same finding, said twice")
        report = _build_convergence(
            [self._issue("blocker", "ambiguity", "same finding, said twice")],
            "spec.md",
            [self._entry([duplicated, duplicated])],
        )

        assert report is not None
        assert report.previous_total == 2
        assert report.repeated == 2


class TestExcludedHistory:
    """#280: the audit history the report does not count, named."""

    def _entry(
        self,
        project: str,
        spec_file: str = "spec.md",
        event_type: str = SPEC_ISSUES_EVENT,
        timestamp: str = "2026-08-20T00:00:00Z",
    ) -> dict[str, object]:
        return {
            "event_type": event_type,
            "project": project,
            "spec_file": spec_file,
            "timestamp": timestamp,
        }

    def _history(
        self,
        journal: EvolutionJournal,
        project: str,
        spec_file: str = "mine.md",
        lookback: int = 10,
    ) -> ExcludedHistory:
        audits = _journal_snapshot(journal, project).audits
        return _excluded_history(audits, project, spec_file, lookback)

    def _lines(
        self,
        journal: EvolutionJournal,
        project: str,
        spec_file: str = "mine.md",
        counted: int = 0,
    ) -> str:
        """The rendered note lines for ``project``, joined."""
        return "\n".join(
            _excluded_lines(self._history(journal, project, spec_file), project, counted)
        )

    def test_a_journal_of_one_project_excludes_no_other_project(self) -> None:
        entries = [self._entry("writers-room"), self._entry("writers-room")]

        assert _excluded_projects(entries, "writers-room", "spec.md") == ()

    def test_another_project_is_counted_with_the_files_it_read(self) -> None:
        entries = [
            self._entry("writers-room", "spec.md"),
            self._entry("writers-room", "spec.md"),
            self._entry("writers-room-slice1", "spec-slice-1.md"),
        ]

        excluded = _excluded_projects(entries, "writers-room-slice1", "spec-slice-1.md")

        assert len(excluded) == 1
        assert excluded[0].project == "writers-room"
        assert excluded[0].audits == 2
        assert excluded[0].spec_files == ("spec.md",)
        assert excluded[0].read_this_spec is False
        assert excluded[0].last_recorded == "2026-08-20T00:00:00Z"

    def test_only_spec_audits_count(self, tmp_path: Path) -> None:
        """The journal carries component results and experiments too.
        Counting those would inflate the number the operator reads.

        Through the snapshot rather than by handing this function a
        component_result directly: since #314 the selection is
        ``EvolutionJournal.get_spec_audits``'s, so the end-to-end path
        is where the claim is still true.
        """
        journal = EvolutionJournal(EvolutionConfig.load(tmp_path))
        journal.append_entries(
            [
                self._entry("other", event_type="component_result"),
                self._entry("other", event_type=SPEC_ISSUES_EVENT),
            ]
        )

        audits = _journal_snapshot(journal, "mine").audits
        excluded = _excluded_projects(audits, "mine", "mine.md")

        assert [(e.project, e.audits) for e in excluded] == [("other", 1)]

    def test_an_entry_without_a_project_is_not_evidence(self) -> None:
        """An unnamed project cannot be somewhere the operator can go
        and look, so it is not history worth pointing at."""
        entries: list[dict[str, object]] = [{"event_type": SPEC_ISSUES_EVENT}]

        assert _excluded_projects(entries, "mine", "mine.md") == ()

    def test_a_json_null_project_is_not_a_project_named_none(self) -> None:
        """Round 1 of review: ``str(entry.get("project", ""))`` renders
        a JSON null as the literal "None", which then passes the
        emptiness guard and prints a phantom project. A null field is
        an absent field, and ``get_spec_issue_runs`` promises nothing
        is assumed about an entry beyond it being a JSON object."""
        entries: list[dict[str, object]] = [
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": None,
                "spec_file": None,
                "timestamp": None,
            }
        ]

        assert _excluded_projects(entries, "mine", "mine.md") == ()

    def test_a_non_string_spec_file_and_timestamp_are_dropped_not_stringified(
        self,
    ) -> None:
        """The same rule on the other two fields: a hand-edited journal
        must not put a file literally named ``None`` or ``7`` in the
        list, nor a date the operator cannot act on."""
        entries: list[dict[str, object]] = [
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": "other",
                "spec_file": 7,
                "timestamp": None,
            }
        ]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert excluded[0].spec_files == ()
        assert excluded[0].last_recorded == ""

    def test_projects_are_ordered_by_how_much_history_they_hold(self) -> None:
        entries = [
            self._entry("a"),
            self._entry("b"),
            self._entry("b"),
            self._entry("c"),
        ]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert [(e.project, e.audits) for e in excluded] == [("b", 2), ("a", 1), ("c", 1)]

    def test_a_project_that_read_this_spec_file_sorts_first(self) -> None:
        """#280's first arm: the project that audited the file this run
        audited is the strongest evidence of a plain rename, so it
        leads even though it holds the least history here."""
        entries = [self._entry("busy") for _ in range(9)] + [self._entry("renamed", "mine.md")]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert [e.project for e in excluded] == ["renamed", "busy"]
        assert excluded[0].read_this_spec is True
        assert excluded[1].read_this_spec is False

    def test_the_last_recorded_timestamp_is_the_newest_entry_in_file_order(
        self,
    ) -> None:
        """The journal is append-only, so the last entry for a project
        is its most recent audit."""
        entries = [
            self._entry("other", timestamp="2026-01-01T00:00:00Z"),
            self._entry("other", timestamp="2026-06-30T12:00:00Z"),
        ]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert excluded[0].last_recorded == "2026-06-30T12:00:00Z"

    def test_distinct_spec_files_are_deduplicated_and_sorted(self) -> None:
        entries = [
            self._entry("other", "b.md"),
            self._entry("other", "a.md"),
            self._entry("other", "b.md"),
            self._entry("other", ""),
        ]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert excluded[0].audits == 4
        assert excluded[0].spec_files == ("a.md", "b.md")

    def test_no_journal_excludes_nothing(self) -> None:
        assert _excluded_history([], "writers-room", "spec.md", 10).is_empty

    def test_the_read_covers_the_whole_journal(self, tmp_path: Path) -> None:
        journal = _journal_with(tmp_path, [self._entry("writers-room", "spec.md")] * 2)

        assert self._lines(journal, "writers-room-slice1", "spec-slice-1.md") == (
            "Note: audits are matched by project name, and this report covers "
            "'writers-room-slice1'. This journal also records 2 spec audit(s) under "
            "'writers-room' (2 audit(s), spec.md, last 2026-08-20)."
        )

    def test_a_journal_holding_only_this_project_names_no_other(
        self,
        tmp_path: Path,
    ) -> None:
        journal = _journal_with(tmp_path, [self._entry("writers-room")] * 3)

        assert self._history(journal, "writers-room", "spec.md").projects == ()

    def test_this_projects_own_audits_are_counted_unwindowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The count the accounting line rests on. Not windowed by
        ``lookback_runs``, because a count of what the trend does not
        cover that was itself windowed would omit history silently."""
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        journal = _journal_with(tmp_path, [self._entry("writers-room")] * 6)

        assert self._history(journal, "writers-room", "spec.md", lookback=2).own_recorded == 6

    def test_a_missing_journal_file_excludes_nothing(self, tmp_path: Path) -> None:
        journal = EvolutionJournal(EvolutionConfig.load(tmp_path))

        assert self._history(journal, "writers-room", "spec.md").is_empty

    def test_a_torn_line_does_not_cost_the_note(self, tmp_path: Path) -> None:
        """The journal is append-only and a crash mid-write leaves a
        torn tail; the rest of the history still has to be readable.

        The write side of the same tear is
        ``tests/test_journal_torn_tail.py`` (#312), and the fragment is
        shared rather than copied so the two cannot drift apart.
        """
        journal = _journal_with(tmp_path, [self._entry("writers-room", "spec.md")])
        tear(journal.config.journal_path)

        assert "records 1 spec audit(s)" in self._lines(journal, "writers-room-slice1")

    def test_many_projects_are_summarised_rather_than_all_named(
        self,
        tmp_path: Path,
    ) -> None:
        """A display cap on the names, never on the count: the total
        still covers every audit the report leaves out."""
        journal = _journal_with(tmp_path, [self._entry(f"p{n}", f"s{n}.md") for n in range(6)])

        line = self._lines(journal, "mine")

        assert "records 6 spec audit(s)" in line
        assert (
            "'p0' (1 audit(s), s0.md, last 2026-08-20), "
            "'p1' (1 audit(s), s1.md, last 2026-08-20), "
            "'p2' (1 audit(s), s2.md, last 2026-08-20) and 3 more project(s)" in line
        )

    def test_every_project_that_read_this_spec_file_survives_the_cap(
        self,
        tmp_path: Path,
    ) -> None:
        """Round 1 of review: the sort put spec-file matches first but
        the cap then truncated them, so four projects that had all read
        the current spec file - a repo that split one spec across
        several names, which is #280's own shape - printed three and
        "and 1 more project(s)". The cap now applies only to projects
        that did NOT read it."""
        journal = _journal_with(
            tmp_path,
            [self._entry(f"p{n}", "mine.md") for n in range(4)]
            + [self._entry(f"q{n}", "other.md") for n in range(4)],
        )

        line = self._lines(journal, "mine")

        for name in ("p0", "p1", "p2"):
            assert f"'{name}' (1 audit(s), mine.md" in line
        assert "and 1 more project(s) that read this spec file" in line
        assert "and 1 more project(s)." in line
        assert "'q3'" not in line

    def test_many_spec_files_under_one_project_are_summarised_too(
        self,
        tmp_path: Path,
    ) -> None:
        journal = _journal_with(tmp_path, [self._entry("other", f"s{n}.md") for n in range(5)])

        assert "'other' (5 audit(s), s0.md, s1.md, s2.md and 2 more file(s), last " in (
            self._lines(journal, "mine")
        )

    def test_an_entry_with_no_timestamp_names_the_project_without_a_date(
        self,
        tmp_path: Path,
    ) -> None:
        journal = _journal_with(tmp_path, [self._entry("other", "o.md", timestamp="")])

        assert "'other' (1 audit(s), o.md)." in self._lines(journal, "mine")


class TestJournalFieldsAreReadNotStringified:
    """#280 round 2, finding 1: a null field is an absent field, on
    every site that reads one, not just the site that was patched."""

    def _run_with_history(self, tmp_path: Path, entry: dict[str, object]) -> str:
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        return _run_decompose(
            tmp_path,
            _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE]),
            spec_name="spec.md",
            project_name="mine",
        )

    def test_a_null_spec_file_is_not_a_phantom_rename(self, tmp_path: Path) -> None:
        """``str(entry.get("spec_file", ""))`` yields 'None' when the
        key is PRESENT and null, so the rename line fired comparing
        'None' with the real file: "the previous audit read None"."""
        output = self._run_with_history(
            tmp_path,
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": "mine",
                "spec_file": None,
                "timestamp": "2026-08-20T00:00:00Z",
                "issues": [{"severity": "blocker", "kind": "ambiguity", "summary": "old"}],
            },
        )

        assert "previous audit read None" not in output
        assert "Runs are matched by project name" not in output

    def test_an_unscoreable_severity_does_not_put_a_false_zero_in_the_trend(
        self,
        tmp_path: Path,
    ) -> None:
        """The worse half of the same class. ``_issue_counts`` buckets
        by severity, so seven issues stored with a null severity were
        counted as nothing: "Previous run raised 0 issue(s)" and a 0 in
        the blocker trend for a run that raised seven. The audit is now
        refused and reported instead of part-scored."""
        output = self._run_with_history(
            tmp_path,
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": "mine",
                "spec_file": "spec.md",
                "timestamp": "2026-08-20T00:00:00Z",
                "issues": [
                    {"severity": None, "kind": "ambiguity", "summary": f"old-{n}"} for n in range(7)
                ],
            },
        )

        assert "Previous run raised 0 issue(s)" not in output
        assert "0, 1 (blockers" not in output
        assert "could not be scored" in output

    def test_a_severity_outside_the_three_is_refused_by_the_rehydrator(self) -> None:
        entry: dict[str, object] = {
            "issues": [{"severity": "critical", "kind": "ambiguity", "summary": "x"}]
        }

        assert _stored_issues(entry) is None

    def test_a_well_formed_issue_list_still_rehydrates(self) -> None:
        entry: dict[str, object] = {
            "issues": [{"severity": "minor", "kind": "ambiguity", "summary": "x"}]
        }
        stored = _stored_issues(entry)

        assert stored is not None
        assert [i.severity for i in stored] == ["minor"]


class TestUnattributedAudits:
    """#280 round 2, finding 2: audits belonging to no project name."""

    def test_they_are_counted_by_a_third_bucket(self) -> None:
        """They satisfy neither ``own_recorded`` nor ``_excluded_projects``,
        so three audits on disk were reported as one."""
        entries: list[dict[str, object]] = [
            {"event_type": SPEC_ISSUES_EVENT, "project": None, "spec_file": "a.md"},
            {"event_type": SPEC_ISSUES_EVENT, "spec_file": "b.md"},
            {"event_type": SPEC_ISSUES_EVENT, "project": "other", "spec_file": "c.md"},
        ]

        history = _excluded_history(entries, "mine", "mine.md", 10)

        assert history.own_recorded == 0
        assert history.other_audits == 1
        assert history.unattributed == 2

    def test_every_spec_audit_lands_in_exactly_one_bucket(self, tmp_path: Path) -> None:
        """The property the accounting docstring claims, checked rather
        than asserted in prose. Read back through the journal, so the
        component_result is dropped by the reader that owns that rule
        (#314) and the buckets still sum to the audits on disk."""
        entries: list[dict[str, object]] = [
            {"event_type": SPEC_ISSUES_EVENT, "project": "mine"},
            {"event_type": SPEC_ISSUES_EVENT, "project": "mine"},
            {"event_type": SPEC_ISSUES_EVENT, "project": "other"},
            {"event_type": SPEC_ISSUES_EVENT, "project": None},
            {"event_type": "component_result", "project": "mine"},
        ]
        journal = EvolutionJournal(EvolutionConfig.load(tmp_path))
        journal.append_entries(entries)

        audits = _journal_snapshot(journal, "mine").audits
        history = _excluded_history(audits, "mine", "mine.md", 10)

        assert len(audits) == 4
        assert history.own_recorded + history.other_audits + history.unattributed == len(audits)

    @pytest.mark.parametrize("name", ["", "mine", "nobody", " mine ", "   "])
    def test_the_partition_holds_for_every_project_name(self, tmp_path: Path, name: str) -> None:
        """#338: the test above pins the property at one project name,
        and the two counts were computed by two predicates that agree
        everywhere except at "". There ``x == project_name`` and
        ``not x`` are the same question, so an audit with an absent,
        null or non-string project was counted as this project's AND as
        unattributed: seven audits, eleven placements.

        The second assertion is the one that fixes WHICH bucket takes
        it. ``EvolutionJournal.get_spec_issue_runs`` matches a project
        by the same ``entry_str`` expression, so at "" the trend counts
        those audits; ``own_recorded`` has to count them too, or the
        accounting printed under the trend contradicts it and the note
        saying neither counts them is false.
        """
        entries: list[dict[str, Any]] = [
            audit("mine"),
            audit("mine", "b.md"),
            audit("other"),
            audit(None, "null.md"),
            audit(7, "int.md"),
            audit("", "empty.md"),
            # The helper always writes the key; an absent one is the
            # shape a journal from an older version carries.
            {
                "timestamp": "2026-08-20T00:00:00Z",
                "event_type": SPEC_ISSUES_EVENT,
                "spec_file": "absent.md",
            },
        ]
        journal = journal_at(tmp_path)
        journal.append_entries(entries)

        audits = _journal_snapshot(journal, name).audits
        history = _excluded_history(audits, name, "mine.md", 10)

        assert len(audits) == len(entries)
        assert history.own_recorded + history.other_audits + history.unattributed == len(audits)
        assert history.own_recorded == len(
            journal.get_spec_issue_runs(name, len(audits), audits=audits)
        )


class TestOneJournalRead:
    """#280 round 2, findings 6 and 7, and #314: one read, taken through
    ``EvolutionJournal`` rather than past it."""

    def _journal(
        self,
        tmp_path: Path,
        entries: list[dict[str, Any]],
        lookback_runs: int | None = None,
    ) -> EvolutionJournal:
        journal = journal_at(tmp_path)
        if lookback_runs is not None:
            journal.config.lookback_runs = lookback_runs
        journal.append_entries(entries)
        return journal

    def test_the_journal_is_parsed_once_per_report(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The trend and the accounting used to read the same file
        twice. The call site has both in scope, so one read feeds both
        and they cannot disagree because the file moved between them.

        Counted at ``_read_all_entries``, the journal's single read
        point, so dropping the ``audits=`` argument that lets the
        window reuse the snapshot fails here rather than costing a
        silent second parse.
        """
        reads: list[int] = []
        real = EvolutionJournal._read_all_entries

        def counting(self: EvolutionJournal) -> list[dict[str, object]]:
            reads.append(1)
            return real(self)

        monkeypatch.setattr(EvolutionJournal, "_read_all_entries", counting)
        _run_decompose(
            tmp_path,
            _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE]),
            project_name="mine",
        )

        assert len(reads) == 1

    def test_the_report_reads_through_the_journal_not_its_storage_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#314 item 1. ``_journal_snapshot`` used to open
        ``journal.config.journal_path`` itself, which works only while
        the file is the whole story; ``get_spec_audits`` says what that
        costs when it stops being.

        Proved by giving the journal a reader that answers something
        the file does not contain. A snapshot that reaches for the path
        cannot see it, so this fails on the shortcut rather than on the
        hypothetical second segment nobody has written yet.
        """
        journal = self._journal(tmp_path, [audit("mine", "on-disk.md")])
        elsewhere = [audit("mine", "b.md"), audit("other", "c.md")]
        monkeypatch.setattr(EvolutionJournal, "get_spec_audits", lambda self: elsewhere)

        snapshot = _journal_snapshot(journal, "mine")

        assert [a["spec_file"] for a in snapshot.audits] == ["b.md", "c.md"]
        assert [w["spec_file"] for w in snapshot.window] == ["b.md"]

    def test_the_window_is_the_journals_own_over_the_journals_own_lookback(
        self,
        tmp_path: Path,
    ) -> None:
        """#314 item 2. ``_windowed_audits`` was a second copy of the
        rule ``get_spec_issue_runs`` owns, and two copies of one rule
        can drift; if they had, the trend and the accounting would have
        disagreed about the same journal. There is one copy now, and
        the snapshot has to reach it with the journal's own
        ``lookback_runs`` rather than a number of its own.
        """
        entries = [audit("mine" if n % 2 else "other", f"spec-{n}.md") for n in range(9)]

        for lookback in (1, 3, 10):
            # A journal of its own per lookback: one file appended to
            # three times would compare a growing history against
            # itself and pass whatever the window did.
            journal = self._journal(tmp_path / f"lookback-{lookback}", entries, lookback)
            assert _journal_snapshot(journal, "mine").window == journal.get_spec_issue_runs(
                "mine", last_n=lookback
            )

    def test_no_journal_reads_nothing_and_windows_nothing(self) -> None:
        assert _journal_snapshot(None, "mine") == AuditSnapshot(audits=[], window=[], lookback=0)

    def test_the_snapshot_says_unhashable_instead_of_pretending(self) -> None:
        """A frozen dataclass with ``eq`` on gets a generated
        ``__hash__``, and this one holds two lists, so the generated one
        raised ``unhashable type: 'list'`` from a method nobody wrote.
        Nothing hashes a snapshot; making it hashable is not available
        either, since tuple fields would still hold ``dict`` elements.
        So it says so, and names itself when somebody tries.

        Both halves asserted: the message alone would pass on a class
        that had simply kept the broken generated hash.
        """
        snapshot = AuditSnapshot(audits=[{"project": "mine"}], window=[], lookback=3)

        assert AuditSnapshot.__hash__ is None
        with pytest.raises(TypeError, match="unhashable type: 'AuditSnapshot'"):
            hash(snapshot)
        assert snapshot == AuditSnapshot(audits=[{"project": "mine"}], window=[], lookback=3)


class TestExcludedAccountingLine:
    """#280 round 1, finding 2: the same-project half of the accounting."""

    def _history(self, own: int, lookback: int = 10) -> ExcludedHistory:
        return ExcludedHistory(own_recorded=own, projects=(), lookback=lookback)

    def test_nothing_is_said_when_the_trend_counted_everything(self) -> None:
        assert _excluded_lines(self._history(3), "mine", 3) == []

    def test_a_windowed_out_audit_is_a_trend_footnote_not_a_warning(self) -> None:
        """Round 2 of review: once a project has more audits than
        ``lookback_runs`` this holds on every run forever, so a Note
        would be permanent noise. It is a footnote on the trend line
        instead; see ``_surface_trend``."""
        history = self._history(40, lookback=10)

        assert _excluded_lines(history, "mine", 10) == []
        assert history.windowed_out(10) == 30
        assert history.unreadable(10) == 0

    def test_an_audit_the_window_offered_but_could_not_be_scored_is_named(self) -> None:
        """The anomaly half of the same gap, which does deserve a line."""
        history = self._history(3, lookback=10)

        assert history.unreadable(0) == 3
        assert _excluded_lines(history, "mine", 0) == [
            "Note: 3 earlier audit(s) of 'mine' fall inside the lookback window but "
            "could not be scored, so the trend does not count them. An audit is "
            "skipped when it records no issue list, or an issue whose severity is "
            "not blocker, major or minor."
        ]

    def test_the_two_causes_are_separated_when_both_apply(self) -> None:
        history = self._history(40, lookback=10)

        assert history.unreadable(7) == 3
        assert history.windowed_out(7) == 30

    def test_audits_with_no_project_name_are_their_own_line(self) -> None:
        """Round 2 of review: an entry whose ``project`` is null or
        absent was counted by neither axis, so three audits on disk
        were reported as one."""
        history = ExcludedHistory(own_recorded=0, projects=(), unattributed=2, lookback=10)

        assert _excluded_lines(history, "mine", 0) == [
            "Note: 2 spec audit(s) in this journal record no project name, so neither "
            "the trend nor the line above counts them."
        ]
        assert not history.is_empty

    def test_counted_audits_is_read_off_the_rendered_trend(self) -> None:
        """So the accounting line can never disagree with the trend
        printed directly above it."""
        report = SpecConvergence(
            current_counts={"blocker": 0, "major": 0, "minor": 0},
            previous_counts={"blocker": 0, "major": 0, "minor": 0},
            current_spec_file="spec.md",
            previous_spec_file="spec.md",
            repeated=0,
            blocker_trend=(1, 1, 0),
        )

        assert _counted_audits(report) == 2
        assert _counted_audits(None) == 0


class TestSpecConvergenceThroughDecompose:
    """The report as the operator meets it, on the real code path."""

    def _run(
        self,
        tmp_path: Path,
        issues: list[dict[str, object]],
        spec_name: str = "spec.md",
        project_name: str = "writers-room",
    ) -> str:
        return _run_decompose(
            tmp_path,
            _single_component_output([_story()], spec_issues=issues),
            spec_name=spec_name,
            project_name=project_name,
        )

    def test_first_run_prints_no_report(self, tmp_path: Path) -> None:
        output = self._run(tmp_path, [BLOCKER_ISSUE])
        assert "Spec Convergence" not in output

    def test_second_run_compares_against_the_first(self, tmp_path: Path) -> None:
        """Also pins the ordering: the history is read before this run
        is appended, so "previous run" is never this run."""
        self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(
            tmp_path,
            [
                BLOCKER_ISSUE,
                {
                    "severity": "blocker",
                    "kind": "contradiction",
                    "summary": "Stage is recorded twice",
                    "location": "",
                    "suggestion": "",
                },
                MINOR_ISSUE,
            ],
        )

        assert "Spec Convergence" in output
        assert "Blocker:" in output
        assert "2 (previous run: 1, +1)" in output
        assert "Minor:" in output
        assert "1 (previous run: 0, +1)" in output
        assert "1, 2 (blockers, oldest run first)" in output
        assert "Previous run raised 1 issue(s): 1 reappear verbatim, 0 do not." in output

    def test_journal_entries_still_carry_no_run_id(self, tmp_path: Path) -> None:
        """The report reads entries the run-windowed reader drops; if a
        run_id ever appears here, that reader would start windowing
        spec audits by factory run and this feature would go quiet.

        Read off the bytes rather than through ``get_spec_audits``: the
        subject is what decompose PUT on disk, so a reader that dropped
        or renamed an unknown key could answer this question "no run_id"
        about rows that carry one, which is the failure the paragraph
        above names. Every row this run writes is a spec audit
        (``_record_spec_issues_event`` is decompose's only journal
        write), so nothing is selected here and no selection rule is
        copied.
        """
        self._run(tmp_path, [BLOCKER_ISSUE])
        rows = _journal_rows(tmp_path)

        assert rows, "the run wrote no journal rows, so this assertion pins nothing"
        assert all("run_id" not in row for row in rows), rows

    def test_a_legacy_entry_without_run_id_does_not_break_the_read(
        self,
        tmp_path: Path,
    ) -> None:
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps({"event_type": "component_result", "component": "legacy"}) + "\n"
        )

        self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert "1 (previous run: 1, no change)" in output
        assert "1, 1 (blockers, oldest run first)" in output

    def test_a_rename_is_reported_rather_than_hidden(self, tmp_path: Path) -> None:
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec.md")
        output = self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec-slice-1.md")

        assert "the previous audit read spec.md, this one read spec-slice-1.md" in output

    def test_a_different_project_has_its_own_history(self, tmp_path: Path) -> None:
        """Still its own trend, but no longer its own silence (#280).

        This test previously asserted the whole section was absent,
        which is exactly the loss #280 reports: the operator was told
        nothing at all about the audits the trend had just dropped.
        """
        self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="deckgen")

        assert "Trend:" not in output
        assert "No earlier audit of this project is recorded." in output
        assert "also records 1 spec audit(s)" in output
        assert "'writers-room' (1 audit(s), spec.md, last " in output

    def test_the_rename_that_lost_two_runs_now_says_so(self, tmp_path: Path) -> None:
        """#280's own shape, end to end: five audits, a project AND
        spec rename between runs 2 and 3, and a trend that covers only
        the last three. The trend is unchanged; what is new is the line
        that says the other two exist."""
        for spec, project in [
            ("spec.md", "writers-room"),
            ("spec.md", "writers-room"),
            ("spec-slice-1.md", "writers-room-slice1"),
            ("spec-slice-1.md", "writers-room-slice1"),
        ]:
            self._run(tmp_path, [BLOCKER_ISSUE], spec_name=spec, project_name=project)
        output = self._run(
            tmp_path,
            [BLOCKER_ISSUE],
            spec_name="spec-slice-1.md",
            project_name="writers-room-slice1",
        )

        assert "1, 1, 1 (blockers, oldest run first)" in output
        assert (
            "Note: audits are matched by project name, and this report covers "
            "'writers-room-slice1'. This journal also records 2 spec audit(s) under "
            "'writers-room' (2 audit(s), spec.md, last " in output
        )
        # The trend counted every audit of this project, so neither the
        # anomaly line nor the trend footnote has anything to say.
        assert "earlier audit(s) of 'writers-room-slice1'" not in output
        assert "outside the lookback window" not in output

    def test_an_ordinary_single_project_history_prints_no_note(
        self,
        tmp_path: Path,
    ) -> None:
        """The false-positive check. A warning that always fires is
        noise, and noise is how a report stops being read."""
        for _ in range(4):
            self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert "1, 1, 1, 1, 1 (blockers, oldest run first)" in output
        assert "Note:" not in output

    def test_a_genuine_two_project_repo_is_told_the_truth(self, tmp_path: Path) -> None:
        """The measured cost of keying the note on "any other project"
        rather than on a matching spec file: a repo holding two real
        projects sees the line on every decompose of either.

        Pinned rather than hidden, because it is the price of covering
        #280's own session, where the spec file was renamed at the same
        moment as the project and a spec-file match would have found
        nothing. The line names the other project and the file it read,
        so an operator on 'billing' dismisses 'auth' (auth.md) at a
        glance instead of investigating.
        """
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="auth.md", project_name="auth")
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="billing.md", project_name="billing")
        output = self._run(
            tmp_path,
            [BLOCKER_ISSUE],
            spec_name="billing.md",
            project_name="billing",
        )

        assert "1, 1 (blockers, oldest run first)" in output
        assert "this report covers 'billing'" in output
        assert "also records 1 spec audit(s) under 'auth' (1 audit(s), auth.md, last " in output

    def test_a_rename_within_one_project_prints_no_note(self, tmp_path: Path) -> None:
        """The spec file moving is already reported by the rename line;
        the note is about the OTHER half of the key and must stay out
        of it."""
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec.md")
        output = self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec-slice-1.md")

        assert "the previous audit read spec.md" in output
        assert "Note:" not in output

    def test_no_note_when_the_journal_is_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A disabled journal reads nothing, so it can claim nothing."""
        self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        monkeypatch.setenv("KSTRL_EVOLUTION_ENABLED", "0")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="deckgen")

        assert "Spec Convergence" not in output

    def test_the_note_is_not_windowed_by_the_lookback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``lookback_runs`` bounds how far back the trend reaches. A
        note about history the trend excludes that were itself windowed
        would omit history silently, which is the bug it fixes."""
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for _ in range(4):
            self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="deckgen")

        assert "also records 4 spec audit(s)" in output

    def test_a_windowed_out_run_is_counted_not_swallowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 1 of review, finding 2: the note counted only
        cross-project audits while its wording claimed everything the
        journal holds, so same-project audits dropped by
        ``lookback_runs`` went silently missing. That is #280's own
        defect on the other axis. Reachable on the DEFAULT lookback of
        10 after 11 audits, so not an exotic config.
        """
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for _ in range(6):
            self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")

        assert (
            "1, 1, 1 (blockers, oldest run first; 4 older audit(s) outside the "
            "lookback window)" in output
        )
        # Round 2 of review: this is the configured steady state, so it
        # qualifies the trend in place rather than firing a warning that
        # would print on every run forever.
        assert "could not be scored" not in output

    def test_no_earlier_audit_is_claimed_only_when_none_is_recorded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 1 of review, finding 1: with ``lookback_runs=0`` the
        trend reads nothing, and the report announced that no earlier
        audit of this project was recorded while three of them sat on
        disk. A confident statement over less data than the journal
        holds is the defect #280 is about.
        """
        for _ in range(3):
            self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "0")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")

        assert "No earlier audit of this project is recorded." not in output
        assert (
            "No earlier audit of 'mine' could be compared, though this journal records 3." in output
        )

    def test_a_legacy_entry_with_no_issue_list_is_counted_not_denied(
        self,
        tmp_path: Path,
    ) -> None:
        """The second route to the same false line: entries the trend
        cannot compare because they carry no issue list, which is the
        legacy journal shape ``_stored_issues`` exists to tolerate."""
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            "".join(
                json.dumps(
                    {
                        "event_type": SPEC_ISSUES_EVENT,
                        "project": "mine",
                        "spec_file": "spec.md",
                        "timestamp": "2026-08-20T00:00:00Z",
                        "counts": {"blocker": 1, "major": 0, "minor": 0},
                    }
                )
                + "\n"
                for _ in range(3)
            ),
            encoding="utf-8",
        )

        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")

        assert "No earlier audit of this project is recorded." not in output
        assert (
            "No earlier audit of 'mine' could be compared, though this journal records 3." in output
        )
        assert (
            "Note: 3 earlier audit(s) of 'mine' fall inside the lookback window but "
            "could not be scored" in output
        )

    def test_the_journal_reader_agrees_with_the_event_name_written(
        self,
        tmp_path: Path,
    ) -> None:
        """Round 1 of review, finding 5, and #314 item 3. There is one
        ``SPEC_ISSUES_EVENT`` now, on ``evolution`` where the journal's
        schema is defined, and the writer imports it; the second copy
        this module used to hold, and the third the journal held as a
        literal, are gone.

        The end-to-end round trip is still worth its own test, because
        one constant makes the two spellings agree by construction but
        says nothing about the row actually reaching the reader: change
        the constant and this passes, break the write path and it
        fails.
        """
        self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        runs = EvolutionJournal(EvolutionConfig.load(tmp_path)).get_spec_issue_runs("writers-room")

        assert [r["event_type"] for r in runs] == [SPEC_ISSUES_EVENT]

    def test_a_clean_audit_still_reports_the_drop(self, tmp_path: Path) -> None:
        self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [])

        assert "0 (previous run: 1, -1)" in output
        assert "1, 0 (blockers, oldest run first)" in output
        assert "Previous run raised 1 issue(s): 0 reappear verbatim, 1 do not." in output

    def test_the_window_is_the_journal_lookback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for _ in range(3):
            self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert (
            "1, 1, 1 (blockers, oldest run first; 1 older audit(s) outside the "
            "lookback window)" in output
        )

    def test_the_window_keeps_the_newest_audits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Direction, not just size. Every other window test in this
        file records audits that raise the same count, so ``[:last_n]``
        and ``[-last_n:]`` produce identical output and the whole suite
        stays green with the trend reading the OLDEST audits: measured,
        with the rule inverted, before this test existed. The counts
        differ per run here, so the trend line says which end was kept.
        """
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for count in (1, 2, 3):
            self._run(tmp_path, _blockers(count))
        output = self._run(tmp_path, _blockers(4))

        assert "2, 3, 4 (blockers, oldest run first" in output
        assert "1, 2, 4" not in output

    def test_no_report_when_the_journal_is_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._run(tmp_path, [BLOCKER_ISSUE])
        monkeypatch.setenv("KSTRL_EVOLUTION_ENABLED", "0")
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert "Spec Convergence" not in output

    def test_the_rendered_overlap_never_goes_negative(self, tmp_path: Path) -> None:
        """The end-to-end guard on the count: two current issues that
        normalize to the previous run's single issue must not print
        "2 reappear verbatim, -1 do not"."""
        self._run(tmp_path, [BLOCKER_ISSUE])
        restated = dict(BLOCKER_ISSUE)
        # A DIFFERENT id: v3.0.0 requires them unique, and the point of
        # the test is two issues that NORMALIZE to one, not two records
        # that are the same record.
        restated["id"] = "fast-undefined-restated"
        restated["severity"] = "major"
        restated["summary"] = "  What   'FAST'  MEANS is not   defined  "
        output = self._run(tmp_path, [BLOCKER_ISSUE, restated])

        assert "Previous run raised 1 issue(s): 1 reappear verbatim, 0 do not." in output
        assert "-1 do not" not in output

    def test_a_bad_evolution_config_does_not_cost_the_audit_artifact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R1.7 says the artifact is written for halt, success and
        clean-audit alike. Loading the journal config happens before
        that write, so a config that will not parse must degrade to
        "no journal", never abort the audit."""
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "many")

        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()
        assert "Evolution config unreadable" in output
        assert "Spec Convergence" not in output

    def test_malformed_toml_does_not_cost_the_audit_artifact_either(
        self,
        tmp_path: Path,
    ) -> None:
        """The other ValueError path into EvolutionConfig.load.

        Scoped to the halt path on purpose, and the reason narrowed when
        #272 landed. It used to be that a malformed kstrl.toml ALSO
        failed LinearConfig.load further down decompose, after the
        architect had been paid for; ``ks decompose`` now rejects the
        file at command entry and never reaches this function, which
        ``tests/test_config_preflight.py`` pins. What this still covers
        is the direct call: ``decompose_spec`` invoked in-process, where
        the halt raises before the Linear load and the artifact is the
        only record the operator gets.
        """
        (tmp_path / "kstrl.toml").write_text("[evolution\nenabled = true\n")

        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()
        assert "Evolution config unreadable" in output

    def test_the_artifact_is_written_before_any_journal_work(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The structural version of the two tests above, independent of
        which exceptions the config guard happens to catch.

        R1.7's artifact is the only durable record on the halt path, so
        nothing that can fail belongs upstream of it. An error the guard
        does not catch still leaves the artifact on disk.
        """
        import kstrl.evolution

        def _explode(root_dir: Path | None = None) -> None:
            raise RuntimeError("journal config exploded")

        monkeypatch.setattr(kstrl.evolution.EvolutionConfig, "load", _explode)

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
        with pytest.raises(RuntimeError, match="journal config exploded"):
            decompose_spec(
                spec_path=spec_file,
                project_name="writers-room",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(
                    _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE])
                ),
                ui=PlainUI(no_color=True, file=io.StringIO()),
                root_dir=tmp_path,
            )

        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()
