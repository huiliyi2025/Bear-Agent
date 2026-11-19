"""Shared enums used by configuration, policy and tools.

They live in ``core`` so no module has to import a higher layer just to name a
mode.
"""

from __future__ import annotations

from enum import StrEnum


class PermissionMode(StrEnum):
    PLAN = "plan"
    SUPERVISED = "supervised"
    AUTO = "auto"
    UNRESTRICTED = "unrestricted"


class ToolSideEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    EXEC = "exec"
    NETWORK = "network"
