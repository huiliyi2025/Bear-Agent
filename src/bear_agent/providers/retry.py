"""Provider error classification and bounded retries.

Retries are only attempted before the first streamed event: replaying a stream
after partial output would duplicate text or tool calls. Provider-level retries
share a single budget (``max_attempts``), preventing the classic
``provider_retries x agent_retries`` multiplication.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx

from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.errors import ProviderError
from bear_agent.providers.base import ModelEvent, ModelProvider, ModelRequest

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def classify_exception(exc: BaseException) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retry_after = _retry_after_seconds(exc.response)
        if status in RETRYABLE_STATUS:
            category = "rate_limit" if status == 429 else "server_error"
            return ProviderError(
                f"HTTP {status}: {_safe_body(exc.response)}",
                category=category,
                retryable=True,
                status_code=status,
                retry_after_s=retry_after,
            )
        if status in (401, 403):
            category = "auth"
        elif status == 400:
            category = "invalid_request"
        elif status == 404:
            category = "not_found"
        elif status == 413:
            category = "context_length"
        else:
            category = "http_error"
        return ProviderError(
            f"HTTP {status}: {_safe_body(exc.response)}",
            category=category,
            retryable=False,
            status_code=status,
        )
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return ProviderError("provider request timed out", category="timeout", retryable=True)
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
        return ProviderError(f"network error: {exc}", category="network", retryable=True)
    if isinstance(exc, OSError):
        return ProviderError(f"network error: {exc}", category="network", retryable=True)
    return ProviderError(f"{type(exc).__name__}: {exc}", category="unknown", retryable=False)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _safe_body(response: httpx.Response) -> str:
    try:
        return response.text[:500]
    except Exception:
        return "<unreadable response body>"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    jitter: float = 0.2

    def delay_for(self, attempt: int, error: ProviderError) -> float:
        if error.retry_after_s is not None:
            return min(self.max_delay_s, error.retry_after_s)
        raw = min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1)))
        return float(raw * (1.0 + random.uniform(-self.jitter, self.jitter)))


class RetryingProvider(ModelProvider):
    def __init__(
        self,
        inner: ModelProvider,
        policy: RetryPolicy | None = None,
        on_retry: Callable[[ProviderError, int, float], None] | None = None,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.capabilities = inner.capabilities
        self.policy = policy or RetryPolicy()
        self.on_retry = on_retry

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        attempts = max(1, self.policy.max_attempts)
        for attempt in range(1, attempts + 1):
            cancel.raise_if_cancelled()
            emitted = False
            try:
                async for event in self.inner.stream(request, cancel):
                    emitted = True
                    yield event
                return
            except ProviderError as provider_error:
                if emitted or not provider_error.retryable or attempt >= attempts:
                    raise
                delay = self.policy.delay_for(attempt, provider_error)
                if self.on_retry:
                    self.on_retry(provider_error, attempt, delay)
                await _sleep_or_cancel(delay, cancel)
            except Exception as exc:
                classified = classify_exception(exc)
                if emitted or not classified.retryable or attempt >= attempts:
                    raise classified from exc
                delay = self.policy.delay_for(attempt, classified)
                if self.on_retry:
                    self.on_retry(classified, attempt, delay)
                await _sleep_or_cancel(delay, cancel)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def _sleep_or_cancel(delay_s: float, cancel: CancelToken) -> None:
    try:
        await asyncio.wait_for(cancel.wait(), timeout=delay_s)
    except TimeoutError:
        return
    raise RunCancelled("run cancelled during provider backoff")
