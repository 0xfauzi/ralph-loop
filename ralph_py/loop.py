"""Main agentic loop for Ralph."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py import git, guards
from ralph_py.prd import PRD

if TYPE_CHECKING:
    from ralph_py.agents.base import Agent
    from ralph_py.config import RalphConfig
    from ralph_py.ui.base import UI

COMPLETION_MARKER = "<promise>COMPLETE</promise>"

PLAN_PREFIXES = (
    "preparing to",
    "planning to",
    "plan:",
    "goal:",
    "next:",
    "listing",
    "reading",
    "reviewing",
    "i will",
    "i'm going to",
    "i am going to",
    "i plan to",
)

TOKEN_LABELS = {
    "tokens used": "Tokens used",
    "prompt tokens": "Prompt tokens",
    "completion tokens": "Completion tokens",
    "total tokens": "Total tokens",
}

TOKEN_VALUE_RE = re.compile(r"([0-9][0-9,]*)")
SYS_KV_RE = re.compile(r"^([^:]{1,24}):\s+(.+)$")
TOOL_HEADER_RE = re.compile(
    r"-lc (?P<cmd>.+?) in .+ (?P<status>succeeded|failed) in (?P<duration>[0-9.]+(?:ms|s))"
)

SYS_KV_KEYS = {
    "workdir",
    "model",
    "provider",
    "approval",
    "sandbox",
    "reasoning effort",
    "reasoning summaries",
    "session id",
}
TOOL_STATUS_MAP = {
    "succeeded": "ok",
    "failed": "fail",
}


def _normalize_plan_line(text: str) -> str:
    line = text.strip()
    line = line.lstrip("-").strip()
    line = line.lstrip("#").strip()
    line = line.strip("*_").strip()
    return line


def _looks_like_plan_line(text: str) -> bool:
    cleaned = _normalize_plan_line(text).lower()
    if not cleaned:
        return False
    return any(cleaned.startswith(prefix) for prefix in PLAN_PREFIXES)


def _extract_plan_lines(lines: list[tuple[str, str]]) -> list[str]:
    plan_lines: list[str] = []
    for tag, text in lines:
        if tag != "THINK":
            continue
        if _looks_like_plan_line(text):
            plan_lines.append(_normalize_plan_line(text))
    return plan_lines


def _extract_completion_metrics(lines: list[tuple[str, str]]) -> dict[str, str]:
    metrics: dict[str, str] = {}
    pending_label: str | None = None

    for _, text in lines:
        raw = text.strip()
        if not raw:
            continue

        if pending_label:
            if re.match(r"^[0-9][0-9,]*$", raw):
                metrics[pending_label] = raw
            pending_label = None

        lower = raw.lower()
        for key, label in TOKEN_LABELS.items():
            if key in lower:
                match = TOKEN_VALUE_RE.search(raw)
                if match:
                    metrics[label] = match.group(1)
                else:
                    pending_label = label
                break

    return metrics


def _render_callout(ui: UI, tag: str, title: str, lines: list[str]) -> None:
    if not lines:
        return
    ui.panel(tag, title, "\n".join(lines))


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return f"{text[: max_len - 3]}..."


def _extract_sys_kv(line: str) -> tuple[str, str] | None:
    match = SYS_KV_RE.match(line)
    if not match:
        return None
    key = match.group(1).strip()
    value = match.group(2).strip()
    if key.lower() in SYS_KV_KEYS:
        return key, value
    return None


@dataclass
class ToolSummary:
    """Summarized tool call details."""

    command: str
    status: str
    duration: str | None
    output_lines: int = 0


def _parse_tool_header(line: str) -> ToolSummary:
    match = TOOL_HEADER_RE.search(line)
    if match:
        command = match.group("cmd").strip()
        status = TOOL_STATUS_MAP.get(match.group("status"), "unknown")
        duration = match.group("duration")
        return ToolSummary(command=command, status=status, duration=duration)
    return ToolSummary(command=line.strip(), status="unknown", duration=None)


def _format_tool_summary(summary: ToolSummary) -> str:
    command = _truncate(summary.command, 80)
    parts = [command]
    if summary.duration:
        parts.append(summary.duration)
    if summary.status != "unknown":
        parts.append(summary.status)
    if summary.output_lines == 0:
        parts.append("no output")
    else:
        parts.append(f"{summary.output_lines} lines")
    return " · ".join(parts)


@dataclass
class LoopResult:
    """Result of running the agentic loop."""

    completed: bool
    iterations: int
    exit_code: int


def run_loop(config: RalphConfig, ui: UI, agent: Agent, cwd: Path | None = None) -> LoopResult:
    """Run the main agentic loop.

    Args:
        config: Ralph configuration
        ui: UI implementation for output
        agent: Agent to run
        cwd: Working directory (defaults to current)

    Returns:
        LoopResult with completion status and exit code
    """
    if cwd is None:
        cwd = Path.cwd()

    ui.startup_art()

    # Display title
    ui.title("Ralph")

    # Display startup info
    ui.section("Startup")
    ui.kv("Root", str(cwd))
    ui.kv("Prompt", str(config.prompt_file))
    ui.kv("PRD", str(config.prd_file))
    ui.kv("Agent", agent.name)
    ui.kv("Max iterations", str(config.max_iterations))
    ui.kv("Sleep", f"{config.sleep_seconds}s")
    ui.kv("Interactive", "yes" if config.interactive else "no")
    allowed_paths = (
        ", ".join(config.allowed_paths) if config.allowed_paths else "<disabled>"
    )
    ui.kv("Allowed paths", allowed_paths)
    ui.kv("Reasoning", config.model_reasoning_effort or "<default>")
    ui.kv("UI", config.ui_mode)
    ui.kv("Tool output", config.ai_tool_mode)
    ui.kv("Sys output", config.ai_sys_mode)

    # Validate prompt file
    if not config.prompt_file.exists():
        ui.err(f"Prompt file not found: {config.prompt_file}")
        return LoopResult(completed=False, iterations=0, exit_code=1)

    # Read prompt
    prompt = config.prompt_file.read_text()

    # Preflight
    ui.section("Preflight")

    # Git/Branch handling
    ui.subsection("Git / Branch")
    is_repo = git.is_git_repo(cwd)

    if not is_repo:
        ui.warn("Not a git repository")
    else:
        branch, source = _determine_branch(config)
        if branch:
            if not git.checkout_branch(branch, ui, cwd, source):
                ui.err(f"Failed to checkout branch: {branch}")
                return LoopResult(completed=False, iterations=0, exit_code=1)
        elif branch == "":
            ui.info("Branch: RALPH_BRANCH is set but empty; skipping branch checkout")
        else:
            ui.info("Branch: no branch configured")

    # Guardrails info
    ui.subsection("Guardrails")
    if config.allowed_paths and is_repo:
        ui.info(f"Enforcing ALLOWED_PATHS={','.join(config.allowed_paths)}")
    else:
        ui.info("ALLOWED_PATHS is empty; enforcement disabled")

    # Main loop
    from ralph_py.agents.codex import CodexAgent
    from ralph_py.ui.codex_transcript import CodexTranscriptParser

    is_codex_agent = isinstance(agent, CodexAgent)

    for iteration in range(1, config.max_iterations + 1):
        ui.section(f"Iteration {iteration} / {config.max_iterations}")

        # Run agent
        ui.channel_header("AI", "Agent output")
        output_lines: list[str] = []
        parsed_lines: list[tuple[str, str]] = []
        tool_summaries: list[ToolSummary] = []
        sys_info: dict[str, str] = {}
        sys_notes: list[str] = []
        prompt_note: str | None = None
        active_tool: ToolSummary | None = None
        active_tool_lines = 0
        tool_mode = config.ai_tool_mode
        parser: CodexTranscriptParser | None = None
        if is_codex_agent and not config.ai_raw:
            parser = CodexTranscriptParser(
                prompt_file=config.prompt_file,
                show_prompt=config.ai_show_prompt,
                prompt_progress_every=config.ai_prompt_progress_every,
            )

        def flush_tool(
            tool_summaries_ref: list[ToolSummary] = tool_summaries,
        ) -> None:
            nonlocal active_tool, active_tool_lines
            if active_tool is None:
                return
            active_tool.output_lines = active_tool_lines
            tool_summaries_ref.append(active_tool)
            active_tool = None
            active_tool_lines = 0

        for line in agent.run(prompt, cwd):
            output_lines.append(line)
            if parser is None:
                ui.stream_line("AI", line)
            else:
                for parsed in parser.feed(line):
                    if parsed.tag == "TOOL":
                        if active_tool is None:
                            active_tool = _parse_tool_header(parsed.text)
                            active_tool_lines = 0
                            if tool_mode == "full":
                                ui.stream_line("TOOL", parsed.text)
                        elif TOOL_HEADER_RE.search(parsed.text):
                            flush_tool()
                            active_tool = _parse_tool_header(parsed.text)
                            active_tool_lines = 0
                            if tool_mode == "full":
                                ui.stream_line("TOOL", parsed.text)
                        else:
                            active_tool_lines += 1
                            if tool_mode == "full":
                                ui.stream_line("TOOL", parsed.text)
                        continue

                    if active_tool is not None:
                        flush_tool()

                    if parsed.tag in {"SYS", "PROMPT"} and config.ai_sys_mode == "summary":
                        if parsed.tag == "SYS":
                            sys_kv = _extract_sys_kv(parsed.text)
                            if sys_kv:
                                sys_info[sys_kv[0]] = sys_kv[1]
                                continue
                        note = parsed.text.strip()
                        if note:
                            if parsed.tag == "PROMPT":
                                prompt_note = note
                            else:
                                sys_notes.append(f"System: {note}")
                        continue

                    parsed_lines.append((parsed.tag, parsed.text))
                    ui.stream_line(parsed.tag, parsed.text)

        if active_tool is not None:
            flush_tool()
        ui.channel_footer("AI", "Agent output")

        completion_seen = any(COMPLETION_MARKER in line for line in output_lines)

        if parser is not None:
            if (sys_info or sys_notes) and config.ai_sys_mode == "summary" and iteration == 1:
                sys_lines = [f"{key}: {value}" for key, value in sys_info.items()]
                if prompt_note:
                    sys_notes.insert(0, f"Prompt: {prompt_note}")
                if sys_notes:
                    sys_lines.append("")
                    sys_lines.append("Notes:")
                    sys_lines.extend(f"- {note}" for note in sys_notes)
                _render_callout(ui, "SYS", "Session info", sys_lines)

            plan_lines = _extract_plan_lines(parsed_lines)
            plan_lines = [
                line if line.startswith(("-", "*")) else f"- {line}" for line in plan_lines
            ]
            _render_callout(ui, "PLAN", "Iteration intent", plan_lines)

            if tool_summaries and tool_mode == "summary":
                tool_lines = [f"- {_format_tool_summary(tool)}" for tool in tool_summaries]
                _render_callout(ui, "ACTIONS", "Tool activity", tool_lines)

            metrics = _extract_completion_metrics(parsed_lines)
            report_lines: list[str] = []
            if completion_seen:
                report_lines.append("Completion: yes")
            if metrics:
                report_lines.extend(f"{key}: {value}" for key, value in metrics.items())
            _render_callout(ui, "REPORT", "Completion report", report_lines)

        if is_codex_agent and config.ai_show_final:
            final_message = agent.final_message
            if final_message:
                ui.channel_header("AI", "Final message")
                for line in final_message.splitlines():
                    ui.stream_line("AI", line)
                ui.channel_footer("AI", "Final message")

        # Check for completion
        if completion_seen:
            ui.ok("Done")
            return LoopResult(completed=True, iterations=iteration, exit_code=0)

        # Enforce ALLOWED_PATHS
        if config.allowed_paths and is_repo:
            ok, _ = guards.enforce_allowed_paths(config, ui, cwd)
            if not ok:
                return LoopResult(completed=False, iterations=iteration, exit_code=1)

        # Interactive pause
        if config.interactive and ui.can_prompt():
            choice = ui.choose(
                "Iteration complete. What next?",
                ["Continue", "Skip interactive", "Quit"],
                default=0,
            )
            if choice == 1:
                # Disable interactive for remaining iterations
                config.interactive = False
            elif choice == 2:
                return LoopResult(completed=False, iterations=iteration, exit_code=0)

        # Sleep before next iteration (except on last)
        if iteration < config.max_iterations:
            time.sleep(config.sleep_seconds)

    # Max iterations reached
    ui.warn(f"Max iterations reached (no {COMPLETION_MARKER} seen)")
    return LoopResult(completed=False, iterations=config.max_iterations, exit_code=1)


def _determine_branch(config: RalphConfig) -> tuple[str | None, str | None]:
    """Determine which branch to use.

    Returns:
        Tuple of (branch_name, source) where:
        - branch_name: Branch to checkout, "" to skip, None if not configured
        - source: Source description (e.g. "from RALPH_BRANCH", "from PRD")
    """
    # If a branch is configured directly on the config, prefer it.
    # `ralph_branch_explicit` is used to indicate whether it came from RALPH_BRANCH/--branch.
    if config.ralph_branch is not None:
        if config.ralph_branch_explicit:
            return config.ralph_branch, "from RALPH_BRANCH"
        return config.ralph_branch, "default"

    # Try to get from PRD
    if config.prd_file.exists():
        try:
            prd = PRD.load(config.prd_file)
            if prd.branch_name:
                return prd.branch_name, "from PRD"
        except Exception:
            pass

    return None, None
