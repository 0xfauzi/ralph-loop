"""E3: structured Finding type for adversarial role outputs.

Before this module, review/security findings funneled through
``Component.review_findings`` as a single rendered string. Downstream
consumers (dashboards, evolution journal, retry-context builder) had to
re-parse that string to recover structure. Strings drift, parsers break,
the typed information is gone.

``Finding`` is the source-of-truth representation. The rendered string at
``Component.review_findings`` is now a **derived view** kept for backward
compatibility with manifest.json readers and PR body rendering. New code
should consume ``Component.findings`` directly.

Phase tag examples: ``"review"`` (Phase 2 reviewer), ``"security"`` (Phase
2.5 security reviewer). Severity uses each role's native taxonomy
(``critical/high/medium/low`` for security, ``fail/advisory`` for review).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

_INFRASTRUCTURE_CATEGORY = "infrastructure_error"
_PHASE_SKIPPED_CATEGORY = "phase_skipped"
# R8.1: policy-envelope violations. Mechanical (no LLM), so these carry
# no model tag - the "reviewer" is the envelope itself.
POLICY_CATEGORY_PREFIX = "policy_"
# R8.5: test-suite adequacy findings. Mechanical like the policy family,
# so no model tag - the gate, not an LLM, decided.
ADEQUACY_CATEGORY_PREFIX = "adequacy_"

# #265: the across-attempt divergence detector tripped - the change kept
# growing while the reviewer's blocking findings never became a proper
# subset of the previous attempt's. Mechanical like the policy and
# adequacy families, so no model tag: the predicate decided, not an LLM.
DIVERGENCE_CATEGORY = "review_divergence"

# R10.3: one story the engineer marked passes=true that the reviewer did
# not independently mark pass. The comparison is mechanical, but the
# evidence is an LLM's verdict, so unlike the policy and adequacy
# families these findings DO carry a model tag: which reviewer
# disagreed is the fact worth attributing.
SETPOINT_DISAGREEMENT_CATEGORY = "setpoint_disagreement"

# R3.3: every finding the factory records is tagged with the attempt
# that produced it, so the journal can distinguish superseded findings
# (an attempt that was retried) from the shipped attempt's findings.
ATTEMPT_TAG_PREFIX = "attempt:"

# R7.1: every finding a reviewer produces is tagged with the reviewing
# model identity (e.g. "model:codex (gpt-5)"), so the journal and PR
# body can attribute each finding to the model family that raised it -
# the measurement substrate for the same-family vs cross-family
# correlated-miss comparison. Findings recorded when NO reviewer ran
# (phase_skipped, pre-agent budget failures) carry no model tag: there
# is no reviewing model to attribute them to.
MODEL_TAG_PREFIX = "model:"


@dataclass(frozen=True)
class Finding:
    """One adversarial finding produced by a Phase 2 or 2.5 role.

    Frozen so a list[Finding] is safe to share across threads / pickling
    boundaries; the factory's ProcessPoolExecutor path (Phase C1) needs
    structurally-comparable items.
    """

    phase: str
    category: str
    severity: str
    location: str
    explanation: str
    suggestion: str = ""
    owasp: str = ""
    cwe: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_infrastructure_error(self) -> bool:
        """True when this finding represents a failed role run, not a
        real finding. ``len(findings) == 0`` then means "the role ran
        cleanly"; ``[f for f in findings if not f.is_infrastructure_error]``
        gives the verified-clean subset (E3-infra)."""
        return self.category == _INFRASTRUCTURE_CATEGORY

    @property
    def is_phase_skip(self) -> bool:
        """True when this finding records that a phase never executed
        (mode=skip, budget exhausted). Deliberately NOT an
        infrastructure error: nothing broke, the phase was skipped on
        purpose. But like infra errors it marks non-execution, so
        ``len(findings) == 0`` keeps meaning "every phase ran and found
        nothing" (R1.2)."""
        return self.category == _PHASE_SKIPPED_CATEGORY

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "category": self.category,
            "severity": self.severity,
            "location": self.location,
            "explanation": self.explanation,
            "suggestion": self.suggestion,
            "owasp": self.owasp,
            "cwe": self.cwe,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        tags_raw = data.get("tags", []) or []
        if not isinstance(tags_raw, (list, tuple)):
            tags_raw = []
        return cls(
            phase=str(data.get("phase", "")),
            category=str(data.get("category", "")),
            severity=str(data.get("severity", "")),
            location=str(data.get("location", "")),
            explanation=str(data.get("explanation", "")),
            suggestion=str(data.get("suggestion", "")),
            owasp=str(data.get("owasp", "")),
            cwe=str(data.get("cwe", "")),
            tags=tuple(str(t) for t in tags_raw),
        )

    @classmethod
    def infrastructure_error(cls, phase: str, explanation: str) -> Finding:
        """Build a synthetic Finding marking that the given role failed
        to execute (timeout, parse failure, agent crash) or was never
        started, for example because the adversarial call budget was
        spent before a hard-mode phase could run. Treated by the
        factory as a critical-severity blocker in hard mode (E9 already
        produces this signal via the result-level flag; this lifts it
        into the typed findings stream so `len(findings)==0` is a safe
        proxy for "the role ran cleanly")."""
        return cls(
            phase=phase,
            category=_INFRASTRUCTURE_CATEGORY,
            severity="critical",
            location="",
            explanation=explanation,
            tags=("infrastructure",),
        )

    @classmethod
    def policy_violation(
        cls,
        category: str,
        explanation: str,
        location: str = "",
        severity: str = "high",
        suggestion: str = "",
    ) -> Finding:
        """Build a Finding for an R8.1 policy-envelope violation.

        ``category`` is the envelope rule that fired (``paths_deny``,
        ``max_files_changed``, ``license_denied``, ...) and is stored
        prefixed (``policy_paths_deny``) so policy findings are greppable
        as a family and cannot collide with a reviewer concern of the
        same name. Severity is ``critical`` for the non-overridable
        enforcement-machinery halt, ``high`` for other blocking
        violations, ``advisory`` for non-blocking notices.

        Mechanical, so no model tag: the envelope, not an LLM, decided.
        """
        return cls(
            phase="policy",
            category=f"{POLICY_CATEGORY_PREFIX}{category}",
            severity=severity,
            location=location,
            explanation=explanation,
            suggestion=suggestion,
            tags=("policy", f"policy:{category}"),
        )

    @classmethod
    def adequacy_finding(
        cls,
        category: str,
        explanation: str,
        location: str = "",
        severity: str = "advisory",
        suggestion: str = "",
    ) -> Finding:
        """Build a Finding for an R8.5 test-adequacy concern.

        Severity defaults to ``advisory`` because the gate lands
        advisory-first: these layers are judged against thresholds that
        have not been measured yet, and a gate that blocks on an invented
        number teaches people to switch gates off.
        """
        return cls(
            phase="adequacy",
            category=f"{ADEQUACY_CATEGORY_PREFIX}{category}",
            severity=severity,
            location=location,
            explanation=explanation,
            suggestion=suggestion,
            tags=("adequacy", f"adequacy:{category}"),
        )

    @classmethod
    def divergence(cls, explanation: str, severity: str = "advisory") -> Finding:
        """Build a Finding for a #265 across-attempt divergence trip.

        Severity defaults to ``advisory`` and matches the gate's mode:
        the predicate is mechanical, but its retirement half reconstructs
        a finding's identity from what the reviewer wrote, and that
        heuristic's false-positive rate has not been measured on real
        runs. Advisory mode is what produces the evidence a graduation to
        ``fail`` would rest on.

        Mechanical, so no model tag: the predicate decided, not an LLM.
        """
        return cls(
            phase="review",
            category=DIVERGENCE_CATEGORY,
            severity=severity,
            location="",
            explanation=explanation,
            tags=("divergence", "phase:review"),
        )

    @classmethod
    def phase_skipped(cls, phase: str, reason: str) -> Finding:
        """Build a synthetic Finding recording that the given phase was
        deliberately not executed (mode=skip or adversarial budget
        exhausted). Severity "skipped" keeps it out of every real
        severity bucket; the ``non_execution`` tag is the machine-
        readable marker (R1.2)."""
        return cls(
            phase=phase,
            category=_PHASE_SKIPPED_CATEGORY,
            severity="skipped",
            location="",
            explanation=reason,
            tags=("non_execution", f"phase:{phase}"),
        )

    @classmethod
    def from_review_concern(
        cls,
        category: str,
        severity: str,
        location: str,
        explanation: str,
        suggestion: str = "",
    ) -> Finding:
        """Construct a review-phase Finding with consistent tagging
        (`phase:review`, `category:<X>`). Centralizes the tag format so
        all consumers can rely on it."""
        return cls(
            phase="review",
            category=category,
            severity=severity,
            location=location,
            explanation=explanation,
            suggestion=suggestion,
            tags=("phase:review", f"category:{category}"),
        )

    @classmethod
    def from_security_finding(
        cls,
        category: str,
        severity: str,
        location: str,
        explanation: str,
        suggestion: str,
        owasp: str,
        cwe: str,
    ) -> Finding:
        """Construct a security-phase Finding with OWASP/CWE tags
        populated. Tag set: ``phase:security``, ``category:<X>``, and
        when present ``owasp:<bucket>``, ``cwe:<id>``. Lets downstream
        consumers filter by taxonomy without re-parsing the owasp/cwe
        string fields."""
        tags: list[str] = ["phase:security", f"category:{category}"]
        if owasp:
            tags.append(f"owasp:{owasp}")
        if cwe:
            tags.append(f"cwe:{cwe}")
        return cls(
            phase="security",
            category=category,
            severity=severity,
            location=location,
            explanation=explanation,
            suggestion=suggestion,
            owasp=owasp,
            cwe=cwe,
            tags=tuple(tags),
        )


def tag_finding_with_attempt(finding: Finding, attempt: int) -> Finding:
    """Return a copy of *finding* tagged ``attempt:<n>`` (R3.3).

    Idempotent: a finding already carrying an attempt tag is returned
    unchanged, so re-tagging on a resumed manifest cannot stack
    conflicting attempt numbers."""
    if any(t.startswith(ATTEMPT_TAG_PREFIX) for t in finding.tags):
        return finding
    return replace(
        finding,
        tags=finding.tags + (f"{ATTEMPT_TAG_PREFIX}{attempt}",),
    )


def tag_finding_with_model(finding: Finding, model_id: str) -> Finding:
    """Return a copy of *finding* tagged ``model:<id>`` (R7.1).

    Idempotent: a finding already carrying a model tag is returned
    unchanged, so resumed manifests cannot
    stack conflicting identities. An empty ``model_id`` is a no-op -
    tagging with an unknown identity would be a fabricated claim."""
    if not model_id:
        return finding
    if any(t.startswith(MODEL_TAG_PREFIX) for t in finding.tags):
        return finding
    return replace(
        finding,
        tags=finding.tags + (f"{MODEL_TAG_PREFIX}{model_id}",),
    )


def finding_model(finding: Finding) -> str | None:
    """The reviewing model identity a finding was recorded under, or
    None for findings that predate R7.1 model tagging (or were recorded
    without a reviewer having run)."""
    for tag in finding.tags:
        if tag.startswith(MODEL_TAG_PREFIX):
            return tag[len(MODEL_TAG_PREFIX) :] or None
    return None


def finding_attempt(finding: Finding) -> int | None:
    """The attempt number a finding was recorded on, or None for
    findings that predate the R3.3 attempt tagging."""
    for tag in finding.tags:
        if tag.startswith(ATTEMPT_TAG_PREFIX):
            try:
                return int(tag[len(ATTEMPT_TAG_PREFIX) :])
            except ValueError:
                return None
    return None


def dump_raw_debug(
    debug_dir: Path | None,
    phase: str,
    raw_output: str,
    label: str,
) -> str | None:
    """Persist the FULL raw agent output of a failed parse so the
    forensic tail survives (R1.2; mirrors knowledge.py's
    ``_distill_raw.txt`` pattern). The in-memory result keeps only a
    2000-char sample to bound manifest/journal size; this dump is where
    the rest lives.

    Best-effort: returns the written path as a string, or None when
    ``debug_dir`` is None or the write failed. Never raises."""
    if debug_dir is None:
        return None
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        raw_path = debug_dir / f"_{phase}_raw.txt"
        raw_path.write_text(raw_output, encoding="utf-8")
        (debug_dir / f"_{phase}_status.txt").write_text(
            label,
            encoding="utf-8",
        )
        return str(raw_path)
    except OSError:
        return None


def render_findings_markdown(findings: list[Finding]) -> str:
    """Render a list[Finding] as a markdown section. Returns empty
    string for empty input.

    Intended for ad-hoc dumping, debugging, and downstream consumers
    that prefer a typed-list rendering. NOT used by the canonical PR
    body builder: ``pr.py`` keeps the legacy ``review_findings`` string
    because it carries information the typed Finding stream does not
    (PASS criteria, pass/fail/advisory counts, criterion text as
    headers). See the comment in ``pr.py::build_pr_body`` for the
    full rationale.

    The output is grouped by phase and includes infrastructure-error
    callouts so the reader can distinguish "no findings" (good) from
    "review never ran" (must investigate)."""
    if not findings:
        return ""
    by_phase: dict[str, list[Finding]] = {}
    for f in findings:
        by_phase.setdefault(f.phase, []).append(f)

    lines: list[str] = ["## Adversarial Findings", ""]
    for phase in sorted(by_phase):
        items = by_phase[phase]
        infra = [f for f in items if f.is_infrastructure_error]
        skipped = [f for f in items if f.is_phase_skip]
        real = [f for f in items if not f.is_infrastructure_error and not f.is_phase_skip]

        lines.append(f"### {phase.capitalize()} ({len(real)} findings)")
        lines.append("")
        # R7.1: attribute the phase's findings to the model(s) that
        # produced them so a PR reader can see who reviewed whom.
        models = sorted({m for m in (finding_model(f) for f in items) if m})
        if models:
            lines.append(f"Reviewer model: {', '.join(models)}")
            lines.append("")
        if infra:
            for f in infra:
                lines.append(
                    f"- **INFRASTRUCTURE ERROR**: {f.explanation} "
                    "(this role did not actually run -- the PR is unverified for this phase)"
                )
            lines.append("")
        if skipped:
            for f in skipped:
                lines.append(
                    f"- **PHASE SKIPPED**: {f.explanation} (this role did not run for this PR)"
                )
            lines.append("")
        for f in real:
            loc = f" at `{f.location}`" if f.location else ""
            tax = ""
            if f.owasp or f.cwe:
                pieces = [p for p in (f.owasp, f.cwe) if p]
                tax = f" [{', '.join(pieces)}]"
            lines.append(f"- [{f.severity}] **{f.category}**{loc}{tax}")
            lines.append(f"  - {f.explanation}")
            if f.suggestion:
                lines.append(f"  - Suggestion: {f.suggestion}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
