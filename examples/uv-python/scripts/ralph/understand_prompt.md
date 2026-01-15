# Ralph Codebase Understanding Instructions (Read-Only)

## Goal (one iteration)

You are running a **codebase understanding** loop. Your job is to explore the existing codebase and write an evidence-based "map" for humans.

**Hard rule:** do NOT modify application code, tests, configs, dependencies, or CI.

**The only file you may edit is:**
- `scripts/ralph/codebase_map.md`

If you think code changes are needed, write that as a note in the map under **Open questions / Follow-ups**. Do not implement changes in this mode.

## What to do

1. Read `scripts/ralph/codebase_map.md`.
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
4. Update **ONLY** `scripts/ralph/codebase_map.md`:
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

Append this to the END of `scripts/ralph/codebase_map.md`:

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

If there are **no remaining unchecked topics** in the Next Topics checklist (or you have covered the default list above), reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
