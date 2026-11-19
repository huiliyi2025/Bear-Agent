from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from tests.support import (
    make_container,
    new_session,
    text_script,
    tool_script,
)

from bear_agent.bootstrap import build_container
from bear_agent.core.cancel import CancelToken
from bear_agent.core.events import EventType
from bear_agent.policy.approver import DenyingApprover
from bear_agent.providers.base import (
    FinishEvent,
    ModelCapabilities,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    TextDelta,
)
from bear_agent.runtime.stop_policy import RunStatus


def _tool_text(message) -> str:
    result = message.tool_result()
    return result.content if result is not None else ""


@pytest.mark.asyncio
async def test_text_only_run_persists_everything(workspace: Path, settings) -> None:
    container, _provider = make_container(settings, workspace, [text_script("hi there")])
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "hello")
        assert result.status == RunStatus.COMPLETED
        assert result.final_text == "hi there"
        assert result.turns == 1
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5
        messages = await container.sessions.load_messages(session.id)
        assert [message.role for message in messages] == ["user", "assistant"]
        events = await container.sessions.events_for(session.id)
        types = [event.type for event in events]
        assert EventType.RUN_STARTED in types
        assert EventType.RUN_COMPLETED in types
        assert types.index(EventType.RUN_STARTED) < types.index(EventType.RUN_COMPLETED)
        usage = await container.sessions.usage_summary(session.id)
        assert usage.calls == 1
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_empty_response_fails(workspace: Path, settings) -> None:
    container, _provider = make_container(settings, workspace, [text_script("")])
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "hello")
        assert result.status == RunStatus.FAILED
        assert "empty response" in result.stop_reason
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_tool_call_read_file(workspace: Path, settings) -> None:
    scripts = [
        tool_script("c1", "read_file", {"path": "hello.txt"}),
        text_script("the file says hello"),
    ]
    container, provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "read hello.txt")
        assert result.status == RunStatus.COMPLETED
        assert result.final_text == "the file says hello"
        messages = await container.sessions.load_messages(session.id)
        assert [message.role for message in messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert "hello from bear" in _tool_text(messages[2])
        row = await container.sessions.tool_calls.get("c1")
        assert row is not None and row["status"] == "completed"
        second_request = provider.requests[1]
        tool_messages = [m for m in second_request.messages if m.role == "tool"]
        assert tool_messages and "hello from bear" in _tool_text(tool_messages[0])
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_write_and_edit_tools(workspace: Path, settings) -> None:
    scripts = [
        tool_script("c1", "write_file", {"path": "new.txt", "content": "one two\n"}),
        tool_script(
            "c2",
            "edit_file",
            {"path": "new.txt", "old_string": "two", "new_string": "three"},
        ),
        text_script("done"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "create and edit")
        assert result.status == RunStatus.COMPLETED
        assert (workspace / "new.txt").read_text(encoding="utf-8") == "one three\n"
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_path_escape_is_rejected(workspace: Path, settings) -> None:
    scripts = [
        tool_script("c1", "write_file", {"path": "../escape.txt", "content": "nope"}),
        text_script("could not write"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "escape")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        assert "outside workspace" in _tool_text(messages[2])
        assert not (workspace.parent / "escape.txt").exists()
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_supervised_write_denied(workspace: Path, settings) -> None:
    settings.agent.permission_mode = "supervised"
    scripts = [
        tool_script("c1", "write_file", {"path": "x.txt", "content": "x"}),
        text_script("denied"),
    ]
    container, _provider = make_container(settings, workspace, scripts, approver=DenyingApprover())
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "write")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        assert "denied" in _tool_text(messages[2]).lower()
        assert not (workspace / "x.txt").exists()
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_max_turns(workspace: Path, settings) -> None:
    settings.agent.max_turns = 2
    scripts = [
        tool_script("c0", "read_file", {"path": "hello.txt"}),
        tool_script("c1", "read_file", {"path": "hello.txt"}),
        text_script("never"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "loop")
        assert result.status == RunStatus.MAX_TURNS
        assert result.turns == 2
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_repeated_call_no_progress(workspace: Path, settings) -> None:
    scripts = [tool_script(f"c{i}", "read_file", {"path": "hello.txt"}) for i in range(8)]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "spin")
        assert result.status == RunStatus.NO_PROGRESS
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_unknown_and_invalid_tools(workspace: Path, settings) -> None:
    scripts = [
        tool_script("c1", "no_such_tool", {}),
        tool_script("c2", "read_file", {}),
        text_script("survived"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "bad calls")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        assert "unknown tool" in _tool_text(messages[2])
        assert "invalid arguments" in _tool_text(messages[4])
    finally:
        await container.close()


class BlockingProvider(ModelProvider):
    def __init__(self) -> None:
        self.name = "blocking"
        self.capabilities = ModelCapabilities(context_window=32_000, max_output_tokens=1_000)
        self.started = asyncio.Event()

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.started.set()
        await asyncio.sleep(30)
        yield TextDelta(text="late")
        yield FinishEvent()


@pytest.mark.asyncio
async def test_cancel_run(workspace: Path, settings) -> None:
    provider = BlockingProvider()
    container = build_container(settings, cwd=workspace, provider=provider)
    try:
        session = await new_session(container, workspace)
        task = asyncio.create_task(container.runtime.run(session.id, "slow"))
        await asyncio.wait_for(provider.started.wait(), timeout=5)
        assert container.runtime.cancel(session.id)
        result = await asyncio.wait_for(task, timeout=10)
        assert result.status == RunStatus.CANCELLED
        assert not container.runtime.is_running(session.id)
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_session_resume_keeps_history(workspace: Path, settings) -> None:
    first, _ = make_container(settings, workspace, [text_script("first answer")])
    try:
        session = await new_session(first, workspace)
        result = await first.runtime.run(session.id, "first question")
        assert result.status == RunStatus.COMPLETED
        session_id = session.id
    finally:
        await first.close()

    second, provider = make_container(settings, workspace, [text_script("second answer")])
    try:
        resumed = await second.sessions.get(session_id)
        assert resumed is not None
        result = await second.runtime.run(session_id, "second question")
        assert result.status == RunStatus.COMPLETED
        request_messages = provider.requests[0].messages
        texts = [message.text_content() for message in request_messages]
        assert "first question" in texts
        assert "first answer" in texts
        assert "second question" in texts
        assert len(await second.sessions.load_messages(session_id)) == 4
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_running_session_rejects_concurrent_run(workspace: Path, settings) -> None:
    provider = BlockingProvider()
    container = build_container(settings, cwd=workspace, provider=provider)
    try:
        session = await new_session(container, workspace)
        task = asyncio.create_task(container.runtime.run(session.id, "slow"))
        await asyncio.wait_for(provider.started.wait(), timeout=5)
        with pytest.raises(Exception, match="already running"):
            await container.runtime.run(session.id, "second")
        container.runtime.cancel(session.id)
        await asyncio.wait_for(task, timeout=10)
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_file_claim_conflict_reaches_model(workspace: Path, settings) -> None:
    scripts = [
        tool_script("c1", "write_file", {"path": "locked.txt", "content": "b"}),
        text_script("blocked by claim"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session_a = await new_session(container, workspace)
        session_b = await new_session(container, workspace)
        owner = "manual-claim-owner"
        await container.sessions.acquire_lease(session_a.id, owner, 300)
        await container.claims.acquire(session_a.id, owner, "locked.txt")
        result = await container.runtime.run(session_b.id, "write locked")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session_b.id)
        tool_text = _tool_text(messages[2])
        assert "conflicts with a claim" in tool_text
        assert not (workspace / "locked.txt").exists()
        await container.claims.release_owner(session_a.id, owner)
    finally:
        await container.close()
