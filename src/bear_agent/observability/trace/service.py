"""Trace lifecycle and storage.

Design rules:

* Trace writes are **not** part of the security or correctness contract. Any
  failure is logged and dropped; it never fails a run, a tool, an approval or a
  recovery.
* A trace row is inserted when the root operation starts, which makes a crashed
  process visible as a ``running``/``incomplete`` trace instead of silence.
* Spans are buffered and flushed in batches so SQLite lock pressure stays low.
* Only bounded, redacted attributes are accepted. Prompts, tool arguments and
  tool results are never recorded by this layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from bear_agent import __version__
from bear_agent.config.models import ObservabilityConfig
from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.ids import new_id
from bear_agent.observability.trace.context import (
    reset_current_span,
    reset_tracer,
    set_current_span,
    set_tracer,
)
from bear_agent.observability.trace.models import (
    SpanHandle,
    TraceRecord,
    TraceStatus,
)
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.storage.database import Database
from bear_agent.storage.observability_repositories import TraceRepository

logger = logging.getLogger(__name__)

MAX_ATTRIBUTE_STRING = 1_024
MAX_ERROR_CHARS = 1_000
MAX_EVENTS_EXPORT = 20_000


@dataclass(slots=True)
class TraceScope:
    """Handle for the root span of a trace."""

    trace_id: str
    session_id: str
    run_id: str | None
    task_id: str | None
    name: str
    kind: str
    started_at: datetime
    _service: TraceService
    _root: SpanHandle
    _attributes: dict[str, Any] = field(default_factory=dict)
    finished: bool = False

    def set_attribute(self, key: str, value: Any) -> None:
        if self.finished:
            return
        self._root.set_attribute(key, value)
        self._attributes[key] = value

    def set_attributes(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.set_attribute(key, value)

    def set_status(self, status: TraceStatus, error: str | None = None) -> None:
        if self.finished:
            return
        self._root.set_status(status)
        if error:
            self._root.error_type = self._root.error_type or "TraceStatus"
            self._root.error_message = self._service.safe_error(error)


@dataclass(slots=True)
class _TraceState:
    trace: TraceRecord
    root: SpanHandle
    scope: TraceScope
    sequence: int = 1
    spans: list[SpanHandle] = field(default_factory=list)
    span_count: int = 0
    dropped_spans: int = 0
    finished: bool = False


class TraceService:
    def __init__(
        self,
        db: Database,
        *,
        config: ObservabilityConfig | None = None,
        redactor: SecretRedactor | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.repository = TraceRepository(db)
        self.config = config or ObservabilityConfig()
        self.redactor = redactor or SecretRedactor()
        self.clock = clock or SystemClock()
        self._states: dict[str, _TraceState] = {}
        self._flush_lock: asyncio.Lock | None = None
        self._random = random.Random()

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.config.traces_enabled

    # ------------------------------------------------------------- lifecycle

    @asynccontextmanager
    async def trace(
        self,
        name: str,
        *,
        kind: str = "run",
        session_id: str,
        run_id: str | None = None,
        task_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> AsyncIterator[TraceScope | None]:
        if not self.enabled or not self._sampled():
            yield None
            return
        started = self.clock.now()
        trace_id = new_id("trc")
        safe_attributes = self.safe_attributes(attributes or {})
        try:
            await self.repository.insert_trace(
                trace_id=trace_id,
                session_id=session_id,
                run_id=run_id,
                task_id=task_id,
                name=name,
                kind=kind,
                status="running",
                started_at=started,
                attributes=safe_attributes,
                sampled=True,
                created_at=started,
            )
        except Exception:
            logger.warning("trace persistence unavailable; tracing this operation", exc_info=True)
            yield None
            return

        root = SpanHandle(
            span_id=new_id("spn"),
            trace_id=trace_id,
            parent_span_id=None,
            name=name,
            kind=kind,
            started_at=started,
            sequence=1,
            attributes=dict(safe_attributes),
        )
        record = TraceRecord(
            trace_id=trace_id,
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            name=name,
            kind=kind,
            status="running",
            started_at=started,
            attributes=safe_attributes,
            sampled=True,
        )
        scope = TraceScope(
            trace_id=trace_id,
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            name=name,
            kind=kind,
            started_at=started,
            _service=self,
            _root=root,
        )
        state = _TraceState(trace=record, root=root, scope=scope)
        self._states[trace_id] = state
        # Make the root visible immediately so a crash leaves a complete
        # parent row instead of orphan child spans.
        try:
            await self.repository.insert_spans([self._running_root_row(root)])
        except Exception:
            logger.debug("could not persist running root span", exc_info=True)
        tracer_token = set_tracer(self)
        span_token = set_current_span(root)
        try:
            yield scope
        except BaseException as exc:
            root.fail(exc)
            raise
        else:
            if root.status is None:
                root.set_status("ok")
        finally:
            reset_current_span(span_token)
            reset_tracer(tracer_token)
            await self._finish_trace(state, scope, root)

    def start_span(
        self,
        name: str,
        *,
        kind: str = "internal",
        attributes: dict[str, Any] | None = None,
    ) -> SpanHandle | None:
        from bear_agent.observability.trace.context import current_span

        parent = current_span()
        if parent is None:
            return None
        state = self._states.get(parent.trace_id)
        if state is None or state.finished:
            return None
        state.sequence += 1
        try:
            safe_attributes = self.safe_attributes(attributes or {})
        except Exception:
            safe_attributes = {}
        handle = SpanHandle(
            span_id=new_id("spn"),
            trace_id=parent.trace_id,
            parent_span_id=parent.span_id,
            name=name,
            kind=kind,
            started_at=self.clock.now(),
            sequence=state.sequence,
            attributes=safe_attributes,
        )
        return handle

    async def end_span(self, handle: SpanHandle) -> None:
        if handle.ended:
            return
        handle.ended = True
        handle.ended_at = self.clock.now()
        handle.duration_ms = max(
            0, int((handle.ended_at - handle.started_at).total_seconds() * 1000)
        )
        state = self._states.get(handle.trace_id)
        if state is None or state.finished:
            return
        if handle.status is None:
            handle.status = "ok"
        state.sequence += 1
        state.spans.append(handle)
        state.span_count += 1
        if len(state.spans) >= self.config.trace_batch_size:
            await self._flush_spans(handle.trace_id)

    async def _finish_trace(self, state: _TraceState, scope: TraceScope, root: SpanHandle) -> None:
        if state.finished:
            return
        scope.finished = True
        ended = self.clock.now()
        if root.status is None:
            root.status = "ok"
        root.ended = True
        root.ended_at = ended
        root.duration_ms = max(0, int((ended - root.started_at).total_seconds() * 1000))
        root.attributes.update(scope._attributes)
        if root not in state.spans:
            state.spans.append(root)
            state.span_count += 1
        trace = state.trace
        trace.ended_at = ended
        trace.duration_ms = max(0, int((ended - trace.started_at).total_seconds() * 1000))
        trace.status = (
            "incomplete"
            if state.dropped_spans > 0
            else root.status
            if root.status in {"ok", "error", "cancelled", "incomplete"}
            else "error"
        )
        trace.error_type = root.error_type
        trace.error_message = self.safe_error(root.error_message) if root.error_message else None
        trace.attributes = self.safe_attributes(
            {**trace.attributes, **self.safe_attributes(root.attributes)}
        )
        trace.span_count = state.span_count
        trace.dropped_spans = state.dropped_spans
        persisted = False
        try:
            await self._flush_spans(trace.trace_id)
            await self.repository.finish_trace(
                trace.trace_id,
                status=trace.status,
                ended_at=ended,
                duration_ms=trace.duration_ms,
                attributes=trace.attributes,
                error_type=trace.error_type,
                error_message=trace.error_message,
                span_count=state.span_count,
                dropped_spans=state.dropped_spans,
                incomplete=state.dropped_spans > 0,
            )
            persisted = True
        except asyncio.CancelledError:
            # Keep the state: close() will retry finalization. The buffer is
            # restored by _flush_spans on cancellation.
            logger.debug("trace flush interrupted by cancellation", exc_info=True)
            raise
        except Exception:
            state.dropped_spans += len(state.spans)
            state.spans.clear()
            logger.warning("failed to persist trace %s", trace.trace_id, exc_info=True)
        finally:
            if persisted:
                state.finished = True
                self._states.pop(trace.trace_id, None)

    async def _flush_spans(self, trace_id: str) -> None:
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
        async with self._flush_lock:
            state = self._states.get(trace_id)
            if state is None or not state.spans:
                return
            batch = state.spans
            state.spans = []
            try:
                rows = [self._span_row(handle) for handle in batch]
                await self.repository.insert_spans(rows)
            except asyncio.CancelledError:
                # Restore the batch so close()/the next flush can retry. A
                # cancelled finalizer must not silently lose diagnostics.
                state.spans = batch + state.spans
                raise
            except Exception:
                state.dropped_spans += len(batch)
                logger.warning(
                    "failed to persist %d span(s) for trace %s",
                    len(batch),
                    trace_id,
                    exc_info=True,
                )

    async def flush(self) -> None:
        """Flush every buffered span; safe to call at shutdown."""
        for trace_id in list(self._states):
            with contextlib.suppress(Exception):
                await self._flush_spans(trace_id)

    async def close(self) -> None:
        for state in list(self._states.values()):
            if not state.finished:
                state.root.set_status("incomplete")
                with contextlib.suppress(BaseException):
                    await self._finish_trace(state, state.scope, state.root)
        await self.flush()
        self._states.clear()

    # -------------------------------------------------------------- queries

    async def get(self, trace_id: str) -> dict[str, Any] | None:
        return await self.repository.get_trace(trace_id)

    async def list_traces(self, **filters: Any) -> list[dict[str, Any]]:
        return await self.repository.list_traces(**filters)

    async def spans(self, trace_id: str) -> list[dict[str, Any]]:
        return await self.repository.spans(trace_id)

    async def events(self, trace_id: str, *, limit: int = 2_000) -> list[dict[str, Any]]:
        rows = await self.repository.events(trace_id, limit=limit)
        for row in rows:
            row["payload"] = self._safe_event_payload(row.get("payload") or {})
        return rows

    _CONTENT_KEYS = frozenset(
        {
            "arguments",
            "arguments_preview",
            "completion_summary",
            "content",
            "content_preview",
            "description",
            "evidence",
            "final_text",
            "goal",
            "output",
            "prompt",
            "reason",
            "response",
            "result",
            "summary",
            "text",
            "title",
            "unmet",
        }
    )

    def _safe_event_payload(self, payload: Any) -> dict[str, Any]:
        """Keep event metadata, drop prompt/tool content from trace exports.

        The session event log is already redacted, but some fact events carry
        short content previews by design. Trace export must not become a second,
        fuller copy of that data.
        """
        if not isinstance(payload, dict):
            return {}
        result: dict[str, Any] = {}
        for key, value in payload.items():
            lowered = str(key).casefold()
            if lowered in self._CONTENT_KEYS:
                continue
            if isinstance(value, dict):
                result[str(key)] = self._safe_event_payload(value)
            elif isinstance(value, list):
                result[str(key)] = self._safe_list(value)
            elif isinstance(value, str):
                result[str(key)] = self.redactor.redact(value)[:300]
            else:
                result[str(key)] = value
        return result

    def _safe_list(self, values: list[Any]) -> list[Any]:
        result: list[Any] = []
        for value in values[:50]:
            if isinstance(value, str):
                result.append(self.redactor.redact(value)[:200])
            elif isinstance(value, dict):
                result.append(self._safe_event_payload(value))
            elif isinstance(value, (int, float, bool)) or value is None:
                result.append(value)
            else:
                result.append(self.redactor.redact(str(value))[:200])
        return result

    def _running_root_row(self, handle: SpanHandle) -> dict[str, Any]:
        return {
            "span_id": handle.span_id,
            "trace_id": handle.trace_id,
            "parent_span_id": None,
            "name": handle.name[:128],
            "kind": handle.kind[:32],
            "status": "running",
            "started_at": handle.started_at.isoformat(),
            "ended_at": None,
            "duration_ms": None,
            "attributes_json": json.dumps(
                self.safe_attributes(handle.attributes),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
            "error_type": None,
            "error_message": None,
            "sequence": 1,
            "created_at": handle.started_at.isoformat(),
        }

    async def find_by_run(self, run_id: str) -> list[dict[str, Any]]:
        return await self.repository.list_traces(run_id=run_id, limit=20)

    async def find_by_session(self, session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self.repository.list_traces(session_id=session_id, limit=limit)

    async def stats(self) -> dict[str, Any]:
        return await self.repository.stats()

    async def diagnose(self) -> dict[str, Any]:
        """Report storage integrity signals without mutating anything."""
        cutoff = self.clock.now() - timedelta(seconds=self.config.incomplete_trace_grace_s)
        stats = await self.repository.stats()
        try:
            issues = await self.repository.diagnostics(cutoff)
        except Exception:
            issues = {
                "orphan_spans": -1,
                "stale_running": -1,
                "invalid_attribute_json": -1,
                "spans_missing_end": -1,
            }
        return {**stats, **issues}

    async def reconcile_incomplete(self) -> int:
        cutoff = self.clock.now() - timedelta(seconds=self.config.incomplete_trace_grace_s)
        try:
            return await self.repository.mark_incomplete(cutoff)
        except Exception:
            logger.warning("failed to reconcile incomplete traces", exc_info=True)
            return 0

    async def prune(self) -> int:
        cutoff = self.clock.now() - timedelta(days=self.config.trace_retention_days)
        try:
            return await self.repository.prune(
                cutoff=cutoff, max_traces=self.config.trace_max_traces
            )
        except Exception:
            logger.warning("failed to prune traces", exc_info=True)
            return 0

    async def export(self, trace_id: str) -> dict[str, Any] | None:
        trace = await self.get(trace_id)
        if trace is None:
            return None
        spans = await self.spans(trace_id)
        events = await self.events(trace_id, limit=MAX_EVENTS_EXPORT)
        return {
            "schema_version": 1,
            "kind": "bear-agent.trace",
            "app_version": __version__,
            "exported_at": self.clock.now().isoformat(),
            "redaction": {
                "policy": "secret-pattern-redaction",
                "prompts": "not-recorded",
                "tool_arguments": "not-recorded",
                "tool_results": "not-recorded",
                "event_payloads": "existing-redacted-event-log",
            },
            "trace": trace,
            "spans": spans,
            "events": events,
        }

    async def hierarchy(self, trace_id: str) -> dict[str, Any] | None:
        trace = await self.get(trace_id)
        if trace is None:
            return None
        spans = await self.spans(trace_id)
        nodes: dict[str, dict[str, Any]] = {
            span["span_id"]: {**span, "children": []} for span in spans
        }
        roots: list[dict[str, Any]] = []
        for span in spans:
            node = nodes[span["span_id"]]
            parent_id = span["parent_span_id"]
            if parent_id and parent_id in nodes:
                nodes[parent_id]["children"].append(node)
            else:
                roots.append(node)
        return {"trace": trace, "roots": roots, "orphan_spans": _orphan_count(nodes)}

    # ------------------------------------------------------------- helpers

    def safe_attributes(self, attributes: dict[str, Any]) -> dict[str, Any]:
        if not attributes:
            return {}
        try:
            redacted = self.redactor.redact_obj(attributes)
            return self._bounded(redacted, self.config.trace_max_attributes_bytes)
        except Exception:
            return {"_error": "attributes-dropped"}

    def safe_error(self, message: str | BaseException) -> str:
        try:
            text = self.redactor.redact(str(message))
        except Exception:
            text = "<unprintable error>"
        return text[:MAX_ERROR_CHARS]

    def _bounded(self, attributes: Any, max_bytes: int) -> dict[str, Any]:
        if not isinstance(attributes, dict):
            return {"value": self._scalar(attributes)}
        result: dict[str, Any] = {}
        for key, value in attributes.items():
            clean_key = str(key)[:128]
            result[clean_key] = self._scalar(value)
        try:
            encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return {"_error": "attributes-not-serializable"}
        if len(encoded.encode("utf-8")) <= max_bytes:
            return result
        # Keep the most useful small fields and record the truncation instead
        # of storing a giant tool payload by accident.
        compact: dict[str, Any] = {}
        for key in sorted(result):
            candidate = {**compact, key: result[key]}
            if (
                len(json.dumps(candidate, ensure_ascii=False, default=str).encode("utf-8"))
                > max_bytes
            ):
                break
            compact = candidate
        compact["_attributes_truncated"] = True
        return compact

    @staticmethod
    def _scalar(value: Any) -> Any:
        if isinstance(value, str):
            return value[:MAX_ATTRIBUTE_STRING]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (list, tuple)):
            return [
                item[:MAX_ATTRIBUTE_STRING]
                if isinstance(item, str)
                else item
                if isinstance(item, (int, float, bool)) or item is None
                else str(item)[:MAX_ATTRIBUTE_STRING]
                for item in list(value)[:50]
            ]
        if isinstance(value, dict):
            return {
                str(key)[:64]: str(item)[:MAX_ATTRIBUTE_STRING]
                if not isinstance(item, (int, float, bool))
                else item
                for key, item in list(value.items())[:30]
            }
        return str(value)[:MAX_ATTRIBUTE_STRING]

    def _sampled(self) -> bool:
        ratio = self.config.trace_sample_ratio
        if ratio >= 1.0:
            return True
        if ratio <= 0.0:
            return False
        return self._random.random() < ratio

    def _span_row(self, handle: SpanHandle) -> dict[str, Any]:
        ended = handle.ended_at or self.clock.now()
        if handle.status is None:
            handle.status = "ok"
        duration_ms = (
            handle.duration_ms
            if handle.ended_at is not None
            else max(0, int((ended - handle.started_at).total_seconds() * 1000))
        )
        return {
            "span_id": handle.span_id,
            "trace_id": handle.trace_id,
            "parent_span_id": handle.parent_span_id,
            "name": handle.name[:128],
            "kind": handle.kind[:32],
            "status": handle.status,
            "started_at": handle.started_at.isoformat(),
            "ended_at": ended.isoformat(),
            "duration_ms": duration_ms,
            "attributes_json": json.dumps(
                self.safe_attributes(handle.attributes),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
            "error_type": handle.error_type,
            "error_message": (
                self.safe_error(handle.error_message) if handle.error_message else None
            ),
            "sequence": handle.sequence,
            "created_at": ended.isoformat(),
        }


def _orphan_count(nodes: dict[str, dict[str, Any]]) -> int:
    # Every span whose parent is set but absent is an orphan. This happens when
    # a buffer flush lost rows; surfacing it beats pretending the tree is whole.
    return sum(
        1
        for node in nodes.values()
        if node.get("parent_span_id") and node["parent_span_id"] not in nodes
    )
