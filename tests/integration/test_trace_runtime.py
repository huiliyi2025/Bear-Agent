"""Trace integration with the real Runtime, ToolExecutor, TaskEngine and ProviderRouter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from tests.support import (
    ScriptedProvider,
    collect_events,
    make_container,
    new_session,
    text_script,
    tool_script,
)

from bear_agent.benchmarks.fixtures import FlakyProvider
from bear_agent.core.events import EventType
from bear_agent.hooks import HookDecision, HookEvent, HookRegistration
from bear_agent.providers.retry import RetryPolicy
from bear_agent.providers.router import FallbackProvider, ProviderRoute
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.tasks.models import PlanDraft, TaskSpec, VerificationMode


@pytest.mark.asyncio
async def test_run_trace_contains_key_path(workspace: Path, settings: Any) -> None:
    container, _ = make_container(
        settings,
        workspace,
        [
            tool_script("read-1", "read_file", {"path": "hello.txt"}),
            text_script("done"),
        ],
    )
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "read hello.txt")
        assert result.status is RunStatus.COMPLETED

        traces = await container.tracer.find_by_run(result.run_id)
        assert len(traces) == 1
        trace = traces[0]
        assert trace["status"] == "ok"
        assert trace["kind"] == "run"
        assert trace["session_id"] == session.id
        spans = await container.tracer.spans(trace["trace_id"])
        names = {span["name"] for span in spans}
        assert {"run", "turn", "model.call", "tool.call"} <= names
        assert all(span["parent_span_id"] is not None for span in spans if span["name"] != "run")
        assert any(span["name"] == "checkpoint.create" for span in spans)
        assert any(
            span["name"] == "tool.call" and span["attributes"].get("tool") == "read_file"
            for span in spans
        )
        events = await container.tracer.events(trace["trace_id"])
        event_types = {event["type"] for event in events}
        assert EventType.RUN_STARTED.value in event_types
        assert EventType.RUN_COMPLETED.value in event_types
        assert EventType.MODEL_REQUEST.value in event_types
        assert EventType.TOOL_CALL_COMPLETED.value in event_types
        exported = await container.tracer.export(trace["trace_id"])
        assert exported is not None
        serialized = str(exported)
        assert "hello e2e" not in serialized
        assert "read hello.txt" not in serialized
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_cancel_during_trace_setup_releases_lease(
    workspace: Path, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, _ = make_container(settings, workspace, [text_script("done")])
    try:
        session = await new_session(container, workspace)
        entered = asyncio.Event()

        async def slow_insert(**kwargs: object) -> None:
            entered.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(container.tracer.repository, "insert_trace", slow_insert)
        task = asyncio.create_task(container.runtime.run(session.id, "hello"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not container.runtime.is_running(session.id)
        epoch = await container.sessions.acquire_lease(session.id, "test-owner", 30)
        await container.sessions.release_lease(session.id, "test-owner", epoch)
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_trace_write_failure_does_not_break_run(
    workspace: Path, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, _ = make_container(settings, workspace, [text_script("done")])
    try:

        async def broken(*args: object, **kwargs: object) -> None:
            raise RuntimeError("trace storage is down")

        monkeypatch.setattr(container.tracer.repository, "insert_trace", broken)
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "hello")
        assert result.status is RunStatus.COMPLETED
        assert result.final_text == "done"
        assert await container.tracer.list_traces() == []
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_plan_trace_has_task_spans(workspace: Path, settings: Any) -> None:
    settings.tasks.verify_completion = False
    scripts = []
    for index in range(3):
        scripts.append(
            tool_script(
                f"task-{index}",
                "task_complete",
                {
                    "summary": f"task {index} completed",
                    "evidence": [f"evidence {index}"],
                },
            )
        )
        scripts.append(text_script("task finished"))
    container, _ = make_container(settings, workspace, scripts)
    events = collect_events(container)
    try:
        session = await new_session(container, workspace)
        draft = PlanDraft(
            goal="Trace the plan",
            tasks=[
                TaskSpec(
                    key=f"T{index}",
                    title=f"Task {index}",
                    description=f"Perform task {index}.",
                    acceptance_criteria=f"Task {index} is done.",
                    verification=VerificationMode.EVIDENCE,
                )
                for index in range(1, 4)
            ],
        )
        await container.tasks.create_plan(session.id, draft)
        report = await container.runtime.run_plan(session.id)
        assert report.status == "completed"

        plan_event = next(event for event in events if event.type == EventType.TASK_RUN_STARTED)
        assert plan_event.run_id is not None
        traces = await container.tracer.find_by_run(plan_event.run_id)
        assert len(traces) == 1
        trace = traces[0]
        assert trace["kind"] == "plan_run"
        assert trace["status"] == "ok"
        spans = await container.tracer.spans(trace["trace_id"])
        task_spans = [span for span in spans if span["name"] == "task"]
        assert len(task_spans) == 3
        assert {span["attributes"].get("task_outcome") for span in task_spans} == {"completed"}
        assert {span["attributes"].get("task_key") for span in task_spans} == {
            "T1",
            "T2",
            "T3",
        }
        assert any(span["name"] == "turn" for span in spans)
        assert any(span["name"] == "tool.call" for span in spans)
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_provider_fallback_and_hook_spans(workspace: Path, settings: Any) -> None:
    failing = FlakyProvider(failures=10_000, category="rate_limit")
    healthy = ScriptedProvider([text_script("fallback answer")])
    router = FallbackProvider(
        [
            ProviderRoute(
                name="primary",
                model="fake-model",
                provider=failing,
                retry_policy=RetryPolicy(max_attempts=1),
                capabilities=failing.capabilities,
            ),
            ProviderRoute(
                name="secondary",
                model="fake-model",
                provider=healthy,
                retry_policy=RetryPolicy(max_attempts=1),
                capabilities=healthy.capabilities,
            ),
        ],
        fallback_enabled=True,
    )
    container, _ = make_container(settings, workspace, [], provider=router)

    async def observe(invocation: object) -> HookDecision:
        return HookDecision(action="continue")

    container.hooks.register(
        HookRegistration(
            id="observe-model",
            events=frozenset({HookEvent.MODEL_BEFORE}),
            handler=observe,
        )
    )
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "answer")
        assert result.status is RunStatus.COMPLETED
        traces = await container.tracer.find_by_run(result.run_id)
        assert len(traces) == 1
        spans = await container.tracer.spans(traces[0]["trace_id"])
        names = [span["name"] for span in spans]
        assert "provider.attempt" in names
        assert "provider.fallback" in names
        assert "hook" in names
        attempts = [span for span in spans if span["name"] == "provider.attempt"]
        assert len(attempts) == 2
        assert {span["attributes"].get("provider") for span in attempts} == {
            "primary",
            "secondary",
        }
    finally:
        await container.close()
