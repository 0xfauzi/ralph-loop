"""R2.1/R2.2 config control plane tests.

Every phase config must resolve through the real CLI construction path
with precedence: explicit CLI flag > env var > kstrl.toml > dataclass
default. The tests here invoke the actual click commands (``kstrl
factory`` / ``ks run`` / ``ks evolve`` / ``ks init``) with
``run_factory`` (or ``EvolutionJournal``) replaced by a capturing fake,
so the assertion target is the exact config object the orchestrator
would receive - not a loader called in isolation.

Nine config surfaces map to kstrl.toml sections:
KstrlConfig (agent/run/paths/git/ui), TimeoutConfig ([timeout]),
KnowledgeConfig ([knowledge]), FactoryConfig ([factory]), VerifyConfig
([verify]), SecurityConfig ([security]), ContractConfig ([contract]),
FeedforwardConfig ([feedforward]), EvolutionConfig ([evolution]).
KnowledgeConfig is consumed inside run_factory (factory.py calls
``KnowledgeConfig.load(root_dir)``), so its CLI-path coverage here is
the loader round-trip plus the new ``from_env``.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import kstrl.cli as cli_mod
import kstrl.evolution as evolution_mod
from kstrl.evolution import EvolutionConfig
from kstrl.factory import FactoryConfig, FactoryResult
from kstrl.feedforward import FeedforwardConfig
from kstrl.init_cmd import DEFAULT_KSTRL_TOML
from kstrl.knowledge import KnowledgeConfig
from kstrl.manifest import Component, Manifest
from kstrl.verify import VerifyConfig

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace run_factory with a capturing fake; return the capture dict."""
    box: dict[str, Any] = {}

    def fake_run_factory(
        manifest: Manifest,
        factory_config: FactoryConfig,
        base_config: Any,
        ui: Any,
        root_dir: Path,
        manifest_path: Path | None = None,
        **kwargs: Any,
    ) -> FactoryResult:
        box["manifest"] = manifest
        box["factory_config"] = factory_config
        box["base_config"] = base_config
        box["root_dir"] = root_dir
        return FactoryResult(exit_code=0)

    monkeypatch.setattr(cli_mod, "run_factory", fake_run_factory)
    return box


def _write_manifest(tmp_path: Path) -> Path:
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="cp-test",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id="c1",
                title="c1",
                description="component one",
                dependencies=[],
                prd_path="scripts/kstrl/prd.json",
                branch_name="kstrl/factory/c1",
            ),
        ],
    )
    path = tmp_path / "manifest.json"
    manifest.save(path)
    return path


def _invoke_factory(tmp_path: Path, *extra_args: str) -> Any:
    manifest_path = _write_manifest(tmp_path)
    runner = CliRunner()
    return runner.invoke(
        cli_mod.cli,
        [
            "factory",
            "--manifest",
            str(manifest_path),
            "--root",
            str(tmp_path),
            "--yes",
            "--agent-cmd",
            "true",
            "--ui",
            "plain",
            *extra_args,
        ],
    )


def _invoke_run(tmp_path: Path, *extra_args: str) -> Any:
    # R2.4: `ks run` preflights prd.json before run_factory, so the
    # round-trip needs a schema-valid PRD in place.
    prd_path = tmp_path / "scripts" / "kstrl" / "prd.json"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    if not prd_path.exists():
        prd_path.write_text(json.dumps({"branchName": "kstrl/test", "userStories": []}))
    runner = CliRunner()
    return runner.invoke(
        cli_mod.cli,
        [
            "run",
            "1",
            "--root",
            str(tmp_path),
            "--agent-cmd",
            "true",
            "--ui",
            "plain",
            *extra_args,
        ],
    )


# ---------------------------------------------------------------------------
# TOML round-trips through the real `ks factory` construction path
# ---------------------------------------------------------------------------


class TestFactoryCommandTomlRoundTrip:
    def test_factory_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\n"
            "max_parallel = 7\n"
            "max_retries = 9\n"
            'review_mode = "advisory"\n'
            "max_adversarial_calls = 5\n"
            "pause_before_pr_merge = true\n"
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        fc = captured["factory_config"]
        assert fc.max_parallel == 7
        assert fc.max_retries == 9
        assert fc.review_mode == "advisory"
        assert fc.max_adversarial_calls == 5
        assert fc.pause_before_pr_merge is True

    def test_verify_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[verify]\n"
            'test_command = "echo verify-toml"\n'
            "mutation_threshold = 75.0\n"
            "require_self_critique = true\n"
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        vc = captured["factory_config"].verify_config
        assert vc is not None
        assert vc.test_command == "echo verify-toml"
        assert vc.mutation_threshold == 75.0
        assert vc.require_self_critique is True

    def test_security_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[security]\nmode = "hard"\nfail_threshold = "critical"\ntimeout_seconds = 123.0\n'
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        sc = captured["factory_config"].security_config
        assert sc is not None
        assert sc.mode == "hard"
        assert sc.fail_threshold == "critical"
        assert sc.timeout_seconds == 123.0

    def test_contract_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[contract]\nmode = "final"\ntest_command = "echo contract-toml"\ntimeout = 44.0\n'
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        cc = captured["factory_config"].contract_config
        assert cc is not None
        assert cc.mode == "final"
        assert cc.test_command == "echo contract-toml"
        assert cc.timeout == 44.0

    def test_contract_toml_skip_disables_phase(
        self, tmp_path: Path, captured: dict[str, Any]
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[contract]\nmode = "skip"\n')
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].contract_config is None

    def test_feedforward_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[feedforward]\nmodule_map = false\nmax_context_tokens = 1234\n"
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        ff = captured["factory_config"].feedforward_config
        assert ff is not None
        assert ff.module_map is False
        assert ff.max_context_tokens == 1234

    def test_timeout_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[timeout]\nagent_iteration = 42.0\ncomponent_total = 99.0\n"
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        tc = captured["factory_config"].timeout_config
        assert tc is not None
        assert tc.agent_iteration == 42.0
        assert tc.component_total == 99.0

    def test_base_config_sections(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        # KstrlConfig covers the [agent]/[run]/[paths]/[git]/[ui] sections.
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\nmodel = "model-from-toml"\n[run]\nsleep_seconds = 0.25\n'
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        base = captured["base_config"]
        assert base.model == "model-from-toml"
        assert base.sleep_seconds == 0.25


# ---------------------------------------------------------------------------
# TOML round-trips through the real `ks run` construction path
# ---------------------------------------------------------------------------


class TestRunCommandTomlRoundTrip:
    def test_verify_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text('[verify]\ntest_command = "echo run-verify"\n')
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        vc = captured["factory_config"].verify_config
        assert vc is not None
        assert vc.test_command == "echo run-verify"

    def test_security_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text('[security]\nmode = "advisory"\n')
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        sc = captured["factory_config"].security_config
        assert sc is not None
        assert sc.mode == "advisory"

    def test_feedforward_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text("[feedforward]\nmax_context_tokens = 555\n")
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        ff = captured["factory_config"].feedforward_config
        assert ff is not None
        assert ff.max_context_tokens == 555

    def test_factory_tunables_honored(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\nmax_retries = 6\nmax_adversarial_calls = 2\n"
        )
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        fc = captured["factory_config"]
        assert fc.max_retries == 6
        assert fc.max_adversarial_calls == 2

    def test_single_component_structure_is_forced(
        self, tmp_path: Path, captured: dict[str, Any]
    ) -> None:
        # Structural fields cannot be overridden by toml: `ks run` is
        # by definition a local single-component no-PR invocation.
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\n"
            "max_parallel = 8\n"
            "use_worktrees = true\n"
            "single_pr = true\n"
            "create_prs = true\n"
        )
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        fc = captured["factory_config"]
        assert fc.max_parallel == 1
        assert fc.use_worktrees is False
        assert fc.single_pr is False
        assert fc.create_prs is False

    def test_review_mode_defaults_to_advisory(
        self, tmp_path: Path, captured: dict[str, Any]
    ) -> None:
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].review_mode == "advisory"

    def test_review_mode_toml_optin_is_honored(
        self, tmp_path: Path, captured: dict[str, Any]
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[factory]\nreview_mode = "hard"\n')
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].review_mode == "hard"


# ---------------------------------------------------------------------------
# `ks evolve` builds EvolutionConfig via load (the [evolution] section)
# ---------------------------------------------------------------------------


class TestEvolveCommandTomlRoundTrip:
    def test_enabled_false_in_toml_stops_evolve(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[evolution]\nenabled = false\n")
        runner = CliRunner()
        result = runner.invoke(
            cli_mod.cli,
            ["evolve", "--status", "--root", str(tmp_path), "--ui", "plain"],
        )
        assert result.exit_code == 1
        assert "disabled" in result.output

    def test_journal_path_reaches_journal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[evolution]\njournal_path = "custom/evo.jsonl"\n')
        box: dict[str, Any] = {}

        # Subclasses the real journal so a second call does not AttributeError (#327 F7).
        class FakeJournal(evolution_mod.EvolutionJournal):
            def __init__(self, config: EvolutionConfig) -> None:
                super().__init__(config)
                box["config"] = config

        monkeypatch.setattr(evolution_mod, "EvolutionJournal", FakeJournal)
        runner = CliRunner()
        result = runner.invoke(
            cli_mod.cli,
            ["evolve", "--status", "--root", str(tmp_path), "--ui", "plain"],
        )
        assert result.exit_code == 0, result.output
        assert box["config"].journal_path == tmp_path / "custom/evo.jsonl"


# ---------------------------------------------------------------------------
# Precedence: explicit CLI flag > env > toml (factory / verify / security)
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_factory_flag_beats_env_beats_toml(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_parallel = 5\n")
        monkeypatch.setenv("FACTORY_MAX_PARALLEL", "6")
        result = _invoke_factory(tmp_path, "--max-parallel", "7")
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_parallel == 7

        result = _invoke_factory(tmp_path)
        assert captured["factory_config"].max_parallel == 6

        monkeypatch.delenv("FACTORY_MAX_PARALLEL")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_parallel == 5

    def test_verify_flag_beats_env_beats_toml(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[verify]\ntest_command = "echo from-toml"\n')
        monkeypatch.setenv("KSTRL_VERIFY_TEST_CMD", "echo from-env")
        result = _invoke_factory(tmp_path, "--test-command", "echo from-flag")
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].verify_config.test_command == "echo from-flag"

        result = _invoke_factory(tmp_path)
        assert captured["factory_config"].verify_config.test_command == "echo from-env"

        monkeypatch.delenv("KSTRL_VERIFY_TEST_CMD")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].verify_config.test_command == "echo from-toml"

    def test_security_flag_beats_env_beats_toml(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[security]\nmode = "advisory"\n')
        monkeypatch.setenv("KSTRL_SECURITY_MODE", "skip")
        result = _invoke_factory(tmp_path, "--security-mode", "hard")
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].security_config.mode == "hard"

        result = _invoke_factory(tmp_path)
        assert captured["factory_config"].security_config.mode == "skip"

        monkeypatch.delenv("KSTRL_SECURITY_MODE")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].security_config.mode == "advisory"

    def test_verify_env_equal_to_default_still_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: the old overlay compared the env-derived value to
        # the dataclass default and skipped it on equality, so an env
        # var explicitly set to the default value could not override a
        # toml value.
        (tmp_path / "kstrl.toml").write_text("[verify]\nmutation_threshold = 75.0\n")
        monkeypatch.setenv("KSTRL_MUTATION_THRESHOLD", "50")
        config = VerifyConfig.load(tmp_path)
        assert config.mutation_threshold == 50.0


# ---------------------------------------------------------------------------
# R2.2: the two safety knobs are reachable via all three surfaces
# ---------------------------------------------------------------------------


class TestSafetyKnobs:
    def test_max_adversarial_calls_all_surfaces(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_adversarial_calls = 1\n")
        monkeypatch.setenv("KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS", "2")
        result = _invoke_factory(tmp_path, "--max-adversarial-calls", "3")
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_adversarial_calls == 3

        result = _invoke_factory(tmp_path)
        assert captured["factory_config"].max_adversarial_calls == 2

        monkeypatch.delenv("KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_adversarial_calls == 1

    def test_max_total_tokens_all_surfaces(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R3.1: the token budget is reachable via flag > env > toml."""
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_total_tokens = 100\n")
        monkeypatch.setenv("KSTRL_FACTORY_MAX_TOTAL_TOKENS", "200")
        result = _invoke_factory(tmp_path, "--max-total-tokens", "300")
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_total_tokens == 300

        result = _invoke_factory(tmp_path)
        assert captured["factory_config"].max_total_tokens == 200

        monkeypatch.delenv("KSTRL_FACTORY_MAX_TOTAL_TOKENS")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_total_tokens == 100

    def test_pause_before_pr_merge_all_surfaces(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\npause_before_pr_merge = true\n")
        # env (explicitly false) beats toml (true)
        monkeypatch.setenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", "0")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].pause_before_pr_merge is False

        # flag beats env
        result = _invoke_factory(tmp_path, "--pause-before-pr-merge")
        assert captured["factory_config"].pause_before_pr_merge is True

        # negated flag also wins
        monkeypatch.setenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", "1")
        result = _invoke_factory(tmp_path, "--no-pause-before-pr-merge")
        assert captured["factory_config"].pause_before_pr_merge is False

        # toml alone
        monkeypatch.delenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].pause_before_pr_merge is True


# ---------------------------------------------------------------------------
# NOTE lines: toml-driven changes are surfaced at factory startup
# ---------------------------------------------------------------------------


class TestTomlNotes:
    def test_note_emitted_for_toml_value(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_parallel = 9\n")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert "NOTE: [factory] max_parallel = 9" in result.output

    def test_no_note_when_flag_overrides(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_parallel = 9\n")
        result = _invoke_factory(tmp_path, "--max-parallel", "4")
        assert result.exit_code == 0, result.output
        assert "NOTE: [factory] max_parallel" not in result.output

    def test_no_notes_without_toml(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert "NOTE: [" not in result.output


# ---------------------------------------------------------------------------
# from_env for the loaders that were missing it (R2.1)
# ---------------------------------------------------------------------------


class TestNewFromEnv:
    def test_feedforward_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_FEEDFORWARD_MAX_TOKENS", "123")
        monkeypatch.setenv("KSTRL_FEEDFORWARD_MODULE_MAP", "false")
        config = FeedforwardConfig.from_env()
        assert config.max_context_tokens == 123
        assert config.module_map is False

    def test_evolution_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "3")
        monkeypatch.setenv("KSTRL_EVOLUTION_JOURNAL_PATH", "custom/j.jsonl")
        config = EvolutionConfig.from_env(tmp_path)
        assert config.lookback_runs == 3
        assert config.journal_path == tmp_path / "custom/j.jsonl"
        # defaults resolve against root_dir, not CWD
        assert config.experiments_path == tmp_path / ".kstrl/experiments.tsv"

    def test_knowledge_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_KNOWLEDGE_MAX_CORE_TOKENS", "99")
        monkeypatch.setenv("KSTRL_KNOWLEDGE_DEPENDENCY_SCOPE", "transitive")
        config = KnowledgeConfig.from_env(tmp_path)
        assert config.max_core_tokens == 99
        assert config.dependency_scope == "transitive"
        assert config.knowledge_root == tmp_path / ".kstrl" / "knowledge"

    def test_knowledge_from_env_rejects_bad_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_KNOWLEDGE_DEPENDENCY_SCOPE", "everything")
        with pytest.raises(ValueError, match="DEPENDENCY_SCOPE"):
            KnowledgeConfig.from_env(tmp_path)

    def test_factory_from_env_reads_safety_knobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS", "4")
        monkeypatch.setenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", "true")
        config = FactoryConfig.from_env()
        assert config.max_adversarial_calls == 4
        assert config.pause_before_pr_merge is True


# ---------------------------------------------------------------------------
# Knowledge section loader round-trip (consumed by run_factory internally)
# ---------------------------------------------------------------------------


class TestKnowledgeSectionRoundTrip:
    def test_toml_reaches_loaded_config(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[knowledge]\nmax_core_tokens = 777\ndistill_model = "m"\n'
        )
        config = KnowledgeConfig.load(tmp_path)
        assert config.max_core_tokens == 777
        assert config.distill_model == "m"


# ---------------------------------------------------------------------------
# ks init scaffolds kstrl.toml
# ---------------------------------------------------------------------------


EXPECTED_SCAFFOLD_SECTIONS = {
    "agent",
    "run",
    "paths",
    "git",
    "ui",
    "factory",
    "verify",
    "policy",
    "autonomy",
    "adequacy",
    "divergence",
    "inbox",
    "security",
    "contract",
    "feedforward",
    "knowledge",
    "evolution",
    "timeout",
}

# Keys each loader actually consumes, mirrored by hand so a typo'd or
# phantom key in the scaffold fails membership below.
EXPECTED_SCAFFOLD_KEYS = {
    "agent": {"type", "command", "model", "reasoning_effort"},
    "run": {"max_iterations", "sleep_seconds", "interactive"},
    "paths": {"prompt", "prd", "progress", "codebase_map", "golden_patterns", "allowed"},
    "git": {"branch", "auto_checkout"},
    "ui": {"ascii"},
    "factory": {
        "max_parallel",
        "max_retries",
        "retry_delay",
        "use_worktrees",
        "single_pr",
        "create_prs",
        "review_mode",
        "merge_timeout",
        "max_adversarial_calls",
        "pause_before_pr_merge",
    },
    "verify": {
        "test_command",
        "typecheck_command",
        "lint_command",
        "check_diff_scope",
        "check_bad_patterns",
        "dead_code_cleanup",
        "dead_code_command",
        "mutation_testing",
        "mutation_threshold",
        "mutation_timeout",
        "subprocess_timeout",
        "require_self_critique",
        "self_critique_min_bullets",
        "progress_file_path",
    },
    "policy": {
        "enabled",
        "paths_deny",
        "max_files_changed",
        "max_lines_changed",
        "deps_allow_new",
        "secret_patterns",
        "enforcement_paths_extra",
        "license_allow",
        "license_deny_partial",
        "license_unresolved",
        "license_use_network",
        "deploy",
    },
    "autonomy": {"enabled", "max_level"},
    "adequacy": {
        "enabled",
        "layer0",
        "require_strong_oracle",
        "flag_assertionless_tests",
    },
    "divergence": {
        "mode",
        "growth_steps",
    },
    "inbox": {
        "enabled",
        "open_item_cap",
        "snooze_hours",
        "notify_action_required",
    },
    "security": {
        "mode",
        "fail_threshold",
        "timeout_seconds",
        "agent_cmd",
        "agent_type",
        "model",
    },
    "contract": {"mode", "test_command", "timeout"},
    "feedforward": {
        "enabled",
        "module_map",
        "public_interfaces",
        "dependency_graph",
        "conventions",
        "max_context_tokens",
    },
    "knowledge": {
        "enabled",
        "max_core_tokens",
        "max_dependency_tokens",
        "max_sibling_tokens",
        "distill_timeout_seconds",
        "distill_model",
        "max_facts_per_distill",
        "dependency_scope",
    },
    "evolution": {
        "enabled",
        "journal_path",
        "experiments_path",
        "min_pattern_frequency",
        "lookback_runs",
    },
    "timeout": {
        "git_operation",
        "agent_iteration",
        "component_total",
        "verification_check",
        "review_agent",
        "contract_test",
        "subprocess_default",
        "scheduler_backstop_margin",
    },
}


def _uncomment_scaffold(text: str) -> str:
    """Uncomment every `# key = value` line inside the scaffold."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith("# ")
            and " = " in stripped
            and not (stripped.startswith(("# Resolved", "# kstrl")) or stripped[2:3].isupper())
        ):
            lines.append(stripped[2:])
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


class TestInitScaffold:
    def test_init_creates_kstrl_toml(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_mod.cli, ["init", str(tmp_path), "--ui", "plain"])
        assert result.exit_code == 0, result.output
        toml_path = tmp_path / "kstrl.toml"
        assert toml_path.exists()
        data = tomllib.loads(toml_path.read_text())
        assert set(data.keys()) == EXPECTED_SCAFFOLD_SECTIONS
        # All keys commented out: scaffolding changes no effective value.
        assert all(section == {} for section in data.values())

    def test_init_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_parallel = 2\n")
        runner = CliRunner()
        result = runner.invoke(cli_mod.cli, ["init", str(tmp_path), "--ui", "plain"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "kstrl.toml").read_text() == "[factory]\nmax_parallel = 2\n"

    def test_scaffold_keys_are_real(self) -> None:
        # Uncomment every key and check each against the loader key sets;
        # a scaffold key the loaders do not read fails here.
        data = tomllib.loads(_uncomment_scaffold(DEFAULT_KSTRL_TOML))
        assert set(data.keys()) == EXPECTED_SCAFFOLD_SECTIONS
        for section, keys in data.items():
            unexpected = set(keys) - EXPECTED_SCAFFOLD_KEYS[section]
            assert not unexpected, (
                f"[{section}] scaffold keys not consumed by any loader: {sorted(unexpected)}"
            )

    def test_uncommented_scaffold_loads_through_every_loader(self, tmp_path: Path) -> None:
        from kstrl.config import KstrlConfig
        from kstrl.contract import ContractConfig
        from kstrl.security import SecurityConfig
        from kstrl.timeout import TimeoutConfig

        (tmp_path / "kstrl.toml").write_text(_uncomment_scaffold(DEFAULT_KSTRL_TOML))
        KstrlConfig.load(tmp_path)
        FactoryConfig.load(tmp_path)
        VerifyConfig.load(tmp_path)
        SecurityConfig.load(tmp_path)
        ContractConfig.load(tmp_path)
        FeedforwardConfig.load(tmp_path)
        EvolutionConfig.load(tmp_path)
        KnowledgeConfig.load(tmp_path)
        TimeoutConfig.load(tmp_path)
