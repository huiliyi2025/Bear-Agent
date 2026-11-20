"""Model-facing tools for task state.

The tools are the only way a model can signal completion, block, or revise a
plan. They are session-scoped writes to internal task state, not workspace
effects, so they are declared ``READ`` to avoid approval friction; every
mutation still goes through ``TaskService``, which validates transitions,
enforces the session lease fence and records events/audit.

The tools never call the runtime, permission engine, claims or audit directly.
``ToolExecutor`` remains the only execution path. A missing lease fence is
treated as a hard failure: state is never mutated without proving the caller
still owns the session.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from bear_agent.core.errors import TaskError
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.modes import ToolSideEffect
from bear_agent.tasks.models import PlanDraft, TaskRecord, TaskSpec
from bear_agent.tasks.service import TaskService
from bear_agent.tools.base import Tool, ToolContext, ToolResult

_NO_ACTIVE_TASK = (
    "no task is currently in progress in this session; call this tool only while executing a task"
)
_NO_ACTIVE_PLAN = "this session has no plan; create a plan before revising it"
_FENCE_MISSING = "this call has no valid session lease fence; task state was not modified"


def _fence_from(ctx: ToolContext) -> LeaseFence | None:
    if not ctx.lease_owner or ctx.lease_epoch is None or ctx.lease_epoch < 1:
        return None
    return LeaseFence(ctx.lease_owner, ctx.lease_epoch)


def _fence_error() -> ToolResult:
    return ToolResult(
        content=_FENCE_MISSING,
        is_error=True,
        error_kind="task_fence_unavailable",
    )


def _run_id_from(ctx: ToolContext) -> str | None:
    value = ctx.extras.get("run_id")
    return value if isinstance(value, str) and value else None


class _TaskTool(Tool):
    side_effect = ToolSideEffect.READ
    concurrency_safe = False
    idempotent = False

    def __init__(self, service: TaskService) -> None:
        self.service = service

    async def _resolve_active(self, ctx: ToolContext) -> tuple[TaskRecord, LeaseFence] | ToolResult:
        if not ctx.approved:
            return ToolResult(
                content="task tools must be invoked through the tool executor authorization chain",
                is_error=True,
                error_kind="task_tool_unapproved",
            )
        task = await self.service.active_task(ctx.session_id)
        if task is None:
            return ToolResult(
                content=_NO_ACTIVE_TASK,
                is_error=True,
                error_kind="no_active_task",
            )
        fence = _fence_from(ctx)
        if fence is None:
            return _fence_error()
        return task, fence


class TaskCompleteArgs(BaseModel):
    summary: str = Field(
        min_length=5,
        max_length=4_000,
        description=("What was accomplished and how it satisfies the acceptance criteria."),
    )
    evidence: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete evidence such as commands run, test results, file paths or "
            "outputs observed. Empty evidence is not enough for evidence-based "
            "verification."
        ),
    )


class TaskCompleteTool(_TaskTool):
    name = "task_complete"
    description = (
        "Request completion of the task currently in progress. Provide a concise "
        "summary and concrete evidence (commands run, test results, files or "
        "observed output). The engine verifies the claim independently; calling "
        "this does not mark the task complete and the task may still be retried "
        "or blocked."
    )
    args_model = TaskCompleteArgs
    idempotent = True

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TaskCompleteArgs)
        resolved = await self._resolve_active(ctx)
        if isinstance(resolved, ToolResult):
            return resolved
        task, fence = resolved
        try:
            attempt = await self.service.request_completion(
                task.id,
                summary=args.summary,
                evidence=args.evidence,
                fence=fence,
                run_id=_run_id_from(ctx),
            )
        except TaskError as exc:
            return ToolResult(
                content=f"completion request rejected: {exc}",
                is_error=True,
                error_kind="task_completion_rejected",
            )
        return ToolResult(
            content=(
                f"Completion request recorded for task {task.task_key}. The "
                "engine will independently verify the result before the task is "
                f"marked complete; {len(attempt.evidence)} evidence item(s) "
                "were recorded."
            ),
            meta={
                "task_id": task.id,
                "task_key": task.task_key,
                "attempt_id": attempt.id,
                "attempt_no": attempt.attempt_no,
                "evidence_count": len(attempt.evidence),
            },
        )


class TaskBlockArgs(BaseModel):
    reason: str = Field(
        min_length=5,
        max_length=4_000,
        description=(
            "Why the task cannot be completed, e.g. missing credentials, "
            "contradictory requirements or repeated failures."
        ),
    )


class TaskBlockTool(_TaskTool):
    name = "task_block"
    description = (
        "Mark the task currently in progress as blocked when it cannot be "
        "completed: missing credentials, contradictory requirements, an "
        "unavailable dependency or repeated failures. Work on the task stops; "
        "revise the plan when the blocker changes the approach."
    )
    args_model = TaskBlockArgs
    idempotent = False

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TaskBlockArgs)
        resolved = await self._resolve_active(ctx)
        if isinstance(resolved, ToolResult):
            return resolved
        task, fence = resolved
        try:
            blocked = await self.service.block_task(
                task.id,
                reason=args.reason,
                fence=fence,
                run_id=_run_id_from(ctx),
            )
        except TaskError as exc:
            return ToolResult(
                content=f"block request rejected: {exc}",
                is_error=True,
                error_kind="task_block_rejected",
            )
        return ToolResult(
            content=(
                f"Task {blocked.task_key} is now blocked. Reason: {args.reason}. "
                "Work on it has stopped; the plan may need a revision."
            ),
            meta={
                "task_id": blocked.id,
                "task_key": blocked.task_key,
                "status": blocked.status.value,
            },
        )


class PlanReviseArgs(BaseModel):
    reason: str = Field(
        min_length=5,
        max_length=4_000,
        description="Why the current plan is being replaced.",
    )
    goal: str | None = Field(
        default=None,
        max_length=4_000,
        description="Replacement goal; defaults to the current plan goal.",
    )
    tasks: list[TaskSpec] = Field(
        min_length=1,
        max_length=100,
        description="The complete replacement task list.",
    )


class PlanReviseTool(_TaskTool):
    name = "plan_revise"
    description = (
        "Replace the active plan with a revised task list. Use when the goal or "
        "approach changed, tasks are wrong or incomplete, or a blocker requires "
        "a different decomposition. Provide the complete task list, not a diff. "
        "Revisions are rejected while a task is in progress and after the "
        "revision limit."
    )
    args_model = PlanReviseArgs
    idempotent = False

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, PlanReviseArgs)
        if not ctx.approved:
            return ToolResult(
                content="task tools must be invoked through the tool executor authorization chain",
                is_error=True,
                error_kind="task_tool_unapproved",
            )
        fence = _fence_from(ctx)
        if fence is None:
            return _fence_error()
        plan = await self.service.get_plan(ctx.session_id)
        if plan is None:
            return ToolResult(
                content=_NO_ACTIVE_PLAN,
                is_error=True,
                error_kind="no_active_plan",
            )
        goal = (args.goal or "").strip() or plan.goal
        try:
            draft = PlanDraft(goal=goal, tasks=args.tasks)
            revised = await self.service.revise_plan(
                ctx.session_id,
                draft,
                reason=args.reason,
                source="model",
                run_id=_run_id_from(ctx),
                fence=fence,
            )
        except (TaskError, ValidationError) as exc:
            return ToolResult(
                content=f"plan revision rejected: {exc}",
                is_error=True,
                error_kind="plan_revision_rejected",
            )
        return ToolResult(
            content=(
                f"Plan revised to version {revised.version}. Active tasks: "
                + ", ".join(spec.key for spec in args.tasks)
            ),
            meta={
                "plan_id": revised.id,
                "version": revised.version,
                "task_keys": [spec.key for spec in args.tasks],
            },
        )


def create_task_tools(service: TaskService) -> list[Tool]:
    """Build the task-state tools bound to one ``TaskService``."""
    return [
        TaskCompleteTool(service),
        TaskBlockTool(service),
        PlanReviseTool(service),
    ]


__all__ = [
    "PlanReviseArgs",
    "PlanReviseTool",
    "TaskBlockArgs",
    "TaskBlockTool",
    "TaskCompleteArgs",
    "TaskCompleteTool",
    "create_task_tools",
]
