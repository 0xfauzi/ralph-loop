# kstrl Operator Runbook

Recovery procedures for the failure modes that actually happen during factory runs.

## Phase 1: mechanical verification failed

**Symptom**: `Phase 1 FAILED for <comp_id>: <check_names>`

**Diagnose**:

- `prd_stories`: the agent never set `passes: true` on its assigned story. Either the iteration ran out, or the agent didn't understand the PRD. It also fails with "The PRD is not the one this run started with" when the component rewrote its own PRD in a way no engineer may: a story's criteria, title, priority or id, the approved fixtures, or `branchName`. Only `passes` and `notes` are the engineer's to write. Restore the file from the pre-run copy in the main checkout; do not adjust what the component is measured against. Editing `allowedPaths` is NOT this failure and never has any effect: scope is resolved once, before the first engineer call, and neither guard re-reads the PRD for it. `specIssues` is not compared either, and it is not validated beyond being an array: it is the architect's spec audit, routed here so the engineer reads it, and no gate judges the component against it. A component may annotate, resolve or delete that block and nothing will report it, including on a later iteration, since the worktree copy is seeded once. If you need the copy no component can reach, it is `scripts/kstrl/spec-issues.json`, written before any worktree existed.
- `test_suite`, `typecheck`, `linter`: the project's commands failed. To inspect the failed state, re-run with `--keep-worktrees-on-failure`: by default cleanup removes component worktrees at the end of the run, so `.kstrl/worktrees/<comp_id>/` will not survive a failed run without the flag. With the flag set, check the preserved worktree and rerun the command manually.
- `diff_scope`: the agent wrote files outside `ALLOWED_PATHS`. Tighten the allowlist or relax it as appropriate. Do NOT widen it to cover kstrl's own files: on `ks factory`, `ks run` and `ks understand`, the component's PRD, its progress log and `scripts/kstrl/codebase_map.md` are carved out automatically per component and reported separately in the failure as `plus harness artifacts:`. Widening to the bare `scripts/kstrl/` prefix to reach them exposes the manifest and every sibling component's PRD. `ks feature` carves out its own `.kstrl/logs/` run directory the same way. The allowlist it enforces is the one this run resolved before its first engineer call, from the component's `allowedPaths` or, when the PRD carries none, from the run-wide `--allowed-paths`; the `component_scope_resolved` event in the run's `events.jsonl` records which, per component. Editing the PRD mid-run does not move it. A scope that could not be READ at all is a different check, `scope_unreadable`, below.
- `scope_unreadable`: this run has no scope for the component that it can trust, so it refuses without judging any diff. Two different faults produce it and they have different remedies, so read the `Error:` line before acting: either the component's pre-run PRD could not be read or parsed, or the manifest and the run's resolved scope disagree about which components exist and the component was never given a plan-time scope at all. The first is fixed by restoring the PRD in the main checkout, the second by re-running the decomposition so the manifest and the scope snapshot are built together. **Setting `--allowed-paths` fixes neither**, even though it is the fallback when a PRD simply carries no `allowedPaths`: scope resolution returns `unresolved` before it reaches the flag, on the argument that a scope nobody could read is not a scope that does not exist, so a re-run with the flag set fails identically. Nothing the engineer writes can clear it either, since the file is outside every worktree and the snapshot is fixed for the life of the run.

  Where you see it depends on how far the component got. The scheduler checks the component's scope immediately before launching an engineer and fails it there (#294), so the normal case costs no agent call at all and never reaches Phase 1; the `phase` on the failure record is `scope`, not `verify`. `_preflight_component_scope` catches the common case earlier still, refusing the whole run before the first component starts. The Phase 1 check of the same name is the backstop for a worktree that already had an engineer in it, and is ungated: `[verify] check_diff_scope = false` switches off the diff COMPARISON, not the report that there was nothing to compare against. Because the launch gate sits outside Phase 1, turning verification off entirely does not turn this refusal off. Either way the component is failed immediately rather than retried, so you will not see three attempts burn on it, and the failure signature is `scope_unreadable:...`, which `ks autonomy` counts as an infrastructure abort rather than a decisive run.
- `bad_patterns`: a secret-like pattern landed in the diff.
- `dead_code` / `mutation`: the optional advanced checks failed.
- `self_critique`: the engineer prompt's self-critique block is missing, too short, or filled with placeholder content.

**Resolve**: the agent retries automatically up to `FactoryConfig.max_retries` (default 3). After that the component is marked FAILED and cascade-skips dependents.

The retry prompt shows the failures measured in the most recent attempt first, under `## Current failures`, because those are the ones still happening; findings from an earlier attempt whose gate never ran again are listed separately under `## Not re-measured`, and the retry prompt tells the agent to re-check them rather than assume they still apply. Findings that a later attempt re-measured or got past are replaced by a one-line count under `## Resolved or superseded`, so reading the prompt and not finding an old failure means it was cleared, not lost.

Manual options:

1. Edit the PRD to clarify the story; re-run.
2. Increase `--max-retries`.
3. Re-run with `--keep-worktrees-on-failure`, then run the agent loop manually against the preserved worktree to debug interactively.

## `ks feature` reported `verification: FAIL`

**Symptom**: `verification: FAIL (N of M checks failed)` after the baseline, the implement phase or a repair attempt, with the command's exit code unchanged.

**What it is**: `ks feature` runs the same mechanical checker `ks factory` and `ks sense` run, in the read-only mode `ks sense` uses, against your live checkout: once immediately before the implement loop, and again after every engineer loop that actually called the agent (#288). It REPORTS. It does not gate: the flow's control flow and exit codes are exactly what they were, and a failure is not routed into the repair loop. Each verdict also lands in the run's `events.jsonl` as a `verification_result` event carrying `phase` (`baseline`, `implement`, `repair-2`) and `advisory: true` - the copy that matters under `--implementation-auto-run`, where nobody is reading the terminal.

**Was it the agent, or was your tree already broken?** That is what the `baseline` row is for. This report measures the WHOLE checkout, not the diff, so a checkout whose lint was already red before the agent started would otherwise read as the agent having broken lint. On the terminal, a failure that was already failing at baseline is called out under the verdict. In `events.jsonl`, diff the `phase: "baseline"` event's `failures` against the later one: anything in both was not this loop's doing. Only the baseline is ever the before-picture: a repair report is attributed against the baseline, never against the implement report that preceded it, so a failure the implement loop caused is never excused by the repair report that follows.

**When there is no `baseline` row**: the comparison is unavailable, not merely missing, and the reports that follow stand on their own. Three reasons, all stated on the terminal.

- `Verification report (baseline) skipped: --no-verify` - you declined.
- `Verification report (baseline) skipped: the implement loop will check out the existing branch ...` - your PRD's `branchName` names a branch that exists and is not the one checked out now. `run_loop` performs that checkout AFTER the baseline would have run, so a measurement here would be of a tree the loop never sees. Check out the branch yourself first and re-run if you want the baseline.
- You stopped the run before it got there.

**What it measures**: `test_suite`, `typecheck` and `linter` - your `[verify]` commands, printed before they run so a long suite is not a dead terminal. Plus `self_critique` if, and only if, you set BOTH `[verify] require_self_critique` and `[verify] progress_file_path`; without the second key there is no PRD sibling to derive the log from on this path, so the check is skipped rather than pointed at a file that may not exist. When it does run it is announced on a `reading:` line. When you asked for it and it cannot run, the report says `NOT running self_critique` and names the missing key rather than quietly reporting a clean pass over three checks. Point `progress_file_path` at the same file as `[paths] progress`, which is where the engineer prompt tells the agent to write.

The three commands are resolved ONCE per run and pinned, so the baseline, every later report, and the `VERIFY_COMMANDS_PROMPT` block the engineer is given all name the same three strings. Without that pin, `typecheck` is a function of your `pyproject.toml` - `uv run mypy` when `[tool.mypy] files` is set and `uv run mypy .` when it is not - so an agent adding a mypy scope mid-run would make the baseline and the later reports run different commands.

Every check that answers its question by reading `git diff <base>...HEAD` is deliberately skipped and named in the report (`verify.DIFF_DEPENDENT_CHECKS`: `diff_scope`, `bad_patterns`, `policy_envelope`, `test_adequacy`, `dead_code`, `mutation_testing`). Nothing in this flow commits, and the branch it works on comes from the PRD's `branchName`, which may be the base branch itself, so that diff is empty whenever the agent left its work uncommitted or worked on the base branch - and an empty diff is indistinguishable from nothing changed. Those checks would report a pass having measured nothing, which is worse than not running. Use `ks sense` when you want them, on a checkout where the diff is real.

**When it does not run**: any exit before the implement loop (understand incomplete, review gate declined, review gate unavailable in a non-TTY, a PRD with no user stories), an engineer loop that never called the agent, and a loop you stopped. The last one is deliberate: stopping should not make you wait out a test suite. A stop pressed while a report is already running cannot be honoured - each command is killed at its own `[verify] subprocess_timeout` (300s default), so that window is bounded by three of them.

**What it costs, and how to decline**: one test + typecheck + lint run for the baseline, plus one per engineer loop, so `2 + repair_max_runs` at worst. Measured on the kstrl repo itself: 246s per report, essentially all test suite, against 317-348s for the engineer loop each one follows. On a project with a fast suite it is seconds; on a 20-minute suite it is not.

Pass `--no-verify` to run none of them, the same flag `ks run` and `ks factory` have always had. It also stops the engineer prompt being given the `VERIFY_COMMANDS_PROMPT` block, which is the point: do NOT instead set `[verify] test_command` to a no-op, because the SAME config feeds that block, so the workaround would leave the agent being told a gate will run three commands that do nothing.

**`verification: could not run`**: the measurement itself failed to start (a `Popen` that could not fork, a removed working directory). The event records `passed: false` with an EMPTY `checks` list, which is the unambiguous "nothing was measured". It never halts the flow.

**Diagnose**: run the failing command yourself in the checkout. The `running:` lines in the report name the three resolved commands, and they are in the run's `events.jsonl` and in `.kstrl/logs/feature_<name>/`. Do NOT reach for `ks config show` here: it prints the configured values, so with no `[verify]` section it prints `test_command = None  (default)` and tells you nothing about what ran.

**Resolve**: fix the code, or fix the command in `kstrl.toml`. There is no retry to consume and no gate to override.

## Phase 2: review failed (hard mode)

**Symptom**: `Phase 2 FAILED for <comp_id>: N failures`

**Diagnose**:

- Inspect `comp.review_findings` (also written to the PR body when the PR gets created).
- If the failures are PRD-criterion failures, the diff genuinely does not implement what was asked.
- If the failures are concerns (`scope_creep`, `security_concern`, `test_quality`, `unrelated_change`, `dead_code`, `error_handling`, `copy_paste`), the reviewer surfaced cross-cutting issues.

**Resolve**: the retry path injects the review findings back into the agent's context so the implementer has a concrete checklist. If the reviewer is wrong, switch the run to `--review-mode advisory` and the failures become warnings.

If `ReviewResult.infrastructure_error=True`, the reviewer agent itself failed (timeout, API outage, parse error). Same retry path, but check API health.

## Phase 2: set-point disagreement

**Symptom**: `Phase 2 FAILED for <comp_id>: set-point disagreement on N story(ies); passes reverted in the PRD`, with `failed_check = setpoint`.

A story is marked done when the engineer agent sets `passes: true` in the PRD. That is the agent that did the work reporting on the work, so it is a claim rather than a measurement. R10.3 checks the claim against the reviewer's per-story verdicts, which are an independent reading. This fires when the engineer said done and the reviewer did not confirm it - because it judged a criterion unmet, raised an advisory on one, or never covered the story at all.

**Diagnose**:

- Look for `setpoint_disagreement` findings in the PR body, under the callouts block. Each names the story in `location`, the reviewer's verdict in the explanation, and the criteria it would not pass in the suggestion.
- The PRD itself carries the audit trail: each reverted story gains a `reverted by reviewer (attempt N): <criterion>` note.
- The explanation says how the claim failed to be confirmed, and the three readings mean different things. A verdict of `fail` or `advisory` means the reviewer looked and was not satisfied. "not covered" means it returned no verdict for that story at all, usually a story the diff did not touch. "pass on only N of M acceptance criteria" means it passed everything it judged but did not judge everything: the story is unconfirmed rather than judged unmet, and the reviewer's coverage is what to look at first.

**Symptom, second form**: `Phase 2 FAILED for <comp_id>: set-point agreement cannot be confirmed, the reviewer did not report`.

In advisory review mode a crashed or unparseable reviewer still passes the review (`passed = review_mode != hard`), so with `setpoint_agreement = "block"` a story claiming done would otherwise sail through with nothing having checked it. Nothing is reverted in this case: no evidence points at any story. The failure is recorded as `failed_check = infrastructure` and journalled as `review:infrastructure`, not as a disagreement, because no reviewer disagreed with anything. Check reviewer API health, as for any `infrastructure_error`, and re-run.

**Symptom, third form**: `Phase 2 FAILED for <comp_id>: Set-point agreement cannot be confirmed: the reviewer never ran (adversarial LLM budget (N) exhausted) and a story is still marked passes=true`.

The adversarial budget covers review, security and knowledge distillation together. When it runs out, Phase 2 downgrades to a skip, and in blocking mode a skipped reviewer cannot confirm anything. This does not retry, because retrying cannot recover budget: raise `max_adversarial_calls`, or accept the components already done and re-run the rest.

**Resolve**: the retry resets `passes` to false on each unconfirmed story and puts the disagreement in the agent's context. The engineer's own story selection then picks the story up again, because it takes the highest-priority story where `passes` is false. Nothing needs doing by hand.

If `setpoint_agreement = "block"` is set together with `review_mode = "skip"`, the run warns at startup that the gate can never fire: with no reviewer there is no verdict to confirm with.

If the reviewer is the one that is wrong, set `[factory] setpoint_agreement = "advisory"` (the default). Disagreements are then recorded on the PR and in the journal without failing anything. Note the gate also blocks whenever the autonomy ladder is at L1 or above, regardless of this setting: autonomy tightens a gate and never loosens one, so turning it off there means turning the ladder down.

## Phase 2: the retry loop is diverging (#265)

**Symptom**: `divergence detector tripped: the change is outgrowing the reviewer. Across attempts 1, 2, 3 the review failed every time, the change got larger at every step (...), and not one of the reviewer's blocking findings was retired at any step (...)`.

In advisory mode (the default) this is a warning line and a `review_divergence` finding, and the component keeps retrying. Under `[divergence] mode = "block"` it is terminal, with `failed_check = divergence` and journal signature `review:divergence`.

The loop drove a component the wrong way. Every retry hands the engineer the review findings and asks it to address them, the engineer correctly answers by writing more code, and the change gets larger while the reviewer stays exactly as unhappy. #265 measured one such component at $21.44 and 71 minutes across four attempts, with zero completions, and that is what motivated the detector. It is deliberately narrower than that run: on the #265 trajectory itself (6 blocking findings, then 1, then 10) attempt 2 retired findings, which resets the streak, so this predicate would not have fired there. It catches the case where nothing at all is being retired.

**Diagnose**:

- The message carries the whole case: the attempt numbers, lines changed (added plus removed) per attempt, files touched per attempt, and the reviewer's blocking-finding count per attempt. The same series is on the `review_divergence` event in `.kstrl/runs/<run_id>/events.jsonl`, with `blocking` recording whether the trip actually failed the component. In advisory mode the finding is also journalled to `.kstrl/evolution.jsonl` as `findings_superseded` when the attempt is retried, so it survives a component that later passes.
- Read the per-attempt review findings alongside it. A genuine trip looks like the same objections restated attempt after attempt while the diff climbs. Retiring even ONE blocking finding at any step resets the streak, so a trip means none was retired at any of them.
- The known false-positive channel is the reverse of what most people expect. The retirement half reconstructs a finding's identity from reviewer prose, and any instability there (a reworded criterion, a moved line, a rephrased explanation) makes an old key vanish, which reads as a retirement and resets the streak. The heuristic therefore errs toward staying quiet. If a trip looks wrong, the thing to check is whether the reviewer really was repeating itself verbatim, because that is what it takes to fire.

**Resolve**: split the component into smaller ones, or narrow its PRD. The message says so because that is the only fix: the change has grown past what one review pass can converge on, and another attempt from the same branch can only add to it.

`ks retry <comp_id>` is the override. It resets `retries` and clears the finding stream, and the detector's reading history is in-run only, so a retry starts with a clean slate and a full retry budget. That is the escape hatch when the operator disagrees with a blocking trip.

To stop it failing components, set `[divergence] mode = "advisory"` (the default) so trips are recorded without blocking, or `mode = "skip"` to stop measuring. To make it more patient, raise `[divergence] growth_steps`; it must stay >= 1.

## Phase 2.5: security review failed (hard mode)

**Symptom**: `Phase 2.5 FAILED for <comp_id>: N critical, M high`

**Diagnose**: same logic as Phase 2, but the findings are typed against the security taxonomy. Each finding has `category`, `severity`, `location`, `explanation`, `suggestion`.

**Resolve**:

- For genuine security issues, the retry context goes back to the agent.
- For false positives, switch to `--security-mode advisory` (findings logged, not blocking) or `--security-fail-threshold critical` (only critical findings block).
- If `infrastructure_error=True`, the security reviewer didn't actually run. In hard mode this fails the component; in advisory mode it passes with a warning.

## Phase 3: contract test breaker

**Symptom**: `Contract breaker '<comp_id>' sent back for retry`

**Diagnose**: the merged tier branch's tests failed; Phase 3 attributes the failure to a "breaker" component (the most recent one merged into that tier). The breaker gets reset to PENDING and re-runs.

**Resolve**: the system handles this automatically up to `max_retries`. If it keeps breaking, the integration is genuinely broken: inspect the merged tier branch, fix the spec or the components' contracts, re-run.

## Knowledge layer reports `no_valid_facts`

**Symptom**: `Knowledge: knowledge.no_valid_facts (raw: ...)`

**Diagnose**: the distiller LLM returned output, the JSON parsed, but `_coerce_facts` rejected every fact. Common causes:

- Fact ids don't match `/^fact-\d{3}$/` (e.g. `fact-1` instead of `fact-001`)
- Unknown scope value (the agent invented categories beyond handler/adapter/schema/contract/invariant/gotcha)
- Empty evidence array
- Empty claim text
- Prompt-injection pattern matched in claim text (Phase A1 rejection)

**Resolve**:

- Inspect `.kstrl/knowledge/<comp_id>/<run_id>/_distill_raw.txt` (saved automatically on failure paths) to see the agent's actual output.
- If the agent consistently produces malformed output, the distill prompt may need to be tightened.
- If the failure is `no_facts` (not `no_valid_facts`), the JSON didn't parse at all; usually means the agent emitted prose around the JSON.

## Concurrent factory runs clobbering each other

**Symptom**: One run's worktree disappears or its branch gets force-pushed by the other.

**Diagnose**: on POSIX, Phase A4's `fcntl.flock` on `.kstrl/worktrees/<comp_id>.lock` should prevent this. On Windows there is no flock and the runs race.

**Resolve**: avoid running concurrent factory invocations against the same `root_dir` on Windows. On POSIX, the lock serializes worktree setup but doesn't prevent two runs from doing different work on the same component. Use distinct `root_dir`s for distinct factory invocations.

## Adversarial budget exhausted mid-run

**Symptom**: `Phase 2 SKIPPED for <comp_id>: adversarial LLM budget exhausted`

**Diagnose**: `FactoryConfig.max_adversarial_calls` is set and the count of review + security + distillation calls has hit the cap.

**Resolve**: increase the cap, or accept that later components run without adversarial phases. The mechanical pipeline (Phase 1) still gates them.

## Spec was rejected by the architect

**Symptom**: factory exits with code 2; stderr lists `[blocker/<kind>] <summary>` lines.

**Diagnose**: the architect's red-team pass found blocker-severity issues. The pipeline halts rather than implementing against a vague spec.

**Resolve**: read the surfaced issues, edit the spec to address them, re-run. There is no override flag; that's deliberate: the alternative was producing brittle code from ambiguous instructions.

## Calibration suite reports a regression

**Symptom**: `tests/test_calibration.py` test fails after a prompt edit; detection rate dropped.

**Diagnose**: the prompt change made the role miss a planted bug it previously caught.

**Resolve**: either revert the prompt change or update the fixture's `must_detect` if the change deliberately narrowed scope. Do not just unskip the test: a calibration regression is the signal you wrote the system to produce.

**With the autonomy ladder on**: `python -m kstrl.calibration compare <old> <new> --root <repo>` also opens a `calibration_drift` inbox item, deduped on the new baseline's timestamp so re-reading the report does not add rows. With `[autonomy] demote_on_calibration_regression = true` it additionally demotes one level, trigger `calibration_regression`, once per new baseline however often the comparison is re-run. With the ladder off it prints `autonomy ladder disabled; regression recorded in the report only`, so the absence of an item is stated rather than silent. The exit code is unchanged either way (0 pass, 1 regression); 2 now also covers a `kstrl.toml` that will not load, because "the config is broken" must not read as "the ladder is off".

## The dashboard (TUI)

`ks factory` on a terminal runs the embedded dashboard by default
(`--no-tui`, `--ui plain`, or `KSTRL_NO_TUI=1` opt out; automatic
selection uses plain output for non-TTY stdio, while explicit `--tui`
requires a terminal). `ks dash` attaches a read-only
dashboard to a live run from another terminal, or replays a finished
one (`--run-id` takes a unique prefix; newest run is the default).

Keys: `enter` opens a component's detail (phase timeline, findings,
live transcript, evidence paths), `escape` returns, `f` toggles
transcript follow, `c` reopens a pending E6 checkpoint, `q` quits.

Quit semantics differ by mode. In `ks dash`, `q` detaches
immediately - the run is not yours to stop. Embedded, `q` asks first:
confirming group-kills in-flight agents, runs the worktree cleanup
pass, flushes the manifest, and exits 130; a second `q` (or second
Ctrl-C) force-kills. This is also what Ctrl-C now does in plain mode -
the pre-TUI behavior (skipped cleanup, orphaned agents) was a bug,
fixed in the same rewrite.

The E6 checkpoint modal shows the diff excerpt, review + security
findings, and the attempt's spend; approve/reject/retry with
`a`/`r`/`t`, or `escape` to leave it pending (the run stays blocked -
that is what a checkpoint is - and the banner points back at it).

Tradeoff to know: in embedded mode, notify hooks run with their
output captured (a hook writing to the terminal would corrupt the
alt screen - measured in the Stage 0 spike), so a `printf '\a'`
terminal bell only rings in plain mode. Everything the dashboard
shows also exists on disk: `.kstrl/runs/<run_id>/events.jsonl`
(schema-v2 event stream), `components/<id>/engineer.log` (agent
transcripts), `components/<id>/{review,security,distill}.log` (phase
transcripts), and `orchestrator.log` (embedded-mode narration). When
a run breaks, those files are the record; the TUI is only a view.

## Safe mode

kstrl is in **safe mode** whenever any of the four signals below is
degraded. It is one name for four states that already existed
separately, and asking about it costs nothing.

Two surfaces print it. `ks status` prints `safe mode: nominal` or
`safe mode: <n> reason(s)` followed by one line per reason, and
`ks serve --dry-run` prints the same block above its admission gates.

The dashboard shows it too. On a terminal `ks status` opens the
dashboard rather than the plain report whenever a run directory exists,
so `f2` opens a safe-mode panel from any screen, and a warning banner
appears under the run masthead the moment a signal goes degraded, naming
the sources.

A function key rather than a letter, and a priority binding rather than
an ordinary one. Neither is cosmetic. Textual's text inputs consume
printable keys before application bindings, so a letter key would
silently type itself into the launch, config, decompose and init fields;
and an ordinary application binding never reaches a system modal such as
the command palette. The argument below only holds if the key always
works.

The banner is hidden while everything is clear, and `f2` is what makes
that safe: the panel distinguishes the three states the banner cannot,
telling "not checked yet" apart from "checked and clear" apart from a
list of reasons. So an absent banner never has to carry a meaning on its
own. On the home screen, where there is room, a chip in the masthead
carries the same three states at a glance.

The run masthead has no chip, and that was measured rather than chosen:
at 120 columns the run header wants 41 cells and the cost meter 79, so
the topbar is already over-subscribed and anything added there costs the
run its own state label.

The dashboard re-checks every few seconds on a background thread, not on
its event poll: the predicate reads a run's whole event stream, and doing
that at the poll rate would stutter the display.

Safe mode itself refuses nothing. Each signal below already refuses
where refusing is right, and the predicate only reads them, so leaving
safe mode always means fixing the underlying signal rather than
clearing a flag.

A reason line looks like this:

```
  safe mode:      1 reason(s)
  - [queue] daily budget exhausted (see docs/runbook.md#queue-paused)
```

The label in brackets is the source; the anchor at the end is the
subsection to read here.

### Control directory untrusted

Live control state (`autonomy.json`, `pause.json`, `spend.json`,
`inbox.jsonl`, `github_processed.json`) must sit outside the tree the
agent can write to. When it does not, kstrl refuses to spend and treats
the queue as paused, because a factory that can edit its own budget
ledger is not bounded by it.

The detail line is the reason verbatim: `XDG_STATE_HOME` resolving under
the repository, legacy in-tree control files left behind by a partial
migration, a control file that is a symlink or resolves outside the
control directory, or a control directory that cannot be created or
listed at all.

To recover, point `XDG_STATE_HOME` outside the repository and finish the
migration. See "Control plane (R8.9)" under
[Where to find things](#where-to-find-things) for the exact layout, and
note that L3+ autonomy refuses to run at all while this is unresolved.

### Autonomy fell back or was clamped

Two distinct things land here.

**Fell back.** `AutonomyState.load` validates every field, not just the
level. Any malformed field, history entry, or out-of-range level
discards the whole record and returns a fresh L1 Supervised state,
because the safe direction for unknown autonomy is the least autonomy.
The earned level is lost. Restore `autonomy.json` from a backup if you
have one, or re-earn the level; `ks autonomy status` shows what the next
promotion needs.

**Clamped.** The run is executing below the level the ladder awarded.
Three independent ceilings can do this and the lowest wins: `[autonomy]
max_level` in `kstrl.toml`, `[policy] enabled = false` (L3 is
auto-merge inside the policy envelope, so with no envelope there is
nothing to merge inside and L2 is the ceiling), and control state that
still resolves under the repository (R8.9). The detail line names the
ceiling that fired. Clamping does not consume the earned level: raise
the ceiling and the level returns.

This source is silent while `[autonomy] enabled` is false, which is the
default. A ladder nobody switched on is not a ladder that fell down.

### Queue paused

`ks serve` admits no new work while the queue is paused. A pause is
either deliberate (`ks queue pause`) or self-inflicted by the daemon:
the daily budget stop sets tomorrow's local midnight as `resume_after`
and clears itself, and the poison breaker pauses after consecutive
poisoned items. Run `ks queue` to see the marker, and `ks queue resume`
to lift a pause that no longer applies.

An unreadable pause marker also reads as paused, and the detail line
says so. That is deliberate: resuming unattended spending on the
strength of a corrupt file is the one failure this marker exists to
prevent. Repair or delete `pause.json` in the control directory.

### An adversarial phase did not run

Review or security did not execute for at least one component of the
newest run. The count and the run id are in the detail line, and the
per-component reason is in the pull request body and in that run's event
stream (`.kstrl/runs/<run_id>/events.jsonl`, `phase_skipped` events).

The usual causes are `mode = skip` in `[security]` or the review config,
a security reviewer that was never configured, and the adversarial LLM
budget (`max_adversarial_calls`) running out mid-run. The first two are
choices; the third is worth acting on, because it means components
merged on mechanical checks alone. See also
[Adversarial budget exhausted mid-run](#adversarial-budget-exhausted-mid-run).

This reason clears when the next factory run **completes** without
skipping a phase. A run that is still in flight does not clear it: a run
writes its first event long before it reaches review, so treating "no
skip recorded yet" as "no skip" would clear the verdict the moment the
next run started. Runs of other kinds (`decompose`, `feature`,
`understand`) never clear it either, because they have no phase chain
and so finish clean by construction without ever asking the question.

A separate detail line beginning `could not read run` means the question
could not be answered rather than answered clean: the newest factory run
left no event stream (`[factory] progress_log_enabled = false` writes
accounting files and no events), or its `events.jsonl` could not be
opened. When that happens the last finished run's verdict is still
reported beside it.

## Where to find things

- Tracker for the hardening roadmap: `docs/adversarial-roadmap.md`
- Adversarial design overview: `docs/adversarial-design.md`
- Env-var reference: `docs/env-vars.md`
- Per-run captures: `.kstrl/evolution.jsonl`, `.kstrl/experiments.tsv`
- Run event stream + transcripts: `.kstrl/runs/<run_id>/`
- Distillation debug dumps: `.kstrl/knowledge/<comp>/<run>/_distill_raw.txt` (on failure)
- Control plane (R8.9): `${XDG_STATE_HOME:-~/.local/state}/kstrl/<repo-id>/`
  (`autonomy.json`, `inbox.jsonl`, `spend.json`, `pause.json`,
  `github_processed.json`). Marker `.kstrl/control_relocated` records where
  legacy in-tree files were moved. L3+ autonomy refuses to proceed while
  control state still resolves under the repo.
- Phase F sample real-world run log: `docs/phase-f-run-log.md`
