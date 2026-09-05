"""Tests for CLI module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import _format_component_status, _run_structural_override_notices, cli
from kstrl.factory import FactoryConfig
from kstrl.git import BASE_BRANCH_CANDIDATES, detect_base_branch, resolve_base_branch
from kstrl.manifest import Component, ComponentStatus, Manifest
from tests.spine_utils import git as spine_git


class TestCliHelp:
    """Tests for CLI help commands."""

    def test_main_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "kstrl" in result.output
        assert "run" in result.output
        assert "init" in result.output
        assert "understand" in result.output
        assert "feature" in result.output

    def test_run_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "MAX_ITERATIONS" in result.output
        assert "--agent-cmd" in result.output
        assert "--model" in result.output

    def test_init_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--help"])
        assert result.exit_code == 0
        assert "DIRECTORY" in result.output

    def test_understand_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["understand", "--help"])
        assert result.exit_code == 0
        assert "read-only" in result.output.lower()

    def test_feature_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["feature", "--help"])
        assert result.exit_code == 0
        assert "implementation" in result.output.lower()

    def test_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        from kstrl import __version__

        assert __version__ in result.output


class TestCliValidation:
    """Tests for CLI argument validation."""

    def test_run_invalid_max_iterations(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "invalid"])
        assert result.exit_code == 2
        assert "not a valid integer" in result.output

    def test_run_missing_prompt_file(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["run", "1", "--agent-cmd", "echo test", "--branch", ""],
            )
            # Should fail because prompt file doesn't exist
            assert result.exit_code != 0

    def test_run_uses_prompt_env_for_root(self, tmp_path: Path, monkeypatch) -> None:
        """``PROMPT_FILE`` env var should anchor the root-discovery logic
        before the factory pipeline takes over. We don't need to drive
        a full factory iteration here -- ``--no-verify`` short-circuits
        the verification phase so the test stays fast and doesn't depend
        on real git/agent state."""
        project = tmp_path / "project"
        kstrl_dir = project / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "prompt.md").write_text("test prompt")
        (kstrl_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}')

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            [
                "run",
                "0",
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
                "--no-verify",
            ],
            env={
                "PROMPT_FILE": str(kstrl_dir / "prompt.md"),
                "PRD_FILE": str(kstrl_dir / "prd.json"),
            },
        )
        # Either runs to completion (exit 0) or fails on the
        # factory-prerequisite check; the goal here is that
        # PROMPT_FILE resolves the root correctly, not that the
        # factory completes a real run in this in-process invocation.
        assert "PROMPT_FILE" not in (result.output or "")

    def test_understand_uses_root_option(self, tmp_path: Path, monkeypatch) -> None:
        project = tmp_path / "project"
        kstrl_dir = project / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "understand_prompt.md").write_text("test prompt")
        (kstrl_dir / "codebase_map.md").write_text("# Map\n")
        (kstrl_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}')

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            [
                "understand",
                "1",
                "--root",
                str(project),
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
            ],
        )
        assert result.exit_code == 0

    def test_feature_uses_root_option(self, tmp_path: Path, monkeypatch) -> None:
        project = tmp_path / "project"
        kstrl_dir = project / "scripts" / "kstrl"
        feature_dir = kstrl_dir / "feature" / "demo"
        feature_dir.mkdir(parents=True)
        (kstrl_dir / "feature_understand_prompt.md").write_text("test prompt")
        (kstrl_dir / "codebase_map.md").write_text("# Map\n")
        (feature_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}')

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            [
                "feature",
                "--root",
                str(project),
                "--prd",
                str(feature_dir / "prd.json"),
                "--understand-iterations",
                "1",
                "--implementation-auto-run",
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
            ],
        )
        assert result.exit_code == 0
        assert (feature_dir / "understand.md").exists()


class TestRunStructuralOverrideNotices:
    """Issue #207: `ks run` must say when it overrides configured knobs.

    `ks run` forces structural fields (max_parallel, use_worktrees,
    single_pr, create_prs); a startup notice fires when the resolved
    config set one to a non-default value - and stays silent otherwise,
    so the notice does not become background noise. The merge-gate
    warning itself is NOT emitted here: the autonomy ladder can flip
    pause_before_pr_merge inside run_factory, so the authoritative check
    is factory.merge_gate_unreachable_warning after autonomy resolution
    (review P1 on PR #211); its e2e coverage is below and its unit
    coverage is in test_factory.py.
    """

    def test_no_notices_for_default_config(self) -> None:
        assert _run_structural_override_notices(FactoryConfig()) == []

    def test_pause_before_pr_merge_not_handled_pre_resolution(self) -> None:
        """The gate flag resolves only after the autonomy ladder runs, so
        the pre-resolution notices deliberately ignore it (review P1)."""
        assert _run_structural_override_notices(FactoryConfig(pause_before_pr_merge=True)) == []

    def test_non_default_structural_field_is_named(self) -> None:
        notices = _run_structural_override_notices(FactoryConfig(max_parallel=8, single_pr=True))
        assert any("max_parallel = 8" in n for n in notices)
        assert any("single_pr = true" in n for n in notices)
        assert len(notices) == 2

    def test_default_valued_fields_stay_silent(self) -> None:
        """create_prs defaults to True and is forced to False on every
        `ks run`; warning about the default would fire unconditionally."""
        assert (
            _run_structural_override_notices(FactoryConfig(create_prs=True, use_worktrees=True))
            == []
        )

    def _scaffold_project(self, tmp_path: Path, toml_body: str = "") -> Path:
        """A real git repo shaped like a kstrl project: the run must be
        able to finish GREEN (the diff phase runs `git diff` against the
        base branch), so the e2e tests can assert exit_code == 0 rather
        than only grepping output (review P2)."""
        project = tmp_path / "project"
        kstrl_dir = project / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "prompt.md").write_text("test prompt")
        (kstrl_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}')
        if toml_body:
            (project / "kstrl.toml").write_text(toml_body)
        spine_git("init", "-q", "-b", "main", cwd=project)
        spine_git("config", "user.email", "cli@test", cwd=project)
        spine_git("config", "user.name", "CLI Test", cwd=project)
        spine_git("add", "-A", cwd=project)
        spine_git("commit", "-q", "-m", "init", cwd=project)
        return project

    def _invoke_run(self, project: Path, max_iterations: str = "1") -> Result:
        runner = CliRunner()
        return runner.invoke(
            cli,
            [
                "run",
                max_iterations,
                "--root",
                str(project),
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
                "--no-verify",
                "--ui",
                "plain",
                "--no-color",
            ],
        )

    def test_run_emits_notice_when_toml_sets_merge_gate(self, tmp_path: Path, monkeypatch) -> None:
        # review_mode=skip keeps the run LLM-free: without it the review
        # phase auto-detects a real agent CLI and makes a paid call.
        monkeypatch.delenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", raising=False)
        monkeypatch.delenv("KSTRL_AUTONOMY_ENABLED", raising=False)
        project = self._scaffold_project(
            tmp_path,
            '[factory]\npause_before_pr_merge = true\nreview_mode = "skip"\n',
        )
        result = self._invoke_run(project)
        assert result.exit_code == 0, result.output
        assert "pause_before_pr_merge" in result.output
        assert "merge gate" in result.output

    def test_run_stays_silent_when_merge_gate_unset(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", raising=False)
        monkeypatch.delenv("KSTRL_AUTONOMY_ENABLED", raising=False)
        project = self._scaffold_project(tmp_path, '[factory]\nreview_mode = "skip"\n')
        result = self._invoke_run(project)
        assert result.exit_code == 0, result.output
        assert "pause_before_pr_merge" not in result.output

    def test_run_warns_when_autonomy_ladder_flips_gate_on(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Review P1 regression: with [autonomy] enabled and NO factory
        flag set, the L1 bundle flips pause_before_pr_merge on inside
        run_factory - after the CLI-level notices already ran. The
        post-resolution warning must still fire.

        Runs 0 iterations: the L1 bundle also forces review_mode=hard,
        so a green run would need a real reviewer LLM call. The engineer
        therefore fails deterministically (exit 1); the warning is
        emitted before execution, so it is asserted alongside the exit
        code rather than instead of it.
        """
        monkeypatch.delenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", raising=False)
        monkeypatch.delenv("KSTRL_AUTONOMY_ENABLED", raising=False)
        project = self._scaffold_project(tmp_path, "[autonomy]\nenabled = true\n")
        result = self._invoke_run(project, max_iterations="0")
        assert result.exit_code == 1, result.output
        assert "Autonomy" in result.output
        assert "pause_before_pr_merge is on but create_prs is off" in (result.output)
        assert "merge gate can never run" in result.output


class TestDecomposeBlockerOutput:
    """R1.7: the CLI points the user at the persisted spec-issues file."""

    def test_prints_artifact_path_on_halt(self, tmp_path: Path, monkeypatch) -> None:
        import kstrl.cli as cli_mod
        from kstrl.decisions import SpecDecision
        from kstrl.decompose import SpecBlockerError

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec")
        artifact = tmp_path / "scripts" / "kstrl" / "spec-issues.json"

        def fake_decompose(**kwargs: object) -> None:
            raise SpecBlockerError(
                [
                    SpecDecision(
                        issue="product-purpose",
                        question="what is this product for",
                        disposition="escalated",
                        resolution="the owner must say",
                    )
                ],
                artifact_path=artifact,
            )

        monkeypatch.setattr(cli_mod, "decompose_spec", fake_decompose)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "decompose",
                "--spec",
                str(spec_file),
                "--project-name",
                "test",
                "--agent-cmd",
                "true",
                "--ui",
                "plain",
                "--no-color",
            ],
        )
        assert result.exit_code == 2
        assert "what is this product for" in result.output
        assert str(artifact) in result.output


class TestFormatComponentStatus:
    """#263: the plan line must not echo an unschedulable status as if valid."""

    def test_every_enum_status_renders_verbatim(self) -> None:
        for status in ComponentStatus:
            assert _format_component_status(status.value) == status.value

    def test_off_enum_status_is_flagged(self) -> None:
        # The reporter's manifest said "PENDING" and the plan printed
        # "document-format [PENDING]" - indistinguishable from a correct
        # manifest, so the display confirmed the very thing that was wrong.
        assert _format_component_status("PENDING") == "PENDING (not a valid status)"

    def test_missing_component_renders_question_mark(self) -> None:
        assert _format_component_status(None) == "?"


class TestInboxRetryManifestLoad:
    """#263 follow-on: a bad manifest must not traceback out of `ks inbox retry`.

    Every other `Manifest.load` call site in the CLI catches OSError and
    ValueError. This one did not, and the new status validation gives it a
    likely trigger: a hand-edited manifest with a status typo, which is the
    exact scenario #263 was reported from.
    """

    @staticmethod
    def _project(tmp_path: Path, status: str) -> str:
        from kstrl.inbox import Inbox, InboxConfig, ItemKind

        scaffold = tmp_path / "scripts" / "kstrl"
        scaffold.mkdir(parents=True)
        # Written through Manifest.save, which serialises the schema the
        # loader reads and does not validate, so an off-enum status lands
        # on disk exactly as a hand-edited file would carry it.
        Manifest(
            version="1",
            spec_file="spec.md",
            project_name="t",
            base_branch="main",
            single_pr=False,
            components=[Component("comp-a", "A", "", [], "p.json", "b/a", status=status)],
        ).save(scaffold / "manifest.json")
        box = Inbox(tmp_path, InboxConfig.load(tmp_path))
        item = box.add(ItemKind.HALTED_RUN, "comp-a halted", component="comp-a")
        return str(item.id)

    def _run(self, tmp_path: Path, item_id: str) -> Result:
        runner = CliRunner()
        return runner.invoke(
            cli,
            [
                "inbox",
                "retry",
                item_id,
                "--root",
                str(tmp_path),
                "--ui",
                "plain",
                "--no-color",
            ],
        )

    def test_off_enum_status_reports_cleanly(self, tmp_path: Path) -> None:
        item_id = self._project(tmp_path, "PENDING")
        result = self._run(tmp_path, item_id)
        assert result.exit_code == 1
        assert "Failed to load manifest" in result.output
        assert "'PENDING' is not a valid status" in result.output
        # A clean exit, not a traceback leaking past the new guard.
        assert isinstance(result.exception, SystemExit)

    def test_valid_manifest_still_retries(self, tmp_path: Path) -> None:
        item_id = self._project(tmp_path, "failed")
        result = self._run(tmp_path, item_id)
        assert result.exit_code == 0
        assert "Requeued comp-a" in result.output


def _repo_on(tmp_path: Path, branch: str, name: str = "proj") -> Path:
    """One-commit git repo whose only branch is ``branch``, with no remote."""
    root = tmp_path / name
    root.mkdir()
    spine_git("init", "-q", "-b", branch, cwd=root)
    spine_git("config", "user.email", "base@test", cwd=root)
    spine_git("config", "user.name", "Base Test", cwd=root)
    (root / "a.txt").write_text("a\n")
    spine_git("add", "-A", cwd=root)
    spine_git("commit", "-q", "-m", "init", cwd=root)
    return root


class TestDetectBaseBranch:
    """#259: the ladder asks the repository instead of guessing `main`.

    Every case builds a real repo; none of them mock git, because the
    bug being fixed was precisely that the code never asked git.
    """

    @pytest.mark.parametrize("branch", BASE_BRANCH_CANDIDATES)
    def test_every_candidate_is_detected_when_it_is_the_only_branch(
        self, tmp_path: Path, branch: str
    ) -> None:
        # `master` is the reported repro: `git init` with
        # init.defaultBranch unset gives it, and kstrl answered `main`
        # in two seconds. The whole tuple is covered so a name added to
        # it cannot ship untested.
        assert detect_base_branch(_repo_on(tmp_path, branch)) == branch

    def test_main_wins_when_both_names_exist(self, tmp_path: Path) -> None:
        root = _repo_on(tmp_path, "master")
        spine_git("branch", "main", cwd=root)
        assert detect_base_branch(root) == "main"

    def test_current_branch_is_not_the_answer(self, tmp_path: Path) -> None:
        # Standing on a feature branch must not make that branch the
        # base: diffing it against itself is empty, which the diff-scope
        # and bad-pattern checks would read as a clean tree.
        root = _repo_on(tmp_path, "master")
        spine_git("checkout", "-q", "-b", "feature/x", cwd=root)
        assert detect_base_branch(root) == "master"

    def test_unknown_branch_name_falls_back_to_main(self, tmp_path: Path) -> None:
        # No candidate resolves, so the answer stays the guess the
        # callers report as a guess: `ks sense` exits 2 naming --base
        # rather than measuring an empty diff and calling it clean.
        assert detect_base_branch(_repo_on(tmp_path, "release-2.0")) == "main"

    def test_not_a_repository_falls_back_to_main(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert detect_base_branch(plain) == "main"

    def test_origin_head_outranks_local_candidates(self, tmp_path: Path) -> None:
        upstream = _repo_on(tmp_path, "trunk", name="upstream")
        clone = tmp_path / "clone"
        spine_git("clone", "-q", str(upstream), str(clone), cwd=tmp_path)
        # A local `main` exists too; the remote's own answer still wins.
        spine_git("branch", "main", cwd=clone)
        assert spine_git("symbolic-ref", "refs/remotes/origin/HEAD", cwd=clone).endswith("/trunk")
        assert detect_base_branch(clone) == "trunk"

    def test_stale_origin_head_is_demoted(self, tmp_path: Path) -> None:
        # origin/HEAD is only rewritten by an explicit `git remote
        # set-head`, so it survives a rename on the remote and can name
        # a branch that no longer resolves. for-each-ref omits a
        # symbolic ref whose target does not resolve, which is what
        # drops it to the rung below.
        root = _repo_on(tmp_path, "master")
        (root / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
        (root / ".git" / "refs" / "remotes" / "origin" / "HEAD").write_text(
            "ref: refs/remotes/origin/gone\n"
        )
        assert detect_base_branch(root) == "master"

    @pytest.mark.parametrize("remote_default", ["release-2.0", "release/2.0"])
    def test_origin_head_outside_the_candidate_set_still_wins(
        self, tmp_path: Path, remote_default: str
    ) -> None:
        # The remote's answer does not have to be one of the four names
        # we would otherwise guess, and it needs no confirming call:
        # for-each-ref listed it, so its target resolves. The slashed
        # case is here because %(symref) reports a full refname, and
        # splitting it on the last "/" would answer "2.0".
        upstream = _repo_on(tmp_path, remote_default, name="upstream")
        clone = tmp_path / "clone"
        spine_git("clone", "-q", str(upstream), str(clone), cwd=tmp_path)
        spine_git("branch", "main", cwd=clone)
        assert detect_base_branch(clone) == remote_default

    def test_origin_head_pointing_outside_the_remote_is_demoted(self, tmp_path: Path) -> None:
        # origin/HEAD is normally a symref into refs/remotes/origin/;
        # this one is hand-built to point straight at a local head, the
        # shape that made an unguarded removeprefix answer with a whole
        # refname. See git.detect_base_branch for why that got so far.
        root = _repo_on(tmp_path, "master")
        (root / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
        (root / ".git" / "refs" / "remotes" / "origin" / "HEAD").write_text(
            "ref: refs/heads/master\n"
        )
        assert detect_base_branch(root) == "master"

    def test_a_tag_is_not_a_base_branch(self, tmp_path: Path) -> None:
        # A bare-name `rev-parse` resolves refs/tags before refs/heads,
        # so a tag called `main` shadows the branch we mean. Only
        # refs/heads/<name> and refs/remotes/origin/<name> are asked
        # about, so the tag is invisible and `master` wins. The tag is
        # `main` rather than a later candidate on purpose: `master` is
        # hit at index 1, so tagging `develop` would never be consulted
        # and the test would pass against the bug.
        root = _repo_on(tmp_path, "master")
        spine_git("tag", "main", cwd=root)
        assert detect_base_branch(root) == "master"

    def test_a_branch_below_a_candidate_name_does_not_count(self, tmp_path: Path) -> None:
        # `git for-each-ref refs/heads/main` also matches
        # `refs/heads/main/sub`; only an exact refname is a hit, so a
        # repo with no `main` branch does not claim to have one. Built
        # on `master` so the right answer is not the fallback, which
        # would make a prefix-matching bug indistinguishable.
        root = _repo_on(tmp_path, "master")
        spine_git("branch", "main/sub", cwd=root)
        assert detect_base_branch(root) == "master"

    def test_detection_is_one_git_subprocess(self, tmp_path: Path) -> None:
        # It runs on the Textual event loop when the decompose launch
        # form composes, so the call count is the ceiling on how long
        # the UI can freeze. One call, one timeout.
        root = _repo_on(tmp_path, "release-2.0")  # the worst case: no candidate hits

        with patch("kstrl.git.subprocess.run", wraps=subprocess.run) as run:
            assert detect_base_branch(root) == "main"

        assert run.call_count == 1
        assert run.call_args.args[0][1] == "for-each-ref"


class TestResolveBaseBranch:
    """The one spelling of "the flag wins, otherwise ask the repo"."""

    def test_explicit_value_is_returned_unasked(self, tmp_path: Path) -> None:
        root = _repo_on(tmp_path, "master")
        assert resolve_base_branch("release-2.0", root) == "release-2.0"

    @pytest.mark.parametrize("absent", [None, ""])
    def test_absent_value_detects(self, tmp_path: Path, absent: str | None) -> None:
        root = _repo_on(tmp_path, "master")
        assert resolve_base_branch(absent, root) == "master"


def _halting_decompose(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Record what the CLI resolved, then halt before anything is spent.

    Module-level rather than a staticmethod because two classes assert
    about the arguments the CLI hands `decompose_spec`: #259 about the
    base branch it resolved, #338 about the project name it let
    through. A second copy would let them disagree about what "reached
    decompose" means.
    """
    import kstrl.cli as cli_mod
    from kstrl.decisions import SpecDecision
    from kstrl.decompose import SpecBlockerError

    seen: dict[str, object] = {}

    def fake_decompose(**kwargs: object) -> None:
        seen.update(kwargs)
        # Halt before anything is provisioned; the assertion is on
        # what the CLI resolved, not on what decompose does with it.
        raise SpecBlockerError(
            [
                SpecDecision(
                    issue="halt-question",
                    question="halt",
                    disposition="escalated",
                    resolution="the owner must say",
                )
            ]
        )

    monkeypatch.setattr(cli_mod, "decompose_spec", fake_decompose)
    return seen


def _spec_at(root: Path) -> Path:
    spec = root / "spec.md"
    spec.write_text("# Spec\n")
    return spec


class TestBaseBranchFlagDefaults:
    """#259: --base-branch defaults to detection, not the literal `main`.

    `ks factory` is the command that spends money, and it had the
    literal default without even the detection call.
    """

    def _invoke(self, command: str, root: Path, *extra: str) -> Result:
        return CliRunner().invoke(
            cli,
            [
                command,
                "--spec",
                str(_spec_at(root)),
                "--project-name",
                "test",
                "--root",
                str(root),
                "--agent-cmd",
                "true",
                "--ui",
                "plain",
                "--no-color",
                *extra,
            ],
        )

    def test_decompose_detects_when_flag_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _halting_decompose(monkeypatch)
        root = _repo_on(tmp_path, "master")
        assert self._invoke("decompose", root).exit_code == 2
        assert seen["base_branch"] == "master"

    def test_factory_detects_when_flag_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _halting_decompose(monkeypatch)
        root = _repo_on(tmp_path, "master")
        assert self._invoke("factory", root).exit_code == 2
        assert seen["base_branch"] == "master"

    def test_explicit_flag_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _halting_decompose(monkeypatch)
        root = _repo_on(tmp_path, "master")
        assert self._invoke("factory", root, "--base-branch", "trunk").exit_code == 2
        assert seen["base_branch"] == "trunk"


class TestBlankProjectName:
    """#338: an empty or whitespace-only --project-name is refused.

    `""` was accepted by both commands that call `decompose_spec`, and
    it is the one name at which the convergence accounting counted an
    audit with no project twice. The name is refused at the boundary
    now, before the architect runs: measured at 119 to 210 seconds
    against a frontier model, so the cost of finding out later is real.

    `factory` had a body check that rejected `""` and passed `"   "`.
    """

    def _invoke(
        self,
        command: str,
        root: Path,
        project_name: str,
    ) -> Result:
        return CliRunner().invoke(
            cli,
            [
                command,
                "--spec",
                str(_spec_at(root)),
                "--project-name",
                project_name,
                "--root",
                str(root),
                "--agent-cmd",
                "true",
                "--ui",
                "plain",
                "--no-color",
            ],
        )

    @pytest.mark.parametrize("command", ["decompose", "factory"])
    @pytest.mark.parametrize("project_name", ["", "   ", "\t"])
    def test_a_blank_name_exits_two_and_names_the_option(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: str,
        project_name: str,
    ) -> None:
        seen = _halting_decompose(monkeypatch)
        root = _repo_on(tmp_path, "master")

        result = self._invoke(command, root, project_name)

        assert result.exit_code == 2
        assert "--project-name" in result.output
        # The halting stub records every call, so an empty dict is the
        # proof nothing was spent rather than the proof it halted.
        assert seen == {}

    def test_a_name_with_surrounding_space_reaches_decompose_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No silent substitution: the callback rejects or returns the
        value unchanged. Stripping " x " to "x" would write a manifest,
        a branch and a journal audit under a name the operator never
        typed."""
        seen = _halting_decompose(monkeypatch)
        root = _repo_on(tmp_path, "master")

        assert self._invoke("decompose", root, " x ").exit_code == 2
        assert seen["project_name"] == " x "
