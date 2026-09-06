# Evolution metrics and journal format

This document defines every metric the evolution layer records: what is
measured, where it comes from, and what it does NOT mean. Definitions
describe the code as implemented (R6.4); nothing here is aspirational.

## Journal: `.kstrl/evolution.jsonl`

Append-only JSONL. Every entry carries `schema_version` so future format
migrations are detectable.

- **Version 2** (current): structured failure signatures (R6.1).
- **Version 1**: entries without a `schema_version` field (pre-R6
  shape). Wave 1 (R4.1) archived the polluted v1 journals to
  `.kstrl/archive/`; they are kept for forensic reference only and are
  never read by current metrics. Fresh journals contain v2 entries only.

### Entry types

| `event_type` | Written by | When |
|---|---|---|
| `component_result` | `EvolutionJournal.record_run` | Once per component at the end of every factory run |
| `findings_superseded` | pipeline `AttemptRecorder.journal_superseded_findings` | When a retry supersedes an attempt's findings (R3.3) |
| `contract_result` | factory `_record_contract_event` | After every contract-test tier, pass or fail (R0.3) |
| `role_usage` | `EvolutionJournal.record_run` | Once per role that spent tokens outside any manifest component (#257) |
| `autonomy_transition` | `autonomy.commit_transition` | Every promotion or demotion of the autonomy ladder |
| `spec_issues` | `decompose._record_spec_issues_event` | Once per spec audit (#280); carries no `run_id` |
| `journal_repair` | `EvolutionJournal.append_entries` | When an append finds the file not newline-terminated, i.e. a previous write was interrupted (#312) |

### `journal_repair`

The journal is append-only, so a crash mid-write leaves an unterminated
tail and the next append would concatenate onto it, making both lines
unparseable. `append_entries` writes a newline first and records that it
did. Reading one of these rows:

| Field | Definition |
|---|---|
| `schema_version` | As above. |
| `timestamp` | UTC ISO-8601 at repair time, NOT at crash time. |
| `event_type` | `journal_repair`. |
| `detail` | Fixed prose describing what was found. |

- **Deliberately carries no `run_id` and no `project`.** Every run
  aggregate windows by the last N distinct `run_id`s, so a repair row
  with one would be one of the N and a single tear would shorten the
  history the metrics above are computed over. It counts towards
  nothing, by construction.
- **What the line above the row is.** POSSIBLE loss, not confirmed
  loss: the line IMMEDIATELY ABOVE the row is the
  interrupted write. If it is a complete JSON object it was a whole
  record that lost only its newline, and it is readable again. If it is
  a fragment, that record was never written and is unrecoverable. The
  row exists so the difference is visible rather than guessed at.
- **Counting.** `ks evolve --status` prints the count when it is
  non-zero (`EvolutionJournal.get_repair_count`), before the
  no-experiments exit, because a journal can hold a repair long before
  any factory run has written `experiments.tsv`. The count is of rows,
  not of incidents, and the two can differ in one direction only.

  It is a LOWER bound: a write split part-way through (residual 4 on
  `append_entries`) can land the newline that isolates the fragment
  without landing the row, and the next append then sees a terminated
  file and adds none.

  It is no longer an UPPER bound as well. Two processes repairing the
  same tear used to write two rows (#330 residual 2). Since #331 the
  append holds `fcntl.LOCK_EX` on the journal's own descriptor across
  the probe and the write, so the second writer probes after the first
  has finished and finds a terminated file: one row is one tear.
  Measured, two processes x 150 appends with 74 tears planted, eight
  runs of each arm: 76 to 86 rows unlocked, exactly 74 locked on every
  run. On a platform with no `fcntl` (Windows) nothing is excluded and
  two rows for one tear are possible again, the same degradation
  `control_lock`, `queue_lock` and the run-level factory lock already
  take there.

### `component_result` fields

| Field | Definition |
|---|---|
| `schema_version` | Journal format version (see above). |
| `timestamp` | UTC ISO-8601, one per run (shared by all components of the run). |
| `run_id` | Factory run id (microsecond precision plus nonce, R1.6). |
| `project` | `manifest.project_name`. |
| `component_id` | Manifest component id. |
| `status` | Terminal manifest status (`completed`, `failed`, `pending`, ...). `pending` with a non-empty `error` means the component was retried and the run ended before another attempt. |
| `retries` | Retry counter at end of run. |
| `error` | Flattened human-readable error of the last failure, `""` on success. Display only: metrics must use `failure_signatures`. |
| `failure_signatures` | R6.1: list of structured `"<check>:<code>"` signatures for the last failed attempt, e.g. `linter:E501`, `typecheck:arg-type`, `test_suite:assertion-error`, `review:scope_creep`, `security:injection`, `diff_scope:files-outside-allowed-scope`, `scope_unreadable:scope-could-not-be-read-at-plan-time-failing-closed`, `contract:tier_1`, `engineer:component-timeout`, `token_budget:exceeded`, `pr:closed-without-merge`. Codes come from the tool parser (ruff rule, mypy error code, pytest exception type) or the finding taxonomy; sites without parser codes record a stable slug of the error text (paths, line numbers, and counts stripped). Empty on success. |
| `check_name` / `error_signature` | Convenience split of the FIRST signature (`check_name:error_signature`). Kept for v1-shaped readers; new consumers should read `failure_signatures`. |
| `failed_phase` / `failed_check` | R3.3 post-mortem pointers: which phase and gate fired last. |
| `duration_seconds` | Wall-clock of the component's LAST attempt, measured from the PENDING->RUNNING transition to the terminal transition (completed / failed / merge-pending / retry scheduled / scheduler backstop). Includes the engineer loop, mechanical verification, review, security review, and PR flow. It is NOT the sum across retries, and 0.0 appears only for components that never started an attempt in this process (e.g. skipped, or state inherited from a crashed run). |
| `iteration_count` | Engineer-loop iterations of the last attempt. |
| `findings` | Full typed Finding stream of the last attempt (E3), attempt-tagged. |
| `findings_summary` | Aggregates of `findings`: `total`, `by_phase`, `by_severity`, `by_category`, `by_owasp`, `infrastructure_errors`. |
| `usage` | R3.1 per-phase token/cost self-reports (lower bounds when `unreported_calls` > 0). |
| `knowledge_utilization` | #191: `{measured, injected, referenced, reason, by_tier}`. Written on EVERY entry. `measured` must be read first: `false` means the run could not measure and the entry is NOT evidence - it is not a zero. `injected`/`referenced` are meaningful only when `measured` is `true`; `measured: true, referenced: 0` IS evidence, of injected facts going unused. `reason` says why an unmeasured entry is unmeasured - see the reason table below. `by_tier` splits the counts across `core` / `dependency` / `sibling`. Entries written before #191 have no key at all, a third distinguishable state that also counts as unmeasured. See the sections below for which components are sampled and how to read the tiers. |

## Experiments: `.kstrl/experiments.tsv`

One row per factory run, appended by `record_run`. Columns:

| Column | Definition |
|---|---|
| `run_id` | Factory run id. |
| `timestamp` | UTC ISO-8601 at record time. |
| `project` | Manifest project name. |
| `components_total` | Number of components in the manifest. |
| `completed` / `failed` / `skipped` | Counts from the run's FactoryResult (skipped = cascade-skipped dependents of failures). |
| `avg_iterations` | Mean engineer-loop iterations over components with `iteration_count` > 0. 0.00 when no component ran. |
| `avg_duration_s` | Mean `duration_seconds` (last-attempt wall clock, see above) over components with a duration > 0. |
| `retry_rate` | Total retries across ALL components divided by `components_total`: the average number of retries per component, NOT the fraction of components that were retried. A run of 4 components where one burned 3 retries records 0.75. Can exceed 1.0. |
| `common_failure` | The most frequent full `"<check>:<code>"` failure signature among FAILED components this run; `""` when nothing failed. |
| `total_tokens` / `total_cost_usd` / `unreported_calls` | R3.1 run totals. Empty string (not 0) when usage tracking was unavailable: zero would misread as "measured, free". Figures are agent-CLI self-reports and are lower bounds whenever `unreported_calls` > 0. |

Rows written before a column existed keep their shorter header;
`get_experiment_trends` (csv.DictReader) tolerates both shapes.

## Concern hit rate (`ks evolve` internals, D8)

`get_concern_hit_rate` consumes `findings_summary.by_category` on
`component_result` entries (R6.2). A component counts as "with concern"
when it has at least one finding in a real category; the synthetic
`infrastructure_error` and `phase_skipped` categories mark
non-execution, not adversarial signal, and are excluded. `by_category`
in the result sums finding counts (not component counts) per category
across the window.

## Fact utilization (`get_fact_utilization`, #191)

Aggregates `knowledge_utilization` across the lookback window and
returns `{runs, components, measured, unmeasured, injected, referenced,
runs_with_referenced}`. Only `measured: true` entries contribute to
`injected`/`referenced`; everything else lands in `unmeasured` and
contributes nothing. An entry that is present but whose counts do not
parse is also counted unmeasured, never as a zero.

This is the query behind the sole remaining L2+ cATO gate in
`docs/remediation-roadmap.md`: "two real factory runs with nonzero
fact-utilization" is `runs_with_referenced >= 2`.

The same measurement is on the event stream as `fact_utilization_measured`
in `events.jsonl`, emitted once per component per attempt on every path
that reaches a measurement point - including components that later fail
review or security, and including the paths where distillation is
skipped or raises. It is deliberately NOT carried on `distill_result`:
one measurement lives in one event, so a consumer folding both cannot
double count, and the utilization record does not depend on a later LLM
phase having run.

### Which components are in the sample

The population is **every component whose diff can be fetched**. That
includes components which fail mechanical verification, review, or
security - a failed component's engineer still received facts and may
well have used them.

Utilization was previously measured in the distill phase, which runs
only after verification, review, and security all pass, so the sample
was successful components exclusively. It is a substring scan costing
no tokens, so there was never a cost reason to defer it.

A verification failure means the diff phase has not run *yet*, not that
no diff exists: the engineer's change is committed in the worktree and
is exactly what verification just inspected. That path therefore
fetches the diff itself rather than writing off every test, typecheck,
and lint failure - which is most of the failure population.

`measured: false` is reserved for a real inability to measure, and the
entry always says which:

| `reason` | When |
|---|---|
| `diff unavailable: <error>` | the `git diff` itself failed |
| `diff unavailable` | the diff phase had already failed |
| `knowledge retrieval failed` | the engineer's prefix could not be built |
| `no injected prefix recorded for this attempt` | nothing was captured at submit |
| `not measured` | the component never reached a measurement point |

### Reading the tiers

`by_tier` splits the same measurement across the prefix's three tiers.
It exists because the totals are denominator-biased: the sibling tier
carries a first-sentence summary of every OTHER component's facts,
rendered in the same shape as full-text core facts, and those are the
claims a component is least likely to echo.

`core_referenced / core_injected` is the sharper question - did the
engineer use what was known about the component it was actually
building? An overall `2/6` alongside a core `2/2` is a very different
result from a genuine `2/6`.

Per-tier counts can sum to **less** than the totals: a claim under a
heading the knowledge module did not write counts in the total but is
attributed to no tier, rather than being silently credited to a real
one. `kstrl/knowledge.py` shares its section-title constants between
the renderer and the tier parser precisely so the two cannot drift and
mis-bin claims while still summing correctly.

### What counts as a reference

Only **added** diff lines and the component's progress log are searched.

A raw unified diff also carries deletion lines and unchanged context. A
claim found in either is not evidence the fact was used - and counting
them meant an engineer could delete the very code that expressed a
fact, or merely edit near it, and still satisfy the
nonzero-utilization gate. That is a false *positive*, the one error
direction a lower-bound metric must not have. `measure_fact_utilization`
takes the diff as a separate `diff=` parameter for this reason: an
artifact is searched raw, so the signature is what prevents a raw diff
being matched unfiltered again.

The progress log is searched whole, because it is not a diff - the
engineer writing about a fact is itself the signal.

### The remaining caveat

**It is a lower bound.** `measure_fact_utilization` is a 30-character
case-insensitive substring match. An LLM that paraphrases a fact it
genuinely used scores as not referencing it, and an added line that
merely happens to contain the claim text scores as using it. The error
is now overwhelmingly in the under-counting direction, but the match is
lexical, not semantic. This is a property of the measurement, not a
defect in the recording, and it bounds every number above.

## Proposals: `.kstrl/proposals/prop-NNN.md`

- IDs are monotonic across `ks evolve` invocations: numbering
  continues after the highest `prop-NNN.md` already on disk (R6.2).
- Existing proposal files are never overwritten; a proposal whose title
  already exists on disk is skipped, not duplicated.
- `ks evolve --apply PROP-NNN` (or `all`) really applies only
  convention-type proposals (computational, target `claude_md`): after
  explicit confirmation it appends the convention to the project
  CLAUDE.md `## Agent Learnings` section and stamps the proposal file
  with `**Applied**: <timestamp>` so a re-apply is a no-op.
  `[evolution] auto_apply_computational = true` skips the confirmation
  prompt. Every other target prints manual instructions (R6.3).
- `[evolution] auto_propose = false` restricts `ks evolve` to
  pattern reporting; no proposal files are generated.
