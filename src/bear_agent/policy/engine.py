"""Capability-based permission decisions.

Deny always wins. ``plan`` mode forbids every side effect; ``supervised`` asks
before writes/exec; ``auto`` allows workspace writes and asks for higher-risk
effects; ``unrestricted`` allows but still passes through path and sensitive
file guards.
"""

from __future__ import annotations

from typing import Protocol

from bear_agent.core.modes import PermissionMode, ToolSideEffect
from bear_agent.policy.models import DecisionType, PolicyDecision


class ToolLike(Protocol):
    side_effect: ToolSideEffect


class PermissionEngine:
    def __init__(self, mode: PermissionMode = PermissionMode.AUTO) -> None:
        self.mode = mode

    def set_mode(self, mode: PermissionMode) -> None:
        self.mode = mode

    def decide(self, tool: ToolLike, _arguments: object | None = None) -> PolicyDecision:
        effect = tool.side_effect
        if self.mode == PermissionMode.PLAN and effect != ToolSideEffect.READ:
            return PolicyDecision(
                DecisionType.DENY,
                f"plan mode blocks {effect.value} tools",
            )
        if effect == ToolSideEffect.READ:
            return PolicyDecision(DecisionType.ALLOW, "read-only tool")
        if self.mode == PermissionMode.PLAN:
            return PolicyDecision(DecisionType.DENY, "plan mode")
        if effect in (ToolSideEffect.EXEC, ToolSideEffect.NETWORK):
            if self.mode == PermissionMode.UNRESTRICTED:
                return PolicyDecision(DecisionType.ALLOW, "unrestricted mode")
            return PolicyDecision(
                DecisionType.ASK,
                f"{effect.value} requires approval in {self.mode.value} mode",
            )
        if self.mode == PermissionMode.SUPERVISED:
            return PolicyDecision(DecisionType.ASK, "writes require approval in supervised mode")
        return PolicyDecision(DecisionType.ALLOW, f"write allowed in {self.mode.value} mode")
