from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import make_container, new_session, text_script

from bear_agent.bootstrap import Container
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.messages import AssistantMessage, ToolCall
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.sessions.checkpoints import CheckpointService
from bear_agent.sessions.recovery import RecoveryService
from bear_agent.shell.process_registry import ProcessRegistry, TerminationResult
from bear_agent.storage.continuity_repositories import (
    AnchorRepository,
    CheckpointRepository,
    CompactionRepository,
    ContextStateRepository,
    ProcessRecord,
    ProcessRepository,
)


def _tool_content(message) -> str:
    result = message.tool_result()
    return result.content if result is not None else ""


class FakeTerminator:
    def __init__(self) -> None:
        self.calls: list[ProcessRecord] = []

    async def terminate(self, record: ProcessRecord) -> TerminationResult:
        self.calls.append(record)
        return TerminationResult(True, "fake_terminated", record.pid)


def _recovery(container: Container, terminator: FakeTerminator | None = None) -> RecoveryService:
    processes = ProcessRegistry(
        ProcessRepository(container.db),
        clock=container.sessions.clock,
        terminator=terminator or FakeTerminator(),
        recorder=container.recorder,
        audit=container.audit,
    )
    checkpoints = CheckpointService(
        sessions=container.sessions,
        checkpoints=CheckpointRepository(container.db),
        compactions=CompactionRepository(container.db),
        anchors=AnchorRepository(container.db),
        context_state=ContextStateRepository(container.db),
        recorder=container.recorder,
        audit=container.audit,
        clock=container.sessions.clock,
    )
    return RecoveryService(
        sessions=container.sessions,
        claims=container.claims,
        processes=processes,
        checkpoints=checkpoints,
        recorder=container.recorder,
        audit=container.audit,
        clock=container.sessions.clock,
    )


@pytest.mark.asyncio
async def test_crash_recovery_reconciles_and_run_resumes(workspace: Path, settings) -> None:
    container, provider = make_container(settings, workspace, [text_script("resumed")])
    try:
        session = await new_session(container, workspace)
        # Simulate a crash mid-tool-call: the transcript holds the assistant
        # tool call, but the process died before the tool result was appended.
        await container.sessions.append_user_message(session.id, "run a command")
        await container.sessions.append_assistant_message(
            session.id,
            AssistantMessage.with_tool_calls(
                None,
                [ToolCall(id="call-crash", name="bash", arguments={"command": "echo crash"})],
            ),
        )
        await container.sessions.tool_calls.start(
            session.id,
            "turn-1",
            "call-crash",
            "bash",
            {"command": "echo crash"},
            False,
        )

        epoch = await container.sessions.acquire_lease(session.id, "recovery-owner", 300)
        fence = LeaseFence("recovery-owner", epoch)
        recovery = _recovery(container)

        report = await recovery.recover_if_needed(session.id, fence=fence, run_id="run-recovery")

        assert report.recovered is True
        assert report.synthesized_tools == ["call-crash"]
        assert report.audit_verified is True
        row = await container.sessions.tool_calls.get("call-crash")
        assert row is not None and row["status"] == "unknown"

        messages = await container.sessions.load_messages(session.id)
        assert messages[-1].role == "tool"
        content = _tool_content(messages[-1])
        assert "interrupted" in content.lower()
        assert "NOT automatically retried" in content
        assert "non-idempotent or side-effecting" in content

        types = [event.type for event in await container.sessions.events_for(session.id)]
        assert EventType.RECOVERY_STARTED in types
        assert EventType.RECOVERY_TOOL_RECONCILED in types
        assert EventType.RECOVERY_COMPLETED in types

        await container.sessions.release_lease(session.id, "recovery-owner", epoch)
        result = await container.runtime.run(session.id, "continue")
        assert result.status == RunStatus.COMPLETED
        assert result.final_text == "resumed"

        # The model saw the synthetic tool result instead of a dangling call.
        request_messages = provider.requests[0].messages
        assert any(
            message.role == "tool" and "NOT automatically retried" in _tool_content(message)
            for message in request_messages
        )
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_idle_session_recovery_is_noop(workspace: Path, settings) -> None:
    container, _provider = make_container(settings, workspace, [text_script("never")])
    try:
        session = await new_session(container, workspace)
        epoch = await container.sessions.acquire_lease(session.id, "owner", 300)
        recovery = _recovery(container)

        report = await recovery.recover_if_needed(session.id, fence=LeaseFence("owner", epoch))

        assert report.recovered is False
        types = [event.type for event in await container.sessions.events_for(session.id)]
        assert EventType.RECOVERY_STARTED not in types
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_orphan_process_is_terminated(workspace: Path, settings) -> None:
    container, _provider = make_container(settings, workspace, [text_script("never")])
    terminator = FakeTerminator()
    try:
        session = await new_session(container, workspace)
        registry = ProcessRegistry(
            ProcessRepository(container.db),
            clock=container.sessions.clock,
            terminator=terminator,
            recorder=container.recorder,
            audit=container.audit,
        )
        record = await registry.register(
            session.id, "call-old", "dead-owner", 424242, None, "sleep 100"
        )
        epoch = await container.sessions.acquire_lease(session.id, "owner", 300)
        recovery = _recovery(container, terminator)

        report = await recovery.recover_if_needed(session.id, fence=LeaseFence("owner", epoch))

        assert report.recovered is True
        assert report.terminated_processes == [424242]
        assert terminator.calls[0].id == record.id
        status = await container.db.run(
            lambda conn: conn.execute(
                "SELECT status FROM process_records WHERE id = ?", (record.id,)
            ).fetchone()["status"]
        )
        assert status == "terminated"
        types = [event.type for event in await container.sessions.events_for(session.id)]
        assert EventType.RECOVERY_PROCESS_TERMINATED in types
    finally:
        await container.close()
