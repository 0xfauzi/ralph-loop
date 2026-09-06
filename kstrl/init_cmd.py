"""Init command for kstrl - initialize harness in a project."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from kstrl import git
from kstrl.appendio import append_records
from kstrl.atomicio import atomic_write_text
from kstrl.prd import PRD

if TYPE_CHECKING:
    from kstrl.ui.base import UI

# Default file contents
DEFAULT_PRD = {
    "branchName": "kstrl/feature",
    "userStories": [],
}

DEFAULT_PROMPT_VERSION = "1.3.0"

# v1.3.0 (#276): step 9 defers to the verification block the harness
# injects (verify.VERIFY_COMMANDS_PROMPT) instead of telling the agent to
# find its own typecheck and test commands. #261 made
# verify.resolve_verify_commands the only answer to "what does Phase 1
# run" and injected it above this body every iteration, but step 9 still
# told the agent to derive two of the three and never mentioned lint at
# all - so a blocking `ruff check` failure was discovered by the gate
# rather than by the agent, which costs a whole engineer iteration.
# Step 14 carried a second copy of the same done-rule ("only after
# tests/typecheck pass"), with the same lint omission, at the moment the
# agent flips passes to true; it now refers to step 9 instead.
#
# The no-block branch states a FLOOR, not a proportionality hint, and it
# is scoped to marking a story done rather than to every iteration:
# `ks feature`'s implement and repair loops reach it while writing
# production code with no Phase 1 at all (feature_cmd has no
# verification), while an `ks understand` iteration that reaches it -
# only un-init'd, since `ks init` scaffolds a separate
# understand_prompt.md - marks no story and so owes nothing.
#
# INTERIM, tracked as #288. A text floor is not a mechanism: nothing on
# that path samples whether the agent ran anything, and it asks the agent
# to find commands that resolve_verify_commands could have named, which
# is the derive-your-own shape #261 exists to remove. The harness stays
# silent there because VERIFY_COMMANDS_PROMPT claims the gate runs the
# commands, which would be false, and because #261 decided that
# verify_config=None states nothing. Revisiting that is #288's job.
#
# The $prd_path / $progress_path / $codebase_map_path placeholders are
# substituted by loop.run_loop (string.Template.safe_substitute) with the
# per-component paths, so a decomposed component's agent reads the SAME
# PRD file that verify.check_prd_stories re-reads (R2.3, H-11). Before
# v1.1.0 the body hardcoded scripts/kstrl/prd.json while decomposed PRDs
# live at scripts/kstrl/feature/<id>/prd.json - the agent and the
# verifier disagreed on which file mattered.
DEFAULT_PROMPT = """# kstrl Agent Instructions

You are the implementing engineer in a software factory. You will be
reviewed by a hostile code reviewer when you declare done; treat that
reviewer as already reading your diff while you write it.

## Your Task (one iteration)

1. Read the PRD file for this run: `$prd_path`
2. Read `$progress_path` (check `## Codebase Patterns` first)
3. Derive a short list of keywords from the PRD intent, not just exact wording.
4. If `$codebase_map_path` exists, query it for sections relevant to your story
   using those keywords.
   - Do not load the entire file.
   - Always check **Quick Facts** and any relevant **Iteration Notes**.
5. If a feature understand file exists for this PRD, query it using the same keywords.
   - Default path: `scripts/kstrl/feature/<feature_name>/understand.md`
   - If the PRD is at `scripts/kstrl/feature/<feature_name>/prd.json`, use that folder name.
   - Otherwise use the PRD filename stem as `<feature_name>`.
6. Branch is pre-checked out to `branchName` from the PRD
   (verify only; do not switch)
7. Pick the highest priority story where `passes` is `false` (lowest `priority` wins)
8. Implement that ONE story (keep the change small and focused)
9. Run the verification commands:
   - Run every command in the `Verification Commands (resolved by kstrl)` block
     above, lint included, exactly as written. Do NOT derive your own or substitute
     a narrower or broader variant (an added path, a `-k` filter, a dropped flag):
     a command the gate will not run proves nothing.
   - If a command fails for a reason other than your work, fix the cause and never
     substitute a different command. Missing tooling is yours to configure. A
     command that is wrong for this project's language is not: name the `[verify]`
     section of `kstrl.toml` in your progress entry rather than editing it, because
     kstrl's policy envelope can treat that edit as tampering.
   - Do NOT mark the story as done until every command passes. If that block is
     absent, nothing will check this work mechanically: run the project's own
     typecheck and tests yourself first.
10. If you discover durable, reusable codebase facts, append a brief, evidence-based note to
   `$codebase_map_path` under **Iteration Notes** or update **Quick Facts**
   (skip if nothing new).
11. Update `AGENTS.md` files with reusable learnings
   (only if you discovered something worth preserving):
   - Only update `AGENTS.md` in directories you edited
   - Add patterns/gotchas/conventions, not story-specific notes
12. **Adversarial self-check.** Before declaring done, append the EXACT
    heading `## Self-Critique` (verbatim, two hash marks - the harness
    verifies this string) followed by AT LEAST 3 bullet lines (`- `).
    Each bullet must be substantive: not `TBD`, not `TODO`, not `N/A`.
    Format each bullet as: `- If X happens, this code will do Y, which
    is wrong because Z.` Categories to consider: invalid/empty/None
    input, concurrent access, partial-failure mid-way through a
    multi-step operation, hostile input, schema drift, missing
    auth/authz check, swallowed errors, performance under load,
    time/locale dependence. If you genuinely cannot find three, look at
    every new function and ask what could break it. Placeholders will
    fail the mechanical check.
13. Commit with message: `feat: [ID] - [Title]`
14. Update `$prd_path`: set that story's `passes` to `true`
    (only after step 9 is green AND the self-critique is written)
15. Append learnings to `$progress_path`

## PRD ambiguity

If the PRD is too vague to implement responsibly, do NOT guess. Append an
`## INTERPRETATION` block to `$progress_path` stating what
assumptions you are making and why. The reviewer will see this and can
push back; silent guesses become silent bugs.

## Progress Format

Append this to the END of `$progress_path`:

## [YYYY-MM-DD] - [Story ID]
- What was implemented
- Files changed
- Verification run (exact commands)
- **Learnings:**
  - Patterns discovered
  - Gotchas encountered
- **Self-Critique:**
  - Failure mode 1: ...
  - Failure mode 2: ...
  - Failure mode 3: ...
- **Interpretations** (only if PRD was ambiguous): ...
---

## Codebase Patterns

Add reusable patterns to the TOP section in `$progress_path`
under `## Codebase Patterns`.

## Stop Condition

If ALL stories pass, reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""

DEFAULT_PROGRESS = """# kstrl Progress Log

## Codebase Patterns
- (add reusable patterns here)

## Iteration Notes
- (append entries below using the format in prompt.md)

---
"""

DEFAULT_CODEBASE_MAP = """# Codebase Map (Brownfield Notes)

This file is meant to be built over time using the kstrl **codebase understanding** loop.

## How to use this map

- **Evidence-first**: prefer citations to specific files/entrypoints over broad claims.
- **Read-only mode**: in understanding mode, the agent should ONLY edit this file.
- **Small increments**: one topic per iteration keeps notes high-signal.

## Next Topics (checklist)

Edit this list to match your repo. During the understanding loop, mark items as done.

- [ ] How to run locally (setup, env vars, start commands)
- [ ] Build / test / lint / CI gates (what runs in CI and how)
- [ ] Repo topology & module boundaries (where code lives, layering rules)
- [ ] Entrypoints (server, worker, cron, CLI)
- [ ] Configuration, env vars, secrets, feature flags
- [ ] Authn/Authz (where permissions are enforced)
- [ ] Data model & persistence (migrations, ORM patterns, transactions)
- [ ] Core domain flow #1 (trace end-to-end)
- [ ] Core domain flow #2 (trace end-to-end)
- [ ] External integrations (third-party APIs, webhooks, queues)
- [ ] Observability (logging, metrics, tracing, error reporting)
- [ ] Deployment / release process

## Quick Facts (keep updated)

- **Language / framework**:
- **How to run**:
- **How to test**:
- **How to typecheck/lint**:
- **Primary entrypoints**:
- **Data store**:

## Known "Do Not Touch" Areas (optional)

- (add directories/files that are fragile or off-limits)

---

## Iteration Notes

(New notes append below; keep older notes for history.)
"""

DEFAULT_FEATURE_UNDERSTAND = """# Feature Understand Notes

This file captures feature-specific understanding tied to one PRD.

## How to use this file

- **Evidence-first**: prefer citations to specific files/entrypoints over broad claims.
- **Feature scope**: keep notes anchored to the PRD for this feature.
- **Small increments**: one topic per iteration keeps notes high-signal.

## Quick Feature Facts (keep updated)

- **PRD**:
- **Branch**:
- **Stories in scope**:
- **Primary entrypoints**:
- **Data touched**:
- **Tests / commands**:

## Story Coverage (checklist)

- [ ] (add story IDs from the PRD)

## Known Risks / Hotspots (optional)

- (add areas likely to break or require extra care)

---

## Iteration Notes

(New notes append below; keep older notes for history.)
"""

DEFAULT_UNDERSTAND_PROMPT = """# kstrl Codebase Understanding Instructions (Read-Only)

## Goal (one iteration)

You are running a **codebase understanding** loop. Your job is to explore the existing codebase
and write an evidence-based "map" for humans.

**Hard rule:** do NOT modify application code, tests, configs, dependencies, or CI.

**The only file you may edit is:**
- `scripts/kstrl/codebase_map.md`

If you think code changes are needed, write that as a note in the map under
**Open questions / Follow-ups**. Do not implement changes in this mode.

## What to do

1. Read `scripts/kstrl/codebase_map.md`.
2. Choose ONE topic to investigate this iteration:
   - If `codebase_map.md` has a **Next Topics** checklist, pick the first unchecked item.
   - Otherwise follow this default order:
     1) How to run locally
     2) Build / test / lint / CI gates
     3) Repo topology & module boundaries
     4) Entrypoints (server/worker/cron/CLI)
     5) Configuration, env vars, secrets, feature flags
     6) Authn/Authz
     7) Data model & persistence (migrations, ORM patterns)
     8) Core domain flows (trace one end-to-end)
     9) External integrations
     10) Observability (logging/metrics/tracing)
     11) Deployment / release process
3. Investigate by reading docs, configs, and code. Prefer fast, high-signal entrypoints:
   - README / docs
   - package/lock files
   - build/test scripts
   - app entrypoints (server/main)
   - routes/controllers
   - data layer (models, migrations)
4. Update **ONLY** `scripts/kstrl/codebase_map.md`:
   - Append a new **Iteration Notes** section for this topic (template below)
   - If you used a Next Topics checklist, mark the topic as done (`[x]`)
   - Keep notes concise, factual, and verifiable

## Evidence rules (important)

- Every "fact" should include **evidence**:
  - File paths
  - What to look for (function/class name)
  - Preferably line ranges (if your tooling can provide them)
- If you are uncertain, label it clearly as a hypothesis and add an **Open question**.

## Iteration Notes format

Append this to the END of `scripts/kstrl/codebase_map.md`:

## [YYYY-MM-DD] - [Topic]

- **Summary**: 1-3 bullets on what you learned
- **Evidence**:
  - `path/to/file.ext` - what to look for (and line range if available)
- **Conventions / invariants**:
  - "Do X, don't do Y" rules implied by the codebase
- **Risks / hotspots**:
  - Areas likely to break or require extra care
- **Open questions / follow-ups**:
  - What's unclear, what needs human confirmation

---

## Stop condition

If there are **no remaining unchecked topics** in the Next Topics checklist
(or you have covered the default list above), reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""

DEFAULT_FEATURE_UNDERSTAND_PROMPT = """# kstrl Feature Understanding Instructions (Read-Only)

## Goal (one iteration)

You are running a **feature understanding** loop for a specific PRD.
Your job is to build a focused, evidence-based map of the code that this feature touches.

**Hard rule:** do NOT modify application code, tests, configs, dependencies, or CI.

**The only file you may edit is the feature understand file, for example:**
- `scripts/kstrl/feature/<feature_name>/understand.md`

If you think code changes are needed, write that as a note in the feature understand file
under **Open questions / Follow-ups**. Do not implement changes in this mode.

## What to do

1. Read the feature PRD file you were given.
2. Derive a short list of keywords from the PRD intent, not just exact wording.
3. Read `scripts/kstrl/codebase_map.md` and query only the sections relevant to this feature.
   - Always check **Quick Facts** and any relevant **Iteration Notes**.
   - Do not load the entire file.
4. Investigate by reading docs, configs, and code. Prefer fast, high-signal entrypoints:
   - README / docs
   - build/test scripts
   - app entrypoints (server/main)
   - routes/controllers
   - data layer (models, migrations)
5. Update **ONLY** the feature understand file:
   - Update **Quick Feature Facts** if you learned something durable
   - Append a new **Iteration Notes** section for this topic (template below)
   - If there is a **Story Coverage** checklist, mark items you verified

## Evidence rules (important)

- Every "fact" should include **evidence**:
  - File paths
  - What to look for (function/class name)
  - Preferably line ranges (if your tooling can provide them)
- If you are uncertain, label it clearly as a hypothesis and add an **Open question**.

## Iteration Notes format

Append this to the END of the feature understand file:

## [YYYY-MM-DD] - [Topic]

- **Summary**: 1-3 bullets on what you learned
- **Evidence**:
  - `path/to/file.ext` - what to look for (and line range if available)
- **Conventions / invariants**:
  - "Do X, don't do Y" rules implied by the codebase
- **Risks / hotspots**:
  - Areas likely to break or require extra care
- **Open questions / follow-ups**:
  - What's unclear, what needs human confirmation

---

## Stop condition

If there are **no remaining unchecked stories** in the **Story Coverage** checklist,
reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""


# Scaffolded kstrl.toml (R2.1): the project's discoverable config
# surface. Every key is commented out and shows its built-in default, so
# scaffolding changes no effective value; uncommenting a line is the
# explicit opt-in. Content mirrors kstrl.toml.example trimmed to keys the
# loaders actually read, plus the [timeout] section wired in R0.1.
DEFAULT_KSTRL_TOML = """\
# kstrl configuration (scaffolded by `ks init`).
# Every key is commented out and shows its built-in default: uncomment a
# line to override it. Precedence: CLI flag > environment variable > this
# file > built-in default. See docs/env-vars.md for the env-var mapping.

[agent]
# type = ""                        # "claude-code" | "claude-sdk" | "codex" (empty = auto-detect)
# command = ""                     # custom agent shell command; overrides type
# model = ""                       # e.g. "sonnet" for claude, "gpt-5.5" for codex (empty = agent default)
# reasoning_effort = ""            # low|medium|high|max

[run]
# max_iterations = 10
# sleep_seconds = 2
# interactive = false

[paths]
# prompt = "scripts/kstrl/prompt.md"
# prd = "scripts/kstrl/prd.json"
# Setting `progress` forces ONE path on every factory component; left
# unset, each component's engineer writes beside its own PRD, which is
# inside that component's allowedPaths.
# progress = "scripts/kstrl/progress.txt"
# codebase_map = "scripts/kstrl/codebase_map.md"
# allowed = []                     # e.g. ["scripts/kstrl/", "src/"]

[git]
# branch = ""                      # override branch (empty = use PRD branchName)
# auto_checkout = true

[ui]
# ascii = false

# Factory orchestrator settings (Phase 0-3 pipeline coordination).
[factory]
# max_parallel = 4                 # concurrent component workers
# max_retries = 3                  # per-component retry budget across all phases
# retry_delay = 5.0                # seconds between retry attempts
# use_worktrees = true             # branch each component into .kstrl/worktrees/<id>
# single_pr = false                # one PR for the whole factory vs per-component
# create_prs = true                # call `gh` to push + merge per component
# review_mode = "hard"             # hard | advisory | skip
# merge_timeout = 300.0            # seconds to wait for PR merge confirmation
# max_adversarial_calls = 0        # 0 = unbounded; caps review+security+distill LLM calls per run
# pause_before_pr_merge = false    # opt-in HITL checkpoint before each PR push+merge

# Phase 1 mechanical verification. These three are the one source of truth for
# how this project is checked: the gate runs them, and kstrl injects them into
# the engineer prompt, so the agent is never told a different command (#261).
# Leave a key empty for the harness default. Do NOT pin typecheck_command to a
# path such as "mypy ." if pyproject.toml scopes mypy itself; the empty default
# already defers to your [tool.mypy] files/packages.
# Chain toolchains to gate a polyglot repo, for example
# "uv run pytest -q && cd web && npm run test".
[verify]
# test_command = ""
# typecheck_command = ""
# lint_command = ""
# check_diff_scope = true
# check_bad_patterns = true
# dead_code_cleanup = false
# dead_code_command = ""           # custom dead-code detector (default: vulture)
# mutation_testing = false
# mutation_threshold = 50.0
# mutation_timeout = 600.0
# subprocess_timeout = 300.0
# require_self_critique = false    # fail Phase 1 if the ## Self-Critique block is missing/sparse
# self_critique_min_bullets = 3
# progress_file_path = ""          # empty = the log beside the component's PRD

# Phase 1 policy envelope (R8.1): declarative merge guardrails enforced on
# ARTIFACTS (git diff, uv.lock), never agent self-report. Opt-in; when
# enabled a violation blocks the merge, and editing enforcement machinery
# (this file, CI workflows, or the verifier code itself) is a
# non-overridable halt.
[policy]
# enabled = false
# paths_deny = [".github/workflows/**", "kstrl.toml", ".kstrl/**", "**/*.pem", "**/.env*"]
# max_files_changed = 40
# max_lines_changed = 1500         # lockfiles excluded from the count
# deps_allow_new = false           # block new uv.lock packages; L3+ may set true
# secret_patterns = ["AKIA[0-9A-Z]{16}", "-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"]
# enforcement_paths_extra = []     # ADDS to the halt set; can never shrink it
# license_allow = ["MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0"]
# license_deny_partial = ["GPL", "AGPL", "SSPL", "Commons-Clause"]
# license_unresolved = "block"     # block | advisory when no source resolves a license
# license_use_network = true       # false = uv cache only; part of the envelope hash
# deploy = false                   # reserved for the R8.7 release gate

# Exception inbox (R8.3): one surface for everything awaiting a human.
# On by default; triage with `ks inbox ls`.
[inbox]
# enabled = true
# open_item_cap = 50               # open items after which queue intake pauses
# snooze_hours = 24.0              # default snooze TTL
# notify_action_required = true    # notify only on action-required items

# Across-attempt divergence detector (#265): record (or, in block mode, fail
# on) a retry loop where the change got larger at every one of N consecutive
# failed reviews AND not one of the reviewer's blocking findings was retired.
# Advisory first; graduate to block once you have seen its output on real runs.
[divergence]
# mode = "advisory"                # skip | advisory | block
# growth_steps = 2                 # consecutive steps; needs N+1 measured attempts, must be >= 1

# Test-suite adequacy gate (R8.5) Layer 0: flags a diff that weakens the
# suite and new tests with no falsifiable assertion. Opt-in, advisory first.
[adequacy]
# enabled = false
# layer0 = "advisory"              # advisory | block
# require_strong_oracle = true
# flag_assertionless_tests = true

# Autonomy ladder (R8.2): one ordered level (L1-L4) instead of scattered
# autonomy flags. The level derives a flag bundle at run start and wins over
# contradicting flags. Opt-in: L1 is stricter than the defaults (merge gate on).
# `ks autonomy status` shows the level; promotion needs a human ack.
[autonomy]
# enabled = false
# max_level = 4                    # hard ceiling: never run above this level

# Phase 2.5 security review (independent adversarial pass focused on vulns).
[security]
# mode = "skip"                    # skip | advisory | hard (skip = default, opt in explicitly)
# fail_threshold = "high"          # critical | high | medium | low (hard mode only)
# timeout_seconds = 600.0
# agent_cmd = ""                   # leave blank to inherit from [agent]
# agent_type = ""
# model = ""

# Phase 3 cross-component contract testing.
[contract]
# mode = "tier"                    # tier | final | skip
# test_command = "uv run pytest"
# timeout = 600.0

# Phase 0 feedforward (computational structural scan; no LLM).
[feedforward]
# enabled = true
# module_map = true
# public_interfaces = true
# dependency_graph = true
# conventions = true
# max_context_tokens = 4000

# Per-component semantic knowledge layer: durable facts about WHAT WAS
# BUILT (interfaces, invariants, contracts, gotchas), written after the
# review passes and read by downstream components automatically.
[knowledge]
# enabled = true
# max_core_tokens = 2000           # current component's facts (full text)
# max_dependency_tokens = 1000     # dependency facts (full text)
# max_sibling_tokens = 500         # other components' facts (first sentence only)
# distill_timeout_seconds = 300
# distill_model = ""               # empty = falls back to [agent].model
# max_facts_per_distill = 7
# dependency_scope = "direct"      # direct | transitive

# Continuous-learning journal.
[evolution]
# enabled = true
# journal_path = ".kstrl/evolution.jsonl"
# experiments_path = ".kstrl/experiments.tsv"
# min_pattern_frequency = 2
# lookback_runs = 10

# Timeouts in seconds; 0 or less disables that limit.
[timeout]
# git_operation = 30.0
# agent_iteration = 1800.0         # per agent iteration
# component_total = 7200.0         # wall clock per component
# verification_check = 300.0
# review_agent = 600.0
# contract_test = 600.0
# subprocess_default = 60.0
# scheduler_backstop_margin = 60.0
"""

# What `ks init` does to a scaffolded file. The TUI wizard's preview
# renders these, so the vocabulary belongs beside the code that decides.
ScaffoldAction = Literal["create", "keep", "append"]


# ---------------------------------------------------------------------------
# #286 / H3b: telling an operator their scaffolded prompt is an older
# harness template than the one this kstrl ships. The policy and the
# argument for it live in docs/adversarial-roadmap.md H3b.
#
# The key is the file's own SHA-256, not a version stamped into it,
# because a scaffolded file is byte-identical to the constant it came
# from: `_create_if_missing` writes the body verbatim and loop.run_loop
# substitutes $prd_path and friends at READ time. So a digest lookup
# classifies files written long before this mechanism existed, separates
# a pristine older scaffold from an edited one exactly, and writes
# nothing into a file the operator owns.
#
# MAINTENANCE. Each tuple is (sha256, label) oldest first, CURRENT LAST.
# When a template body changes, APPEND its new row; never edit or drop
# an old one, because an old row is the only record that can recognise a
# file already on someone's disk. ``label`` is the identifier that body
# shipped under: the ``*_VERSION`` constant where the template has one,
# otherwise the date it first appeared. Enforced by
# tests/test_prompt_versions.py and tests/test_prompt_staleness.py.


@dataclass(frozen=True)
class ScaffoldedTemplate:
    """A file `ks init` writes from a harness constant, plus the digest
    of every body that constant has ever had."""

    filename: str
    constant_name: str
    body: str
    history: tuple[tuple[str, str], ...]

    @property
    def current_label(self) -> str:
        """The identifier of the body this kstrl ships."""
        return self.history[-1][1]


# Harvested from this repository's full history by hashing the constant
# at every commit that changed it (including the pre-rename `ralph`
# era, whose bodies could still be sitting in a migrated checkout).
SCAFFOLDED_TEMPLATES: tuple[ScaffoldedTemplate, ...] = (
    ScaffoldedTemplate(
        filename="prompt.md",
        constant_name="DEFAULT_PROMPT",
        body=DEFAULT_PROMPT,
        history=(
            # No DEFAULT_PROMPT_VERSION constant existed yet; labelled by
            # the date the body first shipped.
            (
                "15810563f3843b6634f6207d052710d72aa4fda0aa32ac86aa7718de86d34140",
                "pre-1.0.0 (2026-01-15)",
            ),
            (
                "9eb6d8f4c956d6fcacf2f39eed4a696e2755ce2ae0e24f53d08e98227fc37fc3",
                "pre-1.0.0 (2026-01-28)",
            ),
            (
                "5ec3e510a0dbd6ff41b181259b707f33a715715648cee0607ae5db6cf9992046",
                "pre-1.0.0 (2026-05-27)",
            ),
            ("a4a3a090139c370d7eecd12e3ef98055352110722750bb7b4cbf9bc50b1b9125", "1.0.0"),
            ("aa7fa6acb045dc6105d1a4c4ce8b687e1e04289c7b751eb0373b7c59dca3f7ae", "1.1.0"),
            ("4f7370f5f4efb2d9b89ce6ae09fcbf7e5c3c8fb3db22cdeb07a9221ccbc638dc", "1.1.1"),
            # Never merged: an in-review draft of #276 revised before it
            # landed. Recorded because a checkout of that branch could
            # have scaffolded from it.
            ("9bde9b20785f3740396906d1d199c2228c553c11ae956dc2f85d8aa2439fb49b", "1.2.0"),
            ("392eb698daf71d486a9d4573698df3bb2b3ca4be87c178657accc8a66c54f384", "1.3.0"),
        ),
    ),
    # The understand templates are H3-exempt (they produce documentation,
    # not adversarial-role output) so they carry no version constant, and
    # their labels are dates. They go stale the same way and the
    # mechanism is indifferent to which kind of label a row holds.
    ScaffoldedTemplate(
        filename="understand_prompt.md",
        constant_name="DEFAULT_UNDERSTAND_PROMPT",
        body=DEFAULT_UNDERSTAND_PROMPT,
        history=(
            ("5514376b0beeb484755d2d7d5effbe9a749b2d0972ddd30e7911e47bcf73e4ff", "2026-01-14"),
            ("1e700b55db8316392de146c549ef9fe9acf503af5c6ba2780f9d341728ac39c4", "2026-01-15"),
            ("fd02d9e3f2e559db5625c4db2d81ef0d24df481a4f4d4f5506fddd9b0962c53a", "2026-07-20"),
            ("cfd43bfeb80eaaf559ccb32d993fc2c5b2471ff90c7816648743135c2aa29688", "2026-07-21"),
        ),
    ),
    ScaffoldedTemplate(
        filename="feature_understand_prompt.md",
        constant_name="DEFAULT_FEATURE_UNDERSTAND_PROMPT",
        body=DEFAULT_FEATURE_UNDERSTAND_PROMPT,
        history=(
            ("5096447a6228e93d7d824ff5e1a334ef3eaf9edc9314a3fb7c6f7f04936cf06f", "2026-01-28"),
            ("e05fedd0ea1aff624966f4ee1e572c1af6f3926dd1b38b64678fdd6525a6f31a", "2026-07-20"),
            ("eb3637acf1918da23e27ad3f4d30bab32b1edd797b4bd1b5587b82b656affb09", "2026-07-21"),
        ),
    ),
)

# absent        - no file there; run_loop falls back to the constant and
#                 says so itself.
# current       - byte-identical to the body this kstrl ships.
# stale         - byte-identical to an OLDER body this kstrl once shipped.
# unrecognised  - matches nothing kstrl has ever shipped. Says nothing
#                 about who wrote it, which is the point: an edited file
#                 and a file from a build outside this history are
#                 indistinguishable, so neither is claimed to be stale.
TemplateStatus = Literal["absent", "current", "stale", "unrecognised"]


@dataclass(frozen=True)
class TemplateState:
    """What a scaffolded file on disk is, against the shipped history."""

    template: ScaffoldedTemplate
    path: Path
    status: TemplateStatus
    shipped_label: str | None = None


def _classify(template: ScaffoldedTemplate, path: Path) -> TemplateState:
    """Classify ``path`` as a copy of ``template``.

    An unreadable file is ``unrecognised``: we cannot prove it is a body
    we shipped, so we make no claim about it (the same reason
    ``_gitignore_state`` leaves an unreadable .gitignore alone).
    """
    if not path.exists():
        return TemplateState(template=template, path=path, status="absent")
    text = _read_text_or_none(path)
    if text is None:
        return TemplateState(template=template, path=path, status="unrecognised")
    labels = dict(template.history)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest not in labels:
        return TemplateState(template=template, path=path, status="unrecognised")
    return TemplateState(
        template=template,
        path=path,
        status="current" if digest == template.history[-1][0] else "stale",
        shipped_label=labels[digest],
    )


def classify_scaffolded_path(path: Path) -> TemplateState | None:
    """Classify ``path``, or None when kstrl does not scaffold that name.

    Matching on the basename rather than the full path is deliberate:
    ``--prompt`` and ``PROMPT_FILE`` can point anywhere, and a file the
    operator named something else is not a scaffold we can speak about.
    """
    template = next((t for t in SCAFFOLDED_TEMPLATES if t.filename == path.name), None)
    return None if template is None else _classify(template, path)


def classify_scaffold(root: Path) -> list[TemplateState]:
    """Classify every template `ks init` scaffolds under ``root``."""
    kstrl_dir = root / "scripts" / "kstrl"
    return [_classify(t, kstrl_dir / t.filename) for t in SCAFFOLDED_TEMPLATES]


def _rewrite_blockers(path: Path, root: Path | None = None) -> list[str]:
    """Every reason `ks init --upgrade-prompts` would not rewrite ``path``.

    The upgrade replaces bytes with ``os.replace``, which swaps the
    directory entry. Wherever that entry is shared, replacing it
    silently un-shares it: the project moves to the new body, the source
    every other project reads stays on the old one, and nobody is told.
    That is the outcome #286 exists to prevent, produced by the fix for
    it. Measured on this tree before the guard: a symlinked prompt.md
    became a regular file with the shared source untouched, and a
    hard-linked one had its inode replaced with the same result.

    Following the link instead of refusing is the other option, and is
    rejected on scope: `ks init <dir>` must not rewrite a file outside
    <dir>, and a shared prompt is by definition read by projects this
    invocation was not pointed at. Naming the target costs the operator
    one command and keeps the decision theirs.

    ``root``, when given, adds the check only a caller that knows the
    project can make: a symlinked ``scripts/kstrl`` directory puts the
    file outside <dir> with no link on the leaf at all. (`ks init` has
    always CREATED straight through such a link; this only refuses to
    REWRITE through it.) Reasons compose rather than shadowing each
    other, so an operator with two of them is told about both.
    """
    blockers: list[str] = []
    if path.is_symlink():
        blockers.append(f"a symlink to {path.resolve()}")
    else:
        try:
            links = path.stat().st_nlink
        except OSError:
            links = 1
        if links > 1:
            blockers.append(f"a hard link shared with {links - 1} other name(s)")
    if root is not None and root.resolve() not in path.resolve().parents:
        blockers.append(f"outside this project, at {path.resolve()}")
    if not path.parent.match("scripts/kstrl"):
        blockers.append("not the copy under scripts/kstrl/ that `ks init` upgrades")
    return blockers


def _upgrade_remedy(path: Path, root: Path | None = None) -> str:
    """The one sentence that says what to do about a stale ``path``.

    Shared by the run-time warning and by `ks init --upgrade-prompts`
    itself, so the advice an operator reads and the behaviour they then
    get cannot be worded into disagreement.
    """
    blockers = _rewrite_blockers(path, root)
    if not blockers:
        return "Run `ks init --upgrade-prompts` to take the current one."
    return (
        "`ks init --upgrade-prompts` will not touch this one: it is "
        + ", and ".join(blockers)
        + ". Update the file it shares, or replace this one by hand."
    )


class StalenessNotice(NamedTuple):
    """The two operator-facing lines about one stale scaffold."""

    headline: str
    advice: str


def staleness_notice(path: Path) -> StalenessNotice | None:
    """What to tell an operator about ``path``, or None to say nothing.

    None whenever there is nothing TRUE to say: the file is current, it
    is missing, it is not a template kstrl scaffolds, or it matches no
    body kstrl ever shipped (see ``unrecognised``).
    """
    state = classify_scaffolded_path(path)
    if state is None or state.status != "stale":
        return None
    # No count of how far behind: the ledger records bodies, not
    # releases (one row never reached main), so "N revisions behind" is
    # a number an operator cannot reconcile with the changelog. The two
    # labels are the actionable fact and both are exact.
    #
    # The remedy is only offered where it works, and _upgrade_remedy is
    # the single place that decides that, so this warning cannot promise
    # something `ks init --upgrade-prompts` then refuses to do.
    remedy = _upgrade_remedy(path)
    return StalenessNotice(
        headline=(
            f"{path} is the {state.template.filename} kstrl shipped at "
            f"{state.shipped_label}, unedited; this kstrl ships "
            f"{state.template.current_label}."
        ),
        advice=(
            "`ks init` never overwrites it, so every change to that "
            f"template since {state.shipped_label} has missed this "
            f"project. {remedy}"
        ),
    )


# First line of the .gitignore block `ks init` writes. Its presence is
# the whole idempotency test: an existing .gitignore is a user-owned
# file, so the block is appended once and never rewritten.
GITIGNORE_BLOCK_MARKER = "# kstrl: artifacts the scope guard would otherwise count"

# The marker plus the reason, so the file explains itself to whoever
# reads it next.
_GITIGNORE_BLOCK_HEADER = f"""{GITIGNORE_BLOCK_MARKER}
# The in-loop scope guard counts UNTRACKED files against a component's
# allowed paths, so anything your test / typecheck / lint commands write
# has to be ignored here or committed - otherwise the agent's own build
# artifacts read as out-of-scope edits and cost an iteration.
"""

# Ignored everywhere: kstrl's own runtime state, plus the one OS artifact
# that lands in a working tree without anybody asking for it.
#
# `.kstrl/` stays here after #274 carved the state directory out at the
# scope guard. The two answer different questions and neither subsumes
# the other: the guard only decides whether a file counts as a scope
# violation, while this line is what keeps `git add -A` from committing
# kstrl's run journals into the project's history and thence into a PR.
# Delete it and a --no-worktrees run starts shipping its own state.
_COMMON_IGNORES = (
    ".kstrl/",
    ".DS_Store",
)

_JS_IGNORES = (
    "node_modules/",
    "dist/",
    "build/",
    "coverage/",
    ".next/",
    "*.tsbuildinfo",
)

# npm, yarn and pnpm each write their own; whichever exists is the one
# this project uses.
_JS_LOCKFILES = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)

_JVM_IGNORES = (
    "target/",
    "build/",
    ".gradle/",
)

# Build output and caches per detected language, keyed like the other
# language tables in this module (_LANGUAGE_STANDARDS,
# _LANGUAGE_ANTIPATTERNS) on the strings _detect_project_context returns.
# Deliberately no lockfile and no .python-version: both pin a build, so
# both belong in version control. _LANGUAGE_LOCKFILES below puts the
# lockfile there instead of hiding it, and measured, none of `uv run`,
# `uv sync`, `uv lock` or `uv venv` writes a .python-version, so it
# cannot appear mid-iteration the way a lockfile can.
_LANGUAGE_IGNORES: dict[str, tuple[str, ...]] = {
    "Python": (
        "__pycache__/",
        "*.py[cod]",
        ".venv/",
        "venv/",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        ".coverage",
        "htmlcov/",
        "build/",
        "dist/",
        "*.egg-info/",
    ),
    "TypeScript": _JS_IGNORES,
    "JavaScript": _JS_IGNORES,
    "Rust": ("target/",),
    "Go": (
        "bin/",
        "*.test",
        "*.out",
    ),
    "Java": _JVM_IGNORES,
    "Kotlin": _JVM_IGNORES,
}

# Lockfiles per detected language: generated files that pin a build and
# therefore belong in version control, NOT in the ignore block above.
# Every key in _LANGUAGE_IGNORES appears here, an empty tuple meaning
# "this toolchain has no lockfile" as a STATED policy rather than an
# omission - tests/test_init_cmd.py fails if the two tables disagree, so
# a language cannot quietly get build-artifact ignores and no lockfile
# rule again (#201 review). The names are drawn from
# policy.LOCKFILE_BASENAMES, which the merge-policy size caps already
# key on, and the same test keeps this table inside that vocabulary.
#
# Measured, not assumed: with none present, `uv run pytest` writes
# uv.lock, `cargo test` writes Cargo.lock and `npm install` writes
# package-lock.json. Go writes go.sum only once the module requires
# something, and Gradle/Maven have no lockfile by default.
_LANGUAGE_LOCKFILES: dict[str, tuple[str, ...]] = {
    "Python": ("uv.lock", "poetry.lock", "Pipfile.lock"),
    "TypeScript": _JS_LOCKFILES,
    "JavaScript": _JS_LOCKFILES,
    "Rust": ("Cargo.lock",),
    "Go": ("go.sum",),
    "Java": (),
    "Kotlin": (),
}

# The "Next steps" block. The spec path leads because it is the one the
# README sells and the one the scaffold cannot suggest on its own: init
# writes an empty userStories array, which reads as "write these by
# hand" (#256). Not named *_PROMPT: that suffix enrols a constant in the
# adversarial prompt version snapshot, and this is UI copy.
NEXT_STEPS = """You have a spec (recommended):
  ks decompose --spec <spec.md> --project-name <name>  # plan it
  ks factory --spec <spec.md> --project-name <name>    # plan and build it

You want to drive one component by hand:
  1. Edit scripts/kstrl/prompt.md
  2. Add user stories to scripts/kstrl/prd.json
  3. ks run [iterations]                               # one component, no PR

Other modes:
  ks understand [iterations]
  ks feature [iterations] --prd scripts/kstrl/feature/<name>/prd.json

Measure before you spend (no agent, no cost):
  ks sense
  ks sense --allowed-path '<glob>'                     # preflight the guard
"""


def run_init(directory: Path, ui: UI, *, upgrade_prompts: bool = False) -> int:
    """Initialize kstrl harness in a project directory.

    Args:
        directory: Target project directory
        ui: UI for output
        upgrade_prompts: Rewrite any scaffolded prompt template that is a
            pristine older harness body (#286). Off by default, because
            `ks init` is otherwise non-destructive by design; this is the
            operator's explicit opt-in to that one overwrite.

    Returns:
        Exit code (0=success, 1=validation failure, 2=directory not found)
    """
    ui.title("kstrl Init")

    # Validate directory
    ui.section("Target")
    if not directory.exists():
        ui.err(f"Directory not found: {directory}")
        return 2

    root = directory.resolve()
    ui.kv("Directory", str(root))

    # Check for git repo
    is_repo = git.is_git_repo(root)
    if is_repo:
        ui.ok("Git repository detected")
    else:
        ui.warn("Not a git repository")

    ui.section("Scaffold")
    kstrl_dir = root / "scripts" / "kstrl"
    if not kstrl_dir.exists():
        kstrl_dir.mkdir(parents=True, exist_ok=True)
        ui.ok("Created scripts/kstrl/")
    else:
        ui.ok("scripts/kstrl/ exists")

    if upgrade_prompts:
        ui.section("Upgrade prompt templates")
        _upgrade_scaffolded_templates(root, ui)

    ui.section("Create defaults")
    _create_if_missing(root / "kstrl.toml", kstrl_toml_for(root), ui)
    _create_if_missing(kstrl_dir / "prompt.md", DEFAULT_PROMPT, ui)
    _create_if_missing(kstrl_dir / "prd.json", json.dumps(DEFAULT_PRD, indent=2) + "\n", ui)
    _create_if_missing(kstrl_dir / "progress.txt", DEFAULT_PROGRESS, ui)
    _create_if_missing(kstrl_dir / "codebase_map.md", DEFAULT_CODEBASE_MAP, ui)
    _create_if_missing(kstrl_dir / "understand_prompt.md", DEFAULT_UNDERSTAND_PROMPT, ui)
    _create_if_missing(
        kstrl_dir / "feature_understand_prompt.md",
        DEFAULT_FEATURE_UNDERSTAND_PROMPT,
        ui,
    )

    # Bootstrap CLAUDE.md and AGENTS.md
    ctx = _detect_project_context(root)
    bootstrap_claude_md(root, ui, ctx)

    # Keep the agent's own build artifacts out of the scope guard (#201)
    ui.section("Git hygiene")
    _ensure_gitignore(root, ctx["language"], ui)
    _ensure_lockfiles_tracked(root, ctx["language"], is_repo, ui)

    # Validate PRD
    ui.section("Validate PRD")
    prd_file = kstrl_dir / "prd.json"

    try:
        with open(prd_file, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        ui.err(f"Invalid JSON in prd.json: {e}")
        return 1

    errors = PRD.validate_schema(data)
    if errors:
        ui.err("PRD schema validation failed:")
        for error in errors:
            ui.info(f"  - {error}")
        return 1

    ui.ok("PRD schema valid")

    # PRD summary
    ui.section("PRD summary")
    prd = PRD.load(prd_file)
    ui.kv("Branch", prd.branch_name)
    ui.kv("Stories", str(len(prd.user_stories)))

    passing = sum(1 for s in prd.user_stories if s.passes)
    failing = len(prd.user_stories) - passing
    if prd.user_stories:
        ui.kv("Passing", str(passing))
        ui.kv("Failing", str(failing))

    # Next steps
    ui.section("Next steps")
    for line in NEXT_STEPS.splitlines():
        ui.info(line)

    return 0


_VERIFY_KEYS = ("test_command", "typecheck_command", "lint_command")

# (test, typecheck, lint) per detected language, "" where the toolchain
# has no such step. Python and an unrecognised tree are absent on
# purpose: the harness defaults are already right for Python, and a
# suggestion that merely restates them is the duplication #261 removed.
_LANGUAGE_VERIFY_COMMANDS: dict[str, tuple[str, str, str]] = {
    "Rust": ("cargo test", "cargo check", "cargo clippy -- -D warnings"),
    "Go": ("go test ./...", "go vet ./...", "golangci-lint run"),
    "TypeScript": ("npm test", "npx tsc --noEmit", "npx eslint ."),
    "JavaScript": ("npm test", "", "npx eslint ."),
}


def _verify_commands_for(root: Path, language: str) -> tuple[str, str, str] | None:
    if language in ("Java", "Kotlin"):
        # The only pair that needs the tree, not just the language.
        runner = "./gradlew test" if (root / "gradlew").exists() else "mvn test"
        return (runner, "", "")
    return _LANGUAGE_VERIFY_COMMANDS.get(language)


def kstrl_toml_for(root: Path) -> str:
    """``DEFAULT_KSTRL_TOML`` with ``[verify]`` seeded for this project.

    The harness gate defaults are Python-shaped, so on a Rust or Go
    project Phase 1 resolves to `uv run pytest` and fails every
    iteration. #261 removed the per-language guesses from the generated
    CLAUDE.md, where they were a second copy of a fact the gate owned.
    They belong here instead: kstrl.toml [verify] IS the source the gate
    and the engineer prompt both read, so seeding it records the
    detected toolchain in the one place that can act on it.

    Seeded COMMENTED, because `ks init` must not change an effective
    value (tests/test_config_control_plane.py pins that). Uncommenting
    one line is the operator's explicit opt-in.
    """
    commands = _verify_commands_for(root, _detect_project_context(root)["language"])
    if commands is None:
        return DEFAULT_KSTRL_TOML
    text = DEFAULT_KSTRL_TOML
    for key, command in zip(_VERIFY_KEYS, commands, strict=True):
        if command:
            text = text.replace(f'# {key} = ""\n', f'# {key} = "{command}"\n', 1)
    return text


def _create_if_missing(path: Path, content: str, ui: UI) -> None:
    """Create file if it doesn't exist.

    "already exists" was the whole report before #286, and it is the
    line an operator re-running `ks init` reads to decide whether their
    scaffold is in good shape. When the file is an OLDER harness
    template it now says so here too, because that is the moment the
    question is being asked.
    """
    if path.exists():
        ui.info(f"  {path.name} already exists")
        notice = staleness_notice(path)
        if notice is not None:
            ui.warn(f"  {notice.headline}")
            ui.info(f"  {notice.advice}")
    else:
        # utf-8 pinned to match _read_text_or_none: what init writes has
        # to read back byte-identical for the #286 digest to mean
        # anything, on any locale.
        path.write_text(content, encoding="utf-8")
        ui.ok(f"  Created {path.name}")


def _atomic_replace(target: Path, content: str) -> None:
    """Replace an EXISTING file's bytes atomically, keeping its mode.

    The write is ``atomicio.atomic_write_text``, which since #291 is the
    one owner of the mode and encoding rules this used to spell out for
    itself. ``atomicio`` is a stdlib-only leaf, so importing it here does
    NOT put ``kstrl.workqueue`` in ``kstrl.loop``'s static import
    closure via loop's deferred ``init_cmd`` import, which is what #274's
    ``tests/test_state_dir_scope.py`` refuses and what kept this copy
    hand-rolled until now.

    REPLACE, not create, is the part that stays here, because it is this
    caller's contract rather than the writer's, and a ``must_exist=``
    flag on the shared writer would be a special case carried by nine
    callers that do not want it. The one caller only ever rewrites a file
    the ledger already classified, so a missing target is a bug rather
    than a case to handle: the run stops instead of quietly doing a
    non-atomic create under a name that promises otherwise, which is what
    ``atomic_write_text`` on its own would do.
    """
    if not target.is_file():
        raise FileNotFoundError(
            f"_atomic_replace expects an existing file, got {target}: this "
            f"function only rewrites templates the scaffold ledger already "
            f"classified, so a missing target means the caller is wrong"
        )
    atomic_write_text(target, content)


def _upgrade_scaffolded_templates(root: Path, ui: UI) -> None:
    """Rewrite pristine older scaffolds with the body kstrl ships now.

    Safe to overwrite precisely because the digest proved the file is
    byte-identical to a template kstrl itself wrote: there is nothing of
    the operator's in it to lose. Anything the history does not
    recognise is left alone and reported, never merged or guessed at.

    "Nothing of the operator's to lose" is about the BYTES. The
    directory entry is a second thing they own, so a prompt they share
    by link, or one that resolves outside this project, is reported and
    left alone: see ``_rewrite_blockers``.

    The write is atomic (``_atomic_replace``) rather than a plain
    ``Path.write_text``: this is the one path in `ks init` that REPLACES
    bytes rather than creating a file, and an interruption mid-write
    would truncate the very prompt this feature exists to protect, into
    a body no ledger row recognises and so nothing will offer to fix.
    """
    for state in classify_scaffold(root):
        name = state.path.name
        if state.status == "stale":
            blockers = _rewrite_blockers(state.path, root)
            if blockers:
                ui.warn(f"  {name} is {blockers[0]}; left alone")
                ui.info(f"    {_upgrade_remedy(state.path, root)}")
                continue
            _atomic_replace(state.path, state.template.body)
            ui.ok(
                f"  {name}: {state.shipped_label} -> "
                f"{state.template.current_label} (it held no local edits)"
            )
        elif state.status == "current":
            ui.info(f"  {name} is already at {state.template.current_label}")
        elif state.status == "unrecognised":
            ui.warn(f"  {name} matches no template kstrl has shipped; left alone")
            ui.info(
                "    Either you edited it or it came from a build outside "
                "this history, and nothing here can tell those apart. "
                f"Diff it against {state.template.constant_name} in "
                "kstrl/init_cmd.py yourself."
            )
        else:  # absent: the scaffold pass below writes it
            ui.info(f"  {name} is missing; the scaffold below creates it")


def gitignore_block(language: str) -> str:
    """The .gitignore block `ks init` writes for a detected language.

    Public because examples/uv-python ships a copy of it, held in
    lockstep by tests/test_gen_docs.py the way the example's prompt.md
    is held against DEFAULT_PROMPT.
    """
    entries = (*_LANGUAGE_IGNORES.get(language, ()), *_COMMON_IGNORES)
    return _GITIGNORE_BLOCK_HEADER + "\n".join(entries) + "\n"


def _read_text_or_none(path: Path) -> str | None:
    """``path``'s text, or None when it cannot be read as text.

    Catches ValueError alongside OSError because UnicodeDecodeError is a
    ValueError: a .gitignore that is not UTF-8 crashed `ks init` and
    the TUI scaffold preview with a traceback (#201 review). A
    directory in the file's place raises IsADirectoryError, an OSError.

    UTF-8 is pinned rather than left to the locale because #286's
    classification is byte equality against a template `ks init` wrote:
    if the read and the write can disagree about the encoding, then on a
    non-UTF-8 locale a template carrying one non-ASCII character would
    scaffold and immediately classify as ``unrecognised``, and so would
    never be reported or upgraded. Every shipped body is ASCII today,
    which is exactly why this is cheap to pin now. It also makes the
    .gitignore read deterministic across machines; a file that is not
    UTF-8 still degrades to None and is still left alone, as before.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _gitignore_state(root: Path) -> tuple[ScaffoldAction, str | None]:
    """What init would do to ``root``/.gitignore, and the text it read.

    One decision with two consumers - the write below and the TUI
    wizard's preview - so the preview cannot describe a write that would
    not happen. "keep" covers both "the block is already there" and
    "the file cannot be read as text", because init leaves both alone;
    the second is the one where the text comes back None.
    """
    path = root / ".gitignore"
    if not path.exists():
        return "create", None
    existing = _read_text_or_none(path)
    if existing is None:
        return "keep", None
    if GITIGNORE_BLOCK_MARKER in existing:
        return "keep", existing
    return "append", existing


def gitignore_plan(root: Path) -> ScaffoldAction:
    """The scaffold preview's half of :func:`_gitignore_state`."""
    return _gitignore_state(root)[0]


def _ensure_gitignore(root: Path, language: str, ui: UI) -> None:
    """Create .gitignore, or append the kstrl block to an existing one.

    An existing .gitignore is a user-owned file: this only ever APPENDS,
    inside a marked block, and skips entirely once the marker is present,
    so re-running `ks init` cannot duplicate it or rewrite a line the
    user wrote. A file it cannot read is left alone for the same reason:
    without the marker check, appending could duplicate the block.

    The append goes through ``appendio``, which is why this function
    reads the file once for the marker and never again to work out its
    terminator; the comment on the call has the window that closes.
    """
    path = root / ".gitignore"
    action, existing = _gitignore_state(root)

    if action == "create":
        _create_if_missing(path, gitignore_block(language), ui)
        return

    if existing is None:
        ui.warn("  .gitignore could not be read as text; leaving it alone")
        ui.info("    Add the usual build-artifact rules yourself, or the scope")
        ui.info("    guard counts what your test and lint commands write.")
        return

    if action == "keep":
        ui.info("  .gitignore already has the kstrl block")
        return

    # One blank line between the user's last rule and ours, and no
    # leading blank when the file is empty. The TERMINATOR is not
    # decided here any more: ``append_records`` probes the handle it is
    # about to write through and pads an unterminated tail itself, so
    # what is left here is the blank line, which is a Markdown-style
    # separation choice and not a repair.
    #
    # That closes a read-then-write window rather than tidying a
    # branch. ``_gitignore_state`` reads the file, this appends to it,
    # and the two reads used to disagree whenever the file changed in
    # between: measured, a file reported empty by the read and holding
    # an unterminated rule by the time of the write got the kstrl block
    # header appended onto the user's last rule, on one line.
    #
    # utf-8 is pinned inside the helper and matches
    # ``_read_text_or_none``, which is what reads this file back to
    # decide whether the block is already there. The block is ASCII, so
    # this changes no byte today; what it removes is a write and a read
    # of the same file that could disagree (#320).
    separator = "" if not existing else "\n"
    append_records(path, separator + gitignore_block(language), repair="")
    ui.ok("  Appended the kstrl block to .gitignore")


def _ensure_lockfiles_tracked(root: Path, language: str, is_repo: bool, ui: UI) -> None:
    """Stage the project's untracked lockfiles, and say so.

    A lockfile is NOT ignored. For an application it belongs in version
    control, so ignoring it (what examples/uv-python/.gitignore did
    before #201) hides it from the scope guard by hiding it from git,
    which is a workaround rather than a lockfile policy. Tracked is the
    real fix: ``git ls-files --others --exclude-standard`` cannot list a
    file that is in the index.

    Staging is as far as init goes - creating a commit in someone's
    repository is not a scaffolder's call - so the printed instruction
    carries the rest. It matters: a factory worktree is cut from a
    COMMIT, so an uncommitted lockfile is absent there and the first
    verify run writes a fresh untracked one.
    """
    candidates = _LANGUAGE_LOCKFILES.get(language, ())
    if not is_repo or not candidates:
        return

    present = [name for name in candidates if (root / name).exists()]
    if not present:
        ui.warn(f"  No lockfile yet ({', '.join(candidates)})")
        ui.info("    Your package manager writes one; create it and commit it")
        ui.info("    before your first run, or the verify commands write it")
        ui.info("    mid-iteration and it reads as an out-of-scope edit.")
        return

    for name in present:
        _track_lockfile(root, name, ui)


def _track_lockfile(root: Path, name: str, ui: UI) -> None:
    """Report or fix one lockfile's tracking state."""
    if git.is_file_tracked(name, root):
        ui.ok(f"  {name} is tracked")
        return

    error = git.stage_file(name, root)
    if error is not None:
        # `git add` refuses an ignored path and its message does not say
        # WHICH rule ignored it, so the remediation has to name one.
        ignored_by = git.ignore_source(name, root)
        if ignored_by:
            ui.warn(f"  {name} is ignored by {ignored_by}, so git will not track it")
            ui.info(f"    Delete that rule and `git add {name}`: the lockfile pins")
            ui.info("    your build, so it belongs in version control.")
        else:
            ui.warn(f"  Could not stage {name}: {error}")
        return

    ui.ok(f"  Staged {name} (staged only, no commit was created)")
    ui.info("    Commit it before your first run: a factory worktree is cut from")
    ui.info("    a commit, so an uncommitted lockfile is not in it.")


# ---------------------------------------------------------------------------
# CLAUDE.md and AGENTS.md bootstrap
# ---------------------------------------------------------------------------


def _detect_project_context(root: Path) -> dict[str, str]:
    """Detect project name, language and framework from config files.

    Inspects the project root for pyproject.toml, Cargo.toml,
    package.json, go.mod, etc. First match wins.

    #261: this deliberately does NOT guess test / typecheck / lint
    commands. ``verify.resolve_verify_commands`` is the only place that
    answers that question.
    """
    ctx: dict[str, str] = {
        "name": root.name,
        "language": "unknown",
        "framework": "",
    }

    # Python
    pyproject = root / "pyproject.toml"
    setup_py = root / "setup.py"
    if pyproject.exists() or setup_py.exists():
        ctx["language"] = "Python"
        pyproject_text = _read_text_or_none(pyproject) or ""
        match = re.search(r'name\s*=\s*"([^"]+)"', pyproject_text)
        if match:
            ctx["name"] = match.group(1)
        if "fastapi" in pyproject_text:
            ctx["framework"] = "FastAPI"
        elif "django" in pyproject_text:
            ctx["framework"] = "Django"
        elif "flask" in pyproject_text:
            ctx["framework"] = "Flask"
        return ctx

    # Rust
    cargo_toml = root / "Cargo.toml"
    if cargo_toml.exists():
        ctx["language"] = "Rust"
        cargo_text = _read_text_or_none(cargo_toml) or ""
        match = re.search(r'name\s*=\s*"([^"]+)"', cargo_text)
        if match:
            ctx["name"] = match.group(1)
        if "actix" in cargo_text or "axum" in cargo_text:
            ctx["framework"] = "Axum/Actix"
        elif "rocket" in cargo_text:
            ctx["framework"] = "Rocket"
        return ctx

    # TypeScript / JavaScript
    pkg_json = root / "package.json"
    if pkg_json.exists():
        ctx["language"] = "TypeScript"
        try:
            pkg = json.loads(_read_text_or_none(pkg_json) or "{}")
            ctx["name"] = pkg.get("name", root.name)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                ctx["framework"] = "Next.js"
            elif "react" in deps:
                ctx["framework"] = "React"
            elif "express" in deps:
                ctx["framework"] = "Express"
            elif "vue" in deps:
                ctx["framework"] = "Vue"
            if "typescript" not in deps and not (root / "tsconfig.json").exists():
                ctx["language"] = "JavaScript"
        except (OSError, ValueError):
            pass
        return ctx

    # Go
    go_mod = root / "go.mod"
    if go_mod.exists():
        ctx["language"] = "Go"
        go_text = (_read_text_or_none(go_mod) or "").strip()
        first_line = go_text.splitlines()[0] if go_text else ""
        if first_line.startswith("module "):
            ctx["name"] = first_line.split()[-1].split("/")[-1]
        return ctx

    # Java / Kotlin
    if (
        (root / "pom.xml").exists()
        or (root / "build.gradle").exists()
        or (root / "build.gradle.kts").exists()
    ):
        ctx["language"] = "Kotlin" if (root / "build.gradle.kts").exists() else "Java"
        return ctx

    return ctx


_LANGUAGE_STANDARDS: dict[str, str] = {
    "Python": """
- Use type hints on ALL function signatures
- Use `from __future__ import annotations` in every file
- Use `T | None` not `Optional[T]`, `A | B` not `Union[A, B]`
- Prefer `@dataclass` for data models, `frozen=True` when immutable
- Use `Protocol` for interfaces (structural subtyping over inheritance)
- Google-style docstrings with Args/Returns/Raises sections
- snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE for constants
- Absolute imports only, grouped: stdlib, third-party, local
- No star imports, no circular imports
- No bare `except:` clauses - always specify the exception type
- No mutable default arguments (use `field(default_factory=...)`)
""",
    "Rust": """
- Use `Result<T, E>` for fallible operations, not panics
- Prefer `&str` over `String` in function parameters
- Use `derive` macros: Debug, Clone, PartialEq where appropriate
- Handle all match arms exhaustively - no catch-all `_` unless justified
- Prefer iterators and combinators over manual loops
- Use `clippy::pedantic` lint level
- Document public APIs with `///` doc comments
- Use `thiserror` for library errors, `anyhow` for application errors
- Minimize `unwrap()` - use `?` or explicit error handling
- Prefer `impl Trait` over `dyn Trait` when the concrete type is known
""",
    "TypeScript": """
- Enable strict mode in tsconfig.json
- Use explicit return types on all exported functions
- Prefer `interface` over `type` for object shapes
- Use `readonly` for properties that should not be mutated
- Prefer `unknown` over `any` - narrow with type guards
- Use discriminated unions for variant types
- Handle all Promise rejections - no unhandled promises
- Use `const` by default, `let` only when mutation is needed, never `var`
- Prefer named exports over default exports
- Use template literals over string concatenation
""",
    "Go": """
- Handle every error - never use `_` for error returns
- Use table-driven tests
- Keep interfaces small (1-3 methods)
- Accept interfaces, return structs
- Use `context.Context` as the first parameter for cancellable operations
- Prefer composition over embedding
- Use `errors.Is` and `errors.As` for error checking, not string matching
- Document all exported identifiers
- Use `go vet` and `golangci-lint` in CI
- Prefer channels for synchronization, mutexes for state protection
""",
    "Java": """
- Use final for variables that should not be reassigned
- Prefer composition over inheritance
- Use Optional<T> instead of null for return types
- Document public APIs with Javadoc
- Use try-with-resources for AutoCloseable resources
- Prefer immutable collections where possible
- Use meaningful exception types, not generic RuntimeException
""",
    "Kotlin": """
- Prefer val over var (immutability by default)
- Use data classes for plain data holders
- Use sealed classes for restricted hierarchies
- Prefer expression bodies for simple functions
- Use coroutines for async operations, not callbacks
- Leverage null safety - avoid `!!` operator
- Use `when` expressions exhaustively
""",
}

_LANGUAGE_ANTIPATTERNS: dict[str, str] = {
    "Python": """
- Do NOT use `typing.Optional` or `typing.Union` - use `|` syntax
- Do NOT use `Any` without a TODO comment explaining why
- Do NOT use mutable default arguments (`def f(x=[])`)
- Do NOT use bare `except:` or `except Exception:` without re-raising
- Do NOT use `import *`
- Do NOT use `type: ignore` without a specific mypy error code
- Do NOT use `global` or `nonlocal` unless absolutely necessary
- Do NOT suppress linter warnings without justification
""",
    "Rust": """
- Do NOT use `unwrap()` or `expect()` in library code
- Do NOT use `unsafe` without a SAFETY comment explaining the invariant
- Do NOT use `clone()` to avoid borrow checker issues - redesign instead
- Do NOT use `Box<dyn Any>` as an escape hatch from the type system
- Do NOT ignore compiler warnings - treat them as errors
- Do NOT use `String` in struct fields when `&str` with a lifetime would work
""",
    "TypeScript": """
- Do NOT use `any` - use `unknown` and narrow with type guards
- Do NOT use `!` non-null assertion operator without justification
- Do NOT use `var` - use `const` or `let`
- Do NOT use `==` - always use `===`
- Do NOT ignore TypeScript errors with `@ts-ignore` without a specific reason
- Do NOT use `Function` or `Object` types - use specific signatures
""",
    "Go": """
- Do NOT use `panic` for error handling in library code
- Do NOT ignore errors with `_`
- Do NOT use `init()` functions unless absolutely necessary
- Do NOT use global mutable state
- Do NOT use `interface{}` / `any` as an escape hatch from the type system
""",
}


# What the generated CLAUDE.md says about verification, and why it names
# no commands. CLAUDE.md is prepended verbatim into the engineer prompt
# (loop.build_project_context), so anything written here is an
# instruction the agent follows; deriving the right commands would still
# be a second copy, and two copies drift. See the #261 note in verify.py.
_VERIFICATION_SECTION = """
## Verification

kstrl resolves this project's test, typecheck and lint commands at run
time and injects them into the engineer prompt, so they are deliberately
not restated here and cannot drift out of step with the gate that
enforces them.

Set them in `kstrl.toml` under `[verify]` (`test_command`,
`typecheck_command`, `lint_command`). An unset key falls back to the
harness default for the project. One command may chain several
toolchains, which is how a polyglot repo is gated:

```toml
[verify]
test_command = "uv run pytest -q && cd web && npm run test"
```
"""


def _generate_claude_md(ctx: dict[str, str]) -> str:
    """Generate CLAUDE.md content from detected project context."""
    lang = ctx["language"]
    framework_line = f" ({ctx['framework']})" if ctx["framework"] else ""

    sections = [f"# CLAUDE.md - {ctx['name']}", ""]

    # Project overview
    sections.append("## Project Overview")
    sections.append(f"- **Language**: {lang}{framework_line}")
    sections.append(f"- **Project**: {ctx['name']}")
    sections.append("")

    sections.append(_VERIFICATION_SECTION.strip())
    sections.append("")

    # Coding standards
    standards = _LANGUAGE_STANDARDS.get(lang, "")
    if standards:
        sections.append("## Coding Standards")
        sections.append(standards.strip())
        sections.append("")

    # Implementation principles (language-agnostic, elite-level)
    sections.append("""## Implementation Principles

### First Principles Thinking
- Reason from first principles about WHY the code should work, not just HOW
- Consider nth-order effects: what happens downstream when this function's contract changes?
- Ask "what invariant does this maintain?" for every data structure and state transition
- Before implementing, understand the problem domain - do not cargo-cult patterns from other contexts

### No Shortcuts
- Do not implement stub functions that return hardcoded values
- Do not add TODO comments as a substitute for implementation
- Do not use placeholder/dummy values in production code paths
- Do not catch exceptions just to silence them
- Do not skip validation because "it should never happen"
- Every code path must be intentional and justified

### No Handwaving
- Every function must have a concrete, complete implementation
- Error handling must cover ALL failure modes, not just the happy path
- Edge cases (empty inputs, None values, boundary conditions, concurrent access) must be handled explicitly
- Do not assume "this will never happen" - if the type system allows it, handle it
- Performance implications must be considered, not deferred

### Correctness Over Cleverness
- Prefer readable, straightforward implementations over clever one-liners
- Add assertions for preconditions that the type system cannot enforce
- Use immutable data structures by default
- Never silently swallow errors or return default values for unexpected inputs
- Make illegal states unrepresentable through the type system

### Testing Discipline
- Every public function needs at least one test
- Test the contract (inputs/outputs), not the implementation details
- Include edge cases: empty inputs, single elements, maximum values, None/null, unicode, negative numbers
- Error paths are tested as thoroughly as success paths
- Do not write tests that always pass (tautological assertions like `assert True`)
- Tests must be deterministic - no flaky tests, no time-dependent assertions

### Completeness
- Implement ALL specified behavior, not a subset
- Handle ALL variants of enums and match/switch expressions
- Implement ALL methods of an interface/protocol/trait, not just the common ones
- Do not leave partial implementations - either fully implement or explicitly raise/panic with a reason
- Documentation matches behavior - if docs say it does X, it must do X""")
    sections.append("")

    # Anti-patterns
    antipatterns = _LANGUAGE_ANTIPATTERNS.get(lang, "")
    if antipatterns:
        sections.append("## What NOT To Do")
        sections.append(antipatterns.strip())
        sections.append("")

    # Agent learnings section (agents append patterns, gotchas, conventions here)
    sections.append("""## Agent Learnings

> This section is maintained by AI agents working on this codebase.
> Agents: append patterns, gotchas, and conventions you discover below.
> This is the single source of truth - AGENTS.md is a symlink to this file.

### Codebase Patterns
<!-- Agents: add reusable patterns you discover here -->

### Gotchas
<!-- Agents: add surprises and non-obvious behaviors here -->

### Conventions
<!-- Agents: add established conventions here -->""")
    sections.append("")

    return "\n".join(sections) + "\n"


def bootstrap_claude_md(root: Path, ui: UI, ctx: dict[str, str]) -> None:
    """Generate CLAUDE.md and symlink AGENTS.md to it.

    ``ctx`` is :func:`_detect_project_context`'s reading of the project's
    language, framework and tooling; the caller detects once because the
    .gitignore block is chosen from the same reading.

    AGENTS.md is a symlink to CLAUDE.md so both names point to the same
    file. When the prompt tells agents to "update AGENTS.md", they are
    writing to CLAUDE.md.
    """

    ui.section("Agent context files")

    ui.kv("Detected language", ctx["language"])
    if ctx["framework"]:
        ui.kv("Detected framework", ctx["framework"])

    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        ui.info("  CLAUDE.md already exists")
    else:
        claude_md.write_text(_generate_claude_md(ctx))
        ui.ok("  Created CLAUDE.md")

    agents_md = root / "AGENTS.md"
    if agents_md.is_symlink() and os.readlink(str(agents_md)) == "CLAUDE.md":
        ui.info("  AGENTS.md already symlinked to CLAUDE.md")
    elif agents_md.exists():
        ui.info("  AGENTS.md already exists (not a symlink)")
    else:
        # Create relative symlink: AGENTS.md -> CLAUDE.md
        agents_md.symlink_to("CLAUDE.md")
        ui.ok("  Created AGENTS.md -> CLAUDE.md (symlink)")
