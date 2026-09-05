"""Calibration tooling: baseline report format, comparison, and thresholds (R5.1/R5.5).

The calibration suite (``tests/test_calibration.py``) measures whether each
adversarial role catches its planted bugs. This module is the non-LLM half of
that loop:

- **Report format (v2)**: N runs per fixture with per-fixture consistency
  (fraction of runs that detected the planted issue), per-role and
  per-category detection rates, and the model id. ``build_report`` /
  ``save_report`` are the single source of the on-disk shape under
  ``tests/adversarial_fixtures/_results/``.
- **Comparison**: ``python -m kstrl.calibration compare <old.json> <new.json>``
  diffs two baseline files (v1 or v2) and applies the codified thresholds
  below. Exit code 0 = no regression, 1 = regression, 2 = usage/load error.
  This is what H2's "compare against the baseline" concretely means.
  ``--root`` points it at a project so a regression also reaches the
  autonomy ladder; the consequences live in ``kstrl.calibration_ladder``
  and change no exit code except by adding 2 for an unloadable config.
- **Kind synonyms**: the architect matcher accepts documented paraphrases of
  the spec-issue taxonomy (see ``KIND_SYNONYM_GROUPS``) so a planted issue
  reported under a sibling label is a hit, not a miss.
- **Model drift (R5.5, H2-extended)**: ``model_drift_message`` compares the
  configured calibration model against the newest recorded baseline's model
  so the always-run structural test can warn that a re-calibration is due.
  H2 extended: calibration re-runs on model change, not just prompt change.

Detection-rate semantics: a fixture's *consistency* is
``runs_detected / runs_completed`` (agent-infrastructure errors are excluded
from the denominator; unparseable model output counts as a completed miss).
A fixture is *detected* when its consistency reaches
``FIXTURE_DETECTION_THRESHOLD`` (majority of completed runs). A role's
*detection_rate* is the mean consistency across its fixtures - the expected
single-run detection probability, which is what the thresholds gate on.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Codified thresholds (R5.1) - the single source of truth for what counts as
# a calibration regression. Documented in docs/adversarial-design.md.
#
# Sizing rationale (fixture counts as of R5.1: security 5, reviewer 3,
# architect 3, architect_allowed_paths 1; default 3 runs per fixture):
#
# - FIXTURE_DETECTION_THRESHOLD = 0.5: a fixture counts as detected when a
#   majority of its completed runs caught the planted issue (2 of 3 at the
#   default run count). One flaky miss does not fail the suite; a fixture
#   that misses most runs does.
# - MAX_ROLE_DETECTION_DROP = 0.15: for a 3-fixture role at 3 runs, one
#   run flipping is a drop of 1/9 ~ 0.11 (allowed as run-to-run variance);
#   an entire fixture going dark is 1/3 ~ 0.33 (fails). For the 5-fixture
#   security role: run flip 0.07 allowed, fixture loss 0.20 fails.
# - MAX_CATEGORY_DETECTION_DROP = 0.40: categories often hold a single
#   fixture, so at 3 runs one run flip is 0.33 (allowed) and two flips are
#   0.67 (fails). Single-run (v1) baselines make any category flip a 1.0
#   drop - category gating is only meaningful at N >= 3, which is why
#   DEFAULT_CALIBRATION_RUNS is 3.
# - MIN_ROLE_DETECTION_RATE: absolute floors on the NEW baseline,
#   independent of the old one, so a slow multi-comparison slide cannot
#   ratchet a role to zero. security 0.80 = at most one of five fixtures
#   lost; reviewer/architect 0.65 = at least two of three caught.
# ---------------------------------------------------------------------------

DEFAULT_CALIBRATION_RUNS = 3
FIXTURE_DETECTION_THRESHOLD = 0.5
MAX_ROLE_DETECTION_DROP = 0.15
MAX_CATEGORY_DETECTION_DROP = 0.40
MIN_ROLE_DETECTION_RATE: dict[str, float] = {
    "security": 0.80,
    "reviewer": 0.65,
    "architect": 0.65,
    "architect_allowed_paths": 0.50,
}
DEFAULT_MIN_ROLE_DETECTION_RATE = 0.50

REPORT_FORMAT_VERSION = 2

# ---------------------------------------------------------------------------
# Spec-issue kind synonyms (R5.1 matcher fix).
#
# DECOMPOSE_PROMPT's taxonomy separates ambiguity / missing_detail /
# contradiction / unstated_assumption / undefined_failure_mode /
# out_of_scope_creep / other, but the boundary inside the "the spec is
# silent about X" family is a judgment call the model makes differently
# run to run: "no audit log requirement" is simultaneously a missing
# detail, an unstated assumption that auditing is not needed, and an
# undefined failure mode. Every architect miss in the recorded baselines
# (baseline-20260527-161822 spec-01, baseline-20260527-191337 and
# baseline-20260527-195157 spec-02) is exactly this artifact: the planted
# issue WAS reported, under a sibling label (missing_detail instead of
# undefined_failure_mode / unstated_assumption).
#
# Groups are symmetric: a required kind is satisfied by any member of its
# group. Kinds outside any group (ambiguity, contradiction,
# out_of_scope_creep, other) only match themselves - ambiguity is about
# vague language that IS present, not absence, and stays distinct.
# Exact-label matching is still reported (``exact_kinds_present``) as a
# non-gating signal so taxonomy drift stays visible in run details.
# ---------------------------------------------------------------------------

KIND_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"missing_detail", "unstated_assumption", "undefined_failure_mode"}),
)


def acceptable_kinds(required_kind: str) -> frozenset[str]:
    """Return the set of emitted kinds that satisfy ``required_kind``."""
    for group in KIND_SYNONYM_GROUPS:
        if required_kind in group:
            return group
    return frozenset({required_kind})


def required_kinds_satisfied(
    required: Iterable[str],
    actual: Iterable[str],
) -> bool:
    """True when every required kind is present in ``actual`` up to synonyms."""
    actual_set = set(actual)
    return all(acceptable_kinds(kind) & actual_set for kind in required)


def exact_kinds_present(required: Iterable[str], actual: Iterable[str]) -> bool:
    """True when every required kind is present under its exact label."""
    return set(required).issubset(set(actual))


# ---------------------------------------------------------------------------
# Consistency math
# ---------------------------------------------------------------------------


def consistency(runs_detected: int, runs_completed: int) -> float:
    """Fraction of completed runs that detected the planted issue.

    Zero completed runs (every run hit an agent-infrastructure error)
    yields 0.0 - the caller is expected to have skipped such fixtures
    before they reach a gate.
    """
    if runs_completed <= 0:
        return 0.0
    return runs_detected / runs_completed


@dataclass(frozen=True)
class FixtureStats:
    """Aggregated result of running one fixture N times."""

    role: str
    fixture_id: str
    category: str | None
    cwe: str | None
    runs_total: int
    runs_errored: int
    runs_detected: int

    @property
    def runs_completed(self) -> int:
        return self.runs_total - self.runs_errored

    @property
    def consistency(self) -> float:
        return consistency(self.runs_detected, self.runs_completed)

    @property
    def detected(self) -> bool:
        return self.runs_completed > 0 and self.consistency >= FIXTURE_DETECTION_THRESHOLD


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def role_detection_rate(fixtures: Sequence[FixtureStats]) -> float:
    """Mean per-fixture consistency: expected single-run detection rate."""
    return _mean([f.consistency for f in fixtures])


# ---------------------------------------------------------------------------
# Baseline report: build / save / load
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Baseline:
    """A parsed baseline file, normalized across format versions."""

    path: Path | None
    model: str
    timestamp: str
    format_version: int
    runs_per_fixture: int
    fixtures: tuple[FixtureStats, ...]

    def roles(self) -> dict[str, list[FixtureStats]]:
        grouped: dict[str, list[FixtureStats]] = {}
        for fixture in self.fixtures:
            grouped.setdefault(fixture.role, []).append(fixture)
        return grouped

    def role_rates(self) -> dict[str, float]:
        return {role: role_detection_rate(fixtures) for role, fixtures in self.roles().items()}

    def category_rates(self) -> dict[str, dict[str, float]]:
        """Per-role, per-category mean consistency. Unknown (None)
        categories - v1 baselines predate category recording - are skipped."""
        rates: dict[str, dict[str, float]] = {}
        for role, fixtures in self.roles().items():
            by_category: dict[str, list[float]] = {}
            for fixture in fixtures:
                if fixture.category is None:
                    continue
                by_category.setdefault(fixture.category, []).append(fixture.consistency)
            if by_category:
                rates[role] = {category: _mean(values) for category, values in by_category.items()}
        return rates


def build_report(
    records: Sequence[Mapping[str, Any]],
    *,
    model: str,
    timestamp: str,
    runs_per_fixture: int,
) -> dict[str, Any]:
    """Assemble the v2 report JSON from per-run records.

    Each record is one run of one fixture:
    ``{"role", "fixture_id", "category", "cwe", "caught", "error", "detail"}``
    where ``error=True`` marks an agent-infrastructure failure (excluded
    from the consistency denominator; a parse failure of model output is
    NOT an error - it is a completed miss).
    """
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for record in records:
        key = (str(record["role"]), str(record["fixture_id"]))
        if key not in grouped:
            order.append(key)
        grouped.setdefault(key, []).append(record)

    fixtures_json: list[dict[str, Any]] = []
    stats: list[FixtureStats] = []
    for role, fixture_id in order:
        runs = grouped[(role, fixture_id)]
        errored = sum(1 for r in runs if bool(r.get("error")))
        detected = sum(1 for r in runs if bool(r.get("caught")) and not bool(r.get("error")))
        first = runs[0]
        fixture = FixtureStats(
            role=role,
            fixture_id=fixture_id,
            category=(str(first["category"]) if first.get("category") is not None else None),
            cwe=str(first["cwe"]) if first.get("cwe") is not None else None,
            runs_total=len(runs),
            runs_errored=errored,
            runs_detected=detected,
        )
        stats.append(fixture)
        fixtures_json.append(
            {
                "role": fixture.role,
                "fixture_id": fixture.fixture_id,
                "category": fixture.category,
                "cwe": fixture.cwe,
                "runs_total": fixture.runs_total,
                "runs_errored": fixture.runs_errored,
                "runs_detected": fixture.runs_detected,
                "consistency": fixture.consistency,
                "detected": fixture.detected,
                "runs": [
                    {
                        "caught": bool(r.get("caught")),
                        "error": bool(r.get("error")),
                        "detail": str(r.get("detail", "")),
                    }
                    for r in runs
                ],
            }
        )

    summary: dict[str, Any] = {}
    by_role: dict[str, list[FixtureStats]] = {}
    for fixture in stats:
        by_role.setdefault(fixture.role, []).append(fixture)
    for role, fixtures in by_role.items():
        role_summary: dict[str, Any] = {
            "fixtures_total": len(fixtures),
            "fixtures_detected": sum(1 for f in fixtures if f.detected),
            "detection_rate": role_detection_rate(fixtures),
        }
        by_category: dict[str, list[float]] = {}
        by_cwe: dict[str, list[float]] = {}
        for fixture in fixtures:
            if fixture.category is not None:
                by_category.setdefault(fixture.category, []).append(fixture.consistency)
            if fixture.cwe is not None:
                by_cwe.setdefault(fixture.cwe, []).append(fixture.consistency)
        if by_category:
            role_summary["by_category"] = {
                category: {
                    "fixtures_total": len(values),
                    "detection_rate": _mean(values),
                }
                for category, values in by_category.items()
            }
        if by_cwe:
            role_summary["by_cwe"] = {
                cwe: {
                    "fixtures_total": len(values),
                    "detection_rate": _mean(values),
                }
                for cwe, values in by_cwe.items()
            }
        summary[role] = role_summary

    return {
        "format_version": REPORT_FORMAT_VERSION,
        "model": model,
        "timestamp": timestamp,
        "runs_per_fixture": runs_per_fixture,
        "summary": summary,
        "fixtures": fixtures_json,
    }


def save_report(report: Mapping[str, Any], results_dir: Path) -> Path:
    """Write a report to ``baseline-<timestamp>.json`` in ``results_dir``."""
    results_dir.mkdir(parents=True, exist_ok=True)
    out = results_dir / f"baseline-{report['timestamp']}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return out


def load_baseline(path: Path) -> Baseline:
    """Load and normalize a baseline file (v1 or v2).

    v1 files (no ``format_version``) recorded a single boolean run per
    fixture and no category metadata: they normalize to
    ``runs_total=1`` with consistency 1.0/0.0 and ``category=None``.

    Raises ``ValueError`` on malformed content.
    """
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read baseline {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # Its own message, because this one is printed at the CLI and
        # the remedy differs: "cannot read" is a path or a permission
        # problem, and this file opened fine. kstrl writes baselines as
        # ASCII JSON, so a byte that will not decode came from elsewhere.
        raise ValueError(f"baseline {path} is not valid UTF-8; re-save it as UTF-8: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"baseline {path} is not a JSON object")

    format_version = int(data.get("format_version", 1))
    raw_fixtures = data.get("fixtures")
    if not isinstance(raw_fixtures, list):
        raise ValueError(f"baseline {path} has no 'fixtures' list")

    fixtures: list[FixtureStats] = []
    for entry in raw_fixtures:
        if not isinstance(entry, dict):
            raise ValueError(f"baseline {path}: fixture entry is not an object")
        role = str(entry.get("role", ""))
        fixture_id = str(entry.get("fixture_id", ""))
        if not role or not fixture_id:
            raise ValueError(f"baseline {path}: fixture entry missing role/fixture_id")
        if format_version >= 2:
            fixtures.append(
                FixtureStats(
                    role=role,
                    fixture_id=fixture_id,
                    category=(
                        str(entry["category"]) if entry.get("category") is not None else None
                    ),
                    cwe=str(entry["cwe"]) if entry.get("cwe") is not None else None,
                    runs_total=int(entry.get("runs_total", 0)),
                    runs_errored=int(entry.get("runs_errored", 0)),
                    runs_detected=int(entry.get("runs_detected", 0)),
                )
            )
        else:
            fixtures.append(
                FixtureStats(
                    role=role,
                    fixture_id=fixture_id,
                    category=None,
                    cwe=None,
                    runs_total=1,
                    runs_errored=0,
                    runs_detected=1 if bool(entry.get("caught")) else 0,
                )
            )

    return Baseline(
        path=path,
        model=str(data.get("model", "unknown")),
        timestamp=str(data.get("timestamp", "unknown")),
        format_version=format_version,
        runs_per_fixture=int(data.get("runs_per_fixture", 1)),
        fixtures=tuple(fixtures),
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateDelta:
    """Old-vs-new detection rate for one role (category=None) or category."""

    role: str
    category: str | None
    old_rate: float | None
    new_rate: float | None

    @property
    def drop(self) -> float:
        if self.old_rate is None or self.new_rate is None:
            return 0.0
        return self.old_rate - self.new_rate


@dataclass(frozen=True)
class Comparison:
    """Result of comparing two baselines under the codified thresholds."""

    old: Baseline
    new: Baseline
    role_deltas: tuple[RateDelta, ...]
    category_deltas: tuple[RateDelta, ...]
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    newly_missed: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def min_role_rate(role: str) -> float:
    return MIN_ROLE_DETECTION_RATE.get(role, DEFAULT_MIN_ROLE_DETECTION_RATE)


def compare_baselines(old: Baseline, new: Baseline) -> Comparison:
    """Diff two baselines and apply the threshold block.

    Failures (regression, exit 1): a role's rate dropping more than
    ``MAX_ROLE_DETECTION_DROP``; a role's new rate below its
    ``MIN_ROLE_DETECTION_RATE`` floor; a category's rate dropping more
    than ``MAX_CATEGORY_DETECTION_DROP``.

    Warnings (reported, exit stays 0): roles/categories present in the
    old baseline but absent from the new one (partial runs are
    legitimate - e.g. an architect-only re-run after a DECOMPOSE_PROMPT
    edit), and comparisons across different models (H2-extended: those
    measure the model change, not the prompt change).
    """
    failures: list[str] = []
    warnings: list[str] = []

    if old.model != new.model:
        warnings.append(
            f"comparing across models ({old.model!r} -> {new.model!r}): "
            "deltas measure the model change, not a prompt change "
            "(H2-extended: re-calibrate on model change)"
        )

    old_rates = old.role_rates()
    new_rates = new.role_rates()

    role_deltas: list[RateDelta] = []
    for role in sorted(set(old_rates) | set(new_rates)):
        old_rate = old_rates.get(role)
        new_rate = new_rates.get(role)
        delta = RateDelta(role=role, category=None, old_rate=old_rate, new_rate=new_rate)
        role_deltas.append(delta)
        if new_rate is None:
            warnings.append(f"role {role!r} present in old baseline but not exercised in new")
            continue
        if new_rate < min_role_rate(role):
            failures.append(
                f"role {role!r} detection rate {new_rate:.2f} is below its "
                f"floor {min_role_rate(role):.2f}"
            )
        if old_rate is not None and delta.drop > MAX_ROLE_DETECTION_DROP:
            failures.append(
                f"role {role!r} detection rate dropped "
                f"{old_rate:.2f} -> {new_rate:.2f} "
                f"(drop {delta.drop:.2f} > {MAX_ROLE_DETECTION_DROP:.2f})"
            )

    old_categories = old.category_rates()
    new_categories = new.category_rates()
    category_deltas: list[RateDelta] = []
    for role in sorted(set(old_categories) | set(new_categories)):
        old_by_cat = old_categories.get(role, {})
        new_by_cat = new_categories.get(role, {})
        if role not in new_rates:
            continue  # whole role missing: already warned above
        for category in sorted(set(old_by_cat) | set(new_by_cat)):
            old_rate = old_by_cat.get(category)
            new_rate = new_by_cat.get(category)
            delta = RateDelta(
                role=role,
                category=category,
                old_rate=old_rate,
                new_rate=new_rate,
            )
            category_deltas.append(delta)
            if new_rate is None:
                warnings.append(
                    f"category {role}/{category} present in old baseline but not exercised in new"
                )
                continue
            if old_rate is not None and delta.drop > MAX_CATEGORY_DETECTION_DROP:
                failures.append(
                    f"category {role}/{category} detection rate dropped "
                    f"{old_rate:.2f} -> {new_rate:.2f} "
                    f"(drop {delta.drop:.2f} > {MAX_CATEGORY_DETECTION_DROP:.2f})"
                )

    old_fixtures = {(f.role, f.fixture_id): f for f in old.fixtures}
    new_fixtures = {(f.role, f.fixture_id): f for f in new.fixtures}
    newly_missed = tuple(
        f"{role}/{fixture_id}"
        for (role, fixture_id), old_fixture in sorted(old_fixtures.items())
        if old_fixture.detected
        and (role, fixture_id) in new_fixtures
        and not new_fixtures[(role, fixture_id)].detected
    )

    return Comparison(
        old=old,
        new=new,
        role_deltas=tuple(role_deltas),
        category_deltas=tuple(category_deltas),
        failures=tuple(failures),
        warnings=tuple(warnings),
        newly_missed=newly_missed,
    )


def _format_rate(rate: float | None) -> str:
    return "-" if rate is None else f"{rate:.2f}"


def format_comparison(comparison: Comparison) -> str:
    """Human-readable comparison report."""
    old, new = comparison.old, comparison.new
    lines: list[str] = [
        "calibration compare",
        f"  old: {old.path} (model={old.model}, runs={old.runs_per_fixture}, ts={old.timestamp})",
        f"  new: {new.path} (model={new.model}, runs={new.runs_per_fixture}, ts={new.timestamp})",
        "",
        "per-role detection rate (mean per-fixture consistency):",
    ]
    for delta in comparison.role_deltas:
        floor = min_role_rate(delta.role)
        lines.append(
            f"  {delta.role:<26} {_format_rate(delta.old_rate)} -> "
            f"{_format_rate(delta.new_rate)}  (floor {floor:.2f})"
        )
    if comparison.category_deltas:
        lines.append("")
        lines.append("per-category detection rate:")
        for delta in comparison.category_deltas:
            lines.append(
                f"  {delta.role}/{delta.category or '?':<24} "
                f"{_format_rate(delta.old_rate)} -> {_format_rate(delta.new_rate)}"
            )
    if comparison.newly_missed:
        lines.append("")
        lines.append("fixtures newly missed (detected in old, not in new):")
        for name in comparison.newly_missed:
            lines.append(f"  {name}")
    if comparison.warnings:
        lines.append("")
        lines.append("warnings:")
        for warning in comparison.warnings:
            lines.append(f"  WARN: {warning}")
    lines.append("")
    if comparison.passed:
        lines.append("PASS: no calibration regression under the codified thresholds")
    else:
        lines.append("FAIL: calibration regression detected:")
        for failure in comparison.failures:
            lines.append(f"  FAIL: {failure}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reviewer-family override (R7.1)
# ---------------------------------------------------------------------------


def reviewer_override_from_env(
    environ: Mapping[str, str],
) -> tuple[str | None, str | None]:
    """(agent_type, model) override for the reviewer and security
    calibration agents (R7.1), read from
    ``KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE`` /
    ``KSTRL_CALIBRATION_REVIEWER_MODEL``. Empty values mean unset.

    The override exists so the calibration suite can measure the
    same-family vs cross-family correlated-miss delta: one baseline with
    no override (same family end to end), one with the reviewer roles on
    the second family. The architect always keeps the base calibration
    agent - rotation applies to reviewers, not the spec red-team."""
    agent_type = (
        environ.get("KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE")
        or environ.get("KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE")
        or None
    )
    model = (
        environ.get("KSTRL_CALIBRATION_REVIEWER_MODEL")
        or environ.get("KSTRL_CALIBRATION_REVIEWER_MODEL")
        or None
    )
    return agent_type, model


def reviewer_override_label(
    base_model: str,
    reviewer_agent_type: str | None,
    reviewer_model: str | None,
) -> str:
    """Baseline ``model`` label for a run with a reviewer override.

    No override keeps the plain base model id. Override runs get
    ``<base>+reviewer:<type>/<model>`` so ``compare_baselines``'s
    cross-model warning fires exactly when the reviewer family differs
    between the two baselines - that comparison measures the family
    change, which is what R7.1 wants surfaced, not hidden."""
    if not reviewer_agent_type and not reviewer_model:
        return base_model
    reviewer = "/".join(part for part in (reviewer_agent_type, reviewer_model) if part)
    return f"{base_model}+reviewer:{reviewer}"


# ---------------------------------------------------------------------------
# Model drift (R5.5, H2-extended)
# ---------------------------------------------------------------------------


class CalibrationModelDriftWarning(UserWarning):
    """The configured calibration model differs from the newest baseline's."""


def newest_baseline_path(results_dir: Path) -> Path | None:
    """Newest ``baseline-*.json`` by filename (baseline-YYYYMMDD-HHMMSS.json
    sorts lexicographically = chronologically)."""
    if not results_dir.is_dir():
        return None
    candidates = sorted(results_dir.glob("baseline-*.json"))
    return candidates[-1] if candidates else None


def model_drift_message(results_dir: Path, configured_model: str) -> str | None:
    """Return a warning message when ``configured_model`` differs from the
    newest baseline's recorded model, else None.

    Tolerates a missing results dir, no baselines, or a malformed newest
    baseline (all return None): this feeds an always-run structural test
    that must warn, never fail (R5.5).
    """
    path = newest_baseline_path(results_dir)
    if path is None:
        return None
    try:
        baseline = load_baseline(path)
    except ValueError:
        return None
    if baseline.model == configured_model:
        return None
    return (
        f"configured calibration model {configured_model!r} differs from the "
        f"newest baseline's model {baseline.model!r} ({path.name}). "
        "H2-extended: calibration must be re-run on model change, not just "
        "prompt change - capture a fresh baseline with "
        "KSTRL_RUN_CALIBRATION=1 before trusting detection rates."
    )


# ---------------------------------------------------------------------------
# CLI: python -m kstrl.calibration compare <old.json> <new.json>
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kstrl.calibration",
        description="Calibration baseline tooling (R5.1).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    compare_parser = subparsers.add_parser(
        "compare",
        help="Diff two baseline JSONs under the codified thresholds; exit 1 on regression.",
    )
    compare_parser.add_argument("old", type=Path, help="older baseline JSON")
    compare_parser.add_argument("new", type=Path, help="newer baseline JSON")
    compare_parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=(
            "project root holding kstrl.toml and the control directory, "
            "consulted for the autonomy ladder (default: cwd)"
        ),
    )
    args = parser.parse_args(argv)

    if args.command == "compare":
        try:
            old = load_baseline(args.old)
            new = load_baseline(args.new)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        comparison = compare_baselines(old, new)
        print(format_comparison(comparison))
        # Imported here rather than at module scope so this module, the
        # non-LLM measurement half, does not drag the control plane
        # (autonomy, inbox, UI) into every importer of a comparison.
        from kstrl.calibration_ladder import report_to_ladder

        override = report_to_ladder(comparison, (args.root or Path.cwd()).resolve())
        if override is not None:
            return override
        return 0 if comparison.passed else 1
    return 2  # pragma: no cover - argparse enforces the subcommand


if __name__ == "__main__":
    raise SystemExit(main())
