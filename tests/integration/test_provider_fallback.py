"""Provider fallback integration with ModelCaller (scripted and HTTP-level).

The scripted tests exercise the real ``FallbackProvider`` + ``ModelCaller``
pair. The final test uses respx to mock two OpenAI-compatible endpoints, so no
real network is required or verified.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from bear_agent.config.models import AgentConfig, ProviderConfig, Settings, StorageConfig
from bear_agent.core.cancel import CancelToken
from bear_agent.core.errors import ProviderError
from bear_agent.core.messages import UserMessage
from bear_agent.core.usage import TokenUsage
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
from bear_agent.providers.retry import RetryPolicy
from bear_agent.providers.router import (
    PARTIAL_STREAM_CATEGORY,
    FallbackProvider,
    ProviderRoute,
    build_fallback_provider,
)
from bear_agent.runtime.model_caller import ModelCaller

CAPS = ModelCapabilities(context_window=32_000, max_output_tokens=1_000)


class ScriptedRoute(ModelProvider):
    def __init__(
        self,
        name: str,
        scripts: list[list[ModelEvent | BaseException]],
        *,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        self.name = name
        self.capabilities = capabilities or CAPS
        self.scripts = [list(script) for script in scripts]
        self.calls = 0
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        self.requests.append(request)
        cancel.raise_if_cancelled()
        if not self.scripts:
            raise AssertionError(f"{self.name} called more times than scripted")
        script = self.scripts.pop(0)
        for item in script:
            if isinstance(item, BaseException):
                raise item
            cancel.raise_if_cancelled()
            await asyncio.sleep(0)
            yield item


def _route(
    name: str,
    provider: ModelProvider,
    model: str,
    *,
    max_attempts: int = 1,
) -> ProviderRoute:
    return ProviderRoute(
        name=name,
        model=model,
        provider=provider,
        retry_policy=RetryPolicy(max_attempts=max_attempts, base_delay_s=0.0, jitter=0.0),
        capabilities=provider.capabilities,
    )


def _request(model: str = "primary-model") -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=[UserMessage.of("hi")],
        stream=True,
    )


async def test_model_caller_falls_back_and_returns_second_route_text() -> None:
    primary = ScriptedRoute(
        "primary",
        [[ProviderError("bad key", category="auth", retryable=False)]],
    )
    backup = ScriptedRoute(
        "backup",
        [
            [
                TextDelta(text="from "),
                TextDelta(text="backup"),
                UsageEvent(usage=TokenUsage(input_tokens=7, output_tokens=2)),
                FinishEvent(reason="stop"),
            ]
        ],
    )
    router = FallbackProvider(
        [
            _route("primary", primary, "primary-model"),
            _route("backup", backup, "backup-model"),
        ]
    )
    caller = ModelCaller(router)

    response = await caller.call(_request(), CancelToken())

    assert response.text == "from backup"
    assert response.finish_reason == "stop"
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 2
    assert primary.calls == 1
    assert backup.calls == 1
    assert backup.requests[0].model == "backup-model"
    assert router.last_route == ("backup", "backup-model")


async def test_model_caller_raises_after_partial_text_without_fallback() -> None:
    primary = ScriptedRoute(
        "primary",
        [
            [
                TextDelta(text="partial answer"),
                ProviderError("connection reset", category="network", retryable=True),
            ]
        ],
    )
    backup = ScriptedRoute("backup", [[TextDelta(text="must not appear"), FinishEvent()]])
    router = FallbackProvider(
        [
            _route("primary", primary, "primary-model", max_attempts=3),
            _route("backup", backup, "backup-model"),
        ]
    )
    caller = ModelCaller(router)
    chunks: list[str] = []

    async def on_text(delta: str) -> None:
        chunks.append(delta)

    with pytest.raises(ProviderError) as info:
        await caller.call(_request(), CancelToken(), on_text_delta=on_text)

    assert info.value.category == PARTIAL_STREAM_CATEGORY
    assert chunks == ["partial answer"]
    assert primary.calls == 1
    assert backup.calls == 0


async def test_model_caller_does_not_fallback_after_partial_tool_call() -> None:
    primary = ScriptedRoute(
        "primary",
        [
            [
                ToolCallStart(index=0, id="call_1", name="read_file"),
                ToolCallArgumentsDelta(index=0, delta='{"path": "a'),
                ProviderError("stream broke", category="server_error", retryable=True),
            ]
        ],
    )
    backup = ScriptedRoute("backup", [[TextDelta(text="must not appear"), FinishEvent()]])
    router = FallbackProvider(
        [
            _route("primary", primary, "primary-model", max_attempts=2),
            _route("backup", backup, "backup-model"),
        ]
    )
    caller = ModelCaller(router)

    with pytest.raises(ProviderError) as info:
        await caller.call(_request(), CancelToken())

    assert info.value.category == PARTIAL_STREAM_CATEGORY
    assert backup.calls == 0


def _http_settings() -> Settings:
    return Settings(
        providers={
            "primary": ProviderConfig(
                base_url="http://primary.local/v1",
                api_key="primary-key",
                models=["primary-model"],
                max_retries=1,
            ),
            "backup": ProviderConfig(
                base_url="http://backup.local/v1",
                api_key="backup-key",
                models=["backup-model"],
                max_retries=1,
            ),
        },
        agent=AgentConfig(
            default_provider="primary",
            default_model="primary-model",
            fallback_models=["backup"],
            context_window=32_000,
            max_output_tokens=1_000,
        ),
        storage=StorageConfig(),
    )


SSE = """\
data: {"choices":[{"delta":{"content":"backup "}}]}

data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}

data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":3}}

data: [DONE]

"""


@respx.mock
async def test_http_400_context_length_falls_back() -> None:
    primary_http = respx.post("http://primary.local/v1/chat/completions").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "message": "maximum context length exceeded",
                    "code": "context_length_exceeded",
                }
            },
        )
    )
    backup_http = respx.post("http://backup.local/v1/chat/completions").mock(
        return_value=httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})
    )
    router = build_fallback_provider(_http_settings())
    try:
        response = await ModelCaller(router).call(_request(), CancelToken())
        assert response.text == "backup answer"
        assert primary_http.called and backup_http.called
        assert router.last_route == ("backup", "backup-model")
        assert router.status()[0]["last_error_category"] == "context_length"
    finally:
        await router.aclose()


@respx.mock
async def test_http_level_503_falls_back_to_second_endpoint() -> None:
    primary_http = respx.post("http://primary.local/v1/chat/completions").mock(
        return_value=httpx.Response(503, text="busy")
    )
    backup_http = respx.post("http://backup.local/v1/chat/completions").mock(
        return_value=httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})
    )
    router = build_fallback_provider(_http_settings())
    try:
        caller = ModelCaller(router)
        response = await caller.call(_request(), CancelToken())

        assert response.text == "backup answer"
        assert response.usage.input_tokens == 9
        assert primary_http.called
        assert backup_http.called
        sent = json.loads(backup_http.calls.last.request.content)
        assert sent["model"] == "backup-model"
        assert router.last_route == ("backup", "backup-model")
        statuses = router.status()
        assert statuses[0]["failures"] == 1
        assert statuses[0]["last_error_category"] == "server_error"
        assert statuses[1]["successes"] == 1
    finally:
        await router.aclose()
