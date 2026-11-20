"""Ambient trace context.

Core modules call :func:`span` instead of holding a tracer dependency. When no
trace is active (maintenance commands, unit tests, tracing disabled) every
helper is a no-op. ContextVars propagate into asyncio tasks and HTTP/subprocess
callbacks created while the span is current, so concurrent sessions do not see
each other's parent spans.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

from bear_agent.observability.trace.models import SpanHandle

if TYPE_CHECKING:
    from bear_agent.observability.trace.service import TraceService

_current_tracer: ContextVar[TraceService | None] = ContextVar(
    "bear_agent_trace_service", default=None
)
_current_span: ContextVar[SpanHandle | None] = ContextVar("bear_agent_current_span", default=None)


def current_tracer() -> TraceService | None:
    return _current_tracer.get()


def current_span() -> SpanHandle | None:
    return _current_span.get()


def current_span_ids() -> tuple[str | None, str | None]:
    """Return ``(span_id, parent_span_id)`` for event/message linkage."""
    handle = _current_span.get()
    if handle is None:
        return None, None
    return handle.span_id, handle.parent_span_id


def set_tracer(tracer: TraceService | None) -> Token[TraceService | None]:
    return _current_tracer.set(tracer)


def reset_tracer(token: Token[TraceService | None]) -> None:
    _current_tracer.reset(token)


def set_current_span(handle: SpanHandle | None) -> Token[SpanHandle | None]:
    return _current_span.set(handle)


def reset_current_span(token: Token[SpanHandle | None]) -> None:
    _current_span.reset(token)


def begin_span(
    name: str,
    kind: str = "internal",
    attributes: dict[str, Any] | None = None,
) -> SpanHandle | None:
    tracer = _current_tracer.get()
    if tracer is None:
        return None
    return tracer.start_span(name, kind=kind, attributes=attributes)


async def end_span(
    handle: SpanHandle | None,
    *,
    status: str | None = None,
    error: BaseException | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    if handle is None:
        return
    if error is not None:
        handle.fail(error)
    elif status is not None:
        handle.set_status(status)
    if attributes:
        handle.set_attributes(attributes)
    tracer = _current_tracer.get()
    if tracer is None:
        handle.ended = True
        return
    await tracer.end_span(handle)


@asynccontextmanager
async def span(
    name: str,
    kind: str = "internal",
    attributes: dict[str, Any] | None = None,
) -> AsyncIterator[SpanHandle | None]:
    """No-op when tracing is inactive, otherwise an async span scope."""
    handle = begin_span(name, kind=kind, attributes=attributes)
    if handle is None:
        yield None
        return
    token = set_current_span(handle)
    try:
        yield handle
    except BaseException as exc:
        handle.fail(exc)
        raise
    else:
        if handle.status is None:
            handle.set_status("ok")
    finally:
        # Async-generator finalization may run in a different Context. Trace
        # bookkeeping must not replace the original exception.
        with contextlib.suppress(ValueError):
            reset_current_span(token)
        try:
            await end_span(handle)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Observation failures are dropped; see ADR 0021.
            pass
