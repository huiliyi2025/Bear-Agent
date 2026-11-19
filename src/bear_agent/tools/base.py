"""Tool contract.

A tool is a typed argument model, a JSON Schema derived from it, and an async
executor. The runtime never inspects provider-specific tool payloads; it passes
validated Pydantic models to tools and serializes ``ToolResult`` back.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from bear_agent.core.cancel import CancelToken
from bear_agent.core.modes import ToolSideEffect
from bear_agent.core.tool_spec import ToolSpec
from bear_agent.policy.path_guard import PathGuard


class ToolResult(BaseModel):
    content: str
    is_error: bool = False
    error_kind: str | None = None
    display: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False


@dataclass(slots=True)
class ToolContext:
    cwd: Path
    session_id: str
    cancel: CancelToken
    path_guard: PathGuard
    max_output_chars: int = 24_000
    extras: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    name: str
    description: str
    args_model: type[BaseModel]
    side_effect: ToolSideEffect = ToolSideEffect.READ
    idempotent: bool = True
    concurrency_safe: bool = True
    timeout_s: float = 60.0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
        )

    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<Tool {self.name} side_effect={self.side_effect.value}>"
