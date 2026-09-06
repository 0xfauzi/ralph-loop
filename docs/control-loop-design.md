# Control loop design: remodelling kstrl as a control system

Status: adopted as the R10 cycle (tracker at the end of this document). Companion to
[dark-factory-roadmap.md](dark-factory-roadmap.md) and
[continuous-learning-design.md](continuous-learning-design.md). This document
does not supersede either; it supplies the frame both were reaching for.

---

## 0. Why this matters

kstrl decides whether AI-written code is good enough to merge. Today the flag
that says a user story is done is written by the agent that wrote the code, and
nothing in the harness ever writes it. In this repository's own run journal,
the most common non-infrastructure failure is the reviewer disagreeing with that
flag after mechanical verification already accepted it.

That is one defect. It is also a symptom of a missing frame. kstrl is described
as a pipeline of phases, and a pipeline has no vocabulary for "the thing that
measures must not be the thing that acts". Control engineering has had that
vocabulary for eighty years, has a name for every failure it prevents, and has
already paid for the lessons in vehicles that crashed.

This document adopts that frame and a proven method for running a loop rather than
re-deriving one. It changes almost no names, adds no phases, and
edits no prompt body. The value is in the gaps the frame makes visible, not in
the vocabulary.

---

## 1. What a control loop is

Start with a thermostat, because every part has an obvious counterpart.

You want the room at 20 degrees. That number is the **set point**: the state
you are steering toward. A thermometer reports it is currently 17. That
thermometer is the **sensor**, and what it reports is the **measured output**.
Subtract one from the other and you get 3 degrees too cold, which is the
**error**: the only number the loop actually acts on. A box decides what to do
about that error, and that box is the **controller**. It turns on the boiler,
which is the **actuator**: the thing that changes the world. The room itself is
the **plant**, the system being controlled. Someone opens a window; that is a
**disturbance**, a change the loop did not command and cannot prevent, only
respond to.

Two properties make this a loop rather than a script. The sensor observes the
plant *after* the actuator has acted on it, so the next decision is informed by
the last one. And the sensor is independent of the actuator: the boiler does not
get to write the thermometer's reading.

That second property is doing more work than it looks. Suppose the boiler could
report the temperature. It would report 20, always, because that is what it was
asked for. The loop would be perfectly stable and the room would be freezing.
This failure has a name in software already, and the name is Goodhart's law, but
control engineering states it as a structural rule rather than an observation:
**a measurement written by the actuator is not a measurement.**

### Acting before the error appears

A thermostat only reacts. It waits for the room to get cold, then heats. A
smarter design knows a cold front is coming and heats early. That is
**feedforward**: acting on a predicted disturbance before it shows up in the
error. Feedforward is fast and cheap and can be completely wrong, because
nothing checks it. Feedback is slow and expensive and self-correcting. Working
systems use both, and kstrl already uses both words, which is the first sign
this frame is not being imported so much as recognised.

### Loops inside loops

Real vehicles do not use one loop. A rocket has a slow outer loop asking "am I
on the right trajectory", which produces a commanded attitude, and a fast inner
loop asking "am I pointed where I was told", which moves the engine gimbals.
This is **cascade control**, and it has one hard rule: the inner loop must run
faster than the outer loop. If the outer loop issues commands faster than the
inner loop can settle, the outer loop is measuring a system still reacting to
the last command, and the two loops fight. The word for the resulting behaviour
is **oscillation**.

Cascade control is where kstrl's most interesting defect lives, and I will come
back to it in section 3.

### The parts that exist to stop the loop hurting you

A loop that only tracks a set point is not yet safe. Four more parts:

An **integrator** accumulates error over time, so a small persistent gap
eventually produces a large correction. Its failure mode is **windup**: when the
actuator cannot respond, the accumulated error keeps growing, and when the
actuator frees up, the controller commands something enormous based on a debt
that is no longer owed. The fix is **anti-windup**: stop accumulating, or
discharge what is no longer live.

**Saturation** is the actuator being asked for more than it can deliver.
A control surface has a maximum deflection and a maximum rate of movement. Past
that, extra command produces no extra response, and if the controller does not
know it has saturated, it winds up.

A **deadband** is a region around the set point where the controller
deliberately does nothing. Spacecraft attitude thrusters work this way: hold
within a tolerance and fire only at the edges, because firing continuously
would exhaust the propellant to correct errors nobody cares about. Doing
nothing inside a tolerance is a design decision, not laziness.

**Safe mode** is the state a spacecraft enters when it detects it is
off-nominal: shed the mission, point the panels at the sun, phone home, wait for
instructions. The principle behind it is that a system that cannot be trusted to
continue must still be trusted to stop, and stopping must be a designed state
rather than a crash.

### Where the human stands

Three positions, and the distinction is load-bearing.

**In the loop**: the human is a required step; nothing proceeds without them.
**On the loop** (also called over the loop): the loop runs itself and the human
watches, intervenes on exception, and adjusts the loop's parameters between
runs. **Out of the loop**: the human is absent, and the well-documented cost is
that an operator who never intervenes loses the ability to intervene when it
finally matters.

kstrl's roadmap already uses "over the loop" for the middle position
(`docs/dark-factory-roadmap.md:74`), which is the correct term and predates this
document.

---

## 2. kstrl as it actually is, drawn as a control system

kstrl is not one loop. It is six nested loops running at different rates, and
the phase list everybody quotes describes one pass through the middle two.

Every claim in this section I verified by reading the code, and each carries its
`file:line`. Where I state a number from run data I say how many runs it came
from.

### The loop nest

| Rate | Loop | Actuator | Sensor | Set point | Closed? |
|---|---|---|---|---|---|
| 1 | **implement** (seconds to minutes) | engineer agent, `loop.py:608` | none | this story's criteria | **open** |
| 2 | **accept** (minutes) | retry with context, `pipeline.py:1126` | verify + review + security | the component PRD | closed, but winds up |
| 3 | **integrate** (tens of minutes) | schedule, merge, reset breaker, `factory.py:3075` | contract tests | manifest DAG satisfied | closed |
| 4 | **intake** (hours) | queue admission, `serve.py:2419` | queue state, spend, inbox | queue drained | closed |
| 5 | **trust** (days) | autonomy level change, `factory.py:1965` | run outcomes | the autonomy evidence supports | closed, two inputs unwired |
| 6 | **learn** (weeks) | playbook and prompt edits | attribution, calibration | the harness's own detection rates | **open** |

There is a seventh loop that does not exist yet: **operate**, where the plant is
production, the sensor is runtime error rates, and the set point is the
service's SLO. That is R8.7 plus R8.8, and the roadmap already calls the gap
"largest structural" (`docs/dark-factory-roadmap.md:1158-1159`).

### The parts, named

**Set point.** Three layers. Per story, `UserStory.acceptance_criteria` and
`UserStory.passes` (`prd.py:12`). Per component, the whole PRD. Per merge, the
policy envelope (`policy.py:491-541`) and the adequacy gate
(`adequacy.py:640`). There is no fourth layer asking whether the union of
merged components satisfies the original spec.

**Sensors.** `verify.run_mechanical_verification` (`verify.py:1375`) returning
`VerificationResult`; the code reviewer (`review.py`) returning `ReviewResult`
with a per-criterion verdict; the security reviewer (`security.py`); contract
tests (`contract.py`); approved fixtures with snapshot comparison
(`fixtures.py:582-597`); and calibration (`calibration.py:460`), which is the
sensor pointed at the other sensors.

**Error signal.** The typed `Finding` (`findings.py:49`) and the structured
failure signature, `"<check>:<code>"` (`evolution.py:313`). kstrl already has a
well-designed error signal. It does not yet treat it as a quantity to be driven
toward zero.

**Controller.** `ComponentPipeline.process_result` (`pipeline.py:1790`) for
loop 2, the scheduler for loop 3, and the autonomy ladder
(`autonomy.py:227-272`) which selects the controller's aggressiveness from
accumulated evidence.

**Actuator.** The engineer agent behind the `Agent` protocol
(`agents/base.py:458`).

**Disturbances.** Model non-determinism between identical calls; git and GitHub
transport failures; the base branch moving under a component; and upstream
dependency changes. kstrl already separates these from real signal with the
`infrastructure_error` category (`findings.py:25`), which is a genuinely good
piece of design under any name.

**Feedforward.** Phase 0 (`feedforward.py`), computed from the tree with no LLM
call. Already correctly named.

**Safe modes.** They exist and are scattered: the autonomy clamp to L1 on a
malformed state file (`autonomy.py:322-377`), the queue pause on three
consecutive poisons (`serve.py:1536`), the fail-closed refusal to spend when the
control directory is untrusted (`statedir.py:261-290`), and review degrading to
advisory. Four safe modes, no shared name, no single predicate an operator can
ask.

### What kstrl already says

The frame is roughly seventy percent latent in the existing docs, which is why
adopting it is cheap.

`README.md:23` says kstrl "applies harness engineering: a combination of
**feedforward controls** (steer the agent before it acts) and **feedback
sensors** (verify after it acts)". `docs/spec-harness-engineering.md:23` says
its principles are "drawn directly from the article's **cybernetics
framework**", and at `:25` states the diagnosis this document extends: "kstrl
today is feedback-dominant." A `[sensors]` config section was proposed there at
`:243` and never built. `PRODUCT.md:25` sets the product personality as
"**Industrial control room.** Calm, dense, precise."

The words with zero occurrences anywhere in the repository are: set point,
actuator, disturbance, saturation, deadband, and error signal. "Safe mode"
occurs once, inside a test fixture's metadata. `oscillat*` appears twice: a
roadmap risk note and a comment in `git.py`.

---

## 3. The diagnosis

Seven gaps. Each is a thing the frame makes visible that the pipeline framing
does not, and each names the aerospace failure it corresponds to. They are
ordered by how much I think they cost, which is a judgement, not a measurement.

### 3.1 The actuator writes the sensor reading

**What I found.** The engineer prompt instructs the agent, at step 14, to
"Update `$prd_path`: set that story's `passes` to `true`"
(`scripts/kstrl/prompt.md:52`, and the harness default at
`init_cmd.py` step 14). Mechanical verification then calls `check_prd_stories`
(`verify.py:526`), which re-reads the file the agent just wrote and fails if any
story has `passes` false.

I grepped every write to that field. There are six occurrences of `.passes`
across `kstrl/`: `decompose.py:695` validates that stories start false,
`prd.py:340` type-checks it, `prd.py:348` and `verify.py:539` read it,
`init_cmd.py:614` counts it. **Nothing in kstrl ever writes `passes`.** The sole
writer is the agent editing the JSON file.

**The evidence it costs something.** This repository's own journal holds 18
component entries across 5 factory runs. Counting failure signatures by
category: 14 review, 3 pull-request transport. The single most frequent
signature is `review:prd_criterion`, at 5 occurrences.

That signature can only occur on a component where every story was already
marked `passes: true`, because `check_prd_stories` is the first check in the
mechanical chain (`verify.py:1398`) and mechanical verification runs at
`pipeline.py:1921`, before review at `pipeline.py:1960`. So each of those five
is a case where the agent said done, the harness accepted the agent's word, and
an independent reviewer then judged an acceptance criterion unmet.

**Confidence.** The code path I am certain of; I read every line cited. The
frequency is a hint, not a rate: n = 5 runs, all on one trivial project
(`slugify`), and 3 of the 5 died on git or GitHub transport. It needs a real
measurement across varied projects before anyone quotes a percentage.

**The aerospace analogue.** MCAS on the 737 MAX took its angle-of-attack input
from a single sensor with no cross-check against the second one, and acted with
authority the crew could not easily overcome. The lesson is not "sensors fail";
it is that a control authority granted on an unvoted measurement is an
unbounded risk. kstrl's version is milder because a second sensor (the reviewer)
does exist and does disagree. It just is not required to agree.

### 3.2 The fast loop has no sensor

**What I found.** In `run_loop`, the prompt is assembled once, before the
iteration loop: the template is read and substituted, `CLAUDE.md` is prepended,
and the retry context prefix is prepended, all at `loop.py:506-541`. The loop
begins at `loop.py:608` and calls `agent.run(prompt, cwd, timeout=...)`. Nothing
in the loop body reassigns `prompt`. Iterations 2 through N receive a
byte-identical string.

**The fair qualification.** Information does move between iterations, because
the prompt tells the agent to read `prd.json`, `progress.txt` and
`codebase_map.md`, and the agent writes those files. So the inner loop closes
*through the plant* rather than through a sensor. In control terms that is not
feedback; it is the plant talking to itself, and it carries exactly the
independence problem of 3.1 one level down.

**The aerospace analogue.** This is cascade control with the sensors on the
wrong loop. Every working cascade puts a fast, cheap sensor on the inner loop
and the expensive one outside. kstrl has all its sensors on loop 2 and none on
loop 1, which is the inverse.

**The honest counterweight.** In all 5 recorded runs, `avg_iterations` is
exactly 1.00. The engineer emits the completion marker on its first iteration
every time. So on this evidence the inner loop has never actually iterated, the
`max_iterations = 10` ceiling has never engaged, and fixing an open loop that
never runs twice would buy nothing today. This gap is real in structure and
unproven in cost, which is why item 5.10 places it in the "ready to iterate faster"
bucket rather than the first wave.

### 3.3 The retry context is an integrator with no anti-windup

**What I found.** `IterationContext` (`context.py:20`) holds four lists:
`records`, `review_findings`, `verification_failures`, `contract_failures`. The
only mutators are `add_*` methods that append (`context.py:33-45`). There is no
clear, no dedup, no expiry; I grepped the file for all three. The object is
serialised into `component_contexts[comp.id]` on retry (`pipeline.py:1170`) and
re-loaded and appended to at nine separate call sites across `pipeline.py` and
`factory.py`.

`format_for_prompt` (`context.py:48-88`) renders every accumulated entry and
ends with the literal line `"Fix ALL issues listed above before completing."`

So on attempt 3, the agent is handed attempt 1's failures, which it may have
fixed on attempt 2, unmarked as fixed, with an instruction to fix them all.

**This is textbook integrator windup.** Error accumulates, nothing discharges
it, and the controller acts on a debt that is no longer owed.

**The codebase already knows the fix exists.** `Component.review_findings`, a
different object on the manifest, *is* cleared at the start of each attempt
(`pipeline.py:1035`, `manifest.py:735`). The prompt payload is the one that is
not.

**Confidence.** The mechanism I verified directly. The magnitude I have not
measured: I do not know how many tokens of stale failure a third attempt
actually carries, and I will not guess. Section 7 says how to measure it.

### 3.4 There is no derivative term

**What I found.** I grepped `pipeline.py`, `verify.py` and `review.py` for any
comparison against a previous attempt's result. Three hits, all about computing
a git diff, none about trend.

The nearest thing is the no-progress breaker (`breaker.py`), which halts after
`no_progress_iterations` consecutive iterations whose worktree fingerprint and
test signature are both unchanged, default 3 (`breaker.py:60`). That detects
*zero* change. It does not detect insufficient change, and it does not detect
change in the wrong direction.

**The failure it misses.** An agent that fixes one lint error and introduces two
per iteration changes the fingerprint every time, resets the stall counter every
time, and never trips. The error is growing and the loop is content.

**The aerospace analogue.** The derivative term in a PID controller responds to
the *rate* of error change, which is what damps overshoot and catches
divergence. kstrl has proportional behaviour (act on current findings) and
integral behaviour (accumulate them, badly, per 3.3) and nothing derivative.

I am not proposing a literal PID controller; see section 6 for why.

### 3.5 No flow control

**What I found.** I grepped for `max_open|open_prs|wip_|work_in_progress|
concurrent_pr` across `kstrl/`. Zero hits. The only bounds on work in flight are
`max_parallel = 4` (`factory.py:169`), the serve daemon running exactly one
queue item per cycle (`serve.py:1875-1880`), and the inbox open-item cap of 50
(`inbox.py:267`), which gates admission of new work rather than output.

Nothing stops kstrl opening pull requests faster than a human reviews them.

**The argument.** Without a bound, a loop that runs daily can generate several
unreviewed pull requests in a week, which produces review fatigue and merge
conflicts. The proven default is one open pull request per loop, with manual
runs bypassing the bound.

**The aerospace analogue.** This is the difference between a control system and
an open-loop actuator command: if the loop produces output faster than the plant
can absorb it, the queue is the thing that saturates, and the human reviewer is
the plant.

### 3.6 Two demotion triggers have no emitter

**What I found.** `DemotionTrigger` (`autonomy.py:90-101`) declares five causes:
policy violation, calibration regression, health breach, human-rejected
auto-merge, manual. Only `POLICY_VIOLATION` is wired to fire automatically
(`factory.py:2042-2043`). `CALIBRATION_REGRESSION` is referenced only by the
offline replay tool, which explicitly does not mutate state. `HEALTH_BREACH`
has no emitter at all, because the module that would compute it does not exist:
`ls kstrl/ | grep -iE "health|trend|ewma"` returns nothing. That is R8.4,
pending.

**Why this belongs in this document.** The autonomy ladder is gain scheduling:
a controller whose aggressiveness is selected by operating regime, exactly as a
flight controller swaps gains between subsonic and supersonic flight. Gain
scheduling with unwired inputs schedules on partial information. The ladder can
currently only be demoted by a policy violation, so a factory that is quietly
getting worse without violating policy keeps its autonomy.

Every threshold in that ladder is also labelled, in the source, an unmeasured
placeholder (`autonomy.py:38-42`). That is honest and it is also a standing
liability.

### 3.7 The learning loop is open, and the outer loop has no return path

The first half is already diagnosed, thoroughly, in
[continuous-learning-design.md](continuous-learning-design.md): proposals are
written and never read back, and the mechanism that would close it is
attribution. I add nothing to that analysis and this document adopts it whole.

The second half is not yet stated anywhere. kstrl's output is a pull request.
A human reviews that pull request and leaves comments. Those comments do not
reach the next run by any path. Pull request bodies are write-only outputs, the
Linear integration is a one-way outbound sink, and inbound HTTP is an explicit
non-goal (`docs/dark-factory-roadmap.md:1383-1385`). The only inbound channels
are the local CLI and a GitHub label.

So the outermost loop that involves a human is open at the point where the human
actually produces the most information.

---

## 4. Vocabulary

The temptation is to rename everything. I looked at what that costs and it is
the first design I rejected, so I will state it before stating what I propose.

### The rejected design: rename the modules

`verify.py` becomes `sensor.py`, `[breaker]` becomes `[dampener]`,
`ComponentStatus` gains control-flavoured values, and the eight adversarial
roles get renamed to loop components. It reads beautifully in a diagram.

Four things kill it.

First, doctrine 6 of the dark-factory roadmap forbids exactly this: "Twelve
distinct terminal-outcome vocabularies already exist across manifest, pipeline,
PR, inbox, queue, and serve. New surfaces express outcomes as a shared
disposition plus a structured reason code instead of minting another enum.
Existing enums are not retrofitted" (`docs/dark-factory-roadmap.md:102-107`).

Second, `breaker` is not a dampener. A circuit breaker interrupts; a dampener
opposes rate of change. They are different components in control theory too, and
the existing name is the correct one. Renaming it would make the vocabulary
*less* accurate, which is the opposite of the point.

Third, the config sections in `README.md` are generated from the config
dataclasses by `scripts/gen_docs.py`, and CI fails if they are stale. Every
section rename is a dataclass move plus a regeneration.

Fourth, and decisively: any wording change inside `DECOMPOSE_PROMPT`,
`REVIEWER_PROMPT`, `SECURITY_PROMPT`, `DISTILL_PROMPT` or `DEFAULT_PROMPT`
requires a version bump, a hash update in `_EXPECTED_SNAPSHOTS`, a calibration
re-run, and human review, all in one PR (H3, `CLAUDE.md:37`). A vocabulary
re-frame that touches prompt bodies is a calibration-gated change. Paying that
for aesthetics would be indefensible.

The rename is cost without mechanism. Skip it.

### What I propose instead

**Adopt as the primary explanatory frame, in prose and in new code only.** Set
point, sensor, controller, actuator, plant, disturbance, error signal,
feedforward (already used), cascade, saturation, deadband, anti-windup, flow
control, safe mode, human on the loop (the roadmap's "over the loop" is the same
thing and stays).

**Restructure two documents.** `ARCHITECTURE.md` currently leads with the phase
chain, which describes one pass through loops 2 and 3. It should lead with the
loop nest from section 2 and present the phase chain as what happens inside one
tick. This is a docs change with no code impact and it is the single highest
leverage item in this section, because every future design decision inherits the
frame it is written in.

**Name the new surfaces for what they are.** Where section 5 adds code it takes
control-loop names, because new names are free and existing ones are not:
`ks sense` for the standalone sensor command, a safe-mode predicate, a dampener
baseline, and `kstrl/health.py` for R8.4, which the roadmap already calls that.

**Rename nothing that exists.** If a rename later proves worth it, the
established pattern is rename-with-read-alias, precedent at `CLAUDE.md:95`
(`"verified"` aliased to `review_passed`) and planned again for
`COMPLETED` to `MERGED` (`docs/dark-factory-roadmap.md:1173-1176`).

**Add no phases.** Doctrine 5 freezes the first-class phase count
(`docs/dark-factory-roadmap.md:98-101`). Everything in section 5 lands inside an
existing phase or outside the phase chain entirely.

### Glossary

Terms used in this document, defined once, in the order a reader meets them.

| Term | Meaning here |
|---|---|
| plant | the thing being changed: the target repository and, after R8.7, the running service |
| set point | the state kstrl is steering toward: acceptance criteria, policy envelope, adequacy floor |
| sensor | anything that measures the plant independently of the agent that changed it |
| measured output | what a sensor reports (a `VerificationResult`, a `ReviewResult`) |
| error signal | the gap between set point and measured output: kstrl's `Finding` stream and failure signatures |
| controller | the code that turns the error signal into the next action: retry, halt, merge, demote |
| actuator | the engineer agent, and later the release driver |
| disturbance | change kstrl did not command: model non-determinism, transport failure, a moving base branch |
| feedforward | acting on a predicted disturbance before it appears in the error: Phase 0 |
| cascade | loops inside loops, where the outer loop's output is the inner loop's set point |
| saturation | the actuator being asked for more than it can deliver |
| windup | error accumulating while the actuator cannot respond, producing a stale over-correction |
| deadband | a tolerance band around the set point where the controller deliberately does nothing |
| level-triggered | acting on current measured state, not on the history of events that got there |
| flow control | bounding work in flight so the loop cannot outrun the reviewer |
| safe mode | a designed degraded state entered on detected fault, held until a human intervenes |
| human on the loop | the human supervises and intervenes on exception; the roadmap's "over the loop" |

---

## 5. Adopting the loop

### 5.0 The correction that reorganises this section

An earlier draft of this document gated seven mechanisms behind "measure this
number first". That was wrong, and it was wrong in a way worth naming, because
the mistake is a common one.

The method this document adopts does not resolve an unknown threshold by
measuring before building. It ships the mechanism in **advisory mode**, where it
measures and reports but does not block, and graduates it to blocking once the
operator trusts the signal. A regression gate under this method is advisory by
default, never failing a teammate's pull request, with a documented path to
blocking once the team trusts the signal.

That strictly dominates measure-then-build. It delivers the signal immediately,
it produces the measurement as a byproduct of running rather than as a
precondition, and when the number finally arrives the mechanism is already in
place to use it. Measure-first delays both the value and the data.

**kstrl already has this tier and already wrote down the reasoning.** Review
runs `hard`, `advisory` or `skip` (`review.py:35-38`). The adequacy gate runs
`advisory` or `block` (`adequacy.py:655`). And `findings.py:165-179` states the
doctrine outright: an adequacy finding defaults to advisory because "a gate that
blocks on an invented number teaches people to switch gates off."

So the graduation path is not a new mechanism. It is kstrl's own advisory tier,
applied consistently instead of ad hoc.

### 5.0.1 Why the numbers were unmeasured in the first place

There is a reason kstrl's thresholds are all placeholders, and it is not
negligence. **You cannot cheaply measure a sensor you cannot run.**

`ks --help` lists fifteen commands. Not one of them runs a sensor on its own.
There is no `ks verify`, no `ks review`, no way to point mechanical verification
at a working tree and read the result. Every sensor in kstrl is reachable only
by starting a factory invocation, which needs a PRD, cuts a branch, creates a
worktree, spends money on an agent, and takes minutes.

The method treats this as a hard gate, not a nicety: run the sensor standalone
and read its output, run the controller against that output, run the actuator
on a selected target, and only proceed to automation once each piece runs
locally on its own. This keeps the loop debuggable and makes the workflow a thin
orchestrator of things the operator can already run.

kstrl inverted that order. It built the orchestrator first, and the components
are now only reachable through it.

This single gap explains most of section 7 in the earlier draft. Once a sensor
can be run by hand for free, its threshold stops being a research project and
becomes an afternoon. So local-first is not one item among nine. It is the item
that makes the others cheap, and it goes first.

---

### 5.1 Make every component runnable standalone

**Local first, adopted as a hard gate.**

Add commands that run each loop component by hand against a working tree, with
no PRD, no branch, no worktree, and no agent spend:

- **`ks sense`** runs the mechanical sensors against the current tree and prints
  the measurement: which checks pass, which fail, and the structured findings.
  `run_mechanical_verification` (`verify.py:1375`) already takes a path and
  returns a `VerificationResult`. This was written up as a command wrapper over
  a function that already exists, and that premise was wrong: the verifier was
  built for a worktree the factory owns and disposes of, so it felt free to edit
  and commit. `check_dead_code` ran `ruff --fix`, `git add -A` and `git commit`;
  `check_bad_patterns` left `__pycache__` beside every file it scanned. Pointed
  at the operator's live checkout those are destructive, so the sensor needs a
  read-only mode, not just a wrapper. Its diff reads need the same care: the
  lenient git helpers map an unresolvable base onto an empty file list, which
  reads as a clean tree, so a standalone sensor must preflight the diff and
  report could-not-measure rather than pass.
- **`ks sense --review`** runs the adversarial sensors over a diff. This one
  costs an LLM call, so it is opt-in and reports its cost, but it makes the
  reviewer inspectable without a factory run.
- **`ks sense --json`** emits the measurement in machine form, which is what
  makes every threshold in this document measurable by piping rather than by
  instrumenting.

**Why this is first.** It is the cheapest item, it unblocks the rest, and it is
the one place where kstrl departs from a proven method for no stated reason.

**Acceptance:** the operator can run sensor, controller and actuator locally
and independently. That is a checklist, not a
number.

### 5.2 Set point: two sensors must agree

**Fixes gap 3.1, the headline defect.**

Fail the component when the agent claims `passes: true` for a story whose
criteria the reviewer did not independently mark `pass`. The reviewer already
emits a per-criterion verdict (`review.py:65`) and already detects uncovered
stories (`review.py:547`). The second sensor exists and is simply not consulted.

**A correction to the earlier draft.** That draft claimed this crosses the H3
prompt-versioning boundary, because the engineer prompt's step 14 would become
misleading. On re-reading, it does not. Step 14 says "set that story's `passes`
to `true` (only after step 9 is green AND the self-critique is written)".
That instruction stays exactly correct. What changes is not what the agent is
asked to do; it is that its claim stops being the sole authority. The flag was
always a claim. The harness just started treating it as one.

**So B1 touches no prompt body, needs no version bump, and needs no calibration
run.** It is a mechanical check inside an existing phase. That makes it both the
highest-value and one of the cheapest items in the document, which is the
opposite of what the earlier draft concluded.

**Ships advisory, graduates to blocking.** In advisory mode it records every
disagreement between claim and verdict as a finding without failing the
component. That is the measurement, produced by running. Flip it to blocking
when the disagreement rate is understood, which by construction takes as long as
it takes to see a few real runs.

### 5.3 Controller: level-triggered, not accumulating

**Fixes gap 3.3, the integrator with no anti-windup.**

At the start of each attempt, re-derive the live failure set from the last
measurement instead of replaying the accumulated log. Render resolved items
separately and briefly, so the agent does not re-break them. Keep the full
history in the journal, where history belongs.

The vocabulary is Kubernetes'. An edge-triggered system acts on transitions
("this failure happened"); a level-triggered system acts on current state ("this
failure is happening now"). `controller-runtime` withholds the event type from
`Reconcile()` on purpose, "because a controller should not care why it was
triggered, only what the current state of the world looks like".

`ks sense` from 5.1 is what makes the re-derivation cheap, which is the second
time local-first pays for itself.

**Acceptance:** for a component reaching attempt 3, the rendered retry context
contains exactly the findings live at the start of attempt 3, plus a short
resolved list. Assert it in a test. No threshold involved.

### 5.4 Actuator: golden patterns before automation

**Golden patterns first, and kstrl has only half of it.**

The rule: before automating, establish what a good change looks like. Ask the
operator which existing patterns in the codebase should be followed, inspect the
code to find them, and capture them where the actuator will read them.

kstrl distils facts after the fact (`knowledge.py`) and computes structure ahead
of time (`feedforward.py`). Neither is a curated statement of what a good change
looks like in this repository. The distiller writes what it observed; nobody
writes what is wanted.

**Adopt:** a curated golden-patterns section, operator-authored, injected
alongside the knowledge prefix. It is a file. It needs no measurement and no new
machinery, because `context_prefix` assembly at `factory.py:1424-1447` already
joins several sources and will join one more.

### 5.5 Disturbance: add the dampener

**The dampener, which kstrl does not have in any form.**

The mechanism, precisely: run a full deterministic scan on the main branch,
sort the results deterministically, track the count in version control, and on
every new pull request check whether the branch made it worse. It is a
disturbance dampener: it stops concurrent work from undoing the loop's progress
while the loop runs.

kstrl's disturbances are real and named: concurrent human commits, dependency
bumps, a base branch that moves under a component. Nothing stops the measured
property regressing while the factory improves it. The closest existing thing is
fixture snapshot regression (`fixtures.py:582-597`), which covers approved
fixtures only and only inside a factory run.

**Adopt:** `ks sense` writes a baseline to version control. A check on pull
requests compares the branch's measurement against that baseline and reports
newly introduced findings. **Advisory first**, with a documented path to
blocking.

This is the second reason 5.1 goes first: the dampener is the same sensor,
pointed at a diff instead of a tree.

### 5.6 Flow control: one open pull request

**One open pull request, adopted as the default.**

Scheduled and daemon-driven runs stop admitting new work when an open
kstrl-authored pull request already exists. Manual invocations bypass the bound,
because a human typing the command is the authorisation.

The rationale: without a bound, a daily loop can generate several unreviewed
pull requests in a week, producing review fatigue and merge conflicts. Stated as
a rule: a loop may be handed only as much autonomy as its output can be cheaply
and reliably verified, and not one inch more.

**The default is 1, and it is configurable.** The earlier draft wanted a month
of review-throughput data before setting it. That was over-cautious for a value
that is one line of config, reversible in seconds, and already carries a proven
default from a team running this in production. Set it to 1, raise it if it
chafes.

### 5.7 Memory: a standing feedback file

**A memory file, adopted with its loading rule.**

A version-controlled markdown file loaded deterministically into the actuator's
context **after the controller**, on every run. What belongs in it: permanent
scope exclusions, known false-positive areas, and reviewer feedback that should
change future work. What does not: one-off instructions and single-run logs.

kstrl has three weak versions of this already: the `## Agent Learnings` section
of `CLAUDE.md`, per-directory `AGENTS.md` files, and `codebase_map.md`. What is
missing is the loading rule. None of them is guaranteed to be loaded after the
controller's output, so standing feedback can be shadowed by the retry context
rather than framing it.

**Adopt:** one named file per loop, loaded at a fixed position after the
controller's output in `context_prefix`. The ordering is the mechanism, not a
detail.

### 5.8 Steering: the `/iterate` channel, polled

**A steering channel, adapted to kstrl's stated non-goal.**

A maintainer comments on a pull request; the loop that created it picks the
comment up, updates the pull request, and writes durable guidance into the
memory file from 5.7. This is the tuning mechanism: it is how the operator
tunes the controller over time.

kstrl cannot use a webhook for this. Inbound HTTP is an explicit non-goal
(`docs/dark-factory-roadmap.md:1383-1385`), and that decision is sound for a
single-operator tool with no server.

**The version that respects the constraint:** `ks serve` already polls GitHub
every 60 seconds (`serve.py:239`) and already reads labels. Extend the same poll
to read comments on kstrl-authored pull requests carrying the run marker.
Polling is not a webhook. The non-goal survives intact.

**Acceptance:** a comment left on a kstrl pull request appears in the next run's
assembled prompt, and durable guidance from it lands in the memory file.
Directly assertable.

### 5.9 Two kstrl-specific fixes the method surfaces

**Name safe mode.** kstrl degrades safely in at least four places: autonomy
clamping to L1 on a malformed state file (`autonomy.py:322-377`), the queue
pausing after three consecutive poisons (`serve.py:1536`), refusing to spend
when the control directory is untrusted (`statedir.py:261-290`), and review
falling back to advisory. Each is correct; none can be asked about together. One
predicate, one event, one dashboard row. JPL's spacecraft carry one named
safe-mode response rather than four unrelated fallbacks, and the value is
exactly that it is one.

**Fix the priority inversion.** When `max_adversarial_calls` is exhausted, an
unchunked review is downgraded to SKIP and the component proceeds on mechanical
checks alone (`pipeline.py:2465-2478`). Chunked hard-mode review instead fails
closed (`pipeline.py:2479-2500`), which is right. So under budget pressure kstrl
drops the reviewer, which accounts for 14 of the 17 failure signatures in the
local journal. Apollo 11's guidance computer shed load by discarding
low-priority jobs and keeping navigation, guidance and engine control. Overload
shedding needs a priority policy decided in advance, and kstrl's currently drops
the sensor doing most of the catching. Make the unchunked path halt, matching
the chunked one.

Two fair qualifications: the degrade is loud, and `max_adversarial_calls = 0`
means unbounded by default, so this is latent rather than active.

### 5.10 Ready to iterate faster

**Ready to iterate faster, and this is where the two speculative mechanisms
belong.**

The sequencing rule is explicit: only once the loop is tuned and producing
consistent, high-quality output do you increase frequency, widen the batch, or
run more cycles per invocation. That is the right home for the two items the
earlier draft could not justify, and it is a better frame than "measure first",
because it says what has to be true rather than what has to be counted.

**A sensor on the fast loop** (gap 3.2). Run a cheap subset of `ks sense`
between engineer iterations and feed the result forward, making the cascade
well-formed. Turn this on when the loop is otherwise tuned and components are
taking more than one iteration. Today `avg_iterations` is 1.00 across all five
recorded runs, so there is no inner loop to instrument yet. Note that 5.1 makes
this nearly free when the time comes, since the sensor will already run
standalone.

**A convergence check** (gap 3.4). Halt when the live-finding count stops
decreasing across attempts, which catches the agent that fixes one finding and
introduces two. It can currently fire at most at attempt 3, because
`max_retries` defaults to 3, which is when retries are exhausted anyway. Raise
the retry ceiling or leave this off. It is the weakest item here and it should
be the last built.

### 5.11 Wire the dead demotion triggers

`DemotionTrigger` declares five causes and fires one. `CALIBRATION_REGRESSION`
can be emitted today from `compare_baselines`, which already returns a verdict
and is currently consulted only by an offline replay tool. `HEALTH_BREACH` needs
R8.4, which stays a tracked roadmap item rather than being re-owned here.

**On the control-chart rules R8.4 will use:** ship them advisory, exactly like
everything else in this section. The published false-alarm arithmetic (one point
beyond three sigma fires by chance about once in 370 observations; all four
Western Electric rules together, about once in 92) is a reason to watch the
advisory output before letting a breach auto-demote, not a reason to delay
building. `ks autonomy replay` already exists, already reports without mutating
state, and is the advisory mode for this particular gate.

---

## 6. What this design refuses

The most useful part of a design is usually the option it killed and why. Five,
in descending order of how tempting they were.

### 6.1 A literal PID controller

**The idea.** Treat the finding count as a continuous error, add proportional,
integral and derivative terms with tunable gains, and tune them from run history.

**Why not.** PID's mathematics assume a continuous plant with a linear response
to actuator effort. kstrl's plant responds to a natural-language instruction, its
error signal is a set of typed categorical findings rather than a scalar, and
there is no meaningful sense in which "more control effort" produces
proportionally more correction. Gains tuned on this would be numerology.

**What survives.** The three behaviours, named honestly rather than lettered.
Reacting to the current error is what kstrl already does. Accumulating persistent
error is item 5.3, and its failure mode, windup, is a real defect kstrl has
today. Responding to the *rate* of error change is the convergence check in 5.10,
and it is genuinely missing. Take the diagnosis, leave the arithmetic.

### 6.2 Gain and phase margin

**The idea.** Quantify how much the review threshold could move before the loop
oscillates.

**Why not.** Gain and phase margin are properties of a frequency response.
kstrl has no frequency response, no transfer function, and no periodic input.
Any number produced under these names would be invented, which
`CLAUDE.md` forbids in as many words.

**What survives.** The qualitative pair, which is genuinely useful vocabulary: a
gate tuned too strict oscillates (reject, resubmit, reject) and a gate tuned too
loose is sluggish (merges defects). That is worth saying. It is not worth
measuring in decibels.

### 6.3 Kalman filtering the sensor outputs

**The idea.** Fuse reviewer, security and mechanical outputs into a single state
estimate with uncertainty.

**Why not.** A Kalman filter needs a dynamic model of how the state evolves and
Gaussian noise on the measurements. LLM findings are neither independent nor
identically distributed, and there is no state-transition model to predict from.
The aerospace research agent independently flagged exact-majority voting as
forced here for the same reason.

**What survives.** The underlying discipline, which kstrl already practises:
a raw sensor reading is not the state. That is precisely why
`exhaustively_searched` is rendered but never gates, and why `CLAUDE.md` says
self-reported flags are hints, not signals. Item 5.2 extends the same discipline
to `passes`, which is currently the one self-report that *does* gate.

### 6.4 Renaming the existing vocabulary

Covered in section 4. Forbidden by doctrine 6, expensive because of generated
docs, and in the case of `breaker` it would make the vocabulary less accurate,
not more. The rename is cost without mechanism.

### 6.5 Nyquist and sample-rate analysis

**The idea.** Argue that loop 2 undersamples the plant because the plant changes
ten times per attempt while the sensor reads once.

**Why not.** Nyquist is about aliasing a continuous signal, and there is no
continuous signal here. The observation that the fast loop is unmeasured is real
and it is gap 3.2; dressing it in sampling theory adds a claim the system does
not support.

---
## 7. What advisory mode buys, and what it costs

Every threshold this document once wanted measured in advance is now produced by
a mechanism that is already running and already useful. That is the whole
substitution, and it is worth stating what each one becomes.

| Was gated on measuring | Now produced by | Blocks anything? |
|---|---|---|
| how often the reviewer disagrees with the done flag | 5.2 running advisory | no |
| how much stale text attempt 3 carries | 5.3, asserted in a test | no |
| review throughput, to set the WIP bound | 5.6 shipped at 1 | no |
| live-finding distribution per attempt | 5.1 `ks sense --json`, piped | no |
| control-chart false-alarm rate | 5.11 replay, advisory | no |
| iterations per component | `experiments.tsv`, already written | gates only 5.10 |
| latency cost of an inner-loop sensor | measured when 5.10 turns it on | gates only 5.10 |

One gate survives: 5.10 is conditional on the loop being tuned and producing
consistent output. That is not a measurement gate. It is a readiness gate.

**The honest cost of advisory-first.** An advisory finding that nobody reads is
worse than no finding, because it manufactures the appearance of a gate. kstrl
already carries the antidote: `phase_skipped` and `infrastructure_error` are
first-class finding categories precisely so that `len(findings) == 0` means
every phase ran and found nothing. Advisory output must land in that same
stream, on the pull request body and in the journal, or it is decoration.

**What graduation requires, stated once so it is not improvised later.** A gate
moves from advisory to blocking when the operator has seen its output on real
runs and can name what it caught and what it flagged wrongly. That is a
judgement, deliberately, and it is the operator's. Writing a number here would
be inventing one, which is the failure this whole section replaces.

---

## 8. Sequencing

The method's own phase order, applied to kstrl. Each ships before the next
starts, and every gate ships advisory unless it is mechanical and exact.

| Order | Item | Method phase | Advisory first? | Touches a prompt? |
|---|---|---|---|---|
| 1 | 5.1 `ks sense` standalone | D, run locally first | n/a | no |
| 2 | 5.3 level-triggered controller | B, controller | n/a, exact | no |
| 3 | 5.2 set-point agreement | B, set point | yes | **no** |
| 4 | 5.9 safe mode, priority inversion | kstrl-specific | n/a | no |
| 5 | 5.5 dampener | B, disturbance | yes | no |
| 6 | 5.6 flow control, bound 1 | G | n/a | no |
| 7 | 5.4 golden patterns | B, actuator | n/a | no |
| 8 | 5.7 memory file | F, human on the loop | n/a | no |
| 9 | 5.8 polled `/iterate` | F, human on the loop | n/a | no |
| 10 | 5.11 wire demotion triggers | kstrl-specific | yes | no |
| 11 | 5.10 faster loop, convergence | H, iterate faster | yes | no |

**Nothing in this plan edits a prompt body.** That is a change from the earlier
draft, which mis-read the set-point fix as requiring it. H3 is not engaged, no
version bump is needed, and no calibration run is required by anything on this
list. Calibration should still be run after the set-point change lands, not
because H2 demands it but because a new blocking condition near the reviewer is
worth checking against the fixtures.

**Why this order and not the earlier one.** Item 1 moved to the front because
every later item is cheaper once the sensors run by hand, and because it is the
one place kstrl departs from a proven method with no stated reason. Item 3 moved
up because removing its supposed H3 cost made it cheap as well as important.
Items 10 and 11 moved to the back, not because they need data first, but because
the readiness rule puts them there.

### What this does not change

The eight adversarial roles, the phase numbering, every existing enum, the
roadmap item IDs, the `[breaker]` config section, and every prompt body.

### Relationship to the existing roadmap

This is new work, filed as the R10 cycle under the existing tracker
convention (each cycle has its own R number and milestone). R8.4 stays R8.4. Item 5.8 consumes
the global playbook that
[continuous-learning-design.md](continuous-learning-design.md) phase 2 builds
and should land after it. The operate loop named in section 2 is R8.7 plus R8.8
and stays theirs.

---
## Sources

Control theory and aerospace:

- Åström and Murray, *Feedback Systems*, https://fbsbook.org
- Cascade attitude control, https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0328622
- Shuttle computer redundancy and voting, https://people.cs.rutgers.edu/~uli/cs673/papers/RedundancyManagementSpaceShuttleIBM76.pdf
- Spacecraft attitude deadband control, https://ntrs.nasa.gov/api/citations/20160001829/downloads/20160001829.pdf
- Actuator rate saturation and pilot-induced oscillation, https://ntrs.nasa.gov/api/citations/20100033693/downloads/20100033693.pdf
- JPL fault protection and safe mode, https://s3vi.ndc.nasa.gov/ssri-kb/static/resources/05-2750.pdf
- Flight mission rules, https://ntrs.nasa.gov/api/citations/19750002893/downloads/19750002893.pdf
- Sheridan and Verplank, levels of automation, 1978, https://www.researchgate.net/publication/23882567_Human_and_Computer_Control_of_Undersea_Teleoperators
- Apollo 11 1201/1202 alarms, https://www.nasa.gov/wp-content/uploads/static/history//alsj/a11/a11.1201-fm.html
- Ariane 5 flight 501, https://en.wikipedia.org/wiki/Ariane_flight_V88
- Mars Polar Lander review board report, https://www.dcs.gla.ac.uk/~johnson/Mars/mpl_report.pdf
- 737 MAX final committee report, https://democrats-transportation.house.gov/imo/media/doc/final_boeing_737_max_report1.pdf

Control loops in software:

- Kubernetes controllers, https://kubernetes.io/docs/concepts/architecture/controller/
- Level-triggering and reconciliation, https://www.chainguard.dev/unchained/the-principle-of-reconciliation
- HPA tolerance and stabilization window, https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/
- SRE book, embracing risk and error budgets, https://sre.google/sre-book/embracing-risk/
- SRE workbook, alerting on SLOs, https://sre.google/workbook/alerting-on-slos/
- Addressing cascading failures, https://sre.google/sre-book/addressing-cascading-failures/
- Hellerstein et al., *Feedback Control of Computing Systems*, https://onlinelibrary.wiley.com/doi/book/10.1002/047166880X
- Western Electric rules and their false-alarm arithmetic, https://handwiki.org/wiki/Western_Electric_rules
- Goodhart's law in reinforcement learning, https://arxiv.org/pdf/2310.09144
- GEPA, https://arxiv.org/abs/2507.19457
- ACE, https://arxiv.org/abs/2510.04618

### Closing note

kstrl began as a bare loop that re-ran a prompt until a condition held. It has
spent its whole R-series moving away from that starting point, adding sensors,
gates, envelopes, budgets and an autonomy ladder, without a word for what it was
moving toward. This document supplies the word. Most of the work was already
done.

---

## Tracker

Cycle: R10, milestone [R10: Control Loop](https://github.com/0xfauzi/kstrl/milestone/3), tracking issue [#235](https://github.com/0xfauzi/kstrl/issues/235). Status legend: `[ ]` pending, `[~]` in progress, `[x]` done. Tick the box in the same PR that lands the item (audit-trail doctrine).

| Order | Item | Issue | Status |
|---|---|---|---|
| 1 | 5.1 `ks sense` standalone sensor command | [#222](https://github.com/0xfauzi/kstrl/issues/222) | `[x]` merged in #237 |
| 2 | 5.3 level-triggered retry context | [#223](https://github.com/0xfauzi/kstrl/issues/223) | `[x]` |
| 3 | 5.2 set-point agreement | [#224](https://github.com/0xfauzi/kstrl/issues/224) | `[x]` |
| 4 | 5.9 name safe mode | [#225](https://github.com/0xfauzi/kstrl/issues/225) | `[x]` |
| 5 | 5.9 adversarial budget: hard mode halts | [#226](https://github.com/0xfauzi/kstrl/issues/226) | `[ ]` |
| 6 | 5.5 dampener | [#227](https://github.com/0xfauzi/kstrl/issues/227) | `[ ]` |
| 7 | 5.6 flow control | [#228](https://github.com/0xfauzi/kstrl/issues/228) | `[ ]` |
| 8 | 5.4 golden patterns | [#229](https://github.com/0xfauzi/kstrl/issues/229) | `[x]` |
| 9 | 5.7 memory file | [#230](https://github.com/0xfauzi/kstrl/issues/230) | `[ ]` |
| 10 | 5.8 polled steering | [#231](https://github.com/0xfauzi/kstrl/issues/231) | `[ ]` |
| 11 | 5.11 wire the dead demotion triggers | [#232](https://github.com/0xfauzi/kstrl/issues/232) | `[ ]` |
| 12 | 5.10 iterate faster (blocked on entry criterion) | [#233](https://github.com/0xfauzi/kstrl/issues/233) | `[ ]` |
| 13 | section 4, reframe ARCHITECTURE.md | [#234](https://github.com/0xfauzi/kstrl/issues/234) | `[ ]` |

Follow-up from R10.4, now closed: safe mode had no dashboard surface, so the default interactive `ks status` never showed it. A masthead chip plus an `m` panel closed that gap over the same predicate, with no second evaluation and no second wording.

Graduation follow-ups are listed on #235 and are filed only after the advisory item they depend on has produced output on real runs. The lesson that teaches this document is `docs/lessons/pr-221.html`.
