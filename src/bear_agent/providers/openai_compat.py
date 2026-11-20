"""OpenAI-compatible Chat Completions adapter.

Only protocol translation lives here. It works with OpenAI, DeepSeek, Qwen,
Ollama, vLLM and most relays because they implement the same chat-completions
shape. Vendor-specific quirks belong in capabilities/config, not in the runtime.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from bear_agent.config.models import ProviderConfig
from bear_agent.core.cancel import CancelToken
from bear_agent.core.errors import ProviderError
from bear_agent.core.messages import Message
from bear_agent.core.usage import TokenUsage
from bear_agent.providers.base import (
    FinishEvent,
    ModelCapabilities,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    TextDelta,
    ThinkingDelta,
    ToolCallArgumentsDelta,
    ToolCallEnd,
    ToolCallStart,
    UsageEvent,
)
from bear_agent.providers.retry import classify_exception


def message_to_openai(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        result = message.tool_result()
        if result is None:
            return {"role": "tool", "tool_call_id": "", "content": ""}
        return {
            "role": "tool",
            "tool_call_id": result.tool_call_id,
            "content": result.content,
        }

    if message.role == "assistant":
        payload: dict[str, Any] = {
            "role": "assistant",
            "content": message.text_content() or None,
        }
        calls = message.tool_calls()
        if calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in calls
            ]
        return payload

    return {"role": message.role, "content": message.text_content()}


def _parse_usage(raw: dict[str, Any] | None) -> TokenUsage | None:
    if not raw:
        return None
    details = raw.get("completion_tokens_details") or {}
    return TokenUsage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cache_read_tokens=int(
            raw.get("cache_read_input_tokens") or raw.get("prompt_cache_hit_tokens") or 0
        ),
        cache_write_tokens=int(
            raw.get("cache_creation_input_tokens") or raw.get("prompt_cache_miss_tokens") or 0
        ),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
    )


def _error_from_payload(raw: object) -> ProviderError:
    if isinstance(raw, dict):
        message = str(raw.get("message") or raw.get("code") or "provider error")
        code = str(raw.get("code") or raw.get("type") or "").lower()
        status = raw.get("status")
    else:
        message = str(raw)
        code = ""
        status = None
    category = "server_error"
    if any(
        marker in code or marker in message.lower()
        for marker in (
            "rate_limit",
            "rate limit",
            "insufficient_quota",
            "quota",
        )
    ):
        category = "rate_limit"
    if any(
        marker in code or marker in message.lower()
        for marker in (
            "context_length",
            "context length",
            "maximum context",
        )
    ):
        category = "context_length"
    return ProviderError(
        message[:500],
        category=category,
        retryable=category in {"rate_limit", "server_error"},
        status_code=int(status) if isinstance(status, int) else None,
    )


class OpenAICompatProvider(ModelProvider):
    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        *,
        model: str,
        capabilities: ModelCapabilities,
        secret_store: object | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.model = model
        self.capabilities = capabilities
        self.secret_store = secret_store
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_s),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.config.headers}
        key = self.config.resolve_api_key(self.secret_store)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message_to_openai(message) for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": request.stream,
        }
        if request.stream and self.config.include_usage:
            payload["stream_options"] = {"include_usage": True}
        if request.tools:
            payload["tools"] = [tool.to_openai() for tool in request.tools]
            payload["tool_choice"] = "auto"
        return payload

    @property
    def _url(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        if not request.stream:
            async for event in self._stream_complete(request, cancel):
                yield event
            return
        payload = self._build_payload(request)
        client = self._get_client()
        try:
            async with client.stream(
                "POST", self._url, headers=self._headers(), json=payload
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise classify_exception(exc) from exc
                finish_reason: str | None = None
                saw_done = False
                async for line in response.aiter_lines():
                    cancel.raise_if_cancelled()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if not data_text:
                        continue
                    if data_text == "[DONE]":
                        saw_done = True
                        break
                    try:
                        chunk = json.loads(data_text)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            f"malformed stream chunk: {data_text[:300]}",
                            category="malformed_response",
                            retryable=True,
                        ) from exc
                    if not isinstance(chunk, dict):
                        raise ProviderError(
                            "malformed stream chunk: expected a JSON object",
                            category="malformed_response",
                            retryable=True,
                        )
                    if chunk.get("error") is not None:
                        raise _error_from_payload(chunk.get("error"))
                    usage = _parse_usage(chunk.get("usage"))
                    if usage is not None:
                        yield UsageEvent(usage=usage)
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield TextDelta(text=str(content))
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        yield ThinkingDelta(text=str(reasoning))
                    for raw_call in delta.get("tool_calls") or []:
                        index = int(raw_call.get("index") or 0)
                        function = raw_call.get("function") or {}
                        call_id = raw_call.get("id")
                        name = function.get("name")
                        if call_id or name:
                            yield ToolCallStart(
                                index=index,
                                id=str(call_id or f"call_{index}"),
                                name=str(name or ""),
                            )
                        arguments = function.get("arguments")
                        if arguments:
                            yield ToolCallArgumentsDelta(index=index, delta=str(arguments))
                    chunk_finish = choice.get("finish_reason")
                    if chunk_finish:
                        finish_reason = str(chunk_finish)
                if finish_reason is None and not saw_done:
                    raise ProviderError(
                        "provider stream ended before a finish reason or [DONE]",
                        category="malformed_response",
                        retryable=True,
                    )
                # Some providers put usage after the finish_reason chunk. Delaying
                # Finish until the stream closes keeps Usage before Finish for
                # deterministic consumers.
                yield FinishEvent(reason=finish_reason or "stop")
        except ProviderError:
            raise
        except Exception as exc:
            raise classify_exception(exc) from exc

    async def _stream_complete(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncIterator[ModelEvent]:
        payload = self._build_payload(request.model_copy(update={"stream": False}))
        payload.pop("stream_options", None)
        try:
            response = await self._get_client().post(
                self._url, headers=self._headers(), json=payload
            )
            if response.status_code >= 400:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise classify_exception(exc) from exc
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError(
                    "provider returned malformed JSON",
                    category="malformed_response",
                    retryable=True,
                ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise classify_exception(exc) from exc

        if data.get("error") is not None:
            raise _error_from_payload(data.get("error"))
        usage = _parse_usage(data.get("usage"))
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(
                f"unexpected response shape: {str(data)[:300]}",
                category="malformed_response",
                retryable=False,
            )
        choice = choices[0]
        message_data = choice.get("message") or {}
        content = message_data.get("content")
        if content:
            yield TextDelta(text=str(content))
        for index, raw_call in enumerate(message_data.get("tool_calls") or []):
            function = raw_call.get("function") or {}
            call_id = str(raw_call.get("id") or f"call_{index}")
            name = str(function.get("name") or "")
            yield ToolCallStart(index=index, id=call_id, name=name)
            arguments = str(function.get("arguments") or "")
            if arguments:
                yield ToolCallArgumentsDelta(index=index, delta=arguments)
            yield ToolCallEnd(index=index)
        if usage is not None:
            yield UsageEvent(usage=usage)
        yield FinishEvent(reason=str(choice.get("finish_reason") or "stop"))
