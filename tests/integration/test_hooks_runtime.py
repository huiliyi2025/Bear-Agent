"""Hooks integrated with the real Runtime and ToolExecutor."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import make_container, text_script, tool_script

from bear_agent.core.events import EventType
from bear_agent.hooks.events import HookEvent
from bear_agent.hooks.models import HookDecision, HookRegistration
from bear_agent.runtime.stop_policy import RunStatus


@pytest.mark.asyncio
async def test_before_tool_hook_can_deny_without_bypassing_policy(
    workspace: Path, settings
) -> None:
    container, _provider = make_container(
        settings,
        workspace,
        [
            tool_script("write-call", "write_file", {"path": "hooked.txt", "content": "nope"}),
            text_script("stopped"),
        ],
    )

    async def deny(_invocation):
        return HookDecision(action="deny", reason="hook policy blocks writes")

    container.hooks.register(
        HookRegistration(
            id="test-deny-write",
            events=frozenset({HookEvent.TOOL_BEFORE}),
            handler=deny,
            can_deny=True,
            fail_mode="closed",
            source="test",
        )
    )
    try:
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        result = await container.runtime.run(session.id, "write the file")
        assert result.status == RunStatus.COMPLETED
        assert not (workspace / "hooked.txt").exists()
        messages = await container.sessions.load_messages(session.id)
        tool = next(message for message in messages if message.role == "tool")
        assert tool.tool_result().is_error
        assert "hook policy" in tool.tool_result().content
        types = [event.type for event in await container.sessions.events_for(session.id)]
        assert EventType.HOOK_DENIED in types
        actions = [entry.action for entry in await container.audit.tail(50)]
        assert "hook.denied" in actions
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_before_model_hook_appends_pinned_context(workspace: Path, settings) -> None:
    container, provider = make_container(settings, workspace, [text_script("answer")])

    async def add_context(_invocation):
        return HookDecision(
            action="continue",
            reason="add project policy",
            context_append="HOOK-EXTRA-CONTEXT: use tabs",
        )

    container.hooks.register(
        HookRegistration(
            id="test-model-context",
            events=frozenset({HookEvent.MODEL_BEFORE}),
            handler=add_context,
            can_append_context=True,
            source="test",
        )
    )
    try:
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        result = await container.runtime.run(session.id, "hello")
        assert result.status == RunStatus.COMPLETED
        rendered = "\n".join(
            message.text_content()
            for message in provider.requests[0].messages
            if message.role == "system"
        )
        assert "HOOK-EXTRA-CONTEXT" in rendered
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_fail_closed_model_hook_aborts_the_turn(workspace: Path, settings) -> None:
    container, _provider = make_container(settings, workspace, [text_script("must not run")])

    async def broken(_invocation):
        raise RuntimeError("hook broke")

    container.hooks.register(
        HookRegistration(
            id="test-model-fail-closed",
            events=frozenset({HookEvent.MODEL_BEFORE}),
            handler=broken,
            fail_mode="closed",
            source="test",
        )
    )
    try:
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        result = await container.runtime.run(session.id, "hello")
        assert result.status == RunStatus.FAILED
        messages = await container.sessions.load_messages(session.id)
        assert [message.role for message in messages] == ["user"]
        types = [event.type for event in await container.sessions.events_for(session.id)]
        assert EventType.HOOK_FAILED in types
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_observation_hook_receives_model_after_and_run_events(
    workspace: Path, settings
) -> None:
    container, _provider = make_container(settings, workspace, [text_script("done")])
    seen: list[HookEvent] = []

    async def observe(invocation):
        seen.append(invocation.event)
        return None

    container.hooks.register(
        HookRegistration(
            id="test-observer",
            events=frozenset({HookEvent.MODEL_AFTER, HookEvent.RUN_COMPLETED}),
            handler=observe,
            source="test",
        )
    )
    try:
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        result = await container.runtime.run(session.id, "hello")
        assert result.status == RunStatus.COMPLETED
        assert HookEvent.MODEL_AFTER in seen
        assert HookEvent.RUN_COMPLETED in seen
    finally:
        await container.close()
