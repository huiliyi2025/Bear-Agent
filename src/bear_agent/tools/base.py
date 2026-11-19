"""Tool contract.

A tool is a typed argument model, a JSON Schema derived from it, and an async
executor. The runtime never inspects provider-specific tool payloads; it passes
validated Pydantic models to tools and serializes ``ToolResult`` back.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from bear_agent.core.cancel import CancelToken
from bear_agent.core.modes import ToolSideEffect
from bear_agent.core.tool_spec import ToolSpec
from bear_agent.policy.path_guard import PathGuard


class PreflightAction(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PreflightDecision:
    action: PreflightAction
    reason: str
    risk: str = "low"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    path: str
    scope: Literal["file", "tree"] = "file"
    mode: Literal["exclusive", "shared_read"] = "exclusive"


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
    # Set by the executor after policy/approval allow execution. Tools with
    # their own preflight (shell) use this to refuse direct-call bypasses.
    approved: bool = False
    # Re-checks the write claim immediately before a file mutation. The
    # executor sets this after acquiring claims; tools call it at the write
    # site, closing the check-to-write window as much as the OS allows.
    assert_write_allowed: Callable[[str], Awaitable[None]] | None = None


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

    async def preflight(self, args: BaseModel, ctx: ToolContext) -> PreflightDecision | None:
        """Optional execution-layer check before permission/approval.

        A tool may escalate, allow or deny a specific call based on its own
        semantics (e.g. a shell command). Returning None keeps the generic
        permission matrix behavior.
        """
        return None

    def claim_paths(self, args: BaseModel) -> list[ClaimRequest]:
        """Files or trees this call will write; empty for read-only tools."""
        return []

    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<Tool {self.name} side_effect={self.side_effect.value}>"
