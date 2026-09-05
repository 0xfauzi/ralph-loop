"""CLI entry point for kstrl."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from kstrl.evolution import EvolutionConfig, EvolutionJournal
    from kstrl.interaction import InteractionChannel

    # Annotation only, and the cheapest available: with
    # `from __future__ import annotations` above, _sense_document's
    # signature is a string at run time and this block never executes.
    # Not a deferral - line 104 already imports kstrl.verify at module
    # scope, so it is on `ks --help`'s import path either way (measured
    # here at 5.2ms of cli.py's 84.7ms cumulative).
    from kstrl.verify import VerificationResult

from dataclasses import replace

import click
from click.core import ParameterSource

from kstrl import __version__
from kstrl.agents import (
    AGENT_TYPE_ALIASES,
    ClaudeCodeAgent,
    ClaudeSdkAgent,
    CodexAgent,
    get_agent,
)
from kstrl.agents.base import (
    ARCHITECT_COMPONENT,
    Agent,
    UsageTotals,
    collect_usage,
    usage_cursor,
)
from kstrl.agents.liveness import CLAUDE_FAMILY, PROBE_ENV_VAR, probe_family
from kstrl.agents.logging import LoggingAgent
from kstrl.breaker import BreakerConfig
from kstrl.commandrun import CommandRun, open_command_run
from kstrl.config import (
    ConfigError,
    KstrlConfig,
    _parse_paths,
    reconcile_progress_config,
    resolve_config_file,
)
from kstrl.config_report import build_config_report
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.decompose import SpecBlockerError, decompose_spec
from kstrl.events import (
    ArtifactWritten,
    ComponentCompleted,
    ComponentFailed,
    ComponentStarted,
    PhaseCompleted,
    PhaseStarted,
    RunCompleted,
    RunPlan,
    RunStarted,
)
from kstrl.factory import (
    BudgetConfigError,
    FactoryConfig,
    _cli_family,
    run_factory,
    validate_cost_ceiling,
    validate_token_ceiling,
)
from kstrl.feature_cmd import FeatureParams, run_feature
from kstrl.git import detect_base_branch, resolve_base_branch
from kstrl.init_cmd import DEFAULT_FEATURE_UNDERSTAND, run_init, staleness_notice
from kstrl.interaction import (
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)
from kstrl.launch import assemble_factory_configs
from kstrl.loop import run_loop
from kstrl.manifest import COMPONENT_STATUS_VALUES, Manifest
from kstrl.observability import (
    event_age_seconds,
    format_age,
    latest_run_id,
    read_progress_events,
)
from kstrl.output import build_console
from kstrl.prd import PRD
from kstrl.proposals import append_to_agent_learnings as _append_to_agent_learnings
from kstrl.proposals import existing_proposal_titles as _existing_proposal_titles
from kstrl.proposals import mark_applied, parse_proposal_file
from kstrl.reducer import ComponentState, RunState, fold, load_run_state, upconvert_v1
from kstrl.retry_plan import RetryError, prepare_retry
from kstrl.sandbox import SandboxConfig
from kstrl.shutdown import StopController, install_signal_handlers
from kstrl.timeout import TimeoutConfig
from kstrl.ui.base import UI
from kstrl.verify import DEFAULT_LINT_COMMAND, DEFAULT_TEST_COMMAND


def _load_manifest_or_exit(path: Path, ui: UI) -> Manifest:
    """Load a manifest, or print why not and exit 1.

    A `Manifest.load` reachable from the CLI must never traceback at the
    operator: the input is a file a human or another tool wrote, so a bad
    one is an expected outcome. Shared so a new call site cannot forget
    the guard, which is how `ks inbox retry` came to lack it (#263).
    """
    try:
        return Manifest.load(path)
    except (OSError, ValueError) as exc:
        ui.err(f"Failed to load manifest {path}: {exc}")
        sys.exit(1)


def _format_component_status(status: str | None) -> str:
    """Render a component status for the plan, flagging an off-enum one.

    A status the enum does not know can never be scheduled, so echoing it
    plain reads as confirmation that the manifest is correct - the plan
    line for a component that will silently never run looks exactly like
    the plan line for one that will (#263). ``None`` means the execution
    order named an id the manifest has no component for.
    """
    if status is None:
        return "?"
    if status in COMPONENT_STATUS_VALUES:
        return status
    return f"{status} (not a valid status)"


def _console_ui(
    mode: str = "auto",
    no_color: bool = False,
    ascii_only: bool = False,
    force_rich: bool = False,
) -> UI:
    """Event-native drop-in for get_ui() (TUI rewrite chunk 7).

    Same signature and mode resolution; returns the console's
    EventBridgeUI so every line the command narrates becomes a typed
    Log event, rendered synchronously and byte-identically onto the
    same concrete UI get_ui() would have picked. run_factory discovers
    the bus via ``ui.bus`` to attach the run's file sinks.
    """
    return build_console(
        mode,
        no_color=no_color,
        ascii_only=ascii_only,
        force_rich=force_rich,
    ).ui


def _use_cli_value(ctx: click.Context, name: str) -> bool:
    return ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE


def _reject_blank_project_name(
    ctx: click.Context,
    param: click.Parameter,
    value: str | None,
) -> str | None:
    """Refuse an empty or whitespace-only project name at the boundary (#338).

    The project name is an identity: it keys the journal audits, the
    decision register and, under ``--single-pr``, the branch. "" was
    accepted by Click and is the one value at which the convergence
    accounting counted an audit with no project as BOTH this project's
    and unattributed, reporting five audits as eight. Refusing it here
    means no downstream reader has to carry a special case for it, and
    the refusal lands before the architect is invoked, measured at 119
    to 210 seconds against a frontier model on a real spec.

    Rejects or returns the value VERBATIM; it never strips. " x " is a
    strange name but it is the name the operator typed, and quietly
    substituting "x" would write a manifest, a branch and a journal
    audit under something else.

    Not on ``ks queue add``: its ``--project-name`` defaults to "" as
    the sentinel ``serve`` derives ``queue-<id>`` from, and Click runs
    a callback over an option's own default. A queued item with a
    whitespace name still reaches this check, because ``serve`` spawns
    a child ``ks factory --project-name``, which refuses it with exit 2.
    """
    if value is not None and not value.strip():
        raise click.BadParameter("must not be empty or whitespace-only", ctx=ctx, param=param)
    return value


# Accepted spellings for the agent type across the config surface.
# kstrl.toml documents "claude" | "codex" | "custom"; the --agent-type
# flags and KSTRL_AGENT_TYPE historically use "claude-code" | "codex" |
# "auto". Both families resolve to get_agent's vocabulary here.
# One vocabulary, defined next to the adapters it selects (R8 review):
# the CLI and get_agent previously kept separate tables and disagreed
# about "claude".
_AGENT_TYPE_ALIASES = AGENT_TYPE_ALIASES


def _agent_preflight(
    agent_cmd: str | None,
    agent_type: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Check that the agent the resolved config selects is reachable.

    Mirrors the factory/decompose preflight (R2.4, H-12): the check must
    look for whichever agent the config actually selects, not
    hardcode codex. Returns ``(canonical_agent_type, error, hint)``:
    ``canonical_agent_type`` is the get_agent-vocabulary spelling to
    construct the agent with (so a toml ``type = "claude"`` selects
    Claude Code rather than falling through to the codex default), and
    ``error``/``hint`` are user-facing lines when the preflight fails.
    """
    if agent_cmd:
        # Custom command takes precedence in get_agent regardless of
        # type; there is nothing to look up in PATH.
        return agent_type, None, None

    normalized = (agent_type or "auto").strip().lower()
    canonical = _AGENT_TYPE_ALIASES.get(normalized)

    if canonical is None:
        return (
            agent_type,
            f"Unknown agent type {agent_type!r} "
            "(expected: claude, claude-sdk, codex, custom, or auto)",
            "Fix [agent].type in kstrl.toml, KSTRL_AGENT_TYPE, or --agent-type",
        )
    if canonical == "custom":
        return (
            agent_type,
            'Agent type "custom" is configured but no agent command is set',
            "Set [agent].command in kstrl.toml, AGENT_CMD, or --agent-cmd",
        )
    if canonical == "claude-code":
        if not ClaudeCodeAgent.is_available():
            return (
                agent_type,
                "claude not found in PATH (config selects agent type 'claude')",
                "Install Claude Code, or use --agent-cmd / change [agent].type",
            )
        return "claude-code", None, None
    if canonical == "claude-sdk":
        if not ClaudeSdkAgent.is_available():
            return (
                agent_type,
                "claude-agent-sdk is not installed (config selects agent type 'claude-sdk')",
                "Install the sdk extra (uv sync --extra sdk), or change [agent].type",
            )
        return "claude-sdk", None, None
    if canonical == "codex":
        if not CodexAgent.is_available():
            return (
                agent_type,
                "codex not found in PATH (config selects agent type 'codex')",
                "Install codex, or use --agent-cmd / change [agent].type",
            )
        return "codex", None, None

    # auto: accept whichever agent is installed, like the factory does.
    if not ClaudeCodeAgent.is_available() and not CodexAgent.is_available():
        return (
            agent_type,
            "No agent available (codex and claude not found in PATH)",
            "Install an agent or use --agent-cmd to specify a custom one",
        )
    return "auto", None, None


def _probe_target_family(agent_cmd: str | None, canonical: str | None) -> str | None:
    """Which CLI family this config's turns would run on, or None.

    None means there is no CLI turn to probe. ``claude-sdk`` is the
    Claude FAMILY reached over the SDK transport rather than through the
    ``claude`` binary, so that carve-out is the only fact stated here;
    everything else defers to ``_cli_family``, the mirror of
    ``get_agent``'s dispatch that the R7.1 rotation already depends on
    (a custom command returns None there, and the auto-detect preference
    order stays stated once).
    """
    if canonical == "claude-sdk":
        return None
    return _cli_family(agent_cmd, canonical, ClaudeCodeAgent.is_available())


def _check_agent_preflight(config: KstrlConfig, ui_impl: UI) -> None:
    """Run the agent preflight against a resolved config; exit(1) on failure.

    On success, canonicalizes ``config.agent_type`` in place so every
    downstream ``get_agent`` call selects the same agent the preflight
    verified.

    A CLI on PATH that cannot complete a turn warns rather than exiting
    (#262). A probe wrong in the pessimistic direction - a network blip,
    a CLI startup quirk - would otherwise turn a working setup into a
    hard failure before any work happened, and a genuinely dead engineer
    CLI fails on its own first call anyway. The value is the message:
    the operator reads the CLI's own refusal instead of a confused
    downstream error.
    """
    canonical, error, hint = _agent_preflight(config.agent_cmd, config.agent_type)
    if error is not None:
        ui_impl.err(error)
        if hint is not None:
            ui_impl.info(hint)
        sys.exit(1)
    config.agent_type = canonical
    family = _probe_target_family(config.agent_cmd, canonical)
    if family is None:
        return
    probe = probe_family(family)
    if probe.live:
        return
    cli_name = "claude" if family == CLAUDE_FAMILY else "codex"
    detail = f": {probe.detail}" if probe.detail else ""
    ui_impl.warn(f"{cli_name} is installed but cannot run a turn{detail}")
    ui_impl.info(
        f"Check {cli_name}'s authentication, quota and config. The run "
        f"continues, but every {cli_name} call is likely to fail. "
        f"Set {PROBE_ENV_VAR}=0 to skip this check."
    )


def _check_prd_preflight(prd_file: Path, ui_impl: UI) -> None:
    """Validate prd.json existence + schema BEFORE any agent spend (R2.4).

    Without this, the agent burns full iterations against a prompt
    referencing a nonexistent PRD before Phase 1 reports "Failed to load
    PRD". Error style mirrors ``ks init``'s per-field messages.
    """
    if not prd_file.exists():
        ui_impl.err(f"PRD file not found: {prd_file}")
        ui_impl.info(
            "Run `ks init` to scaffold scripts/kstrl/prd.json, "
            "or point --prd / PRD_FILE at an existing PRD."
        )
        sys.exit(1)

    try:
        with open(prd_file, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        ui_impl.err(f"Invalid JSON in {prd_file}: {exc}")
        sys.exit(1)
    except OSError as exc:
        ui_impl.err(f"Cannot read PRD file {prd_file}: {exc}")
        sys.exit(1)

    errors = PRD.validate_schema(data)
    if errors:
        ui_impl.err(f"PRD schema validation failed for {prd_file}:")
        for error in errors:
            ui_impl.info(f"  - {error}")
        sys.exit(1)


def _check_prompt_preflight(path: Path | None, ui_impl: UI) -> None:
    """Tell the OPERATOR when a prompt file is an older kstrl template (#286).

    Here, in the parent process, on the console UI, and BEFORE the TUI
    branch of every command that has one. That placement is the whole
    point of this function existing rather than the check living in
    ``run_loop`` beside the read: on a factory run ``run_loop`` executes
    in a pool worker whose UI writes to that component's engineer.jsonl,
    so a warning there reaches nobody. #261's CLAUDE.md divergence
    warning was written that way and #275 had to lift it into the parent
    for exactly this reason.

    Advisory only, never fatal: the run is still correct with an older
    prompt, just missing whatever the newer template fixed.

    Two limits, stated rather than papered over.

    Under the default interactive ``ks factory`` the dashboard
    deliberately drops ``Log`` events, so this lands on the plain
    terminal before Textual takes the screen and is in scrollback again
    when it exits. Putting it in the feed would mean a typed event,
    which is the right shape for something DISCOVERED mid-run and the
    wrong shape for a start-up fact about a file on disk.

    The home shell's own factory launch (``tui/session._prepare_factory``)
    is not covered, because it has no non-fatal notice channel: its only
    pre-run signal is ``LaunchError``, and a stale prompt is advisory,
    not fatal. Giving the launcher an advisory channel is UI work that
    would also carry every other factory advisory it currently swallows.

    ``ks init`` is the surface that both reports this and fixes it, and
    it is reachable from every path.
    """
    if path is None:
        return
    notice = staleness_notice(path)
    if notice is None:
        return
    ui_impl.warn(notice.headline)
    ui_impl.info(notice.advice)


def _apply_cli_overrides(
    ctx: click.Context,
    config: KstrlConfig,
    root_dir: Path,
    prompt_default: Path,
    prd_default: Path,
) -> set[str]:
    """Overlay explicitly-passed CLI flags onto a loaded KstrlConfig.

    Shared by ``run`` and ``config show`` so what the observability
    command prints is exactly what the run command executes. Only flags
    the invoking command declares are considered (``ctx.params``), and
    only when the user actually passed them. Returns the KstrlConfig
    field names a flag overrode, for per-value source reporting.
    """

    def passed(name: str) -> bool:
        return name in ctx.params and _use_cli_value(ctx, name)

    overridden: set[str] = set()
    if passed("max_iterations"):
        config.max_iterations = ctx.params["max_iterations"]
        overridden.add("max_iterations")
    if passed("prompt"):
        config.prompt_file = _resolve_path(root_dir, ctx.params["prompt"], prompt_default)
        overridden.add("prompt_file")
    if passed("prd"):
        config.prd_file = _resolve_path(root_dir, ctx.params["prd"], prd_default)
        overridden.add("prd_file")
    if passed("sleep"):
        config.sleep_seconds = ctx.params["sleep"]
        overridden.add("sleep_seconds")
    if passed("interactive"):
        config.interactive = ctx.params["interactive"]
        overridden.add("interactive")
    if passed("allowed_paths"):
        config.allowed_paths = _parse_paths(ctx.params["allowed_paths"])
        overridden.add("allowed_paths")
    if passed("branch"):
        config.kstrl_branch = ctx.params["branch"]
        config.kstrl_branch_explicit = True
        overridden.add("kstrl_branch")
    if passed("agent_cmd"):
        config.agent_cmd = ctx.params["agent_cmd"]
        overridden.add("agent_cmd")
    if passed("model"):
        config.model = ctx.params["model"]
        overridden.add("model")
    if passed("reasoning"):
        config.model_reasoning_effort = ctx.params["reasoning"]
        overridden.add("model_reasoning_effort")
    if passed("agent_type"):
        config.agent_type = ctx.params["agent_type"]
        overridden.add("agent_type")
    if passed("ui"):
        config.ui_mode = _normalize_ui_mode(ctx.params["ui"])
        overridden.add("ui_mode")
    if passed("no_color"):
        config.no_color = ctx.params["no_color"]
        overridden.add("no_color")
    if passed("ascii"):
        config.ascii_only = ctx.params["ascii"]
        overridden.add("ascii_only")
    return overridden


def _collect_toml_notes(
    notes: list[str],
    section: str,
    loaded: Any,
    baseline: Any,
    flag_overridden: set[str],
) -> None:
    """Record which effective values kstrl.toml moved off the CLI default.

    Before R2.1 six kstrl.toml sections were silently ignored by the
    factory command, so a value that now takes effect is a behavior
    change for existing setups; the collected NOTE lines make that
    visible at startup. Comparing the loaded config against an env-only
    baseline isolates the toml contribution (env overlays are applied
    identically on both sides, so they cancel out). Fields whose CLI
    flag was explicitly passed are excluded: the flag wins, so the toml
    value is not effective.
    """
    for f in dataclass_fields(loaded):
        if f.name in flag_overridden:
            continue
        loaded_val = getattr(loaded, f.name)
        baseline_val = getattr(baseline, f.name)
        if loaded_val != baseline_val:
            notes.append(
                f"NOTE: [{section}] {f.name} = {loaded_val!r} from "
                f"kstrl.toml (built-in default: {baseline_val!r}; "
                f"this section was ignored before R2.1)"
            )


def _collect_evolution_notes(
    notes: list[str],
    root_dir: Path,
    ui_impl: UI,
) -> None:
    """The evolution section's NOTE sweep, guarded against a bad knob.

    Its own function because the guard has to sit BETWEEN the two loads:
    ``from_env`` applies the same env overrides as ``load``, so passing
    both as arguments to :func:`_collect_toml_notes` would evaluate the
    unguarded one first and raise before the guard could run.

    Why this section and not the others, stated precisely because the
    obvious reading is wrong (#257 review): evolution is NOT the only
    loader here that can raise. Measured on this tree, `ks factory`
    exits 1 with a raw ValueError traceback under
    ``KSTRL_MUTATION_THRESHOLD=many`` and ``KSTRL_SECURITY_TIMEOUT=many``
    too, and the toml paths of verify, security, contract, feedforward
    and timeout raise the same way. Five sibling loads in this command
    are still unguarded.

    What separates evolution is what a failure MAY cost. The journal is
    an optional audit trail, so "unreadable, carry on without it" is an
    honest degrade. For verify, security, contract and timeout it is
    not: silently substituting defaults for the checks an operator
    configured is a semantic substitution, and those need an error
    boundary - a typed parse error caught where
    ``_KstrlGroup.invoke`` already catches ``BudgetConfigError`` - not a
    fallback. That boundary is a change to six config modules and is
    left out of #257.

    So this is one exit plugged, not the class closed. It is the exit
    worth plugging first: it runs AFTER the architect has been paid for
    and BEFORE ``run_factory``, with the spend live only in a local
    variable. It is not the only exit in that window either - the
    confirm prompt's "Quit" is an ordinary one - which is the honest
    argument for eventually making the architect's spend durable where
    it is earned rather than plugging exits one at a time.
    """
    from kstrl.evolution import EvolutionConfig

    config = EvolutionConfig.load_or_none(root_dir, warn=ui_impl.warn)
    if config is None:
        return
    _collect_toml_notes(
        notes,
        "evolution",
        config,
        EvolutionConfig.from_env(root_dir),
        flag_overridden=set(),
    )


def _resolve_root(root: Path | None, prompt: Path | None, prd: Path | None) -> Path:
    if root is not None:
        return root.resolve()

    for candidate in (prompt, prd):
        if candidate is None:
            continue
        resolved = candidate.resolve()
        parent = resolved.parent
        if parent.name == "kstrl" and parent.parent.name == "scripts":
            return parent.parent.parent

    return Path.cwd()


def _resolve_path(root: Path, value: str | None, default: Path) -> Path:
    if value is None or value == "":
        return default
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def _derive_feature_name(prd_path: Path, root: Path) -> str:
    try:
        rel = prd_path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = None

    if rel is not None and len(rel.parts) >= 4:
        if rel.parts[0] == "scripts" and rel.parts[1] == "kstrl" and rel.parts[2] == "feature":
            return rel.parts[3]

    return prd_path.stem


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# The commands that must NOT be preflighted, and why each one is not a
# hole in the guarantee:
#
# - `ks init` WRITES a kstrl.toml, including over a broken one. Refusing
#   to run it because the file it is about to replace does not parse
#   takes away the tool the operator recovers with.
# - `ks config show` is the surface that must ALWAYS run and always
#   explain, because every other command refuses on a rejected section.
#   It prints every row it can resolve, then names each rejected section
#   with its key and value. That guarantee is what makes universal
#   fatality defensible without an escape flag: the way out of a bad
#   config is a command, not a way to skip the check.
# - `ks sense` reports a config failure through a documented MACHINE
#   contract that the seam would destroy: exit 2 (not 1) and a JSON
#   error document on stdout for `--json`. It calls the preflight
#   itself, under that contract.
# - `ks serve` has the same documented exit 2, and also calls the
#   preflight itself, before `--print-plist` returns.
#
# The last three are exempt from the SEAM, never from the check: each
# runs the same `preflight_config` in its own body, under its own
# contract. `init` is the one command exempt from the check itself, for
# the reason above. An exemption that skipped the check for any other
# reason would be the property #272 removed, smuggled back in under a
# name.
#
# Bare `ks` is NOT on this list and is not a command: its callback
# belongs to the group, so it preflights explicitly (see `cli`).
#
# Keyed by the TOP-LEVEL command name (see `_KstrlCommand._top_level_name`),
# so `ks config show` is covered by "config" while a later `ks queue init`
# is not exempted by its leaf name.
_PREFLIGHT_EXEMPT = frozenset({"init", "config", "sense"})

# Sections a command is ABOUT, promoted from degrading to fatal for that
# command only. `[evolution]` degrades everywhere because the journal is
# an audit trail attached to work about something else; `ks evolve` IS
# the journal, so warning and continuing would be a promise the next two
# lines break. Declared here, beside the exemptions, so a command does
# not carry its own remembered guard for a policy the seam already owns.
_PREFLIGHT_REQUIRED: dict[str, frozenset[str]] = {
    "evolve": frozenset({"evolution"}),
}

# Exit code for a rejected configuration, per command. 1 unless the
# command documents otherwise. `ks serve` is the only entry here: it
# promises exit 2 for "cannot run", and reading that from the seam is
# what makes the ORDERING structural. Calling the check in its body
# instead left `--print-plist` in front of it, returning before the
# check ran and then exiting 1 through the group handler; the next early
# return would have done the same.
_PREFLIGHT_EXIT: dict[str, int] = {"serve": 2}

# The commands that derive their root from a prompt or PRD path, and so
# the only ones whose `--prompt` / `--prd` / `--understand-prompt` (and
# PROMPT_FILE / PRD_FILE) the entry check may read. See `_preflight_root`
# for why this is a list of commands and not "whichever command declares
# the option".
_ROOT_FROM_PROMPT = frozenset({"run", "understand", "feature"})


def _preflight_warn(message: str) -> None:
    """A degrading section's warning, on STDERR.

    Stdout belongs to the command's output, and `ks sense --json` puts a
    single JSON document there that a script parses.
    """
    click.echo(f"warning: {message}", err=True)


def _echo_config_problems(problems: list[str]) -> None:
    """`ks config show`'s rejected-section block, in ONE shape.

    Both of that command's failure paths print it: the one where no row
    could be built, and the one where the rows printed and some section
    was still rejected. Two renderings 35 lines apart is how an operator
    piping stderr gets a different answer for the same fault.
    """
    click.echo("# Rejected sections (every other command refuses while these stand)")
    for problem in problems:
        click.echo(f"  {problem}")
    click.echo("")
    click.echo(f"error: {len(problems)} unusable config section(s)", err=True)


def _echo_journal_repairs(journal: EvolutionJournal, ui_impl: UI) -> None:
    """`ks evolve --status`'s report of interrupted journal writes (#312).

    Why the count exists at all is argued once, on
    ``EvolutionJournal.get_repair_count``. What is decided HERE:

    ``warn``, not ``err``: this command exits 0, and every other
    ``ui_impl.err`` in this module precedes a non-zero exit, so printing
    ERROR in red would make a repaired journal indistinguishable from
    "Evolution is disabled in config".

    POSSIBLE loss, not confirmed loss (#327 round 2, F8): the line
    names both outcomes and points at the one line that decides which.

    Silent at zero, which is the whole reason this is worth a line at
    all: a healthy journal that prints "0 repairs" every time teaches an
    operator to skip the line that matters.

    Only ``ks evolve --status`` calls it. The evolve TUI screen renders
    the same trends and stays silent about repairs, which is #333:
    ``get_repair_count`` is click-free and on the journal, so that
    screen can ask for itself, but claiming this helper serves the TUI
    would be false. It lives in the click module.

    Takes the journal and asks IT for the path, rather than being handed
    both: a count from one journal printed beside another one's path is
    a report that cannot be wrong today and could be after any edit. A
    helper rather than four lines inline because ``evolve`` is already
    over the cognitive gate at 23, so a branch added there would fail
    the staged complexity ratchet on a function this change is not
    otherwise touching.
    """
    repairs = journal.get_repair_count()
    if repairs:
        ui_impl.warn(
            f"  journal: {repairs} interrupted write(s) repaired. A crash left "
            f"{journal.config.journal_path} without a trailing newline. The line above "
            "each journal_repair row is what that write left behind: either a torn "
            "fragment, which is lost, or a whole record that lost only its newline, "
            "which is readable again. Read it to tell which."
        )


def _preflight_root(ctx: click.Context) -> Path:
    """The root the command is about to use, derived before it runs.

    Reuses ``_resolve_root`` - the same inputs, in the same precedence -
    so the file the preflight validates is the file the command will
    load. Every other command uses ``root or Path.cwd()``, which is what
    this returns for them.

    The prompt and PRD inputs are read ONLY for the commands in
    ``_ROOT_FROM_PROMPT``, and that is the whole correctness argument.
    Reading them for every command broke this both ways with one stale
    ``PROMPT_FILE`` export: ``ks status`` refused on a broken kstrl.toml
    belonging to an unrelated checkout, and - worse, because nothing
    shows it - ``ks status`` in a project whose OWN config was broken
    PASSED, by validating that other checkout instead.

    Keyed by command rather than by "declares the option", which reads
    like the same rule and is not: `ks config show` declares ``--prompt``
    and ``--prd`` as ``[paths]`` OVERRIDES and still roots itself at the
    cwd, so the proxy already pointed that command at another checkout.
    ``tests/test_config_preflight.py`` fails on any new command that
    declares one of these options without a decision being recorded here.
    """

    def _param(name: str) -> str | None:
        value = ctx.params.get(name)
        return str(value) if value else None

    def _path(*names: str, env_var: str) -> Path | None:
        if _KstrlCommand._top_level_name(ctx) not in _ROOT_FROM_PROMPT:
            return None
        for name in names:
            value = _param(name)
            if value:
                return Path(value)
        from_env = os.environ.get(env_var)
        return Path(from_env) if from_env else None

    root = _param("root")
    return _resolve_root(
        Path(root) if root else None,
        # `ks feature` names its prompt option --understand-prompt and
        # feeds THAT to _resolve_root. No command declares both.
        _path("prompt", "understand_prompt", env_var="PROMPT_FILE"),
        _path("prd", env_var="PRD_FILE"),
    )


class _KstrlCommand(click.Command):
    """Every command, with one guarantee: the configuration is resolved
    before the command body constructs anything.

    THIS seam and not ``_KstrlGroup.invoke``, which is where the error is
    caught: at group level click has parsed the group's own arguments but
    not the subcommand's, so ``--root`` is not known yet and the
    preflight would validate the wrong file whenever an operator pointed
    a command at another checkout. ``Command.invoke`` runs after the
    subcommand's parameters are parsed and before its callback, which is
    the first moment both facts are available - the root, and that
    nothing has been built or paid for yet.

    Installed through ``_KstrlGroup.command_class`` rather than on each
    command, for the reason the group gives below: a guarantee that every
    entry point has to remember is one a later entry point will forget.
    """

    def invoke(self, ctx: click.Context) -> Any:
        from kstrl.config_preflight import preflight_config

        name = self._top_level_name(ctx)
        if name not in _PREFLIGHT_EXEMPT:
            preflight_config(
                _preflight_root(ctx),
                warn=_preflight_warn,
                required=_PREFLIGHT_REQUIRED.get(name, frozenset()),
            )
        return super().invoke(ctx)

    @staticmethod
    def _top_level_name(ctx: click.Context) -> str:
        """The command name directly under the root group.

        Both tables key off THIS, not off any name in the chain: keying
        off any would exempt a later ``ks queue init`` or ``ks inbox
        serve`` purely because of its leaf name, which is a decision
        nobody would have made. It is also what puts ``ks config show``
        under ``config``.
        """
        node = ctx
        while node.parent is not None and node.parent.parent is not None:
            node = node.parent
        return node.command.name or ""


class _KstrlGroup(click.Group):
    """The CLI group, with two guarantees: a rejected budget ceiling and
    unusable configuration are reported, never raised.

    ``BudgetConfigError`` is thrown deep inside config loading, which
    happens in `factory`, `run`, `retry`, the config report and the
    launch path - and, after a ``--max-cost-usd`` override, inside
    ``run_factory`` itself. Catching it per command meant every one of
    those sites had to remember; `factory` did not, and exited 1 with an
    empty stdout and a raw traceback (review finding on #180). Catching
    it HERE means no entry point can leak one, including entry points
    added later.

    ``ConfigError`` is the same contract for the same reason (#272).
    Before it, a typo's blast radius depended on which section it was in
    and which command was run: ``KSTRL_MUTATION_THRESHOLD=many`` and
    ``KSTRL_SECURITY_TIMEOUT=many`` both left a raw ``ValueError``
    traceback out of `ks factory`, and a bad ``[linear]`` value aborted
    `ks decompose` only after the architect had been paid for.
    ``command_class`` puts the check that raises it in front of every
    command body; this catches what it raises.

    Exit code 1 with an ``error:`` line, matching what `config show`
    already did for the same defect - one class of failure, one contract.
    """

    command_class = _KstrlCommand
    #: ``type`` is click's "same class as this group", so `ks config`,
    #: `ks queue` and every later subgroup inherit ``command_class``.
    group_class = type

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except ConfigError as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(_PREFLIGHT_EXIT.get(ctx.invoked_subcommand or "", 1))
        except BudgetConfigError as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(1)


@click.group(cls=_KstrlGroup, invoke_without_command=True)
@click.version_option(version=__version__)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """kstrl - a software factory for AI coding agents.

    Hand it a spec. It plans, builds, measures the result with checks the
    agent did not write, feeds the gap back, and stops only when independent
    checks agree. Every run is recorded; boundary decisions come to you."""
    if ctx.invoked_subcommand is not None:
        return
    # Bare `ks` on a TTY opens the home shell (D1 user decision);
    # everywhere else stays byte-identical to click's no-args behavior
    # (help on stdout, exit 2) - the pipe/CI contract.
    if sys.stdout.isatty() and sys.stdin.isatty() and os.environ.get("KSTRL_NO_TUI") != "1":
        from kstrl.config_preflight import preflight_config
        from kstrl.tui.home import run_home_shell

        # The home shell is an ENTRY POINT, not a command: this callback
        # belongs to the group, so `_KstrlCommand.invoke` never runs for
        # it. Without this line it was a fifth exemption that nobody
        # declared, and the most expensive one, because the shell
        # launches runs IN-PROCESS (`tui/session.py` calls run_factory
        # and decompose_spec directly). A bad [linear] value would have
        # paid for the architect and then aborted: the original #272
        # defect, on the path a user reaches by typing `ks`.
        #
        # Bound once: the point of the check is that the root it
        # validates is the root the shell opens.
        root_dir = Path.cwd()
        preflight_config(root_dir, warn=_preflight_warn)
        ctx.exit(run_home_shell(root_dir))
    click.echo(ctx.get_help())
    ctx.exit(2)


# Issue #207: `ks run` forces these FactoryConfig fields because it is by
# definition a local, single-component, no-PR invocation. When the resolved
# config (kstrl.toml + env) set one of them to a non-default value, the
# operator must be told the knob does not apply - a silent override of a
# configured knob is exactly the fail-open-and-quiet behaviour R8.3
# corrected on the factory path. Field name -> (forced value, why the
# configured value does not apply).
_RUN_FORCED_STRUCTURAL_FIELDS: tuple[tuple[str, object, str], ...] = (
    ("max_parallel", 1, "ks run executes a single component"),
    ("use_worktrees", False, "ks run works directly in the repo checkout"),
    ("single_pr", False, "ks run creates no PRs"),
    ("create_prs", False, "ks run creates no PRs"),
)


def _toml_literal(value: object) -> str:
    """Render a Python value the way the operator wrote it in kstrl.toml."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _run_structural_override_notices(loaded: FactoryConfig) -> list[str]:
    """Notices for structural config that `ks run` is about to force.

    Compares the resolved config against the dataclass defaults so a notice
    fires only when the operator (kstrl.toml or env) actually set a knob
    that ``ks run`` forces to a different value - an unset knob stays
    silent, so the notices never become background noise.

    ``pause_before_pr_merge`` is deliberately NOT handled here. The
    autonomy ladder resolves inside ``run_factory`` and its L1/L2 bundle
    can flip the gate on when no config flag ever set it, so any check at
    this point sees a stale value. The authoritative warning is
    ``factory.merge_gate_unreachable_warning``, emitted after autonomy
    resolution - the one point that sees the final flag on every path.
    """
    defaults = FactoryConfig()
    notices: list[str] = []
    for field_name, forced_value, reason in _RUN_FORCED_STRUCTURAL_FIELDS:
        configured = getattr(loaded, field_name)
        if configured == forced_value:
            continue
        if configured == getattr(defaults, field_name):
            continue
        notices.append(
            f"[factory] {field_name} = {_toml_literal(configured)} does not "
            f"apply to `ks run` ({reason}); using "
            f"{_toml_literal(forced_value)}"
        )
    return notices


@cli.command()
@click.argument("max_iterations", type=int, default=10)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prompt",
    "-p",
    type=str,
    help="Prompt file path",
)
@click.option(
    "--prd",
    type=str,
    help="PRD file path",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model",
    "-m",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--sleep",
    "-s",
    type=float,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    help="Enable human-in-the-loop mode",
)
@click.option(
    "--branch",
    help="Git branch to use (empty string to skip checkout)",
)
@click.option(
    "--allowed-paths",
    help="Comma-separated allowed paths for guardrails",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
@click.option(
    "--ascii",
    is_flag=True,
    help="Use ASCII characters only",
)
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip mechanical verification (raw loop, no post-checks)",
)
@click.option(
    "--force-lock",
    is_flag=True,
    help="Proceed even if another kstrl invocation holds "
    ".kstrl/factory.lock (may corrupt the other run's state)",
)
def run(
    max_iterations: int,
    root: Path | None,
    prompt: str | None,
    prd: str | None,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    sleep: float,
    interactive: bool,
    branch: str | None,
    allowed_paths: str | None,
    ui: str,
    no_color: bool,
    ascii: bool,
    no_verify: bool,
    force_lock: bool,
) -> None:
    """Run the agentic loop as a single-component factory invocation.

    MAX_ITERATIONS is the maximum number of iterations (default: 10).

    Delegates to the factory pipeline with mechanical verification.
    Use --no-verify to skip the verification phase.
    """
    ctx = click.get_current_context()
    env_prompt = os.environ.get("PROMPT_FILE")
    env_prd = os.environ.get("PRD_FILE")

    prompt_for_root = Path(prompt) if _use_cli_value(ctx, "prompt") and prompt is not None else None
    if prompt_for_root is None and env_prompt is not None:
        prompt_for_root = Path(env_prompt)

    prd_for_root = Path(prd) if _use_cli_value(ctx, "prd") and prd is not None else None
    if prd_for_root is None and env_prd is not None:
        prd_for_root = Path(env_prd)

    root_value = root if _use_cli_value(ctx, "root") else None
    root_dir = _resolve_root(root_value, prompt_for_root, prd_for_root)

    # Build config from kstrl.toml + environment defaults first.
    config = KstrlConfig.load(root_dir)

    # Apply CLI overrides when explicitly provided.
    _apply_cli_overrides(
        ctx,
        config,
        root_dir,
        prompt_default=root_dir / "scripts/kstrl/prompt.md",
        prd_default=root_dir / "scripts/kstrl/prd.json",
    )

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(
        config.ui_mode,
        config.no_color,
        config.ascii_only,
        force_rich=force_rich,
    )

    if config.max_iterations < 0:
        ui_impl.err(f"MAX_ITERATIONS must be non-negative (got: {config.max_iterations})")
        sys.exit(2)

    # R2.4 preflight: verify the agent the config selects is reachable,
    # then validate the PRD - both BEFORE any agent invocation.
    _check_agent_preflight(config, ui_impl)
    _check_prd_preflight(config.prd_file, ui_impl)
    _check_prompt_preflight(config.prompt_file, ui_impl)

    # Single-component factory invocation
    from kstrl.config import load_toml_section
    from kstrl.feedforward import FeedforwardConfig
    from kstrl.manifest import Manifest
    from kstrl.security import SecurityConfig
    from kstrl.verify import VerifyConfig

    # Determine branch from config or PRD. The preflight above already
    # validated existence + schema, so a load failure here is a real bug
    # worth surfacing, not something to swallow.
    prd_branch = PRD.load(config.prd_file).branch_name

    effective_branch = config.kstrl_branch or prd_branch or "kstrl/run"

    # Detect base branch from git
    detected_base = detect_base_branch(root_dir)

    # Build single-component manifest from PRD
    rel_prd = str(config.prd_file)
    try:
        rel_prd = str(config.prd_file.relative_to(root_dir))
    except ValueError:
        pass

    manifest = Manifest.from_prd(
        prd_path=Path(rel_prd),
        branch=effective_branch,
        base_branch=detected_base,
    )

    # Build factory config for single-component mode (R2.1): tunables
    # resolve through the loaders (kstrl.toml overlaid with env); the
    # structural fields below are forced because `ks run` is by
    # definition a local, single-component, no-PR invocation.
    # R2.3: --no-verify sets the explicit skip sentinel; passing
    # verify_config=None meant "use defaults" in run_factory and Phase 1
    # ran anyway. Feedforward is independent of --no-verify (it builds
    # context, not checks).
    factory_cfg = FactoryConfig.load(root_dir)
    # Issue #207: say which configured knobs the forcing below overrides,
    # BEFORE mutating the config. The merge-gate warning itself is emitted
    # later, by run_factory after autonomy resolution (see
    # factory.merge_gate_unreachable_warning).
    for notice in _run_structural_override_notices(factory_cfg):
        ui_impl.warn(notice)
    factory_cfg.max_parallel = 1
    factory_cfg.use_worktrees = False
    factory_cfg.single_pr = False
    factory_cfg.create_prs = False
    factory_cfg.verify_config = None if no_verify else VerifyConfig.load(root_dir)
    factory_cfg.skip_verification = no_verify
    factory_cfg.security_config = SecurityConfig.load(root_dir)
    factory_cfg.contract_config = None
    factory_cfg.feedforward_config = FeedforwardConfig.load(root_dir)
    factory_cfg.timeout_config = TimeoutConfig.load(root_dir)
    factory_cfg.force_lock = force_lock
    # `ks run` reviews in advisory mode unless the project's
    # kstrl.toml explicitly opts into a different review_mode (there is
    # no review_mode env var, so the toml section check is exhaustive).
    if "review_mode" not in load_toml_section(resolve_config_file(root_dir), "factory"):
        factory_cfg.review_mode = "advisory"

    # R0.5 (H-15): `ks run` persists to its own run-manifest.json so
    # it can never clobber a factory run's resumable manifest.json.
    stop = StopController()
    uninstall = install_signal_handlers(stop)
    try:
        factory_result = run_factory(
            manifest,
            factory_cfg,
            config,
            ui_impl,
            root_dir,
            manifest_path=root_dir / "scripts" / "kstrl" / "run-manifest.json",
            stop=stop,
        )
    finally:
        uninstall()
    sys.exit(factory_result.exit_code)


@cli.command()
@click.argument("directory", type=click.Path(path_type=Path), default=".")
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
@click.option(
    "--upgrade-prompts",
    is_flag=True,
    help="Rewrite any scaffolded prompt template that is still a "
    "pristine older kstrl template. A template you have edited is "
    "reported and left alone.",
)
def init(directory: Path, ui: str, no_color: bool, upgrade_prompts: bool) -> None:
    """Initialize kstrl in a project directory.

    DIRECTORY is the target project directory (default: current directory).
    """
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)
    exit_code = run_init(directory, ui_impl, upgrade_prompts=upgrade_prompts)
    sys.exit(exit_code)


@cli.command()
@click.argument("max_iterations", type=int, default=10)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prompt",
    "-p",
    type=str,
    help="Prompt file path",
)
@click.option(
    "--prd",
    type=str,
    help="PRD file path",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command",
)
@click.option(
    "--model",
    "-m",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--sleep",
    "-s",
    type=float,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    help="Enable human-in-the-loop mode",
)
@click.option(
    "--branch",
    help="Git branch (default: kstrl/understanding)",
)
@click.option(
    "--allowed-paths",
    help="Comma-separated allowed paths for guardrails",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
@click.option(
    "--ascii",
    is_flag=True,
    help="Use ASCII characters only",
)
@click.option(
    "--tui/--no-tui",
    "tui",
    default=None,
    help="Embedded dashboard (default: auto - on when stdin/stdout are "
    "TTYs and --ui is not plain; KSTRL_NO_TUI=1 forces off)",
)
def understand(
    max_iterations: int,
    root: Path | None,
    prompt: str | None,
    prd: str | None,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    sleep: float,
    interactive: bool,
    branch: str | None,
    allowed_paths: str | None,
    ui: str,
    no_color: bool,
    ascii: bool,
    tui: bool | None,
) -> None:
    """Run codebase understanding loop (read-only mode).

    MAX_ITERATIONS is the maximum number of iterations (default: 10).

    This mode:
    - Uses understand_prompt.md instead of prompt.md
    - Only allows edits to codebase_map.md
    - Works on kstrl/understanding branch by default
    """
    ctx = click.get_current_context()
    env_prompt = os.environ.get("PROMPT_FILE")
    env_prd = os.environ.get("PRD_FILE")

    prompt_for_root = Path(prompt) if _use_cli_value(ctx, "prompt") and prompt is not None else None
    if prompt_for_root is None and env_prompt is not None:
        prompt_for_root = Path(env_prompt)

    prd_for_root = Path(prd) if _use_cli_value(ctx, "prd") and prd is not None else None
    if prd_for_root is None and env_prd is not None:
        prd_for_root = Path(env_prd)

    root_value = root if _use_cli_value(ctx, "root") else None
    root_dir = _resolve_root(root_value, prompt_for_root, prd_for_root)
    kstrl_dir = root_dir / "scripts" / "kstrl"

    # Create codebase_map.md if missing
    codebase_map = kstrl_dir / "codebase_map.md"
    if not codebase_map.exists():
        from kstrl.init_cmd import DEFAULT_CODEBASE_MAP

        codebase_map.parent.mkdir(parents=True, exist_ok=True)
        codebase_map.write_text(DEFAULT_CODEBASE_MAP)

    config = KstrlConfig.load(root_dir)

    # Apply CLI overrides when explicitly provided.
    if _use_cli_value(ctx, "max_iterations"):
        config.max_iterations = max_iterations
    if _use_cli_value(ctx, "prompt"):
        config.prompt_file = _resolve_path(root_dir, prompt, kstrl_dir / "understand_prompt.md")
    if _use_cli_value(ctx, "prd"):
        config.prd_file = _resolve_path(root_dir, prd, kstrl_dir / "prd.json")
    if _use_cli_value(ctx, "sleep"):
        config.sleep_seconds = sleep
    if _use_cli_value(ctx, "interactive"):
        config.interactive = interactive
    if _use_cli_value(ctx, "allowed_paths"):
        config.allowed_paths = _parse_paths(allowed_paths)
    if _use_cli_value(ctx, "branch"):
        config.kstrl_branch = branch
        config.kstrl_branch_explicit = True
    if _use_cli_value(ctx, "agent_cmd"):
        config.agent_cmd = agent_cmd
    if _use_cli_value(ctx, "model"):
        config.model = model
    if _use_cli_value(ctx, "reasoning"):
        config.model_reasoning_effort = reasoning
    if _use_cli_value(ctx, "ui"):
        config.ui_mode = _normalize_ui_mode(ui)
    if _use_cli_value(ctx, "no_color"):
        config.no_color = no_color
    if _use_cli_value(ctx, "ascii"):
        config.ascii_only = ascii

    # Apply understanding defaults when not overridden by env or CLI.
    if not _use_cli_value(ctx, "prompt") and "PROMPT_FILE" not in os.environ:
        config.prompt_file = kstrl_dir / "understand_prompt.md"
    if not _use_cli_value(ctx, "allowed_paths") and "ALLOWED_PATHS" not in os.environ:
        config.allowed_paths = ["scripts/kstrl/codebase_map.md"]
    # Only fall back to the understand-mode branch default when no other
    # source (CLI / env / TOML) supplied a branch. KstrlConfig.load sets
    # kstrl_branch_explicit=True when TOML provides a non-empty [git].branch.
    if (
        not _use_cli_value(ctx, "branch")
        and "KSTRL_BRANCH" not in os.environ
        and not config.kstrl_branch_explicit
    ):
        config.kstrl_branch = "kstrl/understanding"
        config.kstrl_branch_explicit = False

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(
        config.ui_mode,
        config.no_color,
        config.ascii_only,
        force_rich=force_rich,
    )

    if config.max_iterations < 0:
        ui_impl.err(f"MAX_ITERATIONS must be non-negative (got: {config.max_iterations})")
        sys.exit(2)

    # R2.4 preflight: accept whichever agent the resolved config selects.
    _check_agent_preflight(config, ui_impl)
    _check_prompt_preflight(config.prompt_file, ui_impl)

    sandbox_cfg = SandboxConfig.load(root_dir)
    if sandbox_cfg.enabled and config.agent_cmd:
        ui_impl.warn(
            "[sandbox] enabled but the agent is a custom command; sandbox "
            "settings cannot be applied to it and are ignored"
        )
    agent = get_agent(
        config.agent_cmd,
        config.model,
        config.model_reasoning_effort,
        config.agent_type,
        sandbox=sandbox_cfg,
        max_budget_usd=config.agent_budget_usd,
    )

    use_tui = (
        tui
        if tui is not None
        else (
            sys.stdout.isatty()
            and sys.stdin.isatty()
            and os.environ.get("KSTRL_NO_TUI") != "1"
            and config.ui_mode != "plain"
        )
    )
    if use_tui:
        if not (sys.stdout.isatty() and sys.stdin.isatty()):
            click.echo(
                "--tui requires an interactive terminal; use --no-tui "
                "for non-interactive execution.",
                err=True,
            )
            sys.exit(2)
        from kstrl.runid import mint_run_id
        from kstrl.tui.embed import EmbeddedContext, run_embedded
        from kstrl.tui.screens.component import ComponentScreen
        from kstrl.tui.screens.overview import OverviewScreen

        def _target(embed_ctx: EmbeddedContext) -> int:
            command_run = open_command_run(
                embed_ctx.ui,
                root_dir,
                "understand",
                component="understand",
                run_id=embed_ctx.run_id,
            )
            try:
                return _understand_core(
                    config,
                    agent,
                    root_dir,
                    embed_ctx.ui,
                    run=command_run,
                    interaction=embed_ctx.channel,
                    stop_check=embed_ctx.stop.is_set,
                )
            finally:
                command_run.close()

        sys.exit(
            run_embedded(
                _target,
                root_dir=root_dir,
                run_id=mint_run_id("understand"),
                screen_factory=lambda: [
                    OverviewScreen(observe_only=False),
                    ComponentScreen("understand"),
                ],
            )
        )

    command_run = open_command_run(
        ui_impl,
        root_dir,
        "understand",
        component="understand",
    )
    try:
        code = _understand_core(
            config,
            agent,
            root_dir,
            ui_impl,
            run=command_run,
        )
    finally:
        command_run.close()
    sys.exit(code)


def _understand_core(
    config: KstrlConfig,
    agent: Agent,
    root_dir: Path,
    ui_impl: UI,
    *,
    run: CommandRun,
    interaction: InteractionChannel | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> int:
    """The understand loop as an event-stream run (TUI surface C1).

    The reducer projects the work onto the pseudo-component
    "understand": one plan row, one phase, the loop's iterations. When
    recording, the agent is wrapped so its transcript lands where the
    dashboard's transcript pane tails.
    """
    bus = run.bus
    component = "understand"
    loop_agent = agent
    transcript = run.transcript_path(component)
    if transcript is not None:
        loop_agent = LoggingAgent(agent, transcript)

    started = time.monotonic()
    bus.emit(RunStarted(project=root_dir.name, components=1))
    bus.emit(
        RunPlan(components=({"id": component, "title": "Codebase understanding", "deps": []},))
    )
    bus.emit(ComponentStarted(component=component))
    bus.emit(PhaseStarted(component=component, phase="understand", attempt=1))

    # #264/#268: the standalone loop gets the same carve-out the factory
    # gets. `ks understand` writes the codebase map - the understand
    # prompt says it is the ONLY file it may edit - and its progress log.
    # The `understand` command's own default allowed_paths already names
    # the map, but an operator who passes --allowed-paths REPLACES that
    # default, and the in-loop guard then reverts the one file the prompt
    # told the agent to write. `ks run` needs nothing here: it is a single-component
    # factory invocation (Manifest.from_prd -> run_factory), so it
    # carries the carve-out through factory._run_component.
    understand_harness_paths = config.standalone_harness_files(root_dir)

    try:
        result = run_loop(
            config,
            ui_impl,
            loop_agent,
            root_dir,
            timeouts=TimeoutConfig.load(root_dir),
            breaker_config=BreakerConfig.load(root_dir),
            bus=bus,
            interaction=interaction,
            stop_check=stop_check,
            guard_ignored_paths=understand_harness_paths,
            guard_state_root=root_dir,
        )
    except Exception as exc:
        duration = round(time.monotonic() - started, 2)
        detail = f"{type(exc).__name__}: {exc}"
        bus.emit(
            PhaseCompleted(
                component=component,
                phase="understand",
                passed=False,
                detail=detail,
                duration_seconds=duration,
            )
        )
        bus.emit(ComponentFailed(component=component, error=detail))
        bus.emit(
            RunCompleted(
                completed=0,
                failed=1,
                duration_seconds=duration,
            )
        )
        raise

    duration = round(time.monotonic() - started, 2)
    passed = result.completed and result.exit_code == 0
    failure_detail = (
        f"exit {result.exit_code}" if result.exit_code != 0 else "ended before completion"
    )
    bus.emit(
        PhaseCompleted(
            component=component,
            phase="understand",
            passed=passed,
            detail="" if passed else failure_detail,
            duration_seconds=duration,
        )
    )
    if passed:
        map_path = config.codebase_map_file
        try:
            map_display = str(map_path.relative_to(root_dir))
        except ValueError:
            map_display = str(map_path)
        bus.emit(ArtifactWritten(label="codebase_map", path=map_display))
        bus.emit(
            ComponentCompleted(
                component=component,
                duration_seconds=duration,
                iterations=result.iterations,
            )
        )
    else:
        bus.emit(
            ComponentFailed(
                component=component,
                error=f"understand loop {failure_detail}",
            )
        )
    bus.emit(
        RunCompleted(
            completed=1 if passed else 0,
            failed=0 if passed else 1,
            duration_seconds=duration,
        )
    )
    return result.exit_code


@cli.command()
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prd",
    type=str,
    help="Feature PRD file path",
)
@click.option(
    "--understand-iterations",
    type=int,
    help="Iterations for the feature understanding phase",
)
@click.option(
    "--understand-prompt",
    "-p",
    type=str,
    help="Prompt file path for feature understanding",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model",
    "-m",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--sleep",
    "-s",
    type=float,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    help="Enable human-in-the-loop mode",
)
@click.option(
    "--branch",
    help="Git branch to use (empty string to skip checkout)",
)
@click.option(
    "--implementation-allowed-paths",
    help="Comma-separated allowed paths for implementation/repairs",
)
@click.option(
    "--implementation-auto-run",
    is_flag=True,
    help="Skip review gate and start implementation automatically",
)
@click.option(
    "--repair-max-runs",
    type=int,
    default=5,
    help="Maximum auto repair runs after a failed implementation",
)
@click.option(
    "--repair-iterations",
    type=int,
    default=5,
    help="Iterations per repair run",
)
@click.option(
    "--repair-agent-cmd",
    help="Custom agent command for repair runs (prompt piped to stdin)",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
@click.option(
    "--ascii",
    is_flag=True,
    help="Use ASCII characters only",
)
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip the advisory verification reports (raw loops, no post-checks)",
)
@click.option(
    "--tui/--no-tui",
    "tui",
    default=None,
    help="Embedded dashboard (default: auto - on when stdin/stdout are "
    "TTYs and --ui is not plain; KSTRL_NO_TUI=1 forces off)",
)
def feature(
    root: Path | None,
    prd: str | None,
    understand_iterations: int | None,
    understand_prompt: str | None,
    agent_cmd: str | None,
    repair_agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    sleep: float,
    interactive: bool,
    branch: str | None,
    implementation_allowed_paths: str | None,
    implementation_auto_run: bool,
    repair_max_runs: int,
    repair_iterations: int,
    ui: str,
    no_color: bool,
    ascii: bool,
    no_verify: bool,
    tui: bool | None,
) -> None:
    """Run feature understanding, then implementation.

    This mode:
    - Uses feature_understand_prompt.md for understanding by default
    - Only allows edits to the feature understand file during understanding
    - Uses the PRD branch by default
    - Starts implementation after review
    """
    ctx = click.get_current_context()
    env_prompt = os.environ.get("PROMPT_FILE")
    env_prd = os.environ.get("PRD_FILE")

    prompt_for_root = (
        Path(understand_prompt)
        if _use_cli_value(ctx, "understand_prompt") and understand_prompt is not None
        else None
    )
    if prompt_for_root is None and env_prompt is not None:
        prompt_for_root = Path(env_prompt)

    prd_for_root = Path(prd) if _use_cli_value(ctx, "prd") and prd is not None else None
    if prd_for_root is None and env_prd is not None:
        prd_for_root = Path(env_prd)

    root_value = root if _use_cli_value(ctx, "root") else None
    root_dir = _resolve_root(root_value, prompt_for_root, prd_for_root)
    kstrl_dir = root_dir / "scripts" / "kstrl"

    base_config = KstrlConfig.load(root_dir)

    # Apply CLI overrides that should affect both phases.
    if _use_cli_value(ctx, "sleep"):
        base_config.sleep_seconds = sleep
    if _use_cli_value(ctx, "interactive"):
        base_config.interactive = interactive
    if _use_cli_value(ctx, "agent_cmd"):
        base_config.agent_cmd = agent_cmd
    if _use_cli_value(ctx, "model"):
        base_config.model = model
    if _use_cli_value(ctx, "reasoning"):
        base_config.model_reasoning_effort = reasoning
    if _use_cli_value(ctx, "ui"):
        base_config.ui_mode = _normalize_ui_mode(ui)
    if _use_cli_value(ctx, "no_color"):
        base_config.no_color = no_color
    if _use_cli_value(ctx, "ascii"):
        base_config.ascii_only = ascii

    base_config.ui_mode = _normalize_ui_mode(base_config.ui_mode)

    # Check codex availability
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(
        base_config.ui_mode,
        base_config.no_color,
        base_config.ascii_only,
        force_rich=force_rich,
    )

    codebase_map = kstrl_dir / "codebase_map.md"
    if not codebase_map.exists():
        ui_impl.err(f"codebase_map.md not found: {codebase_map}")
        ui_impl.info("Run `ks init` or `ks understand` first.")
        sys.exit(1)

    if _use_cli_value(ctx, "understand_iterations"):
        if understand_iterations is None or understand_iterations < 0:
            ui_impl.err(
                f"UNDERSTAND_ITERATIONS must be non-negative (got: {understand_iterations})"
            )
            sys.exit(2)
        understand_iterations_value = understand_iterations
    else:
        if base_config.max_iterations < 0:
            ui_impl.err(
                f"UNDERSTAND_ITERATIONS must be non-negative (got: {base_config.max_iterations})"
            )
            sys.exit(2)
        understand_iterations_value = base_config.max_iterations

    if repair_max_runs < 0:
        ui_impl.err(f"REPAIR_MAX_RUNS must be non-negative (got: {repair_max_runs})")
        sys.exit(2)

    if repair_iterations < 0:
        ui_impl.err(f"REPAIR_ITERATIONS must be non-negative (got: {repair_iterations})")
        sys.exit(2)

    if _use_cli_value(ctx, "prd"):
        prd_path = _resolve_path(root_dir, prd, kstrl_dir / "prd.json")
    elif env_prd is not None:
        prd_path = _resolve_path(root_dir, env_prd, kstrl_dir / "prd.json")
    else:
        prd_path = None
    if prd_path is None:
        ui_impl.err("Feature PRD is required. Use --prd or PRD_FILE.")
        sys.exit(2)

    if not prd_path.exists():
        ui_impl.err(f"Feature PRD not found: {prd_path}")
        sys.exit(1)

    try:
        prd_doc = PRD.load(prd_path)
    except Exception as exc:
        ui_impl.err(f"Invalid PRD: {exc}")
        sys.exit(1)

    feature_name = _derive_feature_name(prd_path, root_dir)
    if not feature_name:
        ui_impl.err("Unable to determine feature name from PRD path.")
        sys.exit(2)

    feature_dir = kstrl_dir / "feature" / feature_name
    feature_dir.mkdir(parents=True, exist_ok=True)
    feature_understand = feature_dir / "understand.md"
    if not feature_understand.exists():
        feature_understand.write_text(DEFAULT_FEATURE_UNDERSTAND)

    log_dir = root_dir / ".kstrl" / "logs" / f"feature_{feature_name}"

    # R2.4 preflight: accept whichever agent the resolved config selects.
    _check_agent_preflight(base_config, ui_impl)

    sandbox_cfg = SandboxConfig.load(root_dir)
    if sandbox_cfg.enabled and base_config.agent_cmd:
        ui_impl.warn(
            "[sandbox] enabled but the agent is a custom command; sandbox "
            "settings cannot be applied to it and are ignored"
        )
    agent = get_agent(
        base_config.agent_cmd,
        base_config.model,
        base_config.model_reasoning_effort,
        base_config.agent_type,
        sandbox=sandbox_cfg,
        max_budget_usd=base_config.agent_budget_usd,
    )

    # ``understand_prompt_file`` is the OVERRIDE feature_cmd applies, so
    # None means "leave the understand loop on the resolved config".
    # ``understand_reads`` is the file that loop will actually open,
    # which is what the #286 preflight has to be pointed at; they differ
    # in exactly the PROMPT_FILE case.
    if _use_cli_value(ctx, "understand_prompt"):
        understand_prompt_file: Path | None = _resolve_path(
            root_dir, understand_prompt, kstrl_dir / "feature_understand_prompt.md"
        )
        understand_reads = understand_prompt_file
    elif "PROMPT_FILE" not in os.environ:
        understand_prompt_file = kstrl_dir / "feature_understand_prompt.md"
        understand_reads = understand_prompt_file
    else:
        understand_prompt_file = None
        understand_reads = base_config.prompt_file

    # #286: the two prompts this command actually opens, and only those.
    #
    # The engineer prompt is the scaffolded file rather than
    # ``base_config.prompt_file``: `ks feature` has always run its
    # implement and repair loops on `scripts/kstrl/prompt.md`, ignoring
    # [paths] prompt and PROMPT_FILE. Warning about the resolved path
    # would name a file the command never opens and stay silent about
    # the stale one it does. That was a literal in feature_cmd matching
    # a literal here; it is now passed on FeatureParams below, so the
    # path warned about and the path read cannot drift apart.
    #
    # ``understand_reads`` is resolved above: checking the engineer path
    # alone lost the PROMPT_FILE case, where the understand loop stays on
    # the resolved config because feature_cmd only overrides it for a
    # non-None ``understand_prompt_file``.
    engineer_prompt_file = kstrl_dir / "prompt.md"
    _check_prompt_preflight(engineer_prompt_file, ui_impl)
    _check_prompt_preflight(understand_reads, ui_impl)

    params = FeatureParams(
        prd_path=prd_path,
        prd_doc=prd_doc,
        feature_name=feature_name,
        feature_dir=feature_dir,
        feature_understand=feature_understand,
        log_dir=log_dir,
        understand_iterations=understand_iterations_value,
        understand_prompt_file=understand_prompt_file,
        prompt_file=engineer_prompt_file,
        implementation_auto_run=implementation_auto_run,
        no_verify=no_verify,
        repair_max_runs=repair_max_runs,
        repair_iterations=repair_iterations,
        repair_agent_cmd=repair_agent_cmd,
        branch_override=branch if _use_cli_value(ctx, "branch") else None,
        allowed_paths_override=(
            _parse_paths(implementation_allowed_paths)
            if _use_cli_value(ctx, "implementation_allowed_paths")
            else None
        ),
        sandbox=sandbox_cfg,
    )

    use_tui = (
        tui
        if tui is not None
        else (
            sys.stdout.isatty()
            and sys.stdin.isatty()
            and os.environ.get("KSTRL_NO_TUI") != "1"
            and base_config.ui_mode != "plain"
        )
    )
    if use_tui:
        if not (sys.stdout.isatty() and sys.stdin.isatty()):
            click.echo(
                "--tui requires an interactive terminal; use --no-tui "
                "for non-interactive execution.",
                err=True,
            )
            sys.exit(2)
        from kstrl.runid import mint_run_id
        from kstrl.tui.embed import EmbeddedContext, run_embedded
        from kstrl.tui.screens.component import ComponentScreen
        from kstrl.tui.screens.overview import OverviewScreen

        def _target(embed_ctx: EmbeddedContext) -> int:
            command_run = open_command_run(
                embed_ctx.ui,
                root_dir,
                "feature",
                component=feature_name,
                run_id=embed_ctx.run_id,
            )
            try:
                return run_feature(
                    params,
                    base_config,
                    agent,
                    embed_ctx.ui,
                    root_dir,
                    interaction=embed_ctx.channel,
                    run=command_run,
                    stop_check=embed_ctx.stop.is_set,
                )
            finally:
                command_run.close()

        sys.exit(
            run_embedded(
                _target,
                root_dir=root_dir,
                run_id=mint_run_id("feature"),
                screen_factory=lambda: [
                    OverviewScreen(observe_only=False),
                    ComponentScreen(feature_name),
                ],
            )
        )

    command_run = open_command_run(
        ui_impl,
        root_dir,
        "feature",
        component=feature_name,
    )
    try:
        code = run_feature(
            params,
            base_config,
            agent,
            ui_impl,
            root_dir,
            run=command_run,
        )
    finally:
        command_run.close()
    sys.exit(code)


@cli.command()
@click.option(
    "--spec",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help=(
        "Markdown spec file, or a SpecKit artifact directory "
        "(spec.md [+ plan.md] [+ tasks.md]) to decompose"
    ),
)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--project-name",
    required=True,
    callback=_reject_blank_project_name,
    help="Name for this factory project",
)
@click.option(
    "--base-branch",
    default=None,
    help="Base git branch (default: auto-detected from the repository)",
)
@click.option(
    "--single-pr",
    is_flag=True,
    help="Use a single branch for all components",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model",
    "-m",
    help="Model for the agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--agent-type",
    type=click.Choice(["auto", "claude-code", "claude-sdk", "codex"]),
    default="auto",
    help="Agent type",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
@click.option(
    "--tui/--no-tui",
    "tui",
    default=None,
    help="Embedded dashboard (default: auto - on when stdin/stdout are "
    "TTYs and --ui is not plain; KSTRL_NO_TUI=1 forces off)",
)
def decompose(
    spec: Path,
    root: Path | None,
    project_name: str,
    base_branch: str | None,
    single_pr: bool,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str,
    ui: str,
    no_color: bool,
    tui: bool | None,
) -> None:
    """Decompose a spec into components and generate PRDs."""
    ctx = click.get_current_context()

    root_dir = root.resolve() if root else Path.cwd()

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    effective_cmd = agent_cmd or os.environ.get("AGENT_CMD")
    effective_model = model if _use_cli_value(ctx, "model") else os.environ.get("MODEL")
    effective_reasoning = (
        reasoning if _use_cli_value(ctx, "reasoning") else os.environ.get("MODEL_REASONING_EFFORT")
    )
    effective_type = (
        agent_type
        if _use_cli_value(ctx, "agent_type")
        else os.environ.get("KSTRL_AGENT_TYPE", "auto")
    )

    # R2.4 mirror (measured 2026-07-20): canonicalize aliases like
    # "claude" before get_agent, whose unrecognized-type fallthrough is
    # codex; the preflight also covers the no-agent-available check.
    canonical_type, type_error, type_hint = _agent_preflight(
        effective_cmd,
        effective_type,
    )
    if type_error:
        ui_impl.err(type_error)
        if type_hint:
            ui_impl.info(type_hint)
        sys.exit(1)
    effective_type = canonical_type or effective_type

    agent = get_agent(effective_cmd, effective_model, effective_reasoning, effective_type)

    # decompose_spec takes a `str` base and runs it through
    # validate_branch_name, so the flag's None is resolved here rather
    # than deeper, the way `ks sense` already resolves --base (#259).
    effective_base = resolve_base_branch(base_branch, root_dir)

    def _decompose_core(core_ui: UI, command_run: CommandRun) -> int:
        try:
            manifest = decompose_spec(
                spec_path=spec,
                project_name=project_name,
                base_branch=effective_base,
                single_pr=single_pr,
                agent=agent,
                ui=core_ui,
                root_dir=root_dir,
                bus=command_run.bus,
                transcript=command_run.transcript_writer(ARCHITECT_COMPONENT),
            )
            core_ui.ok(f"Decomposed into {len(manifest.components)} components")
            return 0
        except SpecBlockerError as exc:
            core_ui.err(str(exc))
            # R1.7: point at the durable artifacts so the user iterates
            # against files, not scrollback. Plural, because after #260
            # the audit holds the finding and the register holds the
            # question the owner has to answer.
            for line in exc.artifact_lines():
                core_ui.info(line)
            return 2
        except ValueError as exc:
            core_ui.err(str(exc))
            return 1

    use_tui = (
        tui
        if tui is not None
        else (
            sys.stdout.isatty()
            and sys.stdin.isatty()
            and os.environ.get("KSTRL_NO_TUI") != "1"
            and _normalize_ui_mode(ui) != "plain"
        )
    )
    if use_tui:
        if not (sys.stdout.isatty() and sys.stdin.isatty()):
            click.echo(
                "--tui requires an interactive terminal; use --no-tui "
                "for non-interactive execution.",
                err=True,
            )
            sys.exit(2)
        from kstrl.runid import mint_run_id
        from kstrl.tui.dispatch import initial_screens_for_kind
        from kstrl.tui.embed import EmbeddedContext, run_embedded

        def _target(embed_ctx: EmbeddedContext) -> int:
            command_run = open_command_run(
                embed_ctx.ui,
                root_dir,
                "decompose",
                component=ARCHITECT_COMPONENT,
                run_id=embed_ctx.run_id,
            )
            try:
                return _decompose_core(embed_ctx.ui, command_run)
            finally:
                command_run.close()

        sys.exit(
            run_embedded(
                _target,
                root_dir=root_dir,
                run_id=mint_run_id("decompose"),
                screen_factory=initial_screens_for_kind(
                    "decompose",
                    observe_only=False,
                ),
            )
        )

    command_run = open_command_run(
        ui_impl,
        root_dir,
        "decompose",
        component=ARCHITECT_COMPONENT,
    )
    try:
        code = _decompose_core(ui_impl, command_run)
    finally:
        command_run.close()
    sys.exit(code)


@cli.command()
@click.option(
    "--spec",
    type=click.Path(exists=True, path_type=Path),
    help=("Markdown spec file or SpecKit artifact directory (runs decompose first)"),
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, path_type=Path),
    help="Existing manifest file (skip decompose)",
)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--project-name",
    callback=_reject_blank_project_name,
    help="Name for this factory project (required with --spec)",
)
@click.option(
    "--base-branch",
    default=None,
    help="Base git branch (default: auto-detected from the repository)",
)
@click.option(
    "--single-pr",
    is_flag=True,
    help="Use a single branch/PR for all components",
)
@click.option(
    "--max-parallel",
    type=int,
    default=None,
    help="Maximum parallel components (default: 4)",
)
@click.option(
    "--max-retries",
    type=int,
    default=None,
    help="Maximum retries per component (default: 3)",
)
@click.option(
    "--create-prs/--no-prs",
    default=None,
    help="Create PRs for completed components (default: on)",
)
@click.option(
    "--verify-command",
    help="Legacy: single verify command (prefer --test-command etc.)",
)
@click.option(
    "--test-command",
    help=f"Test suite command (default: {DEFAULT_TEST_COMMAND!r})",
)
@click.option(
    "--typecheck-command",
    help="Typecheck command (default: 'uv run mypy', or 'uv run mypy .' "
    "when pyproject.toml does not scope mypy itself)",
)
@click.option(
    "--lint-command",
    help=f"Lint command (default: {DEFAULT_LINT_COMMAND!r})",
)
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip Phase 1 mechanical verification",
)
@click.option(
    "--dead-code-cleanup",
    is_flag=True,
    default=None,
    help=(
        "Enable dead code cleanup: ruff auto-fixes unused "
        "imports/variables, vulture detects remaining dead code"
    ),
)
@click.option(
    "--dead-code-command",
    help="Custom dead code detection command (default: vulture on changed files)",
)
@click.option(
    "--mutation-testing",
    is_flag=True,
    default=None,
    help="Enable mutation testing (requires mutmut, off by default)",
)
@click.option(
    "--mutation-threshold",
    type=float,
    default=None,
    help="Mutation score threshold percent (default: 50)",
)
@click.option(
    "--review-mode",
    type=click.Choice(["hard", "advisory", "skip"]),
    default=None,
    help="Phase 2 review: hard (block), advisory (warn), skip (default: hard)",
)
@click.option(
    "--review-agent-cmd",
    help="Custom agent for reviewer (default: same as implementation agent)",
)
@click.option(
    "--review-model",
    help="Model for reviewer agent",
)
@click.option(
    "--security-mode",
    type=click.Choice(["hard", "advisory", "skip"]),
    default=None,
    help="Phase 2.5 security review: hard (block on critical+high), "
    "advisory (warn only), skip (default - opt in explicitly)",
)
@click.option(
    "--security-agent-cmd",
    help="Custom agent for security reviewer (default: same as implementation agent)",
)
@click.option(
    "--security-model",
    help="Model for security reviewer agent (default: same as implementation agent)",
)
@click.option(
    "--security-fail-threshold",
    type=click.Choice(["critical", "high", "medium", "low"]),
    default=None,
    help="In hard mode, findings at or above this severity block "
    "(default: high - critical+high fail)",
)
@click.option(
    "--contract-check",
    type=click.Choice(["tier", "final", "skip"]),
    default=None,
    help="Phase 3 contract testing: tier (per-tier), final (end-only), skip (default: tier)",
)
@click.option(
    "--contract-test-cmd",
    help="Test command for contract testing (default: same as --test-command)",
)
@click.option(
    "--agent-timeout",
    type=float,
    default=None,
    help="Timeout per agent iteration in seconds; 0 disables "
    "(default: 1800, or KSTRL_TIMEOUT_AGENT_ITERATION / "
    "[timeout].agent_iteration in kstrl.toml)",
)
@click.option(
    "--component-timeout",
    type=float,
    default=None,
    help="Timeout per component total in seconds; 0 disables "
    "(default: 7200, or KSTRL_TIMEOUT_COMPONENT / "
    "[timeout].component_total in kstrl.toml)",
)
@click.option(
    "--max-adversarial-calls",
    type=int,
    default=None,
    help="Hard cap on adversarial LLM calls (review + security + "
    "distill) per run; 0 = unbounded (default: 0, or "
    "KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS / "
    "[factory].max_adversarial_calls in kstrl.toml)",
)
@click.option(
    "--max-total-tokens",
    type=int,
    default=None,
    help="Run-level token budget across ALL phases (engineer + review "
    "+ security + distill); 0 = unbounded. Counts CACHE READS at "
    "par, so it is a poor proxy for spend - prefer --max-cost-usd. "
    "On breach the current component fails with a synthetic budget "
    "finding and pending components halt (default: 0, or "
    "KSTRL_FACTORY_MAX_TOTAL_TOKENS "
    "/ [factory].max_total_tokens in kstrl.toml)",
)
@click.option(
    "--max-cost-usd",
    type=float,
    default=None,
    help="Run-level USD budget across ALL phases; 0 = unbounded. "
    "Checked between engineer iterations and at phase boundaries, "
    "so it is NOT a hard cap: the iteration already in flight is "
    "unbounded. Distinct from [agent] budget_usd, which is "
    "adapter-internal (claude-sdk only). Default: 0, or "
    "KSTRL_FACTORY_MAX_COST_USD / [factory].max_cost_usd in "
    "kstrl.toml",
)
@click.option(
    "--pause-before-pr-merge/--no-pause-before-pr-merge",
    default=None,
    help="Pause for human approval before each component's PR "
    "push+merge (default: off, or "
    "KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE / "
    "[factory].pause_before_pr_merge in kstrl.toml)",
)
@click.option(
    "--progress-log",
    type=click.Path(path_type=Path),
    help="Path for the JSONL progress log (default: .kstrl/progress.jsonl; "
    "the log is on by default, disable via "
    "[factory].progress_log_enabled = false or "
    "KSTRL_FACTORY_PROGRESS_LOG_ENABLED=0)",
)
@click.option(
    "--no-worktrees",
    is_flag=True,
    help="Disable git worktrees (forces sequential execution)",
)
@click.option(
    "--keep-worktrees-on-failure",
    is_flag=True,
    help="Keep a failed component's worktree for post-mortem instead of "
    "removing it at cleanup; the failure summary points at it "
    "(default: off, or KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE / "
    "[factory].keep_worktrees_on_failure in kstrl.toml)",
)
@click.option(
    "--force-lock",
    is_flag=True,
    help="Proceed even if another kstrl invocation holds "
    ".kstrl/factory.lock (may corrupt the other run's state)",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model",
    "-m",
    help="Model for the agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--agent-type",
    type=click.Choice(["auto", "claude-code", "claude-sdk", "codex"]),
    default="auto",
    help="Agent type",
)
@click.option(
    "--sleep",
    "-s",
    type=float,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
@click.option(
    "--tui/--no-tui",
    "tui",
    default=None,
    help="Embedded dashboard (default: auto - on when stdin/stdout are "
    "TTYs and --ui is not plain; KSTRL_NO_TUI=1 forces off)",
)
def factory(
    tui: bool | None,
    spec: Path | None,
    manifest_path: Path | None,
    root: Path | None,
    project_name: str | None,
    base_branch: str | None,
    single_pr: bool,
    max_parallel: int | None,
    max_retries: int | None,
    create_prs: bool | None,
    verify_command: str | None,
    test_command: str | None,
    typecheck_command: str | None,
    lint_command: str | None,
    no_verify: bool,
    dead_code_cleanup: bool | None,
    dead_code_command: str | None,
    mutation_testing: bool | None,
    mutation_threshold: float | None,
    review_mode: str | None,
    review_agent_cmd: str | None,
    review_model: str | None,
    security_mode: str | None,
    security_agent_cmd: str | None,
    security_model: str | None,
    security_fail_threshold: str | None,
    contract_check: str | None,
    contract_test_cmd: str | None,
    agent_timeout: float | None,
    component_timeout: float | None,
    max_adversarial_calls: int | None,
    max_total_tokens: int | None,
    max_cost_usd: float | None,
    pause_before_pr_merge: bool | None,
    progress_log: Path | None,
    no_worktrees: bool,
    keep_worktrees_on_failure: bool,
    force_lock: bool,
    yes: bool,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str,
    sleep: float,
    ui: str,
    no_color: bool,
) -> None:
    """Run the software factory - decompose and execute a spec.

    Provide either --spec (to decompose first) or --manifest (to resume).
    """
    ctx = click.get_current_context()

    if not spec and not manifest_path:
        ui_impl = _console_ui("auto", no_color)
        ui_impl.err("Either --spec or --manifest is required")
        sys.exit(2)

    root_dir = root.resolve() if root else Path.cwd()

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    effective_cmd = agent_cmd or os.environ.get("AGENT_CMD")
    effective_model = model if _use_cli_value(ctx, "model") else os.environ.get("MODEL")
    effective_reasoning = (
        reasoning if _use_cli_value(ctx, "reasoning") else os.environ.get("MODEL_REASONING_EFFORT")
    )
    effective_type = (
        agent_type
        if _use_cli_value(ctx, "agent_type")
        else os.environ.get("KSTRL_AGENT_TYPE", "auto")
    )

    # R2.4 mirror (measured 2026-07-20): canonicalize aliases like
    # "claude" before get_agent, whose unrecognized-type fallthrough is
    # codex; the preflight also covers the no-agent-available check.
    canonical_type, type_error, type_hint = _agent_preflight(
        effective_cmd,
        effective_type,
    )
    if type_error:
        ui_impl.err(type_error)
        if type_hint:
            ui_impl.info(type_hint)
        sys.exit(1)
    effective_type = canonical_type or effective_type

    agent = get_agent(effective_cmd, effective_model, effective_reasoning, effective_type)

    # R8 review (#180): budget preflight, deliberately next to the agent
    # preflight above. The full config is not built until AFTER the
    # decompose call below, so an unbounding ceiling used to be caught
    # only once the architect had already spent - the operator paid for
    # a call under the very ceiling that was supposed to bound it.
    #
    # Both sources are checked, because they fail independently: the
    # file/env value (validated inside load) and the two flags, which
    # reach run_factory without passing any config loader. The flags are
    # validated but NOT applied here - applying them early would change
    # what _collect_toml_notes reports as overridden further down.
    factory_config = FactoryConfig.load(root_dir)
    if max_cost_usd is not None:
        validate_cost_ceiling(max_cost_usd, "--max-cost-usd")
    if max_total_tokens is not None:
        validate_token_ceiling(max_total_tokens, "--max-total-tokens")

    # Get or create manifest.
    #
    # #257: a --spec run also pays for the architect here, before any run
    # id or run directory exists, so what it cost has to be carried into
    # the run by hand for `--max-cost-usd` to bound five roles instead of
    # four. A --manifest resume ran no architect and leaves this empty.
    #
    # Only the paths that reach run_factory are covered: a blocker halt
    # exits below, before there is anywhere on disk to record it.
    # `decompose_spec` prints the number to the terminal on that path.
    architect_usage = UsageTotals()
    if manifest_path:
        try:
            manifest = Manifest.load(manifest_path)
        except Exception as exc:
            ui_impl.err(f"Failed to load manifest: {exc}")
            sys.exit(1)
    else:
        assert spec is not None
        if not project_name:
            ui_impl.err("--project-name is required with --spec")
            sys.exit(2)

        # Read BEFORE the work, and sliced from there afterwards, the
        # same way `decompose_spec` reports it - so the two derivations
        # of "what the architect spent" cannot disagree, and neither
        # rests on an invariant about who else touched this agent.
        usage_before = usage_cursor(agent)
        try:
            manifest = decompose_spec(
                spec_path=spec,
                project_name=project_name,
                # The --manifest path above is untouched: a manifest
                # already carries its own base_branch. Only the --spec
                # path has a flag to resolve, and detection is the
                # default rather than the literal "main" (#259).
                base_branch=resolve_base_branch(base_branch, root_dir),
                single_pr=single_pr,
                agent=agent,
                ui=ui_impl,
                root_dir=root_dir,
            )
        except SpecBlockerError as exc:
            # Architect halted: it escalated a question only the owner
            # can answer (#260). Surface it and exit cleanly. The user
            # answers, edits the spec and re-runs, iterating against the
            # persisted artifacts (R1.7).
            ui_impl.err(str(exc))
            for line in exc.artifact_lines():
                ui_impl.info(line)
            sys.exit(2)
        except ValueError as exc:
            ui_impl.err(str(exc))
            sys.exit(1)
        architect_usage = collect_usage(agent, since=usage_before)

    # Build configs (R2.1). Resolution order for every phase config:
    # explicit CLI flag > env > kstrl.toml > dataclass default. The
    # loaders handle env-over-toml-over-default; flags use None
    # sentinels so "not passed" is distinguishable from "passed the
    # default value", and an explicitly-passed flag is applied on top.
    from kstrl.contract import ContractConfig
    from kstrl.feedforward import FeedforwardConfig
    from kstrl.security import SecurityConfig
    from kstrl.verify import VerifyConfig

    toml_notes: list[str] = []

    # factory_config was loaded in the budget preflight above.
    _collect_toml_notes(
        toml_notes,
        "factory",
        factory_config,
        FactoryConfig.from_env(),
        flag_overridden={
            name
            for name, passed in (
                ("max_parallel", max_parallel is not None),
                ("max_retries", max_retries is not None),
                ("create_prs", create_prs is not None),
                ("review_mode", review_mode is not None),
                ("max_adversarial_calls", max_adversarial_calls is not None),
                ("max_total_tokens", max_total_tokens is not None),
                ("max_cost_usd", max_cost_usd is not None),
                ("pause_before_pr_merge", pause_before_pr_merge is not None),
                ("use_worktrees", no_worktrees),
                ("keep_worktrees_on_failure", keep_worktrees_on_failure),
                # The manifest is authoritative for single_pr, so a toml
                # value never becomes effective in this command.
                ("single_pr", True),
            )
            if passed
        },
    )
    if max_parallel is not None:
        factory_config.max_parallel = max_parallel
    if max_retries is not None:
        factory_config.max_retries = max_retries
    if create_prs is not None:
        factory_config.create_prs = create_prs
    if review_mode is not None:
        factory_config.review_mode = review_mode
    if max_adversarial_calls is not None:
        factory_config.max_adversarial_calls = max_adversarial_calls
    if max_total_tokens is not None:
        factory_config.max_total_tokens = max_total_tokens
    if max_cost_usd is not None:
        factory_config.max_cost_usd = max_cost_usd
    if pause_before_pr_merge is not None:
        factory_config.pause_before_pr_merge = pause_before_pr_merge
    if no_worktrees:
        factory_config.use_worktrees = False
    if keep_worktrees_on_failure:
        factory_config.keep_worktrees_on_failure = True
    factory_config.single_pr = manifest.single_pr
    factory_config.verify_command = verify_command
    factory_config.review_agent_cmd = review_agent_cmd
    factory_config.review_model = review_model
    factory_config.progress_log_path = progress_log
    if progress_log is not None:
        # An explicit --progress-log path is an explicit opt-in; it wins
        # over a toml/env progress_log_enabled = false.
        factory_config.progress_log_enabled = True
    factory_config.force_lock = force_lock
    # R2.3: --no-verify is an explicit skip sentinel that run_factory
    # honors; verify_config=None alone would substitute default checks.
    factory_config.skip_verification = no_verify

    v_config: VerifyConfig | None = None
    if not no_verify:
        v_config = VerifyConfig.load(root_dir)
        _collect_toml_notes(
            toml_notes,
            "verify",
            v_config,
            VerifyConfig.from_env(),
            flag_overridden={
                name
                for name, passed in (
                    ("test_command", test_command is not None),
                    ("typecheck_command", typecheck_command is not None),
                    ("lint_command", lint_command is not None),
                    ("dead_code_cleanup", dead_code_cleanup is not None),
                    ("dead_code_command", dead_code_command is not None),
                    ("mutation_testing", mutation_testing is not None),
                    ("mutation_threshold", mutation_threshold is not None),
                )
                if passed
            },
        )
        if test_command is not None:
            v_config.test_command = test_command
        if typecheck_command is not None:
            v_config.typecheck_command = typecheck_command
        if lint_command is not None:
            v_config.lint_command = lint_command
        if dead_code_cleanup is not None:
            v_config.dead_code_cleanup = dead_code_cleanup
        if dead_code_command is not None:
            v_config.dead_code_command = dead_code_command
        if mutation_testing is not None:
            v_config.mutation_testing = mutation_testing
        if mutation_threshold is not None:
            v_config.mutation_threshold = mutation_threshold

    s_config = SecurityConfig.load(root_dir)
    _collect_toml_notes(
        toml_notes,
        "security",
        s_config,
        SecurityConfig.from_env(),
        flag_overridden={
            name
            for name, passed in (
                ("mode", security_mode is not None),
                ("agent_cmd", security_agent_cmd is not None),
                ("model", security_model is not None),
                ("fail_threshold", security_fail_threshold is not None),
            )
            if passed
        },
    )
    if security_mode is not None:
        s_config.mode = security_mode
    if security_agent_cmd is not None:
        s_config.agent_cmd = security_agent_cmd
    if security_model is not None:
        s_config.model = security_model
    if security_fail_threshold is not None:
        s_config.fail_threshold = security_fail_threshold

    # --test-command historically flowed through to contract testing
    # when --contract-test-cmd was absent; both are explicit CLI input,
    # so either beats env/toml.
    cli_contract_cmd = contract_test_cmd or test_command
    contract_resolved = ContractConfig.load(root_dir)
    _collect_toml_notes(
        toml_notes,
        "contract",
        contract_resolved,
        ContractConfig.from_env(),
        flag_overridden={
            name
            for name, passed in (
                ("mode", contract_check is not None),
                ("test_command", cli_contract_cmd is not None),
            )
            if passed
        },
    )
    if contract_check is not None:
        contract_resolved.mode = contract_check
    if cli_contract_cmd is not None:
        contract_resolved.test_command = cli_contract_cmd
    # mode == "skip" keeps the historical contract of passing no config.
    c_config: ContractConfig | None = (
        contract_resolved if contract_resolved.mode != "skip" else None
    )

    ff_config = FeedforwardConfig.load(root_dir)
    _collect_toml_notes(
        toml_notes,
        "feedforward",
        ff_config,
        FeedforwardConfig.from_env(),
        flag_overridden=set(),
    )

    # Evolution config is consumed inside run_factory via
    # EvolutionJournal.open; swept here only for the NOTE lines, and by
    # a helper because a failed load must not cost the run. The helper
    # says why this section and not its five raising siblings.
    _collect_evolution_notes(toml_notes, root_dir, ui_impl)

    # R0.1: TimeoutConfig is the single source for timeout values.
    timeout_config = TimeoutConfig.load(root_dir)
    _collect_toml_notes(
        toml_notes,
        "timeout",
        timeout_config,
        TimeoutConfig.from_env(),
        flag_overridden={
            name
            for name, passed in (
                ("agent_iteration", agent_timeout is not None),
                ("component_total", component_timeout is not None),
            )
            if passed
        },
    )
    if agent_timeout is not None:
        timeout_config.agent_iteration = agent_timeout
    if component_timeout is not None:
        timeout_config.component_total = component_timeout

    factory_config.verify_config = v_config
    factory_config.security_config = s_config
    factory_config.contract_config = c_config
    factory_config.feedforward_config = ff_config
    factory_config.timeout_config = timeout_config

    # Display summary and confirm (resolved values, not raw flags)
    ui_impl.section("Factory Plan")
    ui_impl.kv("Project", manifest.project_name)
    ui_impl.kv("Components", str(len(manifest.components)))
    ui_impl.kv("Base branch", manifest.base_branch)
    ui_impl.kv("Single PR", "yes" if manifest.single_pr else "no")
    ui_impl.kv("Max parallel", str(factory_config.max_parallel))
    ui_impl.kv("Create PRs", "yes" if factory_config.create_prs else "no")

    # R2.1 behavior change: kstrl.toml sections that used to be silently
    # ignored now take effect. Surface every value a toml section moved
    # away from the CLI default so existing setups see the change.
    for note in toml_notes:
        ui_impl.info(note)

    topo = manifest.topological_order()
    ui_impl.info("")
    ui_impl.info("Execution order:")
    for i, comp_id in enumerate(topo, 1):
        comp = manifest.get_component(comp_id)
        status = _format_component_status(comp.status if comp else None)
        dep_list = ", ".join(comp.dependencies) if comp and comp.dependencies else ""
        deps = f" (depends on: {dep_list})" if dep_list else ""
        ui_impl.info(f"  {i}. {comp_id} [{status}]{deps}")

    _factory_channel = UiInteractionChannel(ui_impl)
    if not yes and _factory_channel.can_prompt():
        response = _factory_channel.request(
            PromptRequest(
                kind=PromptKind.CONFIRM,
                header="Proceed with factory execution?",
                options=("Start", "Quit"),
                default=0,
            )
        )
        if response.answered and response.choice != 0:
            sys.exit(0)

    kstrl_dir = root_dir / "scripts" / "kstrl"
    base_config = KstrlConfig.load(root_dir)
    if _use_cli_value(ctx, "agent_cmd"):
        base_config.agent_cmd = agent_cmd
    if _use_cli_value(ctx, "model"):
        base_config.model = model
    if _use_cli_value(ctx, "reasoning"):
        base_config.model_reasoning_effort = reasoning
    if _use_cli_value(ctx, "agent_type"):
        base_config.agent_type = agent_type
    if _use_cli_value(ctx, "sleep"):
        base_config.sleep_seconds = sleep
    base_config.ui_mode = "plain"
    base_config.no_color = True

    # R2.4 mirror for the factory path (measured 2026-07-20 on the first
    # real factory run): without this, a toml alias like type = "claude"
    # reaches get_agent RAW in every engineer worker and silently falls
    # through to the codex default - and _cli_family misreads the
    # engineer family, inverting the R7.1 reviewer rotation.
    _check_agent_preflight(base_config, ui_impl)

    # --no-verify leaves no reader, so there is nothing to reconcile.
    if v_config is not None:
        # R8 review: the progress log's writer ([paths] progress) and its
        # reader ([verify] progress_file_path) default to the same
        # derivation, so they agree until exactly ONE is set - and then the
        # self-critique check inspects a file the engineer never wrote and
        # fails for a reason the operator cannot see from either setting.
        # The reconciliation lives in config.py (and is applied in ONE path
        # domain, review finding 3) so a test can exercise the same wiring
        # this command runs.
        mismatch = reconcile_progress_config(base_config, v_config, root_dir)
        if mismatch is not None:
            ui_impl.warn(mismatch)

    # Ensure prompt file exists
    if not base_config.prompt_file.exists():
        default_prompt = kstrl_dir / "prompt.md"
        if default_prompt.exists():
            base_config.prompt_file = default_prompt

    # #286: after the fallback above, so it speaks about the file every
    # worker will actually copy into its worktree.
    _check_prompt_preflight(base_config.prompt_file, ui_impl)

    # R0.5 (H-15): state saves back to the file it was loaded from.
    # --manifest /custom.json persists to /custom.json; --spec runs keep
    # the default scripts/kstrl/manifest.json that decompose wrote.
    use_tui = (
        tui
        if tui is not None
        else (
            sys.stdout.isatty()
            and sys.stdin.isatty()
            and os.environ.get("KSTRL_NO_TUI") != "1"
            and _normalize_ui_mode(ui) != "plain"
        )
    )
    if use_tui:
        if not (sys.stdout.isatty() and sys.stdin.isatty()):
            click.echo(
                "--tui requires an interactive terminal; use --no-tui "
                "for non-interactive execution.",
                err=True,
            )
            sys.exit(2)
        # PR F: embedded dashboard. The pre-execution confirm already
        # happened on the plain terminal (plan decision: no
        # modal-before-app); everything from here renders in Textual.
        from kstrl.tui.embed import run_factory_embedded

        sys.exit(
            run_factory_embedded(
                manifest,
                factory_config,
                base_config,
                root_dir,
                manifest_path,
                architect_usage=architect_usage,
            )
        )

    stop = StopController()
    uninstall = install_signal_handlers(stop)
    try:
        result = run_factory(
            manifest,
            factory_config,
            base_config,
            ui_impl,
            root_dir,
            manifest_path=manifest_path,
            stop=stop,
            architect_usage=architect_usage,
        )
    finally:
        uninstall()
    sys.exit(result.exit_code)


# Display structure for the KstrlConfig-backed kstrl.toml sections:
# section -> [(toml_key, dataclass_field)]. Mirrors DEFAULT_KSTRL_TOML in
# init_cmd.py plus the env/flag-only UI knobs (ui_mode, no_color).
@cli.group(name="config")
def config_group() -> None:
    """Inspect kstrl configuration."""


@config_group.command(name="show")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option("--max-iterations", type=int, help="Override [run] max_iterations")
@click.option("--prompt", "-p", type=str, help="Override [paths] prompt")
@click.option("--prd", type=str, help="Override [paths] prd")
@click.option("--sleep", "-s", type=float, help="Override [run] sleep_seconds")
@click.option("--interactive", "-i", is_flag=True, help="Override [run] interactive")
@click.option("--allowed-paths", help="Override [paths] allowed")
@click.option("--branch", help="Override [git] branch")
@click.option("--agent-cmd", help="Override [agent] command")
@click.option("--model", "-m", help="Override [agent] model")
@click.option("--reasoning", help="Override [agent] reasoning_effort")
@click.option("--agent-type", help="Override [agent] type")
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="Override [ui] ui_mode",
)
@click.option("--no-color", is_flag=True, help="Override [ui] no_color")
@click.option("--ascii", is_flag=True, help="Override [ui] ascii")
def config_show(
    root: Path | None,
    max_iterations: int | None,
    prompt: str | None,
    prd: str | None,
    sleep: float | None,
    interactive: bool,
    allowed_paths: str | None,
    branch: str | None,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str | None,
    ui: str,
    no_color: bool,
    ascii: bool,
) -> None:
    """Print the fully resolved config with the source of each value.

    Every value is tagged (flag), (env), (toml), or (default). Flags
    mirror `ks run`'s config-affecting options, so the output is what
    a run invoked with the same flags would execute. Factory-phase
    sections (factory/verify/security/...) have no flags here; their
    values resolve from env > kstrl.toml > defaults.

    Source detection for env is behavioral: a value is tagged (env) when
    removing the environment changes it. An env var that sets a value
    identical to the toml/default value is therefore reported as the
    lower-precedence source; the effective value is identical either way.
    """
    ctx = click.get_current_context()
    root_dir = root.resolve() if root else Path.cwd()

    def _overlay(config: KstrlConfig) -> set[str]:
        return _apply_cli_overrides(
            ctx,
            config,
            root_dir,
            prompt_default=root_dir / "scripts/kstrl/prompt.md",
            prd_default=root_dir / "scripts/kstrl/prd.json",
        )

    from kstrl.config_preflight import (
        SURFACE_REJECTIONS,
        config_problem_lines,
        raise_if_defect,
    )

    try:
        report = build_config_report(root_dir, overlay=_overlay)
    except SURFACE_REJECTIONS as exc:
        # No rows are possible: the base config itself was rejected, or
        # the document will not parse. Report it in the same shape as the
        # success-with-problems path below, and in the seam's words -
        # section, key, offending value - rather than the bare coercion
        # message this used to print.
        #
        # The tuple is IMPORTED, and `config` being preflight-exempt is
        # exactly why it has to be: no entry seam catches this first.
        # `except ValueError` here let `[run] max_iterations = ["3"]`
        # out as a raw TypeError traceback, from the one command whose
        # whole job is explaining a broken config (#289).
        #
        # Except when the RuntimeError is one kstrl never defined: that
        # is our defect, and printing it under "configuration problems"
        # both blames the operator and loses the traceback.
        raise_if_defect(exc)
        _echo_config_problems(config_problem_lines(root_dir, warn=_preflight_warn) or [str(exc)])
        sys.exit(1)

    toml_path = report.toml_path
    click.echo(f"# Resolved kstrl config for {root_dir}")
    click.echo(f"# kstrl.toml: {toml_path if report.toml_exists else '(absent)'}")
    click.echo("")

    section = ""
    for row in report.rows:
        if row.section != section:
            if section:
                click.echo("")
            section = row.section
            click.echo(f"[{section}]")
        click.echo(f"  {row.key} = {row.value}  ({row.source})")
    click.echo("")

    # Every command refuses on an unusable section, so ONE command has to
    # always run and always explain: this one. The rows above cover what
    # resolved (a rejected section costs its rows, not the report); the
    # problems below cover every section, the eleven this report does not
    # render included, in the words the rest of the CLI uses.
    problems = config_problem_lines(root_dir, warn=_preflight_warn)
    if problems:
        _echo_config_problems(problems)
        sys.exit(1)

    sys.exit(0)


def _age_label(ts: str) -> str:
    """ "5m ago" for an event timestamp, or "" when unparseable."""
    age = event_age_seconds(ts)
    if age is None:
        return ""
    return f"{format_age(age)} ago"


def _age_label_epoch(ts: float) -> str:
    """ "5m ago" for a float epoch timestamp (reducer times), or ""."""
    if ts <= 0:
        return ""
    age = max(0.0, time.time() - ts)
    return f"{format_age(age)} ago"


def _render_safe_mode(ui_impl: UI, root_dir: Path) -> None:
    """Print the safe-mode block: nominal, or one line per reason.

    Shared by `ks status` and `ks serve --dry-run` so the two surfaces
    cannot word the same state differently. A REPORT, never a gate:
    safe mode refuses nothing by itself, because every signal it reads
    already refuses where refusing is right.

    ``info`` rather than ``warn`` for the reason lines: PlainUI prefixes
    warnings with "WARN: ", which would break the line format the
    runbook documents.
    """
    from kstrl.safemode import safe_mode_reasons

    reasons = safe_mode_reasons(root_dir)
    if not reasons:
        ui_impl.kv("safe mode", "nominal")
        return
    ui_impl.kv("safe mode", f"{len(reasons)} reason(s)")
    for reason in reasons:
        ui_impl.info(f"  - [{reason.source}] {reason.detail} (see {reason.recovery})")


def _render_status(
    manifest: Manifest,
    manifest_file: Path,
    ui_impl: UI,
    state: RunState | None = None,
    source_path: Path | None = None,
    root_dir: Path | None = None,
) -> None:
    """Render the per-component status view from a manifest.

    ``state`` is the reducer's RunState joined onto the same
    per-component skeleton (chunk 8): phase (authoritative under the v2
    layout, inferred for v1 logs), attempt, last-event age, usage
    totals, PR/checkpoint/heartbeat detail, and evidence paths.
    """
    ui_impl.section("ks status")
    ui_impl.kv("Project", manifest.project_name)
    ui_impl.kv("Manifest", str(manifest_file))
    ui_impl.kv("Base branch", manifest.base_branch)

    if state is not None and source_path is not None:
        label = "Events" if source_path.name == "events.jsonl" else "Progress log"
        ui_impl.kv(label, str(source_path))
        if state.run_id:
            ui_impl.kv("Run id", state.run_id)
        if state.last_event_ts:
            age = _age_label_epoch(state.last_event_ts)
            run_state = "finished" if state.finished else "in flight"
            ui_impl.kv(
                "Run state",
                f"{run_state} (last event {age})" if age else run_state,
            )
        if state.usage_calls:
            # Per axis (R8 review finding 1): the two totals can be
            # short by different amounts, and on the measured run only
            # the dollar one was.
            token_note = "+" if state.tokens_are_lower_bound else ""
            cost_note = "+" if state.cost_is_lower_bound else ""
            caps = []
            if state.max_total_tokens:
                caps.append(f"{state.max_total_tokens} token cap")
            if state.max_cost_usd:
                caps.append(f"${state.max_cost_usd} cost cap")
            ui_impl.kv(
                "Run usage",
                f"{state.total_tokens}{token_note} tokens, "
                f"${state.cost_usd:.4f}{cost_note}" + (f" of {', '.join(caps)}" if caps else ""),
            )
            for gap in state.coverage_gaps.values():
                # The ceiling's own words, uncovered magnitude in TOKENS.
                if gap.detail:
                    ui_impl.kv("  coverage", gap.detail)

    if root_dir is not None:
        _render_safe_mode(ui_impl, root_dir)

    counts: dict[str, int] = {}
    for comp in manifest.components:
        counts[comp.status] = counts.get(comp.status, 0) + 1
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    ui_impl.kv("Components", f"{len(manifest.components)} ({summary})" if summary else "0")

    for comp in manifest.components:
        ui_impl.info("")
        ui_impl.info(f"{comp.id}: {comp.status}")
        ui_impl.kv("  branch", comp.branch_name)
        ui_impl.kv("  retries", str(comp.retries))
        if comp.started_at:
            ui_impl.kv("  started_at", comp.started_at)
        if comp.completed_at:
            ui_impl.kv("  completed_at", comp.completed_at)

        comp_state: ComponentState | None = (
            state.components.get(comp.id) if state is not None else None
        )
        if comp.pr_url:
            pr_note = (
                f" ({comp_state.pr_state})"
                if comp_state is not None and comp_state.pr_state
                else ""
            )
            ui_impl.kv("  pr", f"{comp.pr_url}{pr_note}")
        elif comp_state is not None and comp_state.pr_url:
            ui_impl.kv(
                "  pr",
                f"{comp_state.pr_url} ({comp_state.pr_state})",
            )
        if comp.error:
            ui_impl.kv("  error", comp.error)

        if comp_state is not None:
            if comp_state.phase:
                ui_impl.kv("  phase", comp_state.phase)
            attempt = comp_state.attempt or comp.retries + 1
            ui_impl.kv("  attempt", str(attempt))
            if comp_state.last_event:
                age = _age_label_epoch(comp_state.last_event_ts)
                ui_impl.kv(
                    "  last event",
                    f"{comp_state.last_event} ({age})" if age else comp_state.last_event,
                )
            if comp_state.checkpoint_open:
                ui_impl.kv(
                    "  checkpoint",
                    f"{comp_state.checkpoint_open} awaiting decision",
                )
            if comp_state.last_heartbeat_ts and comp.status in ("running", "verifying"):
                ui_impl.kv(
                    "  worker",
                    f"last heartbeat {_age_label_epoch(comp_state.last_heartbeat_ts)}",
                )
            if comp_state.usage_calls:
                # Which AXIS is short, not just how many calls said
                # nothing (R8 review finding 1): a call reporting a cost
                # and no token count is not "unreported" but still
                # leaves the token total short.
                short = [
                    name
                    for name, is_short in (
                        ("tokens", comp_state.tokens_are_lower_bound),
                        ("cost", comp_state.cost_is_lower_bound),
                    )
                    if is_short
                ]
                detail = f"lower bound: {', '.join(short)}" if short else ""
                if comp_state.unreported_calls:
                    detail += f"; {comp_state.unreported_calls} call(s) unreported"
                note = f" ({detail})" if detail else ""
                ui_impl.kv(
                    "  usage",
                    f"{comp_state.total_tokens} tokens, "
                    f"${comp_state.cost_usd:.4f}, "
                    f"{comp_state.usage_calls} calls{note}",
                )
        # Evidence paths: whatever this run left on disk for the
        # component (worktree kept after a failure, adversarial raw
        # outputs under .kstrl/debug/).
        if root_dir is not None and state is not None and state.run_id:
            evidence = [
                path
                for path in (
                    root_dir / ".kstrl" / "worktrees" / state.run_id / comp.id,
                    root_dir / ".kstrl" / "debug" / state.run_id / comp.id,
                )
                if path.exists()
            ]
            for path in evidence:
                ui_impl.kv("  evidence", str(path))


@cli.command()
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--run-id",
    "run_id",
    help="Run to observe (unique prefix ok; default: newest run)",
)
@click.option(
    "--poll",
    type=click.FloatRange(min=0, min_open=True),
    default=0.2,
    help="Tail poll interval in seconds (default: 0.2, spike-measured)",
)
def dash(root: Path | None, run_id: str | None, poll: float) -> None:
    """Live dashboard over a factory run (observe-only).

    Tails .kstrl/runs/<run_id>/ - a run in flight in another terminal,
    or a finished one (post-mortem replay works by construction). This
    command never writes to the run; E6 checkpoints are answered where
    the factory runs.
    """
    import sys as _sys

    root_dir = root.resolve() if root else Path.cwd()
    if not (_sys.stdout.isatty() and _sys.stdin.isatty()):
        click.echo(
            "ks dash needs a terminal; use `ks status` for non-interactive output.",
            err=True,
        )
        _sys.exit(2)

    from kstrl.tui.runs import find_run, latest_run

    ref = find_run(root_dir, run_id) if run_id else latest_run(root_dir)
    if ref is None:
        click.echo(
            f"No run found under {root_dir / '.kstrl' / 'runs'}"
            + (f" matching '{run_id}'" if run_id else "")
            + ". Run `ks factory` first, or check --root.",
            err=True,
        )
        _sys.exit(1)

    from kstrl.tui.app import KstrlTuiApp, Mode
    from kstrl.tui.dispatch import initial_screens_for_kind

    app = KstrlTuiApp(
        run_dir=ref.run_dir,
        root_dir=root_dir,
        mode=Mode.DASH,
        poll_interval=poll,
        screen_factory=initial_screens_for_kind(
            ref.kind,
            observe_only=True,
        ),
    )
    try:
        code = app.run()
    finally:
        # Spike finding 2: belt-and-braces terminal restore for any
        # exit path Textual could not clean up after.
        _sys.stdout.write("\x1b[?1049l\x1b[?25h\x1b[0m")
        _sys.stdout.flush()
    _sys.exit(code or 0)


@cli.command()
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path),
    help="Manifest file (default: scripts/kstrl/manifest.json, falling "
    "back to scripts/kstrl/run-manifest.json)",
)
@click.option(
    "--progress-log",
    "progress_log_path",
    type=click.Path(path_type=Path),
    help="Progress log to join onto the manifest (default: <root>/.kstrl/progress.jsonl)",
)
@click.option(
    "--watch",
    is_flag=True,
    help="Re-render on an interval until interrupted",
)
@click.option(
    "--interval",
    type=float,
    default=5.0,
    help="Polling interval in seconds for --watch (default: 5)",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
@click.option(
    "--tui/--no-tui",
    "tui",
    default=None,
    help="Open the dashboard for the newest run (default: auto - on "
    "when stdin/stdout are TTYs, --ui is not plain, and --watch "
    "is not set; KSTRL_NO_TUI=1 forces off)",
)
def status(
    root: Path | None,
    manifest_path: Path | None,
    progress_log_path: Path | None,
    watch: bool,
    interval: float,
    ui: str,
    no_color: bool,
    tui: bool | None,
) -> None:
    """Show per-component status from the manifest + progress log.

    On a TTY this opens the dashboard for the newest run of any kind
    (post-mortem or live); the plain text report remains the contract
    for pipes/CI, --no-tui, --watch, and KSTRL_NO_TUI=1.

    R3.2 (plain report): joins the factory manifest with the
    ProgressLog (default .kstrl/progress.jsonl): per component status,
    retries, branch, timestamps, plus phase, attempt, last-event age,
    usage totals and evidence paths for the latest run found in the
    log. Works manifest-only when no log exists.
    """
    import time as _time

    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    use_tui = (
        tui
        if tui is not None
        else (
            sys.stdout.isatty()
            and sys.stdin.isatty()
            and os.environ.get("KSTRL_NO_TUI") != "1"
            and _normalize_ui_mode(ui) != "plain"
            and not watch
        )
    )
    if use_tui:
        from kstrl.tui.runs import latest_run

        ref = latest_run(root_dir)
        if ref is not None:
            from kstrl.tui.app import KstrlTuiApp, Mode
            from kstrl.tui.dispatch import initial_screens_for_kind

            app = KstrlTuiApp(
                run_dir=ref.run_dir,
                root_dir=root_dir,
                mode=Mode.DASH,
                screen_factory=initial_screens_for_kind(
                    ref.kind,
                    observe_only=True,
                ),
            )
            try:
                code = app.run()
            finally:
                sys.stdout.write("\x1b[?1049l\x1b[?25h\x1b[0m")
                sys.stdout.flush()
            sys.exit(code or 0)
        # No run dirs yet: fall through to the plain report, whose
        # missing-manifest guidance is the useful answer here.

    if manifest_path is not None:
        candidates = [manifest_path]
    else:
        # Factory runs persist to manifest.json; `ks run` persists to
        # run-manifest.json (R0.5, H-15). Prefer the factory manifest.
        candidates = [
            root_dir / "scripts" / "kstrl" / "manifest.json",
            root_dir / "scripts" / "kstrl" / "run-manifest.json",
        ]

    def _load_state(manifest: Manifest) -> tuple[RunState | None, Path | None]:
        """Chunk 8: the versioned reader.

        An explicit --progress-log pins the v1 arm on that file.
        Otherwise the reducer resolves the newest v2 run dir (preferring
        the manifest's recorded run when it still exists on disk) and
        falls back to v1 progress.jsonl up-conversion.
        """
        if progress_log_path is not None:
            raw = read_progress_events(progress_log_path)
            if not raw:
                return None, None
            rid = latest_run_id(raw)
            return (
                fold((upconvert_v1(e) for e in raw), run_id=rid),
                progress_log_path,
            )
        state, source = load_run_state(root_dir, manifest.run_id or "")
        if manifest.run_id and (source is None or not state.started_ts):
            # The recorded run left no stream (dir pruned, or a v1 log
            # that predates it): fall back to the newest stream rather
            # than rendering nothing.
            state, source = load_run_state(root_dir)
        if source is None:
            return None, None
        return state, source

    def _load_and_render() -> int:
        manifest_file = next((p for p in candidates if p.exists()), None)
        if manifest_file is None:
            looked = ", ".join(str(p) for p in candidates)
            ui_impl.err(f"No manifest found (looked for: {looked})")
            ui_impl.info("Run `ks factory` or `ks run` first, or pass --manifest.")
            # Safe mode does not depend on a manifest, and "nothing has
            # run here" is exactly when an operator wants to know whether
            # the factory is holding back. Withholding the answer on this
            # path would make the question unaskable on a repo that has
            # never completed a run - including this one (R10.4).
            _render_safe_mode(ui_impl, root_dir)
            return 1

        try:
            manifest = Manifest.load(manifest_file)
        except (OSError, ValueError) as exc:
            ui_impl.err(f"Failed to load manifest {manifest_file}: {exc}")
            # Same reason as the missing-manifest path above: the
            # predicate does not read the manifest, so a broken one must
            # not hide a paused queue.
            _render_safe_mode(ui_impl, root_dir)
            return 1

        state, source_path = _load_state(manifest)
        _render_status(
            manifest,
            manifest_file,
            ui_impl,
            state=state,
            source_path=source_path,
            root_dir=root_dir,
        )
        if source_path is not None and source_path.name == "events.jsonl" and not watch:
            ui_impl.info("")
            ui_impl.info("Dashboard: ks dash")
        return 0

    if not watch:
        sys.exit(_load_and_render())

    try:
        while True:
            click.clear()
            exit_code = _load_and_render()
            if exit_code != 0:
                sys.exit(exit_code)
            _time.sleep(max(0.5, interval))
    except KeyboardInterrupt:
        sys.exit(0)


#: 2 (#306): a ``not_measured`` array joined the document, and the
#: meaning of an absent ``mutation_testing`` row changed with it. Under
#: 1 that absence meant one thing, "turned off in kstrl.toml", because
#: an enabled mutation check emitted a row even when it had measured
#: nothing. Under 2 it also covers "asked for, measured nothing", and
#: ``not_measured`` is what tells the two apart. A v1 reader inferring
#: "absent means disabled" is wrong about a v2 document, which is why
#: this is a bump and not a silent addition.
#:
#: Scoped to that one check on purpose, because that is all v2
#: delivers. ``not_measured`` is not yet a complete index of every
#: check that did not run: ``check_dead_code`` still reports three
#: non-measurements as passing rows, and ``require_self_critique`` with
#: no ``progress_file_path`` and no PRD emits neither a row nor a gap.
#: Both predate this and both are follow-ups on #306; a reader must not
#: read an empty array as "everything enabled was measured".
SENSE_SCHEMA_VERSION = 2


def _sense_document(path: Path, base: str, result: VerificationResult) -> dict[str, Any]:
    """The ``ks sense --json`` document, at :data:`SENSE_SCHEMA_VERSION`.

    Its own function because it is a published contract and ``sense``
    is a 200-line command: a reader checking what v2 promises should not
    have to find it among the preflight, the base resolution and the
    terminal rendering.
    """
    return {
        "schema_version": SENSE_SCHEMA_VERSION,
        "path": str(path),
        "base_branch": base,
        "passed": result.passed,
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "message": check.message,
                "details": list(check.details),
                "duration_seconds": check.duration_seconds,
                "findings": [f.to_dict() for f in check.findings],
            }
            for check in result.checks
        ],
        # Beside ``checks``, never inside it: an entry here is a check
        # that ran no measurement, and putting it in the array a reader
        # folds with ``all(passed)`` is exactly the defect #306 closed.
        # Empty for a tree where every enabled check measured something.
        "not_measured": [gap.to_dict() for gap in result.not_measured],
    }


def _sense_error(message: str, as_json: bool) -> NoReturn:
    """Exit 2: the measurement itself could not run.

    One ``error:`` line on stderr always; with ``--json`` a one-key
    document on stdout so a pipe reading stdout sees the failure too.
    """
    click.echo(f"error: {message}", err=True)
    if as_json:
        click.echo(
            json.dumps(
                {"schema_version": SENSE_SCHEMA_VERSION, "error": message},
            )
        )
    sys.exit(2)


@cli.command()
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root; kstrl.toml is read from here (defaults to current directory)",
)
@click.option(
    "--path",
    "tree_path",
    type=click.Path(path_type=Path),
    help="Tree to measure: a worktree, a checkout, any directory (defaults to --root)",
)
@click.option(
    "--base",
    "base_branch",
    type=str,
    default=None,
    help="Base branch for the diff-scope and bad-pattern checks "
    "(default: auto-detected from the repository)",
)
@click.option(
    "--prd",
    "prd_path",
    type=click.Path(path_type=Path),
    help="PRD file; when given, the prd_stories check and the approved-fixtures oracle also run",
)
@click.option(
    "--allowed-path",
    "allowed_paths",
    multiple=True,
    help="Glob the diff must stay inside (repeatable); when absent "
    "diff-scope reports no scope constraints",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print the measurement as one JSON document instead of a table",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
def sense(
    root: Path | None,
    tree_path: Path | None,
    base_branch: str | None,
    prd_path: Path | None,
    allowed_paths: tuple[str, ...],
    as_json: bool,
    ui: str,
    no_color: bool,
) -> None:
    """Run the mechanical sensors against a tree and print the measurement.

    R10.1: the same checks Phase 1 runs inside the factory (test suite,
    typecheck, linter, diff scope, bad patterns, plus any opt-in
    policy / adequacy / dead-code / mutation checks from kstrl.toml),
    run by hand with no PRD, no branch, no worktree and no agent spend.

    The measurement is read-only. It runs against your live checkout,
    not a worktree kstrl owns, so it writes nothing to .kstrl/ and never
    edits, stages, commits or leaves bytecode: the dead-code check
    reports what it would remove instead of removing it, and mutation
    testing cannot run at all because mutmut works by rewriting source.
    The exception is the project's OWN configured test / typecheck /
    lint commands, which are your programs and write their own caches.

    A check that could not run gets NO row: it is reported under
    not_measured with the reason, never as a passing check (#306).

    Most checks read `git diff <base>...HEAD`, so the tree must be a git
    repository with a reachable base unless every diff-based check is
    turned off in kstrl.toml.

    Exit 0 when every check passed, 1 when any failed, 2 when the
    measurement itself could not run (missing path, bad kstrl.toml, or
    git cannot produce the diff).
    """
    root_dir = root.resolve() if root else Path.cwd()
    path = tree_path.resolve() if tree_path else root_dir

    if not root_dir.is_dir():
        _sense_error(f"root is not a directory: {root_dir}", as_json)
    if not path.is_dir():
        _sense_error(f"path is not a directory: {path}", as_json)

    from kstrl.adequacy import AdequacyConfig
    from kstrl.config_preflight import preflight_config
    from kstrl.fixtures import FixturesConfig
    from kstrl.policy import PolicyConfig
    from kstrl.verify import VerifyConfig, run_mechanical_verification

    try:
        # The WHOLE configuration, not only the four sections this
        # command reads. `sense` is exempt from the entry seam because
        # its contract is exit 2 plus a JSON error document rather than
        # the seam's exit 1, and an exemption is only honest if the
        # command does the same check: checking four of twenty-two
        # would keep exactly the "depends which section you typo'd"
        # property #272 removed, inside the exemption.
        preflight_config(root_dir, warn=_preflight_warn)
        verify_cfg = VerifyConfig.load(root_dir)
        policy_cfg = PolicyConfig.load(root_dir)
        adequacy_cfg = AdequacyConfig.load(root_dir)
        fixtures_cfg = FixturesConfig.load(root_dir) if prd_path is not None else None
    except (OSError, ValueError) as exc:
        # ValueError covers malformed TOML (load_toml_section), the
        # preflight's ConfigError, and the loaders' own validation
        # errors (PolicyConfigError is one).
        _sense_error(f"could not load kstrl.toml from {root_dir}: {exc}", as_json)

    base = resolve_base_branch(base_branch, path)

    # Every check below that consumes the diff reads it through the
    # LENIENT git helpers, which map a bad ref, a missing base or a
    # non-repository onto an EMPTY file list - indistinguishable from
    # "nothing changed". diff_scope then reports "0 files, all within
    # scope", bad_patterns "scanned 0 Python files", and `ks sense`
    # exits 0 having measured nothing. mutation_testing is deliberately
    # absent from this list: sense skips that check outright (read-only),
    # so its diff read never happens and demanding a base for it would
    # be a false exit 2.
    needs_diff = (
        verify_cfg.check_diff_scope
        or verify_cfg.check_bad_patterns
        or verify_cfg.dead_code_cleanup
        or policy_cfg.enabled
        or adequacy_cfg.enabled
    )
    if needs_diff:
        # Ask git the same question once, strictly, before any check
        # runs. Cannot-measure is exit 2; it is never a pass.
        from kstrl import git as _git

        try:
            _git.get_diff_names(base, path, strict=True)
        except _git.GitDiffError as exc:
            origin = (
                "from --base" if base_branch else "auto-detected; name the right one with --base"
            )
            _sense_error(
                f"git cannot measure the diff against {base!r} ({origin}): {exc}",
                as_json,
            )

    result = run_mechanical_verification(
        worktree_path=path,
        prd_path=prd_path.resolve() if prd_path is not None else None,
        base_branch=base,
        allowed_paths=list(allowed_paths) or None,
        config=verify_cfg,
        policy_config=policy_cfg,
        adequacy_config=adequacy_cfg,
        fixtures_config=fixtures_cfg,
        autonomy_level=0,
        component_id=None,
        read_only=True,
    )

    if as_json:
        click.echo(json.dumps(_sense_document(path, base, result), indent=2))
        sys.exit(0 if result.passed else 1)

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)
    ui_impl.section("ks sense")
    ui_impl.kv("Path", str(path))
    ui_impl.kv("Base branch", base)
    ui_impl.info("")
    # Shared with `ks feature`'s #288 report: one renderer for this
    # object, so a column change cannot land in one command and silently
    # not the other.
    for line in result.report_lines():
        ui_impl.info(line)
    ui_impl.info("")
    failed = sum(1 for c in result.checks if not c.passed)
    if result.passed:
        ui_impl.ok("sense: PASS")
        sys.exit(0)
    ui_impl.err(f"sense: FAIL ({failed} of {len(result.checks)} checks failed)")
    sys.exit(1)


@cli.command()
@click.argument("component_id")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path),
    help="Manifest file (default: scripts/kstrl/manifest.json)",
)
@click.option(
    "--progress-log",
    type=click.Path(path_type=Path),
    help="Path for JSONL progress log",
)
@click.option(
    "--keep-worktrees-on-failure",
    is_flag=True,
    help="Keep a failed component's worktree for post-mortem instead of "
    "removing it at cleanup (also via "
    "KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE / "
    "[factory].keep_worktrees_on_failure in kstrl.toml)",
)
@click.option(
    "--force-lock",
    is_flag=True,
    help="Proceed even if another kstrl invocation holds "
    ".kstrl/factory.lock (may corrupt the other run's state)",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
def retry(
    component_id: str,
    root: Path | None,
    manifest_path: Path | None,
    progress_log: Path | None,
    keep_worktrees_on_failure: bool,
    force_lock: bool,
    yes: bool,
    ui: str,
    no_color: bool,
) -> None:
    """Retry a FAILED component from the factory manifest (R3.3).

    Resets COMPONENT_ID and its cascade-skipped dependents to PENDING,
    removes the failed attempt's kept worktree and branch (a retry
    starts fresh from the base branch; the failed attempt's findings
    stay in the evolution journal), then re-enters the factory with the
    same manifest. Phase configs resolve env > kstrl.toml > defaults,
    exactly like `ks factory` invoked without flags. The run-level
    factory lock applies as usual.
    """
    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    manifest_file = (
        manifest_path
        if manifest_path is not None
        else root_dir / "scripts" / "kstrl" / "manifest.json"
    )
    if not manifest_file.exists():
        ui_impl.err(f"No manifest found at {manifest_file}")
        ui_impl.info("Run `ks factory` first, or pass --manifest.")
        sys.exit(1)
    manifest = _load_manifest_or_exit(manifest_file, ui_impl)

    try:
        prepare_retry(manifest, component_id, manifest_file, root_dir, ui_impl)
    except ValueError as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    except RetryError:
        sys.exit(1)

    _retry_channel = UiInteractionChannel(ui_impl)
    if not yes and _retry_channel.can_prompt():
        response = _retry_channel.request(
            PromptRequest(
                kind=PromptKind.CONFIRM,
                header=f"Re-enter the factory to retry '{component_id}'?",
                options=("Start", "Quit"),
                default=0,
            )
        )
        if response.answered and response.choice != 0:
            sys.exit(0)

    # Config assembly mirrors `ks factory` with no flags: every phase
    # config resolves env > kstrl.toml > defaults (R2.1 control plane).
    factory_config, base_config = assemble_factory_configs(
        root_dir,
        single_pr=manifest.single_pr,
        progress_log_path=progress_log,
        force_lock=force_lock,
        keep_worktrees_on_failure=keep_worktrees_on_failure,
    )
    _check_agent_preflight(base_config, ui_impl)
    _check_prompt_preflight(base_config.prompt_file, ui_impl)

    stop = StopController()
    uninstall = install_signal_handlers(stop)
    try:
        result = run_factory(
            manifest,
            factory_config,
            base_config,
            ui_impl,
            root_dir,
            manifest_path=manifest_file,
            stop=stop,
        )
    finally:
        uninstall()
    sys.exit(result.exit_code)


@cli.command()
@click.option(
    "--apply",
    "apply_id",
    type=str,
    default=None,
    help="Apply a specific proposal (e.g. PROP-001) or 'all' for all proposals",
)
@click.option(
    "--status",
    "show_status",
    is_flag=True,
    help="Show experiment trends",
)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
def evolve(
    apply_id: str | None,
    show_status: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Analyze factory runs and propose harness improvements.

    Without arguments, analyzes recent runs and shows proposals.
    Use --status to see experiment trends.
    Use --apply PROP-NNN (or 'all') to apply proposals: convention-type
    proposals (target claude_md) are appended to the project CLAUDE.md
    Agent Learnings section after confirmation; every other target
    prints manual instructions.
    """
    from kstrl.evolution import EvolutionConfig, EvolutionJournal

    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    # R2.1: honor [evolution] in kstrl.toml + env, anchored to --root.
    # Unguarded on purpose: `_PREFLIGHT_REQUIRED` lists this command as
    # one that [evolution] is FATAL for, so the entry seam has already
    # rejected a config this would raise on.
    evo_config = EvolutionConfig.load(root_dir)

    if not evo_config.enabled:
        ui_impl.err("Evolution is disabled in config")
        sys.exit(1)

    journal = EvolutionJournal(evo_config)

    if show_status:
        ui_impl.section("Experiment Trends")
        trends = journal.get_experiment_trends(last_n=10)
        # Before the empty-trends exit, not after (#327 round 2, F7).
        # decompose and autonomy both write to the journal before any
        # factory run has written experiments.tsv, so a repair with no
        # trends is not an edge case: it is the state the operator most
        # likely to HAVE a repair is in, and the exit below skipped it.
        _echo_journal_repairs(journal, ui_impl)
        if not trends:
            ui_impl.info("No experiments recorded yet. Run `ks factory` first.")
            sys.exit(0)

        for entry in trends:
            ui_impl.info(
                f"  {entry.get('run_id', '?')} | "
                f"completed={entry.get('completed', '?')} "
                f"failed={entry.get('failed', '?')} "
                f"retry_rate={entry.get('retry_rate', '?')}"
            )
        sys.exit(0)

    if apply_id:
        proposals_dir = root_dir / ".kstrl" / "proposals"
        if not proposals_dir.exists():
            ui_impl.err("No proposals found. Run `ks evolve` first.")
            sys.exit(1)
        exit_code = _evolve_apply(
            apply_id,
            proposals_dir,
            root_dir,
            evo_config,
            ui_impl,
        )
        sys.exit(exit_code)

    # Default: analyze and propose
    ui_impl.section("Evolution: Analyzing Runs")
    patterns = journal.get_cross_run_patterns(lookback_runs=evo_config.lookback_runs)

    if not patterns:
        ui_impl.info("No recurring failure patterns found across recent runs.")
        ui_impl.info("Run more factory sessions to accumulate data.")
        sys.exit(0)

    ui_impl.ok(f"Found {len(patterns)} recurring patterns")
    for pattern in patterns:
        ui_impl.info(
            f"  [{pattern.check_name}] {pattern.description} "
            f"(seen in {pattern.frequency} components)"
        )

    # R6.3: honor [evolution] auto_propose - when disabled, evolve only
    # reports patterns and never writes proposal files.
    if not evo_config.auto_propose:
        ui_impl.info(
            "auto_propose is disabled ([evolution] auto_propose = false); "
            "patterns reported, no proposals generated."
        )
        sys.exit(0)

    proposals_dir = root_dir / ".kstrl" / "proposals"
    proposals = journal.propose_improvements(patterns)
    # Idempotence across repeated `ks evolve` runs: a proposal whose
    # title already exists on disk is the same pattern re-detected, not
    # new signal - skip it rather than duplicating files.
    existing_titles = _existing_proposal_titles(proposals_dir)
    fresh = [p for p in proposals if p.title not in existing_titles]
    already = len(proposals) - len(fresh)
    # R6.2: monotonic IDs across invocations - number only the fresh
    # proposals, continuing after the highest PROP number on disk, so a
    # deduped batch never burns or reuses an existing number.
    start = journal.next_proposal_number(proposals_dir)
    for offset, proposal in enumerate(fresh):
        proposal.id = f"PROP-{start + offset:03d}"
    if fresh:
        paths = journal.save_proposals(fresh, proposals_dir)
        ui_impl.section("Proposals Generated")
        for path in paths:
            ui_impl.info(f"  {path}")
        if already:
            ui_impl.info(f"  ({already} proposal(s) already on disk; not duplicated)")
        ui_impl.info("")
        ui_impl.info("Review proposals and apply with `ks evolve --apply <ID>`")
    elif already:
        ui_impl.info(
            f"All {already} proposal(s) for these patterns already exist in {proposals_dir}."
        )
    else:
        ui_impl.info("No actionable proposals generated from current patterns.")

    sys.exit(0)


def _evolve_apply(
    apply_id: str,
    proposals_dir: Path,
    root_dir: Path,
    evo_config: EvolutionConfig,
    ui_impl: UI,
) -> int:
    """R6.3: the minimal REAL apply path. Convention-type proposals
    (computational, target=claude_md) append to the project CLAUDE.md
    Agent Learnings section after explicit confirmation
    (auto_apply_computational=true skips the prompt); every other
    proposal type prints honest manual instructions - no false
    "applied" claims. Mechanics live in kstrl.proposals (shared with
    the evolve screen); narration and the click.confirm wrapper stay
    here."""
    if apply_id.lower() == "all":
        paths = sorted(proposals_dir.glob("prop-*.md"))
        if not paths:
            ui_impl.err(f"No proposal files in {proposals_dir}.")
            return 1
    else:
        candidate = proposals_dir / f"{apply_id.lower()}.md"
        if not candidate.exists():
            ui_impl.err(f"Proposal '{apply_id}' not found (expected {candidate}).")
            return 1
        paths = [candidate]

    claude_md = root_dir / "CLAUDE.md"
    failures = 0
    for path in paths:
        proposal = parse_proposal_file(path)
        pid = proposal.display_id
        if proposal.applied:
            ui_impl.info(f"{pid} already applied at {proposal.applied}; skipping.")
            continue
        if not proposal.is_convention:
            ui_impl.info(f"{pid}: {proposal.title}")
            ui_impl.warn(
                f"  Automated apply only covers convention-type proposals "
                f"(target claude_md). This one targets "
                f"'{proposal.target or 'unknown'}': review {path} and "
                f"apply it manually."
            )
            continue
        ui_impl.info(f"{pid}: {proposal.title}")
        ui_impl.info(f"  Convention: {proposal.convention}")
        if not evo_config.auto_apply_computational:
            # PR A: the old bare click.confirm raised click.Abort on
            # non-TTY EOF and crashed the command. Piped input
            # ("echo y | ks evolve --apply ...") must keep working,
            # so this stays click.confirm - with EOF now meaning
            # "declined", never a crash.
            try:
                confirmed = click.confirm(
                    f"Append this convention to {claude_md}?",
                    default=False,
                )
            except click.Abort:
                ui_impl.info("")
                confirmed = False
            if not confirmed:
                ui_impl.info(f"  {pid} not applied (declined).")
                continue
        if not _append_to_agent_learnings(
            claude_md,
            pid,
            proposal.convention,
        ):
            ui_impl.err(
                f"  Could not apply {pid}: {claude_md} is missing or has "
                f"no '## Agent Learnings' section. Add the section or "
                f"apply manually from {path}."
            )
            failures += 1
            continue
        mark_applied(path)
        ui_impl.ok(f"  {pid} appended to {claude_md}.")
    return 1 if failures else 0


@cli.group(name="autonomy")
def autonomy_group() -> None:
    """Inspect and change the autonomy ladder (R8.2).

    Autonomy is earned, bounded, and revocable: promotion needs evidence
    AND your explicit acknowledgement, demotion is automatic, and every
    transition is recorded.
    """


def _autonomy_ui(ui: str, no_color: bool) -> UI:
    force_rich = os.environ.get("GUM_FORCE") == "1"
    return _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)


_autonomy_root_option = click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
_autonomy_ui_option = click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
_autonomy_no_color_option = click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)


@autonomy_group.command(name="status")
@_autonomy_root_option
@_autonomy_ui_option
@_autonomy_no_color_option
def autonomy_status(root: Path | None, ui: str, no_color: bool) -> None:
    """Show the current level, its flag bundle, and what promotion needs."""
    from kstrl.autonomy import (
        AutonomyConfig,
        AutonomyState,
        flag_bundle_for,
        resolve_runtime_level,
    )
    from kstrl.policy import PolicyConfig

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    config = AutonomyConfig.load(root_dir)
    state = AutonomyState.load(root_dir)
    policy_enabled = PolicyConfig.load(root_dir).enabled
    level, clamps = resolve_runtime_level(
        state,
        config,
        policy_enabled=policy_enabled,
        root_dir=root_dir,
    )

    ui_impl.section("Autonomy")
    ui_impl.kv("level", f"L{state.level} - {state.autonomy_level.label}")
    if int(level) != state.level:
        ui_impl.kv("in force", f"L{int(level)}")
        for note in clamps:
            ui_impl.warn(f"  {note}")
    ui_impl.kv(
        "enabled",
        "yes" if config.enabled else "no ([autonomy] enabled=false)",
    )
    ui_impl.kv("since", state.since or "-")
    if state.last_promoted_by:
        ui_impl.kv("promoted by", state.last_promoted_by)

    ui_impl.subsection("Flag bundle")
    # The bundle for the level that would actually be ENFORCED: showing
    # the stored level's permissions when max_level (or a disabled policy
    # envelope) clamps the run would advertise deploy/auto-merge the run
    # will never grant.
    for line in flag_bundle_for(level).describe():
        ui_impl.info(f"  {line}")

    ui_impl.subsection("Evidence at this level")
    ui_impl.kv("decisive runs", str(state.decisive_runs_at_level))
    ui_impl.kv("components merged", str(state.components_merged_at_level))
    ui_impl.kv("clean merge streak", str(state.clean_merges_at_level))
    ui_impl.kv("policy violations", str(state.policy_violations_at_level))
    if state.cooldown_runs_remaining:
        ui_impl.kv(
            "cool-down",
            f"{state.cooldown_runs_remaining} run(s) remaining",
        )

    blockers = state.promotion_blockers()
    ui_impl.subsection("Promotion")
    if blockers:
        ui_impl.warn("  Not eligible:")
        for blocker in blockers:
            ui_impl.info(f"    - {blocker}")
    else:
        ui_impl.ok("  Criteria met; `ks autonomy promote --actor <you> --ack <why>`")
    ui_impl.info("  Thresholds are UNMEASURED placeholders; run `ks autonomy replay`.")
    sys.exit(0)


@autonomy_group.command(name="promote")
@click.option("--actor", required=True, help="Who is acknowledging (a human)")
@click.option("--ack", required=True, help="Why the evidence justifies promotion")
@click.option(
    "--force",
    is_flag=True,
    help="Override unmet criteria (recorded as such in the audit trail)",
)
@_autonomy_root_option
@_autonomy_ui_option
@_autonomy_no_color_option
def autonomy_promote(
    actor: str,
    ack: str,
    force: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Raise the autonomy level by one. Requires a human ack."""
    from kstrl.autonomy import (
        AutonomyError,
        AutonomyLevel,
        AutonomyState,
        commit_transition,
        control_relocation_error,
        promotion_authority_error,
    )

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    # --actor/--ack are strings any caller can supply, so they cannot by
    # themselves distinguish a human from an unattended agent. Require an
    # out-of-band signal the agent's subprocess does not have: a
    # controlling terminal. Checked BEFORE the state is even loaded.
    authority_error = promotion_authority_error(force=force)
    if authority_error is not None:
        ui_impl.err(f"Promotion refused: {authority_error}")
        sys.exit(1)
    state = AutonomyState.load(root_dir)
    target = AutonomyLevel(min(int(state.autonomy_level) + 1, int(AutonomyLevel.L4_DEPLOY)))
    relocation_error = control_relocation_error(root_dir, target_level=target)
    if relocation_error is not None:
        ui_impl.err(f"Promotion refused: {relocation_error}")
        sys.exit(1)
    try:
        record = state.promote(actor=actor, ack=ack, force=force)
    except AutonomyError as exc:
        ui_impl.err(f"Promotion refused: {exc}")
        sys.exit(1)
    commit_transition(state, record, root_dir)
    ui_impl.ok(
        f"Promoted L{record.from_level} -> L{record.to_level} "
        f"({state.autonomy_level.label}) by {actor}"
    )
    if force:
        ui_impl.warn("  Recorded as a forced promotion over unmet criteria.")
    sys.exit(0)


@autonomy_group.command(name="demote")
@click.option("--reason", required=True, help="Why the level is being revoked")
@click.option(
    "--trigger",
    type=click.Choice(
        [
            "policy_violation",
            "calibration_regression",
            "health_breach",
            "human_rejected_auto_merge",
            "manual",
        ]
    ),
    default="manual",
    help="Which trigger fired",
)
@_autonomy_root_option
@_autonomy_ui_option
@_autonomy_no_color_option
def autonomy_demote(
    reason: str,
    trigger: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Drop the autonomy level by one and start the cool-down."""
    from kstrl.autonomy import AutonomyState, DemotionTrigger, commit_transition

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    state = AutonomyState.load(root_dir)
    chosen = next(
        (t for t in DemotionTrigger if t.label == trigger),
        DemotionTrigger.MANUAL,
    )
    record = state.demote(chosen, reason, actor="operator")
    if record is None:
        ui_impl.info("Already at L1 Supervised; nothing to revoke.")
        sys.exit(0)
    commit_transition(state, record, root_dir)
    ui_impl.warn(
        f"Demoted L{record.from_level} -> L{record.to_level} "
        f"({state.autonomy_level.label}); cool-down "
        f"{state.cooldown_runs_remaining} decisive run(s)"
    )
    sys.exit(0)


@autonomy_group.command(name="history")
@_autonomy_root_option
@_autonomy_ui_option
@_autonomy_no_color_option
def autonomy_history(root: Path | None, ui: str, no_color: bool) -> None:
    """Show every recorded level transition."""
    from kstrl.autonomy import AutonomyState

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    state = AutonomyState.load(root_dir)
    if not state.history:
        ui_impl.info("No transitions recorded; still at the starting level.")
        sys.exit(0)
    ui_impl.section("Autonomy history")
    for record in state.history:
        detail = f" [{record.trigger}]" if record.trigger else ""
        ui_impl.info(
            f"  {record.at}  L{record.from_level} -> L{record.to_level}  "
            f"{record.direction}{detail}  by {record.actor}"
        )
        if record.reason:
            ui_impl.info(f"      {record.reason}")
    sys.exit(0)


@autonomy_group.command(name="replay")
@_autonomy_root_option
@click.option(
    "--experiments",
    type=click.Path(path_type=Path),
    help="Path to experiments.tsv (default: <root>/.kstrl/experiments.tsv)",
)
@_autonomy_ui_option
@_autonomy_no_color_option
def autonomy_replay_cmd(
    root: Path | None,
    experiments: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Replay the ladder's thresholds over recorded run history.

    Reports what WOULD have fired and whether the sample is large enough
    to calibrate anything. Never mutates ladder state. Exit code 2 means
    "insufficient data", so a script cannot mistake it for a green run.
    """
    from kstrl.autonomy_replay import replay_file

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    report = replay_file(experiments, root_dir)
    for line in report.render().splitlines():
        ui_impl.info(line)
    sys.exit(0 if report.sufficient_data else 2)


@cli.group(name="inbox")
def inbox_group() -> None:
    """Triage what is waiting on a human (R8.3).

    One surface for policy exceptions, halted runs, unconfirmed merges,
    budget overruns, and autonomy demotions - the things that used to be
    discoverable only if you already knew where to look.
    """


_inbox_root_option = click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
_inbox_ui_option = click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
_inbox_no_color_option = click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)


def _inbox_for(root: Path | None) -> tuple[Path, Any]:
    from kstrl.inbox import Inbox, InboxConfig

    root_dir = (root or Path.cwd()).resolve()
    return root_dir, Inbox(root_dir, InboxConfig.load(root_dir))


def _actor() -> str:
    """Who is deciding. Best-effort identity for the audit trail."""
    return os.environ.get("USER") or os.environ.get("USERNAME") or "operator"


@inbox_group.command(name="ls")
@click.option("--all", "show_all", is_flag=True, help="Include decided items")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_ls(
    show_all: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """List items awaiting a decision."""
    from kstrl.inbox import summarize

    _root_dir, box = _inbox_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    items = box.items() if show_all else box.open_items()
    if not items:
        ui_impl.ok("Inbox clear: nothing is waiting on you.")
        sys.exit(0)
    ui_impl.section("Inbox")
    for item in items:
        marker = {"high": "!", "normal": " ", "low": "."}.get(str(item.priority), " ")
        repeat = f" x{item.occurrences}" if item.occurrences > 1 else ""
        state = "" if item.is_open else f" [{item.status}]"
        ui_impl.info(f"  {marker} {item.id[:8]}  {str(item.kind):<18}{item.title}{repeat}{state}")
    ui_impl.info("")
    ui_impl.kv("summary", summarize(box.items()))
    if box.over_cap():
        ui_impl.warn(
            f"  Open items have reached the cap ({box.config.open_item_cap}); "
            "queue intake pauses here (R8.6)."
        )
    sys.exit(0)


@inbox_group.command(name="show")
@click.argument("item_id")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_show(
    item_id: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Show one item in full, including its evidence."""
    import json as _json

    _root_dir, box = _inbox_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    item = box.get(item_id)
    if item is None:
        ui_impl.err(f"No inbox item matching {item_id!r}")
        sys.exit(1)
    ui_impl.section(item.title)
    ui_impl.kv("id", item.id)
    ui_impl.kv("kind", str(item.kind))
    ui_impl.kv("priority", str(item.priority))
    ui_impl.kv("status", str(item.status))
    ui_impl.kv("created", item.created_at)
    if item.component:
        ui_impl.kv("component", item.component)
    if item.run_id:
        ui_impl.kv("run", item.run_id)
    if item.occurrences > 1:
        ui_impl.kv("occurrences", str(item.occurrences))
    if item.decided_by:
        ui_impl.kv("decided by", f"{item.decided_by} at {item.decided_at}")
    if item.decision_comment:
        ui_impl.kv("comment", item.decision_comment)
    if item.detail:
        ui_impl.subsection("Detail")
        for line in item.detail.splitlines():
            ui_impl.info(f"  {line}")
    if item.evidence:
        ui_impl.subsection("Evidence")
        for line in _json.dumps(item.evidence, indent=2).splitlines():
            ui_impl.info(f"  {line}")
    sys.exit(0)


def _decide_and_report(
    action: str,
    item_id: str,
    root: Path | None,
    ui: str,
    no_color: bool,
    comment: str = "",
    hours: float | None = None,
) -> None:
    from kstrl.inbox import InboxError

    _root_dir, box = _inbox_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    try:
        if action == "approve":
            item = box.approve(item_id, actor=_actor(), comment=comment)
        elif action == "reject":
            item = box.reject(item_id, actor=_actor(), comment=comment)
        elif action == "snooze":
            item = box.snooze(item_id, actor=_actor(), hours=hours)
        else:
            item = box.resolve(item_id, actor=_actor(), comment=comment)
    except InboxError as exc:
        ui_impl.err(str(exc))
        sys.exit(1)
    ui_impl.ok(f"{action}d {item.id[:8]}: {item.title}")
    sys.exit(0)


@inbox_group.command(name="approve")
@click.argument("item_id")
@click.option("--comment", default="", help="Why (recorded in the audit trail)")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_approve(
    item_id: str,
    comment: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Accept the exception and close the item."""
    _decide_and_report("approve", item_id, root, ui, no_color, comment=comment)


@inbox_group.command(name="reject")
@click.argument("item_id")
@click.option("--comment", required=True, help="Why it was rejected")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_reject(
    item_id: str,
    comment: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Refuse the exception, recording why."""
    _decide_and_report("reject", item_id, root, ui, no_color, comment=comment)


@inbox_group.command(name="snooze")
@click.argument("item_id")
@click.option("--hours", type=float, help="TTL (default: [inbox] snooze_hours)")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_snooze(
    item_id: str,
    hours: float | None,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Defer an item; it returns when the TTL lapses."""
    _decide_and_report("snooze", item_id, root, ui, no_color, hours=hours)


@inbox_group.command(name="retry")
@click.argument("item_id")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_retry(
    item_id: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Requeue the item's component and close the item.

    A real round-trip, not a status change: the component is reset to
    PENDING in the manifest (with its dependents un-skipped), so the next
    `ks factory` run picks it up.
    """
    root_dir, box = _inbox_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    item = box.get(item_id)
    if item is None:
        ui_impl.err(f"No inbox item matching {item_id!r}")
        sys.exit(1)
    if not item.component:
        ui_impl.err(f"{item.id[:8]} has no component to requeue")
        sys.exit(1)
    manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
    if not manifest_path.exists():
        ui_impl.err(f"No manifest at {manifest_path}")
        sys.exit(1)
    manifest = _load_manifest_or_exit(manifest_path, ui_impl)
    try:
        reset = manifest.reset_for_retry(item.component)
    except (ValueError, KeyError) as exc:
        ui_impl.err(f"Could not requeue {item.component}: {exc}")
        sys.exit(1)
    manifest.save(manifest_path)
    box.resolve(item.id, actor=_actor(), comment="requeued via ks inbox retry")
    ui_impl.ok(
        f"Requeued {item.component} (reset: {', '.join(reset) or item.component}); "
        "run `ks factory` to pick it up."
    )
    sys.exit(0)


@cli.group(name="queue")
def queue_group() -> None:
    """Manage the continuous-intake work queue (R8.6).

    Work waits here instead of requiring a human to fire each run.
    `ks serve` drains it. Nothing in this group starts a factory run or
    spends anything: these verbs only move items between states.
    """


_queue_root_option = click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
_queue_ui_option = click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
_queue_no_color_option = click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)


def _queue_for(root: Path | None) -> tuple[Path, Any]:
    from kstrl.workqueue import Queue, QueueConfig

    root_dir = (root or Path.cwd()).resolve()
    return root_dir, Queue(root_dir, QueueConfig.load(root_dir))


def _resolve_queue_item(queue: Any, item_id: str, ui_impl: UI) -> Any:
    """Look up one item or exit; an ambiguous prefix is an error."""
    from kstrl.workqueue import QueueError

    try:
        item = queue.get(item_id)
    except QueueError as exc:
        ui_impl.err(str(exc))
        sys.exit(1)
    if item is None:
        ui_impl.err(f"No queue item matching {item_id!r}")
        sys.exit(1)
    return item


@queue_group.command(name="add")
@click.argument("spec", type=click.Path(exists=True, path_type=Path))
@click.option("--priority", type=int, default=0, help="Higher runs first")
@click.option("--title", default="", help="Human label (default: spec filename)")
@click.option("--project-name", default="", help="Factory project name")
@click.option(
    "--auto-merge/--stop-at-pr",
    "auto_merge",
    default=False,
    help=(
        "Request auto-merge when green (still gated by the autonomy "
        "ladder), or stop at the PR for a human (default)"
    ),
)
@click.option(
    "--max-attempts",
    type=click.IntRange(min=1),
    default=None,
    help="Execution attempts before poisoning (default: [queue] max_attempts)",
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_add(
    spec: Path,
    priority: int,
    title: str,
    project_name: str,
    auto_merge: bool,
    max_attempts: int | None,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Enqueue a spec file.

    The spec is COPIED into the item, so editing or deleting the
    original afterwards cannot change what eventually runs.
    """
    from kstrl.workqueue import MergeDisposition, QueueError, queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    disposition = MergeDisposition.AUTO_MERGE if auto_merge else MergeDisposition.STOP_AT_PR
    try:
        with queue_lock(root_dir):
            # Under the lock, any staging directory is abandoned by
            # definition (R8.6 #185 F3), so this is the natural recovery
            # point for an enqueue killed mid-publish.
            queue.sweep_staging()
            item = queue.add(
                spec,
                title=title,
                priority=priority,
                project_name=project_name,
                merge_disposition=disposition,
                max_attempts=max_attempts,
                actor=_actor(),
            )
    except QueueError as exc:
        ui_impl.err(str(exc))
        sys.exit(1)
    ui_impl.ok(f"Queued {item.item_id} - {item.title}")
    ui_impl.kv("merge", str(item.merge_disposition))
    ui_impl.kv("attempts allowed", str(item.max_attempts))
    if queue.is_paused():
        ui_impl.warn("Queue is paused; this item waits until `ks queue resume`.")
    sys.exit(0)


@queue_group.command(name="ls")
@click.option(
    "--state",
    "states",
    multiple=True,
    help="Filter by state (repeatable): queued/leased/running/done/failed/poison",
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_ls(
    states: tuple[str, ...],
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """List queue items in run order."""
    from kstrl.workqueue import ItemState, summarize

    _root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    selected: tuple[ItemState, ...] | None = None
    if states:
        try:
            selected = tuple(ItemState(value) for value in states)
        except ValueError as exc:
            ui_impl.err(str(exc))
            sys.exit(1)
    items = queue.items(selected)
    pause = queue.pause_state()
    if pause.active():
        detail = f" ({pause.reason})" if pause.reason else ""
        ui_impl.warn(f"Queue is PAUSED{detail}")
    if not items:
        ui_impl.ok("Queue is empty.")
        sys.exit(0)
    ui_impl.section("Queue")
    for item in items:
        attempts = f"{item.attempts}/{item.max_attempts}"
        ui_impl.info(
            f"  {item.item_id[:12]}  {str(item.state):<8} "
            f"p{item.priority:<3} {attempts:<6} {item.title}"
        )
    ui_impl.info("")
    ui_impl.kv("summary", summarize(queue.counts()))
    sys.exit(0)


@queue_group.command(name="show")
@click.argument("item_id")
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_show(
    item_id: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Show one item in full, with its transition history."""
    _root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    item = _resolve_queue_item(queue, item_id, ui_impl)

    ui_impl.section(item.title)
    ui_impl.kv("id", item.item_id)
    ui_impl.kv("state", str(item.state))
    ui_impl.kv("priority", str(item.priority))
    ui_impl.kv("attempts", f"{item.attempts} of {item.max_attempts}")
    ui_impl.kv("merge", str(item.merge_disposition))
    ui_impl.kv("source", str(item.source) + (f" ({item.source_ref})" if item.source_ref else ""))
    ui_impl.kv("created", item.created_at)
    ui_impl.kv("updated", item.updated_at)
    if item.project_name:
        ui_impl.kv("project", item.project_name)
    if item.last_run_id:
        ui_impl.kv("last run", item.last_run_id)
    if item.lease_pid:
        ui_impl.kv(
            "lease",
            f"pid {item.lease_pid} on {item.lease_host} "
            f"until {item.lease_expires_at}" + (" (EXPIRED)" if item.lease_expired() else ""),
        )
    if item.last_error:
        ui_impl.kv("last error", item.last_error)
    if item.poison_reason:
        ui_impl.kv("poisoned", item.poison_reason)
    ui_impl.kv("spec", str(queue.spec_path(item)))

    history = queue.journal_entries(item.item_id)
    if history:
        ui_impl.subsection("History")
        for entry in history:
            origin = entry.get("from") or "-"
            reason = entry.get("reason") or ""
            ui_impl.info(
                f"  {entry.get('ts', '')}  {origin} -> {entry.get('to', '')}"
                + (f"  ({reason})" if reason else "")
            )
    sys.exit(0)


@queue_group.command(name="retry")
@click.argument("item_id")
@click.option(
    "--reset-attempts",
    is_flag=True,
    help="Zero the attempt counter (explicit human decision to spend again)",
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_retry(
    item_id: str,
    reset_attempts: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Send a failed or poisoned item back to queued.

    A poisoned item that has already spent its attempts needs
    `--reset-attempts` to run again: re-queuing it otherwise would have
    it poisoned straight back without spending anything, which looks
    like the command silently failed.
    """
    from kstrl.workqueue import ItemState, QueueError, queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    item = _resolve_queue_item(queue, item_id, ui_impl)
    if item.state not in (ItemState.FAILED, ItemState.POISON):
        ui_impl.err(
            f"{item.item_id[:12]} is {item.state}; only failed or poisoned items can be retried"
        )
        sys.exit(1)
    if not reset_attempts and item.attempts_remaining == 0:
        ui_impl.err(
            f"{item.item_id[:12]} has used all {item.max_attempts} attempts; "
            "pass --reset-attempts to authorize spending again"
        )
        sys.exit(1)
    try:
        with queue_lock(root_dir):
            queue.requeue(
                item,
                reason="retry (manual)",
                actor=_actor(),
                reset_attempts=reset_attempts,
            )
    except (QueueError, OSError) as exc:
        ui_impl.err(str(exc))
        sys.exit(1)
    ui_impl.ok(f"Requeued {item.item_id[:12]} ({item.attempts}/{item.max_attempts} attempts used)")
    sys.exit(0)


@queue_group.command(name="rm")
@click.argument("item_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_rm(
    item_id: str,
    yes: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Delete an item and its spec."""
    from kstrl.workqueue import QueueError, queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    item = _resolve_queue_item(queue, item_id, ui_impl)
    if not yes and not click.confirm(
        f"Delete {item.item_id[:12]} ({item.title})?",
        default=False,
    ):
        ui_impl.info("Left alone.")
        sys.exit(0)
    try:
        with queue_lock(root_dir):
            queue.remove(item, actor=_actor())
    except (QueueError, OSError) as exc:
        # A deletion that failed must not print success: the operator
        # would believe the item is gone when it is still queued (#185 F6).
        ui_impl.err(f"Could not remove {item.item_id[:12]}: {exc}")
        sys.exit(1)
    ui_impl.ok(f"Removed {item.item_id[:12]}")
    sys.exit(0)


@queue_group.command(name="pause")
@click.option("--reason", default="", help="Why intake is paused")
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_pause(
    reason: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Stop admitting queued work.

    Does not touch anything already running - a pause is an admission
    gate, not a kill switch.
    """
    from kstrl.workqueue import queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    with queue_lock(root_dir):
        queue.pause(reason=reason, actor=_actor())
    ui_impl.ok("Queue paused; nothing new will be claimed.")
    if reason:
        ui_impl.kv("reason", reason)
    sys.exit(0)


@queue_group.command(name="resume")
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_resume(root: Path | None, ui: str, no_color: bool) -> None:
    """Start admitting queued work again."""
    from kstrl.workqueue import ItemState, queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    with queue_lock(root_dir):
        queue.resume(actor=_actor())
    waiting = len(queue.items((ItemState.QUEUED,)))
    ui_impl.ok(f"Queue resumed; {waiting} item(s) waiting.")
    sys.exit(0)


@queue_group.command(name="sync")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Poll and report what would be enqueued, writing nothing",
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_sync(
    dry_run: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Pull labelled GitHub issues into the queue (R8.6).

    Polls open issues carrying the trigger label and enqueues the ones
    not already seen. Remote items ALWAYS stop at the PR for a human.

    The label is the authorization: applying it needs write access to the
    repository, so an issue from a stranger cannot queue a factory run.
    """
    from kstrl.intake_github import GitHubIntakeConfig, IntakeError
    from kstrl.intake_github import sync as run_sync
    from kstrl.workqueue import QueueError, QueueLockedError, queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    try:
        config = GitHubIntakeConfig.load(root_dir)
    except (IntakeError, ValueError) as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    if not config.enabled:
        ui_impl.err(
            "GitHub intake is off. Set [intake_github] enabled = true in "
            "kstrl.toml (or KSTRL_INTAKE_GITHUB_ENABLED=1)."
        )
        sys.exit(1)

    # --dry-run runs the PRODUCTION planner with writes disabled, rather
    # than a second decision tree. Review #187 F4/F11: the old dry-run
    # had its own logic that ignored the admission cap, and a dry run
    # that disagrees with the real thing is worse than none.
    if dry_run:
        config = replace(config, dry_run=True)

    try:
        with queue_lock(root_dir):
            result = run_sync(queue, config, root_dir)
    except QueueLockedError as exc:
        ui_impl.err(f"{exc}. Another queue operation is in progress; retry shortly.")
        sys.exit(1)
    except (QueueError, OSError) as exc:
        ui_impl.err(f"Sync failed: {exc}")
        sys.exit(1)

    heading = "Would sync from" if dry_run else "Sync from"
    ui_impl.section(f"{heading} {result.repo or 'unknown repo'}")
    ui_impl.kv("label", config.queued_label)
    ui_impl.kv("polled", str(result.polled))
    if dry_run:
        ui_impl.kv("would enqueue", str(len(result.would_enqueue)))
    else:
        ui_impl.kv("enqueued", str(len(result.enqueued)))

    for entry in result.planned:
        ref = entry.issue.source_ref(result.repo)
        verdict = "ENQUEUE" if entry.decision.admits else str(entry.decision)
        line = f"  {ref:<28} {verdict:<22} {entry.issue.title}"
        if entry.decision.admits:
            ui_impl.ok(line)
        elif entry.decision is entry.decision.__class__.REFUSE_UNAUTHORIZED:
            ui_impl.err(f"{line}\n      {entry.reason}")
        else:
            ui_impl.info(f"{line}  ({entry.reason})")
    if not result.planned:
        ui_impl.info("  nothing labelled")

    for error in result.errors:
        ui_impl.err(f"  {error}")
    # Nonzero on error so a cron/launchd wrapper notices. Partial success
    # is real: items already enqueued stay enqueued.
    sys.exit(1 if result.errors else 0)


@cli.command()
@click.option(
    "--once",
    is_flag=True,
    help="Run a single poll cycle and exit (launchd/cron fallback mode)",
)
@click.option(
    "--max-cycles",
    type=click.IntRange(min=0),
    default=0,
    help="Stop after this many cycles; 0 runs until interrupted",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what the next cycle would do without spending anything",
)
@click.option(
    "--print-plist",
    "print_plist",
    is_flag=True,
    help="Print a launchd LaunchAgent plist for this checkout and exit",
)
@click.option(
    "--plist-mode",
    type=click.Choice(["keepalive", "interval"]),
    default="keepalive",
    help="keepalive: one long-lived daemon; interval: `--once` on a timer",
)
@click.option(
    "--plist-interval",
    type=click.IntRange(min=1),
    default=5,
    help=(
        "MINUTES between runs in interval mode; must divide an hour "
        "(1,2,3,4,5,6,10,12,15,20,30,60) or be whole hours"
    ),
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def serve(
    once: bool,
    max_cycles: int,
    dry_run: bool,
    print_plist: bool,
    plist_mode: str,
    plist_interval: int,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Drain the continuous-intake queue (R8.6).

    Runs one factory invocation at a time, holding a daemon singleton
    lock, and polls any enabled remote inbox at the start of each cycle.

    Under launchd, `--print-plist --plist-mode interval` schedules this
    with --once on a StartCalendarInterval, which is the only launchd
    timer that catches up after sleep: launchd.plist(5) says a
    StartInterval firing during sleep "will be missed", while
    StartCalendarInterval "will start the job the next time the computer
    wakes up".

    Only infrastructure failures are retried, and only with positive
    evidence. Spec-level failures go to poison/ and wait for a human.
    """
    from kstrl.serve import (
        REQUIRE_TIMEOUT_ENV,
        ServeConfig,
        ServeError,
        ServeLockedError,
        SpendLedger,
        check_budget,
        check_cost_coverage,
        check_inbox_cap,
        check_poison_breaker,
        consecutive_poison_count,
        factory_lock_held,
    )
    from kstrl.serve import serve as run_serve
    from kstrl.workqueue import Queue, QueueConfig, summarize

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)

    if print_plist:
        # Printed rather than installed: writing into ~/Library/LaunchAgents
        # and running launchctl are outward-facing acts an operator should
        # perform deliberately, and the docs walk through them.
        from kstrl.serve import launchd_log_dir, render_launchd_plist

        # launchd creates the log FILE but not its directory, and a
        # missing directory makes the job fail to spawn with nothing in
        # the log to say why. Create it here, where the operator is
        # actively setting the job up.
        try:
            launchd_log_dir(root_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            ui_impl.err(f"could not create the launchd log directory: {exc}")
            sys.exit(2)

        try:
            click.echo(
                render_launchd_plist(
                    root_dir,
                    mode=plist_mode,
                    interval_minutes=plist_interval,
                    factory_timeout_seconds=(ServeConfig.load(root_dir).factory_timeout_seconds),
                ),
                nl=False,
            )
        except ServeError as exc:
            ui_impl.err(str(exc))
            sys.exit(2)
        sys.exit(0)

    try:
        config = ServeConfig.load(root_dir)
    except (ServeError, ValueError) as exc:
        ui_impl.err(str(exc))
        sys.exit(2)

    # A scheduled LaunchAgent carries this marker, so the promise it was
    # installed under is re-checked on every invocation rather than only
    # the day the plist was written (#189 N3).
    if os.environ.get(REQUIRE_TIMEOUT_ENV) == "1" and (config.factory_timeout_seconds <= 0):
        ui_impl.err(
            "this scheduled job was installed on the promise of a bounded "
            "cycle, but [serve] factory_timeout_seconds is now 0. launchd "
            "neither kills nor replaces a job still running at the next "
            "firing, so an unbounded cycle would silently stop every later "
            "one. Restore the timeout, or reinstall the LaunchAgent in "
            "keepalive mode."
        )
        sys.exit(2)
    queue = Queue(root_dir, QueueConfig.load(root_dir))
    ledger = SpendLedger(root_dir)

    if dry_run:
        # Deliberately reports the same gates the loop evaluates, in the
        # same order, so "what would it do" cannot drift from "what it
        # does" without a test noticing.
        ui_impl.section("Serve dry run")
        _render_safe_mode(ui_impl, root_dir)
        ui_impl.kv("queue", summarize(queue.counts()))
        pause = queue.pause_state()
        ui_impl.kv(
            "paused",
            f"yes - {pause.reason}" if pause.active() else "no",
        )
        from kstrl.serve import ServeStateError

        try:
            spend = ledger.read()
        except ServeStateError as exc:
            ui_impl.err(str(exc))
            sys.exit(2)
        floor = (
            (
                f" (a FLOOR: {spend.uncovered_calls} call(s) reported no cost"
                + (
                    "; unmetered: " + ", ".join(spend.unmetered_phases)
                    if spend.unmetered_phases
                    else ""
                )
                + ")"
            )
            if spend.lower_bound
            else ""
        )
        ui_impl.kv(
            "today",
            f"${spend.spent_usd:.2f} over {spend.runs} run(s){floor}",
        )
        ui_impl.kv(
            "daily budget",
            f"${config.daily_budget_usd:.2f}" if config.daily_budget_usd > 0 else "unset (no cap)",
        )
        ui_impl.kv("consecutive poison", str(consecutive_poison_count(ledger)))
        ui_impl.kv(
            "factory lock",
            "held by another run" if factory_lock_held(root_dir) else "free",
        )
        for label, admission in (
            ("poison breaker", check_poison_breaker(ledger, config)),
            ("cost coverage", check_cost_coverage(ledger, config)),
            ("budget", check_budget(ledger, config)),
            ("inbox cap", check_inbox_cap(root_dir)),
        ):
            if admission.allowed:
                ui_impl.info(f"  gate {label}: ok")
            else:
                ui_impl.warn(f"  gate {label}: BLOCKS - {admission.reason}")
        # The real cycle SYNCS before selecting, so a dry run that reads
        # only the existing queue can report "nothing ready" while the
        # live loop would admit an issue and spend on it immediately
        # (#189 N2). Runs the adapter's side-effect-free planner.
        from dataclasses import replace as _replace

        from kstrl.intake_github import GitHubIntakeConfig, IntakeError
        from kstrl.intake_github import sync as intake_sync

        try:
            intake_config = GitHubIntakeConfig.load(root_dir)
        except (IntakeError, ValueError) as exc:
            intake_config = None
            ui_impl.warn(f"  intake config unreadable: {exc}")
        if intake_config is not None and intake_config.enabled:
            plan = intake_sync(
                queue,
                _replace(intake_config, dry_run=True),
                root_dir,
            )
            ui_impl.kv("intake", f"{plan.polled} polled from {plan.repo or '?'}")
            for ref in plan.would_enqueue:
                ui_impl.ok(f"  would admit {ref}")
            for ref, reason in sorted(plan.skipped.items()):
                ui_impl.info(f"  skip {ref}: {reason}")
            for error in plan.errors:
                ui_impl.warn(f"  intake: {error}")
        else:
            ui_impl.kv("intake", "disabled")

        candidate = queue.next_ready()
        pending = f"{candidate.item_id[:12]} - {candidate.title}" if candidate else "nothing ready"
        if candidate is None and intake_config is not None and (intake_config.enabled):
            # Say so explicitly: "nothing ready" alone would be misleading
            # when intake is about to admit work.
            pending = "nothing queued yet (intake may admit the items above)"
        ui_impl.kv("next item", pending)
        sys.exit(0)

    ui_impl.info(
        f"ks serve on {root_dir} "
        f"(poll {config.poll_interval_seconds:.0f}s, "
        f"budget "
        + (f"${config.daily_budget_usd:.2f}/day" if config.daily_budget_usd > 0 else "unset")
        + f", caffeinate {'on' if config.caffeinate else 'off'})"
    )
    observer = _ServeUiObserver(ui_impl)
    try:
        results = run_serve(
            root_dir,
            once=once,
            config=config,
            observer=observer,
            max_cycles=max_cycles,
        )
    except ServeLockedError as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    except KeyboardInterrupt:
        ui_impl.info("Interrupted; the current item keeps its lease for the reaper.")
        sys.exit(0)

    ran = [r for r in results if r.ran_item]
    ui_impl.info("")
    ui_impl.kv("cycles", str(len(results)))
    ui_impl.kv("items run", str(len(ran)))
    # Derived from the TERMINAL queue outcome, not from the classifier's
    # retry permission. Review #186 F10: an infra verdict whose last
    # attempt was spent is poisoned by serve_cycle, yet
    # Verdict.RETRY_INFRA.may_retry stays true - and the reaper and
    # merge-gate poison paths set no ran_item at all - so the old filter
    # exited 0 on work that was waiting for a human.
    needs_human = [r for r in results if r.needs_human]
    if needs_human:
        ui_impl.warn(
            f"{len(needs_human)} cycle(s) produced work awaiting a human; "
            "see `ks inbox ls` and `ks queue ls --state poison`"
        )
    sys.exit(1 if needs_human else 0)


class _ServeUiObserver:
    """Adapts the daemon's narration onto the console UI."""

    def __init__(self, ui_impl: UI) -> None:
        self._ui = ui_impl

    def info(self, message: str) -> None:
        self._ui.info(message)

    def warn(self, message: str) -> None:
        self._ui.warn(message)

    def err(self, message: str) -> None:
        self._ui.err(message)


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
