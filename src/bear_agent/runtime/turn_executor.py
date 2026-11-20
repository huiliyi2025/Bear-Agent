"""One model turn: build context, stream, persist, execute tools.

The executor owns transcript ordering. It never lets a tool result float
without its assistant tool-call message, and it persists the assistant message
before any tool side effect begins.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass

from bear_agent.context.builder import ContextBuilder
from bear_agent.context.compaction import ContextCompactor
from bear_agent.core.cancel import CancelToken
from bear_agent.core.errors import HookError, ProviderError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.ids import new_id
from bear_agent.core.messages import AssistantMessage, ToolMessage
from bear_agent.core.usage import TokenUsage
from bear_agent.hooks.events import HookEvent
from bear_agent.hooks.manager import HookManager
from bear_agent.observability.recorder import EventRecorder
from bear_agent.providers.base import ModelRequest
from bear_agent.runtime.model_caller import ModelCaller, ModelResponse
from bear_agent.sessions.models import SessionRecord
from bear_agent.sessions.service import SessionService
from bear_agent.tasks.service import TaskService
from bear_agent.tools.base import ToolResult
from bear_agent.tools.executor import ToolExecutor
from bear_agent.tools.registry import ToolRegistry


@dataclass
class TurnOutcome:
    turn_id: str
    assistant: AssistantMessage
    response: ModelResponse
    tool_results: list[ToolResult]
    usage: TokenUsage


class TurnExecutor:
    def __init__(
        self,
        *,
        sessions: SessionService,
        context_builder: ContextBuilder,
        model_caller: ModelCaller,
        tool_executor: ToolExecutor,
        registry: ToolRegistry,
        recorder: EventRecorder,
        compactor: ContextCompactor | None = None,
        tasks: TaskService | None = None,
        hooks: HookManager | None = None,
        model: str,
        provider_name: str = "",
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
        stream: bool = True,
    ) -> None:
        self.sessions = sessions
        self.context_builder = context_builder
        self.model_caller = model_caller
        self.tool_executor = tool_executor
        self.registry = registry
        self.recorder = recorder
        self.compactor = compactor
        self.tasks = tasks
        self.hooks = hooks
        self.model = model
        self.provider_name = provider_name
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.stream = stream

    async def execute(
        self,
        session: SessionRecord,
        turn_index: int,
        run_id: str,
        lease_owner: str,
        cancel: CancelToken,
        *,
        lease_epoch: int | None = None,
    ) -> TurnOutcome:
        turn_id = new_id("turn")
        await self.recorder.record(
            session.id,
            EventType.TURN_STARTED,
            run_id=run_id,
            turn_id=turn_id,
            payload={"turn": turn_index},
        )
        messages = await self.sessions.load_messages(session.id)
        tools = self.registry.specs()
        compaction_info: dict[str, object] = {}
        if self.compactor is not None and lease_epoch is not None:
            prepared = await self.compactor.prepare(
                session,
                messages,
                tools,
                turn_index=turn_index,
                run_id=run_id,
                turn_id=turn_id,
                fence=LeaseFence(lease_owner, lease_epoch),
                cancel=cancel,
            )
            messages_for_context = prepared.messages
            system_extra = prepared.anchor_block
            compaction_info = {
                "compacted": prepared.compacted,
                "compaction_ids": prepared.compaction_ids,
                "compaction_levels": prepared.levels,
            }
        else:
            messages_for_context = messages
            system_extra = ""
        task_block = ""
        if self.tasks is not None:
            task_block = await self.tasks.render_context(session.id)
            if task_block:
                system_extra = "\n\n".join(
                    part for part in (system_extra, task_block) if part.strip()
                )
        if self.hooks is not None:
            hook_outcome = await self.hooks.before_model_call(
                model=self.model or (session.model or ""),
                messages=len(messages_for_context),
                tools=len(tools),
                session_id=session.id,
                run_id=run_id,
                turn_id=turn_id,
                fence=(LeaseFence(lease_owner, lease_epoch) if lease_epoch is not None else None),
            )
            if hook_outcome.failed_closed:
                raise HookError(
                    "a fail-closed model hook failed: " + (hook_outcome.reason or "hook failure")
                )
            if hook_outcome.context_append:
                system_extra = "\n\n".join(
                    part for part in (system_extra, hook_outcome.context_append) if part.strip()
                )
        built = self.context_builder.build(messages_for_context, tools, system_extra=system_extra)
        await self.recorder.record(
            session.id,
            EventType.CONTEXT_BUILT,
            run_id=run_id,
            turn_id=turn_id,
            payload={
                "estimated_tokens": built.estimated_tokens,
                "history_tokens": built.history_tokens,
                "system_tokens": built.system_tokens,
                "messages": len(built.messages),
                "dropped_messages": built.dropped_messages,
                "truncated": built.truncated,
                "budget_tokens": built.budget_tokens,
                "task_context_chars": len(task_block),
                **compaction_info,
            },
        )
        if built.dropped_messages or built.truncated:
            await self.recorder.record(
                session.id,
                EventType.CONTEXT_TRUNCATED,
                run_id=run_id,
                turn_id=turn_id,
                payload={
                    "dropped_messages": built.dropped_messages,
                    "truncated": built.truncated,
                },
            )

        request = ModelRequest(
            model=self.model or (session.model or ""),
            messages=built.messages,
            tools=tools,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            stream=self.stream,
            metadata={
                "session_id": session.id,
                "run_id": run_id,
                "turn_id": turn_id,
            },
        )
        await self.recorder.record(
            session.id,
            EventType.MODEL_REQUEST,
            run_id=run_id,
            turn_id=turn_id,
            payload={
                "model": request.model,
                "messages": len(request.messages),
                "tools": len(tools),
                "estimated_tokens": built.estimated_tokens,
                "prompt_hash": _prompt_hash(request),
            },
        )

        async def on_text(delta: str) -> None:
            await self.recorder.record(
                session.id,
                EventType.MODEL_TEXT_DELTA,
                run_id=run_id,
                turn_id=turn_id,
                persist=False,
                payload={"text": delta},
            )

        async def on_thinking(delta: str) -> None:
            await self.recorder.record(
                session.id,
                EventType.MODEL_THINKING_DELTA,
                run_id=run_id,
                turn_id=turn_id,
                persist=False,
                payload={"text": delta},
            )

        try:
            response = await self.model_caller.call(
                request,
                cancel,
                on_text_delta=on_text,
                on_thinking_delta=on_thinking,
            )
        except ProviderError as exc:
            with contextlib.suppress(Exception):
                await self.sessions.model_calls.record(
                    call_id=new_id("mc"),
                    session_id=session.id,
                    turn_id=turn_id,
                    provider=self.provider_name,
                    model=request.model,
                    usage=TokenUsage(),
                    latency_ms=0,
                    status="failed",
                )
            if self.hooks is not None:
                with contextlib.suppress(Exception):
                    await self.hooks.emit(
                        HookEvent.MODEL_ERROR,
                        session_id=session.id,
                        run_id=run_id,
                        turn_id=turn_id,
                        payload={"model": request.model, "category": exc.category},
                        fence=(
                            LeaseFence(lease_owner, lease_epoch)
                            if lease_epoch is not None
                            else None
                        ),
                    )
            raise
        _, message_event = await self.sessions.append_assistant_message(
            session.id,
            response.message,
            run_id=run_id,
            turn_id=turn_id,
            payload={
                "finish_reason": response.finish_reason,
                "text_chars": len(response.text),
                "tool_calls": len(response.message.tool_calls()),
            },
        )
        await self.recorder.publish_message_event(message_event)

        usage = response.usage
        await self.sessions.model_calls.record(
            call_id=new_id("mc"),
            session_id=session.id,
            turn_id=turn_id,
            provider=response.provider_name or self.provider_name or (session.provider or ""),
            model=response.model or request.model,
            usage=usage,
            latency_ms=response.latency_ms,
            status="completed",
        )
        await self.recorder.record(
            session.id,
            EventType.MODEL_RESPONSE,
            run_id=run_id,
            turn_id=turn_id,
            payload={
                "finish_reason": response.finish_reason,
                "latency_ms": response.latency_ms,
                "usage": usage.model_dump(),
                "has_tool_calls": bool(response.message.tool_calls()),
                "provider": response.provider_name or self.provider_name,
                "model": response.model or request.model,
            },
        )

        tool_results: list[ToolResult] = []
        for call in response.message.tool_calls():
            cancel.raise_if_cancelled()
            result = await self.tool_executor.execute(
                call,
                session_id=session.id,
                project_id=session.project_id,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
                run_id=run_id,
                turn_id=turn_id,
                cancel=cancel,
            )
            tool_results.append(result)
            tool_message = ToolMessage.of(
                call.id, result.content or "(no output)", is_error=result.is_error
            )
            _, tool_event = await self.sessions.append_tool_message(
                session.id,
                tool_message,
                run_id=run_id,
                turn_id=turn_id,
                payload={
                    "tool": call.name,
                    "tool_call_id": call.id,
                    "is_error": result.is_error,
                },
            )
            await self.recorder.publish_message_event(tool_event)

        await self.recorder.record(
            session.id,
            EventType.TURN_COMPLETED,
            run_id=run_id,
            turn_id=turn_id,
            payload={
                "turn": turn_index,
                "tool_calls": len(response.message.tool_calls()),
                "finish_reason": response.finish_reason,
            },
        )
        return TurnOutcome(
            turn_id=turn_id,
            assistant=response.message,
            response=response,
            tool_results=tool_results,
            usage=usage,
        )


def _prompt_hash(request: ModelRequest) -> str:
    payload = json.dumps(
        {
            "model": request.model,
            "messages": [message.model_dump(mode="json") for message in request.messages],
            "tools": [tool.model_dump(mode="json") for tool in request.tools],
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
