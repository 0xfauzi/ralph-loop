"""Agent implementations for Ralph."""

from ralph_py.agents.base import Agent
from ralph_py.agents.codex import CodexAgent
from ralph_py.agents.custom import CustomAgent

__all__ = ["Agent", "CodexAgent", "CustomAgent", "get_agent"]


def get_agent(
    agent_cmd: str | None = None,
    model: str | None = None,
    model_reasoning_effort: str | None = None,
) -> Agent:
    """Get appropriate agent based on configuration."""
    if agent_cmd:
        return CustomAgent(agent_cmd)
    return CodexAgent(model=model, reasoning_effort=model_reasoning_effort)
