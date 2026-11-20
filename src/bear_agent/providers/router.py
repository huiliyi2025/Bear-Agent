"""Provider routing: bounded retries, fallback and a per-route circuit breaker.

The router wraps one or more :class:`ModelProvider` instances (one per
``provider:model`` target). It is deliberately stricter than a plain retry
wrapper:

* Retries only happen **before the first streamed event**. Replaying a stream
  after text or tool-call deltas would duplicate output, so a failure after
  emission becomes a non-retryable ``partial_stream`` error and is never
  failed over.
* Fallback is attempted only for categories where another provider can help
  (rate limits, timeouts, network/server errors, auth and not-found). Requests
  rejected as invalid or with malformed tool calls are caller bugs, not
  provider outages, so they are surfaced immediately.
* ``context_length`` fallback is only allowed to a route whose context window
  is strictly larger than the estimated request.
* Every route has an independent circuit breaker. Consecutive failures open
  the circuit for a cooldown; after the cooldown a single half-open probe is
  allowed. Concurrent calls share health state, while ``last_route`` uses a
  :class:`~contextvars.ContextVar` so concurrent calls do not see each
  other's selection.

Tool-call arguments are streamed as deltas and only validated by
``ModelCaller`` after the stream has completed. A malformed tool-call payload
therefore happens *after* events were emitted; the router cannot replay or
fail over that request and reports the original failure. The runtime should
surface the resulting diagnostic instead of silently switching providers.

The router never persists or publishes anything by itself. ``on_event`` is an
observation hook; payloads are redacted before the callback is invoked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from bear_agent.config.models import ProviderConfig, Settings
from bear_agent.context.tokens import estimate_messages_tokens, estimate_tool_specs_tokens
from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.errors import ConfigError, ProviderError
from bear_agent.observability.trace import current_tracer, span
from bear_agent.providers.base import (
    ModelCapabilities,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    ProviderErrorEvent,
)
from bear_agent.providers.openai_compat import OpenAICompatProvider
from bear_agent.providers.retry import RetryPolicy, _sleep_or_cancel, classify_exception
from bear_agent.secrets.redaction import SecretRedactor

logger = logging.getLogger(__name__)

# Categories that are safe to retry against the same route before any event has
# been emitted. These match ``classify_exception`` in ``retry.py``.
RETRYABLE_CATEGORIES: frozenset[str] = frozenset(
    {"rate_limit", "timeout", "network", "server_error", "malformed_response"}
)

# Fallback is a superset: another provider can plausibly succeed where this one
# rejects the key, does not host the model, or has a smaller context window.
FALLBACK_CATEGORIES: frozenset[str] = RETRYABLE_CATEGORIES | frozenset(
    {"auth", "not_found", "context_length"}
)

PARTIAL_STREAM_CATEGORY = "partial_stream"
ALL_UNAVAILABLE_CATEGORY = "all_providers_unavailable"
HEALTH_CATEGORIES: frozenset[str] = FALLBACK_CATEGORIES | frozenset({PARTIAL_STREAM_CATEGORY})

# Event names mirror ``bear_agent.core.events.EventType``. The router keeps the
# callback string-typed so it stays usable without the observability layer.
EVENT_SELECTED = "provider.selected"
EVENT_RETRY = "provider.retry"
EVENT_FALLBACK = "provider.fallback"
EVENT_CIRCUIT_OPEN = "provider.circuit.open"
EVENT_FAILED = "provider.failed"

CircuitState = Literal["closed", "open", "half_open"]
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def _as_provider_error(error: BaseException) -> ProviderError:
    if isinstance(error, ProviderError):
        return error
    return classify_exception(error)


def should_retry(error: BaseException) -> bool:
    """Return True when the same route may be tried again before output."""

    provider_error = _as_provider_error(error)
    return provider_error.retryable and provider_error.category in RETRYABLE_CATEGORIES


def should_fallback(error: BaseException) -> bool:
    """Return True when switching to another route is safe and useful."""

    provider_error = _as_provider_error(error)
    return provider_error.category in FALLBACK_CATEGORIES


def estimate_request_tokens(request: ModelRequest) -> int:
    """Provider-neutral input-token estimate used for capability filtering."""

    return estimate_messages_tokens(request.messages) + estimate_tool_specs_tokens(request.tools)


@dataclass
class ProviderRoute:
    """One concrete ``provider:model`` target plus its policy and capabilities."""

    name: str
    model: str
    provider: ModelProvider
    retry_policy: RetryPolicy
    capabilities: ModelCapabilities

    @property
    def target(self) -> str:
        return f"{self.name}:{self.model}"


@dataclass
class ProviderAttempt:
    """A failed provider call, retained for diagnostics (never secrets)."""

    provider: str
    model: str
    attempt: int
    delay_s: float
    error_category: str
    emitted: bool

    @property
    def target(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class ProviderHealth:
    """Circuit breaker and counters for one route target."""

    target: str
    state: CircuitState = "closed"
    consecutive_failures: int = 0
    opened_until: float = 0.0
    last_error_category: str = ""
    last_error_redacted: str = ""
    successes: int = 0
    failures: int = 0
    total_attempts: int = 0
    probe_in_flight: bool = False


class FallbackProvider(ModelProvider):
    """Route a request across ordered providers with retry/fallback/circuit logic."""

    def __init__(
        self,
        routes: list[ProviderRoute],
        *,
        fallback_enabled: bool = True,
        circuit_breaker_failures: int = 3,
        circuit_breaker_cooldown_s: float = 60.0,
        max_fallbacks: int | None = None,
        on_event: EventCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not routes:
            raise ValueError("FallbackProvider requires at least one route")
        self.routes = list(routes)
        self.name = "router"
        self.capabilities = routes[0].capabilities
        self.fallback_enabled = fallback_enabled
        self.circuit_breaker_failures = max(0, circuit_breaker_failures)
        self.circuit_breaker_cooldown_s = max(0.0, circuit_breaker_cooldown_s)
        self.max_fallbacks = max_fallbacks
        self.on_event = on_event
        self.clock = clock
        # Public so the composition root can register resolved API key values
        # and attach build-time diagnostics without changing this signature.
        self.redactor = SecretRedactor()
        self.configuration_errors: list[dict[str, Any]] = []
        self._health: dict[str, ProviderHealth] = {
            route.target: ProviderHealth(target=route.target) for route in self.routes
        }
        self._recent_attempts: deque[ProviderAttempt] = deque(maxlen=50)
        self._last_route: ContextVar[tuple[str, str] | None] = ContextVar(
            f"bear_agent.provider_router.last_route.{id(self)}",
            default=None,
        )
        self._request_meta: ContextVar[dict[str, str] | None] = ContextVar(
            f"bear_agent.provider_router.request_meta.{id(self)}",
            default=None,
        )

    # ------------------------------------------------------------- inspection

    @property
    def last_route(self) -> tuple[str, str] | None:
        """Provider/model selected by the most recent successful call in this context."""

        return self._last_route.get()

    def status(self) -> list[dict[str, Any]]:
        """Redacted per-route health snapshot, suitable for CLI diagnostics."""

        now = self.clock()
        result: list[dict[str, Any]] = []
        for route in self.routes:
            health = self._health[route.target]
            state: CircuitState = health.state
            if state == "open" and now >= health.opened_until:
                state = "half_open"
            result.append(
                {
                    "provider": route.name,
                    "model": route.model,
                    "state": state,
                    "consecutive_failures": health.consecutive_failures,
                    "opened_until": health.opened_until,
                    "last_error_category": health.last_error_category,
                    "last_error": health.last_error_redacted,
                    "successes": health.successes,
                    "failures": health.failures,
                    "total_attempts": health.total_attempts,
                    "context_window": route.capabilities.context_window,
                    "supports_tools": route.capabilities.supports_tools,
                    "supports_streaming": route.capabilities.supports_streaming,
                }
            )
        return result

    def snapshot(self) -> dict[str, Any]:
        """Configuration + health summary with all registered secrets redacted."""

        return {
            "name": self.name,
            "fallback_enabled": self.fallback_enabled,
            "max_fallbacks": self.max_fallbacks,
            "circuit_breaker_failures": self.circuit_breaker_failures,
            "circuit_breaker_cooldown_s": self.circuit_breaker_cooldown_s,
            "routes": self.status(),
            "last_route": self.last_route,
            "configuration_errors": self.redactor.redact_obj(list(self.configuration_errors)),
            "recent_attempts": [
                {
                    "provider": attempt.provider,
                    "model": attempt.model,
                    "attempt": attempt.attempt,
                    "delay_s": attempt.delay_s,
                    "error_category": attempt.error_category,
                    "emitted": attempt.emitted,
                }
                for attempt in self._recent_attempts
            ],
        }

    async def aclose(self) -> None:
        """Close every distinct route provider instance exactly once."""

        seen: set[int] = set()
        first_error: Exception | None = None
        for route in self.routes:
            provider = route.provider
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            try:
                await provider.aclose()
            except Exception as exc:  # keep closing the remaining providers
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    # ----------------------------------------------------------------- stream

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        try:
            cancel.raise_if_cancelled()
        except BaseException:
            self._clear_probes()
            raise
        self._last_route.set(None)
        self._request_meta.set(dict(request.metadata))

        estimate = estimate_request_tokens(request)
        last_error: ProviderError | None = None
        failure_categories: list[str] = []
        attempts_made = 0
        fallbacks_used = 0
        fallback_from: str | None = None
        fallback_reason = "unavailable"
        # A context-length failure raises the bar for every subsequent route.
        context_length_floor: int | None = None

        for index, route in enumerate(self.routes):
            if index > 0:
                if not self.fallback_enabled:
                    break
                if self.max_fallbacks is not None and fallbacks_used >= self.max_fallbacks:
                    break

            skip_reason = self._capability_problem(route, request, estimate)
            if skip_reason is not None:
                if index == 0 and fallback_from is None:
                    fallback_from = route.target
                    fallback_reason = f"capability:{skip_reason}"
                continue

            if (
                context_length_floor is not None
                and index > 0
                and route.capabilities.context_window <= context_length_floor
            ):
                continue

            available, state = self._circuit_available(route)
            if not available:
                if index == 0 and fallback_from is None:
                    fallback_from = route.target
                    fallback_reason = "circuit_open"
                continue

            if state == "half_open":
                self._health[route.target].probe_in_flight = True

            used_model = self._used_model(route, index, request)
            route_request = self._request_for_route(route, used_model, request)

            if index > 0:
                fallbacks_used += 1
                async with span(
                    "provider.fallback",
                    "provider",
                    {
                        "from": fallback_from,
                        "to": route.name,
                        "model": route.model,
                        "category": fallback_reason,
                    },
                ):
                    await self._emit(
                        EVENT_FALLBACK,
                        {
                            "from": fallback_from,
                            "to": route.name,
                            "model": route.model,
                            "category": fallback_reason,
                        },
                    )

            attempts = max(1, route.retry_policy.max_attempts)
            delay_before_attempt = 0.0
            for attempt in range(1, attempts + 1):
                try:
                    cancel.raise_if_cancelled()
                except BaseException:
                    self._clear_probes()
                    raise
                attempts_made += 1
                health = self._health[route.target]
                health.total_attempts += 1
                emitted = False
                tracer = current_tracer()
                attempt_span = (
                    tracer.start_span(
                        "provider.attempt",
                        kind="provider",
                        attributes={
                            "provider": route.name,
                            "model": used_model,
                            "route_index": index,
                            "attempt": attempt,
                            "max_attempts": attempts,
                        },
                    )
                    if tracer is not None
                    else None
                )
                span_status = "ok"
                try:
                    try:
                        async for event in route.provider.stream(route_request, cancel):
                            if isinstance(event, ProviderErrorEvent):
                                raise ProviderError(
                                    event.message,
                                    category=event.category,
                                    retryable=event.retryable,
                                )
                            emitted = True
                            yield event
                    except (RunCancelled, asyncio.CancelledError, GeneratorExit):
                        span_status = "cancelled"
                        raise
                    except Exception as exc:
                        span_status = "error"
                        if attempt_span is not None:
                            attempt_span.set_attributes(
                                {
                                    "emitted": emitted,
                                    "error_category": _as_provider_error(exc).category,
                                }
                            )
                            attempt_span.fail(exc)
                        raise
                    finally:
                        if attempt_span is not None and tracer is not None:
                            if attempt_span.status is None:
                                attempt_span.set_status(span_status)
                            try:
                                await tracer.end_span(attempt_span)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                # Trace persistence must not alter provider behavior.
                                pass
                except (RunCancelled, asyncio.CancelledError):
                    self._clear_probe(health)
                    raise
                except GeneratorExit:
                    self._clear_probe(health)
                    raise
                except Exception as exc:
                    error = _as_provider_error(exc)
                    if emitted:
                        partial = ProviderError(
                            f"provider {route.target} failed after streaming began "
                            f"(category={error.category}); refusing to retry or fall back",
                            category=PARTIAL_STREAM_CATEGORY,
                            retryable=False,
                            status_code=error.status_code,
                        )
                        self._record_attempt(
                            route,
                            attempt=attempt,
                            delay_s=delay_before_attempt,
                            category=PARTIAL_STREAM_CATEGORY,
                            emitted=True,
                        )
                        failure_categories.append(PARTIAL_STREAM_CATEGORY)
                        await self._record_failure(route, partial)
                        await self._emit(
                            EVENT_FAILED,
                            {
                                "provider": route.name,
                                "model": route.model,
                                "category": PARTIAL_STREAM_CATEGORY,
                                "attempts": attempts_made,
                            },
                        )
                        raise partial from exc

                    self._record_attempt(
                        route,
                        attempt=attempt,
                        delay_s=delay_before_attempt,
                        category=error.category,
                        emitted=False,
                    )
                    if attempt < attempts and should_retry(error):
                        delay = route.retry_policy.delay_for(attempt, error)
                        async with span(
                            "provider.retry",
                            "provider",
                            {
                                "provider": route.name,
                                "model": route.model,
                                "attempt": attempt,
                                "next_attempt": attempt + 1,
                                "delay_s": delay,
                                "category": error.category,
                            },
                        ):
                            await self._emit(
                                EVENT_RETRY,
                                {
                                    "provider": route.name,
                                    "model": route.model,
                                    "attempt": attempt,
                                    "next_attempt": attempt + 1,
                                    "delay_s": delay,
                                    "category": error.category,
                                    "retry_after_s": error.retry_after_s,
                                },
                            )
                            try:
                                await _sleep_or_cancel(delay, cancel)
                            except BaseException:
                                self._clear_probes()
                                raise
                        delay_before_attempt = delay
                        continue

                    failure_categories.append(error.category)
                    last_error = self._redacted_error(error)
                    await self._record_failure(route, last_error)
                    break
                else:
                    self._record_success(route)
                    self._last_route.set((route.name, used_model))
                    await self._emit(
                        EVENT_SELECTED,
                        {"provider": route.name, "model": used_model},
                    )
                    return

            # The route failed (or yielded no success). Decide whether another
            # route may be tried. ``last_error`` is always set here.
            if last_error is None:  # defensive; a route cannot fail without one
                continue
            if not should_fallback(last_error):
                break
            if not self.fallback_enabled:
                break
            if index >= len(self.routes) - 1:
                break
            if self.max_fallbacks is not None and fallbacks_used >= self.max_fallbacks:
                break
            if last_error.category == "context_length":
                context_length_floor = estimate
            fallback_from = route.target
            fallback_reason = last_error.category

        if last_error is None:
            last_error = ProviderError(
                "all providers unavailable: every route was skipped by the circuit "
                "breaker or capability checks",
                category=ALL_UNAVAILABLE_CATEGORY,
                retryable=False,
            )
        await self._emit(
            EVENT_FAILED,
            {
                "category": last_error.category,
                "categories": list(failure_categories),
                "attempts": attempts_made,
                "routes": len(self.routes),
            },
        )
        self._raise_final(last_error)

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _used_model(route: ProviderRoute, index: int, request: ModelRequest) -> str:
        # Callers may pass a per-call model (summarizer/planner). Preserve it for
        # the primary route; fallback routes use their configured model.
        if index == 0 and request.model:
            return request.model
        return route.model

    @staticmethod
    def _request_for_route(
        route: ProviderRoute, used_model: str, request: ModelRequest
    ) -> ModelRequest:
        stream = request.stream and route.capabilities.supports_streaming
        max_output = request.max_output_tokens
        if route.capabilities.max_output_tokens > 0:
            max_output = min(max_output, route.capabilities.max_output_tokens)
        return request.model_copy(
            update={"model": used_model, "stream": stream, "max_output_tokens": max_output}
        )

    @staticmethod
    def _capability_problem(
        route: ProviderRoute, request: ModelRequest, estimate: int
    ) -> str | None:
        if request.tools and not route.capabilities.supports_tools:
            return "tools_unsupported"
        if estimate > route.capabilities.context_window:
            return "context_too_small"
        return None

    def _circuit_available(self, route: ProviderRoute) -> tuple[bool, CircuitState]:
        health = self._health[route.target]
        now = self.clock()
        if health.state == "open":
            if now < health.opened_until:
                return False, health.state
            health.state = "half_open"
        if health.state == "half_open" and health.probe_in_flight:
            return False, health.state
        return True, health.state

    @staticmethod
    def _clear_probe(health: ProviderHealth) -> None:
        health.probe_in_flight = False

    def _clear_probes(self) -> None:
        for health in self._health.values():
            health.probe_in_flight = False

    def _record_attempt(
        self,
        route: ProviderRoute,
        *,
        attempt: int,
        delay_s: float,
        category: str,
        emitted: bool,
    ) -> None:
        self._recent_attempts.append(
            ProviderAttempt(
                provider=route.name,
                model=route.model,
                attempt=attempt,
                delay_s=delay_s,
                error_category=category,
                emitted=emitted,
            )
        )

    def _record_success(self, route: ProviderRoute) -> None:
        health = self._health[route.target]
        health.successes += 1
        health.consecutive_failures = 0
        health.state = "closed"
        health.opened_until = 0.0
        health.probe_in_flight = False

    async def _record_failure(self, route: ProviderRoute, error: ProviderError) -> None:
        health = self._health[route.target]
        health.failures += 1
        # Client/request errors (bad schema, invalid request) say nothing about
        # route health; counting them would let a few oversized prompts trip the
        # circuit and take a healthy provider offline.
        health_related = error.category in HEALTH_CATEGORIES
        if health_related:
            health.consecutive_failures += 1
        health.last_error_category = error.category
        health.last_error_redacted = self.redactor.redact(error.message)
        health.probe_in_flight = False
        should_open = health_related and (
            health.state == "half_open"
            or (
                self.circuit_breaker_failures > 0
                and health.consecutive_failures >= self.circuit_breaker_failures
            )
        )
        if not should_open:
            return
        health.state = "open"
        health.opened_until = self.clock() + self.circuit_breaker_cooldown_s
        await self._emit(
            EVENT_CIRCUIT_OPEN,
            {
                "provider": route.name,
                "model": route.model,
                "consecutive_failures": health.consecutive_failures,
                "cooldown_s": self.circuit_breaker_cooldown_s,
                "opened_until": health.opened_until,
                "category": error.category,
            },
        )

    def _redacted_error(self, error: ProviderError) -> ProviderError:
        message = self.redactor.redact(error.message)
        if message == error.message:
            return error
        return ProviderError(
            message=message,
            category=error.category,
            retryable=error.retryable,
            status_code=error.status_code,
            retry_after_s=error.retry_after_s,
        )

    def _raise_final(self, error: ProviderError) -> NoReturn:
        raise error

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        meta = self._request_meta.get() or {}
        enriched = dict(payload)
        for key in ("session_id", "run_id", "turn_id"):
            value = meta.get(key)
            if value:
                enriched.setdefault(key, value)
        safe_payload = self.redactor.redact_obj(enriched)
        try:
            await self.on_event(event, safe_payload)
        except asyncio.CancelledError:
            self._clear_probes()
            raise
        except Exception:  # observation must never break model routing
            logger.debug("provider router event callback failed", exc_info=True)


# --------------------------------------------------------------------- build


def _retry_policy(config: ProviderConfig) -> RetryPolicy:
    # Mirrors create_provider/RetryingProvider: ``max_retries`` is the attempt
    # budget, and values <= 0 mean a single attempt.
    return RetryPolicy(
        max_attempts=max(1, config.max_retries),
        base_delay_s=config.retry_base_delay_s,
        max_delay_s=config.retry_max_delay_s,
        jitter=config.retry_jitter,
    )


def _capabilities(settings: Settings, config: ProviderConfig) -> ModelCapabilities:
    context_window = (
        config.context_window
        if config.context_window is not None
        else settings.agent.context_window
    )
    return ModelCapabilities(
        context_window=context_window,
        max_output_tokens=settings.agent.max_output_tokens,
        supports_tools=config.supports_tools,
        supports_streaming=config.supports_streaming,
    )


def _parse_fallback_spec(spec: str) -> tuple[str, str | None] | None:
    text = spec.strip()
    if not text:
        return None
    name, separator, model = text.partition(":")
    name = name.strip()
    model = model.strip()
    if not name:
        return None
    if not separator or not model:
        return name, None
    return name, model


def _primary_api_key(config: ProviderConfig, name: str, secret_store: object | None) -> str:
    try:
        key = config.resolve_api_key(secret_store)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"cannot resolve API key for provider {name!r}: {exc}") from exc
    if not config.is_local and not key:
        env_hint = config.api_key_env or "BEAR_API_KEY"
        raise ConfigError(
            f"provider {name!r} has no API key. Set {env_hint}, add api_key to the "
            "config, or point base_url at a local endpoint."
        )
    return key


def build_fallback_provider(
    settings: Settings,
    *,
    secret_store: object | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    on_event: EventCallback | None = None,
) -> FallbackProvider:
    """Build a :class:`FallbackProvider` from agent/provider configuration.

    The primary route must be usable: an unknown provider, missing model or
    missing API key raises :class:`ConfigError` just like ``create_provider``.
    Invalid fallback targets are recorded in ``configuration_errors`` and
    skipped so a typo never blocks the primary provider.
    """

    name = provider_name or settings.agent.default_provider
    try:
        primary_config = settings.provider(name)
    except KeyError as exc:
        raise ConfigError(str(exc)) from exc

    primary_model = model or settings.agent.default_model
    if not primary_model:
        raise ConfigError("no model configured; set agent.default_model or --model")

    redactor = SecretRedactor()
    primary_key = _primary_api_key(primary_config, name, secret_store)
    if primary_key:
        redactor.register(f"provider:{name}", primary_key)

    primary_capabilities = _capabilities(settings, primary_config)
    routes = [
        ProviderRoute(
            name=name,
            model=primary_model,
            provider=OpenAICompatProvider(
                name,
                primary_config,
                model=primary_model,
                capabilities=primary_capabilities,
                secret_store=secret_store,
            ),
            retry_policy=_retry_policy(primary_config),
            capabilities=primary_capabilities,
        )
    ]
    configuration_errors: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = {(name, primary_model)}

    if settings.agent.provider_fallback_enabled:
        for raw_spec in settings.agent.fallback_models:
            parsed = _parse_fallback_spec(raw_spec)
            if parsed is None:
                continue
            fallback_name, fallback_model = parsed
            try:
                fallback_config = settings.providers[fallback_name]
            except KeyError:
                configuration_errors.append(
                    {
                        "target": raw_spec.strip(),
                        "provider": fallback_name,
                        "model": fallback_model,
                        "error": f"unknown provider {fallback_name!r}",
                    }
                )
                continue
            if fallback_model is None:
                fallback_model = (
                    fallback_config.models[0]
                    if fallback_config.models
                    else settings.agent.default_model
                )
            if not fallback_model:
                configuration_errors.append(
                    {
                        "target": raw_spec.strip(),
                        "provider": fallback_name,
                        "model": "",
                        "error": "no model configured for fallback route",
                    }
                )
                continue
            if (fallback_name, fallback_model) in seen:
                continue
            seen.add((fallback_name, fallback_model))

            try:
                fallback_key = fallback_config.resolve_api_key(secret_store)
            except Exception as exc:
                configuration_errors.append(
                    {
                        "target": f"{fallback_name}:{fallback_model}",
                        "provider": fallback_name,
                        "model": fallback_model,
                        "error": redactor.redact(f"cannot resolve API key: {exc}"),
                    }
                )
                continue
            if not fallback_config.is_local and not fallback_key:
                configuration_errors.append(
                    {
                        "target": f"{fallback_name}:{fallback_model}",
                        "provider": fallback_name,
                        "model": fallback_model,
                        "error": "no API key configured; route skipped",
                    }
                )
                continue
            if fallback_key:
                redactor.register(f"provider:{fallback_name}", fallback_key)

            capabilities = _capabilities(settings, fallback_config)
            routes.append(
                ProviderRoute(
                    name=fallback_name,
                    model=fallback_model,
                    provider=OpenAICompatProvider(
                        fallback_name,
                        fallback_config,
                        model=fallback_model,
                        capabilities=capabilities,
                        secret_store=secret_store,
                    ),
                    retry_policy=_retry_policy(fallback_config),
                    capabilities=capabilities,
                )
            )

    router = FallbackProvider(
        routes,
        fallback_enabled=settings.agent.provider_fallback_enabled,
        circuit_breaker_failures=settings.agent.circuit_breaker_failures,
        circuit_breaker_cooldown_s=settings.agent.circuit_breaker_cooldown_s,
        on_event=on_event,
    )
    router.redactor = redactor
    router.configuration_errors = configuration_errors
    return router
