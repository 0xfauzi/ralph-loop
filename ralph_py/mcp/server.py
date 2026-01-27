"""MCP server core and tool handlers for Ralph."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import AnyUrl, BaseModel, ConfigDict

from ralph_py.init_cmd import (
    DEFAULT_CODEBASE_MAP,
    DEFAULT_PRD,
    DEFAULT_PRD_PROMPT,
    DEFAULT_PROGRESS,
    DEFAULT_PROMPT,
    DEFAULT_UNDERSTAND_PROMPT,
)
from ralph_py.mcp import logging as mcp_logging
from ralph_py.mcp import prompts, resources, runner, schema
from ralph_py.mcp.runner import ExecutionFailedError
from ralph_py.loop import LoopResult
from ralph_py.prd import PRD

_TOOL_RUN = "ralph.run"
_TOOL_UNDERSTAND = "ralph.understand"
_TOOL_INIT = "ralph.init"
_TOOL_VALIDATE = "ralph.validate"


class ToolOutput(BaseModel):
    """Structured output for MCP tool responses."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    exit_code: int
    log_path: str
    changed_files: list[str] | None = None


@dataclass(frozen=True)
class ToolContext:
    """Shared context for MCP tool handlers."""

    log_dir: Path


def build_server(*, root: Path, log_dir: Path) -> Server[dict[str, Any], dict[str, Any]]:
    """Create the MCP server with all Ralph tools, prompts, and resources."""
    server = Server("ralph")
    context = ToolContext(log_dir=log_dir)

    @server.list_resources()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_resources() -> list[types.Resource]:
        return resources.list_resources()

    @server.read_resource()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
        return resources.read_resource(root, str(uri))

    @server.list_prompts()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_prompts() -> list[types.Prompt]:
        return prompts.list_prompts()

    @server.get_prompt()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _get_prompt(
        name: str, arguments: Mapping[str, str] | None = None
    ) -> types.GetPromptResult:
        return prompts.get_prompt_result(name, arguments)

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[types.Tool]:
        return _build_tools()

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
        handler = _TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return _error_result(
                {
                    "code": "invalid_argument",
                    "message": f"Unknown tool: {tool_name}",
                    "details": [],
                }
            )
        return await anyio.to_thread.run_sync(handler, context, arguments)

    return server


def _build_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=_TOOL_RUN,
            description="Run the Ralph loop with explicit inputs.",
            inputSchema=_schema_for(schema.RunInputs),
            outputSchema=_schema_for(ToolOutput),
        ),
        types.Tool(
            name=_TOOL_UNDERSTAND,
            description="Run the Ralph understanding loop in read-only mode.",
            inputSchema=_schema_for(schema.UnderstandInputs),
            outputSchema=_schema_for(ToolOutput),
        ),
        types.Tool(
            name=_TOOL_INIT,
            description="Scaffold scripts/ralph in the provided root.",
            inputSchema=_schema_for(schema.InitInputs),
            outputSchema=_schema_for(ToolOutput),
        ),
        types.Tool(
            name=_TOOL_VALIDATE,
            description="Validate required Ralph files under the provided root.",
            inputSchema=_schema_for(schema.ValidateInputs),
            outputSchema=_schema_for(ToolOutput),
        ),
    ]


def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def _handle_run(context: ToolContext, arguments: dict[str, Any]) -> types.CallToolResult | dict[str, Any]:
    log_buffer = io.StringIO()
    summary = ""
    exit_code = 1
    changed_files: list[str] | None = None
    root = Path.cwd()

    try:
        inputs = schema.parse_input(schema.RunInputs, arguments)
        root = _resolve_root(inputs.root)
        with redirect_stdout(log_buffer), redirect_stderr(log_buffer):
            result = runner.run(inputs)
        exit_code = result.loop_result.exit_code
        summary = _summarize_loop_result(result.loop_result)
        if inputs.allow_edits:
            changed_files = result.changed_files
    except schema.InvalidArgumentError as exc:
        return _error_with_log(context, root, _TOOL_RUN, exc.to_payload(), log_buffer)
    except ExecutionFailedError as exc:
        return _error_with_log(context, root, _TOOL_RUN, exc.to_payload(), log_buffer)
    except Exception as exc:
        return _error_with_log(
            context,
            root,
            _TOOL_RUN,
            _execution_failed_payload(str(exc)),
            log_buffer,
        )

    return _success_with_log(
        context,
        root,
        _TOOL_RUN,
        summary,
        exit_code,
        log_buffer,
        changed_files=changed_files,
    )


def _handle_understand(
    context: ToolContext, arguments: dict[str, Any]
) -> types.CallToolResult | dict[str, Any]:
    log_buffer = io.StringIO()
    summary = ""
    exit_code = 1
    root = Path.cwd()

    try:
        inputs = schema.parse_input(schema.UnderstandInputs, arguments)
        root = _resolve_root(inputs.root)
        with redirect_stdout(log_buffer), redirect_stderr(log_buffer):
            result = runner.understand(inputs)
        exit_code = result.loop_result.exit_code
        summary = _summarize_loop_result(result.loop_result)
    except schema.InvalidArgumentError as exc:
        return _error_with_log(context, root, _TOOL_UNDERSTAND, exc.to_payload(), log_buffer)
    except ExecutionFailedError as exc:
        return _error_with_log(context, root, _TOOL_UNDERSTAND, exc.to_payload(), log_buffer)
    except Exception as exc:
        return _error_with_log(
            context,
            root,
            _TOOL_UNDERSTAND,
            _execution_failed_payload(str(exc)),
            log_buffer,
        )

    return _success_with_log(
        context,
        root,
        _TOOL_UNDERSTAND,
        summary,
        exit_code,
        log_buffer,
    )


def _handle_init(context: ToolContext, arguments: dict[str, Any]) -> types.CallToolResult | dict[str, Any]:
    log_buffer = io.StringIO()
    summary = ""
    root = Path.cwd()

    try:
        inputs = schema.parse_input(schema.InitInputs, arguments)
        schema.validate_init_inputs(inputs)
        root = _resolve_root(inputs.root)
        with redirect_stdout(log_buffer), redirect_stderr(log_buffer):
            created, overwritten, skipped = _write_scaffold(root, force=inputs.force)
        summary = _summarize_init(created, overwritten, skipped)
    except schema.InvalidArgumentError as exc:
        return _error_with_log(context, root, _TOOL_INIT, exc.to_payload(), log_buffer)
    except Exception as exc:
        return _error_with_log(
            context,
            root,
            _TOOL_INIT,
            _execution_failed_payload(str(exc)),
            log_buffer,
        )

    return _success_with_log(
        context,
        root,
        _TOOL_INIT,
        summary,
        exit_code=0,
        log_buffer=log_buffer,
    )


def _handle_validate(
    context: ToolContext, arguments: dict[str, Any]
) -> types.CallToolResult | dict[str, Any]:
    log_buffer = io.StringIO()
    root = Path.cwd()

    try:
        inputs = schema.parse_input(schema.ValidateInputs, arguments)
        schema.validate_validate_inputs(inputs)
        root = _resolve_root(inputs.root)
        with redirect_stdout(log_buffer), redirect_stderr(log_buffer):
            prompt_path = _resolve_input_path(
                root,
                inputs.prompt_file,
                root / "scripts" / "ralph" / "prompt.md",
                "prompt_file",
            )
            prd_path = _resolve_input_path(
                root,
                inputs.prd_file,
                root / "scripts" / "ralph" / "prd.json",
                "prd_file",
            )
            _validate_files(root, prompt_path, prd_path)
    except schema.InvalidArgumentError as exc:
        return _error_with_log(context, root, _TOOL_VALIDATE, exc.to_payload(), log_buffer)
    except Exception as exc:
        return _error_with_log(
            context,
            root,
            _TOOL_VALIDATE,
            _execution_failed_payload(str(exc)),
            log_buffer,
        )

    summary = "Validation succeeded for required Ralph files."
    return _success_with_log(
        context,
        root,
        _TOOL_VALIDATE,
        summary,
        exit_code=0,
        log_buffer=log_buffer,
    )


_TOOL_HANDLERS: dict[str, Callable[[ToolContext, dict[str, Any]], types.CallToolResult | dict[str, Any]]] = {
    _TOOL_RUN: _handle_run,
    _TOOL_UNDERSTAND: _handle_understand,
    _TOOL_INIT: _handle_init,
    _TOOL_VALIDATE: _handle_validate,
}


def _success_with_log(
    context: ToolContext,
    root: Path,
    tool_name: str,
    summary: str,
    exit_code: int,
    log_buffer: io.StringIO,
    *,
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    log_text = _normalize_log_text(log_buffer, summary)
    artifacts = mcp_logging.write_log(
        root=root,
        tool=tool_name,
        summary=summary,
        exit_code=exit_code,
        log_text=log_text,
        log_dir=context.log_dir,
    )
    return mcp_logging.build_tool_payload(
        summary=summary,
        exit_code=exit_code,
        log_path=artifacts.log_path,
        changed_files=changed_files,
    )


def _error_with_log(
    context: ToolContext,
    root: Path,
    tool_name: str,
    payload: dict[str, Any],
    log_buffer: io.StringIO,
) -> types.CallToolResult:
    message = str(payload.get("message", "Tool execution failed"))
    log_text = _normalize_log_text(log_buffer, message)
    artifacts = mcp_logging.write_log(
        root=root,
        tool=tool_name,
        summary=message,
        exit_code=1,
        log_text=log_text,
        log_dir=context.log_dir,
    )
    payload = dict(payload)
    payload["log_path"] = str(artifacts.log_path)
    return _error_result(payload)


def _error_result(payload: dict[str, Any]) -> types.CallToolResult:
    text = json.dumps(payload, indent=2, sort_keys=True)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=payload,
        isError=True,
    )


def _execution_failed_payload(message: str) -> dict[str, Any]:
    return {"code": "execution_failed", "message": message, "details": []}


def _summarize_loop_result(result: LoopResult) -> str:
    status = "completed" if result.completed else "stopped"
    return f"Loop {status} after {result.iterations} iterations (exit {result.exit_code})."


def _summarize_init(
    created: list[Path],
    overwritten: list[Path],
    skipped: list[Path],
) -> str:
    return (
        "Scaffolded scripts/ralph - "
        f"created {len(created)}, overwritten {len(overwritten)}, skipped {len(skipped)}."
    )


def _write_scaffold(root: Path, *, force: bool) -> tuple[list[Path], list[Path], list[Path]]:
    ralph_dir = root / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)

    files: list[tuple[Path, str]] = [
        (ralph_dir / "prompt.md", DEFAULT_PROMPT),
        (ralph_dir / "prd_prompt.txt", DEFAULT_PRD_PROMPT),
        (ralph_dir / "prd.json", json.dumps(DEFAULT_PRD, indent=2) + "\n"),
        (ralph_dir / "progress.txt", DEFAULT_PROGRESS),
        (ralph_dir / "codebase_map.md", DEFAULT_CODEBASE_MAP),
        (ralph_dir / "understand_prompt.md", DEFAULT_UNDERSTAND_PROMPT),
    ]

    created: list[Path] = []
    overwritten: list[Path] = []
    skipped: list[Path] = []

    for path, content in files:
        if path.exists() and not force:
            skipped.append(path)
            continue
        if path.exists() and force:
            overwritten.append(path)
        if not path.exists():
            created.append(path)
        path.write_text(content)

    return created, overwritten, skipped


def _validate_files(root: Path, prompt_path: Path, prd_path: Path) -> None:
    issues: list[schema.ValidationIssue] = []

    ralph_dir = root / "scripts" / "ralph"
    required_paths = {
        "prompt_file": ralph_dir / "prompt.md",
        "prd_file": ralph_dir / "prd.json",
        "prd_prompt_file": ralph_dir / "prd_prompt.txt",
        "progress_file": ralph_dir / "progress.txt",
        "codebase_map_file": ralph_dir / "codebase_map.md",
        "understand_prompt_file": ralph_dir / "understand_prompt.md",
    }

    for field, path in required_paths.items():
        if not path.exists():
            issues.append(schema.ValidationIssue(field, f"Required file not found: {path}"))

    extra_paths = {
        "prompt_file": prompt_path,
        "prd_file": prd_path,
    }

    for field, path in extra_paths.items():
        if path != required_paths[field] and not path.exists():
            issues.append(schema.ValidationIssue(field, f"File not found: {path}"))

    prd_target = required_paths["prd_file"]
    if prd_target.exists():
        _validate_prd_schema(prd_target, issues)
    if prd_path != prd_target and prd_path.exists():
        _validate_prd_schema(prd_path, issues)

    if issues:
        raise schema.InvalidArgumentError("Validation failed", issues)


def _validate_prd_schema(prd_path: Path, issues: list[schema.ValidationIssue]) -> None:
    try:
        data = json.loads(prd_path.read_text())
    except json.JSONDecodeError as exc:
        issues.append(schema.ValidationIssue("prd_file", f"PRD JSON invalid: {exc}"))
    else:
        errors = PRD.validate_schema(data)
        if errors:
            for error in errors:
                issues.append(schema.ValidationIssue("prd_file", error))


def _resolve_root(raw_root: str) -> Path:
    root_path = Path(raw_root).expanduser()
    if not root_path.is_absolute():
        raise schema.InvalidArgumentError(
            "Invalid tool input",
            [schema.ValidationIssue("root", "root must be an absolute path.")],
        )
    resolved = root_path.resolve()
    if not resolved.exists():
        raise schema.InvalidArgumentError(
            "Invalid tool input",
            [schema.ValidationIssue("root", "root must exist.")],
        )
    if not resolved.is_dir():
        raise schema.InvalidArgumentError(
            "Invalid tool input",
            [schema.ValidationIssue("root", "root must be a directory.")],
        )
    return resolved


def _resolve_input_path(
    root: Path,
    raw_value: str | None,
    default: Path,
    field: str,
) -> Path:
    if raw_value is None or raw_value == "":
        candidate = default
    else:
        candidate = Path(raw_value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
    resolved = candidate.resolve()
    if not _is_within_root(resolved, root):
        raise schema.InvalidArgumentError(
            "Invalid tool input",
            [schema.ValidationIssue(field, f"{field} must resolve within root.")],
        )
    return resolved


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _normalize_log_text(log_buffer: io.StringIO, fallback: str) -> str:
    log_text = log_buffer.getvalue()
    if log_text.strip():
        return log_text
    return fallback
