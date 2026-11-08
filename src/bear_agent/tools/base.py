"""Tool abstraction. A tool is a name + JSON Schema + an executable function,
plus a permission level consumed by the policy layer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel

from bear_agent.llm.base import ToolSpec

Permission = Literal["read_only", "modify", "dangerous"]

ToolFn = Callable[[dict[str, Any]], "ToolResult"]


class ToolResult(BaseModel):
    """Result of one tool execution; also the payload written back to the model."""

    ok: bool = True
    content: str = ""
    error: str | None = None

    def to_content(self) -> str:
        if not self.ok:
            return f"[tool error] {self.error}"
        return self.content or "(no output)"


class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        fn: ToolFn,
        permission: Permission = "read_only",
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.permission = permission
        self.fn = fn

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name, description=self.description, parameters=self.parameters
        )

    def call(self, arguments: dict[str, Any]) -> ToolResult:
        """Execute with error capture so a failing tool never kills the loop."""
        try:
            return self.fn(arguments)
        except Exception as exc:  # noqa: BLE001 - defensive boundary, keep the loop alive
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def __repr__(self) -> str:
        return f"<Tool name={self.name!r} permission={self.permission}>"
