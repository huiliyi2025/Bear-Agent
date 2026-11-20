"""Task / Plan / Todo application service.

The service owns every state transition, dependency decision and revision
merge. Models can propose plans and completion, but they cannot write task
state directly: tools call this service, which validates transitions and
fences writes with the session lease.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from bear_agent.audit.service import AuditService
from bear_agent.config.models import TasksConfig
from bear_agent.context.tokens import estimate_text_tokens
from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.errors import AuditError, PlanValidationError, TaskStateError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.ids import new_id
from bear_agent.observability.recorder import EventRecorder
from bear_agent.observability.trace import span
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.storage.database import Database, transaction
from bear_agent.storage.repositories import assert_session_lease_row
from bear_agent.storage.task_repositories import TaskRepository
from bear_agent.tasks.models import (
    AttemptStatus,
    PlanDraft,
    PlanRecord,
    PlanRevisionRecord,
    PlanStatus,
    TaskAttemptRecord,
    TaskRecord,
    TaskSpec,
    TaskStatus,
    TodoItem,
    validate_plan_draft,
    validate_plan_transition,
    validate_task_transition,
)

logger = logging.getLogger(__name__)


def _copy[ModelT: BaseModel](model: ModelT, **changes: Any) -> ModelT:
    return model.model_copy(update=changes)


ACTIVE_PLAN_STATUSES = frozenset({PlanStatus.DRAFT, PlanStatus.ACTIVE})


class TaskService:
    def __init__(
        self,
        db: Database,
        recorder: EventRecorder,
        *,
        audit: AuditService | None = None,
        config: TasksConfig | None = None,
        clock: Clock | None = None,
        redactor: SecretRedactor | None = None,
        repository: TaskRepository | None = None,
    ) -> None:
        self.db = db
        self.repository = repository or TaskRepository(db)
        self.recorder = recorder
        self.audit = audit
        self.config = config or TasksConfig()
        self.clock = clock or SystemClock()
        self.redactor = redactor or SecretRedactor()

    # ------------------------------------------------------------------ reads

    async def get_plan(self, session_id: str) -> PlanRecord | None:
        return await self.repository.get_plan(session_id)

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return await self.repository.get_task(task_id)

    async def list_tasks(self, session_id: str, *, active_only: bool = True) -> list[TaskRecord]:
        plan = await self.repository.get_plan(session_id)
        if plan is None:
            return []
        return await self.repository.list_tasks(plan.id, active_only=active_only)

    async def dependencies(self, task_id: str) -> list[str]:
        return await self.repository.dependency_keys(task_id)

    async def attempts(self, task_id: str) -> list[TaskAttemptRecord]:
        return await self.repository.attempts(task_id)

    async def running_attempts(self, session_id: str) -> list[TaskAttemptRecord]:
        return await self.repository.running_attempts(session_id)

    async def attempt_used_non_idempotent_tool(self, session_id: str, run_id: str | None) -> bool:
        """True when the run executed a tool that is not idempotent.

        Automatic retry of such a task could repeat an external side effect
        (for example a shell command). The engine blocks it for explicit user
        retry instead.
        """
        if not run_id:
            return False
        return await self.db.run(
            lambda conn: self._used_non_idempotent_sync(conn, session_id, run_id)
        )

    def _used_non_idempotent_sync(
        self, conn: sqlite3.Connection, session_id: str, run_id: str
    ) -> bool:
        rows = conn.execute(
            "SELECT payload_json FROM events "
            "WHERE session_id = ? AND run_id = ? AND type = 'tool.call.started'",
            (session_id, run_id),
        ).fetchall()
        call_ids: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            call_id = payload.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                call_ids.add(call_id)
        if not call_ids:
            return False
        placeholders = ",".join("?" for _ in call_ids)
        flags = conn.execute(
            f"SELECT idempotent FROM tool_calls WHERE id IN ({placeholders})",
            tuple(call_ids),
        ).fetchall()
        return any(not bool(row["idempotent"]) for row in flags)

    async def revisions(self, session_id: str) -> list[PlanRevisionRecord]:
        plan = await self.repository.get_plan(session_id)
        if plan is None:
            return []
        return await self.repository.revisions(plan.id)

    async def todo(self, session_id: str) -> list[TodoItem]:
        plan = await self.repository.get_plan(session_id)
        if plan is None:
            return []
        tasks = await self.repository.list_tasks(plan.id, active_only=True)
        dep_map = await self.repository.dependencies(plan.id)
        keys = {task.id: task.task_key for task in tasks}
        return [
            TodoItem.from_task(
                task,
                [keys.get(dep, dep) for dep in dep_map.get(task.id, [])],
            )
            for task in tasks
        ]

    async def active_task(self, session_id: str) -> TaskRecord | None:
        tasks = await self.list_tasks(session_id)
        for task in tasks:
            if task.status == TaskStatus.IN_PROGRESS:
                return task
        return None

    def _ready_from(
        self,
        tasks: list[TaskRecord],
        dep_map: dict[str, list[str]],
    ) -> list[TaskRecord]:
        by_id = {task.id: task for task in tasks}
        ready: list[TaskRecord] = []
        for task in tasks:
            if task.status != TaskStatus.PENDING or not task.active:
                continue
            deps = dep_map.get(task.id, [])
            if all(
                by_id.get(dep) is not None
                and by_id[dep].status in {TaskStatus.COMPLETED, TaskStatus.SKIPPED}
                for dep in deps
            ):
                ready.append(task)
        ready.sort(key=lambda item: (-item.priority, item.order_index, item.created_at))
        return ready

    async def ready_tasks(self, session_id: str) -> list[TaskRecord]:
        plan = await self.repository.get_plan(session_id)
        if plan is None:
            return []
        tasks = await self.repository.list_tasks(plan.id, active_only=True)
        deps = await self.repository.dependencies(plan.id)
        return self._ready_from(tasks, deps)

    async def render_context(self, session_id: str, *, max_tokens: int | None = None) -> str:
        """Pinned context injected into every model request for this session."""
        plan = await self.repository.get_plan(session_id)
        if plan is None:
            return ""
        tasks = await self.repository.list_tasks(plan.id, active_only=True)
        dep_map = await self.repository.dependencies(plan.id)
        keys = {task.id: task.task_key for task in tasks}
        lines = [
            "<task-plan>",
            f"Goal: {plan.goal}",
            f"Plan: v{plan.version} ({plan.status.value})",
            "Todo:",
        ]
        for item in self._todo_from(tasks, dep_map, keys):
            dependency_text = (
                f" (depends: {', '.join(item.dependencies)})" if item.dependencies else ""
            )
            lines.append(f"{item.marker} {item.key} {item.title}{dependency_text}")
        active = next(
            (task for task in tasks if task.status == TaskStatus.IN_PROGRESS),
            None,
        )
        if active is not None:
            lines.extend(
                [
                    "",
                    f"Current task: {active.task_key} — {active.title}",
                    f"Acceptance criteria: {active.acceptance_criteria or '(none provided)'}",
                    "Call task_complete only when the acceptance criteria are met; "
                    "call task_block if progress is impossible. Do not claim completion "
                    "without evidence.",
                ]
            )
        lines.append("</task-plan>")
        text = "\n".join(lines)
        budget = max_tokens or self.config.context_max_tokens
        if estimate_text_tokens(text) <= budget:
            return text
        # Keep the goal and current task; trim oldest todo lines first.
        while estimate_text_tokens("\n".join(lines)) > budget and len(lines) > 6:
            lines.pop(4)
        return "\n".join(lines)

    async def render_task_prompt(self, session_id: str, task: TaskRecord) -> str:
        plan = await self.repository.get_plan(session_id)
        todo = await self.todo(session_id)
        goal = plan.goal if plan else ""
        dependency_keys = await self.repository.dependency_keys(task.id)
        lines = [
            "You are executing one task from a larger plan.",
            f"Plan goal: {goal}",
            f"Task {task.task_key}: {task.title}",
            f"Description: {task.description}",
            f"Acceptance criteria: {task.acceptance_criteria or '(none provided)'}",
        ]
        if dependency_keys:
            lines.append("Completed dependencies: " + ", ".join(dependency_keys))
        lines.append("Todo:")
        lines.extend(f"{item.marker} {item.key} {item.title}" for item in todo)
        lines.extend(
            [
                "",
                "Work only on this task. Use tools to verify facts. When the acceptance "
                "criteria are satisfied, call task_complete with a summary and concrete "
                "evidence. If you cannot proceed, call task_block with a reason. Do not "
                "modify task state directly; the engine will verify completion.",
            ]
        )
        return "\n".join(lines)

    def _todo_from(
        self,
        tasks: list[TaskRecord],
        dep_map: dict[str, list[str]],
        keys: dict[str, str],
    ) -> list[TodoItem]:
        return [
            TodoItem.from_task(
                task,
                [keys.get(dep, dep) for dep in dep_map.get(task.id, [])],
            )
            for task in tasks
        ]

    # ----------------------------------------------------------------- create

    async def create_plan(
        self,
        session_id: str,
        draft: PlanDraft,
        *,
        source: Literal["model", "user", "system"] = "model",
        reason: str | None = None,
        run_id: str | None = None,
        fence: LeaseFence | None = None,
    ) -> PlanRecord:
        validate_plan_draft(draft, max_tasks=self.config.max_tasks)
        now = self.clock.now()
        plan = PlanRecord(
            id=new_id("plan"),
            session_id=session_id,
            goal=self.redactor.redact(draft.goal),
            status=PlanStatus.DRAFT,
            version=1,
            created_at=now,
            updated_at=now,
        )

        def _work(conn: sqlite3.Connection) -> PlanRecord:
            with transaction(conn):
                if fence is not None:
                    assert_session_lease_row(conn, session_id, fence)
                if self.repository._plan_for_session(conn, session_id) is not None:
                    raise TaskStateError(
                        "this session already has a plan; revise it instead of creating another"
                    )
                self.repository.insert_plan(conn, plan)
                key_ids: dict[str, str] = {}
                tasks: list[TaskRecord] = []
                for order, raw_spec in enumerate(draft.tasks):
                    spec = self._redacted_spec(raw_spec)
                    task = self._task_from_spec(plan, spec, order=order, now=now)
                    self.repository.insert_task(conn, task)
                    key_ids[spec.key] = task.id
                    tasks.append(task)
                for spec, task in zip(draft.tasks, tasks, strict=True):
                    self.repository.replace_dependencies(
                        conn,
                        plan.id,
                        task.id,
                        [key_ids[dep] for dep in spec.dependencies],
                    )
                snapshot = self.repository.snapshot_for_plan(conn, plan)
                self.repository.insert_revision(
                    conn,
                    PlanRevisionRecord(
                        id=new_id("rev"),
                        plan_id=plan.id,
                        session_id=session_id,
                        version=plan.version,
                        goal=plan.goal,
                        reason=self.redactor.redact(reason or "initial plan"),
                        source=source,
                        snapshot=snapshot,
                        created_by_run_id=run_id,
                        created_at=now,
                    ),
                )
                return plan

        stored = await self.db.run(_work)
        await self._event(
            session_id,
            EventType.TASK_PLAN_CREATED,
            fence=fence,
            run_id=run_id,
            payload={
                "plan_id": stored.id,
                "version": stored.version,
                "goal": stored.goal,
                "tasks": [task.key for task in draft.tasks],
                "source": source,
            },
        )
        await self._audit(
            "task.plan.created",
            session_id,
            resource=stored.id,
            detail={"version": stored.version, "tasks": len(draft.tasks), "source": source},
        )
        return stored

    def _redacted_spec(self, spec: TaskSpec) -> TaskSpec:
        return spec.model_copy(
            update={
                "title": self.redactor.redact(spec.title),
                "description": self.redactor.redact(spec.description),
                "acceptance_criteria": self.redactor.redact(spec.acceptance_criteria),
            }
        )

    def _task_from_spec(
        self,
        plan: PlanRecord,
        spec: TaskSpec,
        *,
        order: int,
        now: datetime,
        task_id: str | None = None,
    ) -> TaskRecord:
        return TaskRecord(
            id=task_id or new_id("task"),
            plan_id=plan.id,
            session_id=plan.session_id,
            task_key=spec.key,
            title=spec.title,
            description=spec.description,
            acceptance_criteria=spec.acceptance_criteria,
            status=TaskStatus.PENDING,
            priority=spec.priority,
            order_index=order,
            attempt_count=0,
            max_attempts=min(
                spec.max_attempts or self.config.max_attempts,
                self.config.max_attempts,
            ),
            verification=spec.verification,
            active=True,
            result_summary=None,
            last_error=None,
            created_at=now,
            updated_at=now,
            started_at=None,
            finished_at=None,
            metadata={},
        )

    # ----------------------------------------------------------------- revise

    async def revise_plan(
        self,
        session_id: str,
        draft: PlanDraft,
        *,
        reason: str,
        source: Literal["model", "user", "system"] = "model",
        run_id: str | None = None,
        fence: LeaseFence | None = None,
    ) -> PlanRecord:
        async with span(
            "plan.revision",
            "plan",
            {
                "session_id": session_id,
                "run_id": run_id,
                "source": source,
                "draft_tasks": len(draft.tasks),
            },
        ) as revision_span:
            plan = await self._revise_plan(
                session_id,
                draft,
                reason=reason,
                source=source,
                run_id=run_id,
                fence=fence,
            )
            if revision_span is not None:
                revision_span.set_attributes({"plan_id": plan.id, "version": plan.version})
            return plan

    async def _revise_plan(
        self,
        session_id: str,
        draft: PlanDraft,
        *,
        reason: str,
        source: Literal["model", "user", "system"] = "model",
        run_id: str | None = None,
        fence: LeaseFence | None = None,
    ) -> PlanRecord:
        if not reason.strip():
            raise PlanValidationError("a plan revision requires a reason")
        reason = self.redactor.redact(reason)
        validate_plan_draft(draft, max_tasks=self.config.max_tasks)
        now = self.clock.now()

        def _work(conn: sqlite3.Connection) -> PlanRecord:
            with transaction(conn):
                if fence is not None:
                    assert_session_lease_row(conn, session_id, fence)
                plan = self.repository._plan_for_session(conn, session_id)
                if plan is None:
                    raise TaskStateError(f"session {session_id} has no plan to revise")
                if plan.status == PlanStatus.CANCELLED:
                    raise TaskStateError("a cancelled plan cannot be revised; create a new session")
                if self.repository._revision_count(conn, plan.id) - 1 >= self.config.max_revisions:
                    raise TaskStateError(
                        f"plan revision limit reached ({self.config.max_revisions})"
                    )
                existing = self.repository._tasks(conn, plan.id, active_only=False)
                by_key = {task.task_key: task for task in existing}
                if any(task.status == TaskStatus.IN_PROGRESS for task in existing):
                    raise TaskStateError(
                        "cannot revise a plan while a task is in progress; "
                        "block or cancel the active task first"
                    )

                kept: dict[str, TaskRecord] = {}
                for order, raw_spec in enumerate(draft.tasks):
                    spec = self._redacted_spec(raw_spec)
                    current = by_key.get(spec.key)
                    if current is None:
                        task = self._task_from_spec(plan, spec, order=order, now=now)
                        self.repository.insert_task(conn, task)
                        kept[spec.key] = task
                        continue
                    changed = self._definition_changed(current, spec)
                    status = current.status
                    attempt_count = current.attempt_count
                    started_at = current.started_at
                    finished_at = current.finished_at
                    result_summary = current.result_summary
                    last_error = current.last_error
                    if status == TaskStatus.COMPLETED and changed:
                        validate_task_transition(
                            status, TaskStatus.PENDING, allow_revision_reset=True
                        )
                        status = TaskStatus.PENDING
                        started_at = None
                        finished_at = None
                        result_summary = None
                        attempt_count = 0
                        last_error = "completed task definition changed by plan revision"
                    elif status in {TaskStatus.FAILED, TaskStatus.BLOCKED} and changed:
                        validate_task_transition(status, TaskStatus.PENDING)
                        status = TaskStatus.PENDING
                        started_at = None
                        finished_at = None
                        result_summary = None
                        attempt_count = 0
                        last_error = "task definition changed by plan revision"
                    elif status in {TaskStatus.CANCELLED, TaskStatus.SKIPPED}:
                        status = TaskStatus.PENDING
                        finished_at = None
                        attempt_count = 0
                    task = _copy(
                        current,
                        title=spec.title,
                        description=spec.description,
                        acceptance_criteria=spec.acceptance_criteria,
                        priority=spec.priority,
                        order_index=order,
                        max_attempts=min(
                            spec.max_attempts or self.config.max_attempts,
                            self.config.max_attempts,
                        ),
                        verification=spec.verification,
                        active=True,
                        status=status,
                        attempt_count=attempt_count,
                        started_at=started_at,
                        finished_at=finished_at,
                        result_summary=result_summary,
                        last_error=last_error,
                        updated_at=now,
                    )
                    self.repository.update_task(conn, task)
                    kept[spec.key] = task

                for task in existing:
                    if task.task_key in kept:
                        continue
                    if not task.active:
                        continue
                    archived_status = task.status
                    if archived_status != TaskStatus.COMPLETED:
                        archived_status = TaskStatus.CANCELLED
                    archived = _copy(
                        task,
                        active=False,
                        status=archived_status,
                        last_error=(
                            task.last_error
                            if archived_status == TaskStatus.COMPLETED
                            else f"removed by plan revision v{plan.version + 1}"
                        ),
                        updated_at=now,
                        finished_at=(
                            task.finished_at if archived_status == TaskStatus.COMPLETED else now
                        ),
                    )
                    self.repository.update_task(conn, archived)

                all_tasks = {task.task_key: task for task in kept.values()}
                for spec in draft.tasks:
                    task = all_tasks[spec.key]
                    self.repository.replace_dependencies(
                        conn,
                        plan.id,
                        task.id,
                        [all_tasks[dep].id for dep in spec.dependencies],
                    )

                if plan.status != PlanStatus.ACTIVE:
                    validate_plan_transition(plan.status, PlanStatus.ACTIVE)
                updated = _copy(
                    plan,
                    goal=self.redactor.redact(draft.goal),
                    status=PlanStatus.ACTIVE,
                    version=plan.version + 1,
                    updated_at=now,
                )
                self.repository.update_plan(conn, updated)
                snapshot = self.repository.snapshot_for_plan(conn, updated)
                self.repository.insert_revision(
                    conn,
                    PlanRevisionRecord(
                        id=new_id("rev"),
                        plan_id=plan.id,
                        session_id=session_id,
                        version=updated.version,
                        goal=updated.goal,
                        reason=reason,
                        source=source,
                        snapshot=snapshot,
                        created_by_run_id=run_id,
                        created_at=now,
                    ),
                )
                return updated

        stored = await self.db.run(_work)
        await self._event(
            session_id,
            EventType.TASK_PLAN_REVISED,
            fence=fence,
            run_id=run_id,
            payload={
                "plan_id": stored.id,
                "version": stored.version,
                "goal": stored.goal,
                "reason": reason,
                "source": source,
                "tasks": [task.key for task in draft.tasks],
            },
        )
        await self._audit(
            "task.plan.revised",
            session_id,
            resource=stored.id,
            detail={"version": stored.version, "reason": reason, "source": source},
        )
        return stored

    def _definition_changed(self, task: TaskRecord, spec: TaskSpec) -> bool:
        effective_attempts = min(
            spec.max_attempts or self.config.max_attempts,
            self.config.max_attempts,
        )
        return (
            task.title != spec.title
            or task.description != spec.description
            or task.acceptance_criteria != spec.acceptance_criteria
            or task.priority != spec.priority
            or task.verification != spec.verification
            or task.max_attempts != effective_attempts
        )

    # -------------------------------------------------------------- scheduling

    async def claim_next(
        self,
        session_id: str,
        *,
        fence: LeaseFence | None = None,
        run_id: str | None = None,
    ) -> tuple[TaskRecord, TaskAttemptRecord] | None:
        """Atomically pick and start the next runnable task.

        Claiming is serialized by the session lease when a fence is supplied;
        the same transaction checks that no other task is in progress and that
        all dependencies are complete.
        """
        now = self.clock.now()
        claimed: tuple[TaskRecord, TaskAttemptRecord] | None = None

        def _work(conn: sqlite3.Connection) -> tuple[TaskRecord, TaskAttemptRecord] | None:
            nonlocal claimed
            with transaction(conn):
                if fence is not None:
                    assert_session_lease_row(conn, session_id, fence)
                plan = self.repository._plan_for_session(conn, session_id)
                if plan is None or plan.status in {
                    PlanStatus.COMPLETED,
                    PlanStatus.CANCELLED,
                }:
                    return None
                tasks = self.repository._tasks(conn, plan.id, active_only=True)
                in_progress = [task for task in tasks if task.status == TaskStatus.IN_PROGRESS]
                if in_progress:
                    raise TaskStateError(f"task {in_progress[0].task_key} is already in progress")
                deps = self.repository._dependencies(conn, plan.id)
                ready = self._ready_from(tasks, deps)
                if not ready:
                    evaluated = self._evaluate_locked(conn, plan, tasks, deps)
                    if evaluated.status != plan.status:
                        self.repository.update_plan(conn, evaluated)
                    return None
                task = ready[0]
                if plan.status in {PlanStatus.DRAFT, PlanStatus.FAILED, PlanStatus.BLOCKED}:
                    validate_plan_transition(plan.status, PlanStatus.ACTIVE)
                    plan = _copy(plan, status=PlanStatus.ACTIVE, updated_at=now)
                    self.repository.update_plan(conn, plan)
                validate_task_transition(task.status, TaskStatus.IN_PROGRESS)
                # attempt_no is monotonic across plan revisions; attempt_count
                # is the per-revision retry budget and may legitimately reset.
                attempt_no = self.repository._max_attempt_no(conn, task.id) + 1
                message_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS n FROM messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                start_message_seq = int(message_row["n"]) + 1
                attempt = TaskAttemptRecord(
                    id=new_id("att"),
                    task_id=task.id,
                    session_id=session_id,
                    attempt_no=attempt_no,
                    run_id=run_id,
                    status=AttemptStatus.RUNNING,
                    started_at=now,
                    finished_at=None,
                    completion_summary=None,
                    evidence=[],
                    verification={},
                    error_kind=None,
                    error_message=None,
                    metadata={"start_message_seq": start_message_seq},
                )
                updated_task = _copy(
                    task,
                    status=TaskStatus.IN_PROGRESS,
                    attempt_count=task.attempt_count + 1,
                    started_at=now,
                    finished_at=None,
                    last_error=None,
                    updated_at=now,
                )
                self.repository.update_task(conn, updated_task)
                self.repository.insert_attempt(conn, attempt)
                claimed = (updated_task, attempt)
                return claimed

        result = await self.db.run(_work)
        if result is not None:
            task, attempt = result
            await self._event(
                session_id,
                EventType.TASK_STARTED,
                fence=fence,
                run_id=run_id,
                payload={
                    "task_id": task.id,
                    "task_key": task.task_key,
                    "title": task.title,
                    "attempt": attempt.attempt_no,
                },
            )
            await self._event(
                session_id,
                EventType.TASK_ATTEMPT_STARTED,
                fence=fence,
                run_id=run_id,
                payload={
                    "task_id": task.id,
                    "attempt_id": attempt.id,
                    "attempt": attempt.attempt_no,
                },
            )
            await self._audit(
                "task.started",
                session_id,
                resource=task.id,
                detail={
                    "task_key": task.task_key,
                    "attempt": attempt.attempt_no,
                    "run_id": run_id,
                },
            )
        return result

    def _evaluate_locked(
        self,
        conn: sqlite3.Connection,
        plan: PlanRecord,
        tasks: list[TaskRecord],
        deps: dict[str, list[str]],
    ) -> PlanRecord:
        active = [task for task in tasks if task.active]
        if not active:
            return _copy(plan, status=PlanStatus.COMPLETED, updated_at=self.clock.now())
        if any(task.status == TaskStatus.IN_PROGRESS for task in active):
            status = PlanStatus.ACTIVE
        elif any(task.status == TaskStatus.PENDING for task in active):
            ready = self._ready_from(active, deps)
            status = PlanStatus.ACTIVE if ready else PlanStatus.BLOCKED
        elif all(task.status in {TaskStatus.COMPLETED, TaskStatus.SKIPPED} for task in active):
            status = PlanStatus.COMPLETED
        elif any(task.status == TaskStatus.FAILED for task in active):
            status = PlanStatus.FAILED
        elif any(task.status == TaskStatus.BLOCKED for task in active):
            status = PlanStatus.BLOCKED
        else:
            status = PlanStatus.CANCELLED
        return _copy(plan, status=status, updated_at=self.clock.now())

    async def evaluate_plan(
        self, session_id: str, *, fence: LeaseFence | None = None
    ) -> PlanRecord | None:
        def _work(conn: sqlite3.Connection) -> PlanRecord | None:
            with transaction(conn):
                if fence is not None:
                    assert_session_lease_row(conn, session_id, fence)
                plan = self.repository._plan_for_session(conn, session_id)
                if plan is None:
                    return None
                tasks = self.repository._tasks(conn, plan.id, active_only=True)
                deps = self.repository._dependencies(conn, plan.id)
                evaluated = self._evaluate_locked(conn, plan, tasks, deps)
                if evaluated.status != plan.status:
                    self.repository.update_plan(conn, evaluated)
                return evaluated

        result = await self.db.run(_work)
        if result is not None and result.status != PlanStatus.ACTIVE:
            event_type = {
                PlanStatus.COMPLETED: EventType.TASK_PLAN_COMPLETED,
                PlanStatus.BLOCKED: EventType.TASK_PLAN_BLOCKED,
                PlanStatus.FAILED: EventType.TASK_PLAN_FAILED,
                PlanStatus.CANCELLED: EventType.TASK_PLAN_CANCELLED,
            }.get(result.status)
            if event_type is not None:
                await self._event(
                    session_id,
                    event_type,
                    fence=fence,
                    payload={"plan_id": result.id, "status": result.status.value},
                )
        return result

    # ------------------------------------------------------------- completion

    async def request_completion(
        self,
        task_id: str,
        *,
        summary: str,
        evidence: list[str],
        fence: LeaseFence | None = None,
        run_id: str | None = None,
    ) -> TaskAttemptRecord:
        summary = self.redactor.redact(summary.strip())
        if not summary:
            raise TaskStateError("task_complete requires a non-empty summary")

        def _work(conn: sqlite3.Connection) -> TaskAttemptRecord:
            with transaction(conn):
                task = self.repository._task(conn, task_id)
                if task is None:
                    raise TaskStateError(f"unknown task: {task_id}")
                if fence is not None:
                    assert_session_lease_row(conn, task.session_id, fence)
                if task.status != TaskStatus.IN_PROGRESS:
                    raise TaskStateError(
                        f"task {task.task_key} is {task.status.value}, not in_progress"
                    )
                attempt = self.repository._active_attempt(conn, task_id)
                if attempt is None:
                    raise TaskStateError(f"task {task.task_key} has no running attempt")
                updated = _copy(
                    attempt,
                    completion_summary=summary,
                    evidence=[
                        self.redactor.redact(item.strip()) for item in evidence if item.strip()
                    ],
                    run_id=run_id or attempt.run_id,
                )
                self.repository.update_attempt(conn, updated)
                return updated

        attempt = await self.db.run(_work)
        await self._event(
            session_id=attempt.session_id,
            event_type=EventType.TASK_COMPLETION_REQUESTED,
            fence=fence,
            run_id=run_id or attempt.run_id,
            payload={
                "task_id": task_id,
                "attempt_id": attempt.id,
                "summary": attempt.completion_summary,
                "evidence": attempt.evidence,
            },
        )
        return attempt

    async def complete_task(
        self,
        task_id: str,
        attempt_id: str,
        *,
        summary: str,
        verification: dict[str, Any],
        fence: LeaseFence | None = None,
        run_id: str | None = None,
    ) -> TaskRecord:
        now = self.clock.now()
        summary = self.redactor.redact(summary)
        verification = self.redactor.redact_obj(verification)

        def _work(conn: sqlite3.Connection) -> TaskRecord:
            with transaction(conn):
                task = self.repository._task(conn, task_id)
                if task is None:
                    raise TaskStateError(f"unknown task: {task_id}")
                if fence is not None:
                    assert_session_lease_row(conn, task.session_id, fence)
                validate_task_transition(task.status, TaskStatus.COMPLETED)
                attempt = self.repository._active_attempt(conn, task_id)
                if attempt is None or attempt.id != attempt_id:
                    raise TaskStateError("completion requires the active attempt")
                updated = _copy(
                    task,
                    status=TaskStatus.COMPLETED,
                    result_summary=summary,
                    last_error=None,
                    finished_at=now,
                    updated_at=now,
                )
                self.repository.update_task(conn, updated)
                self.repository.update_attempt(
                    conn,
                    _copy(
                        attempt,
                        status=AttemptStatus.COMPLETED,
                        finished_at=now,
                        completion_summary=summary,
                        verification=verification,
                        run_id=run_id or attempt.run_id,
                    ),
                )
                return updated

        task = await self.db.run(_work)
        await self._event(
            task.session_id,
            EventType.TASK_COMPLETED,
            fence=fence,
            run_id=run_id,
            payload={"task_id": task.id, "task_key": task.task_key, "summary": summary},
        )
        await self._event(
            task.session_id,
            EventType.TASK_ATTEMPT_FINISHED,
            fence=fence,
            run_id=run_id,
            payload={"task_id": task.id, "attempt_id": attempt_id, "status": "completed"},
        )
        await self._audit(
            "task.completed",
            task.session_id,
            resource=task.id,
            detail={"task_key": task.task_key, "verification": verification},
        )
        await self.evaluate_plan(task.session_id, fence=fence)
        return task

    async def fail_task(
        self,
        task_id: str,
        attempt_id: str,
        *,
        reason: str,
        error_kind: str = "task_failed",
        fence: LeaseFence | None = None,
        run_id: str | None = None,
        retry: bool = True,
    ) -> TaskRecord:
        now = self.clock.now()
        reason = self.redactor.redact(reason)

        def _work(conn: sqlite3.Connection) -> tuple[TaskRecord, bool]:
            with transaction(conn):
                task = self.repository._task(conn, task_id)
                if task is None:
                    raise TaskStateError(f"unknown task: {task_id}")
                if fence is not None:
                    assert_session_lease_row(conn, task.session_id, fence)
                if task.status != TaskStatus.IN_PROGRESS:
                    raise TaskStateError(
                        f"task {task.task_key} is {task.status.value}, not in_progress"
                    )
                attempt = self.repository._active_attempt(conn, task_id)
                if attempt is not None:
                    if attempt.id != attempt_id:
                        raise TaskStateError(
                            f"stale attempt {attempt_id}; active attempt is {attempt.id}"
                        )
                    self.repository.update_attempt(
                        conn,
                        _copy(
                            attempt,
                            status=AttemptStatus.FAILED,
                            finished_at=now,
                            error_kind=error_kind,
                            error_message=reason,
                            run_id=run_id or attempt.run_id,
                        ),
                    )
                can_retry = retry and task.attempt_count < task.max_attempts
                next_status = TaskStatus.PENDING if can_retry else TaskStatus.FAILED
                validate_task_transition(task.status, TaskStatus.FAILED)
                if can_retry:
                    validate_task_transition(TaskStatus.FAILED, TaskStatus.PENDING)
                updated = _copy(
                    task,
                    status=next_status,
                    last_error=reason,
                    started_at=None if can_retry else task.started_at,
                    finished_at=None if can_retry else now,
                    updated_at=now,
                )
                self.repository.update_task(conn, updated)
                return updated, can_retry

        task, can_retry = await self.db.run(_work)
        await self._event(
            task.session_id,
            EventType.TASK_ATTEMPT_FINISHED,
            fence=fence,
            run_id=run_id,
            payload={
                "task_id": task.id,
                "attempt_id": attempt_id,
                "status": "failed",
                "reason": reason,
            },
        )
        await self._event(
            task.session_id,
            EventType.TASK_RETRY_SCHEDULED if can_retry else EventType.TASK_FAILED,
            fence=fence,
            run_id=run_id,
            payload={
                "task_id": task.id,
                "task_key": task.task_key,
                "attempt": task.attempt_count,
                "max_attempts": task.max_attempts,
                "reason": reason,
            },
        )
        await self._audit(
            "task.retry_scheduled" if can_retry else "task.failed",
            task.session_id,
            resource=task.id,
            detail={"reason": reason, "attempt": task.attempt_count},
        )
        await self.evaluate_plan(task.session_id, fence=fence)
        return task

    async def block_task(
        self,
        task_id: str,
        *,
        reason: str,
        fence: LeaseFence | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
    ) -> TaskRecord:
        now = self.clock.now()
        reason = self.redactor.redact(reason.strip())
        if not reason:
            raise TaskStateError("blocking a task requires a reason")

        def _work(conn: sqlite3.Connection) -> TaskRecord:
            with transaction(conn):
                task = self.repository._task(conn, task_id)
                if task is None:
                    raise TaskStateError(f"unknown task: {task_id}")
                if fence is not None:
                    assert_session_lease_row(conn, task.session_id, fence)
                validate_task_transition(task.status, TaskStatus.BLOCKED)
                attempt = self.repository._active_attempt(conn, task_id)
                if attempt is not None:
                    if attempt_id is not None and attempt.id != attempt_id:
                        raise TaskStateError(
                            f"stale attempt {attempt_id}; active attempt is {attempt.id}"
                        )
                    self.repository.update_attempt(
                        conn,
                        _copy(
                            attempt,
                            status=AttemptStatus.BLOCKED,
                            finished_at=now,
                            error_kind="task_blocked",
                            error_message=reason,
                            run_id=run_id or attempt.run_id,
                        ),
                    )
                updated = _copy(
                    task,
                    status=TaskStatus.BLOCKED,
                    last_error=reason,
                    finished_at=now,
                    updated_at=now,
                )
                self.repository.update_task(conn, updated)
                return updated

        task = await self.db.run(_work)
        await self._event(
            task.session_id,
            EventType.TASK_BLOCKED,
            fence=fence,
            run_id=run_id,
            payload={"task_id": task.id, "task_key": task.task_key, "reason": reason},
        )
        await self._audit(
            "task.blocked",
            task.session_id,
            resource=task.id,
            detail={"task_key": task.task_key, "reason": reason},
        )
        await self.evaluate_plan(task.session_id, fence=fence)
        return task

    async def cancel_task(
        self,
        task_id: str,
        *,
        reason: str,
        fence: LeaseFence | None = None,
        run_id: str | None = None,
    ) -> TaskRecord:
        now = self.clock.now()
        reason = self.redactor.redact(reason.strip() or "cancelled by user")

        def _work(conn: sqlite3.Connection) -> TaskRecord:
            with transaction(conn):
                task = self.repository._task(conn, task_id)
                if task is None:
                    raise TaskStateError(f"unknown task: {task_id}")
                if fence is not None:
                    assert_session_lease_row(conn, task.session_id, fence)
                validate_task_transition(task.status, TaskStatus.CANCELLED)
                attempt = self.repository._active_attempt(conn, task_id)
                if attempt is not None:
                    self.repository.update_attempt(
                        conn,
                        _copy(
                            attempt,
                            status=AttemptStatus.CANCELLED,
                            finished_at=now,
                            error_kind="task_cancelled",
                            error_message=reason,
                            run_id=run_id or attempt.run_id,
                        ),
                    )
                updated = _copy(
                    task,
                    status=TaskStatus.CANCELLED,
                    last_error=reason,
                    finished_at=now,
                    updated_at=now,
                )
                self.repository.update_task(conn, updated)
                return updated

        task = await self.db.run(_work)
        await self._event(
            task.session_id,
            EventType.TASK_CANCELLED,
            fence=fence,
            run_id=run_id,
            payload={"task_id": task.id, "task_key": task.task_key, "reason": reason},
        )
        await self._audit(
            "task.cancelled",
            task.session_id,
            resource=task.id,
            detail={"task_key": task.task_key, "reason": reason},
        )
        await self.evaluate_plan(task.session_id, fence=fence)
        return task

    async def retry_task(
        self,
        task_id: str,
        *,
        fence: LeaseFence | None = None,
        reason: str = "manual retry",
        force: bool = False,
    ) -> TaskRecord:
        now = self.clock.now()

        def _work(conn: sqlite3.Connection) -> TaskRecord:
            with transaction(conn):
                task = self.repository._task(conn, task_id)
                if task is None:
                    raise TaskStateError(f"unknown task: {task_id}")
                if fence is not None:
                    assert_session_lease_row(conn, task.session_id, fence)
                if not task.active:
                    raise TaskStateError(
                        f"task {task.task_key} was removed by a plan revision; "
                        "re-add it with a revision before retrying"
                    )
                if task.status not in {
                    TaskStatus.FAILED,
                    TaskStatus.BLOCKED,
                    TaskStatus.CANCELLED,
                }:
                    raise TaskStateError(
                        f"task {task.task_key} is {task.status.value}; only failed, "
                        "blocked or cancelled tasks can be retried"
                    )
                attempts = self.repository._attempts(conn, task.id)
                last_attempt = attempts[-1] if attempts else None
                if (
                    last_attempt is not None
                    and last_attempt.run_id
                    and not force
                    and self._used_non_idempotent_sync(conn, task.session_id, last_attempt.run_id)
                ):
                    raise TaskStateError(
                        f"task {task.task_key} last attempt used non-idempotent tools; "
                        "inspect the result and use an explicit force retry"
                    )
                if task.attempt_count >= task.max_attempts:
                    if not force:
                        raise TaskStateError(
                            f"task {task.task_key} exhausted its {task.max_attempts} "
                            "attempts; use an explicit force retry or revise the plan"
                        )
                    if task.metadata.get("force_retry_used"):
                        raise TaskStateError(
                            f"task {task.task_key} already used its one forced retry; "
                            "revise the plan to change the work"
                        )
                    task = _copy(
                        task,
                        max_attempts=task.attempt_count + 1,
                        last_error=f"{task.last_error or ''} | manual force retry".strip(" |"),
                        metadata={**task.metadata, "force_retry_used": True},
                    )
                validate_task_transition(task.status, TaskStatus.PENDING)
                updated = _copy(
                    task,
                    status=TaskStatus.PENDING,
                    last_error=None,
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
                self.repository.update_task(conn, updated)
                plan = self.repository._plan_by_id(conn, task.plan_id)
                if plan is not None and plan.status in {
                    PlanStatus.BLOCKED,
                    PlanStatus.FAILED,
                    PlanStatus.COMPLETED,
                    PlanStatus.CANCELLED,
                }:
                    validate_plan_transition(plan.status, PlanStatus.ACTIVE)
                    self.repository.update_plan(
                        conn, _copy(plan, status=PlanStatus.ACTIVE, updated_at=now)
                    )
                return updated

        task = await self.db.run(_work)
        await self._event(
            task.session_id,
            EventType.TASK_RETRY_SCHEDULED,
            fence=fence,
            payload={"task_id": task.id, "task_key": task.task_key, "reason": reason},
        )
        return task

    async def cancel_plan(
        self,
        session_id: str,
        *,
        reason: str,
        fence: LeaseFence | None = None,
        run_id: str | None = None,
    ) -> PlanRecord | None:
        now = self.clock.now()
        reason = self.redactor.redact(reason.strip() or "cancelled by user")

        def _work(conn: sqlite3.Connection) -> PlanRecord | None:
            with transaction(conn):
                if fence is not None:
                    assert_session_lease_row(conn, session_id, fence)
                plan = self.repository._plan_for_session(conn, session_id)
                if plan is None:
                    return None
                if plan.status == PlanStatus.CANCELLED:
                    return plan
                if plan.status == PlanStatus.COMPLETED:
                    raise TaskStateError("a completed plan cannot be cancelled")
                tasks = self.repository._tasks(conn, plan.id, active_only=True)
                for task in tasks:
                    if task.status in {
                        TaskStatus.PENDING,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.FAILED,
                        TaskStatus.BLOCKED,
                    }:
                        attempt = self.repository._active_attempt(conn, task.id)
                        if attempt is not None:
                            self.repository.update_attempt(
                                conn,
                                _copy(
                                    attempt,
                                    status=AttemptStatus.CANCELLED,
                                    finished_at=now,
                                    error_kind="plan_cancelled",
                                    error_message=reason,
                                ),
                            )
                        self.repository.update_task(
                            conn,
                            _copy(
                                task,
                                status=TaskStatus.CANCELLED,
                                last_error=reason,
                                finished_at=now,
                                updated_at=now,
                            ),
                        )
                updated = _copy(plan, status=PlanStatus.CANCELLED, updated_at=now)
                self.repository.update_plan(conn, updated)
                return updated

        plan = await self.db.run(_work)
        if plan is not None:
            await self._event(
                session_id,
                EventType.TASK_PLAN_CANCELLED,
                fence=fence,
                run_id=run_id,
                payload={"plan_id": plan.id, "reason": reason},
            )
            await self._audit(
                "task.plan.cancelled",
                session_id,
                resource=plan.id,
                detail={"reason": reason},
            )
        return plan

    # --------------------------------------------------------------- recovery

    async def reconcile_after_crash(
        self,
        session_id: str,
        *,
        fence: LeaseFence,
        run_id: str | None = None,
    ) -> list[TaskRecord]:
        """Mark interrupted task attempts unknown and their tasks blocked.

        A mid-execution task may have produced side effects. It is never retried
        automatically: the user must resume or retry explicitly.
        """
        now = self.clock.now()
        reconciled: list[TaskRecord] = []

        def _work(conn: sqlite3.Connection) -> list[TaskRecord]:
            with transaction(conn):
                assert_session_lease_row(conn, session_id, fence)
                attempts = self.repository._running_attempts(conn, session_id)
                for attempt in attempts:
                    self.repository.update_attempt(
                        conn,
                        _copy(
                            attempt,
                            status=AttemptStatus.UNKNOWN,
                            finished_at=now,
                            error_kind="recovery_interrupted",
                            error_message="harness exited while the task attempt was running",
                        ),
                    )
                    task = self.repository._task(conn, attempt.task_id)
                    if task is None:
                        continue
                    if task.status == TaskStatus.IN_PROGRESS:
                        updated = _copy(
                            task,
                            status=TaskStatus.BLOCKED,
                            last_error=(
                                "task execution was interrupted by a crash; "
                                "inspect the result and retry explicitly"
                            ),
                            finished_at=now,
                            updated_at=now,
                        )
                        self.repository.update_task(conn, updated)
                        reconciled.append(updated)

                # State corruption safety net: an in-progress task without a
                # running attempt cannot make progress and must not block the
                # plan invisibly.
                plan = self.repository._plan_for_session(conn, session_id)
                if plan is not None:
                    done_ids = {task.id for task in reconciled}
                    for task in self.repository._tasks(conn, plan.id, active_only=True):
                        if task.status != TaskStatus.IN_PROGRESS or task.id in done_ids:
                            continue
                        if self.repository._active_attempt(conn, task.id) is not None:
                            continue
                        updated = _copy(
                            task,
                            status=TaskStatus.BLOCKED,
                            last_error=(
                                "task was in_progress without a running attempt; "
                                "state was repaired and requires explicit retry"
                            ),
                            finished_at=now,
                            updated_at=now,
                        )
                        self.repository.update_task(conn, updated)
                        reconciled.append(updated)
                return reconciled

        tasks = await self.db.run(_work)
        for task in tasks:
            await self._event(
                task.session_id,
                EventType.TASK_RECONCILED,
                fence=fence,
                run_id=run_id,
                payload={
                    "task_id": task.id,
                    "task_key": task.task_key,
                    "status": task.status.value,
                    "reason": task.last_error,
                },
            )
            await self._audit(
                "task.reconciled",
                task.session_id,
                resource=task.id,
                detail={"task_key": task.task_key, "reason": task.last_error},
            )
        if tasks:
            await self.evaluate_plan(session_id, fence=fence)
        return tasks

    # -------------------------------------------------------------- snapshots

    async def snapshot(self, session_id: str) -> dict[str, Any]:
        return await self.db.run(lambda conn: self.repository.snapshot(conn, session_id))

    def snapshot_sync(self, conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
        return self.repository.snapshot(conn, session_id)

    # -------------------------------------------------------------- helpers

    async def _event(
        self,
        session_id: str,
        event_type: EventType,
        *,
        fence: LeaseFence | None,
        payload: dict[str, Any],
        run_id: str | None = None,
    ) -> None:
        await self.recorder.record(
            session_id,
            event_type,
            payload=payload,
            run_id=run_id,
            fence=fence,
        )

    async def _audit(
        self,
        action: str,
        session_id: str,
        *,
        resource: str | None = None,
        detail: dict[str, Any],
    ) -> None:
        if self.audit is None or not self.audit.enabled:
            return
        try:
            await self.audit.append(
                actor="agent",
                action=action,
                resource=resource,
                detail=detail,
                session_id=session_id,
            )
        except AuditError:
            # Task bookkeeping is not a workspace side effect. The state is
            # already durable; surface the audit failure to logs rather than
            # making the caller believe the transition did not happen.
            logger.exception("task audit action failed: %s", action)
