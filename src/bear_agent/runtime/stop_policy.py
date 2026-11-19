"""Stop conditions and budgets.

Stopping is deliberate: every run ends with a structured reason, not just a
silent loop exit. Repetition is detected across turns so a model that keeps
calling the same tool cannot spin forever.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum

from bear_agent.core.messages import AssistantMessage, ToolCall


class RunStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    MAX_TURNS = "max_turns"
    MAX_TOOL_CALLS = "max_tool_calls"
    NO_PROGRESS = "no_progress"
    TIMEOUT = "timeout"
    CONTEXT_OVERFLOW = "context_overflow"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class StopDecision:
    status: RunStatus
    reason: str


@dataclass
class BudgetTracker:
    max_turns: int
    max_tool_calls: int
    max_run_seconds: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    tool_calls: int = 0

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def check_time(self) -> StopDecision | None:
        if self.max_run_seconds > 0 and self.elapsed_s() > self.max_run_seconds:
            return StopDecision(
                RunStatus.TIMEOUT,
                f"run exceeded {self.max_run_seconds:.0f}s wall-clock budget",
            )
        return None

    def add_tool_calls(self, count: int) -> StopDecision | None:
        self.tool_calls += count
        if self.tool_calls > self.max_tool_calls:
            return StopDecision(
                RunStatus.MAX_TOOL_CALLS,
                f"tool-call budget exhausted ({self.max_tool_calls})",
            )
        return None


class StopPolicy:
    def __init__(self, *, max_repeated_calls: int = 3) -> None:
        self.max_repeated_calls = max_repeated_calls
        self._fingerprints: list[str] = []

    def reset(self) -> None:
        self._fingerprints.clear()

    def evaluate(
        self, assistant: AssistantMessage, turn_index: int, max_turns: int
    ) -> StopDecision | None:
        calls = assistant.tool_calls()
        if not calls:
            if not assistant.text_content().strip():
                return StopDecision(RunStatus.FAILED, "assistant returned an empty response")
            return StopDecision(RunStatus.COMPLETED, "assistant produced a final answer")
        if turn_index + 1 >= max_turns:
            return StopDecision(RunStatus.MAX_TURNS, f"turn budget exhausted ({max_turns})")
        fingerprint = _fingerprint(calls)
        if fingerprint and self._fingerprints.count(fingerprint) >= self.max_repeated_calls:
            return StopDecision(
                RunStatus.NO_PROGRESS,
                "the same tool call repeated without progress",
            )
        if fingerprint:
            self._fingerprints.append(fingerprint)
        return None


def _fingerprint(calls: list[ToolCall]) -> str:
    payload = sorted(
        (call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))
        for call in calls
    )
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()[:16]
