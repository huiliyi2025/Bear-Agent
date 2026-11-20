"""Hook subsystem: interception, observation and external command hooks."""

from bear_agent.hooks.command import (
    ENV_ALLOWLIST,
    STDOUT_LIMIT_BYTES,
    HookCommandError,
    HookCommandViolation,
    run_command_hook,
)
from bear_agent.hooks.events import HookEvent
from bear_agent.hooks.manager import HookManager
from bear_agent.hooks.models import (
    FailMode,
    HookAction,
    HookDecision,
    HookExecution,
    HookHandler,
    HookInvocation,
    HookOutcome,
    HookPayload,
    HookRegistration,
    HookStatus,
)

__all__ = [
    "ENV_ALLOWLIST",
    "STDOUT_LIMIT_BYTES",
    "FailMode",
    "HookAction",
    "HookCommandError",
    "HookCommandViolation",
    "HookDecision",
    "HookEvent",
    "HookExecution",
    "HookHandler",
    "HookInvocation",
    "HookManager",
    "HookOutcome",
    "HookPayload",
    "HookRegistration",
    "HookStatus",
    "run_command_hook",
]
