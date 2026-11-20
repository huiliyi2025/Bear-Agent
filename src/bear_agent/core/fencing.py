"""Lease fencing tokens.

A session lease is owned by exactly one run at a time. Every state write that
can affect a session must prove that it still holds the current lease; otherwise
an old owner that lost its lease (process pause, network stall, clock jump)
could append transcript rows or overwrite tool state after a recovery took over.

The token is deliberately small and immutable so it can be passed down through
the runtime, executor, recorder and repositories without coupling them to the
session service.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LeaseFence:
    owner: str
    epoch: int

    def __post_init__(self) -> None:
        if not self.owner:
            raise ValueError("lease fence requires an owner")
        if self.epoch < 1:
            raise ValueError("lease fence requires a positive epoch")


# Ambient fence for the current run. The runtime sets it once after acquiring
# the lease; repositories fall back to it when no explicit fence is passed, so
# every transcript/event/tool-state write inside the run is fenced. Background
# tasks inherit the context and therefore cannot write after the lease is lost
# (they keep the stale token), which is the desired fail-closed behavior.
_current_fence: ContextVar[LeaseFence | None] = ContextVar("bear_lease_fence", default=None)


def current_fence() -> LeaseFence | None:
    return _current_fence.get()


def set_fence(fence: LeaseFence) -> Token[LeaseFence | None]:
    return _current_fence.set(fence)


def reset_fence(token: Token[LeaseFence | None]) -> None:
    _current_fence.reset(token)


def resolve_fence(fence: LeaseFence | None = None) -> LeaseFence | None:
    """Explicit fence wins; otherwise use the ambient run fence (if any)."""
    return fence if fence is not None else current_fence()


# A process-local counter for rejected stale writes. It is diagnostic only:
# incrementing it can never alter the fencing decision. Historical counts from
# previous processes are not persisted, which the metrics report states.
_fence_rejections = 0
_fence_lock = threading.Lock()


def note_fence_rejection() -> None:
    global _fence_rejections
    with _fence_lock:
        _fence_rejections += 1


def fence_rejections_total() -> int:
    with _fence_lock:
        return _fence_rejections
