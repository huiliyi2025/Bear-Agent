"""Structured diagnostics results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


class CheckMode(StrEnum):
    """Whether a check only inspected state or actively contacted something."""

    STATIC = "static"
    ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    id: str
    category: str
    status: CheckStatus
    summary: str
    detail: str = ""
    mode: CheckMode = CheckMode.STATIC
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "status": self.status.value,
            "summary": self.summary,
            "detail": self.detail,
            "mode": self.mode.value,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    app_version: str
    schema_version: int
    generated_at: datetime
    checks: list[DiagnosticCheck]
    config_path: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        result = {status.value: 0 for status in CheckStatus}
        for check in self.checks:
            result[check.status.value] += 1
        return result

    @property
    def ok(self) -> bool:
        return all(check.status is not CheckStatus.FAIL for check in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "bear-agent.doctor",
            "app_version": self.app_version,
            "database_schema_version": self.schema_version,
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "config_path": self.config_path,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "counts": self.counts,
            "checks": [check.to_dict() for check in self.checks],
        }
