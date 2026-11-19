"""Core domain types shared by every layer.

This package deliberately has no dependency on storage, providers, tools or UI.
"""

from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.clock import Clock, FakeClock, SystemClock
from bear_agent.core.errors import (
    BearError,
    ConfigError,
    ContextOverflowError,
    PolicyError,
    ProviderError,
    SessionLeaseError,
    StorageError,
    ToolExecutionError,
)
from bear_agent.core.events import Event, EventType
from bear_agent.core.ids import new_id
from bear_agent.core.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolMessage,
    ToolResultPart,
    UserMessage,
)
from bear_agent.core.modes import PermissionMode, ToolSideEffect
from bear_agent.core.tool_spec import ToolSpec
from bear_agent.core.usage import TokenUsage, UsageSummary

__all__ = [
    "AssistantMessage",
    "BearError",
    "CancelToken",
    "Clock",
    "ConfigError",
    "ContextOverflowError",
    "Event",
    "EventType",
    "FakeClock",
    "Message",
    "PermissionMode",
    "PolicyError",
    "ProviderError",
    "RunCancelled",
    "SessionLeaseError",
    "StorageError",
    "SystemClock",
    "SystemMessage",
    "TextPart",
    "TokenUsage",
    "ToolCall",
    "ToolCallPart",
    "ToolExecutionError",
    "ToolMessage",
    "ToolResultPart",
    "ToolSideEffect",
    "ToolSpec",
    "UsageSummary",
    "UserMessage",
    "new_id",
]
