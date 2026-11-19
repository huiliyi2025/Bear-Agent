"""Tool registry with stable schema ordering.

Stable order matters for prompt-prefix caching: adding a tool should not
reshuffle every other tool's position in the request.
"""

from __future__ import annotations

from bear_agent.core.tool_spec import ToolSpec
from bear_agent.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool name must not be empty")
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.all()]

    def __len__(self) -> int:
        return len(self._tools)
