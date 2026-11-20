"""Live metrics from the event bus.

Durable reports are computed from domain tables (``query.py``). This collector
exists for live surfaces (the future TUI, interactive sessions, benchmark
instrumentation) and keeps only bounded aggregate labels; run/session IDs are
never labels. It is an observer: a failure here can never affect a run.
"""

from __future__ import annotations

import time
from typing import Any

from bear_agent.core.events import Event, EventType
from bear_agent.observability.metrics.registry import MetricsRegistry

_MAX_ACTIVE = 10_000


class LiveMetricsCollector:
    def __init__(self, registry: MetricsRegistry) -> None:
        self.registry = registry
        self._runs: dict[str, tuple[float, str, str]] = {}
        self._tools: dict[str, float] = {}

    async def on_event(self, event: Event) -> None:
        try:
            self._consume(event)
        except Exception:
            # Observation must never break the event bus or a run.
            return

    def snapshot(self) -> dict[str, Any]:
        return self.registry.snapshot()

    def _consume(self, event: Event) -> None:
        if event.type in {
            EventType.MODEL_TEXT_DELTA,
            EventType.MODEL_THINKING_DELTA,
            EventType.TOOL_STARTED,
            EventType.TOOL_FINISHED,
        }:
            # Streaming deltas are hot; live metrics only consumes fact events.
            return
        payload = event.payload
        now = time.monotonic()
        if event.type == EventType.RUN_STARTED and event.run_id:
            if len(self._runs) < _MAX_ACTIVE:
                self._runs[event.run_id] = (
                    now,
                    str(payload.get("provider") or ""),
                    str(payload.get("model") or ""),
                )
            self.registry.gauge("bear.active_runs", float(len(self._runs)))
        elif event.type in {
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
        }:
            status = str(payload.get("status") or event.type.value.rsplit(".", 1)[-1])
            provider = str(payload.get("provider") or "")
            model = str(payload.get("model") or "")
            self.registry.counter("bear.runs", {"status": status})
            if event.run_id and event.run_id in self._runs:
                started, provider, model = self._runs.pop(event.run_id)
                self.registry.observe(
                    "bear.run.duration_ms",
                    (now - started) * 1000,
                    {"status": status},
                )
            self.registry.gauge("bear.active_runs", float(len(self._runs)))
            if provider or model:
                self.registry.counter(
                    "bear.run.provider",
                    {"provider": provider, "model": model, "status": status},
                )
        elif event.type == EventType.MODEL_RESPONSE:
            usage = payload.get("usage") or {}
            labels = {
                "provider": str(payload.get("provider") or ""),
                "model": str(payload.get("model") or ""),
            }
            self.registry.counter("bear.model.calls", labels)
            self.registry.observe(
                "bear.model.latency_ms", float(payload.get("latency_ms") or 0), labels
            )
            self.registry.counter(
                "bear.model.input_tokens",
                labels,
                float(usage.get("input_tokens") or 0),
            )
            self.registry.counter(
                "bear.model.output_tokens",
                labels,
                float(usage.get("output_tokens") or 0),
            )
        elif event.type in {EventType.MODEL_RETRY, EventType.PROVIDER_RETRY}:
            self.registry.counter(
                "bear.provider.retries",
                {
                    "provider": str(payload.get("provider") or ""),
                    "model": str(payload.get("model") or ""),
                    "category": str(payload.get("category") or "unknown"),
                },
            )
        elif event.type == EventType.PROVIDER_FALLBACK:
            self.registry.counter(
                "bear.provider.fallbacks",
                {
                    "to": str(payload.get("to") or ""),
                    "category": str(payload.get("category") or "unknown"),
                },
            )
        elif event.type == EventType.PROVIDER_CIRCUIT_OPEN:
            self.registry.counter(
                "bear.provider.circuit_open",
                {
                    "provider": str(payload.get("provider") or ""),
                    "model": str(payload.get("model") or ""),
                },
            )
        elif event.type == EventType.TOOL_CALL_STARTED:
            if len(self._tools) < _MAX_ACTIVE and payload.get("tool_call_id"):
                self._tools[str(payload["tool_call_id"])] = now
        elif event.type in {EventType.TOOL_CALL_COMPLETED, EventType.TOOL_CALL_FAILED}:
            labels = {
                "tool": str(payload.get("name") or ""),
                "status": str(payload.get("status") or ""),
            }
            self.registry.counter("bear.tool.calls", labels)
            call_id = str(payload.get("tool_call_id") or "")
            duration = payload.get("duration_ms")
            if duration is None and call_id in self._tools:
                duration = (now - self._tools.pop(call_id)) * 1000
            if isinstance(duration, (int, float)):
                self.registry.observe("bear.tool.duration_ms", float(duration), labels)
            if payload.get("error_kind"):
                self.registry.counter(
                    "bear.tool.errors",
                    {
                        "tool": labels["tool"],
                        "kind": str(payload["error_kind"]),
                    },
                )
        elif event.type == EventType.TASK_STARTED:
            self.registry.counter("bear.tasks.started")
        elif event.type == EventType.TASK_COMPLETED:
            self.registry.counter("bear.tasks.completed")
        elif event.type in {EventType.TASK_FAILED, EventType.TASK_BLOCKED}:
            self.registry.counter(
                "bear.tasks.failed", {"status": event.type.value.rsplit(".", 1)[-1]}
            )
        elif event.type == EventType.TASK_RETRY_SCHEDULED:
            self.registry.counter("bear.tasks.retries")
        elif event.type == EventType.CONTEXT_COMPACTED:
            self.registry.counter(
                "bear.context.compactions", {"level": str(payload.get("level") or "")}
            )
            before = float(payload.get("before_tokens") or 0)
            after = float(payload.get("after_tokens") or 0)
            self.registry.observe("bear.context.before_tokens", before)
            self.registry.observe("bear.context.after_tokens", after)
            if before > after:
                self.registry.counter("bear.context.saved_tokens", amount=before - after)
        elif event.type == EventType.RECOVERY_STARTED:
            self.registry.counter("bear.recovery.started")
        elif event.type == EventType.RECOVERY_COMPLETED:
            self.registry.counter("bear.recovery.completed")
        elif event.type == EventType.RECOVERY_FAILED:
            self.registry.counter("bear.recovery.failed")
        elif event.type == EventType.CHECKPOINT_CREATED:
            self.registry.counter("bear.checkpoints.created")
        elif event.type == EventType.HOOK_DENIED:
            self.registry.counter("bear.hooks.denied")
        elif event.type == EventType.PROVIDER_SELECTED:
            self.registry.counter(
                "bear.provider.selected",
                {
                    "provider": str(payload.get("provider") or ""),
                    "model": str(payload.get("model") or ""),
                },
            )
        if event.ts:
            self.registry.gauge("bear.event.last_timestamp_s", event.ts.timestamp())
