"""Continuous learning - evolution journal, experiment tracking, and harness proposals.

Records factory run outcomes and extracts recurring failure patterns across runs.
Inspired by AutoResearchClaw's evolution directory and autoresearch-agents' results.tsv.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.manifest import ADVERSARIAL_BUDGET_CHECK
from kstrl.observability import handle_ends_without_newline, read_progress_events
from kstrl.verify import SCOPE_UNREADABLE_CHECK, SCOPE_UNREADABLE_ERROR_PREFIX

if TYPE_CHECKING:
    from kstrl.factory import FactoryResult
    from kstrl.findings import Finding
    from kstrl.manifest import Component, Manifest
    from kstrl.verify import CheckResult

logger = logging.getLogger("kstrl.evolution")

# R6.4: journal entries carry an explicit schema version so future
# format migrations are detectable. Version 2 = structured failure
# signatures (R6.1). Entries without the field are version 1 (the
# pre-R6 shape); wave 1 (R4.1) archived the polluted v1 journals to
# .kstrl/archive/, so fresh journals contain v2 entries only.
JOURNAL_SCHEMA_VERSION = 2

# #312: the event_type of the row append_entries writes when it finds the
# journal not newline-terminated. Its own type rather than a synthetic
# component_result, for the reason _role_usage_entries gives: every
# aggregate in this module selects on event_type, so a row of this type
# counts towards nothing and cannot invent an outcome. It exists to be
# grepped: it is the only durable trace that a crash tore the file.
JOURNAL_REPAIR_EVENT = "journal_repair"

# #260: the event_type of one recorded spec audit. ``decompose`` writes
# these rows and :meth:`EvolutionJournal.get_spec_audits` selects on
# them, so the name belongs on the layer that defines the journal's
# schema rather than on the writer (#314). It lived in ``decompose``
# until then, with this module holding a second copy as a literal, and
# the cost of that placement is the reason it moved: a reader added
# HERE reaches for the nearest spelling, which was the literal.
SPEC_ISSUES_EVENT = "spec_issues"


# The header row record_run writes to experiments.tsv, at module scope so
# that a test can assert against the columns the writer actually emits
# rather than a shorter hand-typed row that csv.DictReader happens to
# tolerate. Files written before R3.1 keep their shorter header.
EXPERIMENTS_HEADER = (
    "run_id\ttimestamp\tproject\tcomponents_total\tcompleted\tfailed\t"
    "skipped\tavg_iterations\tavg_duration_s\tretry_rate\tcommon_failure\t"
    "total_tokens\ttotal_cost_usd\tunreported_calls"
)

# #191: what a component_result entry records when no fact-utilization
# measurement reached the journal - the component never got past the
# gates to the distill phase, knowledge was off, or the measurement
# itself failed. Deliberately NOT the same value as a measured zero:
# `measured=False` is "no evidence", `measured=True, referenced=0` is
# evidence that injected facts went unused. Not version-gated, because
# the key is written on every entry from here on; a pre-#191 entry has
# no key at all, which is a third, distinguishable state.
_UNMEASURED_UTILIZATION: dict[str, Any] = {
    "measured": False,
    "injected": 0,
    "referenced": 0,
    "reason": "not measured",
    # Same shape as a measured entry, so a consumer reads `measured`
    # rather than probing for key presence.
    "by_tier": {
        "core": {"injected": 0, "referenced": 0},
        "dependency": {"injected": 0, "referenced": 0},
        "sibling": {"injected": 0, "referenced": 0},
    },
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class EvolutionConfig:
    enabled: bool = True
    journal_path: Path = field(default_factory=lambda: Path(".kstrl/evolution.jsonl"))
    experiments_path: Path = field(default_factory=lambda: Path(".kstrl/experiments.tsv"))
    min_pattern_frequency: int = 2
    lookback_runs: int = 10
    auto_propose: bool = True
    auto_apply_computational: bool = False

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> EvolutionConfig:
        """Load evolution config from environment variables only.

        Relative journal/experiments paths resolve against ``root_dir``
        (the project root), not the process CWD, matching :meth:`load`.
        """
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        _apply_env_overrides(config, root_dir)
        _resolve_relative_paths(config, root_dir)
        return config

    @classmethod
    def load(cls, root_dir: Path | None = None) -> EvolutionConfig:
        """Load evolution config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "evolution")
        if "enabled" in section:
            config.enabled = bool(section["enabled"])
        if "journal_path" in section:
            jp = str(section["journal_path"])
            config.journal_path = Path(jp) if Path(jp).is_absolute() else root_dir / jp
        if "experiments_path" in section:
            ep = str(section["experiments_path"])
            config.experiments_path = Path(ep) if Path(ep).is_absolute() else root_dir / ep
        if "min_pattern_frequency" in section:
            config.min_pattern_frequency = int(section["min_pattern_frequency"])
        if "lookback_runs" in section:
            config.lookback_runs = int(section["lookback_runs"])
        if "auto_propose" in section:
            config.auto_propose = bool(section["auto_propose"])
        if "auto_apply_computational" in section:
            config.auto_apply_computational = bool(section["auto_apply_computational"])
        _apply_env_overrides(config, root_dir)
        _resolve_relative_paths(config, root_dir)
        return config

    @classmethod
    def load_or_none(
        cls,
        root_dir: Path,
        warn: Callable[[str], None],
    ) -> EvolutionConfig | None:
        """:meth:`load`, but a config that will not parse returns None.

        The journal is an optional audit trail and every caller loads it
        in the middle of work that has already been paid for, so a typo
        in one of its knobs should cost the journal, not the run.

        Which exceptions that means is stated here rather than at a call
        site, because it is a fact about :meth:`load`: ``ValueError``
        from malformed TOML or a non-integer ``lookback_runs`` (from the
        file or from ``KSTRL_EVOLUTION_LOOKBACK_RUNS``), ``TypeError``
        from a toml array where a number belongs, and ``OSError`` from
        an unreadable ``kstrl.toml``. A future coercion added to
        ``load`` is then covered here instead of silently escaping a
        guard somebody wrote around a call.

        ``TypeError`` was added when #272 gave the same section an entry
        check: ``config_preflight.REJECTIONS`` treats it as operator
        input, and two lists that disagree would degrade the same value
        at startup and then raise on it mid-run.

        It is deliberately NOT
        ``config_preflight.SURFACE_REJECTIONS``, which is this tuple
        plus ``RuntimeError``. #289 tried importing that instead, on
        the reasoning above, and
        ``test_decompose.py::test_the_artifact_is_written_before_any_journal_work``
        failed: that test raises ``RuntimeError`` from :meth:`load` on
        purpose, to assert that an error the guard does NOT catch still
        leaves the halt artifact on disk. No coercion in :meth:`load`
        produces one, so widening to it could only ever swallow a
        defect, and the entry check degrading where this raises is the
        price of keeping that defect visible mid-run.

        The cost of that widening, stated because it is real: a
        ``TypeError`` from a DEFECT inside :meth:`load` - a None where a
        path belongs, a signature that stopped matching - now reads as
        "config unreadable, skipping journal" rather than surfacing. It
        cannot be narrowed to the toml-array case without inspecting the
        message, which would be guessing. The journal going quiet is the
        signal that a defect is hiding here, so treat a "skipping
        journal" warning on a config that looks correct as a bug report
        about this method rather than about the operator's file.

        Degrades loudly: ``warn`` is called with the parse failure.
        """
        try:
            return cls.load(root_dir)
        except (ValueError, TypeError, OSError) as exc:
            warn(f"Evolution config unreadable, skipping journal: {exc}")
            return None


def _apply_env_overrides(config: EvolutionConfig, root_dir: Path) -> None:
    """Overlay env vars that are explicitly set; unset vars leave the
    existing value untouched (so toml values survive the overlay)."""
    if "KSTRL_EVOLUTION_ENABLED" in os.environ:
        config.enabled = os.environ["KSTRL_EVOLUTION_ENABLED"].lower() in {
            "1",
            "true",
            "yes",
        }
    if "KSTRL_EVOLUTION_JOURNAL_PATH" in os.environ:
        raw = os.environ["KSTRL_EVOLUTION_JOURNAL_PATH"]
        config.journal_path = Path(raw) if Path(raw).is_absolute() else root_dir / raw
    if "KSTRL_EVOLUTION_LOOKBACK_RUNS" in os.environ:
        config.lookback_runs = int(os.environ["KSTRL_EVOLUTION_LOOKBACK_RUNS"])


def _resolve_relative_paths(config: EvolutionConfig, root_dir: Path) -> None:
    """Anchor relative journal/experiments paths to the project root.

    The bare ``EvolutionConfig()`` constructor keeps its historical
    CWD-relative defaults; the load/from_env paths always hand back
    absolute paths so ``ks factory --root X`` run from elsewhere
    cannot scatter ``.kstrl/`` state into the operator's CWD.
    """
    if not config.journal_path.is_absolute():
        config.journal_path = root_dir / config.journal_path
    if not config.experiments_path.is_absolute():
        config.experiments_path = root_dir / config.experiments_path


@dataclass
class FailurePattern:
    description: str
    frequency: int
    total_components: int
    affected_components: list[str]
    check_name: str  # e.g. "test_suite", "typecheck", "linter", "review"
    # structured failure code (e.g. "S608" for ruff, "arg-type" for mypy,
    # "scope_creep" for a review concern) - the part after the colon in
    # the full "<check>:<code>" signature
    error_signature: str
    # A _CATEGORY_BY_CHECK value; see category_for_check.
    category: str


@dataclass
class HarnessProposal:
    id: str  # e.g. "PROP-001"
    title: str
    description: str
    proposal_type: str  # "computational" or "inferential"
    target: str  # what to change: "claude_md", "typecheck_config", "feedforward_config"
    suggested_change: str  # the actual proposed content/config change
    source_patterns: list[str]  # pattern descriptions that led to this proposal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Regex for linter rule codes like S608, E501, W291, etc.
_LINTER_CODE_RE = re.compile(r"\b([A-Z]\d{3,4})\b")

# Regex to strip file paths (unix and windows style)
_PATH_RE = re.compile(r"(?:/[\w./-]+|[A-Z]:\\[\w.\\-]+)")

# Regex to strip line/column numbers like ":42:" or "line 42"
_LINENO_RE = re.compile(r"(?::\d+:?\d*|line \d+|col(?:umn)? \d+)", re.IGNORECASE)

# Regex to strip quoted variable/argument names
_QUOTED_NAME_RE = re.compile(r"['\"][\w.]+['\"]")


def _normalize_error(error: str) -> str:
    """Normalize an error string into a stable signature.

    - Keeps linter rule codes as-is (e.g. S608, E501).
    - Strips file paths, line numbers, and variable names.
    - Converts the remaining message to a slug.
    """
    if not error:
        return ""

    # Check for a linter rule code first - it is the most stable identifier.
    code_match = _LINTER_CODE_RE.search(error)
    if code_match:
        return code_match.group(1)

    normalized = error
    normalized = _PATH_RE.sub("", normalized)
    normalized = _LINENO_RE.sub("", normalized)
    normalized = _QUOTED_NAME_RE.sub("", normalized)

    # Take the first meaningful line only.
    first_line = normalized.strip().split("\n")[0].strip()

    # Extract "ErrorType: message" pattern if present.
    colon_idx = first_line.find(":")
    if colon_idx > 0:
        error_type = first_line[:colon_idx].strip()
        message = first_line[colon_idx + 1 :].strip()
        # Slugify message portion.
        slug = re.sub(r"[^a-z0-9]+", "-", message.lower()).strip("-")
        if slug:
            return slug[:80]
        return re.sub(r"[^a-z0-9]+", "-", error_type.lower()).strip("-")[:80]

    slug = re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
    return slug[:80] if slug else "unknown"


def _classify_check(error: str) -> str:
    """Return the check name inferred from the error text.

    The legacy fallback, used wherever a component has no in-memory
    ``failure_signatures`` entry - which is EVERY ``ks evolve`` or
    metrics read over a manifest written by an earlier process. So a
    failure whose text lands here unrecognised is filed as
    unknown/iteration no matter how carefully it was categorised at the
    time it happened.

    #294 round 2: that is what happened to the scope refusal. Its own
    ``_CATEGORY_BY_CHECK`` row and its own ``propose_improvements`` arm
    were both unreachable on this path, so ``ks evolve`` still emitted
    the generic "add this to CLAUDE.md" proposal - agent advice for a
    state no agent can influence, which is the thing the arm exists to
    prevent. Matched FIRST because the text also contains words the
    later rules claim.

    #315: this returned ``(check_name, category)`` until every caller
    was measured to discard the category, making it a SECOND place a
    category was decided that no consumer could reach. A dead answer
    that can silently disagree with the live one is how the live one
    later gets "fixed" in the wrong file, so the category is gone from
    here and ``category_for_check`` is the only decision.
    """
    lower = error.lower()

    if SCOPE_UNREADABLE_ERROR_PREFIX.lower() in lower:
        return SCOPE_UNREADABLE_CHECK
    if any(kw in lower for kw in ("ruff", "flake8", "pylint", "lint")):
        return "linter"
    if any(kw in lower for kw in ("mypy", "pyright", "typecheck", "type error")):
        return "typecheck"
    if any(kw in lower for kw in ("pytest", "test", "assert", "unittest")):
        return "test_suite"
    if any(kw in lower for kw in ("review", "finding", "reviewer")):
        return "review"
    if any(kw in lower for kw in ("contract", "integration")):
        return "contract"
    if any(kw in lower for kw in ("mechanical verification failed",)):
        return "verification"

    return "unknown"


# ---------------------------------------------------------------------------
# Structured failure signatures (R6.1)
#
# A failure signature is "<check_name>:<code>", e.g. "linter:E501",
# "typecheck:arg-type", "review:scope_creep", "diff_scope:files-outside-
# allowed-scope". The check prefix comes from the gate that fired; the
# code comes from the tool's parser (ruff rule, mypy error code, finding
# category) rather than from re-parsing a flattened error string, so
# cross-run grouping is on real, stable identifiers.
# ---------------------------------------------------------------------------

# Digit runs are counts/limits ("3 files outside scope", "600s wall
# clock") - stripping them keeps slugs stable across runs whose only
# difference is the number.
_DIGIT_RUN_RE = re.compile(r"\d+")

# The categories are verification, review, security, contract,
# iteration and, since #315, infrastructure. That last one is for a
# failure that is neither a gate's verdict on the change nor the
# engineer's loop: the run was shut down, hit the token ceiling, could
# not merge, could not fetch its own diff, could not provision a
# worktree. Before it existed those fell through to "iteration", so the
# journal filed them as engineer-loop problems and the evolve screen's
# category column said so: a verdict about an agent that could not have
# prevented any of them.
#
# NOT Finding.category's "infrastructure_error" (kstrl/findings.py),
# which marks one ROLE RUN that failed to execute inside an otherwise
# healthy component. Different taxonomy, different consumer,
# deliberately different spelling so a grep for either does not silently
# return the other.
#
# They also disagree, on purpose and measurably. factory's live autonomy
# accounting asks the FINDING question (`_infra_casualty`) while the
# replay asks the SIGNATURE question, and #339 review counted the
# divergence rather than leaving it at the one example this comment used
# to give. Seven signatures, in both directions:
#
#   - the whole `pr:` family, `provisioning:` and `aborted:shutdown` are
#     infrastructure to the replay and attach no finding at all, so the
#     live side calls them judgement;
#   - `review:infrastructure` and `security:infrastructure` are the
#     reverse: a finding is attached, but `review`/`security` are not
#     infrastructure prefixes, so the replay counts them as evidence;
#   - `scope_unreadable:` depends on which producer fired - the Phase 1
#     gate attaches a finding, the pre-launch refusal in factory does
#     not;
#   - `adversarial_budget:setpoint` is the seventh, added by #226 round
#     2 and of the first kind: the R10.3 set-point gate refuses a
#     component whose reviewer never ran, the only finding on it is the
#     phase_skipped trace, so the replay calls it plumbing and the live
#     side calls it judgement. Its two siblings do NOT diverge -
#     `adversarial_budget:review` and `adversarial_budget:security` come
#     from `pipeline._budget_refusal`, which attaches the
#     infrastructure_error finding, so both consumers read the same
#     answer. Enrolling the check is what fixed those two and what
#     exposed this one: before the sweep it was spelled
#     `review:setpoint-budget-exhausted` and both consumers agreed by
#     both being wrong. tests/test_setpoint_agreement.py::
#     test_the_setpoint_refusal_is_a_disclosed_divergence asserts both
#     halves, so this row fails if it stops being true.
#
# Reconciling those is not this table's job (#332 holds factory.py), but
# an undercount was, because it read as a single known exception.
#
# tests/test_check_name_enrolment.py pins the whole table row by row, so
# a new row, a dropped row or a typo in a category is a red test with
# the diff as its audit trail.
_CATEGORY_BY_CHECK = {
    "linter": "verification",
    "typecheck": "verification",
    "test_suite": "verification",
    "diff_scope": "verification",
    # #294 split this out of diff_scope; diff_scope stays because
    # journal entries written before the split carry its signatures.
    SCOPE_UNREADABLE_CHECK: "verification",
    "bad_patterns": "verification",
    "self_critique": "verification",
    "dead_code": "verification",
    "mutation_testing": "verification",
    "prd_stories": "verification",
    "verification": "verification",
    # #315: mechanical gates that the table did not carry, so every
    # approved-fixtures failure, policy-envelope breach and
    # test-adequacy block was filed under the engineer loop.
    "fixtures": "verification",
    "policy_envelope": "verification",
    "test_adequacy": "verification",
    # #315 round 2: a failure recorded with no signatures= is filed
    # under its PHASE (pipeline._record_failure_signatures), so these
    # two are check names as much as any gate is. "verify" is the phase
    # spelling of the mechanical gates above; "provisioning" is a
    # worktree that would not build, which no agent can write code
    # against.
    "verify": "verification",
    "provisioning": "infrastructure",
    # #315: recorded outside a CheckResult, by pipeline.fail(signatures=
    # ["<prefix>:<code>"]). None of these is a verdict on the change.
    "aborted": "infrastructure",
    "token_budget": "infrastructure",
    "pr": "infrastructure",
    "diff": "infrastructure",
    # R10.5 (#226): a hard-mode reviewer that never ran because
    # max_adversarial_calls was already spent. Infrastructure for the
    # same reason token_budget is: it is a ceiling the operator set,
    # not a verdict on the change, and no reviewer looked at the
    # component. Enrolling it here is what makes the replay agree with
    # factory's live accounting, which reads the infrastructure_error
    # finding the same refusal attaches.
    ADVERSARIAL_BUDGET_CHECK: "infrastructure",
    # #315: the fallback already answers "iteration" for these two. The
    # rows are here so the table states every name kstrl emits rather
    # than most of them, and so that a reader cannot tell an unenrolled
    # name from a deliberate one by its absence. "unknown" is what
    # _classify_check returns when it cannot recognise a legacy error
    # string: not the engineer's fault so much as nobody's, and inventing
    # a category for "we could not tell" would be a worse answer than
    # the one this table has always given.
    "engineer": "iteration",
    "unknown": "iteration",
    "review": "review",
    "security": "security",
    "contract": "contract",
}

#: Checks whose failures are infrastructure, derived from the table so
#: there is ONE answer rather than two lists to keep in step:
#: :data:`kstrl.autonomy_replay.INFRA_FAILURE_PREFIXES` builds its
#: prefixes from this, so enrolling a check as infrastructure reaches
#: both consumers in one edit (#315).
INFRASTRUCTURE_CHECKS: frozenset[str] = frozenset(
    name for name, category in _CATEGORY_BY_CHECK.items() if category == "infrastructure"
)

# Cap on distinct per-check signatures so one catastrophic run (e.g. 40
# distinct ruff rules) cannot flood the journal entry.
_MAX_SIGNATURES_PER_CHECK = 5


def signature_slug(text: str) -> str:
    """Stable low-cardinality slug for a failure message.

    Strips file paths, line/column numbers, quoted names, and standalone
    counts, then slugifies the first line. Unlike ``_normalize_error``
    this never extracts linter codes (callers get those from the parser
    directly) and never keeps varying counts."""
    if not text:
        return ""
    normalized = _PATH_RE.sub("", text)
    normalized = _LINENO_RE.sub("", normalized)
    normalized = _QUOTED_NAME_RE.sub("", normalized)
    normalized = _DIGIT_RUN_RE.sub("", normalized)
    first_line = normalized.strip().split("\n")[0].strip()
    slug = re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
    return slug[:60]


def signature_for_error(check_name: str, error: str) -> str:
    """Fallback signature when no parser-level codes are available."""
    slug = signature_slug(error) or "failed"
    return f"{check_name or 'unknown'}:{slug}"


def split_signature(signature: str) -> tuple[str, str]:
    """Split "check:code" into (check_name, code)."""
    check, sep, code = signature.partition(":")
    if not sep:
        return "unknown", signature
    return check or "unknown", code or "failed"


def category_for_check(check_name: str) -> str:
    """Map a check/gate name to a FailurePattern category.

    An unlisted name falls through to "iteration", which files a gate
    under the engineer loop. Enrolling a new check in
    ``_CATEGORY_BY_CHECK`` was a convention with no mechanism, and
    measured during #294 the convention did not hold for eight of the
    nineteen names ``kstrl/`` emits. All eight are enrolled as of #315,
    four of them into the ``"infrastructure"`` category that had to be
    invented to hold them, and nothing is grandfathered any more.

    ``tests/test_check_name_enrolment.py`` is the mechanism, and #339
    review corrected what it establishes. It AST-walks ``kstrl/`` for
    the four places a component's failure signatures are written - a
    ``CheckResult`` name, a ``signatures=`` argument, the ``phase=`` of
    a failure recorded with neither, and a direct
    ``component_failure_signatures[...] = [...]`` assignment - and fails
    on a name this table does not carry. What it does NOT establish is
    that no gate can reach the journal uncategorised, which is what this
    docstring claimed while the fourth producer was invisible to it and
    eight of nine shape mutations survived.

    What it establishes now, in three files and stated as three separate
    claims because they hold three separate things:

    - a producer site the walk RECOGNISES and cannot read is enumerated
      in its ``BLIND_SITES`` ledger rather than dropped, which is what
      the old walk did;
    - ``tests/test_signature_spellings.py`` pins every signature-shaped
      string in the package whatever container it sits in, so a producer
      in a shape nobody has thought of still has to appear somewhere;
    - ``tests/test_check_name_shapes.py`` pins what the walk SAYS about
      each shape, including the shapes it says nothing about, because a
      shape the walk does not recognise leaves no trace in either of the
      two above. That is a limit, not a proof, and it is the limit
      ``pipeline._fail_pr_flow`` lived in until #339 review.

    The answer is computed here, at read time, and never stored in the
    journal - measured: a ``record_run`` entry has no ``category`` key
    and experiments.tsv records the signature only. So a correction to
    the table also corrects what is reported about runs that already
    happened. The one surface that displays it is the evolve screen's
    patterns table; the ``ks evolve`` CLI prints the check name.
    """
    return _CATEGORY_BY_CHECK.get(check_name, "iteration")


def signatures_from_verification(checks: Iterable[CheckResult]) -> list[str]:
    """Derive structured signatures from failed mechanical checks.

    Prefers the parser's structured codes (linter rule, checker error
    code, the exception a test died on); falls back to a slug of the
    check message when no parse is available.

    #258: this used to ask ``ParsedOutput.tool`` which of those it was,
    against the exact strings "ruff", "mypy" and "pytest". That is a name
    check standing in for a capability check, and it broke twice over as
    soon as a gate could dispatch: a newly supported tool fell silently
    through to the prose slug, and the unioned label a chained command
    produces ("pytest+vitest") matched nothing at all. The parser now
    names its own signature in ``ParsedFailure.code``, so this reads a
    capability instead of guessing from a label."""
    signatures: list[str] = []
    for check in checks:
        if check.passed:
            continue
        parsed = check.parsed
        codes = [f.code for f in parsed.failures if f.code] if parsed is not None else []
        if codes:
            distinct = list(dict.fromkeys(codes))[:_MAX_SIGNATURES_PER_CHECK]
            signatures.extend(f"{check.name}:{code}" for code in distinct)
        else:
            signatures.append(signature_for_error(check.name, check.message))
    return list(dict.fromkeys(signatures))


def signatures_from_findings(phase: str, findings: Iterable[Finding]) -> list[str]:
    """Derive signatures from the typed findings that failed a review or
    security gate: "<phase>:<category>" for every gating finding
    (severity fail/critical/high) and "<phase>:infrastructure" when the
    role itself failed to run."""
    signatures: list[str] = []
    for finding in findings:
        if finding.is_infrastructure_error:
            signatures.append(f"{phase}:infrastructure")
        elif finding.severity in ("fail", "critical", "high"):
            signatures.append(f"{phase}:{finding.category}")
    return list(dict.fromkeys(signatures))[:_MAX_SIGNATURES_PER_CHECK]


def _timestamp_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_str(entry: dict[str, Any], key: str) -> str:
    """One string field of a JSON-decoded journal record, "" when absent.

    A null or non-string field is an ABSENT field, not a value to be
    stringified. ``str(None)`` renders the literal "None", which #280
    round 1 reproduced as a phantom project named 'None' in the
    convergence report and a spec file printed as ``None``. Nothing is
    assumed about a record beyond it being a JSON object, so a journal
    written by an older version, or edited by hand, still reads.

    Lives here rather than in ``decompose`` (#314) because the window
    in :meth:`EvolutionJournal.get_spec_issue_runs` matches a project
    by this rule and the report's accounting matches by the same one.
    Two copies of it would let the trend and the accounting disagree
    about which audits belong to the project being reported on.

    Applies to a record's nested objects too, which is what the stored
    issue list is: same JSON, same rule.
    """
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def _journal_line(entry: dict[str, Any]) -> str:
    """One JSONL line, terminator included. The journal's line format."""
    return json.dumps(entry, separators=(",", ":")) + "\n"


def _repair_entry() -> dict[str, Any]:
    """The row :meth:`EvolutionJournal.append_entries` writes on finding
    an unterminated tail.

    Carries no ``run_id`` on purpose: ``_read_journal_entries`` keeps the
    last N distinct run_ids, so a repair row with one of its own would be
    one of the N and a single tear would shorten the history every
    aggregate reads by a whole run.
    """
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "timestamp": _timestamp_now(),
        "event_type": JOURNAL_REPAIR_EVENT,
        "detail": (
            "the preceding line was not newline-terminated when this append "
            "ran, so a write was interrupted. It is either a torn fragment "
            "that was never readable, or a complete record that lost only its "
            "newline; both are on their own line now."
        ),
    }


def _summarize_findings(findings: list[Finding]) -> dict[str, Any]:
    """Aggregate counts grouped by phase, severity, category, and OWASP
    bucket for the evolution journal. Lets dashboards query trends
    without re-walking every Finding."""
    summary: dict[str, Any] = {
        "total": len(findings),
        "by_phase": {},
        "by_severity": {},
        "by_category": {},
        "by_owasp": {},
        "infrastructure_errors": 0,
    }
    for f in findings:
        if f.is_infrastructure_error:
            summary["infrastructure_errors"] += 1
        summary["by_phase"][f.phase] = summary["by_phase"].get(f.phase, 0) + 1
        summary["by_severity"][f.severity] = summary["by_severity"].get(f.severity, 0) + 1
        summary["by_category"][f.category] = summary["by_category"].get(f.category, 0) + 1
        if f.owasp:
            summary["by_owasp"][f.owasp] = summary["by_owasp"].get(f.owasp, 0) + 1
    return summary


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


def _role_usage_entries(
    usage_by_component: dict[str, dict[str, dict[str, Any]]],
    *,
    manifest: Manifest,
    run_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """Journal rows for spend that belongs to no manifest component.

    ``record_run`` builds its rows by walking the MANIFEST, so a usage
    key with no component was dropped on the floor while ``run_usage`` -
    which does include it - fed the TSV's ``total_cost_usd``. The
    journal's per-component rows then did not sum to its own run total
    (#257 review). The architect is what made that reachable: a one-role
    pseudo-component that spends before any component exists and never
    appears in a manifest.

    "Never" became structural in #281. This split is a set difference
    against the manifest's ids, so while role keys were bare words a
    component genuinely named `architect` swallowed the role row: the
    difference was empty, the spend was attributed to the component's
    ``usage`` field, and no ``role_usage`` row was written at all.
    ``names.role_component_key`` puts role keys where no component id can
    be spelled, so the two sets are now disjoint by construction rather
    than by what the architect happened to name things.

    A distinct ``event_type`` rather than a synthetic
    ``component_result``, because every field that row carries - status,
    retries, findings, failed_phase - is meaningless for something that
    is not a component, and three readers in this module aggregate over
    ``component_result`` specifically. They ignore this type, which is
    the point: the row records spend without inventing an outcome.
    """
    component_ids = {comp.id for comp in manifest.components}
    return [
        {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "timestamp": timestamp,
            "run_id": run_id,
            "project": manifest.project_name,
            "component_id": role,
            "event_type": "role_usage",
            "usage": usage_by_component[role],
        }
        for role in sorted(set(usage_by_component) - component_ids)
    ]


class EvolutionJournal:
    def __init__(self, config: EvolutionConfig) -> None:
        self.config = config

    @classmethod
    def open(
        cls,
        root_dir: Path,
        warn: Callable[[str], None],
    ) -> EvolutionJournal | None:
        """The journal for ``root_dir``, or None when it is unusable.

        Every writer asks the same two questions in the same order -
        does the config parse, and is the journal switched on - and does
        the same thing on either No. Asking them here rather than at each
        site is what stops the pair drifting: measured, the four writers
        that existed before #257 disagreed, with two of them omitting the
        parse guard entirely and raising a config typo into work that had
        already been paid for.

        Which exceptions "does not parse" covers is ``load_or_none``'s to
        know, not a call site's. Degrades loudly through ``warn``.
        """
        config = EvolutionConfig.load_or_none(root_dir, warn=warn)
        if config is None or not config.enabled:
            return None
        return cls(config)

    # ------------------------------------------------------------------
    # record_run
    # ------------------------------------------------------------------

    def record_run(
        self,
        run_id: str,
        manifest: Manifest,
        factory_result: FactoryResult,
        usage_by_component: dict[str, dict[str, dict[str, Any]]] | None = None,
        run_usage: dict[str, Any] | None = None,
        failure_signatures: dict[str, list[str]] | None = None,
        fact_utilization: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Record a completed factory run to the journal.

        Writes individual component outcomes as JSONL entries.
        Also appends a summary line to experiments.tsv.

        R3.1: ``usage_by_component`` maps component id -> phase ->
        UsageTotals.to_dict() and lands on each component's journal
        entry; ``run_usage`` is the run-level UsageTotals.to_dict() and
        feeds the TSV totals columns. Both optional so pre-R3.1 callers
        keep working; token/cost figures are CLI self-reports and are
        lower bounds whenever ``unreported_calls`` > 0.

        R6.1: ``failure_signatures`` maps component id -> the structured
        "<check>:<code>" signatures the factory recorded when the
        component's last attempt failed (e.g. "linter:E501",
        "review:scope_creep"). When absent for a failed component, the
        legacy flattened-string classification is the fallback so
        journal entries never lose the signature fields entirely.

        #191: ``fact_utilization`` maps component id -> ``{"measured",
        "injected", "referenced", "reason"}``. The key is written for
        EVERY component, present in the map or not. ``measured=False``
        means the run could not measure, which is not the same as a
        measured ``referenced=0``; reading a missing or false
        ``measured`` as a real zero is what made the L2+
        fact-utilization gate un-evidenceable. Only ``measured=True``
        entries are evidence.
        """
        from kstrl.manifest import ComponentStatus

        timestamp = _timestamp_now()
        usage_by_component = usage_by_component or {}
        failure_signatures = failure_signatures or {}
        fact_utilization = fact_utilization or {}

        # --- JSONL entries per component ---
        entries: list[dict[str, Any]] = []
        for comp in manifest.components:
            has_error = bool(comp.error) and comp.status in (
                ComponentStatus.FAILED.value,
                ComponentStatus.PENDING.value,  # retried components reset to pending
            )
            comp_signatures: list[str] = []
            check_name = ""
            error_sig = ""
            if has_error:
                comp_signatures = list(failure_signatures.get(comp.id) or [])
                if not comp_signatures:
                    # Legacy fallback: classify the flattened string.
                    legacy_check = _classify_check(comp.error)
                    legacy_sig = _normalize_error(comp.error)
                    if legacy_sig:
                        comp_signatures = [f"{legacy_check}:{legacy_sig}"]
                if comp_signatures:
                    check_name, error_sig = split_signature(comp_signatures[0])
            # E3-consume: include typed findings in the journal so
            # downstream aggregations (concern hit-rate, OWASP-bucket
            # frequency, infrastructure_error rate) can query the
            # structured stream directly rather than re-parsing the
            # rendered string.
            findings_serialized = [f.to_dict() for f in comp.findings]
            findings_summary = _summarize_findings(comp.findings)
            entry = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "timestamp": timestamp,
                "run_id": run_id,
                "project": manifest.project_name,
                "component_id": comp.id,
                "event_type": "component_result",
                "status": comp.status,
                "retries": comp.retries,
                "error": comp.error,
                "check_name": check_name,
                "error_signature": error_sig,
                "failure_signatures": comp_signatures,
                "failed_phase": comp.failed_phase,
                "failed_check": comp.failed_check,
                "duration_seconds": comp.duration_seconds,
                "iteration_count": comp.iteration_count,
                "findings": findings_serialized,
                "findings_summary": findings_summary,
                "usage": usage_by_component.get(comp.id, {}),
                # #191: always present, so a pre-#191 journal (key
                # missing) is distinguishable from "measured=false, we
                # could not measure" and from "measured=true,
                # referenced=0", which is real evidence of unused facts.
                "knowledge_utilization": (
                    fact_utilization.get(comp.id) or dict(_UNMEASURED_UTILIZATION)
                ),
            }
            entries.append(entry)

        entries.extend(
            _role_usage_entries(
                usage_by_component,
                manifest=manifest,
                run_id=run_id,
                timestamp=timestamp,
            )
        )

        try:
            self.append_entries(entries)
        except OSError as exc:
            logger.warning(
                "evolution journal write failed (non-fatal): %s: %s",
                self.config.journal_path,
                exc,
            )

        # --- Experiments TSV summary line ---
        total = len(manifest.components)
        completed = len(factory_result.completed)
        failed = len(factory_result.failed)
        skipped = len(factory_result.skipped)

        iteration_counts = [c.iteration_count for c in manifest.components if c.iteration_count > 0]
        avg_iterations = sum(iteration_counts) / len(iteration_counts) if iteration_counts else 0.0

        durations = [c.duration_seconds for c in manifest.components if c.duration_seconds > 0]
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        retry_total = sum(c.retries for c in manifest.components)
        retry_rate = retry_total / total if total > 0 else 0.0

        # Most common failure signature (full "<check>:<code>" form).
        failure_sigs: dict[str, int] = {}
        for comp in manifest.components:
            if comp.status != ComponentStatus.FAILED.value or not comp.error:
                continue
            sigs = list(failure_signatures.get(comp.id) or [])
            if not sigs:
                sigs = [signature_for_error(_classify_check(comp.error), comp.error)]
            for sig in sigs:
                failure_sigs[sig] = failure_sigs.get(sig, 0) + 1
        common_failure = max(failure_sigs, key=failure_sigs.get, default="") if failure_sigs else ""  # type: ignore[arg-type]

        # R3.1 totals columns. Empty string (not 0) when no usage was
        # tracked for the run - zero would misread as "measured, free".
        # unreported_calls > 0 marks the token/cost figures as lower
        # bounds. Files written before R3.1 keep their shorter header;
        # csv.DictReader in get_experiment_trends drops the extra values
        # rather than crashing.
        if run_usage:
            total_tokens_col = str(run_usage.get("total_tokens", ""))
            total_cost_col = str(run_usage.get("cost_usd", ""))
            unreported_col = str(run_usage.get("unreported_calls", ""))
        else:
            total_tokens_col = total_cost_col = unreported_col = ""

        row = (
            f"{run_id}\t{timestamp}\t{manifest.project_name}\t{total}\t"
            f"{completed}\t{failed}\t{skipped}\t{avg_iterations:.2f}\t"
            f"{avg_duration:.1f}\t{retry_rate:.2f}\t{common_failure}\t"
            f"{total_tokens_col}\t{total_cost_col}\t{unreported_col}"
        )

        try:
            self.config.experiments_path.parent.mkdir(parents=True, exist_ok=True)
            needs_header = (
                not self.config.experiments_path.exists()
                or self.config.experiments_path.stat().st_size == 0
            )
            # The other side of the two-sided contract: this is the
            # file get_experiment_trends decodes as utf-8.
            with open(self.config.experiments_path, "a", encoding="utf-8") as f:
                if needs_header:
                    f.write(EXPERIMENTS_HEADER + "\n")
                f.write(row + "\n")
        except OSError as exc:
            logger.warning(
                "experiments.tsv write failed (non-fatal): %s: %s",
                self.config.experiments_path,
                exc,
            )

    # ------------------------------------------------------------------
    # extract_failure_patterns (single run)
    # ------------------------------------------------------------------

    def extract_failure_patterns(
        self,
        manifest: Manifest,
        min_frequency: int = 2,
        signatures_by_component: dict[str, list[str]] | None = None,
    ) -> list[FailurePattern]:
        """Extract recurring failure patterns from a single run.

        Looks at failed/retried components to find common failure
        signatures, grouped by the full "<check>:<code>" signature.
        ``signatures_by_component`` carries the factory's structured
        signatures (R6.1); components absent from it fall back to
        classifying their flattened error string.
        """
        from kstrl.manifest import ComponentStatus

        signatures_by_component = signatures_by_component or {}

        # Collect components that failed or were retried.
        troubled: list[Component] = [
            c
            for c in manifest.components
            if c.status == ComponentStatus.FAILED.value or c.retries > 0
        ]

        if not troubled:
            return []

        # Group by full signature string.
        groups: dict[str, list[str]] = {}
        for comp in troubled:
            if not comp.error:
                continue
            sigs = list(signatures_by_component.get(comp.id) or [])
            if not sigs:
                legacy_check = _classify_check(comp.error)
                legacy_sig = _normalize_error(comp.error)
                if not legacy_sig:
                    continue
                sigs = [f"{legacy_check}:{legacy_sig}"]
            for sig in sigs:
                groups.setdefault(sig, []).append(comp.id)

        total = len(manifest.components)
        patterns: list[FailurePattern] = []
        for full_sig, comp_ids in groups.items():
            if len(comp_ids) < min_frequency:
                continue
            check_name, code = split_signature(full_sig)
            patterns.append(
                FailurePattern(
                    description=(
                        f"{check_name} failure '{code}' in {len(comp_ids)}/{total} components"
                    ),
                    frequency=len(comp_ids),
                    total_components=total,
                    affected_components=comp_ids,
                    check_name=check_name,
                    error_signature=code,
                    category=category_for_check(check_name),
                )
            )

        patterns.sort(key=lambda p: p.frequency, reverse=True)
        return patterns

    # ------------------------------------------------------------------
    # get_cross_run_patterns
    # ------------------------------------------------------------------

    def get_cross_run_patterns(
        self,
        lookback_runs: int = 10,
    ) -> list[FailurePattern]:
        """Get patterns that recur across multiple factory runs.

        Reads the journal, groups entries by their structured failure
        signatures ("<check>:<code>", R6.1), and returns patterns that
        appear in >= min_pattern_frequency distinct runs. Legacy v1
        entries without ``failure_signatures`` fall back to composing
        the signature from their check_name/error_signature fields.
        """
        entries = self._read_journal_entries(lookback_runs)
        if not entries:
            return []

        # Group by full signature across distinct run_ids.
        sig_runs: dict[str, set[str]] = {}
        sig_components: dict[str, list[str]] = {}

        for entry in entries:
            if entry.get("event_type", "component_result") != "component_result":
                continue
            sigs = entry.get("failure_signatures") or []
            if not sigs:
                # v1 fallback: compose from the legacy scalar fields.
                legacy_sig = entry.get("error_signature", "")
                if not legacy_sig:
                    continue
                legacy_check = entry.get("check_name") or "unknown"
                sigs = [f"{legacy_check}:{legacy_sig}"]
            run_id = entry.get("run_id", "")
            comp_id = entry.get("component_id", "")
            for sig in sigs:
                if not isinstance(sig, str) or not sig:
                    continue
                sig_runs.setdefault(sig, set()).add(run_id)
                sig_components.setdefault(sig, []).append(comp_id)

        total_runs = len(
            {
                e.get("run_id")
                for e in entries
                if e.get("event_type", "component_result") == "component_result"
            }
        )
        patterns: list[FailurePattern] = []

        for sig, run_ids in sig_runs.items():
            if len(run_ids) < self.config.min_pattern_frequency:
                continue
            check_name, code = split_signature(sig)
            unique_comps = list(dict.fromkeys(sig_components.get(sig, [])))
            patterns.append(
                FailurePattern(
                    description=(
                        f"'{sig}' appeared in {len(run_ids)}/{total_runs} runs "
                        f"across {len(unique_comps)} components"
                    ),
                    frequency=len(run_ids),
                    total_components=total_runs,
                    affected_components=unique_comps,
                    check_name=check_name,
                    error_signature=code,
                    category=category_for_check(check_name),
                )
            )

        patterns.sort(key=lambda p: p.frequency, reverse=True)
        return patterns

    # ------------------------------------------------------------------
    # propose_improvements
    # ------------------------------------------------------------------

    def propose_improvements(
        self,
        patterns: list[FailurePattern],
        starting_number: int = 1,
    ) -> list[HarnessProposal]:
        """Generate concrete harness improvement proposals from patterns.

        Computational proposals only (no LLM calls):
        - Recurring linter errors - suggest CLAUDE.md convention entry
        - Recurring typecheck patterns - suggest config change
        - Recurring test failures on same module - suggest feedforward focus
        - Recurring review/security finding categories - suggest CLAUDE.md
          guidance derived from the finding taxonomy

        R6.2: IDs are monotonic across runs - pass
        ``next_proposal_number(output_dir)`` as ``starting_number`` so a
        second `ks evolve` continues numbering instead of restarting
        at PROP-001 and clobbering earlier files.
        """
        proposals: list[HarnessProposal] = []
        counter = starting_number - 1

        for pattern in patterns:
            counter += 1
            proposal_id = f"PROP-{counter:03d}"

            if pattern.check_name == "linter":
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=f"Add linter convention for {pattern.error_signature} to CLAUDE.md",
                        description=(
                            f"Linter rule {pattern.error_signature} triggered in "
                            f"{pattern.frequency} components. Adding an explicit convention "
                            f"to CLAUDE.md will help the agent avoid this pattern."
                        ),
                        proposal_type="computational",
                        target="claude_md",
                        suggested_change=(
                            f"Add to CLAUDE.md:\n"
                            f"> Avoid triggering linter rule {pattern.error_signature}. "
                            f"Check the rule in your linter's documentation for the "
                            f"correct pattern."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            elif pattern.check_name == "typecheck":
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=f"Adjust type-checking config for '{pattern.error_signature}'",
                        description=(
                            f"Type error pattern '{pattern.error_signature}' recurred in "
                            f"{pattern.frequency} components. Consider adjusting the type "
                            f"checker's config or adding a CLAUDE.md note about the "
                            f"expected typing style."
                        ),
                        proposal_type="computational",
                        # Not "pyproject": save_proposals writes this
                        # verbatim as "**Target**: ..." into the
                        # proposal file, so a TypeScript project got a
                        # proposal naming a file it does not have,
                        # directly above prose that carefully did not.
                        target="typecheck_config",
                        # Toolchain-neutral prose on purpose. The gate
                        # dispatches per project (#258), so this code can
                        # be a tsc TS-number as easily as a mypy code,
                        # and `check_name` is the GATE, which carries no
                        # toolchain. Naming [tool.mypy] here sent a
                        # TypeScript project to edit a pyproject.toml it
                        # does not have.
                        suggested_change=(
                            f"Review the type checker's configuration. If this is a known "
                            f"false positive, add it to the ignore list. Otherwise add to "
                            f"CLAUDE.md:\n"
                            f"> Ensure all functions have return type annotations to avoid "
                            f"'{pattern.error_signature}'."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            elif pattern.check_name == "test_suite":
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=f"Add feedforward focus for test pattern '{pattern.error_signature}'",
                        description=(
                            f"Test failure '{pattern.error_signature}' hit "
                            f"{pattern.frequency} components: "
                            f"{', '.join(pattern.affected_components[:5])}. "
                            f"Focusing feedforward context on this pattern may help the agent "
                            f"fix the root cause earlier in the iteration loop."
                        ),
                        proposal_type="computational",
                        target="feedforward_config",
                        suggested_change=(
                            f"Add to feedforward config or CLAUDE.md:\n"
                            f"> Known recurring test issue: '{pattern.error_signature}'. "
                            f"When tests fail with this pattern, check the affected modules "
                            f"before re-running."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            elif pattern.check_name == "review":
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=f"Add review guidance for '{pattern.error_signature}'",
                        description=(
                            f"Review finding category '{pattern.error_signature}' "
                            f"(reviewer concern taxonomy) appeared in "
                            f"{pattern.frequency} components. Adding explicit guidance to "
                            f"CLAUDE.md can help the agent avoid this in the first pass."
                        ),
                        proposal_type="computational",
                        target="claude_md",
                        suggested_change=(
                            f"Add to CLAUDE.md:\n"
                            f"> Reviewer repeatedly flags '{pattern.error_signature}'. "
                            f"Address this pattern proactively."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            elif pattern.check_name == "security":
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=(f"Add security guidance for '{pattern.error_signature}'"),
                        description=(
                            f"Security finding category '{pattern.error_signature}' "
                            f"(OWASP-mapped taxonomy) appeared in "
                            f"{pattern.frequency} components. Adding an explicit "
                            f"convention to CLAUDE.md can prevent the vulnerability "
                            f"class from being introduced at all."
                        ),
                        proposal_type="computational",
                        target="claude_md",
                        suggested_change=(
                            f"Add to CLAUDE.md:\n"
                            f"> Security reviewer repeatedly flags "
                            f"'{pattern.error_signature}'. Follow the secure "
                            f"pattern for this category from the start."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            # #294: the one check here that no agent can act on. The
            # generic branch below writes CLAUDE.md advice aimed at the
            # ENGINEER, and a scope the harness could not establish at
            # plan time is not something the engineer can take extra
            # care about. Reaching this arm at all depends on
            # _classify_check recognising the failure text, which is why
            # that function matches the error prefix first.
            elif pattern.check_name == SCOPE_UNREADABLE_CHECK:
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=(
                            f"Repair the component scopes that would not "
                            f"resolve ({pattern.frequency} runs)"
                        ),
                        description=(
                            f"No trustworthy scope could be established for a "
                            f"component in {pattern.frequency} runs, across "
                            f"{', '.join(pattern.affected_components[:5])}. "
                            f"The component is refused before its engineer "
                            f"runs, because the snapshot is fixed for the life "
                            f"of the run. No agent can clear it: the scope is "
                            f"read from the main checkout, outside every "
                            f"worktree."
                        ),
                        proposal_type="computational",
                        target="repository",
                        suggested_change=(
                            "Two faults produce this, and the run's failure "
                            "record says which. A pre-run PRD that would not "
                            "read: check that every component's `prdPath` "
                            "names a readable, parseable file in the main "
                            "checkout, and that decompose is writing it. No "
                            "plan-time scope resolved for the component at "
                            "all: the PRD is fine and the manifest disagrees "
                            "with the resolved run scope, which is a harness "
                            "fault. A run-wide `--allowed-paths` fixes "
                            "neither: scope resolution refuses before it "
                            "reaches the flag."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            else:
                # Generic proposal for unknown/iteration category patterns.
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=f"Investigate recurring failure: {pattern.error_signature}",
                        description=(
                            f"Pattern '{pattern.error_signature}' ({pattern.check_name}) "
                            f"occurred {pattern.frequency} times. Manual investigation "
                            f"recommended."
                        ),
                        proposal_type="computational",
                        target="claude_md",
                        suggested_change=(
                            f"Add to CLAUDE.md:\n"
                            f"> Known issue: '{pattern.error_signature}'. "
                            f"Take extra care with this pattern."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

        return proposals

    # ------------------------------------------------------------------
    # save_proposals
    # ------------------------------------------------------------------

    def next_proposal_number(self, output_dir: Path) -> int:
        """Next monotonic proposal number: max existing PROP number in
        ``output_dir`` plus one (R6.2). 1 when the directory is empty or
        missing."""
        highest = 0
        try:
            candidates = list(output_dir.glob("prop-*.md"))
        except OSError:
            return 1
        for path in candidates:
            m = re.fullmatch(r"prop-(\d+)\.md", path.name)
            if m:
                highest = max(highest, int(m.group(1)))
        return highest + 1

    def save_proposals(
        self,
        proposals: list[HarnessProposal],
        output_dir: Path,
    ) -> list[Path]:
        """Write proposals as markdown files to output_dir.

        Returns list of written file paths. Never overwrites an existing
        proposal file (R6.2): a filename collision means the caller
        numbered the batch wrong (see ``next_proposal_number``), and
        clobbering would silently rewrite audit history - skip and warn
        instead.
        """
        written: list[Path] = []
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "proposal dir creation failed (non-fatal): %s: %s",
                output_dir,
                exc,
            )
            return written

        for proposal in proposals:
            filename = f"{proposal.id.lower()}.md"
            filepath = output_dir / filename

            if filepath.exists():
                logger.warning(
                    "refusing to overwrite existing proposal %s; "
                    "renumber with next_proposal_number()",
                    filepath,
                )
                continue

            sources_block = "\n".join(f"- {s}" for s in proposal.source_patterns)

            content = (
                f"# {proposal.id}: {proposal.title}\n"
                f"\n"
                f"**Type**: {proposal.proposal_type}\n"
                f"**Target**: {proposal.target}\n"
                f"**Source patterns**:\n"
                f"{sources_block}\n"
                f"\n"
                f"## Description\n"
                f"\n"
                f"{proposal.description}\n"
                f"\n"
                f"## Suggested change\n"
                f"\n"
                f"{proposal.suggested_change}\n"
            )

            try:
                # encoding named, ValueError caught: the description
                # and suggested_change come from an LLM, so one curly
                # quote makes this a UnicodeEncodeError under LC_ALL=C,
                # and that is a ValueError, which the OSError handler
                # below does not catch (measured: US-ASCII preferred
                # encoding, write_text raises). A proposal write is
                # explicitly non-fatal; without this it took the run
                # down instead.
                filepath.write_text(content, encoding="utf-8")
                written.append(filepath)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "proposal write failed (non-fatal): %s: %s",
                    filepath,
                    exc,
                )

        return written

    # ------------------------------------------------------------------
    # get_concern_hit_rate (D8)
    # ------------------------------------------------------------------

    def get_concern_hit_rate(self, lookback_runs: int = 10) -> dict[str, Any]:
        """Aggregate reviewer/security finding signal across recent runs.

        Returns ``{"runs": N, "components": M, "with_concern": K,
        "by_category": {...}}`` so dashboards can ask "did the
        adversarial reviewers surface anything across the last N runs?"

        R6.2: consumes the typed ``findings_summary`` that record_run
        writes on every component_result entry (E3 stream), replacing
        the old error-string scan that was structurally zero (concern
        categories never appeared in ``component.error``). A component
        counts as "with concern" when its summary has at least one
        finding in a real category - the synthetic
        ``infrastructure_error`` and ``phase_skipped`` categories mark
        non-execution, not adversarial signal, and are excluded.
        """
        entries = [
            e
            for e in self._read_journal_entries(lookback_runs)
            if e.get("event_type", "component_result") == "component_result"
        ]
        runs = len({e.get("run_id", "") for e in entries})
        components = len(entries)
        with_concern = 0
        by_category: dict[str, int] = {}
        for entry in entries:
            summary = entry.get("findings_summary") or {}
            cat_counts = summary.get("by_category") or {}
            if not isinstance(cat_counts, dict):
                continue
            hit = False
            for category, count in cat_counts.items():
                if category in ("infrastructure_error", "phase_skipped"):
                    continue
                try:
                    n = int(count)
                except (TypeError, ValueError):
                    continue
                if n <= 0:
                    continue
                hit = True
                by_category[category] = by_category.get(category, 0) + n
            if hit:
                with_concern += 1
        return {
            "runs": runs,
            "components": components,
            "with_concern": with_concern,
            "by_category": by_category,
        }

    def get_fact_utilization(self, lookback_runs: int = 10) -> dict[str, Any]:
        """Aggregate knowledge fact-utilization across recent runs (#191).

        Returns ``{"runs", "components", "measured", "unmeasured",
        "injected", "referenced", "runs_with_referenced"}``.

        Only ``measured=True`` entries contribute to ``injected`` and
        ``referenced``. An unmeasured component is counted under
        ``unmeasured`` and NEVER as a zero - that conflation is the
        defect this field exists to prevent. Entries written before
        #191 have no ``knowledge_utilization`` key and count as
        unmeasured.

        This is the query behind the L2+ cATO gate in
        ``docs/remediation-roadmap.md``: "two real factory runs with
        nonzero fact-utilization" is ``runs_with_referenced >= 2``.
        ``injected``/``referenced`` are lower bounds - see
        ``knowledge.measure_fact_utilization``.
        """
        entries = [
            e
            for e in self._read_journal_entries(lookback_runs)
            if e.get("event_type", "component_result") == "component_result"
        ]
        measured = unmeasured = 0
        injected = referenced = 0
        runs_with_referenced: set[str] = set()
        for entry in entries:
            util = entry.get("knowledge_utilization")
            if not isinstance(util, dict) or not util.get("measured"):
                unmeasured += 1
                continue
            try:
                n_injected = int(util.get("injected", 0))
                n_referenced = int(util.get("referenced", 0))
            except (TypeError, ValueError):
                # Present but unreadable is not evidence either.
                unmeasured += 1
                continue
            measured += 1
            injected += n_injected
            referenced += n_referenced
            if n_referenced > 0:
                runs_with_referenced.add(str(entry.get("run_id", "")))
        return {
            "runs": len({e.get("run_id", "") for e in entries}),
            "components": len(entries),
            "measured": measured,
            "unmeasured": unmeasured,
            "injected": injected,
            "referenced": referenced,
            "runs_with_referenced": len(runs_with_referenced),
        }

    # ------------------------------------------------------------------
    # get_experiment_trends
    # ------------------------------------------------------------------

    def get_experiment_trends(self, last_n: int = 10) -> list[dict[str, Any]]:
        """Read experiments.tsv and return the last N entries as dicts.

        Encoding is named and ``ValueError`` is caught beside
        ``OSError``, which is the house rule for a reader of any file
        kstrl writes (CLAUDE.md): ``UnicodeDecodeError`` IS a
        ``ValueError`` and escapes a fail-closed ``except OSError``.
        Measured before this line: a single non-utf-8 byte in
        experiments.tsv raised straight out of ``EvolveScreen.on_mount``
        two lines after the #289 config banner, which is that issue's
        own crash from that issue's own screen.
        """
        try:
            text = self.config.experiments_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return []

        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        rows = list(reader)
        return rows[-last_n:]

    # ------------------------------------------------------------------
    # get_repair_count
    # ------------------------------------------------------------------

    def get_repair_count(self) -> int:
        """How many interrupted writes this journal has been repaired from.

        The read surface for ``JOURNAL_REPAIR_EVENT`` (#327 round 1,
        F5). Writing the row was only half of "if it's worth deciding,
        it's worth recording": a row no command reports is reachable
        only by an operator who already suspects the problem, and the
        logger warning goes to orchestrator.log under the TUI. ``ks
        evolve --status`` prints this when it is non-zero.

        Counts rows, not incidents: :meth:`append_entries` residual 2
        is how one tear can produce two, and residual 4 is how a repair
        can happen and not be counted, so this is a lower bound. A
        non-zero count means at least one crash left an unterminated
        tail. It does NOT mean a record was lost: the line above each
        row is either a fragment that was never readable or a whole
        record that lost only its newline and is readable again, which
        is the distinction ``docs/evolution-metrics.md`` and the status
        line both draw.
        """
        return sum(
            1 for e in self._read_all_entries() if e.get("event_type") == JOURNAL_REPAIR_EVENT
        )

    # ------------------------------------------------------------------
    # get_spec_audits / get_spec_issue_runs
    # ------------------------------------------------------------------

    def get_spec_audits(self) -> list[dict[str, Any]]:
        """Every recorded spec audit in the journal, oldest first (#314).

        The whole set, across every project, because the caller that
        accounts for the history a windowed trend leaves out needs
        exactly what the window drops: filtering by project here would
        hide the thing it is asking for.

        This is the read surface a caller uses INSTEAD of opening
        ``config.journal_path`` for itself. The difference is not
        cosmetic: if the journal ever compacts, rotates or gains a
        second segment, this method is what changes, while a caller
        holding the path would quietly return less than the journal
        holds - and silent loss of the excluded-history accounting is
        the defect #280 exists to fix.

        Deliberately NOT routed through :meth:`_read_journal_entries`:
        that reader keeps only entries whose ``run_id`` is among the
        last N distinct run ids, and a spec audit carries no ``run_id``
        at all (decompose runs before a factory run id exists), so
        every one of them is dropped there. Reading the raw entries is
        what makes the architect's own history readable.
        """
        return [e for e in self._read_all_entries() if e.get("event_type") == SPEC_ISSUES_EVENT]

    def get_spec_issue_runs(
        self,
        project: str,
        last_n: int = 10,
        audits: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """The last N recorded spec audits for ``project``, oldest first (#260).

        The one place the window rule lives (#314). ``decompose`` had a
        second copy of it, and two copies of one rule can drift: if
        they had, the convergence trend and the accounting printed
        under it would have disagreed about the same journal.

        ``last_n`` counts spec audits, not factory runs - a spec audit
        happens once per decompose, whether or not a factory run
        follows. Windowed here rather than by the caller, matching
        :meth:`get_experiment_trends`.

        ``audits`` lets a caller that has already called
        :meth:`get_spec_audits` window that snapshot instead of reading
        the file a second time, so the window and any accounting over
        the same entries cannot disagree because the file moved between
        two reads. The event-type filter is applied either way, so
        passing raw entries answers the same as passing audits.

        Nothing is assumed about an entry beyond it being a JSON
        object, so journals written by older versions read cleanly. A
        project is matched by :func:`entry_str`, so a null or
        non-string ``project`` field is an unattributed audit rather
        than a project named "None".
        """
        source = self.get_spec_audits() if audits is None else audits
        runs = [
            entry
            for entry in source
            if entry.get("event_type") == SPEC_ISSUES_EVENT
            and entry_str(entry, "project") == project
        ]
        return runs[-last_n:] if last_n > 0 else []

    # ------------------------------------------------------------------
    # append_entries
    # ------------------------------------------------------------------

    def append_entries(self, entries: list[dict[str, Any]]) -> None:
        """Append entries to the journal in JSONL form.

        The one writer of the journal's line format, enforced by
        ``tests/test_journal_one_writer.py``. Raises ``OSError`` rather
        than handling it, because the three callers surface a failed
        write differently: :meth:`record_run` logs it, decompose warns
        through the run's UI, and ``autonomy.commit_transition`` warns.

        #312: a crash mid-write leaves a tail with no newline, and an
        append onto that tail concatenates the two into one unparseable
        line, so the tolerant reader drops the NEW entry as well. The
        cost is measured, not assumed, and it is not always one entry: a
        tail that lost only its newline is a COMPLETE record, and
        concatenating onto it destroys that record too. Writing a
        newline first repairs the tail into a line of its own, which
        drops a genuine fragment (unavoidable, it was never written) and
        RECOVERS a record that lost only its terminator.

        Healing forward rather than raising, because the caller is a
        record-keeper: refusing to append would answer the loss of one
        record by losing every later one. So the repair is recorded
        instead, twice, per "if it's worth deciding, it's worth
        recording" - a ``JOURNAL_REPAIR_EVENT`` row in the file itself,
        which is what ``ks evolve --status`` counts and an operator
        greps months later, and a warning on this module's logger for
        whoever is watching now. The row is durable where the log line
        is not: the process that tore the file is exactly the process
        whose stderr nobody kept.

        An empty append writes nothing, so it repairs nothing: there is
        no entry to protect and the next real append will do it.

        ONE file description does the probe and the append, in
        ``"a+b"``, and the repair row plus the whole batch go in ONE
        ``write``. Neither is an optimisation. The single description is
        what removes the window in which the path could be replaced or a
        symlink retargeted between the two, and what makes a journal
        this process cannot READ raise out of the open rather than being
        probed as "not torn" and appended to blind; the single write is
        what stops another appender landing between the newline that
        isolates a torn fragment and the entries the repair was for. It
        costs the text-mode ``encoding="utf-8"``, so the bytes are
        encoded explicitly instead, which is the same two-sided contract
        stated at the other end.

        Both are enforced by ``tests/test_journal_write_boundary.py``,
        which counts descriptors and writes. Round 2 of review on #327
        found that neither was, and the measurement here agrees: a
        version that reopens the file for the append, and a version
        that writes the newline, the marker and the batch separately,
        each pass 284 tests and 1 xfail across
        ``test_journal_torn_tail``, ``test_journal_one_writer``,
        ``test_decompose``, ``test_autonomy_ladder`` and
        ``test_config_control_plane``. An argument in a docstring is
        not a mechanism.

        WHAT IS STILL NOT ATOMIC, precisely, because a docstring that
        implied otherwise would be worse than no docstring. This takes
        no lock, and #330 tracks that:

        1. Between this process's tail read and its write, another
           process can append. If that other write is a complete line,
           nothing is lost. If it crashed mid-line inside that window,
           this append lands on the fragment and the pair is unreadable
           - the #312 outcome, in the narrower window.
        2. Two processes repairing one tear each write a newline and a
           repair row, so a single incident can be recorded twice. Both
           the blank line and the extra row are skipped by every reader
           and counted by none.
        3. O_APPEND makes each ``write`` land at the end, and the repair
           row plus the whole batch go in ONE ``write`` so that another
           appender cannot land between them. That is not a guarantee,
           but not for the reason it is tempting to write down. Measured
           on this interpreter: ``BufferedWriter`` hands a payload of
           ANY size to the raw layer in one ``write(2)`` (100 bytes to
           5 MB, one raw call each), so the split is not size-driven and
           ``io.DEFAULT_BUFFER_SIZE`` is not the threshold. It loops
           only when the OS returns a SHORT write, which on a regular
           file means a signal or ENOSPC. Rare, not impossible, and it
           predates this change.
        4. The repair is not two-phase-safe either. There is no gap
           BETWEEN two calls, because there is only one call: the
           newline that isolates the fragment, the marker and the batch
           are one ``write``. What is left is a partial write INSIDE it,
           which is residual 3's short write, landing the newline and
           not the marker. That leaves a file that is terminated,
           malformed one line up and carrying no repair row, and the
           next append reads the last BYTE, finds a newline and adds
           none. Nothing is at risk by then - the fragment is isolated,
           which was the point - so this is an audit gap, not data loss,
           and the isolated fragment line is still on disk to be read.
           Closing it means parsing the last LINE on every append, which
           would fire on any malformed tail rather than a torn one: a
           different contract, not a bug fix.
           ``test_a_terminated_but_malformed_tail_is_not_a_tear`` pins
           it, so it cannot quietly stop being true.

        Neither 1 nor 3 is made worse by the probe: at cbdff7c the same
        two writers produced the same interleaving with no probe at all.
        """
        path = self.config.journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+b") as handle:
            repairing = bool(entries) and handle_ends_without_newline(handle)
            payload = "".join(_journal_line(entry) for entry in entries)
            if repairing:
                payload = "\n" + _journal_line(_repair_entry()) + payload
            handle.write(payload.encode("utf-8"))
        if repairing:
            logger.warning(
                "evolution journal did not end in a newline, so a crash tore it: "
                "%s. A newline and a %s row were written before this append, so "
                "the unterminated tail cannot swallow the entries after it.",
                path,
                JOURNAL_REPAIR_EVENT,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_all_entries(self) -> list[dict[str, Any]]:
        """Every well-formed JSON object in the journal, in file order.

        Delegates to ``observability.read_progress_events``: the
        journal and the progress log are the same JSONL-of-objects
        convention, and the tolerant-read policy (missing file, blank
        line, torn line, non-object line - all skipped) should be one
        policy rather than two. One unreadable line must not cost the
        reader the rest of the history.
        """
        return read_progress_events(self.config.journal_path)

    def _read_journal_entries(self, lookback_runs: int = 10) -> list[dict[str, Any]]:
        """Read JSONL journal and return entries from the last N distinct runs.

        Entries without a ``run_id`` are dropped, because the window is
        defined in terms of runs. Spec audits are exactly that case;
        :meth:`get_spec_audits` reads those instead.
        """
        entries = self._read_all_entries()
        if not entries:
            return []

        # Determine the last N distinct run_ids (preserving order of appearance).
        seen_runs: list[str] = []
        seen_set: set[str] = set()
        for entry in reversed(entries):
            rid = entry.get("run_id", "")
            if rid and rid not in seen_set:
                seen_set.add(rid)
                seen_runs.append(rid)
            if len(seen_runs) >= lookback_runs:
                break

        allowed = set(seen_runs)
        return [e for e in entries if e.get("run_id", "") in allowed]
