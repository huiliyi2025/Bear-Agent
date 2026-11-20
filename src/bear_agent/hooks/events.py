"""Hook lifecycle events.

Hook events are a compact, stable vocabulary exposed to hook authors. They are
deliberately separate from the persisted :class:`EventType` bus: hooks observe
tool/task/checkpoint lifecycle facts but must never be able to trigger
themselves from the ``HOOK_*`` bookkeeping events or from high-volume streaming
deltas.

``model_before`` / ``tool_before`` are real interception points; the remaining
events are observations. ``from_event_type`` maps persisted and live bus events
to observation hooks and returns ``None`` for everything else.
"""

from __future__ import annotations

from enum import StrEnum

from bear_agent.core.events import EventType


class HookEvent(StrEnum):
    # Session / run / turn lifecycle
    SESSION_CREATED = "session_created"
    RUN_STARTED = "run_started"
    TURN_STARTED = "turn_started"

    # Model interception and observation
    MODEL_BEFORE = "model_before"
    MODEL_AFTER = "model_after"
    MODEL_ERROR = "model_error"

    # Tool interception and observation
    TOOL_BEFORE = "tool_before"
    TOOL_AFTER = "tool_after"
    TOOL_ERROR = "tool_error"

    # Task / plan lifecycle
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_BLOCKED = "task_blocked"
    TASK_CANCELLED = "task_cancelled"
    PLAN_REVISED = "plan_revised"

    # Continuity facts
    CONTEXT_COMPACTED = "context_compacted"
    CHECKPOINT_CREATED = "checkpoint_created"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"

    # Run terminal states
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"

    @classmethod
    def from_event_type(cls, event_type: EventType) -> HookEvent | None:
        """Map a bus/persisted event to an observation hook event.

        ``HOOK_*`` events and ephemeral delta/UI events intentionally map to
        ``None`` so a bus-subscribed manager cannot recurse through its own
        bookkeeping or flood hooks with token deltas.
        """
        return _EVENT_MAP.get(event_type)

    @classmethod
    def from_string(cls, value: str) -> HookEvent:
        """Parse a config string, accepting a few historical aliases."""
        try:
            return cls(value)
        except ValueError:
            pass
        alias = _CONFIG_ALIASES.get(value)
        if alias is None:
            allowed = ", ".join(sorted(event.value for event in cls))
            raise ValueError(f"unknown hook event {value!r}; allowed: {allowed}") from None
        return alias


# Persisted/bus facts that observation hooks may subscribe to. Streaming and
# UI-only events (MODEL_TEXT_DELTA, TOOL_STARTED, TOOL_FINISHED, ...) are not
# mapped. The start events are intentionally excluded: tool_before/model_before
# are emitted synchronously by the executors before the corresponding bus fact.
_EVENT_MAP: dict[EventType, HookEvent] = {
    EventType.SESSION_CREATED: HookEvent.SESSION_CREATED,
    EventType.RUN_STARTED: HookEvent.RUN_STARTED,
    EventType.TURN_STARTED: HookEvent.TURN_STARTED,
    EventType.MODEL_RESPONSE: HookEvent.MODEL_AFTER,
    EventType.MODEL_RETRY: HookEvent.MODEL_ERROR,
    EventType.TOOL_CALL_COMPLETED: HookEvent.TOOL_AFTER,
    EventType.TOOL_CALL_FAILED: HookEvent.TOOL_ERROR,
    EventType.TASK_STARTED: HookEvent.TASK_STARTED,
    EventType.TASK_COMPLETED: HookEvent.TASK_COMPLETED,
    EventType.TASK_FAILED: HookEvent.TASK_FAILED,
    EventType.TASK_BLOCKED: HookEvent.TASK_BLOCKED,
    EventType.TASK_CANCELLED: HookEvent.TASK_CANCELLED,
    EventType.TASK_PLAN_REVISED: HookEvent.PLAN_REVISED,
    EventType.CONTEXT_COMPACTED: HookEvent.CONTEXT_COMPACTED,
    EventType.CHECKPOINT_CREATED: HookEvent.CHECKPOINT_CREATED,
    EventType.RECOVERY_STARTED: HookEvent.RECOVERY_STARTED,
    EventType.RECOVERY_COMPLETED: HookEvent.RECOVERY_COMPLETED,
    EventType.RUN_COMPLETED: HookEvent.RUN_COMPLETED,
    EventType.RUN_FAILED: HookEvent.RUN_FAILED,
    EventType.RUN_CANCELLED: HookEvent.RUN_CANCELLED,
}

_CONFIG_ALIASES: dict[str, HookEvent] = {
    "before_tool_call": HookEvent.TOOL_BEFORE,
    "after_tool_call": HookEvent.TOOL_AFTER,
    "before_model_call": HookEvent.MODEL_BEFORE,
    "after_model_call": HookEvent.MODEL_AFTER,
    "run_finished": HookEvent.RUN_COMPLETED,
}
