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
- [ ] Core domain flow #1 (trace end-to-end)
- [ ] Core domain flow #2 (trace end-to-end)
- [ ] External integrations (third-party APIs, webhooks, queues)
- [ ] Observability (logging, metrics, tracing, error reporting)
- [ ] Deployment / release process

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
