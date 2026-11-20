"""Task engine integration: real SQLite database, fake Runtime loop.

The fake ``run_task`` callable below stands in for ``AgentRuntime``. It records
a completion request (what the ``task_complete`` tool does in a real run) and
returns a structured outcome. The real Runtime must hold one session lease
while looping ``start_next -> run -> finish``; that bootstrap wiring belongs to
the lead and is intentionally not exercised here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from bear_agent.audit.service import AuditService
from bear_agent.config.models import TasksConfig
from bear_agent.core.clock import SystemClock
from bear_agent.core.fencing import LeaseFence
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database
from bear_agent.tasks.engine import TaskDecision, TaskEngine, TaskExecution, TaskRunOutcome
from bear_agent.tasks.models import PlanDraft, TaskSpec, TaskStatus
from bear_agent.tasks.service import TaskService
from bear_agent.tasks.verifier import VerificationResult


class AllowVerifier:
    async def verify(self, *, goal, task, attempt, transcript) -> VerificationResult:
        return VerificationResult(completed=True, reason="accepted for the test")


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        max_attempts: int = 3,
        max_tasks_per_run: int = 10,
    ) -> None:
        self.db = Database(tmp_path / "engine_runtime.db")
        self.clock = SystemClock()
        self.sessions = SessionService(self.db, self.clock)
        redactor = SecretRedactor()
        self.recorder = EventRecorder(self.sessions, EventBus(), redactor)
        self.audit = AuditService(self.db, redactor=redactor, clock=self.clock)
        self.config = TasksConfig(
            max_tasks=10,
            max_attempts=max_attempts,
            max_revisions=3,
            max_tasks_per_run=max_tasks_per_run,
        )
        self.tasks = TaskService(
            self.db,
            self.recorder,
            audit=self.audit,
            config=self.config,
            clock=self.clock,
        )
        self.verifier = AllowVerifier()
        self.engine = self.new_engine()

    def new_engine(self, config: TasksConfig | None = None) -> TaskEngine:
        return TaskEngine(
            tasks=self.tasks,
            sessions=self.sessions,
            verifier=self.verifier,
            config=config or self.config,
            recorder=self.recorder,
            audit=self.audit,
            clock=self.clock,
        )

    async def new_session(self, owner: str = "owner") -> tuple[str, LeaseFence]:
        session = await self.sessions.create(cwd=Path.cwd(), model="m", provider="p")
        epoch = await self.sessions.acquire_lease(session.id, owner, 300)
        return session.id, LeaseFence(owner, epoch)

    async def create_plan(
        self, session_id: str, fence: LeaseFence, *tasks: tuple[str, list[str], int]
    ) -> None:
        draft = PlanDraft(
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
        await self.tasks.create_plan(session_id, draft, fence=fence)

    async def fake_run_task(self, execution: TaskExecution, fence: LeaseFence) -> TaskRunOutcome:
        """Stand-in for the Runtime: record task_complete, then finish."""
        await self.tasks.request_completion(
            execution.task.id,
            summary=f"{execution.task.task_key} complete",
            evidence=[f"{execution.task.task_key} verified by tests"],
            fence=fence,
            run_id="run-1",
        )
        return TaskRunOutcome(
            status="completed",
            final_text=f"{execution.task.task_key} done",
            turns=2,
        )

    async def drive(
        self, engine: TaskEngine, session_id: str, fence: LeaseFence
    ) -> list[TaskDecision]:
        decisions: list[TaskDecision] = []
        for _ in range(20):
            execution = await engine.start_next(session_id, fence=fence, run_id="run-1")
            if execution is None:
                return decisions
            outcome = await self.fake_run_task(execution, fence)
            decisions.append(await engine.finish(execution, outcome, fence=fence, run_id="run-1"))
        pytest.fail("task loop did not terminate")

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    instance = Harness(tmp_path)
    yield instance
    instance.close()


@pytest.mark.asyncio
async def test_fake_runtime_drives_dependency_chain_to_completion(
    harness: Harness,
) -> None:
    session_id, fence = await harness.new_session()
    await harness.create_plan(session_id, fence, ("A", [], 1), ("B", ["A"], 2), ("C", ["B"], 3))

    decisions = await harness.drive(harness.engine, session_id, fence)

    assert [decision.outcome for decision in decisions] == [
        "completed",
        "completed",
        "completed",
    ]
    assert [decision.task.task_key for decision in decisions] == ["A", "B", "C"]
    assert all(decision.verified for decision in decisions)

    plan = await harness.tasks.get_plan(session_id)
    assert plan is not None and plan.status.value == "completed"
    report = await harness.engine.report(session_id)
    assert report.status == "completed"
    assert report.tasks_run == 3
    assert report.tasks_completed == 3
    assert report.tasks_failed == 0
    assert report.issues == []


@pytest.mark.asyncio
async def test_restart_reconciles_interrupted_task_then_continues(
    harness: Harness,
) -> None:
    session_id, fence = await harness.new_session()
    await harness.create_plan(session_id, fence, ("A", [], 1), ("B", ["A"], 2))

    crashed = await harness.engine.start_next(session_id, fence=fence, run_id="run-1")
    assert crashed is not None
    # Simulate process death: the claim is durable but no outcome is applied.
    await harness.sessions.release_lease(session_id, fence.owner, fence.epoch)
    epoch = await harness.sessions.acquire_lease(session_id, "process-2", 300)
    fence2 = LeaseFence("process-2", epoch)
    restarted = harness.new_engine()

    reconciled = await restarted.reconcile(session_id, fence=fence2, run_id="run-2")
    assert [task.task_key for task in reconciled] == ["A"]
    assert reconciled[0].status == TaskStatus.BLOCKED

    await harness.tasks.retry_task(crashed.task.id, fence=fence2, reason="resume")
    decisions = await harness.drive(restarted, session_id, fence2)

    assert [decision.task.task_key for decision in decisions] == ["A", "B"]
    assert all(decision.outcome == "completed" for decision in decisions)
    plan = await harness.tasks.get_plan(session_id)
    assert plan is not None and plan.status.value == "completed"
    attempts = await harness.tasks.attempts(crashed.task.id)
    assert attempts[-1].status.value == "completed"
    assert attempts[-1].attempt_no == 2


@pytest.mark.asyncio
async def test_failed_runs_retry_then_report_failure(harness: Harness) -> None:
    session_id, fence = await harness.new_session()
    await harness.create_plan(session_id, fence, ("A", [], 1))

    decisions: list[TaskDecision] = []
    for _ in range(5):
        execution = await harness.engine.start_next(session_id, fence=fence)
        if execution is None:
            break
        decisions.append(
            await harness.engine.finish(
                execution,
                TaskRunOutcome(status="failed", error="tool exploded", turns=1),
                fence=fence,
            )
        )
    assert [decision.outcome for decision in decisions] == [
        "retry_scheduled",
        "retry_scheduled",
        "failed",
    ]

    report = await harness.engine.report(session_id, reason="max attempts reached")
    assert report.status == "failed"
    assert report.tasks_run == 1
    assert report.tasks_failed == 1
    assert any("tool exploded" in issue for issue in report.issues)

    attempts = await harness.tasks.attempts(decisions[-1].task.id)
    assert [attempt.status.value for attempt in attempts] == [
        "failed",
        "failed",
        "failed",
    ]
    assert all(attempt.error_kind == "run_failed" for attempt in attempts)


@pytest.mark.asyncio
async def test_budget_is_enforced_per_engine_instance(harness: Harness) -> None:
    session_id, fence = await harness.new_session()
    await harness.create_plan(session_id, fence, ("A", [], 1), ("B", [], 2), ("C", [], 3))
    budgeted = harness.new_engine(
        TasksConfig(
            max_tasks=10,
            max_attempts=3,
            max_revisions=3,
            max_tasks_per_run=2,
        )
    )

    first = await harness.drive(budgeted, session_id, fence)
    assert [decision.task.task_key for decision in first] == ["C", "B"]
    assert "budget" in budgeted.last_stop_reason
    report = await budgeted.report(session_id)
    assert report.status == "active"
    assert report.tasks_run == 2
    assert report.tasks_completed == 2

    # A fresh engine instance gets a fresh in-memory budget and resumes the plan.
    resumed = harness.new_engine()
    second = await harness.drive(resumed, session_id, fence)
    assert [decision.task.task_key for decision in second] == ["A"]
    final = await resumed.report(session_id)
    assert final.status == "completed"
    assert final.tasks_completed == 3
