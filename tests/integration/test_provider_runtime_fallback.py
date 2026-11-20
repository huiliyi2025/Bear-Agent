"""Provider fallback through the real Runtime, ToolExecutor and TaskEngine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from tests.support import ScriptedProvider, make_container, text_script, tool_script

from bear_agent.core.cancel import CancelToken
from bear_agent.core.errors import ProviderError
from bear_agent.providers.base import (
    ModelCapabilities,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    TextDelta,
)
from bear_agent.providers.retry import RetryPolicy
from bear_agent.providers.router import FallbackProvider, ProviderRoute
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.tasks.models import PlanDraft, TaskSpec


class FailingProvider(ModelProvider):
    def __init__(self, *, before: list[ModelEvent] | None = None) -> None:
        self.name = "failing"
        self.capabilities = ModelCapabilities(context_window=32_000, max_output_tokens=1_000)
        self.calls = 0
        self.before = list(before or [])

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        for event in self.before:
            yield event
        raise ProviderError("primary unavailable", category="server_error", retryable=True)

    async def aclose(self) -> None:
        return None


def _router(
    primary: ModelProvider,
    fallback: ModelProvider,
    events: list[tuple[str, dict]],
) -> FallbackProvider:
    async def on_event(event: str, payload: dict) -> None:
        events.append((event, payload))

    return FallbackProvider(
        [
            ProviderRoute(
                name="primary",
                model="primary-model",
                provider=primary,
                retry_policy=RetryPolicy(max_attempts=1, base_delay_s=0, jitter=0),
                capabilities=primary.capabilities,
            ),
            ProviderRoute(
                name="backup",
                model="backup-model",
                provider=fallback,
                retry_policy=RetryPolicy(max_attempts=1, base_delay_s=0, jitter=0),
                capabilities=fallback.capabilities,
            ),
        ],
        on_event=on_event,
    )


@pytest.mark.asyncio
async def test_runtime_falls_back_and_executes_tool_once(workspace: Path, settings) -> None:
    events: list[tuple[str, dict]] = []
    primary = FailingProvider()
    backup = ScriptedProvider(
        [
            tool_script("call-read", "read_file", {"path": "hello.txt"}),
            text_script("fallback finished"),
        ]
    )
    router = _router(primary, backup, events)
    container, _ = make_container(settings, workspace, [], provider=router)
    try:
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        result = await container.runtime.run(session.id, "read the file")
        assert result.status == RunStatus.COMPLETED
        assert result.final_text == "fallback finished"
        assert primary.calls == 2  # one failed call per turn
        assert len(backup.requests) == 2
        messages = await container.sessions.load_messages(session.id)
        tool_messages = [message for message in messages if message.role == "tool"]
        assert len(tool_messages) == 1
        assert "hello from bear" in (tool_messages[0].tool_result().content or "")
        calls = container.db.connection.execute(
            "SELECT provider, model FROM model_calls ORDER BY created_at"
        ).fetchall()
        assert [row["provider"] for row in calls] == ["backup", "backup"]
        fallback_events = [item for item in events if item[0] == "provider.fallback"]
        assert len(fallback_events) == 2
        # The requests carry session metadata, so events can be correlated.
        assert fallback_events[0][1]["session_id"] == session.id
        assert (await container.audit.verify()).ok
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_partial_stream_is_never_retried_or_failed_over(workspace: Path, settings) -> None:
    events: list[tuple[str, dict]] = []
    primary = FailingProvider(before=[TextDelta(text="partial output")])
    backup = ScriptedProvider([text_script("must not run")])
    router = _router(primary, backup, events)
    container, _ = make_container(settings, workspace, [], provider=router)
    try:
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        result = await container.runtime.run(session.id, "hello")
        assert result.status == RunStatus.FAILED
        assert "refusing to retry or fall back" in (result.error or "")
        assert primary.calls == 1
        assert backup.requests == []
        assert not any(item[0] == "provider.fallback" for item in events)
        messages = await container.sessions.load_messages(session.id)
        assert [message.role for message in messages] == ["user"]
        failed_calls = container.db.connection.execute("SELECT status FROM model_calls").fetchall()
        assert [row["status"] for row in failed_calls] == ["failed"]
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_task_engine_uses_fallback_for_completion(workspace: Path, settings) -> None:
    events: list[tuple[str, dict]] = []
    primary = FailingProvider()
    backup = ScriptedProvider(
        [
            tool_script(
                "t1-complete",
                "task_complete",
                {"summary": "completed and verified", "evidence": ["checked"]},
            ),
            text_script("task finished"),
        ]
    )
    router = _router(primary, backup, events)
    settings.tasks.verify_completion = True
    container, _ = make_container(settings, workspace, [], provider=router)
    try:
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        from bear_agent.core.fencing import LeaseFence, reset_fence, set_fence

        owner = "plan-owner"
        epoch = await container.sessions.acquire_lease(session.id, owner, 60)
        fence = LeaseFence(owner, epoch)
        token = set_fence(fence)
        try:
            await container.tasks.create_plan(
                session.id,
                PlanDraft(
                    goal="do it",
                    tasks=[
                        TaskSpec(
                            key="T1",
                            title="do",
                            description="do it",
                            acceptance_criteria="done",
                            verification="evidence",
                        )
                    ],
                ),
                source="user",
                fence=fence,
            )
        finally:
            reset_fence(token)
            await container.sessions.release_lease(session.id, owner, epoch)

        report = await container.runtime.run_plan(session.id)
        assert report.status == "completed", report
        assert report.tasks_completed == 1
        assert primary.calls >= 2
        assert len(backup.requests) == 2
    finally:
        await container.close()
