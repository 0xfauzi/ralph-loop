"""Harness-proposal files: parsing and the convention apply path.

Extracted from cli's evolve helpers (TUI surface B1) so the plain
``ks evolve --apply`` flow and the evolve screen share one engine.
The proposal file format is what ``evolution.save_proposals`` writes:
a ``# PROP-NNN: title`` heading, ``**Type**``/``**Target**`` fields,
the suggested change as a ``> `` blockquote, and an ``**Applied**``
stamp once applied.

Only convention-type proposals (computational, target claude_md) have
an automated apply; everything else honestly requires manual review -
``apply_proposal`` never fakes an "applied" claim (R6.3).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kstrl.appendio import append_records

PROPOSAL_TITLE_RE = re.compile(r"^# (PROP-\d+): (.+)$")
PROPOSAL_FIELD_RE = re.compile(r"^\*\*(Type|Target)\*\*: (.+)$")
PROPOSAL_APPLIED_RE = re.compile(r"^\*\*Applied\*\*: (.+)$")


@dataclass(frozen=True)
class Proposal:
    path: Path
    id: str
    title: str
    type: str
    target: str
    convention: str  # blockquote body of the suggested change; "" = none
    applied: str  # timestamp, "" = not applied

    @property
    def display_id(self) -> str:
        return self.id or self.path.stem.upper()

    @property
    def is_convention(self) -> bool:
        """Whether the automated apply path covers this proposal."""
        return self.type == "computational" and self.target == "claude_md" and bool(self.convention)


@dataclass(frozen=True)
class ApplyOutcome:
    status: str  # applied | declined | already_applied | manual_required | error
    message: str = ""


def existing_proposal_titles(proposals_dir: Path) -> set[str]:
    """Titles of every proposal already saved to disk."""
    titles: set[str] = set()
    if not proposals_dir.is_dir():
        return titles
    for path in sorted(proposals_dir.glob("prop-*.md")):
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, ValueError, IndexError):
            continue
        m = PROPOSAL_TITLE_RE.match(first_line)
        if m:
            titles.add(m.group(2))
    return titles


def parse_proposal_file(path: Path) -> Proposal:
    """Parse the structured fields save_proposals writes."""
    parsed = {
        "id": "",
        "title": "",
        "type": "",
        "target": "",
        "applied": "",
    }
    convention_lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PROPOSAL_TITLE_RE.match(line)
        if m and not parsed["id"]:
            parsed["id"], parsed["title"] = m.group(1), m.group(2)
            continue
        m = PROPOSAL_FIELD_RE.match(line)
        if m:
            parsed[m.group(1).lower()] = m.group(2).strip()
            continue
        m = PROPOSAL_APPLIED_RE.match(line)
        if m:
            parsed["applied"] = m.group(1).strip()
            continue
        if line.startswith("> "):
            convention_lines.append(line[2:].strip())
    return Proposal(
        path=path,
        id=parsed["id"],
        title=parsed["title"],
        type=parsed["type"],
        target=parsed["target"],
        convention=" ".join(convention_lines).strip(),
        applied=parsed["applied"],
    )


def list_proposals(proposals_dir: Path) -> list[Proposal]:
    if not proposals_dir.is_dir():
        return []
    proposals: list[Proposal] = []
    for path in sorted(proposals_dir.glob("prop-*.md")):
        try:
            proposals.append(parse_proposal_file(path))
        except (OSError, ValueError):
            # ValueError beside OSError because UnicodeDecodeError is
            # one: measured, a single non-utf-8 byte in a prop-*.md
            # raised out of EvolveScreen.on_mount, one line BEFORE the
            # #289 banner guard (CLAUDE.md, encoding is two-sided).
            continue
    return proposals


def append_to_agent_learnings(
    claude_md: Path,
    proposal_id: str,
    convention: str,
) -> bool:
    """Append one convention bullet to the end of the "## Agent
    Learnings" section of the project CLAUDE.md. Returns False (no
    write) when the file or the section is missing - the caller then
    falls back to honest manual instructions instead of guessing a
    location."""
    try:
        content = claude_md.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    marker = "## Agent Learnings"
    idx = content.find(marker)
    if idx == -1:
        return False
    # End of the section = next level-2 header after it, else EOF.
    next_header = content.find("\n## ", idx + len(marker))
    insert_at = len(content) if next_header == -1 else next_header
    application_marker = f"(applied from {proposal_id} by ks evolve)"
    if application_marker in content[idx:insert_at]:
        return True
    entry = f"- {convention} (applied from {proposal_id} by ks evolve)\n"
    head = content[:insert_at]
    if not head.endswith("\n"):
        head += "\n"
    try:
        claude_md.write_text(head + entry + content[insert_at:], encoding="utf-8")
    except OSError:
        return False
    return True


def mark_applied(path: Path, when: str | None = None) -> str:
    """Stamp the proposal file as applied; returns the timestamp.

    Through ``appendio`` rather than its own ``open(path, "a")``. The
    leading newline in the payload is the MARKDOWN separator - a stamp
    written directly under the last line of a paragraph joins that
    paragraph - and it is not a tear repair, even though it happened to
    act as one. Those are two different claims and this file used to
    make them with one character: #352 measured the append-open guard's
    ``PADS_ITSELF`` reason and found it could not fail for the reason it
    named, because the check could not tell this newline from the
    terminator at the other end of the same f-string. A reason that
    cannot fail is not a control, so the site is routed and the reason
    is gone.

    Byte-identical to what it replaced on an intact file, which is what
    ``tests/test_proposals.py`` pins. On a file whose tail lost its
    newline the helper's pad lands first, so the fragment ends up on a
    line of its own and the stamp on the next: one newline more than
    before, in a Markdown file where a blank line is a paragraph break.

    THE ``"a+b"`` CONSEQUENCE, since ``appendio`` opens for update and
    not for append alone: a proposal file this process can write but not
    read can no longer be stamped, and the open RAISES rather than the
    stamp going missing. Two callers and they differ, which is checked
    rather than assumed. :func:`apply_proposal` catches ``OSError`` and
    says the learning was appended but the proposal could not be
    stamped, and that retrying is safe. ``cli``'s own loop does not, so
    an unreadable proposal file ends ``ks proposals apply`` with a
    traceback where 568bca4 stamped it. Reaching that needs a deliberate
    chmod on a file kstrl created itself under ``.kstrl/proposals`` at
    the umask default, with one writer. The alternative is the fail-OPEN
    shape #327 round 1 found, where an unreadable file was reported as
    "not torn" and appended to blind.
    """
    applied_at = when or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_records(path, f"\n**Applied**: {applied_at}\n", repair="")
    return applied_at


def apply_proposal(
    proposal: Proposal,
    root_dir: Path,
    *,
    confirm: Callable[[str], bool],
) -> ApplyOutcome:
    """Apply one proposal through the confirm seam.

    ``confirm`` receives the prompt text and answers yes/no - the CLI
    wraps click.confirm (piped-stdin semantics preserved), the TUI's
    modal has ALREADY confirmed and passes ``lambda _: True``.
    """
    pid = proposal.display_id
    claude_md = root_dir / "CLAUDE.md"
    if proposal.applied:
        return ApplyOutcome(
            "already_applied",
            f"{pid} already applied at {proposal.applied}; skipping.",
        )
    if not proposal.is_convention:
        return ApplyOutcome(
            "manual_required",
            f"Automated apply only covers convention-type proposals "
            f"(target claude_md). This one targets "
            f"'{proposal.target or 'unknown'}': review {proposal.path} "
            f"and apply it manually.",
        )
    if not confirm(f"Append this convention to {claude_md}?"):
        return ApplyOutcome("declined", f"{pid} not applied (declined).")
    if not append_to_agent_learnings(claude_md, pid, proposal.convention):
        return ApplyOutcome(
            "error",
            f"Could not apply {pid}: {claude_md} is missing or has no "
            f"'## Agent Learnings' section. Add the section or apply "
            f"manually from {proposal.path}.",
        )
    try:
        mark_applied(proposal.path)
    except OSError as exc:
        return ApplyOutcome(
            "error",
            f"{pid} was appended to {claude_md}, but the proposal file "
            f"could not be marked applied: {exc}. Retrying is safe.",
        )
    return ApplyOutcome("applied", f"{pid} appended to {claude_md}.")
