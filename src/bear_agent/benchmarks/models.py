"""Benchmark contracts.

A benchmark scenario is a deterministic, named operation with explicit
parameters. The runner owns timing, warmup, statistics, persistence and
comparison; scenarios own setup and the operation being measured. Real-provider
scenarios are marked ``kind="real"`` and are never mixed with scripted results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from bear_agent.config.models import Settings
from bear_agent.observability.metrics.registry import MetricsRegistry
from bear_agent.observability.trace.service import TraceService
from bear_agent.secrets.redaction import SecretRedactor

BenchmarkKind = Literal["synthetic", "scripted", "real"]
BenchmarkStatus = Literal["completed", "failed", "skipped"]


@dataclass
class BenchmarkSample:
    """Scenario-specific observations for one timed iteration."""

    operations: int = 1
    tokens: int = 0
    cost: float = 0.0
    note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class BenchmarkSkipped(Exception):
    """Raised when the current platform/configuration cannot run a scenario."""


@dataclass
class BenchmarkContext:
    """Isolated filesystem/database context for one iteration.

    Each iteration gets a new data directory and workspace, so a benchmark can
    never observe rows or files left by a previous sample. ``settings`` is a
    deep copy; the user's real configuration is never mutated.
    """

    settings: Settings
    root: Path
    workspace: Path
    data_dir: Path
    params: dict[str, Any] = field(default_factory=dict)
    redactor: SecretRedactor = field(default_factory=SecretRedactor)
    registry: MetricsRegistry = field(default_factory=MetricsRegistry)
    tracer: TraceService | None = None
    state: dict[str, Any] = field(default_factory=dict)

    def settings_copy(self, *, data_dir: Path | None = None) -> Settings:
        copy = self.settings.model_copy(deep=True)
        copy.storage.data_dir = data_dir or self.data_dir
        return copy


@runtime_checkable
class BenchmarkScenario(Protocol):
    id: str
    name: str
    category: str
    kind: BenchmarkKind
    description: str
    default_params: dict[str, Any]
    requires_network: bool

    async def prepare(self, ctx: BenchmarkContext) -> None:
        """Create untimed fixture state before the measured operation."""

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        """The measured operation. Runs once per iteration."""

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        """Release resources after timing; errors are reported, not raised."""


@dataclass(frozen=True)
class ScenarioInfo:
    id: str
    name: str
    category: str
    kind: str
    description: str
    requires_network: bool
    default_params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "kind": self.kind,
            "description": self.description,
            "requires_network": self.requires_network,
            "default_params": self.default_params,
        }
