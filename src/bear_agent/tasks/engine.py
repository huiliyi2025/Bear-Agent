"""Task execution engine.

The engine is the program-side driver for one Plan: it claims the next runnable
task, hands the Runtime a prompt, then interprets the run outcome and the
completion request. It owns *decisions*, never model calls and never the turn
loop: the Runtime executes a task run and calls ``finish`` with the structured
outcome.

State transitions, retry limits, dependency checks and revisions stay in
``TaskService``. This module only applies policy (which verifier to use, when a
task is blocked instead of retried) and records the verification decision.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from bear_agent.audit.service import AuditService
from bear_agent.config.models import TasksConfig
from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.errors import AuditError, TaskStateError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.messages import Message
from bear_agent.observability.recorder import EventRecorder
from bear_agent.sessions.service import SessionService
from bear_agent.tasks.models import (
    AttemptStatus,
    PlanRecord,
    TaskAttemptRecord,
    TaskRecord,
    TaskRunReport,
    TaskStatus,
    VerificationMode,
)
from bear_agent.tasks.service import TaskService
from bear_agent.tasks.verifier import EvidenceVerifier, TaskVerifier, VerificationResult

logger = logging.getLogger(__name__)

# Run statuses that describe a non-terminal execution failure and are worth
# another attempt (subject to TaskService's max_attempts).
RETRYABLE_RUN_STATUSES = frozenset({"failed", "max_turns", "context_overflow", "no_progress"})

# A verifier that cannot decide must not burn retries forever. Both raised
# exceptions and this reason prefix are treated as "verification unavailable".
UNAVAILABLE_PREFIX = "verification unavailable"

# Verification only sees a bounded tail of the transcript; the whole session
# may contain several prior tasks and is needlessly expensive to pass along.
TRANSCRIPT_TAIL = 40


@dataclass(frozen=True)
class TaskExecution:
    """One claimed task attempt plus the prompt that starts its run."""

    task: TaskRecord
    attempt: TaskAttemptRecord
    prompt: str


@dataclass(frozen=True)
class TaskRunOutcome:
    """Structured result of the Runtime run that executed a task.

    ``status`` uses the Runtime ``RunStatus`` values. Unknown values are
    accepted and fail the task safely instead of crashing the engine.
    """

    status: str
    final_text: str = ""
    error: str | None = None
    turns: int = 0


@dataclass(frozen=True)
class TaskDecision:
    """What the engine did after a task run.

    ``outcome`` is one of ``completed``, ``failed``, ``blocked``,
    ``cancelled``, ``retry_scheduled`` or ``noop``. ``verified`` is true only
    when this decision was reached through a passing verification.
    """

    task: TaskRecord
    outcome: str
    reason: str
    verified: bool = False


class TaskEngine:
    def __init__(
        self,
        *,
        tasks: TaskService,
        sessions: SessionService,
        verifier: TaskVerifier,
        config: TasksConfig,
        recorder: EventRecorder,
        audit: AuditService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.tasks = tasks
        self.sessions = sessions
        self.verifier = verifier
        self.config = config
        self.recorder = recorder
        self.audit = audit
        self.clock = clock or SystemClock()
        self.last_stop_reason = ""
        self._tasks_started = 0

    # ------------------------------------------------------------- scheduling

    async def start_next(
        self,
        session_id: str,
        *,
        fence: LeaseFence,
        run_id: str | None = None,
    ) -> TaskExecution | None:
        """Claim the next runnable task, or return None when the plan is done.

        The per-engine budget is checked before claiming so an exhausted budget
        never leaves a task in ``in_progress``. ``last_stop_reason`` explains
        why None was returned.
        """
        limit = self.config.max_tasks_per_run
        if self._tasks_started >= limit:
            self.last_stop_reason = f"task run budget exhausted ({limit} per run)"
            return None

        claimed = await self.tasks.claim_next(session_id, fence=fence, run_id=run_id)
        if claimed is None:
            plan = await self.tasks.evaluate_plan(session_id, fence=fence)
            if plan is None:
                self.last_stop_reason = "session has no plan"
            else:
                self.last_stop_reason = f"no runnable task (plan {plan.status.value})"
            return None

        task, attempt = claimed
        prompt = await self.tasks.render_task_prompt(session_id, task)
        self._tasks_started += 1
        self.last_stop_reason = ""
        return TaskExecution(task=task, attempt=attempt, prompt=prompt)

    def reset_budget(self) -> None:
        """Start a new plan-run budget window.

        The counter is deliberately in memory. A Runtime that reuses one
        engine across plan runs (the bootstrap container does) must call this
        at the start of each ``run_plan`` so the previous run's claims do not
        consume the new run's budget.
        """
        self._tasks_started = 0
        self.last_stop_reason = ""

    # -------------------------------------------------------------- finishing

    async def finish(
        self,
        execution: TaskExecution,
        outcome: TaskRunOutcome,
        *,
        fence: LeaseFence,
        run_id: str | None = None,
    ) -> TaskDecision:
        """Apply the outcome of a task run and always return a decision.

        A tool may already have transitioned the task (``task_block`` /
        ``task_cancel``). In that case the engine reports the existing state
        without a second transition, so a stale run cannot overwrite it.
        """
        task = await self.tasks.get_task(execution.task.id)
        if task is None:
            raise TaskStateError(f"unknown task: {execution.task.id}")
        if task.status != TaskStatus.IN_PROGRESS:
            return _existing_state_decision(task)

        status = str(outcome.status).strip().lower()

        if status == "cancelled":
            reason = (outcome.error or "").strip() or "task run was cancelled"
            cancelled = await self.tasks.cancel_task(
                task.id, reason=reason, fence=fence, run_id=run_id
            )
            return TaskDecision(task=cancelled, outcome="cancelled", reason=reason)

        if status in RETRYABLE_RUN_STATUSES:
            reason = _run_failure_reason(outcome)
            return await self._fail_or_block(
                task,
                execution.attempt,
                reason=reason,
                error_kind=f"run_{status}",
                fence=fence,
                run_id=run_id,
            )

        if status != "completed":
            reason = f"unexpected run status: {status or '(empty)'}"
            return await self._fail_or_block(
                task,
                execution.attempt,
                reason=reason,
                error_kind="unexpected_run_status",
                fence=fence,
                run_id=run_id,
            )

        attempt = await self._attempt_for(execution)
        if attempt.completion_summary is None:
            reason = "agent finished without calling task_complete"
            return await self._fail_or_block(
                task,
                attempt,
                reason=reason,
                error_kind="completion_missing",
                fence=fence,
                run_id=run_id,
            )

        return await self._verify_and_decide(task=task, attempt=attempt, fence=fence, run_id=run_id)

    async def _attempt_for(self, execution: TaskExecution) -> TaskAttemptRecord:
        attempts = await self.tasks.attempts(execution.task.id)
        for item in reversed(attempts):
            if item.id == execution.attempt.id and item.status == AttemptStatus.RUNNING:
                return item
        active = next(
            (item for item in reversed(attempts) if item.status == AttemptStatus.RUNNING),
            None,
        )
        if active is not None:
            return active
        for item in attempts:
            if item.id == execution.attempt.id:
                return item
        raise TaskStateError(f"task {execution.task.task_key} has no active attempt to finish")

    def _verifier_for(self, task: TaskRecord) -> TaskVerifier:
        if task.verification == VerificationMode.EVIDENCE or not self.config.verify_completion:
            return EvidenceVerifier()
        return self.verifier

    async def _verify_and_decide(
        self,
        *,
        task: TaskRecord,
        attempt: TaskAttemptRecord,
        fence: LeaseFence,
        run_id: str | None,
    ) -> TaskDecision:
        session_id = task.session_id
        plan = await self.tasks.get_plan(session_id)
        goal = plan.goal if plan is not None else ""
        messages = await self.sessions.load_messages(session_id)
        start_seq = attempt.metadata.get("start_message_seq")
        if isinstance(start_seq, int):
            # Verify only this attempt's work. The task prompt and its tool
            # results start at this transcript position; earlier tasks' output
            # must not be accepted as evidence for the current task.
            messages = [
                message for message in messages if message.seq is None or message.seq >= start_seq
            ]
        transcript: Sequence[Message] = messages[-TRANSCRIPT_TAIL:]
        verifier = self._verifier_for(task)

        try:
            result = await verifier.verify(
                goal=goal,
                task=task,
                attempt=attempt,
                transcript=transcript,
            )
        except Exception as exc:
            reason = f"{UNAVAILABLE_PREFIX}: {type(exc).__name__}: {exc}"
            blocked = await self.tasks.block_task(
                task.id,
                reason=reason,
                fence=fence,
                run_id=run_id,
                attempt_id=attempt.id,
            )
            await self._record_verification(
                session_id,
                task,
                attempt,
                completed=False,
                reason=reason,
                source="unavailable",
                unmet=[],
                fence=fence,
                run_id=run_id,
            )
            return TaskDecision(task=blocked, outcome="blocked", reason=reason)

        if not result.completed and _verification_unavailable(result):
            reason = result.reason.strip() or UNAVAILABLE_PREFIX
            blocked = await self.tasks.block_task(
                task.id,
                reason=reason,
                fence=fence,
                run_id=run_id,
                attempt_id=attempt.id,
            )
            await self._record_verification(
                session_id,
                task,
                attempt,
                completed=False,
                reason=reason,
                source=result.source,
                unmet=result.unmet,
                fence=fence,
                run_id=run_id,
            )
            return TaskDecision(task=blocked, outcome="blocked", reason=reason)

        if result.completed:
            summary = (attempt.completion_summary or result.reason or "task completed").strip()
            completed = await self.tasks.complete_task(
                task.id,
                attempt.id,
                summary=summary,
                verification=asdict(result),
                fence=fence,
                run_id=run_id,
            )
            await self._record_verification(
                session_id,
                completed,
                attempt,
                completed=True,
                reason=result.reason,
                source=result.source,
                unmet=result.unmet,
                fence=fence,
                run_id=run_id,
            )
            return TaskDecision(
                task=completed,
                outcome="completed",
                reason=result.reason,
                verified=True,
            )

        reason = _verification_failure_reason(result)
        decision = await self._fail_or_block(
            task,
            attempt,
            reason=reason,
            error_kind="verification_failed",
            fence=fence,
            run_id=run_id,
        )
        await self._record_verification(
            session_id,
            task,
            attempt,
            completed=False,
            reason=reason,
            source=result.source,
            unmet=result.unmet,
            fence=fence,
            run_id=run_id,
        )
        return decision

    async def _fail_or_block(
        self,
        task: TaskRecord,
        attempt: TaskAttemptRecord,
        *,
        reason: str,
        error_kind: str,
        fence: LeaseFence,
        run_id: str | None,
    ) -> TaskDecision:
        """Retry only when repeating the task cannot repeat a side effect."""
        effective_run = run_id or attempt.run_id
        if await self.tasks.attempt_used_non_idempotent_tool(task.session_id, effective_run):
            blocked_reason = (
                reason + "; non-idempotent tools already ran, so the task was not "
                "retried automatically. Inspect the result and retry explicitly."
            )
            blocked = await self.tasks.block_task(
                task.id,
                reason=blocked_reason,
                fence=fence,
                run_id=run_id,
                attempt_id=attempt.id,
            )
            return TaskDecision(task=blocked, outcome="blocked", reason=blocked_reason)
        failed = await self.tasks.fail_task(
            task.id,
            attempt.id,
            reason=reason,
            error_kind=error_kind,
            fence=fence,
            run_id=run_id,
            retry=True,
        )
        return _failure_decision(failed, reason)

    # ------------------------------------------------------------ lifecycle

    async def cancel(self, session_id: str, *, fence: LeaseFence, reason: str) -> PlanRecord | None:
        """Cancel the whole plan and every unfinished task."""
        return await self.tasks.cancel_plan(session_id, reason=reason, fence=fence)

    async def reconcile(
        self,
        session_id: str,
        *,
        fence: LeaseFence,
        run_id: str | None = None,
    ) -> list[TaskRecord]:
        """Repair task state after a crash before a new plan run starts."""
        return await self.tasks.reconcile_after_crash(session_id, fence=fence, run_id=run_id)

    async def report(
        self,
        session_id: str,
        *,
        reason: str = "",
        issues: list[str] | None = None,
    ) -> TaskRunReport:
        """Summarize plan execution for the caller/UI.

        ``revisions`` counts applied revisions; the initial plan snapshot
        (version 1) is not a revision. When ``issues`` is omitted, failed and
        blocked tasks contribute their last error.
        """
        plan = await self.tasks.get_plan(session_id)
        if plan is None:
            return TaskRunReport(
                session_id=session_id,
                status="none",
                reason=reason,
                issues=list(issues or []),
            )
        tasks = await self.tasks.list_tasks(session_id, active_only=True)
        revisions = await self.tasks.revisions(session_id)
        if issues is None:
            issues = [
                f"{task.task_key}: {task.last_error}"
                for task in tasks
                if task.status in {TaskStatus.FAILED, TaskStatus.BLOCKED} and task.last_error
            ]
        return TaskRunReport(
            session_id=session_id,
            plan_id=plan.id,
            status=plan.status.value,
            tasks_run=sum(1 for task in tasks if task.attempt_count > 0),
            tasks_completed=sum(1 for task in tasks if task.status == TaskStatus.COMPLETED),
            tasks_failed=sum(1 for task in tasks if task.status == TaskStatus.FAILED),
            tasks_blocked=sum(1 for task in tasks if task.status == TaskStatus.BLOCKED),
            revisions=max(len(revisions) - 1, 0),
            reason=reason,
            issues=list(issues),
        )

    # -------------------------------------------------------------- helpers

    async def _record_verification(
        self,
        session_id: str,
        task: TaskRecord,
        attempt: TaskAttemptRecord,
        *,
        completed: bool,
        reason: str,
        source: str,
        unmet: Sequence[str],
        fence: LeaseFence,
        run_id: str | None,
    ) -> None:
        await self.recorder.record(
            session_id,
            EventType.TASK_VERIFICATION,
            payload={
                "task_id": task.id,
                "task_key": task.task_key,
                "attempt_id": attempt.id,
                "attempt": attempt.attempt_no,
                "completed": completed,
                "reason": reason,
                "source": source,
                "unmet": list(unmet),
            },
            run_id=run_id,
            fence=fence,
        )
        await self._audit_verification(
            session_id,
            task,
            attempt,
            completed=completed,
            reason=reason,
            source=source,
            unmet=unmet,
        )

    async def _audit_verification(
        self,
        session_id: str,
        task: TaskRecord,
        attempt: TaskAttemptRecord,
        *,
        completed: bool,
        reason: str,
        source: str,
        unmet: Sequence[str],
    ) -> None:
        if self.audit is None or not self.audit.enabled:
            return
        try:
            await self.audit.append(
                actor="agent",
                action="task.verification",
                resource=task.id,
                detail={
                    "task_key": task.task_key,
                    "attempt": attempt.attempt_no,
                    "completed": completed,
                    "reason": reason,
                    "source": source,
                    "unmet": list(unmet),
                },
                session_id=session_id,
            )
        except AuditError:
            # The task transition is already durable; an audit failure must
            # not make the caller believe verification did not happen.
            logger.exception("task verification audit failed")


def _existing_state_decision(task: TaskRecord) -> TaskDecision:
    reason = (task.last_error or "").strip()
    if task.status == TaskStatus.BLOCKED:
        return TaskDecision(
            task=task,
            outcome="blocked",
            reason=reason or "task was blocked before the run finished",
        )
    if task.status == TaskStatus.CANCELLED:
        return TaskDecision(
            task=task,
            outcome="cancelled",
            reason=reason or "task was cancelled before the run finished",
        )
    if task.status == TaskStatus.COMPLETED:
        return TaskDecision(
            task=task,
            outcome="completed",
            reason=reason or "task was already completed before verification",
            verified=False,
        )
    return TaskDecision(
        task=task,
        outcome="noop",
        reason=reason or f"task is already {task.status.value}",
    )


def _failure_decision(task: TaskRecord, reason: str) -> TaskDecision:
    outcome = "retry_scheduled" if task.status == TaskStatus.PENDING else "failed"
    return TaskDecision(task=task, outcome=outcome, reason=reason)


def _run_failure_reason(outcome: TaskRunOutcome) -> str:
    detail = (outcome.error or "").strip()
    if detail:
        return f"task run {outcome.status}: {detail}"
    return f"task run ended with status {outcome.status}"


def _verification_unavailable(result: VerificationResult) -> bool:
    return result.reason.strip().lower().startswith(UNAVAILABLE_PREFIX)


def _verification_failure_reason(result: VerificationResult) -> str:
    reason = result.reason.strip() or "verification failed"
    if result.unmet:
        reason = f"{reason}; unmet: {'; '.join(result.unmet)}"
    return reason


__all__: list[str] = [
    "RETRYABLE_RUN_STATUSES",
    "UNAVAILABLE_PREFIX",
    "TaskDecision",
    "TaskEngine",
    "TaskExecution",
    "TaskRunOutcome",
]
