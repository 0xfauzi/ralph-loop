"""Every signature-shaped string in ``kstrl/``, counted by no shape at all.

``tests/test_check_name_enrolment.py`` names four producers, resolves
them and asserts the table carries what they emit. That is the layer
with the right message, and it is also the layer that keeps being holed:
each of the four was found only after a version of it claimed to have
them all, and #339 review measured eight of nine ordinary shape
mutations surviving it.

This is the other layer, borrowed whole from #336 (which landed on main
as ``tests/test_event_names_have_one_home.py``, after this branch was
cut), where widening a matcher was measured to be necessary but not
sufficient: seven more ordinary shapes walked past the widened version,
and the answer was a guard that enumerates no node types.

THE NET: every expression in the package whose folded value has the
shape of a journal signature, ``"<check>:<code>"``, wherever it sits. A
component's failure signature cannot be written by a string the package
never spells, so a fifth producer - in a container, a dispatch table, a
return value, a comprehension, a default argument, whatever nobody has
thought of - has to appear here first, whatever it does with the string
afterwards.

WHY THIS IS AN INVENTORY AND NOT AN ASSERTION. The net cannot tell a
journal signature from any other colon-joined pair, and the package has
several of those: ``findings`` spells ``"cwe:..."`` and ``"owasp:..."``,
``linear`` spells issue keys, ``pipeline`` spells inbox dedupe keys like
``"halted:..."``. Requiring every head to be enrolled would put those in
``_CATEGORY_BY_CHECK``, which is worse than the hole - a table that
carries names the journal never files stops being readable as the list
of names it does. So the net pins WHERE the package spells such strings,
and the diff that adds a row is where somebody says which kind it is.

WHAT THE NET CANNOT SEE is one thing and it is the same thing the #336
guard discloses: a string the interpreter has to build. ``":".join(...)``,
``%``-formatting, ``"".format(...)``, a run-time lookup. Folding answers
nothing for those. It DOES see an f-string whose tail is dynamic, since
the check name is everything before the first colon.
"""

from __future__ import annotations

import ast
from functools import lru_cache

from tests.helpers.astfold import (
    HOLE,
    fold,
    folded_nodes,
    module_level_strings,
    parsed_modules,
    signature_head,
)

#: ``(module, check part)`` for every signature-shaped string in the
#: package. A SET rather than the per-module counts #336 pins, because
#: the question here is which NAMES a module can write, not how many
#: times it writes them: a second ``"pr:..."`` in ``pipeline`` changes
#: nothing about enrolment, and failing on it would make this a thing to
#: be regenerated rather than read.
#:
#: THE RULE FOR A NEW ROW, since eight of these heads are also pinned in
#: ``EXPECTED_CATEGORIES`` and #339 review asked what decides that.
#: Every signature-shaped string goes here, enrolled or not. The
#: enrolled eight are NOT derived from ``_CATEGORY_BY_CHECK``, on
#: purpose: derivation would answer "which module spells this" with
#: "whichever one does", and this list is what turns a ``pr:`` literal
#: MOVING to another module, or being deleted, into a red test. The
#: three-place edit for a genuinely new check name is the same audit
#: trail ``tests/test_prompt_versions.py`` asks for, not an oversight.
EXPECTED_SPELLINGS: frozenset[tuple[str, str]] = frozenset(
    {
        # --- journal signatures: these eight ARE check names, and every
        # one is enrolled in evolution._CATEGORY_BY_CHECK.
        ("kstrl/factory.py", "contract"),
        ("kstrl/factory.py", "scope_unreadable"),
        ("kstrl/pipeline.py", "aborted"),
        ("kstrl/pipeline.py", "diff"),
        ("kstrl/pipeline.py", "engineer"),
        ("kstrl/pipeline.py", "pr"),
        ("kstrl/pipeline.py", "review"),
        ("kstrl/pipeline.py", "token_budget"),
        # --- other vocabularies that share the shape. None of these
        # reaches component_failure_signatures, and enrolling any of
        # them would make the category table unreadable as the list of
        # names the journal files.
        # Divergence's own reasons for a trip.
        ("kstrl/divergence.py", "concern"),
        ("kstrl/divergence.py", "criterion"),
        # An autonomy demotion record, not a component failure. It moved
        # out of factory.py in #232, when the demotion-apply block became
        # autonomy.apply_demotion; this row moving is the guard doing the
        # job the comment above claims for it.
        ("kstrl/autonomy.py", "demotion"),
        # #232's two new demotion triggers, both inbox dedupe keys in the
        # same family as pipeline's below. "calibration:<comparison id>"
        # is also the run id the compare command demotes under, since a
        # comparison is not a factory run and has none of its own. The
        # comparison id is both baselines' timestamps, or a digest of the
        # failure lines when either file carries no timestamp.
        ("kstrl/calibration_ladder.py", "calibration"),
        ("kstrl/factory.py", "health"),
        # Finding metadata: CWE and OWASP ids, and the keys a finding
        # serialises its own fields under.
        ("kstrl/findings.py", "adequacy"),
        ("kstrl/findings.py", "attempt"),
        ("kstrl/findings.py", "category"),
        ("kstrl/findings.py", "cwe"),
        ("kstrl/findings.py", "model"),
        ("kstrl/findings.py", "owasp"),
        ("kstrl/findings.py", "phase"),
        ("kstrl/findings.py", "policy"),
        # A GitHub label namespace.
        ("kstrl/intake_github.py", "kstrl"),
        # Inbox dedupe keys and the budget label, all keyed the same way
        # and none of them a failure signature.
        ("kstrl/pipeline.py", "adequacy"),
        ("kstrl/pipeline.py", "budget"),
        ("kstrl/pipeline.py", "halted"),
        ("kstrl/pipeline.py", "merge"),
        ("kstrl/pipeline.py", "policy"),
    }
)


def signature_heads(tree: ast.AST, own: dict[str, str]) -> set[str]:
    """The check part of every folded signature in one tree.

    THE NET ITSELF, one tree at a time, so
    ``tests/test_check_name_shapes.py`` can hold it to the shapes the
    other layer misses rather than re-implementing six lines that could
    then be widened apart from these.
    """
    return {
        head for folded in folded_nodes(tree, own) if (head := signature_head(folded)) is not None
    }


@lru_cache(maxsize=1)
def signature_spellings() -> frozenset[tuple[str, str]]:
    """``(module, check part)`` for every folded signature in ``kstrl/``.

    Cached for the session, as ``parsed_modules`` and ``_census`` are.
    Measured on #339 review: 93 ms a pass and two tests call it, so the
    second pass was pure duplicate.
    """
    return frozenset(
        (rel, head)
        for rel, tree in parsed_modules()
        for head in signature_heads(tree, module_level_strings()[rel])
    )


class TestTheSignatureSpellingsAreInventoried:
    def test_the_package_spells_exactly_these(self) -> None:
        """A new row is not forbidden, it is the point: the diff that
        adds one is where somebody says whether a new signature-shaped
        string reaches the journal. If it does, enrol its check part in
        ``evolution._CATEGORY_BY_CHECK`` as well."""
        measured = signature_spellings()
        assert measured == EXPECTED_SPELLINGS, (
            f"new spellings: {sorted(measured - EXPECTED_SPELLINGS)} (if any "
            f"of these reaches component_failure_signatures, enrol its check "
            f"part in evolution._CATEGORY_BY_CHECK too). Gone: "
            f"{sorted(EXPECTED_SPELLINGS - measured)}."
        )

    def test_the_net_sees_the_producers_the_other_layer_names(self) -> None:
        """Anti-vacuity, and the specific claim this layer makes: it
        finds the fourth producer without knowing that an assignment is
        a producer, and the conditional in ``_setpoint_failure`` without
        knowing what an ``IfExp`` is.

        This guards the EXPECTATION, not the net: the equality above
        already implies every membership below, so nothing here can fail
        while that passes. What it stops is a future author deleting
        these four from the pin to make a red test go away, which is the
        one edit that would silently un-cover all four producers."""
        measured = signature_spellings()
        assert ("kstrl/factory.py", "contract") in measured, "a direct assignment"
        assert ("kstrl/pipeline.py", "engineer") in measured, "a direct assignment"
        assert ("kstrl/pipeline.py", "review") in measured, "a conditional expression"
        assert ("kstrl/factory.py", "scope_unreadable") in measured, "an f-string constant"

    def test_a_dynamic_tail_does_not_hide_the_check_name(self) -> None:
        """``f"contract:tier_{n}"`` is the fourth producer's actual
        shape, and the version of the walk this replaces folded it to
        nothing because one piece was unknown."""
        node = ast.parse('f"contract:tier_{n}"', mode="eval").body
        folded = fold(node, {})
        assert folded is not None
        assert signature_head(folded) == "contract"

    def test_a_dynamic_check_name_is_not_guessed(self) -> None:
        """The other half. An unknown HEAD must fold to nothing here, or
        the net would invent a check name; the enrolment layer resolves
        that case from the call sites instead."""
        node = ast.parse('f"{phase}:coverage-unverified"', mode="eval").body
        folded = fold(node, {})
        assert folded is not None
        assert signature_head(folded) is None

    def test_a_concatenation_folds_like_a_literal(self) -> None:
        """The ``+`` branch of the fold has no site in ``kstrl/`` today -
        125 concatenations, none of them a signature - so it is measured
        here or it is not measured at all. A concatenation is exactly
        what somebody writes to get past a string search (#327 F9)."""
        node = ast.parse('"review" + ":setpoint_disagreement"', mode="eval").body
        folded = fold(node, {})
        assert folded is not None
        assert signature_head(folded) == "review"

    def test_prose_is_not_a_signature(self) -> None:
        """A guard that fires on every colon in the package is a guard
        that gets silenced. Three real shapes from this tree."""
        for text in ("Note: the thing failed", "https://example.test/x", "Error:"):
            node = ast.parse(repr(text), mode="eval").body
            folded = fold(node, {})
            assert folded is not None
            assert signature_head(folded) is None, text


def test_no_literal_in_the_package_contains_the_hole_marker() -> None:
    """The marker's whole job is to be distinguishable from text, and
    #339 review measured the first attempt at it wrong: a single NUL
    already appears in three ``kstrl/`` literals, so the docstring
    claiming otherwise was a claim the package contradicted. Lives here
    rather than next to the constant because it needs the package walk,
    and it is asserted rather than believed because that is the standard
    this pair of guards exists to hold."""
    colliding = [
        (rel, node.lineno)
        for rel, tree in parsed_modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and HOLE in node.value
    ]
    assert not colliding, (
        f"a literal in kstrl/ now contains the hole marker, so a folded "
        f"string cannot be told from one with an undecidable piece: "
        f"{colliding}. Lengthen astfold.HOLE."
    )
