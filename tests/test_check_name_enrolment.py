"""Every check and signature prefix is enrolled in the category map.

``evolution.category_for_check`` maps a name onto a
``FailurePattern.category`` and falls through to ``"iteration"`` for
anything the table does not carry, which files a mechanical verification
gate in the journal under the engineer loop. Enrolment in
``_CATEGORY_BY_CHECK`` was a convention with no mechanism, and measured
during #294 the convention did not hold.

This is the mechanism, in the shape the repo already uses three times
(``tests/test_prompt_versions.py``, ``tests/test_atomicio.py``,
``tests/test_process_scoping.py``): AST-walk ``kstrl/`` for every name
that can reach ``category_for_check`` and fail on one the table does not
carry.

FOUR PRODUCERS, each found after a version of this file claimed to have
them all. A component's failure signatures are whatever ends up in
``pipeline.component_failure_signatures``, and that mapping is written
from four places:

- a ``CheckResult`` name, which ``evolution.signatures_from_verification``
  turns into ``"<name>:<code>"``.
- a ``signatures=["<check>:<code>"]`` argument, handed straight to
  ``PhaseFailure`` and never through a ``CheckResult``.
- the ``phase`` of a failure recorded with NEITHER of those, because
  ``pipeline._record_failure_signatures`` falls back to
  ``signature_for_error(phase or "unknown", error)``. Censused at that
  funnel AND at the two entry points that call it, ``fail`` and
  ``retry_or_fail``, because the phase is a literal at their call sites
  and a parameter inside them. #339 review is why the funnel is in that
  list: keying on the two entry points alone missed ``_fail_pr_flow``,
  which calls the funnel directly, and mutating its ``"pr"`` to
  ``"bogus_flow"`` left 4737 tests green.
- a direct ``component_failure_signatures[comp] = [...]`` assignment,
  which bypasses ``_record_failure_signatures`` altogether. Four sites,
  in ``factory`` and ``pipeline``, invisible to every earlier version of
  this walk because none of them is a call.

THE RESOLUTION IS SEPARATE FROM THE DETECTION, which is the correction
#339 review made to this file. Detecting a producer site and reading the
name out of it are two jobs, and folding them into one meant a site
whose name would not resolve was DROPPED - silently, in the skip
direction #324 is about. Now a site that resolves feeds the census and a
site that does not feeds :data:`BLIND_SITES`, which is pinned. Measured
on the branch that added this: eight of nine shape mutations survived
the old walk, including ``phase="review"`` at the coverage-refusal call
site, which two reviewers found independently.

TWO LAYERS, borrowed from #336 (which landed on main as
``tests/test_event_names_have_one_home.py``, after this branch was cut),
where widening a matcher was measured to be necessary but not
sufficient: seven ordinary shapes walked past the widened version. The
numbering is #336's, so a reader following the citation finds the same
two things under the same two labels.

- LAYER 1 is ``tests/test_signature_spellings.py``: a net that
  enumerates no node types at all and pins every expression in
  ``kstrl/`` whose folded value has the shape of a journal signature,
  by module. It cannot say what a new spelling MEANS, so it is a pinned
  inventory rather than an assertion, and it is what catches a producer
  in a container nothing here has thought of.
- LAYER 2 is this file: the four producers above, named, resolved, and
  asserted against the table. It gives the right message ("add a row to
  ``_CATEGORY_BY_CHECK``") and it names the offending site.

A THIRD file holds this one from the other side.
``tests/test_signature_spellings.py`` pins what is IN the package;
``tests/test_check_name_shapes.py`` feeds the two matchers below a
snippet per shape and pins what they SAY, including the shapes they say
nothing about. #339 review is why it exists: :data:`BLIND_SITES`
inventories this walk's own resolution failures, which is a strictly
smaller set than the producer shapes that exist, so a shape the matchers
do not enumerate is not a blind site - it is silence. That is exactly
how ``_fail_pr_flow`` survived.

Both layers fold expressions with ``tests/helpers/astfold.py``, which is
where the string-folding lives so there is one copy of it rather than
two that can be widened apart.

What the walk CANNOT see, stated rather than left as a silent gap:
``evolution._classify_check`` RETURNS ``verification`` and ``unknown``
for a legacy flattened error string, so no call site anywhere carries
them. They are enrolled today, and :data:`ENROLLED_BUT_INVISIBLE` plus
:meth:`TestEveryCheckNameIsEnrolled.test_the_walk_covers_every_enrolled_name_but_these`
say so by measurement rather than by claim.

That test is the one aimed at this repo's dominant defect (#324): a
guard that goes BLIND rather than red. Every miss logged there is in the
skip direction - a refactor moves a name out of the shape the matcher
recognises, the matcher sees fewer sites and reports clean. This file
has already been bitten once, by #306, and once more by #339. So the
walk is pinned from three ends: an emitted name the table does not carry
fails, an enrolled name the walk stops seeing fails, and a producer site
whose name the walk cannot read is enumerated rather than dropped.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from functools import cache, lru_cache

from kstrl.evolution import _CATEGORY_BY_CHECK
from tests.helpers.astfold import (
    HOLE,
    Folded,
    Scope,
    called_name,
    fold,
    folded_nodes,
    module_level_strings,
    parameter_index,
    parsed_modules,
    scoped_nodes,
    signature_head,
    unambiguous_pool,
)

#: Calls whose first argument (or ``name=``) is a check name.
#: ``CheckResult`` is the type itself; ``_failed_gate_result`` is the
#: shared builder the three subprocess gates package their failures
#: through.
CHECK_NAME_CALLS = frozenset({"CheckResult", "_failed_gate_result"})

#: Calls that put a ``phase`` on the path to the fallback, and where each
#: one's ``phase`` sits among its positional arguments (the receiver not
#: counted). When no truthy ``signatures`` travels with it, that phase
#: becomes the check name.
#:
#: ``_record_failure_signatures`` is the FUNNEL - ``fail`` and
#: ``retry_or_fail`` both call it, and it is where the fallback actually
#: lives - so it is keyed here alongside the two public entry points, in
#: the shape ``tests/test_journal_one_writer.py`` uses when it keys on
#: ``append_entries`` being the only writer. #339 review is why: keying
#: on the two entry points alone missed ``_fail_pr_flow``, a third caller
#: of the funnel that reaches it directly, and mutating its phase to
#: ``"bogus_flow"`` left 4737 tests green. The two entry points stay
#: because they are where the phase is a LITERAL and can be read; the
#: funnel's own two internal callers pass a parameter through and land in
#: :data:`BLIND_SITES`, which is the point of having that ledger.
#:
#: ``PhaseFailure`` is not a call that records anything - it is the typed
#: carrier ``_route_failure`` unpacks into ``fail`` / ``retry_or_fail`` -
#: but its ``phase`` reaches the fallback by exactly the same route, and
#: ``_route_failure`` hands on ``failure.phase``, which the walk cannot
#: read. Keyed here so the ten construction sites are censused where the
#: phase is a literal. Measured: it changes neither the names nor the
#: blind sites today, because every phase it spells is one another site
#: already gives. ``tests/test_check_name_shapes.py`` is what holds it,
#: and an eleventh site with a new phase is what it is for.
#:
#: A NAME set, not a name-to-index table. The previous version of this
#: wrote the positional index of each ``phase`` by hand, and #339 review
#: measured what that bought: six of eight off-by-one mutations to those
#: four integers left every guard green, because all but one call site
#: passes ``phase`` by keyword. The index is a fact about the
#: definition, so :func:`astfold.parameter_index` reads it off the
#: ``def``, and it also keeps ``feature_cmd``'s one-argument local
#: ``fail`` out of the census BY CONSTRUCTION - that definition has no
#: ``phase`` parameter - rather than by the special case the integers
#: needed a paragraph to defend.
PHASE_FALLBACK_CALLS = frozenset(
    {
        "_record_failure_signatures",
        "fail",
        "retry_or_fail",
        "PhaseFailure",
    }
)

#: The container the fourth producer writes into directly.
#: ``pipeline.component_failure_signatures`` and the local
#: ``component_failure_signatures`` ``factory`` hands it are the same
#: mapping under two spellings, so the suffix is what is matched.
SIGNATURE_CONTAINER_SUFFIX = "failure_signatures"


#: Enrolled names the walk provably cannot see, with the reason each is
#: invisible. Anything else in the table must be reachable by the walk,
#: or the walk has gone blind and says nothing (#324).
ENROLLED_BUT_INVISIBLE = {
    # _classify_check RETURNS these for a legacy flattened error string.
    # They are never arguments, so no call site carries them.
    "verification": "returned by _classify_check, never passed to a call",
    "unknown": "returned by _classify_check, never passed to a call",
}

#: The whole of ``_CATEGORY_BY_CHECK``, pinned row by row rather than in
#: part. The table is data, so an edit to it is a behaviour change with
#: no code diff to review: it decides what the journal calls a failure
#: and, through ``INFRASTRUCTURE_CHECKS``, which runs the autonomy replay
#: counts as evidence about the factory's judgement. Pinning all of it
#: is the same audit-trail shape ``tests/test_prompt_versions.py`` uses:
#: the table and its expectation move together in one diff, or CI is red.
#: A partial mirror was tried first and gave a future author no rule for
#: whether a new row belonged in it.
EXPECTED_CATEGORIES = {
    "linter": "verification",
    "typecheck": "verification",
    "test_suite": "verification",
    "diff_scope": "verification",
    "scope_unreadable": "verification",
    "bad_patterns": "verification",
    "self_critique": "verification",
    "dead_code": "verification",
    # #335: the ruff half of the split dead-code gate.
    "dead_code_ruff": "verification",
    "mutation_testing": "verification",
    "prd_stories": "verification",
    "verification": "verification",
    # #315: the three Phase 1 gates the table did not carry.
    "fixtures": "verification",
    "policy_envelope": "verification",
    "test_adequacy": "verification",
    # #315 round 2: the phase names a failure recorded without
    # signatures= is filed under.
    "verify": "verification",
    "provisioning": "infrastructure",
    # #315: the category invented for the four failures that are neither
    # a gate's verdict nor the engineer's loop.
    "aborted": "infrastructure",
    "token_budget": "infrastructure",
    "pr": "infrastructure",
    "diff": "infrastructure",
    # #315: the fallback's answer, stated rather than inherited.
    "engineer": "iteration",
    "unknown": "iteration",
    "review": "review",
    "security": "security",
    "contract": "contract",
}

#: Producer sites the walk DETECTS but whose check name it cannot read,
#: as ``(module, enclosing qualname, expression, why)``. Pinned by
#: ``TestTheBlindSitesAreEnumerated.test_the_ledger_is_exactly_these_sites``,
#: so a new one is a red test and a resolved one is a red test too.
#:
#: The standard is ``tests/test_journal_one_writer.py``: "The test below
#: asserts that miss, so this disclosure fails if it stops being true."
#: A disclosure with no test behind it is how #339 review found this
#: file claiming "a new gate cannot reach the journal uncategorised"
#: while a whole producer was invisible to it.
#:
#: WHAT THIS LEDGER DOES NOT CLOSE, said plainly because the resemblance
#: to its precedent is misleading. ``EXPECTED_JOURNAL_PATH_SITES`` in
#: ``tests/test_journal_one_writer.py`` inventories every place the
#: resource is OBTAINED, so a new way of obtaining it lands in the list
#: whatever it looks like: closed by construction. This inventories the
#: places the walk GAVE UP, which is a different quantity. It is closed
#: only over the producer shapes ``_name_sites`` and ``_signature_sites``
#: already enumerate. A producer written in a shape neither of them
#: matches is not resolved AND not recorded here: it is silence, and the
#: ledger staying the same length is not evidence of anything.
#:
#: That is not hypothetical - it is how ``pipeline._fail_pr_flow``
#: survived two review rounds. #324 is the tracking item that closes the
#: class properly, by giving every guard in this repo one shared AST
#: walker instead of a hand-rolled matcher each; this file is the tenth
#: instance of that defect and should be retired onto it rather than
#: widened again.
#:
#: ``tests/test_signature_chokepoint.py`` is the one pin in this story
#: that DOES have the precedent's guarantee, and it is the answer to the
#: paragraph above until #324 lands: a check name cannot reach
#: ``category_for_check`` without touching a ``*failure_signatures``
#: name, and it pins all five such sites. Measured: a new writer
#: reaching the mapping through a local alias, with a signature the fold
#: cannot read, is invisible to the census, to this ledger, to
#: ``tests/test_check_name_shapes.py`` and to the spellings net, and
#: fails that pin alone.
#:
#: No line numbers: they rot on every edit above them and would make
#: this list a thing to be regenerated rather than read. The expression
#: text is what pins the site, so changing what a blind site computes is
#: a red test.
#: Every row today is a PASS-THROUGH or a RUNTIME COMPOSER, which is
#: the shape of an acceptable blind site: the string is decided
#: somewhere the walk does read, and the row says where. A site whose
#: name is decided HERE and cannot be read is not acceptable, and there
#: are none left - the last one was ``_coverage_failure``, resolved by
#: :func:`_from_the_callers`.
BLIND_SITES: tuple[tuple[str, str, str, str], ...] = (
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._phase_verify",
        "signatures_from_verification(verification.checks)",
        "composed at run time from the CheckResult names the gates built; "
        "those names are censused at their CheckResult call sites",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._record_failure_signatures",
        "list(signatures)",
        "pass-through: re-emits what fail/retry_or_fail was handed, censused at those call sites",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._record_failure_signatures",
        "signature_for_error(phase or 'unknown', error)",
        "the phase= fallback itself; the phase is censused at the "
        "fail/retry_or_fail call sites and 'unknown' is enrolled outright",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._review_failure",
        "signatures_from_findings('review', review_result.as_findings())",
        "composed at run time as '<phase>:<Finding.category>'; the phase is "
        "the literal in this call and the category comes from findings.py",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._route_failure",
        "failure.signatures",
        "pass-through: a PhaseFailure built at one of the sites above. "
        "This row appears TWICE on purpose - _route_failure has two "
        "branches spelling it, the comparison is a sorted list rather "
        "than a set, and collapsing them would hide one of the two",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._route_failure",
        "failure.signatures",
        "pass-through, the second of the two branches named above",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._route_failure",
        "failure.phase",
        "pass-through: the phase of a PhaseFailure built elsewhere in this "
        "module. PhaseFailure is a PHASE_FALLBACK_CALL in its own right, so "
        "each of those phases is censused where it is written",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._route_failure",
        "failure.phase",
        "pass-through, the second of the same two branches",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._security_failure",
        "signatures_from_findings('security', sec_result.as_findings())",
        "composed at run time, as _review_failure above",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline.fail",
        "phase",
        "pass-through of its own parameter into _record_failure_signatures; "
        "censused at the fail() call sites, which is why fail is itself a "
        "PHASE_FALLBACK_CALL",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline.retry_or_fail",
        "phase",
        "pass-through of its own parameter into _record_failure_signatures; "
        "censused at the retry_or_fail() call sites",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline.retry_or_fail",
        "phase",
        "the second of two, and a different call: the retries-exhausted route "
        "hands the same parameter on to fail(), censused at the same sites",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline.retry_or_fail",
        "signatures",
        "pass-through of its own parameter, censused at its call sites",
    ),
    (
        "kstrl/verify.py",
        "_failed_gate_result",
        "name",
        "pass-through of its own parameter; _failed_gate_result is itself a "
        "CHECK_NAME_CALL, so every caller is censused",
    ),
)


# --- where a name is written, and what it is called there -----------------


def _keyword_arguments(func_name: str, keyword: str) -> Iterator[tuple[str, ast.expr]]:
    """``(module, expression)`` for every ``keyword=`` at a call to ``func_name``."""
    for rel, tree in parsed_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or called_name(node.func) != func_name:
                continue
            yield from ((rel, kw.value) for kw in node.keywords if kw.arg == keyword)


@cache
def _keyword_values(func_name: str, keyword: str) -> frozenset[str] | None:
    """Every string passed as ``keyword`` at a call to ``func_name``.

    ``None`` when any call site passes something this walk cannot
    decide, so a partial answer is never mistaken for a complete one.
    That distinction is the whole point: a check name read off SOME of
    its callers is a guess.

    Asked one key at a time rather than indexed. #339 review measured
    the index at 1,742 ``(callable, keyword)`` entries and 60 ms to
    answer the one question the census asks of it, and the sticky-None
    merge it needed is an early return here.

    That holds while the census asks ONE question, which is what it does
    today: one cache miss a run, 60 ms either way. Re-measured on #339,
    the per-key walk is linear in keys (62 / 100 / 144 / 209 ms for one
    to four) where the index is flat, so the crossover is at two. A
    second key is the moment to index, not a later one.

    Keyed on the BARE callable name, so two same-named functions merge.
    That over-matches rather than under-matches, and the direction is
    the point: an extra name surfaces as unenrolled, which is a red test
    a human resolves, where a missed name is the silence #324 is about.
    """
    found: set[str] = set()
    for rel, value in _keyword_arguments(func_name, keyword):
        folded = fold(value, module_level_strings()[rel])
        if folded is None or HOLE in folded.text:
            return None
        found.add(folded.text)
    return frozenset(found)


def _parameter_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    args = func.args
    return frozenset(a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs))


def _from_the_callers(folded: Folded, scope: Scope) -> frozenset[str]:
    """Check names read off the call sites, when the name is a parameter.

    ``pipeline._coverage_failure`` builds
    ``f"{phase}:coverage-unverified:{reason}"`` from its own ``phase``
    argument, so the check name is nowhere in that function. Both #339
    reviewers mutated a caller's ``phase="review"`` to a bogus string
    and measured the suite still green. One interprocedural step fixes
    that and nothing less does: the name is decided by the callers.

    Deliberately narrow. It fires only when the head of the signature is
    exactly the parameter and the callers all pass it by keyword, which
    is what makes the answer complete rather than a sample. Anything
    else stays a blind site, so the rescue can move a row OUT of
    :data:`BLIND_SITES` and can never make the walk quieter.

    It also assumes the callers CALL BY BARE NAME, because that is what
    ``called_name`` resolves. Route the same function through a dispatch
    table or a ``functools.partial`` and this returns nothing and the row
    goes back to :data:`BLIND_SITES`, which is the safe end of that
    failure but is a limit rather than a property (#339 review, A6).
    """
    head = folded.first_hole
    if scope.function is None or not isinstance(head, ast.Name):
        return frozenset()
    if not folded.text.startswith(HOLE + ":"):
        return frozenset()
    if head.id not in _parameter_names(scope.function):
        return frozenset()
    return _keyword_values(scope.function.name, head.id) or frozenset()


# --- the four producers ---------------------------------------------------


def _writes_signatures(target: ast.expr) -> bool:
    """A subscript assignment into a ``*failure_signatures`` mapping.

    The fourth producer, and the only one that is not a call: ``factory``
    and ``pipeline`` set four components' signatures directly, bypassing
    ``_record_failure_signatures``. A census keyed on ``signatures=``
    could not see them at all.
    """
    if not isinstance(target, ast.Subscript):
        return False
    container = target.value
    name = getattr(container, "attr", None) or getattr(container, "id", "")
    return isinstance(name, str) and name.endswith(SIGNATURE_CONTAINER_SUFFIX)


def _listed(value: ast.expr) -> list[ast.expr]:
    """The elements of a signature list, or the list expression itself."""
    return list(value.elts) if isinstance(value, ast.List) else [value]


def _signatures_taken(node: ast.expr | None) -> bool | None:
    """Will the recorder take these ``signatures`` over the phase?

    Three answers, and the two producers read opposite ends of it.
    ``_record_failure_signatures`` branches on a plain ``if signatures:``,
    so ``True`` means the phase cannot become a check name and ``False``
    means no signature can come out of this expression. ``None`` is a
    run-time value, where EITHER branch can fire and so neither producer
    may skip.

    ``node is None`` is the argument being absent, which is the same
    thing to the recorder as an empty one.

    Two-valued before #339 review, as ``"signatures" in keywords``, and
    both mistakes it made were in the silencing direction: a literal
    ``None`` at ``_fail_pr_flow`` counted as signatures and hid the
    phase, and ``signatures=failure.signatures`` at ``_route_failure``
    did the same with a value the walk cannot decide at all.
    """
    if node is None:
        return False
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return bool(node.elts)
    if isinstance(node, ast.Constant):
        return bool(node.value)
    return None


def _phase_argument(node: ast.Call, keywords: dict[str | None, ast.expr]) -> ast.expr | None:
    """The ``phase`` handed to a failure recorder, keyword or positional."""
    name = called_name(node.func)
    if name not in PHASE_FALLBACK_CALLS:
        return None
    if "phase" in keywords:
        return keywords["phase"]
    index = parameter_index(name, "phase")
    if index is None:
        return None
    return node.args[index] if len(node.args) > index else None


def _name_sites(node: ast.AST) -> list[ast.expr]:
    """Expressions that give a check name OUTRIGHT, with no code after it."""
    if not isinstance(node, ast.Call):
        return []
    keywords: dict[str | None, ast.expr] = {kw.arg: kw.value for kw in node.keywords}
    if called_name(node.func) in CHECK_NAME_CALLS:
        named = keywords.get("name") or (node.args[0] if node.args else None)
        return [named] if named is not None else []
    if _signatures_taken(keywords.get("signatures")) is True:
        return []
    phase = _phase_argument(node, keywords)
    return [phase] if phase is not None else []


def _signature_sites(node: ast.AST) -> list[ast.expr]:
    """Expressions that give a whole ``"<check>:<code>"`` signature."""
    if isinstance(node, ast.Assign):
        if any(_writes_signatures(t) for t in node.targets):
            return _listed(node.value)
        return []
    if isinstance(node, ast.Call):
        signatures = {kw.arg: kw.value for kw in node.keywords}.get("signatures")
        # A provably empty `signatures=` is not a signature the walk
        # failed to read, it is the absence of one, and a BLIND_SITES row
        # for `signatures=None` is a row no reader can act on.
        #
        # The first clause is a TYPE narrowing, not a condition:
        # `_signatures_taken(None)` already answers False by its own
        # contract, so the second clause covers the absent case. It is
        # here because `_listed` takes `ast.expr`, not `ast.expr | None`.
        if signatures is not None and _signatures_taken(signatures) is not False:
            return _listed(signatures)
    return []


def _name_at(expr: ast.expr, own: dict[str, str]) -> frozenset[str]:
    """The check name a bare-name expression is known to give."""
    folded = fold(expr, own)
    if folded is None or HOLE in folded.text:
        return frozenset()
    return frozenset({folded.text})


def _names_in(expr: ast.expr, own: dict[str, str], scope: Scope) -> frozenset[str]:
    """Every check name a signature expression is known to give.

    The whole SUBTREE, so a conditional, an f-string and a list all
    answer without this knowing what any of them is.
    """
    found: set[str] = set()
    for folded in folded_nodes(expr, own):
        head = signature_head(folded)
        if head is not None:
            found.add(head)
        else:
            found |= _from_the_callers(folded, scope)
    return frozenset(found)


def _producer_sites(
    tree: ast.Module, own: dict[str, str]
) -> Iterator[tuple[Scope, ast.expr, frozenset[str]]]:
    """Every producer expression in one module, with the names it gives.

    An empty name set is the whole point of yielding the expression
    alongside it: that is a site the walk DETECTED and could not read,
    and the census records it rather than dropping it.
    """
    for node, scope in scoped_nodes(tree):
        for expr in _name_sites(node):
            yield scope, expr, _name_at(expr, own)
        for expr in _signature_sites(node):
            yield scope, expr, _names_in(expr, own, scope)


@lru_cache(maxsize=1)
def _census() -> tuple[dict[str, str], tuple[tuple[str, str, str], ...]]:
    """The names ``kstrl/`` emits, and the sites whose name would not read."""
    found: dict[str, str] = {}
    blind: list[tuple[str, str, str]] = []
    for rel, tree in parsed_modules():
        own = module_level_strings()[rel]
        for scope, expr, names in _producer_sites(tree, own):
            for name in names:
                found.setdefault(name, rel)
            if not names:
                blind.append((rel, scope.qualname, ast.unparse(expr)))
    return found, tuple(sorted(blind))


def check_names() -> dict[str, str]:
    """Every name ``kstrl/`` can send to ``category_for_check``."""
    return _census()[0]


def blind_sites() -> tuple[tuple[str, str, str], ...]:
    """Every producer site whose check name the walk cannot read."""
    return _census()[1]


class TestEveryCheckNameIsEnrolled:
    def test_the_walk_sees_the_gates_it_claims_to_guard(self) -> None:
        """A walk that matches nothing passes vacuously forever. Each
        group below is one resolution path, and each was broken at some
        point in this file's short history."""
        names = set(check_names())
        assert {"test_suite", "typecheck", "linter"} <= names, "module-constant resolution"
        assert {"diff_scope", "scope_unreadable", "prd_stories"} <= names, "literal names"
        assert {"pr", "engineer", "token_budget"} <= names, "signature prefixes"
        # #315 round 2: failures recorded with no signatures= are filed
        # under their phase, and the walk was blind to that whole
        # producer. `provisioning` is the one that proves it resolves
        # across modules: the call is in factory.py, not pipeline.py.
        assert {"provisioning", "verify"} <= names, "phase= fallback names"
        # #339 review: the fourth producer, four direct assignments into
        # component_failure_signatures that are not calls at all.
        assert "contract" in names, "component_failure_signatures[...] = [...]"
        # #339 review round 2 measured this line inert as first written
        # ("security" in names): `security` is spelled at another site,
        # so deleting the whole interprocedural rescue left it green.
        # The property that actually moves is the rescue taking a row
        # OUT of the ledger, so that is what is asserted.
        assert (
            "kstrl/pipeline.py",
            "ComponentPipeline._coverage_failure",
            "f'{phase}:coverage-unverified:{reason}'",
        ) not in blind_sites(), "a check name resolved from the call sites"
        # #306: this one was not pinned, and so was not protected.
        # Rewriting `CheckResult(name="mutation_testing", ...)` as
        # `name=name` off a function-local took the walk from 19 names
        # to 18 with nothing failing: the walk fails on an unenrolled
        # name and cannot fail on one it cannot see. Measured on that
        # branch before the fix.
        assert "mutation_testing" in names, "check-name constant in the defining module"
        # #335 split one dead-code row in two, and both names are now
        # module constants in verify.py rather than repeated literals.
        # Pinned beside mutation_testing for the same reason: the walk
        # resolves module constants, so a rewrite to a function-local
        # would take both names out of the census silently.
        assert {"dead_code", "dead_code_ruff"} <= names, "both dead-code phases"

    def test_every_emitted_name_is_enrolled(self) -> None:
        """#315 emptied the grandfathered set, so this has no exceptions
        left: a name kstrl emits and the table does not carry is a
        failure filed under whichever category the fallback picks."""
        missing = {
            name: where for name, where in check_names().items() if name not in _CATEGORY_BY_CHECK
        }
        assert not missing, (
            f"names emitted by kstrl/ but absent from "
            f"evolution._CATEGORY_BY_CHECK: {missing}. An unenrolled name "
            f"falls through to 'iteration', filing a verification gate under "
            f"the engineer loop. Add a row to that table."
        )

    def test_the_walk_covers_every_enrolled_name_but_these(self) -> None:
        """The blind-guard test (#324). Every miss logged in that issue
        was in the skip direction: the matcher stopped resolving a name,
        saw fewer sites and reported clean. Pinning the enrolled names it
        cannot see turns the next such refactor from a silent narrowing
        into a red test, for the whole table rather than the names the
        anti-vacuity test happens to list."""
        invisible = set(_CATEGORY_BY_CHECK) - set(check_names())
        assert invisible == set(ENROLLED_BUT_INVISIBLE), (
            f"the walk sees a different set of enrolled names than "
            f"expected. Newly invisible: {sorted(invisible - set(ENROLLED_BUT_INVISIBLE))} "
            f"(a refactor probably moved a name out of the shape the walk "
            f"matches - fix the walk, do not add the name here). Newly "
            f"visible: {sorted(set(ENROLLED_BUT_INVISIBLE) - invisible)} "
            f"(delete its row from ENROLLED_BUT_INVISIBLE)."
        )

    def test_the_runtime_composed_phases_are_enrolled(self) -> None:
        """``signatures_from_findings`` builds these from a runtime
        argument. Kept after #315 added a whole-table pin that also
        catches a dropped row: this one names the REASON these three
        cannot be dropped, and a pin is satisfied by editing the
        expectation."""
        for phase in ("review", "security", "contract"):
            assert phase in _CATEGORY_BY_CHECK, (
                f"{phase!r} is composed into failure signatures by "
                f"evolution.signatures_from_findings and must stay enrolled."
            )

    def test_the_table_still_says_what_it_is_pinned_to_say(self) -> None:
        """Every row, not a sample. A typo in a category value invents a
        category nothing would reject; a dropped row silently re-files a
        gate under the engineer loop; an added row can move a run out of
        the autonomy replay's evidence. All three are one diff away and
        none of them changes a line of code."""
        assert _CATEGORY_BY_CHECK == EXPECTED_CATEGORIES

    def test_a_colliding_constant_resolves_to_nothing(self) -> None:
        """The wrong answer is worse than no answer: a name two modules
        bind differently must not be attributed to either."""
        agreed = {"a.py": {"SHARED": "x"}, "b.py": {"SHARED": "x", "OWN": "y"}}
        assert unambiguous_pool(agreed) == {"SHARED": "x", "OWN": "y"}
        conflicting = {"a.py": {"SHARED": "x"}, "b.py": {"SHARED": "z"}}
        assert "SHARED" not in unambiguous_pool(conflicting)


class TestTheBlindSitesAreEnumerated:
    """#339 review: the guard's docstring said "a new gate cannot reach
    the journal uncategorised" and that was false. The walk resolved
    three ``signatures=`` sites out of six, could not see the fourth
    producer at all, and DROPPED every site it could not read instead of
    reporting it.

    Dropping is the failure mode #324 is about, so the ledger below is
    the correction: detection is separate from resolution, and a site
    whose name will not resolve is enumerated rather than forgotten.
    """

    def test_the_ledger_is_exactly_these_sites(self) -> None:
        """Both directions. A new blind site fails because the walk
        found a producer it cannot read; a resolved one fails because
        the ledger is now claiming a limit that no longer exists, which
        is the disclosure-without-a-test this file was pulled up for."""
        measured = sorted(blind_sites())
        pinned = sorted((rel, qual, expr) for rel, qual, expr, _ in BLIND_SITES)
        assert measured == pinned, (
            f"the set of producer sites whose check name the walk cannot "
            f"read has moved. Newly blind: {sorted(set(measured) - set(pinned))} "
            f"(read the name if you can, and add a BLIND_SITES row with a "
            f"reason if you cannot). No longer blind: "
            f"{sorted(set(pinned) - set(measured))} (delete its row)."
        )

    # The property this class asserts on a FIXTURE - a detected producer
    # whose name will not resolve must be reported rather than dropped -
    # is pinned next door, by
    # tests/test_check_name_shapes.py::test_a_pass_through_is_detected_and_unread.
    # It was spelled twice on #339, in twelve lines here and three
    # there, on the same fixture string; the shorter one asserts more.

    def test_every_blind_site_says_why(self) -> None:
        """A ledger row with no reason is a silencer. The reason is what
        a future reader needs to decide whether the limit is still
        acceptable."""
        for rel, qual, expr, why in BLIND_SITES:
            assert why.strip(), f"{rel} {qual} {expr} has no stated reason"
