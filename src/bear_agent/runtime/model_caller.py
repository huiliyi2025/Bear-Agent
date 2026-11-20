"""Consume provider streams into a complete assistant message.

Tool-call arguments arrive in fragments; they are only handed to the executor
after the stream ends and the JSON is validated. An incomplete call is returned
with ``arguments_error`` so the executor can refuse it instead of running ``{}``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bear_agent.core.cancel import CancelToken
from bear_agent.core.errors import ProviderError
from bear_agent.core.messages import AssistantMessage, ToolCall
from bear_agent.core.usage import TokenUsage
from bear_agent.providers.base import (
    FinishEvent,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    ProviderErrorEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallArgumentsDelta,
    ToolCallEnd,
    ToolCallStart,
    UsageEvent,
)

TextHandler = Callable[[str], Awaitable[None]]


@dataclass
class ModelResponse:
    message: AssistantMessage
    text: str
    thinking: str
    usage: TokenUsage
    finish_reason: str
    latency_ms: int
    provider_name: str = ""
    model: str = ""


@dataclass
class _CallBuffer:
    id: str = ""
    name: str = ""
    arguments: str = ""
    ended: bool = False


@dataclass
class _Aggregate:
    text: str = ""
    thinking: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = "stop"
    calls: dict[int, _CallBuffer] = field(default_factory=dict)


class ModelCaller:
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    async def call(
        self,
        request: ModelRequest,
        cancel: CancelToken,
        *,
        on_text_delta: TextHandler | None = None,
        on_thinking_delta: TextHandler | None = None,
    ) -> ModelResponse:
        started = time.monotonic()
        aggregate = _Aggregate()
        async for event in self.provider.stream(request, cancel):
            await self._consume(event, aggregate, on_text_delta, on_thinking_delta)
        latency_ms = int((time.monotonic() - started) * 1000)
        route = getattr(self.provider, "last_route", None)
        route_provider, route_model = route if isinstance(route, tuple) else ("", "")
        calls = [
            self._finalize_call(index, buffer) for index, buffer in sorted(aggregate.calls.items())
        ]
        message = (
            AssistantMessage.with_tool_calls(aggregate.text or None, calls)
            if calls
            else AssistantMessage.of(aggregate.text)
        )
        return ModelResponse(
            message=message,
            text=aggregate.text,
            thinking=aggregate.thinking,
            usage=aggregate.usage,
            finish_reason=aggregate.finish_reason,
            latency_ms=latency_ms,
            provider_name=str(route_provider or ""),
            model=str(route_model or ""),
        )

    async def _consume(
        self,
        event: ModelEvent,
        aggregate: _Aggregate,
        on_text_delta: TextHandler | None,
        on_thinking_delta: TextHandler | None,
    ) -> None:
        if isinstance(event, TextDelta):
            aggregate.text += event.text
            if on_text_delta:
                await on_text_delta(event.text)
        elif isinstance(event, ThinkingDelta):
            aggregate.thinking += event.text
            if on_thinking_delta:
                await on_thinking_delta(event.text)
        elif isinstance(event, ToolCallStart):
            buffer = aggregate.calls.setdefault(event.index, _CallBuffer())
            if event.id:
                buffer.id = event.id
            if event.name:
                buffer.name = event.name
        elif isinstance(event, ToolCallArgumentsDelta):
            buffer = aggregate.calls.setdefault(event.index, _CallBuffer())
            buffer.arguments += event.delta
        elif isinstance(event, ToolCallEnd):
            if event.index in aggregate.calls:
                aggregate.calls[event.index].ended = True
        elif isinstance(event, UsageEvent):
            aggregate.usage = aggregate.usage + event.usage
        elif isinstance(event, FinishEvent):
            aggregate.finish_reason = event.reason
        elif isinstance(event, ProviderErrorEvent):
            raise ProviderError(
                event.message,
                category=event.category,
                retryable=event.retryable,
            )

    @staticmethod
    def _finalize_call(index: int, buffer: _CallBuffer) -> ToolCall:
        call_id = buffer.id or f"call_{index}"
        if not buffer.name:
            return ToolCall(
                id=call_id,
                name="",
                arguments={},
                arguments_error="provider did not send a tool name",
            )
        raw = buffer.arguments.strip()
        if not raw:
            return ToolCall(id=call_id, name=buffer.name, arguments={})
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ToolCall(
                id=call_id,
                name=buffer.name,
                arguments={},
                arguments_error=f"invalid JSON arguments: {exc}",
            )
        if not isinstance(parsed, dict):
            return ToolCall(
                id=call_id,
                name=buffer.name,
                arguments={},
                arguments_error="tool arguments must be a JSON object",
            )
        return ToolCall(id=call_id, name=buffer.name, arguments=parsed)
