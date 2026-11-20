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
    CONTEXT_COMPACTED = "context.compacted"
    CONTEXT_COMPACTION_FAILED = "context.compaction.failed"
    CONTEXT_RESET = "context.reset"
    CONTEXT_ANCHOR_ADDED = "context.anchor.added"
    CONTEXT_ANCHOR_REMOVED = "context.anchor.removed"

    # Checkpoints and recovery
    CHECKPOINT_CREATED = "checkpoint.created"
    CHECKPOINT_INVALID = "checkpoint.invalid"
    CHECKPOINT_PRUNED = "checkpoint.pruned"
    RECOVERY_STARTED = "recovery.started"
    RECOVERY_COMPLETED = "recovery.completed"
    RECOVERY_FAILED = "recovery.failed"
    RECOVERY_TOOL_RECONCILED = "recovery.tool.reconciled"
    RECOVERY_PROCESS_TERMINATED = "recovery.process.terminated"
    RECOVERY_CLAIMS_RELEASED = "recovery.claims.released"

    # Fork
    SESSION_FORKED = "session.forked"

    # Tasks, plans and todo
    TASK_PLAN_CREATED = "task.plan.created"
    TASK_PLAN_REVISED = "task.plan.revised"
    TASK_PLAN_COMPLETED = "task.plan.completed"
    TASK_PLAN_BLOCKED = "task.plan.blocked"
    TASK_PLAN_FAILED = "task.plan.failed"
    TASK_PLAN_CANCELLED = "task.plan.cancelled"
    TASK_STARTED = "task.started"
    TASK_ATTEMPT_STARTED = "task.attempt.started"
    TASK_ATTEMPT_FINISHED = "task.attempt.finished"
    TASK_COMPLETION_REQUESTED = "task.completion.requested"
    TASK_VERIFICATION = "task.verification"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_BLOCKED = "task.blocked"
    TASK_CANCELLED = "task.cancelled"
    TASK_RETRY_SCHEDULED = "task.retry.scheduled"
    TASK_RUN_STARTED = "task.run.started"
    TASK_RUN_COMPLETED = "task.run.completed"
    TASK_RUN_FAILED = "task.run.failed"
    TASK_RECONCILED = "task.reconciled"

    # Hooks
    HOOK_STARTED = "hook.started"
    HOOK_COMPLETED = "hook.completed"
    HOOK_FAILED = "hook.failed"
    HOOK_DENIED = "hook.denied"

    # MCP
    MCP_SERVER_STARTED = "mcp.server.started"
    MCP_SERVER_FAILED = "mcp.server.failed"
    MCP_SERVER_RECONNECTED = "mcp.server.reconnected"
    MCP_SERVER_STOPPED = "mcp.server.stopped"
    MCP_TOOLS_UPDATED = "mcp.tools.updated"

    # Plugins
    PLUGIN_LOADED = "plugin.loaded"
    PLUGIN_UNLOADED = "plugin.unloaded"
    PLUGIN_ENABLED = "plugin.enabled"
    PLUGIN_DISABLED = "plugin.disabled"
    PLUGIN_FAILED = "plugin.failed"

    # Provider routing
    PROVIDER_SELECTED = "provider.selected"
    PROVIDER_RETRY = "provider.retry"
    PROVIDER_FALLBACK = "provider.fallback"
    PROVIDER_CIRCUIT_OPEN = "provider.circuit.open"
    PROVIDER_FAILED = "provider.failed"

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
        EventType.CONTEXT_COMPACTED,
        EventType.CONTEXT_COMPACTION_FAILED,
        EventType.CONTEXT_RESET,
        EventType.CONTEXT_ANCHOR_ADDED,
        EventType.CONTEXT_ANCHOR_REMOVED,
        EventType.CHECKPOINT_CREATED,
        EventType.CHECKPOINT_INVALID,
        EventType.CHECKPOINT_PRUNED,
        EventType.RECOVERY_STARTED,
        EventType.RECOVERY_COMPLETED,
        EventType.RECOVERY_FAILED,
        EventType.RECOVERY_TOOL_RECONCILED,
        EventType.RECOVERY_PROCESS_TERMINATED,
        EventType.RECOVERY_CLAIMS_RELEASED,
        EventType.SESSION_FORKED,
        EventType.TASK_PLAN_CREATED,
        EventType.TASK_PLAN_REVISED,
        EventType.TASK_PLAN_COMPLETED,
        EventType.TASK_PLAN_BLOCKED,
        EventType.TASK_PLAN_FAILED,
        EventType.TASK_PLAN_CANCELLED,
        EventType.TASK_STARTED,
        EventType.TASK_ATTEMPT_STARTED,
        EventType.TASK_ATTEMPT_FINISHED,
        EventType.TASK_COMPLETION_REQUESTED,
        EventType.TASK_VERIFICATION,
        EventType.TASK_COMPLETED,
        EventType.TASK_FAILED,
        EventType.TASK_BLOCKED,
        EventType.TASK_CANCELLED,
        EventType.TASK_RETRY_SCHEDULED,
        EventType.TASK_RUN_STARTED,
        EventType.TASK_RUN_COMPLETED,
        EventType.TASK_RUN_FAILED,
        EventType.TASK_RECONCILED,
        EventType.HOOK_STARTED,
        EventType.HOOK_COMPLETED,
        EventType.HOOK_FAILED,
        EventType.HOOK_DENIED,
        EventType.MCP_SERVER_STARTED,
        EventType.MCP_SERVER_FAILED,
        EventType.MCP_SERVER_RECONNECTED,
        EventType.MCP_SERVER_STOPPED,
        EventType.MCP_TOOLS_UPDATED,
        EventType.PLUGIN_LOADED,
        EventType.PLUGIN_UNLOADED,
        EventType.PLUGIN_ENABLED,
        EventType.PLUGIN_DISABLED,
        EventType.PLUGIN_FAILED,
        EventType.PROVIDER_SELECTED,
        EventType.PROVIDER_RETRY,
        EventType.PROVIDER_FALLBACK,
        EventType.PROVIDER_CIRCUIT_OPEN,
        EventType.PROVIDER_FAILED,
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
