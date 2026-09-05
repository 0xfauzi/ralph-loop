# Dark Factory Roadmap (R8): from merged-PR factory to governed autonomous factory

Durable tracker for the R8 cycle. Goal: close the four structural gaps between
Kestrel and a full software factory - continuous intake, a release stage,
runtime feedback, and an explicit earned-autonomy model - plus the hardening
that makes reduced-human-gating defensible.

Tracking issue: [#156](https://github.com/0xfauzi/kstrl/issues/156).
Milestone: `R8: Dark Factory`.

Provenance: the item designs come from 2024-2026 agentic-factory practice
and research - the "dark factory" pattern of agents planning, implementing,
testing, and shipping without per-change human review (made possible by
Claude Code / Codex-class agents), the intake models of OpenHands, GitHub
Copilot's coding agent, Devin, and Claude Code GitHub Actions, Sentry's
agent-handoff pattern, the LLM test-gaming and correlated-errors
literature, and current deploy/mutation tooling. A 2026-07-21 gap analysis
additionally measured Kestrel against the longer software-factory lineage
(industrial software production, product lines, DoD DevSecOps and
continuous authorization, lights-out manufacturing); from that lineage this
plan borrows the continuous-authorization governance model (autonomy
earned, bounded, revocable - current DoD practice) and two cautionary
lessons, nothing more. Each item below was researched individually with a
build-vs-integrate lens; key sources are cited inline. Caveat recorded per
H4: the definitional/historical claims survived adversarial verification;
the manufacturing and agentic-SWE claims were extracted from sources but
not adversarially verified.

How to read this document: items are numbered R8.1-R8.8, continuing the
tracker-ID sequence from earlier cycles (A1-H5, then R0-R7 in
`docs/adversarial-roadmap.md` and `docs/remediation-roadmap.md`); the
prefix only says which tracker owns the item, and the IDs are used in
issues, PRs, and commits like ticket numbers. H1-H4 are the standing
process rules defined below. Newcomers should start with the project wiki
(Vision and Philosophy, Roadmap), which decodes all project vocabulary.

Status legend: `[ ]` pending - `[~]` in progress - `[x]` done - `[-]` skipped.
Sizing: S (small diff, <~100 lines), M (one PR), L (multi-PR workstream).
Sizing is diff scope, not duration.

Process rules that bind this plan (inherited unchanged):

- H1: no self-review. Every item lands as one or more PRs gated by the user.
- H2: any prompt-body change re-runs calibration and records the delta.
- H3: every prompt edit bumps `*_PROMPT_VERSION` and the snapshot tuple together.
- H4: every "done" claim states what was tested vs assumed. Each item has a
  measurable "Done when" gate.
- R8 addition - no assumed thresholds: every numeric threshold in this plan
  (ladder entry counts, EWMA lambda, sigma multipliers, mutation floors,
  signal thresholds) is a placeholder until measured. Before any threshold
  gates or demotes, replay it against historical data (`experiments.tsv`,
  evolution journal) and record the would-have-fired count in this doc.

---

## The frame: what "dark factory" means here

"Dark factory" is the 2025-2026 agentic pattern: coding agents plan,
implement, test, and ship software without a human reviewing each change.
Practitioner accounts of working implementations converge on the same
preconditions - an evaluator-grade test suite with defenses against agents
gaming it, constrained permissions enforced outside the agents, a mature
deterministic pipeline underneath, rollback paths, and humans at an
oversight layer reached only on boundary conditions.

The research is equally unambiguous that 100% dark is not a defensible
goal: closed AI-certifies-AI loops lose their verification oracle,
same-family builder/verifier pairs share blind spots, and monitors who
never intervene lose the ability to intervene. The defensible end state is
the continuous-authorization shape (borrowed from current DoD software
factory practice): **autonomy that is earned after demonstrated baseline
compliance, bounded by an explicit written envelope, continuously
monitored, and revocable with automatic reversion to human-gated mode**.
Humans move from in-the-loop approval to over-the-loop exception handling.

Kestrel's scorecard against the reference model: the verification core
(adversarial phases, breakers, budgets, audit trail, calibration) is already
at or above the bar. Absent entirely: continuous intake, release/deploy,
runtime telemetry of built products, and an autonomy maturity ladder. Partial:
policy gating (implicit, scattered), the over-the-loop human surface, and the
learning loop (build-time signals only).

Doctrine for this cycle:

1. **Integrate at the edges, build only thin middles.** Queue front-ends,
   deploy engines, error tracking, license metadata, notifications: integrate.
   The state machines, policy checks, and gates that carry trust: build,
   small, harness-side, mechanically verifiable.
2. **Autonomy is earned, bounded, revocable.** Promotion requires evidence
   plus a recorded human ack; demotion is automatic; fast down, slow up.
3. **Every new surface is a projection of `events.jsonl`.** Queue, inbox,
   release states, runtime signals all emit typed events; the files remain
   the record.
4. **Enforcement reads artifacts, never agent self-report.** Policy and
   adequacy checks run on the git diff, lockfiles, and coverage/mutation
   output, in the mechanical verifier.
5. **The first-class phase count is frozen** (added 2026-08-03). New
   evaluations land inside an existing phase (as R8.5 adequacy landed
   inside mechanical verify) or behind a shared evaluator seam - never as
   another hard-coded pipeline branch with its own result type threaded
   through factory, TUI, and events.
6. **New terminal-status vocabularies reuse one disposition set** (added
   2026-08-03). Twelve distinct terminal-outcome vocabularies already
   exist across manifest, pipeline, PR, inbox, queue, and serve. New
   surfaces (R8.7 release states, R8.8 signals) express outcomes as a
   shared disposition plus a structured reason code instead of minting
   another enum. Existing enums are not retrofitted.

---

## User decisions required

Blocking decisions are marked on the items that need them.

1. **Remote-item merge policy** (R8.6): is `stop_at_pr` the permanent default
   for queue items sourced from GitHub/Linear, with `auto-merge` per-item and
   ladder-gated? (Recommended: yes.)
2. **Unattended spend** (R8.6): acceptable `daily_budget_usd` for `ks serve`,
   and on exhaustion: pause queue until next day, or notify-only?
3. **Queue scope** (R8.6): per-target-repo `.kstrl/queue/` or one global
   `~/.kstrl/queue/` with items carrying a target-repo field? Multi-project
   intake forces global.
4. **Deploy reality** (R8.7): what do built products actually deploy to today
   (fly.io, VPS compose, nothing yet)? Decides whether the `gha` driver ships
   in v1 or the command driver alone suffices, and whether L4 is real yet.
5. **Revert doctrine default** (R8.7): `revert-and-requeue` or `fix-forward`
   after a bad release. Philosophy, not engineering.
6. **Migrations** (R8.7): will built components own databases? If yes,
   expand-contract migration discipline belongs in `DECOMPOSE_PROMPT` (a
   prompt change: H2/H3 apply).
7. **Error-tracker license** (R8.8): Bugsink is Polyform Shield (free
   self-host, not MIT). Acceptable, or require GlitchTip (MIT, heavier)?
8. **Runtime signal routing** (R8.8): do signals enter the queue directly, or
   via human triage first, and what severity draws the line?
9. **Second model family** (R8.5): which family reviews Claude-engineered
   code at L3+ (codex adapter exists), and does the calibration family-delta
   justify it? Overlaps remediation-roadmap R7.1.
10. **Test immutability** (R8.5): should existing tests be read-only to the
    engineer role, with test modifications routed through a separate approval
    path? Strongest anti-gaming lever, but constrains legitimate refactors.
11. **Always-on machine** (R8.6): is a Mac mini / small server plausible in
    the next ~6 months? If no, sleep-resilience (lease reaping, post-wake
    retry classification) deserves its own tests.

## User-run measurements required

The ladder's entry criteria are the factory's cATO evidence. These are
already tracked in `docs/remediation-roadmap.md` and remain blocking there;
R8.2 consumes them:

- Calibration baselines green at threshold over 3 runs (R5.1-R5.3):
  **CAPTURED 2026-07-20** (recorded in the remediation roadmap).
- Same-family vs cross-family reviewer detection delta (R7.1): **CAPTURED
  2026-07-20** - both families at the detection ceiling, no
  correlated-miss benefit visible at current fixture difficulty.
- Two real factory runs with nonzero fact-utilization and one traceable
  evolve proposal: **OUTSTANDING - the sole remaining L2+ gate.** The
  recording prerequisite is closed:
  [#191](https://github.com/0xfauzi/kstrl/issues/191) landed, so
  utilization now reaches the `fact_utilization_measured` event and the
  evolution journal's `component_result.knowledge_utilization`. Query it with
  `EvolutionJournal.get_fact_utilization`; the gate is
  `runs_with_referenced >= 2`. #191 also corrected the number itself -
  it was measured against a knowledge prefix rebuilt AFTER distillation
  had written that run's own facts, so it counted facts the engineer
  never saw. Any utilization figure observed before #191 is not valid
  evidence for this gate. What remains is running the two real runs.
  The measurement defects found alongside it are fixed, not merely
  documented: only ADDED diff lines are searched, so deleting the code
  that expressed a fact no longer scores as using it (a false positive
  that could have satisfied this very gate); every component whose diff
  can be fetched is in the sample, not only those passing every gate;
  and the counts are split per prefix tier so sibling summaries do not
  inflate the denominator. The measurement is on both surfaces the
  acceptance criteria name - `fact_utilization_measured` in
  `events.jsonl` and `knowledge_utilization` in `evolution.jsonl`.
  `docs/evolution-metrics.md` records what is in the sample, what
  counts as a reference, how to read the tiers, and the one standing
  caveat (the 30-char match is lexical, not semantic).

L2+ entry is blocked until these exist. R8 adds no new user-run gates beyond
threshold-replay captures noted per item.

---

## Sequencing

Waves order the work by dependency, not importance. Within a wave, items can
proceed in parallel.

| Wave | Items | Rationale |
|---|---|---|
| 1 - governance core | R8.1 policy envelope, R8.4 health trending, R8.2 autonomy ladder, R8.3 inbox | Small code, no new risk surface, everything else gates on the ladder |
| 2 - adequacy | R8.5 test adequacy gate | The lights-out precondition; advisory-first so it can land early |
| 3 - operation | R8.6 continuous intake | Needs merge dispositions (R8.2) and inbox routing (R8.3) |
| 4 - release + loop | R8.7 release stage, then R8.8 runtime feedback | Highest blast radius, lands on top of the ladder and envelope |
| hardening (added 2026-08-03) | R8.9 control-state relocation | Trust boundary for unattended operation; blocks L3+ and unattended serve with the ladder on |
| brownfield (added 2026-08-03) | R8.10 `ks doctor` | Repo-level admission gate: fail before spending, not while spending |

Dependency edges: R8.2 needs R8.1 (envelope defines L3) and consumes R8.4
triggers when available. R8.3 needs R8.1/R8.2 item types (can land with
today's subset). R8.6 needs R8.2 + R8.3. R8.7 needs R8.2 (L4) + R8.6
(re-queue). R8.8 needs R8.6 + R8.7. R8.9 has no upstream dependency but
BLOCKS enabling `[autonomy]` at L3+ and unattended `ks serve` with the
ladder enabled. R8.10 has no upstream dependency and is recommended
before the first unattended serve on any newly onboarded repo.

---

## R8.1 Policy envelope (M) - [#148](https://github.com/0xfauzi/kstrl/issues/148)

Status: `[x]` - Shipped in `kstrl/policy.py` + `kstrl/licensing.py` +
`check_policy_envelope`: `paths_deny`, size caps (lockfiles excluded),
`deps_allow_new`, `secret_patterns`, the non-overridable
enforcement-machinery halt, **license gating** (`license_allow` /
`license_deny_partial`), the `deploy` flag (stored/hashed, enforced by
R8.7), and the policy-envelope hash in the run manifest. Opt-in via
`[policy] enabled` (default false), blocking when enabled.

**Review corrections (PR #173).** Four gaps were caught in review and
closed before merge, recorded here because each was a real enforcement
bypass rather than a style note:

1. The non-overridable halt covered CI and the config file but NOT
   verifier code, so `evaluate_policy(["kstrl/verify.py"], ...,
   paths_deny=[])` passed - an agent could rewrite the checker instead of
   the rules. `ENFORCEMENT_MACHINERY_PATHS` now covers the Phase 1
   enforcement surface (`verify.py`, `policy.py`, `licensing.py`,
   `guards.py`, `fixtures.py`) at any depth, plus an ADDITIVE
   `enforcement_paths_extra` that config can never shrink.
2. The changed-file and numstat reads were fail-OPEN: both helpers return
   `[]` on timeout/nonzero exit, so a `kstrl.toml` diff evaluated as
   "0 files, 0 lines" and passed every path and size rule. A successful
   earlier content read proves nothing about those later, separate
   subprocesses. Both helpers gained `strict=True` (raises `GitDiffError`)
   and the policy check uses it, failing closed.
3. An unresolved license passed as advisory, weakening the explicit-
   allowlist posture, and `KSTRL_POLICY_LICENSE_NET` changed verdicts
   without changing `policy_hash`. Unresolved now BLOCKS by default
   (`license_unresolved = "block" | "advisory"`), and the network toggle
   is a hashed config field (`license_use_network`).
4. Violations now emit typed `Finding`s (`Finding.policy_violation`,
   carried on `CheckResult.findings` and lifted into the component's
   finding stream), as #148 requires, so a machine-made gate decision
   reaches the PR body and journal. Inbox ROUTING still lands with R8.3.

**Measured correction to the license verdict (H4).** The plan assumed
`pip-licenses` / installed dist metadata. Measured against this repo's
uv toolchain that is FALSE: uv's installed venv materializes no
`METADATA` file (empty `licenses/` dir; `importlib.metadata` and `uv pip
show` both return nothing), so pip-licenses would resolve nothing.
`kstrl/licensing.py` instead reads the license from **uv's cache**
(`<cache>/**/<name>-<version>.dist-info/METADATA`, offline) and falls
back to the **PyPI JSON API** (`license_expression` / classifiers /
`license`; `KSTRL_POLICY_LICENSE_NET=0` forces offline). A license that
resolves to a `deny_partial` substring or is not in `license_allow`
blocks; an unresolvable license BLOCKS by default per correction 3 above
(`license_unresolved = "advisory"` is the explicit opt-out for repos that
prefer not to hold a merge hostage to a cache/network miss).
Classification (deny-wins, compound-SPDX atom tokenizing) is pure in
`kstrl.policy`. (This paragraph originally said unresolved was advisory,
contradicting correction 3 fifteen lines up; fixed 2026-08-03 to match
the code, where `"block"` is the default.)

**Why.** Machine-made merge decisions are only defensible inside an explicit,
written envelope. Today the rules are implicit and scattered (diff-scope,
allowed paths, bad patterns).

**Verdict: build the checks, integrate license metadata.** OPA/conftest need
a Go sidecar; Cedar's Python bindings are early-stage; both are foreign
runtimes for rules that fit in ten TOML lines. License gating integrates
`pip-licenses` or `licensecheck` (explicit allowlist + partial-match deny for
copyleft; compound SPDX expressions defeat exact matching).

**Design.**

```toml
[policy]
paths_deny = [".github/workflows/**", "kstrl.toml", ".kstrl/**", "**/*.pem", "**/.env*"]
max_files_changed = 40
max_lines_changed = 1500
deps_allow_new = false          # L3+ may set true
license_allow = ["MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0"]
license_deny_partial = ["GPL", "AGPL", "SSPL", "Commons-Clause"]
secret_patterns = ["AKIA[0-9A-Z]{16}", "-----BEGIN .*PRIVATE KEY"]
deploy = false
```

Enforcement runs harness-side in the Phase 1 mechanical verifier on
artifacts (git diff, `uv.lock`, dist metadata), never on agent self-report.
Violations emit typed `Finding`s and route to the inbox (R8.3) with
approve-once / approve-and-amend-policy / reject actions. The policy file
hash is recorded in the run manifest. A diff touching enforcement machinery
(policy file, CI workflows, verifier code) is an instant halt at every
autonomy level.

**Failure modes.** Agents relocating violations into generated scripts the
pipeline later executes (mitigated by sandbox network-deny backstop and
deny-listing script dirs that CI executes); license-string mismatch on
compound expressions (partial-match deny); policy drift between repos (each
repo owns its envelope; `ks init` writes the conservative default).

**Done when:** planted violations in every category are caught in tests;
policy hash lands in the manifest; enforcement-machinery halt is tested;
`kstrl.toml.example` and docs updated.

---

## R8.2 Autonomy ladder (M) - [#149](https://github.com/0xfauzi/kstrl/issues/149)

Status: `[x]` - Shipped in `kstrl/autonomy.py` + `kstrl/autonomy_replay.py`
+ `ks autonomy` (status/promote/demote/history/replay). Levels derive the
flag bundle at run start (`run_factory`), promotion requires a recorded
human ack, demotion is automatic with a cool-down, and every transition
emits `autonomy_transition` / `autonomy_level_applied` events (the
transition also lands in the evolution journal, which is the durable
cross-run record; `events.jsonl` is per-run, so a CLI transition outside a
run reaches the journal only). Opt-in via
`[autonomy] enabled` (default false) because L1 is STRICTER than today's
defaults - it forces the merge gate on. R8.4 will enrich the demotion
triggers with health-metric breaches; the `HEALTH_BREACH` trigger already
exists for it to fire, and since
[#232](https://github.com/0xfauzi/kstrl/issues/232) so do the inbox kind,
the config key and the call site, so R8.4 supplies `kstrl/health.py` and
nothing else.

**Review corrections (PR #174).** Seven findings, five of them P1, all
closed before merge. Four were substantive holes rather than polish:

1. **L3 auto-merged without an envelope.** L3 is *Enveloped* auto-merge,
   but the bundle dropped the merge gate even with `[policy] enabled=false`
   - auto-merge inside nothing. `resolve_runtime_level` now clamps L3+ to
   L2 without an enabled envelope, and the ladder clamps `deps_allow_new`
   below L3 (the bundle can only ever WITHHOLD an envelope permission).
2. **The ladder was inert.** No production call site touched
   `record_decisive_run` / `record_merged_component` /
   `record_policy_violation` / `demote`, so counters never moved,
   promotion was unreachable without `--force`, and automatic demotion
   never fired. Run outcomes now fold into state at run end, inside the
   factory lock. A follow-on bug found while fixing it: infra-aborted runs
   were counted as decisive, which would have let a string of broken runs
   accrue promotion evidence - now excluded, matching the replay's
   definition.
3. **Promotion authority was a string.** `--actor human --ack x --force`
   from an agent satisfied every check. Promotion now requires a
   controlling TTY, and `.kstrl/autonomy.json` + `kstrl/autonomy.py` were
   added to the R8.1 enforcement-machinery halt set to close the
   write-the-file-directly path.
4. **Transitions never reached the audit streams.** `AutonomyTransition`
   had no production emitter and neither CLI command wrote the journal, so
   the documented "every transition is recorded" claim was false.
   `commit_transition` now performs state save + journal append + event
   emit together.

Plus: the replay never advanced its simulated level (so L3/L4 thresholds
and post-promotion demotion were never exercised), malformed state fields
raised instead of failing closed to L1, and `status` rendered the stored
rather than the effective bundle.

**Threshold replay captured 2026-07-27 (the R8 "no assumed thresholds"
rule).** `ks autonomy replay` over `.kstrl/experiments.tsv`:

```
Runs recorded:        5
  decisive:           2
  infra-aborted:      3 (excluded)
Components merged:    2
Projects:             slugify

Would-have-promoted:  0
Would-have-demoted:   0
Final level after replay: L1

VERDICT: INSUFFICIENT DATA (2 decisive runs vs a MIN_DECISIVE_RUNS floor of 8)
```

Read honestly: **every threshold in the table below remains an unmeasured
placeholder.** Three of the five recorded runs died on PR/push plumbing
(`pr:` failures), which the replay excludes as infrastructure casualties -
they say nothing about the factory's judgement. The remaining sample is
one toy project over a single day, and predates the TUI stack, the rename,
and every fix since. Nothing here calibrates the ladder; it establishes
only that the ladder cannot promote past L1 on the evidence that exists,
which is the safe failure. Generating real evidence (see "User-run
measurements required") is the blocking prerequisite for L2+, not more
code.

**Why.** Autonomy today is a scatter of flags. The cATO shape: earned,
bounded, revocable. Prior art converges (Claude Code permission modes,
OpenHands confirmation policy x risk analyzer, arXiv:2506.12469 levels
defined by the human's remaining role).

**Verdict: build.** ~200 lines of state machine over flags that already
exist. Nothing integrable exists for a local CLI.

**Design.** State in `.kstrl/autonomy.json`; transitions append to
`events.jsonl` and the evolution journal.

| Level | Meaning | Flag bundle | Entry criteria (placeholders until replayed) |
|---|---|---|---|
| L1 Supervised | human approves plan and merge | merge gate on, strictest hard mode, `deps_allow_new=false`, deploy off | default for new repos |
| L2 Gated-merge | human gates merge only | plans auto-accepted | 5 components merged at L1, zero policy violations, calibration compare green |
| L3 Enveloped auto-merge | merge gate off when fully green AND inside envelope | breaches route to inbox instead of merging | 15 consecutive L2 merges approved without edits, health metrics inside limits, recorded `ks autonomy promote` ack |
| L4 Deploy | L3 + release stage enabled | `[release] enabled=true` reachable | L3 held for 30 merged components, explicit promote, deploy target exists |

Promotion needs evidence AND a recorded human ack - agents cannot promote
themselves. Demotion is automatic, one level per trigger: policy violation,
calibration regression beyond baseline tolerance, health-metric breach
(R8.4), human rejects an L3 auto-merge candidate. Fast down, slow up:
re-promotion locked for 10 decisive runs after a demotion. Every demotion
emits an inbox item carrying the triggering evidence.

**Failure modes.** Demotion flapping on 3-run noise (minimum n >= 8 decisive
runs, EWMA not raw points, cool-down); Goodhart pressure lowering retry rate
by weakening verification (calibration detection rate stays in the demotion
basis - adversarial ground truth that laxer verification cannot improve);
stale ladder state after manual config edits (flag bundle derived from level
at run start, manual overrides logged as such).

**Done when:** levels drive the flag bundles; promotion requires a recorded
ack; demotion fires on planted trigger fixtures; the threshold-replay tool
exists and its output over historical `experiments.tsv` is captured here.

---

## R8.3 Exception inbox (L) - [#150](https://github.com/0xfauzi/kstrl/issues/150)

Status: `[x]` - Shipped in `kstrl/inbox.py` + `ks inbox` +
`kstrl/tui/screens/inbox.py`. Append-only `.kstrl/inbox.jsonl` folded on
read; item kinds policy_exception / merge_gate / halted_run /
budget_overrun / demotion_notice / calibration_drift / test_adequacy;
dedupe by key, snooze with a TTL that RETURNS the item, and an open-item
cap that R8.6 will consult before admitting queue work.

**Emitters are wired, not declared.** The halt paths that feed it:
component FAILED and PR-flow failure (halted_run), MERGE_PENDING and the
pre-merge checkpoint that no interactive UI can answer (merge_gate),
token-budget halt (budget_overrun), R8.1 policy findings
(policy_exception, advisories excluded), R8.5 blocking test-adequacy
findings (test_adequacy, advisories excluded for the same reason), and
R8.2 demotions (demotion_notice, carrying the triggering evidence - the
item R8.2 promised). Verified end-to-end: a run with a planted policy
violation produces a policy_exception, a halted_run, AND a
demotion_notice while the ladder drops L3 -> L2.

`pause_before_pr_merge` on an unattended run no longer proceeds. It
returns `CheckpointDecision.PARKED`: the merge is withheld, the
component fails at `phase=pr / check=merge_gate`, and the decision goes
to the inbox. Proceeding defeated the gate in exactly the unattended
case R8.2's L1/L2 forces it on for.

Closes three IOUs left by earlier items: R8.1's "violations route to the
inbox", R8.2's "every demotion emits an inbox item", and the surface R8.6
needs for `stop_at_pr`.

Notification stays one-way, as specified: `notifiable()` selects
action-required kinds plus demotions, so successes are silent; kstrl runs
no inbound HTTP surface, and items are actioned in `ks inbox` or the TUI
screen. The push itself is a dedicated `[notify].on_inbox_item` command,
empty by default: a failing component already fires `on_first_failure`
and raises an item for the same event, so sharing one command would page
twice for one thing. An ntfy.sh example is in `docs/env-vars.md`.

Not built, and NOT claimed as done:

- `approve-and-amend-policy` (widening the R8.1 envelope from a repeated
  approval) and the daily digest. Both are learning-loop refinements that
  want real inbox traffic to design against, and neither is load-bearing
  for R8.6.
- Inbox emission from `SpecBlockerError` exits and from terminal contract
  failures. Review of PR #175 established that the original "every halt
  path feeds it" claim was too broad: those two paths still bypass the
  inbox.

**Why.** Over-the-loop operation needs one surface for everything awaiting a
human decision. Today: SpecBlocker exit codes, FAILED components,
MERGE_PENDING, evolve proposals, calibration captures - all scattered.

**Verdict: build the inbox (thin), integrate ntfy.sh for push.** The inbox is
`.kstrl/inbox.jsonl` + CLI verbs + a Textual screen in the home shell.
A hosted approval SaaS is the wrong fit for local-first solo. ntfy.sh
is one HTTP POST through the existing notify hook, self-hostable, priority
tiers; notification stays one-way (actions happen in `ks inbox`, which avoids
running an inbound HTTP endpoint).

**Design.** Item types: policy exception, merge gate (L1-L2), halted run,
budget overrun, demotion notice, calibration drift. Linear-Triage-style
one-key actions per type: approve / reject-with-comment / retry / edit-spec /
snooze-with-TTL. `approve-and-amend-policy` converts repeated approvals into
envelope widening - the learning loop that shrinks the inbox. Open-item cap
pauses queue intake beyond N items. Notify only action-required items and
demotions; successes silent; daily digest for informational items. Every
decision journaled.

**Failure modes.** Inbox becomes a second job (open-item cap, policy
amendment loop, snooze TTLs, digest batching); alert fatigue (priority
tiers, silence on success); decisions lost to crashes (append-only JSONL,
same atomic-write pattern as the manifest).

**Done when:** all existing halt paths emit inbox items; actions round-trip
(approving a merge-gate item resumes the component); the Textual screen
renders and acts; an ntfy hook example is documented.

---

## R8.4 Factory health trending (M) - [#151](https://github.com/0xfauzi/kstrl/issues/151)

Status: `[ ]` - Depends on: none (feeds R8.2)

**Two-stage delivery (decided 2026-08-03).** Stage 1 ships the evidence
surface only: `ks health` reporting plus the documented historical
replay. Stage 2 wires the `HEALTH_BREACH` demotion emitter, and only
after the stage 1 replay has been captured and recorded in this doc - an
unmeasured EWMA limit must not demote on task-mix noise. The trigger
R8.2 pre-wired stays dormant until then; this makes the standing
"no assumed thresholds" rule (top of this doc) an explicit gate between
the two stages rather than an implied one.

**The seam landed ahead of the emitter**
([#232](https://github.com/0xfauzi/kstrl/issues/232)). `ItemKind.HEALTH_BREACH`,
the `[autonomy] demote_on_health_breach` key (default false) and the call site
in `factory._record_health_breaches` are in place. They read
`kstrl.health.health_breaches(root_dir)` when that module exists and are inert
while it does not, so stage 2 is this module plus the recorded replay rather
than a second round of wiring. `ks autonomy replay` stays the advisory mode for
these rules: it reports what would have fired and never mutates ladder state, so
a candidate rule set is scored against real history before it is allowed to
demote anything.

**False-alarm arithmetic, stated before the rule set is chosen.** One point
beyond three sigma fires by chance about once in 370 observations; all four
Western Electric rules together, about once in 92
([reference](https://handwiki.org/wiki/Western_Electric_rules)). With
`demote_on_health_breach = true` a false alarm costs one autonomy level plus
`DEMOTION_COOLDOWN_RUNS = 10` decisive runs of locked re-promotion. #232 also
suppresses a health demotion while a cool-down is running, so a persistent false
trend cannot walk the ladder to L1 one run at a time; that bounds the damage, it
does not excuse a rule set nobody replayed. Pick the rules with these two
numbers in front of you.

**Why.** Demotion triggers need trend detection over run metrics, and the
operator needs an evidence surface. The journal and `experiments.tsv` record
the data; nothing trends it.

**Verdict: hand-roll detection (stdlib), DuckDB as optional query layer.** No
maintained lightweight SPC library exists (pyspc stale; river heavy for
this). EWMA (lambda ~0.2) plus two Western Electric rules (1 point beyond 3
sigma; 2-of-3 beyond 2 sigma same side) is ~30 lines, designed for small
persistent shifts. DuckDB reads JSONL natively via `read_json_auto`, single
self-contained wheel - right for `ks health query "<sql>"` but stays an
optional extra: autonomy safety logic must not depend on an optional
dependency.

**Design.** Metrics: retry rate, cost per merged component,
`infrastructure_error` rate, calibration detection deltas, human-edit rate.
Control limits computed from the repo's own baseline period, never fixed
constants; minimum n >= 8 decisive runs before any automatic transition.
`ks health` renders per-metric EWMA vs control limits with sparklines;
`ks health why-demoted` replays triggering evidence.

**Failure modes.** False alarms from mixed run kinds (segment by run kind);
baseline contamination by early chaotic runs (explicit baseline window
selection, recorded); metric gaps when runs fail early (decisive-run
definition excludes infra-aborted runs).

**Done when:** `ks health` renders from real journal data; trigger rules are
unit-tested on synthetic drift; the historical replay is documented; the
duckdb extra is wired for the query subcommand only.

---

## R8.5 Test-suite adequacy gate (L) - [#152](https://github.com/0xfauzi/kstrl/issues/152)

Status: `[~]` - **Layer 0 only.** Shipped in `kstrl/adequacy.py` +
`check_test_adequacy`: test-diff discipline (deleted tests - including a
deleted test FILE - added skip/xfail in any of its spellings, net
assertion loss) and oracle-signal linting (a new test file needs one
falsifiable assertion; tests that assert nothing are reported). Opt-in
via `[adequacy] enabled`, ADVISORY first; with the R8.2 ladder on, Layer
0 blocks from L1 up per the level table.

Two scoping rules decide whether the gate is usable rather than merely
strict:

- **The whole-file oracle floor applies to ADDED test files only** (git
  status `A`). A modified file carries tests written under a different
  standard, and failing a one-line edit over a legacy file's weak oracles
  is how a gate gets switched off. Modified files still get every
  diff-discipline check, plus the assertionless check on the test defs
  the diff added.
- **A truthiness call is not an oracle.** `assert bool(x)`,
  `assert compute()` and `assert a is not None or a == 3` are all WEAK; a
  call is strong only when its arguments state an expectation
  (`all(x > 0 for x in xs)`). `unittest` / `mock` assertion methods are
  classified too - `assertEqual` / `assert_called_once_with` strong,
  `assertTrue` / `assert_called` weak - so a `TestCase` file is not read
  as asserting nothing.

Findings are typed (`adequacy_*`) and land in the component's finding
stream (PR body, journal, evolution). **Blocking** findings additionally
raise an R8.3 inbox item (kind `test_adequacy`, deduped by category plus
location, so a recurring finding collapses onto one item); advisory ones
deliberately do not, because the inbox is a queue of decisions and an
advisory asks for none.

Layer 0 needs no test execution, no coverage run, no mutation tooling and
no historical data, which is why it went first: it is the only layer whose
thresholds are not waiting on evidence that does not exist yet.

**Not built, and not claimed:**

- **Layer 1** (patch coverage floor, `diff-cover --fail-under`) - adds a
  dependency and wants a measured floor rather than the roadmap's ~85%
  placeholder.
- **Layer 2** (diff-scoped mutation) - `check_mutation_score` already
  exists and is file-scoped to changed files, but the R8.5 requirements
  on top of it (max 1 mutant per line, hard wall-clock cap, sampling
  recorded in the audit trail, surviving mutants fed back as remediation
  targets) are unbuilt. Its >= 70% gate is explicitly "thresholds set
  from the empirical distribution", and that distribution needs real
  runs.
- **Layer 3** (fixtures oracle required at high autonomy) - `[fixtures]`
  exists and is opt-in; promoting it to mandatory at L3+ is a small
  level-gate that belongs with the same pass as Layer 1/2.
- **Cross-family review defaulting on at L3+** and the calibration
  family-delta - user-run measurements (overlaps remediation R7.1).

The distinction that matters for sequencing: Layer 0 degrades to nothing
without data because it needs none. Layers 1-2 need an empirical
distribution to set a threshold anyone should trust, and shipping them
against invented numbers is the failure this cycle keeps trying to avoid.

**Why.** The lights-out precondition in every tradition is an evaluator-grade
test suite, and the evidence says agent-written tests cannot be assumed
adequate: 80.2% of 86k agent-authored test patches carry weak or no oracle
signals (arXiv:2606.18168); LLM assertions encode actual rather than expected
behavior; test-gaming is measured, not hypothetical (ImpossibleBench,
arXiv:2510.20270). Green tests alone cannot be a merge gate at L3+.

**Verdict: build thin gates, integrate the tooling** (diff-cover, mutmut,
StrykerJS, cargo-mutants).

**Design.** Four layers, complementary (mutation does not catch
wrong-expectation tests; only spec-derived oracles do):

- **Layer 0 - test-diff discipline (mechanical, free).** Extend
  diff-scope/bad-patterns: fail diffs that delete tests, add skip/xfail, or
  loosen assertions without spec-linked justification. Oracle-signal linter
  on new/changed test files (W1-W5/S1-S3 taxonomy from arXiv:2606.18168):
  at least one strong-oracle assertion per new test file.
- **Layer 1 - patch coverage floor.** `diff-cover --fail-under` on changed
  lines (~85%), reusing the existing coverage run. Screens untested code;
  says nothing about oracle strength.
- **Layer 2 - diff-scoped mutation score.** Google-style: mutants only on
  changed+covered lines, max 1 per line, hard wall-clock cap (10 min or 3x
  baseline suite) with sampling recorded in the audit trail. Tools: mutmut
  (Python, coverage-limited; components are small so file-scoping
  approximates diff-scoping), StrykerJS `--incremental` (JS/TS),
  cargo-mutants `--in-diff` (Rust); Go targets get fixtures/property-heavy
  treatment with mutation advisory only. Gate >= 70% killed: advisory first,
  thresholds set from the empirical distribution, ratchet up only. Surviving
  mutants feed back to the engineer as concrete test targets (Meta ACH
  pattern) for one remediation iteration before gating.
- **Layer 3 - fixtures oracle.** Promote the approved input/output fixtures
  from opt-in to required at high autonomy - the only layer whose ground
  truth the engineer cannot rewrite.

Verification independence: cross-family review defaults on at L3+
(arXiv:2506.07962: same-family builder/reviewer pairs have measurably
correlated blind spots - when two models are both wrong they agree ~60% of
the time). Calibration gains the same-family vs cross-family delta (overlaps
remediation R7.1).

Behavior by level: L1 - Layer 0 blocking, 1-2 advisory. L2 - 0-1 blocking, 2
blocking after the remediation iteration. L3+ - all blocking, fixtures
mandatory, cross-family reviewer mandatory, and mutation infra failure halts
for a human rather than skipping (`infrastructure_error` convention,
halt-over-heroics).

**Failure modes.** Runtime blowups (scoping, caps, sampling, nightly full
runs to refresh incremental caches); equivalent mutants deflating scores
(threshold well below 100%, capped equivalence claims with recorded
justification); flaky tests poisoning every layer (clean baseline run before
mutants, rerun-on-fail quarantine as its own Finding); agents gaming the gate
(Layer 0 rules, fixtures never shown verbatim, cross-family review of test
diffs, periodic meta-calibration planting bugs against the whole gate).

**Done when:** Layers 0-1 gate with typed findings; Layer 2 runs diff-scoped
on a Python target within budget with sampling in the audit trail;
level-dependent behavior tested; calibration captures the family delta.

---

## R8.6 Continuous intake (L) - [#153](https://github.com/0xfauzi/kstrl/issues/153)

Status: `[x]` - Shipped across PRs #185, #186, #187, #189. Operator guide:
[docs/continuous-intake.md](continuous-intake.md).

All four slices landed: `kstrl/workqueue.py` + `ks queue` (#185),
`kstrl/serve.py` + `ks serve` (#186), `kstrl/intake_github.py` +
`ks queue sync` (#187), and launchd packaging + the operator guide (#189). Shipped so far:
`kstrl/workqueue.py` (maildir queue, `os.replace` transitions, flock
mutex, pid/ttl leases, journal, pause marker) with the `ks queue
add/ls/show/retry/rm/pause/resume` verbs, and `kstrl/serve.py`
(`ks serve [--once] [--dry-run]`, lease reaper, retry classifier, daily
spend ledger, poison breaker, `caffeinate -i`), and
`kstrl/intake_github.py` + `ks queue sync` (label polling, processed-ids
ledger, label/comment writeback). The launchd plist and its docs are PR 4.

**Inbox cap wiring (recorded 2026-08-03).** The R8.3 promise that serve
consults the inbox open-item cap before admitting queue work IS fulfilled
in code - `check_inbox_cap` is one of serve's admission gates - it was
just never stated in this section. One defect found in that wiring: the
cap originally failed OPEN, because the inbox fold skips unparseable lines
by design, so a torn emission line undercounted open items and admitted
work past the cap. Fixed fail-closed for
[#190](https://github.com/0xfauzi/kstrl/issues/190): `check_inbox_cap`
takes one `Inbox.scan()` snapshot and adds `unparseable_count()` to the
open total, so every line that MIGHT be an open item counts as one; a
whole-file read/decode failure is its own `unreadable` state and refuses
regardless of the configured cap (collapsing it to one skip would
re-admit under any cap > 1). The tolerant fold is unchanged for the
`ks inbox` display path.

**GitHub intake (PR 3).** The trigger is the LABEL, not the issue, and
that is the entire access-control story: applying a label needs write
access, so on a public repo a stranger can open an issue but cannot queue
a factory run. Remote items are forced to `stop_at_pr` regardless of any
label or config value - an issue label is the last place a merge decision
should be settable from. Idempotency has two halves: `find_by_source_ref`
covers items still in the queue, and the processed-ids ledger covers
items that have already left it (without which a completed issue would be
re-enqueued on the very next poll). The adapter is strictly additive:
every `gh` call returns a result object rather than raising, so a GitHub
outage produces an empty sync instead of a stalled queue.

Recorded because the plan claimed otherwise (H4): this does NOT use
ETag-conditional requests. `gh issue list` exposes no ETag, so the saving
R8.6 attributed to ETags is not realised. It is also not needed at this
cadence - one call per poll interval is ~60/hour against a 5,000/hour
budget.

**Review corrections (PR #187).** Twelve gaps were caught in review and
closed before merge, all reproduced against the submitted code first. Two
invalidated claims the PR itself made:

1. **The authorization was not bound to the bytes it authorized.** GitHub
   lets an issue AUTHOR edit the body after the fact, so a contributor
   could submit something benign, wait for a maintainer to label it, then
   rewrite the body - and those new bytes became factory input under an
   authorization granted for different ones. One GraphQL call now fetches
   the trigger label's timestamp and the body's `lastEditedAt`; an edit
   after authorization is refused. Fails closed on every uncertainty.
2. **The stated security premise was FALSE.** Applying a label needs the
   **Triage** role, not push access, so on an organization repo a triager
   who cannot push code can authorize factory spend - and any Action with
   `issues: write` can label. The claim "requires write access" appeared
   in the module docstring, the config comment, the CLI help, and the PR
   body; all corrected, residual risk named, and #188 opened to replace
   the inherited permission with an explicit actor allowlist.
3. **Cross-repository execution.** `target_repo` was metadata only and
   `serve` always runs in its own `root_dir`, so `repo = "B"` inside
   checkout A admitted B's issues and would have opened a PR in A. The
   inbox must now match the checkout.
4. **`dry_run` was not side-effect free** - it suppressed the remote
   writes but still called `queue.add`, so with `ks serve` active a "dry
   run" could launch paid work. Together with the CLI dry-run ignoring
   the admission cap, both had one root cause: two decision trees. There
   is now a single side-effect-free `plan_sync` used by both, so a dry run
   cannot disagree with the real thing.
5. **Admission and dedupe were not transactional.** `queue.add` publishes
   atomically, so a failing `ledger.record` left a live queued item with
   no processed entry AND escaped the whole batch. Now rolled back with a
   structured error per item.
6. **The poll window could not find eligible work.** A fixed `2 x cap`
   window filled with skips hid eligible issues indefinitely, and sorting
   one truncated page does not establish FIFO. Ordering is now requested
   (`sort:created-asc`) and the window widens until the cap is filled or
   the inbox is exhausted. The first fix put that loop inside the poll,
   which was structurally wrong: eligibility is only knowable after
   planning, so the loop belongs where planning happens.
7. **A malformed poll looked like a healthy empty one**, so no cron or
   launchd wrapper could alert. Per-entry tolerance kept; the top level
   is strict.
8. **Remote labels did not track the queue.** Admission labelled the
   issue `running` while the item sat in QUEUED, so a paused or
   backlogged item read as running with no process. Labels now follow real
   transitions - `running` at `queue.start`, `failed` on a queued retry,
   terminal states at the end.
9. **Terminal writeback was incomplete.** Reaper exhaustion, merge-gate
   refusal, `QueueBudgetExhausted`, and an unreaped timeout all poisoned
   and returned without reporting, leaving the issue labelled `running`
   forever. Every committed terminal transition now reports.
10. **Remote I/O ran under the queue mutex**, so an unavailable GitHub
    blocked every local queue transition for the configured timeout.
    Every writeback now happens after the critical section.
11. **Queue-lock contention surfaced as a traceback** rather than an
    actionable message.

All twelve fixes are mutation-checked (28 mutations, 28 caught first run).

**Measured during the live round-trip:** GitHub's issue-list endpoint can
lag a label write by a short interval. A sync issued immediately after
labelling returned `polled: 0`, and the same sync a minute later returned
`polled: 1`. At any realistic poll interval this is invisible, but a test
that labels and syncs in the same breath will look flaky. Not a defect in
the adapter - confirmed by re-running rather than assumed.

**The retry rule as implemented (PR 2).** "Only `infrastructure_error`
failures auto-retry" leaves the UNKNOWN case undefined, and the unknown
case is where the money goes, so `serve.classify_run` implements
*positive evidence only, failing closed*:

| Evidence | Verdict |
|---|---|
| exit 0 | success |
| launch failed before any spend | retry (free) |
| killed by signal / our timeout | retry (external cause, not a spec verdict) |
| exit 2 | spec failure - the architect halted on a blocker |
| every failed component carries `infrastructure_error` | retry |
| any failed component failed on its merits | spec failure |
| nonzero exit, nothing failed | **unclassifiable** - poison |
| manifest unreadable or absent | **unclassifiable** - poison |

The run lock is probed BEFORE launching, which is what keeps exit 2
unambiguous: `ks factory` returns 2 both for a held lock and for an
architect halt, and those need opposite treatment. The classifier reuses
`Finding.is_infrastructure_error` rather than re-deriving the predicate -
two copies of that rule drifting apart is how a spec failure becomes
retryable.

**Four backstops**, because a correct classifier is not sufficient (a
*persistent* infra fault is retryable by the rules and still burns
money): `max_attempts` enforced inside `Queue.start`; exponential backoff
(60s doubling, capped at 30 min); `daily_budget_usd` checked before
admitting each item, pausing until the next LOCAL midnight so a Friday
budget hit is not a dead weekend; and a consecutive-poison breaker that
pauses the whole queue - if `main` is broken then every run fails
verification, each failure is individually legitimate, and no per-item
bound ever notices.

**`daily_budget_usd` honesty (H4).** The budget can only count cost an
adapter reported, and the codex adapter reports tokens with no cost. With
a cost-blind agent the budget is *unenforceable*, not approximate - the
same condition PR #184 named for `max_cost_usd`. The ledger therefore
stores the day's spend WITH its coverage, labels the total a FLOOR
whenever any run under-reported, never converts unreported calls into an
estimated dollar figure, and `ks serve` refuses to run unattended under
an unenforceable budget unless `[serve] allow_uncovered_cost = true`.

**Review corrections (PR #186).** Eleven gaps were caught in review and
closed before merge, all reproduced against the submitted code first.
Recorded because eight were enforcement bypasses in the unattended
spend path, and two broke guarantees the PR body had claimed:

1. **The timeout killed only the direct child.** `subprocess.run(timeout=)`
   signals its immediate child, which on macOS is the `caffeinate`
   wrapper - so the factory was a grandchild and outlived the timeout
   while the daemon recorded an infra failure and requeued the item. Two
   factories on one repo, which is what `factory.lock` exists to prevent.
   Now `Popen(start_new_session=True)` plus `killpg`, with the pgid
   captured AT SPAWN (after the child is reaped it is unrecoverable, and
   that is exactly the case where descendants survive). A timeout whose
   group cannot be confirmed dead poisons instead of retrying. The
   factory child also adopts the queue lease, so a successor's reaper
   cannot requeue a live run.
2. **Accounting and classification read the wrong run.** `serve` read
   `run_id` from the manifest but `Manifest.to_dict` writes `runId`, so
   it got `""` for every real manifest - and an empty id made
   `load_run_state` fall back to the NEWEST run on disk. A failed
   invocation charged a previous run's spend and could be classified from
   a stale manifest. Ownership is now a pre/post snapshot of run
   directories: only runs THIS invocation created are charged, and the
   manifest is read only when its run id is among them.
3. **The architect's spend was invisible.** `decompose_spec` calls the
   agent but emits no usage events at all, so every queued item's
   mandatory architect call was missing from the budget. Metering it
   belongs with the architect's instrumentation, not R8.6, so the phase
   is recorded as a named `unmetered_phases` entry and the day's total is
   always labelled a FLOOR rather than estimated.
4. **The spend ledger failed OPEN.** An unparseable file read as a fresh
   zero day, so charging $9, corrupting it, and setting a $5 budget
   allowed another run - indefinitely, because the other backstops are
   per-item and this is the only queue-wide limit. `ServeStateError` now
   halts the cycle; `FileNotFoundError` remains the one read failure that
   legitimately means "first run".
5. **The poison breaker was built on the queue journal**, which is
   best-effort by design (`_journal` swallows append failures,
   `journal_entries` returns `[]` on any read error). Losing the journal
   reported a zero streak and re-allowed spending with three poisoned
   items on disk. The streak is now authoritative state in `ServeState`,
   reset by a success and NOT by a new day.
6. **A pre-launch lock probe cannot disambiguate exit 2.** The probe
   releases the lock, so a manual factory can take it in the gap - and
   `ks factory` exits 2 for both lock contention and an architect halt.
   The classifier now reads the child's own OUTPUT for each refusal's
   marker and stays unclassifiable when neither is present.
   `factory_lock_held` also fails closed on an unopenable lock file.
7. **The elapsed-pause clear raced operator pauses.** Read and clear
   happened outside the queue mutex, so a fresh emergency pause could be
   overwritten. Both now happen under the lock, re-read inside it.
8. **One full run was spent before an unenforceable budget was noticed.**
   Coverage is now resolved from a persisted `cost_coverage_seen` flag
   before the first claim.
9. **Coverage was inferred from dollars, not calls.** A fully-metered run
   that legitimately cost $0 was rejected as having no coverage, and a
   launch failure counted as a metered run. The ledger now stores
   covered/total call counts, which makes the three cases distinct: exact
   cap, lower-bound cap (fires late - still a bound), and unenforceable.
10. **The exit status followed the classifier, not the outcome.** An
    infra verdict whose last attempt was spent is poisoned, yet
    `may_retry` stays true, so `ks serve --once` exited 0 on work waiting
    for a human - as did the reaper and merge-gate poison paths, which
    set no `ran_item`. `CycleResult.needs_human` is now set on every
    poison and refusal path and drives the exit code.
11. **The daemon retained every poll result forever** with no consumer.
    Bounded runs still return everything; the unbounded path keeps a
    fixed window.

All eleven fixes are mutation-checked (36 mutations, 36 caught). Nine of
the first-draft tests were NOT discriminating and were rewritten before
being counted - including one where the autouse fixture patched the very
function under test, so it passed regardless of that function's
behaviour, and one where a spy counted the liveness probe
(`killpg(pgid, 0)`) as if it were the kill. One "missed" mutation turned
out not to be a defect at all: dropping the spawn-time pgid is
behaviourally identical on the timeout path, so that test now asserts
the wiring and says so.

**Open R8.2 tension found while building PR 2.** `run_factory` assigns
`factory_config.pause_before_pr_merge = bundle.pause_before_pr_merge`
unconditionally, so at L3+ the ladder OVERRIDES an explicit request for
a human merge gate and logs it as "manual override ignored". The R8.2
docstring says the failure mode it guards is a hand-edited flag
*granting* autonomy the ladder never awarded, but the implementation is
symmetric and also refuses a MORE-restrictive request. Rather than change
ladder semantics from inside R8.6, `serve.resolve_merge_gate` REFUSES an
item whose `stop_at_pr` the current level cannot honour (poison + inbox
item) instead of letting the gate be removed silently. Unreachable
today - `[autonomy] enabled` defaults false and L2+ entry is still
blocked on the user-run measurements - but it needs an R8.2 decision
before L3 is real.

**RESOLVED 2026-08-03**
([#195](https://github.com/0xfauzi/kstrl/issues/195)): an explicit
`pause_before_pr_merge = true` survives every level - the bundle may only
WITHHOLD autonomy, never remove a human gate the operator asked for,
mirroring PR #174 correction 1 in the opposite direction.
`serve.resolve_merge_gate`'s refusal stays as a defense-in-depth
backstop. Implementation wrinkle recorded in the issue: `FactoryConfig`
must learn explicit-vs-default provenance for the flag first.

Two invariants in the substrate are money-safety properties rather than
style, and both are mutation-checked:

1. **The attempt is charged before the rename into `running/`.** Every
   transition writes `meta.json` first and renames second, so a crash in
   the commit window over-counts an attempt (one fewer retry - safe)
   instead of under-counting one (a retry nobody recorded - an unbounded
   loop at $1.70-2.60 per first attempt and $3.99-7.42 per retry).
2. **The item directory is authoritative; the sidecar's `state` is a
   mirror.** Readers derive state from the parent directory, so the same
   crash window cannot leave an item whose location and metadata
   disagree about what it is.

Corrupt metadata resolves toward the GATED value in both directions an
attacker or a bad disk could push it: an unreadable `merge_disposition`
decodes to `stop_at_pr`, never `auto_merge`, and an unreadable pause
marker reads as PAUSED, never as running.

**Review corrections (PR #185).** Seven gaps were caught in review and
closed before merge, all reproduced against the submitted code first.
Recorded here because five were enforcement bypasses rather than style
notes, and two of them broke guarantees the PR body had claimed:

1. **`meta.json` was trusted for item IDENTITY**, not just for the
   fields the directory does not carry. A sidecar edited to
   `"item_id": "../../../outside-dir"` loaded normally and then made
   `remove()` delete an unrelated directory while leaving the real item
   in place. The directory entry is now authoritative for identity
   exactly as it already was for state, symlinked items are rejected,
   and every path component passes `is_safe_component`.
2. **`spec_filename` was joined to a queue path unvalidated.** The
   text form of `Queue.add` is the API PR 3's remote adapters will call,
   and `spec_filename="../../../../escaped.md"` wrote outside the queue
   while still publishing a normal-looking item. Validated on enqueue
   AND on decode, with a containment re-check in `spec_path`.
3. **Staging directories were scanned.** A process killed after
   `meta.json` was written but before the publishing rename left a
   `.staging-*` entry inside `queued/` that `items()` returned as a real
   item whose directory did not exist - defeating the stated invariant
   that scans never see partially published work. Staging moved OUT of
   the state directories into `.kstrl/queue/.staging/`, scans skip
   dotted entries as defence in depth, and `sweep_staging()` is the
   explicit recovery policy (run under the queue lock, where an
   abandoned staging dir is stale by definition - no age heuristic).
4. **`max_attempts` was advertised but not enforced.** `start -> fail
   -> requeue -> lease -> start` produced a running item with
   `attempts == 2` against `max_attempts == 1`. The bound now holds at
   the SPENDING boundary (`Queue.start` raises `QueueBudgetExhausted`),
   not only in the CLI's retry path - the callers here are an
   unattended daemon and a reaper, so a bound that requires every caller
   to remember it is not a bound. The pre-existing test skipped the
   second `start()` when no attempts remained, so it asserted the
   caller's good manners rather than the substrate's guarantee.
5. **The per-item `max_attempts` override bypassed `QueueConfig`'s
   validation**, admitting an item with a zero execution budget.
   Rejected in `Queue.add` and by a `click.IntRange(min=1)` option.
6. **`remove()` used `ignore_errors=True` and then journaled success**,
   so a permission failure produced a false operator result AND a false
   audit trail. Failures now propagate and the record is written only
   after the directory is confirmed gone.
7. **The pause marker failed OPEN.** A broad `except OSError` meant a
   `PermissionError` on `pause.json` read as RUNNING, which would have
   resumed unattended spend on an unreadable stop signal.
   `FileNotFoundError` is now the only read failure that means unpaused.

All seven fixes are mutation-checked (14 mutations, 14 caught). Three
of the first-draft regression tests were NOT discriminating - they
passed with the fix reverted, because a second, redundant guard caught
the same case - and were rewritten to assert the specific mechanism
before being counted.

**Why.** Intake is one-shot; the factory has no queue. The first US software
factory (SDC, 1972-78) died because work was not required to flow through
it - intake is a survival capability, not plumbing.

**Verdict: build a thin local substrate, integrate the edges.** Queue
libraries evaluated and rejected (litequeue near-dormant + Python-floor
conflict; persist-queue buys nothing at this concurrency; huey inverts
control). GitHub Issues integrates as the primary remote inbox; launchd
integrates as the scheduler.

**Design.**

- **Substrate:** maildir-style `.kstrl/queue/` (`queued/ leased/ running/
  done/ failed/ poison/`), item = spec file + `meta.json`, transitions via
  `os.replace`, flock singleton, pid/ttl leases with a reaper (the
  sleep/crash recovery path), every transition journaled. ~200-300 lines.
- **Front-ends (pull, no webhooks):** GitHub Issues - poll a `kstrl:queued`
  label via `gh` (ETag-cheap, ~30 req/hr against 5,000/hr), write back state
  labels + result comments, close on merge. Linear - optional second polling
  adapter on the existing client. Linear Agents API deferred until it exits
  preview. Processed-ids ledger makes re-seen issues idempotent; front-end
  outages never block the local queue.
- **Scheduler:** `ks serve` as a launchd KeepAlive LaunchAgent
  (StartInterval misses intervals elapsed during sleep); active runs execute
  under `caffeinate -i`, held only during work so the laptop sleeps between.
- **CLI:** `ks queue add spec.md [--priority N] [--auto-merge|--stop-at-pr]`,
  `ks queue ls|show|retry|rm|pause|resume`, `ks queue sync`,
  `ks serve [--once]` (cron-fallback mode).
- **Safety:** only `infrastructure_error` failures auto-retry with backoff;
  spec failures go straight to `poison/` with a comment back to the source -
  no token-burning crash loops. Remote items default `stop_at_pr`; auto-merge
  is per-item opt-in and ladder-gated. Queue-level `daily_budget_usd` hard
  stop, independent of per-run caps.

**Failure modes.** Queue poisoning (max_attempts + infra/spec distinction);
double-lease (flock singleton + pid-liveness; two-machine operation is an
explicit non-goal for now); laptop sleep mid-run (caffeinate, post-wake
failures classified infra/retryable, lease reaper); unattended spend (the
daily budget is the real cap); governance erosion (continuous intake deletes
the human trigger - `stop_at_pr` default preserves the merge gate).

**Done when:** all queue verbs + `ks serve --once` work; poison path and
budget pause tested; GitHub adapter round-trips labels/comments against a
test repo; launchd plist + caffeinate behavior documented. **All met.**

**launchd and caffeinate (PR 4).** `ks serve --print-plist` generates the
LaunchAgent rather than shipping a template: every path is absolute and
checkout-specific, and a hand-edited template is a class of setup error
(wrong interpreter, wrong root, a `Label` colliding with another checkout)
that costs a debugging session to find. Validated against `plutil -lint`
and round-tripped through `plistlib` in both modes.

Three decisions worth recording:

- **The `Label` is a path hash.** launchd keeps only the last job loaded
  for a given label, silently, so two checkouts of one repo would leave
  one unserved.
- **`ThrottleInterval` is 60s, not launchd's 10s default.** At 10s a
  crash-looping daemon attempts six restarts a minute; the throttle is a
  spend control.
- **`PATH` is set explicitly.** A LaunchAgent inherits no shell
  environment, and both `gh` and `git` must be findable. Getting this
  wrong yields a daemon that runs and silently fails every poll.

**Review corrections (PR #189).** Four gaps were caught in review, all
reproduced first. Two were consequential:

1. **The scheduled job never polled GitHub intake.** `ks serve` never
   called `intake_github.sync`; that was reachable only through the
   manual `ks queue sync`. So an installed LaunchAgent drained a queue
   nothing could fill, and a labelled issue could never enter it. The
   adapter (PR 3) and the daemon (PR 2) were each correct and their
   COMPOSITION did nothing - a seam no single-PR review would catch.
   Intake is now an explicit stage of the cycle, running before the
   admission gates so newly-synced work faces the same budget, breaker
   and cap checks, and strictly additive so an outage cannot stop the
   queue draining.
2. **The `StartInterval` wake contract was stated backwards.**
   `launchd.plist(5)` says a `StartInterval` firing during sleep "will be
   missed due to shortcomings in kqueue(3)", while
   `StartCalendarInterval` "will start the job the next time the computer
   wakes up". The guide claimed the opposite - and this plan had recorded
   the correct behaviour all along. Interval mode now uses
   `StartCalendarInterval`.
3. **`StartInterval` does not bound a wedged cycle either.** The same man
   page: "If the job is running during an interval firing, that interval
   firing will likewise be missed." launchd neither kills nor replaces a
   running job, so a wedge silently stops every later firing - the
   opposite of the "a wedge cannot outlive one interval" claim the PR
   made. `factory_timeout_seconds` is the only real bound, so interval
   mode now refuses to generate without one.
4. **A legal path could produce malformed XML.** Hand-escaping covered
   `&`, `<`, `>` but not control characters, which XML 1.0 cannot
   represent at all. The plist is now built as a dict through
   `plistlib`, which rejects such values outright; that rejection becomes
   a clear `ServeError`.

Also corrected: the claim that a lid close makes the lease owner vanish.
Sleep SUSPENDS processes, it does not kill them - on wake the same serve
and the same factory child resume, and nothing reaps the run because the
process that would reap it is the one running it. The recovery machinery
is for a crash, an OOM kill or a reboot, not an ordinary lid close.

Two modes ship, with the trade-off stated rather than hidden: `keepalive`
(default) runs one long-lived `ks serve` pacing itself from
`[serve] poll_interval_seconds`, relaunched by launchd when it EXITS -
fewer moving parts, but launchd only notices a process that has exited,
so a daemon wedged on a network call looks healthy forever. `interval`
runs `ks serve --once` on a `StartCalendarInterval` calendar schedule
(which catches up after wake) - each cycle is a fresh process whose exit
code carries "needs a human", but launchd bounds neither runtime nor
overlap, so `[serve] factory_timeout_seconds` is the only real bound on
a wedged cycle and interval mode refuses to generate a plist without one.

Two constraints added after that paragraph was written (review #189
N3/N6): the generated interval job also carries
`KSTRL_SERVE_REQUIRE_TIMEOUT`, so every scheduled invocation re-checks
the bound and fails closed if the timeout is later removed - a
generation-time check binds only the config of the day it was run. And
the interval must divide an hour, or be an hour count dividing 24:
`range(0, 24, 5)` yields gaps of 5,5,5,5,4 across midnight, which is not
"every five hours".

**Measured caffeinate behaviour (H4).** With `pmset -g assertions` sampled
around a child process: `caffeinate -i <child>` creates a
`PreventUserIdleSystemSleep` assertion *on behalf of the child*, and that
assertion is gone once the child exits - nothing lingers, which is the
"held only during work" property R8.6 asked for.

The caveat the plan implied but did not state: the assertion is
`PreventUserIdleSystemSleep`, **not** `PreventSystemSleep`. It prevents
*idle* sleep and does not prevent an explicit one, so **closing the lid
still suspends the machine mid-run.**

What that means is narrower than "the run is lost". Sleep SUSPENDS
processes; it does not kill them. On wake the same `ks serve` and the
same factory child resume and the cycle finishes, and nothing reaps the
run because the process that would reap it is the one running it. The
lease reaper, and the classification of a signal-killed process as
infrastructural, exist for the case where the process really is gone - a
crash, an OOM kill, a reboot - not for an ordinary lid close.

**Still not verified (H4), and both need the user.** Sleep/wake resilience
has not been exercised: whether launchd fires a missed interval on wake,
and whether the reaper recovers a run interrupted by a real lid close,
requires genuinely suspending the machine. The `launchd.plist(5)`
contracts quoted above are Apple's documentation, not measurements taken
here. Separately, `ks serve` has never driven a real
factory run - every daemon test uses a stub runner, deliberately, since a
suite that spawned real runs would cost dollars per assertion. The first
unattended run is also the first end-to-end integration test. This is
roadmap open question 11 in concrete form.

---

## R8.7 Release stage, Phase 4 (L) - [#154](https://github.com/0xfauzi/kstrl/issues/154)

Status: `[ ]` - Depends on: R8.2 (L4 gates deploy), R8.6 (re-queue)

**Why.** The factory stops at merged PR + contract tests. Every factory
tradition ends the line at operate, not merge. Largest structural gap.

**Verdict: thin orchestration, two drivers, never per-platform adapters.**
`command` (user shell command + optional `status_command`/`rollback_command`)
and `gha` (`gh workflow run` + `gh run watch --exit-status`; since gh v2.87.0
the run ID returns directly - version-check and keep a poll fallback).
Platform CLIs (fly, Render, Railway, compose-over-SSH) all collapse into the
command driver. Integrate GitHub machinery: Deployments API as the audit
record; environments give approval gates and wait timers for free (plan-gated
on private repos - detect and warn).

**Design.** Phase 4, per-run by default; release ref = merge SHA of the final
tier (always deploy the recorded SHA - main may have moved).

State split: `MERGED` (was `COMPLETED`) -> `RELEASING` -> `RELEASED` |
`RELEASE_FAILED` -> `ROLLING_BACK` -> `ROLLED_BACK` | `ROLLBACK_FAILED`
(halt, human required). Legacy `COMPLETED` aliased to `MERGED` on read,
mirroring the confidence-tier aliasing precedent. Every transition emits
events and a Deployment status. Write-ahead intent record before executing:
deploys are not idempotent; resume asks "did attempt N complete?" via
status_command / `gh run view` instead of re-firing. Per doctrine 6
(2026-08-03), the new states express their terminal outcomes through the
shared disposition + reason-code model rather than growing another
standalone status enum alongside the twelve that already exist.

Verification ladder (each rung optional, failure budget, Argo-analysis
style): exit code -> health poll with SHA match (endpoint must echo the
deployed git SHA or polling can pass against the previous release - which
means DECOMPOSE_PROMPT must require a version-echoing health endpoint in
built services; that is a prompt change, H2/H3 apply) -> `smoke_command` ->
agent-driven E2E (Playwright against the deployed URL, adversarial framing,
separate `agent_verify_max_calls` budget, findings in the standard `Finding`
stream).

Rollback doctrine: restore service first (`rollback_command`), then repo
truth: `revert-and-requeue` (git revert via PR, re-queue with failure
evidence as feedforward - the revert must be in the re-queued story's
feedforward or the engineer will reintroduce the reverted code) or
`fix-forward` per config. Halt-over-heroics for migrations: a failing release
whose diff touched DB migrations halts for a human; the factory does not
auto-undo migrations.

Containment: `enabled=false` default; environment allowlist checked before
any driver runs; `dry_run` prints the exact command + resolved env; release
flock prevents double-deploy; deploy secrets via env allowlist invisible to
engineer agents; release runs from the main checkout, never inside engineer
worktrees; production-named environments require approval unconditionally.

**Failure modes.** Resume-mid-RELEASING double-deploy (write-ahead intent +
status re-query, tested explicitly); health false positives (SHA-stamped
health); rollback restoring the image but not the database (migration halt
rule); revert-requeue skew (revert in feedforward); consumers of terminal
`COMPLETED` (TUI, Linear sync, evolution, resume) all learn the split -
audit them in one PR.

**Done when:** command driver + verification ladder green against a demo
app; resume-mid-RELEASING test proves no double-deploy; rollback and
revert-and-requeue tested; state split lands with the legacy alias and all
consumers updated.

---

## R8.8 Runtime feedback (L) - [#155](https://github.com/0xfauzi/kstrl/issues/155)

Status: `[ ]` - Depends on: R8.6 (queue), R8.7 (release identity)

**Why.** Nothing observes built products at runtime; the learning loop sees
only build-time signals. A factory closes the loop: production behavior flows
back into the queue and the learning substrate.

**Verdict: integrate observability, build a thin poller.** Errors: Bugsink
(single container, SQLite-capable, Sentry-SDK-compatible ingest, versioned
REST API; license is Polyform Shield - user decision 7). Fallback: GlitchTip
(MIT, 4 containers, Sentry-API compatible). Sentry SaaS free tier only for
off-machine products (5k events/mo with silent drop); self-hosted Sentry
ruled out (~30 containers). Health: Gatus for active probes (single binary,
YAML the factory can generate per product, N-consecutive-failures
conditions); dead-man heartbeats for cron-like products - the only
affirmative signal for low-traffic things (never compute error rates on tiny
N; silence proves nothing without a heartbeat). Poll, never webhook: no
inbound HTTP surface, survives laptop sleep.

**Design.**

- **Correlation spine:** scaffold injects SDK init with
  `release="<product>@<version>+<git-sha>"` and environment;
  `.kstrl/releases.jsonl` (written by Phase 4) resolves any error's release
  locally to run/PR/stories.
- **Signal record:** `ks signals poll` normalizes API responses (~30 lines
  per source adapter) into typed events: kind (new_issue, regression,
  frequency_breach, health_down, heartbeat_missed), product, fingerprint,
  release, counts, culprit run/PR, truncated prompt-ready sample, deep link.
- **Dedup:** queue key = `(product, fingerprint)` - one open item per key,
  repeats bump count; a fix PR marks the tracker issue resolved-in-release;
  the same fingerprint in a later release is a regression, reopened at
  higher priority.
- **Threshold ladder:** regression -> enqueue immediately (a shipped fix
  failed - strongest signal); new issue in latest release -> enqueue after
  >= 3 events or 2 distinct users, else watch 24h; frequency breach on
  old/ignored issues -> notify only.
- **Breakers:** storm breaker - more than X new fingerprints within an hour
  of deploy collapses into a single "bad release - investigate/rollback"
  item (sorted by users/count before capping so the cap cannot hide the
  worst issue); lineage breaker - a fingerprint that regresses twice against
  factory-authored fixes stops auto-queueing and flags `needs_human`.
- **Doctrine:** every runtime-fix PR must include a reproducing test -
  converting the runtime signal into a build-time signal the existing
  verifier can hold. This, not the poller, is what prevents
  symptom-patching.
- **Learning:** escaped-defects-per-release becomes a ground-truth stream:
  evolution records which components/story types generate runtime defects;
  calibration gains "did review flag the code that later threw in prod?".

**Failure modes.** Queue floods from bad deploys (storm breaker);
self-fix oscillation (lineage breaker + reproducing-test rule); fingerprint
instability across refactors (SDK fingerprint hints for known error
classes); monitoring the monitor (Gatus probes Bugsink itself); runtime
noise starving planned work (its own budget/priority lane - ties into user
decision 8).

**Done when:** poller normalizes fixture responses from both sources; dedup,
thresholds, and both breakers unit-tested; scaffold injects SDK init +
release tag; end-to-end demo: planted error -> signal -> queue item -> fix
PR carrying a reproducing test.

---

## R8.9 Control-state relocation (M) - [#194](https://github.com/0xfauzi/kstrl/issues/194)

Status: `[x]` - Shipped. Control-plane files resolve under
`${XDG_STATE_HOME:-~/.local/state}/kstrl/<repo-id>/` via
`kstrl/statedir.py` (`control_dir`, `migrate_control_state`,
`control_lock`). Live copies of `autonomy.json`, `inbox.jsonl`,
`spend.json`, `pause.json`, and `github_processed.json` leave the
agent-reachable tree; locks, queue items, events, knowledge, and
worktrees stay in-tree under `.kstrl/`.

**Repo id.** Prefer a hash of the normalized `origin` remote URL so
clones of the same project share one control dir (one autonomy level,
one daily spend ledger). No origin → hash of the resolved absolute path
(checkout-local). Documented caveat: do not run two `ks serve` daemons
against a shared-origin ledger concurrently without holding
`control.lock`.

**Migration.** First control read/write moves any legacy in-tree files
into XDG with a `DeprecationWarning` and writes
`.kstrl/control_relocated` for operators. Legacy paths remain in
`ENFORCEMENT_MACHINERY_PATHS` so a diff recreating them still halts.

**L3+ gate.** `resolve_runtime_level` and `ks autonomy promote` refuse
L3+ while control state is not external (``XDG_STATE_HOME`` under the
repo, inaccessible control dir, or leftover legacy files after a failed
migrate). Fail-closed pause: inaccessible control dir reads as PAUSED.

Depends on: none; BLOCKS enabling `[autonomy]` at L3+ and unattended
`ks serve` with the ladder enabled until control state is external.

## R8.10 Repo readiness: `ks doctor` (M) - [#198](https://github.com/0xfauzi/kstrl/issues/198)

Status: `[ ]` - Added 2026-08-03. Depends on: none; recommended before
the first unattended `ks serve` on any new repo

**Why.** Majority usage is expected to be brownfield, and the most
expensive failure mode on a new repo is discovering unsuitability
mid-run, after money is spent: a red baseline suite fails every
iteration for reasons the agent did not cause; a flaky suite feeds
false failure evidence into the retry classifier; a slow test command
multiplies the measured per-iteration cost by wall-clock. `ks serve`
checks admission before spending at item granularity; `ks doctor` is
the same doctrine at repo granularity.

**Design.** Mechanical only (no LLM), advisory-first, two tiers. Tier A
(static, instant): git/gh state, config validity, verification commands
configured or inferable, language detection (Python gets full
feedforward; otherwise warn, see #200), source/test roots, `.gitignore`
coverage, protected-path candidates suggested for `[policy] paths_deny`.
Tier B (measured, behind an explicit flag, prints what it will run
first): baseline suite run (red baseline = NOT READY), timing, a
two-run flakiness SMOKE (labeled a smoke, not proof), typecheck/lint
baseline noise, an oracle-quality report over the existing suite
reusing the shipped R8.5 linting machinery read-only, and a projected
cost/wall-clock range per component from measured timing. Verdict:
ready / ready-with-warnings / not-ready, plus an ordered fix-first
list, written to a timestamped report.

**Scope constraint (anti-chimera).** Doctor checks ONLY what kstrl
actually consumes; it is not a general repo linter. Additions must name
the kstrl component that consumes the signal.

**Measurement synergy.** Tier B's read-only oracle report over existing
suites accumulates exactly the empirical weak-oracle/coverage
distribution R8.5 Layers 1-2 are blocked on.

**Related brownfield work** (label `brownfield`, tracked outside this
milestone): [#199](https://github.com/0xfauzi/kstrl/issues/199) feeds
`codebase_map.md` + extracted interfaces into the architect prompt (the
planner is currently the context-starved side of the pipeline; H2/H3
apply since `DECOMPOSE_PROMPT` changes);
[#200](https://github.com/0xfauzi/kstrl/issues/200) is the
research-first verdict on language-pluggable interface extraction
(ctags/tree-sitter as edge integrations per doctrine 1, demand check
before any build).

**Failure modes.** A green doctor is repo-readiness, not spec-readiness
(it cannot tell you a task is too cross-cutting for the component
model - the report says so and the fit boundary is documented); the
flakiness smoke is weak by construction; timing gives a range, never a
point estimate; Tier B on suites with side effects is mitigated by the
explicit flag plus print-before-run.

**Done when:** Tier A and Tier B produce the verdict and ordered
recommendations on a real repo; red-baseline, missing-gh, and
non-Python repos each produce the correct verdict in tests; the report
includes the fit-boundaries text, mirrored in the operator docs.

## Non-goals for R8

- **100% dark operation.** Sampled human review persists at every level
  (ironies-of-automation: monitors who never intervene lose the ability
  to); the E6 checkpoint machinery is repurposed, never deleted.
- **Two-machine queue operation.** The processed-ids ledger is per-machine;
  distributed intake is out of scope until an always-on box exists.
- **Per-platform deploy adapters.** The command driver is the escape hatch;
  adapters chasing third-party CLI churn are a maintenance liability.
- **Webhook infrastructure.** Everything polls; Kestrel runs no inbound
  HTTP surface. Revisit only if the Linear Agents API exits preview and
  earns its tunnel.
- **Building queue/monitoring/policy engines in-house** beyond the thin
  substrates specified above.

## Research references

Primary threads behind this plan: DoD cATO evaluation criteria and
DevSecOps reference designs (earned/revocable autonomy); Cusumano, Japan's
Software Factories (intake as survival, process discipline); Greenfield &
Short, Software Factories (economies of scope, limits of mechanization);
Google mutation-testing-at-scale (TSE 2021); "All Smoke, No Alarm"
(arXiv:2606.18168); ImpossibleBench (arXiv:2510.20270); Correlated Errors
in LLMs (arXiv:2506.07962); Meta ACH (arXiv:2501.12862); Levels of Autonomy
for AI Agents (arXiv:2506.12469); Argo Rollouts analysis templates; Western
Electric rules. Per-item source lists live in the R8 issues (#148-#155).
