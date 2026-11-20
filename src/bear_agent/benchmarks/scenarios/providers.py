"""Provider, retry, fallback and circuit-breaker benchmarks."""

from __future__ import annotations

from typing import Any, ClassVar

from bear_agent.benchmarks.fixtures import (
    FlakyProvider,
    ScriptedProvider,
    text_script,
    tool_script,
    usage,
)
from bear_agent.benchmarks.models import BenchmarkContext, BenchmarkSample, BenchmarkSkipped
from bear_agent.core.cancel import CancelToken
from bear_agent.core.messages import SystemMessage, UserMessage
from bear_agent.core.usage import TokenUsage
from bear_agent.providers.base import (
    FinishEvent,
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    TextDelta,
)
from bear_agent.providers.retry import RetryPolicy
from bear_agent.providers.router import (
    FallbackProvider,
    ProviderRoute,
    build_fallback_provider,
)
from bear_agent.runtime.model_caller import ModelCaller
from bear_agent.secrets.store import secret_store_from_config

_CAPABILITIES = ModelCapabilities(context_window=32_000, max_output_tokens=1_000)


class _ProviderScenario:
    category = "provider"
    kind = "scripted"
    requires_network = False
    default_params: ClassVar[dict[str, Any]] = {}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        return None

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        return None

    def _scripts_needed(self, ctx: BenchmarkContext) -> int:
        return max(1_000, int(ctx.params.get("count", 100)) + 10)

    def _request(self, *, tools: list[Any] | None = None) -> ModelRequest:
        return ModelRequest(
            model="bench-model",
            messages=[
                SystemMessage.of("Benchmark request."),
                UserMessage.of("Return a short answer."),
            ],
            tools=tools or [],
            temperature=0.0,
            max_output_tokens=64,
            stream=False,
        )

    def _route(
        self,
        name: str,
        provider: Any,
        *,
        model: str = "bench-model",
        attempts: int = 1,
    ) -> ProviderRoute:
        return ProviderRoute(
            name=name,
            model=model,
            provider=provider,
            retry_policy=RetryPolicy(
                max_attempts=attempts,
                base_delay_s=0.0,
                max_delay_s=0.0,
                jitter=0.0,
            ),
            capabilities=_CAPABILITIES,
        )


class ProviderRequestScenario(_ProviderScenario):
    id = "provider.request"
    name = "Provider request (scripted)"
    description = "Consume one complete provider stream through ModelCaller."
    default_params = {"count": 100}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        provider = ScriptedProvider(
            [text_script("answer") for _ in range(self._scripts_needed(ctx))]
        )
        ctx.state["caller"] = ModelCaller(provider)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        caller: ModelCaller = ctx.state["caller"]
        count = int(ctx.params.get("count", 100))
        tokens = 0
        for _ in range(count):
            response = await caller.call(self._request(), CancelToken())
            tokens += response.usage.total_tokens
        return BenchmarkSample(operations=count, tokens=tokens)


class ProviderStreamScenario(_ProviderScenario):
    id = "provider.stream"
    name = "Provider stream assembly"
    description = "Assemble 200 text deltas into one assistant message."
    default_params = {"count": 20, "deltas": 200}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        deltas = int(ctx.params.get("deltas", 200))
        script: list[ModelEvent] = [TextDelta(text="x") for _ in range(deltas)]
        script.extend([usage(), FinishEvent(reason="stop")])
        provider = ScriptedProvider([script for _ in range(self._scripts_needed(ctx))])
        ctx.state["caller"] = ModelCaller(provider)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        caller: ModelCaller = ctx.state["caller"]
        count = int(ctx.params.get("count", 20))
        tokens = 0
        for _ in range(count):
            response = await caller.call(self._request(), CancelToken())
            tokens += response.usage.total_tokens
        return BenchmarkSample(operations=count, tokens=tokens)


class ProviderToolStreamScenario(_ProviderScenario):
    id = "provider.tool_stream"
    name = "Provider tool-call stream"
    description = "Assemble streamed tool-call argument fragments."
    default_params = {"count": 50}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        script = tool_script("call_1", "read_file", {"path": "hello.txt"})
        provider = ScriptedProvider([script for _ in range(self._scripts_needed(ctx))])
        ctx.state["caller"] = ModelCaller(provider)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        caller: ModelCaller = ctx.state["caller"]
        count = int(ctx.params.get("count", 50))
        tokens = 0
        for _ in range(count):
            response = await caller.call(self._request(), CancelToken())
            tokens += response.usage.total_tokens
            if not response.message.tool_calls():
                raise RuntimeError("tool call was not assembled")
        return BenchmarkSample(operations=count, tokens=tokens)


class ProviderRetryScenario(_ProviderScenario):
    id = "provider.retry"
    name = "Provider bounded retry"
    description = "Recover from two rate-limit failures with zero backoff."
    default_params = {"count": 20}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        provider = FlakyProvider(failures=2, category="rate_limit")
        router = FallbackProvider(
            [self._route("primary", provider, attempts=3)],
            fallback_enabled=False,
        )
        ctx.state.update(caller=ModelCaller(router), router=router, provider=provider)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        caller: ModelCaller = ctx.state["caller"]
        provider: FlakyProvider = ctx.state["provider"]
        count = int(ctx.params.get("count", 20))
        tokens = 0
        for _ in range(count):
            response = await caller.call(self._request(), CancelToken())
            tokens += response.usage.total_tokens
            provider.failures = 2
        return BenchmarkSample(
            operations=count, tokens=tokens, metadata={"provider_calls": provider.calls}
        )

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        router = ctx.state.get("router")
        if router is not None:
            await router.aclose()


class ProviderFallbackScenario(_ProviderScenario):
    id = "provider.fallback"
    name = "Provider fallback"
    description = "Fall back from a failing route to a healthy second route."
    default_params = {"count": 20}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        failing = FlakyProvider(failures=10_000, category="rate_limit")
        healthy = ScriptedProvider(
            [text_script("answer") for _ in range(self._scripts_needed(ctx))]
        )
        router = FallbackProvider(
            [
                self._route("primary", failing, attempts=1),
                self._route("secondary", healthy, attempts=1),
            ],
            fallback_enabled=True,
        )
        ctx.state.update(caller=ModelCaller(router), router=router)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        caller: ModelCaller = ctx.state["caller"]
        router: FallbackProvider = ctx.state["router"]
        count = int(ctx.params.get("count", 20))
        tokens = 0
        for _ in range(count):
            response = await caller.call(self._request(), CancelToken())
            tokens += response.usage.total_tokens
        route = router.last_route
        return BenchmarkSample(
            operations=count,
            tokens=tokens,
            metadata={"last_route": route[0] if route else None},
        )

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        router = ctx.state.get("router")
        if router is not None:
            await router.aclose()


class ProviderCircuitScenario(_ProviderScenario):
    id = "provider.circuit"
    name = "Circuit breaker"
    description = "Open a circuit after failures and skip the broken route."
    default_params = {"count": 6}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        failing = FlakyProvider(failures=10_000, category="server_error")
        healthy = ScriptedProvider(
            [text_script("answer") for _ in range(self._scripts_needed(ctx))]
        )
        router = FallbackProvider(
            [
                self._route("primary", failing, attempts=1),
                self._route("secondary", healthy, attempts=1),
            ],
            fallback_enabled=True,
            circuit_breaker_failures=1,
            circuit_breaker_cooldown_s=3600.0,
        )
        ctx.state.update(caller=ModelCaller(router), router=router)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        caller: ModelCaller = ctx.state["caller"]
        router: FallbackProvider = ctx.state["router"]
        count = int(ctx.params.get("count", 6))
        tokens = 0
        for _ in range(count):
            response = await caller.call(self._request(), CancelToken())
            tokens += response.usage.total_tokens
        states = {item["provider"]: item["state"] for item in router.status()}
        if states.get("primary") not in {"open", "half_open"}:
            raise RuntimeError(f"circuit did not open: {states}")
        return BenchmarkSample(
            operations=count,
            tokens=tokens,
            metadata={"states": states},
        )

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        router = ctx.state.get("router")
        if router is not None:
            await router.aclose()


class ProviderRealRequestScenario(_ProviderScenario):
    id = "provider.real_request"
    name = "Provider request (real)"
    description = "One small real provider request; requires --allow-network."
    kind = "real"
    requires_network = True
    default_params = {"count": 1}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        try:
            ctx.settings.validate_ready(
                secret_store=secret_store_from_config(ctx.settings.secrets.backend)
            )
        except Exception as exc:
            raise BenchmarkSkipped(f"real provider is not configured: {exc}") from exc
        router = build_fallback_provider(
            ctx.settings,
            secret_store=secret_store_from_config(ctx.settings.secrets.backend),
        )
        ctx.state.update(router=router, caller=ModelCaller(router))

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        caller: ModelCaller = ctx.state["caller"]
        router: FallbackProvider = ctx.state["router"]
        count = int(ctx.params.get("count", 1))
        usage_total = TokenUsage()
        cost = 0.0
        currencies: set[str] = set()
        pricing_missing = 0
        for _ in range(count):
            response = await caller.call(
                ModelRequest(
                    model=ctx.settings.agent.default_model,
                    messages=[
                        SystemMessage.of("You are a benchmark health check."),
                        UserMessage.of("Reply with the single word: pong"),
                    ],
                    tools=[],
                    temperature=0.0,
                    max_output_tokens=8,
                    stream=False,
                ),
                CancelToken(),
            )
            usage_total = usage_total + response.usage
            price = ctx.settings.observability.price_for(
                response.provider_name or ctx.settings.agent.default_provider,
                response.model or ctx.settings.agent.default_model,
            )
            if price is None:
                pricing_missing += 1
            else:
                cost += _cost(price, response.usage)
                currencies.add(price.currency)
        route = router.last_route
        return BenchmarkSample(
            operations=count,
            tokens=usage_total.total_tokens,
            cost=cost,
            metadata={
                "route": f"{route[0]}/{route[1]}" if route else None,
                "currencies": sorted(currencies),
                "pricing_missing": pricing_missing,
            },
            note="real network/model call; latency and price are not reproducible",
        )

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        router = ctx.state.get("router")
        if router is not None:
            await router.aclose()


def _cost(price: Any, usage: TokenUsage) -> float:
    uncached = max(0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens)
    return (
        uncached / 1_000_000 * float(price.input_per_million)
        + usage.cache_read_tokens / 1_000_000 * float(price.cache_read_per_million)
        + usage.cache_write_tokens / 1_000_000 * float(price.cache_write_per_million)
        + usage.output_tokens / 1_000_000 * float(price.output_per_million)
    )


def provider_scenarios() -> list[Any]:
    return [
        ProviderRequestScenario(),
        ProviderStreamScenario(),
        ProviderToolStreamScenario(),
        ProviderRetryScenario(),
        ProviderFallbackScenario(),
        ProviderCircuitScenario(),
        ProviderRealRequestScenario(),
    ]
