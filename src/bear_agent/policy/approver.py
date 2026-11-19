"""Approver port and headless defaults."""

from __future__ import annotations

from typing import Protocol

from bear_agent.policy.models import ApprovalRequest


class Approver(Protocol):
    async def request(self, request: ApprovalRequest) -> bool:
        """Return True to allow the action, False to deny it."""
        ...


class AutoApprover:
    async def request(self, request: ApprovalRequest) -> bool:
        return True


class DenyingApprover:
    async def request(self, request: ApprovalRequest) -> bool:
        return False
