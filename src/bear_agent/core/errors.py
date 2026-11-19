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


@dataclass(slots=True)
class ProviderError(BearError):
    message: str
    category: str = "unknown"
    retryable: bool = False
    status_code: int | None = None
    retry_after_s: float | None = None

    def __str__(self) -> str:
        return self.message
