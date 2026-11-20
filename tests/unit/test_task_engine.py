"""TaskEngine: scheduling, run outcomes, verification policy and budgets.

These tests use the real SQLite-backed ``TaskService`` and a scripted verifier
so the engine's decisions are checked against persisted state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_agent.audit.service import AuditService
from bear_agent.config.models import TasksConfig
from bear_agent.core.clock import SystemClock
from bear_agent.core.errors import SessionLeaseError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.messages import AssistantMessage
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database
from bear_agent.tasks.engine import TaskEngine, TaskExecution, TaskRunOutcome
from bear_agent.tasks.models import (
    AttemptStatus,
    PlanDraft,
    TaskSpec,
    TaskStatus,
    VerificationMode,
)
from bear_agent.tasks.service import TaskService
from bear_agent.tasks.verifier import VerificationResult


class ScriptedVerifier:
    """Fake ``TaskVerifier``: returns queued results or raises a scripted error."""

    def __init__(
        self,
        results: list[VerificationResult] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.results = list(results or [])
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def verify(
        self,
        *,
        goal: str,
        task: object,
        attempt: object,
        transcript: list[object],
    ) -> VerificationResult:
        self.calls.append(
            {
                "goal": goal,
                "task": task,
                "attempt": attempt,
                "transcript": list(transcript),
            }
        )
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.pop(0)
        return VerificationResult(completed=True, reason="goal satisfied")


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        max_attempts: int = 2,
        max_tasks_per_run: int = 10,
        verify_completion: bool = True,
        verifier: ScriptedVerifier | None = None,
    ) -> None:
        self.db = Database(tmp_path / "engine.db")
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
            verify_completion=verify_completion,
        )
        self.tasks = TaskService(
            self.db,
            self.recorder,
            audit=self.audit,
            config=self.config,
            clock=self.clock,
        )
        self.verifier = verifier or ScriptedVerifier()
        self.engine = TaskEngine(
            tasks=self.tasks,
            sessions=self.sessions,
            verifier=self.verifier,
            config=self.config,
            recorder=self.recorder,
            audit=self.audit,
            clock=self.clock,
        )

    async def new_session(self) -> tuple[str, LeaseFence]:
        session = await self.sessions.create(cwd=Path.cwd(), model="m", provider="p")
        epoch = await self.sessions.acquire_lease(session.id, "owner", 300)
        return session.id, LeaseFence("owner", epoch)

    def draft(self, *tasks: tuple[str, list[str], int]) -> PlanDraft:
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

    async def create_plan(
        self, session_id: str, fence: LeaseFence, *tasks: tuple[str, list[str], int]
    ) -> None:
        await self.tasks.create_plan(session_id, self.draft(*tasks), fence=fence)

    async def request_completion(
        self,
        execution: TaskExecution,
        fence: LeaseFence,
        *,
        summary: str | None = None,
        evidence: list[str] | None = None,
    ) -> None:
        await self.tasks.request_completion(
            execution.task.id,
            summary=summary or f"{execution.task.task_key} done",
            evidence=list(evidence or []),
            fence=fence,
        )

    async def finish_completed(
        self,
        execution: TaskExecution,
        fence: LeaseFence,
        *,
        summary: str | None = None,
        evidence: list[str] | None = None,
        request: bool = True,
    ):
        if request:
            await self.request_completion(execution, fence, summary=summary, evidence=evidence)
        return await self.engine.finish(
            execution,
            TaskRunOutcome(status="completed", final_text="done", turns=2),
            fence=fence,
        )

    def close(self) -> None:
        self.db.close()


@pytest.mark.asyncio
async def test_start_next_picks_ready_task_and_renders_prompt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1), ("B", ["A"], 2))

        execution = await harness.engine.start_next(session_id, fence=fence)
        assert execution is not None
        assert execution.task.task_key == "A"
        assert execution.task.status == TaskStatus.IN_PROGRESS
        assert execution.attempt.attempt_no == 1
        assert execution.attempt.status == AttemptStatus.RUNNING
        assert "ship a feature" in execution.prompt
        assert "Task A: title A" in execution.prompt
        assert "Acceptance criteria: A verified" in execution.prompt
        assert "Todo:" in execution.prompt
        assert "[>] A title A" in execution.prompt
        assert "[ ] B title B" in execution.prompt
        assert harness.engine.last_stop_reason == ""
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_completed_without_completion_request_retries_then_fails(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, max_attempts=2)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1))

        first = await harness.engine.start_next(session_id, fence=fence)
        assert first is not None
        decision = await harness.finish_completed(first, fence, request=False)
        assert decision.outcome == "retry_scheduled"
        assert "task_complete" in decision.reason
        assert decision.task.status == TaskStatus.PENDING

        second = await harness.engine.start_next(session_id, fence=fence)
        assert second is not None
        decision = await harness.finish_completed(second, fence, request=False)
        assert decision.outcome == "failed"
        assert decision.task.status == TaskStatus.FAILED
        assert "task_complete" in decision.reason

        assert await harness.engine.start_next(session_id, fence=fence) is None
        assert "plan failed" in harness.engine.last_stop_reason
        assert harness.verifier.calls == []
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_successful_verification_completes_and_advances_plan(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1), ("B", ["A"], 2))

        execution = await harness.engine.start_next(session_id, fence=fence)
        assert execution is not None
        decision = await harness.finish_completed(
            execution, fence, summary="A is implemented", evidence=["pytest passed"]
        )

        assert decision.outcome == "completed"
        assert decision.verified is True
        assert decision.task.status == TaskStatus.COMPLETED
        assert decision.task.result_summary == "A is implemented"
        assert len(harness.verifier.calls) == 1
        assert harness.verifier.calls[0]["goal"] == "ship a feature"

        attempts = await harness.tasks.attempts(execution.task.id)
        assert attempts[-1].status == AttemptStatus.COMPLETED
        assert attempts[-1].completion_summary == "A is implemented"
        assert attempts[-1].verification["completed"] is True

        events = await harness.sessions.events_for(session_id)
        verification_events = [
            event for event in events if event.type == EventType.TASK_VERIFICATION
        ]
        assert len(verification_events) == 1
        assert verification_events[0].payload["completed"] is True
        assert verification_events[0].payload["source"] == "model"
        assert verification_events[0].payload["task_key"] == "A"
        assert any(entry.action == "task.verification" for entry in await harness.audit.tail())

        following = await harness.engine.start_next(session_id, fence=fence)
        assert following is not None
        assert following.task.task_key == "B"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_verifier_failure_retries_then_fails(tmp_path: Path) -> None:
    verifier = ScriptedVerifier(
        [
            VerificationResult(completed=False, reason="missing tests", unmet=["tests pass"]),
            VerificationResult(completed=False, reason="still missing", unmet=["tests pass"]),
        ]
    )
    harness = Harness(tmp_path, max_attempts=2, verifier=verifier)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1))

        first = await harness.engine.start_next(session_id, fence=fence)
        assert first is not None
        decision = await harness.finish_completed(first, fence, evidence=["implementation claimed"])
        assert decision.outcome == "retry_scheduled"
        assert "missing tests" in decision.reason
        assert "unmet: tests pass" in decision.reason
        assert decision.verified is False
        attempts = await harness.tasks.attempts(first.task.id)
        assert attempts[-1].status == AttemptStatus.FAILED
        assert attempts[-1].error_kind == "verification_failed"

        second = await harness.engine.start_next(session_id, fence=fence)
        assert second is not None
        decision = await harness.finish_completed(
            second, fence, evidence=["implementation claimed"]
        )
        assert decision.outcome == "failed"
        assert "still missing" in decision.reason
        assert decision.task.status == TaskStatus.FAILED

        assert await harness.engine.start_next(session_id, fence=fence) is None
        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None and plan.status.value == "failed"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_verifier_exception_blocks_without_burning_retries(
    tmp_path: Path,
) -> None:
    verifier = ScriptedVerifier(error=RuntimeError("verifier offline"))
    harness = Harness(tmp_path, max_attempts=3, verifier=verifier)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1))

        execution = await harness.engine.start_next(session_id, fence=fence)
        assert execution is not None
        decision = await harness.finish_completed(execution, fence, evidence=["looks good"])

        assert decision.outcome == "blocked"
        assert "verification unavailable" in decision.reason
        assert "verifier offline" in decision.reason
        assert decision.verified is False
        refreshed = await harness.tasks.get_task(execution.task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.BLOCKED
        assert refreshed.attempt_count == 1
        attempts = await harness.tasks.attempts(execution.task.id)
        assert attempts[-1].status == AttemptStatus.BLOCKED

        assert await harness.engine.start_next(session_id, fence=fence) is None
        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None and plan.status.value == "blocked"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_evidence_mode_requires_evidence(tmp_path: Path) -> None:
    harness = Harness(tmp_path, max_attempts=2)
    try:
        session_id, fence = await harness.new_session()
        draft = PlanDraft(
            goal="ship a feature",
            tasks=[
                TaskSpec(
                    key="A",
                    title="title A",
                    description="do A",
                    acceptance_criteria="A verified",
                    verification=VerificationMode.EVIDENCE,
                )
            ],
        )
        await harness.tasks.create_plan(session_id, draft, fence=fence)

        first = await harness.engine.start_next(session_id, fence=fence)
        assert first is not None
        decision = await harness.finish_completed(first, fence, evidence=[])
        assert decision.outcome == "retry_scheduled"
        assert "evidence" in decision.reason.lower()
        assert harness.verifier.calls == []

        second = await harness.engine.start_next(session_id, fence=fence)
        assert second is not None
        decision = await harness.finish_completed(second, fence, evidence=["pytest -q: 12 passed"])
        assert decision.outcome == "completed"
        assert decision.verified is True
        assert harness.verifier.calls == []
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_disabled_model_verification_falls_back_to_evidence_gate(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, max_attempts=1, verify_completion=False)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1))

        execution = await harness.engine.start_next(session_id, fence=fence)
        assert execution is not None
        decision = await harness.finish_completed(execution, fence, evidence=[])

        assert decision.outcome == "failed"
        assert "evidence" in decision.reason.lower()
        assert harness.verifier.calls == []
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_block_tool_transition_before_finish_is_reported_without_repeat(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1))

        execution = await harness.engine.start_next(session_id, fence=fence)
        assert execution is not None
        blocked = await harness.tasks.block_task(
            execution.task.id, reason="missing credentials", fence=fence
        )
        assert blocked.status == TaskStatus.BLOCKED

        decision = await harness.engine.finish(
            execution,
            TaskRunOutcome(status="completed", final_text="done"),
            fence=fence,
        )
        assert decision.outcome == "blocked"
        assert "missing credentials" in decision.reason
        assert decision.task.status == TaskStatus.BLOCKED
        refreshed = await harness.tasks.get_task(execution.task.id)
        assert refreshed is not None
        assert refreshed.attempt_count == 1
        attempts = await harness.tasks.attempts(execution.task.id)
        assert attempts[-1].status == AttemptStatus.BLOCKED
        assert harness.verifier.calls == []
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_cancelled_outcome_cancels_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1))

        execution = await harness.engine.start_next(session_id, fence=fence)
        assert execution is not None
        decision = await harness.engine.finish(
            execution,
            TaskRunOutcome(status="cancelled", error="user pressed stop"),
            fence=fence,
        )
        assert decision.outcome == "cancelled"
        assert decision.reason == "user pressed stop"
        assert decision.task.status == TaskStatus.CANCELLED
        attempts = await harness.tasks.attempts(execution.task.id)
        assert attempts[-1].status == AttemptStatus.CANCELLED
        plan = await harness.tasks.get_plan(session_id)
        assert plan is not None and plan.status.value == "cancelled"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_unknown_run_status_fails_safely(tmp_path: Path) -> None:
    harness = Harness(tmp_path, max_attempts=1)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1))

        execution = await harness.engine.start_next(session_id, fence=fence)
        assert execution is not None
        decision = await harness.engine.finish(
            execution, TaskRunOutcome(status="hologram"), fence=fence
        )
        assert decision.outcome == "failed"
        assert "unexpected run status: hologram" in decision.reason
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_max_tasks_per_run_stops_scheduling(tmp_path: Path) -> None:
    harness = Harness(tmp_path, max_tasks_per_run=1)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1), ("B", [], 2))

        execution = await harness.engine.start_next(session_id, fence=fence)
        assert execution is not None
        decision = await harness.finish_completed(execution, fence)
        assert decision.outcome == "completed"

        assert await harness.engine.start_next(session_id, fence=fence) is None
        assert "budget" in harness.engine.last_stop_reason
        report = await harness.engine.report(session_id)
        assert report.tasks_run == 1
        assert report.tasks_completed == 1
        assert report.tasks_failed == 0

        # A Runtime that reuses the engine starts a fresh budget window.
        harness.engine.reset_budget()
        following = await harness.engine.start_next(session_id, fence=fence)
        assert following is not None
        assert following.task.task_key == "A"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_report_counts_and_revisions(tmp_path: Path) -> None:
    verifier = ScriptedVerifier(
        [
            VerificationResult(completed=True, reason="A ok"),
            VerificationResult(completed=False, reason="B broke", unmet=["B verified"]),
        ]
    )
    harness = Harness(tmp_path, max_attempts=1, verifier=verifier)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1), ("B", ["A"], 2))

        first = await harness.engine.start_next(session_id, fence=fence)
        assert first is not None
        decision = await harness.finish_completed(first, fence, evidence=["A evidence"])
        assert decision.outcome == "completed"

        revised = await harness.tasks.revise_plan(
            session_id,
            harness.draft(("A", [], 1), ("B", ["A"], 2)),
            reason="refresh",
            fence=fence,
        )
        assert revised.version == 2

        second = await harness.engine.start_next(session_id, fence=fence)
        assert second is not None
        decision = await harness.finish_completed(second, fence, evidence=["B evidence"])
        assert decision.outcome == "failed"

        report = await harness.engine.report(session_id, reason="plan run finished")
        assert report.plan_id is not None
        assert report.status == "failed"
        assert report.tasks_run == 2
        assert report.tasks_completed == 1
        assert report.tasks_failed == 1
        assert report.tasks_blocked == 0
        assert report.revisions == 1
        assert report.reason == "plan run finished"
        assert any(issue.startswith("B:") for issue in report.issues)
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_stale_fence_while_finishing_raises_and_writes_nothing(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1))

        execution = await harness.engine.start_next(session_id, fence=fence)
        assert execution is not None
        await harness.request_completion(execution, fence, evidence=["done"])
        events_before = await harness.sessions.events_for(session_id)

        await harness.sessions.release_lease(session_id, fence.owner, fence.epoch)
        epoch = await harness.sessions.acquire_lease(session_id, "new-owner", 300)
        assert epoch == fence.epoch + 1

        with pytest.raises(SessionLeaseError):
            await harness.engine.finish(
                execution,
                TaskRunOutcome(status="completed", final_text="done"),
                fence=fence,
            )

        task = await harness.tasks.get_task(execution.task.id)
        assert task is not None
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.result_summary is None
        attempts = await harness.tasks.attempts(execution.task.id)
        assert attempts[-1].status == AttemptStatus.RUNNING
        assert attempts[-1].completion_summary == "A done"
        events_after = await harness.sessions.events_for(session_id)
        assert len(events_after) == len(events_before)
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_verifier_transcript_is_scoped_to_the_current_attempt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1), ("B", ["A"], 2))
        execution_a = await harness.engine.start_next(session_id, fence=fence, run_id="run-a")
        assert execution_a is not None
        await harness.tasks.request_completion(
            execution_a.task.id, summary="A done", evidence=["A proof"], fence=fence
        )
        await harness.engine.finish(
            execution_a,
            TaskRunOutcome(status="completed"),
            fence=fence,
            run_id="run-a",
        )
        # Messages produced by earlier tasks must not be visible as evidence.
        await harness.sessions.append_user_message(session_id, "old task A evidence")
        await harness.sessions.append_assistant_message(
            session_id, AssistantMessage.of("A tests passed")
        )

        execution_b = await harness.engine.start_next(session_id, fence=fence, run_id="run-b")
        assert execution_b is not None
        await harness.sessions.append_user_message(session_id, "B task prompt")
        await harness.tasks.request_completion(
            execution_b.task.id, summary="B done", evidence=["B proof"], fence=fence
        )
        await harness.engine.finish(
            execution_b,
            TaskRunOutcome(status="completed"),
            fence=fence,
            run_id="run-b",
        )

        transcript = harness.verifier.calls[-1]["transcript"]
        rendered = "\n".join(
            message.text_content()  # type: ignore[attr-defined]
            for message in transcript  # type: ignore[union-attr]
        )
        assert "B task prompt" in rendered
        assert "old task A evidence" not in rendered
        assert "A tests passed" not in rendered
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_failure_after_non_idempotent_tool_blocks_instead_of_retry(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        session_id, fence = await harness.new_session()
        await harness.create_plan(session_id, fence, ("A", [], 1))
        execution = await harness.engine.start_next(
            session_id, fence=fence, run_id="run-side-effect"
        )
        assert execution is not None
        await harness.sessions.tool_calls.start(
            session_id, None, "call-shell", "bash", {"command": "rm -rf build"}, False
        )
        await harness.recorder.record(
            session_id,
            EventType.TOOL_CALL_STARTED,
            run_id="run-side-effect",
            payload={"tool_call_id": "call-shell", "name": "bash"},
        )

        decision = await harness.engine.finish(
            execution,
            TaskRunOutcome(status="failed", error="tool failed"),
            fence=fence,
            run_id="run-side-effect",
        )

        assert decision.outcome == "blocked"
        assert "not retried automatically" in decision.reason
        task = await harness.tasks.get_task(execution.task.id)
        assert task is not None
        assert task.status == TaskStatus.BLOCKED
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_cancel_and_reconcile_delegate(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        cancelled_session, cancelled_fence = await harness.new_session()
        await harness.create_plan(cancelled_session, cancelled_fence, ("A", [], 1))
        plan = await harness.engine.cancel(
            cancelled_session, fence=cancelled_fence, reason="stop the plan"
        )
        assert plan is not None and plan.status.value == "cancelled"
        cancelled_tasks = await harness.tasks.list_tasks(cancelled_session)
        assert cancelled_tasks[0].status == TaskStatus.CANCELLED

        recovered_session, recovered_fence = await harness.new_session()
        await harness.create_plan(recovered_session, recovered_fence, ("A", [], 1))
        execution = await harness.engine.start_next(recovered_session, fence=recovered_fence)
        assert execution is not None
        reconciled = await harness.engine.reconcile(recovered_session, fence=recovered_fence)
        assert [task.id for task in reconciled] == [execution.task.id]
        assert reconciled[0].status == TaskStatus.BLOCKED
        attempts = await harness.tasks.attempts(execution.task.id)
        assert attempts[-1].status == AttemptStatus.UNKNOWN
    finally:
        harness.close()
