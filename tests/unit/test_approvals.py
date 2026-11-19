from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bear_agent.core.clock import FakeClock
from bear_agent.policy.approvals import ApprovalService, approval_fingerprint
from bear_agent.policy.models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalScope,
)
from bear_agent.storage.database import Database
from bear_agent.storage.security_repositories import ApprovalRepository


class FakeApprover:
    def __init__(self, scope: ApprovalScope, approved: bool = True) -> None:
        self.scope = scope
        self.approved = approved
        self.calls = 0

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        self.calls += 1
        return ApprovalDecision(self.approved, self.scope, "fake")


def _request(fingerprint: str = "fp", project_id: str | None = "project-1") -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="bash",
        arguments={"command": "echo hi"},
        reason="test",
        risk="moderate",
        session_id="session-1",
        project_id=project_id,
        fingerprint=fingerprint,
    )


@pytest.mark.asyncio
async def test_once_is_not_reused(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        approver = FakeApprover(ApprovalScope.ONCE)
        service = ApprovalService(ApprovalRepository(db), approver)
        assert (await service.request(_request())).approved
        assert (await service.request(_request())).approved
        assert approver.calls == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_session_approval_is_persisted_and_reused(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        approver = FakeApprover(ApprovalScope.SESSION)
        service = ApprovalService(ApprovalRepository(db), approver)
        first = await service.request(_request())
        assert first.approved and first.scope == ApprovalScope.SESSION
        second = await service.request(_request())
        assert second.approved and approver.calls == 1
        # A fresh service instance reads the persisted grant.
        third = await ApprovalService(ApprovalRepository(db), approver).request(_request())
        assert third.approved and approver.calls == 1
        # A different fingerprint is not covered.
        other = await service.request(_request(fingerprint="other"))
        assert other.approved and approver.calls == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_project_approval_is_scoped(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        approver = FakeApprover(ApprovalScope.PROJECT)
        service = ApprovalService(ApprovalRepository(db), approver)
        await service.request(_request(project_id="p1"))
        await service.request(_request(project_id="p1"))
        assert approver.calls == 1
        await service.request(_request(project_id="p2"))
        assert approver.calls == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_approval_expires(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        clock = FakeClock(datetime.now(UTC))
        approver = FakeApprover(ApprovalScope.SESSION)
        service = ApprovalService(
            ApprovalRepository(db),
            approver,
            clock=clock,
            session_ttl_s=10,
        )
        await service.request(_request())
        await service.request(_request())
        assert approver.calls == 1
        clock.advance(20)
        await service.request(_request())
        assert approver.calls == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_denial_is_not_persisted(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        approver = FakeApprover(ApprovalScope.SESSION, approved=False)
        service = ApprovalService(ApprovalRepository(db), approver)
        assert not (await service.request(_request())).approved
        assert not (await service.request(_request())).approved
        assert approver.calls == 2
    finally:
        db.close()


def test_fingerprint_is_stable_and_argument_sensitive() -> None:
    a = approval_fingerprint("bash", {"command": "echo hi", "cwd": "."})
    b = approval_fingerprint("bash", {"cwd": ".", "command": "echo hi"})
    c = approval_fingerprint("bash", {"command": "echo bye", "cwd": "."})
    assert a == b
    assert a != c


@pytest.mark.asyncio
async def test_approved_true_with_deny_scope_is_rejected(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:

        class BadApprover:
            async def request(self, request: ApprovalRequest) -> ApprovalDecision:
                return ApprovalDecision(True, ApprovalScope.DENY, "bad")

        service = ApprovalService(ApprovalRepository(db), BadApprover())
        decision = await service.request(_request())
        assert not decision.approved
    finally:
        db.close()
