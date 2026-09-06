"""#247: two site inventories over ``kstrl/``.

The record of which phases produced a reading is only as good as its
coverage. Two things can silently un-fix this: a skippable phase with no
recording site, and a third writer of the retry context that does not
merge the record in. Neither shows up as a failing behaviour test,
because both are ABSENCES.

Both censuses FLAG rather than CLEAR, per the guard-direction rule in
CLAUDE.md: over-matching costs a false positive somebody reads, while a
clearing guard that over-matches deletes the mechanism. Neither proves
the merge is correct - the behaviour tests in ``tests/test_pipeline.py``
do that, one per writer, and this file's docstrings name them so the
scope cannot be misread.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kstrl.context import SKIPPABLE_PHASES
from tests.helpers.astwalk import (
    assert_census,
    folded_str,
    label,
    leaf_name,
    package_sources,
)

#: A call recording a phase reading, with a phase argument to key on.
_RECORDING_CALL = """
self._note_phase_reading(comp, "review", review.produced_a_reading)
"""

#: A write to the retry context, in the shape both writers use.
_CONTEXT_WRITE = """
self.component_contexts[comp.id] = ctx.to_json()
"""


def _records_a_reading(node: ast.AST) -> bool:
    """Every call of the recorder, whatever shape its arguments take.

    Deliberately not conditioned on arity or on the phase being
    readable. A conjunct like ``len(node.args) >= 2`` would make a site
    spelled ``_note_phase_reading(comp, phase="review", ...)`` invisible
    and leave the census green, which is the second-site case the tests
    below exist to catch, failing in the skip direction.
    """
    return isinstance(node, ast.Call) and leaf_name(node.func) == "_note_phase_reading"


def _writes_the_retry_context(node: ast.AST) -> bool:
    """A statement that installs a context under a component's key.

    Three shapes, because the guard FLAGS: a subscript assignment, and
    the ``update``/``setdefault`` calls that do the same job without
    one. Widening a flagging guard costs a false positive somebody
    reads; leaving a shape out costs a writer that never merges.

    Disclosed miss: a write through a local alias
    (``m = self.component_contexts; m[cid] = ...``) is invisible, because
    resolving that binding needs a parent map the top-down walk in
    ``tests/helpers/astwalk`` does not keep.
    """
    if isinstance(node, ast.Assign):
        return any(
            isinstance(target, ast.Subscript) and leaf_name(target.value) == "component_contexts"
            for target in node.targets
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr in {"update", "setdefault"} and (
            leaf_name(node.func.value) == "component_contexts"
        )
    return False


def _phase_argument(source_file: Path, node: ast.AST) -> str:
    """Key a recording site by the phase it names.

    A phase this cannot read is its own ``<unfolded>`` row rather than
    an absence, so a site the walk cannot key fails the inventory
    instead of quietly not counting. That covers the positional spelling
    the two shipped sites use, the keyword spelling, and a call whose
    arguments are a splat.
    """
    assert isinstance(node, ast.Call)
    del source_file
    named = [kw.value for kw in node.keywords if kw.arg == "phase"]
    written = named or list(node.args[1:2])
    return (folded_str(written[0]) if written else None) or "<unfolded>"


def test_every_skippable_phase_has_exactly_one_recording_site() -> None:
    """One ``_note_phase_reading`` call per skippable phase, and none
    for any other phase.

    The expectation is DERIVED from ``SKIPPABLE_PHASES`` rather than
    written out, so widening that set without adding a recording site
    fails here: the new phase's entries would then never be retired by
    an observed pass, which is the silent half of the defect. A second
    site for a phase that has one fails too, because two sites is how
    one of them ends up on a path that did not measure anything.

    A phase OUTSIDE the set fails as an unexpected row. That is
    deliberate: the rank rule already infers those phases ran, so a
    record for one is a second, overlapping source of truth.
    """
    assert_census(
        sources=package_sources(),
        sees=_records_a_reading,
        key=_phase_argument,
        expected={phase: 1 for phase in sorted(SKIPPABLE_PHASES)},
        control=_RECORDING_CALL,
        message=(
            "the phase-reading recording sites no longer match "
            "SKIPPABLE_PHASES. A phase in that set with no site is a "
            "finding that an observed pass can never retire; a phase "
            "outside it with a site duplicates the rank rule."
        ),
    )


def test_the_retry_context_has_exactly_two_writers_and_both_are_in_the_pipeline() -> None:
    """The retry context is written twice, both times in ``pipeline.py``.

    Before #247 the two writers were ``pipeline.retry_or_fail`` and the
    contract-breaker reset in ``factory.py``, and only one of them could
    merge the phase readings. Moving the contract write into
    ``ComponentPipeline.record_contract_failure`` puts both behind
    ``_merge_phase_readings``.

    WHAT THIS DOES AND DOES NOT SAY. It flags a third writer in any of
    the shapes ``_writes_the_retry_context`` sees, and it flags either
    of the two leaving ``pipeline.py``. It does not prove the two merge:
    a writer could be added inside ``pipeline.py`` that ignores the
    record and this stays green. Nor does it see a write through a local
    alias, which that predicate records as a disclosed miss. The proof
    that each writer merges is behavioural and lives one per writer -
    ``TestPhaseReadingsRetireSkippableFindings::test_a_review_that_ran_
    and_passed_retires_its_own_earlier_finding`` for ``retry_or_fail``
    and ``::test_the_contract_gate_retires_a_review_that_passed`` for
    ``record_contract_failure``. Saying so explicitly is the point: a
    guard whose scope is misread is how a clearing guard gets written by
    accident.
    """
    assert_census(
        sources=package_sources(),
        sees=_writes_the_retry_context,
        key=lambda source_file, _node: label(source_file),
        expected={"pipeline.py": 2},
        control=_CONTEXT_WRITE,
        message=(
            "the retry context gained or lost a writer. Every writer "
            "must merge the attempt's phase readings through "
            "ComponentPipeline._merge_phase_readings, or a failure at "
            "that gate re-raises a finding an earlier phase cleared."
        ),
    )
