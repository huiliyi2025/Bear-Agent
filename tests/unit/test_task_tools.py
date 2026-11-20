"""Task tools: completion requests, blocking, revisions and lease fencing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from bear_agent.audit.service import AuditService
from bear_agent.config.models import TasksConfig
from bear_agent.core.cancel import CancelToken
from bear_agent.core.clock import SystemClock
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.modes import ToolSideEffect
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.policy.path_guard import PathGuard
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database
from bear_agent.tasks.models import (
    AttemptStatus,
    PlanDraft,
    TaskSpec,
    TaskStatus,
)
from bear_agent.tasks.service import TaskService
from bear_agent.tasks.tools import (
    TaskBlockArgs,
    TaskCompleteArgs,
    create_task_tools,
)
from bear_agent.tools.base import Tool, ToolContext, ToolResult


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.db = Database(tmp_path / "tasks.db")
        self.clock = SystemClock()
        self.sessions = SessionService(self.db, self.clock)
        self.redactor = SecretRedactor()
        self.recorder = EventRecorder(self.sessions, EventBus(), self.redactor)
        self.audit = AuditService(self.db, redactor=self.redactor, clock=self.clock)
        self.tasks = TaskService(
            self.db,
            self.recorder,
            audit=self.audit,
            config=TasksConfig(max_tasks=10, max_attempts=2, max_revisions=3),
            clock=self.clock,
        )
        self.tools = {tool.name: tool for tool in create_task_tools(self.tasks)}

    async def new_session(self) -> tuple[str, LeaseFence]:
        session = await self.sessions.create(cwd=Path.cwd(), model="m", provider="p")
        epoch = await self.sessions.acquire_lease(session.id, "owner", 300)
        return session.id, LeaseFence("owner", epoch)

    async def create_plan(self, session_id: str, fence: LeaseFence, *keys: str):
        draft = PlanDraft(
            goal="ship a feature",
            tasks=[
                TaskSpec(
                    key=key,
                    title=f"title {key}",
                    description=f"do {key}",
                    acceptance_criteria=f"{key} verified",
                )
                for key in keys
            ],
        )
        return await self.tasks.create_plan(session_id, draft, fence=fence)

    async def claim(self, session_id: str, fence: LeaseFence):
        claimed = await self.tasks.claim_next(session_id, fence=fence, run_id="run-claim")
        assert claimed is not None
        return claimed

    def close(self) -> None:
        self.db.close()


def _ctx(
    session_id: str,
    fence: LeaseFence | None,
    *,
    run_id: str = "run-1",
    approved: bool = True,
) -> ToolContext:
    return ToolContext(
        cwd=Path.cwd(),
        session_id=session_id,
        cancel=CancelToken(),
        path_guard=PathGuard(Path.cwd()),
        lease_owner=fence.owner if fence is not None else None,
        lease_epoch=fence.epoch if fence is not None else None,
        extras={"run_id": run_id},
        approved=approved,
    )


async def invoke(tool: Tool, ctx: ToolContext, **arguments: Any) -> ToolResult:
    args = tool.args_model.model_validate(arguments)
    return await tool.run(args, ctx)


def _revise_tasks(*keys: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    previous: list[str] = []
    for key in keys:
        tasks.append(
            {
                "key": key,
                "title": f"title {key}",
                "description": f"do {key}",
                "acceptance_criteria": f"{key} verified",
                "dependencies": list(previous),
            }
        )
        previous = [key]
    return tasks


def test_create_task_tools_metadata(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        tools = create_task_tools(harness.tasks)
        assert [tool.name for tool in tools] == [
            "task_complete",
            "task_block",
            "plan_revise",
        ]
        assert all(tool.side_effect == ToolSideEffect.READ for tool in tools)
        assert all(tool.concurrency_safe is False for tool in tools)
        assert tools[0].idempotent is True
        assert tools[1].idempotent is False
        assert tools[2].idempotent is False
        assert "summary" in tools[0].spec.parameters["properties"]
        assert "evidence" in tools[0].spec.parameters["properties"]
        assert "reason" in tools[1].spec.parameters["properties"]
        assert "tasks" in tools[2].spec.parameters["properties"]
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_task_complete_records_request_but_does_not_complete(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, "A")
        task, _attempt = await harness.claim(session_id, fence)

        result = await invoke(
            harness.tools["task_complete"],
            _ctx(session_id, fence, run_id="run-42"),
            summary="Implemented A and the tests pass",
            evidence=["pytest -q: 12 passed"],
        )

        assert result.is_error is False
        assert "A" in result.content
        assert "verify" in result.content
        assert result.meta["task_key"] == "A"
        assert result.meta["evidence_count"] == 1

        refreshed = await harness.tasks.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.IN_PROGRESS
        assert refreshed.result_summary is None

        attempts = await harness.tasks.attempts(task.id)
        latest = attempts[-1]
        assert latest.status == AttemptStatus.RUNNING
        assert latest.completion_summary == "Implemented A and the tests pass"
        assert latest.evidence == ["pytest -q: 12 passed"]
        assert latest.run_id == "run-42"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_task_complete_without_active_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, "A")

        result = await invoke(
            harness.tools["task_complete"],
            _ctx(session_id, fence),
            summary="nothing is running",
            evidence=[],
        )

        assert result.is_error is True
        assert result.error_kind == "no_active_task"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_task_block_without_active_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, "A")

        result = await invoke(
            harness.tools["task_block"],
            _ctx(session_id, fence),
            reason="missing credentials",
        )

        assert result.is_error is True
        assert result.error_kind == "no_active_task"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_task_tools_fail_closed_without_fence(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, "A")
        task, _attempt = await harness.claim(session_id, fence)
        ctx = _ctx(session_id, None)

        complete = await invoke(
            harness.tools["task_complete"],
            ctx,
            summary="this must not be recorded",
            evidence=["nothing"],
        )
        blocked = await invoke(
            harness.tools["task_block"],
            ctx,
            reason="this must not block the task",
        )
        revised = await invoke(
            harness.tools["plan_revise"],
            ctx,
            reason="this must not revise the plan",
            tasks=_revise_tasks("A"),
        )

        assert complete.error_kind == "task_fence_unavailable"
        assert blocked.error_kind == "task_fence_unavailable"
        assert revised.error_kind == "task_fence_unavailable"

        refreshed = await harness.tasks.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.IN_PROGRESS
        attempts = await harness.tasks.attempts(task.id)
        assert attempts[-1].completion_summary is None
        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None and plan.version == 1
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_task_block_transitions_task_to_blocked(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, "A")
        task, _attempt = await harness.claim(session_id, fence)
        ctx = _ctx(session_id, fence)

        result = await invoke(harness.tools["task_block"], ctx, reason="missing credentials")

        assert result.is_error is False
        assert "blocked" in result.content.lower()
        assert result.meta["status"] == "blocked"
        refreshed = await harness.tasks.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.BLOCKED
        attempts = await harness.tasks.attempts(task.id)
        assert attempts[-1].status == AttemptStatus.BLOCKED

        # A blocked task is no longer the active task, so a repeated call is
        # refused before the service transition check.
        again = await invoke(harness.tools["task_block"], ctx, reason="already blocked")
        assert again.is_error is True
        assert again.error_kind == "no_active_task"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_plan_revise_creates_version_two(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, "A")
        ctx = _ctx(session_id, fence)

        result = await invoke(
            harness.tools["plan_revise"],
            ctx,
            reason="the plan needs a verification task",
            tasks=_revise_tasks("A", "B"),
        )

        assert result.is_error is False
        assert "version 2" in result.content
        assert result.meta["version"] == 2
        assert result.meta["task_keys"] == ["A", "B"]

        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None
        assert plan.version == 2
        assert plan.goal == "ship a feature"
        tasks = await harness.tasks.list_tasks(session_id)
        assert [task.task_key for task in tasks] == ["A", "B"]
        revisions = await harness.tasks.revisions(session_id)
        assert [record.version for record in revisions] == [1, 2]
        assert revisions[-1].reason == "the plan needs a verification task"
        assert revisions[-1].source == "model"
        assert revisions[-1].created_by_run_id == "run-1"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_plan_revise_rejected_while_task_in_progress(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, "A")
        await harness.claim(session_id, fence)

        result = await invoke(
            harness.tools["plan_revise"],
            _ctx(session_id, fence),
            reason="change the plan mid-task",
            tasks=_revise_tasks("A"),
        )

        assert result.is_error is True
        assert result.error_kind == "plan_revision_rejected"
        assert "in progress" in result.content
        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None and plan.version == 1
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_plan_revise_rejects_cyclic_and_unknown_dependencies(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, "A")
        ctx = _ctx(session_id, fence)

        cycle = await invoke(
            harness.tools["plan_revise"],
            ctx,
            reason="introduce a dependency cycle",
            tasks=[
                {
                    "key": "A",
                    "title": "title A",
                    "description": "do A",
                    "dependencies": ["B"],
                },
                {
                    "key": "B",
                    "title": "title B",
                    "description": "do B",
                    "dependencies": ["A"],
                },
            ],
        )
        assert cycle.is_error is True
        assert cycle.error_kind == "plan_revision_rejected"
        assert "cyclic" in cycle.content

        unknown = await invoke(
            harness.tools["plan_revise"],
            ctx,
            reason="depend on a task that does not exist",
            tasks=[
                {
                    "key": "A",
                    "title": "title A",
                    "description": "do A",
                    "dependencies": ["missing"],
                }
            ],
        )
        assert unknown.is_error is True
        assert unknown.error_kind == "plan_revision_rejected"
        assert "unknown" in unknown.content

        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None and plan.version == 1
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_plan_revise_without_plan(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()

        result = await invoke(
            harness.tools["plan_revise"],
            _ctx(session_id, fence),
            reason="there is no plan yet",
            tasks=_revise_tasks("A"),
        )

        assert result.is_error is True
        assert result.error_kind == "no_active_plan"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_task_tools_reject_direct_calls_without_executor_authorization(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, "A")
        task, _attempt = await harness.claim(session_id, fence)

        result = await invoke(
            harness.tools["task_complete"],
            _ctx(session_id, fence, approved=False),
            summary="this should be rejected",
            evidence=["none"],
        )

        assert result.is_error is True
        assert result.error_kind == "task_tool_unapproved"
        refreshed = await harness.tasks.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status.value == "in_progress"
        attempts = await harness.tasks.attempts(task.id)
        assert attempts[-1].completion_summary is None
    finally:
        harness.close()


def test_tool_argument_constraints() -> None:
    with pytest.raises(ValidationError):
        TaskCompleteArgs.model_validate({"summary": "tiny", "evidence": []})
    with pytest.raises(ValidationError):
        TaskBlockArgs.model_validate({"reason": "no"})

    parsed = TaskCompleteArgs.model_validate({"summary": "long enough"})
    assert parsed.evidence == []
