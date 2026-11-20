"""Unit tests for provider routing, retry, fallback and circuit breaking."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from bear_agent.config.models import AgentConfig, ProviderConfig, Settings, StorageConfig
from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.errors import ConfigError, ProviderError
from bear_agent.core.messages import UserMessage
from bear_agent.core.tool_spec import ToolSpec
from bear_agent.providers import router as router_module
from bear_agent.providers.base import (
    FinishEvent,
    ModelCapabilities,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStart,
)
from bear_agent.providers.retry import RetryPolicy
from bear_agent.providers.router import (
    ALL_UNAVAILABLE_CATEGORY,
    PARTIAL_STREAM_CATEGORY,
    FallbackProvider,
    ProviderRoute,
    build_fallback_provider,
    estimate_request_tokens,
    should_fallback,
    should_retry,
)

CAPS = ModelCapabilities(context_window=32_000, max_output_tokens=1_000)


class ScriptedProvider(ModelProvider):
    """Each call consumes one script; items may be events or raised exceptions."""

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
        self.close_calls = 0

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        self.requests.append(request)
        cancel.raise_if_cancelled()
        if not self.scripts:
            yield TextDelta(text="script exhausted")
            yield FinishEvent()
            return
        script = self.scripts.pop(0)
        for item in script:
            if isinstance(item, BaseException):
                raise item
            cancel.raise_if_cancelled()
            await asyncio.sleep(0)
            yield item

    async def aclose(self) -> None:
        self.close_calls += 1


class AlwaysFailProvider(ModelProvider):
    def __init__(
        self,
        name: str,
        error: ProviderError,
        *,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        self.name = name
        self.capabilities = capabilities or CAPS
        self.error = error
        self.calls = 0

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        raise self.error
        yield FinishEvent()  # pragma: no cover - makes this an async generator


class EventSink:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, payload: dict[str, Any]) -> None:
        self.items.append((name, payload))

    def names(self) -> list[str]:
        return [name for name, _ in self.items]

    def payloads(self, name: str) -> list[dict[str, Any]]:
        return [payload for event, payload in self.items if event == name]


def _route(
    name: str,
    provider: ModelProvider,
    *,
    model: str = "model",
    capabilities: ModelCapabilities | None = None,
    max_attempts: int = 1,
    base_delay_s: float = 0.0,
    max_delay_s: float = 8.0,
    jitter: float = 0.0,
) -> ProviderRoute:
    caps = capabilities or provider.capabilities
    return ProviderRoute(
        name=name,
        model=model,
        provider=provider,
        retry_policy=RetryPolicy(
            max_attempts=max_attempts,
            base_delay_s=base_delay_s,
            max_delay_s=max_delay_s,
            jitter=jitter,
        ),
        capabilities=caps,
    )


def _request(
    *,
    stream: bool = True,
    tools: list[ToolSpec] | None = None,
    text: str = "hi",
    model: str = "model",
    metadata: dict[str, str] | None = None,
) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=[UserMessage.of(text)],
        tools=tools or [],
        stream=stream,
        metadata=metadata or {},
    )


async def _collect(
    router: FallbackProvider,
    request: ModelRequest | None = None,
    cancel: CancelToken | None = None,
) -> list[ModelEvent]:
    token = cancel or CancelToken()
    return [event async for event in router.stream(request or _request(), token)]


def _text(events: list[ModelEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------- classification


def test_should_retry_and_should_fallback_categories() -> None:
    for category in ("rate_limit", "timeout", "network", "server_error", "malformed_response"):
        error = ProviderError("x", category=category, retryable=True)
        assert should_retry(error)
        assert should_fallback(error)

    for category in ("auth", "not_found", "context_length"):
        error = ProviderError("x", category=category, retryable=False)
        assert not should_retry(error)
        assert should_fallback(error)

    for category in ("invalid_request", "tool_format", "partial_stream", "unknown"):
        error = ProviderError("x", category=category, retryable=False)
        assert not should_retry(error)
        assert not should_fallback(error)


def test_estimate_request_tokens_includes_tools() -> None:
    plain = estimate_request_tokens(_request(text="hello"))
    with_tools = estimate_request_tokens(
        _request(
            text="hello",
            tools=[ToolSpec(name="read_file", description="Read a file", parameters={})],
        )
    )
    assert with_tools > plain


# ---------------------------------------------------------------------- retry


async def test_retry_before_first_event_then_success() -> None:
    sink = EventSink()
    primary = ScriptedProvider(
        "primary",
        [
            [ProviderError("busy", category="server_error", retryable=True)],
            [TextDelta(text="ok"), FinishEvent()],
        ],
    )
    router = FallbackProvider(
        [_route("primary", primary, model="primary-model", max_attempts=2)],
        on_event=sink,
    )

    events = await _collect(router, _request(model="primary-model"))

    assert primary.calls == 2
    assert _text(events) == "ok"
    assert "provider.retry" in sink.names()
    retry_payload = sink.payloads("provider.retry")[0]
    assert retry_payload["attempt"] == 1
    assert retry_payload["category"] == "server_error"
    assert sink.payloads("provider.selected")[0]["model"] == "primary-model"
    assert router.last_route == ("primary", "primary-model")
    assert router.status()[0]["successes"] == 1
    assert router.status()[0]["total_attempts"] == 2


async def test_retry_honors_retry_after_and_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float, cancel: CancelToken) -> None:
        delays.append(delay)
        cancel.raise_if_cancelled()

    monkeypatch.setattr(router_module, "_sleep_or_cancel", fake_sleep)
    primary = ScriptedProvider(
        "primary",
        [
            [
                ProviderError(
                    "slow down",
                    category="rate_limit",
                    retryable=True,
                    retry_after_s=2.5,
                )
            ],
            [ProviderError("boom", category="server_error", retryable=True)],
            [ProviderError("boom again", category="server_error", retryable=True)],
        ],
    )
    router = FallbackProvider(
        [
            _route(
                "primary",
                primary,
                max_attempts=3,
                base_delay_s=1.0,
                max_delay_s=8.0,
                jitter=0.0,
            )
        ]
    )

    with pytest.raises(ProviderError) as info:
        await _collect(router)

    assert info.value.category == "server_error"
    assert primary.calls == 3
    assert delays == [2.5, 2.0]


async def test_raw_exception_is_classified_before_fallback() -> None:
    sink = EventSink()
    primary = ScriptedProvider("primary", [[OSError("connection refused")]])
    backup = ScriptedProvider("backup", [[TextDelta(text="backup"), FinishEvent()]])
    router = FallbackProvider(
        [_route("primary", primary), _route("backup", backup)],
        on_event=sink,
    )

    events = await _collect(router)

    assert _text(events) == "backup"
    assert primary.calls == 1
    assert backup.calls == 1
    assert sink.payloads("provider.fallback")[0]["category"] == "network"


async def test_retry_budget_is_bounded() -> None:
    primary = AlwaysFailProvider(
        "primary",
        ProviderError("down", category="server_error", retryable=True),
    )
    router = FallbackProvider([_route("primary", primary, max_attempts=2)])

    with pytest.raises(ProviderError):
        await _collect(router)

    assert primary.calls == 2
    assert router.status()[0]["failures"] == 1
    assert router.status()[0]["total_attempts"] == 2


async def test_non_retryable_error_is_not_retried() -> None:
    primary = ScriptedProvider(
        "primary",
        [[ProviderError("bad request", category="invalid_request")]],
    )
    backup = ScriptedProvider("backup", [[TextDelta(text="nope"), FinishEvent()]])
    router = FallbackProvider(
        [_route("primary", primary, max_attempts=3), _route("backup", backup)],
        fallback_enabled=True,
    )

    with pytest.raises(ProviderError) as info:
        await _collect(router)

    assert info.value.category == "invalid_request"
    assert primary.calls == 1
    assert backup.calls == 0


# -------------------------------------------------------------- partial stream


async def test_no_retry_after_partial_text() -> None:
    primary = ScriptedProvider(
        "primary",
        [
            [
                TextDelta(text="partial"),
                ProviderError("stream broke", category="server_error", retryable=True),
            ]
        ],
    )
    backup = ScriptedProvider("backup", [[TextDelta(text="backup"), FinishEvent()]])
    router = FallbackProvider(
        [
            _route("primary", primary, max_attempts=3),
            _route("backup", backup),
        ]
    )

    seen: list[ModelEvent] = []
    with pytest.raises(ProviderError) as info:
        async for event in router.stream(_request(), CancelToken()):
            seen.append(event)

    assert info.value.category == PARTIAL_STREAM_CATEGORY
    assert info.value.retryable is False
    assert _text(seen) == "partial"
    assert primary.calls == 1
    assert backup.calls == 0
    assert router.status()[0]["last_error_category"] == PARTIAL_STREAM_CATEGORY


async def test_no_fallback_after_partial_tool_call() -> None:
    primary = ScriptedProvider(
        "primary",
        [
            [
                ToolCallStart(index=0, id="call_1", name="read_file"),
                ToolCallArgumentsDelta(index=0, delta='{"path":'),
                ProviderError("stream broke", category="network", retryable=True),
            ]
        ],
    )
    backup = ScriptedProvider("backup", [[TextDelta(text="backup"), FinishEvent()]])
    router = FallbackProvider(
        [
            _route("primary", primary, max_attempts=3),
            _route("backup", backup),
        ]
    )

    with pytest.raises(ProviderError) as info:
        await _collect(router)

    assert info.value.category == PARTIAL_STREAM_CATEGORY
    assert primary.calls == 1
    assert backup.calls == 0


# ------------------------------------------------------------------- fallback


async def test_fallback_before_first_event_uses_route_model() -> None:
    sink = EventSink()
    primary = ScriptedProvider(
        "primary",
        [[ProviderError("bad key", category="auth", retryable=False)]],
    )
    backup = ScriptedProvider(
        "backup",
        [[TextDelta(text="from backup"), FinishEvent()]],
    )
    router = FallbackProvider(
        [
            _route("primary", primary, model="primary-model"),
            _route("backup", backup, model="backup-model"),
        ],
        on_event=sink,
    )

    events = await _collect(router, _request(model="primary-model"))

    assert _text(events) == "from backup"
    assert primary.calls == 1
    assert backup.calls == 1
    assert backup.requests[0].model == "backup-model"
    assert router.last_route == ("backup", "backup-model")
    fallback = sink.payloads("provider.fallback")[0]
    assert fallback["from"] == "primary:primary-model"
    assert fallback["to"] == "backup"
    assert fallback["category"] == "auth"
    statuses = router.status()
    assert statuses[0]["failures"] == 1
    assert statuses[1]["successes"] == 1


@pytest.mark.parametrize("category", ["invalid_request", "tool_format"])
async def test_no_fallback_for_request_errors(category: str) -> None:
    primary = ScriptedProvider("primary", [[ProviderError("bad request", category=category)]])
    backup = ScriptedProvider("backup", [[TextDelta(text="nope"), FinishEvent()]])
    router = FallbackProvider([_route("primary", primary), _route("backup", backup)])

    with pytest.raises(ProviderError) as info:
        await _collect(router)

    assert info.value.category == category
    assert backup.calls == 0


async def test_fallback_disabled_keeps_single_route_behavior() -> None:
    primary = ScriptedProvider(
        "primary",
        [
            [ProviderError("busy", category="server_error", retryable=True)],
            [TextDelta(text="ok"), FinishEvent()],
        ],
    )
    backup = ScriptedProvider("backup", [[TextDelta(text="nope"), FinishEvent()]])
    router = FallbackProvider(
        [
            _route("primary", primary, max_attempts=2),
            _route("backup", backup),
        ],
        fallback_enabled=False,
    )

    events = await _collect(router)

    assert primary.calls == 2
    assert backup.calls == 0
    assert _text(events) == "ok"


async def test_max_fallbacks_bounds_fan_out() -> None:
    providers = [
        AlwaysFailProvider(name, ProviderError("bad key", category="auth", retryable=False))
        for name in ("p1", "p2", "p3")
    ]
    router = FallbackProvider(
        [_route(provider.name, provider) for provider in providers],
        max_fallbacks=1,
    )

    with pytest.raises(ProviderError) as info:
        await _collect(router)

    assert info.value.category == "auth"
    assert [provider.calls for provider in providers] == [1, 1, 0]


async def test_last_error_is_raised_when_all_routes_fail() -> None:
    first = AlwaysFailProvider(
        "first", ProviderError("first down", category="server_error", retryable=True)
    )
    second = AlwaysFailProvider(
        "second", ProviderError("second down", category="timeout", retryable=True)
    )
    router = FallbackProvider([_route("first", first), _route("second", second)])

    with pytest.raises(ProviderError) as info:
        await _collect(router)

    assert info.value.category == "timeout"
    assert "second down" in str(info.value)


# ---------------------------------------------------------- context/capability


async def test_context_length_fallback_requires_larger_window() -> None:
    request = _request(text="x" * 400, model="primary-model")
    estimate = estimate_request_tokens(request)
    primary_caps = ModelCapabilities(context_window=estimate + 1_000, max_output_tokens=1_000)
    equal_caps = ModelCapabilities(context_window=estimate, max_output_tokens=1_000)
    large_caps = ModelCapabilities(context_window=estimate + 2_000, max_output_tokens=1_000)
    primary = ScriptedProvider(
        "primary",
        [[ProviderError("too long", category="context_length")]],
        capabilities=primary_caps,
    )
    equal_route = ScriptedProvider(
        "equal", [[TextDelta(text="should not run"), FinishEvent()]], capabilities=equal_caps
    )
    large_route = ScriptedProvider(
        "large", [[TextDelta(text="bigger window"), FinishEvent()]], capabilities=large_caps
    )
    router = FallbackProvider(
        [
            _route("primary", primary, model="primary-model", capabilities=primary_caps),
            _route("equal", equal_route, capabilities=equal_caps),
            _route("large", large_route, capabilities=large_caps),
        ]
    )

    events = await _collect(router, request)

    assert _text(events) == "bigger window"
    assert equal_route.calls == 0
    assert large_route.calls == 1


async def test_tools_unsupported_route_is_skipped() -> None:
    sink = EventSink()
    no_tools = ModelCapabilities(
        context_window=32_000, max_output_tokens=1_000, supports_tools=False
    )
    primary = ScriptedProvider("primary", [[TextDelta(text="nope")]], capabilities=no_tools)
    backup = ScriptedProvider("backup", [[TextDelta(text="tool-capable"), FinishEvent()]])
    request = _request(tools=[ToolSpec(name="read_file", description="Read", parameters={})])
    router = FallbackProvider(
        [
            _route("primary", primary, capabilities=no_tools),
            _route("backup", backup),
        ],
        on_event=sink,
    )

    events = await _collect(router, request)

    assert _text(events) == "tool-capable"
    assert primary.calls == 0
    assert backup.calls == 1
    assert sink.payloads("provider.fallback")[0]["category"] == "capability:tools_unsupported"


async def test_streaming_unsupported_route_gets_non_streaming_request() -> None:
    no_stream = ModelCapabilities(
        context_window=32_000, max_output_tokens=1_000, supports_streaming=False
    )
    primary = ScriptedProvider(
        "primary", [[TextDelta(text="plain"), FinishEvent()]], capabilities=no_stream
    )
    router = FallbackProvider([_route("primary", primary, capabilities=no_stream)])

    events = await _collect(router, _request(stream=True))

    assert _text(events) == "plain"
    assert primary.requests[0].stream is False


async def test_output_budget_is_capped_and_empty_request_model_uses_route_model() -> None:
    caps = ModelCapabilities(context_window=32_000, max_output_tokens=64)
    primary = ScriptedProvider(
        "primary", [[TextDelta(text="ok"), FinishEvent()]], capabilities=caps
    )
    router = FallbackProvider([_route("primary", primary, model="route-model", capabilities=caps)])
    request = _request(model="", stream=True).model_copy(update={"max_output_tokens": 4_096})

    events = await _collect(router, request)

    assert _text(events) == "ok"
    sent = primary.requests[0]
    assert sent.model == "route-model"
    assert sent.max_output_tokens == 64


async def test_event_callback_failure_does_not_break_stream() -> None:
    async def broken_sink(_event: str, _payload: dict[str, Any]) -> None:
        raise RuntimeError("observer bug")

    primary = ScriptedProvider("primary", [[TextDelta(text="still works"), FinishEvent()]])
    router = FallbackProvider([_route("primary", primary)], on_event=broken_sink)

    events = await _collect(router)

    assert _text(events) == "still works"
    assert router.last_route == ("primary", "model")


async def test_too_small_context_window_route_is_skipped() -> None:
    small = ModelCapabilities(context_window=10, max_output_tokens=1_000)
    primary = ScriptedProvider("primary", [[TextDelta(text="nope")]], capabilities=small)
    backup = ScriptedProvider("backup", [[TextDelta(text="fits"), FinishEvent()]])
    router = FallbackProvider(
        [_route("primary", primary, capabilities=small), _route("backup", backup)]
    )

    events = await _collect(router, _request(text="a" * 400))

    assert _text(events) == "fits"
    assert primary.calls == 0
    assert backup.calls == 1


# ------------------------------------------------------------ circuit breaker


async def test_circuit_opens_and_recovers_through_half_open_probe() -> None:
    sink = EventSink()
    clock = FakeClock()
    primary = ScriptedProvider(
        "primary",
        [
            [ProviderError("bad key", category="auth")],
            [ProviderError("bad key", category="auth")],
            [TextDelta(text="recovered"), FinishEvent()],
            [TextDelta(text="still good"), FinishEvent()],
        ],
    )
    router = FallbackProvider(
        [_route("primary", primary)],
        circuit_breaker_failures=2,
        circuit_breaker_cooldown_s=10.0,
        on_event=sink,
        clock=clock,
    )

    for _ in range(2):
        with pytest.raises(ProviderError):
            await _collect(router)
    assert primary.calls == 2
    assert router.status()[0]["state"] == "open"
    assert "provider.circuit.open" in sink.names()

    with pytest.raises(ProviderError) as info:
        await _collect(router)
    assert info.value.category == ALL_UNAVAILABLE_CATEGORY
    assert primary.calls == 2

    clock.now = 11.0
    events = await _collect(router)
    assert _text(events) == "recovered"
    assert primary.calls == 3
    assert router.status()[0]["state"] == "closed"
    assert router.status()[0]["successes"] == 1

    events = await _collect(router)
    assert _text(events) == "still good"
    assert primary.calls == 4


async def test_failed_half_open_probe_reopens_circuit() -> None:
    clock = FakeClock()
    primary = ScriptedProvider(
        "primary",
        [
            [ProviderError("bad key", category="auth")],
            [ProviderError("bad key", category="auth")],
            [ProviderError("still bad", category="auth")],
        ],
    )
    router = FallbackProvider(
        [_route("primary", primary)],
        circuit_breaker_failures=2,
        circuit_breaker_cooldown_s=10.0,
        clock=clock,
    )
    for _ in range(2):
        with pytest.raises(ProviderError):
            await _collect(router)

    clock.now = 11.0
    with pytest.raises(ProviderError) as info:
        await _collect(router)
    assert info.value.category == "auth"
    assert primary.calls == 3
    assert router.status()[0]["state"] == "open"

    with pytest.raises(ProviderError) as info:
        await _collect(router)
    assert info.value.category == ALL_UNAVAILABLE_CATEGORY
    assert primary.calls == 3


async def test_cancellation_during_half_open_retry_clears_probe() -> None:
    clock = FakeClock()
    primary = ScriptedProvider(
        "primary",
        [
            [ProviderError("bad key", category="auth")],
            [ProviderError("transient", category="server_error", retryable=True)],
            [TextDelta(text="recovered"), FinishEvent()],
        ],
    )
    cancelled = False

    async def cancel_on_retry(name: str, payload: dict[str, Any]) -> None:
        nonlocal cancelled
        if name == "provider.retry" and not cancelled:
            cancelled = True
            raise asyncio.CancelledError()

    router = FallbackProvider(
        [_route("primary", primary, max_attempts=2)],
        circuit_breaker_failures=1,
        circuit_breaker_cooldown_s=10.0,
        on_event=cancel_on_retry,
        clock=clock,
    )
    with pytest.raises(ProviderError):
        await _collect(router)
    assert router.status()[0]["state"] == "open"

    clock.now = 11.0
    with pytest.raises(asyncio.CancelledError):
        await _collect(router)
    assert cancelled is True
    status = router.status()[0]
    assert status["state"] == "half_open"
    assert router._health["primary:model"].probe_in_flight is False

    clock.now = 12.0
    events = await _collect(router)
    assert _text(events) == "recovered"
    assert router.status()[0]["state"] == "closed"


async def test_client_errors_do_not_open_the_circuit() -> None:
    primary = AlwaysFailProvider(
        "primary",
        ProviderError("bad schema", category="invalid_request", retryable=False),
    )
    router = FallbackProvider(
        [_route("primary", primary)],
        circuit_breaker_failures=1,
        circuit_breaker_cooldown_s=60.0,
    )
    for _ in range(2):
        with pytest.raises(ProviderError):
            await _collect(router)
    assert primary.calls == 2
    assert router.status()[0]["state"] == "closed"
    assert router.status()[0]["consecutive_failures"] == 0


async def test_all_open_circuits_raise_unavailable_without_calls() -> None:
    first = AlwaysFailProvider("first", ProviderError("bad key", category="auth", retryable=False))
    second = AlwaysFailProvider(
        "second", ProviderError("bad key", category="auth", retryable=False)
    )
    router = FallbackProvider(
        [_route("first", first), _route("second", second)],
        circuit_breaker_failures=1,
        circuit_breaker_cooldown_s=60.0,
    )

    with pytest.raises(ProviderError) as info:
        await _collect(router)
    assert info.value.category == "auth"
    assert (first.calls, second.calls) == (1, 1)

    with pytest.raises(ProviderError) as info:
        await _collect(router)
    assert info.value.category == ALL_UNAVAILABLE_CATEGORY
    assert (first.calls, second.calls) == (1, 1)


# -------------------------------------------------------------- cancellation


async def test_cancelled_token_aborts_before_any_provider_call() -> None:
    primary = ScriptedProvider("primary", [[TextDelta(text="nope"), FinishEvent()]])
    router = FallbackProvider([_route("primary", primary)])
    cancel = CancelToken()
    cancel.cancel()

    with pytest.raises(RunCancelled):
        await _collect(router, cancel=cancel)

    assert primary.calls == 0


async def test_cancellation_during_backoff_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancel_sleep(_delay: float, _cancel: CancelToken) -> None:
        raise RunCancelled("cancelled during backoff")

    monkeypatch.setattr(router_module, "_sleep_or_cancel", cancel_sleep)
    primary = ScriptedProvider(
        "primary",
        [[ProviderError("busy", category="server_error", retryable=True)]],
    )
    backup = ScriptedProvider("backup", [[TextDelta(text="nope"), FinishEvent()]])
    router = FallbackProvider(
        [_route("primary", primary, max_attempts=2), _route("backup", backup)]
    )

    with pytest.raises(RunCancelled):
        await _collect(router)

    assert primary.calls == 1
    assert backup.calls == 0


# ------------------------------------------------------------------ isolation


async def test_last_route_is_isolated_between_concurrent_calls() -> None:
    class SelectiveProvider(ModelProvider):
        def __init__(self) -> None:
            self.name = "primary"
            self.capabilities = CAPS
            self.calls = 0

        async def stream(
            self, request: ModelRequest, cancel: CancelToken
        ) -> AsyncIterator[ModelEvent]:
            self.calls += 1
            await asyncio.sleep(0)
            if request.metadata.get("call") == "b":
                raise ProviderError("bad key", category="auth", retryable=False)
            yield TextDelta(text="primary answer")
            yield FinishEvent()

    primary = SelectiveProvider()
    backup = ScriptedProvider("backup", [[TextDelta(text="backup answer"), FinishEvent()]])
    router = FallbackProvider(
        [_route("primary", primary, model="shared"), _route("backup", backup, model="shared")]
    )

    async def run(call_id: str) -> tuple[str, tuple[str, str] | None]:
        events = await _collect(router, _request(model="shared", metadata={"call": call_id}))
        return _text(events), router.last_route

    result_a, result_b = await asyncio.gather(run("a"), run("b"))

    assert result_a == ("primary answer", ("primary", "shared"))
    assert result_b == ("backup answer", ("backup", "shared"))
    assert router.last_route is None


# -------------------------------------------------------------- introspection


async def test_status_and_snapshot_redact_secrets() -> None:
    primary = AlwaysFailProvider(
        "primary",
        ProviderError("token sk-abcdefghijklmnopqrstuv rejected", category="auth"),
    )
    router = FallbackProvider([_route("primary", primary)], circuit_breaker_failures=99)
    router.redactor.register("api", "super-secret-value")
    primary.error = ProviderError(
        "super-secret-value and sk-abcdefghijklmnopqrstuv rejected",
        category="auth",
    )

    with pytest.raises(ProviderError) as info:
        await _collect(router)

    assert "super-secret-value" not in str(info.value)
    assert "sk-abcdefghijklmnopqrstuv" not in str(info.value)
    blob = json.dumps(router.status()) + json.dumps(router.snapshot())
    assert "super-secret-value" not in blob
    assert "sk-abcdefghijklmnopqrstuv" not in blob
    assert "[REDACTED]" in blob
    status = router.status()[0]
    assert status["total_attempts"] == 1
    assert status["failures"] == 1


async def test_aclose_deduplicates_shared_provider_instances() -> None:
    shared = ScriptedProvider("shared", [])
    router = FallbackProvider([_route("one", shared), _route("two", shared)])

    await router.aclose()

    assert shared.close_calls == 1


# ------------------------------------------------------------ configuration


def _settings(
    providers: dict[str, ProviderConfig],
    *,
    fallback_models: list[str] | None = None,
    fallback_enabled: bool = True,
    default_provider: str = "primary",
    default_model: str = "primary-model",
) -> Settings:
    return Settings(
        providers=providers,
        agent=AgentConfig(
            default_provider=default_provider,
            default_model=default_model,
            fallback_models=fallback_models or [],
            provider_fallback_enabled=fallback_enabled,
            context_window=8_192,
            max_output_tokens=512,
        ),
        storage=StorageConfig(),
    )


def test_build_parses_fallbacks_dedupes_and_derives_capabilities() -> None:
    settings = _settings(
        {
            "primary": ProviderConfig(
                base_url="http://primary.local/v1",
                api_key="pk",
                max_retries=3,
                retry_base_delay_s=0.25,
                retry_max_delay_s=4.0,
                retry_jitter=0.1,
                context_window=64_000,
            ),
            "backup": ProviderConfig(
                base_url="http://backup.local/v1",
                api_key="bk",
                models=["backup-model"],
                max_retries=1,
            ),
            "local": ProviderConfig(
                base_url="http://127.0.0.1:11434/v1",
                models=["local-model"],
            ),
        },
        fallback_models=[
            "backup",
            "local:local-model",
            "backup:backup-model",
            "backup",
        ],
    )

    router = build_fallback_provider(settings)

    assert [route.target for route in router.routes] == [
        "primary:primary-model",
        "backup:backup-model",
        "local:local-model",
    ]
    primary_route = router.routes[0]
    assert primary_route.retry_policy.max_attempts == 3
    assert primary_route.retry_policy.base_delay_s == 0.25
    assert primary_route.capabilities.context_window == 64_000
    assert primary_route.capabilities.max_output_tokens == 512
    backup_route = router.routes[1]
    assert backup_route.provider.capabilities.context_window == 8_192
    assert backup_route.retry_policy.max_attempts == 1
    assert router.fallback_enabled is True
    assert router.configuration_errors == []
    snapshot = json.dumps(router.snapshot())
    assert "pk" not in snapshot
    assert "bk" not in snapshot


def test_build_skips_unknown_fallback_provider_defensively() -> None:
    # Settings normally rejects unknown fallback providers during validation.
    # model_construct keeps that impossible state so the builder's defensive
    # skip-and-record path is covered.
    settings = Settings.model_construct(
        providers={"primary": ProviderConfig(base_url="http://p.local/v1", api_key="pk")},
        agent=AgentConfig(
            default_provider="primary",
            default_model="primary-model",
            fallback_models=["missing:model"],
        ),
        storage=StorageConfig(),
    )

    router = build_fallback_provider(settings)

    assert [route.target for route in router.routes] == ["primary:primary-model"]
    assert router.configuration_errors[0]["provider"] == "missing"
    assert "unknown provider" in router.configuration_errors[0]["error"]


def test_build_provider_only_uses_models_then_default_model() -> None:
    settings = _settings(
        {
            "primary": ProviderConfig(base_url="http://p.local/v1", api_key="pk"),
            "with_models": ProviderConfig(
                base_url="http://w.local/v1", api_key="wk", models=["w-model"]
            ),
            "without_models": ProviderConfig(base_url="http://n.local/v1", api_key="nk"),
        },
        fallback_models=["with_models", "without_models"],
    )

    router = build_fallback_provider(settings)

    assert [route.target for route in router.routes] == [
        "primary:primary-model",
        "with_models:w-model",
        "without_models:primary-model",
    ]


def test_build_missing_primary_key_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BEAR_TEST_MISSING_KEY", raising=False)
    settings = _settings(
        {
            "primary": ProviderConfig(
                base_url="https://api.example.com/v1",
                api_key_env="BEAR_TEST_MISSING_KEY",
            )
        }
    )

    with pytest.raises(ConfigError, match="no API key"):
        build_fallback_provider(settings)


def test_build_skips_fallback_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEAR_TEST_MISSING_FALLBACK_KEY", raising=False)
    settings = _settings(
        {
            "primary": ProviderConfig(base_url="http://p.local/v1", api_key="pk"),
            "backup": ProviderConfig(
                base_url="https://api.example.com/v1",
                api_key_env="BEAR_TEST_MISSING_FALLBACK_KEY",
                models=["backup-model"],
            ),
        },
        fallback_models=["backup:backup-model"],
    )

    router = build_fallback_provider(settings)

    assert [route.target for route in router.routes] == ["primary:primary-model"]
    assert len(router.configuration_errors) == 1
    error = router.configuration_errors[0]
    assert error["provider"] == "backup"
    assert "no API key" in error["error"]
    assert "backup:backup-model" in json.dumps(router.snapshot())


def test_build_fallback_disabled_returns_primary_only() -> None:
    settings = _settings(
        {
            "primary": ProviderConfig(base_url="http://p.local/v1", api_key="pk"),
            "backup": ProviderConfig(base_url="http://b.local/v1", api_key="bk", models=["b"]),
        },
        fallback_models=["backup"],
        fallback_enabled=False,
    )

    router = build_fallback_provider(settings)

    assert [route.target for route in router.routes] == ["primary:primary-model"]
    assert router.fallback_enabled is False


def test_build_unknown_primary_provider_raises() -> None:
    settings = _settings(
        {"other": ProviderConfig(base_url="http://o.local/v1", api_key="ok")},
        default_provider="primary",
    )

    with pytest.raises(ConfigError, match="unknown provider"):
        build_fallback_provider(settings)
