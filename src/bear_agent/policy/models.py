"""Policy value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DecisionType(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: DecisionType
    reason: str


class ApprovalScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    PROJECT = "project"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool
    scope: ApprovalScope = ApprovalScope.DENY
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    risk: str = "moderate"
    session_id: str = ""
    project_id: str | None = None
    fingerprint: str = ""
    scope_options: tuple[ApprovalScope, ...] = (
        ApprovalScope.ONCE,
        ApprovalScope.SESSION,
        ApprovalScope.PROJECT,
    )
