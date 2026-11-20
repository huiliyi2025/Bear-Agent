from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bear_agent.audit.service import AuditService
from bear_agent.config.models import CheckpointConfig, RecoveryConfig
from bear_agent.core.clock import FakeClock
from bear_agent.core.errors import RecoveryError, SessionLeaseError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.ids import new_id
from bear_agent.core.messages import AssistantMessage, ToolCall, ToolMessage
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.policy.path_guard import PathGuard
from bear_agent.sessions.checkpoints import CheckpointService
from bear_agent.sessions.claims import ClaimService
from bear_agent.sessions.recovery import RecoveryService
from bear_agent.sessions.service import SessionService
from bear_agent.shell.process_registry import (
    DefaultTerminator,
    ProcessRegistry,
    TerminationResult,
)
from bear_agent.storage.continuity_repositories import (
    AnchorRepository,
    CheckpointRepository,
    CompactionRepository,
    ContextStateRepository,
    ProcessRecord,
    ProcessRepository,
)
from bear_agent.storage.database import Database
from bear_agent.storage.security_repositories import ClaimRepository


def _tool_content(message) -> str:
    result = message.tool_result()
    return result.content if result is not None else ""


class FakeTerminator:
    def __init__(self, terminated: bool = True, reason: str = "fake_terminated") -> None:
        self.terminated = terminated
        self.reason = reason
        self.calls: list[ProcessRecord] = []

    async def terminate(self, record: ProcessRecord) -> TerminationResult:
        self.calls.append(record)
        return TerminationResult(self.terminated, self.reason, record.pid)


class Harness:
    def __init__(self, tmp_path: Path, *, require_audit: bool = True):
        self.tmp_path = tmp_path
        self.db = Database(tmp_path / "bear.db")
        self.clock = FakeClock(datetime.now(UTC))
        self.sessions = SessionService(self.db, self.clock)
        self.recorder = EventRecorder(self.sessions, EventBus())
        self.audit = AuditService(self.db, clock=self.clock)
        self.checkpoints = CheckpointService(
            sessions=self.sessions,
            checkpoints=CheckpointRepository(self.db),
            compactions=CompactionRepository(self.db),
            anchors=AnchorRepository(self.db),
            context_state=ContextStateRepository(self.db),
            recorder=self.recorder,
            audit=self.audit,
            config=CheckpointConfig(),
            clock=self.clock,
        )
        self.terminator = FakeTerminator()
        self.processes = ProcessRegistry(
            ProcessRepository(self.db),
            clock=self.clock,
            terminator=self.terminator,
            recorder=self.recorder,
            audit=self.audit,
        )
        self.claims = ClaimService(
            ClaimRepository(self.db), PathGuard(tmp_path), clock=self.clock, ttl_seconds=600
        )
        self.recovery = RecoveryService(
            sessions=self.sessions,
            claims=self.claims,
            processes=self.processes,
            checkpoints=self.checkpoints,
            recorder=self.recorder,
            audit=self.audit,
            config=RecoveryConfig(require_audit=require_audit),
            clock=self.clock,
        )

    async def new_session(self):
        return await self.sessions.create(cwd=self.tmp_path, model="m", provider="p")

    async def crashed_tool(
        self, session_id: str, *, call_id: str = "call-1", idempotent: bool = True
    ) -> None:
        await self.sessions.append_user_message(session_id, "do the work")
        assistant = AssistantMessage.with_tool_calls(
            None,
            [ToolCall(id=call_id, name="bash", arguments={"command": "echo hi"})],
        )
        await self.sessions.append_assistant_message(session_id, assistant)
        await self.sessions.tool_calls.start(
            session_id,
            "turn-1",
            call_id,
            "bash",
            {"command": "echo hi"},
            idempotent,
        )

    async def acquire(self, session_id: str, owner: str = "owner-b") -> LeaseFence:
        epoch = await self.sessions.acquire_lease(session_id, owner, 300)
        return LeaseFence(owner, epoch)

    async def event_types(self, session_id: str) -> list[EventType]:
        return [event.type for event in await self.sessions.events_for(session_id)]

    async def audit_actions(self) -> list[str]:
        return [entry.action for entry in await self.audit.tail(limit=200)]

    def close(self) -> None:
        self.db.close()


@pytest.mark.asyncio
async def test_idle_session_is_a_cheap_noop(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        await harness.sessions.append_user_message(session.id, "hello")
        fence = await harness.acquire(session.id)

        report = await harness.recovery.recover_if_needed(session.id, fence=fence)

        assert report.recovered is False
        assert report.running_tools == 0
        assert report.synthesized_tools == []
        types = await harness.event_types(session.id)
        assert EventType.RECOVERY_STARTED not in types
        assert EventType.RECOVERY_COMPLETED not in types
        actions = await harness.audit_actions()
        assert "recovery.started" not in actions
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_running_idempotent_tool_gets_safe_retry_synthetic_result(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        await harness.crashed_tool(session.id, idempotent=True)
        fence = await harness.acquire(session.id)

        report = await harness.recovery.recover_if_needed(
            session.id, fence=fence, run_id="run-recovery"
        )

        assert report.recovered is True
        assert report.running_tools == 1
        assert report.synthesized_tools == ["call-1"]
        assert report.audit_verified is True

        messages = await harness.sessions.load_messages(session.id)
        assert messages[-1].role == "tool"
        content = _tool_content(messages[-1])
        assert "interrupted" in content.lower()
        assert "status unknown" in content.lower()
        assert "NOT automatically retried" in content
        assert "tool is idempotent; retrying is considered safe" in content
        assert messages[-1].tool_result() is not None
        assert messages[-1].tool_result().tool_call_id == "call-1"

        row = await harness.sessions.tool_calls.get("call-1")
        assert row is not None and row["status"] == "unknown"

        types = await harness.event_types(session.id)
        assert EventType.RECOVERY_STARTED in types
        assert EventType.RECOVERY_TOOL_RECONCILED in types
        assert EventType.RECOVERY_COMPLETED in types
        assert EventType.TOOL_CALL_COMPLETED not in types

        actions = await harness.audit_actions()
        assert "recovery.started" in actions
        assert "recovery.tool_reconciled" in actions
        assert "recovery.completed" in actions
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_non_idempotent_tool_is_not_automatically_retried(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        await harness.crashed_tool(session.id, idempotent=False)
        fence = await harness.acquire(session.id)

        report = await harness.recovery.recover_if_needed(session.id, fence=fence)

        assert report.synthesized_tools == ["call-1"]
        messages = await harness.sessions.load_messages(session.id)
        content = _tool_content(messages[-1])
        assert "non-idempotent or side-effecting" in content
        assert "inspect the workspace and ask the user before retrying" in content
        assert "NOT automatically retried" in content

        row = await harness.sessions.tool_calls.get("call-1")
        assert row is not None
        assert row["status"] == "unknown"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_partial_turn_repairs_only_missing_call(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        await harness.sessions.append_user_message(session.id, "two calls")
        assistant = AssistantMessage.with_tool_calls(
            None,
            [
                ToolCall(id="call-1", name="read_file", arguments={"path": "a"}),
                ToolCall(id="call-2", name="read_file", arguments={"path": "b"}),
            ],
        )
        await harness.sessions.append_assistant_message(session.id, assistant)
        await harness.sessions.tool_calls.start(
            session.id, "turn-1", "call-1", "read_file", {"path": "a"}, True
        )
        await harness.sessions.tool_calls.start(
            session.id, "turn-1", "call-2", "read_file", {"path": "b"}, True
        )
        await harness.sessions.append_tool_message(
            session.id, ToolMessage.of("call-1", "a content")
        )
        await harness.sessions.tool_calls.finish("call-1", "completed")

        fence = await harness.acquire(session.id)
        report = await harness.recovery.recover_if_needed(session.id, fence=fence)

        assert report.synthesized_tools == ["call-2"]
        messages = await harness.sessions.load_messages(session.id)
        tool_messages = [message for message in messages if message.role == "tool"]
        assert [message.tool_result().tool_call_id for message in tool_messages] == [
            "call-1",
            "call-2",
        ]
        assert _tool_content(tool_messages[0]) == "a content"
        assert "NOT automatically retried" in _tool_content(tool_messages[1])
        assert report.issues == []
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_dangling_call_in_the_middle_is_reported_not_repaired(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        await harness.sessions.append_user_message(session.id, "first")
        assistant = AssistantMessage.with_tool_calls(
            None, [ToolCall(id="call-mid", name="bash", arguments={"command": "ls"})]
        )
        await harness.sessions.append_assistant_message(session.id, assistant)
        await harness.sessions.tool_calls.start(
            session.id, "turn-1", "call-mid", "bash", {"command": "ls"}, False
        )
        await harness.sessions.append_user_message(session.id, "move on")
        await harness.sessions.append_assistant_message(
            session.id, AssistantMessage.of("continued")
        )
        before = await harness.sessions.load_messages(session.id)

        fence = await harness.acquire(session.id)
        report = await harness.recovery.recover_if_needed(session.id, fence=fence)

        assert report.synthesized_tools == []
        assert any("not at the transcript tail" in issue for issue in report.issues)
        after = await harness.sessions.load_messages(session.id)
        assert len(after) == len(before)
        assert all(message.role != "tool" for message in after)
        row = await harness.sessions.tool_calls.get("call-mid")
        assert row is not None and row["status"] == "running"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_stale_claims_released_current_owner_kept(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        epoch_old = await harness.sessions.acquire_lease(session.id, "owner-a", 300)
        await harness.sessions.release_lease(session.id, "owner-a", epoch_old)
        fence = await harness.acquire(session.id, "owner-b")
        await harness.claims.acquire(session.id, fence.owner, "new.txt")

        now = datetime.now(UTC)
        await harness.db.run(
            lambda conn: conn.execute(
                """
                INSERT INTO file_claims (
                    id, session_id, lease_owner, path, scope, mode, acquired_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("claim"),
                    session.id,
                    "owner-a",
                    "old.txt",
                    "file",
                    "exclusive",
                    now.isoformat(),
                    (now + timedelta(seconds=600)).isoformat(),
                ),
            )
        )
        await harness.crashed_tool(session.id, call_id="call-claims")

        report = await harness.recovery.recover_if_needed(session.id, fence=fence)

        assert report.released_claims == 1
        old = await harness.claims.list_for_owner(session.id, "owner-a")
        assert old == []
        current = await harness.claims.list_for_owner(session.id, fence.owner)
        assert [claim.path for claim in current] == ["new.txt"]
        types = await harness.event_types(session.id)
        assert EventType.RECOVERY_CLAIMS_RELEASED in types
        actions = await harness.audit_actions()
        assert "recovery.claims.released" in actions
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_orphan_process_is_terminated(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        record = await harness.processes.register(
            session.id, "call-proc", "dead-owner", 424242, None, "sleep 100"
        )
        fence = await harness.acquire(session.id)

        report = await harness.recovery.recover_if_needed(session.id, fence=fence)

        assert report.recovered is True
        assert report.terminated_processes == [424242]
        assert harness.terminator.calls[0].pid == 424242
        status = await harness.db.run(
            lambda conn: conn.execute(
                "SELECT status FROM process_records WHERE id = ?", (record.id,)
            ).fetchone()["status"]
        )
        assert status == "terminated"
        types = await harness.event_types(session.id)
        assert EventType.RECOVERY_PROCESS_TERMINATED in types
        actions = await harness.audit_actions()
        assert "recovery.process.terminated" in actions
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_refused_termination_marks_process_unknown(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        harness.terminator.terminated = False
        harness.terminator.reason = "identity_unverified"
        session = await harness.new_session()
        record = await harness.processes.register(
            session.id, None, "dead-owner", 515151, None, "sleep 100"
        )
        fence = await harness.acquire(session.id)

        report = await harness.recovery.recover_if_needed(session.id, fence=fence)

        assert report.terminated_processes == []
        assert any("identity_unverified" in issue for issue in report.issues)
        status = await harness.db.run(
            lambda conn: conn.execute(
                "SELECT status FROM process_records WHERE id = ?", (record.id,)
            ).fetchone()["status"]
        )
        assert status == "unknown"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_current_owner_process_is_not_treated_as_orphan(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        fence = await harness.acquire(session.id)
        record = await harness.processes.register(
            session.id, None, fence.owner, 616161, None, "current owner command"
        )

        report = await harness.recovery.recover_if_needed(session.id, fence=fence)

        assert report.terminated_processes == []
        assert harness.terminator.calls == []
        status = await harness.db.run(
            lambda conn: conn.execute(
                "SELECT status FROM process_records WHERE id = ?", (record.id,)
            ).fetchone()["status"]
        )
        assert status == "running"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_stale_recovery_cannot_terminate_after_takeover(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        record = await harness.processes.register(
            session.id, None, "dead-owner", 717171, None, "orphan"
        )
        old_fence = await harness.acquire(session.id, "owner-a")
        await harness.sessions.release_lease(session.id, "owner-a", old_fence.epoch)
        await harness.acquire(session.id, "owner-b")

        with pytest.raises(SessionLeaseError):
            await harness.processes.terminate_orphans(
                session.id, lease_owner="owner-a", fence=old_fence
            )
        assert harness.terminator.calls == []
        status = await harness.db.run(
            lambda conn: conn.execute(
                "SELECT status FROM process_records WHERE id = ?", (record.id,)
            ).fetchone()["status"]
        )
        assert status == "running"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_tampered_audit_chain_fails_closed(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        await harness.audit.append(
            actor="agent", action="seed.one", detail={"n": 1}, session_id=session.id
        )
        await harness.audit.append(
            actor="agent", action="seed.two", detail={"n": 2}, session_id=session.id
        )
        await harness.db.run(
            lambda conn: conn.execute("UPDATE audit_log SET hash = 'tampered' WHERE id = 2")
        )
        await harness.crashed_tool(session.id)
        fence = await harness.acquire(session.id)

        with pytest.raises(RecoveryError, match="audit chain verification failed"):
            await harness.recovery.recover_if_needed(session.id, fence=fence)

        types = await harness.event_types(session.id)
        assert EventType.RECOVERY_STARTED in types
        assert EventType.RECOVERY_FAILED in types
        assert EventType.RECOVERY_COMPLETED not in types
        # Nothing was repaired after the fail-closed abort.
        row = await harness.sessions.tool_calls.get("call-1")
        assert row is not None and row["status"] == "running"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_concurrent_recovery_uses_the_lease_as_lock(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    other_db = Database(tmp_path / "bear.db")
    other_sessions = SessionService(other_db, harness.clock)
    try:
        session = await harness.new_session()
        await harness.crashed_tool(session.id)
        fence = await harness.acquire(session.id, "owner-a")

        with pytest.raises(SessionLeaseError):
            await other_sessions.acquire_lease(session.id, "owner-b", 300)

        report = await harness.recovery.recover_if_needed(session.id, fence=fence)
        assert report.recovered is True
        assert report.synthesized_tools == ["call-1"]

        types = await harness.event_types(session.id)
        assert types.count(EventType.RECOVERY_STARTED) == 1
        assert types.count(EventType.RECOVERY_COMPLETED) == 1
    finally:
        other_db.close()
        harness.close()


@pytest.mark.asyncio
async def test_stale_fence_on_recovery_write_is_rejected(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        await harness.crashed_tool(session.id)
        fence = await harness.acquire(session.id, "owner-a")
        # A newer owner takes over before recovery writes anything.
        await harness.sessions.release_lease(session.id, "owner-a", fence.epoch)
        await harness.acquire(session.id, "owner-b")

        with pytest.raises(RecoveryError):
            await harness.recovery.recover_if_needed(session.id, fence=fence)
        row = await harness.sessions.tool_calls.get("call-1")
        assert row is not None and row["status"] == "running"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_default_terminator_refuses_unsafe_pids(tmp_path: Path) -> None:
    terminator = DefaultTerminator(platform="linux")
    base = dict(
        id="proc_1",
        session_id="ses_1",
        tool_call_id=None,
        lease_owner="owner",
        pid=1,
        pgid=None,
        command_preview=None,
        created_at=datetime.now(UTC),
        finished_at=None,
        status="running",
    )
    refused = await terminator.terminate(ProcessRecord(**base))
    assert refused.terminated is False
    assert refused.reason == "refused_pid"

    self_record = await terminator.terminate(ProcessRecord(**{**base, "pid": os.getpid()}))
    assert self_record.terminated is False
    assert self_record.reason == "refused_self"


@pytest.mark.asyncio
async def test_default_terminator_terminates_verified_process() -> None:
    kwargs: dict[str, object] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.DEVNULL,
    }
    posix = sys.platform != "win32"
    if posix:
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", **kwargs
    )
    try:
        record = ProcessRecord(
            id="proc_real",
            session_id="ses_1",
            tool_call_id=None,
            lease_owner="owner",
            pid=proc.pid,
            pgid=proc.pid if posix else None,
            command_preview=None,
            created_at=datetime.now(UTC),
            finished_at=None,
            status="running",
        )
        result = await DefaultTerminator().terminate(record)
        assert result.terminated is True
        assert result.pid == proc.pid
        await asyncio.wait_for(proc.wait(), timeout=10)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_process_registry_marks_exit(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.new_session()
        record = await harness.processes.register(
            session.id, "call-1", "owner-a", 12345, None, "echo hi"
        )
        assert record.status == "running"
        assert record.id.startswith("proc_")
        await harness.processes.mark_exited(record.id)
        status = await harness.db.run(
            lambda conn: conn.execute(
                "SELECT status FROM process_records WHERE id = ?", (record.id,)
            ).fetchone()["status"]
        )
        assert status == "exited"
    finally:
        harness.close()
