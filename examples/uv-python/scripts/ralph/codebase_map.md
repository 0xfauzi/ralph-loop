# Codebase Map (Brownfield Notes)

This file is meant to be built over time using the Ralph **codebase understanding** loop.

## How to use this map

- **Evidence-first**: prefer citations to specific files/entrypoints over broad claims.
- **Read-only mode**: in understanding mode, the agent should ONLY edit this file.
- **Small increments**: one topic per iteration keeps notes high-signal.

## Next Topics (checklist)

Edit this list to match your repo. During the understanding loop, mark items as done.

- [x] How to run locally (setup, env vars, start commands)
- [x] Build / test / lint / CI gates (what runs in CI and how)
- [x] Repo topology & module boundaries (where code lives, layering rules)
- [x] Entrypoints (server, worker, cron, CLI)
- [x] Configuration, env vars, secrets, feature flags
- [x] Authn/Authz (where permissions are enforced)
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

## 2026-01-17 - How to run locally

- **Summary**: Use `uv sync` for local setup from the project directory. Run tests with `uv run pytest`. Run the example CLI with `uv run ralph-uv-example Alice`.
- **Evidence**:
  - `README.md` - Requirements and local setup with `uv sync`, tests, and example CLI commands (lines 5-28).
- **Conventions / invariants**:
  - Local workflows are `uv`-driven and assume Python 3.11+ (README.md lines 5-16).
- **Risks / hotspots**:
  - Running Ralph with branch checkout could affect the top-level repo, and the README recommends `RALPH_BRANCH=""` to disable it (README.md lines 47-48).
- **Open questions / follow-ups**:
  - No long-running server or app start command is documented - confirm whether the CLI is the primary local entrypoint or if a service exists elsewhere.

## 2026-01-17 - Build / test / lint / CI gates

- **Summary**:
  - Tests are run with `uv run pytest`, and pytest is configured to look in `tests/`.
  - Linting uses Ruff with a defined rule set and line length in `pyproject.toml`.
  - Build backend is Hatchling, and the wheel packaging target includes `src/ralph_uv_example`.
- **Evidence**:
  - `README.md` - Test command uses `uv run pytest` (lines 18-22).
  - `pyproject.toml` - Dev dependencies include `pytest` and `ruff` (lines 14-18).
  - `pyproject.toml` - Pytest `testpaths` points to `tests` (lines 27-28).
  - `pyproject.toml` - Ruff config and lint selects (lines 30-36).
  - `pyproject.toml` - Hatchling build backend and wheel target packages (lines 20-25).
- **Conventions / invariants**:
  - Use `uv` to run test and lint tooling rather than invoking tools directly.
  - Ruff linting uses the selected rulesets `E`, `F`, `I`, `UP`, `B` with `line-length = 100`.
- **Risks / hotspots**:
  - Build and lint behavior depends on `pyproject.toml` settings, so changes there affect local and CI behavior.
- **Open questions / follow-ups**:
  - CI gates are not documented in the README or other visible configs here - confirm which CI system runs tests and linting, and the exact commands.

## 2026-01-17 - Repo topology & module boundaries

- **Summary**:
  - The repo uses a `src/` layout with a single Python package `ralph_uv_example`, plus `tests/` and `scripts/` at the top level.
  - The public API is defined in `ralph_uv_example/__init__.py`, while CLI behavior lives in `ralph_uv_example/cli.py`.
- **Evidence**:
  - `pyproject.toml` - wheel packages include `src/ralph_uv_example` (lines 24-25).
  - `src/ralph_uv_example/__init__.py` - `__all__` and `greet` define the package API (lines 3-10).
  - `src/ralph_uv_example/cli.py` - CLI module imports `greet` from the package (lines 5-17).
  - `tests/test_greet.py` - tests import `greet` from `ralph_uv_example` (lines 3-11).
  - `scripts/ralph/codebase_map.md` - Ralph harness notes live under `scripts/ralph` (this file).
- **Conventions / invariants**:
  - Package code lives under `src/ralph_uv_example`, and tests import from the installed package namespace.
  - CLI behavior is implemented in a separate module (`cli.py`) that depends on the package API.
- **Risks / hotspots**:
  - Changes to `ralph_uv_example.__init__` affect both the CLI and tests that rely on `greet`.
- **Open questions / follow-ups**:
  - None for this topic.

## 2026-01-17 - Entrypoints (server, worker, cron, CLI)

- **Summary**:
  - The only defined entrypoint is a console script named `ralph-uv-example` that invokes `ralph_uv_example.cli:main`.
  - The CLI module supports direct execution via a `__main__` guard and prints a greeting before exiting.
- **Evidence**:
  - `pyproject.toml` - `[project.scripts] ralph-uv-example = "ralph_uv_example.cli:main"` (lines 11-12).
  - `src/ralph_uv_example/cli.py` - `main` implementation and `if __name__ == "__main__"` guard (lines 14-21).
  - `README.md` - example CLI invocation `uv run ralph-uv-example Alice` (lines 24-28).
- **Conventions / invariants**:
  - CLI entrypoint is wired through `pyproject.toml` and expects to resolve `ralph_uv_example.cli:main`.
  - CLI accepts an optional `name` argument via argparse and prints a greeting (see `build_parser` / `main`).
- **Risks / hotspots**:
  - Renaming `ralph_uv_example.cli:main` or the script name would break the published CLI entrypoint.
- **Open questions / follow-ups**:
  - No server, worker, or cron entrypoints are visible here - confirm whether this example is CLI-only.

## 2026-01-17 - Configuration, env vars, secrets, feature flags

- **Summary**:
  - Configuration is primarily via environment variables for running the Ralph agent loop and adjusting UI and Codex output behavior.
  - The example package code does not read env vars or secrets - the CLI only parses a positional name argument and calls `greet`.
- **Evidence**:
  - `README.md` - env vars for Ralph run control, UI knobs, and Codex output knobs (lines 35-87).
  - `src/ralph_uv_example/cli.py` - argparse-only CLI with `name` argument and no env var access (lines 8-17).
  - `src/ralph_uv_example/__init__.py` - pure `greet` implementation with no configuration hooks (lines 6-10).
- **Conventions / invariants**:
  - Ralph runtime behavior is configured by setting environment variables on the `uv run python -m ralph_py run ...` commands.
- **Risks / hotspots**:
  - Omitting `RALPH_BRANCH=""` can trigger branch checkout behavior in the surrounding repo when running the agent loop (README.md lines 47-48).
- **Open questions / follow-ups**:
  - Are there additional Ralph or project-level env vars beyond the README examples that should be documented here?

## 2026-01-18 - Authn/Authz (where permissions are enforced)

- **Summary**:
  - No application-level authentication or authorization exists in this example project (local-only CLI).
  - The README does not describe any credentials or auth flow for running the Ralph loop from this directory.
- **Evidence**:
  - `src/ralph_uv_example/cli.py` - `build_parser` / `main` only parse args and print output; no identity or permission checks (lines 8-17).
  - `src/ralph_uv_example/__init__.py` - `greet` is pure string logic with no identity/permissions (lines 6-10).
  - `README.md` - Ralph run examples set run-control and UI env vars, but do not mention credentials or auth (lines 30-88).
  - `pyproject.toml` - depends on `ralph-py` (lines 7-9, 38-39).
- **Conventions / invariants**:
  - Running `ralph-uv-example` assumes local execution under the current OS user, with no app-level auth boundary.
- **Risks / hotspots**:
  - Any future networked entrypoints would need new authn/authz design and enforcement (no scaffolding exists today).
- **Open questions / follow-ups**:
  - Where are credentials for any model/provider access configured when running `python -m ralph_py ...` from this example?
