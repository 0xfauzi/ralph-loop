"""TUI surface B1: the proposal engine shared by cli and the screen."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from kstrl.proposals import (
    apply_proposal,
    existing_proposal_titles,
    list_proposals,
    mark_applied,
    parse_proposal_file,
)

CONVENTION_PROP = """# PROP-001: Always pin versions
**Type**: computational
**Target**: claude_md

Suggested change:

> Pin every dependency version in pyproject.toml.
"""

MANUAL_PROP = """# PROP-002: Bump feedforward budget
**Type**: inferential
**Target**: feedforward_config

Suggested change:

> Raise max_context_tokens to 12000.
"""

APPLIED_PROP = CONVENTION_PROP.replace("PROP-001", "PROP-003") + (
    "\n**Applied**: 2026-07-19T00:00:00Z\n"
)

CLAUDE_MD = """# CLAUDE.md

## Agent Learnings

- existing bullet

## Other Section
"""


def _write_props(tmp_path: Path) -> Path:
    proposals_dir = tmp_path / ".kstrl" / "proposals"
    proposals_dir.mkdir(parents=True)
    (proposals_dir / "prop-001.md").write_text(CONVENTION_PROP)
    (proposals_dir / "prop-002.md").write_text(MANUAL_PROP)
    (proposals_dir / "prop-003.md").write_text(APPLIED_PROP)
    return proposals_dir


class TestParsing:
    def test_parse_fields(self, tmp_path: Path) -> None:
        proposals_dir = _write_props(tmp_path)
        proposal = parse_proposal_file(proposals_dir / "prop-001.md")
        assert proposal.id == "PROP-001"
        assert proposal.title == "Always pin versions"
        assert proposal.type == "computational"
        assert proposal.target == "claude_md"
        assert proposal.convention == ("Pin every dependency version in pyproject.toml.")
        assert proposal.applied == ""
        assert proposal.is_convention

    def test_applied_stamp_and_classification(self, tmp_path: Path) -> None:
        proposals_dir = _write_props(tmp_path)
        applied = parse_proposal_file(proposals_dir / "prop-003.md")
        assert applied.applied == "2026-07-19T00:00:00Z"
        manual = parse_proposal_file(proposals_dir / "prop-002.md")
        assert not manual.is_convention
        assert manual.display_id == "PROP-002"

    def test_list_and_titles(self, tmp_path: Path) -> None:
        proposals_dir = _write_props(tmp_path)
        proposals = list_proposals(proposals_dir)
        assert [p.id for p in proposals] == ["PROP-001", "PROP-002", "PROP-003"]
        assert existing_proposal_titles(proposals_dir) == {
            "Always pin versions",
            "Bump feedforward budget",
        }
        assert list_proposals(tmp_path / "nope") == []


class TestApply:
    def test_applied_path_writes_bullet_and_stamp(self, tmp_path: Path) -> None:
        proposals_dir = _write_props(tmp_path)
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(CLAUDE_MD)
        proposal = parse_proposal_file(proposals_dir / "prop-001.md")

        outcome = apply_proposal(proposal, tmp_path, confirm=lambda _: True)

        assert outcome.status == "applied"
        content = claude_md.read_text()
        learnings = content.split("## Other Section")[0]
        assert (
            "- Pin every dependency version in pyproject.toml. "
            "(applied from PROP-001 by ks evolve)" in learnings
        )
        assert "**Applied**:" in proposal.path.read_text()
        assert parse_proposal_file(proposal.path).applied

    def test_declined_writes_nothing(self, tmp_path: Path) -> None:
        proposals_dir = _write_props(tmp_path)
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(CLAUDE_MD)
        proposal = parse_proposal_file(proposals_dir / "prop-001.md")

        outcome = apply_proposal(proposal, tmp_path, confirm=lambda _: False)

        assert outcome.status == "declined"
        assert claude_md.read_text() == CLAUDE_MD
        assert "**Applied**:" not in proposal.path.read_text()

    def test_manual_and_already_applied(self, tmp_path: Path) -> None:
        proposals_dir = _write_props(tmp_path)
        (tmp_path / "CLAUDE.md").write_text(CLAUDE_MD)
        manual = parse_proposal_file(proposals_dir / "prop-002.md")
        assert (
            apply_proposal(
                manual,
                tmp_path,
                confirm=lambda _: True,
            ).status
            == "manual_required"
        )
        applied = parse_proposal_file(proposals_dir / "prop-003.md")
        assert (
            apply_proposal(
                applied,
                tmp_path,
                confirm=lambda _: True,
            ).status
            == "already_applied"
        )

    def test_missing_learnings_section_is_an_error(
        self,
        tmp_path: Path,
    ) -> None:
        proposals_dir = _write_props(tmp_path)
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md\n\nno section\n")
        proposal = parse_proposal_file(proposals_dir / "prop-001.md")
        outcome = apply_proposal(proposal, tmp_path, confirm=lambda _: True)
        assert outcome.status == "error"
        assert "**Applied**:" not in proposal.path.read_text()

    def test_stamp_failure_is_reported_and_retry_is_idempotent(
        self,
        tmp_path: Path,
    ) -> None:
        proposals_dir = _write_props(tmp_path)
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(CLAUDE_MD)
        proposal = parse_proposal_file(proposals_dir / "prop-001.md")

        with patch("kstrl.proposals.mark_applied", side_effect=OSError("full")):
            outcome = apply_proposal(
                proposal,
                tmp_path,
                confirm=lambda _: True,
            )

        assert outcome.status == "error"
        assert "Retrying is safe" in outcome.message
        retry = apply_proposal(proposal, tmp_path, confirm=lambda _: True)
        assert retry.status == "applied"
        assert claude_md.read_text().count("applied from PROP-001") == 1

    def test_claude_write_failure_is_an_error(self, tmp_path: Path) -> None:
        proposals_dir = _write_props(tmp_path)
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(CLAUDE_MD)
        proposal = parse_proposal_file(proposals_dir / "prop-001.md")

        with patch.object(Path, "write_text", side_effect=OSError("read-only")):
            outcome = apply_proposal(
                proposal,
                tmp_path,
                confirm=lambda _: True,
            )

        assert outcome.status == "error"
        assert "**Applied**:" not in proposal.path.read_text()

    def test_mark_applied_explicit_timestamp(self, tmp_path: Path) -> None:
        proposals_dir = _write_props(tmp_path)
        path = proposals_dir / "prop-001.md"
        stamp = mark_applied(path, "2026-07-20T12:00:00Z")
        assert stamp == "2026-07-20T12:00:00Z"
        assert parse_proposal_file(path).applied == "2026-07-20T12:00:00Z"


class TestTheAppliedStampGoesThroughAppendio:
    """#352: the stamp is a routed append, not a hand-rolled one.

    Its leading newline is a Markdown paragraph break; it is not a tear
    repair, and the append-open guard's deleted ``PADS_ITSELF`` reason
    could not tell the two apart. What the routing has to preserve is
    the bytes on an intact file, since a proposal file is read back by
    ``parse_proposal_file`` and shown to a person.
    """

    def test_the_bytes_on_an_intact_file_are_unchanged(self, tmp_path: Path) -> None:
        """Byte-for-byte the same append the hand-rolled open made."""
        path = tmp_path / "prop-001.md"
        path.write_text(CONVENTION_PROP, encoding="utf-8")

        mark_applied(path, "2026-07-20T12:00:00Z")

        assert path.read_text(encoding="utf-8") == (
            CONVENTION_PROP + "\n**Applied**: 2026-07-20T12:00:00Z\n"
        )

    def test_a_tail_with_no_newline_keeps_its_own_line(self, tmp_path: Path) -> None:
        """The pad is the helper's now, and it lands before the stamp.

        A proposal file whose last write was interrupted ends mid-line.
        The stamp must not join that line, or ``parse_proposal_file``
        sees neither the fragment nor the ``**Applied**`` field and the
        proposal reads as unapplied for ever.
        """
        path = tmp_path / "prop-001.md"
        path.write_text(CONVENTION_PROP.rstrip("\n"), encoding="utf-8")

        mark_applied(path, "2026-07-20T12:00:00Z")

        assert parse_proposal_file(path).applied == "2026-07-20T12:00:00Z"
        assert path.read_text(encoding="utf-8").splitlines()[-1] == (
            "**Applied**: 2026-07-20T12:00:00Z"
        )
