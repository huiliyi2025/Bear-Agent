"""Trace service and ambient span helpers."""

from bear_agent.observability.trace.context import (
    begin_span,
    current_span,
    current_span_ids,
    current_tracer,
    end_span,
    span,
)
from bear_agent.observability.trace.models import SpanHandle, TraceRecord, TraceStatus
from bear_agent.observability.trace.service import TraceScope, TraceService

__all__ = [
    "SpanHandle",
    "TraceRecord",
    "TraceScope",
    "TraceService",
    "TraceStatus",
    "begin_span",
    "current_span",
    "current_span_ids",
    "current_tracer",
    "end_span",
    "span",
]
