"""Tests for evolution module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kstrl.evolution import (
    JOURNAL_SCHEMA_VERSION,
    SPEC_ISSUES_EVENT,
    EvolutionConfig,
    EvolutionJournal,
    FailurePattern,
    signature_counts_from_verification,
    signature_for_error,
    signatures_from_findings,
    signatures_from_verification,
    split_signature,
)
from kstrl.factory import FactoryResult
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.parsers import ParsedFailure, ParsedOutput
from kstrl.verify import CheckResult
from tests.helpers.journal import audit, journal_at


def _make_manifest(
    components: list[Component] | None = None,
) -> Manifest:
    """Build a minimal manifest for testing."""
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test-project",
        base_branch="main",
        single_pr=False,
        components=components or [],
    )


def _make_component(
    id: str,
    status: str = ComponentStatus.PENDING.value,
    error: str = "",
    retries: int = 0,
    duration_seconds: float = 0.0,
    iteration_count: int = 0,
) -> Component:
    return Component(
        id=id,
        title=f"Component {id}",
        description=f"Description of {id}",
        dependencies=[],
        prd_path=f"prd/{id}.json",
        branch_name=f"kstrl/{id}",
        status=status,
        error=error,
        retries=retries,
        duration_seconds=duration_seconds,
        iteration_count=iteration_count,
    )


# ---------------------------------------------------------------------------
# EvolutionConfig defaults
# ---------------------------------------------------------------------------


class TestEvolutionConfigDefaults:
    def test_evolution_config_defaults(self) -> None:
        config = EvolutionConfig()
        assert config.enabled is True
        assert config.min_pattern_frequency == 2
        assert config.lookback_runs == 10
        assert config.auto_propose is True
        assert config.auto_apply_computational is False
        assert str(config.journal_path).endswith("evolution.jsonl")
        assert str(config.experiments_path).endswith("experiments.tsv")


# ---------------------------------------------------------------------------
# record_run
# ---------------------------------------------------------------------------


class TestRecordRun:
    def test_record_run_creates_files(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "evolution.jsonl"
        experiments_path = tmp_path / "experiments.tsv"
        config = EvolutionConfig(
            journal_path=journal_path,
            experiments_path=experiments_path,
        )
        journal = EvolutionJournal(config)

        manifest = _make_manifest(
            [
                _make_component(
                    "a",
                    status=ComponentStatus.COMPLETED.value,
                    duration_seconds=10.0,
                    iteration_count=2,
                ),
                _make_component(
                    "b",
                    status=ComponentStatus.FAILED.value,
                    error="pytest: assert 1 == 2",
                    retries=3,
                    duration_seconds=5.0,
                    iteration_count=1,
                ),
            ]
        )
        factory_result = FactoryResult(completed=["a"], failed=["b"], skipped=[])

        journal.record_run("run-001", manifest, factory_result)

        # JSONL file should exist with 2 entries (one per component).
        assert journal_path.exists()
        lines = journal_path.read_text().strip().splitlines()
        assert len(lines) == 2
        entry_a = json.loads(lines[0])
        assert entry_a["component_id"] == "a"
        assert entry_a["run_id"] == "run-001"
        entry_b = json.loads(lines[1])
        assert entry_b["component_id"] == "b"
        assert entry_b["status"] == ComponentStatus.FAILED.value

        # TSV file should exist with a header and one data row.
        assert experiments_path.exists()
        tsv_lines = experiments_path.read_text().strip().splitlines()
        assert len(tsv_lines) == 2  # header + data row
        assert "run-001" in tsv_lines[1]

    def test_record_run_includes_typed_findings(
        self,
        tmp_path: Path,
    ) -> None:
        """E3-consume: the evolution journal must serialize
        Component.findings alongside the existing scalars, and include
        a findings_summary for fast aggregation. This is what makes the
        typed Finding stream actually load-bearing."""
        from kstrl.findings import Finding

        journal_path = tmp_path / "evolution.jsonl"
        experiments_path = tmp_path / "experiments.tsv"
        config = EvolutionConfig(
            journal_path=journal_path,
            experiments_path=experiments_path,
        )
        journal = EvolutionJournal(config)

        comp = _make_component(
            "a",
            status=ComponentStatus.COMPLETED.value,
            duration_seconds=10.0,
        )
        comp.findings = [
            Finding.from_review_concern(
                category="dead_code",
                severity="fail",
                location="src/a.py:1",
                explanation="unused",
            ),
            Finding.from_security_finding(
                category="injection",
                severity="critical",
                location="src/b.py:2",
                explanation="raw sql",
                suggestion="parametrize",
                owasp="A03:2021-Injection",
                cwe="CWE-89",
            ),
            Finding.infrastructure_error("security", "agent timeout"),
        ]
        manifest = _make_manifest([comp])
        factory_result = FactoryResult(completed=["a"], failed=[], skipped=[])

        journal.record_run("run-findings", manifest, factory_result)

        entry = json.loads(journal_path.read_text().strip())
        # All three findings serialized.
        assert len(entry["findings"]) == 3
        # Summary aggregates correctly.
        summary = entry["findings_summary"]
        assert summary["total"] == 3
        assert summary["by_phase"]["review"] == 1
        assert summary["by_phase"]["security"] == 2
        assert summary["by_severity"]["fail"] == 1
        assert summary["by_severity"]["critical"] == 2
        assert summary["by_category"]["dead_code"] == 1
        assert summary["by_category"]["injection"] == 1
        assert summary["by_owasp"]["A03:2021-Injection"] == 1
        # Infrastructure errors are counted separately from real findings.
        assert summary["infrastructure_errors"] == 1


# ---------------------------------------------------------------------------
# extract_failure_patterns
# ---------------------------------------------------------------------------


class TestExtractFailurePatterns:
    def test_extract_failure_patterns(self) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)

        manifest = _make_manifest(
            [
                _make_component(
                    "a",
                    status=ComponentStatus.FAILED.value,
                    error="ruff: S608 violation",
                    retries=1,
                ),
                _make_component(
                    "b",
                    status=ComponentStatus.FAILED.value,
                    error="ruff: S608 violation",
                    retries=2,
                ),
                _make_component("c", status=ComponentStatus.COMPLETED.value),
            ]
        )

        patterns = journal.extract_failure_patterns(manifest, min_frequency=2)
        assert len(patterns) >= 1
        # Both a and b share the S608 signature
        assert any("S608" in p.error_signature for p in patterns)
        assert any(p.frequency >= 2 for p in patterns)

    def test_extract_failure_patterns_no_failures(self) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)
        manifest = _make_manifest(
            [
                _make_component("a", status=ComponentStatus.COMPLETED.value),
            ]
        )
        patterns = journal.extract_failure_patterns(manifest)
        assert patterns == []


# ---------------------------------------------------------------------------
# get_experiment_trends
# ---------------------------------------------------------------------------


class TestGetExperimentTrends:
    def test_get_experiment_trends(self, tmp_path: Path) -> None:
        tsv_path = tmp_path / "experiments.tsv"
        tsv_path.write_text(
            "run_id\ttimestamp\tproject\tcomponents_total\tcompleted\tfailed\t"
            "skipped\tavg_iterations\tavg_duration_s\tretry_rate\tcommon_failure\n"
            "run-001\t2025-01-01T00:00:00Z\ttest\t3\t2\t1\t0\t1.50\t10.0\t0.33\tS608\n"
            "run-002\t2025-01-02T00:00:00Z\ttest\t3\t3\t0\t0\t1.00\t8.0\t0.00\t\n"
        )

        config = EvolutionConfig(experiments_path=tsv_path)
        journal = EvolutionJournal(config)
        trends = journal.get_experiment_trends(last_n=10)
        assert len(trends) == 2
        assert trends[0]["run_id"] == "run-001"
        assert trends[1]["completed"] == "3"

    def test_get_experiment_trends_missing_file(self, tmp_path: Path) -> None:
        config = EvolutionConfig(experiments_path=tmp_path / "nonexistent.tsv")
        journal = EvolutionJournal(config)
        trends = journal.get_experiment_trends()
        assert trends == []


class TestConcernHitRate:
    """D8: aggregate reviewer-concern signal across recent runs."""

    def _write_entries(self, path: Path, entries: list[dict]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def test_empty_journal(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "evolution.jsonl"
        journal_path.write_text("")
        config = EvolutionConfig(journal_path=journal_path)
        journal = EvolutionJournal(config)
        result = journal.get_concern_hit_rate()
        assert result == {
            "runs": 0,
            "components": 0,
            "with_concern": 0,
            "by_category": {},
        }

    def test_counts_categories_from_findings_summary(
        self,
        tmp_path: Path,
    ) -> None:
        """R6.2: the hit rate consumes the typed findings_summary that
        record_run writes, not the error string (concern categories
        never appear there, so the old scan was structurally zero)."""
        journal_path = tmp_path / "evolution.jsonl"
        self._write_entries(
            journal_path,
            [
                {
                    "run_id": "run-1",
                    "component_id": "a",
                    "event_type": "component_result",
                    "findings_summary": {
                        "total": 2,
                        "by_category": {"scope_creep": 1, "test_quality": 1},
                    },
                },
                {
                    "run_id": "run-1",
                    "component_id": "b",
                    "event_type": "component_result",
                    # Infrastructure-only summaries are non-execution, not
                    # adversarial signal.
                    "findings_summary": {
                        "total": 1,
                        "by_category": {"infrastructure_error": 1},
                    },
                },
                {
                    "run_id": "run-2",
                    "component_id": "c",
                    "event_type": "component_result",
                    "findings_summary": {
                        "total": 1,
                        "by_category": {"security_concern": 1},
                    },
                },
                {
                    "run_id": "run-2",
                    "component_id": "d",
                    "event_type": "component_result",
                    "findings_summary": {"total": 0, "by_category": {}},
                },
                # Non-component entries are excluded from the denominator.
                {
                    "run_id": "run-2",
                    "component_id": "",
                    "event_type": "contract_result",
                    "tier": 1,
                    "passed": True,
                },
            ],
        )
        config = EvolutionConfig(journal_path=journal_path)
        journal = EvolutionJournal(config)
        result = journal.get_concern_hit_rate()
        assert result["runs"] == 2
        assert result["components"] == 4
        assert result["with_concern"] == 2
        assert result["by_category"] == {
            "scope_creep": 1,
            "test_quality": 1,
            "security_concern": 1,
        }
        assert "infrastructure_error" not in result["by_category"]


# ---------------------------------------------------------------------------
# propose_improvements
# ---------------------------------------------------------------------------


class TestProposeImprovements:
    def test_propose_improvements(self) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)

        patterns = [
            FailurePattern(
                description="linter failure 'S608' in 3/5 components",
                frequency=3,
                total_components=5,
                affected_components=["a", "b", "c"],
                check_name="linter",
                error_signature="S608",
                category="verification",
            ),
            FailurePattern(
                description="test_suite failure 'assert-mismatch' in 2/5 components",
                frequency=2,
                total_components=5,
                affected_components=["d", "e"],
                check_name="test_suite",
                error_signature="assert-mismatch",
                category="verification",
            ),
        ]

        proposals = journal.propose_improvements(patterns)
        assert len(proposals) == 2
        assert proposals[0].id == "PROP-001"
        assert "S608" in proposals[0].title
        assert proposals[0].target == "claude_md"
        assert proposals[1].target == "feedforward_config"

    # Every branch of propose_improvements, not just the two that named
    # a toolchain. `check_name` is the GATE, which carries no toolchain
    # at all now that each gate dispatches per project (#258), so a code
    # in any of these can be a tsc TS-number or an eslint rule as easily
    # as a mypy or ruff one.
    @pytest.mark.parametrize(
        ("check_name", "signature", "expected_target"),
        [
            ("linter", "no-unused-vars", "claude_md"),
            ("typecheck", "TS2322", "typecheck_config"),
            ("test_suite", "assertion-error", "feedforward_config"),
            ("review", "scope_creep", "claude_md"),
            ("security", "injection", "claude_md"),
        ],
    )
    def test_proposals_name_no_toolchain(
        self, check_name: str, signature: str, expected_target: str
    ) -> None:
        """#258 review: a TypeScript project was sent to edit a pyproject.toml.

        save_proposals writes the target verbatim into the proposal file
        as `**Target**: ...`, one line above the prose, so the field is
        as visible to the reader as the sentences are and has to be as
        neutral. The whole proposal is searched, not just the suggested
        change, because a name in the description or the target ships
        just as far.
        """
        config = EvolutionConfig()
        journal = EvolutionJournal(config)
        patterns = [
            FailurePattern(
                description=f"{check_name} failure '{signature}' in 3/5 components",
                frequency=3,
                total_components=5,
                affected_components=["a", "b", "c"],
                check_name=check_name,
                error_signature=signature,
                category="verification",
            )
        ]

        proposal = journal.propose_improvements(patterns)[0]
        written = " ".join(
            [proposal.target, proposal.title, proposal.description, proposal.suggested_change]
        )

        assert proposal.target == expected_target
        for toolchain in ("pyproject", "mypy", "pyright", "ruff", "flake8"):
            assert toolchain not in written, f"{check_name} proposal names {toolchain}"

    def test_propose_improvements_empty(self) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)
        proposals = journal.propose_improvements([])
        assert proposals == []


# ---------------------------------------------------------------------------
# save_proposals
# ---------------------------------------------------------------------------


class TestSaveProposals:
    def test_save_proposals(self, tmp_path: Path) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)

        patterns = [
            FailurePattern(
                description="linter failure 'E501' in 4/6 components",
                frequency=4,
                total_components=6,
                affected_components=["a", "b", "c", "d"],
                check_name="linter",
                error_signature="E501",
                category="verification",
            ),
        ]
        proposals = journal.propose_improvements(patterns)

        output_dir = tmp_path / "proposals"
        written = journal.save_proposals(proposals, output_dir)
        assert len(written) == 1
        assert written[0].name == "prop-001.md"
        content = written[0].read_text()
        assert "PROP-001" in content
        assert "E501" in content
        assert "computational" in content


# ---------------------------------------------------------------------------
# R6.1: structured failure signatures
# ---------------------------------------------------------------------------


class TestSignatureHelpers:
    def test_signatures_from_verification_uses_parser_codes(self) -> None:
        from kstrl.gateparse import GATE_LINT, GATE_TEST, GATE_TYPECHECK, parse_gate_output
        from kstrl.verify import CheckResult

        # Real tool output through the real dispatcher rather than
        # hand-built ParsedOutputs: the signature is only worth anything
        # if the parser actually puts a code where this reads one, and a
        # synthetic ParsedFailure can be given a code the parser never
        # emits (#258).
        ruff = parse_gate_output(
            "a.py:1:1: E501 Line too long (120 > 100)\n"
            "b.py:2:5: S608 Possible SQL injection vector\n"
            "c.py:3:9: E501 Line too long (110 > 100)\n"
            "Found 3 errors.\n",
            GATE_LINT,
        )
        mypy = parse_gate_output(
            'a.py:4: error: Argument 1 has incompatible type "str" [arg-type]\n'
            "Found 1 error in 1 file (checked 2 source files)\n",
            GATE_TYPECHECK,
        )
        pytest_out = parse_gate_output(
            "=========================== short test summary info ===========================\n"
            "FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2\n"
            "=============================== 1 failed in 0.10s ===============================\n",
            GATE_TEST,
        )
        assert (ruff.tool, mypy.tool, pytest_out.tool) == ("ruff", "mypy", "pytest")
        checks = [
            CheckResult(name="linter", passed=False, message="Linter failed", parsed=ruff),
            CheckResult(name="typecheck", passed=False, message="Typecheck failed", parsed=mypy),
            CheckResult(name="test_suite", passed=False, message="Tests failed", parsed=pytest_out),
            CheckResult(name="bad_patterns", passed=True, message="ok"),
        ]
        sigs = signatures_from_verification(checks)
        assert "linter:E501" in sigs
        assert "linter:S608" in sigs
        assert "typecheck:arg-type" in sigs
        assert "test_suite:assertion-error" in sigs
        # Passing checks contribute nothing; duplicates collapse.
        assert sigs.count("linter:E501") == 1
        assert not any(s.startswith("bad_patterns") for s in sigs)

    def test_signatures_from_verification_fallback_slug(self) -> None:
        from kstrl.verify import CheckResult

        checks = [
            CheckResult(
                name="diff_scope",
                passed=False,
                message="3 files outside allowed scope (diff vs base branch 'main')",
            )
        ]
        sigs = signatures_from_verification(checks)
        assert len(sigs) == 1
        check, code = split_signature(sigs[0])
        assert check == "diff_scope"
        # Counts and quoted names are stripped so the slug is stable
        # across runs with different violation counts.
        assert "3" not in code
        assert "main" not in code
        assert "outside-allowed-scope" in code

    def test_signatures_from_findings(self) -> None:
        from kstrl.findings import Finding

        findings = [
            Finding.from_review_concern(
                category="scope_creep",
                severity="fail",
                location="a.py",
                explanation="x",
            ),
            Finding.from_review_concern(
                category="test_quality",
                severity="advisory",
                location="b.py",
                explanation="y",
            ),
        ]
        assert signatures_from_findings("review", findings) == [
            "review:scope_creep",
        ]

    def test_signatures_from_findings_infrastructure(self) -> None:
        from kstrl.findings import Finding

        findings = [Finding.infrastructure_error("review", "crashed")]
        assert signatures_from_findings("review", findings) == [
            "review:infrastructure",
        ]

    def test_signature_for_error_stable(self) -> None:
        sig1 = signature_for_error(
            "engineer",
            "component timeout: exceeded 600s wall clock",
        )
        sig2 = signature_for_error(
            "engineer",
            "component timeout: exceeded 1200s wall clock",
        )
        assert sig1 == sig2
        assert sig1.startswith("engineer:")


class TestRecordRunSignatures:
    def test_journal_entry_carries_structured_signatures(
        self,
        tmp_path: Path,
    ) -> None:
        config = EvolutionConfig(
            journal_path=tmp_path / "evolution.jsonl",
            experiments_path=tmp_path / "experiments.tsv",
        )
        journal = EvolutionJournal(config)
        comp = _make_component(
            "b",
            status=ComponentStatus.FAILED.value,
            error="Mechanical verification failed",
            retries=1,
        )
        comp.failed_phase = "verify"
        comp.failed_check = "linter"
        manifest = _make_manifest([comp])
        factory_result = FactoryResult(completed=[], failed=["b"], skipped=[])

        journal.record_run(
            "run-001",
            manifest,
            factory_result,
            failure_signatures={"b": ["linter:S608", "linter:E501"]},
        )

        entry = json.loads(config.journal_path.read_text().strip())
        assert entry["schema_version"] == JOURNAL_SCHEMA_VERSION
        assert entry["failure_signatures"] == ["linter:S608", "linter:E501"]
        assert entry["check_name"] == "linter"
        assert entry["error_signature"] == "S608"
        assert entry["failed_phase"] == "verify"
        assert entry["failed_check"] == "linter"
        # TSV common_failure carries the full signature, not a slug of
        # the flattened string.
        tsv = config.experiments_path.read_text()
        assert "linter:S608" in tsv

    def test_legacy_fallback_without_signatures(self, tmp_path: Path) -> None:
        """A failed component with no recorded signatures still gets a
        classified signature from its error string, so entries never
        lose the fields entirely."""
        config = EvolutionConfig(
            journal_path=tmp_path / "evolution.jsonl",
            experiments_path=tmp_path / "experiments.tsv",
        )
        journal = EvolutionJournal(config)
        comp = _make_component(
            "b",
            status=ComponentStatus.FAILED.value,
            error="ruff: S608 violation",
        )
        manifest = _make_manifest([comp])
        journal.record_run(
            "run-001",
            manifest,
            FactoryResult(completed=[], failed=["b"], skipped=[]),
        )
        entry = json.loads(config.journal_path.read_text().strip())
        assert entry["failure_signatures"] == ["linter:S608"]


class TestRecordRunFactUtilization:
    """#191: the L2+ gate needs fact-utilization to survive the run, and
    it needs "we could not measure" to stay distinguishable from "the
    engineer referenced nothing"."""

    def _journal(
        self,
        tmp_path: Path,
    ) -> tuple[EvolutionJournal, EvolutionConfig]:
        config = EvolutionConfig(
            journal_path=tmp_path / "evolution.jsonl",
            experiments_path=tmp_path / "experiments.tsv",
        )
        return EvolutionJournal(config), config

    def test_entry_carries_utilization(self, tmp_path: Path) -> None:
        journal, config = self._journal(tmp_path)
        comp = _make_component("b", status=ComponentStatus.COMPLETED.value)
        journal.record_run(
            "run-001",
            _make_manifest([comp]),
            FactoryResult(completed=["b"], failed=[], skipped=[]),
            fact_utilization={
                "b": {
                    "measured": True,
                    "injected": 4,
                    "referenced": 2,
                    "reason": "",
                }
            },
        )
        entry = json.loads(config.journal_path.read_text().strip())
        assert entry["knowledge_utilization"] == {
            "measured": True,
            "injected": 4,
            "referenced": 2,
            "reason": "",
        }

    def test_entry_carries_the_tier_breakdown(self, tmp_path: Path) -> None:
        """The denominator bias is only visible if the tiers survive to
        the journal: an overall 2/6 with a core 2/2 is a very different
        result from a genuine 2/6."""
        journal, config = self._journal(tmp_path)
        comp = _make_component("b", status=ComponentStatus.COMPLETED.value)
        journal.record_run(
            "run-001",
            _make_manifest([comp]),
            FactoryResult(completed=["b"], failed=[], skipped=[]),
            fact_utilization={
                "b": {
                    "measured": True,
                    "injected": 6,
                    "referenced": 2,
                    "reason": "",
                    "by_tier": {
                        "core": {"injected": 2, "referenced": 2},
                        "dependency": {"injected": 1, "referenced": 0},
                        "sibling": {"injected": 3, "referenced": 0},
                    },
                }
            },
        )
        entry = json.loads(config.journal_path.read_text().strip())
        by_tier = entry["knowledge_utilization"]["by_tier"]
        assert by_tier["core"] == {"injected": 2, "referenced": 2}
        assert by_tier["sibling"] == {"injected": 3, "referenced": 0}

    def test_unrecorded_component_gets_measured_false(
        self,
        tmp_path: Path,
    ) -> None:
        """The key is written for every component, so a reader never has
        to guess whether a missing field means zero or means nothing."""
        journal, config = self._journal(tmp_path)
        comp = _make_component("b", status=ComponentStatus.FAILED.value)
        journal.record_run(
            "run-001",
            _make_manifest([comp]),
            FactoryResult(completed=[], failed=["b"], skipped=[]),
        )
        entry = json.loads(config.journal_path.read_text().strip())
        util = entry["knowledge_utilization"]
        assert util["measured"] is False
        assert util["injected"] == 0 and util["referenced"] == 0
        assert util["reason"] == "not measured"

    def test_schema_version_not_bumped(self, tmp_path: Path) -> None:
        """Purely additive: every journal reader is .get()-based, and the
        three-way distinction rides on always writing the key."""
        journal, config = self._journal(tmp_path)
        comp = _make_component("b", status=ComponentStatus.COMPLETED.value)
        journal.record_run(
            "run-001",
            _make_manifest([comp]),
            FactoryResult(completed=["b"], failed=[], skipped=[]),
            fact_utilization={
                "b": {
                    "measured": True,
                    "injected": 1,
                    "referenced": 1,
                    "reason": "",
                }
            },
        )
        entry = json.loads(config.journal_path.read_text().strip())
        assert entry["schema_version"] == JOURNAL_SCHEMA_VERSION
        assert "knowledge_utilization" in entry

    def test_get_fact_utilization_never_counts_unmeasured_as_zero(
        self,
        tmp_path: Path,
    ) -> None:
        journal, config = self._journal(tmp_path)
        # run-001: one component referenced facts, one measured zero.
        journal.record_run(
            "run-001",
            _make_manifest(
                [
                    _make_component("a", status=ComponentStatus.COMPLETED.value),
                    _make_component("b", status=ComponentStatus.COMPLETED.value),
                ]
            ),
            FactoryResult(completed=["a", "b"], failed=[], skipped=[]),
            fact_utilization={
                "a": {"measured": True, "injected": 4, "referenced": 2, "reason": ""},
                "b": {"measured": True, "injected": 3, "referenced": 0, "reason": ""},
            },
        )
        # run-002: never measured at all.
        journal.record_run(
            "run-002",
            _make_manifest(
                [
                    _make_component("a", status=ComponentStatus.FAILED.value),
                ]
            ),
            FactoryResult(completed=[], failed=["a"], skipped=[]),
        )

        stats = journal.get_fact_utilization()
        assert stats["runs"] == 2
        assert stats["components"] == 3
        assert stats["measured"] == 2
        assert stats["unmeasured"] == 1
        # The unmeasured component contributes nothing to either total.
        assert stats["injected"] == 7
        assert stats["referenced"] == 2
        # The gate reads this: only run-001 has nonzero utilization.
        assert stats["runs_with_referenced"] == 1

    def test_get_fact_utilization_ignores_legacy_entries(
        self,
        tmp_path: Path,
    ) -> None:
        """Pre-#191 entries have no knowledge_utilization key at all and
        must count as unmeasured, not as measured zeros."""
        journal, config = self._journal(tmp_path)
        config.journal_path.write_text(
            json.dumps(
                {
                    "schema_version": JOURNAL_SCHEMA_VERSION,
                    "run_id": "run-legacy",
                    "component_id": "a",
                    "event_type": "component_result",
                    "status": "completed",
                }
            )
            + "\n"
        )
        stats = journal.get_fact_utilization()
        assert stats["measured"] == 0
        assert stats["unmeasured"] == 1
        assert stats["runs_with_referenced"] == 0

    def test_get_fact_utilization_rejects_unreadable_counts(
        self,
        tmp_path: Path,
    ) -> None:
        """Present but garbled is not evidence either - fail toward
        "no measurement", never toward a fabricated number."""
        journal, config = self._journal(tmp_path)
        config.journal_path.write_text(
            json.dumps(
                {
                    "schema_version": JOURNAL_SCHEMA_VERSION,
                    "run_id": "r",
                    "component_id": "a",
                    "event_type": "component_result",
                    "status": "completed",
                    "knowledge_utilization": {
                        "measured": True,
                        "injected": "four",
                        "referenced": 2,
                    },
                }
            )
            + "\n"
        )
        stats = journal.get_fact_utilization()
        assert stats["measured"] == 0
        assert stats["unmeasured"] == 1
        assert stats["referenced"] == 0


class TestEvolutionIntegration:
    """R6 'done when': a synthetic-but-realistic journal (real signature
    strings, typed findings) yields a proposal traceable to a recorded
    signature and a nonzero concern hit rate."""

    def _failed_component(self, comp_id: str) -> Component:
        from kstrl.findings import Finding

        comp = _make_component(
            comp_id,
            status=ComponentStatus.FAILED.value,
            error="Review failed",
            retries=1,
            duration_seconds=42.0,
            iteration_count=3,
        )
        comp.failed_phase = "review"
        comp.failed_check = "criteria"
        comp.findings = [
            Finding.from_review_concern(
                category="scope_creep",
                severity="fail",
                location=f"{comp_id}.py",
                explanation="touched other files",
            ),
        ]
        return comp

    def test_journal_to_traceable_proposal(self, tmp_path: Path) -> None:
        config = EvolutionConfig(
            journal_path=tmp_path / "evolution.jsonl",
            experiments_path=tmp_path / "experiments.tsv",
            min_pattern_frequency=2,
        )
        journal = EvolutionJournal(config)

        # Two runs, each with one linter:S608 failure and one review
        # scope_creep failure - the shapes record_run actually writes.
        for run_id in ("run-001", "run-002"):
            lint_comp = _make_component(
                "comp-lint",
                status=ComponentStatus.FAILED.value,
                error="Mechanical verification failed",
                retries=2,
                duration_seconds=30.0,
                iteration_count=2,
            )
            lint_comp.failed_phase = "verify"
            lint_comp.failed_check = "linter"
            review_comp = self._failed_component("comp-review")
            manifest = _make_manifest([lint_comp, review_comp])
            journal.record_run(
                run_id,
                manifest,
                FactoryResult(
                    completed=[],
                    failed=["comp-lint", "comp-review"],
                    skipped=[],
                ),
                failure_signatures={
                    "comp-lint": ["linter:S608"],
                    "comp-review": ["review:scope_creep"],
                },
            )

        patterns = journal.get_cross_run_patterns(lookback_runs=10)
        linter_patterns = [
            p for p in patterns if p.check_name == "linter" and p.error_signature == "S608"
        ]
        assert linter_patterns, (
            f"expected a linter:S608 pattern, got "
            f"{[(p.check_name, p.error_signature) for p in patterns]}"
        )
        assert linter_patterns[0].frequency == 2
        review_patterns = [
            p for p in patterns if p.check_name == "review" and p.error_signature == "scope_creep"
        ]
        assert review_patterns

        # Proposals trace back to the recorded signature: the S608
        # linter fast path fires, and the review proposal derives from
        # the finding taxonomy.
        proposals = journal.propose_improvements(patterns)
        s608 = [p for p in proposals if "S608" in p.title]
        assert s608 and s608[0].target == "claude_md"
        assert any("S608" in src for src in s608[0].source_patterns)
        assert any("scope_creep" in p.title for p in proposals)

        # Concern hit rate is nonzero because findings_summary carries
        # the scope_creep finding.
        hit_rate = journal.get_concern_hit_rate()
        assert hit_rate["with_concern"] > 0
        assert hit_rate["by_category"].get("scope_creep", 0) > 0


class TestProposalIdMonotonicity:
    def test_ids_continue_across_invocations(self, tmp_path: Path) -> None:
        """R6.2: a second `evolve` run continues numbering after the
        files already on disk and never clobbers them."""
        config = EvolutionConfig()
        journal = EvolutionJournal(config)
        output_dir = tmp_path / "proposals"

        def _pattern(sig: str) -> FailurePattern:
            return FailurePattern(
                description=f"linter failure '{sig}' in 2/4 components",
                frequency=2,
                total_components=4,
                affected_components=["a", "b"],
                check_name="linter",
                error_signature=sig,
                category="verification",
            )

        first = journal.propose_improvements(
            [_pattern("S608")],
            starting_number=journal.next_proposal_number(output_dir),
        )
        assert first[0].id == "PROP-001"
        journal.save_proposals(first, output_dir)
        first_content = (output_dir / "prop-001.md").read_text()

        second = journal.propose_improvements(
            [_pattern("E501")],
            starting_number=journal.next_proposal_number(output_dir),
        )
        assert second[0].id == "PROP-002"
        written = journal.save_proposals(second, output_dir)
        assert [p.name for p in written] == ["prop-002.md"]
        # Prior file untouched.
        assert (output_dir / "prop-001.md").read_text() == first_content

    def test_save_never_clobbers_existing_file(self, tmp_path: Path) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)
        output_dir = tmp_path / "proposals"
        output_dir.mkdir()
        (output_dir / "prop-001.md").write_text("# PROP-001: original\n")

        clashing = journal.propose_improvements(
            [
                FailurePattern(
                    description="linter failure 'E501' in 2/4 components",
                    frequency=2,
                    total_components=4,
                    affected_components=["a", "b"],
                    check_name="linter",
                    error_signature="E501",
                    category="verification",
                ),
            ]
        )
        written = journal.save_proposals(clashing, output_dir)
        assert written == []
        assert (output_dir / "prop-001.md").read_text() == "# PROP-001: original\n"


# ---------------------------------------------------------------------------
# get_spec_issue_runs (#260)
# ---------------------------------------------------------------------------


class TestSpecIssueRuns:
    """#260: the architect's own history, which carries no run_id."""

    def _journal(self, tmp_path: Path, lines: list[str]) -> EvolutionJournal:
        journal_path = tmp_path / "evolution.jsonl"
        journal_path.write_text("".join(line + "\n" for line in lines))
        return EvolutionJournal(EvolutionConfig(journal_path=journal_path))

    def _spec_entry(self, project: str, blockers: int) -> str:
        return json.dumps(
            {
                "timestamp": "2026-08-29T00:00:00Z",
                "project": project,
                "event_type": SPEC_ISSUES_EVENT,
                "spec_file": "spec.md",
                "halted": blockers > 0,
                "counts": {"blocker": blockers, "major": 0, "minor": 0},
                "issues": [
                    {"severity": "blocker", "kind": "ambiguity", "summary": f"issue {n}"}
                    for n in range(blockers)
                ],
            }
        )

    def test_entries_without_run_id_are_returned(self, tmp_path: Path) -> None:
        """The reason this method exists: a spec_issues entry is written
        before a run id exists, so the run-windowed reader drops it."""
        journal = self._journal(
            tmp_path,
            [self._spec_entry("writers-room", 7), self._spec_entry("writers-room", 11)],
        )

        assert journal._read_journal_entries() == []
        runs = journal.get_spec_issue_runs("writers-room")
        assert [r["counts"]["blocker"] for r in runs] == [7, 11]

    def test_other_projects_and_event_types_are_excluded(self, tmp_path: Path) -> None:
        journal = self._journal(
            tmp_path,
            [
                self._spec_entry("writers-room", 7),
                self._spec_entry("deckgen", 1),
                json.dumps({"project": "writers-room", "event_type": "component_result"}),
            ],
        )

        runs = journal.get_spec_issue_runs("writers-room")
        assert len(runs) == 1
        assert runs[0]["counts"]["blocker"] == 7

    def test_torn_and_non_object_lines_are_skipped(self, tmp_path: Path) -> None:
        """One unreadable line must not cost the reader the history."""
        journal = self._journal(
            tmp_path,
            [
                "{not json",
                "[1, 2, 3]",
                '"a bare string"',
                "",
                self._spec_entry("writers-room", 4),
            ],
        )

        assert [r["counts"]["blocker"] for r in journal.get_spec_issue_runs("writers-room")] == [4]

    def test_missing_journal_reads_as_empty(self, tmp_path: Path) -> None:
        config = EvolutionConfig(journal_path=tmp_path / "absent.jsonl")
        assert EvolutionJournal(config).get_spec_issue_runs("writers-room") == []

    def test_run_windowed_reader_survives_non_object_lines(self, tmp_path: Path) -> None:
        """A JSON array on its own line used to enter the entry list and
        then blow up the ``.get("run_id")`` that follows."""
        journal = self._journal(
            tmp_path,
            [
                "[1, 2, 3]",
                json.dumps({"run_id": "r1", "event_type": "component_result", "component": "a"}),
            ],
        )

        entries = journal._read_journal_entries()
        assert [e["component"] for e in entries] == ["a"]


class TestSpecAudits:
    """#314: the unwindowed reader the excluded-history accounting needs,
    and the one window rule built on it."""

    def _journal(self, tmp_path: Path, entries: list[dict[str, Any]]) -> EvolutionJournal:
        journal = journal_at(tmp_path)
        journal.append_entries(entries)
        return journal

    def test_every_project_is_returned_unwindowed(self, tmp_path: Path) -> None:
        """The accounting under the trend counts the history the trend
        LEAVES OUT, so a reader that filtered by project or applied the
        window would hide exactly what the caller is asking for (#280).
        """
        journal = self._journal(
            tmp_path,
            [audit("mine"), audit("other"), audit(None)],
        )

        assert [e.get("project") for e in journal.get_spec_audits()] == ["mine", "other", None]

    def test_other_event_types_are_excluded(self, tmp_path: Path) -> None:
        """The journal carries component results and repair rows too."""
        journal = self._journal(
            tmp_path,
            [
                {"run_id": "r1", "event_type": "component_result", "component": "a"},
                audit("mine"),
            ],
        )

        assert [e["event_type"] for e in journal.get_spec_audits()] == [SPEC_ISSUES_EVENT]

    def test_a_supplied_snapshot_is_windowed_without_a_second_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``audits=`` is what lets ONE read feed both the trend and the
        accounting computed over the same entries, so the two cannot
        disagree because the file moved between two reads."""
        journal = self._journal(tmp_path, [audit("mine", "on-disk.md")])
        elsewhere = [audit("mine", "b.md"), audit("other", "c.md")]

        def refuse(self: EvolutionJournal) -> list[dict[str, Any]]:
            raise AssertionError("read the file despite being handed a snapshot")

        monkeypatch.setattr(EvolutionJournal, "get_spec_audits", refuse)

        runs = journal.get_spec_issue_runs("mine", audits=elsewhere)

        assert [r["spec_file"] for r in runs] == ["b.md"]

    def test_a_supplied_snapshot_answers_the_same_as_raw_entries(self, tmp_path: Path) -> None:
        """The event-type filter is applied either way, so a caller that
        hands over unfiltered entries gets the reader's own answer
        rather than a component_result that happens to name a
        project."""
        entries: list[dict[str, Any]] = [
            {"event_type": "component_result", "project": "mine", "spec_file": "wrong.md"},
            audit("mine", "right.md"),
        ]
        journal = self._journal(tmp_path, entries)

        assert journal.get_spec_issue_runs("mine", audits=entries) == journal.get_spec_issue_runs(
            "mine"
        )

    def test_the_window_keeps_the_newest_audits_not_the_oldest(self, tmp_path: Path) -> None:
        """``[-last_n:]``, and the sign is the whole point: "the last N
        recorded audits" read backwards is a trend the operator has
        already acted on. Measured before this test existed: inverting
        the slice left the ENTIRE suite green, because every other
        window test records audits that raise the same counts.
        """
        journal = self._journal(tmp_path, [audit("mine", f"spec-{n}.md") for n in range(5)])

        assert [r["spec_file"] for r in journal.get_spec_issue_runs("mine", last_n=2)] == [
            "spec-3.md",
            "spec-4.md",
        ]

    def test_a_non_positive_window_reads_nothing(self, tmp_path: Path) -> None:
        """``lookback_runs = 0`` means the trend reads nothing, and the
        report says so out loud rather than claiming no audit exists."""
        journal = self._journal(tmp_path, [audit("mine")])

        assert journal.get_spec_issue_runs("mine", last_n=0) == []
        assert journal.get_spec_issue_runs("mine", last_n=-1) == []

    def test_an_unattributed_audit_belongs_to_no_project(self, tmp_path: Path) -> None:
        """#314: the window matches a project through ``entry_str``, the
        rule the report's accounting already used, so a null or
        non-string ``project`` is an unattributed audit and not a
        project whose name happens to be empty. Before the fold the
        method compared the raw field, and the two answered differently
        for a query on "": the report's rule counted these, the
        journal's did not.
        """
        journal = self._journal(
            tmp_path,
            [audit(None, "null.md"), audit(7, "int.md"), audit("", "empty.md")],
        )

        assert [r["spec_file"] for r in journal.get_spec_issue_runs("")] == [
            "null.md",
            "int.md",
            "empty.md",
        ]


# ---------------------------------------------------------------------------
# R10.6 (#227): the signature counter the dampener baseline is built from
# ---------------------------------------------------------------------------


def _linter_failing(*codes: str) -> CheckResult:
    """One failed linter check whose parser reported ``codes``, in order.

    An empty string in ``codes`` is a failure the parser could not name, which
    is the case that falls through to the message slug.
    """
    return CheckResult(
        name="linter",
        passed=False,
        message="Linter failed",
        parsed=ParsedOutput(
            tool="ruff",
            failures=[ParsedFailure(code=code, message=code or "unnamed") for code in codes],
        ),
    )


class TestSignatureCounts:
    """``limit`` and the occurrence counter.

    The journal caps a check at five distinct signatures so one catastrophic
    run cannot flood a journal entry. A baseline that dropped the sixth would
    report it as new on the very next run, so the dampener asks for no cap -
    and these tests are the control that asking did not move the default the
    journal still gets.
    """

    def test_signatures_from_verification_limit_none_counts_all(self) -> None:
        checks = [_linter_failing("E501", "F401", "S608", "E731", "B008", "C901", "N802")]

        assert len(signatures_from_verification(checks, limit=None)) == 7
        # The journal's behaviour, unchanged: five distinct codes per check.
        assert len(signatures_from_verification(checks)) == 5

    def test_the_default_limit_is_the_journal_cap(self) -> None:
        """The default is the CONSTANT, not a literal that can drift from it.

        The issue text asked for ``limit: int | None = None`` and for the
        default to keep journal behaviour byte-identical. Those cannot both
        hold, so the default is the cap and ``None`` means uncapped. This
        pins the object identity so a later edit cannot separate them.
        """
        import inspect

        from kstrl.evolution import _MAX_SIGNATURES_PER_CHECK

        default = inspect.signature(signatures_from_verification).parameters["limit"].default
        assert default is _MAX_SIGNATURES_PER_CHECK
        counts_default = (
            inspect.signature(signature_counts_from_verification).parameters["limit"].default
        )
        assert counts_default is _MAX_SIGNATURES_PER_CHECK

    def test_signature_counts_count_occurrences_not_presence(self) -> None:
        """12 E501s are 12, not 1.

        ``signatures_from_verification`` ends in ``dict.fromkeys``, so every
        count built from its return value would be 1 and the dampener's
        ``increased`` bucket could never fire. That is why the counter is a
        sibling rather than a wrapper over it.
        """
        checks = [_linter_failing(*["E501"] * 12)]

        assert signature_counts_from_verification(checks, limit=None) == {"linter:E501": 12}

    def test_a_check_with_no_codes_counts_its_message_slug_once(self) -> None:
        checks = [
            CheckResult(name="diff_scope", passed=False, message="3 files outside allowed scope")
        ]

        counts = signature_counts_from_verification(checks, limit=None)
        assert list(counts.values()) == [1]
        assert split_signature(next(iter(counts)))[0] == "diff_scope"

    def test_signatures_from_verification_is_the_keys_of_the_counter(self) -> None:
        """One decision, read two ways, so the two cannot drift apart."""
        checks = [
            _linter_failing("E501", "F401", "E501", "", "S608"),
            CheckResult(name="typecheck", passed=True, message="ok"),
            CheckResult(name="diff_scope", passed=False, message="out of scope"),
        ]

        for limit in (None, 1, 2, 5):
            assert signatures_from_verification(checks, limit=limit) == list(
                signature_counts_from_verification(checks, limit=limit)
            )

    def test_the_limit_caps_distinct_codes_not_occurrences(self) -> None:
        """A cap of 2 keeps the first two DISTINCT codes and all their hits."""
        checks = [_linter_failing("E501", "F401", "E501", "S608", "F401")]

        assert signature_counts_from_verification(checks, limit=2) == {
            "linter:E501": 2,
            "linter:F401": 2,
        }
