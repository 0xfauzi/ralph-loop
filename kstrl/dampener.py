"""The dampener: a sense measurement in version control, and what a branch added to it.

R10.6 (#227). The mechanism is three parts and no LLM: record the current
structured failure signatures of a tree in a file the repository tracks, run the
same sensors on a branch, and report what the branch ADDED. It is advisory by
default - it prints the report and exits 0 whether or not it found a regression -
because a dampener that fails a teammate's pull request on its first day is a
dampener somebody turns off.

The vocabulary is deliberately the evolution journal's. A signature here is
``"<check>:<code>"`` produced by
:func:`kstrl.evolution.signature_counts_from_verification`, so the dampener and
the journal cannot disagree about what a failure is called, and the spelling
lives in exactly one module.

FOUR BUCKETS, AND WHY ``fixed`` IS THE NARROW ONE
-------------------------------------------------
``new`` and ``increased`` FLAG: they may over-match, and the cost of a false one
is a comment somebody reads. ``fixed`` CLEARS: it says a failure went away, and
an over-matching clear deletes the mechanism silently. So ``fixed`` has to be
PROVED, not inferred from absence. A baseline signature that is absent now lands
in ``fixed`` only when the check that produced it MEASURED SOMETHING in the
current run; when it did not, the signature lands in ``unmeasured`` and the
report says so.

That is why :attr:`Baseline.unmeasured_checks` exists on both sides. A sensor
that timed out, whose tool is missing, or that recorded a
:class:`kstrl.verify.NotMeasured` gap contributes NO signatures to a baseline and
is named in ``unmeasured_checks`` instead. Without that rule this repository's
own baseline was measurably wrong: at the default 300s verify timeout the test
suite times out, which is a FAILING row carrying the signature
``test_suite:test-suite-timed-out-after-s`` (the digits are stripped by
``signature_slug``, so 300s and 1800s produce the same string), and the same tree
on a faster machine reports that signature as fixed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from kstrl.atomicio import atomic_write_json
from kstrl.evolution import signature_counts_from_verification, split_signature
from kstrl.verify import CheckResult, VerificationResult

#: Version of the BASELINE document, which is not the version of the
#: ``ks sense --json`` document. They move independently: the baseline records
#: the sensor's schema version in ``sense_schema_version`` so a reader can tell
#: that the sensor changed under a baseline nobody refreshed.
BASELINE_SCHEMA_VERSION = 1

#: Relative to ``--root``. ``scripts/kstrl/`` is the versioned per-project kstrl
#: config home, beside ``prompt.md`` and ``prd.json``.
DEFAULT_BASELINE_PATH = Path("scripts/kstrl/sense-baseline.json")

#: First line of the markdown report. A workflow finds its own earlier comment
#: by this string, so it is a constant here and never retyped in the YAML: the
#: workflow test compares the YAML against THIS.
MARKDOWN_MARKER = "<!-- kstrl-sense-dampener -->"

FORMAT_HUMAN = "human"
FORMAT_MARKDOWN = "markdown"

#: ``--write-baseline`` and ``--compare-baseline`` take an OPTIONAL path. Click
#: spells that with ``is_flag=False, flag_value=<sentinel>``, so the bare flag
#: yields this string. A NUL byte cannot appear in an argv element, so no
#: operator can type it: the sentinel is closed by construction rather than by a
#: reserved word somebody might pass. Measured: it does not appear in ``--help``.
OPTIONAL_VALUE_SENTINEL = "\x00default"

#: Which of the two things the command was asked to do. Not a status enum and
#: nothing routes on it (roadmap doctrine 6): it is a two-way dispatch inside one
#: command, and the only verdict the dampener produces is one boolean.
ACTION_WRITE = "write"
ACTION_COMPARE = "compare"


class BaselineError(ValueError):
    """The baseline could not be read as a baseline.

    A ``ValueError`` because that is what the CLI's fail-closed handlers already
    catch, and because ``UnicodeDecodeError`` - a real way this fails - is one.
    Never raised for a baseline that is merely RED: a brownfield repository's
    baseline is expected to record failures, and that is the case this exists
    for.
    """


class DampenerUsage(ValueError):
    """A flag combination in which one of the flags would silently do nothing."""


def _fail(message: str) -> NoReturn:
    """One raise site, so `-> NoReturn` tells mypy the checks below are
    exhaustive without an `assert isinstance` after every one of them."""
    raise BaselineError(message)


def _object_field(document: Mapping[str, Any], key: str) -> Any:
    if key not in document:
        _fail(f"baseline is missing {key!r}")
    return document[key]


def _bool_field(document: Mapping[str, Any], key: str) -> bool:
    value = _object_field(document, key)
    if not isinstance(value, bool):
        _fail(f"baseline {key!r} must be a boolean, got {type(value).__name__}")
    return value


def _int_field(document: Mapping[str, Any], key: str) -> int:
    value = _object_field(document, key)
    # bool is an int in Python; a `true` here is a malformed document, not a 1.
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"baseline {key!r} must be an integer, got {type(value).__name__}")
    return value


def _optional_str_field(document: Mapping[str, Any], key: str) -> str | None:
    value = _object_field(document, key)
    if value is not None and not isinstance(value, str):
        _fail(f"baseline {key!r} must be a string or null, got {type(value).__name__}")
    return value if isinstance(value, str) else None


def _str_tuple_field(document: Mapping[str, Any], key: str) -> tuple[str, ...]:
    # Required, not defaulted: a missing `measured_checks` read as empty is
    # a lenient read of a key the comparison depends on.
    value = _object_field(document, key)
    if not isinstance(value, list):
        _fail(f"baseline {key!r} must be an array, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            _fail(f"baseline {key!r}[{index}] must be a non-empty string, got {item!r}")
    return tuple(str(item) for item in value)


def _signatures_field(document: Mapping[str, Any], key: str) -> dict[str, int]:
    value = _object_field(document, key)
    if not isinstance(value, dict):
        _fail(f"baseline {key!r} must be an object, got {type(value).__name__}")
    counts: dict[str, int] = {}
    for name, count in value.items():
        if not isinstance(name, str) or not name:
            _fail(f"baseline {key!r} has a key that is not a non-empty string: {name!r}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _fail(f"baseline {key!r}[{name!r}] must be a non-negative integer, got {count!r}")
        counts[str(name)] = int(count)
    return counts


@dataclass(frozen=True)
class Baseline:
    """One sense measurement, reduced to what a later run can be compared to.

    Both sides of a comparison are one of these: the committed document and the
    run that just happened. Same type on purpose, so nothing can compare a
    baseline against a shape that carries less information than it does.
    """

    generated_at: str
    base_ref: str | None
    passed: bool
    sense_schema_version: int
    #: Checks that produced a row AND measured something. The only checks whose
    #: absent signature may be reported as fixed.
    measured_checks: tuple[str, ...]
    #: Checks that were asked for and measured nothing: a gap, a missing tool, a
    #: timeout. They contribute no signatures at all.
    unmeasured_checks: tuple[str, ...]
    signatures: Mapping[str, int]

    @property
    def total_findings(self) -> int:
        """Occurrences, not distinct signatures: 12 E501s are 12, not 1."""
        return sum(self.signatures.values())

    def to_document(self) -> dict[str, Any]:
        """The on-disk JSON, with every collection sorted.

        ``sorted`` on ``str`` compares code points and never consults the
        locale, so the file is byte-stable across machines and a git diff shows
        only what actually moved. Top-level key order is this literal order and
        is pinned by a test, because reordering it would produce a whole-file
        diff on a run that changed nothing.
        """
        return {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "base_ref": self.base_ref,
            "passed": self.passed,
            "sense_schema_version": self.sense_schema_version,
            "measured_checks": sorted(self.measured_checks),
            "unmeasured_checks": sorted(self.unmeasured_checks),
            "signatures": dict(sorted(self.signatures.items())),
        }

    @classmethod
    def from_document(cls, raw: object) -> Baseline:
        """Validate the RAW payload entry by entry, then build.

        Nothing here is read leniently. A document this cannot understand is a
        refusal, never an empty baseline: read as ``{}`` every current signature
        would be "new" (or every baseline one would vanish), and either way the
        mechanism is gone with nothing failing.
        """
        if not isinstance(raw, dict):
            raise BaselineError(f"baseline must be a JSON object, got {type(raw).__name__}")
        version = _int_field(raw, "schema_version")
        if version != BASELINE_SCHEMA_VERSION:
            raise BaselineError(
                f"baseline schema_version is {version}, expected {BASELINE_SCHEMA_VERSION}; "
                "run ks sense --write-baseline --force to regenerate it"
            )
        return cls(
            generated_at=str(raw.get("generated_at", "")),
            base_ref=_optional_str_field(raw, "base_ref"),
            passed=_bool_field(raw, "passed"),
            sense_schema_version=_int_field(raw, "sense_schema_version"),
            measured_checks=_str_tuple_field(raw, "measured_checks"),
            unmeasured_checks=_str_tuple_field(raw, "unmeasured_checks"),
            signatures=_signatures_field(raw, "signatures"),
        )


def measured_and_unmeasured(
    result: VerificationResult,
) -> tuple[list[CheckResult], tuple[str, ...]]:
    """Split a verification into the rows that measured and the names that did not.

    A check named in ``not_measured`` produced no row at all; a check whose row
    carries ``measured=False`` produced one and measured nothing anyway (a
    timeout, a missing detector). Both are equally unable to prove that a
    signature was fixed, so both land on the same side. A name in both is
    unmeasured: the clearing side has to be the narrow one.
    """
    unmeasured = {gap.check for gap in result.not_measured}
    unmeasured.update(check.name for check in result.checks if not check.measured)
    measured = [check for check in result.checks if check.measured and check.name not in unmeasured]
    return measured, tuple(sorted(unmeasured))


def baseline_from_result(
    result: VerificationResult,
    *,
    base_ref: str | None,
    generated_at: str,
    sense_schema_version: int,
) -> Baseline:
    """Reduce a sense run to a :class:`Baseline`.

    ``generated_at`` and ``base_ref`` are injected rather than read here so the
    document is a pure function of the run for tests. ``sense_schema_version``
    is passed in from :data:`kstrl.cli.SENSE_SCHEMA_VERSION` rather than
    imported, because the CLI imports this module.

    ``limit=None``: the journal caps a check at five distinct signatures so one
    catastrophic run cannot flood a journal entry, but a baseline that dropped
    the sixth would report it as new on the very next run.
    """
    measured, unmeasured = measured_and_unmeasured(result)
    counts = signature_counts_from_verification(measured, limit=None)
    return Baseline(
        generated_at=generated_at,
        base_ref=base_ref,
        passed=result.passed,
        sense_schema_version=sense_schema_version,
        measured_checks=tuple(sorted({check.name for check in measured})),
        unmeasured_checks=unmeasured,
        signatures=dict(sorted(counts.items())),
    )


def read_baseline(path: Path) -> Baseline:
    """The committed baseline, or :class:`BaselineError` saying why not.

    ``ValueError`` is caught beside ``OSError`` because ``UnicodeDecodeError``
    is a ``ValueError`` and escapes a fail-closed ``except OSError``; the
    encoding is named rather than left to the locale for the same reason.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BaselineError(f"no baseline at {path}; run ks sense --write-baseline first") from None
    except (OSError, ValueError) as exc:
        raise BaselineError(f"cannot read the baseline at {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise BaselineError(f"{path} is not JSON: {exc}") from exc
    return Baseline.from_document(raw)


def refuse_existing_baseline(path: Path, *, force: bool) -> None:
    """Raise unless ``path`` may be written.

    Called twice: once before the sensors run, so an operator who forgot
    ``--force`` is told in a tenth of a second rather than after a full test
    suite, and once immediately before the write, which is the authoritative
    refusal. The window between them is microseconds and there is no security
    boundary here; ``atomic_write_json`` cannot do an exclusive create because
    ``os.replace`` overwrites.
    """
    if not force and path.exists():
        raise BaselineError(f"{path} exists; pass --force to replace it")


def write_baseline(path: Path, baseline: Baseline, *, force: bool) -> None:
    """Write the baseline atomically, creating ``scripts/kstrl/`` if needed."""
    refuse_existing_baseline(path, force=force)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, baseline.to_document())


def write_summary_line(path: Path, baseline: Baseline) -> str:
    """The one line ``--write-baseline`` prints.

    The unmeasured sensors are named on it, always, ``none`` included: a
    baseline written while the test suite timed out is a baseline with a hole in
    it, and the operator has to be able to see that at the moment they commit
    the file rather than infer it from the JSON later.
    """
    unmeasured = ", ".join(baseline.unmeasured_checks) or "none"
    return (
        f"baseline written: {path} "
        f"({len(baseline.signatures)} signatures, {baseline.total_findings} total findings); "
        f"unmeasured: {unmeasured}"
    )


@dataclass(frozen=True)
class Comparison:
    """What a branch added to, and removed from, a baseline."""

    #: Signature absent from the baseline. Flags: may over-match.
    new: dict[str, int]
    #: ``(baseline count, current count)`` where the count rose. Flags.
    increased: dict[str, tuple[int, int]]
    #: In the baseline, absent now, and its check measured something now.
    #: Clears: proved, never inferred.
    fixed: dict[str, int]
    #: In the baseline, absent now, and its check measured nothing now.
    unmeasured: dict[str, int]
    #: ``(baseline, current)`` when the sensor's own schema version moved under
    #: the baseline, else None. A note, not a refusal: see :func:`compare`.
    sense_schema_changed: tuple[int, int] | None

    @property
    def regressed(self) -> bool:
        """The single verdict. ``fixed`` and ``unmeasured`` never affect it."""
        return bool(self.new) or bool(self.increased)


def compare(baseline: Baseline, current: Baseline) -> Comparison:
    """Bucket every signature on either side.

    A signature whose check the BASELINE never measured still lands in ``new``
    when it appears now. That over-flags when a toolchain gains a binary rather
    than the tree getting worse, which is the safe direction for a flagging
    guard and costs an advisory comment.

    A differing ``sense_schema_version`` is a NOTE rather than exit 2, and this
    is the one place the house fail-closed rule is deliberately not applied. The
    document parses, the baseline schema is v1 either way, and the dangerous
    half of the ambiguity - a renamed check reading as fixed - is already closed
    by the ``unmeasured`` bucket. Failing closed instead would break every
    consumer's pull-request check the moment the sensor version moved.
    """
    new: dict[str, int] = {}
    increased: dict[str, tuple[int, int]] = {}
    for signature, count in sorted(current.signatures.items()):
        before = baseline.signatures.get(signature)
        if before is None:
            new[signature] = count
        elif count > before:
            increased[signature] = (before, count)

    fixed: dict[str, int] = {}
    unmeasured: dict[str, int] = {}
    measured_now = set(current.measured_checks)
    for signature, count in sorted(baseline.signatures.items()):
        if signature in current.signatures:
            continue
        check, _code = split_signature(signature)
        if check in measured_now:
            fixed[signature] = count
        else:
            unmeasured[signature] = count

    changed: tuple[int, int] | None = None
    if baseline.sense_schema_version != current.sense_schema_version:
        changed = (baseline.sense_schema_version, current.sense_schema_version)
    return Comparison(
        new=new,
        increased=increased,
        fixed=fixed,
        unmeasured=unmeasured,
        sense_schema_changed=changed,
    )


def exit_code_for(comparison: Comparison, *, fail_on_regression: bool) -> int:
    """Advisory by default: 0 whether or not the branch regressed.

    One function so the graduation switch has one place to be wrong and one test
    to catch it.
    """
    if fail_on_regression and comparison.regressed:
        return 1
    return 0


@dataclass(frozen=True)
class Mode:
    """What the dampener flags on ``ks sense`` resolved to."""

    action: str
    path: Path
    force: bool
    fail_on_regression: bool
    output_format: str


def _baseline_path(value: str, root_dir: Path) -> Path:
    """The bare flag means the default under ``--root``; a value is taken as given."""
    if value == OPTIONAL_VALUE_SENTINEL:
        return root_dir / DEFAULT_BASELINE_PATH
    return Path(value).expanduser()


def _refuse_dead_flags(
    *,
    write_baseline: str | None,
    compare_baseline: str | None,
    force: bool,
    fail_on_regression: bool,
    output_format: str | None,
    as_json: bool,
) -> None:
    """Refuse every combination in which a flag would silently do nothing.

    Naming both flags in the message, rather than only the ignored one, is what
    lets an operator see which of the two they meant.
    """
    if write_baseline is not None and compare_baseline is not None:
        raise DampenerUsage("--write-baseline and --compare-baseline cannot be used together")
    if force and write_baseline is None:
        raise DampenerUsage("--force does nothing without --write-baseline")
    if fail_on_regression and compare_baseline is None:
        raise DampenerUsage("--fail-on-regression does nothing without --compare-baseline")
    if output_format is not None and compare_baseline is None:
        raise DampenerUsage("--format does nothing without --compare-baseline")
    if as_json and output_format is not None:
        raise DampenerUsage(f"--json and --format {output_format} cannot be used together")
    if as_json and write_baseline is not None:
        # --write-baseline prints one line and writes a file. There is no JSON
        # document for it to produce, so --json would silently do nothing,
        # which is the same defect as the four above rather than a lesser one.
        raise DampenerUsage("--json does nothing with --write-baseline")


def resolve_mode(
    *,
    write_baseline: str | None,
    compare_baseline: str | None,
    force: bool,
    fail_on_regression: bool,
    output_format: str | None,
    as_json: bool,
    root_dir: Path,
) -> Mode | None:
    """The dampener mode, or None when no dampener flag was given.

    ``None`` is the whole of the "plain ``ks sense`` is unchanged" promise: the
    command takes exactly the same path it took before this feature existed.
    """
    _refuse_dead_flags(
        write_baseline=write_baseline,
        compare_baseline=compare_baseline,
        force=force,
        fail_on_regression=fail_on_regression,
        output_format=output_format,
        as_json=as_json,
    )
    if write_baseline is not None:
        return Mode(
            action=ACTION_WRITE,
            path=_baseline_path(write_baseline, root_dir),
            force=force,
            fail_on_regression=False,
            output_format=FORMAT_HUMAN,
        )
    if compare_baseline is not None:
        return Mode(
            action=ACTION_COMPARE,
            path=_baseline_path(compare_baseline, root_dir),
            force=False,
            fail_on_regression=fail_on_regression,
            output_format=output_format or FORMAT_HUMAN,
        )
    return None


def short_ref(base_ref: str | None) -> str:
    """The baseline's provenance sha, abbreviated, or ``unknown``."""
    return base_ref[:7] if base_ref else "unknown"


def _notes(comparison: Comparison, baseline: Baseline) -> list[str]:
    notes: list[str] = []
    if comparison.sense_schema_changed is not None:
        was, now = comparison.sense_schema_changed
        notes.append(
            f"note: the sense schema moved from {was} to {now} since this baseline "
            "was written; refresh it with ks sense --write-baseline --force"
        )
    if baseline.base_ref is None:
        notes.append("note: the baseline records no commit; it was written outside a repository")
    return notes


def _verdict(comparison: Comparison) -> str:
    if not comparison.regressed:
        return "no regression"
    return f"regression: {len(comparison.new)} new, {len(comparison.increased)} increased"


def _human_bucket(title: str, rows: Iterable[str]) -> list[str]:
    lines = [f"{title}:"]
    body = [f"  {row}" for row in rows]
    return [*lines, *(body or ["  (none)"])]


def render_human(comparison: Comparison, baseline: Baseline, path: Path) -> list[str]:
    """The terminal report. Printed INSTEAD of the check table."""
    lines = [f"sense regression report vs {path} ({short_ref(baseline.base_ref)})"]
    lines.extend(_notes(comparison, baseline))
    lines.append("")
    lines.extend(_human_bucket("new", (f"{s}  {n}" for s, n in comparison.new.items())))
    lines.extend(
        _human_bucket(
            "increased",
            (f"{s}  {was} -> {now}" for s, (was, now) in comparison.increased.items()),
        )
    )
    lines.extend(_human_bucket("fixed", (f"{s}  {n}" for s, n in comparison.fixed.items())))
    lines.extend(
        _human_bucket(
            "unmeasured (a check that did not run cannot prove a fix)",
            (f"{s}  {n}" for s, n in comparison.unmeasured.items()),
        )
    )
    lines.append("")
    lines.append(_verdict(comparison))
    return lines


def _markdown_table(title: str, header: str, rows: list[str]) -> list[str]:
    if not rows:
        return []
    columns = header.count("|") - 1
    return [
        "",
        f"**{title}**",
        "",
        header,
        "|" + "|".join([" --- "] * columns) + "|",
        *rows,
        "",
    ]


def render_markdown(comparison: Comparison, baseline: Baseline, path: Path) -> str:
    """The pull-request comment. First line is :data:`MARKDOWN_MARKER`, exactly.

    A workflow finds its own earlier comment by that line and edits it in place,
    so nothing may precede it: not a blank line, not a heading.
    """
    lines = [
        MARKDOWN_MARKER,
        "## sense dampener",
        "",
        f"Baseline: `{path}` at `{short_ref(baseline.base_ref)}`.",
        "",
        f"**{_verdict(comparison)}**",
    ]
    for note in _notes(comparison, baseline):
        lines.extend(["", note])
    lines.extend(
        _markdown_table(
            "New signatures",
            "| signature | count |",
            [f"| `{s}` | {n} |" for s, n in comparison.new.items()],
        )
    )
    lines.extend(
        _markdown_table(
            "Increased",
            "| signature | baseline | now |",
            [f"| `{s}` | {was} | {now} |" for s, (was, now) in comparison.increased.items()],
        )
    )
    lines.extend(
        _markdown_table(
            "Fixed",
            "| signature | was |",
            [f"| `{s}` | {n} |" for s, n in comparison.fixed.items()],
        )
    )
    lines.extend(
        _markdown_table(
            "Unmeasured (a check that did not run cannot prove a fix)",
            "| signature | was |",
            [f"| `{s}` | {n} |" for s, n in comparison.unmeasured.items()],
        )
    )
    lines.append("")
    lines.append("This report is advisory: it never fails the job.")
    return "\n".join(lines)


def comparison_document(
    comparison: Comparison,
    baseline: Baseline,
    current: Baseline,
    path: Path,
) -> dict[str, Any]:
    """The ``dampener`` block of ``ks sense --json``.

    One nested key rather than the six flat ones the issue sketched: a flat
    ``new`` or ``current`` at the top of the sense document could collide with a
    future check name, and this has room for ``unmeasured`` and the schema note
    without another round of top-level keys.
    """
    schema_changed: dict[str, int] | None = None
    if comparison.sense_schema_changed is not None:
        was, now = comparison.sense_schema_changed
        schema_changed = {"baseline": was, "current": now}
    return {
        "baseline_path": str(path),
        "baseline": baseline.to_document(),
        "current": {
            "measured_checks": sorted(current.measured_checks),
            "unmeasured_checks": sorted(current.unmeasured_checks),
            "signatures": dict(sorted(current.signatures.items())),
        },
        "new": dict(comparison.new),
        "increased": {
            signature: {"baseline": was, "current": now}
            for signature, (was, now) in comparison.increased.items()
        },
        "fixed": dict(comparison.fixed),
        "unmeasured": dict(comparison.unmeasured),
        "regressed": comparison.regressed,
        "sense_schema_changed": schema_changed,
    }
