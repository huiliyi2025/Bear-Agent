"""Agent runtime: bounded, observable runs and plan execution.

A single user request and a multi-task plan share the same lease, heartbeat,
recovery and turn machinery. ``run`` owns one request; ``run_plan`` holds the
lease across task runs so task state updates and verification are fenced by the
same owner/epoch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.errors import (
    ContextOverflowError,
    ProviderError,
    RecoveryError,
    SessionLeaseError,
    TaskStateError,
)
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence, reset_fence, set_fence
from bear_agent.core.ids import new_id
from bear_agent.core.usage import TokenUsage
from bear_agent.observability.recorder import EventRecorder
from bear_agent.runtime.stop_policy import (
    BudgetTracker,
    RunStatus,
    StopPolicy,
)
from bear_agent.runtime.turn_executor import TurnExecutor
from bear_agent.sessions.checkpoints import CheckpointService
from bear_agent.sessions.claims import ClaimService
from bear_agent.sessions.models import SessionRecord
from bear_agent.sessions.recovery import RecoveryService
from bear_agent.sessions.service import SessionService
from bear_agent.tasks.engine import TaskEngine, TaskRunOutcome
from bear_agent.tasks.models import PlanStatus, TaskRunReport

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    session_id: str
    run_id: str
    status: RunStatus
    final_text: str
    turns: int
    usage: TokenUsage = field(default_factory=TokenUsage)
    stop_reason: str = ""
    error: str | None = None


@dataclass
class _RequestOutcome:
    status: RunStatus = RunStatus.COMPLETED
    reason: str = ""
    error: str | None = None
    final_text: str = ""
    turns: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass
class _ActiveRun:
    cancel: CancelToken
    task: asyncio.Task[Any] | None = None


class AgentRuntime:
    def __init__(
        self,
        *,
        sessions: SessionService,
        turn_executor: TurnExecutor,
        recorder: EventRecorder,
        max_turns: int = 30,
        max_tool_calls: int = 200,
        max_run_seconds: float = 0.0,
        lease_ttl_s: float = 300.0,
        repeated_call_limit: int = 3,
        claims: ClaimService | None = None,
        recovery: RecoveryService | None = None,
        checkpoints: CheckpointService | None = None,
        checkpoint_interval: int = 0,
        task_engine: TaskEngine | None = None,
        task_max_turns: int = 20,
    ) -> None:
        self.sessions = sessions
        self.turn_executor = turn_executor
        self.recorder = recorder
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.max_run_seconds = max_run_seconds
        self.lease_ttl_s = lease_ttl_s
        self.repeated_call_limit = repeated_call_limit
        self.claims = claims
        self.recovery = recovery
        self.checkpoints = checkpoints
        self.checkpoint_interval = checkpoint_interval
        self.task_engine = task_engine
        self.task_max_turns = task_max_turns or max_turns
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, _ActiveRun] = {}

    def cancel(self, session_id: str) -> bool:
        active = self._active.get(session_id)
        if active is None:
            return False
        active.cancel.cancel()
        # Cancelling the task aborts an in-flight HTTP stream immediately; the
        # run boundary catches CancelledError and performs guarded cleanup.
        if active.task is not None and not active.task.done():
            active.task.cancel()
        return True

    def is_running(self, session_id: str) -> bool:
        return session_id in self._active

    # ---------------------------------------------------------------- one run

    async def run(self, session_id: str, user_input: str) -> RunResult:
        session = await self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"unknown session: {session_id}")
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise SessionLeaseError(f"session {session_id} is already running")

        async with lock:
            run_id = new_id("run")
            owner = f"pid-{os.getpid()}-{run_id}"
            epoch = await self.sessions.acquire_lease(session_id, owner, self.lease_ttl_s)
            fence = LeaseFence(owner=owner, epoch=epoch)
            fence_token = set_fence(fence)
            cancel = CancelToken()
            active = _ActiveRun(cancel=cancel)
            active.task = asyncio.current_task()
            self._active[session_id] = active
            stop_event = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._heartbeat(session_id, owner, epoch, cancel, stop_event)
            )
            outcome = _RequestOutcome(status=RunStatus.FAILED, reason="run failed")
            try:
                outcome = await self._execute_request(
                    session,
                    user_input,
                    run_id=run_id,
                    owner=owner,
                    epoch=epoch,
                    fence=fence,
                    cancel=cancel,
                    max_turns=self.max_turns,
                    run_recovery=True,
                    allow_title=True,
                )
            finally:
                stop_event.set()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self._finish_request_writes(
                    session_id,
                    run_id,
                    fence=fence,
                    owner=owner,
                    epoch=epoch,
                    outcome=outcome,
                    release_lease=True,
                    set_idle=True,
                    record_end=True,
                )
                reset_fence(fence_token)
                self._active.pop(session_id, None)
            return RunResult(
                session_id=session_id,
                run_id=run_id,
                status=outcome.status,
                final_text=outcome.final_text,
                turns=outcome.turns,
                usage=outcome.usage,
                stop_reason=outcome.reason,
                error=outcome.error,
            )

    async def _execute_request(
        self,
        session: SessionRecord,
        user_input: str,
        *,
        run_id: str,
        owner: str,
        epoch: int,
        fence: LeaseFence,
        cancel: CancelToken,
        max_turns: int,
        run_recovery: bool,
        allow_title: bool,
        extra_payload: dict[str, Any] | None = None,
    ) -> _RequestOutcome:
        session_id = session.id
        outcome = _RequestOutcome(reason="assistant produced a final answer")
        budget = BudgetTracker(
            max_turns=max_turns,
            max_tool_calls=self.max_tool_calls,
            max_run_seconds=self.max_run_seconds,
        )
        stop_policy = StopPolicy(max_repeated_calls=self.repeated_call_limit)
        try:
            await self.sessions.set_status(session_id, "active")
            await self.recorder.record(
                session_id,
                EventType.RUN_STARTED,
                run_id=run_id,
                payload={
                    "input_chars": len(user_input),
                    "model": self.turn_executor.model,
                    "provider": self.turn_executor.provider_name,
                    **(extra_payload or {}),
                },
            )
            if run_recovery and self.recovery is not None:
                report = await self.recovery.recover_if_needed(
                    session_id, fence=fence, run_id=run_id
                )
                if report.blocked:
                    detail = "; ".join(report.issues) or "unreconciled transcript state"
                    raise RecoveryError("session needs manual repair before a new turn: " + detail)
            _, user_event = await self.sessions.append_user_message(
                session_id, user_input, run_id=run_id
            )
            await self.recorder.publish_message_event(user_event)
            if allow_title and session.title is None:
                title = " ".join(user_input.strip().split())[:80] or "Untitled"
                await self.sessions.set_title(session_id, title)

            while True:
                cancel.raise_if_cancelled()
                time_stop = budget.check_time()
                if time_stop:
                    outcome.status, outcome.reason = time_stop.status, time_stop.reason
                    break
                if outcome.turns >= max_turns:
                    outcome.status = RunStatus.MAX_TURNS
                    outcome.reason = f"turn budget exhausted ({max_turns})"
                    break
                turn = await self.turn_executor.execute(
                    session,
                    outcome.turns,
                    run_id,
                    owner,
                    cancel,
                    lease_epoch=epoch,
                )
                outcome.turns += 1
                outcome.usage = outcome.usage + turn.usage
                if (
                    self.checkpoints is not None
                    and self.checkpoint_interval > 0
                    and outcome.turns % self.checkpoint_interval == 0
                ):
                    try:
                        await self.checkpoints.create(
                            session_id,
                            fence=fence,
                            kind="turn",
                            run_id=run_id,
                            turn_index=outcome.turns - 1,
                        )
                    except SessionLeaseError:
                        raise
                    except Exception:
                        # A checkpoint is a recovery aid, not a correctness
                        # prerequisite; do not fail a healthy run for it.
                        logger.exception("checkpoint creation failed")
                if turn.response.text:
                    outcome.final_text = turn.response.text
                tool_calls = len(turn.assistant.tool_calls())
                budget_stop = budget.add_tool_calls(tool_calls)
                if budget_stop:
                    outcome.status, outcome.reason = budget_stop.status, budget_stop.reason
                    break
                stop = stop_policy.evaluate(turn.assistant, outcome.turns - 1, max_turns)
                if stop:
                    outcome.status, outcome.reason = stop.status, stop.reason
                    break
        except RunCancelled:
            outcome.status = RunStatus.CANCELLED
            outcome.reason = "run cancelled by user"
        except ProviderError as exc:
            outcome.status = RunStatus.FAILED
            outcome.reason = f"provider error: {exc.category}"
            outcome.error = str(exc)
        except ContextOverflowError as exc:
            outcome.status = RunStatus.CONTEXT_OVERFLOW
            outcome.reason = "context overflow"
            outcome.error = str(exc)
        except RecoveryError as exc:
            outcome.status = RunStatus.FAILED
            outcome.reason = "session recovery failed"
            outcome.error = str(exc)
        except SessionLeaseError as exc:
            outcome.status = RunStatus.FAILED
            outcome.reason = "session lease lost"
            outcome.error = str(exc)
        except asyncio.CancelledError:
            outcome.status = RunStatus.CANCELLED
            outcome.reason = "run task cancelled"
        except Exception as exc:
            logger.exception("run failed")
            outcome.status = RunStatus.FAILED
            outcome.reason = f"internal error: {type(exc).__name__}"
            outcome.error = str(exc)
        return outcome

    async def _finish_request_writes(
        self,
        session_id: str,
        run_id: str,
        *,
        fence: LeaseFence,
        owner: str,
        epoch: int,
        outcome: _RequestOutcome,
        release_lease: bool,
        set_idle: bool,
        record_end: bool,
    ) -> None:
        cleanup: list[tuple[str, Any]] = []
        if self.claims is not None:
            cleanup.append(("release claims", self.claims.release_owner(session_id, owner)))
        cleanup.append(
            (
                "mark interrupted tools",
                self.sessions.mark_interrupted_tools_unknown(session_id, fence=fence),
            )
        )
        if set_idle:
            cleanup.append(("set idle", self.sessions.set_status(session_id, "idle", fence=fence)))
        if record_end:
            cleanup.append(
                (
                    "record run end",
                    self._record_run_end(
                        session_id,
                        run_id,
                        outcome.status,
                        outcome.turns,
                        outcome.usage,
                        outcome.reason,
                        outcome.error,
                        outcome.final_text,
                    ),
                )
            )
        if release_lease:
            cleanup.append(("release lease", self.sessions.release_lease(session_id, owner, epoch)))
        for label, awaitable in cleanup:
            try:
                await awaitable
            except Exception:
                logger.exception("run cleanup failed: %s", label)

    # ------------------------------------------------------------- plan run

    async def run_plan(self, session_id: str) -> TaskRunReport:
        """Execute the session's active plan under one continuous lease.

        Task selection, completion verification and state transitions happen
        between per-task request loops while the same fence is held, so a
        recovery or another process cannot interleave task state.
        """
        if self.task_engine is None:
            raise TaskStateError("task engine is not configured")
        session = await self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"unknown session: {session_id}")
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise SessionLeaseError(f"session {session_id} is already running")

        async with lock:
            plan_run_id = new_id("planrun")
            owner = f"pid-{os.getpid()}-{plan_run_id}"
            epoch = await self.sessions.acquire_lease(session_id, owner, self.lease_ttl_s)
            fence = LeaseFence(owner=owner, epoch=epoch)
            fence_token = set_fence(fence)
            cancel = CancelToken()
            active = _ActiveRun(cancel=cancel)
            active.task = asyncio.current_task()
            self._active[session_id] = active
            stop_event = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._heartbeat(session_id, owner, epoch, cancel, stop_event)
            )
            report = TaskRunReport(session_id=session_id, status="failed")
            tasks_run = 0
            try:
                self.task_engine.reset_budget()
                await self.sessions.set_status(session_id, "active")
                plan = await self.task_engine.tasks.get_plan(session_id)
                await self.recorder.record(
                    session_id,
                    EventType.TASK_RUN_STARTED,
                    run_id=plan_run_id,
                    payload={
                        "plan_id": plan.id if plan else None,
                        "version": plan.version if plan else None,
                    },
                )
                if self.recovery is not None:
                    recovery_report = await self.recovery.recover_if_needed(
                        session_id, fence=fence, run_id=plan_run_id
                    )
                    if recovery_report.blocked:
                        raise RecoveryError(
                            "session needs manual repair before plan execution: "
                            + "; ".join(recovery_report.issues)
                        )
                while True:
                    cancel.raise_if_cancelled()
                    if tasks_run >= self.task_engine.config.max_tasks_per_run:
                        break
                    task_run_id = new_id("run")
                    execution = await self.task_engine.start_next(
                        session_id, fence=fence, run_id=task_run_id
                    )
                    if execution is None:
                        break
                    tasks_run += 1
                    outcome = await self._execute_request(
                        session,
                        execution.prompt,
                        run_id=task_run_id,
                        owner=owner,
                        epoch=epoch,
                        fence=fence,
                        cancel=cancel,
                        max_turns=self.task_max_turns,
                        run_recovery=False,
                        allow_title=False,
                        extra_payload={
                            "task_id": execution.task.id,
                            "task_key": execution.task.task_key,
                            "plan_run_id": plan_run_id,
                        },
                    )
                    await self._finish_request_writes(
                        session_id,
                        task_run_id,
                        fence=fence,
                        owner=owner,
                        epoch=epoch,
                        outcome=outcome,
                        release_lease=False,
                        set_idle=False,
                        record_end=True,
                    )
                    if outcome.status == RunStatus.CANCELLED:
                        await self.task_engine.cancel(
                            session_id, fence=fence, reason="run cancelled by user"
                        )
                        cancel.cancel()
                        break
                    decision = await self.task_engine.finish(
                        execution,
                        TaskRunOutcome(
                            status=outcome.status.value,
                            final_text=outcome.final_text,
                            error=outcome.error,
                            turns=outcome.turns,
                        ),
                        fence=fence,
                        run_id=task_run_id,
                    )
                    await self._record_task_checkpoint(session_id, fence=fence, run_id=task_run_id)
                    plan = await self.task_engine.tasks.get_plan(session_id)
                    if plan is not None and plan.status in {
                        PlanStatus.COMPLETED,
                        PlanStatus.CANCELLED,
                    }:
                        break
                    if decision.outcome in {"blocked", "failed"} and (
                        plan is None or plan.status != PlanStatus.ACTIVE
                    ):
                        break
                report = await self.task_engine.report(
                    session_id,
                    reason=self.task_engine.last_stop_reason,
                )
                await self.recorder.record(
                    session_id,
                    EventType.TASK_RUN_COMPLETED,
                    run_id=plan_run_id,
                    payload={
                        "plan_id": report.plan_id,
                        "status": report.status,
                        "tasks_run": report.tasks_run,
                        "tasks_completed": report.tasks_completed,
                        "tasks_failed": report.tasks_failed,
                        "tasks_blocked": report.tasks_blocked,
                        "revisions": report.revisions,
                        "reason": report.reason,
                    },
                )
            except RunCancelled:
                with contextlib.suppress(Exception):
                    await self.task_engine.cancel(
                        session_id, fence=fence, reason="run cancelled by user"
                    )
                report = await self._safe_report(session_id, reason="run cancelled by user")
                with contextlib.suppress(Exception):
                    await self.recorder.record(
                        session_id,
                        EventType.TASK_RUN_COMPLETED,
                        run_id=plan_run_id,
                        payload={"plan_id": report.plan_id, "status": "cancelled"},
                    )
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await self.task_engine.cancel(
                        session_id, fence=fence, reason="plan run task cancelled"
                    )
                report = await self._safe_report(session_id, reason="plan run task cancelled")
            except Exception as exc:
                logger.exception("plan run failed")
                report = await self._safe_report(
                    session_id,
                    reason=f"plan run failed: {type(exc).__name__}",
                    issues=[str(exc)],
                )
                with contextlib.suppress(Exception):
                    await self.recorder.record(
                        session_id,
                        EventType.TASK_RUN_FAILED,
                        run_id=plan_run_id,
                        payload={
                            "plan_id": report.plan_id,
                            "status": report.status,
                            "error": str(exc),
                        },
                    )
            finally:
                stop_event.set()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                # Any attempt left running by an unexpected failure becomes
                # visibly blocked rather than silently retried next time.
                with contextlib.suppress(Exception):
                    await self.task_engine.reconcile(session_id, fence=fence, run_id=plan_run_id)
                # Recompute after reconciliation so a failed run cannot return a
                # stale "active" report while the DB says blocked/failed.
                with contextlib.suppress(Exception):
                    report = await self.task_engine.report(
                        session_id,
                        reason=report.reason or "plan run finished",
                        issues=list(report.issues) or None,
                    )
                cleanup_outcome = _RequestOutcome(
                    status=(
                        RunStatus.CANCELLED
                        if report.status == "cancelled"
                        else RunStatus.COMPLETED
                        if report.status in {"completed", "active", "blocked", "draft"}
                        else RunStatus.FAILED
                    ),
                    reason=report.reason or "plan run finished",
                    error="; ".join(report.issues) or None,
                )
                await self._finish_request_writes(
                    session_id,
                    plan_run_id,
                    fence=fence,
                    owner=owner,
                    epoch=epoch,
                    outcome=cleanup_outcome,
                    release_lease=True,
                    set_idle=True,
                    record_end=False,
                )
                reset_fence(fence_token)
                self._active.pop(session_id, None)
            return report

    async def _record_task_checkpoint(
        self, session_id: str, *, fence: LeaseFence, run_id: str
    ) -> None:
        if self.checkpoints is None or self.checkpoint_interval <= 0:
            return
        try:
            await self.checkpoints.create(session_id, fence=fence, kind="task", run_id=run_id)
        except SessionLeaseError:
            raise
        except Exception:
            logger.exception("task checkpoint creation failed")

    async def _safe_report(
        self,
        session_id: str,
        *,
        reason: str,
        issues: list[str] | None = None,
    ) -> TaskRunReport:
        if self.task_engine is not None:
            with contextlib.suppress(Exception):
                return await self.task_engine.report(session_id, reason=reason, issues=issues)
        return TaskRunReport(
            session_id=session_id,
            status="failed",
            reason=reason,
            issues=list(issues or []),
        )

    # -------------------------------------------------------------- outcomes

    async def _record_run_end(
        self,
        session_id: str,
        run_id: str,
        status: RunStatus,
        turns: int,
        usage: TokenUsage,
        reason: str,
        error: str | None,
        final_text: str,
    ) -> None:
        event_type = EventType.RUN_COMPLETED
        if status == RunStatus.CANCELLED:
            event_type = EventType.RUN_CANCELLED
        elif status == RunStatus.FAILED:
            event_type = EventType.RUN_FAILED
        payload: dict[str, Any] = {
            "status": status.value,
            "turns": turns,
            "usage": usage.model_dump(),
            "reason": reason,
            "final_chars": len(final_text),
        }
        if error:
            payload["error"] = error
        await self.recorder.record(session_id, event_type, run_id=run_id, payload=payload)

    async def _heartbeat(
        self,
        session_id: str,
        owner: str,
        epoch: int,
        cancel: CancelToken,
        stop_event: asyncio.Event,
    ) -> None:
        interval = max(1.0, self.lease_ttl_s / 3)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            if stop_event.is_set():
                return
            try:
                renewed = await self.sessions.renew_lease(
                    session_id, owner, epoch, self.lease_ttl_s
                )
            except Exception:  # losing the lease must stop the run
                logger.exception("lease heartbeat failed")
                cancel.cancel()
                return
            if not renewed:
                cancel.cancel()
                return
            if self.claims is not None:
                try:
                    await self.claims.renew_owner(session_id, owner, self.lease_ttl_s)
                except Exception:  # losing claims must stop the run
                    logger.exception("claim heartbeat failed")
                    cancel.cancel()
                    return
