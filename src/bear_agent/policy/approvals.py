"""Scoped approval service.

Approvals are exact-fingerprint grants. A session/project grant lets the same
tool call be repeated without prompting; it does not authorize different
commands. Grants expire and are persisted so a resumed session can reuse them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.ids import new_id
from bear_agent.policy.approver import Approver
from bear_agent.policy.models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalScope,
)
from bear_agent.storage.security_repositories import ApprovalRecord, ApprovalRepository


def approval_fingerprint(tool_name: str, arguments: dict[str, object]) -> str:
    payload = json.dumps([tool_name, arguments], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class ApprovalService:
    def __init__(
        self,
        repository: ApprovalRepository,
        approver: Approver,
        *,
        clock: Clock | None = None,
        session_ttl_s: float = 86_400.0,
        project_ttl_s: float = 2_592_000.0,
    ) -> None:
        self.repository = repository
        self.approver = approver
        self.clock = clock or SystemClock()
        self.session_ttl_s = session_ttl_s
        self.project_ttl_s = project_ttl_s

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        fingerprint = request.fingerprint or approval_fingerprint(
            request.tool_name, request.arguments
        )
        now = self.clock.now()
        existing = await self.repository.find(
            fingerprint,
            session_id=request.session_id,
            project_id=request.project_id,
            now=now,
        )
        if existing is not None and existing.decision == "allow":
            return ApprovalDecision(
                True,
                ApprovalScope(existing.scope),
                f"reused persisted {existing.scope} approval",
            )
        decision = await self.approver.request(
            ApprovalRequest(
                tool_name=request.tool_name,
                arguments=request.arguments,
                reason=request.reason,
                risk=request.risk,
                session_id=request.session_id,
                project_id=request.project_id,
                fingerprint=fingerprint,
                scope_options=request.scope_options,
            )
        )
        if not decision.approved or decision.scope == ApprovalScope.DENY:
            return ApprovalDecision(
                False,
                ApprovalScope.DENY,
                decision.reason or "approval denied",
            )
        scope = decision.scope
        if scope == ApprovalScope.PROJECT and not request.project_id:
            scope = ApprovalScope.SESSION
        if scope in (ApprovalScope.SESSION, ApprovalScope.PROJECT):
            ttl = self.project_ttl_s if scope == ApprovalScope.PROJECT else self.session_ttl_s
            record = ApprovalRecord(
                id=new_id("apr"),
                fingerprint=fingerprint,
                tool_name=request.tool_name,
                scope=scope.value,
                decision="allow",
                session_id=request.session_id if scope == ApprovalScope.SESSION else None,
                project_id=request.project_id if scope == ApprovalScope.PROJECT else None,
                reason=decision.reason,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl),
            )
            await self.repository.save(record)
        return ApprovalDecision(True, scope, decision.reason)

    async def list_recent(self, limit: int = 50) -> list[ApprovalRecord]:
        return await self.repository.list_recent(limit)
