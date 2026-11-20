"""Hook contracts shared by programmatic and external hooks.

Registration is the trust boundary: only ``tool_before`` hooks may deny, and
only ``model_before`` hooks may append context. A hook that tries to exceed its
granted capability has the decision ignored and the attempt recorded; it never
gains the capability retroactively.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from bear_agent.core.fencing import LeaseFence
from bear_agent.hooks.events import HookEvent

HookStatus = Literal["ok", "denied", "failed", "timeout", "skipped"]
FailMode = Literal["open", "closed"]
HookAction = Literal["continue", "deny"]

HookHandler = Callable[["HookInvocation"], Awaitable["HookDecision | None"]]


@dataclass(frozen=True, slots=True)
class HookPayload:
    """Everything an in-process hook may observe about an emit call."""

    event: HookEvent
    session_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    workspace: Path = field(default_factory=Path.cwd)
    fence: LeaseFence | None = None


@dataclass(frozen=True, slots=True)
class HookInvocation(HookPayload):
    """Payload passed to a specific registered hook."""

    hook_id: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class HookDecision:
    action: HookAction = "continue"
    reason: str = ""
    context_append: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in ("continue", "deny"):
            raise ValueError(f"invalid hook action: {self.action!r}")
        if not isinstance(self.reason, str) or not isinstance(self.context_append, str):
            raise ValueError("hook decision reason/context_append must be strings")


@dataclass(frozen=True, slots=True)
class HookExecution:
    hook_id: str
    source: str
    event: HookEvent
    status: HookStatus
    duration_ms: int
    error: str | None = None


@dataclass(slots=True)
class HookOutcome:
    denied: bool = False
    reason: str = ""
    context_append: str = ""
    results: list[HookExecution] = field(default_factory=list)
    failed_closed: bool = False

    @property
    def hook_ids(self) -> list[str]:
        return [result.hook_id for result in self.results]


@dataclass(slots=True)
class HookRegistration:
    """A hook and the capabilities explicitly granted to it.

    ``timeout_s <= 0`` means "use ``HooksConfig.timeout_s``". The capability
    checks are intentionally strict: a registration must target exactly
    ``tool_before`` to deny and exactly ``model_before`` to append context.
    """

    id: str
    events: frozenset[HookEvent]
    handler: HookHandler
    priority: int = 0
    timeout_s: float = 0.0
    fail_mode: FailMode = "open"
    can_deny: bool = False
    can_append_context: bool = False
    source: str = "programmatic"

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("hook id must be a non-empty string")
        if not isinstance(self.events, frozenset):
            self.events = frozenset(self.events)
        if not self.events:
            raise ValueError(f"hook {self.id!r} must subscribe to at least one event")
        if not all(isinstance(event, HookEvent) for event in self.events):
            raise ValueError(f"hook {self.id!r} has an invalid event")
        if not callable(self.handler):
            raise ValueError(f"hook {self.id!r} handler is not callable")
        if self.timeout_s < 0:
            raise ValueError(f"hook {self.id!r} timeout must not be negative")
        if self.fail_mode not in ("open", "closed"):
            raise ValueError(f"hook {self.id!r} has an invalid fail_mode")
        if self.can_deny and self.events != frozenset({HookEvent.TOOL_BEFORE}):
            raise ValueError(
                f"hook {self.id!r} may only deny when it subscribes to exactly tool_before"
            )
        if self.can_append_context and self.events != frozenset({HookEvent.MODEL_BEFORE}):
            raise ValueError(
                f"hook {self.id!r} may only append context when it subscribes to "
                "exactly model_before"
            )


__all__ = [
    "FailMode",
    "HookAction",
    "HookDecision",
    "HookExecution",
    "HookHandler",
    "HookInvocation",
    "HookOutcome",
    "HookPayload",
    "HookRegistration",
    "HookStatus",
]
