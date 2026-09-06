"""R10.6 (#227): the dampener's arithmetic and its baseline document.

No CLI here. These drive :mod:`kstrl.dampener` directly, so the bucket rules
and the on-disk format are pinned independently of how ``ks sense`` wires them
up; ``tests/test_sense_dampener_cli.py`` holds the command surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kstrl import dampener
from kstrl.parsers import ParsedFailure, ParsedOutput
from kstrl.verify import CheckResult, NotMeasured, VerificationResult


def _baseline(
    signatures: dict[str, int] | None = None,
    *,
    # Sorted, because that is the order the document round-trips through:
    # `to_document` sorts every collection so the file diffs cleanly.
    measured: tuple[str, ...] = ("linter", "test_suite", "typecheck"),
    unmeasured: tuple[str, ...] = (),
    sense_schema_version: int = 2,
    base_ref: str | None = "0123456789abcdef",
    passed: bool = False,
) -> dampener.Baseline:
    return dampener.Baseline(
        generated_at="2026-09-06T00:00:00Z",
        base_ref=base_ref,
        passed=passed,
        sense_schema_version=sense_schema_version,
        measured_checks=measured,
        unmeasured_checks=unmeasured,
        signatures=dict(signatures or {}),
    )


# --- the four buckets ---------------------------------------------------


def test_compare_no_regression() -> None:
    base = _baseline({"linter:E501": 3})

    comparison = dampener.compare(base, _baseline({"linter:E501": 3}))

    assert comparison.regressed is False
    assert comparison.new == {}
    assert comparison.increased == {}
    assert comparison.fixed == {}
    assert comparison.unmeasured == {}


def test_compare_detects_new_signature() -> None:
    comparison = dampener.compare(
        _baseline({"linter:E501": 3}),
        _baseline({"linter:E501": 3, "typecheck:arg-type": 1}),
    )

    assert comparison.new == {"typecheck:arg-type": 1}
    assert comparison.regressed is True


def test_compare_detects_increased_count() -> None:
    comparison = dampener.compare(_baseline({"linter:E501": 1}), _baseline({"linter:E501": 2}))

    assert comparison.increased == {"linter:E501": (1, 2)}
    assert comparison.new == {}
    assert comparison.regressed is True


def test_compare_reports_fixed_without_regression() -> None:
    """The positive case for ``fixed``, and the control for the M2 mutation.

    ``linter`` measured something in the current run, so its signature's
    absence is a fix that was PROVED. A ``compare`` that never fills ``fixed``
    at all would satisfy the "a gap is not a fix" test below while failing
    this one.
    """
    comparison = dampener.compare(_baseline({"linter:E501": 4}), _baseline({}))

    assert comparison.fixed == {"linter:E501": 4}
    assert comparison.unmeasured == {}
    assert comparison.regressed is False


def test_a_signature_whose_check_did_not_run_is_not_fixed() -> None:
    """A check that produced no measurement cannot prove anything.

    The whole reason ``unmeasured`` exists. ``fixed`` CLEARS, so it has to be
    narrow: an over-matching clear turns "the sensor stopped running" into
    "the problem went away", which is the mechanism deleting itself quietly.
    """
    current = _baseline({}, measured=("linter",), unmeasured=("dead_code",))

    comparison = dampener.compare(_baseline({"dead_code:unused-import": 2}), current)

    assert comparison.unmeasured == {"dead_code:unused-import": 2}
    assert comparison.fixed == {}
    assert comparison.regressed is False


def test_a_new_signature_from_a_check_the_baseline_never_measured_is_flagged() -> None:
    """The deliberate over-flag, pinned so silencing it is a red test.

    A signature from a check the BASELINE never measured still lands in
    ``new``. That over-flags when a toolchain gains a binary rather than the
    tree getting worse, and over-flagging is the safe direction for a guard
    that flags: the cost is an advisory comment somebody reads.
    """
    base = _baseline({}, measured=("linter",), unmeasured=("dead_code",))
    current = _baseline({"dead_code:unused-import": 1}, measured=("linter", "dead_code"))

    comparison = dampener.compare(base, current)

    assert comparison.new == {"dead_code:unused-import": 1}
    assert comparison.regressed is True


def test_a_decreased_count_is_not_a_regression() -> None:
    comparison = dampener.compare(_baseline({"linter:E501": 9}), _baseline({"linter:E501": 2}))

    assert comparison.increased == {}
    assert comparison.new == {}
    assert comparison.fixed == {}
    assert comparison.regressed is False


def test_buckets_are_sorted_by_signature() -> None:
    """Report order is signature order, so two runs diff cleanly."""
    current = _baseline({"typecheck:arg-type": 1, "linter:E501": 1, "linter:F401": 1})

    comparison = dampener.compare(_baseline({}), current)

    assert list(comparison.new) == ["linter:E501", "linter:F401", "typecheck:arg-type"]


@pytest.mark.parametrize(
    ("regressed", "fail_on_regression", "expected"),
    [(False, False, 0), (False, True, 0), (True, False, 0), (True, True, 1)],
)
def test_exit_code_table(regressed: bool, fail_on_regression: bool, expected: int) -> None:
    """Advisory by default: only the flag plus a regression is a non-zero exit."""
    current = _baseline({"linter:E501": 1} if regressed else {})
    comparison = dampener.compare(_baseline({}), current)
    assert comparison.regressed is regressed

    assert dampener.exit_code_for(comparison, fail_on_regression=fail_on_regression) == expected


# --- the schema note ----------------------------------------------------


def test_a_sense_schema_change_is_a_note_not_a_refusal() -> None:
    comparison = dampener.compare(
        _baseline({}, sense_schema_version=2),
        _baseline({}, sense_schema_version=3),
    )

    assert comparison.sense_schema_changed == (2, 3)
    assert comparison.regressed is False


def test_an_unchanged_sense_schema_records_no_note() -> None:
    assert dampener.compare(_baseline({}), _baseline({})).sense_schema_changed is None


# --- building a baseline from a run -------------------------------------


def _result(
    checks: list[CheckResult],
    gaps: list[NotMeasured] | None = None,
) -> VerificationResult:
    return VerificationResult(
        passed=all(c.passed for c in checks),
        checks=checks,
        not_measured=list(gaps or []),
    )


def _from(result: VerificationResult) -> dampener.Baseline:
    return dampener.baseline_from_result(
        result,
        base_ref="abc1234def",
        generated_at="2026-09-06T00:00:00Z",
        sense_schema_version=2,
    )


def _failing_linter(count: int = 3) -> CheckResult:
    return CheckResult(
        name="linter",
        passed=False,
        message="Linter failed",
        parsed=ParsedOutput(
            tool="ruff",
            failures=[ParsedFailure(code="E501", message="long") for _ in range(count)],
        ),
    )


def test_a_baseline_counts_every_signature_not_the_journal_cap() -> None:
    """``limit=None``: a baseline that dropped a check's sixth signature would
    report it as new on the very next run."""
    parsed = ParsedOutput(
        tool="ruff",
        failures=[
            ParsedFailure(code=code, message=code)
            for code in ("E501", "F401", "S608", "E731", "B008", "C901", "N802")
        ],
    )
    result = _result([CheckResult(name="linter", passed=False, message="x", parsed=parsed)])

    assert len(_from(result).signatures) == 7


def test_a_timed_out_check_contributes_no_signatures_and_is_unmeasured() -> None:
    """The measured reason this rule exists.

    At the default 300s verify timeout this repository's own test suite times
    out. That is a FAILING row, not a gap, and its fallback signature strips
    the digits, so 300s and 1800s produce the same string: a baseline written
    on a loaded machine records ``test_suite:test-suite-timed-out-after-s`` and
    the same tree on a faster machine reports it FIXED.
    """
    timed_out = CheckResult(
        name="test_suite",
        passed=False,
        message="Test suite timed out after 300.0s",
        measured=False,
    )
    baseline = _from(_result([timed_out, _failing_linter()]))

    assert baseline.signatures == {"linter:E501": 3}
    assert baseline.unmeasured_checks == ("test_suite",)
    assert baseline.measured_checks == ("linter",)


def test_a_gap_is_unmeasured_on_the_baseline_side_too() -> None:
    result = _result(
        [_failing_linter()],
        [NotMeasured(check="mutation_testing", reason="tool_missing", detail="mutmut absent")],
    )

    assert _from(result).unmeasured_checks == ("mutation_testing",)


def test_a_check_that_is_both_a_row_and_a_gap_counts_as_unmeasured() -> None:
    """Closed by construction, not by trusting that it cannot happen: the
    clearing side has to be the narrow one whatever the sensor emits."""
    result = _result(
        [_failing_linter()],
        [NotMeasured(check="linter", reason="timed_out", detail="x")],
    )

    baseline = _from(result)
    assert baseline.measured_checks == ()
    assert baseline.unmeasured_checks == ("linter",)


def test_passed_is_the_sensors_own_verdict_not_an_empty_signature_map() -> None:
    """A check can fail with no parsed codes at all; ``passed`` is not
    recomputed from ``signatures``."""
    result = _result([CheckResult(name="diff_scope", passed=False, message="out of scope")])

    baseline = _from(result)
    assert baseline.passed is False
    assert baseline.signatures != {}


def test_total_findings_counts_occurrences() -> None:
    assert _baseline({"linter:E501": 12, "typecheck:arg-type": 3}).total_findings == 15


# --- the document on disk -----------------------------------------------


def test_write_baseline_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "sense-baseline.json"
    baseline = _baseline({"linter:E501": 2})

    dampener.write_baseline(path, baseline, force=False)

    assert dampener.read_baseline(path) == baseline
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == dampener.BASELINE_SCHEMA_VERSION == 1
    assert document["base_ref"] == "0123456789abcdef"
    assert document["sense_schema_version"] == 2


def test_baseline_keys_are_sorted_in_the_file_bytes(tmp_path: Path) -> None:
    """Assert on the FILE TEXT, not on a re-parse: ``json.loads`` into a dict
    would hide an unsorted write, and unsorted is a whole-file git diff on
    every regeneration."""
    path = tmp_path / "b.json"
    reversed_order = {"typecheck:arg-type": 1, "linter:F401": 1, "linter:E501": 1}

    dampener.write_baseline(
        path,
        _baseline(reversed_order, measured=("typecheck", "linter"), unmeasured=("z", "a")),
        force=False,
    )

    text = path.read_text(encoding="utf-8")
    assert text.index("linter:E501") < text.index("linter:F401") < text.index("typecheck:arg-type")
    assert text.index('"a"') < text.index('"z"')
    # Top-level key ORDER is part of the format: reordering it would produce a
    # whole-file diff on a run that changed nothing.
    assert list(json.loads(text)) == [
        "schema_version",
        "generated_at",
        "base_ref",
        "passed",
        "sense_schema_version",
        "measured_checks",
        "unmeasured_checks",
        "signatures",
    ]


def test_write_refuses_an_existing_file_without_force(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    dampener.write_baseline(path, _baseline({"linter:E501": 1}), force=False)

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.write_baseline(path, _baseline({}), force=False)
    assert str(path) in str(excinfo.value)
    assert "--force" in str(excinfo.value)

    dampener.write_baseline(path, _baseline({}), force=True)
    assert dampener.read_baseline(path).signatures == {}


def test_missing_baseline_names_the_remedy(tmp_path: Path) -> None:
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(tmp_path / "absent.json")

    assert str(excinfo.value) == (
        f"no baseline at {tmp_path / 'absent.json'}; run ks sense --write-baseline first"
    )


def _valid_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": "2026-09-06T00:00:00Z",
        "base_ref": "abc",
        "passed": False,
        "sense_schema_version": 2,
        "measured_checks": ["linter"],
        "unmeasured_checks": [],
        "signatures": {"linter:E501": 1},
    }


@pytest.mark.parametrize(
    ("mutate", "expected_in_message"),
    [
        (lambda d: [1, 2, 3], "JSON object"),
        (lambda d: d.pop("schema_version") and d, "schema_version"),
        (lambda d: {**d, "schema_version": 2}, "expected 1"),
        (lambda d: {**d, "schema_version": "1"}, "schema_version"),
        (lambda d: {**d, "passed": "false"}, "passed"),
        (lambda d: {**d, "base_ref": 7}, "base_ref"),
        (lambda d: {**d, "sense_schema_version": True}, "sense_schema_version"),
        (lambda d: {**d, "measured_checks": "linter"}, "measured_checks"),
        (lambda d: {**d, "measured_checks": ["linter", 3]}, "measured_checks'[1]"),
        (lambda d: {**d, "measured_checks": [""]}, "measured_checks'[0]"),
        (lambda d: {**d, "signatures": ["linter:E501"]}, "signatures"),
        (lambda d: {**d, "signatures": {"linter:E501": "1"}}, "'linter:E501'"),
        (lambda d: {**d, "signatures": {"linter:E501": -1}}, "'linter:E501'"),
        (lambda d: {**d, "signatures": {"linter:E501": True}}, "'linter:E501'"),
        (lambda d: {**d, "signatures": {"": 1}}, "non-empty string"),
    ],
)
def test_a_malformed_baseline_is_refused_and_names_what_is_wrong(
    mutate: Any,
    expected_in_message: str,
) -> None:
    """Never read leniently. A document read as ``{}`` makes every current
    signature new (or every baseline one vanish) with nothing failing, which
    is the mechanism silently gone."""
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document(mutate(_valid_document()))

    assert expected_in_message in str(excinfo.value)


def test_a_baseline_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(path)
    assert "not JSON" in str(excinfo.value)


def test_a_baseline_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    """``UnicodeDecodeError`` is a ``ValueError`` and escapes a bare
    ``except OSError``, so it is caught explicitly beside it."""
    path = tmp_path / "b.json"
    path.write_bytes(b'{"schema_version": 1, "x": "\xff\xfe"}')

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(path)
    assert "cannot read the baseline" in str(excinfo.value)


def test_a_wrong_schema_version_names_the_remedy() -> None:
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document({**_valid_document(), "schema_version": 99})

    assert "--write-baseline --force" in str(excinfo.value)


# --- rendering ----------------------------------------------------------


def _comparison(**kwargs: Any) -> dampener.Comparison:
    defaults: dict[str, Any] = {
        "new": {},
        "increased": {},
        "fixed": {},
        "unmeasured": {},
        "sense_schema_changed": None,
    }
    return dampener.Comparison(**{**defaults, **kwargs})


def test_human_report_heading_and_verdict() -> None:
    lines = dampener.render_human(_comparison(), _baseline({}), Path("scripts/b.json"))

    assert lines[0] == "sense regression report vs scripts/b.json (0123456)"
    assert lines[-1] == "no regression"


def test_human_report_names_the_counts_when_it_regressed() -> None:
    comparison = _comparison(new={"linter:E501": 2}, increased={"linter:F401": (1, 4)})

    lines = dampener.render_human(comparison, _baseline({}), Path("b.json"))

    assert lines[-1] == "regression: 1 new, 1 increased"
    assert "  linter:E501  2" in lines
    assert "  linter:F401  1 -> 4" in lines


def test_human_report_says_unknown_when_the_baseline_has_no_commit() -> None:
    lines = dampener.render_human(_comparison(), _baseline({}, base_ref=None), Path("b.json"))

    assert lines[0].endswith("(unknown)")
    assert any("outside a repository" in line for line in lines)


def test_markdown_first_line_is_the_marker_exactly() -> None:
    """A workflow finds its own earlier comment by this line, so nothing may
    precede it: not a blank line, not a heading."""
    text = dampener.render_markdown(_comparison(), _baseline({}), Path("b.json"))

    assert text.splitlines()[0] == dampener.MARKDOWN_MARKER == "<!-- kstrl-sense-dampener -->"


def test_markdown_carries_a_table_per_non_empty_bucket() -> None:
    comparison = _comparison(
        new={"linter:E501": 2},
        increased={"linter:F401": (1, 4)},
        fixed={"typecheck:arg-type": 1},
        unmeasured={"dead_code:x": 3},
    )

    text = dampener.render_markdown(comparison, _baseline({}), Path("b.json"))

    assert "| `linter:E501` | 2 |" in text
    assert "| `linter:F401` | 1 | 4 |" in text
    assert "| `typecheck:arg-type` | 1 |" in text
    assert "| `dead_code:x` | 3 |" in text
    assert "advisory" in text


def test_markdown_omits_the_table_of_an_empty_bucket() -> None:
    text = dampener.render_markdown(_comparison(), _baseline({}), Path("b.json"))

    assert "New signatures" not in text
    assert "no regression" in text


def test_both_renderers_carry_the_schema_note() -> None:
    comparison = _comparison(sense_schema_changed=(2, 3))
    base = _baseline({})

    human = "\n".join(dampener.render_human(comparison, base, Path("b.json")))
    markdown = dampener.render_markdown(comparison, base, Path("b.json"))

    for text in (human, markdown):
        assert "from 2 to 3" in text
        assert "--write-baseline --force" in text


def test_the_write_summary_line_names_the_unmeasured_sensors() -> None:
    """Always, ``none`` included. A baseline written while the test suite
    timed out has a hole in it, and the operator has to see that at the moment
    they commit the file rather than infer it from the JSON later."""
    quiet = dampener.write_summary_line(Path("b.json"), _baseline({"linter:E501": 12}))
    holed = dampener.write_summary_line(
        Path("b.json"),
        _baseline({"linter:E501": 12}, unmeasured=("dead_code", "test_suite")),
    )

    assert quiet == "baseline written: b.json (1 signatures, 12 total findings); unmeasured: none"
    assert holed.endswith("; unmeasured: dead_code, test_suite")


def test_the_json_block_carries_every_bucket_and_the_verdict() -> None:
    comparison = _comparison(new={"a:b": 1}, increased={"c:d": (1, 2)}, sense_schema_changed=(2, 3))

    document = dampener.comparison_document(
        comparison,
        _baseline({"c:d": 1}),
        _baseline({"a:b": 1, "c:d": 2}),
        Path("b.json"),
    )

    assert document["new"] == {"a:b": 1}
    assert document["increased"] == {"c:d": {"baseline": 1, "current": 2}}
    assert document["regressed"] is True
    assert document["sense_schema_changed"] == {"baseline": 2, "current": 3}
    assert document["baseline"]["schema_version"] == 1
    assert document["current"]["signatures"] == {"a:b": 1, "c:d": 2}
    assert json.loads(json.dumps(document))["baseline_path"] == "b.json"


@pytest.mark.parametrize("key", ["measured_checks", "unmeasured_checks", "signatures"])
def test_a_missing_collection_is_refused_not_read_as_empty(key: str) -> None:
    """The lenient half of fail-closed, stated separately because it is the
    one that reads as legal. A missing ``measured_checks`` read as ``[]``
    would put every baseline signature in ``unmeasured`` forever, which looks
    exactly like a repository whose sensors are all off."""
    document = _valid_document()
    del document[key]

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document(document)
    assert key in str(excinfo.value)
