# Codebase Map (Brownfield Notes)

This file is meant to be built over time using the Ralph **codebase understanding** loop.

## How to use this map

- **Evidence-first**: prefer citations to specific files/entrypoints over broad claims.
- **Read-only mode**: in understanding mode, the agent should ONLY edit this file.
- **Small increments**: one topic per iteration keeps notes high-signal.

-## Next Topics (checklist)

Edit this list to match your repo. During the understanding loop, mark items as done.

- [x] How to run locally (setup, env vars, start commands)
- [x] Build / test / lint / CI gates (what runs in CI and how)
- [x] Repo topology & module boundaries (where code lives, layering rules)
- [x] Entrypoints (server, worker, cron, CLI)
- [x] Configuration, env vars, secrets, feature flags
- [x] Authn/Authz (where permissions are enforced)
- [x] Data model & persistence (migrations, ORM patterns, transactions)
- [x] Core domain flow #1 (trace end-to-end)
- [x] Core domain flow #2 (trace end-to-end)
- [x] External integrations (third-party APIs, webhooks, queues)
- [x] Observability (logging, metrics, tracing, error reporting)
- [x] Deployment / release process

## Quick Facts (keep updated)

- **Language / framework**: Python 3.11+ managed by `uv` (README.md:5-18)
- **How to run**: `uv run ralph-uv-example Alice` and other CLI commands go through `uv run` (README.md:13-29)
- **How to test**: `uv run pytest` (README.md:19-24)
- **How to typecheck/lint**: Ruff is configured via `[tool.ruff]` and `[tool.ruff.lint]` in `pyproject.toml` and exposed as a dev dependency (pyproject.toml:12-34)
- **Primary entrypoints**: CLI entrypoint `ralph_uv_example.cli:main` plus the Ralph harness scripts under `scripts/ralph/` (pyproject.toml:9-10; README.md:31-47)
- **Data store**:

## Known “Do Not Touch” Areas (optional)

- (add directories/files that are fragile or off-limits)

---

## Iteration Notes

(New notes append below; keep older notes for history.)

## 2025-02-14 - How to run locally

- **Summary**:
  - Local setup uses `uv sync`, and common commands run via `uv run` for tests and the example CLI.
  - The Ralph harness is run directly via `./scripts/ralph/ralph.sh` with optional environment flags to control UI and output; `RALPH_BRANCH=""` disables branch checkout.
- **Evidence**:
  - `README.md`: setup and run commands (`uv sync`, `uv run pytest`, `uv run ralph-uv-example Alice`) and Ralph harness examples with env vars, lines 5-78.
  - `pyproject.toml`: CLI entrypoint `ralph-uv-example = "ralph_uv_example.cli:main"`, lines 9-10.
- **Conventions / invariants**:
  - Use `uv` as the default workflow for installing and running commands in this example (`uv sync`, `uv run ...`).
  - When experimenting with Ralph in this repo, set `RALPH_BRANCH=""` to avoid branch checkout affecting the main repo.
- **Risks / hotspots**:
  - Running Ralph without `RALPH_BRANCH=""` can attempt a branch checkout in the top-level repo.
- **Open questions / follow-ups**:
  - Is there a long-running service or only the CLI entrypoint for local execution?

## 2026-01-07 - Repo topology & module boundaries

- **Summary**:
  - The runnable surface is a single `ralph_uv_example` package under `src` that exposes `greet` plus a CLI entrypoint `ralph_uv_example.cli:main` wired through `pyproject.toml`.
  - CLI logic lives in `src/ralph_uv_example/cli.py` and imports directly from the package root, indicating a tight coupling between CLI and package code without additional layers.
  - Supporting artifacts (tests and Ralph harness scripts) live outside `src` and reference the package via standard Python imports, showing a clean separation between production code and tooling.
- **Evidence**:
  - `pyproject.toml:1-16` — `[project]` defines `ralph_uv_example` as the package, and `[project.scripts]` installs `ralph-uv-example = "ralph_uv_example.cli:main"`.
  - `src/ralph_uv_example/cli.py:1-15` — `build_parser()` and `main()` print `greet` and exit, demonstrating the CLI’s dependency on the package’s public API.
  - `scripts/ralph` directory and `README.md:31-47` — Ralph harness scripts live under `scripts/ralph/` (e.g., `ralph.sh`, `ui.sh`), and documentation distinguishes the harness from package code.
  - `tests/test_greet.py:1-11` — Tests import `ralph_uv_example.greet`, confirming that the package API is the only tested surface.
- **Conventions / invariants**:
  - Keep production logic within `src/ralph_uv_example`; everything else (tests, harness scripts) interacts with it via the package interface.
  - CLI behavior should rely on `greet` for output; adding supplemental modules would likely happen under `src/ralph_uv_example/` to maintain a single package boundary.
- **Risks / hotspots**:
  - The repo currently exposes only one package and CLI; expanding functionality may require deliberately introducing new modules or packages in `src/`, which should be well-structured to avoid entangled CLI logic.
  - Ralph harness scripts live in the top-level `scripts/ralph/` folder, so changes there should avoid assuming deeper package structure to keep tooling decoupled.
- **Open questions / follow-ups**:
  - Are there plans for additional packages or modules beyond `ralph_uv_example`, and if so, should they be nested inside `src` or split into separate namespace packages?
  - Should the Ralph harness ever import from `src/ralph_uv_example`, or must it stay entirely external to preserve tooling isolation?
## 2026-01-07 - Build / test / lint / CI gates

- **Summary**:
  - Tests execute through `uv run pytest`, and the dev dependency group in `pyproject.toml` ensures `pytest` and `ruff` are available for the project.
  - Ruff linting is configured via `[tool.ruff]`/`[tool.ruff.lint]`, providing lint presets (line length 100, target Python 3.11, selected rule sets) that `uv run ruff` would consume.
  - The only automated checks mentioned in docs are the local test and lint flows; no CI workflow files are present in the repo, so automation expectations are unclear.
- **Evidence**:
  - `README.md`: test command `uv run pytest` documented right after setup instructions, lines 19-24.
  - `pyproject.toml`: dev dependency group includes `pytest>=8.0.0` and `ruff>=0.6.0`, and `[tool.ruff]`/`[tool.ruff.lint]` provide lint configuration (line 12-34).
  - `tests/test_greet.py`: example tests that verify `greet` return values for both provided names and defaulting to world, lines 3-11.
- **Conventions / invariants**:
  - Keep verification inside `uv run ...` so the uv-managed environment resolves Python and tools consistently.
  - Suite currently relies on the simple greet unit tests; expand coverage before assuming behavior beyond this helper.
- **Risks / hotspots**:
  - Without documented CI, there is a risk that changes depend on local commands not mirrored in automation, potentially leaving regressions unnoticed.
  - A single `tests/test_greet.py` file means domain logic beyond greeting is unprotected until more tests are added.
- **Open questions / follow-ups**:
  - What CI gates, if any, should run for this repo (tests only, lint, additional checks)?
  - Should lint/test commands be documented in `README.md` or added to automation to keep the pipeline in sync?

## 2026-01-07 - Entrypoints

- **Summary**:
  - `uv run ralph-uv-example <name>` exercises the CLI entrypoint `ralph_uv_example.cli:main` (wired through `pyproject.toml`) which parses an optional name, prints `greet(name)`, and returns a status code of 0, so the package is exposed through one simple CLI.
  - The Ralph harness is invoked via `scripts/ralph/ralph.sh` (with the UI helpers under `scripts/ralph/ui.sh`), as described in `README.md`, and feeds prompts/prd data to an AI loop controlled by environment knobs.
- **Evidence**:
  - `README.md:13-47` — documents running `uv run ralph-uv-example Alice` and the Ralph agent loop via `scripts/ralph/ralph.sh` along with UI/env knobs.
  - `pyproject.toml:9-10` — `[project.scripts]` registers `ralph-uv-example = "ralph_uv_example.cli:main"`, making the CLI available through `uv run`/`uv install`.
  - `src/ralph_uv_example/cli.py:1-19` — `build_parser()` and `main()` parse the optional `name`, call `greet`, print the greeting, and exit cleanly, showing the CLI flow.
  - `scripts/ralph/ralph.sh:1-120` — top section documents the agent loop usage, env vars (AGENT_CMD, MODEL, etc.), and roles it plays as the non-Python entrypoint for Ralph experimentation.
- **Conventions / invariants**:
  - Keep the CLI logic inside `ralph_uv_example.cli` and expose it via `pyproject.toml` scripts so `uv run` reliably locates it; additional entrypoints should follow the same wiring through `[project.scripts]`.
  - Treat `scripts/ralph/ralph.sh` as the single Ralph harness entrypoint, and configure it via env vars rather than modifying the script when running dry runs or fake agents.
- **Risks / hotspots**:
  - The CLI is the only Python entrypoint in this repo, so any new service or worker would need to introduce new scripts or subcommands; mixing responsibilities into `cli.py` risks tangling UI parsing with business logic.
  - The Ralph harness relies on cherrypicked env vars and branch switching; forgetting `RALPH_BRANCH=""` can mutate the outer repo, and customizing scope requires paying attention to `ALLOWED_PATHS`.
- **Open questions / follow-ups**:
  - Are other entrypoints (e.g., long-running servers or background workers) expected in this example, or is the CLI+harness surface intentionally minimal?
  - Should there be documentation tying CLI arguments/env knobs to specific Ralph scenarios, or are the present README notes sufficient?

## 2026-01-07 - Configuration, env vars, secrets, feature flags

- **Summary**:
  - The README documents the Ralph harness knobs: branch isolation via `RALPH_BRANCH`, UI switches (`RALPH_UI`, `RALPH_ASCII`, `NO_COLOR`), and Codex tuning variables (`MODEL`, `MODEL_REASONING_EFFORT`, streaming output controls) that are applied by prefixing values when invoking `./scripts/ralph/ralph.sh`.
  - `scripts/ralph/ralph.sh` enumerates every environment variable it reacts to (AGENT_CMD, MODEL, MODEL_REASONING_EFFORT, SLEEP_SECONDS, INTERACTIVE, PROMPT_FILE, ALLOWED_PATHS, RALPH_BRANCH, etc.), defaulting where sensible and validating required files before running; this script is the central configuration surface for the Ralph loop.
  - Prompt/PRD locations and UI helpers are configurable via `PROMPT_FILE`, `PRD_FILE`, and the `scripts/ralph/ui.sh` helper, while the UI flags described in README simply flip environment variables before calling the shell script.
- **Evidence**:
  - `README.md:33-84` — documents how to override branch checkout (`RALPH_BRANCH=""`), force plain/UI colors, and tune Codex output behavior (`MODEL`, `MODEL_REASONING_EFFORT`, `RALPH_AI_SHOW_PROMPT`, `RALPH_AI_RAW`, `RALPH_AI_PROMPT_PROGRESS_EVERY`).
  - `scripts/ralph/ralph.sh:16-158` — lists the supported environment variables, sets defaults (`MAX_ITERATIONS`, `SLEEP_SECONDS`, etc.), and explains guards such as `ALLOWED_PATHS` and `INTERACTIVE`.
  - `scripts/ralph/ralph.sh:53-70` — shows `PROMPT_FILE`/`PRD_FILE` defaulting to files under `scripts/ralph/`, so they can be replaced with custom inputs when needed.
- **Conventions / invariants**:
  - Configure the Ralph harness strictly through environment variables rather than editing scripts; the README examples standardize this pattern (e.g., UI knobs and AGENT_CMD overrides).
  - Keep prompt/PRD files inside `scripts/ralph/` unless a use case explicitly needs to supply alternatives via `PROMPT_FILE`/`PRD_FILE`.
- **Risks / hotspots**:
  - Forgetting to set `RALPH_BRANCH=""` will trigger branch checkout behavior that affects the parent repo, so experimental runs must explicitly disable it.
  - Custom `ALLOWED_PATHS` must match repo-relative paths precisely (and may require directory prefixes ending with `/`), otherwise the script prevents patches and may exit unexpectedly.
- **Open questions / follow-ups**:
  - Does any automated process (CI or documented workflow) expect to set these environment variables differently, or should the README be the single source for “how to tune Ralph”?
  - Should secrets/configuration that Codex uses (e.g., API keys) be centrally documented or injected through `AGENT_CMD`, and if so where would that policy live?

## 2026-01-07 - Authn/Authz

- **Summary**:
  - There are no authentication or authorization checks anywhere in the Python package or CLI; the CLI only parses a name and prints a greeting.
  - Ralph harness scripts and prompts likewise operate purely through environment knobs and file inputs, with no identity gating mentioned.
- **Evidence**:
  - `src/ralph_uv_example/cli.py:1-17` — CLI builds an argparse parser for an optional name, prints `greet`, and exits; no auth hooks or permission checks appear.
  - `tests/test_greet.py:1-11` — both tests call `greet` directly, confirming the only surfaced functionality is the public greet helper.
  - `scripts/ralph/ralph.sh:16-158` — the harness lists control variables (AGENT_CMD, MODEL, ALLOWED_PATHS, etc.) but none reference credentials, sessions, or permissions.
- **Conventions / invariants**:
  - Treat the CLI as a public-facing helper with no access controls; any future sensitive behavior would need to add explicit auth layers under `src/ralph_uv_example`.
  - Ralph scripts are configured via env variables and disabled branch checkouts; they assume trust in the runner and do not enforce user identity.
- **Risks / hotspots**:
  - Because no auth is enforced, any new feature that touches secrets, data, or external services must deliberately add gating to avoid exposing credentials via the CLI or harness.
  - The absence of auth-related abstractions means adding permissions later could require refactoring the current CLI/tests to accept identity/context objects.
- **Open questions / follow-ups**:
  - Should future work introduce authentication or authorization, and if so, should it be implemented inside `ralph_uv_example` or via surrounding scripts/CI?
  - Are there any hidden expectations (e.g., environment-scoped secrets) that rely on external tooling, in which case documenting that assumption would prevent misuse?

## 2026-01-07 - Data model & persistence

- **Summary**:
  - There is no database or persistence layer; `ralph_uv_example.greet` is the only behavioral code and simply formats the provided name into a greeting without storing state (`src/ralph_uv_example/__init__.py:1-9`).
  - The package manifest lists zero runtime dependencies, and the dev group includes only `pytest` and `ruff`, so no ORM, driver, or migration tooling is pulled into the project (`pyproject.toml:1-24`).
  - Tests exercise only the pure `greet` helper, which reinforces that no data models or persistent state exist in the current surface (`tests/test_greet.py:1-11`).
- **Evidence**:
  - `src/ralph_uv_example/__init__.py:1-9` — `greet` strips, defaults, and returns `Hello, …` with no imports or persistence-related branches.
  - `pyproject.toml:1-24` — `[project]` has an empty `dependencies` list, `[dependency-groups.dev]` includes only `pytest>=8.0.0` and `ruff>=0.6.0`, so no DB/ORM packages are declared.
  - `tests/test_greet.py:1-11` — the suite calls `greet` with/without a name and asserts its return value, showing that only in-memory helpers are covered.
- **Conventions / invariants**:
  - Keep behavior inside `ralph_uv_example` as pure helpers; any data persistence must be added deliberately (with new modules and dependencies) and documented so the simple CLI surface remains predictable.
  - Assume the CLI is stateless until a data layer is explicitly introduced; existing tests and docs expect no migrations or connection state.
- **Risks / hotspots**:
  - Introducing persistence later would require new dependencies and careful architectural planning since the repo currently has no foundation for storing data.
  - Without new tests/documentation, any added data model would be unprotected by the existing suite, so coverage would need to expand before shipping.
- **Open questions / follow-ups**:
  - If data storage becomes necessary, should it live inside `src/ralph_uv_example` (e.g., a dedicated `storage` module) or remain external to keep the CLI minimal?
  - Are there any downstream expectations (from Ralph runs or user workflows) that data should survive between invocations, or is the stateless greeting definitive for this example?

## 2026-01-07 - Core domain flow #1

- **Summary**:
  - `uv run ralph-uv-example <name>` surfaces the CLI defined in `pyproject.toml`, so user input enters `ralph_uv_example.cli:main` immediately after `uv` spins up.
  - The CLI parser accepts an optional `name`, defaults to `world`, prints the `greet(args.name)` result, and exits with `0`, keeping the command safe to script.
  - `ralph_uv_example.greet` trims whitespace, substitutes `world` when no name is provided, and formats `Hello, …!`, with unit tests exercising both code paths.
- **Evidence**:
  - `README.md:13-29` — documents `uv run ralph-uv-example Alice`, describing the visible command that triggers the flow.
  - `pyproject.toml:9-10` — `[project.scripts]` binds `ralph-uv-example` to `ralph_uv_example.cli:main`, so `uv run` dispatches through setuptools-style entrypoints.
  - `src/ralph_uv_example/cli.py:8-21` — `build_parser()` defines the optional `name`, `main()` prints `greet(args.name)` and returns `0`, and the `__main__` guard raises `SystemExit(main())`.
  - `src/ralph_uv_example/__init__.py:3-10` — `greet` strips, defaults to `"world"`, and returns the greeting string without external effects.
  - `tests/test_greet.py:3-11` — coverage ensures the helper returns the correct message for both a provided name and blank input, anchoring the observable output for the flow.
- **Conventions / invariants**:
  - Keep CLI parsing and printing thin, delegating all normalization/formatting to `greet` so future logic lives in a single helper.
  - Maintain the `SystemExit(main())` pattern so the CLI always communicates success/failure through exit codes, which `uv run` relies on for scripting.
- **Risks / hotspots**:
  - Adding more features in `cli.py` risks entwining parsing with domain logic; new behavior should be introduced via additional helpers under `ralph_uv_example`.
  - A single-output path means any new side effects (I/O, dependencies) should be explicitly tested, as the current suite only guards the greeting helper.
- **Open questions / follow-ups**:
  - What should “Core domain flow #2” capture next (for example, the Ralph harness loop or future domain behaviors)?

## 2026-01-07 - Core domain flow #2

- **Summary**:
  - The Ralph harness in `scripts/ralph/ralph.sh` drives an agentic loop: it loads `scripts/ralph/prompt.md`, runs either `AGENT_CMD` or the default `codex` CLI per iteration, and exits successfully only when the transcript contains `<promise>COMPLETE</promise>`.
  - Startup in the script (and README instructions) highlight branch guards, allowed-path checks, and UI/agent knobs controlled via environment variables so configuration stays declarative rather than hard-coded.
  - Each iteration enforces path restrictions (if `ALLOWED_PATHS` is set), optionally pauses (`INTERACTIVE`), sleeps between rounds (`SLEEP_SECONDS`), and repeats until either completion or the `max_iterations` argument is exhausted.
- **Evidence**:
  - `README.md:33-84` — documents invoking `scripts/ralph/ralph.sh` with dry-run/fake-agent examples and the UI/agent knobs (`RALPH_BRANCH`, `RALPH_UI`, `MODEL_REASONING_EFFORT`, etc.).
  - `scripts/ralph/ralph.sh:1-190` — describes loop usage, env vars, exit codes, and prompt/prd defaults at top of the script.
  - `scripts/ralph/ralph.sh:220-360` — shows enforcement helpers (`enforce_allowed_paths_if_configured`, `auto_checkout_branch_from_prd`) and startup messages that tie env overrides to branch/guard behavior.
  - `scripts/ralph/ralph.sh:380-640` — main loop logic, including `AGENT_CMD` handling, Codex default, completion detection (`<promise>COMPLETE</promise>`), interactive review, and sleep/retry behavior.
- **Conventions / invariants**:
  - Completion is only acknowledged via the `<promise>COMPLETE</promise>` marker in the agent transcript; the loop keeps running (or fails) until that string appears in the captured output (scripts/ralph/ralph.sh:400-510).
  - Customize pilot behavior exclusively through the provided env vars (`AGENT_CMD`, `MODEL`, `PROP_FILE`, `ALLOWED_PATHS`, etc.); the script sources `scripts/ralph/ui.sh` when present and defaults to plain text helpers otherwise, so configuration remains external (scripts/ralph/ralph.sh:40-210; README.md:33-84).
  - Guard rails (branch checkout via `RALPH_BRANCH`/`prd.json`, path assertions, interactive prompts) run automatically on start-up so iterations always honor repo boundaries before touching files (scripts/ralph/ralph.sh:220-360).
- **Risks / hotspots**:
  - Running without `RALPH_BRANCH=""` or without reviewing the PRD branch risks mutating the outer repo (`auto_checkout_branch_from_prd` and README warning at `README.md:33-40`).
  - `ALLOWED_PATHS` enforcement rejects any stray files; interactive mode is the only way to revert those hits, so forgetting to reallow necessary paths can leave changes blocked until manually adjusted (scripts/ralph/ralph.sh:230-310).
  - The completion guard relies solely on `<promise>COMPLETE</promise>` in the regex-checked final message, so agents that never emit that marker or stream different formats will cause the script to hit max iterations and exit 1 (scripts/ralph/ralph.sh:430-520).
- **Open questions / follow-ups**:
  - The current `scripts/ralph/prd.json` has an empty `userStories` array—what story(s) should this flow trace in practice, and how should we seed new entries when experimenting with Ralph?
  - Should documentation/state elsewhere explain how to choose between `AGENT_CMD` dry runs versus the default Codex loop, or which `MODEL`/`MODEL_REASONING_EFFORT` values are supported in this demo?

## 2026-01-07 - External integrations

- **Summary**:
  - The Ralph harness defaults to OpenAI’s Codex CLI (`codex`) and feeds it `scripts/ralph/prompt.md`, so the agent loop depends on an external binary and its network-backed models for completion signals.
  - `AGENT_CMD` lets any stdin-accepting CLI (e.g., `claude`, `cat`, a custom script) replace Codex while keeping the same `<promise>COMPLETE</promise>` contract.
  - UI rendering can slot in the third-party `gum` utility via `scripts/ralph/ui.sh`, and the README already suggests installing it for nicer output alongside the default plain renderer.
- **Evidence**:
  - `scripts/ralph/ralph.sh:364-497` — startup summary reports whether `AGENT_CMD` or `codex` runs, then either pipes `PROMPT_FILE` into the custom command or into `codex` with optional `MODEL`/`MODEL_REASONING_EFFORT` flags before checking `<promise>COMPLETE</promise>`.
  - `README.md:31-84` — documents dry-run/fake-agent examples that set `AGENT_CMD`, describes Codex-specific knobs such as `MODEL_REASONING_EFFORT`, and references the prompt/prd artifacts the agent reads.
  - `scripts/ralph/ui.sh:1-72` — optional `gum` integration; environment variables like `RALPH_UI`, `GUM_FORCE`, `NO_COLOR`, `RALPH_ASCII` steer whether the script uses the installed `gum` CLI or falls back to plain styling.
  - `scripts/ralph/prompt.md:1-34` — instructs the agent to load `prd.json`/`progress.txt` and treat them as the canonical story/priorities that whatever external agent runs must inspect.
- **Conventions / invariants**:
  - Keep the external agent contract centered on the prompt file + `<promise>COMPLETE</promise>` signal so custom tools can drop into the same loop (no extra APIs or hooks needed).
  - Configure agent choice/behavior via env vars (`AGENT_CMD`, `MODEL`, `MODEL_REASONING_EFFORT`, UI knobs) rather than editing the scripts.
  - Prefer declarative UI adjustments (gum vs plain) through `scripts/ralph/ui.sh` so the agent loop’s output remains consistent whether or not the optional dependency is present.
- **Risks / hotspots**:
  - If the required `codex` binary is missing (or the network call fails), the script exits 1—external tooling failures immediately halt the loop, so ensuring `codex`/custom agent availability is critical.
  - Arbitrary `AGENT_CMD` strings run through `bash -lc` with the repo prompt piped in; misconfigurations could execute unintended commands, so these env vars must be managed carefully.
  - UI styling depends on `gum` only when it is chosen and a TTY is available; log capture or CI runs might silently fall back to plain mode, so reviewers should not rely on stylized output unless `gum` is stubbed in.
- **Open questions / follow-ups**:
  - Are there preferred agent binaries beyond Codex that contributors expect to plug in, and should we document install steps for them?
  - Should `scripts/ralph/prd.json` or `prompt.md` mention which third-party credentials (e.g., Codex tokens) must be present, or leave those details to higher-level deployment docs?

## 2026-01-07 - Observability

- **Summary**:
  - The Python CLI simply prints the greeting from `greet` and does not import or configure any logging, metrics, or tracing helpers (`src/ralph_uv_example/cli.py:8-21`).
  - The Ralph harness scripts use shell UI helpers (`ui_*`) that echo to stdout/stderr, with no support for structured logging, telemetry, or metric exports (`scripts/ralph/ralph.sh:62-106`).
- **Evidence**:
  - `src/ralph_uv_example/cli.py:8-21` — parser prints `greet(args.name)` and returns exit code `0`, with no logging imports or instrumentation.
  - `scripts/ralph/ralph.sh:62-106` — UI helper functions (`ui_title`, `ui_info`, etc.) simply wrap `echo` statements, showing the script relies on console output rather than a logging framework.
- **Conventions / invariants**:
  - Keep Python behavior minimal and print-only for the CLI; adding observability would require importing logging/metrics in `src/ralph_uv_example`.
  - Treat the Ralph shell loop as a plain console runner; any future telemetry would need to wrap or replace the existing `ui_*` helpers.
- **Risks / hotspots**:
  - Without logs, metrics, or tracing, diagnosing issues in longer Ralph loops or expanded CLI logic depends entirely on stdout/stderr, which makes debugging multi-iteration failures harder.
  - The absence of structured observability leaves no easy way to monitor agent progress, so automation that cares about completion times would need custom hooks.
- **Open questions / follow-ups**:
  - If observability becomes important, should we introduce Python logging and metrics in `src/ralph_uv_example` or keep it outside the package and extend the Ralph shell script?
  - Would a shell-side telemetry wrapper (e.g., exporting metrics via `echo` in a parseable format) be acceptable, or is a richer logging/monitoring solution expected before adding complexity?

## 2026-01-08 - Deployment / release process

- **Summary**:
  - There are no documented deployment or release procedures; the README only describes local setup, testing, and the Ralph harness (no release, CI, or packaging instructions).
  - No automation files (e.g., workflow YAMLs or release scripts) exist in the repo tree, so pushing changes relies entirely on manual `uv` commands and Ralph script runs.
- **Evidence**:
  - `README.md:5-47` — sections cover requirements, `uv sync`, `uv run pytest`, the CLI, and Ralph harness dry-run/fake-agent examples, but nothing about packaging, releases, or deployments.
  - (Implicit) the repo contains only top-level scripts, `pyproject.toml`, and `tests/`; there are no `.github/workflows` or release tooling directories present to describe a deployment pipeline.
- **Conventions / invariants**:
  - Follow the documented `uv` workflow for local experimentation; no release conventions are established, so any deployment work would need new documentation before execution.
- **Risks / hotspots**:
  - Without an agreed release process or automation checks in the repo, it's easy to ship changes without verification beyond the local `uv run` commands, increasing the chance of regression in larger demos.
  - Lack of docs means contributors might invent conflicting release steps; the repository currently has nothing to guide versioning, tagging, or publishing.
- **Open questions / follow-ups**:
  - Should we define a release/deployment checklist (even if simple) in this repo, or is it acceptable for this demo to remain manual?
  - Does the larger Ralph/uv ecosystem expect certain release artifacts or workflows (e.g., tagging, UV package publication) that we should document here?
