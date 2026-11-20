"""Runtime integration: crash recovery before a new user message."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import make_container, new_session, text_script

from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence, reset_fence, set_fence
from bear_agent.core.messages import AssistantMessage, ToolCall, ToolMessage, UserMessage
from bear_agent.runtime.stop_policy import RunStatus


@pytest.mark.asyncio
async def test_runtime_recovers_running_tool_before_new_turn(workspace: Path, settings) -> None:
    container, provider = make_container(
        settings, workspace, [text_script("recovered and continued")]
    )
    try:
        session = await new_session(container, workspace)
        owner = "crashed-owner"
        epoch = await container.sessions.acquire_lease(session.id, owner, 60)
        fence = LeaseFence(owner, epoch)
        token = set_fence(fence)
        try:
            await container.sessions.append_message(
                session.id,
                UserMessage.of("run the tool"),
                event_type=EventType.MESSAGE_USER,
            )
            await container.sessions.append_assistant_message(
                session.id,
                AssistantMessage.with_tool_calls(
                    None,
                    [ToolCall(id="call-crash", name="read_file", arguments={"path": "x"})],
                ),
            )
            await container.sessions.tool_calls.start(
                session.id,
                None,
                "call-crash",
                "read_file",
                {"path": "x"},
                True,
            )
        finally:
            reset_fence(token)
        # Simulate a process exit: lease released, no runtime cleanup.
        await container.sessions.release_lease(session.id, owner, epoch)

        result = await container.runtime.run(session.id, "continue")
        assert result.status == RunStatus.COMPLETED

        messages = await container.sessions.load_messages(session.id)
        synthetic = [
            message
            for message in messages
            if message.role == "tool"
            and message.tool_result() is not None
            and "interrupted" in (message.tool_result().content or "").lower()
        ]
        assert len(synthetic) == 1
        content = synthetic[0].tool_result().content
        assert "NOT automatically retried" in content
        assert "read_file" in content

        row = await container.sessions.tool_calls.get("call-crash")
        assert row is not None
        assert row["status"] == "unknown"

        types = [event.type for event in await container.sessions.events_for(session.id)]
        assert EventType.RECOVERY_STARTED in types
        assert EventType.RECOVERY_TOOL_RECONCILED in types
        assert EventType.RECOVERY_COMPLETED in types

        # The new model request includes the synthetic result before the new
        # user message, so the provider sees a balanced tool-call pair.
        roles = [message.role for message in provider.requests[-1].messages]
        assistant_index = roles.index("assistant")
        tool_index = roles.index("tool")
        assert assistant_index < tool_index

        actions = [entry.action for entry in await container.audit.tail(50)]
        assert "recovery.started" in actions
        assert "recovery.tool_reconciled" in actions
        assert "recovery.completed" in actions
        assert (await container.audit.verify()).ok
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_running_row_with_recorded_result_is_marked_unknown(
    workspace: Path, settings
) -> None:
    container, _provider = make_container(settings, workspace, [])
    try:
        session = await new_session(container, workspace)
        owner = "crashed-owner"
        epoch = await container.sessions.acquire_lease(session.id, owner, 60)
        fence = LeaseFence(owner, epoch)
        token = set_fence(fence)
        try:
            await container.sessions.append_message(
                session.id,
                UserMessage.of("run"),
                event_type=EventType.MESSAGE_USER,
            )
            await container.sessions.append_assistant_message(
                session.id,
                AssistantMessage.with_tool_calls(
                    None,
                    [ToolCall(id="call-done", name="read_file", arguments={"path": "x"})],
                ),
            )
            await container.sessions.tool_calls.start(
                session.id, None, "call-done", "read_file", {"path": "x"}, True
            )
            await container.sessions.append_tool_message(
                session.id,
                ToolMessage.of("call-done", "result already recorded"),
            )
        finally:
            reset_fence(token)
        await container.sessions.release_lease(session.id, owner, epoch)

        epoch2 = await container.sessions.acquire_lease(session.id, "recovery-owner", 60)
        fence2 = LeaseFence("recovery-owner", epoch2)
        token = set_fence(fence2)
        try:
            report = await container.recovery.recover_if_needed(session.id, fence=fence2)
        finally:
            reset_fence(token)
        assert report.running_tools == 1
        assert report.synthesized_tools == []
        assert any("already recorded" in action for action in report.actions)
        row = await container.sessions.tool_calls.get("call-done")
        assert row is not None
        assert row["status"] == "unknown"
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_mid_transcript_dangling_tool_call_blocks_new_turn(workspace: Path, settings) -> None:
    container, _provider = make_container(settings, workspace, [text_script("must not run")])
    try:
        session = await new_session(container, workspace)
        owner = "crashed-owner"
        epoch = await container.sessions.acquire_lease(session.id, owner, 60)
        fence = LeaseFence(owner, epoch)
        token = set_fence(fence)
        try:
            await container.sessions.append_message(
                session.id,
                UserMessage.of("first"),
                event_type=EventType.MESSAGE_USER,
            )
            await container.sessions.append_assistant_message(
                session.id,
                AssistantMessage.with_tool_calls(
                    None,
                    [ToolCall(id="call-mid", name="read_file", arguments={"path": "x"})],
                ),
            )
            await container.sessions.append_message(
                session.id,
                UserMessage.of("later message"),
                event_type=EventType.MESSAGE_USER,
            )
        finally:
            reset_fence(token)
        await container.sessions.release_lease(session.id, owner, epoch)

        result = await container.runtime.run(session.id, "new turn")
        assert result.status == RunStatus.FAILED
        assert "manual repair" in (result.error or "")
        messages = await container.sessions.load_messages(session.id)
        assert [message.text_content() for message in messages if message.role == "user"] == [
            "first",
            "later message",
        ]
    finally:
        await container.close()
