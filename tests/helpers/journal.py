"""The interrupted journal write, spelled once, and the records to test it with.

Three test files assert about the same crash from different sides:
``test_decompose.py`` that a torn tail does not cost the convergence
note (the read side), ``test_journal_torn_tail.py`` that it does not
cost the entry written after it (the write side, #312), and
``test_journal_write_boundary.py`` that the repair is one descriptor
and one write. The first two used to carry their own copy of the
fragment, so the claim that they pin the same interrupted write was
prose. It is an import now.

Two kinds of thing live here, and the reason differs. The CRASH SHAPES
(``TORN_FRAGMENT``, ``DANGLING_UTF8``, ``tear``, ``lose_the_newline``,
``terminate``) are here because a crash spelled two ways in two files
is two different claims; that is the rule, and it holds even for the
ones with a single caller today. The RECORD BUILDERS and READERS
(``audit``, ``component_result``, ``journal_at``, ``audits_in``,
``repair_rows_in``) are here because the boundary file needs the same
entries as the cost file; ``component_result``, ``audits_in`` and
``repair_rows_in`` have one caller each at the moment, which is the
weakest case for this module and worth revisiting if it stays that way.

``audits_in`` takes the journal rather than its path and reads through
``EvolutionJournal.get_spec_audits``, so a test asserting on what the
journal reads back is not asserting on a second copy of that reader's
selection rule (#337). ``repair_rows_in`` still takes a path, because
no production reader returns repair rows to route it through:
``get_repair_count`` returns an int.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kstrl.evolution import (
    JOURNAL_REPAIR_EVENT,
    SPEC_ISSUES_EVENT,
    EvolutionConfig,
    EvolutionJournal,
)
from kstrl.observability import read_progress_events

#: A partial JSONL line: valid JSON up to the point the process died,
#: with no closing brace and no newline. Short enough to read, and it is
#: the shape a crash actually leaves.
TORN_FRAGMENT = '{"event_type": "spec_iss'

#: Bytes that stop mid-utf-8-sequence: the last byte of "café" removed,
#: leaving a dangling 0xc3 that no decoder will accept. kstrl's own
#: writer emits pure ASCII (``json.dumps`` escapes), so bytes like these
#: reach a journal the way an operator's editor or a foreign writer puts
#: them there.
DANGLING_UTF8 = '{"project": "café'.encode()[:-1]


def tear(path: Path, fragment: str = TORN_FRAGMENT) -> None:
    """Leave ``path`` mid-line the way a crash does: no trailing newline."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(fragment)


def lose_the_newline(path: Path) -> None:
    """The other crash: a COMPLETE record whose terminator never landed.

    Costlier than a torn fragment, which is why it is spelled here
    rather than as a one-liner in whichever test needs it: appending
    onto this destroys a whole readable record as well as the new one,
    so it is the case the repair RECOVERS.
    """
    path.write_bytes(path.read_bytes()[:-1])


def terminate(path: Path) -> None:
    """The third shape: a repair that landed its newline and nothing else.

    Residual 4 of ``EvolutionJournal.append_entries``. The fragment is
    isolated, so nothing is at risk, and the file now ends in a newline,
    so the next append writes no repair row and the incident is
    invisible to ``get_repair_count``.
    """
    path.write_bytes(path.read_bytes() + b"\n")


def audit(project: Any, spec_file: str | None = None) -> dict[str, Any]:
    """One recorded spec audit.

    ``project`` is typed ``Any`` because a hand-edited or foreign
    journal can carry a null or a number there, and the readers promise
    to tolerate it; ``spec_file`` defaults to one named after the
    project and is passed explicitly when a test needs the rows told
    apart.
    """
    return {
        "timestamp": "2026-08-20T00:00:00Z",
        "project": project,
        "event_type": SPEC_ISSUES_EVENT,
        "spec_file": f"{project}.md" if spec_file is None else spec_file,
    }


def component_result(run_id: str, component_id: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "timestamp": "2026-08-20T00:00:00Z",
        "run_id": run_id,
        "project": "p",
        "component_id": component_id,
        "event_type": "component_result",
        "failure_signatures": ["tests:assertion"],
        "findings_summary": {"by_category": {"scope_creep": 1}},
        "knowledge_utilization": {"measured": True, "injected": 3, "referenced": 2},
    }


def journal_at(tmp_path: Path) -> EvolutionJournal:
    return EvolutionJournal(EvolutionConfig.load(tmp_path))


def audits_in(journal: EvolutionJournal) -> list[str]:
    """The projects of every spec audit the journal reads back, in order."""
    return [str(entry.get("project")) for entry in journal.get_spec_audits()]


def repair_rows_in(path: Path) -> list[dict[str, Any]]:
    return [e for e in read_progress_events(path) if e.get("event_type") == JOURNAL_REPAIR_EVENT]
