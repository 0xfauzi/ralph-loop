"""Prompt templates for Ralph MCP tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from mcp import types

from ralph_py.mcp.schema import InvalidArgumentError, ValidationIssue


@dataclass(frozen=True)
class PromptTemplate:
    """Definition for a built-in MCP prompt."""

    name: str
    title: str
    description: str
    tool_name: str
    tool_input: Mapping[str, Any]
    arguments: list[types.PromptArgument] | None = None

    def to_prompt(self) -> types.Prompt:
        """Convert the template into an MCP Prompt definition."""
        return types.Prompt(
            name=self.name,
            title=self.title,
            description=self.description,
            arguments=self.arguments,
        )


_ROOT_ARGUMENT = types.PromptArgument(
    name="root",
    description="Absolute path to the project root.",
    required=True,
)


_PROMPT_TEMPLATES: tuple[PromptTemplate, ...] = (
    PromptTemplate(
        name="ralph.run_current",
        title="Run Ralph with the current prompt",
        description="Run the Ralph loop using scripts/ralph/prompt.md.",
        tool_name="ralph.run",
        tool_input={
            "root": "{root}",
            "prompt_file": "scripts/ralph/prompt.md",
            "prd_file": "scripts/ralph/prd.json",
            "allow_edits": False,
        },
        arguments=[_ROOT_ARGUMENT],
    ),
    PromptTemplate(
        name="ralph.init_scaffolding",
        title="Initialize Ralph scaffolding",
        description="Create scripts/ralph scaffolding in the target root.",
        tool_name="ralph.init",
        tool_input={
            "root": "{root}",
        },
        arguments=[_ROOT_ARGUMENT],
    ),
    PromptTemplate(
        name="ralph.understand_loop",
        title="Run Ralph understanding loop",
        description="Run Ralph in understanding mode using scripts/ralph/understand_prompt.md.",
        tool_name="ralph.understand",
        tool_input={
            "root": "{root}",
            "prompt_file": "scripts/ralph/understand_prompt.md",
            "prd_file": "scripts/ralph/prd.json",
            "allow_edits": False,
        },
        arguments=[_ROOT_ARGUMENT],
    ),
)

_PROMPT_BY_NAME = {template.name: template for template in _PROMPT_TEMPLATES}


def list_prompts() -> list[types.Prompt]:
    """Return the list of built-in MCP prompts."""
    return [template.to_prompt() for template in _PROMPT_TEMPLATES]


def get_prompt_template(name: str) -> PromptTemplate:
    """Lookup a prompt template by name."""
    template = _PROMPT_BY_NAME.get(name)
    if template is None:
        raise InvalidArgumentError(
            "Unknown prompt",
            [ValidationIssue("name", f"Unknown prompt: {name}")],
        )
    return template


def get_prompt_result(
    name: str, arguments: Mapping[str, str] | None = None
) -> types.GetPromptResult:
    """Return a prompt result for the requested prompt."""
    template = get_prompt_template(name)
    messages = _build_prompt_messages(template, arguments)
    return types.GetPromptResult(description=template.description, messages=messages)


def _build_prompt_messages(
    template: PromptTemplate, arguments: Mapping[str, str] | None
) -> list[types.PromptMessage]:
    rendered_input = _render_tool_input(template.tool_input, arguments or {})
    payload = json.dumps(rendered_input, indent=2, sort_keys=True)
    text = (
        f"Call the `{template.tool_name}` tool with the following inputs:\n"
        f"{payload}"
    )
    return [
        types.PromptMessage(
            role="user",
            content=types.TextContent(type="text", text=text),
        )
    ]


def _render_tool_input(value: Any, arguments: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return value.format_map(_SafeFormat(arguments))
    if isinstance(value, dict):
        return {key: _render_tool_input(val, arguments) for key, val in value.items()}
    if isinstance(value, list):
        return [_render_tool_input(item, arguments) for item in value]
    return value


class _SafeFormat(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
