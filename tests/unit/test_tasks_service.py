"""TaskService: state machine, dependency scheduling, revisions, fencing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bear_agent.audit.service import AuditService
from bear_agent.config.models import TasksConfig
from bear_agent.core.clock import SystemClock
from bear_agent.core.errors import (
    PlanValidationError,
    SessionLeaseError,
    TaskStateError,
)
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database
from bear_agent.tasks.models import (
    PlanDraft,
    TaskSpec,
    TaskStatus,
    validate_plan_draft,
    validate_task_transition,
)
from bear_agent.tasks.service import TaskService


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
            redactor=self.redactor,
        )

    async def new_session(self) -> tuple[str, LeaseFence]:
        session = await self.sessions.create(cwd=Path.cwd(), model="m", provider="p")
        epoch = await self.sessions.acquire_lease(session.id, "owner", 300)
        return session.id, LeaseFence("owner", epoch)

    async def draft(self, *tasks: tuple[str, list[str], int]) -> PlanDraft:
        return PlanDraft(
            goal="ship a feature",
            tasks=[
                TaskSpec(
                    key=key,
                    title=f"title {key}",
                    description=f"do {key}",
                    acceptance_criteria=f"{key} verified",
                    dependencies=deps,
                    priority=priority,
                )
                for key, deps, priority in tasks
            ],
        )

    def close(self) -> None:
        self.db.close()


def test_state_machine_rejects_illegal_transitions() -> None:
    validate_task_transition(TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
    validate_task_transition(TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED)
    validate_task_transition(TaskStatus.IN_PROGRESS, TaskStatus.FAILED)
    validate_task_transition(TaskStatus.FAILED, TaskStatus.PENDING)
    with pytest.raises(PlanValidationError):
        validate_task_transition(TaskStatus.PENDING, TaskStatus.COMPLETED)
    with pytest.raises(PlanValidationError):
        validate_task_transition(TaskStatus.IN_PROGRESS, TaskStatus.PENDING)
    with pytest.raises(PlanValidationError):
        validate_task_transition(TaskStatus.COMPLETED, TaskStatus.PENDING)
    validate_task_transition(TaskStatus.COMPLETED, TaskStatus.PENDING, allow_revision_reset=True)


def test_plan_validation_rejects_cycles_and_unknown_dependencies() -> None:
    cycle = PlanDraft(
        goal="g",
        tasks=[
            TaskSpec(key="A", title="a", description="a", dependencies=["B"]),
            TaskSpec(key="B", title="b", description="b", dependencies=["A"]),
        ],
    )
    with pytest.raises(PlanValidationError, match="cyclic"):
        validate_plan_draft(cycle, max_tasks=10)
    with pytest.raises(ValueError, match="unknown"):
        PlanDraft(
            goal="g",
            tasks=[TaskSpec(key="A", title="a", description="a", dependencies=["missing"])],
        )
    with pytest.raises(ValueError, match="duplicate"):
        PlanDraft(
            goal="g",
            tasks=[
                TaskSpec(key="A", title="a", description="a"),
                TaskSpec(key="A", title="b", description="b"),
            ],
        )


@pytest.mark.asyncio
async def test_create_claim_and_complete_in_dependency_order(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        draft = await harness.draft(("B", ["A"], 10), ("A", [], 1))
        plan = await harness.tasks.create_plan(session_id, draft, fence=fence)
        assert plan.status.value == "draft"
        todo = await harness.tasks.todo(session_id)
        assert [item.key for item in todo] == ["B", "A"]  # order_index follows draft
        ready = await harness.tasks.ready_tasks(session_id)
        assert [task.task_key for task in ready] == ["A"]

        first = await harness.tasks.claim_next(session_id, fence=fence, run_id="run-1")
        assert first is not None
        task, attempt = first
        assert task.task_key == "A"
        assert task.status == TaskStatus.IN_PROGRESS
        assert attempt.status.value == "running"

        with pytest.raises(TaskStateError):
            await harness.tasks.claim_next(session_id, fence=fence)

        await harness.tasks.request_completion(
            task.id,
            summary="A is done",
            evidence=["tests pass"],
            fence=fence,
        )
        completed = await harness.tasks.complete_task(
            task.id,
            attempt.id,
            summary="A is done",
            verification={"completed": True, "reason": "verified"},
            fence=fence,
        )
        assert completed.status == TaskStatus.COMPLETED
        second = await harness.tasks.claim_next(session_id, fence=fence, run_id="run-2")
        assert second is not None
        assert second[0].task_key == "B"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_failure_retries_until_max_then_fails(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(session_id, await harness.draft(("A", [], 1)), fence=fence)
        task, attempt = (await harness.tasks.claim_next(session_id, fence=fence)) or (None, None)
        assert task is not None and attempt is not None
        updated = await harness.tasks.fail_task(
            task.id, attempt.id, reason="tool failed", fence=fence
        )
        assert updated.status == TaskStatus.PENDING
        assert updated.attempt_count == 1

        task2, attempt2 = (await harness.tasks.claim_next(session_id, fence=fence)) or (
            None,
            None,
        )
        assert task2 is not None and attempt2 is not None
        updated = await harness.tasks.fail_task(
            task2.id, attempt2.id, reason="tool failed again", fence=fence
        )
        assert updated.status == TaskStatus.FAILED
        assert await harness.tasks.claim_next(session_id, fence=fence) is None
        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None and plan.status.value == "failed"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_block_and_manual_retry(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(session_id, await harness.draft(("A", [], 1)), fence=fence)
        task, attempt = (await harness.tasks.claim_next(session_id, fence=fence)) or (None, None)
        assert task is not None and attempt is not None
        blocked = await harness.tasks.block_task(task.id, reason="missing credentials", fence=fence)
        assert blocked.status == TaskStatus.BLOCKED
        assert await harness.tasks.claim_next(session_id, fence=fence) is None
        retried = await harness.tasks.retry_task(task.id, fence=fence)
        assert retried.status == TaskStatus.PENDING
        assert (await harness.tasks.claim_next(session_id, fence=fence)) is not None
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_plan_revision_resets_changed_completed_and_archives_removed(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(
            session_id,
            await harness.draft(("A", [], 1), ("B", ["A"], 2)),
            fence=fence,
        )
        task_a, attempt_a = (await harness.tasks.claim_next(session_id, fence=fence)) or (
            None,
            None,
        )
        assert task_a is not None and attempt_a is not None
        await harness.tasks.complete_task(
            task_a.id,
            attempt_a.id,
            summary="A done",
            verification={"completed": True},
            fence=fence,
        )

        revised_draft = PlanDraft(
            goal="ship a better feature",
            tasks=[
                TaskSpec(
                    key="A",
                    title="new title A",
                    description="do A differently",
                    acceptance_criteria="A2 verified",
                ),
                TaskSpec(key="C", title="c", description="do c"),
            ],
        )
        revised = await harness.tasks.revise_plan(
            session_id,
            revised_draft,
            reason="acceptance changed",
            fence=fence,
        )
        assert revised.version == 2
        tasks = {
            task.task_key: task
            for task in await harness.tasks.list_tasks(session_id, active_only=False)
        }
        assert tasks["A"].status == TaskStatus.PENDING
        assert tasks["B"].active is False
        assert tasks["B"].status == TaskStatus.CANCELLED
        assert tasks["C"].status == TaskStatus.PENDING
        with pytest.raises(TaskStateError, match="removed by a plan revision"):
            await harness.tasks.retry_task(tasks["B"].id, fence=fence)
        revisions = await harness.tasks.revisions(session_id)
        assert [record.version for record in revisions] == [1, 2]
        assert revisions[-1].reason == "acceptance changed"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_revision_rejected_while_task_in_progress(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(session_id, await harness.draft(("A", [], 1)), fence=fence)
        assert (await harness.tasks.claim_next(session_id, fence=fence)) is not None
        with pytest.raises(TaskStateError, match="in progress"):
            await harness.tasks.revise_plan(
                session_id,
                await harness.draft(("A", [], 1)),
                reason="nope",
                fence=fence,
            )
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_stale_fence_cannot_claim_or_transition(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(session_id, await harness.draft(("A", [], 1)), fence=fence)
        await harness.sessions.release_lease(session_id, "owner", fence.epoch)
        await harness.sessions.acquire_lease(session_id, "new-owner", 300)
        with pytest.raises(SessionLeaseError):
            await harness.tasks.claim_next(session_id, fence=fence)
        with pytest.raises(SessionLeaseError):
            await harness.tasks.revise_plan(
                session_id,
                await harness.draft(("A", [], 1)),
                reason="stale",
                fence=fence,
            )
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_retry_after_non_idempotent_tool_requires_force(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(session_id, await harness.draft(("A", [], 1)), fence=fence)
        task, attempt = (
            await harness.tasks.claim_next(session_id, fence=fence, run_id="run-side")
        ) or (None, None)
        assert task is not None and attempt is not None
        await harness.sessions.tool_calls.start(
            session_id, None, "call-shell", "bash", {"command": "touch x"}, False
        )
        await harness.recorder.record(
            session_id,
            EventType.TOOL_CALL_STARTED,
            run_id="run-side",
            payload={"tool_call_id": "call-shell", "name": "bash"},
        )
        await harness.tasks.fail_task(
            task.id, attempt.id, reason="failed after side effect", fence=fence, retry=False
        )
        with pytest.raises(TaskStateError, match="non-idempotent"):
            await harness.tasks.retry_task(task.id, fence=fence)
        retried = await harness.tasks.retry_task(task.id, fence=fence, force=True)
        assert retried.status == TaskStatus.PENDING
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_completed_plan_cannot_be_cancelled(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(session_id, await harness.draft(("A", [], 1)), fence=fence)
        task, attempt = (await harness.tasks.claim_next(session_id, fence=fence)) or (None, None)
        assert task is not None and attempt is not None
        await harness.tasks.request_completion(
            task.id, summary="done", evidence=["checked"], fence=fence
        )
        await harness.tasks.complete_task(
            task.id,
            attempt.id,
            summary="done",
            verification={"completed": True},
            fence=fence,
        )
        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None and plan.status.value == "completed"
        with pytest.raises(TaskStateError, match="completed plan"):
            await harness.tasks.cancel_plan(session_id, reason="too late", fence=fence)
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_identical_revision_does_not_reset_completed_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        spec = TaskSpec(
            key="A",
            title="title",
            description="description",
            acceptance_criteria="done",
            max_attempts=5,
        )
        await harness.tasks.create_plan(
            session_id, PlanDraft(goal="goal", tasks=[spec]), fence=fence
        )
        task, attempt = (await harness.tasks.claim_next(session_id, fence=fence)) or (None, None)
        assert task is not None and attempt is not None
        await harness.tasks.request_completion(
            task.id, summary="done", evidence=["checked"], fence=fence
        )
        await harness.tasks.complete_task(
            task.id,
            attempt.id,
            summary="done",
            verification={"completed": True},
            fence=fence,
        )
        revised = await harness.tasks.revise_plan(
            session_id,
            PlanDraft(goal="goal", tasks=[spec]),
            reason="no-op revision to verify idempotence",
            fence=fence,
        )
        assert revised.version == 2
        refreshed = await harness.tasks.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.COMPLETED
        assert refreshed.attempt_count == 1
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_revised_task_can_be_claimed_again_with_monotonic_attempt_numbers(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(session_id, await harness.draft(("A", [], 1)), fence=fence)
        task, attempt = (await harness.tasks.claim_next(session_id, fence=fence)) or (None, None)
        assert task is not None and attempt is not None
        await harness.tasks.request_completion(
            task.id, summary="done", evidence=["checked"], fence=fence
        )
        await harness.tasks.complete_task(
            task.id,
            attempt.id,
            summary="done",
            verification={"completed": True},
            fence=fence,
        )
        await harness.tasks.revise_plan(
            session_id,
            PlanDraft(
                goal="revised goal",
                tasks=[
                    TaskSpec(
                        key="A",
                        title="new title",
                        description="do A differently",
                    )
                ],
            ),
            reason="change the work",
            fence=fence,
        )
        reclaimed = await harness.tasks.claim_next(session_id, fence=fence)
        assert reclaimed is not None
        new_task, new_attempt = reclaimed
        assert new_task.attempt_count == 1
        assert new_attempt.attempt_no == 2
        attempts = await harness.tasks.attempts(new_task.id)
        assert [item.attempt_no for item in attempts] == [1, 2]
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_task_persistence_redacts_registered_secrets(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    secret = "sk-task-state-secret"
    try:
        harness.redactor.register("task-test", secret)
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(
            session_id,
            PlanDraft(
                goal=f"ship with {secret}",
                tasks=[
                    TaskSpec(
                        key="A",
                        title=f"title {secret}",
                        description=f"use {secret}",
                        acceptance_criteria=f"verify {secret}",
                    )
                ],
            ),
            fence=fence,
        )
        task, attempt = (await harness.tasks.claim_next(session_id, fence=fence)) or (None, None)
        assert task is not None and attempt is not None
        assert secret not in task.title
        assert secret not in task.description
        await harness.tasks.request_completion(
            task.id,
            summary=f"done with {secret}",
            evidence=[f"proof {secret}"],
            fence=fence,
        )
        await harness.tasks.complete_task(
            task.id,
            attempt.id,
            summary=f"done with {secret}",
            verification={"completed": True},
            fence=fence,
        )
        attempts = await harness.tasks.attempts(task.id)
        assert secret not in (attempts[-1].completion_summary or "")
        assert all(secret not in item for item in attempts[-1].evidence)
        refreshed = await harness.tasks.get_task(task.id)
        assert refreshed is not None
        assert secret not in (refreshed.result_summary or "")
        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None and secret not in plan.goal
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_concurrent_claim_only_one_wins(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    second_db = Database(tmp_path / "tasks.db")
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(
            session_id, await harness.draft(("A", [], 1), ("B", [], 2)), fence=fence
        )
        second = TaskService(
            second_db,
            EventRecorder(SessionService(second_db), EventBus(), SecretRedactor()),
            audit=AuditService(second_db),
            config=TasksConfig(max_tasks=10, max_attempts=2),
            clock=SystemClock(),
        )
        results = await asyncio.gather(
            harness.tasks.claim_next(session_id, fence=fence),
            second.claim_next(session_id, fence=fence),
            return_exceptions=True,
        )
        claimed = [value for value in results if isinstance(value, tuple)]
        errors = [value for value in results if isinstance(value, TaskStateError)]
        assert len(claimed) == 1
        assert len(errors) == 1
        in_progress = [
            task
            for task in await harness.tasks.list_tasks(session_id)
            if task.status == TaskStatus.IN_PROGRESS
        ]
        assert len(in_progress) == 1
    finally:
        second_db.close()
        harness.close()


@pytest.mark.asyncio
async def test_dependency_failure_blocks_plan_and_dependents(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(
            session_id, await harness.draft(("A", [], 1), ("B", ["A"], 2)), fence=fence
        )
        task, attempt = (await harness.tasks.claim_next(session_id, fence=fence)) or (None, None)
        assert task is not None and attempt is not None
        await harness.tasks.fail_task(task.id, attempt.id, reason="boom", fence=fence)
        second, second_attempt = (await harness.tasks.claim_next(session_id, fence=fence)) or (
            None,
            None,
        )
        assert second is not None and second_attempt is not None
        await harness.tasks.fail_task(
            second.id, second_attempt.id, reason="boom again", fence=fence
        )
        tasks = {item.task_key: item for item in await harness.tasks.list_tasks(session_id)}
        assert tasks["A"].status == TaskStatus.FAILED
        assert tasks["B"].status == TaskStatus.PENDING
        assert await harness.tasks.claim_next(session_id, fence=fence) is None
        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None and plan.status.value == "blocked"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_revision_limit_is_enforced(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(session_id, await harness.draft(("A", [], 1)), fence=fence)
        for version in range(2, 5):
            await harness.tasks.revise_plan(
                session_id,
                PlanDraft(
                    goal=f"goal v{version}",
                    tasks=[
                        TaskSpec(
                            key="A",
                            title=f"title v{version}",
                            description="do A",
                        )
                    ],
                ),
                reason=f"revision {version}",
                fence=fence,
            )
        with pytest.raises(TaskStateError, match="revision limit"):
            await harness.tasks.revise_plan(
                session_id,
                PlanDraft(
                    goal="goal v5",
                    tasks=[TaskSpec(key="A", title="title v5", description="do A")],
                ),
                reason="too many",
                fence=fence,
            )
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_interrupted_attempt_is_reconciled_to_blocked(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(session_id, await harness.draft(("A", [], 1)), fence=fence)
        task, attempt = (await harness.tasks.claim_next(session_id, fence=fence)) or (None, None)
        assert task is not None and attempt is not None
        reconciled = await harness.tasks.reconcile_after_crash(session_id, fence=fence)
        assert [item.id for item in reconciled] == [task.id]
        refreshed = await harness.tasks.get_task(task.id)
        assert refreshed is not None and refreshed.status == TaskStatus.BLOCKED
        attempts = await harness.tasks.attempts(task.id)
        assert attempts[-1].status.value == "unknown"
        assert await harness.tasks.claim_next(session_id, fence=fence) is None
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_render_context_contains_goal_todo_and_current_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.tasks.create_plan(
            session_id,
            await harness.draft(("A", [], 1), ("B", ["A"], 2)),
            fence=fence,
        )
        task, _ = (await harness.tasks.claim_next(session_id, fence=fence)) or (None, None)
        assert task is not None
        context = await harness.tasks.render_context(session_id)
        assert "ship a feature" in context
        assert "[>] A" in context
        assert "[ ] B" in context
        assert "Current task: A" in context
        prompt = await harness.tasks.render_task_prompt(session_id, task)
        assert "Call task_complete" in prompt or "call task_complete" in prompt
        snapshot = await harness.tasks.snapshot(session_id)
        assert snapshot["plan"]["goal"] == "ship a feature"
        assert len(snapshot["tasks"]) == 2
    finally:
        harness.close()
