"""Tool-level permission policy.

The policy is a standalone step inside the Agent Loop. For every tool call the
loop asks the policy for a Decision:

- ALLOW   -> execute
- CONFIRM -> ask the user (via ``confirm_fn``); execute only if approved
- DENY    -> never execute; report the denial back to the model as a tool result

Three security layers compose here:
1. ``plan_mode``: no mutation at all (modify/dangerous tools are DENY).
2. permission levels on tools: read_only / modify / dangerous.
3. dangerous-command patterns: ``run_shell`` commands matching destructive
   patterns force CONFIRM even when the policy would otherwise allow them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from bear_agent.tools.base import Tool

DANGEROUS_COMMAND_PATTERNS: tuple[str, ...] = (
    r"\brm\s+-rf\b",
    r"\brmdir\s+/[a-z]*[sq]",
    r"\bdel\s+(/[a-z]*f\s+)?[a-z]:",
    r"\bformat\s+[a-z]:",
    r"\bgit\s+push\s+(--force|-f)\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\b(git\s+)?clean\s+(-[a-z]*)f",
    r"curl\b.*\|\s*(ba)?sh\b",
    r"wget\b.*\|\s*(ba)?sh\b",
    r"\bdd\s+if=.*of=/dev/",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|halt|restart)\b",
    r"reg\s+delete",
    r"diskpart",
)


def _empty_confirm(tool: Tool, arguments: dict[str, Any]) -> bool:
    return False


@dataclass
class PermissionPolicy:
    plan_mode: bool = False
    confirm_fn: Callable[[Tool, dict[str, Any]], bool] = _empty_confirm

    def decide(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        if tool.permission == "read_only":
            return Decision.ALLOW
        if self.plan_mode:
            return Decision.DENY
        if tool.permission == "modify":
            return Decision.ALLOW
        # dangerous-level tool
        if self._looks_dangerous(tool, arguments):
            return Decision.CONFIRM
        return Decision.ALLOW

    def confirm(self, tool: Tool, arguments: dict[str, Any]) -> bool:
        return self.confirm_fn(tool, arguments)

    def _looks_dangerous(self, tool: Tool, arguments: dict[str, Any]) -> bool:
        if tool.name == "run_shell":
            command = str(arguments.get("command", ""))
            return any(
                re.search(pattern, command, re.IGNORECASE)
                for pattern in DANGEROUS_COMMAND_PATTERNS
            )
        return True


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value
