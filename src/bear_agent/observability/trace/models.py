"""Trace data model.

A trace is one diagnosable operation (usually one Runtime run). Spans are timed
operations inside it. Events are *not* duplicated here: the existing event log
rows carry ``span_id`` and are joined for display/export. Existing model call,
tool call, task and checkpoint IDs are stored as span attributes so trace data
never becomes a second source of truth for execution state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

SpanKind = Literal[
    "run",
    "plan_run",
    "turn",
    "task",
    "plan",
    "model",
    "provider",
    "tool",
    "mcp",
    "hook",
    "context",
    "checkpoint",
    "recovery",
    "fork",
    "benchmark",
    "internal",
]
TraceStatus = Literal["running", "ok", "error", "cancelled", "incomplete"]


@dataclass(slots=True)
class TraceRecord:
    trace_id: str
    session_id: str
    run_id: str | None
    task_id: str | None
    name: str
    kind: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    span_count: int = 0
    dropped_spans: int = 0
    incomplete: bool = False
    sampled: bool = True
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "span_count": self.span_count,
            "dropped_spans": self.dropped_spans,
            "incomplete": self.incomplete,
            "sampled": self.sampled,
            "schema_version": self.schema_version,
        }


@dataclass(slots=True)
class SpanHandle:
    """Mutable handle for one in-flight span.

    The service owns persistence. Callers only annotate; they can never write
    arbitrary rows or bypass redaction/bounding.
    """

    span_id: str
    trace_id: str
    parent_span_id: str | None
    name: str
    kind: str
    started_at: datetime
    sequence: int = 0
    ended_at: datetime | None = None
    duration_ms: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    ended: bool = False

    def set_attribute(self, key: str, value: Any) -> None:
        if not self.ended:
            self.attributes[key] = value

    def set_attributes(self, values: dict[str, Any]) -> None:
        if not self.ended:
            self.attributes.update(values)

    def set_status(self, status: str) -> None:
        if not self.ended:
            self.status = status

    def fail(self, error: BaseException) -> None:
        if self.ended:
            return
        self.status = "cancelled" if _is_cancelled(error) else "error"
        self.error_type = type(error).__name__
        try:
            # Keep the full text; TraceService.safe_error redacts it before
            # truncation so a secret ending at the boundary still matches.
            self.error_message = str(error)
        except Exception:
            self.error_message = "<unprintable error>"


def _is_cancelled(error: BaseException) -> bool:
    import asyncio

    from bear_agent.core.cancel import RunCancelled

    return isinstance(error, (asyncio.CancelledError, RunCancelled, GeneratorExit))
