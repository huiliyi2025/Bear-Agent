"""Runtime integration: plan execution, verification, recovery and fork."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import make_container, text_script, tool_script

from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence, reset_fence, set_fence
from bear_agent.storage.continuity_repositories import CheckpointRepository
from bear_agent.tasks.models import PlanDraft, PlanStatus, TaskSpec, TaskStatus


async def _create_plan(container, session_id: str) -> None:
    owner = "test-plan-owner"
    epoch = await container.sessions.acquire_lease(session_id, owner, 60)
    fence = LeaseFence(owner, epoch)
    token = set_fence(fence)
    try:
        await container.tasks.create_plan(
            session_id,
            PlanDraft(
                goal="ship two dependent steps",
                tasks=[
                    TaskSpec(
                        key="A",
                        title="step A",
                        description="do A",
                        acceptance_criteria="A verified",
                        verification="evidence",
                    ),
                    TaskSpec(
                        key="B",
                        title="step B",
                        description="do B",
                        dependencies=["A"],
                        acceptance_criteria="B verified",
                        verification="evidence",
                    ),
                ],
            ),
            source="user",
            fence=fence,
        )
    finally:
        reset_fence(token)
        await container.sessions.release_lease(session_id, owner, epoch)


def _complete_scripts(call_prefix: str) -> list:
    return [
        tool_script(
            f"{call_prefix}-complete",
            "task_complete",
            {"summary": "acceptance criteria verified", "evidence": ["tests passed"]},
        ),
        text_script("task finished"),
    ]


@pytest.mark.asyncio
async def test_run_plan_executes_tasks_and_verifies_completion(workspace: Path, settings) -> None:
    settings.tasks.verify_completion = True
    settings.tasks.max_attempts = 2
    settings.checkpoints.enabled = True
    container, provider = make_container(
        settings,
        workspace,
        [*_complete_scripts("a"), *_complete_scripts("b")],
    )
    try:
        session = await container.sessions.create(
            cwd=workspace, model="m", provider="p", title="plan"
        )
        await _create_plan(container, session.id)

        report = await container.runtime.run_plan(session.id)

        assert report.status == PlanStatus.COMPLETED.value
        assert report.tasks_run == 2
        assert report.tasks_completed == 2
        tasks = {task.task_key: task for task in await container.tasks.list_tasks(session.id)}
        assert tasks["A"].status == TaskStatus.COMPLETED
        assert tasks["B"].status == TaskStatus.COMPLETED

        attempts_a = await container.tasks.attempts(tasks["A"].id)
        assert attempts_a[-1].status.value == "completed"
        assert attempts_a[-1].verification["completed"] is True

        types = [event.type for event in await container.sessions.events_for(session.id)]
        assert EventType.TASK_RUN_STARTED in types
        assert EventType.TASK_COMPLETED in types
        assert EventType.TASK_PLAN_COMPLETED in types
        assert EventType.TASK_VERIFICATION in types
        assert (await container.audit.verify()).ok

        checkpoints = CheckpointRepository(container.db)
        latest = await checkpoints.latest(session.id)
        assert latest is not None
        snapshot = latest.state.get("tasks")
        assert isinstance(snapshot, dict)
        assert snapshot["plan"]["status"] == PlanStatus.COMPLETED.value
        assert len(snapshot["tasks"]) == 2
        await container.checkpoints.validate(latest)
        assert len(provider.requests) == 4  # two tool calls + two final answers
        rendered = "\n".join(
            message.text_content() for request in provider.requests for message in request.messages
        )
        assert "<task-plan>" in rendered
        assert "Current task: A" in rendered
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_interrupted_task_attempt_is_blocked_by_recovery(workspace: Path, settings) -> None:
    container, provider = make_container(settings, workspace, [])
    try:
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        await _create_plan(container, session.id)

        owner = "crashed-plan-owner"
        epoch = await container.sessions.acquire_lease(session.id, owner, 60)
        fence = LeaseFence(owner, epoch)
        token = set_fence(fence)
        try:
            execution = await container.task_engine.start_next(
                session.id, fence=fence, run_id="run-crash"
            )
            assert execution is not None
        finally:
            reset_fence(token)
            await container.sessions.release_lease(session.id, owner, epoch)

        report = await container.runtime.run_plan(session.id)

        assert report.status == PlanStatus.BLOCKED.value
        task = await container.tasks.get_task(execution.task.id)
        assert task is not None
        assert task.status == TaskStatus.BLOCKED
        attempts = await container.tasks.attempts(task.id)
        assert attempts[-1].status.value == "unknown"
        types = [event.type for event in await container.sessions.events_for(session.id)]
        assert EventType.TASK_RECONCILED in types
        assert provider.requests == []
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_plan_run_error_returns_post_reconcile_status(workspace: Path, settings) -> None:
    container, _provider = make_container(settings, workspace, [text_script("done")])
    try:
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        await _create_plan(container, session.id)

        async def boom(*args, **kwargs):
            raise RuntimeError("engine failure injected")

        container.task_engine.finish = boom  # type: ignore[method-assign]
        report = await container.runtime.run_plan(session.id)

        assert report.status == PlanStatus.BLOCKED.value
        plan = await container.tasks.get_plan(session.id)
        assert plan is not None and plan.status == PlanStatus.BLOCKED
        tasks = await container.tasks.list_tasks(session.id)
        assert tasks[0].status == TaskStatus.BLOCKED
        attempts = await container.tasks.attempts(tasks[0].id)
        assert attempts[-1].status.value == "unknown"
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_fork_does_not_inherit_task_or_plan_state(workspace: Path, settings) -> None:
    container, _provider = make_container(
        settings, workspace, [*_complete_scripts("a"), *_complete_scripts("b")]
    )
    try:
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        await _create_plan(container, session.id)
        await container.runtime.run_plan(session.id)

        forked = await container.fork.fork(session.id, checkpoints=container.checkpoints)
        assert await container.tasks.get_plan(forked.session.id) is None
        assert await container.tasks.list_tasks(forked.session.id) == []
    finally:
        await container.close()
