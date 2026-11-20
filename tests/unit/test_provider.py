from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from bear_agent.config.models import ProviderConfig
from bear_agent.core.cancel import CancelToken
from bear_agent.core.errors import ProviderError
from bear_agent.core.messages import AssistantMessage, ToolCall, UserMessage
from bear_agent.providers.base import (
    FinishEvent,
    ModelCapabilities,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStart,
    UsageEvent,
)
from bear_agent.providers.openai_compat import OpenAICompatProvider, message_to_openai
from bear_agent.providers.retry import RetryingProvider, RetryPolicy

CAPS = ModelCapabilities(context_window=32_000, max_output_tokens=1_000)
CONFIG = ProviderConfig(base_url="http://fake.local/v1", api_key="test", max_retries=0)


def _provider() -> OpenAICompatProvider:
    return OpenAICompatProvider("fake", CONFIG, model="m", capabilities=CAPS)


def _request(stream: bool = True) -> ModelRequest:
    return ModelRequest(model="m", messages=[UserMessage.of("hi")], stream=stream)


SSE = """\
data: {"choices":[{"delta":{"content":"Hel"}}]}

data: {"choices":[{"delta":{"content":"lo"}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"read_file","arguments":"{\\"path\\":"}}]}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"hello.txt\\"}"}}]},"finish_reason":"tool_calls"}]}

data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}

data: [DONE]

"""


@respx.mock
@pytest.mark.asyncio
async def test_stream_parses_text_tool_calls_and_usage() -> None:
    respx.post("http://fake.local/v1/chat/completions").mock(
        return_value=httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})
    )
    provider = _provider()
    events = [event async for event in provider.stream(_request(), CancelToken())]
    await provider.aclose()
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Hello"
    starts = [e for e in events if isinstance(e, ToolCallStart)]
    assert starts[0].id == "call_1"
    assert starts[0].name == "read_file"
    deltas = "".join(e.delta for e in events if isinstance(e, ToolCallArgumentsDelta))
    assert deltas == '{"path":"hello.txt"}'
    assert any(isinstance(e, UsageEvent) for e in events)
    assert events[-1].type == "finish"


@respx.mock
@pytest.mark.asyncio
async def test_non_stream_response() -> None:
    respx.post("http://fake.local/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "done",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"a"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )
    )
    provider = _provider()
    events = [event async for event in provider.stream(_request(stream=False), CancelToken())]
    await provider.aclose()
    assert any(isinstance(e, TextDelta) and e.text == "done" for e in events)
    assert any(isinstance(e, UsageEvent) for e in events)
    assert any(isinstance(e, FinishEvent) for e in events)


@respx.mock
@pytest.mark.asyncio
async def test_http_error_is_classified() -> None:
    respx.post("http://fake.local/v1/chat/completions").mock(
        return_value=httpx.Response(401, text="bad key")
    )
    provider = _provider()
    with pytest.raises(ProviderError) as info:
        [event async for event in provider.stream(_request(), CancelToken())]
    await provider.aclose()
    assert info.value.category == "auth"
    assert info.value.retryable is False


@respx.mock
@pytest.mark.asyncio
async def test_server_error_is_retryable() -> None:
    respx.post("http://fake.local/v1/chat/completions").mock(
        return_value=httpx.Response(503, text="busy")
    )
    provider = _provider()
    with pytest.raises(ProviderError) as info:
        [event async for event in provider.stream(_request(), CancelToken())]
    await provider.aclose()
    assert info.value.category == "server_error"
    assert info.value.retryable is True


class FlakyProvider(ModelProvider):
    def __init__(self, fail_first: bool = True) -> None:
        self.name = "flaky"
        self.capabilities = CAPS
        self.calls = 0
        self.fail_first = fail_first

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise ProviderError("transient", category="server_error", retryable=True)
        yield TextDelta(text="ok")
        yield FinishEvent()


@pytest.mark.asyncio
async def test_retry_before_first_event() -> None:
    inner = FlakyProvider()
    retries: list[int] = []
    provider = RetryingProvider(
        inner,
        policy=RetryPolicy(max_attempts=2, base_delay_s=0.0, jitter=0.0),
        on_retry=lambda _err, attempt, _delay: retries.append(attempt),
    )
    events = [event async for event in provider.stream(_request(), CancelToken())]
    assert inner.calls == 2
    assert retries == [1]
    assert any(isinstance(e, TextDelta) for e in events)


class PartialThenFailure(ModelProvider):
    def __init__(self) -> None:
        self.name = "partial"
        self.capabilities = CAPS
        self.calls = 0

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        yield TextDelta(text="partial")
        raise ProviderError("boom", category="server_error", retryable=True)


@pytest.mark.asyncio
async def test_no_retry_after_partial_output() -> None:
    inner = PartialThenFailure()
    provider = RetryingProvider(
        inner, policy=RetryPolicy(max_attempts=3, base_delay_s=0.0, jitter=0.0)
    )
    with pytest.raises(ProviderError):
        [event async for event in provider.stream(_request(), CancelToken())]
    assert inner.calls == 1


@respx.mock
@pytest.mark.asyncio
async def test_sse_error_payload_is_a_provider_error() -> None:
    respx.post("http://fake.local/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            text='data: {"error":{"message":"quota exceeded","code":"insufficient_quota"}}\n\n',
            headers={"content-type": "text/event-stream"},
        )
    )
    with pytest.raises(ProviderError) as caught:
        [event async for event in _provider().stream(_request(), CancelToken())]
    assert caught.value.category == "rate_limit"
    assert caught.value.retryable is True


@respx.mock
@pytest.mark.asyncio
async def test_truncated_sse_without_finish_is_malformed() -> None:
    respx.post("http://fake.local/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"half an answer"}}]}\n\n',
            headers={"content-type": "text/event-stream"},
        )
    )
    with pytest.raises(ProviderError) as caught:
        [event async for event in _provider().stream(_request(), CancelToken())]
    assert caught.value.category == "malformed_response"


@respx.mock
@pytest.mark.asyncio
async def test_http_400_context_length_is_classified() -> None:
    respx.post("http://fake.local/v1/chat/completions").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "message": "This model's maximum context length is 8192 tokens",
                    "code": "context_length_exceeded",
                }
            },
        )
    )
    with pytest.raises(ProviderError) as caught:
        [event async for event in _provider().stream(_request(), CancelToken())]
    assert caught.value.category == "context_length"


@respx.mock
@pytest.mark.asyncio
async def test_http_402_is_fallbackable_auth_error() -> None:
    respx.post("http://fake.local/v1/chat/completions").mock(
        return_value=httpx.Response(402, json={"error": {"message": "insufficient balance"}})
    )
    with pytest.raises(ProviderError) as caught:
        [event async for event in _provider().stream(_request(), CancelToken())]
    assert caught.value.category == "auth"


def test_message_conversion() -> None:
    message = AssistantMessage.with_tool_calls(
        "hi", [ToolCall(id="c1", name="read_file", arguments={"path": "a"})]
    )
    payload = message_to_openai(message)
    assert payload["tool_calls"][0]["function"]["name"] == "read_file"
    assert payload["content"] == "hi"
