"""R10.6 (#227): the dampener flags on ``ks sense``.

Real git repositories under ``tmp_path`` whose ``[verify]`` commands are fast
no-op Python one-liners, driven through ``CliRunner``. The arithmetic and the
document format are pinned in ``tests/test_sense_dampener.py``; this file is
about exit codes, flag refusals, order of operations and output surfaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kstrl import dampener
from kstrl.cli import SENSE_SCHEMA_VERSION, cli
from tests.spine_utils import git
from tests.test_sense_cli import _LINT_FAIL_COMMAND, _kstrl_toml, _make_repo

DEFAULT_RELATIVE = "scripts/kstrl/sense-baseline.json"


def _invoke(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["sense", "--root", str(root), *args])


def _make_lint_fail(root: Path) -> None:
    """Point the repo's lint command at one that emits an E501."""
    (root / "kstrl.toml").write_text(_kstrl_toml(_LINT_FAIL_COMMAND), encoding="utf-8")


def _write(root: Path, *args: str) -> Result:
    return _invoke(root, "--write-baseline", *args)


def _baseline_document(root: Path) -> dict[str, Any]:
    text = (root / DEFAULT_RELATIVE).read_text(encoding="utf-8")
    document: dict[str, Any] = json.loads(text)
    return document


# --- writing ------------------------------------------------------------


def test_write_baseline_lands_at_the_default_path_under_root(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result = _write(root)

    assert result.exit_code == 0, result.output
    document = _baseline_document(root)
    assert document["schema_version"] == 1
    assert document["signatures"] == {}
    assert document["passed"] is True
    assert document["base_ref"] == git("rev-parse", "HEAD", cwd=root).strip()
    # Read from the constant, never a literal 2 or 3: PR #353 takes the sense
    # schema to 3, and this PR must pin nothing about which number that is.
    assert document["sense_schema_version"] == SENSE_SCHEMA_VERSION


def test_the_write_line_reports_counts_and_the_unmeasured_sensors(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _make_lint_fail(root)

    result = _write(root)

    # Exit follows the SENSOR here: writing a baseline is a measurement of the
    # tree, and this tree is red.
    assert result.exit_code == 1, result.output
    assert result.output.startswith(f"baseline written: {root / DEFAULT_RELATIVE}")
    assert "(1 signatures, 1 total findings); unmeasured: none" in result.output


def test_write_baseline_refuses_overwrite_without_force(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    assert _write(root).exit_code == 0
    before = (root / DEFAULT_RELATIVE).read_text(encoding="utf-8")

    refused = _write(root)

    assert refused.exit_code == 2
    assert str(root / DEFAULT_RELATIVE) in refused.output
    assert "--force" in refused.output
    assert (root / DEFAULT_RELATIVE).read_text(encoding="utf-8") == before

    _make_lint_fail(root)
    forced = _write(root, "--force")
    assert forced.exit_code == 1, forced.output
    assert _baseline_document(root)["signatures"] == {"linter:E501": 1}


def test_write_baseline_accepts_an_explicit_path(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    target = tmp_path / "elsewhere" / "b.json"

    assert _write(root, str(target)).exit_code == 0
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == 1
    assert not (root / DEFAULT_RELATIVE).exists()


# --- order of operations ------------------------------------------------


class _SensorSpy:
    """Records that it was called, and refuses to be a measurement.

    The point of this test is that the sensors NEVER run, so returning
    something plausible would hide the defect it exists to catch.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("the sensors ran before the baseline was validated")


@pytest.mark.parametrize(
    ("args", "expected_in_output"),
    [
        (("--compare-baseline",), "no baseline at"),
        (("--write-baseline",), "--force"),
    ],
)
def test_the_baseline_is_settled_before_the_sensors_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    expected_in_output: str,
) -> None:
    """A full sense run on kstrl itself costs 327 measured seconds.

    Telling an operator who forgot ``--force`` after five minutes rather than
    a tenth of a second is a latency they feel. ``sense`` imports
    ``run_mechanical_verification`` inside the function body, so the patch
    goes on the module it resolves from.
    """
    root = _make_repo(tmp_path)
    if args == ("--write-baseline",):
        assert _write(root).exit_code == 0
    spy = _SensorSpy()
    monkeypatch.setattr("kstrl.verify.run_mechanical_verification", spy)

    result = _invoke(root, *args)

    assert result.exit_code == 2, result.output
    assert expected_in_output in result.output
    assert spy.calls == 0


# --- comparing ----------------------------------------------------------


def test_compare_missing_baseline_exits_2_and_names_the_remedy(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result = _invoke(root, "--compare-baseline")

    assert result.exit_code == 2
    assert (
        f"error: no baseline at {root / DEFAULT_RELATIVE}; run ks sense --write-baseline first"
        in result.output
    )


def test_compare_on_an_unchanged_tree_says_no_regression(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    assert _write(root).exit_code == 0

    result = _invoke(root, "--compare-baseline")

    assert result.exit_code == 0, result.output
    assert "no regression" in result.output
    assert result.output.startswith("sense regression report vs ")


def test_compare_detects_a_new_signature_and_stays_advisory(tmp_path: Path) -> None:
    """The issue's acceptance bullet, end to end.

    Note what exit 0 proves: the comparison's exit code does NOT follow
    ``result.passed``. The linter is failing here.
    """
    root = _make_repo(tmp_path)
    assert _write(root).exit_code == 0
    _make_lint_fail(root)

    advisory = _invoke(root, "--compare-baseline")
    blocking = _invoke(root, "--compare-baseline", "--fail-on-regression")

    assert advisory.exit_code == 0, advisory.output
    assert "linter:E501" in advisory.output
    assert "regression: 1 new, 0 increased" in advisory.output
    assert blocking.exit_code == 1
    assert "linter:E501" in blocking.output


def test_compare_reports_a_fixed_signature_and_exits_0(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _make_lint_fail(root)
    assert _write(root).exit_code == 1
    (root / "kstrl.toml").write_text(_kstrl_toml(), encoding="utf-8")

    result = _invoke(root, "--compare-baseline", "--fail-on-regression")

    assert result.exit_code == 0, result.output
    assert "no regression" in result.output
    fixed = result.output.split("fixed:", 1)[1]
    assert "linter:E501" in fixed.split("unmeasured", 1)[0]


def test_markdown_format_starts_with_the_marker(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    assert _write(root).exit_code == 0

    result = _invoke(root, "--compare-baseline", "--format", "markdown")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == dampener.MARKDOWN_MARKER


def test_a_sense_schema_change_is_reported_not_refused(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    assert _write(root).exit_code == 0
    path = root / DEFAULT_RELATIVE
    document = json.loads(path.read_text(encoding="utf-8"))
    document["sense_schema_version"] = SENSE_SCHEMA_VERSION - 1
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    human = _invoke(root, "--compare-baseline")
    markdown = _invoke(root, "--compare-baseline", "--format", "markdown")
    as_json = _invoke(root, "--compare-baseline", "--json")

    assert (human.exit_code, markdown.exit_code, as_json.exit_code) == (0, 0, 0)
    note = f"from {SENSE_SCHEMA_VERSION - 1} to {SENSE_SCHEMA_VERSION}"
    assert note in human.output
    assert note in markdown.output
    block = json.loads(as_json.stdout)["dampener"]
    assert block["sense_schema_changed"] == {
        "baseline": SENSE_SCHEMA_VERSION - 1,
        "current": SENSE_SCHEMA_VERSION,
    }


# --- the JSON surface ---------------------------------------------------


def test_the_json_document_carries_the_dampener_block(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    assert _write(root).exit_code == 0

    result = _invoke(root, "--compare-baseline", "--json")

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["schema_version"] == SENSE_SCHEMA_VERSION
    assert set(document["dampener"]) == {
        "baseline_path",
        "baseline",
        "current",
        "new",
        "increased",
        "fixed",
        "unmeasured",
        "regressed",
        "sense_schema_changed",
    }
    assert document["dampener"]["regressed"] is False


def test_plain_sense_json_is_unchanged(tmp_path: Path) -> None:
    """No dampener flag, no new key, and no bump.

    The R10.1 contract is that a v2 reader can index this document. This PR
    adds a key that is ABSENT unless asked for, which is why it is not a
    schema bump.
    """
    root = _make_repo(tmp_path)

    result = _invoke(root, "--json")

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert set(document) == {
        "schema_version",
        "path",
        "base_branch",
        "passed",
        "checks",
        "not_measured",
    }
    for check in document["checks"]:
        assert set(check) == {
            "name",
            "passed",
            "message",
            "details",
            "duration_seconds",
            "findings",
        }


# --- flag refusals ------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "named"),
    [
        (("--write-baseline", "--compare-baseline"), ("--write-baseline", "--compare-baseline")),
        (("--force",), ("--force", "--write-baseline")),
        (("--fail-on-regression",), ("--fail-on-regression", "--compare-baseline")),
        (("--format", "markdown"), ("--format", "--compare-baseline")),
        (("--format", "human"), ("--format", "--compare-baseline")),
        (
            ("--compare-baseline", "--format", "markdown", "--json"),
            ("--json", "--format markdown"),
        ),
        (("--write-baseline", "--fail-on-regression"), ("--fail-on-regression",)),
        (("--compare-baseline", "--force"), ("--force",)),
    ],
)
def test_a_flag_that_would_do_nothing_is_refused(
    tmp_path: Path,
    args: tuple[str, ...],
    named: tuple[str, ...],
) -> None:
    """Exit 2, and the message names BOTH flags.

    Naming only the ignored one leaves an operator guessing which of the two
    they meant. A silent no-op is worse than either: the flag was typed
    because somebody wanted something to happen.
    """
    root = _make_repo(tmp_path)

    result = _invoke(root, *args)

    assert result.exit_code == 2, result.output
    for flag in named:
        assert flag in result.output


def test_a_refusal_is_a_json_error_document_under_json(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result = _invoke(root, "--force", "--json")

    assert result.exit_code == 2
    document = json.loads(result.stdout)
    assert document["schema_version"] == SENSE_SCHEMA_VERSION
    assert "--force" in document["error"]


def test_the_bare_flag_does_not_swallow_the_next_option(tmp_path: Path) -> None:
    """Measured against click 8.4.2 in the planning probe, and pinned here so
    a click upgrade that changes it is a red test rather than a baseline
    written to a file called ``--force``."""
    root = _make_repo(tmp_path)
    assert _write(root).exit_code == 0

    result = _write(root, "--force")

    assert result.exit_code == 0, result.output
    assert (root / DEFAULT_RELATIVE).exists()
    assert not (root / "--force").exists()


# --- malformed baselines, through the command -------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("{not json", "not JSON"),
        ("[1, 2]", "JSON object"),
        ('{"schema_version": 99}', "expected 1"),
        ('{"generated_at": "x"}', "schema_version"),
    ],
)
def test_a_malformed_baseline_exits_2_through_the_command(
    tmp_path: Path,
    payload: str,
    expected: str,
) -> None:
    root = _make_repo(tmp_path)
    path = root / DEFAULT_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    result = _invoke(root, "--compare-baseline")

    assert result.exit_code == 2, result.output
    assert expected in result.output


def test_base_ref_is_null_outside_a_repository(tmp_path: Path) -> None:
    """``ks sense`` runs happily outside a repository when no check reads the
    diff, so provenance the tool cannot get is recorded as null rather than
    refused: nothing gates on it."""
    root = tmp_path / "loose"
    root.mkdir()
    (root / "kstrl.toml").write_text(
        _kstrl_toml() + "check_diff_scope = false\ncheck_bad_patterns = false\n",
        encoding="utf-8",
    )

    assert _write(root).exit_code == 0
    assert _baseline_document(root)["base_ref"] is None

    report = _invoke(root, "--compare-baseline")
    assert report.exit_code == 0, report.output
    assert report.output.splitlines()[0].endswith("(unknown)")
