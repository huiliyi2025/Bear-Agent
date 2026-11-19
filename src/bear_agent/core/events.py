"""Append-only event model.

Events are the observable facts of a session. Fact events are persisted with a
per-session sequence number; delta events (streaming text, transient progress)
are published live but not persisted. The set below is the single place that
decides which is which.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bear_agent.core.ids import new_id


class EventType(StrEnum):
    # Session lifecycle
    SESSION_CREATED = "session.created"
    SESSION_UPDATED = "session.updated"
    SESSION_LEASE_ACQUIRED = "session.lease.acquired"
    SESSION_LEASE_RELEASED = "session.lease.released"

    # Run lifecycle
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_CANCELLED = "run.cancelled"
    RUN_FAILED = "run.failed"

    # Turn lifecycle
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"

    # Messages and model calls
    MESSAGE_USER = "message.user"
    MESSAGE_ASSISTANT = "message.assistant"
    MESSAGE_TOOL = "message.tool"
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"
    MODEL_RETRY = "model.retry"

    # Tools and policy
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    TOOL_CALL_FAILED = "tool.call.failed"
    POLICY_DECISION = "policy.decision"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    SANDBOX_DECISION = "sandbox.decision"
    CLAIM_ACQUIRED = "claim.acquired"
    CLAIM_CONFLICT = "claim.conflict"

    # Context
    CONTEXT_BUILT = "context.built"
    CONTEXT_TRUNCATED = "context.truncated"

    # Ephemeral UI events
    MODEL_TEXT_DELTA = "model.text.delta"
    MODEL_THINKING_DELTA = "model.thinking.delta"
    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"


PERSISTED_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.SESSION_CREATED,
        EventType.SESSION_UPDATED,
        EventType.SESSION_LEASE_ACQUIRED,
        EventType.SESSION_LEASE_RELEASED,
        EventType.RUN_STARTED,
        EventType.RUN_COMPLETED,
        EventType.RUN_CANCELLED,
        EventType.RUN_FAILED,
        EventType.TURN_STARTED,
        EventType.TURN_COMPLETED,
        EventType.MESSAGE_USER,
        EventType.MESSAGE_ASSISTANT,
        EventType.MESSAGE_TOOL,
        EventType.MODEL_REQUEST,
        EventType.MODEL_RESPONSE,
        EventType.MODEL_RETRY,
        EventType.TOOL_CALL_STARTED,
        EventType.TOOL_CALL_COMPLETED,
        EventType.TOOL_CALL_FAILED,
        EventType.POLICY_DECISION,
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_DECIDED,
        EventType.SANDBOX_DECISION,
        EventType.CLAIM_ACQUIRED,
        EventType.CLAIM_CONFLICT,
        EventType.CONTEXT_BUILT,
        EventType.CONTEXT_TRUNCATED,
    }
)


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    session_id: str
    seq: int = 0
    type: EventType
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str | None = None
    turn_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def persisted(self) -> bool:
        return self.type in PERSISTED_EVENT_TYPES

    def with_seq(self, seq: int) -> Event:
        return self.model_copy(update={"seq": seq})

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
