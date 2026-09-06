# The dampener

A dampener stops the thing you are measuring from getting worse while you
improve it. kstrl's disturbances are ordinary and named: a teammate's commit, a
dependency bump, a base branch that moves under a component. Nothing in the
factory notices when one of those undoes progress the loop already made.

The mechanism is three parts and no LLM:

1. Run the mechanical sensors on a known-good tree and record the structured
   failure signatures in a file the repository tracks.
2. Run the same sensors on a branch.
3. Report what the branch ADDED.

It ships advisory. It prints the report, it exits 0, and it never fails a pull
request until somebody chooses that.

## The signature vocabulary

A signature is `"<check>:<code>"`: `linter:E501`, `typecheck:arg-type`,
`test_suite:assertion-error`. It comes from
`kstrl.evolution.signature_counts_from_verification`, the same function the
evolution journal records failures with, so the dampener and the journal cannot
disagree about what a failure is called.

The baseline counts OCCURRENCES, not distinct signatures. Twelve `E501`s are
twelve, so a branch that adds a thirteenth is a regression rather than a
no-change.

## Writing a baseline

```
uv run ks sense --write-baseline
```

Writes `scripts/kstrl/sense-baseline.json` under `--root`, and prints one line:

```
baseline written: scripts/kstrl/sense-baseline.json (12 signatures, 47 total findings); unmeasured: none
```

Exit code follows the sensors: 0 when the tree is green, 1 when it is not. A
RED baseline is expected and fine. The dampener exists for brownfield
repositories; recording what is wrong today is the point.

It refuses to overwrite an existing baseline. Pass `--force` when you mean to
replace one, and do it in its own commit so the diff shows exactly what moved.

Two things to get right when you write one:

- **Write from a clean tree.** `base_ref` records the commit HEAD points at,
  not the working tree. A baseline written from a dirty tree names a commit
  that is not what was measured.
- **Read the `unmeasured:` list.** It names every sensor that was asked for and
  measured nothing: it timed out, its tool is not installed, or it recorded a
  gap. Those sensors contribute NO signatures to the baseline, so a baseline
  with names on that line has holes in it, and the holes are wherever those
  names are. Fix the cause and regenerate, or accept the holes deliberately.

The timeout is the usual cause. kstrl's own test suite takes about 327 seconds
and the default verify timeout is 300, so its own baseline is generated with
`KSTRL_TIMEOUT_VERIFY=1800` and the workflow sets the same value. A baseline
and a comparison measured at different timeouts are not a comparison.

## Comparing a branch

```
uv run ks sense --compare-baseline
```

Four buckets:

| bucket | means |
|---|---|
| `new` | present now, absent from the baseline |
| `increased` | present in both, and the count went up |
| `fixed` | in the baseline, absent now, and its check MEASURED something now |
| `unmeasured` | in the baseline, absent now, and its check measured nothing now |

Only `new` and `increased` decide the verdict. `fixed` and `unmeasured` are
reported so improvement is visible, and they never make a run red.

The split between `fixed` and `unmeasured` is the part worth understanding. A
signature going away can mean two things: somebody fixed it, or the sensor
stopped running. Those look identical from the outside. `fixed` is a CLAIM that
the problem is gone, so it is only made when the check that produced the
signature measured something in this run; otherwise the signature lands in
`unmeasured` and the report says the check did not run. Without that rule,
uninstalling a linter reads as fixing every one of its findings.

The reverse case is deliberately noisy: a signature from a check the BASELINE
never measured is reported as `new`. That over-reports when a toolchain gains a
binary rather than the tree getting worse. Over-reporting costs a comment
somebody reads; under-reporting costs the mechanism.

### Formats and exit codes

- default: a plain-text report on stdout
- `--format markdown`: the same content as GitHub-flavoured markdown, first
  line `<!-- kstrl-sense-dampener -->` so a workflow can find and edit its own
  earlier comment
- `--json`: the whole `ks sense` document with a `dampener` block added

| condition | exit |
|---|---|
| no regression | 0 |
| regression, no `--fail-on-regression` | 0 |
| regression, with `--fail-on-regression` | 1 |
| baseline missing, unreadable, malformed, or the wrong schema version | 2 |
| bad `kstrl.toml`, a path that is not a directory, git cannot diff | 2 |

The comparison's exit code never follows whether the tree is green. A red tree
is the normal state for the repository this exists for.

A baseline the tool cannot read is exit 2, never an empty baseline. Reading a
malformed file as `{}` would make every current signature `new`, or make every
baseline signature vanish, with nothing failing anywhere. The one thing that is
a note rather than a refusal is a `sense_schema_version` that has moved since
the baseline was written: the document still parses, and refusing would break
every consumer's pull-request check the moment the sensor version bumped.

## Adding it to a repository

1. Write and commit a baseline:

   ```
   uv run ks sense --write-baseline
   git add scripts/kstrl/sense-baseline.json
   git commit -m "chore: record the sense baseline"
   ```

2. Copy `.github/workflows/sense-dampener.yml` from this repository. Four
   things in it are load-bearing and easy to lose:

   - `fetch-depth: 0` on the checkout. `ks sense` asks git for the diff
     against the base strictly; a shallow clone cannot reach the base and the
     command exits 2 on every pull request.
   - `--base "$BASE_REF"` passed explicitly, from the event payload through an
     environment variable. A `pull_request` checkout is a detached merge ref;
     do not make base detection guess, and do not interpolate a ref name into
     a shell script.
   - the fork guard on the comment step. A pull request from a fork gets a
     read-only token whatever the `permissions:` block says, so the comment
     would fail. The report also goes to `$GITHUB_STEP_SUMMARY`
     unconditionally, which a fork author can read. Do NOT reach for
     `pull_request_target` to fix this: it runs the pull request's own test
     suite with a write token.
   - the job fails only on exit 2. A regression is a comment, not a failure.

3. Set `KSTRL_TIMEOUT_VERIFY` in the workflow's env to whatever you generated
   the baseline with.

## Graduating to blocking

In this order, and do not skip the middle step:

1. **Advisory.** Leave it as shipped. Read the comments it posts.
2. **Blocking.** Once the comments have been right on several real pull
   requests, add `--fail-on-regression` to the `ks sense` invocation in the
   workflow. That is the whole change.
3. **Refresh deliberately.** After an intentional change to the measured
   property, regenerate with `ks sense --write-baseline --force` in its own
   commit, with nothing else in it, so the diff shows exactly which signatures
   moved and by how much.

The middle step is not ceremony. A dampener that fails a teammate's pull
request before anyone has checked its output is a dampener somebody turns off,
and a turned-off dampener is worse than none: it looks like a control.
