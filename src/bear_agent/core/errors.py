"""Error hierarchy.

Every failure crossing a module boundary has a stable category so the runtime,
UI and observability layer can react without parsing strings.
"""

from __future__ import annotations

from dataclasses import dataclass


class BearError(Exception):
    """Base class for expected, user-facing harness errors."""


class ConfigError(BearError):
    pass


class StorageError(BearError):
    pass


class SessionLeaseError(BearError):
    pass


class PolicyError(BearError):
    pass


class ToolExecutionError(BearError):
    pass


class ContextOverflowError(BearError):
    pass


class CompactionError(BearError):
    pass


class CheckpointError(BearError):
    pass


class CheckpointCorrupt(CheckpointError):
    pass


class CheckpointIncompatible(CheckpointError):
    pass


class RecoveryError(BearError):
    pass


class ForkError(BearError):
    pass


class ArtifactError(BearError):
    pass


class ArtifactCorrupt(ArtifactError):
    pass


class TaskError(BearError):
    pass


class TaskValidationError(TaskError):
    pass


class PlanValidationError(TaskValidationError):
    pass


class TaskStateError(TaskError):
    pass


class PlanGenerationError(TaskError):
    pass


class TaskExecutionError(TaskError):
    pass


class ClaimError(BearError):
    pass


class ClaimConflict(ClaimError):
    def __init__(self, message: str, *, holder: str = "", path: str = "") -> None:
        super().__init__(message)
        self.holder = holder
        self.path = path


class SandboxError(BearError):
    pass


class SandboxUnavailable(SandboxError):
    pass


class AuditError(BearError):
    pass


class SecretStoreError(BearError):
    pass


class ApprovalError(BearError):
    pass


@dataclass(slots=True)
class ProviderError(BearError):
    message: str
    category: str = "unknown"
    retryable: bool = False
    status_code: int | None = None
    retry_after_s: float | None = None

    def __str__(self) -> str:
        return self.message


class HookError(BearError):
    pass


class HookTimeout(HookError):
    pass


class MCPError(BearError):
    pass


class MCPUnavailable(MCPError):
    pass


class MCPToolError(MCPError):
    pass


class PluginError(BearError):
    pass


class PluginManifestError(PluginError):
    pass


class PluginLoadError(PluginError):
    pass
