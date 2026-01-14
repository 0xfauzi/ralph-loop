"""Tests for guards module."""

from __future__ import annotations

from ralph_py.guards import check_violations, path_is_allowed


class TestPathIsAllowed:
    """Tests for path_is_allowed."""

    def test_exact_match(self) -> None:
        assert path_is_allowed("foo/bar.txt", ["foo/bar.txt"]) is True

    def test_exact_match_negative(self) -> None:
        assert path_is_allowed("foo/baz.txt", ["foo/bar.txt"]) is False

    def test_directory_prefix_match(self) -> None:
        assert path_is_allowed("foo/bar/baz.txt", ["foo/"]) is True

    def test_directory_prefix_negative(self) -> None:
        assert path_is_allowed("other/file.txt", ["foo/"]) is False

    def test_multiple_allowed_paths(self) -> None:
        allowed = ["foo/bar.txt", "src/"]
        assert path_is_allowed("foo/bar.txt", allowed) is True
        assert path_is_allowed("src/main.py", allowed) is True
        assert path_is_allowed("other.txt", allowed) is False

    def test_empty_allowed_paths(self) -> None:
        assert path_is_allowed("anything.txt", []) is False

    def test_nested_directory(self) -> None:
        assert path_is_allowed("scripts/ralph/codebase_map.md", ["scripts/ralph/"]) is True

    def test_exact_directory_match(self) -> None:
        # "foo/" should match path "foo" as a directory
        assert path_is_allowed("foo", ["foo/"]) is True


class TestCheckViolations:
    """Tests for check_violations."""

    def test_no_violations(self) -> None:
        changed = {"foo/bar.txt", "src/main.py"}
        allowed = ["foo/", "src/"]
        assert check_violations(changed, allowed) == []

    def test_with_violations(self) -> None:
        changed = {"foo/bar.txt", "other/file.py"}
        allowed = ["foo/"]
        violations = check_violations(changed, allowed)
        assert violations == ["other/file.py"]

    def test_all_violations(self) -> None:
        changed = {"a.txt", "b.txt"}
        allowed = ["c/"]
        violations = check_violations(changed, allowed)
        assert sorted(violations) == ["a.txt", "b.txt"]

    def test_empty_allowed_no_violations(self) -> None:
        # Empty allowed_paths means enforcement is disabled
        changed = {"any/file.txt"}
        violations = check_violations(changed, [])
        assert violations == []

    def test_empty_changed(self) -> None:
        violations = check_violations(set(), ["foo/"])
        assert violations == []

    def test_sorted_output(self) -> None:
        changed = {"z.txt", "a.txt", "m.txt"}
        allowed = ["other/"]
        violations = check_violations(changed, allowed)
        assert violations == ["a.txt", "m.txt", "z.txt"]
