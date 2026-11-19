"""Agent runtime: one user request becomes a bounded, observable run.

Responsibilities are deliberately narrow:
- acquire a session lease (one writer)
- append the user message
- drive turns until a structured stop condition
- persist run outcomes and release the lease

The turn mechanics live in ``TurnExecutor``; stopping lives in ``StopPolicy``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, cast

from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.errors import (
    ContextOverflowError,
    ProviderError,
    SessionLeaseError,
)
from bear_agent.core.events import EventType
from bear_agent.core.ids import new_id
from bear_agent.core.usage import TokenUsage
from bear_agent.observability.recorder import EventRecorder
from bear_agent.runtime.stop_policy import (
    BudgetTracker,
    RunStatus,
    StopPolicy,
)
from bear_agent.runtime.turn_executor import TurnExecutor
from bear_agent.sessions.service import SessionService

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
class _ActiveRun:
    cancel: CancelToken
    task: asyncio.Task[RunResult] | None = None


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
    ) -> None:
        self.sessions = sessions
        self.turn_executor = turn_executor
        self.recorder = recorder
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.max_run_seconds = max_run_seconds
        self.lease_ttl_s = lease_ttl_s
        self.repeated_call_limit = repeated_call_limit
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
            cancel = CancelToken()
            active = _ActiveRun(cancel=cancel)
            active.task = cast(asyncio.Task[RunResult] | None, asyncio.current_task())
            self._active[session_id] = active
            stop_event = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._heartbeat(session_id, owner, epoch, cancel, stop_event)
            )
            await self.sessions.set_status(session_id, "active")
            await self.recorder.record(
                session_id,
                EventType.RUN_STARTED,
                run_id=run_id,
                payload={
                    "input_chars": len(user_input),
                    "model": self.turn_executor.model,
                    "provider": self.turn_executor.provider_name,
                },
            )
            usage = TokenUsage()
            turns = 0
            final_text = ""
            status = RunStatus.COMPLETED
            reason = "assistant produced a final answer"
            error: str | None = None
            budget = BudgetTracker(
                max_turns=self.max_turns,
                max_tool_calls=self.max_tool_calls,
                max_run_seconds=self.max_run_seconds,
            )
            stop_policy = StopPolicy(max_repeated_calls=self.repeated_call_limit)

            try:
                _, user_event = await self.sessions.append_user_message(
                    session_id, user_input, run_id=run_id
                )
                await self.recorder.publish_message_event(user_event)
                if session.title is None:
                    title = " ".join(user_input.strip().split())[:80] or "Untitled"
                    await self.sessions.set_title(session_id, title)

                while True:
                    cancel.raise_if_cancelled()
                    time_stop = budget.check_time()
                    if time_stop:
                        status, reason = time_stop.status, time_stop.reason
                        break
                    if turns >= self.max_turns:
                        status = RunStatus.MAX_TURNS
                        reason = f"turn budget exhausted ({self.max_turns})"
                        break
                    outcome = await self.turn_executor.execute(session, turns, run_id, cancel)
                    turns += 1
                    usage = usage + outcome.usage
                    if outcome.response.text:
                        final_text = outcome.response.text
                    tool_calls = len(outcome.assistant.tool_calls())
                    budget_stop = budget.add_tool_calls(tool_calls)
                    if budget_stop:
                        status, reason = budget_stop.status, budget_stop.reason
                        break
                    stop = stop_policy.evaluate(outcome.assistant, turns - 1, self.max_turns)
                    if stop:
                        status, reason = stop.status, stop.reason
                        break
            except RunCancelled:
                status = RunStatus.CANCELLED
                reason = "run cancelled by user"
            except ProviderError as exc:
                status = RunStatus.FAILED
                reason = f"provider error: {exc.category}"
                error = str(exc)
            except ContextOverflowError as exc:
                status = RunStatus.CONTEXT_OVERFLOW
                reason = "context overflow"
                error = str(exc)
            except asyncio.CancelledError:
                status = RunStatus.CANCELLED
                reason = "run task cancelled"
            except Exception as exc:
                logger.exception("run failed")
                status = RunStatus.FAILED
                reason = f"internal error: {type(exc).__name__}"
                error = str(exc)
            finally:
                stop_event.set()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                # Cleanup must not be able to turn a completed run into a crash.
                cleanup: list[tuple[str, Any]] = [
                    (
                        "mark interrupted tools",
                        self.sessions.mark_interrupted_tools_unknown(session_id),
                    ),
                    ("release lease", self.sessions.release_lease(session_id, owner, epoch)),
                    ("set idle", self.sessions.set_status(session_id, "idle")),
                    (
                        "record run end",
                        self._record_run_end(
                            session_id,
                            run_id,
                            status,
                            turns,
                            usage,
                            reason,
                            error,
                            final_text,
                        ),
                    ),
                ]
                for label, awaitable in cleanup:
                    try:
                        await awaitable
                    except Exception:
                        logger.exception("run cleanup failed: %s", label)
                self._active.pop(session_id, None)
            return RunResult(
                session_id=session_id,
                run_id=run_id,
                status=status,
                final_text=final_text,
                turns=turns,
                usage=usage,
                stop_reason=reason,
                error=error,
            )

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
