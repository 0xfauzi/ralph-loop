# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Work in progress toward the Dark Factory cycle (continuous intake, a release
stage, runtime feedback, and an earned-autonomy ladder). See
[`docs/dark-factory-roadmap.md`](docs/dark-factory-roadmap.md) and the
[R8 milestone](https://github.com/0xfauzi/kstrl/milestone/1).

### Fixed

- `ks decompose --project-name`, `ks factory --project-name` and
  `ks queue add --project-name` refuse an explicit empty or
  whitespace-only name at the command line, exiting 2 and naming the
  option, instead of running the architect against it. `queue add`
  keeps its `""` default, which is how a queued item asks `serve` to
  name it `queue-<id>`: the refusal is gated on the parameter source,
  so only a blank the operator typed is refused. A queue item already
  on disk with a whitespace-only name, added before this change, is
  poisoned on its next attempt instead of run: the child `ks factory`
  refuses the name and `serve` files the refusal as needing a human.
  The name is an identity: it keys the journal audits, the decision
  register and, under `--single-pr`, the branch. Related, and the reason
  the boundary check
  is worth having: the convergence report's accounting of audits the
  trend leaves out is now computed by one classifier per audit, so the
  three buckets (this project's, another project's, no project recorded)
  always sum to the audits on disk. At an empty project name the two
  counts were separate predicates asking the same question, so an audit
  recording no project was counted both as this project's and as
  unattributed: five audits on disk reported as eight (#338).

- `kstrl.toml` and the `KSTRL_*` environment are now resolved once, at
  command entry, before a command constructs anything. They used to be
  parsed lazily, by whichever config dataclass first needed its section,
  so a typo failed at the first loader that reached it: on the decompose
  path that is `LinearConfig.load`, which runs after the architect has
  been invoked and paid for (measured at 119 to 210 seconds against a
  frontier model on a real spec), and `KSTRL_MUTATION_THRESHOLD=many` or
  `KSTRL_SECURITY_TIMEOUT=many` left a raw `ValueError` traceback out of
  `ks factory`. The blast radius of a typo therefore depended on which
  section it was in and which command was run. Every section is now
  checked up front and the error names the section, the offending key or
  environment variable, and its value. `[evolution]` is the one section
  that warns and continues, because the journal is an optional audit
  trail; every other section configures a gate, a budget, a boundary or
  a destination, where substituting a default would measure the run with
  something other than what the operator configured. Bare `ks` on a
  terminal is checked too, before the home shell opens, because the TUI
  launches runs in-process.

  One command is exempt from the check itself: `ks init`, which writes
  the file, and would otherwise refuse to replace the very file it
  cannot parse. Three more skip only the entry seam and run the same
  check in their own bodies, under their own contracts: `ks config show`
  prints every row it can resolve and then names each rejected section
  with its key and value, `ks sense` reports through exit 2 and a JSON
  error document, and `ks serve` through exit 2 before it can poison a
  queue item. `ks config show` is the surface guaranteed to run and
  explain whatever else refuses (#272).

- Safe mode on the dashboard: six defects an independent review
  reproduced after the change merged. All three `dock: top` siblings
  reserved row zero and painted over each other, so the checkpoint
  banner hid the safe-mode warning and the warning hid the run header;
  the banners now flow under the docked top bar, which also repairs a
  pre-existing bug where a checkpoint banner covered the run header. The
  panel key moved from `m` to `f2` because a text input consumes
  printable keys before application bindings, so the advertised key
  typed a letter into the launch, config, decompose and init fields
  instead of opening the panel. The background check no longer relies on
  `exclusive=True`, which cancels the asyncio wrapper and not the
  thread: a superseded check still posted its answer, and a slow nominal
  result landing after a fast degraded one cleared the warning, so
  results now carry the sequence they started with and only one check
  runs at a time. The panel and a freshly mounted screen both replay the
  last completed check rather than starting from nothing, so opening the
  panel early no longer left it reading "not checked yet" forever and
  navigating between screens no longer hid an active warning. The panel
  finally has CSS, without which its border title never rendered and the
  dialog filled the screen (R10.4 follow-up).

  A second review round on those fixes found four more, one of them a
  defect the first round's own fix introduced: the in-flight guard
  dropped a timer tick outright, and because the queue is sampled before
  the expensive event-stream read, a check could sample a nominal queue,
  spend seconds on the stream, and keep that stale answer authoritative
  while the pause that arrived during the read was never sampled. A
  dropped tick is now remembered and rerun. The panel binding is also
  marked priority, without which it never reached Textual's command
  palette, which is a system modal that excludes ordinary application
  bindings. The panel's scroller laid out taller than its dialog, so
  overflowing reasons were clipped while `max_scroll_y` stayed zero and
  no key could reach them. And replay-on-mount was unconditional, so a
  panel constructed with real findings rendered the app's nominal state
  instead.

### Added

- Golden patterns: an operator-authored file, injected into every
  engineer prompt. `ks init` scaffolds `scripts/kstrl/golden-patterns.md`
  and you write what a good change looks like in this repository, with a
  file to copy from for each pattern. The distiller records what happened
  and feedforward computes structure; neither says what is wanted, and
  nothing in kstrl did. The path is `[paths] golden_patterns` in
  `kstrl.toml` or `KSTRL_GOLDEN_PATTERNS_FILE`. The block sits between
  the distilled knowledge and the architect's decisions, is read from the
  component's worktree when the file is committed there and from the repo
  root otherwise, and is read verbatim rather than filtered, the way
  `CLAUDE.md` already is: the operator authored it. Absent, empty or
  unreadable costs nothing and never fails a run; past 6000 characters
  (about 1500 tokens) the text is cut at a line boundary and the prompt
  says so and names the file.
- The architect's non-blocker spec findings now reach the engineer. They
  were written to `scripts/kstrl/spec-issues.json` on every decompose and
  nothing in `kstrl/` ever opened that file: across five recorded runs
  against one real spec, 91 majors and minors were printed once and
  discarded. Each finding is now routed into the PRD of the component
  whose surface it touches, under a new optional `specIssues` key that
  carries the severity, kind, summary, location and suggestion verbatim
  plus an `appliesTo` of `component` or `spec`. The rule matches the
  distinctive words of a component's id and title against the finding's
  own `location` and `summary` text and needs two of them, which scored
  precision 1.00 and recall 0.53 against the 31 real findings whose
  location names the component the architect meant. A finding the rule
  cannot place is not dropped: it goes into every component's PRD as
  `appliesTo: spec`, so nothing the audit produced is lost. Halting is
  untouched, a blocker still stops the decomposition before any PRD is
  written, and `spec-issues.json` remains the full durable record. The
  field is deliberately the loosest thing in the PRD: validated only as
  an array, not compared by `PRD.tamper_changes`, and stripped out
  before the PRD is pasted into the security reviewer's or the
  knowledge distiller's prompt, neither of which asked for it. So an
  engineer may annotate, resolve or delete the block and nothing will
  report it. That is the trade a note nothing is judged against should
  make, and it is the opposite of `fixtures`, which is strict because
  it is both pinned and executed.
- Safe mode: one name, and one question, for the four degraded states
  kstrl already entered separately. An untrusted control directory stops
  the daemon spending, a damaged `autonomy.json` falls back to L1
  Supervised (or a ceiling clamps the run below the level it earned),
  the queue pauses, and an adversarial phase can be skipped for a
  component. Each was correct on its own and each spoke on a different
  surface: a warning line at run start, `ks queue`, `ks serve` output,
  and a callout inside a pull request body. `safe_mode_reasons(root_dir)`
  reads all four and returns a source, a detail sentence taken verbatim
  from the existing signal, and the runbook anchor that recovers it;
  the plain `ks status` report, `ks serve --dry-run` and the dashboard
  read it. On the dashboard `f2` opens a panel from any screen, a warning
  banner appears under the run masthead when a signal degrades, and the
  home masthead carries a chip. The panel is what keeps three facts
  apart that a banner alone would merge: not checked yet, checked and
  clear, and a list of reasons in each signal's own words with the
  runbook section that recovers it. The run masthead carries no chip
  because it has no room: at 120 columns the run header and the cost
  meter already want 126 cells, so anything added there cost the run its
  own state label. The dashboard re-checks on a slow background thread
  rather than on its event poll, because the predicate reads a run's
  whole event stream. It never raises: a signal that cannot be read is
  itself a reason, because a reader that failed is not evidence that the
  signal is clear. No behaviour changed and no gate was added, since
  every signal it reads already refuses where refusing is right
  (R10.4, #225).

- Set-point agreement: a story counts as done only when the reviewer's
  per-story verdicts confirm the engineer's `passes: true`. The engineer
  agent was the only writer of that flag, so the thing doing the work
  also filed the report on it; the reviewer's verdicts were already
  being produced and thrown away at parse time. `[factory]
  setpoint_agreement` (`advisory` by default, or `block`) decides what
  happens on a disagreement: advisory records a `setpoint_disagreement`
  finding on the pull request and in the journal and lets the component
  proceed, while block also resets `passes` to false in the PRD with a
  note saying why and retries the story. Confirmation needs a pass on
  every acceptance criterion, not just on the ones the reviewer chose to
  judge: the existing coverage gate checks that every story got a
  verdict, never that every criterion did. In blocking mode a reviewer
  that crashed, returned nothing usable, or never ran because the
  adversarial budget was exhausted also fails the component, because a
  sensor that did not report has not confirmed anything. An outage is
  recorded as an infrastructure failure rather than as a disagreement,
  so the retry context and the evolution journal do not report a
  reviewer disagreeing when none reported. The autonomy ladder forces
  blocking from L1 upward and can never turn it off. No prompt body
  changed: the engineer is still told to set the flag, the flag has just
  stopped being the sole authority (R10.3, #224).

- `ks sense`: run the mechanical sensors (test suite, typecheck, linter,
  diff scope, bad patterns, plus any opt-in policy / adequacy / dead-code /
  mutation checks) against any tree by hand, with no PRD, branch, worktree
  or agent spend. `--json` emits one machine-readable document; exit 0 on
  pass, 1 on any failed check, 2 when the measurement could not run.
  `run_mechanical_verification` now accepts `prd_path=None` and skips only
  the PRD-dependent checks, and `read_only=True` to measure a tree without
  changing it. Because `ks sense` runs against your live checkout rather
  than a worktree kstrl owns, the measurement never edits, stages, commits
  or leaves bytecode: the dead-code check reports what it would remove
  instead of removing it, and mutation testing is skipped because mutmut
  works by rewriting source. A base branch git cannot resolve is exit 2,
  never a pass on an empty diff (R10.1, #222).

### Changed

- The retry context handed to the engineer is now level-triggered: it renders
  the failures measured in the latest attempt, lists earlier findings whose
  sensor did not run again under "Not re-measured", and replaces the rest with
  a count. Before this it was an integrator with no discharge - every failure
  ever accumulated was re-rendered on every retry under "Fix ALL issues listed
  above before completing", so an agent on attempt 3 was told to fix attempt
  1's failures whether or not attempt 2 had already fixed them. Each failure
  now records the attempt it was measured in and the phase that measured it.
  A finding is only retired when that is observed (the same phase produced a
  fresh reading) or safely inferred (a phase that always runs once its
  predecessor passes). Review and security are excluded from the inference
  because an exhausted `max_adversarial_calls` budget downgrades them to skip
  mid-run, so a later failure does not prove the reviewer ran. A sensor that
  crashed rather than reported retires nothing either: a crashed reviewer, an
  unfetchable diff and an unsplittable diff are recorded as infrastructure
  entries, the same line `Finding.infrastructure_error` already draws.
  `IterationContext.from_json` still reads contexts serialised in the old
  shape, and those undated findings always render as un-re-measured
  (R10.2, #223).

### Removed

- **Breaking:** the one-release compatibility layer for the pre-rename
  names. The legacy environment-variable prefix, config filename, state
  directory, and console script are no longer read or installed. Move to
  `KSTRL_*`, `kstrl.toml`, `.kstrl/`, and the `ks` (or `kstrl`) command.

## [0.2.0] - 2026-07-21

The first release under the **kstrl** name.

### Added

- **Adversarial factory pipeline**: an architect red-teams the spec and
  decomposes it into a component DAG; each component is built by a coding agent
  in an isolated git worktree and gated through mechanical verification, code
  review, security review, and cross-component contract testing before its PR
  merges. An optional human checkpoint can pause before merge.
- **Textual TUI and events substrate**: every run writes a typed
  `events.jsonl` that every surface projects - a live dashboard, the bare-`ks`
  home shell with a run browser, `ks dash` (attach read-only to any run), and
  `ks status` for scripts and CI.
- **Agent adapters**: `claude-code`, `codex`, `custom`, and an opt-in
  `claude-sdk` adapter (installed via the `kstrl[sdk]` extra) with in-loop
  budget enforcement.
- **Safety systems**: per-phase and per-component timeouts, a no-progress
  circuit breaker, adversarial-call and token budgets, an OS-level agent
  sandbox, and a sandboxed approved-fixtures oracle.
- **Learning loop**: a calibration suite with planted-bug fixtures, an
  evolution journal, knowledge distillation across runs, and `ks evolve`
  harness-improvement proposals.
- **Linear mirror**: an optional one-way outbound sink that reflects factory
  progress into a Linear tracker.
- **Dark Factory roadmap**: `docs/dark-factory-roadmap.md` plus the R8 issue
  set defining the path to a governed autonomous factory.

### Changed

- Renamed the project to **kstrl** (CLI `ks`/`kstrl`, config `kstrl.toml`,
  state `.kstrl/`, env prefix `KSTRL_*`). The previous names were honored
  for one release with a deprecation warning.

[Unreleased]: https://github.com/0xfauzi/kstrl/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/0xfauzi/kstrl/releases/tag/v0.2.0
