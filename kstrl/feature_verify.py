"""#288: the `ks feature` verification report.

Split out of ``feature_cmd`` when that module crossed the 800-line
ratchet. One job: run the read-only mechanical checks against the
operator's live checkout and SAY what they found, to the terminal and to
the event stream. It has no say in what `ks feature` does next - no
control flow, no exit code, no repair-loop entry point is reachable from
here, and the only value that flows back is the set of check names that
failed, which the caller passes to the NEXT report so a failure that was
already there before the agent ran is named as pre-existing.

The narrowing that makes the report honest lives in ``verify``
(``narrow_to_undiffed`` and ``DIFF_DEPENDENT_CHECKS``), next to the
checks it turns off, because a check added there is a check this flow
must decide about.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import git
from kstrl.events import Event, VerificationResultEvent
from kstrl.loop import STOP_EXIT_CODE, LoopResult, determine_branch
from kstrl.verify import (
    DIFF_DEPENDENT_CHECKS,
    ResolvedVerifyCommands,
    VerificationResult,
    VerifyConfig,
    narrow_to_undiffed,
    pin_verify_commands,
    resolve_verify_commands,
    run_undiffed_verification,
    self_critique_progress_path,
)

if TYPE_CHECKING:
    from kstrl.config import KstrlConfig
    from kstrl.ui.base import UI


def resolve_feature_verify_config(
    root_dir: Path,
    *,
    no_verify: bool = False,
) -> VerifyConfig | None:
    """The project's ``[verify]`` config, narrowed to what it can measure here.

    ``no_verify`` (``--no-verify``) returns None, which is the sentinel
    `ks run` and `ks factory` already use and which #261 defines as "no
    gate runs, so state nothing": the loops get no
    VERIFY_COMMANDS_PROMPT block and the reports do not run. It is a
    keyword here rather than a conditional at the call site because
    ``run_feature`` is grandfathered at the cognitive ratchet, so a new
    branch there is a refusal at commit time - and because there is one
    right answer to "what config does this flow use", which belongs in
    the one function that answers it.

    The narrowing is ``verify.narrow_to_undiffed``, which lives next to
    the checks it turns off and to ``verify.DIFF_DEPENDENT_CHECKS``, the
    list this module narrates. Measured on a throwaway project (#288),
    one `ks feature` run per row, with a stand-in agent that either
    commits its work or does not:

        agent commits | PRD branchName | git diff main...HEAD
        yes           | feat/demo      | 7 files, incl. src/greet.py
        no            | feat/demo      | 0 files
        yes           | main           | 0 files

    Nothing in this flow commits - ``run_loop`` never does - and the
    branch the loop checks out comes from the PRD's ``branchName``, which
    the operator is free to point at the base branch itself. Two of those
    three configurations leave the diff empty, which is why the narrowing
    applies here.

    ONE object (#261): the same value is handed to the implement and
    repair loops, so the three commands ``VERIFY_COMMANDS_PROMPT`` states
    to the engineer are the three commands that then run on its output.
    The narrowing does not touch the command fields, which is all
    ``resolve_verify_commands`` reads, so the prompt says exactly what it
    would have said with the full config.

    Loaded unguarded, next to ``TimeoutConfig.load`` and
    ``BreakerConfig.load``: ``[verify]`` is a fatal section of the CLI's
    config preflight seam (``cli._PREFLIGHT_EXEMPT`` does not list
    ``feature``), so a ``kstrl.toml`` that would fail here has already
    stopped the command before this flow was entered.
    """
    if no_verify:
        return None
    return narrow_to_undiffed(pin_verify_commands(VerifyConfig.load(root_dir), root_dir))


def baseline_skip_reason(run_config: KstrlConfig, root_dir: Path) -> str | None:
    """Why the pre-implement baseline must not run here, or None.

    The baseline exists to say which failures were already present
    BEFORE the engineer loop, and that claim is only sound if it
    measured the tree the loop actually starts from. It does not always:
    ``run_feature`` reports before it calls ``run_loop``, and ``run_loop``
    checks out the PRD's ``branchName`` in its own preflight (#288 review
    round 2). On a PRD naming an existing branch that is not the one
    checked out now, ``git checkout <branch> --`` swaps the working
    tree's content, so the baseline measured tree A and every later
    report measures tree B - and subtracting one from the other both
    hides failures the agent caused and invents ones it did not.

    This is the same class as #288's own committed-versus-uncommitted
    measurement: a comparison is worth nothing until both sides are
    provably the same measurement.

    Refuses rather than repairs. Doing the checkout here instead would
    put a tree-mutating git call into a flow whose whole contract is that
    it reports and changes nothing, and would narrate the branch twice.
    So the rule is: run the baseline only where the loop's checkout
    provably cannot move the tree.

    - not a repo, ``auto_checkout`` off, or no branch resolved: no
      checkout happens at all.
    - the branch does not exist yet: ``git checkout -b`` branches from
      HEAD and the working tree is untouched.
    - the branch exists and is the one already checked out: a no-op.

    Anything else refuses, including a detached HEAD, where
    ``current_branch`` returns None and no name can match.
    """
    if not git.is_git_repo(root_dir):
        return None
    if not run_config.auto_checkout:
        return None
    branch, _ = determine_branch(run_config)
    if not branch:
        return None
    if not git.branch_exists(branch, root_dir):
        return None
    current = git.current_branch(root_dir)
    if current == branch:
        return None
    return (
        f"the implement loop will check out the existing branch {branch}, and this "
        f"checkout is on {current or 'a detached HEAD'}. A measurement here would be "
        "of a tree the loop never sees, so there is nothing it could honestly be "
        "compared against."
    )


#: Detail lines printed per failing check. A failing gate's details are
#: every parsed failure plus a source-context snippet, and under the
#: embedded TUI each printed line is one Log event on the run bus, up to
#: ``2 + repair_max_runs`` times a run. Twelve shows a couple of
#: failures in full and then says how many were dropped; the complete
#: output is in the command's own log, and `ks sense` prints all of it.
_MAX_DETAIL_LINES = 12


def _announce_verification(
    ui: UI,
    commands: ResolvedVerifyCommands,
    progress_path: Path | None,
    config: VerifyConfig,
    phase: str,
) -> None:
    """Say what is about to run, BEFORE it runs, and what will not.

    The gates capture their output, so the terminal is otherwise dead for
    as long as the project's test suite takes - measured on this repo at
    246s - on a thread this module's docstring says must be able to host
    the TUI. Naming the commands first also lets an operator who does not
    want to wait recognise what they are waiting for.

    ``progress_path`` is ``verify.self_critique_progress_path``'s answer,
    and it is announced because it is a FOURTH check: an operator who set
    ``[verify] require_self_critique`` plus ``progress_file_path`` used to
    get a row in the table that this announcement never mentioned and the
    runbook said could not run (#288 review).

    The harder half is the OPT-IN THAT DID NOT TAKE (round 2).
    ``prd_path`` is always None on this path, so an operator who set
    ``require_self_critique`` and NOT ``progress_file_path`` gets None
    back: there is no PRD to derive a sibling progress log from, the
    check silently does not run, and the report reads as a complete PASS
    over three checks. A skip an operator explicitly asked against has to
    be named, or the report is answering a question it never asked.

    ``dead_code_ruff`` is the third instance of that same rule (#335).
    It is deliberately NOT in :data:`DIFF_DEPENDENT_CHECKS`, because ruff
    scans ``.`` and needs no base to diff against, but ``[verify]
    dead_code_cleanup`` is one toggle owning both dead-code phases and
    ``narrow_to_undiffed`` turns it off, so the phase is suppressed for a
    reason the diff sentence above does not cover. Naming it in the same
    list would have printed a false reason; leaving it out printed no
    reason at all, which was a silent non-measurement the split had just
    introduced.

    Unconditional, like the list above it and NOT keyed on the toggle:
    ``config`` here has already been through
    :func:`resolve_feature_verify_config`, so every one of these toggles
    reads False by the time this function sees it and a condition on one
    would be a branch that can never run - the shape of defect this
    module keeps finding rather than one to add.
    """
    ui.section(f"Verification report ({phase})")
    ui.info(
        "Report only, not a gate: the exit code is unchanged. Not measured "
        "here (no diff to read, see docs/runbook.md): " + ", ".join(DIFF_DEPENDENT_CHECKS)
    )
    ui.info(
        "  also not measured: dead_code_ruff. It reads no diff, but one toggle "
        "([verify] dead_code_cleanup) owns both dead-code phases and this flow "
        "turns it off. Use `ks sense` for it."
    )
    ui.info(f"  running: {commands.test}")
    ui.info(f"  running: {commands.typecheck}")
    ui.info(f"  running: {commands.lint}")
    if progress_path is not None:
        ui.info(f"  reading: {progress_path} (self_critique)")
    elif config.require_self_critique:
        ui.warn(
            "  NOT running self_critique: [verify] require_self_critique is on but "
            "progress_file_path is unset, and this report has no PRD to derive the "
            "progress log from. Set [verify] progress_file_path to the same file as "
            "[paths] progress."
        )


def _narrate_verification(
    ui: UI,
    result: VerificationResult,
    baseline_failing: frozenset[str],
) -> None:
    """Print one line per check, then the verdict and its attribution.

    ``baseline_failing`` is the pre-implement baseline's failing set, and
    ONLY ever that. It used to be re-assigned from each report's own
    result, so a failure the implement loop caused was labelled "already
    failing before the implement loop" by the repair report that followed
    it: the feature telling the operator to ignore the one failure the
    agent actually caused (#288 review round 2). A
    verdict here is about the WHOLE checkout, not about the diff, so
    without it a checkout whose lint was already red before the agent
    started reads as the agent having broken lint. Naming the overlap is
    what makes the verdict attributable; the machine-readable half is the
    baseline's own ``VerificationResultEvent``, which a consumer diffs
    against this one.
    """
    for line in result.report_lines(durations=False, max_detail_lines=_MAX_DETAIL_LINES):
        ui.info(line)
    failed = frozenset(check.name for check in result.checks if not check.passed)
    if result.passed:
        ui.ok(f"verification: PASS ({len(result.checks)} checks)")
    else:
        ui.warn(f"verification: FAIL ({len(failed)} of {len(result.checks)} checks failed)")
    pre_existing = sorted(failed & baseline_failing)
    if pre_existing:
        ui.warn(
            f"  already failing before the implement loop: {', '.join(pre_existing)}. "
            "This report measures the whole checkout, not the diff."
        )


def report_verification(
    ui: UI,
    emit: Callable[[Event], None],
    component: str,
    root_dir: Path,
    verify_config: VerifyConfig | None,
    phase: str,
    *,
    loop_result: LoopResult | None = None,
    stop_check: Callable[[], bool] | None = None,
    skip_reason: str | None = None,
    baseline_failing: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Run the read-only mechanical checks and REPORT what they found.

    Returns the set of check names that FAILED. ONLY the BASELINE call's
    return may be kept: it is the before-picture every later report is
    attributed against, and the later reports' returns must be discarded.
    Re-assigning it was #288 review round 2's first finding, and it
    inverted the feature - the repair report labelled the failure the
    implement loop had just caused as "already failing before the
    implement loop". A report that did not run returns
    ``baseline_failing`` unchanged, so a skipped or stopped report never
    silently empties the chain.

    Report only. `ks feature` keeps its control flow and its exit codes:
    a failing check does not halt the flow, does not change what it
    returns, and is not routed into the repair loop. The one thing that
    changes is that the operator - and, under
    ``--implementation-auto-run``, the event stream, where there is no
    operator at the screen - is told.

    Nothing here may raise into the flow, which is why the whole
    measurement is wrapped: ``check_test_suite`` catches
    ``TimeoutExpired`` and nothing else, so a ``Popen`` that fails on
    EMFILE or a removed cwd would otherwise propagate out of an ADVISORY
    report and take the command with it, ending ``events.jsonl`` at
    ``phase_started`` with no ``phase_completed`` and no
    ``run_completed`` - a component a dashboard shows as running forever.
    Every other blocking call in this flow already has that guard.

    ``loop_result`` carries the exit-path rule, and both halves of it are
    refusals. ``None`` means the pre-implement BASELINE, which is not
    about a loop and is subject only to the stop check.

    ``iterations == 0`` is a loop that never called the agent (a failed
    branch checkout, a stop before iteration 1). Every early return
    upstream - understand incomplete, review gate declined, review gate
    unavailable in a non-TTY, a PRD with no user stories - leaves the
    same way, with no production code written, and a verification verdict
    over work that was never attempted is noise.

    ``exit_code == 130`` is the operator pressing stop between
    iterations. ``stop_check`` is the same refusal one iteration earlier:
    ``run_loop`` returns that code only from its top-of-iteration probe
    (#288 review), so a stop pressed DURING the final iteration comes
    back as an ordinary exit 0 and the flag is still set here. Either
    way, making somebody who pressed stop wait out a test suite is the
    opposite of what they asked for: measured on this repo, 246s.

    A stop pressed during the measurement itself is NOT cancellable, and
    is bounded by ``3 x [verify] subprocess_timeout`` (900s at the
    default) because ``run_scrubbed`` kills each process group at its own
    deadline. That window is the same shape as, and half the size of, the
    one the agent call inside ``run_loop`` already has (``[timeout]``
    ``agent_iteration``, 1800s); closing it needs cooperative
    cancellation inside the shared checker, which is not this flow's to
    add.

    ``read_only=True`` is the mode ``ks sense`` already uses to point
    this same function at a live checkout (R10.1): the two checks that
    would rewrite the tree they measure are forbidden there.

    ``prd_path=None`` skips the PRD-derived checks, exactly as ``ks
    sense`` does. ``prd_stories`` re-reads the flag the agent itself
    set, which is a self-report rather than an independent measurement;
    and on the repair exits there are two PRDs (the operator's and the
    generated repair PRD) with no single right answer for which one this
    report is about. The independent measurement here is the commands.
    """
    if loop_result is not None and (
        loop_result.iterations == 0 or loop_result.exit_code == STOP_EXIT_CODE
    ):
        return baseline_failing
    if stop_check is not None and stop_check():
        return baseline_failing
    if verify_config is None:
        # ``--no-verify``, the same sentinel `ks run` and `ks factory`
        # already use. One line rather than a section: the operator asked
        # for this, and the engineer prompt is losing its
        # VERIFY_COMMANDS_PROMPT block for the same reason, which is the
        # consequence worth naming once per report rather than not at all.
        ui.info(f"Verification report ({phase}) skipped: --no-verify")
        return baseline_failing
    if skip_reason is not None:
        # Said out loud, never silently. No VerificationResultEvent: the
        # skip measured nothing and did not fail, and the two shapes this
        # function does emit both mean something ran or tried to. A
        # consumer reading events.jsonl sees no row for this phase, which
        # is the correct "there is no baseline to subtract".
        ui.warn(f"Verification report ({phase}) skipped: {skip_reason}")
        return baseline_failing

    started = time.monotonic()
    try:
        # INSIDE the try, all of it. Resolution is not free of I/O:
        # resolve_verify_commands reaches _default_typecheck_command,
        # which opens and parses pyproject.toml, and a file with one
        # non-utf-8 byte raised UnicodeDecodeError - a ValueError - past
        # a fail-closed `except (TOMLDecodeError, OSError)` and out of an
        # ADVISORY report, taking `ks feature` down at the BASELINE
        # before the agent had run (#288 review round 2). That except is
        # widened too, but the boundary is what makes the guarantee
        # structural: nothing this function does to produce a report may
        # escape it, not just the measurement.
        commands = resolve_verify_commands(verify_config, root_dir)
        progress_path = self_critique_progress_path(verify_config, root_dir, None)
        _announce_verification(ui, commands, progress_path, verify_config, phase)
        # Every argument that decides whether a check can honestly run is
        # owned by this callee, so no diff-consuming check is reachable
        # from here by construction rather than by convention.
        result = run_undiffed_verification(root_dir, verify_config)
    except Exception as exc:  # noqa: BLE001 - an advisory report may not halt the flow
        detail = f"{type(exc).__name__}: {exc}"
        ui.err(f"verification: could not run ({detail})")
        # passed=False with an EMPTY checks tuple is the unambiguous
        # "nothing was measured and it did not succeed". It is never a
        # pass, and it cannot be mistaken for one by a reader doing
        # all(c.passed) over the names, because there are none.
        emit(
            VerificationResultEvent(
                component=component,
                passed=False,
                checks=(),
                failures=(detail,),
                duration_seconds=round(time.monotonic() - started, 2),
                phase=phase,
                advisory=True,
            )
        )
        return baseline_failing

    duration = round(time.monotonic() - started, 2)
    _narrate_verification(ui, result, baseline_failing)

    # The same event type the factory's Phase 1 emits for a result of
    # this same function, so `events.jsonl` carries one shape for one
    # measurement and existing readers (reducer, observability log) need
    # no new case. ``phase`` and ``advisory`` (#288) are what tell a
    # consumer which loop was measured and that nothing gated on the
    # answer; the ``phase="baseline"`` row is what lets it subtract
    # pre-existing breakage rather than read every failure as the
    # agent's. ``checks`` names what ran, and the suppressed checks are
    # ABSENT from it rather than recorded as passing skips: a machine
    # reader doing ``all(c.passed)`` must never see a check that measured
    # nothing counted as a pass.
    # ``not_measured`` (#306) rides along for the same reason the two
    # emitters share an event type at all: a check that was asked for
    # and measured nothing must look the same in `events.jsonl`
    # whichever loop ran it.
    #
    # UNREACHABLE here today, structurally and not by luck.
    # ``narrow_to_undiffed`` rewrites the operator's toggles to False
    # before the checks run, which erases the very bit the sidecar reads
    # - "was this asked for" - so no gap can be produced on this path
    # even though ``_announce_verification`` prints the suppressed names
    # in prose two lines earlier - ``len(DIFF_DEPENDENT_CHECKS)`` of
    # them, plus ``dead_code_ruff``, which the same toggle suppresses for
    # a different reason (#335). Counted rather than written out because
    # the number here was already one stale when the split added a name.
    # Making those names say so in the
    # field is a change to the suppression layer, not to this emitter,
    # and it is the same "one owner for every argument that decides
    # whether a check can honestly run" that #305 tracks. Wired anyway,
    # because an emitter that drops a field it should carry is how the
    # two of them come to disagree.
    emit(
        VerificationResultEvent(
            component=component,
            passed=result.passed,
            checks=tuple(check.name for check in result.checks),
            failures=tuple(check.message for check in result.checks if not check.passed),
            duration_seconds=duration,
            phase=phase,
            advisory=True,
            not_measured=tuple(gap.as_token() for gap in result.not_measured),
        )
    )
    return frozenset(check.name for check in result.checks if not check.passed)
