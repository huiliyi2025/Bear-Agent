"""One model turn: build context, stream, persist, execute tools.

The executor owns transcript ordering. It never lets a tool result float
without its assistant tool-call message, and it persists the assistant message
before any tool side effect begins.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from bear_agent.context.builder import ContextBuilder
from bear_agent.core.cancel import CancelToken
from bear_agent.core.events import EventType
from bear_agent.core.ids import new_id
from bear_agent.core.messages import AssistantMessage, ToolMessage
from bear_agent.core.usage import TokenUsage
from bear_agent.observability.recorder import EventRecorder
from bear_agent.providers.base import ModelRequest
from bear_agent.runtime.model_caller import ModelCaller, ModelResponse
from bear_agent.sessions.models import SessionRecord
from bear_agent.sessions.service import SessionService
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
        built = self.context_builder.build(messages, tools)
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

        response = await self.model_caller.call(
            request,
            cancel,
            on_text_delta=on_text,
            on_thinking_delta=on_thinking,
        )
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
            provider=self.provider_name or (session.provider or ""),
            model=request.model,
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
