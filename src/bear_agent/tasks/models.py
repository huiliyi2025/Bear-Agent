"""Task / Plan / Todo models and the task state machine.

A Plan is the current structured decomposition of a user goal. Tasks are the
executable nodes; Todo items are a read-only projection of the same tasks, not
a second source of truth. Plan revisions are snapshots stored separately so the
current task rows keep stable identities and execution history.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from bear_agent.core.errors import PlanValidationError

TASK_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class PlanStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class VerificationMode(StrEnum):
    MODEL = "model"
    EVIDENCE = "evidence"


TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
        TaskStatus.SKIPPED,
    }
)

# Explicit edges. "completed -> pending" is only used when a plan revision
# changes the definition of a completed task and the caller opts into the
# revision reset explicitly.
ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
    ),
    TaskStatus.IN_PROGRESS: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.CANCELLED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.PENDING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset({TaskStatus.PENDING}),
    TaskStatus.CANCELLED: frozenset({TaskStatus.PENDING}),
    TaskStatus.SKIPPED: frozenset({TaskStatus.PENDING}),
}

ALLOWED_PLAN_TRANSITIONS: dict[PlanStatus, frozenset[PlanStatus]] = {
    PlanStatus.DRAFT: frozenset({PlanStatus.ACTIVE, PlanStatus.CANCELLED}),
    PlanStatus.ACTIVE: frozenset(
        {
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
            PlanStatus.BLOCKED,
            PlanStatus.CANCELLED,
        }
    ),
    PlanStatus.FAILED: frozenset({PlanStatus.ACTIVE, PlanStatus.CANCELLED}),
    PlanStatus.BLOCKED: frozenset({PlanStatus.ACTIVE, PlanStatus.CANCELLED}),
    PlanStatus.COMPLETED: frozenset({PlanStatus.ACTIVE}),
    PlanStatus.CANCELLED: frozenset({PlanStatus.ACTIVE}),
}


def validate_task_transition(
    current: TaskStatus | str,
    target: TaskStatus | str,
    *,
    allow_revision_reset: bool = False,
) -> None:
    source = TaskStatus(current)
    destination = TaskStatus(target)
    if destination in ALLOWED_TASK_TRANSITIONS[source]:
        if (
            source == TaskStatus.COMPLETED
            and destination == TaskStatus.PENDING
            and not allow_revision_reset
        ):
            raise PlanValidationError("a completed task can only be reset by a plan revision")
        return
    raise PlanValidationError(f"illegal task transition: {source.value} -> {destination.value}")


def validate_plan_transition(current: PlanStatus | str, target: PlanStatus | str) -> None:
    source = PlanStatus(current)
    destination = PlanStatus(target)
    if destination not in ALLOWED_PLAN_TRANSITIONS[source]:
        raise PlanValidationError(f"illegal plan transition: {source.value} -> {destination.value}")


class TaskSpec(BaseModel):
    key: str = Field(description="Stable identifier within this plan, e.g. T1.")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4_000)
    acceptance_criteria: str = Field(default="", max_length=2_000)
    dependencies: list[str] = Field(default_factory=list, max_length=64)
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    verification: VerificationMode = VerificationMode.MODEL

    @model_validator(mode="after")
    def _key_and_dependencies(self) -> TaskSpec:
        if not TASK_KEY_RE.match(self.key):
            raise ValueError(
                "task key must start with a letter/number and contain only "
                "letters, numbers, underscore, dot or dash (max 64)"
            )
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError(f"task {self.key} has duplicate dependencies")
        if self.key in self.dependencies:
            raise ValueError(f"task {self.key} cannot depend on itself")
        return self


class PlanDraft(BaseModel):
    goal: str = Field(min_length=1, max_length=4_000)
    tasks: list[TaskSpec] = Field(default_factory=list)
    notes: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def _unique_keys(self) -> PlanDraft:
        keys = [task.key for task in self.tasks]
        if len(set(keys)) != len(keys):
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            raise ValueError(f"duplicate task keys: {', '.join(duplicates)}")
        known = set(keys)
        for task in self.tasks:
            missing = [dep for dep in task.dependencies if dep not in known]
            if missing:
                raise ValueError(
                    f"task {task.key} depends on unknown task(s): {', '.join(missing)}"
                )
        return self


def validate_plan_draft(draft: PlanDraft, *, max_tasks: int) -> None:
    """Structural and dependency validation owned by the program.

    Pydantic already checked shape, unique keys, self-dependencies and unknown
    references. This function enforces limits and rejects cycles.
    """
    if not draft.tasks:
        raise PlanValidationError("a plan must contain at least one task")
    if len(draft.tasks) > max_tasks:
        raise PlanValidationError(f"plan has {len(draft.tasks)} tasks; the maximum is {max_tasks}")
    graph = {task.key: list(task.dependencies) for task in draft.tasks}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        colour = state.get(node, 0)
        if colour == 1:
            cycle_start = stack.index(node) if node in stack else 0
            cycle = [*stack[cycle_start:], node]
            raise PlanValidationError("cyclic task dependency: " + " -> ".join(cycle))
        if colour == 2:
            return
        state[node] = 1
        stack.append(node)
        for dependency in graph.get(node, []):
            visit(dependency)
        stack.pop()
        state[node] = 2

    for key in graph:
        visit(key)


class PlanRecord(BaseModel):
    id: str
    session_id: str
    goal: str
    status: PlanStatus
    version: int
    created_at: datetime
    updated_at: datetime


class TaskRecord(BaseModel):
    id: str
    plan_id: str
    session_id: str
    task_key: str
    title: str
    description: str
    acceptance_criteria: str
    status: TaskStatus
    priority: int
    order_index: int
    attempt_count: int
    max_attempts: int
    verification: VerificationMode
    active: bool
    result_summary: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskAttemptRecord(BaseModel):
    id: str
    task_id: str
    session_id: str
    attempt_no: int
    run_id: str | None
    status: AttemptStatus
    started_at: datetime
    finished_at: datetime | None
    completion_summary: str | None
    evidence: list[str] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    error_kind: str | None
    error_message: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanRevisionRecord(BaseModel):
    id: str
    plan_id: str
    session_id: str
    version: int
    goal: str
    reason: str | None
    source: Literal["model", "user", "system"]
    snapshot: dict[str, Any]
    created_by_run_id: str | None
    created_at: datetime


class TodoItem(BaseModel):
    task_id: str
    key: str
    title: str
    status: TaskStatus
    priority: int
    dependencies: list[str] = Field(default_factory=list)
    marker: str

    @classmethod
    def from_task(
        cls,
        task: TaskRecord,
        dependencies: list[str] | None = None,
    ) -> TodoItem:
        marker = {
            TaskStatus.COMPLETED: "[x]",
            TaskStatus.IN_PROGRESS: "[>]",
            TaskStatus.BLOCKED: "[!]",
            TaskStatus.FAILED: "[!]",
            TaskStatus.CANCELLED: "[-]",
            TaskStatus.SKIPPED: "[-]",
        }.get(task.status, "[ ]")
        return cls(
            task_id=task.id,
            key=task.task_key,
            title=task.title,
            status=task.status,
            priority=task.priority,
            dependencies=list(dependencies or []),
            marker=marker,
        )


class TaskRunReport(BaseModel):
    session_id: str
    plan_id: str | None = None
    status: str
    tasks_run: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_blocked: int = 0
    revisions: int = 0
    reason: str = ""
    issues: list[str] = Field(default_factory=list)


def utcnow() -> datetime:
    return datetime.now(UTC)
