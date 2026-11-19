"""Approver port and headless defaults."""

from __future__ import annotations

from typing import Protocol

from bear_agent.policy.models import ApprovalDecision, ApprovalRequest, ApprovalScope


class Approver(Protocol):
    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        """Return an allow decision with a scope, or a deny decision."""
        ...


class AutoApprover:
    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(True, ApprovalScope.ONCE, "auto-approved")


class DenyingApprover:
    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(False, ApprovalScope.DENY, "denied by policy")
