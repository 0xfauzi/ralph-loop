# Continuous intake (R8.6)

How to make work flow into the factory without firing each run by hand,
and what each safety mechanism actually guarantees.

Three layers, each usable without the ones above it:

| Layer | Command | Needs |
|---|---|---|
| Local queue | `ks queue add/ls/show/retry/rm/pause/resume` | nothing |
| Daemon | `ks serve [--once]` | the queue |
| GitHub inbox | `ks queue sync` | `gh`, an opt-in config |
| Scheduling | launchd | `ks serve` |

**Start at the top.** For a single operator, the local queue plus
`ks serve` already delivers the thing that matters - work runs without you
firing it - with no remote surface, no tokens, and no polling. The GitHub
layer adds the ability to queue from your phone and see status where your
code lives. That is a convenience, not the capability.

---

## 1. The local queue

```bash
ks queue add specs/add-widget.md --priority 3
ks queue ls
ks queue show <id>
```

Items live under `.kstrl/queue/` as one directory each (spec + `meta.json`),
moved between `queued/ leased/ running/ done/ failed/ poison/` by a single
`os.replace`. The spec is **copied** at enqueue, so editing or deleting the
original afterwards cannot change what runs. Locks (`queue.lock`,
`serve.lock`) stay beside the queue. The pause marker and spend ledger do
**not**: under R8.9 they live in the XDG control directory
(`${XDG_STATE_HOME:-~/.local/state}/kstrl/<repo-id>/`) so a worktree agent
cannot edit them. First use migrates any legacy in-tree copies and writes
`.kstrl/control_relocated` pointing at the new location. Clones that share
the same `origin` remote share one control dir: do not run two `ks serve`
daemons against that ledger at once.

`ks queue pause` stops new work being claimed; it does not touch a run
already in flight. `ks queue resume` re-opens intake.

### Attempts are money

`[queue] max_attempts` bounds how many times one item may execute. The
counter is charged **before** the run starts, so an interrupted attempt
counts. That direction is deliberate: over-counting costs the item one
retry, while under-counting is an unbounded loop, and a measured engineer
iteration costs **$1.70-2.60** first-attempt and **$3.99-7.42** on a retry.

`ks queue retry <id>` refuses an item that has spent its attempts unless
you pass `--reset-attempts`. That flag is the point at which you are
authorizing more spend.

---

## 2. `ks serve`

```bash
ks serve --dry-run     # what would happen, spending nothing
ks serve --once        # one cycle
ks serve               # poll until interrupted
```

One factory run at a time, holding a daemon singleton lock. `--dry-run`
reports every admission gate in the order the real loop evaluates them.

### Only infrastructure failures retry, and only on positive evidence

The rule is not "retry unless it looks like a spec problem". It is
**nothing retries without affirmative evidence that the failure was
infrastructural**:

| Evidence | Verdict |
|---|---|
| exit 0 | success |
| launch failed before any spend | retry (free) |
| killed by signal, or our timeout with the process group confirmed dead | retry |
| exit 2 with a lock-contention marker in the output | retry |
| exit 2 with a spec-blocker marker | **poison** |
| the run halted on a configured ceiling (`max_total_tokens` / `max_cost_usd`) | **poison** |
| every failed component carries `infrastructure_error` | retry |
| any failed component failed on its merits (with findings) | **poison** |
| a component failed with NO finding at all | **poison** (unclassifiable; its own error is printed) |
| nonzero exit, nothing blamed | **poison** (unclassifiable) |
| manifest unreadable, or not produced by this invocation | **poison** (unclassifiable) |
| timeout whose process group could **not** be confirmed dead | **poison** |

A **budget halt is listed separately from infrastructure on purpose.** The
factory records a blown ceiling as an `infrastructure_error` finding, but
it is deliberate and deterministic: retrying re-runs the same work against
the same limit, and a retry costs MORE because it carries accumulated
context. Raising the ceiling or narrowing the spec is a human decision.

Poisoned items wait for a human. `ks queue ls --state poison` lists them;
`ks inbox ls` carries the decision.

### Five backstops, because a correct classifier is not enough

A *persistent* infrastructure fault is retryable by the rules above and
still burns money. So:

1. **`[queue] max_attempts`** - enforced inside the queue itself, not just
   by the daemon's policy.
2. **Exponential backoff** - 60s doubling, capped at 30 minutes.
3. **`[serve] daily_budget_usd`** - checked *before* admitting each item,
   pausing until the next **local** midnight so a Friday-night stop is not
   a dead weekend.
4. **`[serve] max_consecutive_poison`** - pauses the whole queue. If the
   base branch is broken, every run fails verification, each failure is
   individually legitimate, and no per-item bound ever notices.
5. **`[serve] max_open_prs`** - flow control on the OUTPUT rather than
   the spend. The first four bound what the daemon starts; this one
   bounds what it leaves behind for a human to read.

### Flow control: `max_open_prs`

Scheduled admission stops while `max_open_prs` kstrl-authored pull
requests are open. The default is 1. Without a bound, a daily loop can
generate several unreviewed pull requests in a week, producing review
fatigue and merge conflicts. The rule: a loop may be handed only as much
autonomy as its output can be cheaply and reliably verified.

A pull request counts as kstrl-authored when its body carries the footer
line kstrl writes on every PR it opens, which covers pull requests
created before this bound existed. The count comes from `gh pr list` in
the repo root, and a count that FAILS refuses admission rather than
reading as zero.

The refusal is a wait, not a pause: nothing needs to be resumed, and the
next cycle admits work as soon as the pull request is merged or closed.

**Manual `ks factory` and `ks run` bypass the bound entirely**, because a
human typing the command is the authorisation. Only the daemon's own
admission consults it. Set `max_open_prs = 0` to switch it off, or raise
it if 1 chafes.

### What `daily_budget_usd` can and cannot do

It counts only cost an adapter **reports**. The codex adapter reports
tokens and no cost, and `decompose` (the architect) emits no usage events
at all - so **every** queued item has some unmetered spend.

Three cases, and they are reported distinctly:

- **full coverage** - the cap is exact.
- **partial coverage** - a *lower-bound* cap: it fires at or after the
  threshold, never before. Still a real bound. Every total is labelled
  `(a FLOOR: ...)` with what was not counted.
- **zero coverage** - no call has ever reported a cost figure, so the cap
  can never fire. `ks serve` **refuses to run** unless you set
  `[serve] allow_uncovered_cost = true`.

The unreported spend is deliberately **never** estimated into a dollar
figure. A number that looks like a measurement but is a guess is worse
than an admitted gap.

---

## 3. GitHub Issues as an inbox

```toml
[intake_github]
enabled = true
repo = "owner/name"          # must match the checkout you run in
queued_label = "kstrl:queued"
max_items_per_sync = 5
```

```bash
gh label create "kstrl:queued"  --color 0e8a16
gh label create "kstrl:running" --color fbca04
gh label create "kstrl:done"    --color 0075ca
gh label create "kstrl:failed"  --color d93f0b
gh label create "kstrl:poison"  --color b60205

ks queue sync --dry-run
ks queue sync
```

An issue carrying `kstrl:queued` becomes a queue item. The verdict comes
back as a state label and a comment. Polling only - no webhooks, nothing
to keep reachable.

### What actually authorizes work

**Read this before enabling it on a repo that others can reach.**

A stranger can open an issue but cannot label it. However:

- Applying a label needs the **Triage** role, **not** push access. On an
  organization repo, a triager who cannot push a line of code can
  authorize factory spend.
- Any GitHub Action in the repo with `issues: write` can apply the label,
  so a workflow can trigger spend with no human involved.

This is a permission designed for *managing issues*, borrowed to authorize
*money*. [Issue #188](https://github.com/0xfauzi/kstrl/issues/188) replaces
it with an explicit actor allowlist. Until then, the residual risk is
exactly those two bullets.

**What is enforced today:** an issue edited *after* it was labelled is
refused. GitHub lets an issue author rewrite the body after a maintainer
has labelled it, so the label is bound to the body revision it authorized.
Re-apply the label to authorize the new text.

**Remote items always stop at the PR.** No label and no config value can
grant auto-merge to remote-sourced work.

### Cross-repository intake is refused

`ks serve` always runs the factory against its own checkout, so an inbox
pointing at a different repo would open a PR in the wrong place. Sync
refuses unless `[intake_github] repo` matches the checkout's remote.

### One measured quirk

GitHub's issue-list endpoint can lag a label write. A sync issued
immediately after labelling returned `polled: 0`; the same sync a minute
later returned `polled: 1`. Invisible at any realistic poll interval, but
do not expect label-then-sync-in-one-breath to work.

---

## 4. Scheduling with launchd

Generate a LaunchAgent for this checkout. Every command below was run as
written:

```bash
ks serve --print-plist --root "$PWD" > /tmp/kstrl.plist
plutil -lint /tmp/kstrl.plist                      # sanity check
LABEL=$(plutil -extract Label raw -o - /tmp/kstrl.plist)
cp /tmp/kstrl.plist ~/Library/LaunchAgents/$LABEL.plist
launchctl load  ~/Library/LaunchAgents/$LABEL.plist
launchctl list | grep kstrl                        # confirm it is loaded
```

To stop it:

```bash
launchctl unload ~/Library/LaunchAgents/$LABEL.plist
```

Logs land in `.kstrl/logs/`. **Measured on a real launchd run: everything
goes to `serve.err.log` and `serve.out.log` stays empty** - the console UI
writes to stderr, so `serve.err.log` is the normal operating log, not an
error-only one. Tail that:

```bash
tail -f .kstrl/logs/serve.err.log
```

### Two modes

```bash
ks serve --print-plist                                        # keepalive
ks serve --print-plist --plist-mode interval --plist-interval 10
```

- **`keepalive`** (default) - one long-lived `ks serve` pacing itself from
  `[serve] poll_interval_seconds`; launchd relaunches it when it **exits**.
  Sleep is survived trivially: the process simply resumes on wake.
- **`interval`** - `ks serve --once` on a **calendar** schedule.
  `--plist-interval` is in MINUTES and must divide an hour evenly
  (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60) or be whole hours.

### What launchd does NOT do

Two guarantees people assume and `launchd.plist(5)` explicitly denies:

> **`StartInterval`** - "If the system is asleep during the time of the
> next scheduled interval firing, that interval will be missed due to
> shortcomings in `kqueue(3)`. **If the job is running during an interval
> firing, that interval firing will likewise be missed.**"

> **`StartCalendarInterval`** - "Unlike cron which skips job invocations
> when the computer is asleep, launchd will start the job **the next time
> the computer wakes up**."

So:

1. **Only `StartCalendarInterval` catches up after sleep.** Interval mode
   uses it for that reason. `StartInterval` would silently skip every
   firing that elapsed while the lid was shut.
2. **launchd never bounds how long a job runs, and never replaces one
   still running.** It just skips the firing. A wedged cycle is therefore
   not killed - it silently stops every later one.

Because of (2), interval mode **refuses to generate** unless
`[serve] factory_timeout_seconds` is set. That timeout is the only real
bound on a cycle; `ThrottleInterval` limits relaunch after exit, not
runtime.

### Why the label is a path hash

Two checkouts of the same repo would otherwise collide on one launchd
`Label`, and launchd keeps only the last job loaded for a given label -
silently. The hash means a worktree and its parent can each be served.

### `PATH` is set explicitly

A LaunchAgent inherits none of your shell environment. Both `gh` and `git`
must be findable, so the plist sets `PATH` to the interpreter's directory
plus the usual system and Homebrew locations. Getting this wrong produces
a daemon that runs and silently fails every poll - the hardest setup bug
to see. If your tools live elsewhere, edit the `PATH` entry.

### The restart throttle is a spend control

`ThrottleInterval` is 60s, not launchd's 10s default. At 10s a
crash-looping daemon would attempt six restarts a minute.

---

## 5. caffeinate: what it does and does not prevent

`[serve] caffeinate = true` (the default on macOS) wraps each factory run
in `caffeinate -i`, so the machine will not fall asleep mid-run, and sleeps
freely between runs.

**Measured** with `pmset -g assertions` around a child process:

- During the run, a new assertion appears:
  `PreventUserIdleSystemSleep ... caffeinate asserting on behalf of <child>`.
- After the child exits, that assertion is **gone**. Nothing lingers.

**The caveat that matters:** the assertion is
`PreventUserIdleSystemSleep`, not `PreventSystemSleep`. It prevents *idle*
sleep. It does **not** prevent an explicit sleep - **closing the lid will
still suspend the machine mid-run.**

What that actually means is narrower than "the run is lost". Sleep
*suspends* processes; it does not kill them. On wake the same `ks serve`
and the same factory child resume and the cycle finishes. The lease TTL
may have elapsed during the suspend, but nothing reaps it, because the
process holding the run is the same one that would do the reaping and it
is busy running.

The recovery machinery exists for the case where the process really is
gone - a crash, an OOM kill, a reboot - not for an ordinary lid close.

---

## 6. Troubleshooting

| Symptom | Check |
|---|---|
| Daemon runs, nothing happens | `ks serve --dry-run` - it prints every gate and which one blocks |
| Queue paused unexpectedly | `ks queue ls` shows the reason; budget pauses clear at local midnight |
| `ks serve` refuses to start | a budget is set with no cost coverage; see §2, or set `allow_uncovered_cost` |
| Items poisoned in a row | the poison breaker paused the queue; something systemic is failing |
| `sync` finds nothing | the label may not have propagated yet (§3); confirm with `gh issue list --label kstrl:queued` |
| launchd job not running | `launchctl list \| grep kstrl`; then `.kstrl/logs/serve.err.log` |
| `serve.out.log` is empty | expected - the UI writes to stderr; read `serve.err.log` |
| Component failed, cause unclear | an unevidenced failure now prints the component's own error; check it before suspecting the spec |
| Every poll fails silently under launchd | `PATH` - `gh` is not findable (§4) |
| Daemon says `N kstrl PR(s) open` | flow control is holding the queue; merge or close the PR, or set `[serve] max_open_prs = 0` |

---

## 7. What is verified, and what is not (H4)

**Verified by test:** the queue state machine and its money-safety
invariants, the retry classifier's every branch, all four backstops, the
lease reaper, process-group termination, the GitHub adapter's parsing,
planning, idempotency, authorization binding and writeback, and that the
generated plist parses with `plistlib`/`plutil` in both modes.

**Verified by hand, once:** the GitHub round-trip against a real
repository - label polled, item enqueued with provenance, label swapped,
re-sync correctly skipped, terminal writeback leaving exactly one state
label plus a comment. And the caffeinate assertion lifetime above.

**Verified by a live run (2026-08-03):** a LaunchAgent generated by
`--print-plist --plist-mode interval --plist-interval 1` loaded with
`launchctl load`, fired twice on its calendar schedule, ran
`ks serve --once` to completion each time, and reported
`LastExitStatus = 0`. And `ks serve` drove a real `ks factory` run end to
end - claiming the item, charging the attempt, launching the factory,
classifying the outcome, transitioning the item, filing the inbox entry
and exiting nonzero.

**NOT verified:**

- **Sleep and wake behaviour end to end.** The `launchd.plist(5)`
  contracts quoted above are Apple's documentation, not measurements
  taken here, and nothing has been exercised against a genuinely
  suspended machine. **If you plan to run this on a laptop, close the lid
  mid-run once and confirm the cycle finishes on wake and the calendar
  job fires.**
- **Automated coverage of a real factory run.** Still true, and still
  deliberate: a suite that spawned real runs would cost dollars per
  assertion, so no test runs a factory. The end-to-end path above is
  verified by hand only.

  Narrowed since (#205, `tests/test_serve_seam.py`): the *launch* half no
  longer depends on that hand check. `subprocess_factory_runner` is now
  executed for real against a stub interpreter - only `sys.executable` is
  replaced, so the argv, the cwd, the `KSTRL_NO_TUI` env, the caffeinate
  wrapping, the process-group spawn and the `RunOutcome` mapping are all
  shipping code - and the argv it builds is then parsed by the real
  `ks factory` Click command, so a flag renamed on either side fails a
  test instead of the next unattended run. A further set drives
  `serve_cycle` with no injected runner at all, which is the only path
  that executes `_default_runner`.

  Measured by mutation, each of these passed the whole suite before those
  tests existed:

  | Mutation | Suite before |
  |---|---|
  | Rename the merge-gate flag in the runner | 3,125 pass |
  | `_default_runner` forwards a wrong `project_name` | 3,140 pass |
  | Drop `caffeinate_prefix` from the runner's command | 3,140 pass |
  | `_default_runner` ignores `serve.caffeinate` | 3,140 pass |

  The caffeinate ones are worth knowing about: `caffeinate -i` **execs in
  place** (measured, macOS 25.5), so the wrapper leaves the child's pid,
  argv and exit status all identical and its absence is invisible from
  the outside. The tests use a fake `caffeinate` on `PATH` that touches a
  marker before exec'ing, which is the only way the wiring is observable
  at all - and they patch `sys.platform` so they run on CI's ubuntu
  rather than skipping there, which is where a macOS-gated test would
  have been useless.

  What remains uncovered is everything *below* `ks factory`'s argument
  parsing: no component is built, no review runs, and the classification
  of a real run's on-disk artifacts is still exercised only through stub
  runners. A regression there is still caught only by a live run.
- **Rate-limit behaviour under sustained polling.** One call per poll
  interval is ~60/hour against a 5,000/hour budget, so this is expected to
  be a non-issue, but it has not been driven to a limit.
- **Conditional requests.** The R8.6 plan credited ETags for cheap
  polling. `gh issue list` exposes no ETag, so that saving is not
  realised. It is not needed at this cadence.
