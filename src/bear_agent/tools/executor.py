"""Tool execution pipeline.

Order matters and is a security contract:
resolve -> validate -> policy -> approval -> persist start -> execute -> normalize
-> persist finish. Unknown tools are never executed; invalid arguments never
reach a tool; denied calls are auditable.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from pydantic import ValidationError

from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.events import EventType
from bear_agent.core.ids import new_id
from bear_agent.core.messages import ToolCall
from bear_agent.observability.recorder import EventRecorder
from bear_agent.policy.approver import Approver
from bear_agent.policy.engine import PermissionEngine
from bear_agent.policy.models import ApprovalRequest, DecisionType
from bear_agent.policy.path_guard import PathGuard
from bear_agent.sessions.service import SessionService
from bear_agent.tools.base import ToolContext, ToolResult
from bear_agent.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        sessions: SessionService,
        policy: PermissionEngine,
        approver: Approver,
        recorder: EventRecorder,
        path_guard: PathGuard,
        max_output_chars: int = 24_000,
    ) -> None:
        self.registry = registry
        self.sessions = sessions
        self.policy = policy
        self.approver = approver
        self.recorder = recorder
        self.path_guard = path_guard
        self.max_output_chars = max_output_chars

    async def execute(
        self,
        call: ToolCall,
        *,
        session_id: str,
        run_id: str | None,
        turn_id: str | None,
        cancel: CancelToken,
    ) -> ToolResult:
        if not call.id:
            call = call.model_copy(update={"id": new_id("call")})
        tool = self.registry.get(call.name)
        idempotent = tool.idempotent if tool else False
        await self.sessions.tool_calls.start(
            session_id,
            turn_id,
            call.id,
            call.name,
            call.arguments,
            idempotent,
        )
        await self.recorder.record(
            session_id,
            EventType.TOOL_CALL_STARTED,
            run_id=run_id,
            turn_id=turn_id,
            payload={
                "tool_call_id": call.id,
                "name": call.name,
                "arguments_preview": _preview(call.arguments),
            },
        )
        await self.recorder.record(
            session_id,
            EventType.TOOL_STARTED,
            run_id=run_id,
            turn_id=turn_id,
            persist=False,
            payload={"tool_call_id": call.id, "name": call.name},
        )

        if tool is None:
            return await self._fail(
                session_id,
                run_id,
                turn_id,
                call,
                "unknown_tool",
                f"unknown tool: {call.name}",
            )
        if call.arguments_error:
            return await self._fail(
                session_id,
                run_id,
                turn_id,
                call,
                "invalid_arguments",
                f"tool arguments were incomplete or unparsable: {call.arguments_error}",
            )

        try:
            args = tool.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            return await self._fail(
                session_id,
                run_id,
                turn_id,
                call,
                "invalid_arguments",
                f"invalid arguments for {call.name}: {exc}",
            )

        decision = self.policy.decide(tool, args)
        await self.recorder.record(
            session_id,
            EventType.POLICY_DECISION,
            run_id=run_id,
            turn_id=turn_id,
            payload={
                "tool_call_id": call.id,
                "tool": call.name,
                "decision": decision.decision.value,
                "reason": decision.reason,
            },
        )
        if decision.decision == DecisionType.DENY:
            return await self._fail(
                session_id, run_id, turn_id, call, "policy_denied", decision.reason
            )
        if decision.decision == DecisionType.ASK:
            await self.recorder.record(
                session_id,
                EventType.APPROVAL_REQUESTED,
                run_id=run_id,
                turn_id=turn_id,
                payload={"tool_call_id": call.id, "tool": call.name},
            )
            approved = await self.approver.request(
                ApprovalRequest(
                    tool_name=call.name,
                    arguments=call.arguments,
                    reason=decision.reason,
                    session_id=session_id,
                )
            )
            await self.recorder.record(
                session_id,
                EventType.APPROVAL_DECIDED,
                run_id=run_id,
                turn_id=turn_id,
                payload={"tool_call_id": call.id, "approved": approved},
            )
            if not approved:
                return await self._fail(
                    session_id,
                    run_id,
                    turn_id,
                    call,
                    "approval_denied",
                    f"user denied {call.name}",
                )

        cancel.raise_if_cancelled()
        ctx = ToolContext(
            cwd=self.path_guard.cwd,
            session_id=session_id,
            cancel=cancel,
            path_guard=self.path_guard,
            max_output_chars=self.max_output_chars,
        )
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(tool.run(args, ctx), timeout=tool.timeout_s)
        except asyncio.CancelledError:
            await self._finish_row(call.id, "cancelled", "execution cancelled")
            await self._record_finish(session_id, run_id, turn_id, call, "cancelled", None, None)
            raise
        except RunCancelled:
            await self._finish_row(call.id, "cancelled", "run cancelled")
            await self._record_finish(session_id, run_id, turn_id, call, "cancelled", None, None)
            raise
        except TimeoutError:
            result = ToolResult(
                content=f"tool {call.name} timed out after {tool.timeout_s:.0f}s",
                is_error=True,
                error_kind="timeout",
            )
        except Exception as exc:
            result = ToolResult(
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
                error_kind="tool_error",
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        result = self._normalize(result)
        status = "failed" if result.is_error else "completed"
        await self._finish_row(call.id, status, result.content, result.error_kind)
        await self._record_finish(session_id, run_id, turn_id, call, status, result, duration_ms)
        return result

    async def _fail(
        self,
        session_id: str,
        run_id: str | None,
        turn_id: str | None,
        call: ToolCall,
        kind: str,
        message: str,
    ) -> ToolResult:
        result = ToolResult(content=message, is_error=True, error_kind=kind)
        await self._finish_row(call.id, "failed", message, kind)
        await self._record_finish(session_id, run_id, turn_id, call, "failed", result, None)
        return result

    async def _finish_row(
        self,
        call_id: str,
        status: str,
        summary: str | None,
        error_kind: str | None = None,
    ) -> None:
        await self.sessions.tool_calls.finish(
            call_id,
            status,
            result_summary=(summary or "")[:500] or None,
            error_kind=error_kind,
            error_message=(summary or "")[:1000] if status == "failed" else None,
        )

    async def _record_finish(
        self,
        session_id: str,
        run_id: str | None,
        turn_id: str | None,
        call: ToolCall,
        status: str,
        result: ToolResult | None,
        duration_ms: int | None,
    ) -> None:
        event_type = (
            EventType.TOOL_CALL_COMPLETED if status == "completed" else EventType.TOOL_CALL_FAILED
        )
        payload: dict[str, Any] = {
            "tool_call_id": call.id,
            "name": call.name,
            "status": status,
        }
        if result is not None:
            payload["is_error"] = result.is_error
            payload["error_kind"] = result.error_kind
            payload["content_preview"] = result.content[:300]
            payload["files_changed"] = result.files_changed
            payload["truncated"] = result.truncated
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        await self.recorder.record(
            session_id,
            event_type,
            run_id=run_id,
            turn_id=turn_id,
            payload=payload,
        )
        await self.recorder.record(
            session_id,
            EventType.TOOL_FINISHED,
            run_id=run_id,
            turn_id=turn_id,
            persist=False,
            payload={
                "tool_call_id": call.id,
                "name": call.name,
                "status": status,
                "duration_ms": duration_ms,
            },
        )

    def _normalize(self, result: ToolResult) -> ToolResult:
        if len(result.content) <= self.max_output_chars:
            return result
        limit = self.max_output_chars
        head = limit * 2 // 3
        tail = limit - head
        content = (
            result.content[:head]
            + f"\n... [truncated {len(result.content) - limit} chars] ...\n"
            + result.content[-tail:]
        )
        return result.model_copy(update={"content": content, "truncated": True})


def _preview(arguments: dict[str, Any]) -> str:
    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(arguments)
    return text if len(text) <= 500 else text[:500] + "…"
