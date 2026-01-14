"""Tests for PRD module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralph_py.prd import PRD, UserStory


class TestPRDValidateSchema:
    """Tests for PRD.validate_schema."""

    def test_valid_empty_stories(self) -> None:
        data = {"branchName": "main", "userStories": []}
        errors = PRD.validate_schema(data)
        assert errors == []

    def test_valid_with_story(self) -> None:
        data = {
            "branchName": "feature/test",
            "userStories": [
                {
                    "id": "US-001",
                    "title": "Test story",
                    "acceptanceCriteria": ["Criterion 1"],
                    "priority": 1,
                    "passes": False,
                    "notes": "",
                }
            ],
        }
        errors = PRD.validate_schema(data)
        assert errors == []

    def test_missing_branch_name(self) -> None:
        data = {"userStories": []}
        errors = PRD.validate_schema(data)
        assert any("branchName" in e for e in errors)

    def test_missing_user_stories(self) -> None:
        data = {"branchName": "main"}
        errors = PRD.validate_schema(data)
        assert any("userStories" in e for e in errors)

    def test_extra_top_level_key(self) -> None:
        data = {"branchName": "main", "userStories": [], "extra": "value"}
        errors = PRD.validate_schema(data)
        assert any("extra" in e for e in errors)

    def test_empty_branch_name(self) -> None:
        data = {"branchName": "", "userStories": []}
        errors = PRD.validate_schema(data)
        assert any("non-empty" in e for e in errors)

    def test_wrong_branch_name_type(self) -> None:
        data = {"branchName": 123, "userStories": []}
        errors = PRD.validate_schema(data)
        assert any("string" in e for e in errors)

    def test_wrong_user_stories_type(self) -> None:
        data = {"branchName": "main", "userStories": "not an array"}
        errors = PRD.validate_schema(data)
        assert any("array" in e for e in errors)

    def test_story_missing_field(self) -> None:
        data = {
            "branchName": "main",
            "userStories": [
                {
                    "id": "US-001",
                    "title": "Test",
                    # Missing acceptanceCriteria, priority, passes, notes
                }
            ],
        }
        errors = PRD.validate_schema(data)
        assert any("missing keys" in e for e in errors)

    def test_story_extra_field(self) -> None:
        data = {
            "branchName": "main",
            "userStories": [
                {
                    "id": "US-001",
                    "title": "Test",
                    "acceptanceCriteria": [],
                    "priority": 1,
                    "passes": False,
                    "notes": "",
                    "extra": "field",
                }
            ],
        }
        errors = PRD.validate_schema(data)
        assert any("extra" in e for e in errors)

    def test_story_wrong_type_priority(self) -> None:
        data = {
            "branchName": "main",
            "userStories": [
                {
                    "id": "US-001",
                    "title": "Test",
                    "acceptanceCriteria": [],
                    "priority": "high",  # Should be int
                    "passes": False,
                    "notes": "",
                }
            ],
        }
        errors = PRD.validate_schema(data)
        assert any("integer" in e for e in errors)

    def test_story_wrong_type_passes(self) -> None:
        data = {
            "branchName": "main",
            "userStories": [
                {
                    "id": "US-001",
                    "title": "Test",
                    "acceptanceCriteria": [],
                    "priority": 1,
                    "passes": "no",  # Should be bool
                    "notes": "",
                }
            ],
        }
        errors = PRD.validate_schema(data)
        assert any("boolean" in e for e in errors)


class TestPRDLoad:
    """Tests for PRD.load."""

    def test_load_valid(self, tmp_path: Path) -> None:
        prd_file = tmp_path / "prd.json"
        prd_file.write_text(
            json.dumps(
                {
                    "branchName": "test-branch",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test story",
                            "acceptanceCriteria": ["AC1", "AC2"],
                            "priority": 2,
                            "passes": True,
                            "notes": "Some notes",
                        }
                    ],
                }
            )
        )

        prd = PRD.load(prd_file)

        assert prd.branch_name == "test-branch"
        assert len(prd.user_stories) == 1
        assert prd.user_stories[0].id == "US-001"
        assert prd.user_stories[0].acceptance_criteria == ["AC1", "AC2"]
        assert prd.user_stories[0].passes is True

    def test_load_invalid_schema(self, tmp_path: Path) -> None:
        prd_file = tmp_path / "prd.json"
        prd_file.write_text('{"invalid": "schema"}')

        with pytest.raises(ValueError, match="Invalid PRD schema"):
            PRD.load(prd_file)


class TestPRDGetNextStory:
    """Tests for PRD.get_next_story."""

    def test_no_stories(self) -> None:
        prd = PRD(branch_name="main", user_stories=[])
        assert prd.get_next_story() is None

    def test_all_passing(self) -> None:
        prd = PRD(
            branch_name="main",
            user_stories=[
                UserStory("1", "Story 1", [], 1, True, ""),
                UserStory("2", "Story 2", [], 2, True, ""),
            ],
        )
        assert prd.get_next_story() is None

    def test_returns_highest_priority_failing(self) -> None:
        prd = PRD(
            branch_name="main",
            user_stories=[
                UserStory("1", "Story 1", [], 3, False, ""),  # Priority 3
                UserStory("2", "Story 2", [], 1, False, ""),  # Priority 1 (highest)
                UserStory("3", "Story 3", [], 2, True, ""),  # Passing
            ],
        )
        next_story = prd.get_next_story()
        assert next_story is not None
        assert next_story.id == "2"
